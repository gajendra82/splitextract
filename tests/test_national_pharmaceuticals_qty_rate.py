"""NATIONAL PHARMACEUTICALS stockist: QTY./RATE from table OCR (not NET RATE)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import (  # noqa: E402
    fix_national_pharmaceuticals_qty_rate_from_ocr,
    ocr_suggests_national_pharmaceuticals_stockist,
    _parse_national_pharmaceuticals_qty_rate_rows,
)


OCR = """
NATIONAL PHARMACEUTICALS
TAX INVOICE
SN ITEM NAME & PACK HSN CODE BATCH EXPIRY QTY. NET RATE M.R.P. RATE C.D% GST% AMOUNT
1 BETD 0.5ML B.E LT30022022 A2700225 03/28 5.00 20.58 24.49 20.00 2.0 5% 102.90
2 LINID TAB 600 MG 1*10 TAB. ZYDUS 30049039 IA01838A 10/27 6.00 246.46 381.04 239.51 2.0 5% 1478.74
3 LINID TAB 600 MG 1*10 TAB. ZYDUS 30049039 IB00221A 12/27 2.00 246.46 381.04 239.51 2.0 5% 492.91
4 LINID TAB 600 MG 1*10 TAB. ZYDUS 30049039 IB00540A 02/28 2.00 246.46 381.04 239.51 2.0 5% 492.91
5 THROMBOPHOB OINTMENT 30G ZYDUS 30019091 IA01681A 09/28 5.00 143.43 225.53 139.39 2.0 5% 717.16
6 BOOSTRIX GSK PH30022021 AC37B531AD 11/28 5.00 1324.96 1690.00 1287.62 2.0 5% 6624.81
Tot Qty: 25.00
BILL AMT : 9909.00
"""


class TestNationalPharmaceuticalsQtyRate(unittest.TestCase):
    def test_detects_national_pharmaceuticals(self):
        self.assertTrue(ocr_suggests_national_pharmaceuticals_stockist(OCR))
        self.assertFalse(ocr_suggests_national_pharmaceuticals_stockist(
            "JACKSON MEDICALS Inv.No. D7655", "JACKSON MEDICALS"))

    def test_parse_betd_uses_rate_not_net_rate(self):
        rows = _parse_national_pharmaceuticals_qty_rate_rows(OCR)
        betd = next(r for r in rows if "BETD" in r["product_description"].upper())
        self.assertEqual(betd["quantity"], 5.0)
        self.assertAlmostEqual(betd["unit_price"], 20.00, places=2)
        self.assertAlmostEqual(betd["total_amount"], 102.90, places=2)
        self.assertFalse(betd["is_free"])

    def test_fix_restores_swapped_betd_only(self):
        items = [
            {
                "product_description": "BETD 0.5ML B.E",
                "quantity": "20",
                "unit_price": "5.15",
                "total_amount": "102.90",
                "lot_batch_number": "A2700225",
                "additional_fields": {"mrp": "24.49", "free_quantity": "0"},
            },
            {
                "product_description": "LINID TAB 600 MG 1*10 TAB.",
                "quantity": "6.00",
                "unit_price": "239.51",
                "total_amount": "1478.74",
                "lot_batch_number": "IA01838A",
                "additional_fields": {"mrp": "381.04", "free_quantity": "0"},
            },
            {
                "product_description": "LINID TAB 600 MG 1*10 TAB.",
                "quantity": "2.00",
                "unit_price": "239.51",
                "total_amount": "492.91",
                "lot_batch_number": "IB00221A",
                "additional_fields": {"mrp": "381.04", "free_quantity": "0"},
            },
            {
                "product_description": "LINID TAB 600 MG 1*10 TAB.",
                "quantity": "2.00",
                "unit_price": "239.51",
                "total_amount": "492.91",
                "lot_batch_number": "IB00540A",
                "additional_fields": {"mrp": "381.04", "free_quantity": "0"},
            },
            {
                "product_description": "THROMBOPHOB OINTMENT 30G",
                "quantity": "5.00",
                "unit_price": "139.39",
                "total_amount": "717.16",
                "lot_batch_number": "IA01681A",
                "additional_fields": {"mrp": "225.53", "free_quantity": "0"},
            },
            {
                "product_description": "BOOSTRIX",
                "quantity": "5.00",
                "unit_price": "1287.62",
                "total_amount": "6624.81",
                "lot_batch_number": "AC37B531AD",
                "additional_fields": {"mrp": "1690.00", "free_quantity": "0"},
            },
        ]
        out = fix_national_pharmaceuticals_qty_rate_from_ocr(
            items, OCR, vendor="NATIONAL PHARMACEUTICALS"
        )
        betd = out[0]
        self.assertEqual(betd["quantity"], "5")
        self.assertEqual(betd["unit_price"], "20.00")
        # Already-correct rows unchanged
        self.assertEqual(out[1]["quantity"], "6.00")
        self.assertEqual(out[1]["unit_price"], "239.51")
        self.assertEqual(out[5]["quantity"], "5.00")
        self.assertEqual(out[5]["unit_price"], "1287.62")

    def test_free_row_keeps_zero_rate(self):
        ocr = OCR + (
            "\n7 FREE SAMPLE TAB ZYDUS 30049039 FREE001 01/28 "
            "2.00 0.00 10.00 0.00 0.0 0% 0.00\n"
        )
        items = [
            {
                "product_description": "FREE SAMPLE TAB",
                "quantity": "2",
                "unit_price": "239.51",
                "total_amount": "0.00",
                "lot_batch_number": "FREE001",
                "additional_fields": {},
            }
        ]
        out = fix_national_pharmaceuticals_qty_rate_from_ocr(items, ocr)
        self.assertEqual(out[0]["quantity"], "2")
        self.assertEqual(out[0]["unit_price"], "0.00")
        self.assertEqual(out[0]["total_amount"], "0.00")
        self.assertEqual(out[0]["additional_fields"]["free_quantity"], "2")

    def test_fix_skips_other_vendors(self):
        items = [{
            "product_description": "BETD 0.5ML B.E",
            "quantity": "20",
            "unit_price": "5.15",
            "total_amount": "102.90",
        }]
        out = fix_national_pharmaceuticals_qty_rate_from_ocr(
            items, "JACKSON MEDICALS Inv.No. D7655", vendor="JACKSON MEDICALS"
        )
        self.assertEqual(out[0]["quantity"], "20")
        self.assertEqual(out[0]["unit_price"], "5.15")


if __name__ == "__main__":
    unittest.main()
