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
import sys
import json
import hashlib
import shutil
import subprocess
import xml.etree.ElementTree as ET
from typing import List, Dict, Any, Optional
from datetime import datetime
import time
import httpx
from urllib.parse import urlparse, parse_qs, unquote


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
        self.timeout = float(os.environ.get("RSS_HTTP_TIMEOUT", "10"))
        self.max_retries = max(1, int(os.environ.get("RSS_HTTP_RETRIES", "2")))
        self.retry_backoff = float(os.environ.get("RSS_HTTP_BACKOFF", "0.8"))
        self.last_error = None

    def get(self, url: str, headers: Optional[Dict] = None,
            timeout: Optional[float] = None,
            retries: Optional[int] = None) -> httpx.Response:
        """Make GET request with retry logic"""
        request_timeout = timeout if timeout is not None else self.timeout
        max_retries = max(1, retries if retries is not None else self.max_retries)
        request_headers = {**self.headers, **(headers or {})}
        for attempt in range(max_retries):
            try:
                with httpx.Client(timeout=request_timeout, follow_redirects=True) as client:
                    response = client.get(url, headers=request_headers)
                    response.raise_for_status()
                    return response
            except Exception:
                if attempt == max_retries - 1:
                    raise
                time.sleep(self.retry_backoff * (attempt + 1))


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
        self.last_error = None
        user_id = self.extract_user_id(url)
        if not user_id:
            print(f"    ⚠️  Cannot extract user ID from: {url}")
            self.last_error = f"Cannot extract user ID from {url}"
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
                self.last_error = f"Non-RSS content from RSSHub (HTTP {response.status_code})"
                return []
        except Exception as e:
            self.last_error = str(e)
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
        self.last_error = None
        username = self.extract_user_id(url)
        if not username:
            self.last_error = f"Cannot extract Vimeo username from {url}"
            return []

        rss_url = f"https://vimeo.com/{username}/videos/rss"
        print(f"    📡 Native RSS: {rss_url}")

        try:
            response = self.get(rss_url)
            return self.parse_rss_xml(response.text, "vimeo")
        except Exception as e:
            self.last_error = str(e)
            print(f"    ❌ Vimeo RSS fetch failed: {e}")
            return []


class BehanceScraper(NativeRSSScraper):
    """Behance scraper — uses native RSS feed at /feeds/user?username={user}"""

    def extract_user_id(self, url: str) -> Optional[str]:
        """Extract username from Behance URL"""
        match = re.search(r"behance\.net/([^/?#]+)", url)
        return match.group(1) if match else None

    def fetch_items(self, url: str) -> List[Dict[str, Any]]:
        self.last_error = None
        username = self.extract_user_id(url)
        if not username:
            self.last_error = f"Cannot extract Behance username from {url}"
            return []

        rss_url = f"https://www.behance.net/feeds/user?username={username}"
        print(f"    📡 Native RSS: {rss_url}")

        try:
            response = self.get(rss_url)
            return self.parse_rss_xml(response.text, "behance")
        except Exception as e:
            err = str(e)
            # Behance occasionally rate-limits with empty 403 responses.
            # Treat as temporary skip (no new items) instead of hard failure.
            if "403" in err:
                print("    ⚠️  Behance temporarily rate-limited (403), skip this cycle")
                self.last_error = None
                return []
            self.last_error = err
            print(f"    ❌ Behance RSS fetch failed: {e}")
            return []


# ============================================================
#  Tier 1b: YouTube via yt-dlp (no API key needed)
# ============================================================

class YouTubeScraper(BaseScraper):
    """YouTube scraper — uses yt-dlp for reliable metadata extraction"""

    def _fetch_via_native_feed(self, channel_ref: str) -> List[Dict[str, Any]]:
        """Fallback: parse YouTube Atom feed directly.

        Useful when yt-dlp is blocked by bot checks.
        """
        channel_id = ""
        if channel_ref.startswith("UC"):
            channel_id = channel_ref
        else:
            # Resolve channel_id from HTML page (externalId / browseId)
            if channel_ref.startswith("@"):
                page_url = f"https://www.youtube.com/{channel_ref}/videos"
            else:
                page_url = f"https://www.youtube.com/channel/{channel_ref}/videos"
            try:
                resp = self.get(page_url, timeout=max(self.timeout, 12), retries=2)
                text = resp.text
                m = re.search(r'"externalId":"([A-Za-z0-9_-]+)"', text)
                if not m:
                    m = re.search(r'"browseId":"(UC[A-Za-z0-9_-]+)"', text)
                if m:
                    channel_id = m.group(1)
            except Exception:
                channel_id = ""

        if not channel_id:
            return []

        feed_url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
        try:
            response = self.get(feed_url, timeout=max(self.timeout, 12), retries=2)
            parser = NativeRSSScraper()
            items = parser.parse_rss_xml(response.text, "youtube")
            if items:
                print(f"    ✓ YouTube fallback feed: {len(items)} items")
            return items
        except Exception:
            return []

    def _resolve_ytdlp_bin(self) -> Optional[str]:
        """Find yt-dlp binary from PATH / venv / python -m yt_dlp fallback."""
        # 1) Direct binary in current PATH
        ytdlp_bin = shutil.which("yt-dlp")
        if ytdlp_bin:
            return ytdlp_bin

        # 2) Binary next to current python executable (venv/bin/yt-dlp)
        py_dir = os.path.dirname(sys.executable)
        candidates = [
            os.path.join(py_dir, "yt-dlp"),
            os.path.join(py_dir, "yt_dlp"),
        ]
        for c in candidates:
            if os.path.isfile(c) and os.access(c, os.X_OK):
                return c

        # 3) If yt_dlp module import works, use `python -m yt_dlp`
        try:
            import yt_dlp  # noqa: F401
            return "__PYTHON_MODULE__"
        except Exception:
            return None

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
        self.last_error = None
        channel_ref = self.extract_channel_id(url)
        if not channel_ref:
            self.last_error = f"Cannot extract YouTube channel from {url}"
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

        # Resolve binary robustly in cron / venv environments.
        ytdlp_bin = self._resolve_ytdlp_bin()
        if not ytdlp_bin:
            self.last_error = "yt-dlp not found"
            print("    ❌ yt-dlp not found. Install: pip install yt-dlp")
            return []

        try:
            ytdlp_timeout = max(20, int(os.environ.get("RSS_YTDLP_TIMEOUT", "30")))
            cmd = [
                "--print", PRINT_FORMAT,
                "--playlist-items", "1:15",
                "--no-warnings",
                videos_url,
            ]
            if ytdlp_bin == "__PYTHON_MODULE__":
                run_cmd = [sys.executable, "-m", "yt_dlp", *cmd]
            else:
                run_cmd = [ytdlp_bin, *cmd]

            result = subprocess.run(
                run_cmd,
                capture_output=True,
                text=True,
                timeout=ytdlp_timeout,
            )

            if result.returncode != 0:
                err_text = result.stderr[:200] or "yt-dlp returned non-zero status"
                print(f"    ⚠️  yt-dlp error: {err_text}")
                # Fallback for bot-check / transient yt-dlp failures.
                fallback_items = self._fetch_via_native_feed(channel_ref)
                if fallback_items:
                    self.last_error = None
                    return fallback_items
                self.last_error = err_text
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
            self.last_error = "yt-dlp timed out"
            print(f"    ❌ yt-dlp timed out ({ytdlp_timeout}s)")
            fallback_items = self._fetch_via_native_feed(channel_ref)
            if fallback_items:
                self.last_error = None
                return fallback_items
            return []
        except Exception as e:
            self.last_error = str(e)
            print(f"    ❌ YouTube fetch error: {e}")
            fallback_items = self._fetch_via_native_feed(channel_ref)
            if fallback_items:
                self.last_error = None
                return fallback_items
            return []


# ============================================================
#  Tier 2: RSSHub-based scrapers
# ============================================================

class BilibiliScraper(RSSHubScraper):
    """Bilibili scraper via RSSHub — tries /video route first, falls back to /dynamic."""

    def __init__(self):
        super().__init__("/bilibili/user/video/{id}")
        self._fallback_route = "/bilibili/user/dynamic/{id}"

    def extract_user_id(self, url: str) -> Optional[str]:
        match = re.search(r"space\.bilibili\.com/(\d+)", url)
        return match.group(1) if match else None

    def fetch_items(self, url: str) -> List[Dict[str, Any]]:
        """Try video route first; on 503 (anti-bot), fall back to dynamic route."""
        self.last_error = None
        user_id = self.extract_user_id(url)
        if not user_id:
            self.last_error = f"Cannot extract Bilibili user ID from {url}"
            return []

        # Try primary /video route
        video_url = f"{self.RSSHUB_BASE}/bilibili/user/video/{user_id}"
        print(f"    📡 RSSHub: {video_url}")
        try:
            response = self.get(video_url, timeout=max(self.timeout, 12), retries=3)
            ct = response.headers.get("content-type", "")
            if "xml" in ct or "rss" in ct:
                items = self.parse_rss_xml(response.text, "bilibili")
                if items:
                    return items
        except Exception as e:
            err_msg = str(e)
            if "503" in err_msg or "风控" in err_msg:
                # Anti-bot triggered, try dynamic route
                dynamic_url = f"{self.RSSHUB_BASE}/bilibili/user/dynamic/{user_id}"
                print(f"    ⚠️  Video route blocked, trying dynamic: {dynamic_url}")
                try:
                    response = self.get(dynamic_url, timeout=max(self.timeout, 12), retries=3)
                    ct = response.headers.get("content-type", "")
                    if "xml" in ct or "rss" in ct:
                        items = self.parse_rss_xml(response.text, "bilibili")
                        if items:
                            return items
                except Exception as e2:
                    # Treat repeated 503 as temporary skip to avoid noisy hard failures.
                    if "503" in str(e2):
                        print("    ⚠️  Bilibili RSSHub temporarily unavailable (503), skip this cycle")
                        self.last_error = None
                        return []
                    self.last_error = f"Both routes failed: video={err_msg[:60]}, dynamic={e2}"
                    print(f"    ❌ Dynamic fallback also failed: {e2}")
                    return []
            self.last_error = err_msg
            print(f"    ❌ RSSHub fetch failed: {e}")
            return []

        return []


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


class XiaohongshuScraper(BaseScraper):
    """Xiaohongshu scraper — uses Playwright with rednote-mcp cookies.

    Strategy: load user profile page in headless browser, intercept the XHR
    response from the `user_posted` API. The browser handles request signing
    automatically, bypassing the 406 error from direct API calls.

    Cookie source: ~/.mcp/rednote/cookies.json (managed by rednote-mcp init)
    Fallback: RSSHub route (may fail due to XHS anti-scraping)
    """

    RSSHUB_BASE = os.environ.get("RSSHUB_BASE_URL", "http://localhost:1200")
    COOKIE_PATH = os.path.expanduser("~/.mcp/rednote/cookies.json")

    def __init__(self):
        super().__init__()
        self.timeout = float(os.environ.get("RSS_XHS_TIMEOUT", "15"))
        self._pw_cookies = self._load_pw_cookies()

    def _load_pw_cookies(self) -> list:
        """Load Playwright-format cookies from rednote-mcp store."""
        try:
            with open(self.COOKIE_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return []

    def extract_user_id(self, url: str) -> Optional[str]:
        match = re.search(r"user/profile/([a-zA-Z0-9]+)", url)
        if match:
            return match.group(1)
        match = re.search(r"xiaohongshu\.com/([a-zA-Z0-9]+)", url)
        return match.group(1) if match else None

    def fetch_items(self, url: str) -> List[Dict[str, Any]]:
        self.last_error = None
        user_id = self.extract_user_id(url)
        if not user_id:
            self.last_error = f"Cannot extract XHS user ID from {url}"
            return []

        # 1) Try direct HTML profile parse first (fast, no browser dependency).
        items = self._fetch_via_html_profile(user_id)
        if items:
            return items

        # 2) Try Playwright with rednote-mcp cookies.
        if self._pw_cookies:
            items = self._fetch_via_playwright(user_id)
            if items:
                return items

        # 3) Fallback: RSSHub
        return self._fetch_via_rsshub(user_id)

    def _fetch_via_html_profile(self, user_id: str) -> List[Dict[str, Any]]:
        """Fetch XHS notes from SSR initial-state JSON in profile HTML.

        This route avoids Playwright dependency and works when note list is server-rendered.
        """
        profile_url = f"https://www.xiaohongshu.com/user/profile/{user_id}"
        print(f"    📡 XHS HTML: {profile_url}")
        try:
            response = self.get(profile_url, timeout=max(self.timeout, 12), retries=2)
            html = response.text
            m = re.search(r"__INITIAL_STATE__=(\{.*?\})</script>", html, re.S)
            if not m:
                return []

            # XHS embeds `undefined`; normalize to valid JSON.
            json_text = re.sub(r":undefined([,}])", r":null\1", m.group(1))
            state = json.loads(json_text)

            user_obj = state.get("user", {}) if isinstance(state, dict) else {}
            notes_groups = user_obj.get("notes", [])
            if not notes_groups:
                return []

            # Usually a nested list: user.notes[0] is the current tab notes.
            notes = notes_groups[0] if isinstance(notes_groups[0], list) else notes_groups
            items: List[Dict[str, Any]] = []

            author = ""
            user_info = user_obj.get("userInfo", {}) if isinstance(user_obj, dict) else {}
            if isinstance(user_info, dict):
                author = user_info.get("nickname") or user_info.get("nickName") or ""

            for note in notes[:20]:
                if not isinstance(note, dict):
                    continue
                card = note.get("noteCard", {}) if isinstance(note.get("noteCard"), dict) else {}
                title = (card.get("displayTitle") or "").strip()
                if not title:
                    continue

                # noteId may be empty in SSR; recover from cover URL when possible.
                note_id = (card.get("noteId") or note.get("id") or "").strip()
                cover = card.get("cover", {}) if isinstance(card.get("cover"), dict) else {}
                cover_url = cover.get("urlPre") or cover.get("urlDefault") or cover.get("url") or ""
                if not note_id and cover_url:
                    mm = re.search(r"/([0-9a-zA-Z]{16,32})!", cover_url)
                    if mm:
                        note_id = mm.group(1)

                xsec_token = (card.get("xsecToken") or note.get("xsecToken") or "").strip()

                if note_id:
                    link = f"https://www.xiaohongshu.com/explore/{note_id}"
                elif xsec_token:
                    link = f"https://www.xiaohongshu.com/user/profile/{user_id}?xsec_token={unquote(xsec_token)}&xsec_source=pc_user"
                else:
                    link = profile_url

                # Stable fallback id even when note_id is absent.
                unique_seed = note_id or f"{title}|{cover_url}|{xsec_token}"
                item_id = f"xiaohongshu_{hashlib.md5(unique_seed.encode()).hexdigest()[:12]}"

                note_author = ""
                n_user = card.get("user", {}) if isinstance(card.get("user"), dict) else {}
                if isinstance(n_user, dict):
                    note_author = n_user.get("nickname") or n_user.get("nickName") or ""

                items.append({
                    "item_id": item_id,
                    "title": title,
                    "description": "",
                    "link": link,
                    "pub_date": "",
                    "metadata": {
                        "_channel_title": note_author or author,
                        "note_id": note_id,
                        "cover_url": cover_url,
                        "liked_count": (card.get("interactInfo", {}) or {}).get("likedCount", ""),
                    },
                })

            if items:
                print(f"  ✓ XHS: {len(items)} notes via HTML")
            return items
        except Exception as e:
            print(f"    ⚠️  XHS HTML parse failed: {e}")
            return []

    def _fetch_via_playwright(self, user_id: str) -> List[Dict[str, Any]]:
        """Fetch user notes by intercepting XHR in a headless browser."""
        print(f"    📡 XHS Playwright: user={user_id}")
        captured_notes = []

        try:
            from playwright.sync_api import sync_playwright

            def handle_response(response):
                """Capture the user_posted API response."""
                if "user_posted" in response.url or "user/posted" in response.url:
                    try:
                        data = response.json()
                        notes = data.get("data", {}).get("notes", [])
                        if notes:
                            captured_notes.extend(notes)
                    except Exception:
                        pass

            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                context = browser.new_context(
                    user_agent=self.headers["User-Agent"],
                    viewport={"width": 1280, "height": 800},
                )
                # Load rednote-mcp cookies
                context.add_cookies(self._pw_cookies)

                page = context.new_page()
                page.on("response", handle_response)

                profile_url = f"https://www.xiaohongshu.com/user/profile/{user_id}"
                page.goto(profile_url, timeout=int(self.timeout * 1000), wait_until="domcontentloaded")

                # Check for captcha/login redirect (cookies expired)
                current_url = page.url
                if "captcha" in current_url or "login" in current_url:
                    print("    ⚠️  XHS cookies expired (captcha/login redirect)")
                    print("    💡 Fix: run 'npx rednote-mcp init' to re-login")
                    self.last_error = "XHS cookies expired — run 'npx rednote-mcp init'"
                    browser.close()
                    return []

                # Wait for notes to load (either via XHR interception or DOM)
                try:
                    page.wait_for_selector(
                        ".note-item, .feeds-container, section.note-item",
                        timeout=8000,
                    )
                    # Small delay for XHR to complete
                    page.wait_for_timeout(1500)
                except Exception:
                    pass  # XHR may have already been captured

                # If XHR interception worked, parse captured_notes
                if captured_notes:
                    items = self._parse_api_notes(captured_notes, user_id, page)
                    browser.close()
                    if items:
                        print(f"  ✓ XHS: {len(items)} notes via Playwright XHR")
                        return items

                # Fallback: try to extract from DOM
                items = self._parse_dom_notes(page, user_id)
                browser.close()
                if items:
                    print(f"  ✓ XHS: {len(items)} notes via DOM")
                return items

        except ImportError:
            print("    ⚠️  Playwright not available")
            return []
        except Exception as e:
            print(f"    ⚠️  XHS Playwright failed: {e}")
            return []

    def _parse_api_notes(self, notes: list, user_id: str, page) -> List[Dict[str, Any]]:
        """Parse notes from intercepted API response."""
        items = []
        # Try to get author name from page
        author = ""
        try:
            author_el = page.query_selector(".user-name, .info .username")
            if author_el:
                author = author_el.inner_text().strip()
        except Exception:
            pass

        for note in notes[:20]:
            note_id = note.get("note_id", "")
            if not note_id:
                continue
            display_title = note.get("display_title", "")
            timestamp_ms = note.get("time", 0) or note.get("last_update_time", 0)
            pub_date = ""
            if timestamp_ms:
                try:
                    pub_date = datetime.fromtimestamp(timestamp_ms / 1000).isoformat()
                except (ValueError, OSError):
                    pass

            link = f"https://www.xiaohongshu.com/explore/{note_id}"
            item_id = f"xiaohongshu_{hashlib.md5(note_id.encode()).hexdigest()[:12]}"

            cover = note.get("cover", {})
            cover_url = cover.get("url", "") if isinstance(cover, dict) else ""
            note_author = note.get("user", {}).get("nickname", "") or author

            items.append({
                "item_id": item_id,
                "title": display_title or "(无标题)",
                "description": "",
                "link": link,
                "pub_date": pub_date,
                "metadata": {
                    "_channel_title": note_author,
                    "note_id": note_id,
                    "cover_url": cover_url,
                    "liked_count": note.get("liked_count", ""),
                }
            })
        return items

    def _parse_dom_notes(self, page, user_id: str) -> List[Dict[str, Any]]:
        """Fallback: extract notes directly from DOM."""
        items = []
        seen_ids = set()

        # Get author name from page header
        author = ""
        try:
            author_el = page.query_selector(".user-name, .info .username, .user-nickname")
            if author_el:
                author = author_el.inner_text().strip()
        except Exception:
            pass

        try:
            # Each section.note-item contains a cover link and title text
            note_sections = page.query_selector_all("section.note-item")
            if not note_sections:
                note_sections = page.query_selector_all(".note-item")

            for section in note_sections[:20]:
                # Extract note_id from cover link href
                cover = section.query_selector("a.cover")
                if not cover:
                    continue
                href = cover.get_attribute("href") or ""
                # Pattern: /user/profile/{uid}/{note_id}?... or /explore/{note_id}
                note_match = re.search(r"/([a-f0-9]{24})(?:\?|$)", href)
                if not note_match:
                    note_match = re.search(r"/explore/([a-f0-9]+)", href)
                if not note_match:
                    continue

                note_id = note_match.group(1)
                if note_id in seen_ids:
                    continue
                seen_ids.add(note_id)

                # Extract title from section inner_text
                title = ""
                try:
                    text = section.inner_text().strip()
                    # inner_text contains: optional "置顶\n", title, author, likes
                    lines = [l.strip() for l in text.split("\n") if l.strip()]
                    # Skip "置顶" tag and author/likes at end
                    for line in lines:
                        if line in ("置顶",) or line.isdigit():
                            continue
                        if line == author:
                            continue
                        title = line
                        break
                except Exception:
                    pass

                link = f"https://www.xiaohongshu.com/explore/{note_id}"
                item_id = f"xiaohongshu_{hashlib.md5(note_id.encode()).hexdigest()[:12]}"

                items.append({
                    "item_id": item_id,
                    "title": title or "(无标题)",
                    "description": "",
                    "link": link,
                    "pub_date": "",
                    "metadata": {
                        "_channel_title": author,
                        "note_id": note_id,
                    }
                })
        except Exception as e:
            print(f"    ⚠️  DOM extraction failed: {e}")
        return items

    def _fetch_via_rsshub(self, user_id: str) -> List[Dict[str, Any]]:
        """Fallback: fetch via RSSHub (may fail due to XHS anti-scraping)."""
        rsshub_url = f"{self.RSSHUB_BASE}/xiaohongshu/user/{user_id}/notes"
        print(f"    📡 RSSHub fallback: {rsshub_url}")
        try:
            response = self.get(
                rsshub_url,
                timeout=max(self.timeout, 10),
                retries=max(2, int(os.environ.get("RSS_XHS_RETRIES", "1"))),
            )
            ct = response.headers.get("content-type", "")
            if "xml" in ct or "rss" in ct:
                parser = NativeRSSScraper()
                return parser.parse_rss_xml(response.text, "xiaohongshu")
            else:
                # Avoid hard-failing whole cycle on common 503 anti-bot pages.
                if response.status_code == 503:
                    print("    ⚠️  XHS RSSHub temporarily unavailable (503), skip this cycle")
                    self.last_error = None
                    return []
                self.last_error = f"Non-RSS content from RSSHub (HTTP {response.status_code})"
                return []
        except Exception as e:
            if "503" in str(e):
                print("    ⚠️  XHS RSSHub temporarily unavailable (503), skip this cycle")
                self.last_error = None
                return []
            self.last_error = str(e)
            print(f"    ❌ RSSHub fallback also failed: {e}")
            return []


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
