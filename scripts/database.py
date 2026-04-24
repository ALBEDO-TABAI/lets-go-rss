import re
import sqlite3
import threading
import time
from typing import List, Optional, Dict, Any
from datetime import datetime
from email.utils import parsedate_to_datetime
import json

_ISO_DATE_RE = re.compile(r'^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}')

# Module-level lock: serialises ALL SQLite opens / writes so that
# Playwright's Chromium sub-process can never hold a WAL shm-lock
# while another thread tries to open the same database.
_DB_LOCK = threading.Lock()


class RSSDatabase:
    def __init__(self, db_path: str = "rss_database.db"):
        self.db_path = db_path
        self.init_database()

    def _connect(self, retries: int = 3):
        """Create a SQLite connection with WAL mode, busy timeout, and retry."""
        last_err = None
        for attempt in range(retries):
            try:
                with _DB_LOCK:
                    conn = sqlite3.connect(self.db_path, timeout=30)
                    conn.execute("PRAGMA journal_mode=WAL")
                    conn.execute("PRAGMA busy_timeout=30000")
                return conn
            except sqlite3.OperationalError as e:
                last_err = e
                if attempt < retries - 1:
                    time.sleep(0.5 * (attempt + 1))
        raise last_err

    def init_database(self):
        """Initialize database schema + idempotent migrations."""
        with self._connect() as conn:
            cursor = conn.cursor()

            # Subscriptions table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS subscriptions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    url TEXT UNIQUE NOT NULL,
                    platform TEXT NOT NULL,
                    title TEXT,
                    description TEXT,
                    added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    last_updated TIMESTAMP,
                    active INTEGER DEFAULT 1
                )
            """)

            # Items table - stores all fetched items
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id TEXT UNIQUE NOT NULL,
                    subscription_id INTEGER NOT NULL,
                    title TEXT,
                    description TEXT,
                    link TEXT,
                    category TEXT,
                    pub_date TIMESTAMP,
                    fetched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    metadata TEXT,
                    FOREIGN KEY (subscription_id) REFERENCES subscriptions(id)
                )
            """)

            # Create indices for faster lookups
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_item_id ON items(item_id)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_subscription_id ON items(subscription_id)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_category ON items(category)")

            # --- Migrations (idempotent) ---
            self._migrate_subscriptions(cursor)

            conn.commit()

    @staticmethod
    def _column_names(cursor, table: str) -> set:
        cursor.execute(f"PRAGMA table_info({table})")
        return {row[1] for row in cursor.fetchall()}

    def _migrate_subscriptions(self, cursor) -> None:
        cols = self._column_names(cursor, "subscriptions")
        if "consecutive_failures" not in cols:
            cursor.execute(
                "ALTER TABLE subscriptions ADD COLUMN consecutive_failures INTEGER DEFAULT 0"
            )
        if "last_error" not in cols:
            cursor.execute(
                "ALTER TABLE subscriptions ADD COLUMN last_error TEXT"
            )
        if "last_error_kind" not in cols:
            cursor.execute(
                "ALTER TABLE subscriptions ADD COLUMN last_error_kind TEXT"
            )
        if "last_success_at" not in cols:
            cursor.execute(
                "ALTER TABLE subscriptions ADD COLUMN last_success_at TIMESTAMP"
            )

    def record_fetch_outcome(self, subscription_id: int, *,
                             success: bool, error: Optional[str] = None,
                             error_kind: Optional[str] = None) -> None:
        """Update a subscription's health signals after a fetch attempt.

        - On success: clear consecutive_failures, clear last_error, bump
          last_success_at.
        - On failure: increment consecutive_failures, record error + kind.
        """
        now = datetime.now().isoformat()
        with self._connect() as conn:
            cur = conn.cursor()
            if success:
                cur.execute(
                    "UPDATE subscriptions "
                    "SET consecutive_failures=0, last_error=NULL, last_error_kind=NULL, "
                    "    last_success_at=?, last_updated=? "
                    "WHERE id=?",
                    (now, now, subscription_id),
                )
            else:
                cur.execute(
                    "UPDATE subscriptions "
                    "SET consecutive_failures=COALESCE(consecutive_failures, 0) + 1, "
                    "    last_error=?, last_error_kind=?, last_updated=? "
                    "WHERE id=?",
                    (error, error_kind, now, subscription_id),
                )
            conn.commit()

    def add_subscription(self, url: str, platform: str, title: str = "", description: str = "") -> int:
        """Add a new subscription"""
        with self._connect() as conn:
            cursor = conn.cursor()
            try:
                cursor.execute("""
                    INSERT INTO subscriptions (url, platform, title, description)
                    VALUES (?, ?, ?, ?)
                """, (url, platform, title, description))
                conn.commit()
                return cursor.lastrowid
            except sqlite3.IntegrityError:
                # Subscription already exists, return existing id
                cursor.execute("SELECT id FROM subscriptions WHERE url = ?", (url,))
                return cursor.fetchone()[0]

    def get_subscriptions(self, active_only: bool = True) -> List[Dict[str, Any]]:
        """Get all subscriptions"""
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            if active_only:
                cursor.execute("SELECT * FROM subscriptions WHERE active = 1")
            else:
                cursor.execute("SELECT * FROM subscriptions")

            return [dict(row) for row in cursor.fetchall()]

    def item_exists(self, item_id: str) -> bool:
        """Check if an item has already been fetched"""
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT 1 FROM items WHERE item_id = ?", (item_id,))
            return cursor.fetchone() is not None

    @staticmethod
    def _normalize_date(date_str: Optional[str]) -> str:
        """Normalize any date format to ISO 8601 (YYYY-MM-DDTHH:MM:SS)."""
        if not date_str:
            return datetime.now().isoformat()
        # Already ISO 8601? (e.g. 2026-02-11T09:00:00)
        if _ISO_DATE_RE.match(date_str):
            return date_str
        # Try RFC 822 (e.g. 'Wed, 11 Feb 2026 02:07:30 GMT')
        try:
            dt = parsedate_to_datetime(date_str)
            return dt.isoformat()
        except Exception:
            pass
        # Fallback: store as-is
        return date_str

    def add_item(self, item_id: str, subscription_id: int, title: str,
                 description: str = "", link: str = "", category: str = "",
                 pub_date: Optional[str] = None, metadata: Optional[Dict] = None) -> bool:
        """Add a new item. Uses INSERT OR IGNORE for thread-safe dedup."""
        normalized_date = self._normalize_date(pub_date)

        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT OR IGNORE INTO items (item_id, subscription_id, title,
                    description, link, category, pub_date, metadata)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (item_id, subscription_id, title, description, link, category,
                  normalized_date,
                  json.dumps(metadata) if metadata else None))
            conn.commit()
            return cursor.rowcount > 0

    def get_items_by_category(self, category: Optional[str] = None,
                              since: Optional[str] = None) -> List[Dict[str, Any]]:
        """Get items, optionally filtered by category and date"""
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            query = "SELECT * FROM items WHERE 1=1"
            params = []

            if category:
                query += " AND category = ?"
                params.append(category)

            if since:
                query += " AND fetched_at >= ?"
                params.append(since)

            query += " ORDER BY pub_date DESC"

            cursor.execute(query, params)
            return [dict(row) for row in cursor.fetchall()]

    def get_new_items_since(self, since: str) -> List[Dict[str, Any]]:
        """Get all items fetched since a specific timestamp"""
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("""
                SELECT i.*, s.platform, s.url as subscription_url
                FROM items i
                JOIN subscriptions s ON i.subscription_id = s.id
                WHERE i.fetched_at >= ?
                ORDER BY i.category, i.pub_date DESC
            """, (since,))
            return [dict(row) for row in cursor.fetchall()]

    def get_latest_per_subscription(self) -> list:
        """Get the single latest item for each subscription, sorted by pub_date DESC."""
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("""
                SELECT i.*, s.platform, s.url as subscription_url, s.title as subscription_title
                FROM items i
                JOIN subscriptions s ON i.subscription_id = s.id
                WHERE i.id = (
                    SELECT i2.id FROM items i2
                    WHERE i2.subscription_id = i.subscription_id
                    ORDER BY i2.pub_date DESC
                    LIMIT 1
                )
                ORDER BY i.pub_date DESC
            """)
            return [dict(row) for row in cursor.fetchall()]

    def update_subscription_timestamp(self, subscription_id: int):
        """Update the last_updated timestamp for a subscription"""
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE subscriptions
                SET last_updated = CURRENT_TIMESTAMP
                WHERE id = ?
            """, (subscription_id,))
            conn.commit()

    def update_subscription_title(self, subscription_id: int, title: str):
        """Auto-update subscription title from feed channel name."""
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE subscriptions SET title = ?
                WHERE id = ? AND (title LIKE '%Subscription%' OR title = '')
            """, (title, subscription_id))
            conn.commit()

    def get_all_items(self) -> List[Dict[str, Any]]:
        """Get all items for RSS feed generation"""
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("""
                SELECT i.*, s.platform
                FROM items i
                JOIN subscriptions s ON i.subscription_id = s.id
                ORDER BY i.pub_date DESC
                LIMIT 1000
            """)
            return [dict(row) for row in cursor.fetchall()]
