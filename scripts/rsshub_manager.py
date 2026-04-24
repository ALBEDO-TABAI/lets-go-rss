#!/usr/bin/env python3
"""
Skill-owned RSSHub lifecycle manager.

Keeps a private RSSHub instance running on port 1201 (configurable) so that
the skill does not depend on any externally-owned RSSHub (e.g. ready-cowork's
embedded worker on :1200, which is version-pinned by that project). The
manager installs / upgrades rsshub into SKILL/node_modules on demand so we
can stay current with upstream route fixes without any external coordination.

Commands:
  status            Probe health of the managed RSSHub.
  start             Ensure rsshub is installed, then spawn the worker.
  stop              Terminate the running worker (via pidfile).
  restart           stop + start.
  update            Pull latest rsshub, restart worker.
  start-if-needed   No-op if already healthy; otherwise start (used by cron).
  logs              Tail the worker log.

Environment:
  RSSHUB_MANAGED_PORT   Default 1201
  RSSHUB_NODE_BIN       Path to node (auto-detected via PATH if unset)
  RSSHUB_NPM_BIN        Path to npm
  RSSHUB_START_TIMEOUT  Seconds to wait for health after start (default 60)
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Optional, Tuple


SKILL_DIR = Path(__file__).resolve().parent.parent
ASSETS_DIR = SKILL_DIR / "assets"
WORKER_SCRIPT = SKILL_DIR / "scripts" / "rsshub_worker.mjs"
PID_PATH = ASSETS_DIR / ".rsshub.pid"
LOG_PATH = ASSETS_DIR / ".rsshub.log"


def _port() -> int:
    return int(os.environ.get("RSSHUB_MANAGED_PORT", "1201"))


def _resolve_bin(name: str, env_var: str) -> Optional[str]:
    explicit = os.environ.get(env_var)
    if explicit and Path(explicit).exists():
        return explicit
    for base in ("/opt/homebrew/bin", "/usr/local/bin", "/usr/bin"):
        candidate = Path(base) / name
        if candidate.exists():
            return str(candidate)
    # Fall back to PATH lookup
    try:
        out = subprocess.run(["which", name], capture_output=True, text=True, timeout=2)
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except Exception:
        pass
    return None


def _node_bin() -> str:
    path = _resolve_bin("node", "RSSHUB_NODE_BIN")
    if not path:
        raise RuntimeError("node not found. Install Node 18+ (e.g. `brew install node`).")
    return path


def _npm_bin() -> str:
    path = _resolve_bin("npm", "RSSHUB_NPM_BIN")
    if not path:
        raise RuntimeError("npm not found.")
    return path


def _read_pid() -> Optional[int]:
    try:
        return int(PID_PATH.read_text().strip())
    except (FileNotFoundError, ValueError):
        return None


def _pid_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def _health_probe(timeout: float = 1.5) -> Tuple[bool, dict]:
    url = f"http://127.0.0.1:{_port()}/healthz"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return True, json.loads(raw) if raw else {}
    except Exception as e:
        return False, {"error": str(e)}


def status() -> int:
    pid = _read_pid()
    pid_alive = bool(pid and _pid_running(pid))
    healthy, info = _health_probe()
    print(json.dumps({
        "port": _port(),
        "pid_recorded": pid,
        "pid_alive": pid_alive,
        "healthy": healthy,
        "info": info,
    }, ensure_ascii=False, indent=2))
    return 0 if healthy else 1


def _ensure_installed() -> None:
    """Ensure `<skill>/node_modules/rsshub/package.json` exists."""
    pkg_json = SKILL_DIR / "node_modules" / "rsshub" / "package.json"
    if pkg_json.exists():
        return
    print("[manager] rsshub not installed; running npm install rsshub@latest …", file=sys.stderr)
    npm = _npm_bin()
    subprocess.run(
        [npm, "install", "rsshub@latest", "--prefix", str(SKILL_DIR),
         "--no-audit", "--no-fund", "--loglevel=error"],
        check=True,
        timeout=300,
    )


def start() -> int:
    # If already healthy, nothing to do
    healthy, _ = _health_probe()
    if healthy:
        print(f"[manager] already healthy on :{_port()}")
        return 0

    # Stale pid cleanup
    pid = _read_pid()
    if pid and not _pid_running(pid):
        try:
            PID_PATH.unlink()
        except FileNotFoundError:
            pass

    _ensure_installed()

    ASSETS_DIR.mkdir(parents=True, exist_ok=True)
    log_f = open(LOG_PATH, "ab")
    env = os.environ.copy()
    env["RSSHUB_PORT"] = str(_port())
    env.setdefault("NODE_ENV", "production")

    node = _node_bin()
    proc = subprocess.Popen(
        [node, str(WORKER_SCRIPT)],
        stdout=log_f,
        stderr=log_f,
        stdin=subprocess.DEVNULL,
        cwd=str(SKILL_DIR),
        env=env,
        start_new_session=True,  # detach so we can exit without SIGHUP-ing it
    )
    PID_PATH.write_text(str(proc.pid))

    # Wait for health
    deadline = time.time() + float(os.environ.get("RSSHUB_START_TIMEOUT", "60"))
    while time.time() < deadline:
        if proc.poll() is not None:
            print(f"[manager] worker exited prematurely (rc={proc.returncode}); see {LOG_PATH}",
                  file=sys.stderr)
            return 2
        healthy, _ = _health_probe(timeout=1.0)
        if healthy:
            print(f"[manager] rsshub ready on :{_port()} (pid={proc.pid})")
            return 0
        time.sleep(1.0)

    print(f"[manager] timed out waiting for health on :{_port()}; see {LOG_PATH}", file=sys.stderr)
    return 3


def stop() -> int:
    pid = _read_pid()
    if not pid or not _pid_running(pid):
        print("[manager] not running")
        try:
            PID_PATH.unlink()
        except FileNotFoundError:
            pass
        return 0

    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass

    for _ in range(30):
        if not _pid_running(pid):
            break
        time.sleep(0.1)
    else:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    try:
        PID_PATH.unlink()
    except FileNotFoundError:
        pass
    print(f"[manager] stopped pid={pid}")
    return 0


def restart() -> int:
    stop()
    return start()


def update() -> int:
    """Upgrade rsshub to latest and restart the worker."""
    npm = _npm_bin()
    print("[manager] upgrading rsshub to latest …", file=sys.stderr)
    subprocess.run(
        [npm, "install", "rsshub@latest", "--prefix", str(SKILL_DIR),
         "--no-audit", "--no-fund", "--loglevel=error"],
        check=True,
        timeout=600,
    )
    return restart()


def start_if_needed() -> int:
    """For cron preflight — idempotent; never blocks cron on failure."""
    healthy, _ = _health_probe(timeout=1.0)
    if healthy:
        return 0
    # Best-effort start; cap overall time so cron does not wait > 10s.
    os.environ.setdefault("RSSHUB_START_TIMEOUT", "10")
    try:
        return start()
    except Exception as e:
        print(f"[manager] start-if-needed failed: {e}", file=sys.stderr)
        return 1


def logs() -> int:
    if not LOG_PATH.exists():
        print("(no log file yet)")
        return 0
    subprocess.run(["tail", "-n", "200", str(LOG_PATH)])
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Manage skill-owned RSSHub instance")
    parser.add_argument(
        "command",
        choices=["status", "start", "stop", "restart", "update", "start-if-needed", "logs"],
    )
    args = parser.parse_args()
    return {
        "status": status,
        "start": start,
        "stop": stop,
        "restart": restart,
        "update": update,
        "start-if-needed": start_if_needed,
        "logs": logs,
    }[args.command]()


if __name__ == "__main__":
    sys.exit(main())
