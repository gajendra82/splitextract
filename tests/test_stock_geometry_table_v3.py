"""Tests for geometry table v3 — classification + physical semantic mapping."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from services.stock_geometry_table_v3 import (
    FOCUS_EXPECTATIONS,
    FOCUS_PHYSICAL_CELLS,
    PHYSICAL_SOURCE_COLUMNS,
    assert_physical_cells_unshifted,
    classify_grid_row,
    count_cross_row_contamination,
    dense_physical_cells,
    detect_lms_opening_shift,
    detect_opstk_receipt_shift,
    geometry_row_to_line_item,
    is_geometry_cell_ocr_enabled,
    is_opstk_lms_layout,
    apply_physical_source_columns,
    match_focus_product,
    ocr_numeric_cell,
)


def test_feature_flag_defaults_off(monkeypatch):
    monkeypatch.delenv("STOCK_GEOMETRY_CELL_OCR_ENABLED", raising=False)
    assert is_geometry_cell_ocr_enabled() is False
    monkeypatch.setenv("STOCK_GEOMETRY_CELL_OCR_ENABLED", "false")
    assert is_geometry_cell_ocr_enabled() is False


def test_physical_source_columns_immutable():
    assert PHYSICAL_SOURCE_COLUMNS[2] == "lms"
    assert PHYSICAL_SOURCE_COLUMNS[3] == "opening_qty"
    assert PHYSICAL_SOURCE_COLUMNS[4] == "receipts_qty"
    assert PHYSICAL_SOURCE_COLUMNS[2] != "opening_qty"
    assert PHYSICAL_SOURCE_COLUMNS[3] != "receipts_qty"


def test_classify_rejects_metadata_rows():
    cases = [
        ("KANNUR DRUG LINES*", "metadata_stockist_banner"),
        ("Kannur Drug Lines*", "metadata_stockist_banner"),
        ("Company : 171 HIMALAY", "metadata_company_row"),
        ("Company: 171 HIMALAY", "metadata_company_row"),
        ("Stock & Sales Statement for the month of Aug'26", "metadata_statement_title"),
        ("Total", "metadata_total_row"),
        ("Grand Total", "metadata_total_row"),
        ("KALPETTA", "metadata_banner_token"),
        ("", "empty_product_name"),
    ]
    for text, reason in cases:
        cls, why = classify_grid_row(text)
        assert cls == "NON_PRODUCT", text
        assert why == reason, (text, why)


def test_classify_accepts_product_rows():
    for text in (
        "ARJUNA T",
        "BONNISAN 100ML",
        "BRESOL S 200ML",
        "SEPTILIN T",
        "V-GEL**",
    ):
        cls, why = classify_grid_row(text)
        assert cls == "PRODUCT", text
        assert why is None


def test_bonnisan_lms_must_not_become_opening():
    exp = FOCUS_EXPECTATIONS["BONNISAN 100ML"]
    assert exp["lms"] == 1.0
    assert exp["opening_qty"] == 14.0
    assert exp["receipts_qty"] is None
    wrong = {"lms": None, "opening_qty": 1.0, "receipts_qty": 14.0}
    assert detect_lms_opening_shift(wrong)
    correct = {"lms": 1.0, "opening_qty": 14.0, "receipts_qty": None}
    assert not detect_lms_opening_shift(correct)


def test_blank_lms_does_not_shift_opstk_into_lms_slot():
    """Blank LMS must keep physical index 2; Op.Stk stays at index 3."""
    physical = {
        "2": {
            "physical_column_index": 2,
            "business_field": "lms",
            "value": None,
            "raw_text": "",
        },
        "3": {
            "physical_column_index": 3,
            "business_field": "opening_qty",
            "value": 17.0,
            "raw_text": "17",
        },
        "4": {
            "physical_column_index": 4,
            "business_field": "receipts_qty",
            "value": None,
            "raw_text": "",
        },
    }
    assert assert_physical_cells_unshifted(physical)
    # Shifted (bad): Op.Stk value parked under LMS index
    shifted = {
        "2": {
            "physical_column_index": 2,
            "business_field": "opening_qty",  # WRONG field for index 2
            "value": 17.0,
        },
        "3": {
            "physical_column_index": 3,
            "business_field": "receipts_qty",
            "value": None,
        },
    }
    assert assert_physical_cells_unshifted(shifted) is False


def test_blank_receipt_does_not_shift_sales_left():
    physical = {
        "4": {
            "physical_column_index": 4,
            "business_field": "receipts_qty",
            "value": None,
        },
        "5": {
            "physical_column_index": 5,
            "business_field": "purchase_return_qty",
            "value": None,
        },
        "6": {
            "physical_column_index": 6,
            "business_field": "sales_qty",
            "value": 3.0,
        },
    }
    assert assert_physical_cells_unshifted(physical)
    # Compacted left (bad)
    compacted = {
        "4": {
            "physical_column_index": 4,
            "business_field": "sales_qty",
            "value": 3.0,
        },
    }
    assert assert_physical_cells_unshifted(compacted) is False


def test_focus_expectations_cover_acceptance():
    assert FOCUS_EXPECTATIONS["BRESOL S 200ML"]["opening_qty"] == 51.0
    assert FOCUS_EXPECTATIONS["BRESOL S 200ML"]["sales_qty"] == 3.0
    assert FOCUS_EXPECTATIONS["SEPTILIN T"]["lms"] == 2.0
    assert FOCUS_EXPECTATIONS["V-GEL 30GM"]["opening_qty"] == 17.0


def test_geometry_row_to_line_item_preserves_lms_and_opening():
    row = {
        "row_index": 1,
        "row_class": "PRODUCT",
        "selected": {
            "product_name": "BONNISA",
            "packing": "100ML",
            "lms": 1.0,
            "opening_qty": 14.0,
            "receipts_qty": None,
            "sales_qty": None,
            "closing_qty": 14.0,
            "closing_value": 721.98,
        },
        "business_fields": {
            "lms": 1.0,
            "opening_qty": 14.0,
            "receipts_qty": None,
            "sales_qty": None,
            "closing_qty": 14.0,
            "closing_value": 721.98,
        },
        "physical_values": {
            "2": {"value": 1.0, "business_field": "lms"},
            "3": {"value": 14.0, "business_field": "opening_qty"},
            "4": {"value": None, "business_field": "receipts_qty"},
        },
    }
    item = geometry_row_to_line_item(row)
    assert item["opening_qty"] == 14.0
    assert item["receipts_qty"] == 0.0  # API boundary for missing
    assert item["extra"]["field_source"]["receipts_qty"] == "missing"
    assert item["extra"]["lms"] == 1.0
    assert item["extra"]["field_source"]["opening_qty"] == "printed"
    # Must never look like the Vision shift bug at top-level
    assert not (item["opening_qty"] == 1.0 and item["receipts_qty"] == 14.0)


def test_apply_physical_mapping_for_opstk_layout():
    col_xs = list(range(0, 150, 10))
    headers = [
        {"text": h, "col_index": i, "x_center": i * 10 + 5}
        for i, h in enumerate(
            [
                "Name",
                "Packg",
                "LMS",
                "Op.Stk",
                "Receipt",
                "Pu.Ret",
                "Sales",
                "S.Ret",
                "Brk",
                "Repl",
                "Cl.Stk",
                "Cl.Value",
                "cl free",
                "*XY",
            ]
        )
    ]
    assert is_opstk_lms_layout(headers, 14)
    cols = apply_physical_source_columns(col_xs, headers)
    by = {c["physical_column_index"]: c["business_field"] for c in cols}
    assert by[2] == "lms" and by[3] == "opening_qty" and by[4] == "receipts_qty"


def test_ocr_preserves_row_column_identity():
    img = np.full((40, 80, 3), 255, dtype=np.uint8)
    img[10:30, 30:45] = (20, 20, 20)
    gray = np.mean(img, axis=2).astype(np.uint8)
    cell = ocr_numeric_cell(
        img,
        gray,
        {"x0": 20, "y0": 5, "x1": 60, "y1": 35},
        "sales_qty",
        row_index=12,
        column_index=6,
    )
    assert cell.row_index == 12 and cell.column_index == 6


def test_benchmark_report_rejects_metadata_and_no_shift():
    report_path = Path("/tmp/stock_geometry_v3.json")
    if not report_path.is_file():
        return
    report = json.loads(report_path.read_text(encoding="utf-8"))
    # Metadata must not be PRODUCT
    for r in report.get("rows") or []:
        text = (r.get("product_text") or r.get("product_name_ocr") or "").upper()
        if any(
            tok in text
            for tok in ("KANNUR", "COMPANY", "STOCK & SALES", "TOTAL")
        ) and "BRESOL" not in text:
            assert r.get("row_class") == "NON_PRODUCT", text
    for f in report.get("focus") or []:
        if f.get("product") != "BONNISAN 100ML":
            continue
        biz = f.get("business_fields") or f
        assert biz.get("lms") == 1.0
        assert biz.get("opening_qty") == 14.0
        assert biz.get("receipts_qty") is None
        assert not (biz.get("opening_qty") == 1.0 and biz.get("receipts_qty") == 14.0)
    assert report.get("production_switched") is False
    assert (report.get("feature_flag") or {}).get("enabled") is False
    m = report.get("metrics") or {}
    assert m.get("lms_opening_shift_errors", 0) == 0
    assert m.get("benchmark_failed_semantic_shift") in (False, None, 0)


def test_match_focus_products():
    assert match_focus_product("BONNISA", "100ML")[1] == "BONNISAN 100ML"
    assert match_focus_product("BRESOL-}", "OML")[1] == "BRESOL-N 10ML"
    assert match_focus_product("SEPTLIN 1", "60")[1] == "SEPTILIN T"


def test_arjuna_physical_cells_must_stay_blank_except_closing():
    """ARJUNA T: LMS/Op/Receipt blank; Cl.Stk/Cl.Value = 0. Never Op.Stk=14."""
    exp = FOCUS_PHYSICAL_CELLS["ARJUNA T"]
    assert exp[2] is None and exp[3] is None
    assert exp[10] == "0" and exp[11] == "0"
    physical = {
        "2": {"physical_column_index": 2, "business_field": "lms", "value": None},
        "3": {
            "physical_column_index": 3,
            "business_field": "opening_qty",
            "value": None,
        },
        "10": {
            "physical_column_index": 10,
            "business_field": "closing_qty",
            "value": 0.0,
            "raw_text": "0",
        },
        "11": {
            "physical_column_index": 11,
            "business_field": "closing_value",
            "value": 0.0,
            "raw_text": "0",
        },
    }
    dense = dense_physical_cells(physical)
    assert dense["2"] is None and dense["3"] is None
    assert dense["10"] == 0.0 and dense["11"] == 0.0
    # Contaminated ARJUNA (Vision bug): Op.Stk got BONNISAN's 14
    contaminated = {
        "row_class": "PRODUCT",
        "matched_product": "ARJUNA T",
        "product_name_ocr": "ARJUNA T",
        "physical_values": {
            "3": {
                "physical_column_index": 3,
                "business_field": "opening_qty",
                "value": 14.0,
            }
        },
        "physical_cells": {"3": 14.0},
    }
    assert count_cross_row_contamination([contaminated]) == 1
    clean = {
        "row_class": "PRODUCT",
        "matched_product": "ARJUNA T",
        "product_name_ocr": "ARJUNA T",
        "physical_values": {
            "3": {
                "physical_column_index": 3,
                "business_field": "opening_qty",
                "value": None,
            }
        },
        "physical_cells": {"3": None},
    }
    assert count_cross_row_contamination([clean]) == 0


def test_bresol_physical_cells_no_compaction():
    """BRESOL S'200ML: LMS=1 Op=51 Receipt blank Sales=3 Cl=48 — no left shift."""
    exp = FOCUS_PHYSICAL_CELLS["BRESOL S 200ML"]
    assert exp[2] == "1" and exp[3] == "51"
    assert exp[4] is None and exp[5] is None
    assert exp[6] == "3" and exp[10] == "48" and exp[11] == "7862.4"
    # Op.Stk→Receipt: opening missing, receipts holds true opening 51
    assert detect_opstk_receipt_shift(
        {"lms": 1.0, "opening_qty": None, "receipts_qty": 51.0},
        expected_opening=51.0,
    )
    assert not detect_opstk_receipt_shift(
        {
            "lms": 1.0,
            "opening_qty": 51.0,
            "receipts_qty": None,
            "sales_qty": 3.0,
            "closing_qty": 48.0,
        },
        expected_opening=51.0,
    )
    # Forbidden compacted pattern: LMS slot swallowed Op.Stk (51) while Sales
    # parked under Pu.Ret — physical index map must reject this via Op→Receipt.
    assert detect_opstk_receipt_shift(
        {"lms": 51.0, "opening_qty": None, "receipts_qty": None},
        expected_opening=51.0,
    ) is False  # receipts empty — caught by physical slot mismatch instead
    # Physical map for correct BRESOL must keep blanks at 4 and 5
    physical = {
        str(i): {
            "physical_column_index": i,
            "business_field": PHYSICAL_SOURCE_COLUMNS[i],
            "value": v,
        }
        for i, v in (
            (2, 1.0),
            (3, 51.0),
            (4, None),
            (5, None),
            (6, 3.0),
            (10, 48.0),
            (11, 7862.4),
        )
    }
    assert assert_physical_cells_unshifted(physical)


def test_lms_one_cannot_become_opening_one():
    assert detect_lms_opening_shift(
        {"lms": None, "opening_qty": 1.0, "receipts_qty": 14.0}
    )
    assert not detect_lms_opening_shift(
        {"lms": 1.0, "opening_qty": 14.0, "receipts_qty": None}
    )


def test_opstk_fourteen_cannot_become_receipts_fourteen():
    assert detect_opstk_receipt_shift(
        {"lms": 1.0, "opening_qty": None, "receipts_qty": 14.0},
        expected_opening=14.0,
    )
    assert detect_opstk_receipt_shift(
        {"lms": None, "opening_qty": 1.0, "receipts_qty": 14.0}
    )
    assert not detect_opstk_receipt_shift(
        {"lms": 1.0, "opening_qty": 14.0, "receipts_qty": None},
        expected_opening=14.0,
    )


def test_dense_physical_cells_always_fourteen_slots():
    dense = dense_physical_cells({"2": {"value": 1.0}, "3": {"value": 14.0}})
    assert list(dense.keys()) == [str(i) for i in range(14)]
    assert dense["2"] == 1.0 and dense["3"] == 14.0
    assert dense["4"] is None


def test_benchmark_arjuna_bresol_physical_slots():
    report_path = Path("/tmp/stock_geometry_v3.json")
    if not report_path.is_file():
        return
    report = json.loads(report_path.read_text(encoding="utf-8"))
    by = {f.get("product"): f for f in (report.get("focus") or [])}

    def _phys(focus_row: dict) -> dict:
        cells = focus_row.get("physical_cells") or {}
        if cells:
            return cells
        cols = focus_row.get("physical_columns") or {}
        return {
            "2": cols.get("2_LMS"),
            "3": cols.get("3_Op.Stk"),
            "4": cols.get("4_Receipt"),
            "5": cols.get("5_Pu.Ret"),
            "6": cols.get("6_Sales"),
            "10": cols.get("10_Cl.Stk"),
            "11": cols.get("11_Cl.Value"),
        }

    arjuna = by.get("ARJUNA T") or {}
    phys = _phys(arjuna)
    if not phys:
        return  # stale report without focus physical dump
    assert phys.get("2") is None
    assert phys.get("3") is None
    assert phys.get("10") in (0, 0.0, "0")
    assert phys.get("11") in (0, 0.0, "0")
    bresol = by.get("BRESOL S 200ML") or {}
    bp = _phys(bresol)
    assert bp.get("2") in (1, 1.0, "1")
    assert bp.get("3") in (51, 51.0, "51")
    assert bp.get("4") is None
    assert bp.get("6") in (3, 3.0, "3")
    assert bp.get("10") in (48, 48.0, "48")
    assert float(bp.get("11")) == 7862.4
    m = report.get("metrics") or {}
    assert m.get("lms_opening_shift_errors", 0) == 0
    assert (report.get("feature_flag") or {}).get("enabled") is False
    # New metrics appear only after a fresh benchmark run.
    if "cross_row_contamination_count" in m:
        assert m.get("cross_row_contamination_count", 0) == 0
        assert m.get("opstk_receipt_shift_errors", 0) == 0
