"""Tests for geometry hybrid v2 helpers."""

from __future__ import annotations

from services.stock_geometry_hybrid_v2 import (
    POSITIONAL_10,
    _cluster,
    _normalize_numeric,
    apply_gemini_results,
)
from services.stock_header_resolver import resolve_columns


def test_v2_normalize_no_invention():
    assert _normalize_numeric("p3") == (None, False, True)
    assert _normalize_numeric("pq") == (None, False, True)
    assert _normalize_numeric("2") == (2.0, False, False)
    assert _normalize_numeric("26") == (26.0, False, False)
    assert _normalize_numeric("") == (None, True, False)


def test_v2_positional_headers_purc_ret():
    cells = [
        {"text": POSITIONAL_10[i], "col_index": i, "x_center": float(i)}
        for i in range(10)
    ]
    cols = resolve_columns(cells)["columns"]
    by = {c["col_index"]: c["canonical"] for c in cols}
    assert by[4] == "purchase_qty"
    assert by[8] == "purchase_return_qty"
    assert by[5] == "sales_return_qty"
    assert by[9] == "closing_qty"


def test_v2_cluster():
    xs = _cluster([10, 11, 12, 40, 41], gap=2)
    assert len(xs) == 2


def test_v2_apply_gemini_index_ids():
    """When model returns only bare indices (no c-prefix), map by order."""
    rows = [
        {
            "row_index": 0,
            "selected": {"purchase_qty": None, "sales_qty": None},
            "cells": {
                "purchase_qty": {"ocr_uncertain": True, "blank": False},
                "sales_qty": {"blank": True, "ocr_uncertain": False},
            },
        }
    ]
    gemini = {
        "requested": [
            {"id": "c0", "row_index": 0, "column": "purchase_qty"},
            {"id": "c1", "row_index": 0, "column": "sales_qty"},
        ],
        "results": [
            {"id": "0", "value": 60, "raw_text": "60"},
            {"id": "1", "value": 26, "raw_text": "26"},
        ],
    }
    n = apply_gemini_results(rows, gemini)
    assert n == 2
    assert rows[0]["selected"]["purchase_qty"] == 60.0
    assert rows[0]["selected"]["sales_qty"] == 26.0


def test_v2_apply_gemini_exact_c_ids():
    rows = [
        {
            "row_index": 0,
            "selected": {"purchase_qty": None, "sales_qty": None},
            "cells": {
                "purchase_qty": {"ocr_uncertain": True, "blank": False},
                "sales_qty": {"blank": True, "ocr_uncertain": False},
            },
        }
    ]
    gemini = {
        "requested": [
            {"id": "c0", "row_index": 0, "column": "purchase_qty"},
            {"id": "c1", "row_index": 0, "column": "sales_qty"},
        ],
        "results": [
            {"id": "c0", "value": 60, "raw_text": "60"},
            {"id": "c1", "value": 26, "raw_text": "26"},
        ],
    }
    n = apply_gemini_results(rows, gemini)
    assert n == 2
    assert rows[0]["selected"]["purchase_qty"] == 60.0
    assert rows[0]["selected"]["sales_qty"] == 26.0


def test_v2_apply_gemini_rejects_positional_when_c_ids_partial():
    rows = [
        {
            "row_index": 0,
            "selected": {"purchase_qty": None},
            "cells": {"purchase_qty": {"ocr_uncertain": True, "blank": False}},
        }
    ]
    gemini = {
        "requested": [{"id": "c0", "row_index": 0, "column": "purchase_qty"}],
        "results": [{"id": "c9", "value": 999, "raw_text": "999"}],
    }
    assert apply_gemini_results(rows, gemini) == 0
    assert rows[0]["selected"]["purchase_qty"] is None
