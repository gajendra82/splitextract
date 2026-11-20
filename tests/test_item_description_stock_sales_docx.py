"""Omnis STOCK & SALES ANALYSIS paragraphs. Does not replace the Word table parser."""

from __future__ import annotations

import io
import unittest
from pathlib import Path

from services.sales_statement_extractor import (
    _parse_docx,
    _parse_item_description_stock_sales_lines,
    extract_sales_statement,
)

FIXTURE = Path(
    "0000721532_2026_08_ZL_24_416_05092026081542 (1).docx"
)

SAMPLE = """
OMNIS LIFECARE LLP.
H-160,SUIT NO. 1-A,BSI BUSINESS PARK, SEC-63,NOIDA,201301
Phone : 8800635325 Website : www.omnislifecare.com E-Mail : info@omnislifecare.com
GSTIN : 09AAEFO7682C1Z2
STOCK & SALES ANALYSIS 01-05-2026 - 31-05-2026
ITEM DESCRIPTION RATE OPENING RECEIPT ISSUE CLOSING DUMP QTY. VALUE
HIMALAYA
ASHWAGANDA CAPS ORGANIC 30'S 328.41 24 7881.84 0 0.00 1 328.41 23 7553.43 23
BABY WIPES 48 88.09 0 0.00 0 0.00 0 0.00 0 0.00 0
TOTAL CARE BABY PANTS L 9'S 96.24 0 0.00 0 0.00 0 0.00 0 0.00 0
DIABECON TAB (FC) 60'S 120.31 30 3609.30 0 0.00 0 0.00 30 3609.30 30 DIABECON TAB DS 60'S 217.14 13 2822.82 0 0.00 0 0.00 13 2822.82 13
TOTAL 67 14313.96 0 0.00 1 328.41 66 13985.55 0
HIMALAYA ZANDRA
LIV 52 TAB 100'S 10.00 2 20.00 0 0.00 1 10.00 1 10.00 0
TOTAL 2 20.00 0 0.00 1 10.00 1 10.00 0
TOTAL 69 14333.96 0 0.00 2 338.41 67 13995.55 0
"""


class TestItemDescriptionStockSalesDocx(unittest.TestCase):
    def test_sample_groups_one_month_and_keeps_glued_rows(self):
        parsed = _parse_item_description_stock_sales_lines(
            SAMPLE.splitlines(), "omnis.docx"
        )
        self.assertIsNotNone(parsed)
        self.assertNotIn("statements", parsed)
        self.assertEqual(parsed["stockist_name"], "OMNIS LIFECARE LLP.")
        self.assertEqual(parsed["period_from"], "2026-05-01")
        self.assertEqual(parsed["period_to"], "2026-05-31")
        names = [item["product_name"] for item in parsed["line_items"]]
        self.assertEqual(
            names,
            [
                "ASHWAGANDA CAPS ORGANIC",
                "BABY WIPES",
                "TOTAL CARE BABY PANTS L",
                "DIABECON TAB (FC)",
                "DIABECON TAB DS",
                "LIV 52 TAB",
            ],
        )
        wipes = parsed["line_items"][1]
        self.assertEqual(wipes["packing"], "48")
        self.assertEqual(wipes["opening_qty"], 0)
        self.assertEqual(wipes["extra"]["rate"], 88.09)
        self.assertEqual(parsed["line_items"][0]["opening_qty"], 24)
        self.assertEqual(parsed["line_items"][0]["sales_qty"], 1)
        self.assertEqual(parsed["line_items"][0]["closing_qty"], 23)
        self.assertEqual(parsed["line_items"][3]["extra"]["company_name"], "HIMALAYA")
        self.assertEqual(parsed["line_items"][5]["extra"]["company_name"], "HIMALAYA ZANDRA")
        self.assertEqual(parsed["totals"]["sales_value"], 338.41)
        self.assertEqual(parsed["totals"]["closing_value"], 13995.55)

    def test_uploaded_omnis_file_matches_printed_grand_total(self):
        self.assertTrue(FIXTURE.is_file(), "fixture docx is missing")
        result = extract_sales_statement(FIXTURE.read_bytes(), FIXTURE.name)
        items = result["line_items"]
        self.assertGreaterEqual(len(items), 200)
        self.assertNotIn("statements", result)
        self.assertEqual(result["stockist_name"], "OMNIS LIFECARE LLP.")
        self.assertEqual(result["company_name"], "HIMALAYA")
        self.assertEqual(result["period_from"], "2026-05-01")
        self.assertEqual(result["period_to"], "2026-05-31")
        self.assertNotEqual(result["period_from"][:7], "2026-09")
        self.assertEqual(result["totals"]["sales_value"], 19070.56)
        self.assertEqual(result["totals"]["closing_value"], 171339.84)
        self.assertEqual(sum(item["opening_qty"] for item in items), 1161)
        self.assertEqual(sum(item["sales_qty"] for item in items), 105)
        self.assertEqual(sum(item["closing_qty"] for item in items), 1056)
        self.assertAlmostEqual(sum(item["sales_value"] for item in items), 19070.56, places=2)
        self.assertAlmostEqual(
            sum(item["closing_value"] for item in items), 171339.84, places=2
        )
        first = items[0]
        self.assertEqual(first["product_name"], "ASHWAGANDA CAPS ORGANIC")
        self.assertEqual(first["packing"], "30'S")
        self.assertEqual(first["sales_value"], 328.41)
        self.assertIn("VASAKA TAB", [item["product_name"] for item in items])
        self.assertIn("DIABECON TAB (FC)", [item["product_name"] for item in items])
        self.assertEqual(
            result["totals"]["extra"]["extraction_method"],
            "item_description_stock_sales_analysis_docx",
        )

    def test_existing_word_table_parser_still_runs(self):
        from docx import Document

        doc = Document()
        doc.add_paragraph("Stock and Sales Detail Report")
        table = doc.add_table(rows=2, cols=20)
        headers = [
            "Sl.No",
            "Item",
            "Op.Qty",
            "Op.Val",
            "P.Qty",
            "P.Sch",
            "P.Val",
            "S.Qty",
            "S.Sch",
            "S.Val",
            "Br.S.Qty",
            "Br.S.Val",
            "Cr.Qty",
            "Cr.Sch.Qty",
            "Cr.Val",
            "Db.Qty",
            "Db.Sch.Qty",
            "Db.Val",
            "Cl.Qty",
            "Cl.Val",
        ]
        values = [
            "1",
            "LIV 52",
            "10",
            "0",
            "0",
            "0",
            "0",
            "2",
            "0",
            "50",
            "0",
            "0",
            "0",
            "0",
            "0",
            "0",
            "0",
            "0",
            "8",
            "80",
        ]
        for index, header in enumerate(headers):
            table.rows[0].cells[index].text = header
        for index, value in enumerate(values):
            table.rows[1].cells[index].text = value
        buffer = io.BytesIO()
        doc.save(buffer)
        result = _parse_docx(buffer.getvalue(), "detail.docx")
        self.assertEqual(
            result["totals"]["extra"].get("extraction_method"),
            "docx_stock_sales_detail",
        )
        self.assertEqual(result["line_items"][0]["product_name"], "LIV 52")
        self.assertEqual(result["line_items"][0]["opening_qty"], 10)
        self.assertEqual(result["line_items"][0]["sales_qty"], 2)
        self.assertEqual(result["line_items"][0]["sales_value"], 50)
        self.assertEqual(result["line_items"][0]["closing_qty"], 8)
        self.assertEqual(result["line_items"][0]["closing_value"], 80)


if __name__ == "__main__":
    unittest.main()
