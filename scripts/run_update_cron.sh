#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd -- "${SCRIPT_DIR}/.." && pwd)"

# Ensure Homebrew tools (yt-dlp, python3, etc.) are available in cron env
export PATH="/opt/homebrew/bin:/usr/local/bin:${PATH}"

# Safer defaults for bot timeout budgets; can be overridden by env.
export RSS_HTTP_TIMEOUT="${RSS_HTTP_TIMEOUT:-10}"
export RSS_HTTP_RETRIES="${RSS_HTTP_RETRIES:-2}"
export RSS_XHS_TIMEOUT="${RSS_XHS_TIMEOUT:-6}"
export RSS_XHS_RETRIES="${RSS_XHS_RETRIES:-1}"
export RSS_YTDLP_TIMEOUT="${RSS_YTDLP_TIMEOUT:-30}"

PYTHON_BIN="${PYTHON_BIN:-python3}"

resolve_bin() {
  local cand="$1"
  if [[ "${cand}" == */* ]]; then
    [[ -x "${cand}" ]] && printf '%s\n' "${cand}" || true
  else
    command -v "${cand}" 2>/dev/null || true
  fi
}

pick_python_with_httpx() {
  local candidates=(
    "${PYTHON_BIN}"
    "python3"
    "/opt/homebrew/bin/python3"
    "/usr/local/bin/python3"
    "/usr/bin/python3"
  )
  local seen=""
  local cand=""
  local bin=""
  for cand in "${candidates[@]}"; do
    bin="$(resolve_bin "${cand}")"
    [[ -n "${bin}" ]] || continue
    if [[ " ${seen} " == *" ${bin} "* ]]; then
      continue
    fi
    seen="${seen} ${bin}"
    if "${bin}" -c "import httpx" >/dev/null 2>&1; then
      printf '%s\n' "${bin}"
      return 0
    fi
  done
  return 1
}

cd "${ROOT_DIR}"
if [[ -f "${ROOT_DIR}/.env" ]]; then
  set -a
  source "${ROOT_DIR}/.env"
  set +a
fi
echo "===== [$(date '+%Y-%m-%d %H:%M:%S %z')] run_update_cron start ====="
SELECTED_PYTHON="$(pick_python_with_httpx || true)"
if [[ -z "${SELECTED_PYTHON}" ]]; then
  BOOTSTRAP_PYTHON="$(resolve_bin "${PYTHON_BIN}")"
  [[ -n "${BOOTSTRAP_PYTHON}" ]] || BOOTSTRAP_PYTHON="$(resolve_bin "python3")"
  if [[ -n "${BOOTSTRAP_PYTHON}" ]]; then
    echo "⚠️  httpx missing; trying one-time setup via ${BOOTSTRAP_PYTHON}"
    "${BOOTSTRAP_PYTHON}" scripts/setup.py || true
    SELECTED_PYTHON="$(pick_python_with_httpx || true)"
  fi
fi

if [[ -z "${SELECTED_PYTHON}" ]]; then
  echo "❌ No usable Python interpreter with httpx found."
  echo "   Fix: install deps with one of these:"
  echo "   - /opt/homebrew/bin/python3 scripts/setup.py"
  echo "   - python3 scripts/setup.py"
  rc=1
elif "${SELECTED_PYTHON}" scripts/lets_go_rss.py --update --no-llm --digest --skip-setup; then
  rc=0
else
  rc=$?
fi
echo "===== [$(date '+%Y-%m-%d %H:%M:%S %z')] run_update_cron end rc=${rc} ====="
exit "${rc}"
