# Operations Runbook — OCR Platform

Laravel → **split-pdf:8000** → **split-extract:8001** → Vertex AI / GPT / OCR

---

## Quick health check

```bash
bash /var/www/html/split-extract/scripts/check-health.sh
bash /var/www/html/split-extract/scripts/status-services.sh
```

| Service | Liveness | Readiness |
|---------|----------|-----------|
| split-pdf | `GET :8000/health` | Same |
| split-extract | `GET :8001/live` | `GET :8001/ready` |

---

## systemd commands

```bash
sudo systemctl status split-pdf split-extract
sudo systemctl restart split-pdf split-extract
sudo journalctl -u split-pdf -u split-extract -f
systemctl show split-pdf split-extract -p NRestarts,ActiveState,SubState
```

---

## split-pdf operations

### Health

```bash
curl -s http://127.0.0.1:8000/health | jq .
# Expected: {"status":"healthy","service":"split-pdf"}
```

### Logs

```bash
sudo journalctl -u split-pdf --since "30 min ago"
```

### Restart safely

```bash
sudo systemctl restart split-pdf
```

split-pdf is stateless for in-flight HTTP; 30s stop timeout.

---

## split-extract operations

### Heartbeat / readiness

```bash
curl -s http://127.0.0.1:8001/ready | jq '{
  ready, active_request, heartbeat_age,
  active_ocr, active_gpt, active_gemini,
  current_stage, current_invoice, current_page
}'
```

### Watchdog (when enabled)

```bash
grep OCR_WATCHDOG /var/www/html/.env
sudo journalctl -u split-extract | grep WATCHDOG
```

Watchdog terminates only when heartbeat is stale **and** all worker counters are zero.

### OCR / GPT / Gemini workers

```bash
curl -s http://127.0.0.1:8001/ready | jq '{active_ocr, active_gpt, active_gemini}'
curl -s http://127.0.0.1:8001/health | jq '{active_ocr_threads, tesseract_slot_active}'
```

### Safe restart

1. Wait for idle: `curl -s .../ready | jq .active_request` → `false`
2. `sudo systemctl restart split-extract`

---

## Logging

### journalctl usage

```bash
# Live tail — both services
sudo journalctl -u split-pdf -u split-extract -f

# Time window
sudo journalctl -u split-extract --since "2026-07-11 09:00" --until "2026-07-11 10:00"

# Priority
sudo journalctl -u split-extract -p err --since today
```

### Log rotation and retention

journald (default):

```bash
# /etc/systemd/journald.conf
Storage=persistent
SystemMaxUse=2G
MaxRetentionSec=30day
```

Apply: `sudo systemctl restart systemd-journald`

### Filtering (split-extract)

Requires `OCR_STRUCTURED_LOGGING_ENABLED=true` for request_id/invoice prefixes.

```bash
# By request_id
sudo journalctl -u split-extract --since today | grep 'request_id=abc-123'

# By invoice filename
sudo journalctl -u split-extract --since today | grep 'invoice=my-invoice.pdf'

# Watchdog events
sudo journalctl -u split-extract | grep WATCHDOG

# OCR / Tesseract
sudo journalctl -u split-extract | grep -E 'TESSERACT|ocr_'

# GPT
sudo journalctl -u split-extract | grep -E 'openai|GPT|gpt_'

# Gemini
sudo journalctl -u split-extract | grep -i gemini
```

Logging failures never interrupt processing (filter wrapped in try/except).

---

## Recovery scenarios

| Scenario | Action |
|----------|--------|
| split-pdf down | Auto-restart; check `journalctl -u split-pdf` |
| split-extract down | Auto-restart; check journal |
| Watchdog loop | Disable `OCR_WATCHDOG_ENABLED`; increase stuck threshold |
| Port conflict | Stop tmux/orphans; `systemctl restart` both |
| Stuck queue | Restart split-extract when idle |
| Azure upload failures | Check Azure vars in `/var/www/html/.env` |

---

## Failover validation (post-deploy)

Verified automatically:

```bash
# kill -9 recovery
sudo kill -9 $(ss -ltnp | grep ':8000 ' | grep -oP 'pid=\K[0-9]+' | head -1)
sleep 6 && systemctl is-active split-pdf

sudo kill -9 $(ss -ltnp | grep ':8001 ' | grep -oP 'pid=\K[0-9]+' | head -1)
sleep 8 && bash scripts/check-health.sh
```

---

## Common troubleshooting

| Symptom | Check |
|---------|-------|
| 8000 down | `systemctl status split-pdf` |
| 8001 down | `systemctl status split-extract` |
| `/live` slow | Active OCR blocking single worker — check `/ready` |
| `ready: false` | Stale heartbeat + zero workers — watchdog may restart |
| Laravel 502 | Both health endpoints; Laravel → split-pdf connectivity |
| dotenv parse warning | Fix line 1 of `/var/www/html/.env` (malformed entry) |

---

## Related docs

- [MONITORING.md](./MONITORING.md)
- [SYSTEMD_SETUP.md](./SYSTEMD_SETUP.md)
- [ROLLBACK.md](./ROLLBACK.md)
- [ENVIRONMENT_VARIABLES.md](./ENVIRONMENT_VARIABLES.md)
