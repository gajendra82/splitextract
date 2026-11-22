"""Direct original-image Gemini fallback for stock-statement photos.

Structured OCR/geometry still runs first. This module only decides whether
that result is reliable. When it is not, the original uploaded bytes are sent
to Gemini Vision. OCR text is not substituted for the image, and Gemini
numbers are not rewritten to force a stock-identity equation.

Does not change the OCR retry limit or the Gemini rate limiter.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import io
import logging
import os
import re
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

STOCK_IMAGE_VISION_PROMPT = """
You are looking at the original stock-statement image.

Read the table directly from the image.

Do not rely on OCR text supplied by the application.

First identify the visible column headers and their left-to-right positions.

Then map each data cell to the correct column based on its physical position.

Do not infer a column based only on the numeric value.

Do not move numbers between adjacent columns.

Do not treat numbers in product names or packing as quantities.

Do not treat value columns as quantity columns.

Return the values exactly as printed in the corresponding table cells.

Do not force every statement into opening / receipts / sales / closing.
Read the headers that are actually printed.

Example mapping when those headers are visible:
- OPSTK / Opening / Op. -> opening_qty
- PURCH / Purchase / Pur -> purchase_qty / receipts_qty
- SALE / Sale -> sales_qty
- SALE VAL -> sales_value
- Total -> extra.total_stock (NOT closing_qty)
- Stock in Tran / Transit / Tran Qty / Tran Value -> extra.transit / ignore
  (NEVER total_stock, NEVER closing_qty, NEVER sales_qty)
- SaleRet / Sale Ret / S.RETURN -> extra.sale_return (NOT sales_qty)
- P.RETURN -> purchase_return_qty (NOT sales_qty)
- Exp/Dmg / Expired Stock -> expiry_damage_qty / extra.exp_damage
- ClosStock / Closing Stock / Closing / Cl Stk / bare Stk (after Sale) -> closing_qty
- Clos.Amt / Cls Amt / Closing Amount / Value beside Cl Stk -> closing_value
- IN/OTSTOCK or N/O/STOCK or bare STOCK (stock-on-hand after SALE VAL) -> closing_qty
- STK VAL -> closing_value
- Rate / PTR / MRP -> extra.rate (NEVER closing_value, NEVER sales_value)
- EXP3M / Sh.E / Shelf Exp -> ignore / extra.shelf_expiry (NEVER closing)
- Footer labels "Opening stock value" / "Sales value" / "Closing stock value"
  / Opg.Stk Val / Cl.Stk Val / Pur Val / Sales Val
  -> totals.extra.opening_value / totals.sales_value / totals.closing_value
    / totals.extra.purchase_value
  (not line-item fields; do not invent per-row sales_value when no SL.VALUE column)
- PRODUCT|PACK|OPENING|PURCHASE|FREE|P.RETURN|FREE|SALE|FREE|S.RETURN|FREE|OTHERS|CLOSING:
  PACK (60, 30ML, 60 TAB) is packing, never opening_qty. Keep one cell per FREE
  even when it is a dash so SALE/CLOSING do not shift. Period "01-Apr-26 To
  30-Apr-26" is period_from/period_to (DD-Mon-YY).

Item Name | Op. | Pur | SP | Aug | Sep | Sale | SFree | Ins Sale | Ins Free | Stk | Rate | Sh.E:
- Op. -> opening_qty, Pur -> receipts_qty, Sale -> sales_qty, Stk -> closing_qty
- Rate -> extra.rate ONLY (never closing_value; rows often have Rate with blank Stk)
- Aug / Sep (or any month pair) -> extra.prior_month_a_qty / prior_month_b_qty
  (also aug_qty / sep_qty). NEVER opening_qty / receipts_qty / sales_qty
- SP / SFree / Ins Free -> extra.sp_qty / free_out_qty / ins_free_qty (not sales)
- Ins Sale -> extra.institution_sale_qty (NOT sales_qty)
- Sh.E -> ignore. closing_value stays null when no per-row Cl Value column.
- stockist_name = letterhead agency at top; stockist_address = street/city/PIN
  under it; company_name = division band (e.g. 068 HIMALAYA ZEAL DIV), never
  the letterhead. Skip "068 HIMALAYA ZEAL DIV" / HIMALAYA-ZEAL as products.
- Footer Opg.Stk Val / Cl.Stk Val / Pur Val / Sales Val / SalRet Val /
  NetSal (GVal) / Instution Sale / Tran.Stk.Out go to totals only.
- When several TOTAL / GRAND TOTAL bands print, use ONLY the last GRAND TOTAL
  (else the last TOTAL).

These are not quantity columns unless the printed header clearly says so:
PACK, PACKING, SALE VAL, STK VAL, JUL, JUN, AUG, SEP, Rate, Sh.E, STK120,
Total, Clos.Amt.

Skip division banners such as HIMALAYA-ZEAL, "068 HIMALAYA ZEAL DIV", and
Division Total rows.
Do not invent products from phone UI chrome ("Done", free-trial banners).
Ignore overline bars drawn above digits; read the digit values underneath.

If the image is genuinely unreadable, set image_quality.quality to "poor" or
"unreadable" and return no invented line items.

Return JSON only:
{
  "image_quality": {
    "readable": true,
    "table_visible": true,
    "headers_readable": true,
    "numeric_cells_readable": true,
    "quality": "good",
    "confidence": 0.0
  },
  "detected_columns": [
    {"header": "OPSTK", "semantic": "opening_qty"}
  ],
  "stockist_name": null,
  "stockist_address": null,
  "company_name": null,
  "period_from": null,
  "period_to": null,
  "report_title": null,
  "line_items": [
    {
      "product_code": null,
      "product_name": "",
      "packing": null,
      "opening_qty": null,
      "purchase_qty": null,
      "receipts_qty": null,
      "sales_qty": null,
      "sales_value": null,
      "closing_qty": null,
      "closing_value": null,
      "expiry_damage_qty": null,
      "extra": {
        "total_stock": null,
        "sale_return": null,
        "exp_damage": null,
        "rate": null,
        "prior_month_a_qty": null,
        "prior_month_b_qty": null,
        "institution_sale_qty": null,
        "free_out_qty": null
      },
      "cells": {}
    }
  ],
  "totals": {
    "sales_value": null,
    "closing_value": null,
    "extra": {
      "opening_value": null,
      "purchase_value": null
    }
  }
}
""".strip()

_TRUSTED_METHODS = frozenset(
    {
        "main_stock_sales_statement",
        "code_item_stock_statement_photo",
        "code_item_stock_statement",
        "pack_op_pur_bal_stock_sale_vision",
        "pack_op_pur_bal_stock_sale",
        "op_pur_sp_sale_bal_val_photo",
        "op_pur_sp_sale_bal_val",
        "medivision_op_purc_nm60d",
        "medivision_op_val_sr_nm60d",
        "medivision_op_wsale_photo",
        "product_wise_stock_statement_image",
        "medica_opstk_columns",
        "medica_stock_statement_vision",
        "branch_opening_receipt_closing_balance",
        "product_stock_report_vision",
        "psr_closstock_columns",
        "swilerp_page_split_vision",
        "swilerp_opbal_ocr_pages",
        "stock_valuation_as_on",
        "stock_valuation_stock_rate",
        "batchwise_stock_summary",
        "op_stk_rcpts_stock_sale_vision",
        "zandra_two_column_order_form",
        "sap_order_form_vision",
        # Qty-only ORIC Vision (not the weak Tesseract OCR sibling).
        "ssa_oric_qty_only_vision",
    }
)
_TRUSTED_LAYOUTS = frozenset(
    {
        "opening_receive_issue_closing",
        "op_stk_rcpts_sales_closing",
        "code_item_stock_statement",
        "pack_op_pur_bal_stock_sale",
        "op_pur_sp_sale_bal_val",
        "medivision_op_purc_nm60d",
        "medivision_op_val_sr_nm60d",
        "medivision_op_wsale",
        "product_wise_stock_statement_photo",
        "medica_stock_statement",
        "branch_opening_receipt_closing_balance",
        "zandra_two_column_order_form",
        "handwritten_order_form",
        "order_form_qty",
    }
)
_WEAK_METHODS = frozenset(
    {
        "",
        "parser",
        "unknown",
        "generic",
        "ocr",
        "image_ocr",
        "vikash",
        "tesseract_heuristic",
        "pdf_text_heuristic",
    }
)
_VISION_METHODS = frozenset(
    {"gemini_vision", "gemini_extraction_fallback", "stock_image_gemini"}
)
_QTY_FIELDS = ("opening_qty", "receipts_qty", "sales_qty", "closing_qty")
_NON_QTY_SEMANTICS = frozenset(
    {
        "packing",
        "product_name",
        "product_description",
        "sales_value",
        "sale_val",
        "stk_val",
        "stock_value",
        "closing_value",
        "rate",
        "jul",
        "jun",
        "aug",
        "sep",
        "oct",
        "nov",
        "dec",
        "jan",
        "feb",
        "mar",
        "apr",
        "may",
        "prior_month",
        "prior_month_a_qty",
        "prior_month_b_qty",
        "lms",
        "ins_sale",
        "institution_sale",
        "institution_sale_qty",
        "sh_e",
        "shelf_expiry",
        "stk120",
        "unknown",
        "ignore",
    }
)
_CLOSING_QTY_SEMANTICS = frozenset(
    {
        "closing_qty",
        "in_otstock",
        "in_ot",
        "in_out",
        "n_o_stock",
        "stock_qty",
        "stock",
        "stk",
        "curstk",
    }
)
_MONTH_SEMANTICS = frozenset(
    {
        "jan",
        "feb",
        "mar",
        "apr",
        "may",
        "jun",
        "jul",
        "aug",
        "sep",
        "oct",
        "nov",
        "dec",
        "prior_month",
        "prior_month_a_qty",
        "prior_month_b_qty",
        "lms",
    }
)


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name, str(default)).strip()
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def _enabled() -> bool:
    return os.getenv("ENABLE_GEMINI_EXTRACTION_FALLBACK", "true").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _items(result: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not isinstance(result, dict):
        return []
    return [item for item in (result.get("line_items") or []) if isinstance(item, dict)]


def _extra(result: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not isinstance(result, dict):
        return {}
    extra = (result.get("totals") or {}).get("extra") or {}
    return extra if isinstance(extra, dict) else {}


def _number(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", "")
    if not text or text in {"-", "--", "—"}:
        return None
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def _is_vision_method(method: str) -> bool:
    return method in _VISION_METHODS or "gemini" in method or method.endswith("_vision")


def _is_trusted(method: str, layout: str) -> bool:
    return method in _TRUSTED_METHODS or layout in _TRUSTED_LAYOUTS


def _numeric_coverage(items: List[Dict[str, Any]]) -> float:
    if not items:
        return 0.0
    covered = 0
    for item in items:
        if any(_number(item.get(field)) is not None for field in _QTY_FIELDS):
            covered += 1
    return covered / len(items)


def _name_contamination(item: Dict[str, Any]) -> bool:
    name = str(item.get("product_name") or "")
    return bool(re.search(r"[A-Za-z].*(?:\d+\s+){2,}\d+\s*$", name))


def _boundary_cross(item: Dict[str, Any]) -> bool:
    name = str(item.get("product_name") or "")
    return len(re.findall(r"\d+(?:\.\d+)?", name)) >= 4


def _rate(items: List[Dict[str, Any]], predicate) -> float:
    if not items:
        return 0.0
    return sum(1 for item in items if predicate(item)) / len(items)


def _phone_ui(text: str) -> bool:
    return bool(
        re.search(
            r"whatsapp|screenshot|battery|volte|\b\d{1,2}:\d{2}\s*(?:am|pm)\b",
            text or "",
            re.I,
        )
    )


def _headers_sufficient(headers: List[Any]) -> bool:
    blob = " ".join(str(header) for header in headers).lower()
    hits = 0
    for token in ("op", "pur", "sale", "clos", "stock", "issue", "receipt"):
        if token in blob:
            hits += 1
    return hits >= 2


def assess_stock_structured_quality(
    result: Optional[Dict[str, Any]],
    signals: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Decide whether structured OCR is reliable enough to keep.

    A balanced opening + purchase - sale = closing is not treated as proof.
    """
    signals = signals or {}
    extra = _extra(result)
    items = _items(result)
    method = str(extra.get("extraction_method") or "")
    layout = str(extra.get("layout") or "")
    trusted = _is_trusted(method, layout)
    vision = _is_vision_method(method)
    schema = str(signals.get("schema") or extra.get("schema") or layout or "")
    reasons: List[str] = []

    unknown_schema = str(signals.get("schema") or "").lower() == "unknown" or (
        not trusted
        and not vision
        and method.lower() in _WEAK_METHODS
    )
    if unknown_schema:
        reasons.append("schema_unknown")

    detected = signals.get("detected_headers", extra.get("detected_headers"))
    required_flag = signals.get("required_columns_detected")
    if required_flag is False or detected == []:
        reasons.append("required_headers_missing")
    elif isinstance(detected, list) and detected and not _headers_sufficient(detected):
        reasons.append("required_headers_missing")

    header_confidence = signals.get("header_confidence", extra.get("header_confidence"))
    header_min = _env_float("STOCK_HEADER_CONFIDENCE_MIN", 0.60)
    if header_confidence is not None and float(header_confidence) < header_min:
        reasons.append("header_confidence_low")

    coverage = _numeric_coverage(items)
    coverage_min = _env_float("STOCK_MIN_NUMERIC_COVERAGE", 0.50)
    if not items or (coverage < coverage_min and not trusted):
        reasons.append("numeric_coverage_low")
    # Generic vision often emits product names with every qty coerced to 0.0 —
    # treat that as low coverage so we do not KEEP_PARSER on empty movement.
    if items and len(items) >= 3 and not trusted:
        nonzero_move = any(
            abs(_number(item.get(field)) or 0.0) > 0
            for item in items
            for field in ("opening_qty", "receipts_qty", "sales_qty", "closing_qty")
        )
        if not nonzero_move:
            if "numeric_coverage_low" not in reasons:
                reasons.append("numeric_coverage_low")
            reasons.append("all_zero_qtys")

    fail_count = extra.get("stock_identity_fail_count", signals.get("identity_fail_count"))
    identity_rate = None
    veto_active = False
    try:
        from services.stock_row_classifier import (
            infer_input_type,
            veto_active_for,
            veto_decision,
        )

        # Image-vision path is always an image input for type filtering.
        veto_active = veto_active_for("image")
        if veto_active:
            veto_info = veto_decision(result or {}, "image")
            if veto_info.get("veto"):
                reasons.append("identity_failure_rate")
                reasons.append("IDENTITY_VETO")
    except Exception:
        veto_active = False
    if fail_count is not None and items:
        identity_rate = float(fail_count) / len(items)
        threshold = _env_float("STOCK_MAX_IDENTITY_FAILURE_RATE", 0.25)
        # Marg OPENING/RECEIVE reader must not stay "trusted" when most rows
        # fail identity — Op_Stk|Rcpts|Sales|Cl_Stk photos land here often.
        main_stock_layout = (
            method
            in {
                "main_stock_sales_statement",
                "main_stock_sales_statement_sheets",
            }
            or layout == "opening_receive_issue_closing"
        )
        if identity_rate >= threshold:
            # Phase 1: do not suppress identity for trusted methods when veto is on.
            if (not trusted and not vision) or veto_active or main_stock_layout:
                if "identity_failure_rate" not in reasons:
                    reasons.append("identity_failure_rate")
                if main_stock_layout:
                    trusted = False
            elif method in {"gemini_vision", "gemini_extraction_fallback"}:
                # Generic vision often shifts SaleRet / ClosStock / Exp/Dmg sheets.
                has_saleret = any(
                    isinstance(item.get("extra"), dict)
                    and item["extra"].get("sale_return") not in (None, "")
                    for item in items
                )
                if not has_saleret:
                    reasons.append("identity_failure_rate")
                    reasons.append("layout_not_understood")

    # A trusted column reader may keep a few noisy names. Explicit signals
    # above still send the original image to Gemini.
    if not trusted:
        if _rate(items, _name_contamination) >= 0.25:
            reasons.append("product_name_quantity_contamination")
        if _rate(items, _boundary_cross) >= 0.25:
            reasons.append("column_boundary_cross")

    ocr_confidence = signals.get("ocr_confidence", extra.get("ocr_confidence"))
    if ocr_confidence is not None and float(ocr_confidence) < 0.45:
        reasons.append("poor_ocr_quality")
    elif items and not trusted and not vision:
        noisy = 0
        for item in items:
            letters = re.findall(r"[A-Za-z]", str(item.get("product_name") or ""))
            if len(letters) < 3:
                noisy += 1
        if noisy / len(items) >= 0.40:
            reasons.append("poor_ocr_quality")

    readability = str(
        signals.get("image_readability") or extra.get("image_readability") or ""
    ).lower()
    if readability in {"poor", "unreadable"}:
        reasons.append("poor_image_quality")

    ocr_text = " ".join(
        [
            str(signals.get("ocr_text") or ""),
            str((result or {}).get("stockist_name") or ""),
            str((result or {}).get("report_title") or ""),
        ]
    )
    if _phone_ui(ocr_text):
        reasons.append("phone_ui_text")

    extraction_confidence = signals.get(
        "extraction_confidence", extra.get("extraction_confidence")
    )
    confidence_min = _env_float("STOCK_MIN_EXTRACTION_CONFIDENCE", 0.60)
    if extraction_confidence is not None and float(extraction_confidence) < confidence_min:
        reasons.append("low_extraction_confidence")
    elif unknown_schema:
        reasons.append("low_extraction_confidence")

    if signals.get("layout_unsafe") or extra.get("layout_unsafe"):
        reasons.append("layout_not_understood")

    # Qty-only ORIC Tesseract OCR often keeps high numeric coverage while still
    # dropping/shifting rows (valid_ratio < 0.90). Escalate to ORIC Vision.
    if method == "ssa_oric_qty_only_ocr" and not vision and items:
        try:
            from services.stock_row_classifier import (
                classify_result_safe,
                would_fallback,
            )

            summary = classify_result_safe(result or {})
            weak, weak_reason = would_fallback(summary or {})
            if weak:
                if "oric_ocr_weak" not in reasons:
                    reasons.append("oric_ocr_weak")
                if "identity_failure_rate" not in reasons and weak_reason:
                    reasons.append("identity_failure_rate")
        except Exception:
            try:
                from services.sales_statement_extractor import _ssa_oric_ocr_needs_vision

                if _ssa_oric_ocr_needs_vision(result):
                    reasons.append("oric_ocr_weak")
            except Exception:
                pass

    deduped: List[str] = []
    for reason in reasons:
        if reason not in deduped:
            deduped.append(reason)
    if extraction_confidence is None:
        extraction_confidence = 0.9 if trusted and not deduped else (0.35 if deduped else 0.75)
    return {
        "needs_direct_gemini": bool(deduped),
        "reasons": deduped,
        "schema": "unknown" if "schema_unknown" in deduped else (schema or method or "unknown"),
        "header_confidence": header_confidence,
        "numeric_coverage": round(coverage, 4),
        "identity_failure_rate": None if identity_rate is None else round(identity_rate, 4),
        "extraction_confidence": extraction_confidence,
        "trusted_structured_reader": trusted,
        "row_count": len(items),
    }


def prepare_original_image(
    file_bytes: bytes, ext: str
) -> Tuple[bytes, str, Dict[str, Any]]:
    """Return bytes for Gemini. EXIF-orient first; resize only if still too large."""
    original = hashlib.sha256(file_bytes or b"").hexdigest()
    mime = {
        ".png": "image/png",
        ".webp": "image/webp",
        ".bmp": "image/bmp",
        ".tif": "image/tiff",
        ".tiff": "image/tiff",
    }.get((ext or "").lower(), "image/jpeg")
    max_bytes = int(_env_float("STOCK_GEMINI_IMAGE_MAX_BYTES", 12000000))
    meta = {
        "original_sha256": original,
        "gemini_input_sha256": original,
        "normalized": False,
        "normalization_reason": None,
        "byte_length": len(file_bytes or b""),
        "exif_oriented": False,
    }
    if not file_bytes:
        return file_bytes, mime, meta

    from PIL import Image, ImageOps

    working = file_bytes
    try:
        with Image.open(io.BytesIO(file_bytes)) as raw:
            ori = int(raw.getexif().get(274) or 1) if hasattr(raw, "getexif") else 1
            if ori not in (0, 1):
                oriented = ImageOps.exif_transpose(raw)
                if oriented is not None:
                    if oriented.mode not in ("RGB", "L"):
                        oriented = oriented.convert("RGB")
                    buf = io.BytesIO()
                    fmt = "JPEG" if file_bytes[:2] == b"\xff\xd8" else "PNG"
                    save_kw: Dict[str, Any] = {}
                    if fmt == "JPEG":
                        save_kw["quality"] = 92
                        if oriented.mode != "RGB":
                            oriented = oriented.convert("RGB")
                    oriented.save(buf, format=fmt, **save_kw)
                    out = buf.getvalue()
                    if out:
                        working = out
                        meta["exif_oriented"] = True
                        meta["normalized"] = True
                        meta["normalization_reason"] = "exif_orient"
                        meta["gemini_input_sha256"] = hashlib.sha256(working).hexdigest()
                        meta["byte_length"] = len(working)
                        mime = "image/jpeg" if fmt == "JPEG" else "image/png"
                        logger.info(
                            "STOCK_IMAGE_GEMINI_EXIF orientation=%s original_sha256=%s "
                            "gemini_input_sha256=%s",
                            ori,
                            meta["original_sha256"],
                            meta["gemini_input_sha256"],
                        )
    except Exception as exc:
        logger.info(
            "STOCK_IMAGE_GEMINI_EXIF skipped err=%s", type(exc).__name__
        )
        working = file_bytes

    if len(working) <= max_bytes:
        return working, mime, meta

    image = Image.open(io.BytesIO(working))
    if image.mode not in ("RGB", "L"):
        image = image.convert("RGB")
    image.thumbnail((2200, 2200))
    buf = io.BytesIO()
    image.save(buf, format="JPEG", quality=90)
    normalized = buf.getvalue()
    meta["gemini_input_sha256"] = hashlib.sha256(normalized).hexdigest()
    meta["normalized"] = True
    meta["normalization_reason"] = (
        "exif_orient+original_exceeds_max_bytes"
        if meta.get("exif_oriented")
        else "original_exceeds_max_bytes"
    )
    meta["byte_length"] = len(normalized)
    logger.info(
        "STOCK_IMAGE_GEMINI_NORMALIZE original_sha256=%s gemini_input_sha256=%s "
        "reason=%s original_bytes=%s gemini_bytes=%s",
        meta["original_sha256"],
        meta["gemini_input_sha256"],
        meta["normalization_reason"],
        len(file_bytes),
        len(normalized),
    )
    return normalized, "image/jpeg", meta


def _apply_detected_cells(item: Dict[str, Any], columns: List[Dict[str, Any]]) -> None:
    cells = item.get("cells")
    if not isinstance(cells, dict):
        extra = item.get("extra") if isinstance(item.get("extra"), dict) else {}
        cells = extra.get("cells") if isinstance(extra, dict) else None
    if not isinstance(cells, dict) or not columns:
        return
    extra = item.setdefault("extra", {})
    if not isinstance(extra, dict):
        extra = {}
        item["extra"] = extra
    month_slot = 0
    for column in columns:
        if not isinstance(column, dict):
            continue
        header = str(column.get("header") or "")
        semantic = str(column.get("semantic") or "").strip().lower().replace(" ", "_")
        if header not in cells:
            continue
        if semantic in _NON_QTY_SEMANTICS:
            if semantic == "packing":
                item["packing"] = cells[header]
            elif semantic in {"sales_value", "sale_val"}:
                item["sales_value"] = _number(cells[header])
            elif semantic in {"closing_value", "stk_val", "stock_value"}:
                item["closing_value"] = _number(cells[header])
            elif semantic == "rate":
                extra["rate"] = _number(cells[header])
            elif semantic in _MONTH_SEMANTICS:
                number = _number(cells[header])
                mon = semantic if semantic in {
                    "jan", "feb", "mar", "apr", "may", "jun",
                    "jul", "aug", "sep", "oct", "nov", "dec",
                } else None
                if mon:
                    extra[f"{mon}_qty"] = number
                if month_slot == 0:
                    extra["prior_month_a_qty"] = number
                    month_slot = 1
                elif month_slot == 1:
                    extra["prior_month_b_qty"] = number
                    month_slot = 2
            elif semantic in {
                "ins_sale",
                "institution_sale",
                "institution_sale_qty",
            }:
                extra["institution_sale_qty"] = _number(cells[header])
            else:
                extra.setdefault("unmapped_columns", {})[header] = cells[header]
            continue
        number = _number(cells[header])
        if semantic in {"opening_qty", "opstk"}:
            item["opening_qty"] = number
        elif semantic in {"purchase_qty", "receipts_qty", "purch"}:
            item["receipts_qty"] = number
        elif semantic in {"sales_qty", "sale"}:
            item["sales_qty"] = number
        elif semantic in _CLOSING_QTY_SEMANTICS:
            item["closing_qty"] = number
        elif semantic == "expiry_damage_qty":
            extra["expiry_damage_qty"] = number
        elif semantic in {"free", "free_out", "free_out_qty", "sfree"}:
            extra["free_out_qty"] = number
        else:
            extra.setdefault("unmapped_columns", {})[header] = cells[header]


def _header_key(text: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(text or "").lower())


def _looks_like_op_pur_sale_stk_rate_columns(
    columns: Optional[List[Dict[str, Any]]] = None,
    cells: Optional[Dict[str, Any]] = None,
) -> bool:
    """Fingerprint Op|Pur|Sale|Stk|Rate(+ optional Aug/Sep/SFree/Ins) grids."""
    headers: List[str] = []
    for col in columns or []:
        if isinstance(col, dict):
            headers.append(str(col.get("header") or ""))
    if isinstance(cells, dict):
        headers.extend(str(k) for k in cells.keys())
    keys = {_header_key(h) for h in headers if h}
    has_stk = bool(keys & {"stk", "clstk", "closingstk"})
    has_rate = "rate" in keys
    has_sale = bool(keys & {"sale", "sales"})
    has_op = bool(keys & {"op", "opening", "opstk"})
    # Stk+Rate with Sale/Op — not Bal/BVal money grids.
    if has_stk and has_rate and (has_sale or has_op):
        if keys & {"bval", "balval", "sval", "saleval", "clvalue", "clval"}:
            return False
        return True
    return False


def _cell_number_by_keys(cells: Dict[str, Any], *want: str) -> Any:
    want_set = {w.lower() for w in want}
    for header, value in cells.items():
        if _header_key(header) in want_set:
            return _number(value)
    return None


def repair_op_pur_sale_stk_rate_misfile(result: Dict[str, Any]) -> Dict[str, Any]:
    """Move Rate digits out of closing_value; restore Stk into closing_qty.

    Layout family: Item Name|Op.|Pur|SP|Aug|Sep|Sale|SFree|Ins Sale|Ins Free|
    Stk|Rate|Sh.E. Kill switch: STOCK_OP_PUR_STK_RATE_REPAIR=false.
    """
    if not isinstance(result, dict):
        return result
    raw = os.getenv("STOCK_OP_PUR_STK_RATE_REPAIR", "true").strip().lower()
    if raw in {"0", "false", "no", "off"}:
        return result
    extra_t = result.setdefault("totals", {}).setdefault("extra", {})
    columns = extra_t.get("detected_columns") if isinstance(extra_t, dict) else None
    if not isinstance(columns, list):
        columns = []
    items = [i for i in (result.get("line_items") or []) if isinstance(i, dict)]
    if not items:
        return result
    sample_cells = None
    for item in items:
        ex = item.get("extra") if isinstance(item.get("extra"), dict) else {}
        cells = ex.get("cells") if isinstance(ex, dict) else None
        if isinstance(cells, dict) and cells:
            sample_cells = cells
            break
    layout_hit = _looks_like_op_pur_sale_stk_rate_columns(columns, sample_cells)
    # Heuristic when Vision omitted detected_columns but parked Rate in closing_value.
    if not layout_hit:
        rate_like = 0
        active = 0
        for item in items:
            try:
                cv = item.get("closing_value")
                cq = item.get("closing_qty")
                if cv in (None, ""):
                    continue
                cv_f = float(cv)
                cq_f = float(cq) if cq not in (None, "") else 0.0
            except (TypeError, ValueError):
                continue
            # Unit rates on these sheets are typically 10..500 with paise.
            if 1.0 <= abs(cv_f) <= 500.0 and abs(cv_f - round(cv_f, 2)) < 1e-9:
                if cq_f == 0.0:
                    rate_like += 1
            op = item.get("opening_qty")
            try:
                if op not in (None, "") and float(op) > 0:
                    active += 1
            except (TypeError, ValueError):
                pass
        title = str(result.get("report_title") or "")
        title_ok = bool(
            re.search(r"Stock\s+and\s+Sales\s+Report", title, re.I)
            or re.search(r"Stock\s+and\s+Sale\s+Report", title, re.I)
        )
        if rate_like >= max(3, len(items) // 2) and (title_ok or rate_like == len(items)):
            layout_hit = True
    if not layout_hit:
        return result

    # Division chrome (068 HIMALAYA ZEAL DIV) is never a SKU on this family.
    kept_items: List[Dict[str, Any]] = []
    dropped_div = 0
    for item in items:
        name = str(item.get("product_name") or "").strip()
        if re.search(
            r"^\d{2,4}\s+HIMALAYA|\bHIMALAYA\s*-?\s*ZEAL\s*DIV\b",
            name,
            re.I,
        ):
            dropped_div += 1
            logger.info(
                "STOCK_OP_PUR_STK_RATE_REPAIR drop_division_banner product=%r",
                name[:80],
            )
            continue
        kept_items.append(item)
    if dropped_div:
        result["line_items"] = kept_items
        items = kept_items
        extra_t["op_pur_stk_rate_division_dropped"] = dropped_div

    repaired = 0
    for item in items:
        extra = item.setdefault("extra", {})
        if not isinstance(extra, dict):
            extra = {}
            item["extra"] = extra
        cells = extra.get("cells") if isinstance(extra.get("cells"), dict) else {}
        fs = extra.setdefault("field_source", {})
        if not isinstance(fs, dict):
            fs = {}
            extra["field_source"] = fs

        rate_from_cell = _cell_number_by_keys(cells, "rate") if cells else None
        stk_from_cell = _cell_number_by_keys(cells, "stk", "clstk", "closingstk") if cells else None
        sale_from_cell = _cell_number_by_keys(cells, "sale", "sales") if cells else None
        aug_from_cell = _cell_number_by_keys(cells, "aug") if cells else None
        sep_from_cell = _cell_number_by_keys(cells, "sep") if cells else None
        sfree_from_cell = _cell_number_by_keys(cells, "sfree", "sfreeqty", "salesfree") if cells else None
        ins_sale_from_cell = (
            _cell_number_by_keys(cells, "inssale", "institutionsale", "instsale")
            if cells
            else None
        )

        if rate_from_cell not in (None, ""):
            extra["rate"] = rate_from_cell
        if stk_from_cell not in (None, ""):
            item["closing_qty"] = stk_from_cell
            fs["closing_qty"] = "printed"
        if sale_from_cell not in (None, ""):
            item["sales_qty"] = sale_from_cell
            fs["sales_qty"] = "printed"
        if aug_from_cell not in (None, ""):
            extra["aug_qty"] = aug_from_cell
            extra.setdefault("prior_month_a_qty", aug_from_cell)
        if sep_from_cell not in (None, ""):
            extra["sep_qty"] = sep_from_cell
            extra.setdefault("prior_month_b_qty", sep_from_cell)
        if sfree_from_cell not in (None, ""):
            extra["free_out_qty"] = sfree_from_cell
        if ins_sale_from_cell not in (None, ""):
            extra["institution_sale_qty"] = ins_sale_from_cell

        # Rate misfiled as closing_value (no per-row Cl Value on this family).
        try:
            cv = item.get("closing_value")
            cv_f = float(cv) if cv not in (None, "") else None
        except (TypeError, ValueError):
            cv_f = None
        rate_now = extra.get("rate")
        try:
            rate_f = float(rate_now) if rate_now not in (None, "") else None
        except (TypeError, ValueError):
            rate_f = None
        if cv_f is not None and 1.0 <= abs(cv_f) <= 500.0:
            if rate_f is None or abs(rate_f - cv_f) < 0.011:
                extra["rate"] = cv_f
                item["closing_value"] = None
                fs["closing_value"] = "missing"
                repaired += 1
                logger.info(
                    "STOCK_OP_PUR_STK_RATE_REPAIR product=%s rate=%s "
                    "cleared_closing_value=1",
                    str(item.get("product_name") or "")[:60],
                    cv_f,
                )

        # Clear Aug/Sep parked in receipts/sales when Sale cell restored Stk identity.
        if sale_from_cell not in (None, "") and (
            aug_from_cell not in (None, "") or sep_from_cell not in (None, "")
        ):
            try:
                receipts = item.get("receipts_qty")
                if (
                    receipts not in (None, "")
                    and aug_from_cell not in (None, "")
                    and abs(float(receipts) - float(aug_from_cell)) < 0.011
                ):
                    # Pur cell may still be present under another header; only
                    # clear when Pur cell is blank/absent.
                    pur_cell = _cell_number_by_keys(cells, "pur", "purchase", "purch")
                    if pur_cell in (None, "", 0, 0.0):
                        item["receipts_qty"] = pur_cell if pur_cell not in (None, "") else 0.0
                        fs["receipts_qty"] = "printed"
            except (TypeError, ValueError):
                pass

    if repaired:
        extra_t["op_pur_stk_rate_repair"] = True
        extra_t["op_pur_stk_rate_repair_count"] = repaired
        logger.info(
            "STOCK_OP_PUR_STK_RATE_REPAIR applied count=%s items=%s",
            repaired,
            len(items),
        )
    return result


def normalize_stock_gemini_extraction(
    raw: Dict[str, Any], filename: str, ext: str
) -> Dict[str, Any]:
    """Copy Gemini fields. Do not rebalance quantities to satisfy identity."""
    from services.sales_statement_extractor import empty_line_item, empty_result

    result = empty_result(filename, (ext or "").lstrip("."))
    for key in (
        "stockist_name",
        "stockist_address",
        "company_name",
        "period_from",
        "period_to",
        "report_title",
    ):
        if raw.get(key) not in (None, ""):
            result[key] = raw.get(key)
    columns = [col for col in (raw.get("detected_columns") or []) if isinstance(col, dict)]
    items: List[Dict[str, Any]] = []
    for source in raw.get("line_items") or []:
        if not isinstance(source, dict):
            continue
        item = empty_line_item()
        name = str(source.get("product_name") or "").strip()
        if not name:
            continue
        if re.search(
            r"^\d{2,4}\s+HIMALAYA|HIMALAYA\s*-?\s*ZEAL|Division\s*Total|"
            r"^Total\b|^Done$|free\s*trial",
            name,
            re.I,
        ):
            continue
        item["product_name"] = name
        item["product_code"] = source.get("product_code")
        item["packing"] = source.get("packing")
        for field in (
            "opening_qty",
            "sales_qty",
            "sales_value",
            "closing_qty",
            "closing_value",
        ):
            if field in source and source.get(field) not in (None, ""):
                item[field] = _number(source.get(field))
        purchase = source.get("purchase_qty", source.get("receipts_qty"))
        if purchase not in (None, ""):
            item["receipts_qty"] = _number(purchase)
        extra = dict(source.get("extra") or {}) if isinstance(source.get("extra"), dict) else {}
        if source.get("expiry_damage_qty") not in (None, ""):
            extra["expiry_damage_qty"] = _number(source.get("expiry_damage_qty"))
            extra.setdefault("exp_damage", extra["expiry_damage_qty"])
        for alias, dest in (
            ("sale_return", "sale_return"),
            ("sale_ret", "sale_return"),
            ("exp_damage", "exp_damage"),
            ("total_stock", "total_stock"),
            ("total", "total_stock"),
            ("rate", "rate"),
            ("prior_month_a_qty", "prior_month_a_qty"),
            ("prior_month_b_qty", "prior_month_b_qty"),
            ("institution_sale_qty", "institution_sale_qty"),
            ("free_out_qty", "free_out_qty"),
        ):
            if source.get(alias) not in (None, "") and extra.get(dest) in (None, ""):
                extra[dest] = _number(source.get(alias))
            if isinstance(source.get("extra"), dict) and source["extra"].get(alias) not in (
                None,
                "",
            ):
                extra[dest] = _number(source["extra"].get(alias))
        if isinstance(source.get("cells"), dict):
            extra["cells"] = dict(source["cells"])
            item["cells"] = dict(source["cells"])
        item["extra"] = extra
        _apply_detected_cells(item, columns)
        # Map header cells for Product Stock Report aliases.
        cells = item.get("cells") if isinstance(item.get("cells"), dict) else extra.get("cells")
        if isinstance(cells, dict):
            for header, value in cells.items():
                key = re.sub(r"[^a-z0-9]+", "", str(header).lower())
                number = _number(value)
                if key in {"saleret", "salereturn"} and extra.get("sale_return") in (None, ""):
                    extra["sale_return"] = number
                elif key in {"expdmg", "expdamage"} and extra.get("exp_damage") in (None, ""):
                    extra["exp_damage"] = number
                    extra["expiry_damage_qty"] = number
                elif key in {"total"} and extra.get("total_stock") in (None, ""):
                    extra["total_stock"] = number
                elif key in {"closstock", "closingstock", "closing", "stk"} and item.get(
                    "closing_qty"
                ) in (None, ""):
                    item["closing_qty"] = number
                elif key == "rate" and extra.get("rate") in (None, ""):
                    extra["rate"] = number
                elif key in {"closamt", "clsamt", "closingamount", "closingamt"} and item.get(
                    "closing_value"
                ) in (None, ""):
                    item["closing_value"] = number
        # Keep cells for Stk|Rate repair; strip after repair below.
        items.append(item)
    result["line_items"] = items
    image_quality = raw.get("image_quality") if isinstance(raw.get("image_quality"), dict) else {}
    result["totals"]["extra"]["image_quality"] = image_quality
    result["totals"]["extra"]["detected_columns"] = columns
    result["totals"]["extra"]["extraction_method"] = "stock_image_gemini"
    result["totals"]["extra"]["layout"] = "stock_image_gemini"
    result = repair_op_pur_sale_stk_rate_misfile(result)
    for item in result.get("line_items") or []:
        if isinstance(item, dict):
            item.pop("cells", None)
            ex = item.get("extra") if isinstance(item.get("extra"), dict) else None
            if isinstance(ex, dict):
                ex.pop("cells", None)
    # Drop transit mis-filed as total_stock when Op/Sales/Cl already balance.
    for item in result["line_items"]:
        extra = item.get("extra") if isinstance(item.get("extra"), dict) else None
        if not extra or extra.get("total_stock") in (None, ""):
            continue
        try:
            tot_f = float(extra.get("total_stock"))
            op = float(item.get("opening_qty") or 0)
            pur = float(
                item.get("receipts_qty")
                if item.get("receipts_qty") is not None
                else (item.get("purchase_qty") or 0)
            )
            sales = float(item.get("sales_qty") or 0)
            cl = float(item.get("closing_qty") or 0)
        except (TypeError, ValueError):
            continue
        if abs((op + pur) - tot_f) <= 1.0:
            continue
        if abs((op + pur - sales) - cl) <= 1.0:
            extra.setdefault("transit", tot_f)
            extra.pop("total_stock", None)
            logger.info(
                "STOCK_IMAGE_DROP_UNTRUSTED_TOTAL product=%s total=%s",
                str(item.get("product_name") or "")[:80],
                tot_f,
            )
    # Vision totals from footer labels when present on the raw response.
    raw_totals = raw.get("totals") if isinstance(raw.get("totals"), dict) else {}
    if raw_totals:
        if result["totals"].get("sales_value") in (None, "") and raw_totals.get(
            "sales_value"
        ) not in (None, ""):
            result["totals"]["sales_value"] = raw_totals.get("sales_value")
        if result["totals"].get("closing_value") in (None, "") and raw_totals.get(
            "closing_value"
        ) not in (None, ""):
            result["totals"]["closing_value"] = raw_totals.get("closing_value")
        raw_extra = raw_totals.get("extra") if isinstance(raw_totals.get("extra"), dict) else {}
        for k in ("opening_value", "purchase_value"):
            if raw_extra.get(k) not in (None, "") and result["totals"]["extra"].get(k) in (
                None,
                "",
            ):
                result["totals"]["extra"][k] = raw_extra.get(k)
    return result


def _response_json(response: Any) -> Optional[Dict[str, Any]]:
    from services.sales_statement_extractor import (
        _extract_json_object,
        _gemini_response_text,
    )

    return _extract_json_object(_gemini_response_text(response))


def invoke_stock_gemini(payload: Dict[str, Any]) -> Any:
    """Sales Gemini slot. Does not change the rate limiter."""
    from services.sales_extraction_runtime import sales_generate_content_via_vertex

    model = os.getenv("VISION_MODEL", "gemini-2.5-flash-lite").strip()
    return sales_generate_content_via_vertex(
        model=model,
        payload=payload,
        timeout=120,
        label="stock_image_gemini",
    )


def _unreadable(image_quality: Dict[str, Any]) -> bool:
    quality = str(image_quality.get("quality") or "").lower()
    if quality in {"poor", "unreadable"}:
        return True
    if image_quality.get("readable") is False:
        return True
    return False


def _structured_is_untrusted(decision: Dict[str, Any]) -> bool:
    """True when returning OCR/heuristic rows would be a false success."""
    if not decision.get("needs_direct_gemini"):
        return False
    reasons = set(decision.get("reasons") or [])
    if reasons & {
        "identity_failure_rate",
        "IDENTITY_VETO",
        "numeric_coverage_low",
        "schema_unknown",
        "required_headers_missing",
        "layout_not_understood",
        "oric_ocr_weak",
    }:
        return True
    try:
        conf = float(decision.get("extraction_confidence") or 1.0)
    except (TypeError, ValueError):
        conf = 1.0
    return conf < 0.5


def _fail_closed_untrusted(
    result: Dict[str, Any],
    decision: Dict[str, Any],
    reason: str,
) -> Dict[str, Any]:
    """Mark extraction_failed so /extract-sales-statement returns 422.

    Laravel secondary-sales reprocess must not treat garbage OCR as success
    (HTTP 200 with COLUMN_ASSIGNMENT_SUSPECTED rows leaves the UI unchanged
    or shows wrong qtys).
    """
    if not _structured_is_untrusted(decision):
        return result
    extra = result.setdefault("totals", {}).setdefault("extra", {})
    if not isinstance(extra, dict):
        return result
    extra["extraction_failed"] = True
    extra["extraction_failed_reason"] = reason
    extra["final_selected_extraction"] = "failed"
    extra["stock_image_gemini_status"] = reason
    extra["quality_decision"] = decision
    extra["stock_image_vision_decided"] = True
    logger.info(
        "STOCK_IMAGE_FAIL_CLOSED file=%s reason=%s confidence=%s reasons=%s",
        (result.get("source_file") or extra.get("source_file") or "-"),
        reason,
        decision.get("extraction_confidence"),
        ",".join(decision.get("reasons") or []) or "none",
    )
    return result


def apply_direct_stock_image_gemini(
    result: Dict[str, Any],
    file_bytes: bytes,
    filename: str,
    ext: str,
    signals: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Keep one complete extract: structured OCR, or original-image Gemini."""
    try:
        from services.stock_direct_vision import is_stock_vision_locked
        from services.stock_row_classifier import (
            gemini_call_budget,
            get_gemini_calls,
            veto_active_for,
            veto_decision,
        )

        if is_stock_vision_locked(result):
            # Phase 1: failing locked Vision must not finalise under veto —
            # leave decided unset so maybe_apply can run one more call.
            if veto_active_for("image"):
                locked_veto = veto_decision(result, "image")
                if locked_veto.get("veto") and get_gemini_calls(result) < gemini_call_budget(result):
                    extra = result.setdefault("totals", {}).setdefault("extra", {})
                    if isinstance(extra, dict):
                        extra.pop("stock_image_vision_decided", None)
                        extra["stock_vision_locked"] = False
                    return result
            extra = result.setdefault("totals", {}).setdefault("extra", {})
            extra["stock_image_vision_decided"] = True
            extra.setdefault("final_selected_extraction", "gemini")
            return result
    except Exception:
        pass
    decision = assess_stock_structured_quality(result, signals)
    extra = result.setdefault("totals", {}).setdefault("extra", {})
    extra["quality_decision"] = decision
    force_gemini = bool(decision.get("needs_direct_gemini"))
    try:
        from services.stock_row_classifier import veto_active_for, veto_decision

        if veto_active_for("image") and veto_decision(result, "image").get("veto"):
            force_gemini = True
    except Exception:
        pass
    extra["stock_image_vision_decided"] = True
    logger.info(
        "STOCK_IMAGE_QUALITY file=%s needs_gemini=%s schema=%s confidence=%s "
        "reasons=%s rows=%s",
        filename,
        str(force_gemini).lower(),
        decision.get("schema"),
        decision.get("extraction_confidence"),
        ",".join(decision.get("reasons") or []) or "none",
        decision.get("row_count"),
    )
    # Prefer Op_Stk|Rcpts|Sales|Cl_Stk BEFORE the trusted-structured early return.
    # Misfiled Marg main_stock was returning with needs_direct_gemini=false.
    method = str(extra.get("extraction_method") or "")
    title = str(result.get("report_title") or "")
    layout = str(extra.get("layout") or "")
    peek_text = ""
    try:
        from services.sales_statement_extractor import _ocr_image_to_text

        peek_text = _ocr_image_to_text(file_bytes, psm=6, enhance=False) or ""
    except Exception:
        peek_text = ""

    try:
        from services.sales_statement_extractor import (
            _extract_medivision_op_wsale_photo,
            _extract_op_stk_rcpts_stock_sale_vision,
            _is_medivision_op_wsale_header,
            _looks_like_op_stk_rcpts_grid_text,
            _looks_like_ostk_qoh_stock_sales_text,
            _main_stock_result_is_op_stk_misfiled,
            _medivision_op_wsale_early_enabled,
            _op_stk_before_ssa_phone_enabled,
            _ssa_dump_result_looks_like_op_stk_misfile,
        )

        # MediVision Op/Purc/Sale/WSale before Op_Stk — SSA misfile + Cl val
        # peek otherwise steals this chrome into Op_Stk/Rcpts column shift.
        # Tall stacked Pack|Rate|Sale Value scans must stay on VISION_TABLE.
        _skip_medivision_stacked = False
        try:
            from services.stock_vision_table import _needs_vertical_split

            _skip_medivision_stacked = bool(_needs_vertical_split(file_bytes))
        except Exception:
            _skip_medivision_stacked = False
        medivision = None
        if (
            _enabled()
            and _medivision_op_wsale_early_enabled()
            and _is_medivision_op_wsale_header(peek_text)
            and not _skip_medivision_stacked
        ):
            try:
                medivision = _extract_medivision_op_wsale_photo(
                    file_bytes, filename, ext, force=False
                )
            except Exception as exc:
                logger.info(
                    "STOCK_IMAGE_GEMINI file=%s medivision_op_wsale preferred "
                    "path failed: %s",
                    filename,
                    exc,
                )
                medivision = None
        elif _skip_medivision_stacked and _is_medivision_op_wsale_header(peek_text):
            logger.info(
                "STOCK_IMAGE_GEMINI file=%s medivision_op_wsale skipped "
                "reason=stacked_multi_block_pack_rate",
                filename,
            )
        if medivision and medivision.get("line_items"):
            try:
                from services.gemini_extraction_fallback import (
                    _pack_rate_prev_sale_shift_suspected,
                )

                if _pack_rate_prev_sale_shift_suspected(medivision):
                    logger.info(
                        "STOCK_IMAGE_GEMINI file=%s medivision_op_wsale rejected "
                        "reason=pack_rate_prev_sale_shift rows=%s",
                        filename,
                        len(medivision.get("line_items") or []),
                    )
                    medivision = None
            except Exception:
                pass
        if medivision and medivision.get("line_items"):
            selected_extra = medivision.setdefault("totals", {}).setdefault("extra", {})
            selected_extra["quality_decision"] = decision
            selected_extra["structured_extraction"] = {
                "extraction_method": extra.get("extraction_method"),
                "layout": layout,
                "schema": decision.get("schema"),
                "row_count": decision.get("row_count"),
                "line_items": copy.deepcopy(result.get("line_items") or []),
            }
            selected_extra["final_selected_extraction"] = "gemini"
            selected_extra["stock_image_vision_decided"] = True
            selected_extra["stock_image_gemini_status"] = "medivision_op_wsale_preferred"
            selected_extra["early_vision_reason"] = "medivision_op_wsale_before_op_stk"
            logger.info(
                "STOCK_IMAGE_GEMINI file=%s status=medivision_op_wsale_preferred "
                "rows=%s",
                filename,
                len(medivision.get("line_items") or []),
            )
            return medivision

        # SSA dump / pack-Op-Pur-Bal misfile of Stock and Sales Detail → Op.Qty Vision.
        # Pack-Op-Pur-Bal of PharmAssist Report → locked PharmAssist Vision
        # (prompt example Pur Val=125277 must not stick as purchase_value).
        try:
            from services.stock_direct_vision import (
                _has_stock_sales_op_qty_val_headers,
                _has_pharmassist_stock_sale_headers,
                extract_stock_sales_op_qty_val_direct_vision,
                extract_pharmassist_direct_vision,
                is_stock_vision_locked,
            )

            wrong_schema = method in {
                "ssa_qty_value_vision",
                "ssa_qty_value_vision_pdf",
                "pack_op_pur_bal_stock_sale_vision",
                "op_pur_sp_sale_bal_val_photo",
            } or layout in {
                "ssa_opening_receipt_issue_value",
                "pack_op_pur_bal_stock_sale",
                "op_pur_sp_sale_bal_val",
            }
            pack_misfile = method in {
                "pack_op_pur_bal_stock_sale_vision",
                "op_pur_sp_sale_bal_val_photo",
            } or layout in {
                "pack_op_pur_bal_stock_sale",
                "op_pur_sp_sale_bal_val",
            }
            # Prompt-example bleed / Sep|Aug→Op/Pur shift / stockist-in-company.
            result_misfile = False
            try:
                from services.sales_statement_extractor import (
                    _pack_op_pur_bal_looks_like_pharmassist_misfile,
                )

                result_misfile = _pack_op_pur_bal_looks_like_pharmassist_misfile(
                    result, peek_text
                )
            except Exception:
                result_misfile = False
            try:
                pack_example_pv = abs(
                    float(
                        ((result.get("totals") or {}).get("extra") or {}).get(
                            "purchase_value"
                        )
                        or 0
                    )
                    - 125277.0
                ) < 0.51
            except (TypeError, ValueError):
                pack_example_pv = False
            if (
                _enabled()
                and pack_misfile
                and (
                    _has_pharmassist_stock_sale_headers(peek_text)
                    or pack_example_pv
                    or result_misfile
                )
            ):
                pharm_fix = extract_pharmassist_direct_vision(
                    file_bytes,
                    filename,
                    ext,
                    reason="pharmassist_after_pack_op_pur_misfile",
                )
                if (
                    pharm_fix
                    and pharm_fix.get("line_items")
                    and is_stock_vision_locked(pharm_fix)
                ):
                    selected_extra = pharm_fix.setdefault("totals", {}).setdefault(
                        "extra", {}
                    )
                    selected_extra["quality_decision"] = decision
                    selected_extra["structured_extraction"] = {
                        "extraction_method": extra.get("extraction_method"),
                        "layout": layout,
                        "schema": decision.get("schema"),
                        "row_count": decision.get("row_count"),
                        "line_items": copy.deepcopy(result.get("line_items") or []),
                    }
                    selected_extra["final_selected_extraction"] = "gemini"
                    selected_extra["stock_image_vision_decided"] = True
                    selected_extra["stock_image_gemini_status"] = (
                        "pharmassist_after_pack_op_pur_misfile"
                    )
                    logger.info(
                        "STOCK_IMAGE_GEMINI file=%s "
                        "status=pharmassist_after_pack_op_pur_misfile rows=%s "
                        "from_method=%s example_pv=%s",
                        filename,
                        len(pharm_fix.get("line_items") or []),
                        method,
                        pack_example_pv,
                    )
                    return pharm_fix
            if (
                _enabled()
                and wrong_schema
                and _has_stock_sales_op_qty_val_headers(peek_text)
            ):
                detail_fix = extract_stock_sales_op_qty_val_direct_vision(
                    file_bytes,
                    filename,
                    ext,
                    reason="stock_sales_detail_after_wrong_schema",
                )
                if (
                    detail_fix
                    and detail_fix.get("line_items")
                    and is_stock_vision_locked(detail_fix)
                ):
                    selected_extra = detail_fix.setdefault("totals", {}).setdefault(
                        "extra", {}
                    )
                    selected_extra["quality_decision"] = decision
                    selected_extra["structured_extraction"] = {
                        "extraction_method": extra.get("extraction_method"),
                        "layout": layout,
                        "schema": decision.get("schema"),
                        "row_count": decision.get("row_count"),
                        "line_items": copy.deepcopy(result.get("line_items") or []),
                    }
                    selected_extra["final_selected_extraction"] = "gemini"
                    selected_extra["stock_image_vision_decided"] = True
                    selected_extra["stock_image_gemini_status"] = (
                        "stock_sales_detail_after_wrong_schema"
                    )
                    logger.info(
                        "STOCK_IMAGE_GEMINI file=%s "
                        "status=stock_sales_detail_after_wrong_schema rows=%s "
                        "from_method=%s",
                        filename,
                        len(detail_fix.get("line_items") or []),
                        method,
                    )
                    return detail_fix
        except Exception as detail_exc:
            logger.info(
                "STOCK_IMAGE_GEMINI file=%s stock_sales_detail/pharmassist after "
                "wrong schema failed: %s",
                filename,
                type(detail_exc).__name__,
            )

        ssa_misfile = (
            method in {"ssa_qty_value_vision", "ssa_qty_value_vision_pdf"}
            or layout == "ssa_opening_receipt_issue_value"
        ) and _ssa_dump_result_looks_like_op_stk_misfile(result)
        want_op_stk = (
            not _looks_like_ostk_qoh_stock_sales_text(peek_text)
            and not _is_medivision_op_wsale_header(peek_text)
            and (
                _looks_like_op_stk_rcpts_grid_text(peek_text)
                or _main_stock_result_is_op_stk_misfiled(result, peek_text)
                or (ssa_misfile and _op_stk_before_ssa_phone_enabled())
            )
        )
        if want_op_stk and _enabled():
            op_stk = _extract_op_stk_rcpts_stock_sale_vision(
                file_bytes, filename, ext, ocr_hint=peek_text
            )
        else:
            op_stk = None
    except Exception as exc:
        logger.info(
            "STOCK_IMAGE_GEMINI file=%s op_stk_rcpts preferred path failed: %s",
            filename,
            exc,
        )
        op_stk = None
    if op_stk and op_stk.get("line_items"):
        selected_extra = op_stk.setdefault("totals", {}).setdefault("extra", {})
        selected_extra["quality_decision"] = decision
        selected_extra["structured_extraction"] = {
            "extraction_method": extra.get("extraction_method"),
            "layout": layout,
            "schema": decision.get("schema"),
            "row_count": decision.get("row_count"),
            "line_items": copy.deepcopy(result.get("line_items") or []),
        }
        selected_extra["final_selected_extraction"] = "gemini"
        selected_extra["stock_image_vision_decided"] = True
        selected_extra["stock_image_gemini_status"] = "op_stk_rcpts_preferred"
        logger.info(
            "STOCK_IMAGE_GEMINI file=%s status=op_stk_rcpts_preferred rows=%s",
            filename,
            len(op_stk.get("line_items") or []),
        )
        return op_stk

    # Weak qty-only ORIC OCR: prefer dedicated ORIC Vision over generic prompt
    # (generic often invents VALUE columns / shifts RECEIPT).
    if (
        _enabled()
        and method == "ssa_oric_qty_only_ocr"
        and (force_gemini or "oric_ocr_weak" in set(decision.get("reasons") or []))
    ):
        try:
            from services.sales_statement_extractor import (
                _extract_ssa_oric_qty_only_vision,
                _ssa_oric_vision_result_usable,
            )

            ocr_rows = [
                item
                for item in (result.get("line_items") or [])
                if isinstance(item, dict)
            ] or None
            oric_vision = _extract_ssa_oric_qty_only_vision(
                file_bytes, filename, ext, ocr_items=ocr_rows
            )
        except Exception as exc:
            logger.info(
                "STOCK_IMAGE_GEMINI file=%s ssa_oric_qty_only preferred path "
                "failed: %s",
                filename,
                type(exc).__name__,
            )
            oric_vision = None
        if (
            oric_vision
            and oric_vision.get("line_items")
            and _ssa_oric_vision_result_usable(oric_vision)
        ):
            selected_extra = oric_vision.setdefault("totals", {}).setdefault("extra", {})
            selected_extra["quality_decision"] = decision
            selected_extra["structured_extraction"] = {
                "extraction_method": extra.get("extraction_method"),
                "layout": layout,
                "schema": decision.get("schema"),
                "row_count": decision.get("row_count"),
                "line_items": copy.deepcopy(result.get("line_items") or []),
            }
            selected_extra["final_selected_extraction"] = "gemini"
            selected_extra["stock_image_vision_decided"] = True
            selected_extra["stock_image_gemini_status"] = "ssa_oric_qty_only_preferred"
            logger.info(
                "STOCK_IMAGE_GEMINI file=%s status=ssa_oric_qty_only_preferred rows=%s",
                filename,
                len(oric_vision.get("line_items") or []),
            )
            return oric_vision

    if not force_gemini:
        extra["final_selected_extraction"] = "structured"
        return result
    if not _enabled():
        extra["final_selected_extraction"] = "structured"
        extra["stock_image_gemini_status"] = "disabled"
        return _fail_closed_untrusted(result, decision, "gemini_disabled")

    # Prefer the dedicated Medica OPSTK strip reader over the generic prompt when
    # Pharma Hub / generic vision already misfiled this STOCK STATEMENT photo.
    try:
        from services.sales_statement_extractor import (
            _extract_product_stock_report_image_vision,
            _looks_like_psr_saleret_image,
        )

        if _looks_like_psr_saleret_image(result, peek_text):
            psr = _extract_product_stock_report_image_vision(
                file_bytes, filename, ext
            )
        else:
            psr = None
    except Exception as exc:
        logger.info(
            "STOCK_IMAGE_GEMINI file=%s product-stock preferred path failed: %s",
            filename,
            exc,
        )
        psr = None
    if psr and psr.get("line_items"):
        selected_extra = psr.setdefault("totals", {}).setdefault("extra", {})
        selected_extra["quality_decision"] = decision
        selected_extra["structured_extraction"] = {
            "extraction_method": extra.get("extraction_method"),
            "layout": extra.get("layout"),
            "schema": decision.get("schema"),
            "row_count": decision.get("row_count"),
            "line_items": copy.deepcopy(result.get("line_items") or []),
        }
        selected_extra["final_selected_extraction"] = "gemini"
        selected_extra["stock_image_vision_decided"] = True
        selected_extra["stock_image_gemini_status"] = "product_stock_report_preferred"
        # Keep PSR vision's gemini_input meta (may be overline-denoised).
        if not isinstance(selected_extra.get("gemini_input"), dict):
            selected_extra["gemini_input"] = {
                "original_sha256": hashlib.sha256(file_bytes).hexdigest(),
                "gemini_input_sha256": hashlib.sha256(file_bytes).hexdigest(),
                "normalized": False,
                "normalization_reason": None,
                "byte_length": len(file_bytes),
            }
        return psr

    # Medica OPSTK/PURCH/STK VAL only — do not steal Opening stock /
    # Purchased Qty / Sales Qty / Closing Stock (STOCK & SALES STATEMENT)
    # sheets that landed in generic early Vision.
    _medica_title = bool(re.fullmatch(r"STOCK STATEMENT", title, re.I))
    _medica_peek = bool(
        re.search(r"\bOPSTK\b", peek_text or "", re.I)
        and re.search(r"\bPURCH\b", peek_text or "", re.I)
    )
    _medica_prefer = (
        method.startswith("pharma_hub")
        or _medica_title
        or (
            method in {"gemini_vision", "gemini_extraction_fallback"}
            and _medica_peek
            and os.getenv(
                "STOCK_MEDICA_PREFER_ON_GENERIC_VISION", "true"
            ).strip().lower()
            in {"1", "true", "yes", "on"}
        )
    )
    if not _medica_prefer and method in {
        "gemini_vision",
        "gemini_extraction_fallback",
    }:
        logger.info(
            "STOCK_IMAGE_GEMINI file=%s medica preferred skipped "
            "reason=not_medica_opstk title=%s",
            filename,
            (title or "")[:60],
        )
    if _medica_prefer:
        try:
            from services.sales_statement_extractor import (
                _extract_medica_stock_statement_vision,
            )

            medica = _extract_medica_stock_statement_vision(
                file_bytes, filename, ext
            )
        except Exception as exc:
            logger.info(
                "STOCK_IMAGE_GEMINI file=%s medica preferred path failed: %s",
                filename,
                exc,
            )
            medica = None
        if medica and medica.get("line_items"):
            selected_extra = medica.setdefault("totals", {}).setdefault("extra", {})
            selected_extra["quality_decision"] = decision
            selected_extra["structured_extraction"] = {
                "extraction_method": extra.get("extraction_method"),
                "layout": extra.get("layout"),
                "schema": decision.get("schema"),
                "row_count": decision.get("row_count"),
                "line_items": copy.deepcopy(result.get("line_items") or []),
            }
            selected_extra["final_selected_extraction"] = "gemini"
            selected_extra["stock_image_vision_decided"] = True
            selected_extra["stock_image_gemini_status"] = "medica_preferred"
            selected_extra["gemini_input"] = {
                "original_sha256": hashlib.sha256(file_bytes).hexdigest(),
                "gemini_input_sha256": hashlib.sha256(file_bytes).hexdigest(),
                "normalized": False,
                "normalization_reason": "medica_statement_strips",
                "byte_length": len(file_bytes),
            }
            return medica

    image_bytes, mime, image_meta = prepare_original_image(file_bytes, ext)
    logger.info(
        "STOCK_IMAGE_GEMINI file=%s original_sha256=%s gemini_input_sha256=%s "
        "normalized=%s reason=%s bytes=%s",
        filename,
        image_meta["original_sha256"],
        image_meta["gemini_input_sha256"],
        str(image_meta["normalized"]).lower(),
        image_meta.get("normalization_reason") or "none",
        image_meta["byte_length"],
    )
    payload = {
        "contents": [
            {
                "role": "user",
                "parts": [
                    {"text": STOCK_IMAGE_VISION_PROMPT},
                    {
                        "inline_data": {
                            "mime_type": mime,
                            "data": base64.b64encode(image_bytes).decode("ascii"),
                        }
                    },
                ],
            }
        ],
        "generationConfig": {"temperature": 0.0, "maxOutputTokens": 8192},
    }
    try:
        from services.stock_row_classifier import bump_gemini_calls

        bump_gemini_calls(result, 1)
        response = invoke_stock_gemini(payload)
        parsed = _response_json(response)
    except Exception as exc:
        logger.warning("STOCK_IMAGE_GEMINI file=%s status=failed error=%s", filename, exc)
        extra["stock_image_gemini_status"] = "failed"
        extra["final_selected_extraction"] = "structured"
        extra["gemini_input"] = image_meta
        try:
            from services.stock_row_classifier import veto_active_for, veto_decision

            if veto_active_for("image") and veto_decision(result, "image").get("veto"):
                extra.pop("stock_image_vision_decided", None)
        except Exception:
            pass
        if extra.get("ocr_header_aligned") and result.get("line_items"):
            extra["stock_image_gemini_status"] = "failed_keep_header_aligned"
            logger.info(
                "STOCK_IMAGE_GEMINI file=%s status=failed_keep_header_aligned rows=%s",
                filename,
                len(result.get("line_items") or []),
            )
            return result
        return _fail_closed_untrusted(result, decision, "gemini_invoke_failed")
    if not isinstance(parsed, dict):
        extra["stock_image_gemini_status"] = "invalid_json"
        extra["final_selected_extraction"] = "structured"
        extra["gemini_input"] = image_meta
        logger.warning(
            "STOCK_IMAGE_GEMINI file=%s status=invalid_json response_type=%s",
            filename,
            type(parsed).__name__,
        )
        return _fail_closed_untrusted(result, decision, "gemini_invalid_json")

    raw = copy.deepcopy(parsed)
    image_quality = raw.get("image_quality") if isinstance(raw.get("image_quality"), dict) else {}
    if _unreadable(image_quality):
        raw["line_items"] = []
    selected = normalize_stock_gemini_extraction(raw, filename, ext)
    if not (selected.get("line_items") or []):
        logger.warning(
            "STOCK_IMAGE_GEMINI file=%s status=empty_products image_quality=%s",
            filename,
            image_quality.get("quality") if isinstance(image_quality, dict) else None,
        )
        extra["stock_image_gemini_status"] = "empty_products"
        extra["final_selected_extraction"] = "structured"
        extra["gemini_input"] = image_meta
        if isinstance(image_quality, dict) and image_quality:
            extra["image_quality"] = image_quality
        # Prefer a clean empty gemini shell (with quality meta) over garbage OCR
        # when the model reported unreadable / returned no products.
        if _structured_is_untrusted(decision):
            selected_extra = selected.setdefault("totals", {}).setdefault("extra", {})
            selected_extra["quality_decision"] = decision
            selected_extra["gemini_input"] = image_meta
            selected_extra["stock_image_gemini_status"] = "empty_products"
            selected_extra["final_selected_extraction"] = "failed"
            selected_extra["extraction_failed"] = True
            selected_extra["extraction_failed_reason"] = "gemini_empty_products"
            selected_extra["stock_image_vision_decided"] = True
            if isinstance(image_quality, dict) and image_quality:
                selected_extra["image_quality"] = image_quality
            logger.info(
                "STOCK_IMAGE_FAIL_CLOSED file=%s reason=gemini_empty_products "
                "confidence=%s reasons=%s",
                filename,
                decision.get("extraction_confidence"),
                ",".join(decision.get("reasons") or []) or "none",
            )
            return selected
        return result
    selected_extra = selected.setdefault("totals", {}).setdefault("extra", {})
    selected_extra["GEMINI_RAW_EXTRACTION"] = parsed
    selected_extra["quality_decision"] = decision
    selected_extra["structured_extraction"] = {
        "extraction_method": extra.get("extraction_method"),
        "layout": extra.get("layout"),
        "schema": decision.get("schema"),
        "row_count": decision.get("row_count"),
        "line_items": copy.deepcopy(result.get("line_items") or []),
    }
    selected_extra["gemini_extraction"] = {
        "row_count": len(selected.get("line_items") or []),
        "image_quality": image_quality,
        "detected_columns": raw.get("detected_columns") or [],
    }
    selected_extra["final_selected_extraction"] = "gemini"
    selected_extra["stock_image_vision_decided"] = True
    selected_extra["gemini_input"] = image_meta
    selected_extra["stock_image_gemini_status"] = "success"
    try:
        from services.stock_row_classifier import (
            get_gemini_calls,
            veto_active_for,
            veto_decision,
        )

        selected_extra["gemini_calls"] = get_gemini_calls(result)
        if veto_active_for("image") and veto_decision(selected, "image").get("veto"):
            # Allow exactly one more call via maybe_apply_gemini_fallback.
            selected_extra.pop("stock_image_vision_decided", None)
    except Exception:
        pass
    return selected
