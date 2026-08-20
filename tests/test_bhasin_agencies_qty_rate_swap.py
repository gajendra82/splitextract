"""BHASIN AGENCIES: unswap integer RATE values that Pattern 1 put in Quantity."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import (  # noqa: E402
    _extract_line_items_for_validation,
    _parse_bhasin_agencies_qty_rate_rows,
    enforce_schema,
    fix_bhasin_agencies_swapped_qty_rate_from_ocr,
    fix_swapped_quantity_unit_price,
    ocr_suggests_bhasin_agencies,
    ocr_suggests_life_line_care,
)


BHASIN_OCR = """
GST NO. 03AAQFB4135K1ZG TAX INVOICE DUPLICATE COPY
BHASIN AGENCIES
WHOLESALE CHEMISTS & DRUGGISTS
M/S : AMAR PHARMACY PATIALA BILL NO. : GST-14779 DATE : 23/06/2026,01:03 PM
SN PACK ITEM NAME BATCH EXP. QTY. FREE RATE CD TD MRP AMOUNT
NAME IGST CODE
1 LINUX IN1FXI1N0A EPITRA 750MG TAB D2512043LLB 11/28 30.00 9.00 162.37 4.00 0.00 202.96 %5 30049082 4871.10
2 INSCOL 1 MINIVIAL A 4ML AF-25-229 08/27 20.00 80.00 4.00 16.66 105.00 5 210690 1600.00
3 ALKEM PE1NXT1A5CARDEONEP M TAB 26441243 03/28 40.00 289.49 4.00 0.00 379.95 5 300490 11579.60
4 ZYDUS RE1SXP1I5CAROEDIMONT LC KID TABLET IA01833A 10/27 10.00 193.62 4.00 9.09 254.10 5 300490 1936.20
5 SANOFI C1NXS10 BUSCOGAST TAB BSA26013 01/29 40.00 34.88 4.00 0.00 45.78 5 300490 1395.20
6 ZYDUS AE1RXO1F0ORCMEUCOTAB ET TABLET SGZ0060 01/28 10.00 297.35 4.00 9.09 390.23 5 300490 2973.50
7 WIN MEDI1CXA1R3E. 8P1MV OTGV MILCTODL SACHET 13.81 GM# C02026 02/28 160.00 48.93 4.00 0.00 64.22 5 30049099 7828.64
NET AMOUNT: 31723.00
"""

LIFE_LINE_OCR = """
LIFE LINE CARE TAX INVOICE
PRODUCT DESCRIPTION PACK BATCH QTY FREE RATE
1 30049099 KABILYTE 500 UNIT 82WB689602 01-28 60.00 0.00 151.43
TOTAL ITEM:- 7
"""


class TestBhasinAgenciesQtyRateSwap(unittest.TestCase):
    def test_detects_bhasin_not_other_formats(self):
        self.assertTrue(ocr_suggests_bhasin_agencies(BHASIN_OCR))
        self.assertFalse(ocr_suggests_bhasin_agencies(LIFE_LINE_OCR))
        self.assertFalse(ocr_suggests_life_line_care(BHASIN_OCR))

    def test_parses_multiline_wrapped_ocr(self):
        multiline = """
BHASIN AGENCIES
WHOLESALE CHEMISTS & DRUGGISTS
ITEM NAME
BATCH
EXP.
QTY.
RATE
AMOUNT
2
INSCOL 1
MINIVIAL A 4ML
AF-25-229
08/27
20.00
80.00
4.00 16.66
105.00
5 210690
1600.00
"""
        rows = _parse_bhasin_agencies_qty_rate_rows(multiline)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["lot_batch_number"], "AF-25-229")
        self.assertEqual(rows[0]["quantity"], 20.0)
        self.assertAlmostEqual(rows[0]["unit_price"], 80.0, places=2)

    def test_parses_qty_rate_with_and_without_free(self):
        rows = _parse_bhasin_agencies_qty_rate_rows(BHASIN_OCR)
        self.assertEqual(len(rows), 7)
        mini = next(r for r in rows if r["lot_batch_number"] == "AF-25-229")
        self.assertEqual(mini["quantity"], 20.0)
        self.assertAlmostEqual(mini["unit_price"], 80.0, places=2)
        epit = next(r for r in rows if r["lot_batch_number"] == "D2512043LLB")
        self.assertEqual(epit["quantity"], 30.0)
        self.assertAlmostEqual(epit["unit_price"], 162.37, places=2)

    def test_unswaps_only_minivial(self):
        items = [
            {
                "product_description": "EPITRA 750MG TAB",
                "quantity": "30.00",
                "unit_price": "162.37",
                "total_amount": "4871.10",
                "lot_batch_number": "D2512043LLB",
            },
            {
                "product_description": "MINIVIAL A 4ML",
                "quantity": "80",
                "unit_price": "20.00",
                "total_amount": "1600.00",
                "lot_batch_number": "AF-25-229",
            },
            {
                "product_description": "BUSCOGAST TAB",
                "quantity": "40.00",
                "unit_price": "34.88",
                "total_amount": "1395.20",
                "lot_batch_number": "BSA26013",
            },
        ]
        fixed = fix_bhasin_agencies_swapped_qty_rate_from_ocr(items, BHASIN_OCR)
        mini = next(i for i in fixed if "MINIVIAL" in i["product_description"].upper())
        epit = next(i for i in fixed if "EPITRA" in i["product_description"].upper())
        busc = next(i for i in fixed if "BUSCOGAST" in i["product_description"].upper())
        self.assertEqual(mini["quantity"], "20.00")
        self.assertEqual(mini["unit_price"], "80")
        self.assertEqual(mini["total_amount"], "1600.00")
        self.assertEqual(epit["quantity"], "30.00")
        self.assertEqual(epit["unit_price"], "162.37")
        self.assertEqual(busc["quantity"], "40.00")
        self.assertEqual(busc["unit_price"], "34.88")

    def test_shared_pattern1_swaps_then_enforce_unswaps(self):
        item = {
            "product_description": "MINIVIAL A 4ML",
            "quantity": "20.00",
            "unit_price": "80.00",
            "total_amount": "1600.00",
            "lot_batch_number": "AF-25-229",
        }
        swapped = fix_swapped_quantity_unit_price(dict(item))
        self.assertEqual(str(swapped["quantity"]), "80")
        self.assertEqual(swapped["unit_price"], "20.00")

        payload = {
            "data": {
                "invoice_summary": {
                    "vendor": "BHASIN AGENCIES",
                    "customer": "AMAR PHARMACY PATIALA",
                    "invoice_no": "GST-14779",
                    "total": "31723.00",
                },
                "line_items": {
                    "items": [
                        dict(item),
                        {
                            "product_description": "EPITRA 750MG TAB",
                            "quantity": "30.00",
                            "unit_price": "162.37",
                            "total_amount": "4871.10",
                            "lot_batch_number": "D2512043LLB",
                        },
                    ],
                    "count": 2,
                },
                "ocr_text": BHASIN_OCR,
            }
        }
        out = _extract_line_items_for_validation(enforce_schema(payload))
        mini = next(i for i in out if "MINIVIAL" in i["product_description"].upper())
        epit = next(i for i in out if "EPITRA" in i["product_description"].upper())
        self.assertEqual(float(mini["quantity"]), 20.0)
        self.assertAlmostEqual(float(mini["unit_price"]), 80.0, places=2)
        self.assertEqual(mini["total_amount"], "1600.00")
        self.assertEqual(float(epit["quantity"]), 30.0)
        self.assertAlmostEqual(float(epit["unit_price"]), 162.37, places=2)


if __name__ == "__main__":
    unittest.main()
