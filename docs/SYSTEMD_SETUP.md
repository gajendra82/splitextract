# systemd Setup — OCR Platform

Both FastAPI services run under systemd with shared `/var/www/html/.env`.

---

## Unit files

| Service | Unit file | Port | WorkingDirectory |
|---------|-----------|------|------------------|
| split-pdf | `/etc/systemd/system/split-pdf.service` | 8000 | `/var/www/html/split-pdf` |
| split-extract | `/etc/systemd/system/split-extract.service` | 8001 | `/var/www/html/split-extract` |

Source templates: `split-extract/deploy/systemd/`

---

## Install

```bash
sudo cp /var/www/html/split-extract/deploy/systemd/split-pdf.service /etc/systemd/system/
sudo cp /var/www/html/split-extract/deploy/systemd/split-extract.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable split-pdf split-extract
sudo systemctl restart split-pdf split-extract
```

---

## Configuration

### split-pdf.service

```ini
WorkingDirectory=/var/www/html/split-pdf
EnvironmentFile=/var/www/html/.env
ExecStart=/var/www/html/venv/bin/python3 -m uvicorn main:app \
  --host 0.0.0.0 --port 8000 --workers 1 --timeout-keep-alive 1200
User=root
Restart=always
RestartSec=5
KillMode=mixed
TimeoutStopSec=30
LimitNOFILE=65535
StandardOutput=journal
StandardError=journal
```

### split-extract.service

```ini
WorkingDirectory=/var/www/html/split-extract
EnvironmentFile=/var/www/html/.env
ExecStart=/var/www/html/venv/bin/python3 -m uvicorn app:app \
  --host 0.0.0.0 --port 8001 --workers 1 --timeout-keep-alive 1200
User=root
Restart=always
RestartSec=5
KillMode=mixed
TimeoutStopSec=30
LimitNOFILE=65535
StandardOutput=journal
StandardError=journal
```

---

## Boot validation

```bash
systemctl is-enabled split-pdf split-extract   # both: enabled
systemctl is-active split-pdf split-extract    # both: active
ss -tulpn | grep -E ':8000|:8001'              # both listening
bash /var/www/html/split-extract/scripts/check-health.sh
```

After reboot, both services start automatically via `WantedBy=multi-user.target`.

---

## Auto recovery

| Event | Recovery |
|-------|----------|
| Uncaught exception | `Restart=always` within ~5s |
| Watchdog `os._exit(1)` | systemd restart |
| `kill -9 <pid>` | systemd restart |
| OOM kill | systemd restart |
| Server reboot | `enable` → auto-start at boot |

No manual restart required for normal failure modes.

---

## Logging

```bash
# Both services
sudo journalctl -u split-pdf -u split-extract -f

# Individual
sudo journalctl -u split-pdf -n 100
sudo journalctl -u split-extract -n 100
```

See [OPERATIONS_RUNBOOK.md](./OPERATIONS_RUNBOOK.md) for filtering and retention.

---

## tmux (legacy)

`start-tmux.sh` remains for manual split-extract deployment. **Do not run tmux and systemd on the same port.**

Before enabling systemd:

```bash
tmux kill-session -t splitextract 2>/dev/null || true
# Free ports 8000/8001 if orphaned
```

---

## Management scripts

```bash
/var/www/html/split-extract/scripts/start-services.sh
/var/www/html/split-extract/scripts/stop-services.sh
/var/www/html/split-extract/scripts/restart-services.sh
/var/www/html/split-extract/scripts/status-services.sh
/var/www/html/split-extract/scripts/check-health.sh
```
