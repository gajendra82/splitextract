"""YASH AGENCIES: AMOUNT parked as rate must not collapse qty→1."""
import unittest

from app import (
    fix_mrp_as_unit_price,
    _yash_line_items_rates_look_corrupt,
    _ocr_suggests_yash_agencies,
)


YASH_OCR = """
YASH AGENCIES TAX INVOICE
TaxInvNo : YE03417
InvDate : 24/06/2026
PRODUCT NAME PACK HSNCODE BATCH EXP QTY FREE OLDMRP M.R.P RATE DIS% AMOUNT GST%
"""


class TestYashAgenciesRate(unittest.TestCase):
    def test_detect_yash(self):
        self.assertTrue(_ocr_suggests_yash_agencies(YASH_OCR))

    def test_amount_as_rate_derives_unit_price_keeps_qty(self):
        item = {
            "product_description": "THROMBOPHOB GEL 30G",
            "quantity": "12",
            "unit_price": "2422.32",
            "total_amount": "2422.32",
        }
        out = fix_mrp_as_unit_price(
            item, vendor="YASH AGENCIES", ocr_text=YASH_OCR
        )
        self.assertEqual(out["quantity"], "12")
        self.assertAlmostEqual(float(out["unit_price"]), 201.86, places=2)

    def test_cintapro_amount_as_rate(self):
        item = {
            "product_description": "CINTAPRO TABLETS - 8.5",
            "quantity": "350",
            "unit_price": "31080.00",
            "total_amount": "31080.00",
        }
        out = fix_mrp_as_unit_price(
            item, vendor="YASH AGENCIES", ocr_text=YASH_OCR
        )
        self.assertEqual(out["quantity"], "350")
        self.assertAlmostEqual(float(out["unit_price"]), 88.80, places=2)

    def test_corrupt_detector_amount_as_rate(self):
        items = [
            {
                "product_description": "A",
                "quantity": "12",
                "unit_price": "2422.32",
                "total_amount": "2422.32",
            },
            {
                "product_description": "B",
                "quantity": "350",
                "unit_price": "31080.00",
                "total_amount": "31080.00",
            },
            {
                "product_description": "C",
                "quantity": "80",
                "unit_price": "0",
                "total_amount": "0",
            },
        ]
        self.assertTrue(_yash_line_items_rates_look_corrupt(items))

    def test_non_yash_still_collapses_qty_via_generic_path(self):
        # Without YASH marker, generic QTY-misread may still set qty=1
        item = {
            "product_description": "SOME PRODUCT",
            "quantity": "12",
            "unit_price": "2422.32",
            "total_amount": "2422.32",
        }
        out = fix_mrp_as_unit_price(item, vendor="OTHER", ocr_text="OTHER VENDOR")
        # Generic path: nearest_qty from total/rate = 1
        self.assertEqual(str(out["quantity"]), "1")

    def test_amount_invented_total_recovers_rate_from_mrp(self):
        # unit_price holds AMOUNT; total = qty × amount; MRP known
        item = {
            "product_description": "NATUROGEST INJECTION 100",
            "quantity": "40",
            "unit_price": "2542.00",
            "total_amount": "101680.00",
            "additional_fields": {"mrp": "208.72"},
        }
        out = fix_mrp_as_unit_price(
            item, vendor="YASH AGENCIES", ocr_text=YASH_OCR
        )
        self.assertEqual(out["quantity"], "40")
        self.assertAlmostEqual(float(out["unit_price"]), 63.55, places=2)
        self.assertAlmostEqual(float(out["total_amount"]), 2542.00, places=2)


if __name__ == "__main__":
    unittest.main()
