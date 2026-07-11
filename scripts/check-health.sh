#!/usr/bin/env bash
# Health checks for split-pdf and split-extract.
set -euo pipefail

PDF_URL="${SPLIT_PDF_URL:-http://127.0.0.1:8000}"
EXTRACT_URL="${SPLIT_EXTRACT_URL:-http://127.0.0.1:8001}"
TIMEOUT="${HEALTH_TIMEOUT:-5}"

fail=0

check() {
  local name="$1" url="$2"
  local code
  code=$(curl -s -o /dev/null -w "%{http_code}" --max-time "$TIMEOUT" "$url" || echo "000")
  if [[ "$code" == "200" ]]; then
    echo "OK  $name  $url  HTTP $code"
  else
    echo "FAIL  $name  $url  HTTP $code"
    fail=1
  fi
}

check "split-pdf /health" "${PDF_URL}/health"
check "split-extract /live" "${EXTRACT_URL}/live"
check "split-extract /ready" "${EXTRACT_URL}/ready"

if [[ "$fail" -ne 0 ]]; then
  exit 1
fi

echo "All health checks passed."
