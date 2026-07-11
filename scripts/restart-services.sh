#!/usr/bin/env bash
# Restart split-pdf and split-extract via systemd.
set -euo pipefail

sudo systemctl restart split-pdf split-extract
echo "Restarted split-pdf and split-extract."
