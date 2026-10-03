"""Production-path integration: extract_sales_statement + geometry flag.

Uses the SAME entry point as /extract-sales-statement (extract_sales_statement),
not the benchmark CLI. STOCK_GEOMETRY_CELL_OCR_ENABLED is enabled only inside
these tests; .env production flag remains false.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

FIXTURE = Path(
    "/var/www/html/splitextract/"
    "0000725384_2026_08_ZA_11_210_07092026105840.png"
)


def _effective_qty(item: dict, field: str):
    """Missing printed cells → None (field_source), even if top-level is 0.0."""
    extra = item.get("extra") or {}
    if field == "lms":
        fs = (extra.get("field_source") or {}).get("lms")
        val = extra.get("lms")
        return None if fs == "missing" or val is None else float(val)
    fs = (extra.get("field_source") or {}).get(field)
    val = item.get(field)
    if fs == "missing":
        return None
    if val is None:
        return None
    return float(val)


def _index_by_focus(line_items: list) -> dict:
    from services.stock_geometry_table_v3 import match_focus_product

    by = {}
    for item in line_items or []:
        _status, matched = match_focus_product(
            str(item.get("product_name") or ""),
            str(item.get("packing") or ""),
        )
        if matched and matched not in by:
            by[matched] = item
    return by


def test_flag_off_does_not_run_geometry_ocr(monkeypatch):
    monkeypatch.setenv("STOCK_GEOMETRY_CELL_OCR_ENABLED", "false")
    from services.stock_geometry_table_v3 import (
        is_geometry_cell_ocr_enabled,
        run_geometry_cell_ocr_path,
    )

    assert is_geometry_cell_ocr_enabled() is False
    out = run_geometry_cell_ocr_path(b"\x89PNG\r\n", "image", {"filename": "x.png"})
    assert out["status"] == "fallback"
    assert out["reason"] == "GEOMETRY_CELL_OCR_DISABLED"


def test_geometry_ok_skips_vision_in_parse_image(monkeypatch):
    """_parse_image (image branch of extract_sales_statement) must not call Vision
    after geometry returns status=ok.
    """
    monkeypatch.setenv("STOCK_GEOMETRY_CELL_OCR_ENABLED", "true")
    monkeypatch.setenv("STOCK_VISION_TABLE", "true")
    monkeypatch.setenv("STOCK_VISION_TABLE_TYPES", "image")

    fake_result = {
        "source_file": "wire.png",
        "source_format": "png",
        "line_items": [
            {
                "product_name": "BONNISA",
                "packing": "100ML",
                "opening_qty": 14.0,
                "receipts_qty": 0.0,
                "sales_qty": 0.0,
                "sales_value": 0.0,
                "closing_qty": 14.0,
                "closing_value": 721.98,
                "extra": {
                    "lms": 1.0,
                    "field_source": {
                        "lms": "printed",
                        "opening_qty": "printed",
                        "receipts_qty": "missing",
                        "sales_qty": "missing",
                        "closing_qty": "printed",
                        "closing_value": "printed",
                    },
                    "physical_cells": {
                        "2": 1.0,
                        "3": 14.0,
                        "4": None,
                        "10": 14.0,
                        "11": 721.98,
                    },
                    "extraction_method": "geometry_cell_ocr",
                },
            }
        ],
        "totals": {
            "extra": {
                "extraction_method": "geometry_cell_ocr",
                "vision_table_final": True,
                "geometry_cell_ocr_final": True,
            }
        },
    }

    def _geo_ok(*_a, **_k):
        return {
            "status": "ok",
            "result": fake_result,
            "gemini_calls": 0,
            "gemini_budget": 2,
        }

    def _vision_boom(*_a, **_k):
        raise AssertionError("STOCK_VISION_TABLE must not run after geometry ok")

    from services.sales_statement_extractor import _parse_image

    with patch(
        "services.stock_geometry_table_v3.is_geometry_cell_ocr_enabled",
        return_value=True,
    ), patch(
        "services.stock_geometry_table_v3.run_geometry_cell_ocr_path",
        side_effect=_geo_ok,
    ), patch(
        "services.stock_vision_table.run_vision_table_path",
        side_effect=_vision_boom,
    ), patch(
        "services.stock_vision_table.vision_table_active_for",
        return_value=True,
    ):
        result = _parse_image(b"\x89PNG\r\n" + b"\x00" * 32, "wire.png", ".png")

    extra = (result.get("totals") or {}).get("extra") or {}
    assert extra.get("extraction_method") == "geometry_cell_ocr"
    assert extra.get("geometry_skipped_vision_table") is True
    bonn = (result.get("line_items") or [])[0]
    assert _effective_qty(bonn, "lms") == 1.0
    assert _effective_qty(bonn, "opening_qty") == 14.0
    assert _effective_qty(bonn, "receipts_qty") is None
    assert not (
        float(bonn.get("opening_qty") or 0) == 1.0
        and float(bonn.get("receipts_qty") or 0) == 14.0
    )


def test_arjuna_must_not_receive_bonnisan_opstk_fourteen():
    """Regression: BONNISAN Op.Stk=14 must never contaminate ARJUNA opening.

    Mirrors the production Vision contamination seen in Secondary Sales UI.
    """
    from services.stock_geometry_table_v3 import (
        count_cross_row_contamination,
        geometry_row_to_line_item,
    )

    arjuna_row = {
        "row_class": "PRODUCT",
        "matched_product": "ARJUNA T",
        "product_name_ocr": "ARJUNA T",
        "selected": {
            "product_name": "ARJUNA T",
            "packing": "60",
            "lms": None,
            "opening_qty": None,
            "receipts_qty": None,
            "sales_qty": None,
            "closing_qty": 0.0,
            "closing_value": 0.0,
        },
        "business_fields": {
            "lms": None,
            "opening_qty": None,
            "receipts_qty": None,
            "sales_qty": None,
            "closing_qty": 0.0,
            "closing_value": 0.0,
        },
        "physical_cells": {
            "2": None,
            "3": None,
            "4": None,
            "10": 0.0,
            "11": 0.0,
        },
        "physical_values": {
            "3": {
                "physical_column_index": 3,
                "business_field": "opening_qty",
                "value": None,
            }
        },
    }
    bonn_row = {
        "row_class": "PRODUCT",
        "matched_product": "BONNISAN 100ML",
        "selected": {
            "product_name": "BONNISAN 100ML",
            "lms": 1.0,
            "opening_qty": 14.0,
            "receipts_qty": None,
            "closing_qty": 14.0,
            "closing_value": 721.98,
        },
        "business_fields": {
            "lms": 1.0,
            "opening_qty": 14.0,
            "receipts_qty": None,
            "closing_qty": 14.0,
            "closing_value": 721.98,
        },
        "physical_cells": {"2": 1.0, "3": 14.0, "4": None, "10": 14.0, "11": 721.98},
    }
    bresol_row = {
        "row_class": "PRODUCT",
        "matched_product": "BRESOL S 200ML",
        "selected": {
            "product_name": "BRESOL S 200ML",
            "lms": 1.0,
            "opening_qty": 51.0,
            "receipts_qty": None,
            "sales_qty": 3.0,
            "closing_qty": 48.0,
            "closing_value": 7862.4,
        },
        "business_fields": {
            "lms": 1.0,
            "opening_qty": 51.0,
            "receipts_qty": None,
            "sales_qty": 3.0,
            "closing_qty": 48.0,
            "closing_value": 7862.4,
        },
        "physical_cells": {
            "2": 1.0,
            "3": 51.0,
            "4": None,
            "5": None,
            "6": 3.0,
            "10": 48.0,
            "11": 7862.4,
        },
    }

    arjuna = geometry_row_to_line_item(arjuna_row)
    bonn = geometry_row_to_line_item(bonn_row)
    bresol = geometry_row_to_line_item(bresol_row)

    assert _effective_qty(arjuna, "opening_qty") is None
    assert _effective_qty(arjuna, "opening_qty") != 14.0
    assert _effective_qty(arjuna, "closing_qty") == 0.0
    assert _effective_qty(bonn, "lms") == 1.0
    assert _effective_qty(bonn, "opening_qty") == 14.0
    assert _effective_qty(bresol, "opening_qty") == 51.0
    assert _effective_qty(bresol, "sales_qty") == 3.0
    assert _effective_qty(bresol, "closing_qty") == 48.0
    assert count_cross_row_contamination([arjuna_row, bonn_row]) == 0

    # Contaminated Vision-like ARJUNA must be detected
    contaminated = {
        "row_class": "PRODUCT",
        "matched_product": "ARJUNA T",
        "physical_cells": {"3": 14.0},
        "physical_values": {
            "3": {
                "physical_column_index": 3,
                "business_field": "opening_qty",
                "value": 14.0,
            }
        },
    }
    assert count_cross_row_contamination([contaminated]) == 1


def test_physical_index_map_never_compacts_blanks():
    """Dense physical_cells → business fields by index only."""
    from services.stock_geometry_table_v3 import (
        PHYSICAL_SOURCE_COLUMNS,
        geometry_row_to_line_item,
    )

    assert PHYSICAL_SOURCE_COLUMNS[2] == "lms"
    assert PHYSICAL_SOURCE_COLUMNS[3] == "opening_qty"
    assert PHYSICAL_SOURCE_COLUMNS[4] == "receipts_qty"
    assert PHYSICAL_SOURCE_COLUMNS[6] == "sales_qty"

    row = {
        "row_index": 0,
        "row_class": "PRODUCT",
        "selected": {
            "product_name": "BRESOL S",
            "packing": "200ML",
            "lms": None,
            "opening_qty": 51.0,
            "receipts_qty": None,
            "purchase_return_qty": None,
            "sales_qty": 3.0,
            "closing_qty": 48.0,
            "closing_value": 7862.4,
        },
        "business_fields": {
            "lms": None,
            "opening_qty": 51.0,
            "receipts_qty": None,
            "purchase_return_qty": None,
            "sales_qty": 3.0,
            "closing_qty": 48.0,
            "closing_value": 7862.4,
        },
        "physical_cells": {
            "2": None,
            "3": 51.0,
            "4": None,
            "5": None,
            "6": 3.0,
            "10": 48.0,
            "11": 7862.4,
        },
    }
    item = geometry_row_to_line_item(row)
    assert _effective_qty(item, "lms") is None
    assert _effective_qty(item, "opening_qty") == 51.0
    assert _effective_qty(item, "receipts_qty") is None
    assert _effective_qty(item, "sales_qty") == 3.0
    assert _effective_qty(item, "closing_qty") == 48.0
    # Forbidden compact: opening null + receipts=51
    assert not (
        _effective_qty(item, "opening_qty") is None
        and _effective_qty(item, "receipts_qty") == 51.0
    )


@pytest.mark.slow
def test_extract_sales_statement_geometry_integration_png(monkeypatch):
    """Full production path on the Op.Stk|LMS screenshot with geometry ON."""
    if not FIXTURE.is_file():
        pytest.skip(f"fixture missing: {FIXTURE}")

    monkeypatch.setenv("STOCK_GEOMETRY_CELL_OCR_ENABLED", "true")
    monkeypatch.setenv("STOCK_VISION_TABLE", "true")
    monkeypatch.setenv("STOCK_VISION_TABLE_TYPES", "image")

    vision_calls = {"n": 0}

    def _count_vision(*_a, **_k):
        vision_calls["n"] += 1
        return {
            "status": "fallback",
            "reason": "should_not_be_reached",
            "result": None,
            "gemini_calls": 0,
            "gemini_budget": 2,
        }

    from services.sales_statement_extractor import extract_sales_statement

    data = FIXTURE.read_bytes()
    with patch(
        "services.stock_vision_table.run_vision_table_path",
        side_effect=_count_vision,
    ):
        result = extract_sales_statement(
            data, "0000725384_2026_08_ZA_11_210_07092026105840.png"
        )

    extra = (result.get("totals") or {}).get("extra") or {}
    assert extra.get("extraction_method") == "geometry_cell_ocr", extra
    assert extra.get("geometry_cell_ocr_final") is True
    assert vision_calls["n"] == 0, "Vision must not run after geometry success"

    items = result.get("line_items") or []
    assert len(items) >= 50

    names_upper = [str(i.get("product_name") or "").upper() for i in items]
    for banned in (
        "KANNUR DRUG LINES",
        "COMPANY :",
        "COMPANY:",
        "STOCK & SALES STATEMENT",
        "STOCK & STATEMENT",
    ):
        assert not any(banned in n for n in names_upper), banned
    assert not any(n.strip() == "TOTAL" for n in names_upper)

    by = _index_by_focus(items)
    for key in (
        "ARJUNA T",
        "BONNISAN 100ML",
        "BRESOL S 200ML",
        "BRESOL-N 10ML",
        "SEPTILIN T",
        "V-GEL 30GM",
    ):
        assert key in by, f"missing focus product {key}; have={sorted(by)}"

    arjuna = by["ARJUNA T"]
    assert _effective_qty(arjuna, "opening_qty") is None
    assert _effective_qty(arjuna, "receipts_qty") is None
    assert _effective_qty(arjuna, "closing_qty") == 0.0
    assert _effective_qty(arjuna, "closing_value") == 0.0
    assert _effective_qty(arjuna, "opening_qty") != 14.0

    bonn = by["BONNISAN 100ML"]
    assert _effective_qty(bonn, "lms") == 1.0
    assert _effective_qty(bonn, "opening_qty") == 14.0
    assert _effective_qty(bonn, "receipts_qty") is None
    assert _effective_qty(bonn, "closing_qty") == 14.0
    assert abs(_effective_qty(bonn, "closing_value") - 721.98) < 0.06
    assert not (
        _effective_qty(bonn, "opening_qty") == 1.0
        and _effective_qty(bonn, "receipts_qty") == 14.0
    )

    bresol = by["BRESOL S 200ML"]
    assert _effective_qty(bresol, "lms") == 1.0
    assert _effective_qty(bresol, "opening_qty") == 51.0
    assert _effective_qty(bresol, "receipts_qty") is None
    assert _effective_qty(bresol, "sales_qty") == 3.0
    assert _effective_qty(bresol, "closing_qty") == 48.0
    assert abs(_effective_qty(bresol, "closing_value") - 7862.4) < 0.06
    assert _effective_qty(arjuna, "opening_qty") != 51.0
    assert _effective_qty(arjuna, "sales_qty") != 3.0
    assert _effective_qty(arjuna, "closing_qty") != 48.0

    bresol_n = by["BRESOL-N 10ML"]
    assert _effective_qty(bresol_n, "lms") == 4.0
    assert _effective_qty(bresol_n, "opening_qty") == 18.0
    assert _effective_qty(bresol_n, "closing_qty") == 18.0
    assert abs(_effective_qty(bresol_n, "closing_value") - 964.44) < 0.06

    sept = by["SEPTILIN T"]
    assert _effective_qty(sept, "lms") == 2.0
    assert _effective_qty(sept, "opening_qty") == 16.0
    assert _effective_qty(sept, "sales_qty") == 13.0
    assert _effective_qty(sept, "closing_qty") == 3.0
    assert abs(_effective_qty(sept, "closing_value") - 587.4) < 0.06

    vgel = by["V-GEL 30GM"]
    assert _effective_qty(vgel, "opening_qty") == 17.0
    assert _effective_qty(vgel, "sales_qty") == 3.0
    assert _effective_qty(vgel, "closing_qty") == 14.0
    assert abs(_effective_qty(vgel, "closing_value") - 1400.0) < 0.06

    phys = (bonn.get("extra") or {}).get("physical_cells") or {}
    assert phys.get("2") == 1.0
    assert phys.get("3") == 14.0
    assert phys.get("4") is None

    assert os.environ.get("STOCK_GEOMETRY_CELL_OCR_ENABLED") == "true"
