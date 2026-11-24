"""One-shot classic PaddleOCR worker for the isolated ``.venvs/paddleocr`` interpreter."""

from __future__ import annotations

import base64
import io
import json
import os
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
os.environ.setdefault("PADDLE_OCR_BASE_DIR", str(ROOT / ".paddleocr"))


def main() -> int:
    # Torch before paddleocr avoids WinError 127 on shm.dll (albumentations.pytorch).
    try:
        import torch  # noqa: F401
    except Exception:
        if "albumentations.pytorch" not in sys.modules:
            sys.modules["albumentations.pytorch"] = types.ModuleType("albumentations.pytorch")
    from PIL import Image
    import numpy as np
    from paddleocr import PaddleOCR

    image = Image.open(io.BytesIO(base64.b64decode(sys.stdin.buffer.read()))).convert("RGB")
    client = PaddleOCR(use_angle_cls=True, lang="en", show_log=False)
    raw = client.ocr(np.array(image), cls=True)
    sys.stdout.write(json.dumps(raw) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
