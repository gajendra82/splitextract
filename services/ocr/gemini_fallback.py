"""Gemini transcription fallback for unusable PaddleOCR-VL output.

This is not a second OCR engine for healthy pages. It returns plain text for
the existing extraction pipeline and labels the result as Gemini.
"""

from __future__ import annotations

import base64
import logging
import os
import threading
from typing import Any, Callable, Optional

from services.ocr.base import OCREngine, OCRResult

logger = logging.getLogger(__name__)

_PROMPT = (
    "Transcribe this stock/sales statement image into plain text only.\n"
    "Rules:\n"
    "- First copy header banners (stockist, company, period) as printed.\n"
    "- Then emit the table as pipe-separated rows with EXACTLY the same "
    "number of columns on every row as the header row.\n"
    "- Header example: Product Name|Pack|LstSL|Open|Recd.|Sales|Close|Order|Pend\n"
    "- Preserve EVERY printed column header. Keep empty/dashed cells as '-' "
    "(never omit a cell; never shift later values left into another column).\n"
    "- Copy only visible text and numbers. Do not invent, calculate, round, "
    "or reconcile quantities. Never write 0 for a blank/dashed cell.\n"
    "- Do not classify stockist vs company and do not add commentary.\n"
    "Return only the transcription."
)


def gemini_fallback_timeout_s() -> float:
    try:
        value = float(os.getenv("GEMINI_OCR_FALLBACK_TIMEOUT_S", "60") or "60")
    except (TypeError, ValueError):
        value = 60.0
    return max(1.0, min(value, 120.0))


def _image_mime(image_bytes: bytes) -> str:
    if image_bytes.startswith(b"\x89PNG"):
        return "image/png"
    if image_bytes.startswith(b"\xff\xd8"):
        return "image/jpeg"
    return "image/png"


def _response_text(response: Any) -> str:
    payload = response.json() if hasattr(response, "json") else response
    if not isinstance(payload, dict):
        return str(payload or "")
    candidates = payload.get("candidates") or [{}]
    content = (candidates[0] or {}).get("content") or {}
    parts = content.get("parts") or [{}]
    return str((parts[0] or {}).get("text") or "")


def validate_pipe_table_alignment(text: str) -> dict:
    """Check pipe-separated table rows keep a stable column count.

    Does not invent missing cells. Flags ambiguous rows when a data row's
    column count differs from the header (shifted empties).
    """
    import re

    lines = [ln.strip() for ln in str(text or "").splitlines() if ln.strip()]
    pipe_rows = [ln for ln in lines if ln.count("|") >= 2]
    meta: dict = {
        "pipe_table_detected": bool(pipe_rows),
        "column_alignment_ok": True,
        "ambiguous_rows": [],
        "expected_columns": None,
        "checked_rows": 0,
    }
    if not pipe_rows:
        # Free-text fallback: detect likely qty-token undercount vs header words.
        header = next(
            (
                ln
                for ln in lines
                if re.search(r"(?i)\bopen\b", ln)
                and re.search(r"(?i)\b(?:recd|sales|close)\b", ln)
            ),
            None,
        )
        if not header:
            meta["column_alignment_ok"] = False
            meta["reason"] = "no_pipe_table_or_header"
            return meta
        expected = len(re.findall(r"[A-Za-z][A-Za-z./]*", header))
        meta["expected_columns"] = expected
        meta["column_alignment_ok"] = False
        meta["reason"] = "non_pipe_transcription_unverified"
        return meta

    header_cols = [c.strip() for c in pipe_rows[0].split("|")]
    expected = len(header_cols)
    meta["expected_columns"] = expected
    for index, row in enumerate(pipe_rows[1:], start=2):
        cols = [c.strip() for c in row.split("|")]
        meta["checked_rows"] += 1
        if len(cols) != expected:
            meta["column_alignment_ok"] = False
            meta["ambiguous_rows"].append(
                {
                    "line": index,
                    "columns": len(cols),
                    "expected": expected,
                    "preview": row[:120],
                }
            )
    return meta


def _default_invoke(image_bytes: bytes, timeout: float) -> str:
    from services.sales_extraction_runtime import sales_generate_content_via_vertex

    model = os.getenv("VISION_MODEL", "gemini-2.5-flash-lite").strip() or "gemini-2.5-flash-lite"
    payload = {
        "contents": [
            {
                "role": "user",
                "parts": [
                    {"text": _PROMPT},
                    {
                        "inline_data": {
                            "mime_type": _image_mime(image_bytes),
                            "data": base64.b64encode(image_bytes).decode("ascii"),
                        }
                    },
                ],
            }
        ],
        "generationConfig": {"temperature": 0, "maxOutputTokens": 8192},
    }
    response = sales_generate_content_via_vertex(
        model=model,
        payload=payload,
        timeout=int(timeout),
        label="ocr_gemini_fallback",
    )
    return _response_text(response)


def _run_bounded(fn: Callable[[], str], timeout: float) -> str:
    """Return when the deadline expires. Do not join a hung Gemini call."""
    outcome: dict = {}

    def target() -> None:
        try:
            outcome["value"] = fn()
        except BaseException as exc:  # noqa: BLE001
            outcome["error"] = exc

    thread = threading.Thread(target=target, name="gemini-ocr-fallback", daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        raise TimeoutError(f"Gemini OCR fallback timed out after {timeout}s")
    if "error" in outcome:
        raise outcome["error"]
    return str(outcome.get("value") or "")


class GeminiOCRFallback(OCREngine):
    name = "gemini"

    def __init__(
        self,
        *,
        invoke: Optional[Callable[[bytes, float], str]] = None,
        timeout_s: Optional[float] = None,
    ) -> None:
        self._invoke = invoke or _default_invoke
        self._timeout_s = gemini_fallback_timeout_s() if timeout_s is None else timeout_s

    def available(self) -> bool:
        return True

    def recognize(
        self,
        image_bytes: bytes,
        *,
        page: int = 1,
        metadata: Optional[dict] = None,
    ) -> OCRResult:
        base = dict(metadata or {})
        timeout = self._timeout_s
        try:
            text = _run_bounded(lambda: self._invoke(image_bytes, timeout), timeout)
        except Exception as exc:
            logger.warning("ocr_fallback engine=gemini status=error error=%s", exc)
            return OCRResult(
                text="",
                words=[],
                confidence=None,
                page=page,
                engine=self.name,
                metadata={
                    **base,
                    "status": "error",
                    "error": str(exc),
                    "model": os.getenv("VISION_MODEL", "gemini-2.5-flash-lite"),
                },
            )
        cleaned = str(text or "").strip()
        if not cleaned:
            return OCRResult(
                text="",
                words=[],
                confidence=None,
                page=page,
                engine=self.name,
                metadata={**base, "status": "error", "error": "empty_output"},
            )
        alignment = validate_pipe_table_alignment(cleaned)
        meta = {
            **base,
            "status": "success",
            "model": os.getenv("VISION_MODEL", "gemini-2.5-flash-lite"),
            "source": "gemini_ocr_fallback",
            "column_alignment": alignment,
        }
        if not alignment.get("column_alignment_ok"):
            meta["column_alignment_ambiguous"] = True
            meta["unverified_fallback"] = True
            logger.warning(
                "gemini OCR column alignment ambiguous page=%s ambiguous_rows=%s reason=%s",
                page,
                len(alignment.get("ambiguous_rows") or []),
                alignment.get("reason"),
            )
        return OCRResult(
            text=cleaned,
            words=[],
            confidence=None,
            page=page,
            engine=self.name,
            metadata=meta,
        )
