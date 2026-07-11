#!/usr/bin/env bash
# Stop split-pdf and split-extract via systemd.
set -euo pipefail

sudo systemctl stop split-extract split-pdf
echo "Stopped split-extract and split-pdf."
