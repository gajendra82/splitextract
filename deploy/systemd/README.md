# systemd deployment — OCR Platform

Both **split-pdf** (8000) and **split-extract** (8001) run under systemd.

## Install

```bash
sudo cp deploy/systemd/split-pdf.service /etc/systemd/system/
sudo cp deploy/systemd/split-extract.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable split-pdf split-extract
sudo systemctl restart split-pdf split-extract
bash scripts/check-health.sh
```

Environment: **`/var/www/html/.env`** (shared)

See [docs/SYSTEMD_SETUP.md](../docs/SYSTEMD_SETUP.md) for full details.

tmux (`start-tmux.sh`) remains supported for legacy split-extract deployments — do not run both on the same port.
