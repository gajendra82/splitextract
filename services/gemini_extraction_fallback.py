"""One paid Gemini Vision pass when the existing extract fails the quality gate.

Does not replace format parsers. Does not calculate missing source values.
Reuses the same quota client as /split-and-extract.
"""

from __future__ import annotations

import base64
import io
import logging
import os
import re
import time
from typing import Any, Dict, List, Optional, Tuple

from services.extraction_quality import (
    assess_source_text_quality,
    evaluate_extraction_quality,
    quality_threshold,
)

logger = logging.getLogger(__name__)

_FALLBACK_PROMPT = """
You are extracting a Secondary Sales / Stock Statement (NOT an invoice).
The attachment is the original document page image(s), not an OCR transcript.
Read it visually. Extract only values printed in the document.

Do not invent missing values.
Do not calculate a missing quantity or amount from other columns.
Do not compute closing_value as closing_qty × (sales_value / sales_qty).
If a closing value is printed, copy that printed closing value exactly.
If closing value is not printed, return null.
If sales value is not printed, return null.
Preserve product names, packing, and product codes as printed.
Do not expand a short printed name into a catalogue name.
Do not use the filename as the statement period.
A date printed beside the stockist name is a run date unless the document
also prints a From and To period. Use the From and To period when it is printed.
Read every supplied page. Do not stop after the first page.
Do not treat a total, subtotal, address, GSTIN, or page footer as a product.
Do not treat STOCK REPORT / COMPANY NAME banner lines as products.

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
      "closing_value": number|null,
      "extra": {}
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


def _max_attempts() -> int:
    try:
        return max(1, min(3, int(os.getenv("GEMINI_EXTRACTION_FALLBACK_MAX_ATTEMPTS", "1"))))
    except ValueError:
        return 1


def _stamp(result: Dict[str, Any], quality: Dict[str, Any], status: str) -> None:
    extra = result.setdefault("totals", {}).setdefault("extra", {})
    extra["extraction_quality"] = {
        "quality": quality.get("quality"),
        "score": quality.get("score"),
        "reasons": list(quality.get("reasons") or []),
        "threshold": quality_threshold(),
    }
    extra["gemini_fallback"] = status


def _log_gate(quality: Dict[str, Any], used_gemini: bool, filename: str = "") -> None:
    logger.info(
        "[SalesStatement] file=%s parser=%s OCR_quality=%s decision=%s reason=%s gemini=%s",
        filename or "-",
        quality.get("parser") or "generic",
        quality.get("score"),
        "GEMINI_VISION_FALLBACK" if quality.get("should_fallback") else "KEEP_PARSER",
        ",".join(quality.get("reasons") or []) or "none",
        str(used_gemini).lower(),
    )


def _source_metadata(file_bytes: bytes, ext: str) -> Dict[str, Any]:
    pages = 0
    embedded_chars = 0
    if ext == ".pdf":
        try:
            import fitz

            doc = fitz.open(stream=file_bytes, filetype="pdf")
            try:
                pages = doc.page_count
                for page in doc:
                    embedded_chars += len(re.sub(r"\s+", "", page.get_text("text") or ""))
            finally:
                doc.close()
        except Exception:
            pages = 0
    image_only = ext == ".pdf" and pages > 0 and embedded_chars < 40
    return {
        "page_count": pages,
        "size": len(file_bytes or b""),
        "embedded_chars": embedded_chars,
        "image_only_pdf": image_only,
        "source_has_visible_data": bool(file_bytes) and len(file_bytes) > 1500,
    }


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


def _pdf_is_image_only(file_bytes: bytes) -> bool:
    meta = _source_metadata(file_bytes, ".pdf")
    return bool(meta.get("image_only_pdf"))


def _sheet_image(file_bytes: bytes, ext: str) -> Optional[bytes]:
    rows: List[List[str]] = []
    try:
        if ext in {".xlsx", ".xlsm"}:
            from openpyxl import load_workbook

            wb = load_workbook(io.BytesIO(file_bytes), data_only=True, read_only=True)
            try:
                ws = wb.worksheets[0]
                for index, row in enumerate(
                    ws.iter_rows(max_row=60, max_col=10, values_only=True)
                ):
                    if index >= 60:
                        break
                    rows.append(
                        ["" if cell is None else str(cell)[:40] for cell in row]
                    )
            finally:
                wb.close()
        else:
            import xlrd

            book = xlrd.open_workbook(file_contents=file_bytes)
            sheet = book.sheet_by_index(0)
            for r in range(min(sheet.nrows, 60)):
                rows.append(
                    [
                        str(sheet.cell_value(r, c))[:40]
                        for c in range(min(sheet.ncols, 10))
                    ]
                )
    except Exception as exc:
        logger.info("Gemini fallback could not render spreadsheet: %s", exc)
        return None
    lines = [
        " | ".join(cell for cell in row) for row in rows if any(cell.strip() for cell in row)
    ]
    if not lines:
        return None
    return _text_image("\n".join(lines))


def _word_parts(file_bytes: bytes) -> List[Dict[str, Any]]:
    """Word text/tables as an image, plus embedded pictures when present."""
    parts: List[Dict[str, Any]] = []
    try:
        from docx import Document
        from docx.oxml.ns import qn

        doc = Document(io.BytesIO(file_bytes))
    except Exception as exc:
        logger.info("Gemini fallback could not read word file: %s", exc)
        return []

    lines = [p.text for p in doc.paragraphs if p.text and p.text.strip()]
    for table in doc.tables[:8]:
        for row in table.rows[:40]:
            lines.append(" | ".join(cell.text.strip() for cell in row.cells))
    if lines:
        parts.append(_image_part(_text_image("\n".join(lines)), "image/jpeg"))

    # Embedded images — scanned statements inside .docx
    try:
        count = 0
        for rel in doc.part.rels.values():
            if "image" not in str(getattr(rel, "reltype", "")).lower():
                continue
            blob = rel.target_part.blob
            if not blob or len(blob) < 80:
                continue
            mime = "image/png"
            name = str(getattr(rel.target_part, "partname", "") or "").lower()
            if name.endswith((".jpg", ".jpeg")):
                mime = "image/jpeg"
            elif name.endswith(".webp"):
                mime = "image/webp"
            parts.append(_image_part(blob, mime))
            count += 1
            if count >= _max_pages():
                break
    except Exception as exc:
        logger.info("Gemini fallback could not read word images: %s", exc)
    return parts


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
        return _word_parts(file_bytes)
    if ext in {".txt", ".htm", ".html"}:
        # Text-only: Vision needs pixels. Skip Vision; keep parser result.
        return []
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
    parts.extend(
        document_parts if document_parts is not None else _document_parts(file_bytes, ext)
    )
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
        "[SalesStatement] file=%s GEMINI_FALLBACK status=failed reason=%s",
        filename,
        reason,
    )
    return failed


def _result_is_weak(result: Dict[str, Any], quality: Dict[str, Any]) -> bool:
    """True when returning the parser result would look like a false success."""
    if quality.get("should_fallback"):
        reasons = set(quality.get("reasons") or [])
        if reasons & {
            "empty_line_items",
            "header_as_product_rows",
            "high_ocr_noise",
            "all_zero_suspicious",
            "missing_product_names",
        }:
            return True
    items = []
    if isinstance(result, dict):
        items = [
            i
            for i in (result.get("line_items") or [])
            if isinstance(i, dict)
        ]
        if result.get("multi_statement") and result.get("statements"):
            items = []
            for stmt in result["statements"]:
                if isinstance(stmt, dict):
                    items.extend(
                        i
                        for i in (stmt.get("line_items") or [])
                        if isinstance(i, dict)
                    )
    return not items


def try_gemini_vision_extract(
    file_bytes: bytes,
    filename: str,
    ext: str,
) -> Optional[Dict[str, Any]]:
    """Direct Vision extract used as an early escape for unreadable image PDFs.

    Returns a validated statement or None (caller continues existing parsers).
    """
    if not _enabled():
        return None
    document_parts = _document_parts(file_bytes, ext)
    if not document_parts:
        return None
    started = time.time()
    parsed = None
    try:
        for _ in range(_max_attempts()):
            parsed = _call_gemini(file_bytes, ext, document_parts)
            if parsed and parsed.get("line_items"):
                break
    except Exception as exc:
        logger.warning(
            "[SalesStatement] file=%s early Vision unavailable: %s", filename, exc
        )
        return None
    if not parsed or not parsed.get("line_items"):
        return None
    gemini_result = _finish_gemini_result(parsed, filename, ext)
    gemini_quality = evaluate_extraction_quality(
        gemini_result, _source_metadata(file_bytes, ext)
    )
    # Gemini method is weak — re-score ignoring method protection by checking items.
    if not gemini_result.get("line_items"):
        return None
    if gemini_quality.get("reasons") and set(gemini_quality["reasons"]) & {
        "empty_line_items",
        "high_ocr_noise",
        "header_as_product_rows",
        "missing_product_names",
    }:
        return None
    _stamp(gemini_result, gemini_quality, "early_success")
    logger.info(
        "[SalesStatement] file=%s decision=GEMINI_VISION_FALLBACK reason=EARLY_IMAGE_ONLY "
        "pages=%s gemini_duration=%.1fs line_items=%s validation=PASS",
        filename,
        _source_metadata(file_bytes, ext).get("page_count"),
        time.time() - started,
        len(gemini_result.get("line_items") or []),
    )
    return gemini_result


def maybe_apply_gemini_fallback(
    result: Dict[str, Any],
    file_bytes: bytes,
    filename: str,
    ext: str,
) -> Dict[str, Any]:
    """Keep a good existing extract. One Gemini read only when quality fails."""
    if not _enabled():
        return result
    meta = _source_metadata(file_bytes, ext)
    quality = evaluate_extraction_quality(result, meta)
    _log_gate(quality, used_gemini=bool(quality.get("should_fallback")), filename=filename)
    if not quality.get("should_fallback"):
        _stamp(result, quality, "not_called")
        return result

    # Marg PRODUCT DESCRIPTION / OPENING / RECEIVE / ISSUE photos: Gemini shifts
    # neighboring rows. Keep the format-specific OCR/column reader.
    extra = ((result.get("totals") or {}).get("extra") or {})
    if (
        str(extra.get("extraction_method") or "") == "main_stock_sales_statement"
        or str(extra.get("layout") or "") == "opening_receive_issue_closing"
    ):
        logger.info(
            "[SalesStatement] file=%s GEMINI_FALLBACK skipped reason=main_stock_column_reader",
            filename,
        )
        _stamp(result, quality, "not_called")
        return result

    # Code/Item Opening/Purchase/Sales phone photo — keep dedicated Vision reader.
    if str(extra.get("extraction_method") or "") in {
        "code_item_stock_statement_photo",
        "code_item_stock_statement",
    } or str(extra.get("layout") or "") == "code_item_stock_statement":
        logger.info(
            "[SalesStatement] file=%s GEMINI_FALLBACK skipped reason=code_item_stock_statement",
            filename,
        )
        _stamp(result, quality, "not_called")
        return result

    # J R SHAH Op/Pur/Pur Val/Sale Val/Bal Val photo — keep dedicated Vision reader.
    if str(extra.get("extraction_method") or "") in {
        "op_pur_sp_sale_bal_val_photo",
        "op_pur_sp_sale_bal_val",
        "pack_op_pur_bal_stock_sale_vision",
        "pack_op_pur_bal_stock_sale",
    } or str(extra.get("layout") or "") in {
        "op_pur_sp_sale_bal_val",
        "pack_op_pur_bal_stock_sale",
    }:
        logger.info(
            "[SalesStatement] file=%s GEMINI_FALLBACK skipped reason=op_pur_sp_sale_bal_val",
            filename,
        )
        _stamp(result, quality, "not_called")
        return result

    # Native MediVision Op/Purc/NM60D parser: keep printed Cl qty (do not Vision-replace).
    if (
        str(extra.get("extraction_method") or "") == "medivision_op_purc_nm60d"
        or str(extra.get("layout") or "") == "medivision_op_purc_nm60d"
    ):
        logger.info(
            "[SalesStatement] file=%s GEMINI_FALLBACK skipped reason=medivision_op_purc_nm60d",
            filename,
        )
        _stamp(result, quality, "not_called")
        return result

    # Product wise stock statement photo: Closing vs Liqudation days already fixed.
    if (
        str(extra.get("extraction_method") or "")
        == "product_wise_stock_statement_image"
        or str(extra.get("layout") or "") == "product_wise_stock_statement_photo"
    ):
        logger.info(
            "[SalesStatement] file=%s GEMINI_FALLBACK skipped reason=product_wise_stock_statement_image",
            filename,
        )
        _stamp(result, quality, "not_called")
        return result

    # TXT/HTML have no visual page — do not invent Vision input.
    if (ext or "").lower() in {".txt", ".htm", ".html"}:
        _stamp(result, quality, "unavailable_text_only")
        logger.info(
            "[SalesStatement] file=%s GEMINI_FALLBACK skipped reason=text_only",
            filename,
        )
        return result

    document_parts = _document_parts(file_bytes, ext)
    if not document_parts:
        _stamp(result, quality, "unavailable")
        logger.info(
            "[SalesStatement] file=%s GEMINI_FALLBACK status=failed reason=unavailable",
            filename,
        )
        if _result_is_weak(result, quality):
            return _controlled_failure(filename, ext, quality, "unavailable")
        return result

    started = time.time()
    parsed = None
    try:
        for _ in range(_max_attempts()):
            parsed = _call_gemini(file_bytes, ext, document_parts)
            if parsed and parsed.get("line_items"):
                break
    except Exception as exc:
        logger.info(
            "[SalesStatement] file=%s GEMINI_FALLBACK status=failed reason=unavailable",
            filename,
        )
        logger.warning("Gemini extraction fallback unavailable: %s", exc)
        _stamp(result, quality, "unavailable")
        if _result_is_weak(result, quality):
            return _controlled_failure(filename, ext, quality, "unavailable")
        return result

    if not parsed or not parsed.get("line_items"):
        if _result_is_weak(result, quality):
            return _controlled_failure(filename, ext, quality, "validation_failed")
        _stamp(result, quality, "validation_failed")
        return result

    gemini_result = _finish_gemini_result(parsed, filename, ext)
    gemini_quality = evaluate_extraction_quality(
        gemini_result, meta
    )
    # Accept Gemini when it produced real product rows even if weak-method scoring
    # flags low_ocr_quality_score on the Gemini method name itself.
    gemini_items = gemini_result.get("line_items") or []
    # Row-shift signature: many closings with blank openings — never accept.
    shifted = 0
    for item in gemini_items:
        if not isinstance(item, dict):
            continue
        if (
            float(item.get("closing_qty") or 0) > 0
            and float(item.get("opening_qty") or 0) == 0
            and float(item.get("sales_qty") or 0) == 0
        ):
            shifted += 1
    gemini_ok = bool(gemini_items) and not (
        set(gemini_quality.get("reasons") or [])
        & {
            "empty_line_items",
            "high_ocr_noise",
            "header_as_product_rows",
            "missing_product_names",
            "stock_identity_failure",
        }
    )
    if shifted >= max(5, len(gemini_items) // 4):
        gemini_ok = False
    if not gemini_ok:
        if _result_is_weak(result, quality):
            return _controlled_failure(filename, ext, gemini_quality, "validation_failed")
        _stamp(result, quality, "validation_failed")
        return result

    _stamp(gemini_result, gemini_quality, "success")
    logger.info(
        "[SalesStatement] file=%s decision=GEMINI_VISION_FALLBACK reason=%s "
        "pages=%s gemini_duration=%.1fs line_items=%s validation=PASS model=paid",
        filename,
        ",".join(quality.get("reasons") or []) or "LOW_OCR_QUALITY",
        meta.get("page_count"),
        time.time() - started,
        len(gemini_items),
    )
    return gemini_result
