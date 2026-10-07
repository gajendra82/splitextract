"""Client for the internally hosted invoice AI extraction API.

The only AI inference endpoint used for structured invoice extraction is:

    POST https://zydus-aimodel.mediola.in/v1/invoice/extract

Request (OpenAPI): multipart/form-data field ``file`` (Invoice PDF).
Response: ``{success, request_id, result, fallback_used, ocr}`` where ``result``
already matches the application's ``invoice_summary`` + ``line_items`` schema.
"""

from __future__ import annotations

import contextvars
import hashlib
import logging
import os
import time
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlparse

import requests
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

DEFAULT_INVOICE_AI_API_URL = "https://zydus-aimodel.mediola.in/v1/invoice/extract"
DEFAULT_TIMEOUT_SECONDS = 120
DEFAULT_MAX_RETRIES = 2
CONNECT_TIMEOUT_SECONDS = 10

_pdf_bytes_ctx: contextvars.ContextVar[Optional[bytes]] = contextvars.ContextVar(
    "invoice_ai_pdf_bytes", default=None
)
_result_cache_ctx: contextvars.ContextVar[Optional[Dict[str, dict]]] = contextvars.ContextVar(
    "invoice_ai_result_cache", default=None
)


class InternalInvoiceAIError(Exception):
    """Controlled failure talking to the internal invoice AI API."""

    def __init__(
        self,
        category: str,
        message: str,
        status_code: Optional[int] = None,
        request_id: Optional[str] = None,
    ):
        self.category = category
        self.status_code = status_code
        self.request_id = request_id
        super().__init__(message)


def invoice_ai_api_url() -> str:
    raw = (os.getenv("INVOICE_AI_API_URL") or DEFAULT_INVOICE_AI_API_URL).strip()
    parsed = urlparse(raw)
    if parsed.scheme != "https" or not parsed.netloc:
        raise InternalInvoiceAIError(
            "invalid_config",
            "INVOICE_AI_API_URL must be an https URL",
        )
    return raw.rstrip("/")


def invoice_ai_timeout() -> int:
    try:
        value = int(os.getenv("INVOICE_AI_TIMEOUT", str(DEFAULT_TIMEOUT_SECONDS)))
    except (TypeError, ValueError):
        value = DEFAULT_TIMEOUT_SECONDS
    return max(5, value)


def invoice_ai_max_retries() -> int:
    try:
        value = int(os.getenv("INVOICE_AI_MAX_RETRIES", str(DEFAULT_MAX_RETRIES)))
    except (TypeError, ValueError):
        value = DEFAULT_MAX_RETRIES
    return max(0, min(value, 5))


def invoice_ai_api_token() -> str:
    return (os.getenv("INVOICE_AI_API_TOKEN") or "").strip()


def set_invoice_ai_pdf_bytes(pdf_bytes: Optional[bytes]) -> contextvars.Token:
    return _pdf_bytes_ctx.set(pdf_bytes)


def get_invoice_ai_pdf_bytes() -> Optional[bytes]:
    return _pdf_bytes_ctx.get()


def reset_invoice_ai_pdf_bytes(token: Optional[contextvars.Token]) -> None:
    if token is None:
        return
    try:
        _pdf_bytes_ctx.reset(token)
    except Exception:
        _pdf_bytes_ctx.set(None)


def _cache() -> Dict[str, dict]:
    cache = _result_cache_ctx.get()
    if cache is None:
        cache = {}
        _result_cache_ctx.set(cache)
    return cache


def coerce_to_pdf_bytes(raw: bytes, filename: str = "invoice.bin") -> bytes:
    """Accept a PDF as-is, or wrap a raster image as a one-page PDF."""
    if not raw:
        raise InternalInvoiceAIError("invalid_input", "Empty document for internal invoice AI")
    if raw.startswith(b"%PDF"):
        return raw

    import fitz

    last_err: Optional[Exception] = None
    lower_name = (filename or "").lower()
    kinds = []
    if lower_name.endswith(".jpg") or lower_name.endswith(".jpeg"):
        kinds.append("jpeg")
    if lower_name.endswith(".png"):
        kinds.append("png")
    kinds.extend(["png", "jpeg", "jpg", "webp", "tiff", "gif"])
    seen = set()
    ordered = []
    for kind in kinds:
        if kind not in seen:
            seen.add(kind)
            ordered.append(kind)

    for kind in ordered:
        try:
            src = fitz.open(stream=raw, filetype=kind)
        except Exception as exc:
            last_err = exc
            continue
        try:
            return src.convert_to_pdf()
        except Exception as exc:
            last_err = exc
        finally:
            src.close()

    raise InternalInvoiceAIError(
        "invalid_input",
        f"Could not convert input to PDF ({type(last_err).__name__ if last_err else 'unknown'})",
    )


def build_text_pdf_bytes(title: str, body: str) -> bytes:
    """Wrap OCR/Excel text as a minimal PDF for the internal extract API."""
    import fitz

    out = fitz.open()
    try:
        remaining = f"{title}\n\n{(body or '')}"
        chunk_size = 11000
        if not remaining.strip():
            remaining = title or "invoice"
        while remaining:
            page = out.new_page(width=595, height=842)
            chunk, remaining = remaining[:chunk_size], remaining[chunk_size:]
            rect = fitz.Rect(36, 36, 559, 806)
            try:
                page.insert_textbox(rect, chunk, fontsize=8, fontname="helv")
            except Exception:
                page.insert_text((36, 50), chunk[:4000], fontsize=8, fontname="helv")
            if len(out) >= 20:
                break
        return out.tobytes(garbage=4, deflate=True)
    finally:
        out.close()


def _pdf_cache_key(pdf_bytes: bytes) -> str:
    return hashlib.sha256(pdf_bytes).hexdigest()


def _normalize_extract_result(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Map internal API JSON onto the structure enforce_schema already accepts."""
    if not isinstance(payload, dict):
        raise InternalInvoiceAIError("invalid_response", "Internal AI response is not an object")

    result = payload.get("result")
    if result is None and ("invoice_summary" in payload or "line_items" in payload or "data" in payload):
        result = payload.get("data") if isinstance(payload.get("data"), dict) else payload

    if not isinstance(result, dict):
        raise InternalInvoiceAIError(
            "invalid_response",
            "Internal AI response is missing a structured result object",
        )

    if "data" in result and isinstance(result.get("data"), dict):
        data_block = result["data"]
        normalized = dict(result)
    else:
        data_block = result
        normalized = {"data": data_block}

    summary = data_block.get("invoice_summary")
    if isinstance(summary, dict):
        invoice_no = str(summary.get("invoice_no") or "").strip()
        if invoice_no:
            normalized["invoice_no"] = invoice_no

    return normalized


def _should_retry(status_code: Optional[int], category: Optional[str]) -> bool:
    if category in {"timeout", "unavailable", "http_5xx"}:
        return True
    if status_code in (408, 429) or (status_code is not None and status_code >= 500):
        return True
    return False


def _post_extract(pdf_bytes: bytes, filename: str) -> Tuple[Dict[str, Any], str, int, float]:
    url = invoice_ai_api_url()
    timeout = invoice_ai_timeout()
    max_retries = invoice_ai_max_retries()
    headers = {}
    token = invoice_ai_api_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"

    last_error: Optional[InternalInvoiceAIError] = None
    attempts = max_retries + 1
    for attempt in range(1, attempts + 1):
        started = time.monotonic()
        request_id = ""
        status_code = 0
        try:
            response = requests.post(
                url,
                files={"file": (filename or "invoice.pdf", pdf_bytes, "application/pdf")},
                headers=headers,
                timeout=(CONNECT_TIMEOUT_SECONDS, timeout),
            )
            status_code = int(response.status_code)
            duration = time.monotonic() - started
            try:
                payload = response.json()
            except ValueError as exc:
                raise InternalInvoiceAIError(
                    "invalid_response",
                    "Internal AI returned non-JSON body",
                    status_code=status_code,
                ) from exc

            if isinstance(payload, dict):
                request_id = str(payload.get("request_id") or "")

            logger.info(
                "internal_invoice_ai status=%s duration_s=%.2f attempt=%s request_id=%s",
                status_code,
                duration,
                attempt,
                request_id or "-",
            )

            if status_code >= 500:
                raise InternalInvoiceAIError(
                    "http_5xx",
                    f"Internal AI HTTP {status_code}",
                    status_code=status_code,
                    request_id=request_id or None,
                )
            if status_code >= 400:
                raise InternalInvoiceAIError(
                    "http_4xx",
                    f"Internal AI HTTP {status_code}",
                    status_code=status_code,
                    request_id=request_id or None,
                )

            if isinstance(payload, dict) and payload.get("success") is False:
                raise InternalInvoiceAIError(
                    "invalid_response",
                    "Internal AI reported success=false",
                    status_code=status_code,
                    request_id=request_id or None,
                )

            return payload, request_id, status_code, duration

        except InternalInvoiceAIError as exc:
            last_error = exc
            if attempt < attempts and _should_retry(exc.status_code, exc.category):
                sleep_s = min(2 ** (attempt - 1), 8)
                logger.warning(
                    "internal_invoice_ai retryable category=%s status=%s attempt=%s sleep_s=%s request_id=%s",
                    exc.category,
                    exc.status_code,
                    attempt,
                    sleep_s,
                    exc.request_id or "-",
                )
                time.sleep(sleep_s)
                continue
            raise
        except requests.Timeout as exc:
            duration = time.monotonic() - started
            last_error = InternalInvoiceAIError(
                "timeout",
                f"Internal AI timed out after {timeout}s",
            )
            logger.warning(
                "internal_invoice_ai timeout duration_s=%.2f attempt=%s",
                duration,
                attempt,
            )
            if attempt < attempts and _should_retry(None, "timeout"):
                time.sleep(min(2 ** (attempt - 1), 8))
                continue
            raise last_error from exc
        except requests.RequestException as exc:
            duration = time.monotonic() - started
            last_error = InternalInvoiceAIError(
                "unavailable",
                "Internal AI unavailable",
            )
            logger.warning(
                "internal_invoice_ai unavailable duration_s=%.2f attempt=%s error_type=%s",
                duration,
                attempt,
                type(exc).__name__,
            )
            if attempt < attempts and _should_retry(None, "unavailable"):
                time.sleep(min(2 ** (attempt - 1), 8))
                continue
            raise last_error from exc

    raise last_error or InternalInvoiceAIError("unavailable", "Internal AI request failed")


def extract_structured_invoice(
    pdf_bytes: Optional[bytes] = None,
    *,
    ocr_text: Optional[str] = None,
    image_bytes: Optional[bytes] = None,
    filename: str = "invoice.pdf",
    prefer_ocr_text_pdf: bool = False,
) -> Dict[str, Any]:
    """POST a PDF to the internal API and return enforce_schema-compatible JSON.

    Resolution order:
      1. explicit pdf_bytes
      2. OCR text wrapped as PDF, when prefer_ocr_text_pdf=True
      3. request-scoped invoice PDF (ContextVar)
      4. image_bytes coerced to PDF
      5. OCR text wrapped as PDF
    """
    resolved: Optional[bytes] = None
    used_name = filename or "invoice.pdf"

    if pdf_bytes:
        resolved = coerce_to_pdf_bytes(pdf_bytes, used_name)
    elif prefer_ocr_text_pdf and (ocr_text or "").strip():
        resolved = build_text_pdf_bytes("Invoice OCR", ocr_text or "")
        used_name = "invoice-ocr.pdf"
    elif get_invoice_ai_pdf_bytes():
        resolved = get_invoice_ai_pdf_bytes()
        used_name = "invoice.pdf"
    elif image_bytes:
        resolved = coerce_to_pdf_bytes(image_bytes, used_name)
        used_name = "invoice.pdf"
    elif (ocr_text or "").strip():
        resolved = build_text_pdf_bytes("Invoice OCR", ocr_text or "")
        used_name = "invoice-ocr.pdf"

    if not resolved:
        raise InternalInvoiceAIError(
            "invalid_input",
            "No PDF, image, or OCR text available for internal invoice AI",
        )

    cache_key = _pdf_cache_key(resolved)
    cache = _cache()
    cached = cache.get(cache_key)
    if cached is not None:
        logger.info("internal_invoice_ai cache_hit=1")
        return cached

    payload, _request_id, _status, _duration = _post_extract(resolved, used_name)
    normalized = _normalize_extract_result(payload)
    cache[cache_key] = normalized
    return normalized


def invoice_summary_from_full_data(full_data: Optional[dict]) -> Dict[str, Any]:
    if not isinstance(full_data, dict):
        return {}
    data = full_data.get("data") if isinstance(full_data.get("data"), dict) else full_data
    summary = data.get("invoice_summary") if isinstance(data, dict) else None
    if isinstance(summary, dict):
        return summary
    if isinstance(data, dict):
        return data
    return {}


def line_items_from_full_data(full_data: Optional[dict]) -> list:
    if not isinstance(full_data, dict):
        return []
    data = full_data.get("data") if isinstance(full_data.get("data"), dict) else full_data
    if not isinstance(data, dict):
        return []
    line_items = data.get("line_items", data.get("items", []))
    if isinstance(line_items, dict):
        items = line_items.get("items", [])
        return items if isinstance(items, list) else []
    if isinstance(line_items, list):
        return line_items
    return []
