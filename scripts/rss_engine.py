#!/usr/bin/env python3
"""
Universal RSS Engine
A powerful RSS aggregator with AI-powered categorization
"""

import argparse
import sys
import os
import time
from datetime import datetime
from typing import List, Dict, Any
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager

from database import RSSDatabase
from scrapers import ScraperFactory
from classifier import get_classifier
from rss_generator import RSSGenerator, OPMLGenerator
from report_generator import MarkdownReportGenerator


def classify_fetch_error(err: str) -> str:
    """Bucket a free-form scraper error into a short kind tag used for health
    tracking and adaptive retry decisions."""
    e = (err or "").lower()
    if "cookies expired" in e or "api key" in e or "401" in e or "403" in e:
        return "auth"
    if "429" in e or "too many requests" in e or "rate limit" in e:
        return "rate_limit"
    if "connection refused" in e or "errno 61" in e or "timeout" in e or "timed out" in e:
        return "network"
    if "503" in e or "service unavailable" in e or "风控" in e or "captcha" in e or "waf" in e:
        return "upstream_block"
    if "non-rss" in e or "parse" in e or "json" in e:
        return "parse"
    return "other"


@contextmanager
def update_lock(lock_path: str):
    """Ensure only one update job runs at a time."""
    lock_file = None
    try:
        lock_file = open(lock_path, "a+", encoding="utf-8")
        try:
            import fcntl  # Unix only
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError(f"Another update is already running (lock: {lock_path})")
        except ImportError:
            # Non-Unix platforms: proceed without advisory lock.
            pass

        lock_file.seek(0)
        lock_file.truncate()
        lock_file.write(f"pid={os.getpid()} started={datetime.now().isoformat()}\n")
        lock_file.flush()
        yield
    finally:
        if lock_file:
            try:
                import fcntl
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass
            lock_file.close()


class RSSEngine:
    """Main RSS Engine class"""

    def __init__(self, db_path: str = "rss_database.db", use_llm: bool = True):
        self.db = RSSDatabase(db_path)
        self.scraper_factory = ScraperFactory()
        self._use_llm = use_llm
        self._classifier = None  # lazy init
        self.rss_generator = RSSGenerator()
        self.report_generator = MarkdownReportGenerator()

    @property
    def classifier(self):
        """Lazy-load classifier only when needed."""
        if self._classifier is None:
            self._classifier = get_classifier(self._use_llm)
        return self._classifier

    def add_subscription(self, url: str) -> bool:
        """Add a new subscription"""
        print(f"\n🔍 Analyzing URL: {url}")

        # Detect platform
        platform = self.scraper_factory.detect_platform(url)

        if platform == "unknown":
            print("❌ Error: Unsupported platform")
            print("Supported platforms: Bilibili, Xiaohongshu, Weibo, YouTube, Vimeo, Behance, Douyin")
            return False

        print(f"✓ Detected platform: {platform.title()}")

        # Add to database
        subscription_id = self.db.add_subscription(
            url=url,
            platform=platform,
            title=f"{platform.title()} Subscription",
            description=f"Content from {platform}"
        )

        print(f"✓ Subscription added with ID: {subscription_id}")

        # Try to fetch initial content
        print(f"\n📥 Fetching initial content...")
        initial_fetch_ok = True
        try:
            self._fetch_subscription(subscription_id, url, platform)
        except Exception as e:
            print(f"\n⚠️  Initial fetch failed: {e}")
            initial_fetch_ok = False

        if initial_fetch_ok:
            print("\n✅ Subscription added successfully!")
            return True
        else:
            print("\n⚠️  Subscription added, but initial fetch failed. Will retry on next update.")
            return True

    def update_all(self, use_classification: bool = True, digest: bool = False) -> Dict[str, Any]:
        """Update all subscriptions in parallel."""
        started_at = datetime.now()
        print(f"\n🔄 Starting RSS update... [{started_at.strftime('%Y-%m-%d %H:%M:%S')}]")

        subscriptions = self.db.get_subscriptions()

        if not subscriptions:
            print("⚠️  No subscriptions found. Use --add to add subscriptions first.")
            return {"new_items": [], "total_subscriptions": 0}

        print(f"📋 Found {len(subscriptions)} active subscriptions")
        max_workers = max(1, int(os.environ.get("RSS_MAX_WORKERS", "5")))
        print(f"⚡ Fetching in parallel... (workers={max_workers})\n")

        # Track update start time
        update_start = datetime.now().isoformat()
        t0 = time.time()
        all_new_items = []
        results = {}  # sub_id -> (count, error)
        error_rows = []

        # Parallel fetch all subscriptions
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_sub = {}
            for sub in subscriptions:
                future = executor.submit(
                    self._fetch_subscription,
                    sub["id"], sub["url"], sub["platform"], use_classification,
                    int(sub.get("consecutive_failures") or 0),
                )
                future_to_sub[future] = sub

            for future in as_completed(future_to_sub):
                sub = future_to_sub[future]
                platform = sub["platform"].title()
                try:
                    # as_completed() yields only finished futures; no extra timeout needed here.
                    new_items = future.result()
                    if new_items:
                        all_new_items.extend(new_items)
                        results[sub["id"]] = (len(new_items), None)
                        print(f"  ✓ {platform}: +{len(new_items)} new")
                    else:
                        results[sub["id"]] = (0, None)
                        print(f"  → {platform}: no new items")
                    self.db.record_fetch_outcome(sub["id"], success=True)
                except Exception as e:
                    err_msg = str(e)
                    results[sub["id"]] = (0, err_msg)
                    print(f"  ❌ {platform}: {err_msg[:80]}")
                    error_rows.append({
                        "platform": sub["platform"],
                        "url": sub["url"],
                        "error": err_msg,
                    })
                    self.db.record_fetch_outcome(
                        sub["id"], success=False,
                        error=err_msg, error_kind=classify_fetch_error(err_msg),
                    )

        elapsed = time.time() - t0
        total_new = sum(r[0] for r in results.values())
        errors = sum(1 for r in results.values() if r[1])
        ended_at = datetime.now()
        print(f"\n✅ Done in {elapsed:.1f}s | +{total_new} new | {errors} errors")
        print(f"🕒 Window: {started_at.strftime('%H:%M:%S')} -> {ended_at.strftime('%H:%M:%S')}\n")
        if error_rows:
            print("⚠️  Error summary:")
            for row in error_rows:
                print(f"  - {row['platform'].title()}: {row['error'][:100]}")
                print(f"    {row['url']}")
            print("")

        # Output directory = same dir as database (assets/)
        out_dir = os.path.dirname(self.db.db_path) or "."

        # Persist per-source run health so summary.md can surface it.
        try:
            import json as _json
            per_source = {}
            for sub in subscriptions:
                count, err = results.get(sub["id"], (0, None))
                per_source[sub["url"]] = {
                    "platform": sub["platform"],
                    "title": sub.get("title") or sub["platform"].title(),
                    "status": "error" if err else ("ok" if count else "no_new"),
                    "error": err,
                    "new_count": count,
                }
            health_path = os.path.join(out_dir, ".last_run_health.json")
            with open(health_path, "w", encoding="utf-8") as f:
                _json.dump({
                    "generated_at": ended_at.isoformat(),
                    "elapsed_sec": round(elapsed, 1),
                    "total_new": total_new,
                    "errors": errors,
                    "per_source": per_source,
                }, f, ensure_ascii=False, indent=2)
        except Exception as _e:
            print(f"  ⚠️  Failed to write run health: {_e}")

        # Generate RSS feeds
        print("📝 Generating outputs...")
        all_items = self.db.get_all_items()
        feed_paths = self.rss_generator.create_categorized_feeds(all_items, out_dir)
        print(f"✓ {len(feed_paths)} RSS feeds")

        opml_gen = OPMLGenerator()
        opml_gen.create_opml(subscriptions, os.path.join(out_dir, "subscriptions.opml"))
        print("✓ OPML")

        if digest:
            # Digest mode: always show latest 1 item per subscription (by pub_date)
            report_items = self.db.get_latest_per_subscription()
        else:
            # Full mode: show only items fetched in this update cycle
            report_items = self.db.get_new_items_since(update_start)
        self.report_generator.generate_update_report(report_items, os.path.join(out_dir, "latest_update.md"), digest=digest)
        print("✓ latest_update.md (增量)")

        if digest:
            self.report_generator.generate_full_overview(report_items, os.path.join(out_dir, "full_overview.md"))
            print("✓ full_overview.md (全量)")

        self.report_generator.generate_summary_report(self.db, os.path.join(out_dir, "summary.md"))
        print("✓ summary.md")

        return {
            "new_items": all_new_items,
            "total_subscriptions": len(subscriptions),
            "feed_paths": feed_paths
        }

    def _fetch_subscription(self, subscription_id: int, url: str, platform: str,
                           use_classification: bool = True,
                           consecutive_failures: int = 0) -> List[Dict[str, Any]]:
        """Fetch content from a subscription"""

        # Get scraper
        scraper = self.scraper_factory.get_scraper(platform)
        if not scraper:
            raise ValueError(f"No scraper available for platform: {platform}")

        # Adaptive budget: sources that have been failing repeatedly get a
        # tight timeout + no retry, so a single bad source can't blow up the
        # overall run time. BaseScraper.get honors this attribute.
        scraper._adaptive_health_hint = consecutive_failures

        # Fetch items
        items = scraper.fetch_items(url)

        if not items:
            # Only raise if scraper recorded a real error (not just "no content")
            scraper_error = getattr(scraper, "last_error", None)
            if scraper_error:
                raise RuntimeError(scraper_error)
            return []

        # Auto-update subscription title from feed channel name
        first_meta = items[0].get("metadata", {}) or {}
        channel_name = first_meta.get("_channel_title") or first_meta.get("channel") or ""
        if channel_name:
            # Clean up platform-specific suffixes
            import re as _re
            channel_name = _re.sub(r'\s*的\s*bilibili\s*空间$', '', channel_name)
            channel_name = _re.sub(r'\s*的微博$', '', channel_name)
            channel_name = _re.sub(r'^Vimeo\s*/\s*', '', channel_name)
            channel_name = _re.sub(r"['\u2019]s\s*videos$", '', channel_name)
            channel_name = channel_name.strip()
        if channel_name:
            self.db.update_subscription_title(subscription_id, channel_name)

        # Filter out existing items and classify new ones
        new_items = []

        for item in items:
            item_id = item.get("item_id")
            if not item_id:
                continue

            # Fast path: avoid unnecessary classification work for existing items.
            # INSERT OR IGNORE in add_item() still protects against race conditions.
            if self.db.item_exists(item_id):
                continue

            if use_classification:
                # classify_item never raises — it has a keyword fallback internally
                item["category"] = self.classifier.classify_item(
                    item.get("title", ""),
                    item.get("description", ""),
                )
            else:
                item["category"] = "其他"

            # Atomic insert — INSERT OR IGNORE handles dedup
            added = self.db.add_item(
                item_id=item_id,
                subscription_id=subscription_id,
                title=item.get("title", ""),
                description=item.get("description", ""),
                link=item.get("link", ""),
                category=item.get("category", "其他"),
                pub_date=item.get("pub_date"),
                metadata=item.get("metadata")
            )

            if added:
                new_items.append(item)

        return new_items

    def list_subscriptions(self):
        """List all subscriptions"""
        subscriptions = self.db.get_subscriptions()

        if not subscriptions:
            print("No subscriptions found.")
            return

        print("\n📚 Subscriptions:\n")

        for sub in subscriptions:
            print(f"ID: {sub['id']}")
            print(f"Platform: {sub['platform']}")
            print(f"URL: {sub['url']}")
            print(f"Added: {sub['added_at']}")
            print(f"Last Updated: {sub.get('last_updated', 'Never')}")
            print("-" * 60)

    def show_stats(self):
        """Show statistics"""
        subscriptions = self.db.get_subscriptions()
        all_items = self.db.get_all_items()

        print("\n📊 Statistics:\n")
        print(f"Total Subscriptions: {len(subscriptions)}")
        print(f"Total Items: {len(all_items)}")

        # Category breakdown
        categories = {}
        for item in all_items:
            cat = item.get("category", "其他")
            categories[cat] = categories.get(cat, 0) + 1

        print("\nCategory Breakdown:")
        for cat, count in sorted(categories.items(), key=lambda x: x[1], reverse=True):
            print(f"  {cat}: {count}")


def main(db_path: str = None):
    """Main CLI entry point"""

    parser = argparse.ArgumentParser(
        description="Universal RSS Engine - AI-powered content aggregator",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Add a subscription
  python rss_engine.py --add "https://space.bilibili.com/123456"

  # Update all subscriptions
  python rss_engine.py --update

  # Update without LLM classification
  python rss_engine.py --update --no-llm

  # List subscriptions
  python rss_engine.py --list

  # Show statistics
  python rss_engine.py --stats
        """
    )

    parser.add_argument("--add", metavar="URL", help="Add a new subscription")
    parser.add_argument("--update", action="store_true", help="Update all subscriptions")
    parser.add_argument("--status", action="store_true", help="Read cached report (for bot push, no fetching)")
    parser.add_argument("--list", action="store_true", help="List all subscriptions")
    parser.add_argument("--stats", action="store_true", help="Show statistics")
    parser.add_argument("--no-llm", action="store_true", help="Disable LLM classification")
    parser.add_argument("--digest", action="store_true", help="Digest mode: show only latest 1 item per account")
    parser.add_argument("--overview", action="store_true", help="Print full overview (all accounts, latest item each)")
    parser.add_argument("--db", default="rss_database.db", help="Database path (default: rss_database.db)")

    args = parser.parse_args()

    # Check if any action specified
    if not any([args.add, args.update, args.status, args.list, args.stats, args.overview]):
        parser.print_help()
        return

    # --status is a fast path: just read cached file, no engine needed
    if args.status:
        report_path = os.path.join(os.path.dirname(db_path or args.db) or ".", "latest_update.md")
        if os.path.exists(report_path):
            with open(report_path, "r", encoding="utf-8") as f:
                print(f.read())
        else:
            print("⚠️ 尚无缓存报告。请先运行 --update 生成。")
        return

    # --overview is also a fast path: read full_overview.md
    if args.overview:
        overview_path = os.path.join(os.path.dirname(db_path or args.db) or ".", "full_overview.md")
        if os.path.exists(overview_path):
            with open(overview_path, "r", encoding="utf-8") as f:
                print(f.read())
        else:
            print("⚠️ 尚无全量概览。请先运行 --update --digest 生成。")
        return

    # Initialize engine
    use_llm = not args.no_llm
    actual_db_path = db_path or args.db
    engine = RSSEngine(db_path=actual_db_path, use_llm=use_llm)

    # Execute actions
    try:
        if args.add:
            engine.add_subscription(args.add)

        if args.update:
            lock_path = os.path.join(os.path.dirname(actual_db_path) or ".", ".update.lock")
            with update_lock(lock_path):
                engine.update_all(use_classification=use_llm, digest=args.digest)

        if args.list:
            engine.list_subscriptions()

        if args.stats:
            engine.show_stats()

    except KeyboardInterrupt:
        print("\n\n⚠️  Operation cancelled by user")
        sys.exit(1)
    except Exception as e:
        if "Another update is already running" in str(e):
            print(f"\n⚠️  {e}")
            return
        print(f"\n❌ Error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
