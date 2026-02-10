"""
SQLite database manager for RSS engine
Tracks fetched items to support incremental updates
"""

import sqlite3
from typing import List, Optional, Dict, Any
from datetime import datetime
import json


class RSSDatabase:
    def __init__(self, db_path: str = "rss_database.db"):
        self.db_path = db_path
        self.init_database()

    def init_database(self):
        """Initialize database schema"""
        with sqlite3.connect(self.db_path) as conn:
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

            conn.commit()

    def add_subscription(self, url: str, platform: str, title: str = "", description: str = "") -> int:
        """Add a new subscription"""
        with sqlite3.connect(self.db_path) as conn:
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
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            if active_only:
                cursor.execute("SELECT * FROM subscriptions WHERE active = 1")
            else:
                cursor.execute("SELECT * FROM subscriptions")

            return [dict(row) for row in cursor.fetchall()]

    def item_exists(self, item_id: str) -> bool:
        """Check if an item has already been fetched"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT 1 FROM items WHERE item_id = ?", (item_id,))
            return cursor.fetchone() is not None

    def add_item(self, item_id: str, subscription_id: int, title: str,
                 description: str = "", link: str = "", category: str = "",
                 pub_date: Optional[str] = None, metadata: Optional[Dict] = None) -> bool:
        """Add a new item if it doesn't exist"""
        if self.item_exists(item_id):
            return False

        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO items (item_id, subscription_id, title, description,
                                   link, category, pub_date, metadata)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (item_id, subscription_id, title, description, link, category,
                  pub_date or datetime.now().isoformat(),
                  json.dumps(metadata) if metadata else None))
            conn.commit()
            return True

    def get_items_by_category(self, category: Optional[str] = None,
                              since: Optional[str] = None) -> List[Dict[str, Any]]:
        """Get items, optionally filtered by category and date"""
        with sqlite3.connect(self.db_path) as conn:
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
        with sqlite3.connect(self.db_path) as conn:
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

    def update_subscription_timestamp(self, subscription_id: int):
        """Update the last_updated timestamp for a subscription"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE subscriptions
                SET last_updated = CURRENT_TIMESTAMP
                WHERE id = ?
            """, (subscription_id,))
            conn.commit()

    def get_all_items(self) -> List[Dict[str, Any]]:
        """Get all items for RSS feed generation"""
        with sqlite3.connect(self.db_path) as conn:
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
