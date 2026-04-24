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
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-features=IsolateOrigins,site-per-process",
            ],
            ignore_default_args=["--enable-automation"],
        )
        ctx.add_init_script(_STEALTH_INIT_JS)
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

    XHS renders the profile's notes list server-side into DOM (no user_posted
    XHR any more as of 2025+). We parse `section.note-item` cards directly.
    If the profile is not logged in, XHS redirects to captcha/login — raised
    so the error_kind is tagged `auth`.
    """
    def _job(page):
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
        # Let the note grid render (lazy + image loading doesn't matter to us)
        try:
            page.wait_for_selector("section.note-item", timeout=8000)
        except Exception:
            pass
        # Extract note cards in a single evaluate (robust to DOM churn)
        return page.evaluate("""() => {
            const out = [];
            const cards = document.querySelectorAll('section.note-item');
            for (const c of cards) {
                const a = c.querySelector("a[href*='/explore/']") ||
                          c.querySelector("a[href*='/user/profile/']");
                if (!a) continue;
                const href = a.getAttribute('href') || '';
                const m = href.match(/\\/explore\\/([0-9a-f]+)|\\/profile\\/[^/]+\\/([0-9a-f]+)/i);
                const noteId = m ? (m[1] || m[2]) : '';
                if (!noteId) continue;
                // Title: first non-empty innerText line that isn't the pinned badge
                const lines = (c.innerText || '').split('\\n').map(s => s.trim()).filter(Boolean);
                const title = lines.find(l => l !== '置顶') || '';
                out.push({ note_id: noteId, title });
                if (out.length >= 20) break;
            }
            return out;
        }""")

    raw = _run_on_worker(_job, page_timeout=timeout)
    if not raw:
        raise RuntimeError("XHS DOM extraction returned no notes")

    items: List[Dict[str, Any]] = []
    for n in raw:
        note_id = n.get("note_id", "")
        title = (n.get("title") or "").strip()
        if not note_id:
            continue
        link = f"https://www.xiaohongshu.com/explore/{note_id}"
        items.append({
            "item_id": _item_id("xiaohongshu", note_id),
            "title": title,
            "description": "",
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


_STEALTH_INIT_JS = """
// Hide classic automation signals that sites like x.com check on keystroke.
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
// Fill in plugins / languages to match a real browser session
Object.defineProperty(navigator, 'plugins', { get: () => [1,2,3,4,5] });
Object.defineProperty(navigator, 'languages', { get: () => ['zh-CN','zh','en-US','en'] });
// Provide a believable chrome.runtime object
window.chrome = window.chrome || { runtime: {}, app: {}, csi: function(){}, loadTimes: function(){} };
// Permissions.query('notifications') must behave like a real browser
const origQuery = navigator.permissions && navigator.permissions.query;
if (origQuery) {
  navigator.permissions.query = (params) =>
    params && params.name === 'notifications'
      ? Promise.resolve({ state: Notification.permission })
      : origQuery.call(navigator.permissions, params);
}
"""


def _open_login_window(platform: str) -> None:
    """Open a visible real Chrome pointed at the platform's login page.
    Blocks until the user closes the window. Cookies persist in our profile.

    Uses `channel='chrome'` (the user's real Google Chrome binary) when
    available, falling back to bundled Chromium. Real Chrome is dramatically
    harder for sites like x.com to fingerprint as automation. We also strip
    the `--enable-automation` launch flag and inject a stealth init script
    (navigator.webdriver / plugins / languages / chrome.runtime).
    """
    _teardown()  # release any headless context first

    from playwright.sync_api import sync_playwright
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)

    def _launch(pw, channel: Optional[str]):
        kwargs = dict(
            user_data_dir=str(PROFILE_DIR),
            headless=False,
            viewport={"width": 1280, "height": 800},
            user_agent=DEFAULT_UA,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-features=IsolateOrigins,site-per-process",
            ],
            ignore_default_args=["--enable-automation"],
        )
        if channel:
            kwargs["channel"] = channel
        return pw.chromium.launch_persistent_context(**kwargs)

    with sync_playwright() as pw:
        ctx = None
        last_err = None
        # Prefer real Chrome, then stable Chrome channels, then bundled Chromium
        for channel in ("chrome", "chrome-beta", "chrome-dev", None):
            try:
                ctx = _launch(pw, channel)
                if channel:
                    print(f"[login] launched real Chrome (channel={channel})")
                else:
                    print("[login] launched bundled Chromium (no real Chrome found)")
                break
            except Exception as e:
                last_err = e
                continue
        if ctx is None:
            raise RuntimeError(f"could not launch any Chrome channel: {last_err}")

        ctx.add_init_script(_STEALTH_INIT_JS)
        page = ctx.new_page()
        page.goto(LOGIN_URLS[platform])
        print(f"[login] opened {LOGIN_URLS[platform]}")
        print("[login] sign in, then close the Chromium window to continue.")
        try:
            page.wait_for_event("close", timeout=0)
        except Exception:
            pass
        ctx.close()


def _pick_test_user(platform: str, db_path: str) -> Optional[str]:
    """Return a user_id/username from an existing DB subscription of this
    platform, for post-login verification. Returns None if no sub exists."""
    import sqlite3
    try:
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        cur.execute(
            "SELECT url FROM subscriptions WHERE platform=? LIMIT 1",
            (platform,),
        )
        row = cur.fetchone()
        conn.close()
    except Exception:
        return None
    if not row:
        return None
    url = row[0]
    # Reuse the scraper's own extract_user_id logic
    try:
        sys.path.insert(0, str(Path(__file__).parent))
        from scrapers import ScraperFactory
        scraper = ScraperFactory.get_scraper(platform)
        if scraper is None:
            return None
        return scraper.extract_user_id(url)
    except Exception:
        return None


def _verify_platform(platform: str, db_path: str) -> Dict[str, Any]:
    """Run a test fetch against an existing subscription of this platform.

    Returns {"ok": bool, "detail": str, "items": int}."""
    user_id = _pick_test_user(platform, db_path)
    if not user_id:
        return {"ok": False, "detail": "no existing subscription to test against", "items": 0}

    fetchers = {
        "bilibili": fetch_bilibili_user,
        "xiaohongshu": fetch_xhs_user,
        "twitter": fetch_twitter_user,
    }
    fn = fetchers.get(platform)
    if not fn:
        return {"ok": False, "detail": f"no fetcher for {platform}", "items": 0}

    try:
        items = fn(user_id)
    except Exception as e:
        return {"ok": False, "detail": str(e)[:150], "items": 0}
    return {"ok": bool(items), "detail": f"fetched user={user_id}", "items": len(items)}


def _enable_platform_in_env(platform: str, env_path: Path) -> bool:
    """Idempotently add `platform` to RSS_PLAYWRIGHT_PLATFORMS in .env.

    Creates .env if missing. Returns True if the file was written."""
    lines: List[str] = []
    if env_path.exists():
        lines = env_path.read_text(encoding="utf-8").splitlines()

    # Find existing active line (not a comment)
    key = "RSS_PLAYWRIGHT_PLATFORMS"
    found_idx = -1
    current_list: List[str] = []
    for i, line in enumerate(lines):
        stripped = line.lstrip()
        if stripped.startswith("#"):
            continue
        if stripped.startswith(f"{key}="):
            found_idx = i
            raw = stripped.split("=", 1)[1].strip().strip('"').strip("'")
            current_list = [p.strip() for p in raw.split(",") if p.strip()]
            break

    if platform in current_list:
        return False  # already enabled; no write needed

    # If no existing value, seed with the cron default so we don't silently
    # demote bilibili (which run_update_cron.sh enables by default).
    if not current_list:
        current_list = ["bilibili"]

    new_list = current_list + [p for p in [platform] if p not in current_list]
    new_line = f"{key}={','.join(new_list)}"
    if found_idx >= 0:
        lines[found_idx] = new_line
    else:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append(f"# Added by --login {platform} verification flow")
        lines.append(new_line)

    env_path.parent.mkdir(parents=True, exist_ok=True)
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return True


def run_login_flow(platform: str, *, skill_dir: Path, db_path: str,
                   verify: bool = True, enable: bool = True) -> int:
    """End-to-end: open browser → user logs in → verify → persist in .env.

    This is the one-stop "set up platform X" entry point for an agent to
    orchestrate. Returns 0 on success, nonzero on failure.
    """
    platform = platform.lower()
    if platform not in LOGIN_URLS:
        print(f"Unknown platform: {platform!r}. Supported: {list(LOGIN_URLS)}",
              file=sys.stderr)
        return 2

    print(f"\n[{platform}] step 1/3: opening Chromium for you to sign in …")
    _open_login_window(platform)

    if not verify:
        print(f"[{platform}] step 2/3: skipped (verify=False)")
    else:
        print(f"[{platform}] step 2/3: verifying login by fetching a real sub …")
        result = _verify_platform(platform, db_path)
        if not result["ok"]:
            print(f"[{platform}] ❌ verification failed: {result['detail']}")
            print(f"[{platform}] cookies may not have been saved, or platform")
            print(f"[{platform}] still flags us as non-logged-in. Try again.")
            return 3
        print(f"[{platform}] ✅ verified: got {result['items']} items ({result['detail']})")

    if not enable:
        print(f"[{platform}] step 3/3: skipped (enable=False)")
        return 0

    env_path = skill_dir / ".env"
    changed = _enable_platform_in_env(platform, env_path)
    if changed:
        print(f"[{platform}] ✅ step 3/3: enabled in {env_path}")
    else:
        print(f"[{platform}] step 3/3: already enabled in {env_path} (no change)")
    return 0


def login_platform(platform: str) -> int:
    """Back-compat wrapper — opens the browser but does NOT verify/enable.
    Prefer `run_login_flow` for the end-to-end path."""
    platform = platform.lower()
    if platform not in LOGIN_URLS:
        print(f"Unknown platform: {platform!r}. Supported: {list(LOGIN_URLS)}",
              file=sys.stderr)
        return 2
    _open_login_window(platform)
    print(f"[login] done — cookies saved to {PROFILE_DIR}")
    return 0
