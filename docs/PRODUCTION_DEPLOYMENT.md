# Production Deployment — OCR Platform

**Architecture:** Laravel → split-pdf (8000) → split-extract (8001) → Vertex AI / GPT / OCR → Laravel

**Recommended runtime:** systemd (both services)  
**Alternative:** tmux (`start-tmux.sh` for split-extract only — legacy)

---

## Pre-deployment checklist

- [ ] Unit tests pass: `python3 -m unittest tests.test_reliability tests.test_gpt_cost_reduction`
- [ ] No secrets in git (only `.env.example` placeholders)
- [ ] `/var/www/html/.env` configured (see [ENVIRONMENT_VARIABLES.md](./ENVIRONMENT_VARIABLES.md))
- [ ] All reliability flags **false** for initial deploy
- [ ] Ports 8000 and 8001 free

---

## Installation (systemd)

```bash
# 1. Environment (if not already present)
sudo cp /var/www/html/split-extract/.env.example /var/www/html/.env
# Edit /var/www/html/.env with production credentials

# 2. Install units
sudo cp /var/www/html/split-extract/deploy/systemd/split-pdf.service /etc/systemd/system/
sudo cp /var/www/html/split-extract/deploy/systemd/split-extract.service /etc/systemd/system/

# 3. Enable and start
sudo systemctl daemon-reload
sudo systemctl enable split-pdf split-extract
sudo systemctl restart split-pdf split-extract

# 4. Verify
bash /var/www/html/split-extract/scripts/check-health.sh
bash /var/www/html/split-extract/scripts/status-services.sh
```

---

## Environment

| File | Purpose |
|------|---------|
| `/var/www/html/.env` | **Production** — both services |
| `split-extract/.env.example` | Template (never commit real keys) |

Rules:

- Never overwrite existing production values
- Append only missing variables
- Never commit credentials

Full variable reference: [ENVIRONMENT_VARIABLES.md](./ENVIRONMENT_VARIABLES.md)

---

## Startup sequence

1. **Boot** → `multi-user.target`
2. **split-pdf** starts on port **8000**
3. **split-extract** starts on port **8001** (After split-pdf)
4. Both load **`EnvironmentFile=/var/www/html/.env`**

---

## Restart policy (auto recovery)

Both services:

| Setting | Value |
|---------|-------|
| `Restart` | `always` |
| `RestartSec` | `5` |
| `KillMode` | `mixed` |
| `TimeoutStopSec` | `30` |
| `WantedBy` | `multi-user.target` |

Recovers automatically after: crash, exception, watchdog `os._exit(1)`, `kill -9`, reboot, OOM kill.

---

## Health endpoints

| Service | Endpoint | Use |
|---------|----------|-----|
| split-pdf | `GET /health` | Liveness |
| split-extract | `GET /live` | Liveness |
| split-extract | `GET /ready` | Readiness |

```bash
bash /var/www/html/split-extract/scripts/check-health.sh
```

**Note:** With `MAX_CONCURRENT_REQUESTS=1`, `/live` may delay while a long OCR request blocks the worker. Use `/ready` for readiness during processing.

---

## Service management scripts

All scripts use **systemctl only**:

```bash
bash /var/www/html/split-extract/scripts/start-services.sh
bash /var/www/html/split-extract/scripts/stop-services.sh
bash /var/www/html/split-extract/scripts/restart-services.sh
bash /var/www/html/split-extract/scripts/status-services.sh
bash /var/www/html/split-extract/scripts/check-health.sh
```

---

## Staged feature rollout

### Stage 1 — Deploy (current)

Deploy with **all flags disabled**. Behaviour identical to pre-reliability production.

```bash
OCR_WATCHDOG_ENABLED=false
OCR_STRUCTURED_LOGGING_ENABLED=false
OCR_MID_EXECUTION_HEARTBEAT_ENABLED=false
OCR_TESSERACT_LOGGING_ENABLED=false
OCR_TESSERACT_CALL_TIMEOUT_ENABLED=false
ENABLE_GPT_CACHE=false
ENABLE_OCR_NORMALIZATION=false
```

Observe 3–7 days.

### Stage 2 — Observability

```bash
OCR_STRUCTURED_LOGGING_ENABLED=true
OCR_MID_EXECUTION_HEARTBEAT_ENABLED=true
OCR_TESSERACT_LOGGING_ENABLED=true
```

Restart: `sudo systemctl restart split-extract`

Observe production logs and `/ready` heartbeat ages.

### Stage 3 — Watchdog

```bash
OCR_WATCHDOG_ENABLED=true
OCR_WATCHDOG_STUCK_SECONDS=600
```

Observe **one week**. Monitor watchdog restarts via journal.

### Stage 4 — Evaluate

- Tune `OCR_WATCHDOG_STUCK_SECONDS` based on P99 heartbeat age
- Keep **`OCR_TESSERACT_CALL_TIMEOUT_ENABLED=false`** until production data supports it

---

## Upgrade

1. Wait for idle: `curl -s http://127.0.0.1:8001/ready | jq .active_request` → `false`
2. Deploy code
3. Run tests
4. `sudo systemctl restart split-pdf split-extract`
5. `bash scripts/check-health.sh`

---

## Rollback

See [ROLLBACK.md](./ROLLBACK.md).

---

## Related docs

- [SYSTEMD_SETUP.md](./SYSTEMD_SETUP.md)
- [OPERATIONS_RUNBOOK.md](./OPERATIONS_RUNBOOK.md)
- [MONITORING.md](./MONITORING.md)
- [ENVIRONMENT_VARIABLES.md](./ENVIRONMENT_VARIABLES.md)
- [FINAL_PRODUCTION_REPORT.md](./FINAL_PRODUCTION_REPORT.md)
