"""TRILOK ENTERPRISES product-name cleanup (HSN MFG PRODUCT PACK)."""
import unittest

from app import fix_trilok_enterprises_product_names_from_ocr


TRILOK_OCR = """
TAX INVOICE
TRILOK ENTERPRISES To:- WELLNESS FOREVER CHEMIST AND LIFESTYLE STORE
HSN MFG PRODUCT PACK BATCH EXP MRP QTY RATE Dis VALUE
30049039 GER CINTODAC CAP 10'S IA01746A 9/27 405.33 10 257.35 3.00 0.00 5 2573.50
MARG ERP NANO
"""

TRILOK_XYLO_OCR = """
TAX INVOICE
TRILOK ENTERPRISES To:- WELLNESS FOREVER
HSN MFG PRODUCT PACK BATCH EXP MRP QTY RATE Dis VALUE
30039034 GER XYLOCAINE 2% JELLY 30 G GB0489 3/28 34.82 5 23.88 3.00 0.00 5 119.40
30049099 GER XYLOCAINE 10% SPARY 1X50 AFC1048 2/28 654.07 5 498.34 3.00 0.00 5 2491.70
MARG ERP NANO
"""


class TestTrilokEnterprisesProductNames(unittest.TestCase):
    def test_strips_mfg_and_pack_from_cintodac(self):
        items = [
            {
                "product_description": "GER CINTODAC CAP 10'S",
                "quantity": "10",
                "unit_price": "257.35",
                "total_amount": "2573.50",
                "lot_batch_number": "IA01746A",
            }
        ]
        out = fix_trilok_enterprises_product_names_from_ocr(items, TRILOK_OCR)
        self.assertEqual(out[0]["product_description"], "CINTODAC CAP")
        self.assertEqual(out[0]["additional_fields"]["mfg"], "GER")
        self.assertEqual(out[0]["additional_fields"]["pack"], "10'S")

    def test_strips_pack_30g_and_1x50_and_fixes_spary(self):
        items = [
            {
                "product_description": "XYLOCAINE 2% JELLY 30 G",
                "quantity": "5",
                "lot_batch_number": "GB0489",
                "additional_fields": {"mfg": "GER"},
            },
            {
                "product_description": "XYLOCAINE 10% SPARY 1X50",
                "quantity": "5",
                "lot_batch_number": "AFC1048",
                "additional_fields": {"mfg": "GER"},
            },
        ]
        out = fix_trilok_enterprises_product_names_from_ocr(items, TRILOK_XYLO_OCR)
        self.assertEqual(out[0]["product_description"], "XYLOCAINE 2% JELLY")
        self.assertEqual(out[0]["additional_fields"]["pack"], "30 G")
        self.assertEqual(out[1]["product_description"], "XYLOCAINE 10% SPRAY")
        self.assertEqual(out[1]["additional_fields"]["pack"], "1X50")


if __name__ == "__main__":
    unittest.main()
