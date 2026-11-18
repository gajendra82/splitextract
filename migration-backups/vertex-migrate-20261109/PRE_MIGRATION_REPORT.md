# Pre-Migration Report — Vertex AI / Gemini Upgrade
Date: 2026-11-09

## Service
- Unit: /etc/systemd/system/split-extract.service
- User/Group: root/root
- WorkingDirectory: /var/www/html/split-extract
- EnvironmentFile: /var/www/html/.env
- ExecStart: /var/www/html/venv/bin/python3 /var/www/html/split-extract/runner.py
- Port: 8001
- MainPID (pre-change): 367620

## Runtime (before change)
- GOOGLE_CLOUD_PROJECT=pod-ocr-502015
- GOOGLE_APPLICATION_CREDENTIALS=/etc/google/split-extract.json
- GOOGLE_CLOUD_LOCATION=global
- GOOGLE_GENAI_USE_VERTEXAI=true
- VISION_PROVIDER / VISION_MODEL: not present in live .env (code defaults used)

## Model reference map (production code)
1. app.py GEMINI_MODELS[0].name = gemini-2.5-flash-lite (HARDCODED; primary invoice OCR path via get_current_model_config)
2. app.py Riddhishree IRN vision fallback tuple: gemini-2.5-flash, gemini-2.5-flash-lite (HARDCODED)
3. services/config.py vision_model default = gemini-2.5-flash-lite; project default = pod-ocr-502015
4. services/sales_statement_extractor.py VISION_MODEL env default = gemini-2.5-flash-lite (two call sites)
5. services/gemini_vision_extractor.py uses config.vision_model or model_config["name"] (no hardcode)
6. services/vertex_gemini_client.py project default = pod-ocr-502015

## Not modified (out of production flow / docs / tests)
- .env.example
- vapt-assessment/** historical reports
- python_vapt/** historical reports
- docs/**

## Quota / concurrency (preserve)
- MAX_PARALLEL_CALLS=3
- USE_LOCAL_GEMINI_RATE_LIMITER=false
- GEMINI_MAX_REQUESTS_PER_MINUTE=120
- GEMINI_MAX_REQUESTS_PER_DAY=10000
