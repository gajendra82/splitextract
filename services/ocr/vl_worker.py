"""Subprocess worker for PaddleOCR-VL in the isolated venv.

Must never re-enter subprocess mode: this process *is* the VL runtime.
Always runs PaddleOCRVL in-process.

Modes:
  One-shot:
    python services/ocr/vl_worker.py --image path.png --page 1
    python services/ocr/vl_worker.py --stdin-b64 --page 1 < image.b64
  Keep-alive server (one model load, many pages):
    python services/ocr/vl_worker.py --serve
    stdin lines: {\"page\":1,\"image_b64\":\"...\"}
    stdout lines: OCR evidence JSON
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
from pathlib import Path

# Critical: this process is the VL interpreter. Never spawn another worker.
os.environ["PADDLE_VL_ALLOW_INPROCESS"] = "1"
os.environ["PADDLE_VL_WORKER_CHILD"] = "1"
# Avoid Windows cp1252 crashes when model/OCR emits non-ASCII.
os.environ.setdefault("PYTHONIOENCODING", "utf-8")


def _force_utf8_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def _emit(payload: dict) -> None:
    """Write one JSON line; ASCII-safe so cp1252 consoles cannot kill the worker."""
    print(json.dumps(payload, ensure_ascii=True), flush=True)


def _boot_path() -> None:
    root = Path(__file__).resolve().parents[2]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))


def _engine(device: str = ""):
    _boot_path()
    from services.ocr.paddle_vl_engine import PaddleVLEngine

    engine = PaddleVLEngine(device=device or None)
    # Belt-and-suspenders against recursive subprocess selection.
    engine._force_inprocess = True
    return engine


def _run_once(engine, image_bytes: bytes, page: int) -> dict:
    from services.ocr.paddle_vl_engine import evidence_from_result

    result = engine.recognize(image_bytes, page=page)
    return evidence_from_result(result)


def _serve(device: str = "") -> int:
    _force_utf8_stdio()
    engine = _engine(device=device)
    # Preflight availability without loading the heavy model yet.
    if not engine.available():
        _emit(
            {
                "status": "error",
                "error": engine.model_info().get("init_error")
                or "paddleocr_vl_unavailable",
            }
        )
        return 2
    # Ready marker for the parent (not evidence JSON).
    _emit({"status": "ready", "backend": "inprocess"})
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError as exc:
            _emit({"status": "error", "error": f"bad_request:{exc}"})
            continue
        if req.get("cmd") == "shutdown":
            _emit({"status": "shutdown"})
            return 0
        try:
            image_bytes = base64.b64decode(req.get("image_b64") or b"")
            page = int(req.get("page") or 1)
            evidence = _run_once(engine, image_bytes, page)
        except Exception as exc:
            evidence = {
                "status": "error",
                "error": str(exc),
                "error_type": type(exc).__name__,
            }
        _emit(evidence)
    return 0


def main(argv: list[str] | None = None) -> int:
    _force_utf8_stdio()
    parser = argparse.ArgumentParser(description="PaddleOCR-VL worker")
    parser.add_argument("--image", type=str, default="")
    parser.add_argument("--stdin-b64", action="store_true")
    parser.add_argument("--page", type=int, default=1)
    parser.add_argument("--device", type=str, default="")
    parser.add_argument(
        "--serve",
        action="store_true",
        help="Keep-alive server: one model load, JSON lines on stdin/stdout",
    )
    args = parser.parse_args(argv)

    if args.serve:
        return _serve(device=args.device)

    if args.stdin_b64:
        payload = sys.stdin.buffer.read()
        image_bytes = base64.b64decode(payload)
    elif args.image:
        image_bytes = Path(args.image).read_bytes()
    else:
        _emit({"status": "error", "error": "no_image"})
        return 2

    engine = _engine(device=args.device)
    evidence = _run_once(engine, image_bytes, args.page)
    _emit(evidence)
    return 0 if evidence.get("status") == "success" else 1


if __name__ == "__main__":
    raise SystemExit(main())
