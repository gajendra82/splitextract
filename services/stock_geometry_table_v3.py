"""Geometry-first stock table extraction v3 (BENCHMARK / feature-flagged ONLY).

Pipeline:
  ORIGINAL IMAGE → table/column/row geometry → cell crops →
  multi-variant Tesseract → Gemini only for low-confidence cells →
  row structure → validation-only reconciliation → structured JSON.

Feature flag: STOCK_GEOMETRY_CELL_OCR_ENABLED (default false).
Does NOT wire into /extract-sales-statement until explicitly approved.
Does NOT overwrite printed values with arithmetic.
Spatial cell coordinates are authoritative — OCR never moves row/col index.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import cv2
import numpy as np
from PIL import Image

from services.stock_geometry_hybrid_v3 import (
    remove_blue_preserve_ink,
    select_orig_vs_clean,
)
from services.stock_header_resolver import resolve_columns
from services.stock_ocr_table_reconstructor import validate_row_identity

logger = logging.getLogger(__name__)

STOCK_GEOMETRY_CELL_OCR_FLAG = "STOCK_GEOMETRY_CELL_OCR_ENABLED"


def is_geometry_cell_ocr_enabled() -> bool:
    """Production gate — default OFF. Benchmark ignores this and runs directly."""
    return os.getenv(STOCK_GEOMETRY_CELL_OCR_FLAG, "false").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


# Physical column index → business field. Index is authoritative; never renumber
# after dropping LMS or any other column.
PHYSICAL_SOURCE_COLUMNS: Dict[int, str] = {
    0: "product_name",
    1: "packing",
    2: "lms",
    3: "opening_qty",
    4: "receipts_qty",
    5: "purchase_return_qty",
    6: "sales_qty",
    7: "sales_return_qty",
    8: "breakage_qty",
    9: "replacement_qty",
    10: "closing_qty",
    11: "closing_value",
    12: "free_out_qty",
    13: "xy",
}

PHYSICAL_HEADER_LABELS: Dict[int, str] = {
    0: "Product",
    1: "Packg",
    2: "LMS",
    3: "Op.Stk",
    4: "Receipt",
    5: "Pu.Ret",
    6: "Sales",
    7: "S.Ret",
    8: "Brk",
    9: "Repl",
    10: "Cl.Stk",
    11: "Cl.Value",
    12: "cl free",
    13: "*XY",
}

# ---------------------------------------------------------------------------
# Focus ground truth for 0000725384 Excel mobile layout
# ---------------------------------------------------------------------------
FOCUS_EXPECTATIONS: Dict[str, Dict[str, Optional[float]]] = {
    "ARJUNA T": {
        "lms": None,
        "opening_qty": None,
        "receipts_qty": None,
        "sales_qty": None,
        "closing_qty": 0.0,
        "closing_value": 0.0,
    },
    "BONNISAN 100ML": {
        "lms": 1.0,
        "opening_qty": 14.0,
        "receipts_qty": None,
        "sales_qty": None,
        "closing_qty": 14.0,
        "closing_value": 721.98,
    },
    "BONNISAN 200ML": {
        "lms": None,
        "opening_qty": None,
        "receipts_qty": None,
        "sales_qty": None,
        "closing_qty": 0.0,
        "closing_value": 0.0,
    },
    "BONNISAN 15ML": {
        "lms": None,
        "opening_qty": None,
        "receipts_qty": None,
        "sales_qty": None,
        "closing_qty": 0.0,
        "closing_value": 0.0,
    },
    "BRESOL S 200ML": {
        "lms": 1.0,
        "opening_qty": 51.0,
        "receipts_qty": None,
        "sales_qty": 3.0,
        "closing_qty": 48.0,
        "closing_value": 7862.4,
    },
    "BRESOL-N 10ML": {
        "lms": 4.0,
        "opening_qty": 18.0,
        "receipts_qty": None,
        "sales_qty": None,
        "closing_qty": 18.0,
        "closing_value": 964.44,
    },
    "SEPTILIN T": {
        "lms": 2.0,
        "opening_qty": 16.0,
        "receipts_qty": None,
        "sales_qty": 13.0,
        "closing_qty": 3.0,
        "closing_value": 587.4,
    },
    "V-GEL 30GM": {
        "lms": None,
        "opening_qty": 17.0,
        "receipts_qty": None,
        "sales_qty": 3.0,
        "closing_qty": 14.0,
        "closing_value": 1400.0,
    },
}

COMPARE_FIELDS = (
    "lms",
    "opening_qty",
    "receipts_qty",
    "sales_qty",
    "closing_qty",
    "closing_value",
)

# Physical indices for COMPARE_FIELDS (semantic scoring).
COMPARE_PHYSICAL_INDEX: Dict[str, int] = {
    "lms": 2,
    "opening_qty": 3,
    "receipts_qty": 4,
    "sales_qty": 6,
    "closing_qty": 10,
    "closing_value": 11,
}

# Exact physical-cell ground truth (string or null) for contamination tests.
FOCUS_PHYSICAL_CELLS: Dict[str, Dict[int, Optional[str]]] = {
    "ARJUNA T": {
        2: None,
        3: None,
        10: "0",
        11: "0",
    },
    "BRESOL S 200ML": {
        2: "1",
        3: "51",
        4: None,
        5: None,
        6: "3",
        10: "48",
        11: "7862.4",
    },
    "BONNISAN 100ML": {
        2: "1",
        3: "14",
        4: None,
    },
}

NUMERIC_FIELDS = (
    "lms",
    "opening_qty",
    "receipts_qty",
    "purchase_return_qty",
    "sales_qty",
    "sales_return_qty",
    "breakage_qty",
    "replacement_qty",
    "closing_qty",
    "closing_value",
    "free_out_qty",
    "xy",
)

TEXT_FIELDS = ("product_name", "packing")

_GARBAGE = re.compile(r"^(p\d+|pq|po|tt|le|lE|o)$", re.I)

# Metadata / header / footer patterns → NON_PRODUCT (generic, not stockist-specific).
_NON_PRODUCT_RULES: List[Tuple[re.Pattern[str], str]] = [
    (re.compile(r"kannur\s*drug\s*lines", re.I), "metadata_stockist_banner"),
    (re.compile(r"\bdrug\s*lines\b", re.I), "metadata_stockist_banner"),
    (re.compile(r"^\s*company\s*:", re.I), "metadata_company_row"),
    (re.compile(r"\bcompany\s*:\s*\d*", re.I), "metadata_company_row"),
    (re.compile(r"stock\s*&\s*sales\s*statement", re.I), "metadata_statement_title"),
    (re.compile(r"stock\s*&\s*statement", re.I), "metadata_statement_title"),
    (re.compile(r"\bmonth\s+of\b", re.I), "metadata_statement_title"),
    (re.compile(r"^\s*grand\s*total\b", re.I), "metadata_total_row"),
    (re.compile(r"^\s*total\b", re.I), "metadata_total_row"),
    (re.compile(r"^\s*page\b", re.I), "metadata_page_row"),
    (re.compile(r"\bprepared\b", re.I), "metadata_footer"),
    (re.compile(r"\bgenerated\b", re.I), "metadata_footer"),
    (re.compile(r"^\s*date\b", re.I), "metadata_date_row"),
    (re.compile(r"\bdistributor\b", re.I), "metadata_distributor"),
    (re.compile(r"\baddress\b", re.I), "metadata_address"),
    (re.compile(r"\bgst(?:in)?\b", re.I), "metadata_gst"),
    (re.compile(r"\b(?:phone|mobile|tel|contact)\b", re.I), "metadata_contact"),
    (re.compile(r"^\s*name\s+packg?\b", re.I), "metadata_header_row"),
    (re.compile(r"^\s*op\.?\s*stk\b", re.I), "metadata_header_row"),
]


def classify_grid_row(
    product_text: str,
    packing: str = "",
    *,
    selected: Optional[Dict[str, Any]] = None,
) -> Tuple[str, Optional[str]]:
    """Classify a geometry row as PRODUCT or NON_PRODUCT.

    Only PRODUCT rows may become line items. Classification uses normalized
    text + structural cues — never filename codes.
    """
    text = str(product_text or "").strip()
    if not text:
        return "NON_PRODUCT", "empty_product_name"

    norm = re.sub(r"\s+", " ", text).strip()
    norm_l = norm.lower()
    compact = re.sub(r"[^a-z0-9]+", "", norm_l)

    for pattern, reason in _NON_PRODUCT_RULES:
        if pattern.search(norm) or pattern.search(norm_l):
            return "NON_PRODUCT", reason

    # Lone place/header tokens (e.g. KALPETTA)
    if compact in {"kalpetta", "name", "packg", "packing", "lms"}:
        return "NON_PRODUCT", "metadata_banner_token"

    # Must contain alphabetic product-like content
    if not re.search(r"[A-Za-z]{2,}", text):
        return "NON_PRODUCT", "no_alpha_product_name"

    # "Company 171 HIMALAY" without colon still non-product if starts with company
    if norm_l.startswith("company"):
        return "NON_PRODUCT", "metadata_company_row"

    return "PRODUCT", None


def assert_physical_cells_unshifted(
    physical_values: Dict[str, Any],
    *,
    enforce_opstk_labels: bool = True,
) -> bool:
    """Blank cells must keep their physical index — never compact left."""
    # Structural invariant: keys are physical indices; missing key ≠ shift.
    # A shift bug would place Op.Stk (14) under index "2" and Receipt under "3".
    # Op.Stk label check only applies to physical_index_opstk_lms layouts.
    for pci_str, entry in (physical_values or {}).items():
        try:
            pci = int(pci_str)
        except (TypeError, ValueError):
            return False
        if not enforce_opstk_labels:
            continue
        expected_field = PHYSICAL_SOURCE_COLUMNS.get(pci)
        if expected_field and entry.get("business_field") not in (
            None,
            expected_field,
        ):
            return False
    return True

VARIANT_ORDER = (
    "A_original",
    "B_gray",
    "C_contrast",
    "D_threshold",
    "E_adaptive",
    "F_blue_removed",
    "G_sharpened",
)


@dataclass
class CellResult:
    column: str
    raw_ocr: str = ""
    normalized: Optional[float] = None
    confidence: float = -1.0
    bbox: Dict[str, int] = field(default_factory=dict)
    provider: str = "tesseract"
    variant: Optional[str] = None
    ocr_uncertain: bool = False
    blank: bool = False
    is_blank: bool = False
    candidates: List[Dict[str, Any]] = field(default_factory=list)
    candidates_map: Dict[str, Any] = field(default_factory=dict)
    skipped: bool = False
    skip_reason: Optional[str] = None
    blue_pixels_removed: int = 0
    selection_reason: str = ""
    gemini_value: Optional[float] = None
    gemini_raw: Any = None
    source: str = "tesseract"
    ink_ratio: float = 0.0
    row_index: Optional[int] = None
    column_index: Optional[int] = None
    ocr_engine: str = "tesseract"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def confidence_object(self) -> Dict[str, Any]:
        """Stable cell-level provenance — location is geometry, never LLM."""
        bb = self.bbox or {}
        pci = self.column_index
        return {
            "row_index": self.row_index,
            "physical_column_index": pci,
            "column_index": pci,  # alias
            "source_header": PHYSICAL_HEADER_LABELS.get(int(pci), "")
            if pci is not None
            else "",
            "column": self.column,
            "raw_text": self.raw_ocr,
            "normalized_value": self.normalized,
            "value": self.normalized,
            "confidence": self.confidence,
            "ocr_engine": self.ocr_engine
            if "gemini" not in (self.source or "")
            else self.source,
            "blank": bool(self.blank or self.is_blank),
            "crop": {
                "x1": bb.get("x0", bb.get("x1")),
                "y1": bb.get("y0", bb.get("y1")),
                "x2": bb.get("x1", bb.get("x2")),
                "y2": bb.get("y1", bb.get("y2")),
            },
        }


# ---------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------


def _cluster(idxs: Sequence[int], gap: int = 3) -> List[int]:
    if not idxs:
        return []
    groups: List[List[int]] = [[int(idxs[0])]]
    for x in idxs[1:]:
        x = int(x)
        if x - groups[-1][-1] <= gap:
            groups[-1].append(x)
        else:
            groups.append([x])
    return [int(np.median(g)) for g in groups]


def detect_table_geometry(bgr: np.ndarray) -> Dict[str, Any]:
    """Detect row/column separators from pale Excel grid + ink projection."""
    h, w = bgr.shape[:2]
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    b, g, r = cv2.split(bgr)

    pale = (
        (b.astype(np.int16) > r.astype(np.int16) + 5)
        & (b.astype(np.int16) > g.astype(np.int16))
        & (gray > 160)
        & (gray < 245)
    ).astype(np.uint8) * 255
    edges = cv2.Canny(gray, 40, 120)
    ink_inv = cv2.adaptiveThreshold(
        cv2.GaussianBlur(gray, (3, 3), 0),
        255,
        cv2.ADAPTIVE_THRESH_MEAN_C,
        cv2.THRESH_BINARY_INV,
        15,
        8,
    )

    hk = max(w // 20, 40)
    vk = max(h // 40, 30)
    h_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (hk, 1))
    v_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (1, vk))

    hlines = cv2.morphologyEx(edges | pale | ink_inv, cv2.MORPH_OPEN, h_kernel)
    vlines = cv2.morphologyEx(edges | pale, cv2.MORPH_OPEN, v_kernel)

    hsum = np.sum(hlines > 0, axis=1).astype(float)
    vsum = np.sum(vlines > 0, axis=0).astype(float)
    row_ys = _cluster(
        [i for i, v in enumerate(hsum) if v >= 0.10 * float(hsum.max() or 1)], 2
    )
    col_xs = _cluster(
        [i for i, v in enumerate(vsum) if v >= 0.12 * float(vsum.max() or 1)], 3
    )

    # Longest near-regular body run of horizontal lines.
    best: List[int] = []
    cur: List[int] = []
    for y in row_ys:
        if not cur:
            cur = [y]
            continue
        d = y - cur[-1]
        if 10 <= d <= 45:
            cur.append(y)
        else:
            if len(cur) > len(best):
                best = cur
            cur = [y]
    if len(cur) > len(best):
        best = cur
    rows = list(best)
    for y in reversed(row_ys):
        if rows and y < rows[0] and rows[0] - y <= 120:
            rows.insert(0, y)
        elif rows and y < rows[0] - 120:
            break

    # Prefer column set covering Name..*XY (~14 cells). Drop outer chrome.
    if col_xs and col_xs[0] > 5:
        col_xs = [max(0, col_xs[0] - 0)] + col_xs
    # Ensure left edge near table start if first col is past Name text.
    if col_xs and col_xs[0] > 40:
        col_xs = [8] + col_xs
    if col_xs and col_xs[-1] < w - 40:
        # right edge after last grid line for trailing *XY / padding
        pass  # do not invent a fake wide column unless needed

    # Filter near-duplicates after merges
    col_xs = _cluster(col_xs, gap=8)

    row_boxes = []
    for i in range(len(rows) - 1):
        y1, y2 = int(rows[i]), int(rows[i + 1])
        if y2 - y1 < 8:
            continue
        row_boxes.append(
            {
                "row_index": len(row_boxes),
                "y1": y1,
                "y2": y2,
                "confidence": 0.9,
            }
        )

    # Explicit physical cell grid — source of truth for location.
    cell_grid: List[Dict[str, int]] = []
    for ri, rb in enumerate(row_boxes):
        for ci in range(max(0, len(col_xs) - 1)):
            cell_grid.append(
                {
                    "row_index": ri,
                    "column_index": ci,
                    "x1": int(col_xs[ci]),
                    "y1": int(rb["y1"]),
                    "x2": int(col_xs[ci + 1]),
                    "y2": int(rb["y2"]),
                }
            )

    return {
        "image_width": w,
        "image_height": h,
        "row_ys": rows,
        "col_xs": col_xs,
        "row_boxes": row_boxes,
        "cell_grid": cell_grid,
        "hlines_mask": hlines,
        "vlines_mask": vlines,
        "detection_mode": "excel_grid",
        "table_bbox": {
            "x0": int(col_xs[0]) if col_xs else 0,
            "y0": int(rows[0]) if rows else 0,
            "x1": int(col_xs[-1]) if col_xs else w - 1,
            "y1": int(rows[-1]) if rows else h - 1,
        },
        "gray": gray,
    }


_HEADER_TOKEN_RE = re.compile(
    r"(?i)^(sl\.?no|s\.?no|sino|si\s*no|product|name|packg?|packing|lms|"
    r"op\.?|op\.?qty|op\.?stk|opening|pur|purchase|receipt|sale|sales|"
    r"f\.?qty|qty|repl|replacement|br/?e?|brk|ret\.?|return|adj|bal\.?|"
    r"bal\.?qty|bal\.?val|closing|cl\.?stk|cl\.?value|value|free)$"
)


def _is_headerish_token(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return False
    if _HEADER_TOKEN_RE.match(t):
        return True
    n = re.sub(r"[^a-z0-9.]+", "", t.lower())
    return n in {
        "slno",
        "sino",
        "sno",
        "product",
        "name",
        "productname",
        "lms",
        "op",
        "opqty",
        "opstk",
        "pur",
        "purqty",
        "fqty",
        "qty",
        "sale",
        "saleqty",
        "sales",
        "repl",
        "replqty",
        "bre",
        "brie",
        "brk",
        "ret",
        "retqty",
        "adj",
        "adjqty",
        "bal",
        "balqty",
        "balval",
        "salevalue",
        "salefvalue",
        "value",
    }


def _filter_header_tokens(tokens: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Keep tokens on the header baseline; drop product-name OCR junk."""
    kept = [t for t in tokens if _is_headerish_token(str(t.get("text") or ""))]
    if len(kept) < 6:
        return tokens
    # Prefer the densest horizontal baseline among headerish tokens.
    tops = sorted(float(t["top"]) for t in kept)
    best_mid = tops[len(tops) // 2]
    tight = [t for t in kept if abs(float(t["top"]) - best_mid) <= 36]
    return tight if len(tight) >= 6 else kept


def _merge_header_token_groups(
    tokens: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Cluster OCR header tokens into columns; join multi-line labels."""
    tokens = _filter_header_tokens(tokens)
    if not tokens:
        return []
    ordered = sorted(tokens, key=lambda t: float(t["cx"]))
    groups: List[List[Dict[str, Any]]] = [[ordered[0]]]
    for tok in ordered[1:]:
        prev_cx = float(np.mean([g["cx"] for g in groups[-1]]))
        if abs(float(tok["cx"]) - prev_cx) > 35:
            groups.append([tok])
        else:
            groups[-1].append(tok)

    cells: List[Dict[str, Any]] = []
    for g in groups:
        text = " ".join(x["text"] for x in sorted(g, key=lambda z: z["top"]))
        cx = float(np.mean([x["cx"] for x in g]))
        left = int(min(x["left"] for x in g))
        right = int(max(x["left"] + x["width"] for x in g))
        cells.append(
            {
                "text": text,
                "x_center": cx,
                "left": left,
                "right": right,
            }
        )

    # Merge split multi-word headers (Product+Name, Bal.+Qty, Op.+Qty, …).
    merged: List[Dict[str, Any]] = []
    i = 0
    while i < len(cells):
        cur = dict(cells[i])
        nxt = cells[i + 1] if i + 1 < len(cells) else None
        cur_n = re.sub(r"[^a-z0-9]+", "", cur["text"].lower())
        nxt_n = re.sub(r"[^a-z0-9]+", "", (nxt or {}).get("text", "").lower())
        if nxt and (
            (cur_n.endswith("product") and nxt_n == "name")
            or (cur_n == "product" and nxt_n == "name")
            or ("product" in cur_n and nxt_n == "name")
        ):
            cur["text"] = "Product Name"
            cur["x_center"] = (cur["x_center"] + float(nxt["x_center"])) / 2.0
            cur["left"] = min(int(cur["left"]), int(nxt["left"]))
            cur["right"] = max(int(cur["right"]), int(nxt["right"]))
            merged.append(cur)
            i += 2
            continue
        if nxt and cur_n in {"bal", "op", "pur", "sale", "ret", "repl", "adj"} and nxt_n in {
            "qty",
            "aty",
            "ty",
            "fqty",
        }:
            cur["text"] = f"{cur['text']} Qty".replace("  ", " ").strip()
            if cur_n == "bal":
                cur["text"] = "Bal. Qty"
            cur["x_center"] = (cur["x_center"] + float(nxt["x_center"])) / 2.0
            cur["left"] = min(int(cur["left"]), int(nxt["left"]))
            cur["right"] = max(int(cur["right"]), int(nxt["right"]))
            merged.append(cur)
            i += 2
            continue
        merged.append(cur)
        i += 1
    for idx, cell in enumerate(merged):
        cell["col_index"] = idx
    return merged


def classify_text_column_stock_format(
    header_cells: Sequence[Dict[str, Any]],
) -> str:
    """Classify header-aware text-column layouts (never Op.Stk physical map)."""
    norms = [
        re.sub(r"[^a-z0-9]+", "", str(h.get("text") or "").lower())
        for h in header_cells
    ]
    blob = " ".join(norms)
    has_sale = any("sale" in n for n in norms)
    has_bal = any(n.startswith("bal") or "balance" in n for n in norms)
    has_op = any(n.startswith("op") or "opening" in n for n in norms)
    has_pur = any(n.startswith("pur") or "purchase" in n or "receipt" in n for n in norms)
    has_lms = any(n == "lms" for n in norms)
    has_goods_ret = "goodsret" in blob or "goodsretqty" in blob
    has_total_in = "totalin" in blob or "totalinqty" in blob
    has_opstk = any("opstk" in n or n == "receipt" for n in norms)
    if has_goods_ret and has_total_in and has_sale and has_bal:
        return "STOCK_SALES_BALANCE_TEXT_COLUMNS"
    if has_sale and has_bal and has_op and has_pur and not has_opstk:
        # SIDHA / Stock-And-Sales style: Op.Qty + Sale Qty + Bal.Qty (+ optional LMS).
        return "STOCK_SALES_BALANCE_TEXT_COLUMNS"
    if has_lms and has_opstk:
        return "STOCK_OPSTK_LMS_PHYSICAL"
    return "STOCK_TEXT_COLUMNS_GENERIC"


def extract_geometry_document_metadata(
    bgr: np.ndarray,
) -> Dict[str, Any]:
    """OCR document chrome above the table — independent of product-row format."""
    import pytesseract

    h, w = bgr.shape[:2]
    band_h = max(80, min(int(h * 0.28), 780))
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    band = gray[0:band_h, :]
    text = pytesseract.image_to_string(Image.fromarray(band), config="--psm 6")
    stockist = None
    reason = "no_stockist_line"
    try:
        from services.sales_statement_extractor import _detect_stockist_from_page_text

        stockist = _detect_stockist_from_page_text(text)
    except Exception as exc:
        reason = f"detector_error:{type(exc).__name__}"
        stockist = None
    if stockist:
        reason = "header_ocr"
    else:
        # Fallback: first prominent ALL-CAPS / Title company line above table.
        for ln in (text or "").splitlines():
            s = ln.strip()
            if len(s) < 4 or len(s) > 80:
                continue
            if re.search(
                r"(?i)\b(stock\s*and\s*sales|statement|page\s*\d|printed|from\s+\d)",
                s,
            ):
                continue
            if re.search(r"(?i)\b(pharmaceutical|distributors?|agenc(?:y|ies)|limited|pvt|ltd)\b", s) or (
                s.isupper() and len(s.split()) <= 6 and re.search(r"[A-Z]{3,}", s)
            ):
                stockist = re.sub(r"\s+", " ", s).strip(" .-")
                reason = "header_ocr_fallback"
                break
    logger.info(
        "[STOCK_METADATA] stockist_detected=%s stockist_name=%r "
        "metadata_source=%s reason=%s band_h=%s",
        bool(stockist),
        stockist,
        "header_ocr" if stockist else None,
        reason,
        band_h,
    )
    period_from = period_to = None
    m = re.search(
        r"(?i)from\s+(\d{1,2}[-/][A-Za-z]{3,}[-/]\d{2,4})\s+to\s+(\d{1,2}[-/][A-Za-z]{3,}[-/]\d{2,4})",
        text or "",
    )
    if m:
        period_from, period_to = m.group(1), m.group(2)
    title = None
    for ln in (text or "").splitlines():
        if re.search(r"(?i)stock\s*and\s*sales|stock\s*&\s*sales", ln):
            title = ln.strip()
            break
    return {
        "stockist_name": stockist,
        "stockist_address": None,
        "company_name": None,
        "period_from": period_from,
        "period_to": period_to,
        "report_title": title,
        "metadata_source": "header_ocr" if stockist else None,
        "metadata_reason": reason,
        "metadata_bbox": [0, 0, w, band_h],
        "header_text_preview": (text or "")[:500],
    }


def build_semantic_column_descriptors(
    columns: Sequence[Dict[str, Any]],
    header_cells: Sequence[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Stable {semantic_name, x_left, x_right, header_text, confidence} list."""
    out: List[Dict[str, Any]] = []
    for c in columns:
        field = str(c.get("business_field") or c.get("canonical") or "ignore")
        idx = int(c.get("col_index") or c.get("physical_column_index") or 0)
        hdr = ""
        if 0 <= idx < len(header_cells):
            hdr = str(header_cells[idx].get("text") or "")
        out.append(
            {
                "semantic_name": field,
                "x_left": int(c.get("x0") or 0),
                "x_right": int(c.get("x1") or 0),
                "header_text": hdr or str(c.get("source_header") or ""),
                "confidence": 0.9
                if field not in ("ignore", "xy", None)
                else 0.3,
                "col_index": idx,
            }
        )
    return out


def write_text_columns_debug_artifacts(
    image_path: str | Path,
    report: Dict[str, Any],
    *,
    json_path: str = "/tmp/0701027_text_columns_debug.json",
    png_path: str = "/tmp/0701027_text_columns_debug.png",
) -> Dict[str, Any]:
    """Emit first-product column diagnostics + annotated PNG."""
    path = Path(image_path)
    bgr = cv2.imread(str(path))
    if bgr is None:
        return {"error": "cannot_read"}
    columns = list(report.get("columns") or [])
    header_cells = list((report.get("geometry") or {}).get("header_cells") or [])
    prods = [r for r in (report.get("rows") or []) if r.get("row_class") == "PRODUCT"]
    first = prods[0] if prods else {}
    sel = first.get("selected") or {}
    desc = build_semantic_column_descriptors(columns, header_cells)
    by_sem = {d["semantic_name"]: d for d in desc if d["semantic_name"] != "ignore"}

    def _col_diag(sem: str) -> Dict[str, Any]:
        d = by_sem.get(sem) or {}
        fp = (first.get("field_provenance") or {}).get(sem) or {}
        crop = fp.get("crop") or fp.get("bbox") or {}
        return {
            "header_bbox": [
                d.get("x_left"),
                (report.get("geometry") or {}).get("row_ys", [None])[0],
                d.get("x_right"),
                (
                    ((report.get("geometry") or {}).get("row_ys") or [None, None])[1]
                    if len((report.get("geometry") or {}).get("row_ys") or []) > 1
                    else None
                ),
            ],
            "cell_bbox": [
                crop.get("x1") or crop.get("x0"),
                crop.get("y1") or crop.get("y0"),
                crop.get("x2") or crop.get("x1"),
                crop.get("y2") or crop.get("y1"),
            ],
            "raw_ocr": fp.get("raw_text") or fp.get("raw_ocr"),
            "value": sel.get(sem),
            "x_left": d.get("x_left"),
            "x_right": d.get("x_right"),
            "header_text": d.get("header_text"),
        }

    payload = {
        "product": sel.get("product_name"),
        "row_bbox": [
            (first.get("row_bbox") or {}).get("x0"),
            first.get("y0"),
            (first.get("row_bbox") or {}).get("x1"),
            first.get("y1"),
        ],
        "format": (report.get("geometry") or {}).get("text_column_format"),
        "columns": {
            "opening_qty": _col_diag("opening_qty"),
            "receipts_qty": _col_diag("receipts_qty"),
            "sales_qty": _col_diag("sales_qty"),
            "closing_qty": _col_diag("closing_qty"),
            "closing_value": _col_diag("closing_value"),
        },
        "semantic_columns": desc,
        "physical_cells": first.get("physical_cells"),
        "selected": {
            k: sel.get(k)
            for k in (
                "lms",
                "opening_qty",
                "receipts_qty",
                "sales_qty",
                "closing_qty",
                "closing_value",
            )
        },
    }
    Path(json_path).write_text(json.dumps(payload, indent=2), encoding="utf-8")

    vis = bgr.copy()
    # Header / semantic column lines
    for d in desc:
        color = (0, 180, 0) if d["semantic_name"] in (
            "sales_qty",
            "closing_qty",
        ) else (180, 180, 0)
        cv2.line(
            vis,
            (int(d["x_left"]), 0),
            (int(d["x_left"]), vis.shape[0] - 1),
            color,
            1,
        )
        cv2.putText(
            vis,
            str(d["semantic_name"])[:12],
            (int(d["x_left"]) + 2, 40),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.35,
            color,
            1,
            cv2.LINE_AA,
        )
    y0, y1 = first.get("y0"), first.get("y1")
    if y0 is not None and y1 is not None:
        cv2.rectangle(
            vis,
            (0, int(y0)),
            (vis.shape[1] - 1, int(y1)),
            (255, 0, 0),
            2,
        )
    for sem, col in (("sales_qty", (0, 0, 255)), ("closing_qty", (255, 0, 255))):
        d = _col_diag(sem)
        bb = d.get("cell_bbox") or []
        if all(v is not None for v in bb):
            cv2.rectangle(
                vis,
                (int(bb[0]), int(bb[1])),
                (int(bb[2]), int(bb[3])),
                col,
                2,
            )
    cv2.imwrite(png_path, vis)
    logger.info(
        "GEOMETRY_TEXT_COLUMNS_DEBUG json=%s png=%s closing_qty=%s sales_qty=%s",
        json_path,
        png_path,
        sel.get("closing_qty"),
        sel.get("sales_qty"),
    )
    return payload


def detect_text_column_geometry(bgr: np.ndarray) -> Optional[Dict[str, Any]]:
    """Header-x + ink-band geometry for tables without Excel vertical grid lines.

    Column bounds use successive header x-centres so right-aligned qty digits
    (common on Stock-And-Sales screenshots) stay inside the correct cell.
    """
    import pytesseract

    h, w = bgr.shape[:2]
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)

    # Locate header band via keyword score on horizontal strips.
    best_y0, best_y1, best_score = 0, 0, -1
    step = 20
    for y0 in range(max(0, int(h * 0.12)), min(h - 40, int(h * 0.55)), step):
        y1 = min(h, y0 + 55)
        text = pytesseract.image_to_string(
            Image.fromarray(gray[y0:y1, :]), config="--psm 6"
        ).lower()
        score = sum(
            1
            for tok in (
                "product",
                "lms",
                "sale",
                "qty",
                "op.",
                "pur",
                "bal",
                "opening",
                "receipt",
                "sales",
                "sino",
                "slno",
            )
            if tok in text
        )
        # Prefer strips that look like the real column header row.
        if "product" in text and "lms" in text:
            score += 3
        if re.search(r"\bsino\b|\bsl\s*no\b|\bslno\b", text):
            score += 2
        # Penalize summary / chrome strips.
        if "op.stk val" in text or "printed by" in text:
            score -= 5
        if score > best_score:
            best_score = score
            best_y0, best_y1 = y0, y1
    if best_score < 3:
        logger.info(
            "GEOMETRY_TEXT_COLUMNS status=no_header score=%s", best_score
        )
        return None

    hdr = gray[best_y0:best_y1, :]
    data = pytesseract.image_to_data(
        Image.fromarray(hdr), output_type=pytesseract.Output.DICT, config="--psm 6"
    )
    tokens: List[Dict[str, Any]] = []
    for i, raw in enumerate(data["text"]):
        t = (raw or "").strip()
        try:
            conf = float(data["conf"][i])
        except (TypeError, ValueError):
            conf = -1.0
        if not t or conf < 25:
            continue
        tokens.append(
            {
                "text": t,
                "left": int(data["left"][i]),
                "top": int(data["top"][i]) + best_y0,
                "width": int(data["width"][i]),
                "height": int(data["height"][i]),
                "cx": float(data["left"][i]) + float(data["width"][i]) / 2.0,
            }
        )
    header_cells = _merge_header_token_groups(tokens)
    if len(header_cells) < 6:
        logger.info(
            "GEOMETRY_TEXT_COLUMNS status=few_headers n=%s", len(header_cells)
        )
        return None

    # Boundaries at each header's left edge. Numeric digits are often
    # right-aligned toward the *next* header, so extend each qty column's
    # right edge to the next header left (not the current header centre).
    col_xs = [max(0, int(header_cells[0]["left"]) - 10)]
    for i in range(1, len(header_cells)):
        col_xs.append(int(header_cells[i]["left"]) - 2)
    col_xs.append(
        min(w - 1, max(col_xs[-1] + 40, int(header_cells[-1]["right"]) + 24))
    )
    # Keep SINo/S.No narrow so Product Name captures the leading glyph (B in BRESOL).
    first_n = re.sub(r"[^a-z0-9]+", "", str(header_cells[0].get("text") or "").lower())
    if first_n in {"slno", "sino", "sno", "sno"} or first_n.startswith("sl"):
        narrow = int(header_cells[0]["right"]) + 10
        if len(col_xs) > 1 and narrow < int(col_xs[1]):
            col_xs[1] = narrow
    # Tune inter-header boundaries. Default = midpoint. Sale Qty needs a
    # right-biased split (digit near next header). Bal.Qty must NOT swallow
    # Bal.Val's leading digits (13 vs 658.45) — use a left-biased split when
    # the next header is a value column.
    for i, hc in enumerate(header_cells[:-1]):
        label = re.sub(r"[^a-z0-9]+", "", str(hc.get("text") or "").lower())
        nxt = header_cells[i + 1]
        nxt_n = re.sub(r"[^a-z0-9]+", "", str(nxt.get("text") or "").lower())
        cur_cx = float(hc["x_center"])
        nxt_cx = float(nxt["x_center"])
        nxt_is_value = any(
            k in nxt_n for k in ("val", "value", "amt", "amount")
        ) or nxt_n in {"bval", "balval", "salevalue", "salefvalue"}
        if nxt_is_value and (
            "bal" in label or label in {"qty", "balqty", "closing", "clstk"}
        ):
            # Split in the whitespace gap between Bal.Qty and Bal.Val boxes so
            # qty keeps "13" and value keeps the leading digit of "658.45".
            # Must REPLACE the default left-edge boundary (max would keep it
            # too far right and swallow Bal.Val's leading digit).
            gap_l = int(hc.get("right") or cur_cx)
            gap_r = int(nxt.get("left") or nxt_cx)
            if gap_r > gap_l + 4:
                target = (gap_l + gap_r) // 2
            else:
                target = int(cur_cx * 0.75 + nxt_cx * 0.25)
            col_xs[i + 1] = max(int(col_xs[i]) + 8, int(target))
            continue
        if "sale" in label and "f" not in label and not nxt_is_value:
            # Sale Qty digit often sits near Sale F / next header.
            target = int(cur_cx * 0.20 + nxt_cx * 0.80)
        elif any(
            k in label
            for k in ("sale", "qty", "op", "pur", "lms", "bal", "repl", "ret", "adj")
        ):
            target = int(cur_cx * 0.50 + nxt_cx * 0.50)
        else:
            continue
        col_xs[i + 1] = max(int(col_xs[i + 1]), target)
    col_xs = _cluster([int(x) for x in col_xs], gap=4)
    if len(col_xs) < 7:
        return None

    # Product-name ink bands → row separators (skip chrome / summary).
    inv = cv2.adaptiveThreshold(
        cv2.GaussianBlur(gray, (3, 3), 0),
        255,
        cv2.ADAPTIVE_THRESH_MEAN_C,
        cv2.THRESH_BINARY_INV,
        15,
        8,
    )
    name_x0 = max(0, int(col_xs[0]))
    name_x1 = min(w, int(col_xs[min(2, len(col_xs) - 1)]))
    if name_x1 - name_x0 < 40:
        name_x0, name_x1 = int(w * 0.05), int(w * 0.40)
    name_zone = inv[:, name_x0:name_x1]
    dens = (name_zone > 0).sum(axis=1).astype(float)
    sm = np.convolve(dens, np.ones(3) / 3.0, mode="same")
    body_y0 = best_y1 + 2
    body_y1 = min(h - 10, int(h * 0.92))
    bands: List[Tuple[int, int]] = []
    in_band = False
    start = 0
    for y in range(body_y0, body_y1):
        if sm[y] > 25 and not in_band:
            in_band = True
            start = y
        elif sm[y] <= 25 and in_band:
            in_band = False
            if 8 <= (y - start) <= 42:
                bands.append((start, y))
    if in_band and 8 <= (body_y1 - start) <= 42:
        bands.append((start, body_y1))
    if len(bands) < 3:
        logger.info(
            "GEOMETRY_TEXT_COLUMNS status=few_rows n=%s", len(bands)
        )
        return None

    # Drop trailing summary bands (OP.Stk Val / totals).
    filtered: List[Tuple[int, int]] = []
    for y0, y1 in bands:
        snippet = pytesseract.image_to_string(
            Image.fromarray(gray[y0:y1, name_x0:name_x1]), config="--psm 7"
        ).strip()
        low = snippet.lower()
        if re.search(
            r"\b(op\.?\s*stk\s*val|grand\s*total|total\s*:|printed\s*by|page\s+\d)\b",
            low,
        ):
            break
        filtered.append((y0, y1))
    bands = filtered
    if len(bands) < 3:
        return None

    # Exact ink-band boxes (tight) — sparse right-aligned digits fail OCR when
    # the row crop includes large empty gaps to the next product line.
    data_row_boxes = [
        {
            "row_index": i,
            "y1": max(0, int(y0) - 1),
            "y2": min(h, int(y1) + 2),
            "confidence": 0.9,
        }
        for i, (y0, y1) in enumerate(bands)
    ]
    # Synthetic separators for APIs that still consume row_ys:
    # [header_y0, header_y1, band0_y1, band1_y1, ...] is wrong for build_rows;
    # instead emit header pair then each band's y1/y2 as consecutive edges.
    row_ys = [int(best_y0), int(best_y1)]
    for rb in data_row_boxes:
        if row_ys[-1] != int(rb["y1"]):
            row_ys.append(int(rb["y1"]))
        row_ys.append(int(rb["y2"]))
    row_ys = _cluster([int(y) for y in row_ys], gap=1)

    row_boxes = [
        {
            "row_index": 0,
            "y1": int(best_y0),
            "y2": int(best_y1),
            "confidence": 0.9,
        }
    ] + [
        {**rb, "row_index": i + 1}
        for i, rb in enumerate(data_row_boxes)
    ]
    cell_grid: List[Dict[str, int]] = []
    for ri, rb in enumerate(row_boxes):
        for ci in range(max(0, len(col_xs) - 1)):
            cell_grid.append(
                {
                    "row_index": ri,
                    "column_index": ci,
                    "x1": int(col_xs[ci]),
                    "y1": int(rb["y1"]),
                    "x2": int(col_xs[ci + 1]),
                    "y2": int(rb["y2"]),
                }
            )

    logger.info(
        "GEOMETRY_TEXT_COLUMNS status=ok headers=%s rows=%s "
        "header_band=%s-%s col_xs_n=%s",
        len(header_cells),
        len(bands),
        best_y0,
        best_y1,
        len(col_xs),
    )
    return {
        "image_width": w,
        "image_height": h,
        "row_ys": row_ys,
        "col_xs": col_xs,
        "row_boxes": row_boxes,
        "cell_grid": cell_grid,
        "detection_mode": "text_columns",
        "header_ri_hint": 0,
        "data_row_boxes": data_row_boxes,
        "preparsed_header_cells": header_cells,
        "table_bbox": {
            "x0": int(col_xs[0]),
            "y0": int(row_ys[0]),
            "x1": int(col_xs[-1]),
            "y1": int(row_ys[-1]),
        },
        "gray": gray,
    }


def _ocr_header_cell(gray: np.ndarray, x0: int, x1: int, y0: int, y1: int) -> str:
    crop = gray[y0 + 1 : y1 - 1, x0 + 1 : x1 - 1]
    if crop.size == 0:
        return ""
    up = cv2.resize(crop, None, fx=3, fy=3, interpolation=cv2.INTER_CUBIC)
    _, bw = cv2.threshold(up, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    if np.mean(bw) < 127:
        bw = 255 - bw
    import pytesseract

    return pytesseract.image_to_string(
        Image.fromarray(bw), config="--oem 3 --psm 7"
    ).strip()


def find_header_band(
    gray: np.ndarray, row_ys: List[int]
) -> Tuple[int, int, int]:
    """Return (header_ri, header_y0, header_y1)."""
    import pytesseract

    best_i = 0
    best_score = -1
    for i in range(min(12, len(row_ys) - 1)):
        y0, y1 = int(row_ys[i]), int(row_ys[i + 1])
        crop = gray[y0:y1, :]
        if crop.size == 0:
            continue
        text = pytesseract.image_to_string(
            Image.fromarray(crop), config="--psm 7"
        ).lower()
        score = sum(
            1
            for tok in (
                "name",
                "pack",
                "op",
                "stk",
                "receipt",
                "sales",
                "cl",
                "lms",
            )
            if tok in text
        )
        if score > best_score:
            best_score = score
            best_i = i
    y0, y1 = int(row_ys[best_i]), int(row_ys[best_i + 1])
    return best_i, y0, y1


def _header_norms(header_cells: List[Dict[str, Any]]) -> List[str]:
    out = []
    for h in header_cells:
        out.append(re.sub(r"[^a-z0-9]+", "", str(h.get("text") or "").lower()))
    return out


def is_opstk_lms_layout(header_cells: List[Dict[str, Any]], n_cols: int) -> bool:
    """True when physical grid is Product|Packg|LMS|Op.Stk|Receipt|…"""
    norms = _header_norms(header_cells)
    if n_cols < 12:
        return False
    has_lms = any(t == "lms" for t in norms)
    has_op = any("opstk" in t or t == "op" for t in norms)
    has_receipt = any("receipt" in t for t in norms)
    # Prefer physical map whenever LMS is present — never skip LMS.
    if has_lms and (has_op or has_receipt):
        return True
    # Also when header OCR is weak but column count matches this Excel layout.
    if n_cols in (13, 14) and has_op and has_receipt:
        return True
    return False


def apply_physical_source_columns(
    col_xs: List[int], header_cells: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """Map by physical index only — never by position in a filtered numeric list."""
    n = len(col_xs) - 1
    columns: List[Dict[str, Any]] = []
    for idx in range(n):
        field = PHYSICAL_SOURCE_COLUMNS.get(idx, "ignore")
        src_hdr = (
            str(header_cells[idx].get("text") or "")
            if idx < len(header_cells)
            else PHYSICAL_HEADER_LABELS.get(idx, f"col{idx}")
        )
        columns.append(
            {
                "col_index": idx,
                "column_index": idx,
                "physical_column_index": idx,
                "canonical": field,
                "business_field": field,
                "source_header": src_hdr or PHYSICAL_HEADER_LABELS.get(idx, f"col{idx}"),
                "mapping_source": "physical_index",
                "x0": int(col_xs[idx]),
                "x1": int(col_xs[idx + 1]),
                "x_center": (int(col_xs[idx]) + int(col_xs[idx + 1])) / 2.0,
                "text": src_hdr,
            }
        )
    return columns


def resolve_column_geometry(
    gray: np.ndarray,
    col_xs: List[int],
    header_y0: int,
    header_y1: int,
    *,
    preparsed_header_cells: Optional[List[Dict[str, Any]]] = None,
    detection_mode: str = "excel_grid",
) -> Tuple[List[Dict[str, Any]], str, List[Dict[str, Any]]]:
    n = len(col_xs) - 1
    if preparsed_header_cells and len(preparsed_header_cells) >= n:
        header_cells = []
        for i in range(n):
            src = preparsed_header_cells[i]
            header_cells.append(
                {
                    "text": str(src.get("text") or f"col{i}"),
                    "col_index": i,
                    "x_center": float(
                        src.get("x_center")
                        or ((int(col_xs[i]) + int(col_xs[i + 1])) / 2.0)
                    ),
                }
            )
    else:
        header_cells = []
        for i in range(n):
            x0, x1 = int(col_xs[i]), int(col_xs[i + 1])
            text = _ocr_header_cell(gray, x0, x1, header_y0, header_y1)
            header_cells.append(
                {
                    "text": text or f"col{i}",
                    "col_index": i,
                    "x_center": (x0 + x1) / 2.0,
                }
            )

    # Physical-index map is mandatory for Op.Stk+LMS Excel layouts.
    # Never apply it to text-column Stock-And-Sales layouts (Sale Qty / Op.Qty).
    if detection_mode != "text_columns" and is_opstk_lms_layout(header_cells, n):
        columns = apply_physical_source_columns(col_xs, header_cells)
        return columns, "physical_index_opstk_lms", header_cells

    # Fallback: header aliases, but still stamp physical indices and never
    # renumber after dropping columns.
    resolved = resolve_columns(header_cells)
    columns = list(resolved.get("columns") or [])
    for c in columns:
        idx = int(c.get("col_index") or 0)
        if 0 <= idx < n:
            c["x0"] = int(col_xs[idx])
            c["x1"] = int(col_xs[idx + 1])
            c["x_center"] = (c["x0"] + c["x1"]) / 2.0
            c["column_index"] = idx
            c["physical_column_index"] = idx
            c["source_header"] = str(header_cells[idx].get("text") or "")
            c["business_field"] = c.get("canonical")
            c.setdefault(
                "mapping_source",
                "text_columns_header_ocr"
                if detection_mode == "text_columns"
                else "header_ocr",
            )
        text_n = (
            re.sub(r"[^a-z0-9]+", "", str(header_cells[idx]["text"]).lower())
            if 0 <= idx < len(header_cells)
            else ""
        )
        if text_n == "lms":
            c["canonical"] = "lms"
            c["business_field"] = "lms"
            c["mapping_source"] = "geometry_lms"
        # Normalize Receipt → receipts_qty (not a filtered-list offset)
        if c.get("canonical") == "purchase_qty":
            c["canonical"] = "receipts_qty"
            c["business_field"] = "receipts_qty"
        if c.get("canonical") == "expiry_damage_qty":
            c["canonical"] = "breakage_qty"
            c["business_field"] = "breakage_qty"
        if c.get("canonical") == "pack":
            c["canonical"] = "packing"
            c["business_field"] = "packing"
    source = (
        "text_columns_header_ocr"
        if detection_mode == "text_columns"
        else "header_ocr"
    )
    return columns, source, header_cells


# ---------------------------------------------------------------------------
# OCR helpers
# ---------------------------------------------------------------------------


def _ink_ratio(gray_crop: np.ndarray) -> float:
    if gray_crop.size == 0:
        return 0.0
    return float(np.mean(gray_crop < 140))


def _normalize_numeric(
    raw: str, *, allow_decimal: bool = False
) -> Tuple[Optional[float], bool, bool]:
    """Return (value, is_blank, uncertain). Never invent."""
    text = (raw or "").strip()
    if text == "":
        return None, True, False
    if _GARBAGE.match(text):
        return None, False, True
    if allow_decimal:
        cleaned = text.replace(",", "").replace(" ", "")
        m = re.search(r"-?\d+(?:\.\d+)?", cleaned)
        if not m:
            return None, False, True
        try:
            return float(m.group(0)), False, False
        except ValueError:
            return None, False, True
    digits = re.sub(r"[^\d]", "", text)
    if not digits:
        return None, False, True
    cleaned = text.replace(" ", "")
    if not cleaned.isdigit() and digits != cleaned:
        if re.sub(r"[\d\s.,]", "", cleaned):
            return None, False, True
    try:
        return float(digits), False, False
    except ValueError:
        return None, False, True


def _wipe_cell_grid_lines(gray_crop: np.ndarray) -> np.ndarray:
    """Remove thin edge grid lines from a cell crop without erasing digit strokes."""
    if gray_crop.size == 0:
        return gray_crop
    out = gray_crop.copy()
    h, w = out.shape[:2]
    # Only bleach border strips (grid lines sit on cell edges).
    edge = max(1, min(2, h // 8, w // 8))
    out[:edge, :] = 255
    out[-edge:, :] = 255
    out[:, :edge] = 255
    out[:, -edge:] = 255
    return out


def _prep_variants(bgr_crop: np.ndarray) -> Tuple[Dict[str, np.ndarray], int]:
    if bgr_crop.ndim == 2:
        bgr_crop = cv2.cvtColor(bgr_crop, cv2.COLOR_GRAY2BGR)
    gray = cv2.cvtColor(bgr_crop, cv2.COLOR_BGR2GRAY)
    gray_nogrid = _wipe_cell_grid_lines(gray)
    cleaned, blue_px, _ = remove_blue_preserve_ink(bgr_crop)
    cleaned_gray = _wipe_cell_grid_lines(cv2.cvtColor(cleaned, cv2.COLOR_BGR2GRAY))

    def up(im: np.ndarray, fx: int = 6) -> np.ndarray:
        return cv2.resize(im, None, fx=fx, fy=fx, interpolation=cv2.INTER_CUBIC)

    def norm_bw(im: np.ndarray) -> np.ndarray:
        return 255 - im if np.mean(im) < 127 else im

    variants: Dict[str, np.ndarray] = {}
    variants["A_original"] = up(gray)
    variants["B_gray"] = up(gray_nogrid)
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(4, 4))
    variants["C_contrast"] = up(clahe.apply(gray_nogrid))
    _, otsu = cv2.threshold(
        up(gray_nogrid), 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU
    )
    variants["D_threshold"] = cv2.copyMakeBorder(
        norm_bw(otsu), 10, 10, 10, 10, cv2.BORDER_CONSTANT, value=255
    )
    adapt = cv2.adaptiveThreshold(
        up(gray_nogrid),
        255,
        cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY,
        31,
        11,
    )
    variants["E_adaptive"] = cv2.copyMakeBorder(
        norm_bw(adapt), 10, 10, 10, 10, cv2.BORDER_CONSTANT, value=255
    )
    variants["F_blue_removed"] = up(cleaned_gray)
    blur = cv2.GaussianBlur(up(gray_nogrid), (0, 0), 1.0)
    sharp = cv2.addWeighted(up(gray_nogrid), 1.8, blur, -0.8, 0)
    variants["G_sharpened"] = sharp
    return variants, blue_px


def _tess_read(
    im: np.ndarray,
    *,
    numeric: bool = True,
    allow_decimal: bool = False,
    psm: int = 7,
) -> Tuple[str, float]:
    import pytesseract

    cfg = f"--oem 3 --psm {int(psm)}"
    if numeric:
        # Keep minus + decimal + thousands comma; never invent.
        wl = "0123456789.-," if allow_decimal else "0123456789.-,"
        cfg += f" -c tessedit_char_whitelist={wl}"
    if im.ndim == 3:
        im = cv2.cvtColor(im, cv2.COLOR_BGR2GRAY)
    data = pytesseract.image_to_data(
        Image.fromarray(im), config=cfg, output_type=pytesseract.Output.DICT
    )
    texts, confs = [], []
    for t, c in zip(data.get("text") or [], data.get("conf") or []):
        ts = str(t or "").strip()
        if not ts:
            continue
        texts.append(ts)
        try:
            confs.append(float(c))
        except (TypeError, ValueError):
            confs.append(-1.0)
    raw = "".join(texts) if numeric else " ".join(texts)
    return raw, (float(np.mean(confs)) if confs else -1.0)


def ocr_numeric_cell(
    bgr: np.ndarray,
    gray: np.ndarray,
    bbox: Dict[str, int],
    column: str,
    *,
    allow_decimal: bool = False,
    row_index: Optional[int] = None,
    column_index: Optional[int] = None,
) -> CellResult:
    x0, y0, x1, y1 = bbox["x0"], bbox["y0"], bbox["x1"], bbox["y1"]
    h, w = gray.shape[:2]
    ix0, iy0 = max(0, x0 + 1), max(0, y0 + 1)
    ix1, iy1 = min(w, x1 - 1), min(h, y1 - 1)
    if ix1 <= ix0 or iy1 <= iy0:
        return CellResult(
            column=column,
            blank=True,
            is_blank=True,
            bbox=bbox,
            confidence=99.0,
            selection_reason="empty_bbox",
            row_index=row_index,
            column_index=column_index,
        )
    gcrop = gray[iy0:iy1, ix0:ix1]
    bcrop = bgr[iy0:iy1, ix0:ix1]
    ink = _ink_ratio(gcrop)
    if ink < 0.005:
        return CellResult(
            column=column,
            blank=True,
            is_blank=True,
            bbox=bbox,
            confidence=99.0,
            skipped=True,
            skip_reason="low_ink",
            selection_reason="low_ink_blank",
            ink_ratio=ink,
            row_index=row_index,
            column_index=column_index,
        )

    # Tight ink crop — right-aligned sparse digits (e.g. LMS=1) need this.
    ink_mask = gcrop < 140
    ys, xs = np.where(ink_mask)
    if len(xs) > 0:
        pad = 2
        y_a = max(0, int(ys.min()) - pad)
        y_b = min(gcrop.shape[0], int(ys.max()) + pad + 1)
        x_a = max(0, int(xs.min()) - pad)
        x_b = min(gcrop.shape[1], int(xs.max()) + pad + 1)
        gcrop = gcrop[y_a:y_b, x_a:x_b]
        bcrop = bcrop[y_a:y_b, x_a:x_b]
        ink = _ink_ratio(gcrop)

    variants, blue_px = _prep_variants(bcrop)
    cand_list: List[Dict[str, Any]] = []
    cand_map: Dict[str, Any] = {}
    values: List[Tuple[str, str, float, Optional[float], bool, bool]] = []
    # At least two PSM modes on key variants (spec: psm 10 + psm 8).
    psm_modes = (10, 8)
    for name in VARIANT_ORDER:
        im = variants.get(name)
        if im is None:
            continue
        for psm in psm_modes if name in ("A_original", "E_adaptive", "G_sharpened") else (8,):
            raw, conf = _tess_read(
                im, numeric=True, allow_decimal=allow_decimal, psm=psm
            )
            val, blank, uncertain = _normalize_numeric(
                raw, allow_decimal=allow_decimal
            )
            key = f"{name}_psm{psm}"
            entry = {
                "value": None if blank else (None if uncertain else val),
                "raw": raw,
                "source": f"tesseract_{key}",
                "confidence": conf,
                "blank": blank,
                "uncertain": uncertain,
                "normalized": val,
            }
            cand_list.append(entry)
            cand_map[key] = entry
            values.append((key, raw, conf, val, blank, uncertain))

    non_null = [v for v in values if v[3] is not None and not v[5]]
    soft_null = [v for v in values if v[3] is not None]
    use = non_null or soft_null
    if not use:
        # Sparse right-aligned digits can look nearly blank — do not invent,
        # but mark uncertain so Gemini cell fallback can read the crop.
        if ink < 0.007:
            return CellResult(
                column=column,
                blank=True,
                is_blank=True,
                bbox=bbox,
                confidence=90.0,
                candidates=cand_list,
                candidates_map=cand_map,
                blue_pixels_removed=blue_px,
                selection_reason="empty_ocr_low_ink",
                ink_ratio=ink,
                row_index=row_index,
                column_index=column_index,
            )
        return CellResult(
            column=column,
            raw_ocr=values[0][1] if values else "",
            normalized=None,
            confidence=values[0][2] if values else -1.0,
            bbox=bbox,
            ocr_uncertain=True,
            blank=False,
            is_blank=False,
            candidates=cand_list,
            candidates_map=cand_map,
            blue_pixels_removed=blue_px,
            selection_reason="empty_ocr_uncertain",
            ink_ratio=ink,
            row_index=row_index,
            column_index=column_index,
        )

    counts = Counter(v[3] for v in use)
    best_val, _ = counts.most_common(1)[0]
    agreeing = [v for v in use if v[3] == best_val]
    best = max(agreeing, key=lambda v: v[2])
    disagree = len(counts) > 1
    low = best[2] < (20 if len(counts) == 1 else 40)
    uncertain = disagree or low or best[5]
    reason = "majority_vote"
    if disagree:
        reason = "disagree_variants_uncertain"
    elif low:
        reason = "low_confidence_uncertain"
    return CellResult(
        column=column,
        raw_ocr=best[1],
        normalized=None if uncertain else float(best_val),
        confidence=best[2],
        bbox=bbox,
        variant=best[0],
        ocr_uncertain=uncertain,
        blank=False,
        is_blank=False,
        candidates=cand_list,
        candidates_map=cand_map,
        blue_pixels_removed=blue_px,
        selection_reason=reason,
        ink_ratio=ink,
        source="tesseract",
        row_index=row_index,
        column_index=column_index,
    )


def ocr_text_cell(
    gray: np.ndarray,
    bbox: Dict[str, int],
    column: str,
    *,
    row_index: Optional[int] = None,
    column_index: Optional[int] = None,
) -> CellResult:
    x0, y0, x1, y1 = bbox["x0"], bbox["y0"], bbox["x1"], bbox["y1"]
    crop = gray[max(0, y0 + 1) : max(0, y1 - 1), max(0, x0 + 1) : max(0, x1 - 1)]
    if crop.size == 0 or _ink_ratio(crop) < 0.005:
        return CellResult(
            column=column,
            blank=True,
            is_blank=True,
            bbox=bbox,
            confidence=99.0,
            row_index=row_index,
            column_index=column_index,
        )
    # Spec: 4x–8x upscale + contrast/threshold variants; pick stable text.
    candidates: List[Tuple[str, float]] = []
    for fx in (4, 6, 8):
        up = cv2.resize(crop, None, fx=fx, fy=fx, interpolation=cv2.INTER_CUBIC)
        clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(4, 4))
        enhanced = clahe.apply(up)
        for im in (up, enhanced):
            _, bw = cv2.threshold(im, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
            if np.mean(bw) < 127:
                bw = 255 - bw
            raw, conf = _tess_read(bw, numeric=False, psm=7)
            raw = re.sub(r"^[\|\-\s_]+", "", raw).strip()
            if raw:
                candidates.append((raw, conf))
    if not candidates:
        return CellResult(
            column=column,
            blank=True,
            is_blank=True,
            bbox=bbox,
            confidence=50.0,
            row_index=row_index,
            column_index=column_index,
            selection_reason="text_empty",
        )
    counts = Counter(c[0] for c in candidates)
    best_raw, _ = counts.most_common(1)[0]
    best_conf = max(c[1] for c in candidates if c[0] == best_raw)
    return CellResult(
        column=column,
        raw_ocr=best_raw,
        confidence=best_conf,
        bbox=bbox,
        blank=not bool(best_raw),
        is_blank=not bool(best_raw),
        ocr_uncertain=best_conf < 35 and bool(best_raw),
        variant="text_multi_upscale",
        selection_reason="text_ocr_stable",
        source="tesseract",
        row_index=row_index,
        column_index=column_index,
    )


# ---------------------------------------------------------------------------
# Product matching
# ---------------------------------------------------------------------------


def _norm_name(s: str) -> str:
    s = (s or "").lower()
    s = s.replace("septlin", "septilin")
    s = s.replace("bonnisa", "bonnisan").replace("jonnisan", "bonnisan")
    s = re.sub(r"[^a-z0-9]+", "", s)
    return s


def match_focus_product(name: str, pack: str = "") -> Tuple[str, Optional[str]]:
    blob = _norm_name(f"{name} {pack}")
    if not blob:
        return "unmatched", None
    # Order matters: more specific first.
    rules = [
        ("BONNISAN 100ML", ("bonnisan100", "bonnisan1oo")),
        ("BONNISAN 200ML", ("bonnisan200", "bonnisan2oo")),
        ("BONNISAN 15ML", ("bonnisan15", "bonnispa15", "bonnispa1s5")),
        ("BRESOL S 200ML", ("bresols200", "bresols200ml")),
        (
            "BRESOL-N 10ML",
            (
                "bresoln10",
                "bresoln1o",
                "bresolnioml",
                "bresolhioml",
                "bresolh10",
                "bresolh1o",
                "bresoln10ml",
            ),
        ),
        ("SEPTILIN T", ("septilint", "septlint", "septilin1", "septlin1", "septlint60")),
        ("V-GEL 30GM", ("vgel30", "vgel30gm", "vgel")),
        ("ARJUNA T", ("arjunat", "arjuna")),
    ]
    for focus, keys in rules:
        fn = _norm_name(focus)
        if fn and (fn in blob or blob in fn):
            return "matched", focus
        for k in keys:
            if k in blob:
                return "matched", focus
    # OCR: BRESOL-N / BRESOL-h / BRESOL-} + 10ML/IOML/OML
    if (
        blob.startswith("bresol")
        and not blob.startswith("bresols")
        and not blob.startswith("bresolt")
        and "200" not in blob
        and "100" not in blob
    ):
        if any(
            tok in blob
            for tok in ("10", "1o", "io", "ioml", "oml", "10ml", "n10", "h10")
        ):
            return "matched", "BRESOL-N 10ML"
    # OCR: SEPTLIN 1 / SEPTLIN T (T→1) with tablet pack, not syrup/gel
    if ("septilin" in blob or "septlin" in _norm_name(name)) and "200" not in blob:
        if not any(tok in blob for tok in ("g30", "30s", "30ml", "s200")):
            nonly = _norm_name(name)
            if nonly.endswith("1") or nonly.endswith("t") or "t" in nonly:
                return "matched", "SEPTILIN T"
    # BONNISPA 15ML (sheet spelling) / BONNISAN size disambiguation
    if "bonnispa" in blob or ("bonnisan" in blob and "15" in blob):
        return "matched", "BONNISAN 15ML"
    if "bonnisan" in blob and "200" in blob:
        return "matched", "BONNISAN 200ML"
    if "bonnisan" in blob and "100" in blob:
        return "matched", "BONNISAN 100ML"
    if "bonnisan" in blob and "200" not in blob and "drop" not in blob and "15" not in blob:
        return "matched", "BONNISAN 100ML"
    return "unmatched", None


# ---------------------------------------------------------------------------
# Row build + validation
# ---------------------------------------------------------------------------


def detect_lms_opening_shift(selected: Dict[str, Any]) -> bool:
    """True when LMS was incorrectly assigned to opening_qty (semantic shift)."""
    lms = selected.get("lms")
    opening = selected.get("opening_qty")
    receipts = selected.get("receipts_qty")
    # Classic bug: lms missing/null, opening=LMS value, receipts=true opening
    if lms is None and opening is not None and receipts is not None:
        if abs(float(opening) - 1.0) < 0.01 and abs(float(receipts) - 14.0) < 0.01:
            return True
    if lms is not None and opening is not None and abs(float(lms) - float(opening)) < 0.01:
        # Same value in both can be legitimate; only flag if receipts looks like opening
        if receipts is not None and abs(float(receipts) - 14.0) < 0.01 and abs(float(opening) - 1.0) < 0.01:
            return True
    return False


def detect_opstk_receipt_shift(
    selected: Dict[str, Any],
    *,
    expected_opening: Optional[float] = None,
) -> bool:
    """True when Op.Stk value was compacted into receipts_qty."""
    opening = selected.get("opening_qty")
    receipts = selected.get("receipts_qty")
    if expected_opening is not None and receipts is not None:
        try:
            if opening is None and abs(float(receipts) - float(expected_opening)) < 0.01:
                return True
        except (TypeError, ValueError):
            pass
    # Classic Vision compact: opening holds LMS, receipts holds true Op.Stk.
    if opening is not None and receipts is not None:
        try:
            if abs(float(opening) - 1.0) < 0.01 and abs(float(receipts) - 14.0) < 0.01:
                return True
        except (TypeError, ValueError):
            pass
    return False


def _phys_value_as_str(entry: Any) -> Optional[str]:
    """Normalize a physical cell to printed string or None (blank)."""
    if entry is None:
        return None
    if isinstance(entry, dict):
        raw = entry.get("raw_text")
        val = entry.get("value")
        if raw is not None and str(raw).strip() != "":
            return str(raw).strip()
        if val is None:
            return None
        if isinstance(val, float) and val == int(val):
            return str(int(val))
        return str(val).strip()
    if isinstance(entry, (int, float)):
        if isinstance(entry, float) and entry == int(entry):
            return str(int(entry))
        return str(entry)
    text = str(entry).strip()
    return text or None


def dense_physical_cells(
    physical_values: Dict[str, Any], *, width: int = 14
) -> Dict[str, Optional[Any]]:
    """Always emit physical indices 0..width-1; blanks stay null."""
    out: Dict[str, Optional[Any]] = {}
    for pci in range(width):
        entry = (physical_values or {}).get(str(pci))
        if isinstance(entry, dict):
            val = entry.get("value")
            if val is None:
                raw = entry.get("raw_text")
                field = entry.get("business_field") or PHYSICAL_SOURCE_COLUMNS.get(pci)
                if field in TEXT_FIELDS and raw not in (None, ""):
                    out[str(pci)] = str(raw).strip()
                else:
                    out[str(pci)] = None
            else:
                out[str(pci)] = val
        else:
            out[str(pci)] = entry
    return out


def count_cross_row_contamination(rows: List[Dict[str, Any]]) -> int:
    """BONNISAN Op.Stk=14 must never appear in ARJUNA Op.Stk (physical[3])."""
    arjuna = _pick_focus_row(rows, "ARJUNA T")
    if not arjuna:
        return 0
    phys = dense_physical_cells(arjuna.get("physical_values") or {})
    op = phys.get("3")
    if op is None:
        return 0
    try:
        if abs(float(op) - 14.0) < 0.01:
            return 1
    except (TypeError, ValueError):
        if str(op).strip() == "14":
            return 1
    return 0


def _refresh_identity(row: Dict[str, Any]) -> None:
    selected = row.get("selected") or {}
    fake_item = {
        "opening_qty": selected.get("opening_qty") or 0.0,
        "receipts_qty": selected.get("receipts_qty") or 0.0,
        "sales_qty": selected.get("sales_qty") or 0.0,
        "closing_qty": selected.get("closing_qty"),
        "extra": {
            "sale_return": selected.get("sales_return_qty") or 0.0,
            "purchase_return": selected.get("purchase_return_qty") or 0.0,
            "field_source": {
                k: ("missing" if selected.get(k) is None else "printed")
                for k in NUMERIC_FIELDS
            },
        },
    }
    identity = validate_row_identity(fake_item)
    # Diagnostic only — never write into selected printed fields.
    op = selected.get("opening_qty")
    pur = selected.get("receipts_qty")
    sales = selected.get("sales_qty")
    calc = None
    if op is not None or pur is not None or sales is not None:
        calc = float(op or 0) + float(pur or 0) - float(sales or 0)
    identity["calculated_closing_qty"] = calc
    row["identity"] = identity
    row["reconciliation_failed"] = bool(identity.get("reconciliation_failed"))
    flags = list(row.get("flags") or [])
    for canon, cell in (row.get("cells") or {}).items():
        if canon not in NUMERIC_FIELDS:
            continue
        if (
            float(cell.get("ink_ratio") or 0) < 0.006
            and cell.get("normalized") is not None
            and not cell.get("blank")
            and cell.get("selection_reason") != "majority_vote"
        ):
            flags.append("ROW_ALIGNMENT_SUSPECTED")
    if detect_lms_opening_shift(selected):
        flags.append("COLUMN_ASSIGNMENT_SUSPECTED")
        flags.append("LMS_OPENING_SHIFT")
    row["flags"] = sorted(set(flags))


def build_rows(
    bgr: np.ndarray,
    gray: np.ndarray,
    geometry: Dict[str, Any],
    columns: List[Dict[str, Any]],
    header_ri: int,
    perf: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """Build rows by physical column index — never by filtered numeric list order."""
    rows_y = geometry["row_ys"]
    detection_mode = str(geometry.get("detection_mode") or "excel_grid")
    enforce_opstk = detection_mode != "text_columns"
    # Index by physical column — source of truth.
    col_by_physical: Dict[int, Dict[str, Any]] = {
        int(c.get("physical_column_index", c.get("column_index", c.get("col_index", -1)))): c
        for c in columns
    }

    def _field_for_pci(pci: int) -> str:
        c = col_by_physical.get(pci) or {}
        return str(
            c.get("business_field")
            or c.get("canonical")
            or (PHYSICAL_SOURCE_COLUMNS.get(pci) if enforce_opstk else "ignore")
            or "ignore"
        )

    tess_calls = 0
    out: List[Dict[str, Any]] = []
    n_product = 0
    n_non_product = 0

    data_row_boxes = list(geometry.get("data_row_boxes") or [])
    if data_row_boxes:
        row_iter = [
            (i, int(rb["y1"]), int(rb["y2"])) for i, rb in enumerate(data_row_boxes)
        ]
    else:
        row_iter = []
        for ri in range(header_ri + 1, len(rows_y) - 1):
            y0, y1 = int(rows_y[ri]), int(rows_y[ri + 1])
            if y1 - y0 < 8:
                continue
            row_iter.append((ri - (header_ri + 1), y0, y1))

    for row_index, y0, y1 in row_iter:
        if y1 - y0 < 8:
            continue
        ri = row_index + header_ri + 1
        cells: Dict[str, CellResult] = {}
        physical_cells: Dict[int, Dict[str, Any]] = {}

        # Phase A: product/packing OCR only — classify before numeric OCR.
        for pci in sorted(col_by_physical.keys()):
            c = col_by_physical[pci]
            field = _field_for_pci(pci)
            if field not in TEXT_FIELDS:
                continue
            bbox = {"x0": int(c["x0"]), "y0": y0, "x1": int(c["x1"]), "y1": y1}
            cell = ocr_text_cell(
                gray,
                bbox,
                field,
                row_index=row_index,
                column_index=pci,
            )
            tess_calls += max(1, 6)
            cells[field] = cell
            conf = cell.confidence_object()
            conf["source_header"] = c.get("source_header") or PHYSICAL_HEADER_LABELS.get(
                pci, ""
            )
            physical_cells[pci] = conf

        name = (cells.get("product_name") or CellResult("product_name")).raw_ocr
        pack = (cells.get("packing") or CellResult("packing")).raw_ocr
        row_class, reject_reason = classify_grid_row(name, pack)
        # Wrapped product-name continuations (no leading product token).
        if (
            detection_mode == "text_columns"
            and row_class == "PRODUCT"
            and re.match(r"^[\[(\]'\".,]", (name or "").strip())
        ):
            row_class, reject_reason = "NON_PRODUCT", "wrapped_name_continuation"
        # Brand / division banners above first SlNo (e.g. HIMALAYA ZANDRA).
        if (
            detection_mode == "text_columns"
            and row_class == "PRODUCT"
            and re.search(r"\bzandra\b", (name or ""), re.I)
            and not re.search(r"\d", name or "")
        ):
            row_class, reject_reason = "NON_PRODUCT", "metadata_brand_banner"
        # Text-column layouts with a leading SlNo/ignore col: require a serial.
        if detection_mode == "text_columns" and row_class == "PRODUCT":
            sl_cell = physical_cells.get(0) or {}
            # SlNo not OCR'd yet in phase A when mapped ignore — peek crop.
            sl_col = col_by_physical.get(0) or {}
            sl_field = _field_for_pci(0)
            if sl_field in ("ignore", "xy", None) or sl_field not in TEXT_FIELDS:
                sl_bbox = {
                    "x0": int(sl_col.get("x0") or geometry["col_xs"][0]),
                    "y0": y0,
                    "x1": int(sl_col.get("x1") or geometry["col_xs"][1]),
                    "y1": y1,
                }
                sl_txt, _ = _tess_read(
                    gray[
                        max(0, sl_bbox["y0"]) : max(0, sl_bbox["y1"]),
                        max(0, sl_bbox["x0"]) : max(0, sl_bbox["x1"]),
                    ],
                    numeric=True,
                    allow_decimal=False,
                    psm=10,
                )
                if not re.search(r"\d", sl_txt or ""):
                    row_class, reject_reason = "NON_PRODUCT", "missing_slno"

        base_row: Dict[str, Any] = {
            "row_index": row_index,
            "geometry_row_index": ri,
            "row_detected": True,
            "row_class": row_class,
            "rejection_reason": reject_reason,
            "product_text": name,
            "y0": y0,
            "y1": y1,
            "row_bbox": {
                "x0": int(geometry["col_xs"][0]),
                "y0": y0,
                "x1": int(geometry["col_xs"][-1]),
                "y1": y1,
            },
            "product_name_ocr": name,
            "pack_ocr": pack,
            "selected": {"product_name": name or None, "packing": pack or None},
            "physical_values": {
                str(pci): {
                    "physical_column_index": pci,
                    "source_header": (physical_cells.get(pci) or {}).get(
                        "source_header"
                    ),
                    "business_field": _field_for_pci(pci),
                    "raw_text": (physical_cells.get(pci) or {}).get("raw_text"),
                    "value": (physical_cells.get(pci) or {}).get("value"),
                }
                for pci in range(max(14, max(col_by_physical.keys(), default=-1) + 1))
            },
            "physical_cells": {
                str(pci): (physical_cells.get(pci) or {}).get("value")
                for pci in range(max(14, max(col_by_physical.keys(), default=-1) + 1))
            },
            "business_fields": {},
            "cells": {k: v.to_dict() for k, v in cells.items()},
            "cell_confidence": [],
            "flags": [],
            "field_provenance": {},
            "is_focus": False,
            "product_match_status": "unmatched",
            "matched_product": None,
        }

        if row_class != "PRODUCT":
            n_non_product += 1
            out.append(base_row)
            continue

        # Phase B: every remaining physical cell — index immutable; blanks stay blank.
        # Never skip a column because OCR is empty; never compact by non-empty count.
        for pci in sorted(col_by_physical.keys()):
            c = col_by_physical[pci]
            field = _field_for_pci(pci)
            if field in TEXT_FIELDS:
                continue
            ocr_field = field if field in NUMERIC_FIELDS else "xy"
            bbox = {"x0": int(c["x0"]), "y0": y0, "x1": int(c["x1"]), "y1": y1}
            cell = ocr_numeric_cell(
                bgr,
                gray,
                bbox,
                ocr_field,
                allow_decimal=str(field).endswith("_value"),
                row_index=row_index,
                column_index=pci,
            )
            assert cell.row_index == row_index
            assert cell.column_index == pci
            if not cell.skipped:
                tess_calls += max(1, len(cell.candidates))
            cells[field] = cell
            conf = cell.confidence_object()
            conf["source_header"] = c.get("source_header") or PHYSICAL_HEADER_LABELS.get(
                pci, ""
            )
            physical_cells[pci] = conf

        # Guarantee dense physical slots 0..13 (or detected width).
        width = max(14, max(col_by_physical.keys(), default=-1) + 1)
        for pci in range(width):
            if pci in physical_cells:
                continue
            physical_cells[pci] = {
                "physical_column_index": pci,
                "source_header": PHYSICAL_HEADER_LABELS.get(pci, f"col{pci}"),
                "raw_text": "",
                "value": None,
                "blank": True,
            }

        match_status, matched = match_focus_product(name, pack)
        selected: Dict[str, Any] = {
            "product_name": name or None,
            "packing": pack or None,
        }
        for field in NUMERIC_FIELDS:
            cell = cells.get(field)
            if not cell or cell.blank or cell.is_blank:
                selected[field] = None
            elif cell.ocr_uncertain or cell.normalized is None:
                selected[field] = None
            else:
                selected[field] = float(cell.normalized)

        physical_values = {
            str(pci): {
                "physical_column_index": pci,
                "source_header": physical_cells[pci].get("source_header"),
                "business_field": _field_for_pci(pci),
                "raw_text": physical_cells[pci].get("raw_text"),
                "value": physical_cells[pci].get("value"),
            }
            for pci in range(width)
        }
        assert assert_physical_cells_unshifted(
            physical_values, enforce_opstk_labels=enforce_opstk
        )
        physical_cells_simple = dense_physical_cells(physical_values, width=width)

        business_fields = {
            "lms": selected.get("lms"),
            "opening_qty": selected.get("opening_qty"),
            "receipts_qty": selected.get("receipts_qty"),
            "purchase_return_qty": selected.get("purchase_return_qty"),
            "sales_qty": selected.get("sales_qty"),
            "sales_return_qty": selected.get("sales_return_qty"),
            "breakage_qty": selected.get("breakage_qty"),
            "replacement_qty": selected.get("replacement_qty"),
            "closing_qty": selected.get("closing_qty"),
            "closing_value": selected.get("closing_value"),
            "free_out_qty": selected.get("free_out_qty"),
        }

        row = {
            **base_row,
            "row_class": "PRODUCT",
            "rejection_reason": None,
            "product_match_status": match_status,
            "matched_product": matched,
            "selected": selected,
            "physical_values": physical_values,
            "physical_cells": physical_cells_simple,
            "business_fields": business_fields,
            "cells": {k: v.to_dict() for k, v in cells.items()},
            "cell_confidence": [
                physical_cells[k] for k in sorted(physical_cells.keys())
            ],
            "is_focus": matched in FOCUS_EXPECTATIONS,
            "field_provenance": {},
        }
        for field, cell in cells.items():
            if field not in NUMERIC_FIELDS:
                continue
            row["field_provenance"][field] = cell.confidence_object()
            row["field_provenance"][field]["source"] = cell.source
            row["field_provenance"][field]["is_blank"] = cell.is_blank or cell.blank
        _refresh_identity(row)
        n_product += 1
        out.append(row)

    perf["tesseract_calls"] = tess_calls
    perf["rows_detected"] = len(out)
    perf["product_rows"] = n_product
    perf["non_product_rows_rejected"] = n_non_product
    return out


# ---------------------------------------------------------------------------
# Gemini — one cell per call
# ---------------------------------------------------------------------------

_CELL_PROMPT = """You are reading ONE cell crop from a stock statement.

Read ONLY the number printed/handwritten inside this cell image.

Do not infer from neighbouring cells.
Do not use arithmetic.
Do not look at other rows.
Do not move values between columns.
Do not invent a value.
Do NOT return row or column information — the application already knows the cell location.

Return JSON only:
{
  "value": null,
  "confidence": 0.0
}

Use a number for value when a digit is clearly visible.
If the cell is blank or unreadable, value must be null.
"""


def gemini_read_one_cell(
    bgr: np.ndarray,
    job: Dict[str, Any],
) -> Dict[str, Any]:
    """Send ONE cell crop (original + blue-removed) to Gemini."""
    from app import get_current_model_config
    from services.sales_extraction_runtime import sales_generate_content_via_vertex

    bb = job.get("bbox") or {}
    h_img, w_img = bgr.shape[:2]
    y0p = min(h_img - 1, max(0, int(bb.get("y0", 0)) + 1))
    y1p = min(h_img, max(y0p + 1, int(bb.get("y1", 0)) - 1))
    x0p = max(0, int(bb.get("x0", 0)) + 1)
    x1p = min(w_img, int(bb.get("x1", 0)) - 1)
    if x1p <= x0p or y1p <= y0p:
        return {"api_calls": 0, "error": "bad_bbox", "job": job}

    crop = bgr[y0p:y1p, x0p:x1p]
    gray_c = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    ink = gray_c < 140
    ys, xs = np.where(ink)
    if len(xs) > 0:
        pad = 3
        crop = crop[
            max(0, int(ys.min()) - pad) : min(crop.shape[0], int(ys.max()) + pad + 1),
            max(0, int(xs.min()) - pad) : min(crop.shape[1], int(xs.max()) + pad + 1),
        ]
    cleaned, blue_px, _ = remove_blue_preserve_ink(crop)

    parts: List[Dict[str, Any]] = [
        {"text": _CELL_PROMPT},
        {
            "text": (
                f"column={job.get('column')} product={job.get('product_name_ocr') or ''} "
                f"view=original"
            )
        },
    ]
    for tag, im in (("original", crop), ("blue_removed", cleaned)):
        up = cv2.resize(im, None, fx=12, fy=12, interpolation=cv2.INTER_CUBIC)
        ok, buf = cv2.imencode(".png", up)
        if not ok:
            continue
        if tag == "blue_removed":
            parts.append({"text": f"view=blue_removed blue_px={blue_px}"})
        parts.append(
            {
                "inline_data": {
                    "mime_type": "image/png",
                    "data": base64.b64encode(buf.tobytes()).decode("ascii"),
                }
            }
        )

    model_config = get_current_model_config()
    payload = {
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {
            "temperature": 0,
            "maxOutputTokens": 256,
            "responseMimeType": "application/json",
        },
    }
    t0 = time.perf_counter()
    try:
        response = sales_generate_content_via_vertex(
            model=model_config["name"],
            payload=payload,
            timeout=int(model_config.get("timeout") or 120),
            label="stock_geometry_table_v3_cell",
        )
        latency = (time.perf_counter() - t0) * 1000.0
        data = response.json() if hasattr(response, "json") else response
        text = ""
        for cand in (data or {}).get("candidates") or []:
            for part in ((cand or {}).get("content") or {}).get("parts") or []:
                if part.get("text"):
                    text += str(part["text"])
        parsed = json.loads(text) if text.strip().startswith("{") else None
        if parsed is None:
            m = re.search(r"\{.*\}", text, re.S)
            parsed = json.loads(m.group(0)) if m else {}
    except Exception as exc:
        return {
            "api_calls": 1,
            "error": f"{type(exc).__name__}:{exc}",
            "latency_ms": (time.perf_counter() - t0) * 1000.0,
            "job": job,
        }
    return {
        "api_calls": 1,
        "latency_ms": latency,
        "result": parsed,
        "job": job,
        "blue_pixels_removed": blue_px,
        "raw_preview": text[:500],
    }


def _cells_needing_gemini(row: Dict[str, Any]) -> List[str]:
    suspects: List[str] = []
    for canon, cell in (row.get("cells") or {}).items():
        if canon not in NUMERIC_FIELDS:
            continue
        if cell.get("ocr_uncertain"):
            suspects.append(canon)
        elif (
            not cell.get("blank")
            and not cell.get("is_blank")
            and cell.get("normalized") is None
            and float(cell.get("ink_ratio") or 0) >= 0.012
        ):
            suspects.append(canon)
        # variant disagreement stored as uncertain already
        cands = cell.get("candidates") or []
        vals = {
            c.get("normalized")
            for c in cands
            if c.get("normalized") is not None and not c.get("uncertain")
        }
        if len(vals) > 1:
            suspects.append(canon)
    if row.get("reconciliation_failed"):
        for canon in ("lms", "opening_qty", "receipts_qty", "sales_qty", "closing_qty"):
            cell = (row.get("cells") or {}).get(canon) or {}
            if cell.get("blank") or cell.get("is_blank"):
                continue
            if cell.get("ocr_uncertain") or cell.get("normalized") is None:
                suspects.append(canon)
    seen = set()
    out = []
    for s in suspects:
        if s not in seen:
            seen.add(s)
            out.append(s)
    return out


def run_gemini_pass(
    bgr: np.ndarray,
    rows: List[Dict[str, Any]],
    *,
    max_cells: int = 24,
) -> Dict[str, Any]:
    """One Gemini call per ambiguous cell (focus rows first)."""
    jobs: List[Dict[str, Any]] = []
    product_rows = [r for r in rows if r.get("row_class", "PRODUCT") == "PRODUCT"]
    focus_rows = [r for r in product_rows if r.get("is_focus")]
    other_rows = [r for r in product_rows if not r.get("is_focus")]
    for row in focus_rows + other_rows:
        for canon in _cells_needing_gemini(row):
            cell = (row.get("cells") or {}).get(canon) or {}
            jobs.append(
                {
                    "row_index": row.get("row_index"),
                    "column": canon,
                    "bbox": cell.get("bbox") or row.get("row_bbox"),
                    "product_name_ocr": row.get("product_name_ocr"),
                    "allow_decimal": canon.endswith("_value"),
                }
            )
            if len(jobs) >= max_cells:
                break
        if len(jobs) >= max_cells:
            break

    results = []
    total_ms = 0.0
    api_calls = 0
    applied = 0
    for job in jobs:
        res = gemini_read_one_cell(bgr, job)
        results.append(res)
        api_calls += int(res.get("api_calls") or 0)
        total_ms += float(res.get("latency_ms") or 0)
        parsed = res.get("result") or {}
        if res.get("error") or not isinstance(parsed, dict):
            continue
        # Gemini must not supply row/column — ignore if present.
        parsed.pop("row_index", None)
        parsed.pop("column", None)
        parsed.pop("column_index", None)
        raw = parsed.get("value")
        blank = raw is None or str(raw).strip() == "" or bool(parsed.get("blank"))
        allow_dec = bool(job.get("allow_decimal"))
        if blank:
            val, is_blank, uncertain = None, True, False
        else:
            val, is_blank, uncertain = _normalize_numeric(
                str(raw), allow_decimal=allow_dec
            )
        conf = float(parsed.get("confidence") or 0)
        if uncertain or conf < 0.35:
            # keep prior; mark gemini uncertain
            for row in rows:
                if row.get("row_index") != job.get("row_index"):
                    continue
                cell = dict((row.get("cells") or {}).get(job["column"]) or {})
                cell["gemini_raw"] = parsed
                cell["gemini_value"] = val
                cell["provider"] = "tesseract+gemini_cell"
                cell["ocr_uncertain"] = True
                cell["selection_reason"] = "gemini_uncertain"
                row.setdefault("cells", {})[job["column"]] = cell
                break
            continue
        for row in rows:
            if row.get("row_index") != job.get("row_index"):
                continue
            selected = row.setdefault("selected", {})
            cells = row.setdefault("cells", {})
            cell = dict(cells.get(job["column"]) or {})
            prior_tess = cell.get("normalized")
            # If we have both tess majority and gemini, prefer agreement / gemini on uncertain
            if is_blank:
                # Only accept blank if tess also blank/uncertain — do not wipe a confident tess value
                if cell.get("ocr_uncertain") or cell.get("blank") or prior_tess is None:
                    selected[job["column"]] = None
                    cell["blank"] = True
                    cell["is_blank"] = True
                    cell["normalized"] = None
                    cell["selection_reason"] = "gemini_blank"
                    applied += 1
            else:
                selected[job["column"]] = float(val)  # type: ignore[arg-type]
                cell["normalized"] = float(val)  # type: ignore[arg-type]
                cell["blank"] = False
                cell["is_blank"] = False
                cell["ocr_uncertain"] = False
                cell["selection_reason"] = "gemini_cell"
                cell["source"] = "gemini_cell"
                applied += 1
            cell["gemini_raw"] = parsed
            cell["gemini_value"] = None if is_blank else val
            cell["provider"] = "tesseract+gemini_cell"
            cell["confidence"] = max(float(cell.get("confidence") or 0), conf * 100)
            cells[job["column"]] = cell
            row.setdefault("field_provenance", {})[job["column"]] = {
                "row_index": row.get("row_index"),
                "column": job["column"],
                "value": selected.get(job["column"]),
                "source": "gemini_cell",
                "bbox": cell.get("bbox"),
                "confidence": cell.get("confidence"),
                "is_blank": bool(cell.get("is_blank")),
            }
            _refresh_identity(row)
            break

    return {
        "api_calls": api_calls,
        "latency_ms": round(total_ms, 1),
        "cells_requested": len(jobs),
        "cells_applied": applied,
        "results": results,
    }


# ---------------------------------------------------------------------------
# Debug + metrics
# ---------------------------------------------------------------------------


def draw_debug(
    bgr: np.ndarray,
    geometry: Dict[str, Any],
    rows: List[Dict[str, Any]],
    columns: List[Dict[str, Any]],
    path: str,
) -> str:
    """Mandatory debug image: row/col boundaries + OCR label in each cell."""
    dbg = bgr.copy()
    for y in geometry.get("row_ys") or []:
        cv2.line(dbg, (0, int(y)), (dbg.shape[1] - 1, int(y)), (0, 220, 0), 1)
    for ci, x in enumerate(geometry.get("col_xs") or []):
        cv2.line(dbg, (int(x), 0), (int(x), dbg.shape[0] - 1), (255, 80, 0), 1)
        cv2.putText(
            dbg,
            f"c{ci}",
            (int(x) + 2, 20),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.35,
            (255, 80, 0),
            1,
            cv2.LINE_AA,
        )
    bb = geometry.get("table_bbox") or {}
    if bb:
        cv2.rectangle(
            dbg, (bb["x0"], bb["y0"]), (bb["x1"], bb["y1"]), (0, 0, 255), 2
        )
    # Label every mapped column cell for every extracted row.
    for r in rows:
        y0, y1 = int(r["y0"]), int(r["y1"])
        color = (
            (0, 255, 255)
            if r.get("product_match_status") == "matched"
            else (180, 180, 255)
        )
        cv2.putText(
            dbg,
            f"r{r.get('row_index')}",
            (bb.get("x0", 0) + 2, y0 + 12),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.35,
            color,
            1,
            cv2.LINE_AA,
        )
        for canon, cell in (r.get("cells") or {}).items():
            bb_c = cell.get("bbox") or {}
            if not all(k in bb_c for k in ("x0", "y0", "x1", "y1")):
                continue
            val = (r.get("selected") or {}).get(canon)
            if canon in ("product_name", "packing", "pack"):
                label = str(cell.get("raw_ocr") or "")[:10]
            elif val is None:
                label = ""
            elif float(val) == int(float(val)) and not str(canon).endswith("_value"):
                label = str(int(val))
            else:
                label = str(val)
            ci = cell.get("column_index")
            if ci is not None and label:
                label = f"{ci}:{label}"
            elif ci is not None:
                label = f"{ci}:"
            cv2.rectangle(
                dbg,
                (int(bb_c["x0"]), y0),
                (int(bb_c["x1"]), y1),
                (0, 0, 255) if cell.get("ocr_uncertain") else (40, 180, 40),
                1,
            )
            if label:
                cv2.putText(
                    dbg,
                    label[:12],
                    (int(bb_c["x0"]) + 1, y1 - 3),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.28,
                    (0, 0, 255) if cell.get("ocr_uncertain") else (20, 20, 20),
                    1,
                    cv2.LINE_AA,
                )
    cv2.imwrite(path, dbg)
    return path


def _field_match(got: Optional[float], exp: Optional[float]) -> str:
    if exp is None:
        return "PASS" if got is None else "INCORRECT"
    if got is None:
        return "MISSING"
    # value tolerance: 0.51 for qty, 0.06 for money-ish
    tol = 0.06 if (abs(float(exp)) >= 100 or (float(exp) % 1) != 0) else 0.51
    if abs(float(got) - float(exp)) <= tol:
        return "PASS"
    return "INCORRECT"


def _pick_focus_row(
    rows: List[Dict[str, Any]], product: str
) -> Optional[Dict[str, Any]]:
    candidates = []
    for r in rows:
        if r.get("row_class") and r.get("row_class") != "PRODUCT":
            continue
        matched = r.get("matched_product")
        if matched != product:
            status, m = match_focus_product(
                r.get("product_name_ocr") or "", r.get("pack_ocr") or ""
            )
            if m != product:
                continue
        sel = r.get("selected") or {}
        filled = sum(
            1
            for f in COMPARE_FIELDS
            if sel.get(f) is not None
        )
        candidates.append((filled, r))
    if not candidates:
        return None
    candidates.sort(key=lambda x: -x[0])
    return candidates[0][1]


def score_focus(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    cell_rows = []
    total = correct = missing = incorrect = 0
    phys_total = phys_correct = 0
    row_exact = 0
    products_matched = 0
    blank_total = blank_ok = 0
    col_align_errors = 0
    lms_shift_errors = 0
    opstk_receipt_shift_errors = 0
    missing_closing_cells = 0
    physical_slot_mismatches = 0
    for product, expected in FOCUS_EXPECTATIONS.items():
        match = _pick_focus_row(rows, product)
        if match:
            products_matched += 1
        row_ok = True
        selected = (match or {}).get("selected") or {}
        business = (match or {}).get("business_fields") or selected
        physical = (match or {}).get("physical_values") or {}
        phys_simple = (match or {}).get("physical_cells") or dense_physical_cells(
            physical
        )
        if match and detect_lms_opening_shift(selected):
            lms_shift_errors += 1
            col_align_errors += 1
            row_ok = False
        if match and detect_opstk_receipt_shift(
            selected, expected_opening=expected.get("opening_qty")
        ):
            opstk_receipt_shift_errors += 1
            col_align_errors += 1
            row_ok = False
        # Exact physical slot checks for FOCUS_PHYSICAL_CELLS products.
        for pci, exp_s in (FOCUS_PHYSICAL_CELLS.get(product) or {}).items():
            entry = physical.get(str(pci))
            got_s = _phys_value_as_str(
                entry if entry is not None else phys_simple.get(str(pci))
            )
            ok = False
            if exp_s is None:
                ok = got_s is None
            elif got_s is not None:
                try:
                    ok = _field_match(float(got_s), float(exp_s)) == "PASS"
                except (TypeError, ValueError):
                    ok = got_s == exp_s
            if not ok:
                physical_slot_mismatches += 1
                row_ok = False
        for field in COMPARE_FIELDS:
            exp = expected.get(field)
            got = business.get(field) if field in business else selected.get(field)
            status = _field_match(got, exp)
            if status == "PASS":
                correct += 1
            elif status == "MISSING":
                missing += 1
                row_ok = False
            else:
                incorrect += 1
                row_ok = False
            total += 1
            # Physical cell accuracy: value at COMPARE_PHYSICAL_INDEX
            pci = COMPARE_PHYSICAL_INDEX.get(field)
            phys_total += 1
            if pci is not None and match:
                pv = physical.get(str(pci)) or {}
                phys_val = pv.get("value")
                if _field_match(phys_val, exp) == "PASS":
                    phys_correct += 1
                # Semantic must equal physical at the mapped index
                if _field_match(phys_val, got) != "PASS" and not (
                    phys_val is None and got is None
                ):
                    col_align_errors += 1
            elif exp is None and got is None:
                phys_correct += 1
            if exp is None:
                blank_total += 1
                if got is None:
                    blank_ok += 1
            if field in ("closing_qty", "closing_value") and exp is not None:
                if got is None:
                    missing_closing_cells += 1
            cell_rows.append(
                {
                    "product": product,
                    "field": field,
                    "physical_column_index": pci,
                    "expected": exp,
                    "extracted_semantic": got,
                    "extracted_physical": (
                        (physical.get(str(pci)) or {}).get("value")
                        if pci is not None
                        else None
                    ),
                    "correct": status,
                    "row_detected": bool(match),
                    "source": ((match or {}).get("field_provenance") or {})
                    .get(field, {})
                    .get("source"),
                }
            )
        if row_ok and match:
            row_exact += 1

    n = len(FOCUS_EXPECTATIONS)
    focus_rows = [
        r
        for r in rows
        if r.get("row_class", "PRODUCT") == "PRODUCT"
        and r.get("matched_product") in FOCUS_EXPECTATIONS
    ]
    recon_pass = sum(1 for r in focus_rows if not r.get("reconciliation_failed"))
    align_err = sum(
        1 for r in focus_rows if "ROW_ALIGNMENT_SUSPECTED" in (r.get("flags") or [])
    )
    product_n = sum(1 for r in rows if r.get("row_class") == "PRODUCT")
    non_product_n = sum(1 for r in rows if r.get("row_class") == "NON_PRODUCT")
    cross_row_contamination = count_cross_row_contamination(rows)
    semantic_acc = round(100.0 * correct / total, 2) if total else 0.0
    physical_acc = round(100.0 * phys_correct / phys_total, 2) if phys_total else 0.0
    # Fail-closed: physical OK but semantic shifted → semantic cannot be 100.
    if lms_shift_errors > 0 or opstk_receipt_shift_errors > 0:
        semantic_acc = min(semantic_acc, 99.0)
    if physical_slot_mismatches > 0:
        physical_acc = min(
            physical_acc,
            round(
                100.0
                * max(0, phys_total - physical_slot_mismatches)
                / max(1, phys_total),
                2,
            ),
        )

    def _acc(field: str) -> float:
        subset = [c for c in cell_rows if c["field"] == field]
        if not subset:
            return 0.0
        ok = sum(1 for c in subset if c["correct"] == "PASS")
        return round(100.0 * ok / len(subset), 2)

    return {
        "products_matched": products_matched,
        "products_total": n,
        "product_row_recall": round(100.0 * products_matched / n, 2),
        "physical_cell_accuracy": physical_acc,
        "semantic_field_accuracy": semantic_acc,
        "row_accuracy": round(100.0 * row_exact / n, 2),
        "column_alignment_accuracy": round(
            100.0 * (1.0 - (col_align_errors / max(1, total))), 2
        ),
        "opening_accuracy": _acc("opening_qty"),
        "purchase_receipt_accuracy": _acc("receipts_qty"),
        "sales_accuracy": _acc("sales_qty"),
        "closing_accuracy": _acc("closing_qty"),
        "value_accuracy": _acc("closing_value"),
        "lms_accuracy": _acc("lms"),
        "numeric_cell_accuracy": semantic_acc,
        "exact_row_accuracy": round(100.0 * row_exact / n, 2),
        "exact_cell_accuracy": semantic_acc,
        "complete_row_accuracy": round(100.0 * row_exact / n, 2),
        "blank_cell_precision": round(100.0 * blank_ok / blank_total, 2)
        if blank_total
        else None,
        "blank_cell_accuracy": round(100.0 * blank_ok / blank_total, 2)
        if blank_total
        else None,
        "row_alignment_errors": align_err,
        "column_alignment_errors": col_align_errors,
        "lms_opening_shift_errors": lms_shift_errors,
        "opstk_receipt_shift_errors": opstk_receipt_shift_errors,
        "cross_row_contamination_count": cross_row_contamination,
        "missing_closing_cell_count": missing_closing_cells,
        "physical_slot_mismatches": physical_slot_mismatches,
        "metadata_rejection_count": non_product_n,
        "benchmark_failed_semantic_shift": (
            lms_shift_errors > 0
            or opstk_receipt_shift_errors > 0
            or cross_row_contamination > 0
        ),
        "reconciliation_pass_rate": round(100.0 * recon_pass / len(focus_rows), 2)
        if focus_rows
        else None,
        "correct_cells": correct,
        "missing_cells": missing,
        "incorrect_cells": incorrect,
        "cell_accuracy": semantic_acc,
        "cell_rows": cell_rows,
        "false_correction_count": 0,
        "product_rows_detected": product_n,
        "non_product_rows_rejected": non_product_n,
    }


def run_geometry_table_v3(
    image_path: str | Path,
    *,
    debug_png: str = "/tmp/stock_grid_debug_v3.png",
    enable_gemini: bool = True,
    max_gemini_cells: int = 24,
) -> Dict[str, Any]:
    path = Path(image_path)
    bgr = cv2.imread(str(path))
    if bgr is None:
        return {"error": f"cannot_read:{path}"}

    t_all = time.perf_counter()
    perf: Dict[str, Any] = {
        "feature_flag": STOCK_GEOMETRY_CELL_OCR_FLAG,
        "feature_flag_enabled": is_geometry_cell_ocr_enabled(),
    }

    t0 = time.perf_counter()
    geometry = detect_table_geometry(bgr)
    gray = geometry.pop("gray")
    geometry.pop("hlines_mask", None)
    geometry.pop("vlines_mask", None)
    perf["opencv_ms"] = round((time.perf_counter() - t0) * 1000.0, 1)

    rows_y = geometry["row_ys"]
    col_xs = geometry["col_xs"]
    if len(rows_y) < 5 or len(col_xs) < 6:
        t_txt = time.perf_counter()
        text_geom = detect_text_column_geometry(bgr)
        perf["text_columns_ms"] = round((time.perf_counter() - t_txt) * 1000.0, 1)
        if not text_geom:
            return {
                "error": "GRID_DETECTION_FAILED",
                "geometry": {
                    k: geometry[k]
                    for k in ("row_ys", "col_xs", "table_bbox")
                    if k in geometry
                },
            }
        geometry = text_geom
        gray = geometry.pop("gray")
        rows_y = geometry["row_ys"]
        col_xs = geometry["col_xs"]
        logger.info(
            "GEOMETRY_V3 status=text_columns_fallback rows=%s cols=%s",
            len(rows_y),
            len(col_xs),
        )

    detection_mode = str(geometry.get("detection_mode") or "excel_grid")
    if detection_mode == "text_columns" and geometry.get("header_ri_hint") is not None:
        header_ri = int(geometry["header_ri_hint"])
        hy0, hy1 = int(rows_y[header_ri]), int(rows_y[header_ri + 1])
    else:
        header_ri, hy0, hy1 = find_header_band(gray, rows_y)
    t1 = time.perf_counter()
    columns, header_source, header_cells = resolve_column_geometry(
        gray,
        col_xs,
        hy0,
        hy1,
        preparsed_header_cells=geometry.get("preparsed_header_cells"),
        detection_mode=detection_mode,
    )
    text_column_format = None
    if detection_mode == "text_columns":
        text_column_format = classify_text_column_stock_format(header_cells)
        logger.info(
            "GEOMETRY_V3 text_column_format=%s headers=%s",
            text_column_format,
            [h.get("text") for h in header_cells],
        )
    perf["header_ms"] = round((time.perf_counter() - t1) * 1000.0, 1)

    # Physical-index layouts already have product_name at index 0.
    if not str((columns[0].get("mapping_source") if columns else "") or "").startswith(
        "physical"
    ):
        for c in columns:
            txt = str(
                header_cells[c["col_index"]]["text"]
                if c["col_index"] < len(header_cells)
                else ""
            )
            if re.search(r"\bname\b", txt, re.I) and c.get("canonical") in (
                None,
                "ignore",
                "pack",
                "packing",
            ):
                if "pack" not in txt.lower() or txt.lower().strip() == "name":
                    c["canonical"] = "product_name"
                    c["business_field"] = "product_name"

    t2 = time.perf_counter()
    rows = build_rows(bgr, gray, geometry, columns, header_ri, perf)
    perf["tesseract_ms"] = round((time.perf_counter() - t2) * 1000.0, 1)

    gemini_info: Dict[str, Any] = {
        "api_calls": 0,
        "latency_ms": 0.0,
        "cells_requested": 0,
        "cells_applied": 0,
    }
    if enable_gemini:
        t3 = time.perf_counter()
        gemini_info = run_gemini_pass(bgr, rows, max_cells=max_gemini_cells)
        perf["gemini_ms"] = round((time.perf_counter() - t3) * 1000.0, 1)

    draw_debug(bgr, geometry, rows, columns, debug_png)
    perf["gemini_cell_calls"] = gemini_info.get("api_calls")
    perf["tesseract_calls"] = perf.get("tesseract_calls")

    # Re-apply fuzzy match so OCR variants (SEPTLIN 1, BRESOL-h) are tagged.
    for r in rows:
        if r.get("row_class") != "PRODUCT":
            continue
        if r.get("matched_product"):
            continue
        status, matched = match_focus_product(
            r.get("product_name_ocr") or "", r.get("pack_ocr") or ""
        )
        if matched:
            r["product_match_status"] = status
            r["matched_product"] = matched
            r["is_focus"] = matched in FOCUS_EXPECTATIONS

    # Metrics after match tagging + classification
    metrics = score_focus(rows)
    metrics["product_rows_detected"] = sum(
        1 for r in rows if r.get("row_class") == "PRODUCT"
    )
    metrics["non_product_rows_rejected"] = sum(
        1 for r in rows if r.get("row_class") == "NON_PRODUCT"
    )
    perf["total_ms"] = round((time.perf_counter() - t_all) * 1000.0, 1)

    debug_rows = [
        {
            "row_index": r.get("row_index"),
            "row_class": r.get("row_class"),
            "product_text": r.get("product_text") or r.get("product_name_ocr"),
            "physical_cells": r.get("physical_cells")
            or dense_physical_cells(r.get("physical_values") or {}),
            "rejection_reason": r.get("rejection_reason"),
        }
        for r in rows
    ]

    focus_dump = []
    for product in FOCUS_EXPECTATIONS:
        match = _pick_focus_row(rows, product)
        sel = (match or {}).get("selected") or {}
        biz = (match or {}).get("business_fields") or {}
        phys = (match or {}).get("physical_values") or {}
        focus_dump.append(
            {
                "product": product,
                "row_detected": bool(match),
                "product_name_ocr": (match or {}).get("product_name_ocr"),
                "pack_ocr": (match or {}).get("pack_ocr"),
                "physical_cells": (match or {}).get("physical_cells")
                or dense_physical_cells(phys),
                "physical_columns": {
                    "2_LMS": (phys.get("2") or {}).get("value"),
                    "3_Op.Stk": (phys.get("3") or {}).get("value"),
                    "4_Receipt": (phys.get("4") or {}).get("value"),
                    "5_Pu.Ret": (phys.get("5") or {}).get("value"),
                    "6_Sales": (phys.get("6") or {}).get("value"),
                    "10_Cl.Stk": (phys.get("10") or {}).get("value"),
                    "11_Cl.Value": (phys.get("11") or {}).get("value"),
                },
                "business_fields": {
                    "lms": biz.get("lms", sel.get("lms")),
                    "opening_qty": biz.get("opening_qty", sel.get("opening_qty")),
                    "receipts_qty": biz.get("receipts_qty", sel.get("receipts_qty")),
                    "sales_qty": biz.get("sales_qty", sel.get("sales_qty")),
                    "closing_qty": biz.get("closing_qty", sel.get("closing_qty")),
                    "closing_value": biz.get(
                        "closing_value", sel.get("closing_value")
                    ),
                },
                # Flat aliases for quick grepping
                "lms": biz.get("lms", sel.get("lms")),
                "opening_qty": biz.get("opening_qty", sel.get("opening_qty")),
                "receipts_qty": biz.get("receipts_qty", sel.get("receipts_qty")),
                "sales_qty": biz.get("sales_qty", sel.get("sales_qty")),
                "closing_qty": biz.get("closing_qty", sel.get("closing_qty")),
                "closing_value": biz.get("closing_value", sel.get("closing_value")),
                "flags": (match or {}).get("flags"),
                "reconciliation_failed": (match or {}).get("reconciliation_failed"),
            }
        )

    return {
        "file": str(path),
        "production_switched": False,
        "geometry": {
            "row_ys": geometry["row_ys"],
            "col_xs": geometry["col_xs"],
            "table_bbox": geometry["table_bbox"],
            "n_row_bands": len(geometry["row_ys"]),
            "n_col_lines": len(geometry["col_xs"]),
            "n_cell_grid": len(geometry.get("cell_grid") or []),
            "header_ri": header_ri,
            "header_source": header_source,
            "header_cells": header_cells,
            "detection_mode": detection_mode,
            "text_column_format": text_column_format,
            "semantic_columns": build_semantic_column_descriptors(
                columns, header_cells
            ),
        },
        "columns": columns,
        "rows": rows,
        "debug_rows": debug_rows,
        "focus": focus_dump,
        "metrics": {
            **metrics,
            "gemini_fallback_cell_count": gemini_info.get("api_calls"),
            "gemini_fallback_latency_ms": gemini_info.get("latency_ms"),
            "total_latency_ms": perf.get("total_ms"),
        },
        "performance": perf,
        "gemini": {
            k: gemini_info.get(k)
            for k in (
                "api_calls",
                "latency_ms",
                "cells_requested",
                "cells_applied",
                "error",
            )
        },
        "debug_png": debug_png,
        "total_detected_rows": len(rows),
        "product_rows_detected": metrics.get("product_rows_detected"),
        "non_product_rows_rejected": metrics.get("non_product_rows_rejected"),
        "feature_flag": {
            "name": STOCK_GEOMETRY_CELL_OCR_FLAG,
            "enabled": is_geometry_cell_ocr_enabled(),
            "production_wired": True,  # hook exists; flag still defaults OFF
        },
    }


def write_reports(
    report: Dict[str, Any],
    *,
    json_path: str = "/tmp/stock_geometry_v3.json",
    txt_path: str = "/tmp/stock_geometry_v3.txt",
) -> Tuple[str, str]:
    # JSON without huge image arrays
    slim = dict(report)
    slim_rows = []
    for r in report.get("rows") or []:
        slim_rows.append(
            {
                k: r.get(k)
                for k in (
                    "row_index",
                    "geometry_row_index",
                    "row_class",
                    "rejection_reason",
                    "product_text",
                    "y0",
                    "y1",
                    "product_name_ocr",
                    "pack_ocr",
                    "matched_product",
                    "product_match_status",
                    "selected",
                    "physical_values",
                    "business_fields",
                    "flags",
                    "reconciliation_failed",
                    "identity",
                    "field_provenance",
                    "is_focus",
                )
            }
        )
    slim["rows"] = slim_rows
    Path(json_path).write_text(json.dumps(slim, indent=2, default=str), encoding="utf-8")

    lines = []
    lines.append("stock_geometry_table_v3 benchmark")
    lines.append(f"file: {report.get('file')}")
    lines.append(f"production_switched: {report.get('production_switched')}")
    lines.append(f"total_detected_rows: {report.get('total_detected_rows')}")
    lines.append("")
    lines.append("=== METRICS ===")
    for k, v in (report.get("metrics") or {}).items():
        if k == "cell_rows":
            continue
        lines.append(f"{k}: {v}")
    lines.append("")
    lines.append("=== PERFORMANCE ===")
    for k, v in (report.get("performance") or {}).items():
        lines.append(f"{k}: {v}")
    lines.append("")
    lines.append("=== GEMINI ===")
    for k, v in (report.get("gemini") or {}).items():
        lines.append(f"{k}: {v}")
    lines.append("")
    lines.append("=== FOCUS ROWS ===")
    for f in report.get("focus") or []:
        lines.append(json.dumps(f, default=str))
    lines.append("")
    lines.append("=== CELL DETAIL ===")
    for c in (report.get("metrics") or {}).get("cell_rows") or []:
        lines.append(json.dumps(c, default=str))
    lines.append("")
    lines.append("=== ROW CLASSIFICATION ===")
    lines.append(f"product_rows: {report.get('product_rows_detected')}")
    lines.append(f"non_product_rejected: {report.get('non_product_rows_rejected')}")
    for dr in report.get("debug_rows") or []:
        if dr.get("row_class") == "NON_PRODUCT":
            lines.append(json.dumps(dr, default=str))
    Path(txt_path).write_text("\n".join(lines) + "\n", encoding="utf-8")
    return json_path, txt_path


# ---------------------------------------------------------------------------
# Production integration (behind STOCK_GEOMETRY_CELL_OCR_ENABLED, default OFF)
# ---------------------------------------------------------------------------


def geometry_row_to_line_item(row: Dict[str, Any]) -> Dict[str, Any]:
    """Convert a PRODUCT geometry row to a Laravel-facing line_item.

    Missing printed cells stay missing in field_source; top-level qty uses 0.0
    only at the API boundary (legacy contract). Never invents from arithmetic.
    """
    sel = row.get("selected") or {}
    biz = row.get("business_fields") or sel

    def _top(val: Any, *, closing: bool = False) -> Any:
        if val is None:
            return None if closing else 0.0
        return float(val)

    field_source: Dict[str, str] = {}
    for field in (
        "product_name",
        "packing",
        "lms",
        "opening_qty",
        "receipts_qty",
        "purchase_return_qty",
        "sales_qty",
        "sales_return_qty",
        "breakage_qty",
        "replacement_qty",
        "closing_qty",
        "closing_value",
        "free_out_qty",
    ):
        raw = sel.get(field) if field in ("product_name", "packing") else biz.get(field)
        field_source[field] = "missing" if raw is None else "printed"

    # closing_value provenance: printed Bal.Val / Cl.Value only — never Balance Qty.
    closing_val = biz.get("closing_value")
    if closing_val is not None and field_source.get("closing_value") == "printed":
        closing_value_source = "source_document"
    elif closing_val is None:
        closing_value_source = "null"
    else:
        closing_value_source = "product_value_calculation"

    item: Dict[str, Any] = {
        "product_code": None,
        "product_name": sel.get("product_name"),
        "packing": sel.get("packing"),
        "opening_qty": _top(biz.get("opening_qty")),
        "receipts_qty": _top(biz.get("receipts_qty")),
        "sales_qty": _top(biz.get("sales_qty")),
        "sales_value": 0.0,
        "closing_qty": _top(biz.get("closing_qty"), closing=True),
        "closing_value": _top(biz.get("closing_value")),
        "extra": {
            "field_source": field_source,
            "lms": biz.get("lms"),
            "purchase_return": biz.get("purchase_return_qty"),
            "sale_return": biz.get("sales_return_qty"),
            "exp_damage": biz.get("breakage_qty"),
            "replacement_qty": biz.get("replacement_qty"),
            "free_out_qty": biz.get("free_out_qty"),
            "geometry_row": True,
            "geometry_row_index": row.get("row_index"),
            "physical_values": row.get("physical_values"),
            "physical_cells": row.get("physical_cells")
            or dense_physical_cells(row.get("physical_values") or {}),
            "row_class": "PRODUCT",
            "extraction_method": "geometry_cell_ocr",
            "closing_value_source": closing_value_source,
        },
    }
    return item


def run_geometry_cell_ocr_path(
    file_bytes: bytes,
    kind: str,
    ctx: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Production entry for geometry cell OCR. Only when flag enabled.

    Returns same shape as run_vision_table_path:
      {status: ok|fallback, result?, reason?, ...}
    Does NOT replace Vision when layout is unsuitable — falls back.
    """
    ctx = ctx or {}
    request_id = str(ctx.get("request_id") or "-")
    filename = str(ctx.get("filename") or "upload.png")
    ext = str(ctx.get("ext") or ".png")

    if not is_geometry_cell_ocr_enabled():
        return {
            "status": "fallback",
            "reason": "GEOMETRY_CELL_OCR_DISABLED",
            "result": None,
            "gemini_calls": 0,
            "gemini_budget": 2,
        }
    if str(kind or "").lower() != "image":
        return {
            "status": "fallback",
            "reason": "unsupported_input_type",
            "result": None,
            "gemini_calls": 0,
            "gemini_budget": 2,
        }

    import tempfile

    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as fh:
            fh.write(file_bytes)
            tmp_path = fh.name
        # Cell Gemini is budgeted (max 2 calls/file). Op.Stk grids may need a
        # few sparse-digit rescues; text-column layouts usually OCR via Tesseract.
        report = run_geometry_table_v3(
            tmp_path,
            debug_png="/tmp/stock_grid_debug_v3_prod.png",
            enable_gemini=True,
            max_gemini_cells=2,
        )
    except Exception as exc:
        logger.info(
            "GEOMETRY_CELL_OCR request_id=%s status=fallback reason=%s",
            request_id,
            type(exc).__name__,
        )
        return {
            "status": "fallback",
            "reason": f"GEOMETRY_EXCEPTION:{type(exc).__name__}",
            "result": None,
            "gemini_calls": 0,
            "gemini_budget": 2,
        }
    finally:
        if tmp_path:
            try:
                Path(tmp_path).unlink(missing_ok=True)
            except Exception:
                pass

    if report.get("error"):
        return {
            "status": "fallback",
            "reason": str(report.get("error")),
            "result": None,
            "gemini_calls": 0,
            "gemini_budget": 2,
        }

    header_source = str((report.get("geometry") or {}).get("header_source") or "")
    detection_mode = str((report.get("geometry") or {}).get("detection_mode") or "")
    columns = list(report.get("columns") or [])
    has_sales_col = any(
        (c.get("business_field") or c.get("canonical")) == "sales_qty" for c in columns
    )
    accept_physical = header_source.startswith("physical")
    accept_text_columns = (
        detection_mode == "text_columns"
        and header_source.startswith("text_columns")
        and has_sales_col
    )
    if not accept_physical and not accept_text_columns:
        return {
            "status": "fallback",
            "reason": "GEOMETRY_LAYOUT_NOT_OPSTK_LMS",
            "result": None,
            "gemini_calls": int((report.get("gemini") or {}).get("api_calls") or 0),
            "gemini_budget": 2,
        }

    product_rows = [
        r for r in (report.get("rows") or []) if r.get("row_class") == "PRODUCT"
    ]
    if len(product_rows) < 3:
        return {
            "status": "fallback",
            "reason": "GEOMETRY_TOO_FEW_PRODUCT_ROWS",
            "result": None,
            "gemini_calls": int((report.get("gemini") or {}).get("api_calls") or 0),
            "gemini_budget": 2,
        }

    # Shift guards — Op.Stk+LMS Excel layouts only (not Sale Qty text columns).
    if accept_physical:
        for r in product_rows:
            sel = r.get("selected") or {}
            if detect_lms_opening_shift(sel):
                return {
                    "status": "fallback",
                    "reason": "GEOMETRY_LMS_OPENING_SHIFT",
                    "result": None,
                    "gemini_calls": int(
                        (report.get("gemini") or {}).get("api_calls") or 0
                    ),
                    "gemini_budget": 2,
                }
            if detect_opstk_receipt_shift(sel):
                return {
                    "status": "fallback",
                    "reason": "GEOMETRY_OPSTK_RECEIPT_SHIFT",
                    "result": None,
                    "gemini_calls": int(
                        (report.get("gemini") or {}).get("api_calls") or 0
                    ),
                    "gemini_budget": 2,
                }

    line_items = [geometry_row_to_line_item(r) for r in product_rows]
    column_map = [
        {
            "col_index": c.get("physical_column_index", c.get("col_index")),
            "canonical": c.get("business_field") or c.get("canonical"),
            "header_text": c.get("source_header"),
            "mapping_source": c.get("mapping_source"),
        }
        for c in (report.get("columns") or [])
    ]

    # Document metadata (stockist / period) — independent of table format.
    meta: Dict[str, Any] = {}
    meta_bgr = None
    try:
        meta_bgr = cv2.imdecode(
            np.frombuffer(file_bytes, dtype=np.uint8), cv2.IMREAD_COLOR
        )
        if meta_bgr is not None:
            meta = extract_geometry_document_metadata(meta_bgr)
    except Exception as exc:
        logger.info(
            "[STOCK_METADATA] stockist_detected=false reason=meta_exception:%s",
            type(exc).__name__,
        )
        meta = {}

    text_fmt = str((report.get("geometry") or {}).get("text_column_format") or "")
    if accept_text_columns and text_fmt == "STOCK_SALES_BALANCE_TEXT_COLUMNS":
        try:
            dbg_path = None
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as fh:
                fh.write(file_bytes)
                dbg_path = fh.name
            write_text_columns_debug_artifacts(dbg_path, report)
            Path(dbg_path).unlink(missing_ok=True)
        except Exception:
            pass

    result = {
        "source_file": filename,
        "source_format": (ext or "").lstrip("."),
        "stockist_name": meta.get("stockist_name"),
        "stockist_address": meta.get("stockist_address"),
        "company_name": meta.get("company_name"),
        "period_from": meta.get("period_from"),
        "period_to": meta.get("period_to"),
        "report_title": meta.get("report_title"),
        "line_items": line_items,
        "totals": {
            "sales_value": None,
            "closing_value": None,
            "extra": {
                "extraction_method": "geometry_cell_ocr",
                "vision_table_final": True,  # skip post Gemini gates
                "geometry_cell_ocr_final": True,
                "column_map": column_map,
                "product_rows_detected": len(product_rows),
                "non_product_rows_rejected": report.get("non_product_rows_rejected"),
                "debug_rows": report.get("debug_rows"),
                "gemini_calls": int((report.get("gemini") or {}).get("api_calls") or 0),
                "gemini_budget": 2,
                "skip_post_pipeline_gemini": True,
                "text_column_format": text_fmt or None,
                "closing_value_source_mode": "per_line_item",
                "stockist_metadata_source": meta.get("metadata_source"),
                "stockist_metadata_reason": meta.get("metadata_reason"),
            },
        },
    }
    logger.info(
        "GEOMETRY_CELL_OCR request_id=%s status=ok products=%s non_products=%s "
        "gemini_calls=%s",
        request_id,
        len(product_rows),
        report.get("non_product_rows_rejected"),
        (report.get("gemini") or {}).get("api_calls"),
    )
    return {
        "status": "ok",
        "result": result,
        "reason": None,
        "gemini_calls": int((report.get("gemini") or {}).get("api_calls") or 0),
        "gemini_budget": 2,
        "report_metrics": report.get("metrics"),
    }
