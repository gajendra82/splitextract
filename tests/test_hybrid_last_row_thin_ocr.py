"""Last thin product-row OCR must retain printed qty/value (not silent zeros).

Uses the portal screenshot fixture. Does not hardcode stockist/product/qty
literals as the extraction oracle — asserts geometry-backed printed fields
survive for the last PRODUCT row before footer chrome.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from services.stock_geometry_hybrid_v3 import (
    run_hybrid_v3,
    select_ocr_variants,
)
from services.stock_geometry_table_v3 import classify_grid_row, geometry_row_to_line_item

FIX = Path(__file__).resolve().parent / "fixtures" / (
    "0000700040_2026_08_ZA_06_335_05092026022728.jpg"
)


@pytest.mark.skipif(not FIX.is_file(), reason="portal screenshot fixture missing")
def test_last_product_row_keeps_printed_qty_and_value():
    report = run_hybrid_v3(str(FIX), enable_gemini=False)
    assert not report.get("error"), report.get("error")

    product_rows = []
    for r in report.get("rows") or []:
        sel = dict(r.get("selected") or {})
        name = str(sel.get("product_name") or r.get("product_name_ocr") or "")
        pack = str(sel.get("pack") or "")
        if "purchase_qty" in sel and "receipts_qty" not in sel:
            sel["receipts_qty"] = sel.get("purchase_qty")
        cls, _why = classify_grid_row(name, pack, selected=sel)
        if cls != "PRODUCT":
            continue
        r = {**r, "selected": sel}
        product_rows.append(r)

    assert len(product_rows) >= 2
    # Prior rows must still extract (regression vs all-zero collapse).
    first = geometry_row_to_line_item(product_rows[0])
    assert float(first.get("opening_qty") or 0) > 0 or float(
        first.get("receipts_qty") or 0
    ) > 0
    assert float(first.get("closing_value") or 0) > 0

    last = product_rows[-1]
    sel = last.get("selected") or {}
    # Numeric tokens were retained on the geometric last product band.
    assert sel.get("total_qty") is not None
    assert sel.get("opening_qty") is not None
    assert sel.get("closing_qty") is not None
    assert sel.get("closing_value") is not None
    assert sel.get("sales_qty") is not None

    item = geometry_row_to_line_item(last)
    fs = (item.get("extra") or {}).get("field_source") or {}
    assert fs.get("opening_qty") == "printed"
    assert fs.get("closing_qty") == "printed"
    assert fs.get("closing_value") == "printed"
    # API legacy 0.0 must not be the only signal — printed closing_value survives.
    assert float(item.get("closing_value") or 0) > 0
    assert float(item.get("opening_qty") or 0) > 0
    # Sale return column absent on this layout — must stay missing, not shifted.
    assert fs.get("sales_return_qty") == "missing"
    # Closing value must not have been stuffed into closing qty.
    assert float(item.get("closing_qty") or 0) != float(item.get("closing_value") or 0)

    ident = last.get("identity") or {}
    assert ident.get("total_ok") is True
    assert ident.get("closing_ok") is True


def test_multi_variant_low_conf_agreement_accepted():
    """Faint cells often have tess conf < 40 but all variants agree."""
    values = [
        ("A_original", "1264", 31.0, 1264.0, False, False),
        ("D_blue_removed", "1264", 31.0, 1264.0, False, False),
        ("H_max_channel", "1264", 27.0, 1264.0, False, False),
        ("C_threshold", "1264", 24.0, 1264.0, False, False),
    ]
    best, reason, _changed = select_ocr_variants(values, min_conf=40.0)
    assert best is not None
    assert best[3] == 1264.0
    assert "agree" in reason or reason == "majority_vote_low_conf_agree"


def test_product_row_gate_still_rejects_footer():
    selected = {
        "opening_qty": None,
        "purchase_qty": None,
        "sales_qty": None,
        "closing_qty": None,
        "total_qty": None,
    }
    cls, why = classify_grid_row("Total Value", packing="Op.Val.", selected=selected)
    assert cls == "NON_PRODUCT"
    assert why in {"metadata_total_row", "column_header", "no_printed_qty_evidence"}
