"""
RSS feed generator
Creates standard RSS 2.0 XML files from database items
"""

from typing import List, Dict, Any
from datetime import datetime
import xml.etree.ElementTree as ET
from xml.dom import minidom


class RSSGenerator:
    """Generate RSS 2.0 XML feeds"""

    def __init__(self, title: str = "Universal RSS Feed",
                 description: str = "Aggregated content from multiple platforms",
                 link: str = "https://localhost"):
        self.feed_title = title
        self.feed_description = description
        self.feed_link = link

    def create_feed(self, items: List[Dict[str, Any]], output_path: str = "feed.xml"):
        """Generate RSS feed from items"""

        # Create root RSS element
        rss = ET.Element("rss", {
            "version": "2.0",
            "xmlns:atom": "http://www.w3.org/2005/Atom",
            "xmlns:content": "http://purl.org/rss/1.0/modules/content/"
        })

        # Create channel
        channel = ET.SubElement(rss, "channel")

        # Add channel metadata
        ET.SubElement(channel, "title").text = self.feed_title
        ET.SubElement(channel, "link").text = self.feed_link
        ET.SubElement(channel, "description").text = self.feed_description
        ET.SubElement(channel, "language").text = "zh-CN"
        ET.SubElement(channel, "lastBuildDate").text = self._format_date(datetime.now())

        # Add atom:link for self-reference
        ET.SubElement(channel, "{http://www.w3.org/2005/Atom}link", {
            "href": f"{self.feed_link}/feed.xml",
            "rel": "self",
            "type": "application/rss+xml"
        })

        # Add items
        for item_data in items:
            item = ET.SubElement(channel, "item")

            # Required elements
            ET.SubElement(item, "title").text = item_data.get("title", "")
            ET.SubElement(item, "link").text = item_data.get("link", "")

            # Optional elements
            description = item_data.get("description", "")
            if description:
                ET.SubElement(item, "description").text = description

            # Category
            category = item_data.get("category", "")
            if category:
                ET.SubElement(item, "category").text = category

            # Pub date
            pub_date = item_data.get("pub_date")
            if pub_date:
                formatted_date = self._format_date(pub_date)
                if formatted_date:
                    ET.SubElement(item, "pubDate").text = formatted_date

            # GUID (unique identifier)
            guid = item_data.get("item_id", item_data.get("link", ""))
            ET.SubElement(item, "guid", {"isPermaLink": "false"}).text = guid

            # Platform as custom element
            platform = item_data.get("platform", "")
            if platform:
                ET.SubElement(item, "source").text = platform

        # Pretty print and save
        xml_string = self._prettify(rss)

        with open(output_path, "w", encoding="utf-8") as f:
            f.write(xml_string)

        return output_path

    def create_categorized_feeds(self, items: List[Dict[str, Any]], output_dir: str = "."):
        """Generate separate RSS feeds for each category"""
        # Category name mapping: Chinese to English
        category_mapping = {
            "科技": "tech",
            "人文": "humanities",
            "设计": "design",
            "娱乐": "entertainment",
            "其他": "others"
        }

        # Initialize all categories with empty lists
        categories = {cat: [] for cat in category_mapping.keys()}

        # Group items by category
        for item in items:
            category = item.get("category", "其他")
            if category in categories:
                categories[category].append(item)
            else:
                # If unknown category, add to "其他"
                categories["其他"].append(item)

        # Generate feed for each category (including empty ones)
        feed_paths = {}
        for category, category_items in categories.items():
            # Use English filename
            english_name = category_mapping.get(category, "others")
            output_path = f"{output_dir}/{english_name}_feed.xml"
            self.feed_title = f"Universal RSS - {category}"
            self.feed_description = f"{category}类内容聚合"
            # Generate feed even if empty
            self.create_feed(category_items, output_path)
            feed_paths[category] = output_path

        # Also create master feed with all items
        self.feed_title = "Universal RSS Feed"
        self.feed_description = "Aggregated content from multiple platforms"
        master_path = f"{output_dir}/feed.xml"
        self.create_feed(items, master_path)
        feed_paths["master"] = master_path

        return feed_paths

    def _format_date(self, date_input) -> str:
        """Format date to RFC 822 format for RSS"""
        try:
            if isinstance(date_input, str):
                # Try to parse ISO format
                try:
                    dt = datetime.fromisoformat(date_input.replace("Z", "+00:00"))
                except:
                    # Try other common formats
                    dt = datetime.strptime(date_input, "%Y-%m-%d %H:%M:%S")
            elif isinstance(date_input, datetime):
                dt = date_input
            else:
                return ""

            # Format to RFC 822
            return dt.strftime("%a, %d %b %Y %H:%M:%S +0000")
        except:
            return ""

    def _escape_text(self, text: str) -> str:
        """Return text as-is. XML escaping is handled by ElementTree."""
        return text if text else ""

    def _prettify(self, elem: ET.Element) -> str:
        """Return a pretty-printed XML string"""
        rough_string = ET.tostring(elem, encoding="utf-8")
        reparsed = minidom.parseString(rough_string)
        return reparsed.toprettyxml(indent="  ", encoding="utf-8").decode("utf-8")


class OPMLGenerator:
    """Generate OPML file for subscription management"""

    def __init__(self, title: str = "Universal RSS Subscriptions"):
        self.title = title

    def create_opml(self, subscriptions: List[Dict[str, Any]], output_path: str = "subscriptions.opml"):
        """Generate OPML from subscriptions"""

        opml = ET.Element("opml", {"version": "2.0"})

        # Head
        head = ET.SubElement(opml, "head")
        ET.SubElement(head, "title").text = self.title
        ET.SubElement(head, "dateCreated").text = datetime.now().strftime("%a, %d %b %Y %H:%M:%S +0000")

        # Body
        body = ET.SubElement(opml, "body")

        # Group by platform
        platforms = {}
        for sub in subscriptions:
            platform = sub.get("platform", "other")
            if platform not in platforms:
                platforms[platform] = []
            platforms[platform].append(sub)

        # Create outline for each platform
        for platform, subs in platforms.items():
            platform_outline = ET.SubElement(body, "outline", {
                "text": platform.title(),
                "title": platform.title()
            })

            for sub in subs:
                ET.SubElement(platform_outline, "outline", {
                    "type": "rss",
                    "text": sub.get("title", sub.get("url", "")),
                    "title": sub.get("title", sub.get("url", "")),
                    "xmlUrl": sub.get("url", ""),
                    "htmlUrl": sub.get("url", "")
                })

        # Save
        xml_string = ET.tostring(opml, encoding="utf-8")
        reparsed = minidom.parseString(xml_string)
        pretty_xml = reparsed.toprettyxml(indent="  ", encoding="utf-8").decode("utf-8")

        with open(output_path, "w", encoding="utf-8") as f:
            f.write(pretty_xml)

        return output_path
