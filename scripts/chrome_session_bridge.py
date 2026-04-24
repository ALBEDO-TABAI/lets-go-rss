#!/usr/bin/env python3
"""
Chrome session bridge for lets-go-rss.

Goal:
- Reuse the user's real Chrome session when remote debugging is enabled.
- Keep the implementation skill-local and portable across machines.
- Avoid hard-coding machine-specific paths beyond standard Chrome defaults.

Design:
- Discover DevToolsActivePort from standard Chrome profile locations.
- Connect over CDP via Playwright.
- Reuse an existing browser context if available.
- Open a temporary tab, extract page text / DOM payload, then close the tab.

Notes:
- This does NOT keep a process permanently running by itself yet.
- It is a bridge layer that allows the skill to connect to an already-enabled
  user Chrome session in a portable way.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Callable, Any, Optional


class ChromeSessionUnavailable(RuntimeError):
    pass


class ChromeSessionBridge:
    def __init__(self):
        self.chrome_name = os.environ.get("RSS_CHROME_NAME", "Google/Chrome")

    def _candidate_devtools_files(self) -> list[Path]:
        home = Path.home()
        candidates = [
            home / "Library/Application Support/Google/Chrome/DevToolsActivePort",  # macOS Chrome stable
            home / "Library/Application Support/Chromium/DevToolsActivePort",       # macOS Chromium
            home / ".config/google-chrome/DevToolsActivePort",                      # Linux Chrome
            home / ".config/chromium/DevToolsActivePort",                           # Linux Chromium
        ]
        custom = os.environ.get("RSS_CHROME_DEVTOOLS_FILE")
        if custom:
            candidates.insert(0, Path(custom).expanduser())
        return candidates

    def discover_ws_url(self) -> str:
        for path in self._candidate_devtools_files():
            if not path.exists():
                continue
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
                if len(lines) >= 2:
                    port = lines[0].strip()
                    ws_path = lines[1].strip()
                    if port and ws_path:
                        return f"ws://127.0.0.1:{port}{ws_path}"
            except Exception:
                continue
        raise ChromeSessionUnavailable(
            "Chrome remote debugging not available. Open Chrome, enable remote debugging, then retry."
        )

    def with_page(self, url: str, callback: Callable[[Any], Any], wait_ms: int = 6000) -> Any:
        try:
            from playwright.sync_api import sync_playwright
        except Exception as e:
            raise ChromeSessionUnavailable(f"Playwright unavailable: {e}")

        ws_url = self.discover_ws_url()

        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(ws_url)
            context = browser.contexts[0] if browser.contexts else browser.new_context()
            page = context.new_page()
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(wait_ms)
                return callback(page)
            finally:
                try:
                    page.close()
                except Exception:
                    pass
                try:
                    browser.close()
                except Exception:
                    pass

    def extract_basic_page_state(self, url: str, wait_ms: int = 6000) -> dict:
        def _cb(page):
            body = ""
            try:
                body = page.locator("body").inner_text()[:4000]
            except Exception:
                pass
            return {
                "url": page.url,
                "title": page.title(),
                "body": body,
            }
        return self.with_page(url, _cb, wait_ms=wait_ms)
