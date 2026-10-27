"""Surya 2 experimental OCR adapter.

Opt-in evidence producer only. Does not change production OCR routing or
defaults. Requires ``surya-ocr`` >= 0.20 (SuryaInferenceManager API) plus an
inference backend (vLLM/Docker GPU or llama.cpp ``llama-server``).
"""

from __future__ import annotations

import io
import logging
import os
import shutil
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from services.ocr.base import OCREngine, OCRResult, OCRWord
from services.ocr.paddle_vl_engine import extract_numeric_tokens, normalize_ocr_text

logger = logging.getLogger(__name__)

ENGINE_NAME = "surya-2"
MODEL_NAME = "Surya-2"
MODEL_VERSION = "2"
MIN_PACKAGE_HINT = "0.20"


def _bbox_from_any(
    value: Any,
) -> Optional[Tuple[float, float, float, float]]:
    if value is None:
        return None
    try:
        if isinstance(value, (list, tuple)) and len(value) == 4:
            if all(isinstance(v, (int, float)) for v in value):
                x0, y0, x1, y1 = (float(v) for v in value)
                return (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))
            # polygon of points
            xs = [float(p[0]) for p in value if isinstance(p, (list, tuple)) and len(p) >= 2]
            ys = [float(p[1]) for p in value if isinstance(p, (list, tuple)) and len(p) >= 2]
            if xs and ys:
                return (min(xs), min(ys), max(xs), max(ys))
        if hasattr(value, "__iter__") and not isinstance(value, (str, bytes)):
            pts = list(value)
            if len(pts) == 4 and all(isinstance(v, (int, float)) for v in pts):
                x0, y0, x1, y1 = (float(v) for v in pts)
                return (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))
    except (TypeError, ValueError, IndexError):
        return None
    return None


def _attr(obj: Any, *names: str, default: Any = None) -> Any:
    if obj is None:
        return default
    if isinstance(obj, dict):
        for name in names:
            if name in obj and obj[name] is not None:
                return obj[name]
        return default
    for name in names:
        if hasattr(obj, name):
            value = getattr(obj, name)
            if value is not None:
                return value
    return default


def _plain_from_html(html: str) -> str:
    text = str(html or "")
    if not text:
        return ""
    try:
        from bs4 import BeautifulSoup

        return BeautifulSoup(text, "html.parser").get_text(" ", strip=True)
    except Exception:
        # Lightweight fallback — enough for numeric matching if bs4 missing.
        import re

        return re.sub(r"<[^>]+>", " ", text)


class SuryaEngine(OCREngine):
    """Adapter for Surya 2 (``SuryaInferenceManager`` + ``RecognitionPredictor``)."""

    name = ENGINE_NAME
    model_name = MODEL_NAME
    model_version = MODEL_VERSION

    def __init__(
        self,
        *,
        device: Optional[str] = None,
        inference_url: Optional[str] = None,
        keep_alive: Optional[bool] = None,
    ) -> None:
        self._manager = None
        self._predictor = None
        self._init_error: Optional[str] = None
        self._block_reason: Optional[str] = None
        self._init_latency_ms: Optional[float] = None
        self._device = (device or os.getenv("SURYA_DEVICE") or "").strip() or None
        self._inference_url = (
            inference_url or os.getenv("SURYA_INFERENCE_URL") or ""
        ).strip() or None
        self._keep_alive = (
            keep_alive
            if keep_alive is not None
            else os.getenv("SURYA_INFERENCE_KEEP_ALIVE", "").strip().lower()
            in {"1", "true", "yes", "on"}
        )
        self._resolved_backend: Optional[str] = None
        self._package_version: Optional[str] = None

    def available(self) -> bool:
        """Cheap presence check for package + Surya 2 API (+ backend hints)."""
        try:
            import importlib.util

            if importlib.util.find_spec("surya") is None:
                self._init_error = "surya_not_installed"
                self._block_reason = (
                    "BLOCKED: surya-ocr package not installed "
                    "(need Surya 2 / surya-ocr>=0.20 with SuryaInferenceManager)."
                )
                return False
            try:
                import surya

                self._package_version = getattr(surya, "__version__", None)
            except Exception:
                self._package_version = None

            # Surya 2 API gate — reject v1 FoundationPredictor-only installs.
            if importlib.util.find_spec("surya.inference") is None:
                self._init_error = "surya_v1_api_only"
                self._block_reason = (
                    "BLOCKED: installed surya lacks surya.inference "
                    "(Surya 2 / SuryaInferenceManager required)."
                )
                return False
            from surya.inference import SuryaInferenceManager  # noqa: F401
            from surya.recognition import RecognitionPredictor  # noqa: F401

            backend_ok, backend_detail = self._backend_probe()
            if not backend_ok:
                self._init_error = "surya_inference_backend_unavailable"
                self._block_reason = backend_detail
                return False
            return True
        except Exception as exc:
            self._init_error = str(exc)
            self._block_reason = f"BLOCKED: surya import/API check failed: {exc}"
            return False

    def _find_llama_server(self) -> Optional[str]:
        """Locate llama-server on PATH or under project ``.tools/llama.cpp``."""
        for name in ("llama-server", "llama-server.exe"):
            found = shutil.which(name)
            if found:
                return found
        # Project-local tools (not a system install).
        root = Path(__file__).resolve().parents[2]
        candidates = list((root / ".tools" / "llama.cpp").glob("**/llama-server.exe"))
        candidates += list((root / ".tools" / "llama.cpp").glob("**/llama-server"))
        for path in candidates:
            if path.is_file():
                return str(path)
        explicit = (os.getenv("LLAMA_CPP_BINARY") or "").strip()
        if explicit and Path(explicit).is_file():
            return explicit
        return None

    def _backend_probe(self) -> Tuple[bool, str]:
        """Detect whether an inference backend can be used.

        Surya 2 OCR requires vLLM (Docker+NVIDIA) or llama.cpp ``llama-server``.
        An explicit ``SURYA_INFERENCE_URL`` counts as ready.
        """
        if self._inference_url:
            self._resolved_backend = f"url:{self._inference_url}"
            return True, ""

        env_backend = (os.getenv("SURYA_INFERENCE_BACKEND") or "").strip().lower()
        llama = self._find_llama_server()
        if llama:
            # Ensure Surya's autostart can find the binary by name or path.
            os.environ.setdefault("LLAMA_CPP_BINARY", llama)
            # Put containing dir on PATH for child process lookup.
            llama_dir = str(Path(llama).resolve().parent)
            path_now = os.environ.get("PATH") or ""
            if llama_dir not in path_now.split(os.pathsep):
                os.environ["PATH"] = llama_dir + os.pathsep + path_now
            self._resolved_backend = f"llama.cpp:{llama}"
            return True, ""

        # Docker + NVIDIA runtime for vLLM path.
        docker = shutil.which("docker")
        if docker and env_backend in {"", "vllm", "docker"}:
            # Presence of docker binary is not enough — daemon must respond.
            # Cheap probe without pulling images.
            try:
                import subprocess

                proc = subprocess.run(
                    [docker, "info"],
                    capture_output=True,
                    text=True,
                    timeout=8,
                    check=False,
                )
                if proc.returncode == 0:
                    self._resolved_backend = "docker/vllm"
                    return True, ""
                detail = (proc.stderr or proc.stdout or "").strip().splitlines()
                detail_s = detail[0] if detail else f"exit={proc.returncode}"
                return (
                    False,
                    "BLOCKED: Docker is installed but the daemon is not running "
                    f"({detail_s}). Surya 2 GPU path needs Docker + NVIDIA "
                    "Container Toolkit for vLLM, or llama-server on PATH for "
                    "llama.cpp CPU/GPU.",
                )
            except Exception as exc:
                return (
                    False,
                    "BLOCKED: Docker probe failed "
                    f"({type(exc).__name__}: {exc}). Surya 2 needs a running "
                    "vLLM/Docker GPU stack or llama-server on PATH.",
                )

        return (
            False,
            "BLOCKED: no Surya 2 inference backend found. Need either "
            "(1) SURYA_INFERENCE_URL pointing at an OpenAI-compatible server, "
            "(2) llama-server from llama.cpp on PATH, or "
            "(3) Docker daemon with NVIDIA Container Toolkit for vLLM. "
            f"SURYA_INFERENCE_BACKEND={env_backend or 'unset'}; "
            f"docker={'yes' if docker else 'no'}; llama-server=no.",
        )

    def model_info(self) -> Dict[str, Any]:
        return {
            "engine": self.name,
            "model": self.model_name,
            "model_version": self.model_version,
            "package_version": self._package_version,
            "min_package_hint": MIN_PACKAGE_HINT,
            "device": self._device or "auto",
            "inference_backend": self._resolved_backend,
            "inference_url": self._inference_url,
            "keep_alive": self._keep_alive,
            "init_latency_ms": self._init_latency_ms,
            "model_loaded": self._predictor is not None,
            "init_error": self._init_error,
            "block_reason": self._block_reason,
            "experimental": True,
        }

    def block_reason(self) -> Optional[str]:
        if self._block_reason:
            return self._block_reason
        if not self.available():
            return self._block_reason or self._init_error
        return None

    def _client(self):
        if self._predictor is not None:
            return self._predictor
        from surya.inference import SuryaInferenceManager
        from surya.recognition import RecognitionPredictor

        started = time.perf_counter()
        kwargs: Dict[str, Any] = {}
        # Prefer keep-alive for multi-page benchmarks when requested.
        if self._keep_alive:
            os.environ.setdefault("SURYA_INFERENCE_KEEP_ALIVE", "1")
        if self._inference_url:
            os.environ.setdefault("SURYA_INFERENCE_URL", self._inference_url)
        try:
            self._manager = SuryaInferenceManager(**kwargs) if kwargs else SuryaInferenceManager()
        except TypeError:
            # Older/newer signatures may not accept kwargs.
            self._manager = SuryaInferenceManager()
        self._predictor = RecognitionPredictor(self._manager)
        self._init_latency_ms = (time.perf_counter() - started) * 1000.0
        return self._predictor

    def recognize(
        self,
        image_bytes: bytes,
        *,
        page: int = 1,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> OCRResult:
        base_meta = dict(metadata or {})
        base_meta.update(self.model_info())
        if not self.available():
            return OCRResult(
                text="",
                words=[],
                confidence=None,
                page=page,
                engine=self.name,
                metadata={
                    **base_meta,
                    "available": False,
                    "status": "error",
                    "error": self._init_error or "surya_unavailable",
                    "block_reason": self._block_reason,
                },
            )
        try:
            from PIL import Image

            image = Image.open(io.BytesIO(image_bytes)).convert("RGB")
            width, height = image.size
            predictor = self._client()
            started = time.perf_counter()
            raw = predictor([image])
            latency_ms = (time.perf_counter() - started) * 1000.0
            page_result = raw[0] if isinstance(raw, list) and raw else raw
            parsed = self._normalize_prediction(page_result, page=page)
        except BaseException as exc:
            logger.warning("surya-2 recognize failed: %s", exc)
            self._block_reason = (
                f"BLOCKED: Surya 2 execution failed ({type(exc).__name__}: {exc})"
            )
            return OCRResult(
                text="",
                words=[],
                confidence=None,
                page=page,
                engine=self.name,
                metadata={
                    **base_meta,
                    **self.model_info(),
                    "available": False,
                    "status": "error",
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                    "block_reason": self._block_reason,
                },
            )

        text = parsed["text"]
        words = parsed["words"]
        confidences = [
            float(w.confidence)
            for w in words
            if isinstance(w.confidence, (int, float))
        ]
        mean_conf = sum(confidences) / len(confidences) if confidences else None
        token_word_count = len(normalize_ocr_text(text).split()) if text else 0
        meta = {
            **base_meta,
            **self.model_info(),
            "available": True,
            "status": "success" if text.strip() else "error",
            "latency_ms": latency_ms,
            "image_width": width,
            "image_height": height,
            "word_count": token_word_count or len(words),
            "block_count": len(words),
            "line_count": parsed["line_count"],
            "numeric_tokens": extract_numeric_tokens(text),
            "normalized_text": normalize_ocr_text(text),
            "boxes": parsed["boxes"],
            "lines": parsed["lines"],
            "layout": parsed["layout"],
            "tables": parsed["tables"],
            "reading_order": parsed["reading_order"],
            "confidence": {
                "mean": mean_conf,
                "count": len(confidences),
                "available": bool(confidences),
            },
            "bbox_stats": parsed["bbox_stats"],
            "html_fragments": parsed.get("html_fragments") or [],
        }
        if not text.strip():
            meta["error"] = "empty_ocr_text"
        return OCRResult(
            text=text,
            words=words,
            confidence=mean_conf,
            page=page,
            engine=self.name,
            metadata=meta,
        )

    def _normalize_prediction(self, page_result: Any, *, page: int) -> Dict[str, Any]:
        blocks = _attr(page_result, "blocks", default=None) or []
        words: List[OCRWord] = []
        lines: List[Dict[str, Any]] = []
        boxes: List[List[float]] = []
        texts: List[str] = []
        html_fragments: List[str] = []
        layout_regions: List[Dict[str, Any]] = []
        tables: List[Dict[str, Any]] = []
        reading_order: List[Any] = []
        valid_boxes = 0
        invalid_boxes = 0

        if not isinstance(blocks, list):
            blocks = list(blocks) if blocks else []

        for index, block in enumerate(blocks):
            html = str(_attr(block, "html", "content", "text", default="") or "")
            plain = _plain_from_html(html).strip() if html else str(
                _attr(block, "text", "content", default="") or ""
            ).strip()
            label = _attr(block, "label", "block_label", "type", default=None)
            bbox = _bbox_from_any(
                _attr(block, "bbox", "block_bbox", "polygon", "points", default=None)
            )
            conf = _attr(block, "confidence", "score", default=None)
            try:
                conf_f = float(conf) if conf is not None else None
            except (TypeError, ValueError):
                conf_f = None
            order = _attr(block, "order", "block_order", default=index)
            if html:
                html_fragments.append(html)
            layout_regions.append(
                {
                    "index": index,
                    "label": label,
                    "text": plain,
                    "html": html or None,
                    "bbox": list(bbox) if bbox else None,
                    "order": order,
                    "confidence": conf_f,
                }
            )
            reading_order.append(order)
            # Prefer HTML in assembled text so column-aware table matching can run.
            piece = html.strip() if html and ("<table" in html.lower() or "<td" in html.lower()) else plain
            if not piece and plain:
                piece = plain
            if piece:
                texts.append(piece)
                lines.append(
                    {
                        "text": plain or piece,
                        "html": html or None,
                        "bbox": list(bbox) if bbox else None,
                        "label": label,
                        "order": order,
                        "confidence": conf_f,
                    }
                )
                words.append(
                    OCRWord(
                        text=plain or piece,
                        bbox=bbox,
                        confidence=conf_f,
                        page=page,
                    )
                )
                if bbox:
                    boxes.append(list(bbox))
                    valid_boxes += 1
                else:
                    invalid_boxes += 1
            if label and "table" in str(label).lower():
                tables.append(
                    {
                        "index": index,
                        "label": label,
                        "text": plain,
                        "html": html or None,
                        "bbox": list(bbox) if bbox else None,
                    }
                )
            elif html and "<table" in html.lower():
                tables.append(
                    {
                        "index": index,
                        "label": label or "table",
                        "text": plain,
                        "html": html,
                        "bbox": list(bbox) if bbox else None,
                    }
                )

        # Fallback: some builds expose text_lines (v1) — refuse silently inventing.
        if not texts:
            legacy = _attr(page_result, "text_lines", "text", default=None)
            if isinstance(legacy, list):
                for index, line in enumerate(legacy):
                    value = str(_attr(line, "text", "content", default=line) or "").strip()
                    if not value:
                        continue
                    bbox = _bbox_from_any(_attr(line, "bbox", "polygon", default=None))
                    texts.append(value)
                    words.append(OCRWord(text=value, bbox=bbox, confidence=None, page=page))
                    lines.append({"text": value, "bbox": list(bbox) if bbox else None, "order": index})
            elif isinstance(legacy, str) and legacy.strip():
                texts.append(legacy.strip())

        text = "\n".join(t for t in texts if t)
        return {
            "text": text,
            "words": words,
            "lines": lines,
            "boxes": boxes,
            "html_fragments": html_fragments,
            "line_count": len(lines) if lines else (
                len([ln for ln in text.splitlines() if ln.strip()]) if text else 0
            ),
            "layout": {
                "regions": layout_regions,
                "available": bool(layout_regions),
            },
            "tables": {
                "items": tables,
                "available": bool(tables),
                "count": len(tables),
            },
            "reading_order": reading_order if reading_order else None,
            "bbox_stats": {
                "valid": valid_boxes,
                "invalid": invalid_boxes,
                "total": valid_boxes + invalid_boxes,
            },
        }


def evidence_from_result(result: OCRResult) -> Dict[str, Any]:
    """Normalize an OCRResult into the shared benchmark evidence shape."""
    meta = dict(result.metadata or {})
    text = result.text or ""
    words = [w.to_dict() for w in (result.words or [])]
    boxes = meta.get("boxes")
    if not isinstance(boxes, list):
        boxes = [list(w.bbox) for w in (result.words or []) if w.bbox is not None]
    conf = meta.get("confidence")
    if not isinstance(conf, dict):
        conf = {
            "mean": result.confidence,
            "available": result.confidence is not None,
        }
    return {
        "engine": ENGINE_NAME,
        "status": meta.get("status") or ("success" if text.strip() else "error"),
        "model": meta.get("model") or MODEL_NAME,
        "model_version": meta.get("model_version") or MODEL_VERSION,
        "text": text,
        "normalized_text": meta.get("normalized_text") or normalize_ocr_text(text),
        "word_count": meta.get("word_count", len(words)),
        "line_count": meta.get("line_count"),
        "numeric_tokens": meta.get("numeric_tokens") or extract_numeric_tokens(text),
        "words": words,
        "boxes": boxes,
        "confidence": conf if conf else None,
        "layout": meta.get("layout"),
        "tables": meta.get("tables"),
        "latency_ms": meta.get("latency_ms"),
        "image_width": meta.get("image_width"),
        "image_height": meta.get("image_height"),
        "device": meta.get("device"),
        "block_reason": meta.get("block_reason"),
        "error": meta.get("error"),
        "metadata": {
            k: v
            for k, v in meta.items()
            if k
            not in {
                "text",
                "normalized_text",
                "numeric_tokens",
                "words",
                "boxes",
                "layout",
                "tables",
                "confidence",
            }
        },
    }
