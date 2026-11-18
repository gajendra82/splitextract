"""TULSYAN RAMKRISHNA CARE GST CREDIT: Qty. column, not Rate/Old MRP."""
import unittest

from app import (
    extract_tulsyan_rkc_line_items_from_ocr,
    fix_tulsyan_pharmaceuticals_line_items_from_ocr,
    fix_mrp_as_unit_price,
    ocr_suggests_tulsyan_rkc_table,
    _tulsyan_rkc_items_look_garbled,
)


R021392_OCR = """
TULSYAN PHARMACEUTICALS PVT LTD
M/s RAMKRISHNA CARE MED. SCI. PVT LTD
GST INVOICE CREDIT
Invoice No. R021392
Invoice Date 04-09-2026
IMFG Qty. Free | Item Name [Old Mrp) Pack Batch Exp. HSN Mrp Rate Dis SGST CGST Amount
ZYD 2 0 |SOVIHEP V TAB 17500, -1*28 1B01548A 5/28 30049099 18046.82 5749.80 0 250 2.50 11499.60
OPD Total Qty.: 2
SUB TOTAL 11499.60
GRAND TOTAL 12075.00
"""


class TestTulsyanRkcSovihepQty(unittest.TestCase):
    def test_detects_rkc_layout(self):
        self.assertTrue(ocr_suggests_tulsyan_rkc_table(R021392_OCR))

    def test_parses_qty_two_not_rate(self):
        rows = extract_tulsyan_rkc_line_items_from_ocr(R021392_OCR)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["quantity"], "2")
        self.assertAlmostEqual(float(rows[0]["unit_price"]), 5749.80, places=2)
        self.assertAlmostEqual(float(rows[0]["total_amount"]), 11499.60, places=2)
        self.assertIn("SOVIHEP", rows[0]["product_description"].upper())

    def test_rate_parked_as_qty_is_garbled(self):
        items = [{
            "product_description": "SOVIHEP V TAB",
            "quantity": "5749.8",
            "unit_price": "5.48",
            "total_amount": "31499.6",
        }]
        self.assertTrue(_tulsyan_rkc_items_look_garbled(items))

    def test_fix_mrp_does_not_invent_rate_from_qty(self):
        item = {
            "product_description": "SOVIHEP V TAB",
            "quantity": "5749.8",
            "unit_price": "17500.0",
            "total_amount": "31499.6",
            "additional_fields": {"mrp": "18046.82"},
        }
        out = fix_mrp_as_unit_price(
            item,
            vendor="TULSYAN PHARMACEUTICALS PVT LTD",
            ocr_text=R021392_OCR,
        )
        self.assertEqual(str(out["quantity"]), "5749.8")
        self.assertAlmostEqual(float(out["unit_price"]), 17500.0, places=1)

    def test_restores_qty_two_from_ocr(self):
        items = [{
            "product_description": "SOVIHEP V TAB",
            "quantity": "5749.8",
            "unit_price": "17500.0",
            "total_amount": "31499.6",
            "additional_fields": {"mrp": "18046.82"},
        }]
        out = fix_tulsyan_pharmaceuticals_line_items_from_ocr(
            items, R021392_OCR, ""
        )
        self.assertEqual(len(out), 1)
        self.assertEqual(str(int(float(out[0]["quantity"]))), "2")
        self.assertAlmostEqual(float(out[0]["unit_price"]), 5749.80, places=2)
        self.assertAlmostEqual(float(out[0]["total_amount"]), 11499.60, places=2)
        self.assertIn("SOVIHEP", out[0]["product_description"].upper())

    def test_old_mrp_parked_as_qty_restores_two(self):
        items = [{
            "product_description": "SOVIHEP V TAB",
            "quantity": "17500",
            "unit_price": "5749.80",
            "total_amount": "31499.60",
            "additional_fields": {"mrp": "18046.82"},
        }]
        self.assertTrue(_tulsyan_rkc_items_look_garbled(items))
        out = fix_tulsyan_pharmaceuticals_line_items_from_ocr(
            items, R021392_OCR, ""
        )
        self.assertEqual(str(int(float(out[0]["quantity"]))), "2")
        self.assertAlmostEqual(float(out[0]["unit_price"]), 5749.80, places=2)
        self.assertAlmostEqual(float(out[0]["total_amount"]), 11499.60, places=2)

    def test_qty_from_zyd_line_without_subtotal(self):
        weak_ocr = """
TULSYAN PHARMACEUTICALS PVT LTD
GST INVOICE CREDIT
ZYD 2 0 SOVIHEP V TAB 17500 1*28 IB01548A
OPD Total Qty.: 2
GRAND TOTAL 12075.00
"""
        items = [{
            "product_description": "SOVIHEP V TAB",
            "quantity": "17500",
            "unit_price": "5749.80",
            "total_amount": "31499.60",
        }]
        out = fix_tulsyan_pharmaceuticals_line_items_from_ocr(
            items, weak_ocr, ""
        )
        self.assertEqual(str(int(float(out[0]["quantity"]))), "2")

    def test_footer_total_qty_when_zyd_line_missing(self):
        footer_ocr = """
TULSYAN PHARMACEUTICALS PVT LTD
GST INVOICE CREDIT
SOVIHEP V TAB
OPD Total Qty.: 2
GRAND TOTAL 12075.00
"""
        items = [{
            "product_description": "SOVIHEP V TAB",
            "quantity": "17500",
            "unit_price": "5749.80",
            "total_amount": "31499.60",
        }]
        out = fix_tulsyan_pharmaceuticals_line_items_from_ocr(
            items, footer_ocr, ""
        )
        self.assertEqual(str(int(float(out[0]["quantity"]))), "2")

    def test_non_tulsyan_still_rewrites_via_generic_mrp_path(self):
        item = {
            "product_description": "SOVIHEP V TAB",
            "quantity": "5749.8",
            "unit_price": "17500.0",
            "total_amount": "31499.6",
            "additional_fields": {"mrp": "18046.82"},
        }
        out = fix_mrp_as_unit_price(
            item, vendor="OTHER STOCKIST", ocr_text="OTHER STOCKIST TAX INVOICE"
        )
        self.assertAlmostEqual(float(out["unit_price"]), 5.48, places=2)


if __name__ == "__main__":
    unittest.main()
