#!/usr/bin/env python3
"""
Let's Go RSS - Main entry point
Lightweight RSS subscription manager for multiple platforms.
"""

import sys
import os
from pathlib import Path

# Resolve directories relative to this script
SKILL_DIR = Path(__file__).parent.parent
SCRIPTS_DIR = SKILL_DIR / "scripts"
ASSETS_DIR = SKILL_DIR / "assets"

# Add scripts directory to Python path
sys.path.insert(0, str(SCRIPTS_DIR))

# Ensure assets directory exists
ASSETS_DIR.mkdir(exist_ok=True)


def ensure_dependencies():
    """Check and install dependencies if needed."""
    setup_script = SCRIPTS_DIR / "setup.py"
    if not setup_script.exists():
        return True

    import subprocess
    result = subprocess.run(
        [sys.executable, str(setup_script)],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        print("⚠️  Setup check completed with warnings")
        return False
    return True


def print_cached_status() -> int:
    """Print cached report directly without importing engine dependencies."""
    report_path = ASSETS_DIR / "latest_update.md"
    if report_path.exists():
        print(report_path.read_text(encoding="utf-8"))
        return 0
    print("⚠️ 尚无缓存报告。请先运行 --update 生成。")
    return 0


def run_doctor(auto_fix: bool = False) -> int:
    """Print a health snapshot and optionally auto-fix what can be fixed.

    Auto-fixable: stale rsshub pidfile, unhealthy :1201 (restart worker).
    Not auto-fixable (printed as actionables): XHS cookies expired, missing
    ANTHROPIC_API_KEY.
    """
    import json
    import shutil
    import subprocess as _sp
    import urllib.request

    sys.path.insert(0, str(SCRIPTS_DIR))

    print("# Let's Go RSS — Doctor\n")

    # Runtime deps
    print("## 运行时")
    for name in ("python3", "node", "npx", "yt-dlp"):
        where = shutil.which(name)
        print(f"- {name}: {where or '❌ MISSING'}")
    try:
        import httpx  # noqa: F401
        print("- httpx (py): OK")
    except Exception as e:
        print(f"- httpx (py): ❌ {e}")
    try:
        import anthropic  # noqa: F401
        print("- anthropic (py): OK")
    except Exception as e:
        print(f"- anthropic (py): ⚠️ {e}")

    key = os.environ.get("ANTHROPIC_API_KEY")
    if key:
        print(f"- ANTHROPIC_API_KEY: ✅ present (len={len(key)})")
    else:
        env_path = SKILL_DIR / ".env"
        hint = f" (put it in {env_path})" if not env_path.exists() else ""
        print(f"- ANTHROPIC_API_KEY: ⚠️ missing — classifier will use keyword fallback{hint}")

    # RSSHub
    print("\n## RSSHub")
    for label, url in (("primary (:1201, managed)", "http://127.0.0.1:1201/healthz"),
                       ("fallback (:1200, ready-cowork)", "http://127.0.0.1:1200/healthz")):
        try:
            with urllib.request.urlopen(url, timeout=1.5) as r:
                print(f"- {label}: ✅ {r.status}")
        except Exception as e:
            print(f"- {label}: ❌ {e}")

    # Sub health
    try:
        from database import RSSDatabase
        db = RSSDatabase(str(ASSETS_DIR / "rss_database.db"))
        subs = db.get_subscriptions()
        worst = sorted(
            [s for s in subs if (s.get("consecutive_failures") or 0) > 0],
            key=lambda s: -(s.get("consecutive_failures") or 0),
        )[:5]
        print(f"\n## 源健康 ({len(subs)} 总)")
        if worst:
            print("最不健康的源:")
            for s in worst:
                print(f"- {s.get('title') or s['platform']}: "
                      f"fails={s.get('consecutive_failures')} "
                      f"kind={s.get('last_error_kind')} "
                      f"error={(s.get('last_error') or '')[:80]}")
        else:
            print("全部健康 ✅")
    except Exception as e:
        print(f"⚠️ failed to read DB: {e}")

    # XHS cookies
    xhs_cookie = Path(os.path.expanduser("~/.mcp/rednote/cookies.json"))
    print("\n## XHS cookies")
    if xhs_cookie.exists():
        import time as _t
        age_days = (_t.time() - xhs_cookie.stat().st_mtime) / 86400
        if age_days > 14:
            print(f"- ⚠️ {xhs_cookie} is {age_days:.0f} days old — consider `npx rednote-mcp init`")
        else:
            print(f"- ✅ {xhs_cookie} ({age_days:.1f} days old)")
    else:
        print(f"- ❌ {xhs_cookie} not present — run `npx rednote-mcp init`")

    # Playwright managed browser
    print("\n## Playwright (skill-managed Chromium)")
    try:
        import playwright  # noqa: F401
        print("- playwright (py): OK")
    except Exception:
        print("- playwright (py): ❌ not installed — `pip install playwright && python -m playwright install chromium`")
    pw_platforms = os.environ.get("RSS_PLAYWRIGHT_PLATFORMS", "").strip()
    print(f"- RSS_PLAYWRIGHT_PLATFORMS: {pw_platforms or '(unset — Playwright tier disabled)'}")
    pw_profile = Path(os.environ.get(
        "RSS_PLAYWRIGHT_PROFILE",
        str(Path.home() / ".lets-go-rss" / "browser-profile"),
    ))
    if pw_profile.exists():
        cookies_sqlite = pw_profile / "Default" / "Network" / "Cookies"
        size = cookies_sqlite.stat().st_size if cookies_sqlite.exists() else 0
        print(f"- profile: {pw_profile} (cookies db {size} B)")
    else:
        print(f"- profile: {pw_profile} (not yet initialised)")
    print("  login commands: `python scripts/lets_go_rss.py --login {twitter,xiaohongshu}`")

    # Auto-fix pass
    if auto_fix:
        print("\n## Auto-fix")
        mgr = str(SCRIPTS_DIR / "rsshub_manager.py")
        # Restart managed RSSHub if unhealthy
        try:
            r = _sp.run([sys.executable, mgr, "status"],
                        capture_output=True, text=True, timeout=5)
            info = json.loads(r.stdout or "{}")
            if not info.get("healthy"):
                print("- managed rsshub unhealthy → restart")
                _sp.run([sys.executable, mgr, "restart"], timeout=120)
            else:
                print("- managed rsshub healthy ✅ (no action)")
        except Exception as e:
            print(f"- rsshub check failed: {e}")

        # Clear stale update.lock if no pid is alive
        lock = ASSETS_DIR / ".update.lock"
        if lock.exists():
            try:
                txt = lock.read_text()
                # Best-effort: remove if it's clearly stale (> 2h old)
                age = _t.time() - lock.stat().st_mtime
                if age > 7200:
                    lock.unlink()
                    print(f"- cleared stale update.lock ({age/60:.0f} min old)")
            except Exception:
                pass

    return 0


def main():
    """Main entry point — delegates to rss_engine with correct paths."""
    # Fast path: --status should not require runtime scraping dependencies (e.g. httpx).
    if "--status" in sys.argv:
        sys.exit(print_cached_status())

    # Fast path: --overview reads full_overview.md
    if "--overview" in sys.argv:
        overview_path = ASSETS_DIR / "full_overview.md"
        if overview_path.exists():
            print(overview_path.read_text(encoding="utf-8"))
        else:
            print("⚠️ 尚无全量概览。请先运行 --update --digest 生成。")
        sys.exit(0)

    # Fast path: --doctor / --auto-fix — diagnostics, no scraping.
    if "--doctor" in sys.argv or "--auto-fix" in sys.argv:
        sys.exit(run_doctor(auto_fix=("--auto-fix" in sys.argv)))

    # Fast path: --login <platform> — opens a visible Chromium for one-time
    # sign-in, persists cookies in our profile.
    if "--login" in sys.argv:
        idx = sys.argv.index("--login")
        platform = sys.argv[idx + 1] if idx + 1 < len(sys.argv) else ""
        if not platform or platform.startswith("-"):
            print("Usage: python scripts/lets_go_rss.py --login <twitter|xiaohongshu|bilibili>")
            sys.exit(2)
        sys.path.insert(0, str(SCRIPTS_DIR))
        from playwright_adapter import login_platform
        sys.exit(login_platform(platform))

    # Skip setup for --skip-setup (cron jobs)
    skip_setup = '--skip-setup' in sys.argv
    if not skip_setup:
        if not ensure_dependencies():
            print("⚠️  Dependency setup had warnings, continuing anyway...")

    # Remove --skip-setup from argv so argparse doesn't complain
    sys.argv = [a for a in sys.argv if a != '--skip-setup']

    # Pass absolute db_path to engine instead of os.chdir()
    db_path = str(ASSETS_DIR / "rss_database.db")
    os.environ.setdefault("RSS_ASSETS_DIR", str(ASSETS_DIR))

    from rss_engine import main as rss_main
    rss_main(db_path=db_path)


if __name__ == "__main__":
    main()
