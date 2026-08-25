"""ALARIC ENTERPRISES / SmartPharma360: keep Qty when unit_price is MRP."""
import unittest

from app import (
    _apply_alaric_enterprises_multipage_vision_line_items,
    _extract_line_items_for_validation,
    _ocr_suggests_alaric_enterprises_smartpharma360,
    fix_mrp_as_unit_price,
)


ALARIC_OCR = """
ALARIC ENTERPRISES TAX INVOICE
Inv. No. INDEL2627008380
SNO HSN MFG Product Name Pack Batch No Expiry Qty Free Rate MRP Old MRP GST% Amount Disc% Net
Powered by Smartpharma360
"""

JACKSON_OCR = """
JACKSON MEDICALS Inv.No. D4151 ST.THOMAS HOSPITAL
AMLOPIN 5MG TAB 10 20.32 203.20
"""


class TestAlaricEnterprisesQtyRate(unittest.TestCase):
    def test_detects_alaric_not_other_formats(self):
        self.assertTrue(_ocr_suggests_alaric_enterprises_smartpharma360(ALARIC_OCR))
        self.assertTrue(
            _ocr_suggests_alaric_enterprises_smartpharma360(
                "", vendor="ALARIC ENTERPRISES"
            )
        )
        self.assertFalse(_ocr_suggests_alaric_enterprises_smartpharma360(JACKSON_OCR))

    def test_zodox_keeps_qty_and_restores_rate_from_amount(self):
        """Qty 20 × Rate 110 = 2200; Vision used MRP 169.58 as rate."""
        item = {
            "product_description": "ZODOX 10MG INJECTION (1s)",
            "quantity": "20",
            "unit_price": "169.58",
            "total_amount": "2200.00",
        }
        out = fix_mrp_as_unit_price(
            item, vendor="ALARIC ENTERPRISES", ocr_text=ALARIC_OCR
        )
        self.assertEqual(str(out["quantity"]), "20")
        self.assertAlmostEqual(float(out["unit_price"]), 110.00, places=2)
        self.assertAlmostEqual(float(out["additional_fields"]["mrp"]), 169.58, places=2)

    def test_eleftha_still_replaces_mrp_with_rate(self):
        item = {
            "product_description": "ELEFTHA 150MG INJECTION (1s)",
            "quantity": "4",
            "unit_price": "6491.72",
            "total_amount": "10400.00",
        }
        out = fix_mrp_as_unit_price(
            item, vendor="ALARIC ENTERPRISES", ocr_text=ALARIC_OCR
        )
        self.assertEqual(str(out["quantity"]), "4")
        self.assertAlmostEqual(float(out["unit_price"]), 2600.00, places=2)

    def test_non_alaric_qty_misread_still_applies(self):
        item = {
            "product_description": "ZODOX 10MG INJECTION (1s)",
            "quantity": "20",
            "unit_price": "169.58",
            "total_amount": "2200.00",
        }
        out = fix_mrp_as_unit_price(item, vendor="OTHER", ocr_text=JACKSON_OCR)
        self.assertEqual(str(out["quantity"]), "13")
        self.assertAlmostEqual(float(out["unit_price"]), 169.58, places=2)

    def test_multipage_merges_page2_vision_items_when_ocr_empty(self):
        """INDEL2627008376: SN 17–23 live on page 2; grouping kept only page 1."""
        page1_items = [
            {
                "product_description": "BIOVORIN 50MG INJECTION",
                "lot_batch_number": "B1",
                "quantity": "50",
                "unit_price": "53.00",
                "total_amount": "2650.00",
            },
            {
                "product_description": "DOCEAQUALIP 80MG INJECTION (1s)",
                "lot_batch_number": "D80",
                "quantity": "3",
                "unit_price": "8200.00",
                "total_amount": "24600.00",
            },
        ]
        page2_items = [
            {
                "product_description": "DOCEAQUALIP 20MG INJECTION (1s)",
                "lot_batch_number": "D20",
                "quantity": "6",
                "unit_price": "2100.00",
                "total_amount": "12600.00",
            },
            {
                "product_description": "ZODOX 50MG INJECTION (1s)",
                "lot_batch_number": "Z50",
                "quantity": "30",
                "unit_price": "460.00",
                "total_amount": "13800.00",
            },
            {
                "product_description": "TRASTUREL 440MG INJECTION",
                "lot_batch_number": "T440",
                "quantity": "1",
                "unit_price": "40919.90",
                "total_amount": "40919.90",
            },
        ]
        group = {
            "pages": [0, 1],
            "ocr_text": "",
            "extracted_data": {
                "data": {
                    "invoice_summary": {"vendor": "ALARIC ENTERPRISES"},
                    "line_items": {"items": list(page1_items), "count": 2},
                }
            },
        }
        page_results = [
            {"ocr_text": "", "full_data": group["extracted_data"]},
            {
                "ocr_text": "",
                "full_data": {
                    "vendor": "ALARIC ENTERPRISES",
                    "line_items": page2_items,
                },
            },
        ]
        self.assertTrue(
            _apply_alaric_enterprises_multipage_vision_line_items(group, page_results)
        )
        merged = _extract_line_items_for_validation(group["extracted_data"])
        names = [i["product_description"] for i in merged]
        self.assertEqual(len(merged), 5)
        self.assertIn("DOCEAQUALIP 20MG INJECTION (1s)", names)
        self.assertIn("ZODOX 50MG INJECTION (1s)", names)
        self.assertIn("TRASTUREL 440MG INJECTION", names)

    def test_multipage_merge_skips_other_vendors(self):
        group = {
            "pages": [0, 1],
            "ocr_text": JACKSON_OCR,
            "extracted_data": {
                "data": {
                    "invoice_summary": {"vendor": "JACKSON MEDICALS"},
                    "line_items": {"items": [
                        {"product_description": "AMLOPIN", "quantity": "10",
                         "total_amount": "203.20"},
                    ], "count": 1},
                }
            },
        }
        page_results = [
            {"ocr_text": JACKSON_OCR, "full_data": group["extracted_data"]},
            {"ocr_text": JACKSON_OCR, "full_data": {
                "line_items": [
                    {"product_description": "SUPRADYN", "quantity": "2",
                     "total_amount": "50.00"},
                ]
            }},
        ]
        self.assertFalse(
            _apply_alaric_enterprises_multipage_vision_line_items(group, page_results)
        )

