"""STERLING PHARMA: Free rows must not inherit the paid sibling Rate."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import (  # noqa: E402
    _extract_line_items_for_validation,
    enforce_schema,
    fill_missing_price_data,
    fix_sterling_pharma_free_rate_items,
    ocr_suggests_sterling_pharma_free_rate_table,
    ocr_suggests_sterling_pharma_table,
)


STERLING_FREE_OCR = """
STERLING PHARMA
ORIGINAL BUYER'S COPY
Invoice No. SPH7176 Date 22/06/2026
INDIRA GANDHI CO-OP HOSPITAL
DESCRIPTION  M.R.P  Rate  QTY  Amount
AMLONG TAB 2.5MG **  28.65  21.83  5  109.15
DOLO TAB 650 **  32.12  24.47  120  2936.40
DOLO TAB 650 **  32.12  Free  30
EBAST DC TAB  136.00  103.62  10  1036.20
EBAST DC TAB  136.00  Free  2
OTRIVIN OXY FAST RELIEF  120.79  95.87  100  9587.00
OTRIVIN OXY FAST RELIEF  120.79  Free  5
"""

CU_CODE_OCR = """
STERLING PHARMA
Cu. Code DESCRIPTION BATCH
JAGIS TABLET IA01000A
"""


class TestSterlingPharmaFreeRate(unittest.TestCase):
    def test_detects_sph_free_layout_not_cu_code(self):
        self.assertTrue(ocr_suggests_sterling_pharma_free_rate_table(
            STERLING_FREE_OCR, "STERLING PHARMA", "SPH7176"))
        self.assertFalse(ocr_suggests_sterling_pharma_free_rate_table(
            CU_CODE_OCR, "STERLING PHARMA", "1234"))
        self.assertFalse(ocr_suggests_sterling_pharma_table(STERLING_FREE_OCR))

    def test_clears_rate_only_on_free_duplicates(self):
        items = [
            {
                "product_description": "DOLO TAB 650 **",
                "quantity": "120",
                "unit_price": "24.47",
                "total_amount": "3083.22",
                "lot_batch_number": "DOBS4399",
                "additional_fields": {"free_quantity": "0", "mrp": "32.12"},
            },
            {
                "product_description": "DOLO TAB 650 **",
                "quantity": "30",
                "unit_price": "24.47",
                "total_amount": "734.10",
                "lot_batch_number": "DOBS4399",
                "additional_fields": {"free_quantity": "30", "mrp": "32.12"},
            },
            {
                "product_description": "AMLONG TAB 2.5MG **",
                "quantity": "5",
                "unit_price": "21.83",
                "total_amount": "114.61",
                "lot_batch_number": "AMTS0012",
                "additional_fields": {"free_quantity": "0", "mrp": "28.65"},
            },
        ]
        out = fix_sterling_pharma_free_rate_items(
            items, STERLING_FREE_OCR, "STERLING PHARMA", "SPH7176")
        dolo_paid = next(
            i for i in out
            if i["lot_batch_number"] == "DOBS4399" and str(i["quantity"]) == "120"
        )
        dolo_free = next(
            i for i in out
            if i["lot_batch_number"] == "DOBS4399" and str(i["quantity"]) == "30"
        )
        amlong = next(i for i in out if "AMLONG" in i["product_description"])
        self.assertEqual(dolo_paid["unit_price"], "24.47")
        self.assertEqual(dolo_paid["total_amount"], "3083.22")
        self.assertEqual(dolo_paid["quantity"], "120")
        self.assertEqual(dolo_free["quantity"], "30")
        self.assertEqual(dolo_free["unit_price"], "0.00")
        self.assertEqual(dolo_free["total_amount"], "0.00")
        self.assertEqual(amlong["unit_price"], "21.83")
        self.assertEqual(amlong["total_amount"], "114.61")

    def test_fill_missing_then_enforce_clears_copied_free_rate(self):
        items = [
            {
                "product_description": "EBAST DC TAB",
                "quantity": "10",
                "unit_price": "103.62",
                "total_amount": "1088.00",
                "lot_batch_number": "EBDS0063",
                "additional_fields": {"free_quantity": "0", "mrp": "136.00"},
            },
            {
                "product_description": "EBAST DC TAB",
                "quantity": "2",
                "unit_price": None,
                "total_amount": None,
                "lot_batch_number": "EBDS0063",
                "additional_fields": {"free_quantity": "2", "mrp": "136.00"},
            },
        ]
        filled = fill_missing_price_data([dict(x) if x else x for x in items])
        self.assertIsNotNone(filled[1].get("unit_price"))
        payload = {
            "data": {
                "invoice_summary": {
                    "vendor": "STERLING PHARMA",
                    "customer": "INDIRA GANDHI CO-OP HOSPITAL",
                    "invoice_no": "SPH7176",
                    "total": "48784.06",
                },
                "line_items": {"items": items, "count": 2},
                "ocr_text": STERLING_FREE_OCR,
            }
        }
        out = _extract_line_items_for_validation(enforce_schema(payload))
        paid = next(i for i in out if str(i.get("quantity")) in ("10", "10.00"))
        free = next(i for i in out if str(i.get("quantity")) in ("2", "2.00"))
        self.assertAlmostEqual(float(paid["unit_price"]), 103.62, places=2)
        self.assertEqual(paid["total_amount"], "1088.00")
        self.assertEqual(float(free["unit_price"]), 0.0)
        self.assertEqual(float(free["total_amount"]), 0.0)


if __name__ == "__main__":
    unittest.main()
