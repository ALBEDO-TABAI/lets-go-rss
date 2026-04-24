"""
Lightweight scraper modules for various platforms.

Architecture:
- Tier 1 (Native RSS): Vimeo, Behance — direct RSS feed parsing
- Tier 1b (yt-dlp):    YouTube — uses yt-dlp for metadata extraction
- Tier 2 (RSSHub):     Bilibili, Weibo, Douyin, Xiaohongshu — via local RSSHub

Environment variables:
- RSSHUB_BASE_URL_PRIMARY:  Primary RSSHub URL (default http://localhost:1201 — skill-owned via rsshub_manager)
- RSSHUB_BASE_URL_FALLBACK: Fallback RSSHub URL (default http://localhost:1200 — ready-cowork's embedded worker)
- RSSHUB_BASE_URL:          Legacy single-base; appended to the list if set
- RSS_NOTIFY_MACOS=0 to disable macOS notifications (default on)
"""

import re
import os
import json
import hashlib
import subprocess
import sys
import urllib.parse
import xml.etree.ElementTree as ET
from typing import List, Dict, Any, Optional
from datetime import datetime
import time
import httpx
from urllib.parse import urlparse, parse_qs
from chrome_session_bridge import ChromeSessionBridge, ChromeSessionUnavailable


# ============================================================
#  Utilities
# ============================================================

def notify_macos(title: str, message: str, subtitle: Optional[str] = None,
                 dedupe_key: Optional[str] = None) -> None:
    """Post a macOS notification via osascript.

    No-op on non-darwin, or when RSS_NOTIFY_MACOS=0 is set. `dedupe_key`, when
    provided, suppresses duplicate notifications with the same key for 24h
    (uses a tmp-file semaphore under ${TMPDIR}/rss_notify_<key>).
    """
    if sys.platform != "darwin":
        return
    if os.environ.get("RSS_NOTIFY_MACOS", "1") == "0":
        return

    if dedupe_key:
        import tempfile
        sem_path = os.path.join(
            tempfile.gettempdir(),
            f"rss_notify_{hashlib.md5(dedupe_key.encode()).hexdigest()[:12]}",
        )
        try:
            mtime = os.path.getmtime(sem_path)
            if time.time() - mtime < 24 * 3600:
                return  # dedup window still active
        except OSError:
            pass
        try:
            with open(sem_path, "w") as f:
                f.write(str(time.time()))
        except Exception:
            pass

    # AppleScript string quoting: escape double quotes and backslashes.
    def _q(s: str) -> str:
        return s.replace("\\", "\\\\").replace('"', '\\"')
    parts = [f'display notification "{_q(message)}" with title "{_q(title)}"']
    if subtitle:
        parts.append(f'subtitle "{_q(subtitle)}"')
    script = " ".join(parts)
    try:
        subprocess.run(
            ["osascript", "-e", script],
            timeout=3, capture_output=True,
        )
    except Exception:
        pass


# ============================================================
#  RSSHub client — double-base with circuit breaker
# ============================================================

class _RSSHubClient:
    """Tries a list of RSSHub base URLs in order. On connect-refused or 503,
    short-circuits that base for BLACKLIST_WINDOW_SEC so the next call skips
    it immediately rather than paying the timeout again.

    Bases (in order):
      1. RSSHUB_BASE_URL_PRIMARY   (default http://localhost:1201 — skill-owned)
      2. RSSHUB_BASE_URL_FALLBACK  (default http://localhost:1200 — ready-cowork)
      3. RSSHUB_BASE_URL           (legacy single-base env var; appended if set)
    """

    BLACKLIST_WINDOW_SEC = 600
    FAIL_THRESHOLD = 3

    def __init__(self):
        primary = os.environ.get("RSSHUB_BASE_URL_PRIMARY", "http://localhost:1201")
        fallback = os.environ.get("RSSHUB_BASE_URL_FALLBACK", "http://localhost:1200")
        legacy = os.environ.get("RSSHUB_BASE_URL")
        seen = set()
        self.bases = []
        for b in (primary, fallback, legacy):
            if b and b not in seen:
                self.bases.append(b)
                seen.add(b)
        self._state = {b: {"fails": 0, "blocked_until": 0.0} for b in self.bases}

    def _is_transient(self, exc: Exception) -> bool:
        msg = str(exc).lower()
        return (
            "errno 61" in msg
            or "connection refused" in msg
            or "503" in msg
            or "service unavailable" in msg
            or "disconnected" in msg
        )

    def fetch(self, route: str, scraper: "BaseScraper",
              require_rss: bool = True) -> "httpx.Response":
        """Try each base in order using scraper.get (retains its headers/retry).

        When `require_rss` is True (default), a 200 response whose content-type
        does not look like XML/RSS is treated as a soft failure and the next
        base is tried — this catches RSSHub's JSON error envelopes that come
        back with 200 for some route failure modes.

        Raises the final exception / returns the final non-RSS response when
        all bases have been tried.
        """
        last_err: Optional[Exception] = None
        last_non_rss: Optional["httpx.Response"] = None
        tried_any = False
        for base in self.bases:
            st = self._state[base]
            if time.time() < st["blocked_until"]:
                continue
            tried_any = True
            url = f"{base}{route}"
            try:
                response = scraper.get(url)
            except Exception as e:
                last_err = e
                if self._is_transient(e):
                    st["fails"] += 1
                    if st["fails"] >= self.FAIL_THRESHOLD:
                        st["blocked_until"] = time.time() + self.BLACKLIST_WINDOW_SEC
                        st["fails"] = 0
                    continue
                raise
            # Success at HTTP layer — validate content if required
            if require_rss:
                ct = response.headers.get("content-type", "").lower()
                if "xml" not in ct and "rss" not in ct:
                    last_non_rss = response
                    continue  # try next base, don't count as a transient fail
            st["fails"] = 0
            st["blocked_until"] = 0.0
            return response

        # No base returned a usable response.
        if not tried_any:
            raise RuntimeError("All RSSHub bases are currently blacklisted")
        if last_non_rss is not None:
            # All bases returned 200 but nothing RSS-shaped. Return the last one
            # so the caller can surface its status/body.
            return last_non_rss
        assert last_err is not None
        raise last_err


# Module singleton — all RSSHub calls go through this.
rsshub_client = _RSSHubClient()


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

    def _should_try_browser_fallback(self, error: str) -> bool:
        text = (error or "").lower()
        return (
            "503" in text
            or "service unavailable" in text
            or "cooling down before new visitor cookies" in text
            or "captcha" in text
            or "风控" in text
        )

    def _is_connect_refused(self, exc: Exception) -> bool:
        """Detect connection-refused errors (errno 61 on macOS, common during
        RSSHub worker restart windows)."""
        msg = str(exc).lower()
        if "errno 61" in msg or "connection refused" in msg:
            return True
        try:
            if isinstance(exc, httpx.ConnectError):
                return True
        except Exception:
            pass
        return False

    def get(self, url: str, headers: Optional[Dict] = None,
            timeout: Optional[float] = None,
            retries: Optional[int] = None) -> httpx.Response:
        """Make GET request with retry logic.

        On top of the normal retry budget, grants ONE extra attempt specifically
        for ECONNREFUSED (worker restart window ~1-3s on embedded RSSHub).

        If the scraper is tagged as unhealthy (`_adaptive_health_hint >= 3`),
        the budget is squeezed: short timeout, no retry — so a chronically
        failing source does not dominate overall run time.
        """
        health = int(getattr(self, "_adaptive_health_hint", 0) or 0)
        if health >= 3:
            request_timeout = min(timeout if timeout is not None else self.timeout, 4.0)
            max_retries = 1
        else:
            request_timeout = timeout if timeout is not None else self.timeout
            max_retries = max(1, retries if retries is not None else self.max_retries)
        request_headers = {**self.headers, **(headers or {})}
        attempt = 0
        extra_retry_used = False
        while True:
            try:
                with httpx.Client(timeout=request_timeout, follow_redirects=True) as client:
                    response = client.get(url, headers=request_headers)
                    response.raise_for_status()
                    return response
            except Exception as e:
                attempt += 1
                if attempt >= max_retries:
                    if self._is_connect_refused(e) and not extra_retry_used:
                        extra_retry_used = True
                        time.sleep(2.0)
                        continue
                    raise
                time.sleep(self.retry_backoff * attempt)


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

    def __init__(self, route_template: str):
        super().__init__()
        self.route_template = route_template

    def extract_user_id(self, url: str) -> Optional[str]:
        """Override in subclass to extract platform-specific user ID"""
        raise NotImplementedError

    def fetch_items(self, url: str) -> List[Dict[str, Any]]:
        """Fetch via RSSHub route (double-base with circuit breaker)."""
        self.last_error = None
        user_id = self.extract_user_id(url)
        if not user_id:
            print(f"    ⚠️  Cannot extract user ID from: {url}")
            self.last_error = f"Cannot extract user ID from {url}"
            return []

        route = self.route_template.format(id=user_id)
        print(f"    📡 RSSHub: {route}")

        try:
            response = rsshub_client.fetch(route, self)
            ct = response.headers.get("content-type", "")
            if "xml" in ct or "rss" in ct:
                return self.parse_rss_xml(response.text, self._platform_name())
            else:
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
            # Behance occasionally rate-limits with 403; treat as temporary skip.
            if "403" in err:
                print("    ⚠️  Behance rate-limited (403), skip this cycle")
                self.last_error = None
                return []
            self.last_error = err
            print(f"    ❌ Behance RSS fetch failed: {e}")
            return []


# ============================================================
#  Tier 1b: YouTube via yt-dlp (no API key needed)
# ============================================================

class YouTubeScraper(BaseScraper):
    """YouTube scraper — uses yt-dlp, with native Atom feed fallback."""

    def _fetch_via_atom_feed(self, channel_ref: str) -> List[Dict[str, Any]]:
        """Fallback: parse YouTube's native Atom feed (no yt-dlp needed).

        Works when yt-dlp is blocked by bot checks or times out.
        """
        channel_id = ""
        if channel_ref.startswith("UC"):
            channel_id = channel_ref
        else:
            # Resolve channel_id from page HTML
            if channel_ref.startswith("@"):
                page_url = f"https://www.youtube.com/{channel_ref}/videos"
            else:
                page_url = f"https://www.youtube.com/channel/{channel_ref}/videos"
            try:
                resp = self.get(page_url, timeout=12, retries=2)
                m = re.search(r'"externalId":"([A-Za-z0-9_-]+)"', resp.text)
                if not m:
                    m = re.search(r'"browseId":"(UC[A-Za-z0-9_-]+)"', resp.text)
                if m:
                    channel_id = m.group(1)
            except Exception:
                return []

        if not channel_id:
            return []

        feed_url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
        try:
            response = self.get(feed_url, timeout=12, retries=2)
            parser = NativeRSSScraper()
            items = parser.parse_rss_xml(response.text, "youtube")
            if items:
                print(f"    ✓ YouTube Atom feed fallback: {len(items)} items")
            return items
        except Exception:
            return []

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

        try:
            ytdlp_timeout = int(os.environ.get("RSS_YTDLP_TIMEOUT", "20"))
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
                timeout=ytdlp_timeout,
            )

            if result.returncode != 0:
                err_text = result.stderr[:200] or "yt-dlp returned non-zero status"
                print(f"    ⚠️  yt-dlp error: {err_text}")
                # Fallback to native Atom feed
                fallback = self._fetch_via_atom_feed(channel_ref)
                if fallback:
                    self.last_error = None
                    return fallback
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
            fallback = self._fetch_via_atom_feed(channel_ref)
            if fallback:
                self.last_error = None
                return fallback
            return []
        except FileNotFoundError:
            self.last_error = "yt-dlp not found"
            print("    ❌ yt-dlp not found. Install: pip install yt-dlp")
            fallback = self._fetch_via_atom_feed(channel_ref)
            if fallback:
                self.last_error = None
                return fallback
            return []
        except Exception as e:
            self.last_error = str(e)
            print(f"    ❌ YouTube fetch error: {e}")
            fallback = self._fetch_via_atom_feed(channel_ref)
            if fallback:
                self.last_error = None
                return fallback
            return []


# ============================================================
#  Tier 2: RSSHub-based scrapers
# ============================================================

class BilibiliScraper(RSSHubScraper):
    """Bilibili scraper via RSSHub — uses /dynamic route (video route removed in RSSHub v1.0+)."""

    def __init__(self):
        super().__init__("/bilibili/user/dynamic/{id}")

    def extract_user_id(self, url: str) -> Optional[str]:
        match = re.search(r"space\.bilibili\.com/(\d+)", url)
        return match.group(1) if match else None

    def fetch_items(self, url: str) -> List[Dict[str, Any]]:
        """Fetch via /dynamic route. The /video route was removed in RSSHub v1.0."""
        self.last_error = None
        user_id = self.extract_user_id(url)
        if not user_id:
            self.last_error = f"Cannot extract Bilibili user ID from {url}"
            return []

        route = f"/bilibili/user/dynamic/{user_id}"
        print(f"    📡 RSSHub: {route}")
        try:
            response = rsshub_client.fetch(route, self)
            ct = response.headers.get("content-type", "")
            if "xml" in ct or "rss" in ct:
                items = self.parse_rss_xml(response.text, "bilibili")
                if items:
                    return items
            self.last_error = "Non-RSS content from RSSHub (HTTP 200)"
            print(f"    ⚠️  Bilibili: Non-RSS content from RSSHub (HTTP {response.status_code})")
        except Exception as e:
            self.last_error = str(e)
            print(f"    ❌ Bilibili fetch failed: {e}")
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

    def fetch_items(self, url: str) -> List[Dict[str, Any]]:
        items = super().fetch_items(url)
        if items:
            return items

        if not self._should_try_browser_fallback(self.last_error or ""):
            return []

        api_items = self._fetch_via_logged_in_api(url)
        if api_items:
            self.last_error = None
            return api_items

        browser_items = self._fetch_via_browser(url)
        if browser_items:
            self.last_error = None
            return browser_items
        return []

    def _fetch_via_logged_in_api(self, url: str) -> List[Dict[str, Any]]:
        try:
            import browser_cookie3
        except Exception:
            return []

        user_id = self.extract_user_id(url)
        if not user_id:
            return []

        try:
            cookies = browser_cookie3.chrome(domain_name="weibo.cn")
            headers = {
                "User-Agent": (
                    "Mozilla/5.0 (iPhone; CPU iPhone OS 13_2_3 like Mac OS X) "
                    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/13.0.3 "
                    "Mobile/15E148 Safari/604.1"
                ),
                "X-Requested-With": "XMLHttpRequest",
                "MWeibo-Pwa": "1",
            }

            profile_resp = httpx.get(
                f"https://m.weibo.cn/api/container/getIndex?type=uid&value={user_id}",
                headers=headers,
                cookies={c.name: c.value for c in cookies},
                timeout=20,
            )
            profile_resp.raise_for_status()
            profile_data = profile_resp.json().get("data", {})
            tabs = (profile_data.get("tabsInfo") or {}).get("tabs") or []
            weibo_tab = next((tab for tab in tabs if tab.get("tabKey") == "weibo"), None)
            container_id = (weibo_tab or {}).get("containerid") or f"107603{user_id}"

            feed_resp = httpx.get(
                f"https://m.weibo.cn/api/container/getIndex?containerid={container_id}",
                headers=headers,
                cookies={c.name: c.value for c in cookies},
                timeout=20,
            )
            feed_resp.raise_for_status()
            feed_data = feed_resp.json().get("data", {})
            cards = feed_data.get("cards") or []
            channel_title = ((profile_data.get("userInfo") or {}).get("screen_name")) or "微博账号"

            items = []
            for card in cards[:20]:
                mblog = card.get("mblog") or {}
                mid = str(mblog.get("mid") or mblog.get("id") or "")
                if not mid:
                    continue
                text = re.sub(r"<[^>]+>", "", mblog.get("text", "")).strip()
                title = text[:120] + "..." if len(text) > 120 else (text or f"微博动态 {mid[-6:]}")
                items.append({
                    "item_id": f"weibo_{hashlib.md5(mid.encode()).hexdigest()[:12]}",
                    "title": title,
                    "description": text[:500],
                    "link": f"https://m.weibo.cn/status/{mid}",
                    "pub_date": mblog.get("created_at", ""),
                    "metadata": {
                        "_channel_title": channel_title,
                        "mid": mid,
                    }
                })
            return items
        except Exception as e:
            self.last_error = f"微博登录态接口抓取失败: {e}"
            return []

    def _fetch_via_browser(self, url: str) -> List[Dict[str, Any]]:
        user_id = self.extract_user_id(url)
        if not user_id:
            self.last_error = f"Cannot extract Weibo user ID from {url}"
            return []

        bridge = ChromeSessionBridge()

        def _extract(page):
            page_text = page.locator("body").inner_text()[:5000]
            final_url = page.url
            if "captcha" in final_url or "visitor.passport.weibo.cn" in final_url:
                return {"error": "captcha"}
            if "安全验证" in page_text or "环境异常" in page_text:
                return {"error": "security_check"}
            if "前方有点拥堵，请登录后使用" in page_text:
                return {"error": "login_required"}

            payload = page.evaluate(
                """
                () => {
                  const body = document.body ? document.body.innerText : '';
                  const lines = body.split('\n').map(s => s.trim()).filter(Boolean);
                  const channel = lines.find(x => x && !x.includes('粉丝') && !x.includes('关注') && !x.includes('微博网页版')) || '微博账号';
                  const items = [];
                  for (let i = 0; i < lines.length; i++) {
                    const line = lines[i];
                    const next = lines[i + 1] || '';
                    const after = lines[i + 2] || '';
                    const looksLikeTime = /刚刚|分钟前|小时前|昨天|\d{2}-\d{2}|今天/.test(line);
                    if (!looksLikeTime) continue;
                    if (!next || next.includes('来自')) continue;
                    const text = [line, next, after].filter(Boolean).join('\n');
                    items.push({ text });
                    if (items.length >= 10) break;
                  }
                  return { channel, items };
                }
                """
            )
            return {"payload": payload}

        try:
            result = bridge.with_page(f"https://weibo.com/u/{user_id}", _extract, wait_ms=6000)
        except ChromeSessionUnavailable as e:
            self.last_error = f"微博浏览器后备不可用: {e}"
            return []
        except Exception as e:
            self.last_error = f"微博浏览器后备抓取失败: {e}"
            return []

        if result.get("error") == "captcha":
            self.last_error = "微博已触发验证，当前需要登录态浏览器才能继续抓取。"
            return []
        if result.get("error") == "security_check":
            self.last_error = "微博页面要求安全验证，当前浏览器态无法继续。"
            return []
        if result.get("error") == "login_required":
            self.last_error = "微博登录后才能读取微博列表，当前匿名态只能看到主页概要。"
            return []

        payload = result.get("payload") or {}
        items = []
        channel_title = (payload or {}).get("channel") or "微博账号"
        for row in (payload or {}).get("items") or []:
            text = (row.get("text") or "").strip()
            if not text:
                continue
            title = text.split("\n", 2)[1].strip() if "\n" in text else text[:120]
            item_id_basis = text[:120]
            items.append({
                "item_id": f"weibo_{hashlib.md5(item_id_basis.encode()).hexdigest()[:12]}",
                "title": title[:120] if title else "微博动态",
                "description": text[:500],
                "link": url,
                "pub_date": "",
                "metadata": {
                    "_channel_title": channel_title,
                    "source": "chrome_session_bridge",
                }
            })

        if items:
            print(f"    ✓ Weibo browser fallback: {len(items)} items")
            self.last_error = None
            return items[:10]

        self.last_error = "微博浏览器后备已打开页面，但当前页面没有解析出可用的微博列表。"
        return []


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

    def fetch_items(self, url: str) -> List[Dict[str, Any]]:
        items = super().fetch_items(url)
        if items:
            return items

        if not self._should_try_browser_fallback(self.last_error or ""):
            return []

        browser_items = self._fetch_via_browser(url)
        if browser_items or self.last_error is None:
            return browser_items
        return []

    def _fetch_via_browser(self, url: str) -> List[Dict[str, Any]]:
        try:
            from playwright.sync_api import sync_playwright
        except Exception:
            self.last_error = (
                "抖音 RSSHub 被风控，且当前没有可用浏览器后备。"
                "如需继续，请先安装 Playwright 或启用 web-access。"
            )
            return []

        resolved_url = self._resolve_share_url(url)
        if not resolved_url:
            self.last_error = f"无法解析抖音用户页地址: {url}"
            return []

        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                page = browser.new_page(
                    user_agent=(
                        "Mozilla/5.0 (iPhone; CPU iPhone OS 13_2_3 like Mac OS X) "
                        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/13.0.3 "
                        "Mobile/15E148 Safari/604.1"
                    )
                )
                page.goto(resolved_url, wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(3000)
                payload = page.evaluate(
                    """
                    () => {
                      const pageData = (((window._ROUTER_DATA || {}).loaderData || {})['user_(id)/page']) || {};
                      return {
                        userInfoRes: pageData.userInfoRes || {},
                        postListData: pageData.postListData || {},
                      };
                    }
                    """
                )
                browser.close()

            items = self._parse_douyin_page_payload(payload)
            if items:
                self.last_error = None
                return items

            user_info = ((payload or {}).get("userInfoRes") or {}).get("user_info") or {}
            if user_info.get("aweme_count") == 0:
                self.last_error = None
                return []
        except Exception as e:
            self.last_error = f"抖音浏览器后备抓取失败: {e}"
            return []

        self.last_error = "抖音页面可打开，但当前页面没有返回可解析的作品列表。"
        return []

    def _resolve_share_url(self, url: str) -> Optional[str]:
        try:
            response = httpx.get(url, headers={
                "User-Agent": (
                    "Mozilla/5.0 (iPhone; CPU iPhone OS 13_2_3 like Mac OS X) "
                    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/13.0.3 "
                    "Mobile/15E148 Safari/604.1"
                )
            }, follow_redirects=True, timeout=15)
        except Exception:
            return url if "iesdouyin.com/share/user/" in url else None

        final_url = str(response.url)
        if "iesdouyin.com/share/user/" in final_url:
            return final_url

        sec_uid_match = re.search(r"sec_uid=([^&]+)", final_url)
        user_path_match = re.search(r"/share/user/([^/?]+)", final_url)
        if user_path_match:
            sec_uid = sec_uid_match.group(1) if sec_uid_match else ""
            base = f"https://www.iesdouyin.com/share/user/{user_path_match.group(1)}"
            return f"{base}?sec_uid={urllib.parse.quote(sec_uid)}" if sec_uid else base

        return None

    def _parse_douyin_page_payload(self, payload: Dict[str, Any]) -> List[Dict[str, Any]]:
        user_info_res = (payload or {}).get("userInfoRes") or {}
        user_info = user_info_res.get("user_info") or {}
        post_data = (payload or {}).get("postListData") or {}

        aweme_list = (
            post_data.get("aweme_list")
            or post_data.get("awemeList")
            or post_data.get("items")
            or []
        )
        if isinstance(aweme_list, dict):
            aweme_list = aweme_list.get("list") or []

        channel_title = user_info.get("nickname") or user_info.get("unique_id") or "抖音账号"
        items = []
        for aweme in aweme_list[:20]:
            aweme_id = str(aweme.get("aweme_id") or aweme.get("awemeId") or "")
            if not aweme_id:
                continue
            desc = (aweme.get("desc") or aweme.get("description") or "").strip()
            title = desc[:120] + "..." if len(desc) > 120 else (desc or f"抖音视频 {aweme_id[-6:]}")
            create_time = aweme.get("create_time") or aweme.get("createTime") or 0
            pub_date = ""
            if create_time:
                try:
                    pub_date = datetime.fromtimestamp(int(create_time)).isoformat()
                except Exception:
                    pass
            items.append({
                "item_id": f"douyin_{hashlib.md5(aweme_id.encode()).hexdigest()[:12]}",
                "title": title,
                "description": desc[:500],
                "link": f"https://www.douyin.com/video/{aweme_id}",
                "pub_date": pub_date,
                "metadata": {
                    "_channel_title": channel_title,
                    "aweme_id": aweme_id,
                }
            })
        return items


class XiaohongshuScraper(BaseScraper):
    """Xiaohongshu scraper — browser-first, then Playwright cookies, then RSSHub.

    Preferred strategy: use a real browser context to read visible note cards
    from the profile page. This is more stable than headless cookie injection
    when XHS tightens captcha / device verification.

    Fallback 1: Playwright + rednote-mcp cookies
    Fallback 2: RSSHub route (best effort only)
    """

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

        items = self._fetch_via_browser(url, user_id)
        if items:
            return items
        if self.last_error and ("login/captcha wall" in self.last_error or "login_required" in self.last_error):
            return []

        # Fallback: Playwright with rednote-mcp cookies
        if self._pw_cookies:
            items = self._fetch_via_playwright(user_id)
            if items:
                return items
            if self.last_error and "cookies expired" in self.last_error.lower():
                return []

        # Final fallback: RSSHub
        return self._fetch_via_rsshub(user_id)

    def _fetch_via_browser(self, url: str, user_id: str) -> List[Dict[str, Any]]:
        """Use the user's real Chrome session when available."""
        print(f"    🌐 XHS browser-first: user={user_id}")
        bridge = ChromeSessionBridge()

        def _extract(page):
            body_text = page.locator("body").inner_text()[:5000]
            final_url = page.url
            if "captcha" in final_url or "login" in final_url or "登录" in body_text or "扫码" in body_text:
                return {"error": "login_required"}

            payload = page.evaluate(
                """
                () => {
                  const text = (el) => (el && el.innerText ? el.innerText.trim() : '');
                  const cards = [];
                  const nodes = Array.from(document.querySelectorAll('section.note-item, .note-item'));
                  for (const section of nodes) {
                    const linkEl = section.querySelector('a.cover, a[href*="/explore/"], a[href*="/user/profile/"]');
                    const href = linkEl ? (linkEl.getAttribute('href') || '') : '';
                    if (!href) continue;
                    const titleEl = section.querySelector('.title span, .title, [class*="title"]');
                    const raw = text(section);
                    const title = text(titleEl) || raw.split('\n').map(s => s.trim()).filter(Boolean)[0] || '';
                    cards.push({ href, title, raw });
                    if (cards.length >= 12) break;
                  }
                  const authorEl = document.querySelector('.user-name, .info .username, .user-nickname');
                  return { author: text(authorEl), cards };
                }
                """
            )
            return {"payload": payload}

        try:
            result = bridge.with_page(f"https://www.xiaohongshu.com/user/profile/{user_id}", _extract, wait_ms=7000)
        except ChromeSessionUnavailable as e:
            self.last_error = f"XHS browser-first unavailable: {e}"
            return []
        except Exception as e:
            self.last_error = f"XHS browser-first failed: {e}"
            return []

        if result.get("error") == "login_required":
            self.last_error = "XHS login_required: browser-first hit login/captcha wall"
            return []

        payload = result.get("payload") or {}
        items = []
        author = (payload or {}).get("author") or "小红书账号"
        seen_ids = set()
        for row in (payload or {}).get("cards") or []:
            href = (row.get("href") or "").strip()
            title = (row.get("title") or "").strip()
            raw = (row.get("raw") or "").strip()
            if not href:
                continue
            href = urllib.parse.urljoin("https://www.xiaohongshu.com", href)
            note_match = re.search(r"/explore/([a-f0-9]+)", href)
            if not note_match:
                note_match = re.search(r"/([a-f0-9]{24})(?:\?|$)", href)
            if not note_match:
                continue
            note_id = note_match.group(1)
            if note_id in seen_ids:
                continue
            seen_ids.add(note_id)
            items.append({
                "item_id": f"xiaohongshu_{hashlib.md5(note_id.encode()).hexdigest()[:12]}",
                "title": title or "(无标题)",
                "description": raw[:500],
                "link": f"https://www.xiaohongshu.com/explore/{note_id}",
                "pub_date": "",
                "metadata": {
                    "_channel_title": author,
                    "note_id": note_id,
                    "source": "chrome_session_bridge",
                }
            })

        if items:
            print(f"    ✓ XHS browser-first: {len(items)} notes")
            self.last_error = None
            return items[:10]

        self.last_error = "XHS browser-first opened page but found no note cards"
        return []

    def _fetch_via_playwright(self, user_id: str) -> List[Dict[str, Any]]:
        """Fetch user notes by intercepting XHR in a browser context with cookies."""
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
                    notify_macos(
                        title="Let's Go RSS",
                        subtitle="XHS cookies expired",
                        message="Run: npx rednote-mcp init",
                        dedupe_key="xhs_cookies_expired",
                    )
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
        route = f"/xiaohongshu/user/{user_id}/notes"
        print(f"    📡 RSSHub fallback: {route}")
        try:
            # Keep a tight budget for the fallback; rsshub_client handles
            # base rotation, this scraper's `get` honors timeout/retries.
            orig_to, orig_r = self.timeout, self.max_retries
            self.timeout, self.max_retries = 6, 1
            try:
                response = rsshub_client.fetch(route, self)
            finally:
                self.timeout, self.max_retries = orig_to, orig_r
            ct = response.headers.get("content-type", "")
            if "xml" in ct or "rss" in ct:
                parser = NativeRSSScraper()
                return parser.parse_rss_xml(response.text, "xiaohongshu")
            else:
                self.last_error = f"Non-RSS content from RSSHub (HTTP {response.status_code})"
                return []
        except Exception as e:
            self.last_error = str(e)
            print(f"    ❌ RSSHub fallback also failed: {e}")
            return []


# ============================================================
#  Tier 1c: Twitter/X via Syndication API (zero config)
# ============================================================

class TwitterScraper(BaseScraper):
    """Twitter/X scraper — uses the public Syndication API (no auth needed).

    Primary:  syndication.twitter.com (embedded __NEXT_DATA__ JSON)
    Fallback: RSSHub /twitter/user/{id} (requires TWITTER_AUTH_TOKEN in RSSHub)
    """

    SYNDICATION_URL = "https://syndication.twitter.com/srv/timeline-profile/screen-name/{username}"

    def extract_user_id(self, url: str) -> Optional[str]:
        """Extract username from x.com or twitter.com URL."""
        match = re.search(r"(?:x\.com|twitter\.com)/(@?[\w]+)", url)
        if match:
            username = match.group(1).lstrip("@")
            # Skip non-user paths
            if username.lower() in ("home", "explore", "search", "notifications",
                                     "messages", "settings", "i", "compose"):
                return None
            return username
        return None

    def fetch_items(self, url: str) -> List[Dict[str, Any]]:
        self.last_error = None
        username = self.extract_user_id(url)
        if not username:
            self.last_error = f"Cannot extract Twitter username from {url}"
            return []

        # Try Syndication API first (zero config)
        items = self._fetch_via_syndication(username)
        if items:
            return items

        # Fallback: RSSHub (needs TWITTER_AUTH_TOKEN configured in RSSHub)
        return self._fetch_via_rsshub(username)

    def _fetch_via_syndication(self, username: str) -> List[Dict[str, Any]]:
        """Fetch tweets from the public Twitter Syndication API."""
        syndication_url = self.SYNDICATION_URL.format(username=username)
        print(f"    📡 Twitter Syndication: {syndication_url}")

        try:
            response = self.get(syndication_url, timeout=15)
            html = response.text

            # Extract __NEXT_DATA__ JSON from the HTML page
            match = re.search(
                r'<script\s+id="__NEXT_DATA__"\s+type="application/json">\s*({.+?})\s*</script>',
                html, re.DOTALL
            )
            if not match:
                print("    ⚠️  Cannot find __NEXT_DATA__ in Syndication response")
                self.last_error = "No __NEXT_DATA__ in Syndication response"
                return []

            data = json.loads(match.group(1))
            timeline = (data.get("props", {})
                            .get("pageProps", {})
                            .get("timeline", {}))

            # timeline.entries[] contains tweet data
            entries = timeline.get("entries", [])
            if not entries:
                print("    ⚠️  No entries in Syndication timeline")
                self.last_error = "Empty timeline from Syndication API"
                return []

            # Extract user display name from first entry
            channel_title = ""

            items = []
            for entry in entries[:20]:
                content = entry.get("content", {})
                tweet = content.get("tweet", content)  # some entries nest under "tweet"

                tweet_id = tweet.get("id_str", "")
                if not tweet_id:
                    continue

                full_text = tweet.get("full_text", tweet.get("text", ""))
                created_at = tweet.get("created_at", "")
                screen_name = tweet.get("user", {}).get("screen_name", username)
                display_name = tweet.get("user", {}).get("name", "")

                if not channel_title and display_name:
                    channel_title = display_name

                # Parse created_at (format: "Thu Jun 19 02:01:31 +0000 2025")
                pub_date = ""
                if created_at:
                    try:
                        dt = datetime.strptime(created_at, "%a %b %d %H:%M:%S %z %Y")
                        pub_date = dt.isoformat()
                    except ValueError:
                        pass

                # Build tweet link
                link = f"https://x.com/{screen_name}/status/{tweet_id}"

                # Clean text: remove t.co URLs for cleaner title
                clean_text = re.sub(r'https?://t\.co/\S+', '', full_text).strip()
                # Truncate for title use
                title = clean_text[:120] + "..." if len(clean_text) > 120 else clean_text
                if not title:
                    title = f"Tweet by @{screen_name}"

                item_id = f"twitter_{hashlib.md5(tweet_id.encode()).hexdigest()[:12]}"
                items.append({
                    "item_id": item_id,
                    "title": title,
                    "description": full_text[:500],
                    "link": link,
                    "pub_date": pub_date,
                    "metadata": {
                        "_channel_title": channel_title or f"@{screen_name}",
                        "tweet_id": tweet_id,
                        "favorite_count": tweet.get("favorite_count", 0),
                        "retweet_count": tweet.get("retweet_count", 0),
                    }
                })

            if items:
                print(f"    ✓ Twitter: {len(items)} tweets via Syndication API")
            return items

        except Exception as e:
            self.last_error = str(e)
            print(f"    ⚠️  Twitter Syndication failed: {e}")
            return []

    def _fetch_via_rsshub(self, username: str) -> List[Dict[str, Any]]:
        """Fallback: fetch via RSSHub (requires TWITTER_AUTH_TOKEN)."""
        route = f"/twitter/user/{username}"
        print(f"    📡 RSSHub fallback: {route}")
        try:
            orig_to = self.timeout
            self.timeout = 10
            try:
                response = rsshub_client.fetch(route, self)
            finally:
                self.timeout = orig_to
            ct = response.headers.get("content-type", "")
            if "xml" in ct or "rss" in ct:
                parser = NativeRSSScraper()
                return parser.parse_rss_xml(response.text, "twitter")
            else:
                self.last_error = f"Non-RSS content from RSSHub (HTTP {response.status_code})"
                return []
        except Exception as e:
            self.last_error = str(e)
            print(f"    ❌ RSSHub fallback also failed: {e}")
            return []


# ============================================================
#  Tier 1d: 知识星球 via pub-api (zero config)
# ============================================================

class ZsxqScraper(BaseScraper):
    """知识星球 scraper — uses the public API (no auth needed for public groups).

    Primary:  pub-api.zsxq.com/v2/groups/{id} (zero config)
    Fallback: RSSHub /zsxq/group/{id} (requires ZSXQ_ACCESS_TOKEN in RSSHub)
    """

    PUB_API = "https://pub-api.zsxq.com/v2/groups/{group_id}"

    def extract_user_id(self, url: str) -> Optional[str]:
        """Extract group_id from various zsxq URL formats."""
        # Direct group_id in URL: wx.zsxq.com/group/123 or m.zsxq.com/groups/123
        match = re.search(r'zsxq\.com(?:/dweb2/index)?/groups?/(\d+)', url)
        if match:
            return match.group(1)

        # Short link: t.zsxq.com/xxxxx — need to follow redirect
        if 't.zsxq.com' in url:
            return self._resolve_short_link(url)

        # group_id as query parameter
        match = re.search(r'group_id=(\d+)', url)
        if match:
            return match.group(1)

        return None

    def _resolve_short_link(self, url: str) -> Optional[str]:
        """Follow t.zsxq.com short link to extract group_id."""
        try:
            response = self.get(url, timeout=10)
            # Check final URL for group_id
            final_url = str(response.url) if hasattr(response, 'url') else ''
            match = re.search(r'/groups?/(\d+)', final_url)
            if match:
                return match.group(1)
            # Also search in page HTML
            match = re.search(r'/groups?/(\d{10,})', response.text)
            if match:
                return match.group(1)
        except Exception as e:
            print(f"    ⚠️  Cannot resolve zsxq short link: {e}")
        return None

    def fetch_items(self, url: str) -> List[Dict[str, Any]]:
        self.last_error = None
        group_id = self.extract_user_id(url)
        if not group_id:
            self.last_error = f"Cannot extract 知识星球 group_id from {url}"
            return []

        # Try public API first (zero config)
        items = self._fetch_via_pub_api(group_id)
        if items:
            return items

        # Fallback: RSSHub (needs ZSXQ_ACCESS_TOKEN configured)
        return self._fetch_via_rsshub(group_id)

    def _fetch_via_pub_api(self, group_id: str) -> List[Dict[str, Any]]:
        """Fetch topics from the public 知识星球 API."""
        api_url = self.PUB_API.format(group_id=group_id)
        print(f"    📡 知识星球 pub-api: group/{group_id}")

        try:
            response = self.get(
                api_url,
                timeout=10,
                headers={
                    "Origin": "https://wx.zsxq.com",
                    "Referer": "https://wx.zsxq.com/",
                }
            )
            data = response.json()

            if not data.get("succeeded"):
                self.last_error = data.get("info", "API returned failure")
                return []

            resp = data.get("resp_data", {})
            group = resp.get("group", {})
            group_name = group.get("name", "")
            topics = resp.get("topics", [])

            if not topics:
                print("    ⚠️  No topics in pub-api response")
                self.last_error = "Empty topics from pub-api"
                return []

            items = []
            for topic in topics[:20]:
                topic_id = str(topic.get("topic_id", ""))
                if not topic_id:
                    continue

                # Extract text from talk/question/answer
                talk = topic.get("talk", {})
                question = topic.get("question", {})
                text = ""
                if talk:
                    text = talk.get("text", "")
                elif question:
                    text = question.get("text", "")

                title = topic.get("title", "") or text[:120]
                if not title:
                    title = f"知识星球主题 #{topic_id[-6:]}"

                # Clean title
                title = title.replace("\n", " ").strip()
                if len(title) > 120:
                    title = title[:120] + "..."

                # Parse create_time
                pub_date = ""
                create_time = topic.get("create_time", "")
                if create_time:
                    try:
                        dt = datetime.fromisoformat(create_time)
                        pub_date = dt.isoformat()
                    except ValueError:
                        pass

                link = f"https://wx.zsxq.com/topic/{topic_id}"
                item_id = f"zsxq_{hashlib.md5(topic_id.encode()).hexdigest()[:12]}"

                # Author
                author = ""
                owner = (talk or question).get("owner", {})
                if owner:
                    author = owner.get("name", "")

                items.append({
                    "item_id": item_id,
                    "title": title,
                    "description": text[:500] if text else "",
                    "link": link,
                    "pub_date": pub_date,
                    "metadata": {
                        "_channel_title": group_name,
                        "topic_id": topic_id,
                        "author": author,
                        "likes_count": topic.get("likes_count", 0),
                    }
                })

            if items:
                print(f"    ✓ 知识星球: {len(items)} topics via pub-api")
            return items

        except Exception as e:
            self.last_error = str(e)
            print(f"    ⚠️  知识星球 pub-api failed: {e}")
            return []

    def _fetch_via_rsshub(self, group_id: str) -> List[Dict[str, Any]]:
        """Fallback: fetch via RSSHub (requires ZSXQ_ACCESS_TOKEN)."""
        route = f"/zsxq/group/{group_id}"
        print(f"    📡 RSSHub fallback: {route}")
        try:
            orig_to = self.timeout
            self.timeout = 10
            try:
                response = rsshub_client.fetch(route, self)
            finally:
                self.timeout = orig_to
            ct = response.headers.get("content-type", "")
            if "xml" in ct or "rss" in ct:
                parser = NativeRSSScraper()
                return parser.parse_rss_xml(response.text, "zsxq")
            else:
                self.last_error = f"Non-RSS from RSSHub (HTTP {response.status_code})"
                return []
        except Exception as e:
            self.last_error = str(e)
            print(f"    ❌ RSSHub fallback also failed: {e}")
            return []


# ============================================================

def _wrap_with_playwright(scraper: "BaseScraper", platform: str) -> "BaseScraper":
    """Wrap a scraper so that, when RSS_PLAYWRIGHT_PLATFORMS enables this
    platform, we try our skill-owned Playwright Chromium first and fall
    back to the legacy scraper (RSSHub / Chrome CDP / etc.) on error or
    empty result. No-op if playwright is missing or the platform is not
    whitelisted — the original scraper is returned unchanged.
    """
    try:
        import playwright_adapter as _pw
    except ImportError:
        return scraper
    if not _pw.is_platform_enabled(platform):
        return scraper

    fetchers = {
        "bilibili": _pw.fetch_bilibili_user,
        "xiaohongshu": _pw.fetch_xhs_user,
        "twitter": _pw.fetch_twitter_user,
    }
    pw_fetch = fetchers.get(platform)
    if pw_fetch is None:
        return scraper

    legacy_fetch_items = scraper.fetch_items

    def _wrapped(url: str) -> List[Dict[str, Any]]:
        try:
            user_id = scraper.extract_user_id(url)
        except Exception:
            user_id = None
        if not user_id:
            return legacy_fetch_items(url)
        print(f"    🎭 playwright: {platform} user={user_id}")
        try:
            items = pw_fetch(str(user_id))
        except Exception as e:
            print(f"    ⚠️  playwright failed ({e}); falling back")
            scraper.last_error = None
            return legacy_fetch_items(url)
        if items:
            scraper.last_error = None
            return items
        return legacy_fetch_items(url)

    scraper.fetch_items = _wrapped  # type: ignore[assignment]
    return scraper


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
        elif "x.com" in url_lower or "twitter.com" in url_lower:
            return "twitter"
        elif "zsxq.com" in url_lower:
            return "zsxq"
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
            "twitter": TwitterScraper,
            "zsxq": ZsxqScraper,
        }

        scraper_class = scrapers.get(platform.lower())
        if not scraper_class:
            return None
        instance = scraper_class()
        return _wrap_with_playwright(instance, platform.lower())
