# Monitoring Guide — OCR Platform

Covers **split-pdf** (8000) and **split-extract** (8001).

---

## Probe endpoints

| Service | URL | Interval | Timeout |
|---------|-----|----------|---------|
| split-pdf liveness | `GET :8000/health` | 30s | 2s |
| split-extract liveness | `GET :8001/live` | 30s | 5s |
| split-extract readiness | `GET :8001/ready` | 30s | 5s |

Script: `bash /var/www/html/split-extract/scripts/check-health.sh`

---

## Recommended alerts

### Critical

| Alert | Source | Condition |
|-------|--------|-----------|
| split-pdf down | `:8000/health` | Non-200 for 2 checks |
| split-extract down | `:8001/live` | Non-200 for 2 checks (when idle) |
| Readiness failed | `:8001/ready` | `ready == false` for 2 checks |
| Restart storm | systemd | `NRestarts` > 5 in 5 min (either service) |
| Disk full | host | > 90% on `/` or `/var` |

### High

| Alert | Source | Condition |
|-------|--------|-----------|
| Heartbeat age (wedged) | `/ready` | `active_request && heartbeat_age > OCR_WATCHDOG_STUCK_SECONDS && workers == 0` |
| Watchdog restart | journal | `WATCHDOG REPORT` event |
| Memory usage | `/health` or host | `memory_usage_mb > 6000` or OOM events |
| CPU sustained high | host | > 90% for 15 min |
| HTTP 5xx (Laravel/proxy) | access logs | > 1% over 5m |
| Request duration | structured logs | `elapsed > 1800s` |

### Medium

| Alert | Source | Condition |
|-------|--------|-----------|
| Heartbeat age (active work) | `/ready` | `heartbeat_age > 300` with active workers |
| OCR active workers | `/ready` | `active_ocr > MAX_TESSERACT_CONCURRENCY` (misconfig) |
| GPT active workers | `/ready` | `active_gpt > 0` for > 10m |
| Gemini active workers | `/ready` | `active_gemini > 0` for > 10m |
| Thread count | `/ready` | `thread_count` spike vs baseline |
| OCR duration | `REQUEST COMPLETE` log | `ocr_duration_seconds` > P99 |
| GPT duration | `REQUEST COMPLETE` log | `gpt_duration_seconds` > 120s |
| Tesseract failures | logs | `TESSERACT FAILED` rate > baseline |
| Queue depth | `/ready` | `waiting_requests > 0` for > 10m |

### Low / informational

| Alert | Source | Condition |
|-------|--------|-----------|
| Watchdog skip | logs | `skipping exit` — workers protected |
| split-pdf latency | `:8000/health` | p99 > 1s |

---

## Metrics reference

### split-extract `/ready`

```json
{
  "ready": true,
  "active_request": false,
  "heartbeat_age": null,
  "active_ocr": 0,
  "active_gpt": 0,
  "active_gemini": 0,
  "thread_count": 8,
  "uptime_seconds": 3600,
  "waiting_requests": 0
}
```

### split-extract `/health` (deeper, use sparingly)

Adds: `memory_usage_mb`, `cpu_percent`, `tesseract_calls_total`, `processing_status`, `current_model`.

### systemd

```bash
systemctl show split-pdf -p NRestarts,MemoryCurrent,CPUUsageNSec
systemctl show split-extract -p NRestarts,MemoryCurrent,CPUUsageNSec
```

### Watchdog restart count

```bash
journalctl -u split-extract --since "24 hours ago" | grep -c 'WATCHDOG REPORT'
```

---

## Dashboard panels

1. Service up — both health endpoints
2. Ready boolean — split-extract
3. Active workers — OCR / GPT / Gemini
4. Heartbeat age — when `active_request`
5. Memory / CPU — host + `/health`
6. Restart count — systemd NRestarts
7. Request duration — from structured logs
8. Disk usage — host
9. Tesseract error rate — log grep

---

## Threshold tuning

| Setting | Initial | Production (stage 3) |
|---------|---------|----------------------|
| `OCR_WATCHDOG_STUCK_SECONDS` | 300 | **600** |
| Heartbeat alert (idle workers) | — | > stuck threshold |
| Heartbeat alert (active workers) | — | > 300s informational |

---

## Related docs

- [OPERATIONS_RUNBOOK.md](./OPERATIONS_RUNBOOK.md)
- [PRODUCTION_DEPLOYMENT.md](./PRODUCTION_DEPLOYMENT.md)
- [ENVIRONMENT_VARIABLES.md](./ENVIRONMENT_VARIABLES.md)
