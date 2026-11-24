"""PaddleOCR adapter. Optional; unavailable when the package is not installed.

The API interpreter does not install classic PaddleOCR: paddleocr 2.7 pins
protobuf 3.20 and opencv 4.6. Inference runs in ``.venvs/paddleocr`` when
that interpreter is present, and in-process when this interpreter already
has the package.
"""

from __future__ import annotations

import base64
import io
import json
import logging
import subprocess
from pathlib import Path
from typing import Any, Dict, Optional

from services.ocr.base import OCREngine, OCRResult, OCRWord

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CLASSIC_PYTHON = _REPO_ROOT / ".venvs" / "paddleocr" / "Scripts" / "python.exe"
_CLASSIC_PYTHON_UNIX = _REPO_ROOT / ".venvs" / "paddleocr" / "bin" / "python"
_CLASSIC_WORKER = Path(__file__).resolve().parent / "classic_worker.py"


def _classic_python() -> Optional[str]:
    for candidate in (_CLASSIC_PYTHON, _CLASSIC_PYTHON_UNIX):
        if candidate.is_file():
            return str(candidate)
    return None


def preload_torch_before_paddle() -> Optional[str]:
    """Load torch before paddleocr to avoid WinError 127 on shm.dll.

    Classic PaddleOCR pulls ``albumentations.pytorch`` → torch. If paddle's
    native libs are already mapped, torch's ``shm.dll`` fails to resolve. Import
    torch first (when available). Never touches PaddleOCR-VL.
    """
    try:
        import torch  # noqa: F401

        return getattr(torch, "__version__", "ok")
    except Exception as exc:
        logger.debug("torch preload skipped: %s", exc)
        return None


def create_classic_paddleocr(**kwargs: Any) -> Any:
    """Construct classic ``PaddleOCR`` with a safe torch import order."""
    import os

    preload_torch_before_paddle()
    if "PADDLE_OCR_BASE_DIR" not in os.environ:
        repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        local_models = os.path.join(repo_root, ".paddleocr")
        if os.path.exists(os.path.join(local_models, "whl")):
            os.environ["PADDLE_OCR_BASE_DIR"] = local_models
    from paddleocr import PaddleOCR

    defaults = {"use_angle_cls": True, "lang": "en", "show_log": False}
    defaults.update(kwargs)
    return PaddleOCR(**defaults)


class PaddleEngine(OCREngine):
    name = "paddle"

    def __init__(self) -> None:
        self._ocr = None
        self._init_error: Optional[str] = None
        self._backend: Optional[str] = None

    def available(self) -> bool:
        """Cheap presence check. Do not import paddleocr/torch here."""
        try:
            import importlib.util

            if importlib.util.find_spec("paddleocr") is not None:
                if importlib.util.find_spec("numpy") is None:
                    self._init_error = "numpy_not_installed"
                    return False
                self._backend = "inprocess"
                self._init_error = None
                return True
        except Exception as exc:
            self._init_error = str(exc)
            return False
        if _classic_python() and _CLASSIC_WORKER.is_file():
            self._backend = "subprocess"
            self._init_error = None
            return True
        self._init_error = "paddleocr_not_installed"
        return False

    def _client(self):
        if self._ocr is not None:
            return self._ocr
        # Lightweight defaults; production policy decides when to call.
        self._ocr = create_classic_paddleocr()
        return self._ocr

    def recognize(
        self,
        image_bytes: bytes,
        *,
        page: int = 1,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> OCRResult:
        if not self.available():
            return OCRResult(
                text="",
                words=[],
                confidence=None,
                page=page,
                engine=self.name,
                metadata={
                    **dict(metadata or {}),
                    "available": False,
                    "error": self._init_error or "paddleocr_not_installed",
                },
            )
        if self._backend == "subprocess":
            return self._recognize_subprocess(
                image_bytes, page=page, metadata=metadata
            )
        try:
            from PIL import Image
            import numpy as np

            image = Image.open(io.BytesIO(image_bytes)).convert("RGB")
            array = np.array(image)
            client = self._client()
            raw = client.ocr(array, cls=True)
        except BaseException as exc:
            # DLL / torch load failures must not crash the process.
            logger.warning("paddleocr recognize failed: %s", exc)
            return OCRResult(
                text="",
                words=[],
                confidence=None,
                page=page,
                engine=self.name,
                metadata={
                    **dict(metadata or {}),
                    "available": False,
                    "error": str(exc),
                },
            )
        words = []
        texts = []
        confidences = []
        blocks = raw[0] if raw else []
        for block in blocks or []:
            if not block or len(block) < 2:
                continue
            box, payload = block[0], block[1]
            text = str(payload[0] or "").strip()
            try:
                conf = float(payload[1])
            except (TypeError, ValueError, IndexError):
                conf = None
            if not text:
                continue
            texts.append(text)
            if conf is not None:
                confidences.append(conf)
            xs = [float(point[0]) for point in box]
            ys = [float(point[1]) for point in box]
            words.append(
                OCRWord(
                    text=text,
                    bbox=(min(xs), min(ys), max(xs), max(ys)),
                    confidence=conf,
                    page=page,
                )
            )
        mean_conf = (
            sum(confidences) / len(confidences) if confidences else None
        )
        meta = dict(metadata or {})
        meta["word_count"] = len(words)
        meta["available"] = True
        return OCRResult(
            text="\n".join(texts),
            words=words,
            confidence=mean_conf,
            page=page,
            engine=self.name,
            metadata=meta,
        )

    def _recognize_subprocess(
        self,
        image_bytes: bytes,
        *,
        page: int,
        metadata: Optional[Dict[str, Any]],
    ) -> OCRResult:
        python = _classic_python()
        if not python:
            return OCRResult(
                text="",
                words=[],
                confidence=None,
                page=page,
                engine=self.name,
                metadata={
                    **dict(metadata or {}),
                    "available": False,
                    "error": "paddleocr_not_installed",
                },
            )
        from services.ocr.bounded import ocr_execution_timeout_s

        try:
            completed = subprocess.run(
                [python, str(_CLASSIC_WORKER)],
                input=base64.b64encode(image_bytes),
                capture_output=True,
                timeout=ocr_execution_timeout_s(),
                cwd=str(_REPO_ROOT),
                check=False,
            )
        except subprocess.TimeoutExpired:
            return OCRResult(
                text="",
                words=[],
                confidence=None,
                page=page,
                engine=self.name,
                metadata={
                    **dict(metadata or {}),
                    "available": False,
                    "error": "ocr_timeout",
                    "backend": "subprocess",
                },
            )
        if completed.returncode != 0:
            detail = (completed.stderr or b"").decode("utf-8", "replace")[-500:]
            return OCRResult(
                text="",
                words=[],
                confidence=None,
                page=page,
                engine=self.name,
                metadata={
                    **dict(metadata or {}),
                    "available": False,
                    "error": detail or f"classic_worker_exit_{completed.returncode}",
                    "backend": "subprocess",
                },
            )
        try:
            text = completed.stdout.decode("utf-8", "replace")
            line = [item for item in text.splitlines() if item.strip()][-1]
            raw = json.loads(line)
        except (json.JSONDecodeError, IndexError) as exc:
            return OCRResult(
                text="",
                words=[],
                confidence=None,
                page=page,
                engine=self.name,
                metadata={
                    **dict(metadata or {}),
                    "available": False,
                    "error": f"classic_worker_bad_json:{exc}",
                    "backend": "subprocess",
                },
            )
        return self._result_from_blocks(raw, page=page, metadata=metadata)

    def _result_from_blocks(self, raw, *, page: int, metadata: Optional[Dict[str, Any]]) -> OCRResult:
        words = []
        texts = []
        confidences = []
        blocks = raw[0] if raw else []
        for block in blocks or []:
            if not block or len(block) < 2:
                continue
            box, payload = block[0], block[1]
            text = str(payload[0] or "").strip()
            try:
                conf = float(payload[1])
            except (TypeError, ValueError, IndexError):
                conf = None
            if not text:
                continue
            texts.append(text)
            if conf is not None:
                confidences.append(conf)
            xs = [float(point[0]) for point in box]
            ys = [float(point[1]) for point in box]
            words.append(
                OCRWord(
                    text=text,
                    bbox=(min(xs), min(ys), max(xs), max(ys)),
                    confidence=conf,
                    page=page,
                )
            )
        mean_conf = sum(confidences) / len(confidences) if confidences else None
        meta = dict(metadata or {})
        meta["word_count"] = len(words)
        meta["available"] = True
        meta["backend"] = "subprocess"
        return OCRResult(
            text="\n".join(texts),
            words=words,
            confidence=mean_conf,
            page=page,
            engine=self.name,
            metadata=meta,
        )
