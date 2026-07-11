# Environment Variables — OCR Platform

Production file: **`/var/www/html/.env`**  
Template (placeholders only): **`split-extract/.env.example`**

Never commit production credentials. systemd loads `/var/www/html/.env` for both services.

---

## Vertex AI / Gemini (split-extract)

| Variable | Default | Service | Description |
|----------|---------|---------|-------------|
| `GOOGLE_GENAI_USE_VERTEXAI` | `true` | split-extract | Use Vertex AI instead of AI Studio |
| `GOOGLE_CLOUD_PROJECT` | — | split-extract | GCP project ID |
| `GOOGLE_CLOUD_LOCATION` | `global` | split-extract | Vertex region |
| `GOOGLE_APPLICATION_CREDENTIALS` | — | split-extract | Path to service account JSON |
| `GEMINI_API_KEY` | — | split-extract | Optional AI Studio key (if not using Vertex) |

---

## OpenAI GPT (split-extract)

| Variable | Default | Service | Description |
|----------|---------|---------|-------------|
| `USE_GPT_FOR_GOOD_OCR` | `true` | split-extract | Use GPT for good-quality OCR text |
| `OPENAI_API_KEY` | — | split-extract | OpenAI API key |
| `GPT_TEXT_MODEL` | `gpt-5.4-mini` | split-extract | GPT model name |
| `GPT_TIMEOUT` | `45` | split-extract | GPT request timeout (seconds) |

---

## GPT cost optimization (split-extract)

| Variable | Default | Safe prod default | Description |
|----------|---------|-------------------|-------------|
| `ENABLE_GPT_CACHE` | `false` | **false** | Cache GPT responses |
| `ENABLE_OCR_NORMALIZATION` | `false` | **false** | Normalize OCR whitespace before GPT |
| `ENABLE_GPT_TOKEN_LOGGING` | `false` | **false** | Log token usage/cost |
| `GPT_CACHE_TTL_SECONDS` | `86400` | — | Cache TTL |
| `GPT_CACHE_DIR` | `.gpt_cache` | — | Cache directory |
| `GPT_INPUT_COST_PER_1M_USD` | `0.15` | — | Cost metric input |
| `GPT_OUTPUT_COST_PER_1M_USD` | `0.60` | — | Cost metric output |

---

## Azure Blob Storage (both)

| Variable | Default | Service | Description |
|----------|---------|---------|-------------|
| `AZURE_STORAGE_CONNECTION_STRING` | — | both | Full Azure connection string |
| `AZURE_STORAGE_ACCOUNT_NAME` | — | both | Account name (alt auth) |
| `AZURE_STORAGE_ACCOUNT_KEY` | — | both | Account key (alt auth) |
| `AZURE_CONTAINER_NAME` | `invoice-splits` | both | Blob container |

---

## split-pdf

| Variable | Default | Description |
|----------|---------|-------------|
| `OUTPUT_FOLDER` | `output_pdfs` | Local output directory |
| `ROOT_FOLDER` | `POD` | Azure blob root prefix |

---

## split-extract server

| Variable | Default | Description |
|----------|---------|-------------|
| `HOST` | `0.0.0.0` | Bind host (CLI override; systemd uses fixed ports) |
| `PORT` | `8001` | Port (systemd uses 8001 explicitly) |
| `MAX_CONCURRENT_REQUESTS` | `1` | Request semaphore limit |
| `REQUEST_QUEUE_TIMEOUT` | `3600` | Queue wait timeout (seconds) |
| `REQUEST_STUCK_THRESHOLD_SECONDS` | `1800` | Stuck detection for `/health` status |
| `MAX_PARALLEL_CALLS` | `3` | Max parallel Gemini calls |
| `MAX_TESSERACT_CONCURRENCY` | `6` | Tesseract slot limit |

---

## Reliability flags (split-extract)

All default **`false`** for production-safe deploy.

| Variable | Default | Changes behaviour? |
|----------|---------|-------------------|
| `OCR_WATCHDOG_ENABLED` | `false` | Yes — self-terminate when wedged |
| `OCR_WATCHDOG_INTERVAL_SECONDS` | `30` | Watchdog poll interval |
| `OCR_WATCHDOG_STUCK_SECONDS` | `300` | Stale heartbeat threshold (use **600** in stage 3) |
| `OCR_STRUCTURED_LOGGING_ENABLED` | `false` | No — log prefix only |
| `OCR_SEMAPHORE_LOGGING_ENABLED` | `false` | No |
| `OCR_THREADPOOL_LOGGING_ENABLED` | `false` | No |
| `OCR_TESSERACT_LOGGING_ENABLED` | `false` | No |
| `OCR_TESSERACT_CALL_TIMEOUT_ENABLED` | `false` | **Yes** — timeout changes page routing |
| `TESSERACT_CALL_TIMEOUT_SECONDS` | `180` | Used only if timeout enabled |
| `OCR_PAGE_FUTURE_TIMEOUT_SECONDS` | `120` | Thread pool future timeout |
| `OCR_MID_EXECUTION_HEARTBEAT_ENABLED` | `false` | No — liveness ticks during OCR |
| `OCR_HEARTBEAT_TICK_SECONDS` | `30` | Mid-OCR tick interval |

---

## Staged rollout (recommended)

| Stage | Enable |
|-------|--------|
| 1 — Deploy | All flags **off** |
| 2 — Observe | `OCR_STRUCTURED_LOGGING_ENABLED`, `OCR_MID_EXECUTION_HEARTBEAT_ENABLED`, `OCR_TESSERACT_LOGGING_ENABLED` |
| 3 — Watchdog | `OCR_WATCHDOG_ENABLED=true`, `OCR_WATCHDOG_STUCK_SECONDS=600` |
| 4 — Evaluate | Tune threshold; keep `OCR_TESSERACT_CALL_TIMEOUT_ENABLED=false` |

See [PRODUCTION_DEPLOYMENT.md](./PRODUCTION_DEPLOYMENT.md) for full procedure.
