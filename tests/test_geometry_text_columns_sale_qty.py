"""Regression: text-column Stock-And-Sales via Geometry V3.

Fixture: 0000701027_2026_08_ZA_11_210_07092026110044.png (SIDHA AGENCIES).
Printed first product (from source image OCR):
  Op.Qty=18, Sale Qty=5, Bal.Qty=13, Bal.Val=658.45
Stockist on document header: SIDHA AGENCIES
(Op.Stk physical map must NOT apply; LMS/Op.Stk fixture unchanged.)
"""

from __future__ import annotations

from pathlib import Path

import pytest

FIXTURE_0701027 = Path(
    "/var/www/html/splitextract/"
    "0000701027_2026_08_ZA_11_210_07092026110044.png"
)
FIXTURE_0725384 = Path(
    "/var/www/html/splitextract/"
    "0000725384_2026_08_ZA_11_210_07092026105840.png"
)

EXPECTED_FIRST_SALES = 5.0
EXPECTED_FIRST_BALANCE = 13.0  # printed Bal.Qty
EXPECTED_FIRST_BAL_VAL = 658.45  # printed Bal.Val
EXPECTED_STOCKIST = "SIDHA AGENCIES"


@pytest.mark.skipif(not FIXTURE_0701027.is_file(), reason="fixture PNG missing")
def test_text_columns_detects_sale_qty_column():
    import cv2
    from services.stock_geometry_table_v3 import (
        classify_text_column_stock_format,
        detect_text_column_geometry,
    )
    from services.stock_header_resolver import resolve_columns

    bgr = cv2.imread(str(FIXTURE_0701027))
    assert bgr is not None
    geom = detect_text_column_geometry(bgr)
    assert geom is not None
    assert geom.get("detection_mode") == "text_columns"
    headers = geom.get("preparsed_header_cells") or []
    assert (
        classify_text_column_stock_format(headers)
        == "STOCK_SALES_BALANCE_TEXT_COLUMNS"
    )
    resolved = resolve_columns(
        [
            {
                "text": h["text"],
                "col_index": h["col_index"],
                "x_center": h["x_center"],
            }
            for h in headers
        ]
    )
    by_canon = {
        c.get("canonical"): c for c in (resolved.get("columns") or [])
    }
    assert "sales_qty" in by_canon
    assert int(by_canon["sales_qty"]["col_index"]) == 6
    assert "closing_qty" in by_canon
    assert int(by_canon["closing_qty"]["col_index"]) == 13
    # Bal.Val must not be mapped as closing_qty
    assert by_canon.get("closing_value") is not None
    assert int(by_canon["closing_value"]["col_index"]) == 14


@pytest.mark.skipif(not FIXTURE_0701027.is_file(), reason="fixture PNG missing")
def test_first_product_balance_and_sale_via_geometry_table(monkeypatch):
    monkeypatch.setenv("STOCK_GEOMETRY_CELL_OCR_ENABLED", "true")
    from services.stock_geometry_table_v3 import run_geometry_table_v3

    report = run_geometry_table_v3(
        FIXTURE_0701027, enable_gemini=False, max_gemini_cells=0
    )
    assert not report.get("error"), report.get("error")
    assert (report.get("geometry") or {}).get("detection_mode") == "text_columns"
    assert (report.get("geometry") or {}).get("text_column_format") == (
        "STOCK_SALES_BALANCE_TEXT_COLUMNS"
    )
    assert not str((report.get("geometry") or {}).get("header_source") or "").startswith(
        "physical"
    )

    prods = [r for r in (report.get("rows") or []) if r.get("row_class") == "PRODUCT"]
    assert len(prods) >= 3
    first = prods[0]
    sel = first.get("selected") or {}
    phys = first.get("physical_cells") or {}
    name = str(sel.get("product_name") or "").upper()
    assert "RESOL" in name and "NASAL" in name

    assert sel.get("sales_qty") == EXPECTED_FIRST_SALES
    assert phys.get("6") == EXPECTED_FIRST_SALES
    assert sel.get("closing_qty") == EXPECTED_FIRST_BALANCE
    assert phys.get("13") == EXPECTED_FIRST_BALANCE
    assert sel.get("closing_value") == EXPECTED_FIRST_BAL_VAL
    assert phys.get("14") == EXPECTED_FIRST_BAL_VAL

    # Cross-column: Balance Qty must not equal Sale Qty or Bal.Val
    assert phys.get("13") != phys.get("6")
    assert phys.get("13") != phys.get("14")
    # Cross-row: second product Sale Qty stays 0
    second = prods[1]
    assert (second.get("selected") or {}).get("sales_qty") == 0.0
    assert (second.get("physical_cells") or {}).get("6") == 0.0
    assert (second.get("selected") or {}).get("closing_qty") != EXPECTED_FIRST_BALANCE or (
        second.get("selected") or {}
    ).get("product_name", "").upper().find("SY") >= 0

    assert sel.get("lms") == 10.0
    assert sel.get("opening_qty") == 18.0
    assert not (
        float(sel.get("opening_qty") or -1) == 10.0
        and float(sel.get("receipts_qty") or -1) == 18.0
    )


@pytest.mark.skipif(not FIXTURE_0701027.is_file(), reason="fixture PNG missing")
def test_text_column_format_preserves_stockist_name(monkeypatch):
    monkeypatch.setenv("STOCK_GEOMETRY_CELL_OCR_ENABLED", "true")
    monkeypatch.setenv("STOCK_VISION_TABLE", "true")
    monkeypatch.setenv("STOCK_VISION_TABLE_TYPES", "image")

    from services.sales_statement_extractor import extract_sales_statement

    result = extract_sales_statement(
        FIXTURE_0701027.read_bytes(), FIXTURE_0701027.name
    )
    extra = (result.get("totals") or {}).get("extra") or {}
    assert extra.get("extraction_method") == "geometry_cell_ocr"
    assert extra.get("extraction_engine") == "geometry_v3"
    assert extra.get("vision_called") is False
    assert extra.get("geometry_called") is True
    assert result.get("stockist_name") == EXPECTED_STOCKIST

    items = result.get("line_items") or []
    assert items
    # Metadata banners must not appear as products
    for it in items:
        n = str(it.get("product_name") or "").upper()
        assert "KANNUR" not in n
        assert not n.startswith("COMPANY")
        assert n != "TOTAL"

    first = items[0]
    assert float(first.get("sales_qty") or 0) == EXPECTED_FIRST_SALES
    assert float(first.get("closing_qty") or 0) == EXPECTED_FIRST_BALANCE
    assert float(first.get("closing_value") or 0) == EXPECTED_FIRST_BAL_VAL
    assert (first.get("extra") or {}).get("closing_value_source") == "source_document"
    phys = (first.get("extra") or {}).get("physical_cells") or {}
    assert float(phys.get("6")) == EXPECTED_FIRST_SALES
    assert float(phys.get("13")) == EXPECTED_FIRST_BALANCE
    # Balance Qty must never be copied into closing_value
    assert float(phys.get("14")) != float(phys.get("13"))


@pytest.mark.skipif(not FIXTURE_0725384.is_file(), reason="regression PNG missing")
def test_opstk_regression_fixture_still_geometry(monkeypatch):
    """Previously successful Op.Stk|LMS statement must remain on Geometry V3."""
    monkeypatch.setenv("STOCK_GEOMETRY_CELL_OCR_ENABLED", "true")
    monkeypatch.setenv("STOCK_VISION_TABLE", "true")
    monkeypatch.setenv("STOCK_VISION_TABLE_TYPES", "image")

    from services.sales_statement_extractor import extract_sales_statement
    from services.stock_geometry_table_v3 import match_focus_product

    result = extract_sales_statement(
        FIXTURE_0725384.read_bytes(), FIXTURE_0725384.name
    )
    extra = (result.get("totals") or {}).get("extra") or {}
    assert extra.get("extraction_method") == "geometry_cell_ocr"
    assert extra.get("vision_called") is False

    by = {}
    for item in result.get("line_items") or []:
        _st, matched = match_focus_product(
            str(item.get("product_name") or ""),
            str(item.get("packing") or ""),
        )
        if matched and matched not in by:
            by[matched] = item
    bonn = by.get("BONNISAN")
    if bonn is not None:
        assert float(bonn.get("opening_qty") or 0) == 14.0
        fs = (bonn.get("extra") or {}).get("field_source") or {}
        if fs.get("receipts_qty") == "missing":
            assert float(bonn.get("receipts_qty") or 0) == 0.0
        lms = (bonn.get("extra") or {}).get("lms")
        assert lms != 14.0
