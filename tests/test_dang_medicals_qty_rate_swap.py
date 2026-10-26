"""DANG MEDICALS Qty+Free table: do not leave Rate in Quantity for integer rates."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import (  # noqa: E402
    _extract_line_items_for_validation,
    _ocr_suggests_dang_medicals_qty_free_table,
    _parse_dang_medicals_qty_rate_rows,
    enforce_schema,
    fix_dang_medicals_swapped_qty_rate_from_ocr,
    fix_swapped_quantity_unit_price,
)


DANG_OCR = """
DANG MEDICALS
BILL No:- 26-27/1412 GST INVOICE BILL DATE:- 09/06/2026
SNO. Qty+Free Pack ItemName BATCH EXP. HSN O.MRP MRP Rate Disc CGST SGST AMOUNT
1 10.00+0.00 1UNIT FORGLYN PLUS INH. IB00881A 03-28 300490 0.00 1,028.02 783.25 0.00 2.50 2.50 8224.12
6 16.00+0.00 5ML DILZEM INJ K222N001 06-27 300490 0.00 18.67 14.23 0.00 2.50 2.50 239.06
7 15.00+0.00 10S PREGATOR CAP PGRS0100 07-27 300490 0.00 231.00 176.00 0.00 2.50 2.50 2772.00
8 2.00+0.00 1S METATOP N/S MA05965A 11-27 300490 0.00 621.07 437.20 0.00 2.50 2.50 918.12
TOTAL AMOUNT 37,305.00
"""

JACKSON_OCR = """
JACKSON MEDICALS Inv.No. D4151
AMLOPIN 5MG TAB 10 20.32 203.20
"""


class TestDangMedicalsQtyRateSwap(unittest.TestCase):
    def test_detects_dang_not_other_formats(self):
        self.assertTrue(_ocr_suggests_dang_medicals_qty_free_table(DANG_OCR))
        self.assertFalse(_ocr_suggests_dang_medicals_qty_free_table(JACKSON_OCR))

    def test_parses_qty_free_and_rate(self):
        rows = _parse_dang_medicals_qty_rate_rows(DANG_OCR)
        preg = next(r for r in rows if "PREGATOR" in r["product_description"].upper())
        self.assertEqual(preg["quantity"], 15.0)
        self.assertAlmostEqual(preg["unit_price"], 176.0, places=2)

    def test_unswaps_only_inverted_row(self):
        items = [
            {
                "product_description": "FORGLYN PLUS INH.",
                "quantity": "10.00",
                "unit_price": "783.25",
                "total_amount": "8224.12",
            },
            {
                "product_description": "PREGATOR CAP",
                "quantity": "176",
                "unit_price": "15.00",
                "total_amount": "2772.00",
            },
            {
                "product_description": "DILZEM INJ",
                "quantity": "16.00",
                "unit_price": "14.23",
                "total_amount": "239.06",
            },
        ]
        fixed = fix_dang_medicals_swapped_qty_rate_from_ocr(items, DANG_OCR)
        forg = next(i for i in fixed if "FORGLYN" in i["product_description"].upper())
        preg = next(i for i in fixed if "PREGATOR" in i["product_description"].upper())
        dilz = next(i for i in fixed if "DILZEM" in i["product_description"].upper())
        self.assertEqual(forg["quantity"], "10.00")
        self.assertEqual(forg["unit_price"], "783.25")
        self.assertEqual(preg["quantity"], "15.00")
        self.assertEqual(preg["unit_price"], "176")
        self.assertEqual(preg["total_amount"], "2772.00")
        self.assertEqual(dilz["quantity"], "16.00")
        self.assertEqual(dilz["unit_price"], "14.23")

    def test_shared_pattern1_swaps_integer_rate_then_enforce_unswaps(self):
        item = {
            "product_description": "PREGATOR CAP",
            "quantity": "15.00",
            "unit_price": "176.00",
            "total_amount": "2772.00",
        }
        swapped = fix_swapped_quantity_unit_price(dict(item))
        self.assertEqual(str(swapped["quantity"]), "176")
        self.assertEqual(swapped["unit_price"], "15.00")

        payload = {
            "data": {
                "invoice_summary": {
                    "vendor": "DANG MEDICALS",
                    "customer": "SARASWATHI INSTITUTE",
                    "invoice_no": "26-27/1412",
                    "total": "37305.00",
                },
                "line_items": {
                    "items": [
                        dict(item),
                        {
                            "product_description": "FORGLYN PLUS INH.",
                            "quantity": "10.00",
                            "unit_price": "783.25",
                            "total_amount": "8224.12",
                        },
                    ],
                    "count": 2,
                },
                "ocr_text": DANG_OCR,
            }
        }
        out = _extract_line_items_for_validation(enforce_schema(payload))
        preg = next(i for i in out if "PREGATOR" in i["product_description"].upper())
        forg = next(i for i in out if "FORGLYN" in i["product_description"].upper())
        self.assertEqual(float(preg["quantity"]), 15.0)
        self.assertAlmostEqual(float(preg["unit_price"]), 176.0, places=2)
        self.assertEqual(preg["total_amount"], "2772.00")
        self.assertEqual(float(forg["quantity"]), 10.0)
        self.assertAlmostEqual(float(forg["unit_price"]), 783.25, places=2)

    def test_restores_18pct_gst_rate_without_changing_qty(self):
        """BROWN FACEWASH: AMOUNT/qty is not the Rate column (18% GST)."""
        ocr = DANG_OCR + (
            "7 20.00+4.00 100ML REOWN FACEWASH BSC51707 11-28 300490 "
            "370.00 390.00 264.42 0.00 9.00 9.00 6240.32\n"
        )
        items = [
            {
                "product_description": "REOWN FACEWASH",
                "lot_batch_number": "BSC51707",
                "quantity": "20.00",
                "unit_price": "312.02",
                "total_amount": "6240.32",
            },
            {
                "product_description": "FORGLYN PLUS INH.",
                "quantity": "10.00",
                "unit_price": "783.25",
                "total_amount": "8224.12",
            },
        ]
        fixed = fix_dang_medicals_swapped_qty_rate_from_ocr(items, ocr)
        face = next(i for i in fixed if "FACEWASH" in i["product_description"].upper())
        forg = next(i for i in fixed if "FORGLYN" in i["product_description"].upper())
        self.assertEqual(face["product_description"], "REOWN FACEWASH")
        self.assertEqual(face["quantity"], "20.00")
        self.assertEqual(face["unit_price"], "264.42")
        self.assertEqual(face["total_amount"], "6240.32")
        self.assertEqual(forg["quantity"], "10.00")
        self.assertEqual(forg["unit_price"], "783.25")

        payload = {
            "data": {
                "invoice_summary": {
                    "vendor": "DANG MEDICALS",
                    "customer": "SARASWATHI INSTITUTE",
                    "invoice_no": "26-27/1744",
                    "total": "64732.00",
                },
                "line_items": {"items": [dict(items[0]), dict(items[1])], "count": 2},
                "ocr_text": ocr,
            }
        }
        out = _extract_line_items_for_validation(enforce_schema(payload))
        face = next(i for i in out if "FACEWASH" in i["product_description"].upper())
        self.assertEqual(float(face["quantity"]), 20.0)
        self.assertAlmostEqual(float(face["unit_price"]), 264.42, places=2)
        self.assertEqual(face["total_amount"], "6240.32")

    def test_does_not_touch_other_formats(self):
        items = [{
            "product_description": "AMLOPIN 5MG TAB",
            "quantity": "10",
            "unit_price": "20.32",
            "total_amount": "203.20",
        }]
        fixed = fix_dang_medicals_swapped_qty_rate_from_ocr(items, JACKSON_OCR)
        self.assertEqual(fixed[0]["quantity"], "10")
        self.assertEqual(fixed[0]["unit_price"], "20.32")


if __name__ == "__main__":
    unittest.main()
