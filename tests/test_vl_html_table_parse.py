"""Unit tests for PaddleOCR-VL HTML table → sales-statement parse."""

from __future__ import annotations

import unittest
from unittest.mock import patch

SAMPLE_VL_HTML = """
<table>
<tr><th>PRODUCT DESCRIPTION</th><th>OPENING STOCK</th><th>OPENING VALUE</th>
<th>RECEIVE QUANTITY</th><th>RECEIVE VALUE</th><th>ISSUE QUANTITY</th>
<th>ISSUE VALUE</th><th>CLOSING STOCK</th><th>CLOSING VALUE</th>
<th>LAST MONTH SALE</th><th>EXPIRY STOCK</th></tr>
<tr><td>BONNISAN DROPS 30ML</td><td>24</td><td>1200.00</td><td>0</td><td>0</td>
<td>7</td><td>507.69</td><td>17</td><td>848.61</td><td>0</td><td>0</td></tr>
<tr><td>HIORA-K TOOTHPASTE 50G</td><td>10</td><td>500.00</td><td>2</td><td>100.00</td>
<td>3</td><td>150.00</td><td>9</td><td>450.00</td><td>1</td><td>0</td></tr>
<tr><td>TOTAL</td><td>649</td><td>79061.49</td><td>0</td><td>0</td>
<td>106</td><td>13266.61</td><td>543</td><td>67166.75</td><td>0</td><td>0</td></tr>
</table>
"""


class ParseVlHtmlTableTests(unittest.TestCase):
    def test_parses_product_and_qty_fields(self):
        from services.vl_html_table import EXTRACTION_METHOD, parse_vl_html_table

        result = parse_vl_html_table(SAMPLE_VL_HTML, filename="premier.jpg")
        extra = (result.get("totals") or {}).get("extra") or {}
        self.assertEqual(extra.get("extraction_method"), EXTRACTION_METHOD)
        self.assertTrue(extra.get("table_available"))
        self.assertTrue(extra.get("totals_row_detected"))

        items = result.get("line_items") or []
        self.assertEqual(len(items), 2)
        first = items[0]
        self.assertEqual(first["product_name"], "BONNISAN DROPS 30ML")
        self.assertEqual(first["opening_qty"], 24)
        self.assertEqual(first["sales_qty"], 7)
        self.assertEqual(first["closing_qty"], 17)
        self.assertEqual(first["receive_qty"], 0)
        self.assertEqual(first["receipts_qty"], 0)  # schema alias
        self.assertEqual(first["opening_value"], 1200.0)
        self.assertEqual(first["sales_value"], 507.69)
        self.assertEqual(first["closing_value"], 848.61)
        self.assertEqual(first["extra"].get("last_month_sale"), 0)
        self.assertEqual(first["extra"].get("expiry_stock"), 0)

        # TOTAL row is not a product line.
        names = {i["product_name"] for i in items}
        self.assertNotIn("TOTAL", names)
        totals = result.get("totals") or {}
        self.assertEqual(totals.get("opening_qty"), 649)
        self.assertEqual(totals.get("sales_qty"), 106)
        self.assertEqual(totals.get("closing_qty"), 543)

    def test_second_row_not_hallucinated(self):
        from services.vl_html_table import parse_vl_html_table

        result = parse_vl_html_table(SAMPLE_VL_HTML)
        second = (result.get("line_items") or [])[1]
        self.assertEqual(second["product_name"], "HIORA-K TOOTHPASTE 50G")
        self.assertEqual(second["sales_qty"], 3)
        self.assertEqual(second["receive_qty"], 2)

    def test_extract_embedded_table_from_ocr_text(self):
        from services.vl_html_table import extract_vl_table_html, parse_vl_html_table

        wrapped = "Stock Statement\n" + SAMPLE_VL_HTML + "\nPage 1"
        html = extract_vl_table_html(wrapped)
        self.assertIn("<table", html.lower())
        result = parse_vl_html_table(wrapped)
        self.assertEqual(
            (result["line_items"][0]["product_name"]), "BONNISAN DROPS 30ML"
        )

    def test_priority_resolver_prefers_html_over_text(self):
        from services.vl_html_table import (
            EXTRACTION_METHOD,
            resolve_vl_or_text_extraction,
        )

        result = resolve_vl_or_text_extraction(
            SAMPLE_VL_HTML, filename="x.jpg", source_format="jpg"
        )
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(
            ((result.get("totals") or {}).get("extra") or {}).get("extraction_method"),
            EXTRACTION_METHOD,
        )
        self.assertEqual(result["line_items"][0]["sales_qty"], 7)

    def test_priority_resolver_empty_returns_none(self):
        from services.vl_html_table import resolve_vl_or_text_extraction

        self.assertIsNone(resolve_vl_or_text_extraction(""))
        self.assertIsNone(resolve_vl_or_text_extraction("   "))

    def test_extract_core_uses_vl_html_and_skips_gemini(self):
        from services.ocr.ocr_text_override import ocr_text_override
        from services.sales_statement_extractor import extract_sales_statement_core

        with ocr_text_override(SAMPLE_VL_HTML), patch(
            "services.sales_statement_extractor._parse_image",
            side_effect=AssertionError("image cascade must not run"),
        ), patch(
            "services.gemini_extraction_fallback.maybe_apply_gemini_fallback",
            side_effect=AssertionError("gemini must not run"),
        ):
            # Minimal JPEG magic so sniff accepts image extension.
            result = extract_sales_statement_core(
                b"\xff\xd8\xff\xe0" + b"\x00" * 64, "premier_sept.jpg"
            )

        extra = ((result.get("totals") or {}).get("extra") or {})
        self.assertEqual(extra.get("extraction_method"), "vl_html_table_parse")
        self.assertTrue(extra.get("di_ocr_override"))
        self.assertEqual(result["line_items"][0]["product_name"], "BONNISAN DROPS 30ML")
        self.assertEqual(result["line_items"][0]["sales_qty"], 7)

    def test_spatial_header_skipped_for_vl_html_method(self):
        from services.cell_validator import validate_critical_cells
        from services.column_geometry import CellSpan, HeaderSpan

        result = {
            "line_items": [
                {
                    "product_name": "BONNISAN DROPS 30ML",
                    "opening_qty": 24,
                    "sales_qty": 7,
                    "closing_qty": 17,
                }
            ],
            "totals": {"extra": {"extraction_method": "vl_html_table_parse"}},
        }
        # Geometry that would normally fail spatial mapping.
        headers = [
            HeaderSpan(text="OPENING STOCK", x0=0, y0=0, x1=10, y1=5, column_index=0),
            HeaderSpan(text="ISSUE QUANTITY", x0=50, y0=0, x1=60, y1=5, column_index=1),
        ]
        cells = [
            CellSpan(text="24", x0=100, y0=10, x1=110, y1=20),
            CellSpan(text="7", x0=0, y0=10, x1=10, y1=20),
        ]
        report = validate_critical_cells(
            result, column_headers=headers, column_cells=cells
        )
        categories = {i.category for i in report.issues}
        self.assertNotIn("COLUMN_CONFUSION", categories)
        self.assertEqual(report.column_report.layout_id, "vl_html_table_parse")

    def test_gemini_guard_raises_when_table_html_present(self):
        from services.gemini_extraction_fallback import guard_gemini_when_vl_table_present

        result = {
            "totals": {
                "extra": {
                    "extraction_method": "vl_html_table_parse",
                    "table_available": True,
                    "ocr_table_html": SAMPLE_VL_HTML,
                }
            }
        }
        with self.assertRaises(RuntimeError):
            guard_gemini_when_vl_table_present(result, filename="premier.jpg")


if __name__ == "__main__":
    unittest.main()
