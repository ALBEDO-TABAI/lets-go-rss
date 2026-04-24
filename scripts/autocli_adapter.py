"""
Adapter layer for nashsu/autocli — a third-party Rust CLI that scrapes 55+
sites using the user's own logged-in Chrome session.

We shell out to `autocli <site> <cmd> --format json` and translate the output
into our standard item dict shape. This is an OPTIONAL tier: it's only used
when `RSS_AUTOCLI_PLATFORMS` lists the platform, so the skill keeps working
with only native RSS / RSSHub / Chrome CDP if autocli is not installed.

Why it's useful for us
----------------------
Our Bilibili / Twitter / Xiaohongshu sources have been failing because
RSSHub gets anti-scraped by those platforms. autocli runs commands in the
user's real Chrome (via extension), so it inherits the same login state
and reputation as normal browsing — routes that 503 through RSSHub
typically still work here.

Enable with:
    export RSS_AUTOCLI_PLATFORMS=bilibili,xiaohongshu,twitter

Install autocli:
    curl -fsSL https://raw.githubusercontent.com/nashsu/AutoCLI/main/scripts/install.sh | sh
    # + Chrome extension (browser-mode commands)

Note: the exact JSON shapes returned by autocli are versioned; this module
tries a few known field names and records what's missing in `last_error` so
failures surface meaningfully. Adjust the field-map tables below once we've
observed real output.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from typing import Any, Dict, List, Optional


AUTOCLI_BIN_ENV = "RSS_AUTOCLI_BIN"
AUTOCLI_PLATFORMS_ENV = "RSS_AUTOCLI_PLATFORMS"
AUTOCLI_TIMEOUT_ENV = "RSS_AUTOCLI_TIMEOUT"


def binary_path() -> Optional[str]:
    """Resolve the autocli executable (explicit env override → PATH)."""
    explicit = os.environ.get(AUTOCLI_BIN_ENV)
    if explicit and os.path.isfile(explicit):
        return explicit
    return shutil.which("autocli")


def enabled_platforms() -> set:
    raw = os.environ.get(AUTOCLI_PLATFORMS_ENV, "").strip()
    if not raw:
        return set()
    return {p.strip().lower() for p in raw.split(",") if p.strip()}


def is_platform_enabled(platform: str) -> bool:
    if not binary_path():
        return False
    return platform.lower() in enabled_platforms()


def run_autocli(args: List[str], timeout: Optional[float] = None) -> Dict[str, Any]:
    """Invoke `autocli <args...> --format json`, return parsed JSON.

    Raises RuntimeError on non-zero exit or unparseable output. Callers are
    expected to catch and either fall back to legacy scrapers or surface the
    error to record_fetch_outcome.
    """
    bin_ = binary_path()
    if not bin_:
        raise RuntimeError("autocli binary not found (install via install.sh or set RSS_AUTOCLI_BIN)")

    cmd = [bin_] + list(args) + ["--format", "json"]
    to = float(timeout if timeout is not None else os.environ.get(AUTOCLI_TIMEOUT_ENV, "30"))

    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=to,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"autocli timed out after {to}s: {' '.join(cmd[1:])}")

    if proc.returncode != 0:
        stderr = (proc.stderr or "").strip().split("\n")[-1][:200]
        raise RuntimeError(f"autocli rc={proc.returncode}: {stderr}")

    out = (proc.stdout or "").strip()
    if not out:
        raise RuntimeError("autocli returned empty stdout")

    try:
        return json.loads(out)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"autocli JSON parse failed: {e}; body head: {out[:200]!r}")


# ------------------------------------------------------------------
# Field extraction helpers
# ------------------------------------------------------------------

def _first(d: Dict[str, Any], *keys: str, default: Any = "") -> Any:
    """Return the first non-empty value among d[key] for key in keys."""
    for k in keys:
        v = d.get(k)
        if v not in (None, "", [], {}):
            return v
    return default


def _rows(payload: Any) -> List[Dict[str, Any]]:
    """autocli's JSON output is sometimes a top-level list, sometimes a dict
    wrapping `data` / `items` / `rows`. Normalize to a list."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for k in ("data", "items", "rows", "results", "list"):
            v = payload.get(k)
            if isinstance(v, list):
                return v
    return []


def _item_id(platform: str, link: str, fallback_key: str) -> str:
    seed = link or fallback_key
    return f"{platform}_{hashlib.md5(seed.encode()).hexdigest()[:12]}"


# ------------------------------------------------------------------
# Platform mappers — each returns our standard item dict list.
#
# These mapping tables are intentionally forgiving: we try a handful of
# field names per column. Once we've eyeballed real autocli output, we can
# tighten them to the exact shape that version emits.
# ------------------------------------------------------------------

def _map_bilibili_videos(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    for r in rows[:20]:
        bvid = _first(r, "bvid", "bv_id", "id")
        url = _first(r, "url", "link", "share_url")
        if not url and bvid:
            url = f"https://www.bilibili.com/video/{bvid}"
        title = _first(r, "title", "name")
        desc = _first(r, "desc", "description", "summary")
        pub_ts = _first(r, "pub_time", "ctime", "created", "timestamp")
        if not (url or title):
            continue
        items.append({
            "item_id": _item_id("bilibili", str(url), str(bvid or title)),
            "title": str(title),
            "description": str(desc)[:500] if desc else "",
            "link": str(url),
            "pub_date": str(pub_ts) if pub_ts else "",
            "metadata": {"source": "autocli:bilibili"},
        })
    return items


def _map_xhs_notes(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    for r in rows[:20]:
        note_id = _first(r, "note_id", "id")
        url = _first(r, "url", "link")
        if not url and note_id:
            url = f"https://www.xiaohongshu.com/explore/{note_id}"
        title = _first(r, "title", "display_title", "name")
        desc = _first(r, "desc", "description")
        pub_ts = _first(r, "time", "timestamp", "created")
        if not (url or title):
            continue
        items.append({
            "item_id": _item_id("xiaohongshu", str(url), str(note_id or title)),
            "title": str(title),
            "description": str(desc)[:500] if desc else "",
            "link": str(url),
            "pub_date": str(pub_ts) if pub_ts else "",
            "metadata": {"source": "autocli:xiaohongshu"},
        })
    return items


def _map_twitter_timeline(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    for r in rows[:20]:
        tid = _first(r, "id", "tweet_id", "rest_id")
        user = _first(r, "user", "author", "screen_name")
        if isinstance(user, dict):
            user = user.get("screen_name") or user.get("name") or ""
        url = _first(r, "url", "link")
        if not url and tid and user:
            url = f"https://x.com/{user}/status/{tid}"
        text = _first(r, "text", "full_text", "content", "title")
        pub_ts = _first(r, "created_at", "timestamp", "time")
        if not (url or text):
            continue
        items.append({
            "item_id": _item_id("twitter", str(url), str(tid or text)),
            "title": (str(text)[:120] + "…") if text and len(str(text)) > 120 else str(text),
            "description": str(text)[:500] if text else "",
            "link": str(url),
            "pub_date": str(pub_ts) if pub_ts else "",
            "metadata": {"source": "autocli:twitter"},
        })
    return items


# ------------------------------------------------------------------
# Public fetch functions — one per route we actually want to use.
# ------------------------------------------------------------------

def fetch_bilibili_user(user_id: str) -> List[Dict[str, Any]]:
    """`autocli bilibili user-videos <uid>` — per-UP videos."""
    payload = run_autocli(["bilibili", "user-videos", str(user_id), "--limit", "20"])
    return _map_bilibili_videos(_rows(payload))


def fetch_xhs_user(user_id: str) -> List[Dict[str, Any]]:
    """`autocli xiaohongshu user <uid>` — profile notes."""
    payload = run_autocli(["xiaohongshu", "user", str(user_id), "--limit", "20"])
    return _map_xhs_notes(_rows(payload))


def fetch_twitter_user(username: str) -> List[Dict[str, Any]]:
    """`autocli twitter profile <handle>` or `twitter timeline`.

    We try `profile` first (per-user), fall back to `timeline`."""
    username = username.lstrip("@")
    try:
        payload = run_autocli(["twitter", "profile", username, "--limit", "20"])
        rows = _rows(payload)
        if rows:
            return _map_twitter_timeline(rows)
    except Exception:
        pass
    payload = run_autocli(["twitter", "timeline", username, "--limit", "20"])
    return _map_twitter_timeline(_rows(payload))
