"""Acceptance tests for pipeline priorities 1–8."""

from __future__ import annotations

import unittest
from unittest.mock import patch

PREMIER_HTML = """
PREMIER DISTRIBUTORS
STOCK & SALES STATEMENT OF PREMIER DISTRIBUTORS
THE HIMALAYA DRUG CO.
<table>
<tr><th>PRODUCT DESCRIPTION</th><th>OPENING STOCK</th><th>OPENING VALUE</th>
<th>RECEIVE QUANTITY</th><th>RECEIVE VALUE</th><th>ISSUE QUANTITY</th>
<th>ISSUE VALUE</th><th>CLOSING STOCK</th><th>CLOSING VALUE</th>
<th>LAST MONTH SALE</th><th>EXPIRY STOCK</th></tr>
<tr><td>BONNISAN DROPS 30ML</td><td>24</td><td>1,200.00</td><td>0</td><td>0</td>
<td>7</td><td>507.69</td><td>17</td><td>848.61</td><td>0</td><td>0</td></tr>
<tr><td>TOTAL</td><td>649</td><td>79061.49</td><td>0</td><td>0</td>
<td>106</td><td>13266.61</td><td>543</td><td>67166.75</td><td>0</td><td>0</td></tr>
</table>
"""

FUZZY_HTML = """
<table>
<tr><td>ITEM NAME</td><td>OPEN BAL</td><td>INWARD</td><td>OUTWARD</td><td>CLOSING BAL</td></tr>
<tr><td>SPEMAN 60S</td><td>10</td><td>5</td><td>3</td><td>12</td></tr>
</table>
"""


class Priority1RoutingTests(unittest.TestCase):
    def test_vl_html_not_gemini(self):
        from services.ocr.ocr_text_override import ocr_text_override
        from services.sales_statement_extractor import extract_sales_statement_core

        with ocr_text_override(PREMIER_HTML), patch(
            "services.gemini_extraction_fallback.maybe_apply_gemini_fallback",
            side_effect=AssertionError("gemini must not run"),
        ):
            result = extract_sales_statement_core(
                b"\xff\xd8\xff\xe0" + b"\x00" * 64, "premier.jpg"
            )
        extra = ((result.get("totals") or {}).get("extra") or {})
        self.assertEqual(extra.get("extraction_method"), "vl_html_table_parse")
        self.assertEqual(result["line_items"][0]["product_name"], "BONNISAN DROPS 30ML")
        self.assertEqual(result["line_items"][0]["sales_qty"], 7)

    def test_gemini_guard_raises(self):
        from services.gemini_extraction_fallback import guard_gemini_when_vl_table_present

        with self.assertRaises(RuntimeError):
            guard_gemini_when_vl_table_present(
                {
                    "totals": {
                        "extra": {
                            "table_available": True,
                            "ocr_table_html": PREMIER_HTML,
                            "extraction_method": "vl_html_table_parse",
                        }
                    }
                },
                filename="x.jpg",
            )


class Priority2FuzzyTests(unittest.TestCase):
    def test_item_name_open_bal_inward_outward(self):
        from services.html_table.html_table_parser import parse_html_table

        result = parse_html_table(FUZZY_HTML, allow_llm=False)
        self.assertEqual(result["mapping_source"], "fuzzy")
        item = result["line_items"][0]
        self.assertEqual(item["product_name"], "SPEMAN 60S")
        self.assertEqual(item["opening_qty"], 10)
        self.assertEqual(item["receive_qty"], 5)
        self.assertEqual(item["sales_qty"], 3)
        self.assertEqual(item["closing_qty"], 12)

    def test_llm_fallback_unknown_headers(self):
        from services.html_table.html_table_parser import parse_html_table
        from services.html_table.llm_column_mapper import clear_llm_column_cache

        clear_llm_column_cache()
        html = """
        <table><tr><td>Medicine</td><td>Start</td><td>Sold</td><td>Left</td></tr>
        <tr><td>SPEMAN</td><td>10</td><td>2</td><td>8</td></tr></table>
        """

        def _fake(_headers):
            return {0: "product_name", 1: "opening_qty", 2: "sales_qty", 3: "closing_qty"}

        with patch(
            "services.html_table.html_table_parser.llm_map_columns", side_effect=_fake
        ):
            result = parse_html_table(html, allow_llm=True)
        self.assertEqual(result["mapping_source"], "llm")
        self.assertEqual(result["line_items"][0]["sales_qty"], 2)


class Priority3PreprocessTests(unittest.TestCase):
    def test_preprocess_returns_meta(self):
        from PIL import Image
        import io

        from services.ocr.page_preprocess import preprocess_page_image

        img = Image.new("RGB", (800, 1200), color=(240, 240, 240))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        out, meta = preprocess_page_image(buf.getvalue(), page=1)
        self.assertTrue(isinstance(out, (bytes, bytearray)))
        self.assertIn("steps", meta)


class Priority4RowGroundingTests(unittest.TestCase):
    def test_ground_value_normalized(self):
        from services.indian_normalize import ground_value

        self.assertTrue(ground_value(1250, "AACT 1,250 pcs"))
        self.assertFalse(ground_value(1250, "other row has 12,500 only elsewhere"))
        # 1250 is NOT a substring of normalized 12500
        self.assertFalse(ground_value(1250, "qty 12500"))

    def test_row_grounding_scopes_to_row(self):
        from services.row_grounding import apply_row_scoped_grounding

        result = {
            "line_items": [
                {"product_name": "BONNISAN DROPS 30ML", "sales_qty": 7, "extra": {}},
                {"product_name": "OTHER", "sales_qty": 1250, "extra": {}},
            ],
            "totals": {
                "extra": {
                    "extraction_method": "vl_html_table_parse",
                    "ocr_table_html": PREMIER_HTML,
                }
            },
        }
        apply_row_scoped_grounding(result, table_html=PREMIER_HTML)
        # BONNISAN 7 is in its row — no discrepancy required
        self.assertFalse(
            (result["line_items"][0].get("extra") or {}).get("source_discrepancy")
        )


class Priority5NormalizeTests(unittest.TestCase):
    def test_indian_lakh_and_null(self):
        from services.indian_normalize import parse_indian_number, parse_indian_date

        self.assertEqual(parse_indian_number("1,25,000"), 125000)
        self.assertEqual(parse_indian_number("(500)"), -500)
        self.assertEqual(parse_indian_number("500 CR"), -500)
        self.assertIsNone(parse_indian_number("-"))
        self.assertIsNone(parse_indian_number(""))
        self.assertEqual(parse_indian_number("0"), 0)
        self.assertEqual(parse_indian_number("0.00"), 0)
        self.assertEqual(parse_indian_date("01-09-2023"), "2023-09-01")
        # Artifact year: prefer the 4-digit year closest to today (2026).
        self.assertEqual(parse_indian_date("01/09/2023/2026"), "2026-09-01")


class Priority6MultiPageTests(unittest.TestCase):
    def test_continuation_merge(self):
        from services.html_table.multi_page import merge_page_tables

        page1 = """
        <table><tr><td>ITEM NAME</td><td>OPEN BAL</td><td>OUTWARD</td><td>CLOSING BAL</td></tr>
        <tr><td>SPEMAN</td><td>10</td><td>2</td><td>8</td></tr></table>
        """
        page2 = """
        <table><tr><td></td><td>5</td><td>1</td><td>4</td></tr>
        <tr><td>ABANA</td><td>20</td><td>3</td><td>17</td></tr></table>
        """
        merged = merge_page_tables([page1, page2], allow_llm=False)
        names = [i.get("product_name") for i in merged["line_items"]]
        self.assertIn("SPEMAN", names)
        self.assertGreaterEqual(len(merged["line_items"]), 2)


class Priority7RetryTests(unittest.TestCase):
    def test_should_retry_empty(self):
        from services.ocr.base import OCRResult
        from services.ocr.orchestrator import OCRBundle
        from services.ocr.page_retry import should_retry_page

        empty = OCRBundle(primary=OCRResult(text="", confidence=0.2))
        self.assertTrue(should_retry_page(empty))
        good = OCRBundle(primary=OCRResult(text="<table>x</table>", confidence=0.95))
        self.assertFalse(should_retry_page(good))


class Priority8IdentityTests(unittest.TestCase):
    def test_premier_stockist(self):
        from services.identity_disambiguation import disambiguate_identity
        from services.vl_html_table import parse_vl_html_table

        result = parse_vl_html_table(PREMIER_HTML, filename="premier.jpg", allow_llm=False)
        disambiguate_identity(result, ocr_text=PREMIER_HTML)
        self.assertIn("PREMIER", str(result.get("stockist_name") or "").upper())
        company = str(result.get("company_name") or "").upper()
        if company:
            self.assertTrue(
                "HIMALAYA" in company or "CO" in company or "DRUG" in company
            )

    def test_period_from_banner_text(self):
        from services.vl_html_table import parse_vl_html_table

        html = (
            "Product wise stock statement from 01/08/2026 to 29/08/2026\n"
            + FUZZY_HTML
        )
        result = parse_vl_html_table(html, filename="x.jpg", allow_llm=False)
        self.assertEqual(result.get("period_from"), "2026-08-01")
        self.assertEqual(result.get("period_to"), "2026-08-29")


class OcrResultResilienceFieldsTests(unittest.TestCase):
    def test_ocr_result_exposes_timeout_retry(self):
        from services.ocr.base import OCRResult

        result = OCRResult(text="x", timed_out=True, timeout_s=30.0, retry_count=2)
        payload = result.to_dict()
        self.assertTrue(payload["timed_out"])
        self.assertEqual(payload["timeout_s"], 30.0)
        self.assertEqual(payload["retry_count"], 2)


if __name__ == "__main__":
    unittest.main()
