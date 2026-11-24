# Isolated PaddleOCR-VL GPU worker

Runs the already selected PaddleOCR-VL 1.6 pipeline in a Linux GPU container. It does not modify the laptop CUDA installation or the main application image. The application uses the cloud worker only when both `PADDLE_VL_API_URL` and `PADDLE_VL_API_KEY` are configured.

## Runtime and dependency pins

`Dockerfile` uses Ubuntu 22.04, CUDA 12.6.3 with cuDNN runtime, PaddlePaddle GPU 3.2.1 from the CUDA 12.6 Paddle wheel index, PaddleOCR 3.7.0, PaddleX 3.7.2, PyTorch 2.8.0 cu126, Pillow, NumPy, PyMuPDF, psutil, FastAPI, Uvicorn, and python-multipart. The Paddle/PaddleX/PyTorch versions match the repository's isolated `requirements-paddleocr-vl.txt` setup and CUDA 12.6 notes. The API pins its own web dependencies. The image starts `PaddleOCRVL(pipeline_version="v1.6")`; it does not install another OCR engine.

The image has a Docker health check against `/health`. This confirms the process responds and the authentication secret is configured; it does not load the model or prove OCR works. The authenticated image test below is the inference readiness check. First inference downloads/loads the model. Persist both `/root/.paddlex` and `/root/.cache` to avoid downloading model files after each replacement.

## Deploy on the Linux GPU host

Prerequisites: x86_64 Linux, Docker, NVIDIA Container Toolkit, a GPU with sufficient VRAM for the 0.9B VL model plus layout pipeline (24 GB recommended), and outbound access to the package/model sources during build and initial model load. Run these commands from the repository root. Store the API secret in the host's secret manager; do not commit it or put it in shell history. The worker's port must remain private behind a TLS reverse proxy or private application network.

Verify the host GPU/container runtime, build, and launch:

```sh
docker run --rm --gpus all nvidia/cuda:12.6.3-cudnn-runtime-ubuntu22.04 nvidia-smi
docker build --pull -f cloud_ocr/Dockerfile -t splitextract-paddle-vl:1.6 .
# Load this variable securely from the host's secret manager before continuing.
test -n "$PADDLE_VL_WORKER_SECRET"
docker volume create splitextract-paddlex-cache
docker volume create splitextract-model-cache
docker run -d --name split-extract-paddle-vl --gpus all \
  --restart unless-stopped \
  -p 127.0.0.1:8080:8080 \
  -v splitextract-paddlex-cache:/root/.paddlex \
  -v splitextract-model-cache:/root/.cache \
  -e PADDLE_VL_API_KEY="$PADDLE_VL_WORKER_SECRET" \
  -e PADDLE_VL_DEVICE=gpu:0 \
  -e PADDLE_VL_API_MAX_BYTES=26214400 \
  splitextract-paddle-vl:1.6
```

Wait for Docker's liveness/configuration health check:

```sh
for attempt in $(seq 1 36); do
  health="$(docker inspect --format '{{.State.Health.Status}}' split-extract-paddle-vl)"
  [ "$health" = healthy ] && break
  if [ "$health" = unhealthy ]; then docker logs split-extract-paddle-vl; exit 1; fi
  sleep 5
done
[ "$(docker inspect --format '{{.State.Health.Status}}' split-extract-paddle-vl)" = healthy ] || { docker logs split-extract-paddle-vl; exit 1; }
```

For a private worker, put a TLS reverse proxy in front of `127.0.0.1:8080`, restrict source addresses to the app network, and set the app's `PADDLE_VL_API_URL` to that HTTPS URL. A direct HTTP URL is suitable only for loopback/private network smoke testing; bearer authentication does not encrypt traffic.

## Required application environment

Set on the main extraction app after the cloud endpoint is deployed and image-tested:

```dotenv
PADDLE_VL_API_URL=https://<private-worker-host>
PADDLE_VL_API_KEY=<same-secret-from-secret-manager>
OCR_ENGINE=paddleocr-vl
OCR_FALLBACK_ENGINE=gemini
OCR_EXECUTION_TIMEOUT_S=600
PADDLE_VL_MAX_SIDE=2048
```

The API host requires `PADDLE_VL_API_KEY` and `PADDLE_VL_DEVICE=gpu:0`; `PADDLE_VL_API_MAX_BYTES` defaults to `26214400` (25 MiB). The app's `PADDLE_VL_API_URL` is the origin/base URL; the client appends `/v1/ocr`. `OCR_EXECUTION_TIMEOUT_S` bounds the whole remote attempt/retry deadline; the client has a fixed limit of two attempts and retries only connection/timeouts and HTTP 408/429/500/502/503/504. It does not retry authentication, validation, or other non-transient 4xx errors. `PADDLE_VL_MAX_SIDE` controls the shared image downscale step before upload. Do not enable the endpoint until the environment secret and private routing are configured.

The API response contains `text`, `words`, `boxes`, `tables`, `headers`, `rows`, `page`, `page_info`, `model`, `status`, and inference `latency_ms`. The app adapter preserves text and copies table/header/row/page evidence into the existing `OCRResult.metadata`, which continues through source grounding and column parsing.

## Authenticated real-image smoke and audit acceptance

Run from the repository root with Python 3.10+ (the script uses only the standard library):

```powershell
$env:PADDLE_VL_API_URL = 'https://<private-worker-host>'
$env:PADDLE_VL_API_KEY = '<load from secret manager>'
$env:PADDLE_VL_TEST_TIMEOUT_S = '600'
$env:AUDIT_IMAGE_DIR = 'C:\path\to\original-audit-images'
python cloud_ocr/test_image.py `
  (Join-Path $env:AUDIT_IMAGE_DIR '0000727190_2026_08_ZA_06_333_04092026081416.png') `
  (Join-Path $env:AUDIT_IMAGE_DIR '0000701478_2026_08_ZA_01_224_08092026054726.jpg') `
  (Join-Path $env:AUDIT_IMAGE_DIR '0000733955_2026_08_ZA_04_286_05092026143428.jpeg') `
  --save-dir cloud_ocr/results
```

The script sends each original image once, requires non-empty OCR text and all response evidence keys, verifies page numbering, prints text/headers/rows and both worker/client latency, and saves raw responses to `cloud_ocr/results`. Compare those saved results with each original image and the baseline in `audits/za_august_2026_10doc_rerun3/COMPARISON.md`:

- PNG: confirm OCR preserves `Total=55` separately from `CloseStock=54`.
- JPG: leave headerless quantities unresolved unless the image establishes their meaning.
- JPEG: compare product names and quantities against the original photo; do not infer from the prior JSON.

The script's successful HTTP response is only an OCR smoke test. Acceptance requires a human/source comparison of all three actual outputs and the resulting final JSON. Save the generated JSON and document the image-specific observations and latency; a model load, healthy Docker container, or non-empty text alone is not acceptance.
