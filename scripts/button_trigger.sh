#!/usr/bin/env bash
set -euo pipefail

# DR-C125 本体スタートボタン押下時に scanbd から呼び出されるスクリプト。
# wait=true を付けることでスキャン完了まで scanbd のポーリングを一時停止し、USB 競合を防ぐ。
API_URL="${SCANNER_API_URL:-http://127.0.0.1:8000/api/trigger/button?wait=true}"

logger -t dr-c125-button "Hardware button pressed: triggering scan via ${API_URL}"

tmp_body="$(mktemp)"
trap 'rm -f "${tmp_body}"' EXIT

http_code="$(curl -sS -o "${tmp_body}" -w "%{http_code}" -X POST "${API_URL}" 2>&1 || true)"
body="$(cat "${tmp_body}" 2>/dev/null || true)"

if [[ "${http_code}" =~ ^2[0-9][0-9]$ ]]; then
  logger -t dr-c125-button "Scan completed (HTTP ${http_code}): ${body}"
else
  logger -t dr-c125-button "Scan trigger failed (HTTP ${http_code}): ${body}"
  exit 1
fi
