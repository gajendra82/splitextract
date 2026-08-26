"""Novacare GGN/scanned invoices: QTY/RATE from BATCH + money trail fallback."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import fix_novacare_qty_rate_from_ocr  # noqa: E402


SCANNED_OCR = """
Healthcare Solutions Pvt. Ltd. A unit of Entero Healthcare Solutions Ltd.
Email ID : ggn@novacare.in No. GGN-26-23388 Date 23-07-2026
HSN/SAC STR PRODUCT DESCRIPTION Pack MFG EXP BATCH NO. QTY FREE OLD NEW MRP RATE AMOUNT
3049087 [Ac ose? |ABo47e3 30 36.05 33.80|23.43| 702.90| 5.00| 667.76| 5.00 33.39
[scosso7a RT A01384C S| f  |, 46238| 433.49| 258.51| 1,292.55| 5.00| 1227.92| 5.00 | 61.40
| 30049033 ac | XSRS26022 5/ 195.45| 183.23 93.73 468.65| 5.00| 445.22| 5.00 | 22.26
ToPay : 2458.00/-
"""

FREE_OCR = """
ggn@novacare.in NEW MRP RATE AMOUNT BATCH NO.
30049057 AC AKT-4 2TAB LUPIN 0927 AB04763 10 36.05 33.80 23.43 234.30 5.00 222.59 5.00 11.13
30049079 RT ATORVA 80 TAB 1A01384C FREE 2 0.00 0.00 0.00 0.00 5.00 0.00 5.00 0.00
"""


class TestNovacareScannedQtyRate(unittest.TestCase):
    def test_fixes_qty_rate_only_from_scanned_ocr(self):
        items = [
            {
                "product_description": "Ac ose?",
                "quantity": "30",
                "unit_price": "36.05",
                "total_amount": "1081.50",
                "lot_batch_number": "ABo47e3",
                "additional_fields": {"mrp": "33.80"},
            },
            {
                "product_description": "RT",
                "quantity": "46238",
                "unit_price": "433.49",
                "total_amount": "19999999.02",
                "lot_batch_number": "A01384C",
                "additional_fields": {"mrp": "258.51"},
            },
            {
                "product_description": "ac",
                "quantity": "5",
                "unit_price": "195.45",
                "total_amount": "977.25",
                "lot_batch_number": "XSRS26022",
                "additional_fields": {"mrp": "183.23"},
            },
        ]
        out = fix_novacare_qty_rate_from_ocr(items, SCANNED_OCR)
        by_batch = {i["lot_batch_number"].upper(): i for i in out}
        self.assertEqual(by_batch["ABO47E3"]["quantity"], "30")
        self.assertEqual(by_batch["ABO47E3"]["unit_price"], "23.43")
        self.assertEqual(by_batch["ABO47E3"]["product_description"], "Ac ose?")
        self.assertEqual(by_batch["ABO47E3"]["total_amount"], "1081.50")
        self.assertEqual(by_batch["A01384C"]["quantity"], "5")
        self.assertEqual(by_batch["A01384C"]["unit_price"], "258.51")
        self.assertEqual(by_batch["XSRS26022"]["quantity"], "5")
        self.assertEqual(by_batch["XSRS26022"]["unit_price"], "93.73")

    def test_free_row_keeps_zero_rate(self):
        items = [
            {
                "product_description": "AKT-4",
                "quantity": "10",
                "unit_price": "36.05",
                "lot_batch_number": "AB04763",
            },
            {
                "product_description": "ATORVA FREE",
                "quantity": "1",
                "unit_price": "258.51",
                "lot_batch_number": "1A01384C",
                "additional_fields": {},
            },
        ]
        out = fix_novacare_qty_rate_from_ocr(items, FREE_OCR)
        paid = next(i for i in out if "AKT" in i["product_description"].upper())
        free = next(i for i in out if "ATORVA" in i["product_description"].upper())
        self.assertEqual(paid["quantity"], "10")
        self.assertEqual(paid["unit_price"], "23.43")
        self.assertEqual(free["quantity"], "2")
        self.assertEqual(free["unit_price"], "0.00")
        self.assertEqual(
            str((free.get("additional_fields") or {}).get("free_quantity")), "2"
        )

    def test_unrelated_format_unchanged(self):
        items = [{
            "product_description": "SOME TAB",
            "quantity": "10",
            "unit_price": "12.00",
            "lot_batch_number": "XYZ123",
        }]
        out = fix_novacare_qty_rate_from_ocr(
            items, "SUPREME LIFE SCIENCES MARG ERP BATCH NO QTY RATE"
        )
        self.assertEqual(out[0]["unit_price"], "12.00")
        self.assertEqual(out[0]["quantity"], "10")


if __name__ == "__main__":
    unittest.main()
