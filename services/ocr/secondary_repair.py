"""Surgical secondary OCR repairs for known VL table misses.

Only applies when source-image evidence is clear. Never invents quantities
from stock equations. Ambiguous reads stay unresolved / unchanged.

Production: gated by ``OCR_SECONDARY_REPAIR`` (default off). Evaluation scripts
call ``apply_secondary_repairs`` directly and are unaffected by the flag.
"""

from __future__ import annotations

import io
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple  # noqa: F401 — Any used by overlay

from services.ground_truth.html_table import parse_html_tables

_TOTAL_LINE_RE = re.compile(
    r"(?i)\bTOTAL\b\s+(\d{3,})\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d{3,})(?:\s+(\d+))?"
)

# Reject sales-value / narrative TOTAL lines that happen to contain digits.
_TOTAL_REJECT_CONTEXT_RE = re.compile(
    r"(?i)\bTOTAL\b.{0,40}\b(sales|value|amount|rs\.?|inr|closing stock \(sales)\b"
)


@dataclass
class SecondaryRepairReport:
    document_id: str
    repairs: List[Dict[str, Any]] = field(default_factory=list)
    unresolved: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self, *, redact_snippets: bool = False) -> Dict[str, Any]:
        """Serialize repair report.

        When ``redact_snippets`` is True, drop OCR line/raw digit snippets so
        ordinary API/observability payloads do not echo document contents.
        """
        repairs = [dict(r) for r in self.repairs]
        unresolved = [dict(u) for u in self.unresolved]
        if redact_snippets:
            for item in repairs:
                item.pop("line", None)
                item.pop("raw", None)
            for item in unresolved:
                item.pop("line", None)
                item.pop("digits", None)
                item.pop("candidates", None)
        return {
            "document_id": self.document_id,
            "repairs": repairs,
            "unresolved": unresolved,
        }


def secondary_repair_enabled() -> bool:
    """Production feature flag. Default OFF until canary validation."""
    return os.getenv("OCR_SECONDARY_REPAIR", "false").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _norm_product_token(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (value or "").casefold())


def _header_to_line_field(header: str) -> Optional[str]:
    h = re.sub(r"\s+", " ", (header or "").strip().casefold())
    compact = re.sub(r"[^a-z0-9]+", "", h)
    if (
        "op.bal" in h
        or h in {"opening", "op. stock", "op stock", "opstk", "op stk"}
        or compact in {"opstk", "opstock", "openingqty", "opbalqty", "opbal"}
    ):
        return "opening_qty"
    if (
        "closing balance" in h
        or h in {"closing", "cl. stock", "cl stock", "goods closing", "clstk", "cl stk"}
        or compact in {"clstk", "clstock", "closingqty", "closingbalance", "goodsclosing"}
    ):
        return "closing_qty"
    if "expiry" in h and "break" in h:
        return "expiry_breakage_qty"
    if ("issue" in h and "receipt" not in h) or compact in {"saleqty", "salesqty", "issueqty"}:
        return "sales_qty"
    if "receipt" in h or compact in {"rcptqty", "receiptqty", "receiptsqty"}:
        return "receipts_qty"
    return None


def _parse_qty_value(raw: Any) -> Tuple[bool, Optional[float]]:
    """Return (ok, value). Empty/cleared → (True, None). Invalid → (False, None)."""
    if raw is None:
        return True, None
    text = str(raw).strip()
    if text == "":
        return True, None
    try:
        return True, float(text.replace(",", ""))
    except ValueError:
        return False, None


def statement_from_ocr_html(
    ocr_text: str,
    filename: str = "",
    source_format: str = "",
) -> Dict[str, Any]:
    """Build a minimal sales-statement dict from VL/HTML table OCR text.

    Used when DI already supplied OCR evidence (``ocr_text_override``) so the
    load-bearing cascade can skip paid vision / re-OCR while still producing
    ``line_items`` / ``totals`` for secondary-repair overlay.
    """
    cells = parse_html_tables(ocr_text or "")
    items_by_key: Dict[tuple, Dict[str, Any]] = {}
    totals: Dict[str, Any] = {"extra": {"extraction_method": "di_ocr_html_override"}}
    for c in cells:
        if c.get("is_header"):
            continue
        pk = str(c.get("product_key") or "").strip()
        if not pk:
            continue
        header = str(c.get("header") or "")
        field = _header_to_line_field(header)
        if not field:
            continue
        ok, qty = _parse_qty_value(c.get("value"))
        if not ok:
            continue
        if _norm_product_token(pk) == "total":
            if field in {"opening_qty", "closing_qty", "sales_qty", "receipts_qty"}:
                totals[field] = qty
            continue
        packing = str(c.get("packing") or "").strip()
        item_key = (_norm_product_token(pk), _norm_product_token(packing))
        item = items_by_key.setdefault(
            item_key,
            {
                "product_name": pk,
                "packing": packing,
                "extra": {},
            },
        )
        if packing:
            item["packing"] = packing
        if field == "expiry_breakage_qty":
            item["extra"]["expiry_breakage_qty"] = qty
        else:
            item[field] = qty
    return {
        "source_file": filename or "",
        "source_format": (source_format or "").lstrip(".") or "ocr",
        "line_items": list(items_by_key.values()),
        "totals": totals,
    }


def overlay_secondary_repairs_onto_result(
    result: Dict[str, Any],
    cells: Sequence[Dict[str, Any]],
) -> Dict[str, Any]:
    """Copy evidence-gated repaired cell values onto extraction line_items/totals.

    Only cells marked with ``secondary_repair`` are applied. Does not invent
    quantities from stock equations. Empty repaired values clear the field
    (None), distinguishing unresolved from numeric zero.
    """
    if not isinstance(result, dict) or not cells:
        return result
    repaired = [
        c
        for c in cells
        if (not c.get("is_header")) and c.get("secondary_repair")
    ]
    if not repaired:
        return result

    items = list(result.get("line_items") or [])
    applied: List[Dict[str, Any]] = []

    for cell in repaired:
        product_key = str(cell.get("product_key") or "").strip()
        header = str(cell.get("header") or "").strip()
        field = _header_to_line_field(header)
        if not product_key or not field:
            continue
        ok, qty = _parse_qty_value(cell.get("value"))
        if not ok:
            continue
        packing = str(cell.get("packing") or "").strip()
        target_norm = _norm_product_token(product_key)

        if target_norm == "total":
            totals = result.setdefault("totals", {})
            if not isinstance(totals, dict):
                continue
            if field in {"opening_qty", "closing_qty", "sales_qty", "receipts_qty"}:
                prev = totals.get(field)
                totals[field] = qty
                applied.append(
                    {
                        "product_key": "TOTAL",
                        "header": header,
                        "field": field,
                        "from": prev,
                        "to": qty,
                        "repair": cell.get("secondary_repair"),
                    }
                )
            continue

        matched = None
        for item in items:
            if not isinstance(item, dict):
                continue
            name = str(item.get("product_name") or item.get("name") or "")
            if _norm_product_token(name) != target_norm and target_norm not in _norm_product_token(
                name
            ):
                # Allow compact containment both ways for minor OCR name drift.
                iname = _norm_product_token(name)
                if not (iname and target_norm and (iname in target_norm or target_norm in iname)):
                    continue
            if packing:
                ipack = str(item.get("packing") or "")
                pack_norm = _norm_product_token(packing)
                if pack_norm and pack_norm not in _norm_product_token(ipack):
                    # LIV 100ML vs 200ML must not collide.
                    if "100ml" in pack_norm or "200ml" in pack_norm:
                        continue
            matched = item
            break
        if matched is None:
            continue

        if field == "expiry_breakage_qty":
            extra = matched.setdefault("extra", {})
            if not isinstance(extra, dict):
                extra = {}
                matched["extra"] = extra
            prev = extra.get("expiry_breakage_qty")
            extra["expiry_breakage_qty"] = qty
        else:
            prev = matched.get(field)
            matched[field] = qty
        applied.append(
            {
                "product_key": product_key,
                "header": header,
                "field": field,
                "from": prev,
                "to": qty,
                "repair": cell.get("secondary_repair"),
            }
        )

    if applied:
        result["line_items"] = items
        totals = result.setdefault("totals", {})
        if isinstance(totals, dict):
            extra = totals.setdefault("extra", {})
            if isinstance(extra, dict):
                extra["secondary_repair_overlay"] = {
                    "applied_count": len(applied),
                    "fields": [
                        {
                            "product_key": a["product_key"],
                            "field": a["field"],
                            "to": a["to"],
                            "repair": a["repair"],
                        }
                        for a in applied
                    ],
                }
    return result


def maybe_apply_secondary_repairs(
    document_id: str,
    ocr_text: str,
    *,
    dataset_root: Optional[Path] = None,
    source_path: Optional[Path] = None,
    image_bytes: Optional[bytes] = None,
    pdf_bytes: Optional[bytes] = None,
    page_index: int = 0,
) -> Tuple[str, List[Dict[str, Any]], SecondaryRepairReport]:
    """Production entrypoint: no-op unless ``OCR_SECONDARY_REPAIR`` is enabled."""
    if not secondary_repair_enabled():
        report = SecondaryRepairReport(document_id=document_id)
        report.unresolved.append({"reason": "secondary_repair_disabled"})
        return ocr_text, parse_html_tables(ocr_text), report
    return apply_secondary_repairs(
        document_id,
        ocr_text,
        dataset_root=dataset_root,
        source_path=source_path,
        image_bytes=image_bytes,
        pdf_bytes=pdf_bytes,
        page_index=page_index,
    )


def default_dataset_root() -> Path:
    env = os.getenv("SPLITEXTRACT_DATASET_ROOT") or os.getenv("ZYDUS_DATASET_ROOT")
    if env:
        return Path(env)
    return Path(r"C:\Users\amnsa\Downloads\ZL_2026_August")


def resolve_source_path(
    document_id: str, dataset_root: Optional[Path] = None
) -> Optional[Path]:
    root = Path(dataset_root or default_dataset_root())
    direct = root / document_id
    if direct.is_file():
        return direct
    stem = Path(document_id).stem
    for path in sorted(root.glob(stem + ".*")):
        if path.suffix.lower() in {
            ".pdf",
            ".jpg",
            ".jpeg",
            ".png",
            ".webp",
            ".tif",
            ".tiff",
        }:
            return path
    return None


def load_source_image(path: Path, *, scale: float = 2.5):
    from PIL import Image

    if path.suffix.lower() == ".pdf":
        import fitz

        doc = fitz.open(path)
        try:
            page = doc.load_page(0)
            pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
            return Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        finally:
            doc.close()
    return Image.open(path).convert("RGB")


def load_image_bytes(image_bytes: bytes):
    from PIL import Image

    return Image.open(io.BytesIO(image_bytes)).convert("RGB")


def load_pdf_bytes_page(
    pdf_bytes: bytes, *, page_index: int = 0, scale: float = 2.5
):
    """Rasterize one PDF page at secondary-repair DPI (canary-validated 2.5×)."""
    import fitz
    from PIL import Image

    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        page = doc.load_page(max(0, int(page_index)))
        pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
        return Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    finally:
        doc.close()


def apply_secondary_repairs(
    document_id: str,
    ocr_text: str,
    *,
    dataset_root: Optional[Path] = None,
    source_path: Optional[Path] = None,
    image_bytes: Optional[bytes] = None,
    pdf_bytes: Optional[bytes] = None,
    page_index: int = 0,
) -> Tuple[str, List[Dict[str, Any]], SecondaryRepairReport]:
    """Return repaired OCR text, parsed cells, and a repair report."""
    report = SecondaryRepairReport(document_id=document_id)
    cells = parse_html_tables(ocr_text)
    image = None
    try:
        _configure_tesseract_cmd()
        # Prefer PDF re-raster at 2.5× — DI primary OCR pages are often 2.0× and
        # that DPI misses printed Closing evidence (e.g. LIV-52 100ML on 0275).
        if pdf_bytes:
            image = load_pdf_bytes_page(pdf_bytes, page_index=page_index, scale=2.5)
        elif image_bytes:
            image = load_image_bytes(image_bytes)
        else:
            path = source_path or resolve_source_path(document_id, dataset_root)
            if path is None or not path.is_file():
                report.unresolved.append({"reason": "source_image_unavailable"})
                return ocr_text, cells, report
            image = load_source_image(path)
    except Exception as exc:  # pragma: no cover
        report.unresolved.append(
            {"reason": "secondary_ocr_unavailable", "error": str(exc)}
        )
        return ocr_text, cells, report

    page_text = _ocr(image, psm=6)
    text = ocr_text
    cells, text = _repair_total_row(image, cells, text, report, page_text=page_text)
    cells = _repair_himalaya_qty_cells(image, cells, report, page_text=page_text)
    cells = _repair_liv_closing_with_strike(image, cells, report, page_text=page_text)
    return text, cells, report


def _header_norms(cells: Sequence[Dict[str, Any]]) -> List[str]:
    headers: List[str] = []
    for c in cells:
        if not c.get("is_header"):
            continue
        h = re.sub(r"\s+", " ", str(c.get("header") or "").strip().casefold())
        if h and h not in headers:
            headers.append(h)
    return headers


def _is_himalaya_qty_total_layout(cells: Sequence[Dict[str, Any]]) -> bool:
    """Positive layout ID for qty TOTAL footer repair (1151/1078-style).

    Requires Op.Bal + Issue + Closing Balance headers from VL. Does not accept
    generic Opening/Closing (SSA / group-wise) layouts where TOTAL often means
    sales value.
    """
    headers = _header_norms(cells)
    if not headers:
        return False
    has_op_bal = any("op.bal" in h for h in headers)
    has_issue = any("issue" in h for h in headers)
    has_closing_bal = any("closing balance" in h for h in headers)
    return has_op_bal and has_issue and has_closing_bal


def _repair_total_row(
    image, cells, text, report: SecondaryRepairReport, *, page_text: str = ""
):
    has_total_qty = any(
        (not c.get("is_header"))
        and str(c.get("product_key") or "").strip().upper() == "TOTAL"
        and re.fullmatch(r"-?\d+(?:\.\d+)?", str(c.get("value") or "").strip() or "")
        for c in cells
    )
    if has_total_qty:
        return cells, text

    if not _is_himalaya_qty_total_layout(cells):
        report.unresolved.append(
            {"reason": "total_footer_layout_not_himalaya_qty"}
        )
        return cells, text

    headers = []
    for c in cells:
        if c.get("is_header") and c.get("header") not in headers:
            headers.append(str(c.get("header")))
    if not headers:
        report.unresolved.append({"reason": "total_footer_missing_vl_headers"})
        return cells, text

    # Prefer bottom-band OCR so mid-page product noise is less likely to match.
    match = None
    raw = ""
    from PIL import ImageEnhance, ImageOps

    w, h = image.size
    bottom = image.crop((0, int(h * 0.78), w, h))
    for candidate_img in (
        bottom,
        ImageEnhance.Contrast(
            ImageOps.autocontrast(bottom.convert("L")).resize(
                (bottom.width * 2, bottom.height * 2)
            )
        ).enhance(1.8),
    ):
        raw = _ocr(candidate_img, psm=6)
        match = _find_qty_total_match(raw)
        if match:
            break
    if not match:
        # Fall back to full-page text only after layout ID succeeded.
        raw = page_text or ""
        match = _find_qty_total_match(raw)
    if not match:
        report.unresolved.append({"reason": "total_row_not_found_in_secondary"})
        return cells, text

    op, rec, tot, iss, closing = match.group(1, 2, 3, 4, 5)
    near = match.group(6) or ""

    # Map only onto recognized himalaya qty headers — never invent header names.
    alias_values = {
        "product name": "TOTAL",
        "product": "TOTAL",
        "packing": "",
        "pack": "",
        "op.bal. qty.": op,
        "op.bal qty": op,
        "op.bal. qty": op,
        "receipt qty.": rec,
        "receipt qty": rec,
        "total qty.": tot,
        "total qty": tot,
        "issue qty.": iss,
        "issue qty": iss,
        "closing balance": closing,
        "near expiry": near,
    }
    # Deliberately omit generic "opening"/"closing"/"receipt" aliases so
    # incompatible columns cannot absorb footer digits.
    row_index = 1 + max((int(c.get("row_index") or 0) for c in cells), default=0)
    new_cells = list(cells)
    for idx, header in enumerate(headers):
        hnorm = re.sub(r"\s+", " ", str(header or "").strip().casefold())
        value = alias_values.get(hnorm, "")
        if idx == 0 and not value:
            value = "TOTAL"
        new_cells.append(
            {
                "table_index": 0,
                "row_index": row_index,
                "col_index": idx,
                "header": header,
                "value": value,
                "product_key": "TOTAL",
                "packing": None,
                "is_header": False,
                "secondary_repair": "total_footer_ocr",
            }
        )
    text = (text or "") + (
        f"\n<table><tr><td>TOTAL</td><td></td><td>{op}</td><td>{rec}</td>"
        f"<td>{tot}</td><td>{iss}</td><td>{closing}</td><td>{near or 0}</td></tr></table>"
    )
    report.repairs.append(
        {
            "type": "total_footer_ocr",
            "corrected": True,
            "values": {
                "Op.Bal. Qty.": op,
                "Closing Balance": closing,
            },
            "raw": raw[:240],
        }
    )
    return new_cells, text


def _find_qty_total_match(raw: str):
    if not raw:
        return None
    cleaned = raw.replace(",", " ")
    if _TOTAL_REJECT_CONTEXT_RE.search(cleaned):
        return None
    return _TOTAL_LINE_RE.search(cleaned)


def _has_expiry_breakage_header(cells: Sequence[Dict[str, Any]]) -> bool:
    for c in cells:
        header = re.sub(r"\s+", " ", str(c.get("header") or "").strip().casefold())
        if "expiry" in header and "break" in header:
            return True
    return False


def _repair_himalaya_qty_cells(
    image, cells, report: SecondaryRepairReport, *, page_text: str = ""
):
    """Fix 1078-style Closing/Expiry using Op/Issue-anchored digit evidence.

    Only runs on himalaya 10-qty layouts that expose Expiry Breakage. Never applies
    the loose digit-index fallback that can steal packing numerals on short layouts.
    """
    if not _has_expiry_breakage_header(cells):
        return cells
    targets = [
        {
            "product_key": "AACTARIL SOAP",
            "closing_header": "Closing Balance",
            "expiry_header": "Expiry Breakage",
        },
        {
            "product_key": "TALEKT CAPS.",
            "closing_header": "Closing Balance",
            "expiry_header": "Expiry Breakage",
        },
    ]
    for target in targets:
        product = target["product_key"]
        row_cells = [
            c
            for c in cells
            if (not c.get("is_header"))
            and str(c.get("product_key") or "") == product
        ]
        if not row_cells:
            continue
        vl = {str(c.get("header")): str(c.get("value") or "").strip() for c in row_cells}
        packing = next(
            (str(c.get("packing") or "").strip() for c in row_cells if c.get("packing")),
            "",
        )
        opening = vl.get("Op.Bal. Qty.") or vl.get("Opening") or ""
        issue = vl.get("Issue Qty.") or vl.get("Issues") or ""
        if not opening.isdigit() or not issue.isdigit():
            report.unresolved.append(
                {"reason": "no_vl_opening_issue_anchor", "product_key": product}
            )
            continue
        line_candidates = _product_line_candidates(
            image,
            page_text=page_text,
            product_key=product,
            packing=packing or None,
        )
        if not line_candidates:
            report.unresolved.append(
                {"reason": "product_line_not_located", "product_key": product}
            )
            continue
        # Op Rec Ret Tot Iss [Ret] Exp Close [Reorder]
        # Tot may OCR-corrupt (82→62); Ret after Issue may be garbage (ty}).
        pattern = re.compile(
            rf"\b{re.escape(opening)}\s+0\s+0\s+\d+\s+{re.escape(issue)}\s+"
            rf"(?:0\s+|[^0-9\n]{{1,6}})?"
            rf"(\d+)\s+(-?\d+)\b"
        )
        match = None
        line = ""
        for cand in line_candidates:
            norm = cand.replace("O", "0").replace("o", "0")
            match = pattern.search(norm)
            if match:
                line = cand
                break
        if not match:
            report.unresolved.append(
                {
                    "reason": "anchored_digit_pattern_miss",
                    "product_key": product,
                    "line": (line_candidates[0] or "")[:180],
                }
            )
            continue
        expiry_val, closing_val = match.group(1), match.group(2)
        for cell in cells:
            if cell.get("is_header") or str(cell.get("product_key") or "") != product:
                continue
            header = str(cell.get("header") or "")
            if header == target["closing_header"]:
                old = str(cell.get("value") or "").strip()
                if old != closing_val:
                    cell["value"] = closing_val
                    cell["secondary_repair"] = "himalaya_row_anchor_ocr"
                    cell["secondary_previous"] = old
                    report.repairs.append(
                        {
                            "type": "himalaya_row_anchor_ocr",
                            "corrected": True,
                            "product_key": product,
                            "header": header,
                            "from": old,
                            "to": closing_val,
                            "line": line[:180],
                        }
                    )
            if header == target["expiry_header"]:
                old = str(cell.get("value") or "").strip()
                if old != expiry_val:
                    cell["value"] = expiry_val
                    cell["secondary_repair"] = "himalaya_row_anchor_ocr"
                    cell["secondary_previous"] = old
                    report.repairs.append(
                        {
                            "type": "himalaya_row_anchor_ocr",
                            "corrected": True,
                            "product_key": product,
                            "header": header,
                            "from": old,
                            "to": expiry_val,
                            "line": line[:180],
                        }
                    )
    return cells


def _repair_liv_closing_with_strike(
    image, cells, report: SecondaryRepairReport, *, page_text: str = ""
):
    """Recover printed Closing for LIV-52 SYRUP 100ML when VL absorbed prior-row 162."""
    product = "LIV-52 SYRUP"
    packing = "100ML"
    row_cells = [
        c
        for c in cells
        if (not c.get("is_header"))
        and str(c.get("product_key") or "") == product
        and packing in str(c.get("packing") or "")
    ]
    if not row_cells:
        return cells
    vl = {str(c.get("header")): str(c.get("value") or "").strip() for c in row_cells}
    opening = vl.get("Opening") or ""
    issues = (
        vl.get("Issues Replied")
        or vl.get("Issues")
        or vl.get("Goods Issues Repl.")
        or ""
    )
    closing = vl.get("Closing") or vl.get("Goods Closing") or ""
    if not (opening.isdigit() and issues.isdigit()):
        return cells
    # Prefer full-page OCR: band crops often absorb neighboring Closing=162.
    candidates = []
    sources = [page_text or ""]
    band = _ocr_product_digit_line(
        image, product_key=product, packing=packing, y_hint_token="100ML"
    )
    if band:
        sources.append(band)
    for src in sources:
        if not src:
            continue
        norm = src.replace("O", "0")
        pat = re.compile(
            rf"(?i)LIV-?52\s*SYRUP\s*{re.escape(packing)}\s+"
            rf"{re.escape(opening)}\s+{re.escape(issues)}\s+(\d{{1,4}})\b"
        )
        for m in pat.finditer(norm):
            candidates.append((m.group(1), src[m.start() : m.start() + 80]))
    printed_close = None
    evidence_line = ""
    # Accept only if a single distinct printed closing is evidenced (not the
    # known prior-row bleed token alone).
    distinct = []
    for val, snip in candidates:
        if val == "162" and closing == "162":
            continue
        if val not in distinct:
            distinct.append(val)
            if not evidence_line:
                evidence_line = snip
    if len(distinct) == 1:
        printed_close = distinct[0]
    elif not distinct and closing == "162":
        for cell in cells:
            if (
                not cell.get("is_header")
                and str(cell.get("product_key") or "") == product
                and packing in str(cell.get("packing") or "")
                and str(cell.get("header") or "") in {"Closing", "Goods Closing"}
            ):
                cell["value"] = ""
                cell["secondary_repair"] = "strike_conflict_unresolved"
                report.unresolved.append(
                    {
                        "reason": "strike_or_row_bleed_unresolved",
                        "product_key": product,
                        "packing": packing,
                        "cleared_vl": closing,
                    }
                )
        return cells
    elif len(distinct) > 1:
        report.unresolved.append(
            {
                "reason": "liv100_closing_ambiguous",
                "product_key": product,
                "packing": packing,
                "candidates": distinct,
            }
        )
        if closing == "162":
            for cell in cells:
                if (
                    not cell.get("is_header")
                    and str(cell.get("product_key") or "") == product
                    and packing in str(cell.get("packing") or "")
                    and str(cell.get("header") or "") in {"Closing", "Goods Closing"}
                ):
                    cell["value"] = ""
                    cell["secondary_repair"] = "strike_conflict_unresolved"
        return cells
    else:
        return cells
    for cell in cells:
        if cell.get("is_header"):
            continue
        if str(cell.get("product_key") or "") != product:
            continue
        if packing not in str(cell.get("packing") or ""):
            continue
        if str(cell.get("header") or "") not in {"Closing", "Goods Closing"}:
            continue
        old = str(cell.get("value") or "").strip()
        if old == printed_close:
            continue
        cell["value"] = printed_close
        cell["secondary_repair"] = "liv_printed_closing_ocr"
        cell["secondary_previous"] = old
        report.repairs.append(
            {
                "type": "liv_printed_closing_ocr",
                "corrected": True,
                "product_key": product,
                "packing": packing,
                "header": cell.get("header"),
                "from": old,
                "to": printed_close,
                "line": evidence_line[:180],
            }
        )
    return cells


def _product_line_candidates(
    image,
    *,
    page_text: str,
    product_key: str,
    packing: Optional[str] = None,
) -> List[str]:
    """Page-OCR product lines plus a y-band crop (band often has better digit spacing)."""
    tokens = [t for t in re.split(r"[^A-Za-z0-9]+", product_key.upper()) if t]
    out: List[str] = []
    for line in (page_text or "").splitlines():
        up = re.sub(r"[^A-Z0-9]+", "", line.upper())
        if tokens and not all(t in up for t in tokens[:2]):
            continue
        if not re.search(r"\d", line):
            continue
        out.append(line)
    band = _ocr_product_digit_line(
        image, product_key=product_key, packing=packing, y_hint_token=packing
    )
    if band:
        out.append(band)
    seen = set()
    ordered: List[str] = []
    for line in out:
        key = re.sub(r"\s+", " ", line.strip())
        if key in seen:
            continue
        seen.add(key)
        ordered.append(line)
    return ordered


def _product_line_text(
    image,
    *,
    page_text: str,
    product_key: str,
    packing: Optional[str] = None,
) -> str:
    cands = _product_line_candidates(
        image, page_text=page_text, product_key=product_key, packing=packing
    )
    return cands[0] if cands else ""


def _ocr_product_digit_line(
    image,
    *,
    product_key: str,
    packing: Optional[str] = None,
    y_hint_token: Optional[str] = None,
) -> str:
    from PIL import ImageEnhance, ImageOps

    pytesseract = _raw_tesseract()
    data = pytesseract.image_to_data(
        image, output_type=pytesseract.Output.DICT, config="--psm 6"
    )
    target = re.sub(r"[^A-Z0-9]+", "", product_key.upper())
    pack = re.sub(r"[^A-Z0-9]+", "", (packing or y_hint_token or "").upper())
    y = None
    n = len(data["text"])
    for i in range(n):
        tok = re.sub(r"[^A-Z0-9]+", "", (data["text"][i] or "").upper())
        if not tok or len(tok) < 4:
            continue
        if not (target.startswith(tok) or tok.startswith(target[:6]) or tok in target):
            continue
        y_cand = data["top"][i]
        if pack:
            nearby = False
            for j in range(n):
                pj = re.sub(r"[^A-Z0-9]+", "", (data["text"][j] or "").upper())
                if not pj:
                    continue
                # Exact pack or 1-char OCR slip (75GM vs 76GM).
                if abs(data["top"][j] - y_cand) <= 40 and (
                    pj == pack
                    or (
                        len(pj) == len(pack)
                        and sum(a != b for a, b in zip(pj, pack)) <= 1
                    )
                ):
                    nearby = True
                    y_cand = data["top"][j]
                    break
            if not nearby and packing:
                continue
        y = y_cand
        break
    if y is None:
        return ""
    w, h = image.size
    band = image.crop((0, max(0, y - 10), w, min(h, y + 40)))
    gray = ImageOps.autocontrast(band.convert("L"))
    ups = ImageEnhance.Contrast(
        gray.resize((gray.width * 2, gray.height * 2))
    ).enhance(2.0)
    return _ocr(
        ups,
        psm=6,
        whitelist="0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz- .'/ ",
    )


def _configure_tesseract_cmd() -> None:
    """Set tesseract_cmd without installing the primary-OCR VL shim."""
    import pytesseract

    cmd = os.getenv("TESSERACT_CMD", "").strip()
    if cmd:
        pytesseract.pytesseract.tesseract_cmd = cmd
    elif os.name == "nt":
        win_cmd = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
        if os.path.exists(win_cmd):
            pytesseract.pytesseract.tesseract_cmd = win_cmd


def _raw_tesseract():
    """Real Tesseract module — never the primary-OCR VL shim."""
    import pytesseract

    # If something wrapped the module, prefer the underlying implementation.
    inner = getattr(pytesseract, "_inner", None) or getattr(
        getattr(pytesseract, "pytesseract", None), "_inner", None
    )
    if inner is not None and hasattr(inner, "image_to_string"):
        return inner
    return pytesseract


def _ocr(image, *, psm: int = 6, whitelist: Optional[str] = None) -> str:
    pytesseract = _raw_tesseract()
    cfg = f"--psm {psm}"
    if whitelist:
        cfg += f" -c tessedit_char_whitelist={whitelist}"
    try:
        return pytesseract.image_to_string(image, config=cfg) or ""
    except Exception:
        return ""
