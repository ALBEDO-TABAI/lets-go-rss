#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
REPORT_PATH="${ROOT_DIR}/assets/latest_update.md"

# 微信目标用户
WEIXIN_TARGET="${WEIXIN_TARGET:-o9cq806iX5yhT2McomSz0k2JoLXk@im.wechat}"

cd "${ROOT_DIR}"
if [[ -f "${REPORT_PATH}" ]]; then
  content=$(cat "${REPORT_PATH}")
  # 通过 OpenClaw 发送到微信
  openclaw message send --channel openclaw-weixin --target "$WEIXIN_TARGET" --message "$content"
else
  echo "⚠️ 尚无缓存报告。请先运行更新任务生成。"
fi
