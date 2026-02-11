"""
Lightweight scraper modules for various platforms.

Architecture:
- Tier 1 (Native RSS): Vimeo, Behance — direct RSS feed parsing
- Tier 1b (yt-dlp):    YouTube — uses yt-dlp for metadata extraction
- Tier 2 (RSSHub):     Bilibili, Weibo, Douyin, Xiaohongshu — via local RSSHub

Environment variables:
- RSSHUB_BASE_URL: Base URL of self-hosted RSSHub (default: http://localhost:1200)
"""

import re
import os
import json
import hashlib
import subprocess
import xml.etree.ElementTree as ET
from typing import List, Dict, Any, Optional
from datetime import datetime
import time
import httpx
from urllib.parse import urlparse, parse_qs


# ============================================================
#  Base classes
# ============================================================

class BaseScraper:
    """Base scraper with common HTTP functionality"""

    def __init__(self):
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
            "Connection": "keep-alive",
        }
        self.timeout = 15

    def get(self, url: str, headers: Optional[Dict] = None) -> httpx.Response:
        """Make GET request with retry logic"""
        request_headers = {**self.headers, **(headers or {})}
        for attempt in range(3):
            try:
                with httpx.Client(timeout=self.timeout, follow_redirects=True) as client:
                    response = client.get(url, headers=request_headers)
                    response.raise_for_status()
                    return response
            except Exception as e:
                if attempt == 2:
                    raise
                time.sleep(1 * (attempt + 1))


class NativeRSSScraper(BaseScraper):
    """Base class for scrapers that parse native RSS/Atom feeds"""

    def parse_rss_xml(self, xml_text: str, platform: str = "") -> List[Dict[str, Any]]:
        """Parse standard RSS 2.0 or Atom feed XML into item dicts"""
        items = []
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError as e:
            print(f"    ⚠️  XML parse error: {e}")
            return []

        # Atom feed (YouTube uses this)
        atom_ns = "http://www.w3.org/2005/Atom"
        media_ns = "http://search.yahoo.com/mrss/"

        if root.tag == f"{{{atom_ns}}}feed" or root.tag == "feed":
            # Extract feed/channel title
            feed_title_el = root.find(f"{{{atom_ns}}}title")
            channel_title = feed_title_el.text.strip() if feed_title_el is not None and feed_title_el.text else ""

            entries = root.findall(f"{{{atom_ns}}}entry")
            for entry in entries[:20]:
                title_el = entry.find(f"{{{atom_ns}}}title")
                link_el = entry.find(f"{{{atom_ns}}}link")
                published_el = entry.find(f"{{{atom_ns}}}published")
                updated_el = entry.find(f"{{{atom_ns}}}updated")
                summary_el = entry.find(f"{{{media_ns}}}group/{{{media_ns}}}description")

                title = title_el.text if title_el is not None and title_el.text else ""
                link = link_el.get("href", "") if link_el is not None else ""
                pub_date = (published_el.text if published_el is not None
                           else updated_el.text if updated_el is not None else "")
                description = summary_el.text if summary_el is not None and summary_el.text else ""

                item_id = f"{platform}_{hashlib.md5(link.encode()).hexdigest()[:12]}"
                items.append({
                    "item_id": item_id,
                    "title": title.strip(),
                    "description": description[:500] if description else "",
                    "link": link,
                    "pub_date": pub_date,
                    "metadata": {"_channel_title": channel_title}
                })
        else:
            # RSS 2.0 format
            channel = root.find("channel")
            # Extract channel title
            channel_title = ""
            if channel is not None:
                ch_title_el = channel.find("title")
                channel_title = ch_title_el.text.strip() if ch_title_el is not None and ch_title_el.text else ""
            item_elements = channel.findall("item") if channel is not None else root.findall(".//item")

            for item_el in item_elements[:20]:
                title_el = item_el.find("title")
                link_el = item_el.find("link")
                desc_el = item_el.find("description")
                pubdate_el = item_el.find("pubDate")
                guid_el = item_el.find("guid")

                title = title_el.text if title_el is not None and title_el.text else ""
                link = link_el.text if link_el is not None and link_el.text else ""
                description = desc_el.text if desc_el is not None and desc_el.text else ""
                pub_date = pubdate_el.text if pubdate_el is not None and pubdate_el.text else ""
                guid = guid_el.text if guid_el is not None and guid_el.text else link

                # Strip HTML tags from description for cleaner text
                clean_desc = re.sub(r'<[^>]+>', '', description)[:500]

                item_id = f"{platform}_{hashlib.md5(guid.encode()).hexdigest()[:12]}"
                items.append({
                    "item_id": item_id,
                    "title": title.strip(),
                    "description": clean_desc.strip(),
                    "link": link,
                    "pub_date": pub_date,
                    "metadata": {"_channel_title": channel_title}
                })

        return items


class RSSHubScraper(NativeRSSScraper):
    """Base class for scrapers that fetch via self-hosted RSSHub"""

    RSSHUB_BASE = os.environ.get("RSSHUB_BASE_URL", "http://localhost:1200")

    def __init__(self, route_template: str):
        super().__init__()
        self.route_template = route_template

    def extract_user_id(self, url: str) -> Optional[str]:
        """Override in subclass to extract platform-specific user ID"""
        raise NotImplementedError

    def fetch_items(self, url: str) -> List[Dict[str, Any]]:
        """Fetch via RSSHub route"""
        user_id = self.extract_user_id(url)
        if not user_id:
            print(f"    ⚠️  Cannot extract user ID from: {url}")
            return []

        rsshub_url = f"{self.RSSHUB_BASE}{self.route_template.format(id=user_id)}"
        print(f"    📡 RSSHub: {rsshub_url}")

        try:
            response = self.get(rsshub_url)
            ct = response.headers.get("content-type", "")
            if "xml" in ct or "rss" in ct:
                return self.parse_rss_xml(response.text, self._platform_name())
            else:
                # Might be an error page
                print(f"    ⚠️  RSSHub returned non-RSS content (HTTP {response.status_code})")
                return []
        except Exception as e:
            print(f"    ❌ RSSHub fetch failed: {e}")
            return []

    def _platform_name(self) -> str:
        return self.__class__.__name__.replace("Scraper", "").lower()


# ============================================================
#  Tier 1: Native RSS scrapers (zero maintenance)
# ============================================================

class VimeoScraper(NativeRSSScraper):
    """Vimeo scraper — uses native RSS feed at /{username}/videos/rss"""

    def extract_user_id(self, url: str) -> Optional[str]:
        """Extract username from Vimeo URL"""
        match = re.search(r"vimeo\.com/([^/?#]+)", url)
        return match.group(1) if match else None

    def fetch_items(self, url: str) -> List[Dict[str, Any]]:
        username = self.extract_user_id(url)
        if not username:
            return []

        rss_url = f"https://vimeo.com/{username}/videos/rss"
        print(f"    📡 Native RSS: {rss_url}")

        try:
            response = self.get(rss_url)
            return self.parse_rss_xml(response.text, "vimeo")
        except Exception as e:
            print(f"    ❌ Vimeo RSS fetch failed: {e}")
            return []


class BehanceScraper(NativeRSSScraper):
    """Behance scraper — uses native RSS feed at /feeds/user?username={user}"""

    def extract_user_id(self, url: str) -> Optional[str]:
        """Extract username from Behance URL"""
        match = re.search(r"behance\.net/([^/?#]+)", url)
        return match.group(1) if match else None

    def fetch_items(self, url: str) -> List[Dict[str, Any]]:
        username = self.extract_user_id(url)
        if not username:
            return []

        rss_url = f"https://www.behance.net/feeds/user?username={username}"
        print(f"    📡 Native RSS: {rss_url}")

        try:
            response = self.get(rss_url)
            return self.parse_rss_xml(response.text, "behance")
        except Exception as e:
            print(f"    ❌ Behance RSS fetch failed: {e}")
            return []


# ============================================================
#  Tier 1b: YouTube via yt-dlp (no API key needed)
# ============================================================

class YouTubeScraper(BaseScraper):
    """YouTube scraper — uses yt-dlp for reliable metadata extraction"""

    def extract_channel_id(self, url: str) -> Optional[str]:
        """Extract @handle or channel path from YouTube URL"""
        # Match @handle format
        match = re.search(r"youtube\.com/(@[\w-]+)", url)
        if match:
            return match.group(1)
        # Match /channel/ID format
        match = re.search(r"youtube\.com/channel/([\w-]+)", url)
        if match:
            return match.group(1)
        # Match /c/name format
        match = re.search(r"youtube\.com/c/([\w-]+)", url)
        if match:
            return match.group(1)
        return None

    def fetch_items(self, url: str) -> List[Dict[str, Any]]:
        channel_ref = self.extract_channel_id(url)
        if not channel_ref:
            return []

        # Construct videos URL
        if channel_ref.startswith("@"):
            videos_url = f"https://www.youtube.com/{channel_ref}/videos"
        else:
            videos_url = f"https://www.youtube.com/channel/{channel_ref}/videos"

        print(f"    📡 yt-dlp: {videos_url}")

        # Use --print to get structured fields including upload_date
        # --flat-playlist + --dump-json does NOT include upload_date
        SEPARATOR = "|||"
        PRINT_FORMAT = SEPARATOR.join([
            "%(id)s", "%(title)s", "%(upload_date)s",
            "%(duration)s", "%(view_count)s", "%(channel)s",
            "%(description).500s",
        ])

        try:
            result = subprocess.run(
                [
                    "yt-dlp",
                    "--print", PRINT_FORMAT,
                    "--playlist-items", "1:15",
                    "--no-warnings",
                    videos_url,
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )

            if result.returncode != 0:
                print(f"    ⚠️  yt-dlp error: {result.stderr[:200]}")
                return []

            items = []
            for line in result.stdout.strip().split("\n"):
                if not line or SEPARATOR not in line:
                    continue
                parts = line.split(SEPARATOR, 6)
                if len(parts) < 6:
                    continue

                video_id, title, upload_date, duration, view_count, channel = parts[:6]
                description = parts[6] if len(parts) > 6 else ""

                # Parse upload_date (format: YYYYMMDD)
                pub_date = ""
                if upload_date and upload_date != "NA":
                    try:
                        pub_date = datetime.strptime(upload_date, "%Y%m%d").isoformat()
                    except ValueError:
                        pass

                # Parse numeric fields safely
                try:
                    duration_int = int(duration) if duration and duration != "NA" else 0
                except ValueError:
                    duration_int = 0
                try:
                    view_int = int(view_count) if view_count and view_count != "NA" else 0
                except ValueError:
                    view_int = 0

                items.append({
                    "item_id": f"youtube_{video_id}",
                    "title": title,
                    "description": description,
                    "link": f"https://www.youtube.com/watch?v={video_id}",
                    "pub_date": pub_date,
                    "metadata": {
                        "video_id": video_id,
                        "duration": duration_int,
                        "view_count": view_int,
                        "channel": channel,
                    }
                })

            return items

        except subprocess.TimeoutExpired:
            print("    ❌ yt-dlp timed out (60s)")
            return []
        except FileNotFoundError:
            print("    ❌ yt-dlp not found. Install: pip install yt-dlp")
            return []
        except Exception as e:
            print(f"    ❌ YouTube fetch error: {e}")
            return []


# ============================================================
#  Tier 2: RSSHub-based scrapers
# ============================================================

class BilibiliScraper(RSSHubScraper):
    """Bilibili scraper via RSSHub — needs BILIBILI_COOKIE in RSSHub config"""

    def __init__(self):
        super().__init__("/bilibili/user/video/{id}")

    def extract_user_id(self, url: str) -> Optional[str]:
        match = re.search(r"space\.bilibili\.com/(\d+)", url)
        return match.group(1) if match else None


class WeiboScraper(RSSHubScraper):
    """Weibo scraper via RSSHub — works without cookie for most users"""

    def __init__(self):
        super().__init__("/weibo/user/{id}")

    def extract_user_id(self, url: str) -> Optional[str]:
        # Match /u/123456 or /userid
        match = re.search(r"weibo\.com/u/(\d+)", url)
        if match:
            return match.group(1)
        match = re.search(r"weibo\.com/(\d+)", url)
        return match.group(1) if match else None


class DouyinScraper(RSSHubScraper):
    """Douyin scraper via RSSHub — works without cookie for most users"""

    def __init__(self):
        super().__init__("/douyin/user/{id}")

    def extract_user_id(self, url: str) -> Optional[str]:
        # Direct user URL
        match = re.search(r"douyin\.com/user/([A-Za-z0-9_-]+)", url)
        if match:
            return match.group(1)

        # Short URL — need to resolve redirect
        if "v.douyin.com" in url or "douyin.com" in url:
            try:
                with httpx.Client(timeout=10, follow_redirects=True) as client:
                    resp = client.get(url, headers=self.headers)
                    final_url = str(resp.url)
                    match = re.search(r"user/([A-Za-z0-9_-]+)", final_url)
                    if match:
                        return match.group(1)
            except Exception as e:
                print(f"    ⚠️  Douyin URL resolve failed: {e}")

        return None


class XiaohongshuScraper(RSSHubScraper):
    """Xiaohongshu scraper via RSSHub — needs XIAOHONGSHU_COOKIE in RSSHub config.
    
    Note: As of 2026-02, the RSSHub xiaohongshu route may be unstable due to
    aggressive anti-scraping measures by the platform. If this fails, it's likely
    an upstream RSSHub issue rather than a configuration problem.
    """

    def __init__(self):
        super().__init__("/xiaohongshu/user/{id}/notes")

    def extract_user_id(self, url: str) -> Optional[str]:
        match = re.search(r"user/profile/([a-zA-Z0-9]+)", url)
        if match:
            return match.group(1)
        match = re.search(r"xiaohongshu\.com/([a-zA-Z0-9]+)", url)
        return match.group(1) if match else None


# ============================================================
#  Factory
# ============================================================

class ScraperFactory:
    """Factory to get appropriate scraper for a platform"""

    @staticmethod
    def detect_platform(url: str) -> str:
        """Detect platform from URL"""
        url_lower = url.lower()

        if "bilibili.com" in url_lower:
            return "bilibili"
        elif "xiaohongshu.com" in url_lower or "xhslink.com" in url_lower:
            return "xiaohongshu"
        elif "weibo.com" in url_lower:
            return "weibo"
        elif "youtube.com" in url_lower or "youtu.be" in url_lower:
            return "youtube"
        elif "vimeo.com" in url_lower:
            return "vimeo"
        elif "behance.net" in url_lower:
            return "behance"
        elif "douyin.com" in url_lower:
            return "douyin"
        else:
            return "unknown"

    @staticmethod
    def get_scraper(platform: str) -> Optional[BaseScraper]:
        """Get scraper instance for platform"""
        scrapers = {
            "bilibili": BilibiliScraper,
            "xiaohongshu": XiaohongshuScraper,
            "weibo": WeiboScraper,
            "youtube": YouTubeScraper,
            "vimeo": VimeoScraper,
            "behance": BehanceScraper,
            "douyin": DouyinScraper,
        }

        scraper_class = scrapers.get(platform.lower())
        return scraper_class() if scraper_class else None
