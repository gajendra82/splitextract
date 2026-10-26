"""SINGHAL MEDICALS product name corrections from OCR Description column."""
import unittest

from app import (
    extract_singhal_medicals_line_items_from_ocr,
    fix_singhal_medicals_product_names_from_ocr,
    _clean_singhal_medicals_product_name,
)


SINGHAL_OCR = """
SALES INVOICE
SINGHAL MEDICALS
KATRA BAZAR SAGAR-470002
GST No.:- 23AEQFS9225K1ZG
Invoice No. : S12822 Date : 11/06/2026
Sr. Description Qty Unit Batch HSN ExpDt MRP Rate Disc Total Taxable GST%
1 DYNAPAR TAB 15'S 50 15'S D150H246 300490 01/30 130.45 94.28 0.00 4714.00 4714.00 5.00
2 ZINCOVIT TAB 15'S 45 15'S ZVT26049 210690 08/27 107.67 82.03 0.00 3691.35 3691.35 5.00
3 AUGPEN IV 1.2GM INJ I VIAL 20 I VIAL ZLB6AD5014 300410 11/27 151.30 69.00 0.00 1380.00 1380.00 5.00
4 ENZOFLAM SP TAB 10'S 100 10'S 26L3GTA111 300490 04/28 150.50 104.29 0.00 10429.00 10429.00 5.00
8 AUGMENTIN-625 DUO TAB 10'S 30 10'S 826D052 300410 07/27 195.39 146.33 0.00 4389.90 4389.90 5.00
7 ZOCEF 500 TAB 10' s 50 10' s 26460376 300420 02/28 549.39 372.96 0.00 18648.00 18648.00 5.00
"""


class TestSinghalMedicalsProductNames(unittest.TestCase):
    def test_parse_ocr_rows(self):
        rows = extract_singhal_medicals_line_items_from_ocr(SINGHAL_OCR)
        by_batch = {r["lot_batch_number"]: r for r in rows}
        self.assertEqual(by_batch["D150H246"]["product_description"], "DYNAPAR TAB 15'S")
        self.assertEqual(
            by_batch["ZLB6AD5014"]["product_description"],
            "AUGPEN IV 1.2GM INJ I VIAL",
        )
        self.assertEqual(
            by_batch["26460376"]["product_description"],
            "ZOCEF 500 TAB 10'S",
        )

    def test_fix_truncated_and_exp_bleed(self):
        items = [
            {
                "product_description": "MD TAB 15'S",
                "quantity": "50",
                "unit_price": "94.28",
                "lot_batch_number": "D150H246",
                "additional_fields": {"mrp": "130.45"},
            },
            {
                "product_description": "AUGPEN IV 1.2GM INJ I",
                "quantity": "20",
                "unit_price": "69.00",
                "lot_batch_number": "ZLB6AD5014",
                "additional_fields": {"mrp": "151.30"},
            },
            {
                "product_description": "EXPJ NATUROGEST SR 300 TAB 10'S",
                "quantity": "30",
                "unit_price": "146.33",
                "lot_batch_number": "826D052",
                "additional_fields": {"mrp": "195.39"},
            },
        ]
        out = fix_singhal_medicals_product_names_from_ocr(
            items, SINGHAL_OCR, vendor="SINGHAL MEDICALS")
        by_batch = {i["lot_batch_number"]: i for i in out}
        self.assertEqual(by_batch["D150H246"]["product_description"], "DYNAPAR TAB 15'S")
        self.assertEqual(
            by_batch["ZLB6AD5014"]["product_description"],
            "AUGPEN IV 1.2GM INJ I VIAL",
        )
        self.assertEqual(
            by_batch["826D052"]["product_description"],
            "AUGMENTIN-625 DUO TAB 10'S",
        )

    def test_clean_helpers(self):
        self.assertEqual(
            _clean_singhal_medicals_product_name("EXPJ NATUROGEST SR 300"),
            "NATUROGEST SR 300",
        )
        self.assertEqual(
            _clean_singhal_medicals_product_name("ZOCEF 500 TAB 10' s"),
            "ZOCEF 500 TAB 10'S",
        )


if __name__ == "__main__":
    unittest.main()
