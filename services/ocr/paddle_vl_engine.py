"""PaddleOCR-VL 1.6 adapter.

Primary OCR engine for OCR-required documents when ``OCR_ENGINE=paddleocr-vl``
(the production default). Distinct from classic ``PaddleEngine`` (PP-OCR).

Supports in-process inference when ``paddleocr`` is installed, or a subprocess
worker via ``PADDLE_VL_PYTHON`` / the isolated ``.venvs/paddleocr_vl`` venv.
"""

from __future__ import annotations

import base64
import io
import json
import logging
import os
import re
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from services.ocr.base import OCREngine, OCRResult, OCRWord

logger = logging.getLogger(__name__)

ENGINE_NAME = "paddleocr-vl-1.6"
MODEL_NAME = "PaddleOCR-VL-1.6"
PIPELINE_VERSION = "v1.6"
_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_VL_PYTHON = _REPO_ROOT / ".venvs" / "paddleocr_vl" / "Scripts" / "python.exe"
_DEFAULT_VL_PYTHON_UNIX = _REPO_ROOT / ".venvs" / "paddleocr_vl" / "bin" / "python"
_WORKER = Path(__file__).resolve().parent / "vl_worker.py"
_SERVE_LOCK = None  # lazy threading.Lock
_SERVE_PROC: Optional[subprocess.Popen] = None
_SERVE_PYTHON: Optional[str] = None
_PERSISTENT_INIT_ERROR: Optional[str] = None
_CPU_RETRY_USED = False
def paddle_init_blocked() -> Optional[str]:
    """Error text after a cudnn/tensor init failure survived the one CPU retry."""
    return _PERSISTENT_INIT_ERROR


def reset_paddle_init_state() -> None:
    """Test helper. Clears the process-wide init failure cache."""
    global _PERSISTENT_INIT_ERROR, _CPU_RETRY_USED
    _PERSISTENT_INIT_ERROR = None
    _CPU_RETRY_USED = False


def _is_poisoned_paddle_error(message: str) -> bool:
    text = str(message or "").lower()
    return any(
        token in text
        for token in (
            "cudnn",
            "winerror 127",
            "specified procedure could not be found",
            "has no attribute 'tensor'",
            "has no attribute \"tensor\"",
            "partially initialized module 'paddle'",
            "partially initialized module \"paddle\"",
        )
    )


def _mark_persistent_init_failure(message: str) -> None:
    global _PERSISTENT_INIT_ERROR
    _PERSISTENT_INIT_ERROR = str(message or "paddle_init_failed")


def _blocked_result(page: int, metadata: Dict[str, Any]) -> OCRResult:
    return OCRResult(
        text="",
        words=[],
        confidence=None,
        page=page,
        engine=ENGINE_NAME,
        metadata={
            **metadata,
            "available": False,
            "status": "error",
            "error": _PERSISTENT_INIT_ERROR or "paddle_init_failed",
            "persistent_init_failure": True,
        },
    )
NUMERIC_TOKEN_RE = re.compile(
    r"(?<![\w./-])"  # avoid mid-token matches
    r"(?:"
    r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|"  # 19,892.88
    r"\d+\.\d+|"  # 57005.00 / 19892.88
    r"\d+"  # integers
    r")"
    r"(?![\w./-])"
)

# Cap long-edge before VL inference. 12MP pages on a 6GB laptop GPU otherwise
# hang / OOM / crash-loop for many minutes.
def _vl_max_side() -> int:
    try:
        return max(512, int(os.getenv("PADDLE_VL_MAX_SIDE", "2048") or "2048"))
    except (TypeError, ValueError):
        return 2048


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _vl_pipeline_speed_kwargs() -> Dict[str, Any]:
    """Disable unused VL sub-pipelines for pharma stock pages.

    Orientation / unwarping / chart recognition are not needed for upright
    stock-statement photos and add multi-model overhead on a 6GB laptop GPU.
    Layout detection stays ON (default) so HTML tables remain available.
    """
    return {
        "use_doc_orientation_classify": _env_bool(
            "PADDLE_VL_USE_DOC_ORIENTATION", False
        ),
        "use_doc_unwarping": _env_bool("PADDLE_VL_USE_DOC_UNWARPING", False),
        "use_chart_recognition": _env_bool("PADDLE_VL_USE_CHART_RECOGNITION", False),
        "use_layout_detection": _env_bool("PADDLE_VL_USE_LAYOUT_DETECTION", True),
    }


def _downscale_image_bytes(image_bytes: bytes) -> Tuple[bytes, Dict[str, Any]]:
    """Downscale oversized pages; return (bytes, meta). Identity if already small."""
    meta: Dict[str, Any] = {"downscaled": False, "max_side": _vl_max_side()}
    try:
        from PIL import Image

        with Image.open(io.BytesIO(image_bytes)) as im:
            im = im.convert("RGB")
            w, h = im.size
            meta["orig_width"] = w
            meta["orig_height"] = h
            long_edge = max(w, h)
            limit = _vl_max_side()
            if long_edge <= limit:
                return image_bytes, meta
            scale = limit / float(long_edge)
            nw, nh = max(1, int(w * scale)), max(1, int(h * scale))
            resample = getattr(Image, "Resampling", Image).LANCZOS
            resized = im.resize((nw, nh), resample)
            buf = io.BytesIO()
            resized.save(buf, format="JPEG", quality=92, optimize=True)
            meta.update(
                {
                    "downscaled": True,
                    "width": nw,
                    "height": nh,
                    "bytes_in": len(image_bytes),
                    "bytes_out": buf.tell(),
                }
            )
            return buf.getvalue(), meta
    except Exception as exc:
        meta["downscale_error"] = str(exc)
        return image_bytes, meta


def extract_numeric_tokens(text: str) -> List[str]:
    """Return numeric-like tokens exactly as they appear in OCR text."""
    if not text:
        return []
    return NUMERIC_TOKEN_RE.findall(text)


def _env_flag(name: str) -> bool:
    return (os.getenv(name) or "").strip().lower() in {"1", "true", "yes", "on"}


def _worker_child() -> bool:
    return _env_flag("PADDLE_VL_WORKER_CHILD")


def _same_python(candidate: str) -> bool:
    try:
        import sys

        left = Path(candidate).resolve()
        right = Path(sys.executable).resolve()
        return left == right
    except Exception:
        return False


def _serve_lock():
    global _SERVE_LOCK
    if _SERVE_LOCK is None:
        import threading

        _SERVE_LOCK = threading.Lock()
    return _SERVE_LOCK


def normalize_ocr_text(text: str) -> str:
    return " ".join(str(text or "").split())


def _bbox_from_points(
    points: Any,
) -> Optional[Tuple[float, float, float, float]]:
    if not points:
        return None
    try:
        if (
            isinstance(points, (list, tuple))
            and len(points) == 4
            and all(isinstance(v, (int, float)) for v in points)
        ):
            x0, y0, x1, y1 = (float(v) for v in points)
            return (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))
        xs: List[float] = []
        ys: List[float] = []
        for point in points:
            if isinstance(point, (list, tuple)) and len(point) >= 2:
                xs.append(float(point[0]))
                ys.append(float(point[1]))
            elif isinstance(point, dict):
                xs.append(float(point.get("x", point.get(0, 0))))
                ys.append(float(point.get("y", point.get(1, 0))))
        if not xs or not ys:
            return None
        return (min(xs), min(ys), max(xs), max(ys))
    except (TypeError, ValueError, IndexError, KeyError):
        return None


def _unwrap_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Prefer the inner ``res`` dict used by paddlex ``.json`` wrappers."""
    nested = payload.get("res")
    if isinstance(nested, dict) and (
        "parsing_res_list" in nested
        or "rec_texts" in nested
        or "layout_det_res" in nested
        or "markdown" in nested
    ):
        return nested
    return payload


def _as_dict(obj: Any) -> Dict[str, Any]:
    if obj is None:
        return {}
    if isinstance(obj, dict):
        return _unwrap_payload(obj)
    # Mapping-like paddlex results expose page fields via keys()/get().
    if hasattr(obj, "keys") and hasattr(obj, "get") and not isinstance(obj, type):
        try:
            mapping = {key: obj.get(key) for key in list(obj.keys())}
            if mapping:
                return _unwrap_payload(mapping)
        except Exception:
            pass
    for attr in ("json", "res", "data"):
        value = getattr(obj, attr, None)
        if isinstance(value, dict):
            return _unwrap_payload(value)
        if callable(value):
            try:
                maybe = value()
            except Exception:
                maybe = None
            if isinstance(maybe, dict):
                return _unwrap_payload(maybe)
    if hasattr(obj, "to_dict") and callable(obj.to_dict):
        try:
            maybe = obj.to_dict()
            if isinstance(maybe, dict):
                return _unwrap_payload(maybe)
        except Exception:
            pass
    return {}


def _block_field(block: Any, *names: str) -> Any:
    if isinstance(block, dict):
        for name in names:
            if name in block and block[name] is not None:
                return block[name]
        return None
    for name in names:
        if hasattr(block, name):
            value = getattr(block, name)
            if value is not None:
                return value
    return None


def _block_as_dict(block: Any) -> Dict[str, Any]:
    if isinstance(block, dict):
        return block
    out: Dict[str, Any] = {}
    for name in (
        "label",
        "block_label",
        "content",
        "block_content",
        "text",
        "bbox",
        "block_bbox",
        "box",
        "coordinate",
        "points",
        "block_polygon_points",
        "html",
        "table_html",
        "cells",
        "table_cells",
        "block_order",
        "block_id",
        "type",
    ):
        if hasattr(block, name):
            out[name] = getattr(block, name)
    return out


class PaddleVLEngine(OCREngine):
    """Adapter for PaddleOCR-VL 1.6 (``PaddleOCRVL(pipeline_version='v1.6')``)."""

    name = ENGINE_NAME
    model_name = MODEL_NAME
    model_version = PIPELINE_VERSION

    def __init__(
        self,
        *,
        device: Optional[str] = None,
        pipeline_version: str = PIPELINE_VERSION,
        python_executable: Optional[str] = None,
        timeout_s: Optional[float] = None,
    ) -> None:
        self._pipeline = None
        self._init_error: Optional[str] = None
        self._init_latency_ms: Optional[float] = None
        self._device = (device or os.getenv("PADDLE_VL_DEVICE") or "").strip() or None
        self._pipeline_version = pipeline_version or PIPELINE_VERSION
        self._resolved_device: Optional[str] = None
        self._backend: Optional[str] = None  # inprocess | subprocess
        self._force_inprocess = False
        self._python_executable = (
            (python_executable or os.getenv("PADDLE_VL_PYTHON") or "").strip()
            or None
        )
        self._timeout_explicit = timeout_s is not None
        if timeout_s is None:
            from services.ocr.bounded import ocr_execution_timeout_s

            # Cold VL load is often 60–120s; keep-alive reuses the process.
            self._timeout_s = ocr_execution_timeout_s()
        else:
            self._timeout_s = float(timeout_s)

    def available(self) -> bool:
        """Cheap presence check. Avoids heavy model load.

        Prefer the isolated venv subprocess from the *host* interpreter so a
        broken system-site ``paddleocr`` cannot crash the API process.

        Never select subprocess when:
        - ``PADDLE_VL_WORKER_CHILD=1`` (already inside vl_worker)
        - ``PADDLE_VL_ALLOW_INPROCESS=true``
        - ``sys.executable`` is the VL venv python (would recurse forever)
        """
        if os.getenv("PADDLE_VL_API_URL", "").strip():
            self._backend = "remote"
            self._init_error = None
            return bool(os.getenv("PADDLE_VL_API_KEY", "").strip())

        if self._force_inprocess or _worker_child() or _env_flag("PADDLE_VL_ALLOW_INPROCESS"):
            if self._specs_present():
                self._backend = "inprocess"
                self._init_error = None
                return True
            return False

        python = self._resolve_python()
        # Running *as* the VL interpreter: in-process only (no self-spawn).
        if python and _same_python(python) and self._specs_present():
            self._backend = "inprocess"
            self._init_error = None
            return True

        if python and _WORKER.is_file() and not _same_python(python):
            self._backend = "subprocess"
            self._init_error = None
            return True

        if self._specs_present() and _env_flag("PADDLE_VL_ALLOW_INPROCESS"):
            self._backend = "inprocess"
            self._init_error = None
            return True

        if not self._init_error:
            self._init_error = "paddleocr_vl_unavailable"
        return False

    def _specs_present(self) -> bool:
        """Spec-only check. Never import paddleocr here (DLL crashes on Windows)."""
        try:
            import importlib.util

            if importlib.util.find_spec("paddleocr") is None:
                self._init_error = "paddleocr_not_installed"
                return False
            if importlib.util.find_spec("paddle") is None:
                self._init_error = "paddlepaddle_not_installed"
                return False
            self._init_error = None
            return True
        except Exception as exc:
            self._init_error = str(exc)
            return False

    def _inprocess_available(self) -> bool:
        """Backward-compatible alias used by tests."""
        if _worker_child() or _env_flag("PADDLE_VL_ALLOW_INPROCESS") or self._force_inprocess:
            return self._specs_present()
        python = self._resolve_python()
        if python and _same_python(python):
            return self._specs_present()
        return False

    def _resolve_python(self) -> Optional[str]:
        if self._python_executable and Path(self._python_executable).is_file():
            return self._python_executable
        for candidate in (_DEFAULT_VL_PYTHON, _DEFAULT_VL_PYTHON_UNIX):
            if candidate.is_file():
                return str(candidate)
        return None

    def model_info(self) -> Dict[str, Any]:
        return {
            "engine": self.name,
            "model": self.model_name,
            "model_version": self.model_version,
            "pipeline_version": self._pipeline_version,
            "device": self._resolved_device or self._device or "auto",
            "backend": self._backend,
            "python_executable": self._resolve_python() if self._backend == "subprocess" else None,
            "timeout_s": self._deadline(),
            "init_latency_ms": self._init_latency_ms,
            "model_loaded": self._pipeline is not None,
            "init_error": self._init_error,
        }

    def _client(self):
        if self._pipeline is not None:
            return self._pipeline
        from paddleocr import PaddleOCRVL

        kwargs: Dict[str, Any] = {"pipeline_version": self._pipeline_version}
        if self._device:
            kwargs["device"] = self._device
        kwargs.update(_vl_pipeline_speed_kwargs())
        started = time.perf_counter()
        self._pipeline = PaddleOCRVL(**kwargs)
        self._init_latency_ms = (time.perf_counter() - started) * 1000.0
        self._resolved_device = self._device or self._detect_device()
        self._pipeline_speed_kwargs = {
            k: kwargs[k]
            for k in (
                "use_doc_orientation_classify",
                "use_doc_unwarping",
                "use_chart_recognition",
                "use_layout_detection",
            )
        }
        # Guard against accidentally binding classic OCR.
        class_name = type(self._pipeline).__name__
        if "VL" not in class_name.upper():
            self._pipeline = None
            raise RuntimeError(
                f"expected PaddleOCRVL pipeline, got {class_name}"
            )
        return self._pipeline

    @staticmethod
    def _detect_device() -> str:
        try:
            import paddle

            if paddle.is_compiled_with_cuda():
                try:
                    return str(paddle.device.get_device())
                except Exception:
                    return "gpu"
            return "cpu"
        except Exception:
            return "unknown"

    def _deadline(self) -> float:
        if self._timeout_explicit:
            return float(self._timeout_s)
        from services.ocr.bounded import ocr_execution_timeout_s

        return ocr_execution_timeout_s()

    def _predict_bounded(self, client: Any, array: Any, predict_kwargs: Dict[str, Any], page: int, base_meta: Dict[str, Any]):
        """Return the raw prediction, or an OCRResult when the deadline passes.

        The worker child is stopped by the parent process deadline. Timing it
        out in-thread would leave a predict running inside the keep-alive worker.
        """
        def _predict() -> Any:
            return client.predict(array, **predict_kwargs)

        if _worker_child():
            return _predict()
        from services.ocr.bounded import OcrExecutionTimeout, call_bounded

        try:
            return call_bounded(_predict, self._deadline(), label="paddleocr-vl")
        except OcrExecutionTimeout as exc:
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
                    "error": "ocr_timeout",
                    "error_type": "OcrExecutionTimeout",
                    "provider_error": True,
                    "timeout_s": exc.timeout_s,
                },
            )

    def recognize(
        self,
        image_bytes: bytes,
        *,
        page: int = 1,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> OCRResult:
        base_meta = dict(metadata or {})
        base_meta.update(self.model_info())
        if os.getenv("PADDLE_VL_API_URL", "").strip():
            prepared, scale_meta = _downscale_image_bytes(image_bytes)
            base_meta["image_prep"] = scale_meta
            return self._recognize_remote(prepared, page=page, metadata=base_meta)
        if _PERSISTENT_INIT_ERROR:
            return _blocked_result(page, base_meta)
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
                    "error": self._init_error or "paddleocr_vl_unavailable",
                },
            )
        prepared, scale_meta = _downscale_image_bytes(image_bytes)
        base_meta["image_prep"] = scale_meta

        if self._backend == "subprocess" or (
            self._backend is None and not self._inprocess_available()
        ):
            return self._recognize_subprocess(
                prepared, page=page, metadata=base_meta
            )
        try:
            from PIL import Image
            import numpy as np

            image = Image.open(io.BytesIO(prepared)).convert("RGB")
            width, height = image.size
            array = np.asarray(image)
            client = self._client()
            predict_kwargs = getattr(self, "_pipeline_speed_kwargs", None) or (
                _vl_pipeline_speed_kwargs()
            )
            started = time.perf_counter()
            raw = self._predict_bounded(client, array, predict_kwargs, page, base_meta)
            if isinstance(raw, OCRResult):
                return raw
            latency_ms = (time.perf_counter() - started) * 1000.0
            parsed = self._normalize_prediction(raw, page=page)
            base_meta["pipeline_flags"] = dict(predict_kwargs)
        except BaseException as exc:
            logger.warning("paddleocr-vl recognize failed: %s", exc)
            # Never recurse into subprocess from inside the worker child.
            python = self._resolve_python()
            if (
                not _worker_child()
                and python
                and not _same_python(python)
                and _WORKER.is_file()
            ):
                logger.info("paddleocr-vl falling back to subprocess worker")
                return self._recognize_subprocess(
                    prepared, page=page, metadata=base_meta
                )
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
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                },
            )

        return self._result_from_parsed(
            parsed,
            page=page,
            base_meta=base_meta,
            latency_ms=latency_ms,
            width=width,
            height=height,
        )

    def _recognize_remote(
        self, image_bytes: bytes, *, page: int, metadata: Dict[str, Any]
    ) -> OCRResult:
        """Call the opt-in authenticated cloud PaddleOCR-VL worker."""
        url = os.getenv("PADDLE_VL_API_URL", "").strip().rstrip("/") + "/v1/ocr"
        api_key = os.getenv("PADDLE_VL_API_KEY", "").strip()
        timeout = min(max(self._deadline(), 1.0), 600.0)
        attempts = 2
        started = time.perf_counter()
        deadline = started + timeout
        last_error = "remote_ocr_failed"
        attempts_made = 0
        for attempt in range(attempts):
            remaining = deadline - time.perf_counter()
            if remaining <= 0:
                last_error = "TimeoutError:remote_ocr_deadline_exceeded"
                break
            attempts_made += 1
            boundary = "----PaddleOCRVL" + str(time.time_ns())
            body = (
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"page.bin\"\r\n"
                "Content-Type: application/octet-stream\r\n\r\n"
            ).encode("ascii") + image_bytes + f"\r\n--{boundary}--\r\n".encode("ascii")
            request = urllib.request.Request(
                url,
                data=body,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": f"multipart/form-data; boundary={boundary}",
                    "X-OCR-Page": str(int(page)),
                },
                method="POST",
            )
            try:
                with urllib.request.urlopen(request, timeout=remaining) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                payload["page"] = int(page)
                result = _result_from_evidence(
                    payload,
                    page=page,
                    latency_ms=(time.perf_counter() - started) * 1000.0,
                )
                result.metadata.update(metadata)
                result.metadata.update(
                    backend="remote_api",
                    remote_attempts=attempts_made,
                    remote_latency_ms=(time.perf_counter() - started) * 1000.0,
                    remote_tables=payload.get("tables"),
                    headers=payload.get("headers", []),
                    rows=payload.get("rows", []),
                    page_info=payload.get("page_info"),
                )
                return result
            except urllib.error.HTTPError as exc:
                last_error = f"remote_http_{exc.code}"
                if exc.code not in (408, 429, 500, 502, 503, 504) or attempt + 1 >= attempts:
                    break
            except (
                urllib.error.URLError,
                TimeoutError,
                json.JSONDecodeError,
                OSError,
                TypeError,
                ValueError,
                AttributeError,
            ) as exc:
                last_error = f"{type(exc).__name__}:{exc}"
                if attempt + 1 >= attempts:
                    break
            delay = min(0.2 * (attempt + 1), max(0.0, deadline - time.perf_counter()))
            if delay:
                time.sleep(delay)
        return OCRResult(
            text="", words=[], confidence=None, page=page, engine=self.name,
            metadata={
                **metadata, "available": False, "status": "error",
                "error": last_error, "backend": "remote_api",
                "remote_attempts": attempts_made, "timeout_s": timeout,
                "latency_ms": (time.perf_counter() - started) * 1000.0,
            },
        )

    def _recognize_subprocess(
        self,
        image_bytes: bytes,
        *,
        page: int,
        metadata: Dict[str, Any],
    ) -> OCRResult:
        """Talk to a keep-alive VL worker (one model load per process)."""
        if _worker_child():
            return OCRResult(
                text="",
                words=[],
                confidence=None,
                page=page,
                engine=self.name,
                metadata={
                    **metadata,
                    "available": False,
                    "status": "error",
                    "error": "recursive_subprocess_blocked",
                    "backend": "subprocess",
                },
            )
        python = self._resolve_python()
        if not python or _same_python(python):
            return OCRResult(
                text="",
                words=[],
                confidence=None,
                page=page,
                engine=self.name,
                metadata={
                    **metadata,
                    "available": False,
                    "status": "error",
                    "error": "paddle_vl_python_missing_or_self",
                    "backend": "subprocess",
                },
            )
        self._backend = "subprocess"
        started = time.perf_counter()
        try:
            evidence = _serve_request(
                python,
                image_bytes,
                page=page,
                device=self._device,
                timeout_s=self._deadline(),
            )
        except subprocess.TimeoutExpired:
            _kill_serve_proc()
            return OCRResult(
                text="",
                words=[],
                confidence=None,
                page=page,
                engine=self.name,
                metadata={
                    **metadata,
                    "available": False,
                    "status": "error",
                    "error": "ocr_timeout",
                    "error_type": "TimeoutExpired",
                    "provider_error": True,
                    "backend": "subprocess",
                    "timeout_s": self._deadline(),
                },
            )
        except Exception as exc:
            _kill_serve_proc()
            return self._absorb_init_failure(
                OCRResult(
                    text="",
                    words=[],
                    confidence=None,
                    page=page,
                    engine=self.name,
                    metadata={
                        **metadata,
                        "available": False,
                        "status": "error",
                        "error": str(exc),
                        "error_type": type(exc).__name__,
                        "backend": "subprocess",
                    },
                ),
                image_bytes,
                page=page,
                metadata=metadata,
            )
        latency_ms = (time.perf_counter() - started) * 1000.0
        if not evidence:
            return OCRResult(
                text="",
                words=[],
                confidence=None,
                page=page,
                engine=self.name,
                metadata={
                    **metadata,
                    "available": False,
                    "status": "error",
                    "error": "vl_worker_empty_response",
                    "backend": "subprocess",
                    "latency_ms": latency_ms,
                },
            )
        return self._absorb_init_failure(
            _result_from_evidence(evidence, page=page, latency_ms=latency_ms),
            image_bytes,
            page=page,
            metadata=metadata,
        )

    def _absorb_init_failure(
        self,
        result: OCRResult,
        image_bytes: bytes,
        *,
        page: int,
        metadata: Dict[str, Any],
    ) -> OCRResult:
        """Retry a poisoned GPU worker once on CPU, then stop calling it."""
        global _CPU_RETRY_USED
        error = str((result.metadata or {}).get("error") or "")
        if not _is_poisoned_paddle_error(error):
            return result
        _kill_serve_proc()
        if not _CPU_RETRY_USED:
            _CPU_RETRY_USED = True
            self._device = "cpu"
            os.environ["CUDA_VISIBLE_DEVICES"] = "-1"
            return self._recognize_subprocess(
                image_bytes, page=page, metadata=metadata
            )
        _mark_persistent_init_failure(error)
        meta = dict(result.metadata or {})
        meta["persistent_init_failure"] = True
        result.metadata = meta
        return result

    def _result_from_parsed(
        self,
        parsed: Dict[str, Any],
        *,
        page: int,
        base_meta: Dict[str, Any],
        latency_ms: float,
        width: int,
        height: int,
    ) -> OCRResult:
        text = parsed["text"]
        words = parsed["words"]
        confidences = [
            float(w.confidence)
            for w in words
            if isinstance(w.confidence, (int, float))
        ]
        mean_conf = (
            sum(confidences) / len(confidences) if confidences else None
        )
        # VL often returns layout blocks, not whitespace tokens. Expose both.
        token_word_count = len(normalize_ocr_text(text).split()) if text else 0
        meta = {
            **base_meta,
            **self.model_info(),
            "available": True,
            "status": "success",
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
            "device": self._resolved_device or self._device or "auto",
            "raw_keys": parsed.get("raw_keys") or [],
        }
        return OCRResult(
            text=text,
            words=words,
            confidence=mean_conf,
            page=page,
            engine=self.name,
            metadata=meta,
        )


    def _normalize_prediction(
        self, raw: Any, *, page: int
    ) -> Dict[str, Any]:
        payloads: List[Dict[str, Any]] = []
        if isinstance(raw, list):
            for item in raw:
                payloads.append(_as_dict(item))
        else:
            payloads.append(_as_dict(raw))

        words: List[OCRWord] = []
        lines: List[Dict[str, Any]] = []
        boxes: List[List[float]] = []
        texts: List[str] = []
        layout_regions: List[Dict[str, Any]] = []
        tables: List[Dict[str, Any]] = []
        reading_order: List[Any] = []
        valid_boxes = 0
        invalid_boxes = 0

        for payload in payloads:
            # Common paddlex / PaddleOCR-VL shapes (block_* keys in 1.6 JSON).
            nested = payload.get("res") if isinstance(payload.get("res"), dict) else {}
            parsing = (
                payload.get("parsing_res_list")
                or payload.get("parsing_result")
                or nested.get("parsing_res_list")
                or nested.get("parsing_result")
            )
            table_res = (
                payload.get("table_res_list")
                or nested.get("table_res_list")
                or []
            )
            if isinstance(table_res, list):
                for index, table in enumerate(table_res):
                    table_dict = _block_as_dict(table) if not isinstance(table, dict) else table
                    tables.append(
                        {
                            "index": index,
                            "label": "table",
                            "text": str(
                                table_dict.get("content")
                                or table_dict.get("block_content")
                                or table_dict.get("html")
                                or ""
                            ),
                            "bbox": table_dict.get("bbox")
                            or table_dict.get("block_bbox"),
                            "html": table_dict.get("html")
                            or table_dict.get("table_html")
                            or table_dict.get("pred_html"),
                            "cells": table_dict.get("cells")
                            or table_dict.get("table_cells"),
                        }
                    )

            if isinstance(parsing, list) and parsing:
                for index, block in enumerate(parsing):
                    block_dict = _block_as_dict(block)
                    if not block_dict and not hasattr(block, "content"):
                        continue
                    block_text = str(
                        _block_field(
                            block,
                            "block_content",
                            "content",
                            "text",
                        )
                        or ""
                    ).strip()
                    bbox = _bbox_from_points(
                        _block_field(
                            block,
                            "block_bbox",
                            "bbox",
                            "box",
                            "coordinate",
                            "points",
                            "block_polygon_points",
                        )
                    )
                    label = _block_field(block, "block_label", "label", "type")
                    order = _block_field(block, "block_order", "order")
                    if order is None:
                        order = index
                    layout_regions.append(
                        {
                            "index": index,
                            "label": label,
                            "text": block_text,
                            "bbox": list(bbox) if bbox else None,
                            "order": order,
                        }
                    )
                    reading_order.append(order)
                    if block_text:
                        texts.append(block_text)
                        lines.append(
                            {
                                "text": block_text,
                                "bbox": list(bbox) if bbox else None,
                                "label": label,
                                "order": order,
                            }
                        )
                        words.append(
                            OCRWord(
                                text=block_text,
                                bbox=bbox,
                                confidence=None,
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
                                "text": block_text,
                                "bbox": list(bbox) if bbox else None,
                                "html": _block_field(
                                    block, "html", "table_html", "pred_html"
                                ),
                                "cells": _block_field(
                                    block, "cells", "table_cells"
                                ),
                            }
                        )
                continue

            # Fallback: classic-style [[box, (text, conf)], ...] nested lists.
            blocks = payload.get("rec_texts") or payload.get("texts")
            scores = payload.get("rec_scores") or payload.get("scores")
            polys = (
                payload.get("rec_polys")
                or payload.get("dt_polys")
                or payload.get("boxes")
            )
            if isinstance(blocks, list) and blocks:
                for index, text in enumerate(blocks):
                    value = str(text or "").strip()
                    if not value:
                        continue
                    conf = None
                    if isinstance(scores, list) and index < len(scores):
                        try:
                            conf = float(scores[index])
                        except (TypeError, ValueError):
                            conf = None
                    bbox = None
                    if isinstance(polys, list) and index < len(polys):
                        bbox = _bbox_from_points(polys[index])
                    texts.append(value)
                    lines.append(
                        {
                            "text": value,
                            "bbox": list(bbox) if bbox else None,
                            "order": index,
                        }
                    )
                    words.append(
                        OCRWord(
                            text=value,
                            bbox=bbox,
                            confidence=conf,
                            page=page,
                        )
                    )
                    if bbox:
                        boxes.append(list(bbox))
                        valid_boxes += 1
                    else:
                        invalid_boxes += 1
                continue

            # Last resort: markdown / plain text fields.
            markdown = payload.get("markdown") or nested.get("markdown")
            if isinstance(markdown, dict):
                md_texts = markdown.get("markdown_texts") or markdown.get("text")
                if isinstance(md_texts, list):
                    joined = "\n".join(str(t) for t in md_texts if t)
                    if joined.strip():
                        texts.append(joined.strip())
                        continue
                if isinstance(md_texts, str) and md_texts.strip():
                    texts.append(md_texts.strip())
                    continue
            for key in ("markdown", "md", "text", "ocr_text"):
                value = payload.get(key)
                if isinstance(value, str) and value.strip():
                    texts.append(value.strip())
                    break

        text = "\n".join(t for t in texts if t)
        return {
            "text": text,
            "words": words,
            "lines": lines,
            "boxes": boxes,
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
            "raw_keys": sorted({k for p in payloads for k in p.keys()}),
        }


def _kill_serve_proc() -> None:
    """Terminate the keep-alive worker and any Windows child processes."""
    global _SERVE_PROC, _SERVE_PYTHON
    proc = _SERVE_PROC
    _SERVE_PROC = None
    _SERVE_PYTHON = None
    if proc is None:
        return
    try:
        if proc.poll() is not None:
            return
        # On Windows the serve launcher can leave a GPU grandchild alive if we
        # only terminate the Popen handle — kill the whole tree.
        if os.name == "nt" and proc.pid:
            try:
                subprocess.run(
                    ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                    capture_output=True,
                    check=False,
                    timeout=15,
                )
            except Exception:
                pass
        try:
            if proc.poll() is None:
                proc.terminate()
                proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
    except Exception:
        pass


def kill_paddle_vl_serve() -> None:
    """Public hook for page-level timeouts to abandon a stuck VL predict."""
    _kill_serve_proc()
    logger.warning("paddle_vl_serve_killed reason=page_or_execution_timeout")


def _ensure_serve_proc(python: str, *, device: Optional[str], timeout_s: float):
    """Start keep-alive worker once; reuse across pages."""
    global _SERVE_PROC, _SERVE_PYTHON
    proc = _SERVE_PROC
    if proc is not None and proc.poll() is None and _SERVE_PYTHON == python:
        return proc
    _kill_serve_proc()
    # -u: unbuffered stdout so the ready marker is visible immediately.
    cmd = [python, "-u", str(_WORKER), "--serve"]
    if device:
        cmd.extend(["--device", device])
    # stderr must NOT be a PIPE — paddle/model logs fill the buffer and
    # deadlock the worker while the parent waits on stdout.
    log_path = _REPO_ROOT / "phase4" / "paddle_vl_benchmark" / "vl_worker.stderr.log"
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        stderr_handle = open(log_path, "a", encoding="utf-8", errors="replace")
    except OSError:
        stderr_handle = subprocess.DEVNULL
    child_env = os.environ.copy()
    child_env["PYTHONIOENCODING"] = "utf-8"
    child_env["PYTHONUTF8"] = "1"
    if str(device or "").strip().lower() == "cpu":
        # Set before the child imports paddle. A GPU wheel that cannot load
        # cudnn_cnn64_9.dll otherwise poisons later imports in that process.
        child_env["CUDA_VISIBLE_DEVICES"] = "-1"
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=stderr_handle,
        cwd=str(_REPO_ROOT),
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        env=child_env,
    )
    # Wait for ready marker (model not necessarily loaded yet).
    assert proc.stdout is not None
    ready_deadline = time.perf_counter() + min(60.0, max(15.0, timeout_s / 4.0))
    while time.perf_counter() < ready_deadline:
        line = proc.stdout.readline()
        if not line:
            if proc.poll() is not None:
                raise RuntimeError(
                    f"vl_worker_exit_before_ready:exit={proc.returncode} log={log_path}"
                )
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if payload.get("status") == "ready":
            _SERVE_PROC = proc
            _SERVE_PYTHON = python
            return proc
        if payload.get("status") == "error":
            raise RuntimeError(str(payload.get("error") or "vl_worker_ready_error"))
    _kill_serve_proc()
    raise TimeoutError(f"vl_worker_ready_timeout log={log_path}")


def _read_stdout_line(proc: subprocess.Popen, deadline: float) -> Optional[str]:
    """Read one stdout line, or None when the deadline passes first."""
    import threading

    if proc.stdout is None:
        return None
    box: Dict[str, Any] = {}

    def _read() -> None:
        try:
            box["line"] = proc.stdout.readline()
        except Exception as exc:
            box["error"] = exc

    thread = threading.Thread(target=_read, daemon=True)
    thread.start()
    thread.join(max(0.0, deadline - time.perf_counter()))
    if thread.is_alive():
        return None
    error = box.get("error")
    if error is not None:
        raise error
    return box.get("line")


def _serve_request(
    python: str,
    image_bytes: bytes,
    *,
    page: int,
    device: Optional[str],
    timeout_s: float,
) -> Dict[str, Any]:
    with _serve_lock():
        proc = _ensure_serve_proc(python, device=device, timeout_s=timeout_s)
        assert proc.stdin is not None and proc.stdout is not None
        request = {
            "page": page,
            "image_b64": base64.b64encode(image_bytes).decode("ascii"),
        }
        proc.stdin.write(json.dumps(request) + "\n")
        proc.stdin.flush()
        deadline = time.perf_counter() + float(timeout_s)
        while time.perf_counter() < deadline:
            if proc.poll() is not None:
                _kill_serve_proc()
                raise RuntimeError(f"vl_worker_died:exit={proc.returncode}")
            # readline() blocks, so the deadline above is not checked until a
            # line arrives. A stuck predict must still raise TimeoutExpired.
            line = _read_stdout_line(proc, deadline)
            if line is None:
                break
            if not line:
                time.sleep(0.05)
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if payload.get("status") == "ready":
                continue
            return payload
        _kill_serve_proc()
        raise subprocess.TimeoutExpired(cmd=python, timeout=timeout_s)


def _result_from_evidence(
    evidence: Dict[str, Any],
    *,
    page: int,
    latency_ms: float,
) -> OCRResult:
    words = []
    for item in evidence.get("words") or []:
        if not isinstance(item, dict):
            continue
        bbox = item.get("bbox")
        box = None
        if isinstance(bbox, (list, tuple)) and len(bbox) == 4:
            box = (float(bbox[0]), float(bbox[1]), float(bbox[2]), float(bbox[3]))
        conf = item.get("confidence")
        words.append(
            OCRWord(
                text=str(item.get("text") or ""),
                bbox=box,
                confidence=float(conf) if isinstance(conf, (int, float)) else None,
                page=int(item.get("page") or page),
            )
        )
    conf = evidence.get("confidence") if isinstance(evidence.get("confidence"), dict) else {}
    mean = conf.get("mean")
    text = str(evidence.get("text") or "")
    meta = {
        "engine": ENGINE_NAME,
        "model": evidence.get("model") or MODEL_NAME,
        "model_version": evidence.get("model_version") or PIPELINE_VERSION,
        "available": evidence.get("status") == "success",
        "status": evidence.get("status") or "error",
        "latency_ms": evidence.get("latency_ms") or latency_ms,
        "image_width": evidence.get("image_width"),
        "image_height": evidence.get("image_height"),
        "word_count": evidence.get("word_count"),
        "line_count": evidence.get("line_count"),
        "numeric_tokens": evidence.get("numeric_tokens") or extract_numeric_tokens(text),
        "normalized_text": evidence.get("normalized_text") or normalize_ocr_text(text),
        "boxes": evidence.get("boxes"),
        "layout": evidence.get("layout"),
        "tables": evidence.get("tables"),
        "confidence": conf if conf else {"mean": mean, "available": mean is not None},
        "device": evidence.get("device"),
        "backend": "subprocess",
        "error": evidence.get("error"),
        "error_type": evidence.get("error_type"),
    }
    nested = evidence.get("metadata")
    if isinstance(nested, dict):
        meta.update({k: v for k, v in nested.items() if k not in meta})
    return OCRResult(
        text=text,
        words=words,
        confidence=float(mean) if isinstance(mean, (int, float)) else None,
        page=page,
        engine=ENGINE_NAME,
        metadata=meta,
    )


def evidence_from_result(result: OCRResult) -> Dict[str, Any]:
    """Normalize an OCRResult into the PaddleOCR-VL benchmark evidence shape."""
    meta = dict(result.metadata or {})
    text = result.text or ""
    words = [w.to_dict() for w in (result.words or [])]
    boxes = meta.get("boxes")
    if not isinstance(boxes, list):
        boxes = [
            list(w.bbox) for w in (result.words or []) if w.bbox is not None
        ]
    conf = meta.get("confidence")
    if not isinstance(conf, dict):
        conf = {
            "mean": result.confidence,
            "available": result.confidence is not None,
        }
    return {
        "engine": ENGINE_NAME,
        "status": meta.get("status") or ("success" if text else "error"),
        "model": meta.get("model") or MODEL_NAME,
        "model_version": meta.get("model_version") or PIPELINE_VERSION,
        "text": text,
        "normalized_text": meta.get("normalized_text")
        or normalize_ocr_text(text),
        "word_count": meta.get("word_count", len(words)),
        "line_count": meta.get("line_count"),
        "numeric_tokens": meta.get("numeric_tokens")
        or extract_numeric_tokens(text),
        "words": words,
        "boxes": boxes,
        "confidence": conf if conf else None,
        "layout": meta.get("layout") if meta.get("layout") is not None else None,
        "tables": meta.get("tables") if meta.get("tables") is not None else None,
        "latency_ms": meta.get("latency_ms"),
        "image_width": meta.get("image_width"),
        "image_height": meta.get("image_height"),
        "device": meta.get("device"),
        "metadata": {
            k: v
            for k, v in meta.items()
            if k
            not in {
                "text",
                "normalized_text",
                "words",
                "boxes",
                "numeric_tokens",
                "layout",
                "tables",
                "confidence",
            }
        },
        "error": meta.get("error"),
        "error_type": meta.get("error_type"),
    }
