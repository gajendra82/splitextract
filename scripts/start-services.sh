#!/usr/bin/env bash
# Start split-pdf and split-extract via systemd.
set -euo pipefail

sudo systemctl start split-pdf split-extract
echo "Started split-pdf and split-extract."
