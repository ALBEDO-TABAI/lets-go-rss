"""
Skill-owned Playwright Chromium — scrapes platforms that anti-scrape RSSHub.

Why this exists
---------------
Bilibili / Twitter / Xiaohongshu all 风控-block server-side scrapers (RSSHub
routes get 503 / JSON error envelopes). A real browser with a normal
reputation gets through. We launch our own headless Chromium via Playwright
(already a pip dep + its Chromium is already cached in
~/Library/Caches/ms-playwright/), using a persistent user-data-dir so
login state (when needed) sticks between runs.

Zero user intervention required for:
  * Bilibili user videos       — public data, no login
  * Bilibili user dynamic      — public for most users
(Twitter timeline / XHS notes do require a one-time login via
 `python scripts/lets_go_rss.py --login <platform>`.)

Enable in .env:
    RSS_PLAYWRIGHT_PLATFORMS=bilibili,xiaohongshu,twitter

Concurrency note
----------------
Playwright's sync API is strict single-thread (the BrowserContext belongs
to the thread that created it). rss_engine uses a ThreadPoolExecutor for
parallel fetches, so we route ALL Playwright work through a single
dedicated worker thread via a ThreadPoolExecutor(max_workers=1). Other
fetch threads submit jobs and block on the returned Future.
"""

from __future__ import annotations

import hashlib
import os
import re
import sys
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional


# ---------------------------------------------------------------------------
# Config / env
# ---------------------------------------------------------------------------

PROFILE_DIR = Path(os.environ.get(
    "RSS_PLAYWRIGHT_PROFILE",
    str(Path.home() / ".lets-go-rss" / "browser-profile"),
))
DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def enabled_platforms() -> set:
    raw = os.environ.get("RSS_PLAYWRIGHT_PLATFORMS", "").strip()
    if not raw:
        return set()
    return {p.strip().lower() for p in raw.split(",") if p.strip()}


def is_platform_enabled(platform: str) -> bool:
    return platform.lower() in enabled_platforms()


# ---------------------------------------------------------------------------
# Dedicated single-thread worker — owns Playwright + the BrowserContext
# ---------------------------------------------------------------------------
#
# Sync Playwright objects are bound to their creator thread, so all calls
# must run on the SAME thread. We use an Executor(max_workers=1) as that
# thread, with thread-local initialization that spins up Playwright on
# first use. Other threads submit jobs via `_submit()` and wait on the
# returned Future.

_worker_init_lock = threading.Lock()
_worker_executor: Optional[ThreadPoolExecutor] = None
_thread_local = threading.local()


def _worker_init():
    """Initializer running inside the worker thread itself — creates
    Playwright + a persistent BrowserContext bound to this thread."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:
        _thread_local.init_error = (
            "playwright is not installed. Run: pip install playwright && "
            "python -m playwright install chromium"
        )
        return

    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        pw = sync_playwright().start()
        ctx = pw.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            headless=True,
            viewport={"width": 1280, "height": 800},
            user_agent=DEFAULT_UA,
            args=["--disable-blink-features=AutomationControlled"],
        )
        _thread_local.pw = pw
        _thread_local.ctx = ctx
        _thread_local.init_error = None
    except Exception as e:
        _thread_local.init_error = f"playwright init failed: {e}"


def _ensure_executor() -> ThreadPoolExecutor:
    global _worker_executor
    with _worker_init_lock:
        if _worker_executor is None:
            _worker_executor = ThreadPoolExecutor(
                max_workers=1, thread_name_prefix="rss-pw",
                initializer=_worker_init,
            )
            import atexit
            atexit.register(_teardown)
    return _worker_executor


def _teardown():
    global _worker_executor
    if _worker_executor is None:
        return
    # Submit a close job on the worker thread
    try:
        _worker_executor.submit(_close_context).result(timeout=5)
    except Exception:
        pass
    _worker_executor.shutdown(wait=False, cancel_futures=True)
    _worker_executor = None


def _close_context():
    ctx = getattr(_thread_local, "ctx", None)
    pw = getattr(_thread_local, "pw", None)
    try:
        if ctx is not None:
            ctx.close()
    except Exception:
        pass
    try:
        if pw is not None:
            pw.stop()
    except Exception:
        pass


def _run_on_worker(job_fn: Callable[[Any], Any], *, page_timeout: float = 30.0):
    """Execute `job_fn(page)` on the worker thread using the shared context.

    The worker thread owns Playwright; this helper creates a fresh Page,
    passes it to the callback, and closes it afterwards. Errors propagate
    back to the caller through the Future.
    """
    def _run():
        if getattr(_thread_local, "init_error", None):
            raise RuntimeError(_thread_local.init_error)
        ctx = getattr(_thread_local, "ctx", None)
        if ctx is None:
            raise RuntimeError("playwright worker not initialised")
        page = ctx.new_page()
        page.set_default_navigation_timeout(int(page_timeout * 1000))
        try:
            return job_fn(page)
        finally:
            try:
                page.close()
            except Exception:
                pass

    exec_ = _ensure_executor()
    fut = exec_.submit(_run)
    # Cap wait at page_timeout + a margin; lets a stuck call fail loudly
    return fut.result(timeout=page_timeout + 10.0)


# ---------------------------------------------------------------------------
# Item mapping helpers
# ---------------------------------------------------------------------------

def _item_id(platform: str, seed: str) -> str:
    return f"{platform}_{hashlib.md5(seed.encode()).hexdigest()[:12]}"


def _clip(s: Any, n: int = 500) -> str:
    if not s:
        return ""
    s = str(s)
    return s[:n]


# ---------------------------------------------------------------------------
# Platform: Bilibili — public, no login needed
# ---------------------------------------------------------------------------

def fetch_bilibili_user(uid: str, timeout: float = 25.0) -> List[Dict[str, Any]]:
    """Fetch a Bilibili UP's recent videos.

    Strategy: visit space.bilibili.com/<uid>, block on the XHR response to
    /x/space/(wbi/)?arc/search (contains vlist). Bilibili's anti-bot
    happily serves real cookies to a legit browser, so no login is needed.
    """
    def _is_arc_search(response) -> bool:
        u = response.url
        return ("/x/space/wbi/arc/search" in u
                or "/x/space/arc/search" in u)

    def _job(page):
        with page.expect_response(_is_arc_search, timeout=int(timeout * 1000)) as info:
            page.goto(
                f"https://space.bilibili.com/{uid}",
                wait_until="domcontentloaded",
            )
        response = info.value
        if response.status != 200:
            raise RuntimeError(f"arc/search returned HTTP {response.status}")
        data = response.json()
        if not isinstance(data, dict) or data.get("code") != 0:
            code = data.get("code") if isinstance(data, dict) else None
            msg = data.get("message") if isinstance(data, dict) else None
            raise RuntimeError(f"arc/search code={code} msg={msg}")
        return (data.get("data", {}).get("list", {}).get("vlist") or [])

    # Bilibili's anti-bot occasionally 412s — retry once after a cool-down
    # the browser state usually clears in 2-3s.
    vlist = None
    last_err: Optional[Exception] = None
    for attempt in range(2):
        try:
            vlist = _run_on_worker(_job, page_timeout=timeout)
            if vlist:
                break
        except Exception as e:
            last_err = e
            msg = str(e).lower()
            # Only retry on transient-looking errors
            if "412" not in msg and "timeout" not in msg and "timed out" not in msg:
                raise
        if attempt == 0:
            time.sleep(3.0)

    if not vlist:
        if last_err:
            raise last_err
        raise RuntimeError("Bilibili arc/search returned empty vlist")

    items: List[Dict[str, Any]] = []
    for v in vlist[:20]:
        bvid = v.get("bvid") or ""
        link = f"https://www.bilibili.com/video/{bvid}" if bvid else ""
        title = v.get("title") or ""
        # Bilibili `created` is unix seconds
        created = v.get("created")
        pub_date = ""
        if created:
            try:
                from datetime import datetime as _dt
                pub_date = _dt.fromtimestamp(int(created)).isoformat()
            except Exception:
                pass
        items.append({
            "item_id": _item_id("bilibili", bvid or link or title),
            "title": title,
            "description": _clip(v.get("description")),
            "link": link,
            "pub_date": pub_date,
            "metadata": {
                "source": "playwright:bilibili",
                "_channel_title": v.get("author") or "",
                "duration": v.get("length") or "",
                "play": v.get("play") or 0,
            },
        })
    return items


# ---------------------------------------------------------------------------
# Platform: Xiaohongshu — requires one-time login (note: platform constraint)
# ---------------------------------------------------------------------------

def fetch_xhs_user(user_id: str, timeout: float = 25.0) -> List[Dict[str, Any]]:
    """Fetch an XHS user's recent notes via our managed profile.

    If the profile is not logged in, XHS redirects to captcha/login — we
    raise a clear error that maps to `auth` error_kind.
    """
    captured: Dict[str, Any] = {"notes": None}

    def _on_response(response):
        try:
            if "user_posted" in response.url or "user/posted" in response.url:
                data = response.json()
                notes = (data.get("data") or {}).get("notes", [])
                if notes and captured["notes"] is None:
                    captured["notes"] = notes
        except Exception:
            pass

    def _job(page):
        page.on("response", _on_response)
        page.goto(
            f"https://www.xiaohongshu.com/user/profile/{user_id}",
            wait_until="domcontentloaded",
        )
        final_url = page.url
        if "captcha" in final_url or "login" in final_url:
            raise RuntimeError(
                "XHS profile not logged in — run: "
                "python scripts/lets_go_rss.py --login xiaohongshu"
            )
        # Wait for XHR
        deadline = time.time() + 10.0
        while time.time() < deadline and captured["notes"] is None:
            page.wait_for_timeout(500)
        return captured["notes"]

    notes = _run_on_worker(_job, page_timeout=timeout)
    if not notes:
        raise RuntimeError("XHS note XHR capture failed (no notes returned)")

    items: List[Dict[str, Any]] = []
    for n in notes[:20]:
        note_id = n.get("note_id") or n.get("id") or ""
        link = f"https://www.xiaohongshu.com/explore/{note_id}" if note_id else ""
        title = n.get("display_title") or n.get("title") or ""
        items.append({
            "item_id": _item_id("xiaohongshu", note_id or link or title),
            "title": title,
            "description": _clip(n.get("desc") or n.get("description")),
            "link": link,
            "pub_date": "",
            "metadata": {"source": "playwright:xiaohongshu"},
        })
    return items


# ---------------------------------------------------------------------------
# Platform: Twitter/X — requires one-time login
# ---------------------------------------------------------------------------

def fetch_twitter_user(username: str, timeout: float = 25.0) -> List[Dict[str, Any]]:
    """Fetch an X/Twitter user's recent tweets via our managed profile.

    Twitter requires login to view timelines as of 2024+. After one-time
    `--login twitter`, cookies persist in the profile.
    """
    username = username.lstrip("@")
    captured: Dict[str, Any] = {"tweets": []}

    tweet_pattern = re.compile(r"status/(\d+)")

    def _on_response(response):
        try:
            url = response.url
            if "UserTweets" in url and response.status == 200:
                data = response.json()
                # Twitter GraphQL: dig through timeline instructions
                entries = _extract_tweet_entries(data)
                if entries:
                    captured["tweets"].extend(entries)
        except Exception:
            pass

    def _job(page):
        page.on("response", _on_response)
        page.goto(f"https://x.com/{username}", wait_until="domcontentloaded")
        final = page.url
        if "login" in final or "/i/flow/login" in final:
            raise RuntimeError(
                "Twitter not logged in — run: "
                "python scripts/lets_go_rss.py --login twitter"
            )
        deadline = time.time() + 10.0
        while time.time() < deadline and not captured["tweets"]:
            page.wait_for_timeout(500)
        return captured["tweets"]

    tweets = _run_on_worker(_job, page_timeout=timeout)
    if not tweets:
        raise RuntimeError("Twitter XHR capture failed (timeline empty or blocked)")

    items: List[Dict[str, Any]] = []
    seen_ids: set = set()
    for t in tweets[:30]:
        tid = t.get("rest_id") or t.get("id_str")
        if not tid or tid in seen_ids:
            continue
        seen_ids.add(tid)
        text = t.get("full_text") or ""
        link = f"https://x.com/{username}/status/{tid}"
        items.append({
            "item_id": _item_id("twitter", tid),
            "title": (text[:120] + "…") if len(text) > 120 else text,
            "description": _clip(text),
            "link": link,
            "pub_date": t.get("created_at") or "",
            "metadata": {"source": "playwright:twitter"},
        })
        if len(items) >= 20:
            break
    return items


def _extract_tweet_entries(payload: Any) -> List[Dict[str, Any]]:
    """Walk Twitter's GraphQL timeline JSON, yielding tweet result dicts.
    Layout is nested: data.user.result.timeline_v2.timeline.instructions[].entries[].
    Each entry contains content.itemContent.tweet_results.result with rest_id + legacy.full_text.
    """
    out: List[Dict[str, Any]] = []
    try:
        instructions = (
            payload.get("data", {})
            .get("user", {})
            .get("result", {})
            .get("timeline_v2", {})
            .get("timeline", {})
            .get("instructions", [])
        )
    except AttributeError:
        return out

    for ins in instructions:
        for entry in ins.get("entries", []) or []:
            content = entry.get("content", {}) or {}
            item_content = content.get("itemContent") or {}
            tw = (item_content.get("tweet_results") or {}).get("result") or {}
            if not tw:
                continue
            legacy = tw.get("legacy") or {}
            out.append({
                "rest_id": tw.get("rest_id") or legacy.get("id_str"),
                "id_str": legacy.get("id_str"),
                "full_text": legacy.get("full_text") or "",
                "created_at": legacy.get("created_at") or "",
            })
    return out


# ---------------------------------------------------------------------------
# Interactive login — opens a visible browser for the user to sign in
# ---------------------------------------------------------------------------

LOGIN_URLS = {
    "twitter": "https://x.com/i/flow/login",
    "xiaohongshu": "https://www.xiaohongshu.com/",
    "bilibili": "https://passport.bilibili.com/login",
}


def login_platform(platform: str) -> int:
    """Open a visible Chromium window pointed at the platform's login page.
    Blocks until the user closes the window. Cookies persist in our profile.
    """
    platform = platform.lower()
    if platform not in LOGIN_URLS:
        print(f"Unknown platform: {platform!r}. Supported: {list(LOGIN_URLS)}",
              file=sys.stderr)
        return 2

    # Tear down any existing headless context; we want a fresh visible one.
    _teardown()

    from playwright.sync_api import sync_playwright
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            headless=False,
            viewport={"width": 1280, "height": 800},
            user_agent=DEFAULT_UA,
        )
        page = ctx.new_page()
        page.goto(LOGIN_URLS[platform])
        print(f"[login] opened {LOGIN_URLS[platform]}")
        print("[login] sign in, then close the browser window to finish.")
        try:
            # Wait for browser window to close
            page.wait_for_event("close", timeout=0)
        except Exception:
            pass
        ctx.close()
    print(f"[login] done — cookies saved to {PROFILE_DIR}")
    return 0
