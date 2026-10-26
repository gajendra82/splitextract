"""DROGARIA COLVALCAR / New Drug House qty/rate corrections."""
import unittest

from app import (
    fix_drogaria_colvalcar_line_items_from_ocr,
    _parse_drogaria_colvalcar_table_rows,
    _clean_drogaria_colvalcar_product_name,
)


COLVALCAR_OCR = """
New Drug House Devki- Krishna Niwas Opp.F omento E-lnvoice
DROGARIA co LVALCAR Ph OT ITS TSI 22 MAPA 8.645 1.650.226 332
Bill No.: 26-2713020
Bill Date.: 25/06/2026
TAX INVOICE
Mfr Product Name HSN Sch Batch Ex.Dt MRP Qty Rate PD% BD% Value
Products Under GST 5.00 %
GERM AEROHALE SPACER 30049099 PC BEC 1146 12/28 492.88 300 312.79 3.50 90552.71
GERM RESPIHALER INHALER 30049099 UNIT BEC 1120 10/30 315.45 300 214.14 3.50 61993.53
Taxable | CGST% OGST Amt. SGST Amt GST Amt
152546.23 | 2.50% 3813.66 | 2.50% 3813.66
Grand Total 160174.00
Discount: 6532.77 Total Items: 2 Total Oty: 600 For DROGARIA COLVALCAR
"""


class TestDrogariaColvalcarLineItems(unittest.TestCase):
    def test_parse_table_rows(self):
        rows = _parse_drogaria_colvalcar_table_rows(COLVALCAR_OCR)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["quantity"], 300)
        self.assertAlmostEqual(rows[0]["unit_price"], 312.79, places=2)
        self.assertEqual(rows[1]["quantity"], 300)
        self.assertAlmostEqual(rows[1]["unit_price"], 214.14, places=2)

    def test_mrp_mapped_as_rate_corrected(self):
        items = [
            {
                "product_description": "GERM AEROHALE SPACER",
                "quantity": "1",
                "unit_price": "492.88",
                "total_amount": "492.88",
                "lot_batch_number": "BEC 1146",
                "additional_fields": {"mrp": "492.88", "expiry_date": "12/28"},
            },
            {
                "product_description": "GERM RESPIHALER INHALER",
                "quantity": "1",
                "unit_price": "315.45",
                "total_amount": "315.45",
                "lot_batch_number": "BEC 1120",
                "additional_fields": {"mrp": "315.45", "expiry_date": "10/30"},
            },
        ]
        summary = {"total": "160174.00", "tax": "7627.32", "vendor": "New Drug House"}
        out = fix_drogaria_colvalcar_line_items_from_ocr(
            items,
            COLVALCAR_OCR,
            vendor="New Drug House",
            invoice_summary=summary,
        )
        by_desc = {i["product_description"]: i for i in out}
        self.assertIn("AEROHALE SPACER", by_desc)
        self.assertIn("RESPIHALER INHALER", by_desc)
        self.assertEqual(by_desc["AEROHALE SPACER"]["quantity"], "300")
        self.assertEqual(by_desc["RESPIHALER INHALER"]["quantity"], "300")
        self.assertEqual(by_desc["AEROHALE SPACER"]["unit_price"], "312.79")
        self.assertEqual(by_desc["RESPIHALER INHALER"]["unit_price"], "214.14")
        self.assertAlmostEqual(
            float(by_desc["AEROHALE SPACER"]["total_amount"]), 90552.71, places=1)
        self.assertAlmostEqual(
            float(by_desc["RESPIHALER INHALER"]["total_amount"]), 61993.53, places=1)

    def test_aerghale_typo_and_mfr_prefix_cleaned(self):
        self.assertEqual(
            _clean_drogaria_colvalcar_product_name("GERM AERGHALE SPACER"),
            "AEROHALE SPACER",
        )


if __name__ == "__main__":
    unittest.main()
