"""Unit tests for geometry / per-cell OCR benchmark helpers."""

from __future__ import annotations

import numpy as np

from services.stock_geometry_ocr import (
    normalize_numeric,
    detect_table_grid,
    _cluster_positions,
)
from services.stock_header_resolver import resolve_columns


def test_normalize_never_invents_garbage():
    assert normalize_numeric("p3") == (None, False, True)
    assert normalize_numeric("pq") == (None, False, True)
    assert normalize_numeric("25") == (25.0, False, False)
    assert normalize_numeric("618") == (618.0, False, False)
    assert normalize_numeric("") == (None, True, False)
    assert normalize_numeric("26") == (26.0, False, False)


def test_purc_ret_header_never_purchase():
    cells = [
        {"text": "Opening Qty", "col_index": 0, "x_center": 1},
        {"text": "Purchase Qty", "col_index": 1, "x_center": 2},
        {"text": "Goods Ret. Qty", "col_index": 2, "x_center": 3},
        {"text": "Total In. Qty", "col_index": 3, "x_center": 4},
        {"text": "Sale Qty", "col_index": 4, "x_center": 5},
        {"text": "Purc. Ret. Qty", "col_index": 5, "x_center": 6},
        {"text": "Balance Qty", "col_index": 6, "x_center": 7},
    ]
    resolved = resolve_columns(cells)
    by = {c["col_index"]: c["canonical"] for c in resolved["columns"]}
    assert by[1] == "purchase_qty"
    assert by[5] == "purchase_return_qty"
    assert by[5] != "purchase_qty"


def test_cluster_positions():
    xs = _cluster_positions([10, 11, 12, 50, 51], gap=2)
    assert len(xs) == 2
    assert 10 <= xs[0] <= 12
    assert 50 <= xs[1] <= 51


def test_detect_grid_on_problem_png():
    import cv2
    from pathlib import Path

    path = Path("/var/www/html/splitextract/0000730099_2026_08_ZA_06_333_04092026081833.png")
    if not path.is_file():
        return
    bgr = cv2.imread(str(path))
    g = detect_table_grid(bgr)
    assert g["v_line_count"] >= 8
    assert len(g["row_ys"]) >= 10
    assert g["table_bbox"] is not None
