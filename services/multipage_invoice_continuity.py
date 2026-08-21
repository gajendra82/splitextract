"""Generic multi-page invoice continuity, validation, and safe page-level merge.

This module is a common layer around existing page extractions. It does NOT
implement stockist-specific OCR/Vision fallbacks — those remain in app.py and
must keep priority over the generic combined-OCR path.

Default strategy for multi-page groups (after stockist-specific handlers):
  1. Keep independent page-level extractions
  2. Validate each page's line items
  3. Merge only validated rows into the invoice
  4. Use combined multi-page Gemini re-extract ONLY as a controlled fallback
     when page-level merge yields no usable products — and never overwrite a
     stronger page-level result with a weaker combined result.
"""

from __future__ import annotations

import copy
import logging
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

# Footer / non-product phrases commonly misread as line items
_FOOTER_PRODUCT_RE = re.compile(
    r"(?i)\b("
    r"authorized\s+signatory|for\s+[A-Z].{0,40}|"
    r"total\s+(?:invoice|taxable|amount|value)|grand\s+total|"
    r"bank\s+details|terms\s*(?:&|and)?\s*conditions|"
    r"page\s+\d+\s*(?:of|/)\s*\d+|e[- ]?invoice|irn\b|ack\s*no"
    r")\b"
)

# GST % values often OCR'd into the qty column on pharma invoices
_GST_PERCENT_VALUES = {1.0, 2.0, 2.5, 5.0, 6.0, 12.0, 18.0, 28.0}


class PageClassification(str, Enum):
    NEW_INVOICE = "NEW_INVOICE"
    SAME_INVOICE = "SAME_INVOICE"
    CONTINUATION_NO_INVOICE = "CONTINUATION_NO_INVOICE"
    AMBIGUOUS = "AMBIGUOUS"


@dataclass
class PageValidationResult:
    status: str  # ok | suspicious | invalid
    warnings: List[str] = field(default_factory=list)
    accepted_items: List[Dict[str, Any]] = field(default_factory=list)
    rejected_items: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def is_usable(self) -> bool:
        return self.status in {"ok", "suspicious"} and bool(self.accepted_items)


@dataclass
class PageAuditRecord:
    page_number: int  # 1-based
    detected_invoice_number: Optional[str]
    normalized_invoice_number: Optional[str]
    previous_invoice_number: Optional[str]
    classification: PageClassification
    validation_status: str = "n/a"
    validation_warnings: List[str] = field(default_factory=list)
    number_of_products_extracted: int = 0
    number_of_products_merged: int = 0
    extraction_strategy_used: str = "page_level"
    fallback_used: bool = False


@dataclass
class InvoiceGroupAudit:
    invoice_number: Optional[str]
    source_pages: List[int]
    total_products_before_aggregation: int
    total_products_after_aggregation: int
    validation_status: str
    extraction_strategy_used: str
    fallback_used: bool = False
    page_audits: List[PageAuditRecord] = field(default_factory=list)


@dataclass
class MultipageMergeResult:
    applied: bool
    extracted_data: Optional[Dict[str, Any]]
    needs_combined_fallback: bool
    strategy: str
    before_count: int
    after_count: int
    page_audits: List[PageAuditRecord] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


def normalize_invoice_number(value: Any) -> Optional[str]:
    """Normalize for continuity compare: strip spaces/hyphens/case; keep alnum.

    Intentionally does not over-normalize (no fuzzy/edit-distance matching) so
    genuinely different invoice numbers stay distinct.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.upper() in {"NONE", "NULL", "N/A", "NA", "NIL", ""}:
        return None
    compact = re.sub(r"[^A-Z0-9]", "", text.upper())
    return compact or None


def classify_page_continuity(
    *,
    page_index: int,
    page_invoice_no: Any,
    current_invoice_no: Any,
    attach_decision: Optional[bool] = None,
) -> PageClassification:
    """Classify page relative to current invoice context.

    When ``attach_decision`` is provided (from existing
    ``_should_attach_page_to_current_invoice_group``), it is the authority for
    attach vs new-group. This function only labels the case for audit logs.
    """
    if page_index <= 0 or current_invoice_no is None:
        return PageClassification.NEW_INVOICE

    page_norm = normalize_invoice_number(page_invoice_no)
    current_norm = normalize_invoice_number(current_invoice_no)

    if attach_decision is False:
        return PageClassification.NEW_INVOICE

    if page_norm and current_norm and page_norm == current_norm:
        return PageClassification.SAME_INVOICE

    if page_norm is None:
        if attach_decision is True:
            return PageClassification.CONTINUATION_NO_INVOICE
        return PageClassification.AMBIGUOUS

    if page_norm and current_norm and page_norm != current_norm:
        if attach_decision is True:
            # Existing attach said continue despite different raw value
            # (footer artifact / over-normalized conflict already handled upstream)
            return PageClassification.AMBIGUOUS
        return PageClassification.NEW_INVOICE

    if attach_decision is True:
        return PageClassification.CONTINUATION_NO_INVOICE
    return PageClassification.AMBIGUOUS


def _to_float(value: Any) -> float:
    try:
        text = str(value or "").strip()
        if not text:
            return 0.0
        text = text.replace(",", "").replace("₹", "").replace("Rs.", "").replace("Rs", "")
        text = re.sub(r"[^0-9.\-]", "", text)
        if not text or text in {".", "-", "-."}:
            return 0.0
        return float(text)
    except Exception:
        return 0.0


def _product_name(item: Dict[str, Any]) -> str:
    return str(
        item.get("product_description")
        or item.get("product_name")
        or item.get("description")
        or ""
    ).strip()


def _batch_key(item: Dict[str, Any]) -> str:
    af = item.get("additional_fields") if isinstance(item.get("additional_fields"), dict) else {}
    return str(
        item.get("lot_batch_number")
        or item.get("batch")
        or af.get("batch")
        or ""
    ).strip().upper()


def _looks_like_footer_product(name: str) -> bool:
    if not name or not re.search(r"[A-Za-z0-9]", name):
        return True
    if _FOOTER_PRODUCT_RE.search(name):
        return True
    # Pure HSN / tax codes as "product"
    digits = re.sub(r"[^0-9]", "", name)
    if re.fullmatch(r"(?:\d{6}|\d{8})", digits) and len(name.strip()) <= 12:
        return True
    return False


def _qty_looks_like_gst_percent(qty: float, rate: float, amount: float) -> bool:
    if qty not in _GST_PERCENT_VALUES:
        return False
    # If qty is a GST slab and rate×qty is nowhere near amount, treat as suspicious
    if amount <= 0 or rate <= 0:
        return qty in {5.0, 12.0, 18.0, 28.0}
    expected = qty * rate
    if expected <= 0:
        return False
    rel = abs(expected - amount) / max(amount, 1.0)
    return rel > 0.45


def _amount_relationship_ok(qty: float, rate: float, amount: float) -> bool:
    """Allow GST/discount/scheme variance; reject only extreme mismatches."""
    if qty <= 0 or rate <= 0 or amount <= 0:
        return True  # incomplete numeric data — don't reject pharma formats
    expected = qty * rate
    if expected <= 0:
        return True
    rel = abs(expected - amount) / max(abs(amount), abs(expected), 1.0)
    # Wide tolerance: tax-inclusive, free qty, scheme, rounding
    return rel <= 0.55


def validate_page_line_items(
    items: Sequence[Dict[str, Any]],
    *,
    page_number: Optional[int] = None,
) -> PageValidationResult:
    """Validate extracted rows before merging into an invoice."""
    warnings: List[str] = []
    accepted: List[Dict[str, Any]] = []
    rejected: List[Dict[str, Any]] = []

    if not items:
        return PageValidationResult(
            status="ok",
            warnings=["no_line_items"],
            accepted_items=[],
            rejected_items=[],
        )

    suspicious_count = 0
    for raw in items:
        if not isinstance(raw, dict):
            continue
        item = copy.deepcopy(raw)
        name = _product_name(item)
        qty = _to_float(item.get("quantity"))
        rate = _to_float(item.get("unit_price") or item.get("rate"))
        amount = _to_float(
            item.get("total_amount")
            or item.get("gross_amount")
            or (item.get("additional_fields") or {}).get("gross_amount")
        )

        page_warnings: List[str] = []
        reject = False

        if _looks_like_footer_product(name):
            page_warnings.append("footer_or_header_as_product")
            reject = True

        if qty < 0 or rate < 0 or amount < 0:
            page_warnings.append("negative_values")
            reject = True

        if amount >= 50_000_000:  # ₹5 crore line — almost always OCR corruption
            page_warnings.append("unrealistic_gross_amount")
            reject = True

        if _qty_looks_like_gst_percent(qty, rate, amount):
            page_warnings.append("gst_percent_as_quantity")
            suspicious_count += 1
            # Keep row but flag — stockist fixups may correct later; do not
            # silently drop unless amount relationship is also broken.
            if not _amount_relationship_ok(qty, rate, amount):
                reject = True

        if qty > 0 and rate > 0 and amount > 0 and not _amount_relationship_ok(qty, rate, amount):
            page_warnings.append("qty_rate_amount_mismatch")
            suspicious_count += 1

        # Invoice total repeated as a product row
        if re.search(r"(?i)\b(?:invoice\s+total|grand\s+total|net\s+amount)\b", name):
            page_warnings.append("invoice_total_as_product")
            reject = True

        if page_number is not None:
            af = item.get("additional_fields")
            if not isinstance(af, dict):
                af = {}
                item["additional_fields"] = af
            af.setdefault("_source_page", page_number)

        if reject:
            rejected.append(item)
            warnings.extend(page_warnings)
        else:
            if page_warnings:
                warnings.extend(page_warnings)
                suspicious_count += 1
            accepted.append(item)

    if not accepted and rejected:
        status = "invalid"
    elif suspicious_count >= max(1, len(accepted) // 2) and accepted:
        status = "suspicious"
    else:
        status = "ok"

    if page_number is not None and warnings:
        logger.info(
            "[MultipageValidation] page=%s status=%s accepted=%s rejected=%s warnings=%s",
            page_number,
            status,
            len(accepted),
            len(rejected),
            warnings[:12],
        )

    return PageValidationResult(
        status=status,
        warnings=warnings,
        accepted_items=accepted,
        rejected_items=rejected,
    )


def _aggregate_confident_duplicates(
    items: List[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], int]:
    """Aggregate only when name+batch match confidently; never fuzzy-merge names."""
    before = len(items)
    buckets: Dict[Tuple[str, str], Dict[str, Any]] = {}
    order: List[Tuple[str, str]] = []

    for item in items:
        name = re.sub(r"\s+", " ", _product_name(item)).strip().upper()
        if not name:
            key = (f"__anon_{len(order)}", _batch_key(item))
            buckets[key] = copy.deepcopy(item)
            order.append(key)
            continue
        key = (name, _batch_key(item))
        if key not in buckets:
            buckets[key] = copy.deepcopy(item)
            order.append(key)
            continue
        base = buckets[key]
        # Exact same qty+amount → duplicate OCR row, keep one
        b_qty = _to_float(base.get("quantity"))
        i_qty = _to_float(item.get("quantity"))
        b_amt = _to_float(base.get("total_amount") or base.get("gross_amount"))
        i_amt = _to_float(item.get("total_amount") or item.get("gross_amount"))
        if abs(b_qty - i_qty) < 1e-6 and abs(b_amt - i_amt) < 0.05:
            continue
        # Confident same product across pages → sum qty/amount
        new_qty = b_qty + i_qty
        new_amt = b_amt + i_amt
        if new_qty > 0:
            base["quantity"] = str(new_qty if new_qty != int(new_qty) else int(new_qty))
        if new_amt > 0:
            base["total_amount"] = f"{new_amt:.2f}"
        af = base.get("additional_fields")
        if not isinstance(af, dict):
            af = {}
            base["additional_fields"] = af
        sources = af.get("_source_pages")
        if not isinstance(sources, list):
            sources = [af.get("_source_page")] if af.get("_source_page") else []
        i_af = item.get("additional_fields") if isinstance(item.get("additional_fields"), dict) else {}
        if i_af.get("_source_page") is not None:
            sources.append(i_af.get("_source_page"))
        af["_source_pages"] = sorted({s for s in sources if s is not None})

    return [buckets[k] for k in order], before


def extract_items_from_full_data(full_data: Any) -> List[Dict[str, Any]]:
    """Return line items regardless of nested response shape."""
    if not isinstance(full_data, dict):
        return []
    if isinstance(full_data.get("line_items"), list):
        return [x for x in full_data["line_items"] if isinstance(x, dict)]
    if isinstance(full_data.get("line_items"), dict):
        items = full_data["line_items"].get("items", [])
        return [x for x in items if isinstance(x, dict)] if isinstance(items, list) else []
    data = full_data.get("data")
    if isinstance(data, dict):
        if isinstance(data.get("line_items"), list):
            return [x for x in data["line_items"] if isinstance(x, dict)]
        if isinstance(data.get("line_items"), dict):
            items = data["line_items"].get("items", [])
            return [x for x in items if isinstance(x, dict)] if isinstance(items, list) else []
    return []


def merge_items_into_extracted_data(
    base: Optional[Dict[str, Any]],
    items: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Write merged items into a deep-copied base extraction payload."""
    if isinstance(base, dict):
        out = copy.deepcopy(base)
    else:
        out = {"data": {}}
    if "data" not in out or not isinstance(out.get("data"), dict):
        # Flat Gemini shape
        if any(k in out for k in ("invoice_no", "vendor", "customer", "line_items")):
            out = {"data": out}
        else:
            out.setdefault("data", {})
    data = out["data"]
    container = data.get("line_items")
    if isinstance(container, dict):
        container["items"] = list(items)
        container["count"] = len(items)
    else:
        data["line_items"] = {"items": list(items), "count": len(items)}
    return out


def score_extraction_quality(full_data: Any) -> Tuple[int, int, int]:
    """Return (accepted_count, warning_count, rejected_hint) for comparison."""
    items = extract_items_from_full_data(full_data)
    result = validate_page_line_items(items)
    return (
        len(result.accepted_items),
        len(result.warnings),
        len(result.rejected_items),
    )


def combined_result_beats_page_level(
    page_level: Any,
    combined: Any,
) -> bool:
    """Combined OCR may replace page-level only when clearly better and valid.

    If page-level already has usable products, combined must produce *strictly
    more* accepted rows. Equal counts (even when combined is also valid but
    different) keep page-level — never silent replacement of good page data.
    """
    if not combined:
        return False
    p_ok, p_warn, p_rej = score_extraction_quality(page_level)
    c_ok, c_warn, c_rej = score_extraction_quality(combined)
    if c_ok <= 0:
        return False
    # Page-level already usable → combined must strictly improve product count
    if p_ok > 0:
        return c_ok > p_ok
    # Empty/unusable page-level → accept any validated combined result
    return c_ok > 0


def merge_multipage_page_level_extractions(
    *,
    group: Dict[str, Any],
    page_results: Sequence[Any],
    batch_id: Optional[str] = None,
) -> MultipageMergeResult:
    """Merge independent page extractions for a multi-page invoice group.

    Does not call Gemini / OCR. Stockist-specific handlers should run before this.
    """
    pages = list(group.get("pages") or [])
    if len(pages) <= 1:
        return MultipageMergeResult(
            applied=False,
            extracted_data=group.get("extracted_data"),
            needs_combined_fallback=False,
            strategy="single_page",
            before_count=0,
            after_count=0,
        )

    page_audits: List[PageAuditRecord] = []
    collected: List[Dict[str, Any]] = []
    base = group.get("extracted_data")
    if not isinstance(base, dict) and pages:
        first = page_results[pages[0]] if pages[0] < len(page_results) else None
        if isinstance(first, dict):
            base = first.get("full_data")

    invoice_no = group.get("invoice_no")
    usable_pages = 0
    invalid_pages = 0

    for pidx in pages:
        pr = page_results[pidx] if pidx < len(page_results) else None
        if not isinstance(pr, dict):
            continue
        full = pr.get("full_data")
        raw_items = extract_items_from_full_data(full)
        page_no = pidx + 1
        validation = validate_page_line_items(raw_items, page_number=page_no)
        detected = pr.get("invoice_no")
        classification = classify_page_continuity(
            page_index=pidx,
            page_invoice_no=detected,
            current_invoice_no=invoice_no,
            attach_decision=True,  # already grouped together
        )
        if pidx == pages[0]:
            classification = PageClassification.NEW_INVOICE if not normalize_invoice_number(
                detected
            ) else (
                PageClassification.SAME_INVOICE
                if normalize_invoice_number(detected) == normalize_invoice_number(invoice_no)
                else PageClassification.NEW_INVOICE
            )
        elif normalize_invoice_number(detected) is None:
            classification = PageClassification.CONTINUATION_NO_INVOICE
        elif normalize_invoice_number(detected) == normalize_invoice_number(invoice_no):
            classification = PageClassification.SAME_INVOICE

        merged_n = 0
        if validation.is_usable:
            collected.extend(validation.accepted_items)
            merged_n = len(validation.accepted_items)
            usable_pages += 1
        else:
            invalid_pages += 1

        audit = PageAuditRecord(
            page_number=page_no,
            detected_invoice_number=str(detected).strip() if detected else None,
            normalized_invoice_number=normalize_invoice_number(detected),
            previous_invoice_number=str(invoice_no).strip() if invoice_no else None,
            classification=classification,
            validation_status=validation.status,
            validation_warnings=list(validation.warnings),
            number_of_products_extracted=len(raw_items),
            number_of_products_merged=merged_n,
            extraction_strategy_used="page_level",
            fallback_used=False,
        )
        page_audits.append(audit)
        log_page_audit(batch_id=batch_id, audit=audit)

    before = len(collected)
    aggregated, _ = _aggregate_confident_duplicates(collected)
    after = len(aggregated)

    if after <= 0:
        warnings = ["page_level_merge_empty"]
        logger.warning(
            "[MultipageMerge] invoice=%s pages=%s page-level merge produced 0 products "
            "(usable_pages=%s invalid_pages=%s) — combined OCR fallback may run",
            invoice_no,
            [p + 1 for p in pages],
            usable_pages,
            invalid_pages,
        )
        return MultipageMergeResult(
            applied=False,
            extracted_data=base if isinstance(base, dict) else group.get("extracted_data"),
            needs_combined_fallback=True,
            strategy="page_level_empty",
            before_count=before,
            after_count=0,
            page_audits=page_audits,
            warnings=warnings,
        )

    merged_data = merge_items_into_extracted_data(
        base if isinstance(base, dict) else None,
        aggregated,
    )
    group_audit = InvoiceGroupAudit(
        invoice_number=str(invoice_no) if invoice_no else None,
        source_pages=[p + 1 for p in pages],
        total_products_before_aggregation=before,
        total_products_after_aggregation=after,
        validation_status="ok" if invalid_pages == 0 else "partial",
        extraction_strategy_used="page_level_merge",
        fallback_used=False,
        page_audits=page_audits,
    )
    log_invoice_group_audit(batch_id=batch_id, audit=group_audit)

    # Fallback only if most pages were invalid AND we have very few products
    needs_fallback = invalid_pages > 0 and usable_pages == 0
    return MultipageMergeResult(
        applied=True,
        extracted_data=merged_data,
        needs_combined_fallback=needs_fallback,
        strategy="page_level_merge",
        before_count=before,
        after_count=after,
        page_audits=page_audits,
        warnings=[],
    )


def log_page_audit(*, batch_id: Optional[str], audit: PageAuditRecord) -> None:
    logger.info(
        "[MultipageContinuity] batch_id=%s page=%s detected_inv=%s normalized_inv=%s "
        "previous_inv=%s classification=%s validation=%s warnings=%s "
        "products_extracted=%s products_merged=%s strategy=%s fallback=%s",
        batch_id or "-",
        audit.page_number,
        audit.detected_invoice_number or "-",
        audit.normalized_invoice_number or "-",
        audit.previous_invoice_number or "-",
        audit.classification.value,
        audit.validation_status,
        audit.validation_warnings[:8],
        audit.number_of_products_extracted,
        audit.number_of_products_merged,
        audit.extraction_strategy_used,
        audit.fallback_used,
    )


def log_invoice_group_audit(*, batch_id: Optional[str], audit: InvoiceGroupAudit) -> None:
    logger.info(
        "[MultipageInvoiceGroup] batch_id=%s invoice=%s source_pages=%s "
        "products_before_agg=%s products_after_agg=%s validation=%s "
        "strategy=%s fallback=%s",
        batch_id or "-",
        audit.invoice_number or "-",
        audit.source_pages,
        audit.total_products_before_aggregation,
        audit.total_products_after_aggregation,
        audit.validation_status,
        audit.extraction_strategy_used,
        audit.fallback_used,
    )


def apply_controlled_combined_ocr_fallback(
    *,
    group: Dict[str, Any],
    page_level_data: Any,
    combined_data: Any,
    batch_id: Optional[str] = None,
) -> Tuple[Any, bool]:
    """Return (chosen_data, used_combined). Never overwrite better page-level."""
    if not combined_data:
        return page_level_data, False
    if combined_result_beats_page_level(page_level_data, combined_data):
        logger.info(
            "[MultipageFallback] batch_id=%s invoice=%s accepted controlled combined OCR "
            "(beat page-level after validation)",
            batch_id or "-",
            group.get("invoice_no"),
        )
        return combined_data, True
    logger.info(
        "[MultipageFallback] batch_id=%s invoice=%s rejected combined OCR "
        "(keeping page-level merge)",
        batch_id or "-",
        group.get("invoice_no"),
    )
    return page_level_data, False
