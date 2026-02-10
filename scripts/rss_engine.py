#!/usr/bin/env python3
"""
Universal RSS Engine
A powerful RSS aggregator with AI-powered categorization
"""

import argparse
import sys
import os
from datetime import datetime
from typing import List, Dict, Any

from database import RSSDatabase
from scrapers import ScraperFactory
from classifier import get_classifier
from rss_generator import RSSGenerator, OPMLGenerator
from report_generator import MarkdownReportGenerator


class RSSEngine:
    """Main RSS Engine class"""

    def __init__(self, db_path: str = "rss_database.db", use_llm: bool = True):
        self.db = RSSDatabase(db_path)
        self.scraper_factory = ScraperFactory()
        self.classifier = get_classifier(use_llm)
        self.rss_generator = RSSGenerator()
        self.report_generator = MarkdownReportGenerator()

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
        success = self._fetch_subscription(subscription_id, url, platform)

        if success:
            print("\n✅ Subscription added successfully!")
            return True
        else:
            print("\n⚠️  Subscription added, but initial fetch failed. Will retry on next update.")
            return True

    def update_all(self, use_classification: bool = True, digest: bool = False) -> Dict[str, Any]:
        """Update all subscriptions"""
        print("\n🔄 Starting RSS update...")

        subscriptions = self.db.get_subscriptions()

        if not subscriptions:
            print("⚠️  No subscriptions found. Use --add to add subscriptions first.")
            return {"new_items": [], "total_subscriptions": 0}

        print(f"📋 Found {len(subscriptions)} active subscriptions\n")

        # Track update start time
        update_start = datetime.now().isoformat()
        new_items_count = 0
        all_new_items = []

        # Process each subscription
        for i, sub in enumerate(subscriptions, 1):
            sub_id = sub["id"]
            url = sub["url"]
            platform = sub["platform"]
            title = sub.get("title", url)

            print(f"[{i}/{len(subscriptions)}] {platform.title()}: {title}")

            try:
                new_items = self._fetch_subscription(sub_id, url, platform, use_classification)
                if new_items:
                    new_items_count += len(new_items)
                    all_new_items.extend(new_items)
                    print(f"  ✓ Added {len(new_items)} new items")
                else:
                    print(f"  → No new items")

                # Update subscription timestamp
                self.db.update_subscription_timestamp(sub_id)

            except Exception as e:
                print(f"  ❌ Error: {e}")

            print()

        print(f"✅ Update complete! Added {new_items_count} new items\n")

        # Generate RSS feeds
        print("📝 Generating RSS feeds...")
        all_items = self.db.get_all_items()
        feed_paths = self.rss_generator.create_categorized_feeds(all_items, ".")
        print(f"✓ Generated {len(feed_paths)} RSS feeds")

        # Generate OPML
        opml_gen = OPMLGenerator()
        opml_gen.create_opml(subscriptions, "subscriptions.opml")
        print("✓ Generated OPML file")

        # Generate markdown report
        print("\n📊 Generating update report...")
        new_items_with_details = self.db.get_new_items_since(update_start)
        self.report_generator.generate_update_report(new_items_with_details, "latest_update.md", digest=digest)
        print("✓ Generated latest_update.md")

        # Generate summary report
        self.report_generator.generate_summary_report(self.db, "summary.md")
        print("✓ Generated summary.md")

        return {
            "new_items": all_new_items,
            "total_subscriptions": len(subscriptions),
            "feed_paths": feed_paths
        }

    def _fetch_subscription(self, subscription_id: int, url: str, platform: str,
                           use_classification: bool = True) -> List[Dict[str, Any]]:
        """Fetch content from a subscription"""

        # Get scraper
        scraper = self.scraper_factory.get_scraper(platform)
        if not scraper:
            raise ValueError(f"No scraper available for platform: {platform}")

        # Fetch items
        items = scraper.fetch_items(url)

        if not items:
            return []

        # Filter out existing items and classify new ones
        new_items = []

        for item in items:
            item_id = item.get("item_id")

            # Skip if already exists
            if self.db.item_exists(item_id):
                continue

            # Classify item
            if use_classification:
                try:
                    category = self.classifier.classify_item(
                        item.get("title", ""),
                        item.get("description", "")
                    )
                    item["category"] = category
                except Exception as e:
                    print(f"  ⚠️  Classification error: {e}")
                    item["category"] = "其他"
            else:
                item["category"] = "其他"

            # Add to database
            self.db.add_item(
                item_id=item_id,
                subscription_id=subscription_id,
                title=item.get("title", ""),
                description=item.get("description", ""),
                link=item.get("link", ""),
                category=item.get("category", "其他"),
                pub_date=item.get("pub_date"),
                metadata=item.get("metadata")
            )

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
    parser.add_argument("--list", action="store_true", help="List all subscriptions")
    parser.add_argument("--stats", action="store_true", help="Show statistics")
    parser.add_argument("--no-llm", action="store_true", help="Disable LLM classification")
    parser.add_argument("--digest", action="store_true", help="Digest mode: show only latest 1 item per account")
    parser.add_argument("--db", default="rss_database.db", help="Database path (default: rss_database.db)")

    args = parser.parse_args()

    # Check if any action specified
    if not any([args.add, args.update, args.list, args.stats]):
        parser.print_help()
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
            engine.update_all(use_classification=use_llm, digest=args.digest)

        if args.list:
            engine.list_subscriptions()

        if args.stats:
            engine.show_stats()

    except KeyboardInterrupt:
        print("\n\n⚠️  Operation cancelled by user")
        sys.exit(1)
    except Exception as e:
        print(f"\n❌ Error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
