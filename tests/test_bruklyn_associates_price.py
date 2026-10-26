"""BRUKLYN ASSOCIATES: Price column is Rate, not AMOUNT/qty on 18% GST rows."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import (  # noqa: E402
    _extract_line_items_for_validation,
    _ocr_suggests_bruklyn_associates_price_table,
    _parse_bruklyn_associates_price_rows,
    enforce_schema,
    fix_bruklyn_associates_price_from_ocr,
)

BRUKLYN_OCR = """
BRUKLYN ASSOCIATES
Invoice No. : 260027300101569
Description Of Goods Pack Batch No. Qty Sch M.R.P Price Sch % Disc% Taxable CGST% SGST% Amount
ITO VALZAAR 80MG TAB 15 2HN9N002 10 0 371.40 226.40 0.00 0 2264.00 2.50 2.50 2377.20
TOR RITCH LOTION 75 ML 1 CJ332 10 0 435.00 294.92 0.00 0 2949.20 9.00 9.00 3480.06
MAC THYROX 150 (120S TAB) 120 16251922A 5 0 217.92 116.56 0.00 0 582.80 2.50 2.50 611.94
for BRUKLYN ASSOCIATES
Grand Total : 13489.00
"""

JACKSON_OCR = """
JACKSON MEDICALS Inv.No. D4151
AMLOPIN 5MG TAB 10 20.32 203.20
"""


class TestBruklynAssociatesPrice(unittest.TestCase):
    def test_detects_bruklyn_not_other_formats(self):
        self.assertTrue(_ocr_suggests_bruklyn_associates_price_table(BRUKLYN_OCR))
        self.assertFalse(_ocr_suggests_bruklyn_associates_price_table(JACKSON_OCR))

    def test_parses_price_column(self):
        rows = _parse_bruklyn_associates_price_rows(BRUKLYN_OCR)
        ritch = next(r for r in rows if "RITCH" in r["product_description"].upper())
        valz = next(r for r in rows if "VALZAAR" in r["product_description"].upper())
        self.assertEqual(ritch["quantity"], 10.0)
        self.assertAlmostEqual(ritch["unit_price"], 294.92, places=2)
        self.assertAlmostEqual(valz["unit_price"], 226.40, places=2)

    def test_restores_18pct_price_only(self):
        items = [
            {
                "product_description": "VALZAAR 80MG TAB",
                "lot_batch_number": "2HN9N002",
                "quantity": "10",
                "unit_price": "226.40",
                "total_amount": "2377.20",
            },
            {
                "product_description": "RITCH LOTION 75 ML",
                "lot_batch_number": "CJ332",
                "quantity": "10",
                "unit_price": "348.01",
                "total_amount": "3480.06",
            },
        ]
        fixed = fix_bruklyn_associates_price_from_ocr(items, BRUKLYN_OCR)
        valz = next(i for i in fixed if "VALZAAR" in i["product_description"].upper())
        ritch = next(i for i in fixed if "RITCH" in i["product_description"].upper())
        self.assertEqual(valz["quantity"], "10")
        self.assertEqual(valz["unit_price"], "226.40")
        self.assertEqual(valz["total_amount"], "2377.20")
        self.assertEqual(ritch["quantity"], "10")
        self.assertEqual(ritch["unit_price"], "294.92")
        self.assertEqual(ritch["total_amount"], "3480.06")
        self.assertEqual(ritch["product_description"], "RITCH LOTION 75 ML")

        payload = {
            "data": {
                "invoice_summary": {
                    "vendor": "BRUKLYN ASSOCIATES",
                    "customer": "RAJAGIRI HEALTH CARE AND EDUCATION TRUST",
                    "invoice_no": "260027300101569",
                    "total": "13489.00",
                },
                "line_items": {
                    "items": [
                        {
                            "product_description": "RITCH LOTION 75 ML",
                            "lot_batch_number": "CJ332",
                            "quantity": "10",
                            "unit_price": "294.92",
                            "total_amount": "3480.06",
                        },
                        {
                            "product_description": "VALZAAR 80MG TAB",
                            "lot_batch_number": "2HN9N002",
                            "quantity": "10",
                            "unit_price": "226.40",
                            "total_amount": "2377.20",
                        },
                    ],
                    "count": 2,
                },
                "ocr_text": BRUKLYN_OCR,
            }
        }
        out = _extract_line_items_for_validation(enforce_schema(payload))
        ritch = next(i for i in out if "RITCH" in i["product_description"].upper())
        valz = next(i for i in out if "VALZAAR" in i["product_description"].upper())
        self.assertEqual(float(ritch["quantity"]), 10.0)
        self.assertAlmostEqual(float(ritch["unit_price"]), 294.92, places=2)
        self.assertEqual(ritch["total_amount"], "3480.06")
        self.assertAlmostEqual(float(valz["unit_price"]), 226.40, places=2)

    def test_does_not_touch_other_formats(self):
        items = [{
            "product_description": "AMLOPIN 5MG TAB",
            "quantity": "10",
            "unit_price": "20.32",
            "total_amount": "203.20",
        }]
        fixed = fix_bruklyn_associates_price_from_ocr(items, JACKSON_OCR)
        self.assertEqual(fixed[0]["unit_price"], "20.32")


if __name__ == "__main__":
    unittest.main()
