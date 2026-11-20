"""Geometry product-row gate: reject OCR garbage / headers / banners, keep SKUs.

Validates classify_grid_row + hybrid_10col acceptance path without hardcoding
stockist or product names from a single portal screenshot.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from services.stock_geometry_table_v3 import (
    classify_grid_row,
    geometry_row_to_line_item,
)


def test_reject_ocr_garbage_names():
    cases = [
        ("rt_ : er per_ on", "ocr_garbage"),
        ("a—OeeeEeEeEeEeEOEOEeEeEeEeEeEeEeEeEeEe ____ -_ _eeeeee", "ocr_garbage"),
        ("~~~@@@", "ocr_garbage"),
        ("", "empty_product_name"),
    ]
    for text, reason in cases:
        cls, why = classify_grid_row(text)
        assert cls == "NON_PRODUCT", text
        assert why == reason, (text, why)


def test_reject_column_headers_and_period_meta():
    for text, reason in (
        ("Product Name", "column_header"),
        ("Pack", "column_header"),
        ("Op.Stk", "column_header"),
        ("Period From 25/07/2026 To 24/08/2026", "period_meta"),
        ("od From 25/07/2026 To 24/08,", "period_meta"),
        ("Purchase Bills", "ui_chrome"),
        ("Print Data", "ui_chrome"),
        ("Total", "metadata_total_row"),
    ):
        cls, why = classify_grid_row(text)
        assert cls == "NON_PRODUCT", text
        assert why == reason, (text, why)


def test_reject_distributor_and_company_banners_without_qty():
    selected_empty = {
        "opening_qty": None,
        "purchase_qty": None,
        "sales_qty": None,
        "closing_qty": None,
        "total_qty": None,
    }
    for text in (
        "ABAY PHARMA DISTRIBUTORS",
        "CAMBAY PHARMA DISTRIBUTORS",
        "JALAYA DRUG CO(ZANDRA)",
        "HIMALAYA DRUG CO(ZANDRA)",
    ):
        cls, why = classify_grid_row(text, selected=selected_empty)
        assert cls == "NON_PRODUCT", text
        assert why in {
            "metadata_distributor",
            "metadata_company_banner",
            "no_printed_qty_evidence",
        }, (text, why)


def test_reject_name_only_rows_when_qty_cells_were_ocrd():
    selected = {
        "opening_qty": None,
        "receipts_qty": None,
        "sales_qty": None,
        "closing_qty": None,
        "total_qty": None,
        "packing": "oe",
    }
    cls, why = classify_grid_row("~PAMBAY", packing="oe", selected=selected)
    assert cls == "NON_PRODUCT"
    assert why == "no_printed_qty_evidence"


def test_accept_genuine_products_with_printed_qty():
    selected = {
        "opening_qty": 36.0,
        "purchase_qty": 0.0,
        "sales_qty": 4.0,
        "closing_qty": 32.0,
        "total_qty": 36.0,
        "pack": "100ML",
    }
    for text in (
        "BONNISAN SYP 100ML",
        "CYSTONE SYP",
        "LIV 52 DS TAB",
        "SEPTILIN SYP",
        "ARJUNA T",
    ):
        cls, why = classify_grid_row(text, packing="100ML", selected=selected)
        assert cls == "PRODUCT", text
        assert why is None


def test_accept_product_without_selected_for_physical_phase_a():
    """Physical Phase-A classifies before numeric OCR — selected has no qty keys."""
    cls, why = classify_grid_row("BONNISAN 100ML", packing="100ML", selected=None)
    assert cls == "PRODUCT"
    assert why is None


def test_genuine_unmapped_product_still_becomes_line_item():
    """A real product row with printed qtys is kept even if Product Master unknown."""
    row = {
        "row_class": "PRODUCT",
        "selected": {
            "product_name": "UNKNOWN HERBAL SYP 100ML",
            "pack": "100ML",
            "opening_qty": 5.0,
            "purchase_qty": 0.0,
            "sales_qty": 1.0,
            "closing_qty": 4.0,
            "total_qty": 5.0,
        },
        "business_fields": {},
    }
    cls, _ = classify_grid_row(
        row["selected"]["product_name"],
        row["selected"]["pack"],
        selected=row["selected"],
    )
    assert cls == "PRODUCT"
    item = geometry_row_to_line_item(row)
    assert item["product_name"] == "UNKNOWN HERBAL SYP 100ML"
    assert item["opening_qty"] == 5.0


@pytest.mark.skipif(
    not Path(
        "/var/www/html/splitextract/0000700040_2026_08_ZA_06_335_05092026022728.jpg"
    ).exists(),
    reason="fixture image missing",
)
def test_live_cambay_portal_screenshot_rejects_garbage():
    from services.sales_statement_extractor import extract_sales_statement

    path = Path(
        "/var/www/html/splitextract/0000700040_2026_08_ZA_06_335_05092026022728.jpg"
    )
    result = extract_sales_statement(path.read_bytes(), path.name)
    names = [str(i.get("product_name") or "") for i in (result.get("line_items") or [])]
    joined = " | ".join(names).upper()
    assert "PRODUCT NAME" not in joined
    assert "DISTRIBUTOR" not in joined
    assert "FROM 25/07" not in joined and "PERIOD FROM" not in joined
    assert "PURCHASE BILLS" not in joined
    assert not any("~PAMB" in n.upper() for n in names)
    assert not any("OEEEE" in n.upper() for n in names)
    # Genuine SKUs from the portal table must survive.
    assert any("BONNISAN" in n.upper() for n in names)
    assert any("LIV" in n.upper() and "52" in n for n in names)
    assert len(names) >= 4
    assert len(names) <= 10
