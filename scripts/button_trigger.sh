#!/usr/bin/env bash
set -euo pipefail

# DR-C125 本体スタートボタン押下時に scanbd から呼び出されるスクリプト。
# wait=true を付けることでスキャン完了まで scanbd のポーリングを一時停止し、USB 競合を防ぐ。
API_URL="${SCANNER_API_URL:-http://127.0.0.1:8000/api/trigger/button?wait=true}"

logger -t dr-c125-button "Hardware button pressed: triggering scan via ${API_URL}"

if response=$(curl -fsS -X POST "${API_URL}" 2>&1); then
  logger -t dr-c125-button "Scan completed: ${response}"
else
  logger -t dr-c125-button "Scan trigger failed: ${response}"
  exit 1
fi
