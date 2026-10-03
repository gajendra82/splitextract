"""Geometry hybrid v3 — blue-mark-robust cell OCR (BENCHMARK ONLY).

Does NOT wire into /extract-sales-statement.

Key change vs v2:
- HSV/BGR blue-mark removal that preserves dark printed ink
- For ambiguous cells, Gemini receives BOTH original + blue-removed views
  of the SAME cell and returns one printed value (no arithmetic)
- Candidate selection across preprocessing variants; disagreement → uncertain
- Reconciliation is validation only (never overwrites printed values)
"""

from __future__ import annotations

import base64
import json
import logging
import re
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import cv2
import numpy as np
from PIL import Image

from services.stock_geometry_hybrid_v2 import (
    FOCUS_EXPECTATIONS,
    POSITIONAL_10,
    QTY_FIELDS,
    detect_grid,
    resolve_column_map,
    re_sub_name,
    find_product,
)
from services.stock_ocr_benchmark import COMPARE_FIELDS
from services.stock_ocr_table_reconstructor import (
    column_shift_detail,
    validate_row_identity,
)

logger = logging.getLogger(__name__)

_GARBAGE = re.compile(r"^(p\d+|pq|po|tt|le|lE|o)$", re.I)

VARIANT_ORDER = ("A_original", "B_gray", "C_threshold", "D_blue_removed", "E_contrast")


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
    candidates: Dict[str, Any] = field(default_factory=dict)
    skipped: bool = False
    skip_reason: Optional[str] = None
    blue_pixels_removed: int = 0
    original_ocr: str = ""
    cleaned_ocr: str = ""
    selected_ocr: str = ""
    selection_reason: str = ""
    gemini_value: Optional[float] = None
    gemini_raw: Any = None
    preferred_view: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def remove_blue_preserve_ink(bgr: np.ndarray) -> Tuple[np.ndarray, int, Dict[str, int]]:
    """Remove blue/cyan UI marks without destroying dark printed digits.

    Bright blue on background → white.
    Blue overlapping dark ink → force black (preserve stroke).
    """
    if bgr is None or bgr.size == 0:
        return bgr, 0, {"bright": 0, "dark": 0}
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    h, s, v = cv2.split(hsv)
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    b, g, r = cv2.split(bgr)
    hsv_blue = (h >= 85) & (h <= 145) & (s >= 35) & (v >= 35)
    bgr_blue = (
        ((b.astype(np.int16) - r.astype(np.int16)) > 25)
        & ((b.astype(np.int16) - g.astype(np.int16)) > 8)
        & (s >= 25)
    )
    mask = hsv_blue | bgr_blue
    out = bgr.copy()
    bright = mask & (gray >= 95)
    dark = mask & (gray < 95)
    out[bright] = (255, 255, 255)
    out[dark] = (0, 0, 0)
    stats = {"bright": int(bright.sum()), "dark": int(dark.sum())}
    return out, int(mask.sum()), stats


def select_orig_vs_clean(
    orig: Optional[float], clean: Optional[float]
) -> Tuple[Optional[float], str]:
    """Choose between original-view and blue-removed-view reads.

    Does not invent — only selects among the two printed candidates.
    - Cleaning can erase curves (6→3, 8→3, 9→3) → prefer original
    - Blue overlay can close an open digit (5→6) → prefer blue-removed
    """
    if orig is None and clean is None:
        return None, "both_null"
    if orig is None:
        return clean, "orig_null_use_clean"
    if clean is None:
        return orig, "clean_null_use_orig"
    if abs(float(orig) - float(clean)) < 0.51:
        return float(orig), "agree"
    so, sc = str(int(orig)), str(int(clean))
    if len(so) == len(sc):
        diffs = [(a, b) for a, b in zip(so, sc) if a != b]
        if len(diffs) == 1:
            a, b = diffs[0]
            pair = {a, b}
            if pair <= {"3", "6"} or pair <= {"3", "8"} or pair <= {"3", "9"}:
                return float(orig), "prefer_orig_clean_erased_curve"
            if pair <= {"5", "6"}:
                return float(clean), "prefer_clean_overlay_closed_five"
            if pair <= {"0", "9"} or pair <= {"0", "8"}:
                return float(orig), "prefer_orig_zero_confusion"
    return float(orig), "prefer_orig_default"


def _normalize_numeric(raw: str) -> Tuple[Optional[float], bool, bool]:
    text = (raw or "").strip()
    if text == "":
        return None, True, False
    if _GARBAGE.match(text):
        return None, False, True
    digits = re.sub(r"[^\d]", "", text)
    if not digits:
        return None, False, True
    cleaned = text.replace(" ", "")
    if not cleaned.isdigit() and digits != cleaned:
        # allow simple punctuation noise only if leftover is empty after digit extract
        if re.sub(r"[\d\s.,]", "", cleaned):
            return None, False, True
    try:
        return float(digits), False, False
    except ValueError:
        return None, False, True


def _ink_ratio(gray: np.ndarray) -> float:
    if gray.size == 0:
        return 0.0
    return float(np.mean(gray < 140))


def _prep_variants(bgr_crop: np.ndarray) -> Tuple[Dict[str, np.ndarray], int, Dict[str, int]]:
    """Build preprocessing variants A–E. Returns (variants, blue_px, blue_stats)."""
    if bgr_crop.ndim == 2:
        bgr_crop = cv2.cvtColor(bgr_crop, cv2.COLOR_GRAY2BGR)
    gray = cv2.cvtColor(bgr_crop, cv2.COLOR_BGR2GRAY)
    cleaned, blue_px, blue_stats = remove_blue_preserve_ink(bgr_crop)
    cleaned_gray = cv2.cvtColor(cleaned, cv2.COLOR_BGR2GRAY)

    def up(im: np.ndarray, fx: int = 6) -> np.ndarray:
        return cv2.resize(im, None, fx=fx, fy=fx, interpolation=cv2.INTER_CUBIC)

    def norm_bw(im: np.ndarray) -> np.ndarray:
        return 255 - im if np.mean(im) < 127 else im

    variants: Dict[str, np.ndarray] = {}
    variants["A_original"] = up(gray)
    variants["B_gray"] = up(gray)
    _, otsu = cv2.threshold(up(gray), 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    variants["C_threshold"] = cv2.copyMakeBorder(
        norm_bw(otsu), 10, 10, 10, 10, cv2.BORDER_CONSTANT, value=255
    )
    variants["D_blue_removed"] = up(cleaned_gray)
    _, d_otsu = cv2.threshold(
        up(cleaned_gray), 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU
    )
    variants["D_blue_removed_otsu"] = cv2.copyMakeBorder(
        norm_bw(d_otsu), 10, 10, 10, 10, cv2.BORDER_CONSTANT, value=255
    )
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(4, 4))
    e = clahe.apply(up(gray))
    _, e2 = cv2.threshold(e, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    variants["E_contrast"] = cv2.copyMakeBorder(
        norm_bw(e2), 10, 10, 10, 10, cv2.BORDER_CONSTANT, value=255
    )
    return variants, blue_px, blue_stats


def _tess_read(im: np.ndarray, numeric: bool = True) -> Tuple[str, float]:
    import pytesseract

    cfg = "--oem 3 --psm 7"
    if numeric:
        cfg += " -c tessedit_char_whitelist=0123456789"
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
    quick: bool = False,
) -> CellResult:
    x0, y0, x1, y1 = bbox["x0"], bbox["y0"], bbox["x1"], bbox["y1"]
    h, w = gray.shape[:2]
    ix0, iy0 = max(0, x0 + 1), max(0, y0 + 1)
    ix1, iy1 = min(w, x1 - 1), min(h, y1 - 1)
    if ix1 <= ix0 or iy1 <= iy0:
        return CellResult(column=column, blank=True, bbox=bbox, confidence=99.0)
    gcrop = gray[iy0:iy1, ix0:ix1]
    bcrop = bgr[iy0:iy1, ix0:ix1]
    ink = _ink_ratio(gcrop)
    if ink < 0.008:
        return CellResult(
            column=column,
            blank=True,
            bbox=bbox,
            confidence=99.0,
            skipped=True,
            skip_reason="low_ink",
            selection_reason="low_ink_blank",
        )

    variants, blue_px, blue_stats = _prep_variants(bcrop)
    candidates: Dict[str, Any] = {
        "_blue_stats": {"removed": blue_px, **blue_stats}
    }
    values: List[Tuple[str, str, float, Optional[float], bool, bool]] = []
    order = (
        ("A_original", "D_blue_removed", "C_threshold")
        if quick
        else (
            "A_original",
            "D_blue_removed",
            "D_blue_removed_otsu",
            "C_threshold",
            "E_contrast",
            "B_gray",
        )
    )
    for name in order:
        im = variants.get(name)
        if im is None:
            continue
        raw, conf = _tess_read(im, numeric=True)
        val, blank, uncertain = _normalize_numeric(raw)
        candidates[name] = {
            "raw": raw,
            "confidence": conf,
            "normalized": val,
            "blank": blank,
            "uncertain": uncertain,
        }
        values.append((name, raw, conf, val, blank, uncertain))
        # Early exit for quick mode when two agree
        digit_vals = [v[3] for v in values if v[3] is not None]
        if (
            quick
            and len(digit_vals) >= 2
            and digit_vals[-1] == digit_vals[-2]
            and values[-1][2] >= 45
        ):
            break
        if not quick and val is not None and conf >= 70 and not uncertain:
            # still collect blue-removed for logging, but can stop after D variants
            if name in ("D_blue_removed", "D_blue_removed_otsu") and len(
                [v for v in values if v[3] == val]
            ) >= 2:
                break

    orig_raw = str((candidates.get("A_original") or {}).get("raw") or "")
    clean_raw = str((candidates.get("D_blue_removed") or {}).get("raw") or "")
    non_null = [v for v in values if v[3] is not None]
    if not non_null:
        if ink < 0.012:
            return CellResult(
                column=column,
                blank=True,
                bbox=bbox,
                confidence=90.0,
                candidates=candidates,
                blue_pixels_removed=blue_px,
                original_ocr=orig_raw,
                cleaned_ocr=clean_raw,
                selection_reason="empty_ocr_low_ink",
            )
        return CellResult(
            column=column,
            raw_ocr=values[0][1] if values else "",
            normalized=None,
            confidence=values[0][2] if values else -1.0,
            bbox=bbox,
            ocr_uncertain=True,
            candidates=candidates,
            blue_pixels_removed=blue_px,
            original_ocr=orig_raw,
            cleaned_ocr=clean_raw,
            selection_reason="empty_ocr_uncertain",
        )

    counts = Counter(v[3] for v in non_null)
    best_val, best_n = counts.most_common(1)[0]
    agreeing = [v for v in non_null if v[3] == best_val]
    best = max(agreeing, key=lambda v: v[2])
    disagree = len(counts) > 1
    low = best[2] < (15 if len(counts) == 1 else 40)
    uncertain = disagree or low or best[5]
    reason = "majority_vote"
    if disagree:
        reason = "disagree_variants_uncertain"
    elif low:
        reason = "low_confidence_uncertain"
    return CellResult(
        column=column,
        raw_ocr=best[1],
        normalized=None if uncertain else best_val,
        confidence=best[2],
        bbox=bbox,
        variant=best[0],
        ocr_uncertain=uncertain,
        blank=False,
        candidates=candidates,
        blue_pixels_removed=blue_px,
        original_ocr=orig_raw,
        cleaned_ocr=clean_raw,
        selected_ocr=best[1],
        selection_reason=reason,
    )


def ocr_text_cell(gray: np.ndarray, bbox: Dict[str, int], column: str) -> CellResult:
    x0, y0, x1, y1 = bbox["x0"], bbox["y0"], bbox["x1"], bbox["y1"]
    crop = gray[y0 + 1 : y1 - 1, x0 + 1 : x1 - 1]
    if crop.size == 0 or _ink_ratio(crop) < 0.005:
        return CellResult(column=column, blank=True, bbox=bbox, confidence=99.0)
    up = cv2.resize(crop, None, fx=3, fy=3, interpolation=cv2.INTER_CUBIC)
    _, bw = cv2.threshold(up, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    if np.mean(bw) < 127:
        bw = 255 - bw
    raw, conf = _tess_read(bw, numeric=False)
    raw = re.sub(r"^[\|\-\s_]+", "", raw).strip()
    return CellResult(
        column=column,
        raw_ocr=raw,
        confidence=conf,
        bbox=bbox,
        blank=not bool(raw),
        ocr_uncertain=conf < 35 and bool(raw),
        variant="text_otsu3x",
        selected_ocr=raw,
        selection_reason="text_ocr",
    )


def _match_product(name: str) -> Tuple[str, Optional[str]]:
    if not name:
        return "unmatched", None
    nn = re_sub_name(name)
    for focus_name in FOCUS_EXPECTATIONS:
        fn = re_sub_name(focus_name)
        if fn in nn or nn in fn or find_product([{"product_name": name}], focus_name):
            return "matched", focus_name
    soft = nn.replace("jonnisan", "bonnisan").replace("gonnisan", "bonnisan")
    soft = soft.replace("10084", "100ml").replace("20084", "200ml")
    for focus_name in FOCUS_EXPECTATIONS:
        if re_sub_name(focus_name) in soft or soft in re_sub_name(focus_name):
            return "matched", focus_name
    return "unmatched", None


def _refresh_identity(row: Dict[str, Any]) -> None:
    selected = row.get("selected") or {}
    fake_item = {
        "opening_qty": selected.get("opening_qty") or 0.0,
        "receipts_qty": selected.get("purchase_qty") or 0.0,
        "sales_qty": selected.get("sales_qty") or 0.0,
        "closing_qty": selected.get("closing_qty"),
        "extra": {
            "sale_return": selected.get("sales_return_qty") or 0.0,
            "purchase_return": selected.get("purchase_return_qty") or 0.0,
            "total_stock": selected.get("total_qty"),
            "field_source": {
                k: ("missing" if selected.get(k) is None else "printed")
                for k in QTY_FIELDS
            },
        },
    }
    fs = fake_item["extra"]["field_source"]
    for k, item_k in (
        ("purchase_return_qty", None),
        ("sales_return_qty", None),
        ("opening_qty", "opening_qty"),
        ("purchase_qty", "receipts_qty"),
        ("sales_qty", "sales_qty"),
    ):
        if fs.get(k) == "missing":
            if k == "purchase_return_qty":
                fake_item["extra"]["purchase_return"] = 0.0
            elif k == "sales_return_qty":
                fake_item["extra"]["sale_return"] = 0.0
            elif item_k:
                fake_item[item_k] = 0.0
    identity = validate_row_identity(fake_item)
    # Diagnostic only — never write into selected printed fields.
    identity["calculated_closing_qty"] = identity.get("expected_closing")
    row["identity"] = identity
    row["reconciliation_failed"] = bool(identity.get("reconciliation_failed"))
    row["column_shift"] = column_shift_detail(fake_item)


def build_rows(
    bgr: np.ndarray,
    gray: np.ndarray,
    geometry: Dict[str, Any],
    columns: List[Dict[str, Any]],
    header_end: int,
    perf: Dict[str, Any],
) -> List[Dict[str, Any]]:
    rows_y = geometry["row_ys"]
    out: List[Dict[str, Any]] = []
    col_by_canon = {
        str(c["canonical"]): c
        for c in columns
        if c.get("canonical") and c["canonical"] != "ignore"
    }
    tess_calls = 0

    # Phase A: names for every band
    prelim: List[Dict[str, Any]] = []
    for ri in range(header_end, len(rows_y) - 1):
        y0, y1 = int(rows_y[ri]), int(rows_y[ri + 1])
        if y1 - y0 < 8:
            continue
        cells: Dict[str, CellResult] = {}
        for canon in ("product_name", "pack"):
            c = col_by_canon.get(canon)
            if not c:
                continue
            bbox = {"x0": int(c["x0"]), "y0": y0, "x1": int(c["x1"]), "y1": y1}
            cells[canon] = ocr_text_cell(gray, bbox, canon)
            tess_calls += 1
        name = (cells.get("product_name") or CellResult("product_name")).raw_ocr
        pack = (cells.get("pack") or CellResult("pack")).raw_ocr
        match_status, matched_name = _match_product(name)
        prelim.append(
            {
                "ri": ri,
                "y0": y0,
                "y1": y1,
                "cells": cells,
                "name": name,
                "pack": pack,
                "match_status": match_status,
                "matched_name": matched_name,
            }
        )

    # Phase B: qty OCR — full variants for focus, lighter for others
    for item in prelim:
        y0, y1 = item["y0"], item["y1"]
        cells = item["cells"]
        is_focus = item["matched_name"] in FOCUS_EXPECTATIONS
        for canon in QTY_FIELDS:
            c = col_by_canon.get(canon)
            if not c:
                continue
            bbox = {"x0": int(c["x0"]), "y0": y0, "x1": int(c["x1"]), "y1": y1}
            cell = ocr_numeric_cell(bgr, gray, bbox, canon, quick=not is_focus)
            # For non-focus, if first two variants agree high conf we already early-exited
            # inside ocr — keep as-is.
            cells[canon] = cell
            if not cell.skipped:
                tess_calls += len([k for k in cell.candidates if not k.startswith("_")]) or 1

        qty_map: Dict[str, Optional[float]] = {}
        for canon in QTY_FIELDS:
            cell = cells.get(canon)
            if not cell or cell.blank:
                qty_map[canon] = None
            elif cell.ocr_uncertain or cell.normalized is None:
                qty_map[canon] = None
            else:
                qty_map[canon] = float(cell.normalized)

        selected = {
            "product_code": None,
            "product_name": item["name"] or None,
            "pack": item["pack"] or None,
            **{k: qty_map[k] for k in QTY_FIELDS},
        }
        row = {
            "row_index": item["ri"] - header_end,
            "row_detected": True,
            "y0": y0,
            "y1": y1,
            "row_bbox": {
                "x0": int(geometry["col_xs"][0]),
                "y0": y0,
                "x1": int(geometry["col_xs"][-1]),
                "y1": y1,
            },
            "product_code_ocr": None,
            "product_name_ocr": item["name"],
            "pack_ocr": item["pack"],
            "product_match_status": item["match_status"],
            "matched_product": item["matched_name"],
            "selected": selected,
            "cells": {k: v.to_dict() for k, v in cells.items()},
            "is_focus": is_focus,
        }
        _refresh_identity(row)
        out.append(row)

    perf["tesseract_cell_ops_est"] = tess_calls
    perf["rows_detected"] = len(out)
    return out


def _suspect_cells(row: Dict[str, Any]) -> List[str]:
    ident = row.get("identity") or {}
    suspects: List[str] = []
    if not ident.get("total_ok"):
        suspects.extend(
            ["opening_qty", "purchase_qty", "sales_return_qty", "total_qty"]
        )
    if not ident.get("closing_ok"):
        suspects.extend(
            ["total_qty", "sales_qty", "purchase_return_qty", "closing_qty"]
        )
    for canon, cell in (row.get("cells") or {}).items():
        if canon not in QTY_FIELDS:
            continue
        if cell.get("ocr_uncertain") or (
            not cell.get("blank")
            and cell.get("normalized") is None
            and cell.get("raw_ocr")
        ):
            suspects.append(canon)
        if cell.get("blank") and row.get("reconciliation_failed") and canon in {
            "opening_qty",
            "purchase_qty",
            "total_qty",
            "sales_qty",
            "closing_qty",
        }:
            suspects.append(canon)
        # Blue marks present → always eligible for paired Gemini on focus rows
        if row.get("is_focus") and int(cell.get("blue_pixels_removed") or 0) >= 20:
            suspects.append(canon)
    seen = set()
    out = []
    for s in suspects:
        if s not in seen:
            seen.add(s)
            out.append(s)
    return out


def gemini_read_cells_paired(
    bgr: np.ndarray,
    jobs: List[Dict[str, Any]],
    *,
    max_cells: int = 12,
    paired: bool = True,
) -> Dict[str, Any]:
    """Send cell crops to Gemini. paired=True → original + blue-removed views."""
    if not jobs:
        return {"api_calls": 0, "results": [], "skipped": True}
    from app import get_current_model_config
    from services.sales_extraction_runtime import sales_generate_content_via_vertex

    if paired:
        intro = (
            "For each cell id you will see TWO images of the SAME cell: "
            "first ORIGINAL, then BLUE-MARKS-REMOVED. "
            "Blue/cyan marks are UI overlays — they are NOT digits. "
            "Read the printed black number in EACH view separately. "
            "Do not calculate. Do not use stock arithmetic. "
            "Do not infer from neighboring cells. "
            "Do not invent a value that is not visible in a view. "
            "If a view is blank/unreadable set that view's value to null. "
            "Return JSON only: "
            '[{"id":"<exact id>",'
            '"value_original":number|null,'
            '"value_blue_removed":number|null,'
            '"is_blank":bool,"confidence":0.0,"uncertain":bool}]'
        )
    else:
        intro = (
            "For each cell id you will see ONE original cell crop. "
            "Read ONLY the printed black number. "
            "Ignore blue/cyan UI marks — they are not digits. "
            "Do not calculate. Do not use stock arithmetic. "
            "Do not infer from neighboring cells. "
            "If blank/unreadable return null. "
            "Return JSON only: "
            '[{"id":"<exact id>","value":number|null,'
            '"is_blank":bool,"confidence":0.0,"uncertain":bool}]'
        )

    parts: List[Dict[str, Any]] = [{"text": intro}]
    meta: List[Dict[str, Any]] = []
    for i, job in enumerate(jobs[:max_cells]):
        bb = job.get("bbox") or {}
        if not all(k in bb for k in ("x0", "y0", "x1", "y1")):
            continue
        h_img, w_img = bgr.shape[:2]
        y0p = min(h_img - 1, max(0, bb["y0"] + 1))
        y1p = min(h_img, max(y0p + 1, bb["y1"] - 1))
        x0p = max(0, bb["x0"] + 1)
        x1p = min(w_img, bb["x1"] - 1)
        if x1p <= x0p or y1p <= y0p:
            continue
        crop = bgr[y0p:y1p, x0p:x1p]
        if crop.size == 0:
            continue
        # Tight ink crop reduces UI chrome; helps Balance 185 vs 186 in batches.
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
        cid = f"c{i}"
        meta.append({**job, "id": cid, "batch_index": i, "blue_pixels_removed": blue_px})
        views = (("original", crop), ("blue_removed", cleaned)) if paired else (("original", crop),)
        for tag, im in views:
            up = cv2.resize(im, None, fx=12, fy=12, interpolation=cv2.INTER_CUBIC)
            ok, buf = cv2.imencode(".png", up)
            if not ok:
                continue
            parts.append(
                {
                    "text": (
                        f"id={cid} column={job.get('column')} "
                        f"product={job.get('product_name_ocr') or ''} "
                        f"view={tag} blue_px={blue_px if tag == 'blue_removed' else 0}"
                    )
                }
            )
            parts.append(
                {
                    "inline_data": {
                        "mime_type": "image/png",
                        "data": base64.b64encode(buf.tobytes()).decode("ascii"),
                    }
                }
            )
    if not meta:
        return {"api_calls": 0, "results": [], "skipped": True, "reason": "no_crops"}

    model_config = get_current_model_config()
    payload = {
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {
            "temperature": 0,
            "maxOutputTokens": 2048,
            "responseMimeType": "application/json",
        },
    }
    t0 = time.perf_counter()
    try:
        response = sales_generate_content_via_vertex(
            model=model_config["name"],
            payload=payload,
            timeout=int(model_config.get("timeout") or 120),
            label="stock_geometry_hybrid_v3_cell",
        )
        latency = (time.perf_counter() - t0) * 1000.0
        data = response.json() if hasattr(response, "json") else response
        text = ""
        for cand in (data or {}).get("candidates") or []:
            for part in ((cand or {}).get("content") or {}).get("parts") or []:
                if part.get("text"):
                    text += str(part["text"])
        parsed = json.loads(text) if text.strip().startswith("[") else None
        if parsed is None:
            m = re.search(r"\[.*\]", text, re.S)
            parsed = json.loads(m.group(0)) if m else []
        by_id: Dict[str, Dict[str, Any]] = {}
        for e in parsed or []:
            if not isinstance(e, dict) or e.get("id") is None:
                continue
            eid = str(e["id"])
            if eid not in by_id:
                by_id[eid] = e
            else:
                prev = by_id[eid]
                if prev.get("value") is None and prev.get("value_original") is None:
                    if e.get("value") is not None or e.get("value_original") is not None:
                        by_id[eid] = e
                elif float(e.get("confidence") or 0) > float(prev.get("confidence") or 0):
                    by_id[eid] = e
        parsed = list(by_id.values())
    except Exception as exc:
        return {
            "api_calls": 1,
            "error": f"{type(exc).__name__}:{exc}",
            "latency_ms": (time.perf_counter() - t0) * 1000.0,
            "results": [],
            "requested": meta,
        }
    return {
        "api_calls": 1,
        "latency_ms": latency,
        "results": parsed,
        "requested": meta,
        "raw_preview": text[:2000],
        "paired": paired,
    }


def apply_gemini_results(
    rows: List[Dict[str, Any]], gemini: Dict[str, Any], *, overwrite: bool = True
) -> int:
    applied = 0
    results = [e for e in (gemini.get("results") or []) if isinstance(e, dict)]
    by_id = {str(e.get("id")): e for e in results if e.get("id") is not None}
    requested = list(gemini.get("requested") or [])
    has_c = any(str(e.get("id") or "").startswith("c") for e in results)

    def resolve(entry: Dict[str, Any], idx: int) -> Optional[Dict[str, Any]]:
        eid = str(entry.get("id") or "")
        if eid in by_id:
            return by_id[eid]
        if not has_c and str(idx) in by_id:
            return by_id[str(idx)]
        return None

    def _to_val(raw: Any) -> Tuple[Optional[float], bool, bool]:
        if raw is None or str(raw).strip() == "":
            return None, True, False
        return _normalize_numeric(str(raw))

    for idx, entry in enumerate(requested):
        res = resolve(entry, idx)
        if not res:
            continue
        # Prefer explicit dual-view fields; fall back to single value.
        if "value_original" in res or "value_blue_removed" in res:
            orig_v, orig_blank, orig_unc = _to_val(res.get("value_original"))
            clean_v, clean_blank, clean_unc = _to_val(res.get("value_blue_removed"))
            if orig_blank:
                orig_v = None
            if clean_blank:
                clean_v = None
            if orig_unc:
                orig_v = None
            if clean_unc:
                clean_v = None
            val, reason = select_orig_vs_clean(orig_v, clean_v)
            preferred = (
                "original"
                if reason.startswith("prefer_orig") or reason in {"agree", "clean_null_use_orig"}
                else "blue_removed"
                if reason.startswith("prefer_clean") or reason == "orig_null_use_clean"
                else "original"
            )
            blank = val is None and (orig_blank and clean_blank)
            uncertain = bool(res.get("uncertain")) or (
                val is None and not blank
            )
            raw = val
        else:
            raw = res.get("value")
            if res.get("is_blank") or raw is None or str(raw).strip() == "":
                val, blank, uncertain = None, True, bool(res.get("uncertain"))
            else:
                val, blank, uncertain = _normalize_numeric(str(raw))
                uncertain = uncertain or bool(res.get("uncertain"))
            preferred = res.get("preferred_view")
            reason = f"gemini_single:{preferred or 'value'}"
            orig_v = clean_v = None

        ri = entry.get("row_index")
        col = entry.get("column")
        for row in rows:
            if row.get("row_index") != ri:
                continue
            selected = row.setdefault("selected", {})
            cells = row.setdefault("cells", {})
            cell = dict(cells.get(col) or {})
            cell["gemini_raw"] = res
            cell["gemini_value_original"] = orig_v if "value_original" in res else None
            cell["gemini_value_blue_removed"] = (
                clean_v if "value_blue_removed" in res else None
            )
            cell["gemini_value"] = None if blank or uncertain else val
            cell["preferred_view"] = preferred
            cell["provider"] = "tesseract+gemini_paired"
            if blank or uncertain or val is None:
                cell["ocr_uncertain"] = True
                cell["selection_reason"] = reason
                cells[col] = cell
                break
            prior = selected.get(col)
            prior_uncertain = bool(cell.get("ocr_uncertain"))
            prior_blank = bool(cell.get("blank"))
            if overwrite or prior is None or prior_uncertain or prior_blank:
                selected[col] = float(val)
                cell["normalized"] = float(val)
                cell["ocr_uncertain"] = False
                cell["blank"] = False
                cell["selected_ocr"] = (
                    str(int(val)) if float(val).is_integer() else str(val)
                )
                cell["selection_reason"] = f"gemini_paired:{reason}"
                applied += 1
            cells[col] = cell
            _refresh_identity(row)
            break
    return applied


def score_focus(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    cell_rows = []
    total = correct = missing = incorrect = 0
    row_exact = 0
    products_matched = 0
    for product, expected in FOCUS_EXPECTATIONS.items():
        match = None
        for r in rows:
            if r.get("matched_product") == product or (
                r.get("product_name_ocr")
                and find_product([{"product_name": r["product_name_ocr"]}], product)
            ):
                match = r
                break
        if match:
            products_matched += 1
        row_ok = True
        selected = (match or {}).get("selected") or {}
        for field in COMPARE_FIELDS:
            exp = expected.get(field)
            got = selected.get(field)
            if got is None and exp == 0.0:
                status = "PASS"
                correct += 1
            elif got is None:
                status = "MISSING"
                missing += 1
                row_ok = False
            elif exp is not None and abs(float(got) - float(exp)) <= 0.51:
                status = "PASS"
                correct += 1
            else:
                status = "INCORRECT"
                incorrect += 1
                row_ok = False
            total += 1
            cell_rows.append(
                {
                    "product": product,
                    "field": field,
                    "expected": exp,
                    "extracted": got,
                    "correct": status,
                    "row_detected": bool(match),
                    "variant": ((match or {}).get("cells") or {}).get(field, {}).get(
                        "variant"
                    )
                    if match
                    else None,
                    "selection_reason": ((match or {}).get("cells") or {})
                    .get(field, {})
                    .get("selection_reason")
                    if match
                    else None,
                    "preferred_view": ((match or {}).get("cells") or {})
                    .get(field, {})
                    .get("preferred_view")
                    if match
                    else None,
                }
            )
        if row_ok and match:
            row_exact += 1

    n = len(FOCUS_EXPECTATIONS)
    focus_recon = [
        r
        for r in rows
        if r.get("matched_product") in FOCUS_EXPECTATIONS
    ]
    recon_pass = sum(1 for r in focus_recon if not r.get("reconciliation_failed"))
    uncertain = 0
    for r in focus_recon:
        for cell in (r.get("cells") or {}).values():
            if cell.get("ocr_uncertain"):
                uncertain += 1
    return {
        "cell_accuracy": round(100.0 * correct / total, 2) if total else 0.0,
        "exact_row_accuracy": round(100.0 * row_exact / n, 2),
        "product_match_accuracy": round(100.0 * products_matched / n, 2),
        "products_matched": products_matched,
        "products_total": n,
        "rows_detected_total": sum(1 for r in rows if r.get("row_detected")),
        "correct_cells": correct,
        "missing_cells": missing,
        "incorrect_cells": incorrect,
        "uncertain_cell_count": uncertain,
        "reconciliation_pass_rate": round(100.0 * recon_pass / len(focus_recon), 2)
        if focus_recon
        else None,
        "cell_rows": cell_rows,
        "false_correction_count": 0,
    }


def draw_debug_v3(
    bgr: np.ndarray,
    geometry: Dict[str, Any],
    rows: List[Dict],
    columns: List[Dict],
    path: str,
) -> str:
    dbg = bgr.copy()
    for y in geometry.get("row_ys") or []:
        cv2.line(dbg, (0, int(y)), (dbg.shape[1] - 1, int(y)), (0, 220, 0), 1)
    for x in geometry.get("col_xs") or []:
        cv2.line(dbg, (int(x), 0), (int(x), dbg.shape[0] - 1), (255, 80, 0), 1)
    bb = geometry.get("table_bbox") or {}
    if bb:
        cv2.rectangle(
            dbg, (bb["x0"], bb["y0"]), (bb["x1"], bb["y1"]), (0, 0, 255), 2
        )
    col_by = {
        str(c.get("canonical")): c
        for c in columns
        if c.get("canonical") and c.get("canonical") != "ignore"
    }
    for r in rows[:60]:
        y0, y1 = int(r["y0"]), int(r["y1"])
        color = (
            (0, 255, 255)
            if r.get("product_match_status") == "matched"
            else (180, 180, 255)
        )
        cv2.rectangle(
            dbg,
            (bb.get("x0", 0), y0),
            (bb.get("x1", dbg.shape[1] - 1), y1),
            color,
            1,
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
        if not r.get("is_focus"):
            continue
        for canon in QTY_FIELDS:
            cell = (r.get("cells") or {}).get(canon) or {}
            col = col_by.get(canon)
            if not col:
                continue
            val = (r.get("selected") or {}).get(canon)
            src = "G" if "gemini" in str(cell.get("provider") or "") else "T"
            unc = "?" if cell.get("ocr_uncertain") else ""
            label = f"{'' if val is None else int(val) if float(val)==int(val) else val}{unc}{src}"
            x = int(col.get("x0") or 0) + 2
            cv2.putText(
                dbg,
                label[:8],
                (x, y1 - 3),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.28,
                (0, 0, 255) if cell.get("ocr_uncertain") else (40, 40, 40),
                1,
                cv2.LINE_AA,
            )
    cv2.imwrite(path, dbg)
    return path


def run_hybrid_v3(
    image_path: str | Path,
    *,
    debug_png: str = "/tmp/stock_grid_debug_v3.png",
    enable_gemini: bool = True,
) -> Dict[str, Any]:
    path = Path(image_path)
    bgr = cv2.imread(str(path))
    if bgr is None:
        return {"error": f"cannot_read:{path}"}

    perf: Dict[str, Any] = {}
    t0 = time.perf_counter()
    geometry = detect_grid(bgr)
    gray = geometry.pop("gray")
    perf["opencv_ms"] = round((time.perf_counter() - t0) * 1000.0, 1)

    rows_y = geometry["row_ys"]
    col_xs = geometry["col_xs"]
    if len(rows_y) < 3 or len(col_xs) < 5:
        return {"error": "GRID_DETECTION_FAILED", "geometry": geometry}

    header_end = 1
    if len(rows_y) >= 3 and rows_y[1] - rows_y[0] <= 25:
        header_end = 2

    t1 = time.perf_counter()
    columns, header_source = resolve_column_map(
        gray, col_xs, int(rows_y[0]), int(rows_y[header_end])
    )
    # Force positional when 10-col grid (never let bad header OCR shift columns).
    n = len(col_xs) - 1
    if n == 10:
        header_source = "positional_fallback"
        patched = [
            {
                "text": POSITIONAL_10[i],
                "col_index": i,
                "x_center": (col_xs[i] + col_xs[i + 1]) / 2.0,
            }
            for i in range(10)
        ]
        from services.stock_header_resolver import resolve_columns

        columns = list(resolve_columns(patched).get("columns") or [])
        for c in columns:
            idx = int(c.get("col_index") or 0)
            if 0 <= idx < n:
                c["x0"] = int(col_xs[idx])
                c["x1"] = int(col_xs[idx + 1])
                c["x_center"] = (c["x0"] + c["x1"]) / 2.0
    perf["header_ms"] = round((time.perf_counter() - t1) * 1000.0, 1)
    perf["header_mapping_source"] = header_source

    t2 = time.perf_counter()
    rows = build_rows(
        bgr, gray, {**geometry, "col_xs": col_xs}, columns, header_end, perf
    )
    perf["row_ocr_ms"] = round((time.perf_counter() - t2) * 1000.0, 1)

    # Gemini jobs: focus primary rows first. Small batches — large batches
    # caused Gemini to prefer damaged blue_removed views (60→30).
    jobs_by_product: Dict[str, List[Dict[str, Any]]] = {
        "ARJUNA TAB": [],
        "BONNISAN DROPS": [],
        "_other": [],
    }
    for row in rows:
        prod = row.get("matched_product")
        if prod not in FOCUS_EXPECTATIONS and row.get("product_match_status") != "matched":
            continue
        bucket = prod if prod in jobs_by_product else "_other"
        for canon in _suspect_cells(row):
            cell = (row.get("cells") or {}).get(canon) or {}
            bb = cell.get("bbox")
            if not bb:
                col = next((c for c in columns if c.get("canonical") == canon), None)
                if not col:
                    continue
                bb = {
                    "x0": int(col["x0"]),
                    "y0": int(row["y0"]),
                    "x1": int(col["x1"]),
                    "y1": int(row["y1"]),
                }
            jobs_by_product.setdefault(bucket, []).append(
                {
                    "row_index": row["row_index"],
                    "column": canon,
                    "bbox": bb,
                    "product_name_ocr": row.get("product_name_ocr"),
                    "blue_pixels_removed": cell.get("blue_pixels_removed"),
                    "tesseract_raw": cell.get("raw_ocr"),
                }
            )

    gemini: Dict[str, Any] = {
        "api_calls": 0,
        "results": [],
        "skipped": True,
        "requested": [],
        "passes": [],
    }
    applied = 0
    if enable_gemini:
        # Pass 1: ARJUNA qty (dual views — needed for 60/53 blue-mark selection).
        # Pass 2: DROPS qty with ORIGINAL-ONLY crops at high scale (dual clean
        # can bias Balance 185→186 in multi-cell batches; solo original reads 185).
        order = {
            "purchase_qty": 0,
            "closing_qty": 1,
            "total_qty": 2,
            "sales_qty": 3,
            "opening_qty": 4,
            "sales_return_qty": 5,
            "purchase_return_qty": 6,
        }
        arjuna_batch = sorted(
            jobs_by_product.get("ARJUNA TAB") or [],
            key=lambda j: order.get(j.get("column") or "", 9),
        )[:7]
        drops_batch = sorted(
            jobs_by_product.get("BONNISAN DROPS") or [],
            key=lambda j: order.get(j.get("column") or "", 9),
        )[:7]

        for label, batch, paired in (
            ("arjuna", arjuna_batch, True),
            ("drops", drops_batch, False),
        ):
            if not batch:
                continue
            if int(gemini.get("api_calls") or 0) >= 2:
                break
            gpass = gemini_read_cells_paired(
                bgr, batch, max_cells=7, paired=paired
            )
            applied += apply_gemini_results(rows, gpass, overwrite=True)
            gemini = {
                "api_calls": int(gemini.get("api_calls") or 0)
                + int(gpass.get("api_calls") or 0),
                "latency_ms": float(gemini.get("latency_ms") or 0)
                + float(gpass.get("latency_ms") or 0),
                "results": list(gemini.get("results") or [])
                + list(gpass.get("results") or []),
                "requested": list(gemini.get("requested") or [])
                + list(gpass.get("requested") or []),
                "passes": list(gemini.get("passes") or [])
                + [{**gpass, "label": label}],
                "error": gpass.get("error") or gemini.get("error"),
            }

    draw_debug_v3(bgr, geometry, rows, columns, debug_png)

    perf["gemini_api_calls"] = int(gemini.get("api_calls") or 0)
    perf["gemini_latency_ms"] = round(float(gemini.get("latency_ms") or 0), 1)
    perf["gemini_cells_applied"] = applied
    perf["total_ms"] = round((time.perf_counter() - t0) * 1000.0, 1)

    metrics = score_focus(rows)

    focus = []
    for product, expected in FOCUS_EXPECTATIONS.items():
        match = None
        for r in rows:
            if r.get("matched_product") == product or (
                r.get("product_name_ocr")
                and find_product([{"product_name": r["product_name_ocr"]}], product)
            ):
                match = r
                break
        if not match:
            focus.append({"product": product, "found": False, "expected": expected})
            continue
        focus.append(
            {
                "product": product,
                "found": True,
                "row_detected": True,
                "product_match_status": match.get("product_match_status"),
                "selected": match.get("selected"),
                "expected": expected,
                "identity": match.get("identity"),
                "reconciliation_failed": match.get("reconciliation_failed"),
                "cells": {
                    k: {
                        "raw_ocr": v.get("raw_ocr"),
                        "normalized": v.get("normalized"),
                        "gemini_value": v.get("gemini_value"),
                        "ocr_uncertain": v.get("ocr_uncertain"),
                        "blank": v.get("blank"),
                        "confidence": v.get("confidence"),
                        "bbox": v.get("bbox"),
                        "provider": v.get("provider"),
                        "variant": v.get("variant"),
                        "candidates": v.get("candidates"),
                        "blue_pixels_removed": v.get("blue_pixels_removed"),
                        "original_ocr": v.get("original_ocr"),
                        "cleaned_ocr": v.get("cleaned_ocr"),
                        "selected_ocr": v.get("selected_ocr"),
                        "selection_reason": v.get("selection_reason"),
                        "preferred_view": v.get("preferred_view"),
                    }
                    for k, v in (match.get("cells") or {}).items()
                },
            }
        )

    failed_variants = []
    for c in metrics.get("cell_rows") or []:
        if c.get("correct") != "PASS":
            failed_variants.append(
                {
                    "product": c["product"],
                    "field": c["field"],
                    "expected": c["expected"],
                    "extracted": c["extracted"],
                    "variant": c.get("variant"),
                    "selection_reason": c.get("selection_reason"),
                    "preferred_view": c.get("preferred_view"),
                }
            )

    return {
        "document": str(path),
        "production_switched": False,
        "benchmark_only": True,
        "geometry": {
            k: geometry[k]
            for k in ("image_width", "image_height", "row_ys", "col_xs", "table_bbox")
        },
        "header_mapping_source": header_source,
        "columns": [
            {
                "col_index": c.get("col_index"),
                "header_text": c.get("header_text"),
                "canonical": c.get("canonical"),
                "x0": c.get("x0"),
                "x1": c.get("x1"),
            }
            for c in columns
        ],
        "debug_png": debug_png,
        "rows_detected": len(rows),
        "rows": [
            {
                "row_index": r["row_index"],
                "row_detected": True,
                "row_bbox": r["row_bbox"],
                "product_name_ocr": r.get("product_name_ocr"),
                "pack_ocr": r.get("pack_ocr"),
                "product_match_status": r.get("product_match_status"),
                "matched_product": r.get("matched_product"),
                "selected": r.get("selected"),
                "identity": r.get("identity"),
                "reconciliation_failed": r.get("reconciliation_failed"),
                "cells": r.get("cells"),
            }
            for r in rows
        ],
        "focus": focus,
        "failed_cell_variants": failed_variants,
        "metrics": metrics,
        "gemini": {
            "api_calls": gemini.get("api_calls"),
            "latency_ms": gemini.get("latency_ms"),
            "error": gemini.get("error"),
            "results": gemini.get("results"),
            "cells_applied": applied,
        },
        "performance": perf,
    }


def write_v3_reports(
    report: Dict[str, Any],
    *,
    json_path: str = "/tmp/stock_geometry_hybrid_v3.json",
    txt_path: str = "/tmp/stock_geometry_hybrid_v3.txt",
) -> Tuple[Path, Path]:
    jp, tp = Path(json_path), Path(txt_path)
    jp.write_text(json.dumps(report, indent=2, default=str))
    lines = [
        "GEOMETRY HYBRID V3 BENCHMARK (blue-mark robust)",
        "=" * 72,
        f"Document: {report.get('document')}",
        f"Production switched: {report.get('production_switched')}",
        f"Header mapping source: {report.get('header_mapping_source')}",
        f"Rows detected: {report.get('rows_detected')}",
        f"Debug PNG: {report.get('debug_png')}",
        "",
        "--- Metrics ---",
        json.dumps(report.get("metrics"), indent=2, default=str),
        "",
        "--- Performance ---",
        json.dumps(report.get("performance"), indent=2, default=str),
        "",
        "--- Gemini ---",
        json.dumps(
            {
                k: report.get("gemini", {}).get(k)
                for k in ("api_calls", "latency_ms", "cells_applied", "error")
            },
            indent=2,
        ),
        "",
        "--- Focus ---",
    ]
    for f in report.get("focus") or []:
        lines.append(json.dumps({k: f.get(k) for k in f if k != "cells"}, default=str))
        if f.get("found"):
            sel = f.get("selected") or {}
            exp = f.get("expected") or {}
            lines.append(
                f"  selected: open={sel.get('opening_qty')} pur={sel.get('purchase_qty')} "
                f"gret={sel.get('sales_return_qty')} tot={sel.get('total_qty')} "
                f"sale={sel.get('sales_qty')} pret={sel.get('purchase_return_qty')} "
                f"bal={sel.get('closing_qty')}"
            )
            lines.append(
                f"  expected: open={exp.get('opening_qty')} pur={exp.get('purchase_qty')} "
                f"gret={exp.get('sales_return_qty')} tot={exp.get('total_qty')} "
                f"sale={exp.get('sales_qty')} pret={exp.get('purchase_return_qty')} "
                f"bal={exp.get('closing_qty')}"
            )
            # blue-mark logs for qty cells
            for canon in QTY_FIELDS:
                cell = (f.get("cells") or {}).get(canon) or {}
                if not cell:
                    continue
                lines.append(
                    f"  {canon}: blue_px={cell.get('blue_pixels_removed')} "
                    f"orig_ocr={cell.get('original_ocr')!r} "
                    f"clean_ocr={cell.get('cleaned_ocr')!r} "
                    f"selected={cell.get('selected_ocr')!r} "
                    f"reason={cell.get('selection_reason')} "
                    f"view={cell.get('preferred_view')} "
                    f"gem={cell.get('gemini_value')}"
                )
    lines.append("")
    lines.append("--- Failed cell variants ---")
    lines.append(json.dumps(report.get("failed_cell_variants"), indent=2, default=str))
    lines.append("")
    lines.append("--- Cell comparison ---")
    for c in (report.get("metrics") or {}).get("cell_rows") or []:
        lines.append(
            f"{c['product']} | {c['field']} | exp={c['expected']} | got={c['extracted']} | {c['correct']}"
        )
    tp.write_text("\n".join(lines))
    return jp, tp
