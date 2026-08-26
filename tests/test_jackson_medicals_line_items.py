"""Jackson Medicals (FIX12j) — free-row OCR and multipage merge helpers."""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app  # noqa: E402


def test_parse_jackson_free_qty_with_semicolon():
    """Band OCR often emits '1; Free' / '2; Free' after paid triples."""
    ocr = """
133.98 60 381.81 22908.60
92.28 18 103.57 1864.26
2; Free
47.98 12 80.76 969.12
1; Free
68.90 18 77.33 1391.94
2 Free
"""
    rows = app._parse_jackson_qty_rate_rows(ocr)
    assert len(rows) >= 3
    mega = next(r for r in rows if abs(r["total_amount"] - 1864.26) < 0.02)
    carm = next(r for r in rows if abs(r["total_amount"] - 969.12) < 0.02)
    cyclo = next(r for r in rows if abs(r["total_amount"] - 1391.94) < 0.02)
    assert mega.get("free_quantity") == 2
    assert carm.get("free_quantity") == 1
    assert cyclo.get("free_quantity") == 2


def test_parse_jackson_dollar_prefix_prefers_leading_5():
    """HYPONAT band: '$87.63 | $876.30' is 587.63 / 5876.30, not 187.63."""
    rows = app._parse_jackson_qty_rate_rows("00 | 290.88 10 | $87.63 | $876.30")
    assert len(rows) == 1
    assert rows[0]["quantity"] == 10
    assert abs(rows[0]["unit_price"] - 587.63) < 0.02
    assert abs(rows[0]["total_amount"] - 5876.30) < 0.02


def test_parse_jackson_prefers_amount_when_rate_digit_slip():
    """SUPRADYN: OCR Rate 52.01 with Amount 729.54 → rate 52.11."""
    rows = app._parse_jackson_qty_rate_rows("00 36.12 14) 52.01 729.54")
    assert len(rows) == 1
    assert rows[0]["quantity"] == 14
    assert abs(rows[0]["unit_price"] - 52.11) < 0.02
    assert abs(rows[0]["total_amount"] - 729.54) < 0.02


def test_jackson_find_name_for_dollar_amount_ocr():
    ocr = (
        "JACKSON MEDICALS\n"
        "300490 | IIYPONAT-O-15 TAB 10s [03-28] 848.39 [BEC1111 { $876.30\n"
        "300390 = PANTODAC-DSR CAP (II) 1810.40\n"
    )
    name = app._jackson_find_product_name_for_amount(ocr, 5876.30)
    assert "HYPONAT" in name.upper()


def test_jackson_continuation_page_detection():
    page1 = "JACKSON MEDICALS Inv.No. D4151 ST.THOMAS HOSPITAL Page 1 of 2 BILYPSA TAB"
    page2 = (
        "JACKSON MEDICALS Inv.No. D4151 DUPLICATE COPY Page 2 of 2 "
        "AMLOPIN 5MG TAB 10 20.32 203.20 SUPRADYN DAILY TAB"
    )
    assert app._detect_invoice_continuation_page(
        1, "INV/2023/00010", page2, "D4151", page1
    )


def test_merge_jackson_line_items_keeps_distinct_batches():
    base = [
        {
            "product_description": "AMLOPIN 5MG TAB",
            "lot_batch_number": "48020929",
            "quantity": "10",
            "unit_price": "20.32",
            "total_amount": "203.20",
        }
    ]
    donor = [
        {
            "product_description": "AMLOPIN 5MG TAB",
            "lot_batch_number": "48021024",
            "quantity": "10",
            "unit_price": "20.32",
            "total_amount": "203.20",
        },
        {
            "product_description": "SUPRADYN DAILY TAB",
            "lot_batch_number": "MH1209",
            "quantity": "28",
            "unit_price": "52.11",
            "total_amount": "1459.08",
            "additional_fields": {"free_quantity": "2"},
        },
    ]
    merged = app._merge_jackson_line_item_lists(base, donor)
    assert len(merged) == 3


def test_fix12j_keeps_hyponat_and_dual_amlopin():
    """qty=1 rate==amount is valid; two AMLOPIN batches must both survive."""
    items = [
        {
            "product_description": "HYPONAT-O-15 TAB",
            "quantity": "1",
            "unit_price": "587.63",
            "total_amount": "587.63",
            "lot_batch_number": "BEC1041",
            "additional_fields": {"free_quantity": "0"},
        },
        {
            "product_description": "AMLOPIN 5MG TAB",
            "quantity": "10",
            "unit_price": "20.32",
            "total_amount": "203.20",
            "lot_batch_number": "48020929",
            "additional_fields": {"free_quantity": "0"},
        },
        {
            "product_description": "AMLOPIN 5MG TAB",
            "quantity": "10",
            "unit_price": "20.32",
            "total_amount": "203.20",
            "lot_batch_number": "48021024",
            "additional_fields": {"free_quantity": "0"},
        },
    ]
    band = """
60 381.81 22908.60
3 450.32 1350.96
20 197.80 3956.00
70 464.19 32493.30
18 103.57 1864.26
10 45.12 451.20
1 587.63 587.63
30 142.80 4284.00
10 181.04 1810.40
18 378.24 6808.32
12 80.76 969.12
2 27.20 54.40
10 20.32 203.20
10 20.32 203.20
"""
    ocr = "JACKSON MEDICALS Inv.No. D4151\n" + band
    fixed = app.fix_jackson_medicals_line_items_from_ocr(
        items, ocr_text=ocr, table_ocr=band, vendor="JACKSON MEDICALS"
    )
    hy = next(
        it for it in fixed
        if "HYPONAT" in str(it.get("product_description", "")).upper()
    )
    assert hy["quantity"] == "1"
    assert hy["unit_price"] == "587.63"
    assert hy["total_amount"] == "587.63"
    ams = [
        it for it in fixed
        if "AMLOPIN" in str(it.get("product_description", "")).upper()
    ]
    assert len(ams) == 2
    assert all(it["quantity"] == "10" for it in ams)
    assert all(it["unit_price"] == "20.32" for it in ams)
    frees = [
        float((it.get("additional_fields") or {}).get("free_quantity") or 0)
        for it in ams
    ]
    assert all(f == 0 for f in frees)


def test_parse_jackson_qty_zero_slip_ecosprin75():
    """QTY 30 Rate 4.34 OCR as '3 434 13020' must not become 3×43.40."""
    rows = app._parse_jackson_qty_rate_rows("5.00 | 6.44 3 434} 13020")
    assert len(rows) == 1
    assert rows[0]["quantity"] == 30
    assert abs(rows[0]["unit_price"] - 4.34) < 0.02
    assert abs(rows[0]["total_amount"] - 130.20) < 0.02


def test_parse_jackson_glued_qty_rate_supradyn():
    """QTY 14 glued onto Rate 52.10 as 1452.10 with Amount 729.40."""
    rows = app._parse_jackson_qty_rate_rows("5.00 | 36.10 1452.10 | 729.40")
    assert len(rows) == 1
    assert rows[0]["quantity"] == 14
    assert abs(rows[0]["unit_price"] - 52.10) < 0.02
    assert abs(rows[0]["total_amount"] - 729.40) < 0.02


def test_fix12j_keeps_dual_batch_ecosprin_gold():
    """Same product, different batches and Amounts — both paid rows stay."""
    items = [
        {
            "product_description": "ECOSPRIN GOLD -40 CAP",
            "quantity": "1",
            "unit_price": "206.56",
            "total_amount": "206.56",
            "lot_batch_number": "ECS26018",
            "additional_fields": {"free_quantity": "0"},
        },
        {
            "product_description": "ECOSPRIN GOLD -40 CAP",
            "quantity": "4",
            "unit_price": "206.56",
            "total_amount": "826.24",
            "lot_batch_number": "ECS26019",
            "additional_fields": {"free_quantity": "0"},
        },
    ]
    band = """
5 381.81 1909.05
20 464.19 9283.80
18 103.57 1864.26
2 Free
10 82.03 820.30
2 Free
10 587.63 5876.30
10 142.80 1428.00
16 47.62 761.92
2 Free
20 99.20 1984.00
5 19.95 99.75
10 110.77 1107.70
30 74.21 2226.30
5 25.98 129.90
10 19.31 193.10
30 4.34 130.20
1 206.56 206.56
4 206.56 826.24
50 8.69 434.50
10 48.97 489.70
14 52.10 729.40
1 Free
"""
    ocr = "JACKSON MEDICALS Inv.No. D7655\n" + band
    fixed = app.fix_jackson_medicals_line_items_from_ocr(
        items, ocr_text=ocr, table_ocr=band, vendor="JACKSON MEDICALS"
    )
    golds = [
        it for it in fixed
        if "GOLD" in str(it.get("product_description", "")).upper()
    ]
    assert len(golds) == 2
    by_batch = {str(it.get("lot_batch_number")): it for it in golds}
    assert by_batch["ECS26018"]["quantity"] == "1"
    assert by_batch["ECS26018"]["unit_price"] == "206.56"
    assert by_batch["ECS26019"]["quantity"] == "4"
    assert by_batch["ECS26019"]["unit_price"] == "206.56"
    for it in golds:
        fq = float((it.get("additional_fields") or {}).get("free_quantity") or 0)
        assert fq == 0


def test_fix12j_applies_free_quantity_from_band_ocr():
    items = [
        {
            "product_description": "MEGAHEAL GEL",
            "quantity": "1",
            "unit_price": "1864.26",
            "total_amount": "1864.26",
            "additional_fields": {"free_quantity": "0"},
        },
        {
            "product_description": "CARMICIDE+ PAED. LIQ.",
            "quantity": "1",
            "unit_price": "969.12",
            "total_amount": "969.12",
            "additional_fields": {"free_quantity": "0"},
        },
    ]
    # Pad with enough coherent triples so sanity gate passes
    band = """
60 381.81 22908.60
3 450.32 1350.96
20 197.80 3956.00
70 464.19 32493.30
18 103.57 1864.26
2; Free
10 45.12 451.20
1 587.63 587.63
30 142.80 4284.00
10 181.04 1810.40
18 378.24 6808.32
2 Free
12 80.76 969.12
1; Free
"""
    ocr = "JACKSON MEDICALS Inv.No. D4151\n" + band
    fixed = app.fix_jackson_medicals_line_items_from_ocr(
        items, ocr_text=ocr, table_ocr=band, vendor="JACKSON MEDICALS"
    )
    by_name = {
        app._jackson_product_name_key(it["product_description"]): it for it in fixed
    }
    mega = by_name.get(app._jackson_product_name_key("MEGAHEAL GEL"))
    carm = by_name.get(app._jackson_product_name_key("CARMICIDE+ PAED. LIQ."))
    assert mega is not None
    assert carm is not None
    assert mega["quantity"] == "18"
    assert mega["unit_price"] == "103.57"
    assert str(mega.get("additional_fields", {}).get("free_quantity")) == "2"
    assert carm["quantity"] == "12"
    assert carm["unit_price"] == "80.76"
    assert str(carm.get("additional_fields", {}).get("free_quantity")) == "1"
