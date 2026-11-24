"""PaddleOCR-VL 1.6 smoke test — real model init + inference.

Run with the isolated VL venv (production env must stay unchanged):

    .venvs/paddleocr_vl/Scripts/python.exe scripts/test_paddle_vl_ocr.py
    .venvs/paddleocr_vl/Scripts/python.exe scripts/test_paddle_vl_ocr.py --image path.jpg
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.ocr.paddle_vl_engine import (  # noqa: E402
    ENGINE_NAME,
    MODEL_NAME,
    PIPELINE_VERSION,
    PaddleVLEngine,
    evidence_from_result,
    extract_numeric_tokens,
)


DEFAULT_DATASET = Path(r"C:\Users\amnsa\Downloads\ZL_2026_August")
SELECTION = ROOT / "phase3_eval" / "selection.json"


def _pick_default_image() -> Path:
    env = os.getenv("PADDLE_VL_SMOKE_IMAGE")
    if env and Path(env).is_file():
        return Path(env)
    if SELECTION.is_file():
        payload = json.loads(SELECTION.read_text(encoding="utf-8"))
        for entry in payload.get("files") or []:
            path = Path(entry.get("path") or "")
            if path.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"} and path.is_file():
                return path
    if DEFAULT_DATASET.is_dir():
        for path in sorted(DEFAULT_DATASET.glob("*.jpg")):
            return path
    raise SystemExit("No smoke image found. Pass --image.")


def _env_block(engine: PaddleVLEngine) -> None:
    paddle_ver = "not_available"
    paddleocr_ver = "not_available"
    cuda = "not_available"
    gpu = "not_available"
    vram = "not_available"
    device = "not_available"
    try:
        import paddle

        paddle_ver = getattr(paddle, "__version__", "?")
        cuda = str(paddle.is_compiled_with_cuda())
        try:
            if paddle.is_compiled_with_cuda() and paddle.device.cuda.device_count() > 0:
                gpu = "cuda:0"
                try:
                    props = paddle.device.cuda.get_device_properties(0)
                    name = getattr(props, "name", None) or str(props)
                    total = getattr(props, "total_memory", None)
                    gpu = str(name)
                    if total:
                        vram = f"{int(total) / (1024 ** 3):.1f} GiB"
                except Exception:
                    pass
        except Exception:
            pass
        device = engine._detect_device()
    except Exception as exc:
        paddle_ver = f"error: {exc}"
    try:
        import paddleocr

        paddleocr_ver = getattr(paddleocr, "__version__", "?")
    except Exception as exc:
        paddleocr_ver = f"error: {exc}"

    info = engine.model_info()
    print("=" * 50)
    print("PADDLEOCR-VL 1.6 ENVIRONMENT")
    print("=" * 50)
    print(f"PaddlePaddle: {paddle_ver}")
    print(f"PaddleOCR: {paddleocr_ver}")
    print(f"PaddleOCR-VL: pipeline_version={PIPELINE_VERSION} ({MODEL_NAME})")
    print(f"Python: {sys.version.split()[0]} ({platform.system()})")
    print(f"CUDA: {cuda}")
    print(f"GPU: {gpu}")
    print(f"VRAM: {vram}")
    print(f"Device: {device}")
    print(f"Model: {info.get('model')} / {info.get('pipeline_version')}")
    print(f"Model loaded: {info.get('model_loaded')}")
    print("=" * 50)
    print(flush=True)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="PaddleOCR-VL 1.6 smoke test")
    parser.add_argument("--image", type=Path, default=None)
    parser.add_argument("--device", type=str, default=None)
    args = parser.parse_args(argv)

    image_path = args.image or _pick_default_image()
    engine = PaddleVLEngine(device=args.device)

    if not engine.available():
        print("PADDLEOCR-VL 1.6 IMPLEMENTATION: BLOCKED")
        print("available()=False", engine.model_info())
        return 2

    # Warm via recognize. Subprocess backend must not call in-process ``_client()``.
    info = engine.model_info()
    if info.get("backend") != "subprocess":
        try:
            engine._client()
        except Exception as exc:
            _env_block(engine)
            print("Model init failed:", type(exc).__name__, exc)
            print("PADDLEOCR-VL 1.6 IMPLEMENTATION: BLOCKED")
            return 2

    _env_block(engine)

    raw = image_path.read_bytes()
    # Warm dimensions via recognize (includes decode / subprocess worker).
    result = engine.recognize(raw, page=1, metadata={"source_path": str(image_path)})
    evidence = evidence_from_result(result)
    meta = result.metadata or {}
    conf = meta.get("confidence") or {}
    bbox_stats = meta.get("bbox_stats") or {}
    numeric = evidence.get("numeric_tokens") or extract_numeric_tokens(result.text or "")
    text = result.text or ""

    print("=" * 50)
    print("PADDLEOCR-VL 1.6 SMOKE TEST")
    print("=" * 50)
    print(f"Status: {evidence.get('status')}")
    print(f"Engine: {ENGINE_NAME}")
    print(f"Model: {evidence.get('model')}")
    print(f"Version: {evidence.get('model_version')}")
    print(f"Device: {evidence.get('device')}")
    print(f"GPU: {meta.get('device')}")
    print()
    print(f"Image: {image_path}")
    print(f"Width: {meta.get('image_width')}")
    print(f"Height: {meta.get('image_height')}")
    print()
    print(f"Text length: {len(text)}")
    print(f"Word count: {evidence.get('word_count')}")
    print(f"Line count: {meta.get('line_count')}")
    print()
    print(f"Numeric token count: {len(numeric)}")
    print(f"Mean confidence: {conf.get('mean')}")
    print(f"Latency: {meta.get('latency_ms')}")
    print()
    print("Bounding boxes:")
    print(f"Valid: {bbox_stats.get('valid')}")
    print(f"Invalid: {bbox_stats.get('invalid')}")
    print()
    print("First 500 characters:")
    print(text[:500])
    print()
    print("Numeric tokens:")
    print(numeric[:80])
    print("=" * 50)

    if evidence.get("status") != "success" or not text.strip():
        print("PADDLEOCR-VL 1.6 IMPLEMENTATION: BLOCKED")
        return 1
    print("PADDLEOCR-VL 1.6 IMPLEMENTATION: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
