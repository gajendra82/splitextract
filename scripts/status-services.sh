#!/usr/bin/env bash
# Status for split-pdf and split-extract via systemd.
set -euo pipefail

echo "=== split-pdf ==="
systemctl status split-pdf --no-pager || true
echo
echo "=== split-extract ==="
systemctl status split-extract --no-pager || true
echo
echo "=== enabled at boot ==="
systemctl is-enabled split-pdf split-extract 2>&1 || true
echo
echo "=== listening ports ==="
ss -tulpn | grep -E ':8000|:8001' || echo "(no listeners on 8000/8001)"
