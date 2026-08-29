"""STANDARD AGENCIES — restore Rate/QTY when Amount lost its decimal."""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app  # noqa: E402


OCR = """
STANDARD AGENCIES
GERMA 30049099 ODIMONT FX TABLET 158 IBOO478A 02-28 323.29 179.25 (26 2258550 5.00 564.64 564.64 1129.28 23714.78
GERMA 30049099 ODIMONT FX TABLET 158 1B00S78C 02-28 323.29 179.25 4 1326450 5.00 331.61 331.61 663.22 13927.72
GERMA 30049099 ODIMONT LC KID TABLETS 15'S 1B00792A 03-28 254.10 154.50 10 1545.00 5.00 38.63 38.63 77.26 1622.26
ZYDUS 30049087 LINIDLV 300ML ABB1083 05-27 838.04 119.72 30 3591.60 5.00 89.79 89.79 179.58 3771.18
"""


def test_detects_standard_agencies():
    assert app.ocr_suggests_standard_agencies(OCR, "STANDARD AGENCIES")
    assert not app.ocr_suggests_standard_agencies(
        "JACKSON MEDICALS KL-KTM-121923", "JACKSON MEDICALS")


def test_parse_odimont_fx_restores_qty_126():
    row = app._parse_standard_agencies_qty_rate_line(
        "GERMA 30049099 ODIMONT FX TABLET 158 IBOO478A 02-28 323.29 179.25 (26 2258550 5.00 564.64"
    )
    assert row is not None
    assert row["quantity"] == 126
    assert abs(row["unit_price"] - 179.25) < 0.02
    assert abs(row["total_amount"] - 22585.50) < 0.02


def test_parse_odimont_fx_restores_qty_74():
    row = app._parse_standard_agencies_qty_rate_line(
        "GERMA 30049099 ODIMONT FX TABLET 158 1B00S78C 02-28 323.29 179.25 4 1326450 5.00 331.61"
    )
    assert row is not None
    assert row["quantity"] == 74
    assert abs(row["unit_price"] - 179.25) < 0.02
    assert abs(row["total_amount"] - 13264.50) < 0.02


def test_fix_does_not_change_already_correct_linid():
    items = [
        {
            "product_description": "LINID I.V",
            "quantity": "30",
            "unit_price": "119.72",
            "total_amount": "3591.60",
        },
        {
            "product_description": "ODIMONT FX TABLET",
            "quantity": "26",
            "unit_price": "86867.31",
            "total_amount": "2258550",
        },
        {
            "product_description": "ODIMONT FX TABLET",
            "quantity": "4",
            "unit_price": "331612.50",
            "total_amount": "1326450",
        },
        {
            "product_description": "ODIMONT LC KID TABLETS",
            "quantity": "10",
            "unit_price": "154.50",
            "total_amount": "1545.00",
        },
    ]
    out = app.fix_standard_agencies_qty_rate_from_ocr(
        items, OCR, vendor="STANDARD AGENCIES"
    )
    linid = next(i for i in out if "LINID" in i["product_description"].upper())
    assert linid["quantity"] == "30"
    assert linid["unit_price"] == "119.72"
    fxs = [i for i in out if "FX" in i["product_description"].upper()]
    assert len(fxs) == 2
    by_qty = sorted(fxs, key=lambda x: int(x["quantity"]))
    assert by_qty[0]["quantity"] == "74"
    assert by_qty[0]["unit_price"] == "179.25"
    assert by_qty[1]["quantity"] == "126"
    assert by_qty[1]["unit_price"] == "179.25"
    lc = next(i for i in out if "KID" in i["product_description"].upper())
    assert lc["quantity"] == "10"
    assert lc["unit_price"] == "154.50"


def test_fix_skips_other_vendors():
    items = [{
        "product_description": "ODIMONT FX TABLET",
        "quantity": "26",
        "unit_price": "86867.31",
        "total_amount": "2258550",
    }]
    out = app.fix_standard_agencies_qty_rate_from_ocr(
        items, "JACKSON MEDICALS Inv.No. D7655", vendor="JACKSON MEDICALS"
    )
    assert out[0]["quantity"] == "26"
    assert out[0]["unit_price"] == "86867.31"
