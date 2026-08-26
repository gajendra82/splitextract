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

    def test_restores_qty_and_price_not_pack_sch_or_mrp(self):
        ocr = """
BRUKLYN ASSOCIATES
Description Of Goods Pack Batch No. Qty Sch M.R.P Price Sch % Disc% Taxable CGST% SGST% Amount
SHR LI DM TAB 10'S 10 MHT260025G 20 8 104.06 83.24 0.00 0 1664.80 2.50 2.50 1748.04
AA ODIMONT LC 15S TAB 15 IB00280A 200 0 380.63 228.00 0.00 0 45600.00 2.50 2.50 47880.00
F- ALDACTONE 25MG 15S TAB 15 02A26006 20 0 34.90 26.59 0.00 0 531.80 2.50 2.50 558.40
ZYD BILYPSA 4MG TAB 45'S 45 IB00223A 8 0 2255.08 1718.16 0.00 0 13745.28 2.50 2.50 14432.54
MAC MACBERY XT 100ML 1 18260844A 30 0 122.72 66.27 0.00 0 1988.10 2.50 2.50 2087.50
for BRUKLYN ASSOCIATES
"""
        rows = _parse_bruklyn_associates_price_rows(ocr)
        by_name = {r["product_description"].upper(): r for r in rows}
        self.assertEqual(by_name["LI DM TAB 10'S"]["quantity"], 20.0)
        self.assertAlmostEqual(by_name["LI DM TAB 10'S"]["unit_price"], 83.24, places=2)
        self.assertEqual(by_name["ODIMONT LC 15S TAB"]["quantity"], 200.0)
        self.assertAlmostEqual(by_name["ODIMONT LC 15S TAB"]["unit_price"], 228.00, places=2)
        self.assertEqual(by_name["ALDACTONE 25MG 15S TAB"]["quantity"], 20.0)
        self.assertAlmostEqual(by_name["ALDACTONE 25MG 15S TAB"]["unit_price"], 26.59, places=2)
        self.assertEqual(by_name["BILYPSA 4MG TAB 45'S"]["quantity"], 8.0)
        self.assertAlmostEqual(by_name["BILYPSA 4MG TAB 45'S"]["unit_price"], 1718.16, places=2)

        items = [
            {
                "product_description": "LI DM TAB 10'S",
                "lot_batch_number": "MHT260025G",
                "quantity": "8",
                "unit_price": "104.06",
                "total_amount": "1748.04",
            },
            {
                "product_description": "ODIMONT LC 15S TAB",
                "lot_batch_number": "IB00280A",
                "quantity": "15",
                "unit_price": "380.63",
                "total_amount": "47880.00",
            },
            {
                "product_description": "ALDACTONE 25MG 15S TAB",
                "lot_batch_number": "02A26006",
                "quantity": "15",
                "unit_price": "34.90",
                "total_amount": "558.40",
            },
            {
                "product_description": "MACBERY XT 100ML",
                "lot_batch_number": "18260844A",
                "quantity": "30",
                "unit_price": "66.27",
                "total_amount": "2087.50",
            },
        ]
        fixed = fix_bruklyn_associates_price_from_ocr(items, ocr)
        lidm = next(i for i in fixed if "LI DM" in i["product_description"].upper())
        odi = next(i for i in fixed if "ODIMONT" in i["product_description"].upper())
        ald = next(i for i in fixed if "ALDACTONE" in i["product_description"].upper())
        mac = next(i for i in fixed if "MACBERY" in i["product_description"].upper())
        self.assertEqual(lidm["quantity"], "20")
        self.assertEqual(lidm["unit_price"], "83.24")
        self.assertEqual(lidm["total_amount"], "1748.04")
        self.assertEqual(odi["quantity"], "200")
        self.assertEqual(odi["unit_price"], "228.00")
        self.assertEqual(odi["total_amount"], "47880.00")
        self.assertEqual(ald["quantity"], "20")
        self.assertEqual(ald["unit_price"], "26.59")
        self.assertEqual(mac["quantity"], "30")
        self.assertEqual(mac["unit_price"], "66.27")


if __name__ == "__main__":
    unittest.main()
