FROM nvidia/cuda:12.6.3-cudnn-runtime-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive PYTHONUNBUFFERED=1 PYTHONIOENCODING=utf-8 \
    PADDLE_VL_ALLOW_INPROCESS=1 PADDLE_VL_WORKER_CHILD=1
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-pip python3-venv libgomp1 libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements-paddleocr-vl.txt /app/requirements-paddleocr-vl.txt
RUN python3 -m pip install --no-cache-dir --upgrade pip \
    && python3 -m pip install --no-cache-dir torch==2.8.0 --index-url https://download.pytorch.org/whl/cu126 \
    && python3 -m pip install --no-cache-dir paddlepaddle-gpu==3.2.1 \
       --index-url https://www.paddlepaddle.org.cn/packages/stable/cu126/ \
       --extra-index-url https://pypi.org/simple \
    && python3 -m pip install --no-cache-dir paddleocr==3.7.0 paddlex==3.7.2 "pillow>=10.0.0" "numpy>=1.24.0" \
       "PyMuPDF>=1.23.0" "psutil>=5.9.0" fastapi==0.115.12 "uvicorn[standard]==0.34.2" python-multipart==0.0.20
COPY services/ /app/services/
COPY cloud_ocr/ /app/cloud_ocr/
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=120s --retries=3 \
  CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=3)" || exit 1
CMD ["python3", "-m", "uvicorn", "cloud_ocr.app:app", "--host", "0.0.0.0", "--port", "8080"]
