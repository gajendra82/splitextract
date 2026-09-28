"""One paid Gemini Vision pass when the existing extract fails the quality gate.

Does not replace format parsers. Does not calculate missing source values.
"""

from __future__ import annotations

import base64
import io
import logging
import os
from typing import Any, Dict, List, Optional

from services.extraction_quality import evaluate_extraction_quality

logger = logging.getLogger(__name__)

_FALLBACK_PROMPT = """
You are extracting a Secondary Sales / Stock Statement.
The attachment is the original document, not an OCR transcript.
Read it visually. Extract only values printed in the document.
Do not invent missing values. Do not calculate a missing quantity or amount
from other columns. If closing value is not printed, return null.
If sales value is not printed, return null.
Preserve product names, packing, and product codes as printed.
Do not expand a short printed name into a catalogue name.
Do not use the filename as the statement period.
A date printed beside the stockist name is a run date unless the document
also prints a From and To period. Use the From and To period when it is printed.
Read every supplied page. Do not stop after the first page.
Do not treat a total, subtotal, or page footer as a product.
Return JSON only:
{
  "stockist_name": string|null,
  "stockist_address": string|null,
  "company_name": string|null,
  "period_from": "YYYY-MM-DD"|null,
  "period_to": "YYYY-MM-DD"|null,
  "report_title": string|null,
  "line_items": [
    {
      "product_code": string|null,
      "product_name": string,
      "packing": string|null,
      "opening_qty": number|null,
      "receipts_qty": number|null,
      "sales_qty": number|null,
      "sales_value": number|null,
      "closing_qty": number|null,
      "closing_value": number|null
    }
  ],
  "totals": {
    "sales_value": number|null,
    "closing_value": number|null
  }
}
"""


def _enabled() -> bool:
    return os.getenv("ENABLE_GEMINI_EXTRACTION_FALLBACK", "true").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _max_pages() -> int:
    try:
        return max(1, int(os.getenv("GEMINI_EXTRACTION_FALLBACK_MAX_PAGES", "4")))
    except ValueError:
        return 4


def _stamp(result: Dict[str, Any], quality: Dict[str, Any], status: str) -> None:
    extra = result.setdefault("totals", {}).setdefault("extra", {})
    extra["extraction_quality"] = {
        "quality": quality.get("quality"),
        "score": quality.get("score"),
        "reasons": list(quality.get("reasons") or []),
    }
    extra["gemini_fallback"] = status


def _log_gate(quality: Dict[str, Any], used_gemini: bool) -> None:
    logger.info(
        "SECONDARY_SALES_EXTRACTION parser=%s quality_score=%s fallback=%s reason=%s gemini=%s",
        quality.get("parser") or "generic",
        quality.get("score"),
        str(bool(quality.get("should_fallback"))).lower(),
        ",".join(quality.get("reasons") or []) or "none",
        str(used_gemini).lower(),
    )


def _source_metadata(file_bytes: bytes, ext: str) -> Dict[str, Any]:
    pages = 0
    if ext == ".pdf":
        try:
            import fitz

            doc = fitz.open(stream=file_bytes, filetype="pdf")
            try:
                pages = doc.page_count
            finally:
                doc.close()
        except Exception:
            pages = 0
    return {"page_count": pages, "size": len(file_bytes or b"")}


def _image_part(image_bytes: bytes, mime_type: str = "image/png") -> Dict[str, Any]:
    return {
        "inline_data": {
            "mime_type": mime_type,
            "data": base64.b64encode(image_bytes).decode("ascii"),
        }
    }


def _text_image(text: str) -> bytes:
    from PIL import Image, ImageDraw

    lines = [line[:160] for line in (text or "").splitlines() if line.strip()][:120]
    if not lines:
        lines = ["(empty document)"]
    width = 1400
    height = 24 * len(lines) + 20
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    for index, line in enumerate(lines):
        draw.text((8, 8 + index * 24), line, fill="black")
    buf = io.BytesIO()
    image.save(buf, format="JPEG", quality=85)
    return buf.getvalue()


def _pdf_images(file_bytes: bytes) -> List[bytes]:
    import fitz

    images: List[bytes] = []
    doc = fitz.open(stream=file_bytes, filetype="pdf")
    try:
        # Same page scale as the /split-and-extract vision render.
        for page in doc[: _max_pages()]:
            pix = page.get_pixmap(matrix=fitz.Matrix(2.5, 2.5), alpha=False)
            images.append(pix.tobytes("png"))
    finally:
        doc.close()
    return images


def _sheet_image(file_bytes: bytes, ext: str) -> Optional[bytes]:
    rows: List[List[str]] = []
    try:
        if ext in {".xlsx", ".xlsm"}:
            from openpyxl import load_workbook

            wb = load_workbook(io.BytesIO(file_bytes), data_only=True, read_only=True)
            try:
                ws = wb.worksheets[0]
                for index, row in enumerate(ws.iter_rows(max_row=60, max_col=10, values_only=True)):
                    if index >= 60:
                        break
                    rows.append(["" if cell is None else str(cell)[:40] for cell in row])
            finally:
                wb.close()
        else:
            import xlrd

            book = xlrd.open_workbook(file_contents=file_bytes)
            sheet = book.sheet_by_index(0)
            for r in range(min(sheet.nrows, 60)):
                rows.append(
                    [str(sheet.cell_value(r, c))[:40] for c in range(min(sheet.ncols, 10))]
                )
    except Exception as exc:
        logger.info("Gemini fallback could not render spreadsheet: %s", exc)
        return None
    lines = [" | ".join(cell for cell in row) for row in rows if any(cell.strip() for cell in row)]
    if not lines:
        return None
    return _text_image("\n".join(lines))


def _word_image(file_bytes: bytes) -> Optional[bytes]:
    try:
        from docx import Document

        doc = Document(io.BytesIO(file_bytes))
    except Exception as exc:
        logger.info("Gemini fallback could not read word file: %s", exc)
        return None
    lines = [p.text for p in doc.paragraphs if p.text and p.text.strip()]
    for table in doc.tables[:8]:
        for row in table.rows[:40]:
            lines.append(" | ".join(cell.text.strip() for cell in row.cells))
    return _text_image("\n".join(lines))


def _document_parts(file_bytes: bytes, ext: str) -> List[Dict[str, Any]]:
    ext = (ext or "").lower()
    if ext in {".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff", ".webp"}:
        mime = {
            ".png": "image/png",
            ".webp": "image/webp",
            ".bmp": "image/bmp",
            ".tif": "image/tiff",
            ".tiff": "image/tiff",
        }.get(ext, "image/jpeg")
        return [
            {
                "inline_data": {
                    "mime_type": mime,
                    "data": base64.b64encode(file_bytes).decode("ascii"),
                }
            }
        ]
    if ext == ".pdf":
        return [_image_part(image, "image/png") for image in _pdf_images(file_bytes)]
    if ext in {".xls", ".xlsx", ".xlsm"}:
        image = _sheet_image(file_bytes, ext)
        return [_image_part(image, "image/jpeg")] if image else []
    if ext in {".doc", ".docx"}:
        image = _word_image(file_bytes)
        return [_image_part(image, "image/jpeg")] if image else []
    if ext in {".txt", ".htm", ".html"}:
        text = file_bytes.decode("utf-8", errors="replace")[:80000]
        return [{"text": text}]
    return []


def _call_gemini(
    file_bytes: bytes,
    ext: str,
    document_parts: Optional[List[Dict[str, Any]]] = None,
) -> Optional[Dict[str, Any]]:
    """One vision read through the same quota client as /split-and-extract."""
    from services.extraction_logger import log_extraction_event
    from services.sales_statement_extractor import (
        _extract_json_object,
        _gemini_response_text,
    )

    # Imported lazily so this module does not load the invoice pipeline at import time.
    from app import call_gemini_with_quota, get_current_model_config

    parts = [{"text": _FALLBACK_PROMPT}]
    parts.extend(document_parts if document_parts is not None else _document_parts(file_bytes, ext))
    if len(parts) < 2:
        return None
    model_config = get_current_model_config()
    payload = {
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {
            "temperature": 0.0,
            "maxOutputTokens": model_config.get("max_output_tokens") or 8192,
        },
    }
    response = call_gemini_with_quota(
        model=model_config["name"],
        payload=payload,
        timeout=int(model_config.get("timeout") or 60),
        request_type="vision",
    )
    if not response:
        return None
    data = response.json() if hasattr(response, "json") else {}
    usage = (data or {}).get("usageMetadata") or {}
    log_extraction_event(
        document_id="secondary-sales",
        document_type=ext or "unknown",
        selected_strategy="gemini_extraction_fallback",
        vision_model_used=str(model_config.get("name") or ""),
        input_tokens=int(usage.get("promptTokenCount") or 0),
        output_tokens=int(usage.get("candidatesTokenCount") or 0),
    )
    return _extract_json_object(_gemini_response_text(response))


def _finish_gemini_result(
    parsed: Dict[str, Any], filename: str, ext: str
) -> Dict[str, Any]:
    from services.sales_statement_extractor import (
        _apply_parsed_sales_json,
        _apply_stock_identity_validation,
        _ensure_stock_qty_value_fields,
        _product_wise_drop_banner_items,
        _sanitize_statement_financials,
        empty_result,
    )

    result = empty_result(filename, (ext or "").lstrip("."))
    result = _apply_parsed_sales_json(result, parsed)
    result.setdefault("totals", {}).setdefault("extra", {})
    result["totals"]["extra"]["extraction_method"] = "gemini_extraction_fallback"
    result = _sanitize_statement_financials(result)
    result = _apply_stock_identity_validation(result)
    result = _ensure_stock_qty_value_fields(result)
    return _product_wise_drop_banner_items(result)


def _controlled_failure(
    filename: str, ext: str, quality: Dict[str, Any], reason: str
) -> Dict[str, Any]:
    from services.sales_statement_extractor import empty_result

    failed = empty_result(filename, (ext or "").lstrip("."))
    failed.setdefault("totals", {}).setdefault("extra", {})
    failed["totals"]["extra"]["extraction_failed"] = True
    failed["totals"]["extra"]["extraction_method"] = "gemini_extraction_fallback"
    _stamp(failed, quality, reason)
    logger.info(
        "SECONDARY_SALES_GEMINI_FALLBACK status=failed reason=%s",
        reason,
    )
    return failed


def maybe_apply_gemini_fallback(
    result: Dict[str, Any],
    file_bytes: bytes,
    filename: str,
    ext: str,
) -> Dict[str, Any]:
    """Keep a good existing extract. One Gemini read only when quality fails."""
    if not _enabled():
        return result
    quality = evaluate_extraction_quality(result, _source_metadata(file_bytes, ext))
    _log_gate(quality, used_gemini=bool(quality.get("should_fallback")))
    if not quality.get("should_fallback"):
        _stamp(result, quality, "not_called")
        return result
    document_parts = _document_parts(file_bytes, ext)
    if not document_parts:
        _stamp(result, quality, "unavailable")
        logger.info("SECONDARY_SALES_GEMINI_FALLBACK status=failed reason=unavailable")
        return result

    # One fallback read. Client retries are not added here.
    attempts = 1
    try:
        attempts = min(1, max(1, int(os.getenv("GEMINI_EXTRACTION_FALLBACK_MAX_ATTEMPTS", "1"))))
    except ValueError:
        attempts = 1
    parsed = None
    try:
        for _ in range(attempts):
            parsed = _call_gemini(file_bytes, ext, document_parts)
            if parsed and parsed.get("line_items"):
                break
    except Exception as exc:
        logger.info("SECONDARY_SALES_GEMINI_FALLBACK status=failed reason=unavailable")
        logger.warning("Gemini extraction fallback unavailable: %s", exc)
        _stamp(result, quality, "unavailable")
        return result
    if not parsed or not parsed.get("line_items"):
        return _controlled_failure(filename, ext, quality, "validation_failed")

    gemini_result = _finish_gemini_result(parsed, filename, ext)
    gemini_quality = evaluate_extraction_quality(
        gemini_result, _source_metadata(file_bytes, ext)
    )
    if gemini_quality.get("should_fallback"):
        return _controlled_failure(filename, ext, gemini_quality, "validation_failed")
    _stamp(gemini_result, gemini_quality, "success")
    logger.info(
        "SECONDARY_SALES_GEMINI_FALLBACK status=success line_items=%s quality_score=%s",
        len(gemini_result.get("line_items") or []),
        gemini_quality.get("score"),
    )
    return gemini_result
