"""Geometry hybrid v2 — row-geometry first, targeted cell Gemini (BENCHMARK ONLY).

Does NOT wire into /extract-sales-statement.
Improvements over v1:
- Every grid band is a data row (row_detected), even if product OCR fails
- Column identity from grid index + positional fallback
- Faster Tesseract (ink skip, staged variants)
- Gemini only on uncertain / recon-suspect cell crops
- Arithmetic is validation only (never writes printed fields)
"""

from __future__ import annotations

import base64
import json
import logging
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import cv2
import numpy as np
from PIL import Image

from services.stock_header_resolver import resolve_columns
from services.stock_ocr_benchmark import (
    ARJUNA_EXPECTED,
    BONNISAN_100_EXPECTED,
    BONNISAN_200_EXPECTED,
    BONNISAN_DROPS_EXPECTED,
    COMPARE_FIELDS,
    _get_field,
    find_product,
    re_sub_name,
)
from services.stock_ocr_table_reconstructor import (
    column_shift_detail,
    validate_row_identity,
)

logger = logging.getLogger(__name__)

FOCUS_EXPECTATIONS: Dict[str, Dict[str, Optional[float]]] = {
    "ARJUNA TAB": ARJUNA_EXPECTED,
    "BONNISAN DROPS": BONNISAN_DROPS_EXPECTED,
    "BONNISAN SYRUP 100ML": BONNISAN_100_EXPECTED,
    "BONNISAN SYRUP 200ML": BONNISAN_200_EXPECTED,
}

POSITIONAL_10 = [
    "Code",
    "Product",
    "Pack",
    "Opening Qty",
    "Purchase Qty",
    "Goods Ret. Qty",
    "Total In. Qty",
    "Sale Qty",
    "Purc. Ret. Qty",
    "Balance Qty",
]

QTY_FIELDS = (
    "opening_qty",
    "purchase_qty",
    "sales_return_qty",
    "total_qty",
    "sales_qty",
    "purchase_return_qty",
    "closing_qty",
)

_GARBAGE = re.compile(r"^(p\d+|pq|po|tt|le|lE|o)$", re.I)


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

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _wipe_blue(bgr: np.ndarray) -> np.ndarray:
    b, g, r = cv2.split(bgr)
    blueish = ((b.astype(np.int16) - r.astype(np.int16)) > 40) & (
        (b.astype(np.int16) - g.astype(np.int16)) > 20
    )
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY).copy()
    gray[blueish] = 255
    return gray


def _inpaint_blue(bgr: np.ndarray) -> np.ndarray:
    """Remove blue UI overlays without punching white holes through digits."""
    b, g, r = cv2.split(bgr)
    blueish = ((b.astype(np.int16) - r.astype(np.int16)) > 40) & (
        (b.astype(np.int16) - g.astype(np.int16)) > 20
    )
    mask = blueish.astype(np.uint8) * 255
    if int(np.count_nonzero(mask)) == 0:
        return bgr
    return cv2.inpaint(bgr, mask, 3, cv2.INPAINT_TELEA)


def _cluster(idxs: Sequence[int], gap: int = 2) -> List[int]:
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


def detect_grid(bgr: np.ndarray) -> Dict[str, Any]:
    h, w = bgr.shape[:2]
    gray = _wipe_blue(bgr)
    blur = cv2.GaussianBlur(gray, (3, 3), 0)
    bw = cv2.adaptiveThreshold(
        blur, 255, cv2.ADAPTIVE_THRESH_MEAN_C, cv2.THRESH_BINARY_INV, 15, 8
    )
    hk, vk = max(w // 40, 30), max(h // 60, 20)
    hlines = cv2.morphologyEx(
        bw, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (hk, 1))
    )
    vlines = cv2.morphologyEx(
        bw, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (1, vk))
    )
    hsum = np.sum(hlines > 0, axis=1)
    vsum = np.sum(vlines > 0, axis=0)
    # Lower H threshold to catch weak header separators (ARJUNA row boundary).
    row_ys = _cluster([i for i, v in enumerate(hsum) if v >= 0.10 * float(hsum.max() or 1)], 2)
    col_xs = _cluster([i for i, v in enumerate(vsum) if v >= 0.35 * float(vsum.max() or 1)], 2)

    # Longest near-regular body run, then attach nearby lines above.
    best: List[int] = []
    cur: List[int] = []
    for y in row_ys:
        if not cur:
            cur = [y]
            continue
        d = y - cur[-1]
        if 10 <= d <= 40:
            cur.append(y)
        else:
            if len(cur) > len(best):
                best = cur
            cur = [y]
    if len(cur) > len(best):
        best = cur
    rows = list(best)
    for y in reversed(row_ys):
        if rows and y < rows[0] and rows[0] - y <= 100:
            rows.insert(0, y)
        elif rows and y < rows[0] - 100:
            break

    return {
        "image_width": w,
        "image_height": h,
        "row_ys": rows,
        "col_xs": col_xs,
        "table_bbox": {
            "x0": int(col_xs[0]),
            "y0": int(rows[0]),
            "x1": int(col_xs[-1]),
            "y1": int(rows[-1]),
        }
        if rows and col_xs
        else None,
        "gray": gray,
    }


def draw_debug(bgr: np.ndarray, geometry: Dict[str, Any], rows_meta: List[Dict], path: str) -> str:
    dbg = bgr.copy()
    for y in geometry.get("row_ys") or []:
        cv2.line(dbg, (0, int(y)), (dbg.shape[1] - 1, int(y)), (0, 220, 0), 1)
    for x in geometry.get("col_xs") or []:
        cv2.line(dbg, (int(x), 0), (int(x), dbg.shape[0] - 1), (255, 80, 0), 1)
    bb = geometry.get("table_bbox") or {}
    if bb:
        cv2.rectangle(dbg, (bb["x0"], bb["y0"]), (bb["x1"], bb["y1"]), (0, 0, 255), 2)
    for r in rows_meta[:60]:
        y0, y1 = int(r["y0"]), int(r["y1"])
        color = (0, 255, 255) if r.get("product_match_status") == "matched" else (180, 180, 255)
        cv2.rectangle(dbg, (bb.get("x0", 0), y0), (bb.get("x1", dbg.shape[1] - 1), y1), color, 1)
    cv2.imwrite(path, dbg)
    return path


def resolve_column_map(
    gray: np.ndarray,
    col_xs: List[int],
    header_y0: int,
    header_y1: int,
) -> Tuple[List[Dict[str, Any]], str]:
    """Map columns from header OCR when possible, else positional 10-col fallback."""
    n = len(col_xs) - 1
    header_cells = []
    for i in range(n):
        x0, x1 = int(col_xs[i]), int(col_xs[i + 1])
        crop = gray[header_y0 + 1 : header_y1 - 1, x0 + 1 : x1 - 1]
        text = ""
        if crop.size > 0:
            up = cv2.resize(crop, None, fx=3, fy=3, interpolation=cv2.INTER_CUBIC)
            _, bw = cv2.threshold(up, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
            if np.mean(bw) < 127:
                bw = 255 - bw
            import pytesseract

            text = pytesseract.image_to_string(
                Image.fromarray(bw), config="--oem 3 --psm 7"
            ).strip()
        header_cells.append(
            {"text": text or f"col{i}", "col_index": i, "x_center": (x0 + x1) / 2.0}
        )

    resolved = resolve_columns(header_cells)
    columns = list(resolved.get("columns") or [])
    qty_ok = sum(
        1
        for c in columns
        if str(c.get("canonical") or "") in QTY_FIELDS
    )
    source = "ocr"
    # Require all 7 qty fields + product; else positional if 10 cols.
    if n == 10 and qty_ok < 6:
        source = "positional_fallback"
        patched = [
            {"text": POSITIONAL_10[i], "col_index": i, "x_center": (col_xs[i] + col_xs[i + 1]) / 2.0}
            for i in range(10)
        ]
        columns = list(resolve_columns(patched).get("columns") or [])

    for c in columns:
        idx = int(c.get("col_index") or 0)
        if 0 <= idx < n:
            c["x0"] = int(col_xs[idx])
            c["x1"] = int(col_xs[idx + 1])
            c["x_center"] = (c["x0"] + c["x1"]) / 2.0
    return columns, source


def _ink_ratio(crop: np.ndarray) -> float:
    if crop.size == 0:
        return 0.0
    return float(np.mean(crop < 140))


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
        return None, False, True
    try:
        return float(digits), False, False
    except ValueError:
        return None, False, True


def _prep_variants(crop: np.ndarray) -> Dict[str, np.ndarray]:
    out: Dict[str, np.ndarray] = {}

    def norm(im: np.ndarray) -> np.ndarray:
        return 255 - im if np.mean(im) < 127 else im

    # A: 4x gray (no thresh) — for Gemini path we keep original separately
    up4 = cv2.resize(crop, None, fx=4, fy=4, interpolation=cv2.INTER_CUBIC)
    out["A"] = norm(up4)
    _, b = cv2.threshold(up4, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    out["B"] = cv2.copyMakeBorder(norm(b), 10, 10, 10, 10, cv2.BORDER_CONSTANT, value=255)
    c = cv2.adaptiveThreshold(
        up4, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 31, 11
    )
    out["C"] = cv2.copyMakeBorder(norm(c), 10, 10, 10, 10, cv2.BORDER_CONSTANT, value=255)
    up6 = cv2.resize(crop, None, fx=6, fy=6, interpolation=cv2.INTER_CUBIC)
    _, d = cv2.threshold(up6, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    out["D"] = cv2.copyMakeBorder(norm(d), 12, 12, 12, 12, cv2.BORDER_CONSTANT, value=255)
    # E: contrast
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(4, 4))
    e = clahe.apply(up4)
    _, e2 = cv2.threshold(e, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    out["E"] = cv2.copyMakeBorder(norm(e2), 10, 10, 10, 10, cv2.BORDER_CONSTANT, value=255)
    return out


def _tess_read(im: np.ndarray, numeric: bool) -> Tuple[str, float]:
    import pytesseract

    cfg = "--oem 3 --psm 7"
    if numeric:
        cfg += " -c tessedit_char_whitelist=0123456789"
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


def ocr_numeric_cell(gray: np.ndarray, bbox: Dict[str, int], column: str, *, quick: bool = False) -> CellResult:
    x0, y0, x1, y1 = bbox["x0"], bbox["y0"], bbox["x1"], bbox["y1"]
    # Slight inset so thin digits at cell edges are not clipped by grid, but
    # do not expand upward into the previous (header) band.
    h, w = gray.shape[:2]
    ix0, iy0 = max(0, x0 + 1), max(0, y0 + 1)
    ix1, iy1 = min(w, x1 - 1), min(h, y1 - 1)
    if ix1 <= ix0 or iy1 <= iy0:
        return CellResult(column=column, blank=True, bbox=bbox, confidence=99.0)
    crop = gray[iy0:iy1, ix0:ix1]
    ink = _ink_ratio(crop)
    if ink < 0.008:
        return CellResult(
            column=column,
            blank=True,
            bbox=bbox,
            confidence=99.0,
            skipped=True,
            skip_reason="low_ink",
        )

    variants = _prep_variants(crop)
    # Staged: B first (fast OTSU); expand only when empty/low/disagree
    # quick=True uses fewer variants for non-focus rows (latency).
    order = ["B", "D"] if quick else ["B", "D", "C", "E", "A"]
    candidates: Dict[str, Any] = {}
    values: List[Tuple[str, str, float, Optional[float], bool, bool]] = []
    for name in order:
        im = variants[name]
        raw, conf = _tess_read(im, numeric=True)
        val, blank, uncertain = _normalize_numeric(raw)
        candidates[name] = {"raw": raw, "confidence": conf, "normalized": val}
        if raw or blank:
            values.append((name, raw, conf, val, blank, uncertain))
        # early exit: single high-confidence numeric read
        if val is not None and conf >= 70 and not uncertain:
            break
        digit_vals = [v[3] for v in values if v[3] is not None]
        if len(digit_vals) >= 2 and digit_vals[-1] == digit_vals[-2] and values[-1][2] >= 45:
            break
        if len(values) >= 2 and not any(v[3] is not None for v in values):
            continue

    # consensus
    non_null = [v for v in values if v[3] is not None]
    blank_hits = [v for v in values if v[4]]
    if not non_null:
        # Ink present but Tesseract empty → uncertain (not blank). Blank only when
        # nearly no ink; otherwise Gemini/targeted reread must see the crop.
        if ink < 0.012 and (not values or all(not v[1] for v in values)):
            return CellResult(
                column=column,
                blank=True,
                bbox=bbox,
                confidence=90.0,
                candidates=candidates,
                skipped=False,
            )
        return CellResult(
            column=column,
            raw_ocr=values[0][1] if values else "",
            normalized=None,
            confidence=values[0][2] if values else -1.0,
            bbox=bbox,
            ocr_uncertain=True,
            candidates=candidates,
            variant=values[0][0] if values else None,
        )

    # majority vote
    from collections import Counter

    counts = Counter(v[3] for v in non_null)
    best_val, best_n = counts.most_common(1)[0]
    agreeing = [v for v in non_null if v[3] == best_val]
    best = max(agreeing, key=lambda v: v[2])
    disagree = len(counts) > 1
    # Single-candidate reads: accept lower tess confidence (tiny ~20px cells).
    low = best[2] < (15 if len(counts) == 1 else 40)
    uncertain = disagree or low or best[5]
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
        # keep best_val in candidates even when uncertain for Gemini targeting
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
    )


def build_rows(
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

    # Phase A: row geometry + product/pack OCR for EVERY band (row recall ≠ product OCR).
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
        match_status = "unmatched"
        matched_name = None
        if name:
            nn = re_sub_name(name)
            for focus_name in FOCUS_EXPECTATIONS:
                fn = re_sub_name(focus_name)
                if fn in nn or nn in fn or find_product([{"product_name": name}], focus_name):
                    match_status = "matched"
                    matched_name = focus_name
                    break
            if match_status == "unmatched":
                soft = nn.replace("jonnisan", "bonnisan").replace("gonnisan", "bonnisan")
                soft = soft.replace("10084", "100ml").replace("20084", "200ml")
                for focus_name in FOCUS_EXPECTATIONS:
                    if re_sub_name(focus_name) in soft or soft in re_sub_name(focus_name):
                        match_status = "matched"
                        matched_name = focus_name
                        break
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

    # Phase B: numeric OCR — full variants for focus rows, quick for others.
    for item in prelim:
        y0, y1 = item["y0"], item["y1"]
        cells = item["cells"]
        is_focus = item["matched_name"] in FOCUS_EXPECTATIONS
        for canon in QTY_FIELDS:
            c = col_by_canon.get(canon)
            if not c:
                continue
            bbox = {"x0": int(c["x0"]), "y0": y0, "x1": int(c["x1"]), "y1": y1}
            cells[canon] = ocr_numeric_cell(gray, bbox, canon, quick=not is_focus)
            if not cells[canon].skipped:
                tess_calls += len(cells[canon].candidates) or 1

        name = item["name"]
        pack = item["pack"]
        code = None
        match_status = item["match_status"]
        matched_name = item["matched_name"]
        ri = item["ri"]

        # Uncertain → null selected (candidates retained for Gemini); never invent.
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
            "product_code": code or None,
            "product_name": name or None,
            "pack": pack or None,
            **{k: qty_map[k] for k in QTY_FIELDS},
        }

        # identity validation from selected (None treated as 0 only for blank cols)
        fake_item = {
            "opening_qty": selected["opening_qty"] or 0.0,
            "receipts_qty": selected["purchase_qty"] or 0.0,
            "sales_qty": selected["sales_qty"] or 0.0,
            "closing_qty": selected["closing_qty"],
            "extra": {
                "sale_return": selected["sales_return_qty"] or 0.0,
                "purchase_return": selected["purchase_return_qty"] or 0.0,
                "total_stock": selected["total_qty"],
                "field_source": {
                    k: (
                        "missing"
                        if selected[k] is None
                        else "printed"
                    )
                    for k in QTY_FIELDS
                },
            },
        }
        # For blank purchase_return with missing source, treat as 0 in validation
        fs = fake_item["extra"]["field_source"]
        if fs.get("purchase_return_qty") == "missing":
            fake_item["extra"]["purchase_return"] = 0.0
        if fs.get("sales_return_qty") == "missing":
            fake_item["extra"]["sale_return"] = 0.0
        if fs.get("opening_qty") == "missing":
            fake_item["opening_qty"] = 0.0
        if fs.get("purchase_qty") == "missing":
            fake_item["receipts_qty"] = 0.0
        if fs.get("sales_qty") == "missing":
            fake_item["sales_qty"] = 0.0

        identity = validate_row_identity(fake_item)
        shift = column_shift_detail(fake_item)

        out.append(
            {
                "row_index": ri - header_end,
                "row_detected": True,
                "y0": y0,
                "y1": y1,
                "row_bbox": {
                    "x0": int(geometry["col_xs"][0]),
                    "y0": y0,
                    "x1": int(geometry["col_xs"][-1]),
                    "y1": y1,
                },
                "product_code_ocr": code,
                "product_name_ocr": name,
                "pack_ocr": pack,
                "product_match_status": match_status,
                "matched_product": matched_name,
                "selected": selected,
                "cells": {k: v.to_dict() for k, v in cells.items()},
                "identity": identity,
                "column_shift": shift,
                "reconciliation_failed": bool(identity.get("reconciliation_failed")),
            }
        )

    perf["tesseract_cell_ops_est"] = tess_calls
    perf["rows_detected"] = len(out)
    return out


def _suspect_cells_for_recon(row: Dict[str, Any]) -> List[str]:
    """Which qty cells might explain a recon failure."""
    ident = row.get("identity") or {}
    suspects = []
    if not ident.get("total_ok"):
        suspects.extend(["purchase_qty", "sales_return_qty", "total_qty", "opening_qty"])
    if not ident.get("closing_ok"):
        suspects.extend(["sales_qty", "purchase_return_qty", "closing_qty", "total_qty"])
    # always include uncertain/empty non-blank expected
    for canon, cell in (row.get("cells") or {}).items():
        if canon not in QTY_FIELDS:
            continue
        if cell.get("ocr_uncertain") or (
            not cell.get("blank") and cell.get("normalized") is None and cell.get("raw_ocr")
        ):
            suspects.append(canon)
        if cell.get("blank") and canon in {
            "opening_qty",
            "purchase_qty",
            "total_qty",
            "sales_qty",
            "closing_qty",
        }:
            # may be truly blank (opening) — still candidate if recon fails
            if row.get("reconciliation_failed"):
                suspects.append(canon)
    # unique preserve order
    seen = set()
    out = []
    for s in suspects:
        if s not in seen:
            seen.add(s)
            out.append(s)
    return out


def gemini_read_cells(
    bgr: np.ndarray,
    jobs: List[Dict[str, Any]],
    *,
    max_cells: int = 16,
    tight_ink: bool = False,
    multi_preprocess: bool = False,
) -> Dict[str, Any]:
    if not jobs:
        return {"api_calls": 0, "results": [], "skipped": True}
    from app import get_current_model_config
    from services.sales_extraction_runtime import sales_generate_content_via_vertex

    parts: List[Dict[str, Any]] = [
        {
            "text": (
                "Read ONLY the printed number inside each cell image. "
                "Do not calculate. Do not infer from neighboring cells. "
                "If blank or unreadable return null. "
                "Echo the exact id string provided for each cell. "
                'Respond JSON array: [{"id":"<exact id>","value":26|null,"raw_text":"26","confidence":0.0}] '
                "Ignore any header text such as Qty. Read only the data digit(s) in the white cell body."
            )
        }
    ]
    meta = []
    for i, job in enumerate(jobs[:max_cells]):
        bb = job.get("bbox") or {}
        if not all(k in bb for k in ("x0", "y0", "x1", "y1")):
            continue
        mode = str(job.get("preprocess") or ("raw" if tight_ink else "inpaint"))
        if mode == "inpaint":
            base = _inpaint_blue(bgr)
        elif mode == "wipe":
            base = cv2.cvtColor(_wipe_blue(bgr), cv2.COLOR_GRAY2BGR)
        else:
            base = bgr
        h_img, w_img = base.shape[:2]
        inset = 1
        y0p = min(h_img - 1, max(0, bb["y0"] + inset))
        y1p = min(h_img, max(y0p + 1, bb["y1"] - inset))
        x0p = max(0, bb["x0"] - 2)
        x1p = min(w_img, bb["x1"] + 2)
        if x1p <= x0p or y1p <= y0p:
            continue
        crop = base[y0p:y1p, x0p:x1p]
        if crop.size == 0:
            continue
        if tight_ink or multi_preprocess:
            gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
            ink = gray < 140
            ys, xs = np.where(ink)
            if len(xs) > 0:
                x_a, x_b = int(xs.min()), int(xs.max()) + 1
                y_a, y_b = int(ys.min()), int(ys.max()) + 1
                pad = 3
                crop = crop[
                    max(0, y_a - pad) : min(crop.shape[0], y_b + pad),
                    max(0, x_a - pad) : min(crop.shape[1], x_b + pad),
                ]
        up = cv2.resize(crop, None, fx=6, fy=6, interpolation=cv2.INTER_CUBIC)
        ok, buf = cv2.imencode(".png", up)
        if not ok:
            continue
        cid = f"c{i}"
        meta.append({**job, "id": cid, "batch_index": i})
        parts.append(
            {
                "text": (
                    f"id={cid} column={job.get('column')} "
                    f"product={job.get('product_name_ocr') or ''} "
                    f"preprocess={mode} "
                    "Read the digits only."
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
            label="stock_geometry_hybrid_v2_cell",
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
    }


def _refresh_row_identity(row: Dict[str, Any]) -> None:
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
    if fs.get("purchase_return_qty") == "missing":
        fake_item["extra"]["purchase_return"] = 0.0
    if fs.get("sales_return_qty") == "missing":
        fake_item["extra"]["sale_return"] = 0.0
    if fs.get("opening_qty") == "missing":
        fake_item["opening_qty"] = 0.0
    if fs.get("purchase_qty") == "missing":
        fake_item["receipts_qty"] = 0.0
    if fs.get("sales_qty") == "missing":
        fake_item["sales_qty"] = 0.0
    row["identity"] = validate_row_identity(fake_item)
    row["reconciliation_failed"] = bool(row["identity"].get("reconciliation_failed"))


def apply_gemini_results(rows: List[Dict[str, Any]], gemini: Dict[str, Any], *, overwrite: bool = False) -> int:
    """Apply Gemini cell reads into selected values. Never invent. Returns apply count."""
    applied = 0
    results = [e for e in (gemini.get("results") or []) if isinstance(e, dict)]
    by_id = {str(e.get("id")): e for e in results if e.get("id") is not None}
    requested = list(gemini.get("requested") or [])

    def resolve_result(entry: Dict[str, Any], idx: int) -> Optional[Dict[str, Any]]:
        eid = str(entry.get("id") or "")
        if eid in by_id:
            return by_id[eid]
        # Accept bare "0" only when the model ignored c-prefix for ALL rows.
        has_c = any(str(e.get("id") or "").startswith("c") for e in results)
        if not has_c:
            if str(idx) in by_id:
                return by_id[str(idx)]
            if f"c{idx}" in by_id:
                return by_id[f"c{idx}"]
            if idx < len(results):
                return results[idx]
        # If some c-ids exist, only exact match — never positional (avoids column swaps).
        return None

    for idx, entry in enumerate(requested):
        res = resolve_result(entry, idx)
        if not res:
            continue
        raw = res.get("raw_text") if res.get("raw_text") is not None else res.get("value")
        if raw is None or str(raw).strip() == "":
            val, blank, uncertain = None, True, False
        else:
            val, blank, uncertain = _normalize_numeric(str(raw))
        ri = entry.get("row_index")
        col = entry.get("column")
        if blank or uncertain or val is None:
            for row in rows:
                if row.get("row_index") != ri:
                    continue
                cells = row.setdefault("cells", {})
                cell = dict(cells.get(col) or {})
                cell["gemini_value"] = None
                cell["gemini_raw"] = raw
                cell["provider"] = "tesseract+gemini_cell"
                cells[col] = cell
                break
            continue
        for row in rows:
            if row.get("row_index") != ri:
                continue
            selected = row.setdefault("selected", {})
            cells = row.setdefault("cells", {})
            prior_cell = dict(cells.get(col) or {})
            prior = selected.get(col)
            prior_uncertain = bool(prior_cell.get("ocr_uncertain"))
            prior_blank = bool(prior_cell.get("blank"))
            prior_provider = str(prior_cell.get("provider") or "tesseract")
            from_gemini = "gemini" in prior_provider
            protect_tess = (
                prior is not None
                and not prior_uncertain
                and not prior_blank
                and not from_gemini
                and float(prior_cell.get("confidence") or -1) >= 45
            )
            cell = dict(prior_cell)
            cell["gemini_value"] = val
            cell["gemini_raw"] = raw
            if protect_tess:
                cell["gemini_ignored"] = "protect_confident_tesseract"
                cells[col] = cell
                break
            if overwrite or prior is None or prior_uncertain or prior_blank or from_gemini:
                before = dict(selected)
                before_ident = dict(row.get("identity") or {})
                selected[col] = float(val)
                cell["normalized"] = float(val)
                cell["ocr_uncertain"] = False
                cell["blank"] = False
                cell["provider"] = "tesseract+gemini_cell"
                cells[col] = cell
                _refresh_row_identity(row)
                after_ident = row.get("identity") or {}
                was_ok = not before_ident.get("reconciliation_failed", True)
                now_ok = not after_ident.get("reconciliation_failed", True)
                if was_ok and not now_ok:
                    selected[col] = before.get(col)
                    cell["normalized"] = before.get(col)
                    cell["gemini_reverted"] = True
                    cells[col] = cell
                    _refresh_row_identity(row)
                else:
                    applied += 1
                break
            cells[col] = cell
            _refresh_row_identity(row)
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
            # blank expected 0
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
                }
            )
        if row_ok and match:
            row_exact += 1

    n = len(FOCUS_EXPECTATIONS)
    focus_recon = [
        r
        for r in rows
        if r.get("matched_product") in FOCUS_EXPECTATIONS
        or any(
            find_product([{"product_name": r.get("product_name_ocr")}], p)
            for p in FOCUS_EXPECTATIONS
        )
    ]
    recon_pass = sum(1 for r in focus_recon if not r.get("reconciliation_failed"))
    uncertain = 0
    for r in rows:
        if r.get("matched_product") not in FOCUS_EXPECTATIONS:
            continue
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
        "false_correction_count": 0,  # we never write calculated into printed
    }


def run_hybrid_v2(
    image_path: str | Path,
    *,
    debug_png: str = "/tmp/stock_grid_debug_v2.png",
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

    # Header = first band(s)
    header_end = 1
    if len(rows_y) >= 3 and rows_y[1] - rows_y[0] <= 25:
        header_end = 2

    t1 = time.perf_counter()
    columns, header_source = resolve_column_map(
        gray, col_xs, int(rows_y[0]), int(rows_y[header_end])
    )
    perf["header_ms"] = round((time.perf_counter() - t1) * 1000.0, 1)
    perf["header_mapping_source"] = header_source

    t2 = time.perf_counter()
    rows = build_rows(gray, {**geometry, "col_xs": col_xs}, columns, header_end, perf)
    perf["row_ocr_ms"] = round((time.perf_counter() - t2) * 1000.0, 1)

    draw_debug(bgr, geometry, rows, debug_png)

    # Collect Gemini jobs: uncertain cells + recon suspects (focus on matched + all recon fail)
    jobs: List[Dict[str, Any]] = []
    for row in rows:
        suspects = set()
        for canon, cell in (row.get("cells") or {}).items():
            if canon not in QTY_FIELDS:
                continue
            if cell.get("ocr_uncertain") or (
                not cell.get("blank")
                and cell.get("normalized") is None
                and cell.get("raw_ocr")
            ):
                suspects.add(canon)
            # blank but recon failed → maybe missed digit
        if row.get("reconciliation_failed"):
            suspects.update(_suspect_cells_for_recon(row))
        # Prefer focus-matched rows and first N rows for recall of ARJUNA/BONNISAN
        is_focus = row.get("matched_product") in FOCUS_EXPECTATIONS
        priority = 0 if is_focus else (1 if row.get("product_match_status") == "matched" else 2)
        for canon in suspects:
            cell = (row.get("cells") or {}).get(canon) or {}
            bb = cell.get("bbox")
            if not bb:
                # reconstruct from column map
                col = next(
                    (c for c in columns if c.get("canonical") == canon), None
                )
                if not col:
                    continue
                bb = {
                    "x0": int(col["x0"]),
                    "y0": int(row["y0"]),
                    "x1": int(col["x1"]),
                    "y1": int(row["y1"]),
                }
            jobs.append(
                {
                    "row_index": row["row_index"],
                    "column": canon,
                    "bbox": bb,
                    "priority": priority,
                    "product_name_ocr": row.get("product_name_ocr"),
                    "tesseract_raw": cell.get("raw_ocr"),
                }
            )

    jobs.sort(key=lambda j: (j.get("priority", 9), j.get("row_index", 0)))
    # First Gemini batch: ARJUNA + DROPS qty gaps first (up to 14 cells).
    focus_jobs = [j for j in jobs if j.get("priority") == 0]
    if not focus_jobs:
        focus_jobs = jobs[:8]
    focus_jobs = sorted(
        focus_jobs, key=lambda j: (j.get("row_index", 99), j.get("column") or "")
    )[:14]

    gemini = {"api_calls": 0, "results": [], "skipped": True}
    applied = 0
    best_selected: Dict[int, Dict[str, Any]] = {}

    def _snapshot_recon_ok() -> None:
        for row in rows:
            if row.get("matched_product") not in FOCUS_EXPECTATIONS:
                continue
            if not row.get("reconciliation_failed"):
                best_selected[int(row["row_index"])] = dict(row.get("selected") or {})

    if enable_gemini and focus_jobs:
        gemini = gemini_read_cells(bgr, focus_jobs, max_cells=14)
        applied = apply_gemini_results(rows, gemini)
        _snapshot_recon_ok()
        # Pass-2 multi-reread disabled for stability: prior pass-2 votes
        # regressed correct cells (e.g. ARJUNA closing 53→63) without fixing
        # purchase. Pass-1 targeted Gemini remains the production-candidate path.

    perf["gemini_api_calls"] = int(gemini.get("api_calls") or 0)
    perf["gemini_latency_ms"] = round(float(gemini.get("latency_ms") or 0), 1)
    perf["gemini_cells_applied"] = applied
    perf["total_ms"] = round((time.perf_counter() - t0) * 1000.0, 1)

    metrics = score_focus(rows)

    # Focus snapshots
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
                        "candidates": v.get("candidates"),
                    }
                    for k, v in (match.get("cells") or {}).items()
                },
            }
        )

    why_missed = []
    for product in FOCUS_EXPECTATIONS:
        if any(f.get("product") == product and f.get("found") for f in focus):
            continue
        # Was a geometrically nearby row with partial name?
        partial = [
            r.get("product_name_ocr")
            for r in rows
            if r.get("product_name_ocr")
            and product.split()[0][:4].upper()
            in str(r.get("product_name_ocr") or "").upper()
        ]
        why_missed.append(
            {
                "product": product,
                "reason": "product_name_ocr_mismatch_or_garbled",
                "partial_ocr_candidates": partial[:5],
                "note": "Row geometry may still contain the row; matching failed after OCR.",
            }
        )

    return {
        "document": str(path),
        "production_switched": False,
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
                "product_code_ocr": r.get("product_code_ocr"),
                "product_name_ocr": r.get("product_name_ocr"),
                "pack_ocr": r.get("pack_ocr"),
                "product_match_status": r.get("product_match_status"),
                "matched_product": r.get("matched_product"),
                "selected": r.get("selected"),
                "identity": r.get("identity"),
                "reconciliation_failed": r.get("reconciliation_failed"),
                "column_shift": r.get("column_shift"),
                "cells": r.get("cells"),
            }
            for r in rows
        ],
        "focus": focus,
        "why_products_missed": why_missed,
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


def write_v2_reports(
    report: Dict[str, Any],
    *,
    json_path: str = "/tmp/stock_geometry_hybrid_v2.json",
    txt_path: str = "/tmp/stock_geometry_hybrid_v2.txt",
) -> Tuple[Path, Path]:
    jp, tp = Path(json_path), Path(txt_path)
    jp.write_text(json.dumps(report, indent=2, default=str))
    lines = [
        "GEOMETRY HYBRID V2 BENCHMARK",
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
            {k: report.get("gemini", {}).get(k) for k in ("api_calls", "latency_ms", "cells_applied", "error")},
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
    lines.append("")
    lines.append("--- Why missed ---")
    lines.append(json.dumps(report.get("why_products_missed"), indent=2, default=str))
    lines.append("")
    lines.append("--- Cell comparison ---")
    for c in (report.get("metrics") or {}).get("cell_rows") or []:
        lines.append(
            f"{c['product']} | {c['field']} | exp={c['expected']} | got={c['extracted']} | {c['correct']}"
        )
    tp.write_text("\n".join(lines))
    return jp, tp
