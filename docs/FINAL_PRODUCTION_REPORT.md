# Final Production Report — OCR Platform Deployment

**Date:** 2026-07-11  
**Scope:** Final production deployment — Laravel → split-pdf → split-extract pipeline

---

## Executive summary

Production deployment completed. Both FastAPI services run under **systemd** with shared `/var/www/html/.env`, automatic restart, boot enablement, health probes, management scripts, and operational documentation.

**Pre-deployment validation:** PASSED  
**Failover validation:** PASSED (`kill -9` recovery on both services)  
**Feature flags:** All production-safe defaults (disabled)

---

## Deployment completed successfully. The OCR platform is production-ready for staged rollout with automatic recovery.

---

## 1. Pre-deployment validation

| Check | Result |
|-------|--------|
| split-extract unit tests (18) | **PASS** |
| split-extract app import | **PASS** |
| No secrets in split-extract repo | **PASS** (credentials only in `/var/www/html/.env`) |
| No merge conflicts (split-extract git) | **PASS** (clean branch, local changes only) |
| Feature flags default safe | **PASS** (all reliability + cost flags `false`) |
| split-pdf `/health` | **ADDED** (was missing; lightweight endpoint) |

---

## 2. Files changed / created

### Application

| File | Change |
|------|--------|
| `/var/www/html/split-pdf/main.py` | Added `GET /health` (operational only) |
| `/var/www/html/.env` | Appended `OUTPUT_FOLDER`, `ROOT_FOLDER` (missing vars only) |
| `split-extract/.env.example` | Recreated with all platform variables (placeholders) |
| `.gitignore`, `.dockerignore` | Restored `!.env.example` exception |

### systemd

| File | Purpose |
|------|---------|
| `deploy/systemd/split-pdf.service` | Port 8000 unit |
| `deploy/systemd/split-extract.service` | Port 8001 unit (updated to spec) |
| `/etc/systemd/system/split-pdf.service` | **Installed** |
| `/etc/systemd/system/split-extract.service` | **Installed** |

### Scripts

| Script | Purpose |
|--------|---------|
| `scripts/start-services.sh` | Start both via systemctl |
| `scripts/stop-services.sh` | Stop both |
| `scripts/restart-services.sh` | Restart both |
| `scripts/status-services.sh` | Status + ports |
| `scripts/check-health.sh` | Health probe all endpoints |

### Documentation

| Document | Purpose |
|----------|---------|
| `docs/PRODUCTION_DEPLOYMENT.md` | Platform install, rollout stages |
| `docs/OPERATIONS_RUNBOOK.md` | Day-to-day ops + logging |
| `docs/MONITORING.md` | Alerts and metrics |
| `docs/SYSTEMD_SETUP.md` | systemd install and boot validation |
| `docs/ROLLBACK.md` | Rollback procedures |
| `docs/ENVIRONMENT_VARIABLES.md` | Complete env var reference |
| `docs/FINAL_PRODUCTION_REPORT.md` | This report |

---

## 3. Services created

| Service | Port | Status | Boot |
|---------|------|--------|------|
| `split-pdf.service` | 8000 | active | enabled |
| `split-extract.service` | 8001 | active | enabled |

```bash
systemctl is-enabled split-pdf split-extract   # enabled / enabled
systemctl is-active split-pdf split-extract    # active / active
ss -tulpn | grep -E ':8000|:8001'              # both listening
```

---

## 4. Environment variables added

Appended to `/var/www/html/.env` (existing values preserved):

- `OUTPUT_FOLDER=output_pdfs`
- `ROOT_FOLDER=POD`

Previously present (verified production-safe defaults):

- All reliability flags → `false`
- `ENABLE_GPT_CACHE=false`, `ENABLE_OCR_NORMALIZATION=false`

Full reference: [ENVIRONMENT_VARIABLES.md](./ENVIRONMENT_VARIABLES.md)

---

## 5. Startup sequence

1. OS boot → `multi-user.target`
2. `split-pdf.service` → uvicorn `main:app` on **8000**
3. `split-extract.service` → uvicorn `app:app` on **8001**
4. Both load `EnvironmentFile=/var/www/html/.env`

---

## 6. Restart policy

| Setting | Value |
|---------|-------|
| Restart | `always` |
| RestartSec | `5` |
| KillMode | `mixed` |
| TimeoutStopSec | `30` |
| LimitNOFILE | `65535` |

**Validated:** `kill -9` on both PIDs → services returned to `active` within ~8s; health checks passed.

---

## 7. Health endpoints

| Service | Endpoint | HTTP | Blocking |
|---------|----------|------|----------|
| split-pdf | `/health` | 200 | No |
| split-extract | `/live` | 200 | No when idle* |
| split-extract | `/ready` | 200 | Lightweight (~1ms) |

\* With single uvicorn worker, `/live` may delay during active synchronous OCR (pre-existing architecture). Use `/ready` during processing.

---

## 8. Rollback procedure

See [ROLLBACK.md](./ROLLBACK.md):

1. Disable flags or restore code
2. `systemctl stop` both services
3. Optional: return to tmux via `start-tmux.sh`
4. Verify health + Laravel pipeline

---

## 9. Monitoring recommendations

- Probe `:8000/health`, `:8001/live`, `:8001/ready` every 30s
- Alert on `ready: false`, restart storms, disk > 90%
- Stage 2+: alert on heartbeat age, watchdog restarts, memory
- See [MONITORING.md](./MONITORING.md)

---

## 10. Staged rollout plan

| Stage | Action |
|-------|--------|
| **1** | Deploy — all flags off ( **current** ) |
| **2** | Enable structured logging + mid-heartbeat + tesseract logging |
| **3** | Enable watchdog, `OCR_WATCHDOG_STUCK_SECONDS=600`, observe 1 week |
| **4** | Evaluate threshold; keep timeout flag **off** |

---

## 11. Remaining known limitations

| Limitation | Impact |
|------------|--------|
| Single worker blocks event loop during long OCR | `/live` may delay under load |
| Malformed line 1 in `/var/www/html/.env` | dotenv warning; does not block startup |
| `OCR_TESSERACT_CALL_TIMEOUT_ENABLED` | Changes extraction — keep disabled |
| Tesseract timeout orphan subprocess | Only if timeout flag enabled |

---

## 12. Final validation checklist

| Check | Status |
|-------|--------|
| split-pdf starts automatically | ✓ enabled + active |
| split-extract starts automatically | ✓ enabled + active |
| Both restart after crashes | ✓ kill -9 validated |
| Both restart after reboot | ✓ `WantedBy=multi-user.target` |
| Both use `/var/www/html/.env` | ✓ EnvironmentFile |
| Health endpoints work | ✓ |
| Reliability features disabled | ✓ |
| No OCR behaviour changes | ✓ |
| No parsing changes | ✓ |
| No API format changes | ✓ (split-pdf `/health` added for ops) |
| No configuration regressions | ✓ append-only |

---

## 13. Operational commands

```bash
bash /var/www/html/split-extract/scripts/status-services.sh
bash /var/www/html/split-extract/scripts/check-health.sh
sudo journalctl -u split-pdf -u split-extract -f
```

---

**Deployment completed successfully. The OCR platform is production-ready for staged rollout with automatic recovery.**
