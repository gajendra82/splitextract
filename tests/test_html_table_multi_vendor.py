"""Multi-vendor HTML table column matching + unified parser tests."""

from __future__ import annotations

import unittest
from unittest.mock import patch

PREMIER_HTML = """
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

JAIPUR_HTML = """
<table>
<tr><td>PRODUCT NAME</td><td>PACKING</td><td>Op.Bal. Qty.</td><td>Receipt Qty.</td>
<td>Total Qty.</td><td>Issue Qty.</td><td>Closing Balance</td><td>Near Expiry</td></tr>
<tr><td>AACTARIL SOAP</td><td>75GM</td><td>68</td><td>0</td><td>68</td><td>14</td><td>54</td><td>0</td></tr>
<tr><td>ABANA TAB</td><td>1X60</td><td>57</td><td>0</td><td>57</td><td>29</td><td>28</td><td>0</td></tr>
</table>
"""

KALINGA_HTML = """
<table>
<tr><td>Product Name</td><td>Packing</td><td>Opening</td><td>Receipt</td>
<td>Issues Replied</td><td>Goods Issue</td><td>Closing</td></tr>
<tr><td>LIV-52 SYRUP</td><td>100ML</td><td>10</td><td>5</td><td>0</td><td>3</td><td>12</td></tr>
<tr><td>LIV-52 SYRUP</td><td>200ML</td><td>20</td><td>0</td><td>0</td><td>1</td><td>19</td></tr>
</table>
"""


class ColumnMatcherTests(unittest.TestCase):
    def test_premier_headers(self):
        from services.html_table.column_matcher import match_headers

        headers = [
            "PRODUCT DESCRIPTION",
            "OPENING STOCK",
            "OPENING VALUE",
            "RECEIVE QUANTITY",
            "RECEIVE VALUE",
            "ISSUE QUANTITY",
            "ISSUE VALUE",
            "CLOSING STOCK",
            "CLOSING VALUE",
            "LAST MONTH SALE",
            "EXPIRY STOCK",
        ]
        mapped = match_headers(headers)
        self.assertEqual(mapped[0], "product_name")
        self.assertEqual(mapped[1], "opening_qty")
        self.assertEqual(mapped[2], "opening_value")
        self.assertEqual(mapped[5], "sales_qty")
        self.assertEqual(mapped[7], "closing_qty")
        self.assertEqual(mapped[9], "last_month_sale")
        self.assertEqual(mapped[10], "expiry_stock")

    def test_jaipur_op_bal_issue_qty(self):
        from services.html_table.column_matcher import match_headers

        headers = [
            "PRODUCT NAME",
            "PACKING",
            "Op.Bal. Qty.",
            "Receipt Qty.",
            "Total Qty.",
            "Issue Qty.",
            "Closing Balance",
            "Near Expiry",
        ]
        mapped = match_headers(headers)
        self.assertEqual(mapped[0], "product_name")
        self.assertEqual(mapped[2], "opening_qty")
        self.assertEqual(mapped[3], "receive_qty")
        self.assertEqual(mapped[5], "sales_qty")
        self.assertEqual(mapped[6], "closing_qty")
        self.assertEqual(mapped[7], "expiry_stock")
        self.assertNotIn(1, mapped)  # packing unmatched

    def test_duplicate_field_keeps_first(self):
        from services.html_table.column_matcher import match_headers

        with self.assertLogs("services.html_table.column_matcher", level="WARNING"):
            mapped = match_headers(["Opening Stock", "Opening Qty", "Product Name"])
        self.assertEqual(mapped[0], "opening_qty")
        self.assertNotIn(1, mapped)
        self.assertEqual(mapped[2], "product_name")


class TemplateTests(unittest.TestCase):
    def test_premier_template_routes(self):
        from services.html_table.templates import route_template

        headers = [
            "PRODUCT DESCRIPTION",
            "OPENING STOCK",
            "OPENING VALUE",
            "RECEIVE QUANTITY",
            "RECEIVE VALUE",
            "ISSUE QUANTITY",
            "ISSUE VALUE",
            "CLOSING STOCK",
            "CLOSING VALUE",
            "LAST MONTH SALE",
            "EXPIRY STOCK",
        ]
        name, col_map = route_template(headers)
        self.assertEqual(name, "himalaya_premier")
        self.assertEqual(col_map[5], "sales_qty")

    def test_jaipur_not_premier_template(self):
        from services.html_table.templates import route_template

        name, col_map = route_template(
            ["PRODUCT NAME", "PACKING", "Op.Bal. Qty.", "Issue Qty.", "Closing Balance"]
        )
        self.assertIsNone(name)
        self.assertIsNone(col_map)


class UnifiedParserTests(unittest.TestCase):
    def test_premier_uses_template(self):
        from services.html_table.html_table_parser import parse_html_table

        result = parse_html_table(PREMIER_HTML, allow_llm=False)
        self.assertEqual(result["mapping_source"], "template")
        self.assertEqual(result["extraction_method"], "vl_html_table_parse")
        self.assertEqual(result["line_items"][0]["product_name"], "BONNISAN DROPS 30ML")
        self.assertEqual(result["line_items"][0]["sales_qty"], 7)
        self.assertEqual(result["line_items"][0]["opening_qty"], 24)
        self.assertEqual(result["totals"]["sales_qty"], 106)
        self.assertNotIn("TOTAL", {i["product_name"] for i in result["line_items"]})

    def test_jaipur_uses_fuzzy(self):
        from services.html_table.html_table_parser import parse_html_table

        result = parse_html_table(JAIPUR_HTML, allow_llm=False)
        self.assertEqual(result["mapping_source"], "fuzzy")
        first = result["line_items"][0]
        self.assertEqual(first["product_name"], "AACTARIL SOAP")
        self.assertEqual(first["packing"], "75GM")
        self.assertEqual(first["opening_qty"], 68)
        self.assertEqual(first["sales_qty"], 14)
        self.assertEqual(first["closing_qty"], 54)

    def test_kalinga_goods_issue_and_packing(self):
        from services.html_table.html_table_parser import parse_html_table

        result = parse_html_table(KALINGA_HTML, allow_llm=False)
        self.assertEqual(result["mapping_source"], "fuzzy")
        items = result["line_items"]
        self.assertEqual(len(items), 2)
        by_pack = {i["packing"]: i for i in items}
        self.assertEqual(by_pack["100ML"]["sales_qty"], 3)
        self.assertEqual(by_pack["100ML"]["closing_qty"], 12)
        self.assertEqual(by_pack["200ML"]["opening_qty"], 20)

    def test_llm_fallback_when_fuzzy_weak(self):
        from services.html_table.html_table_parser import parse_html_table
        from services.html_table.llm_column_mapper import clear_llm_column_cache

        clear_llm_column_cache()
        html = """
        <table>
        <tr><td>Medicine</td><td>Start</td><td>Sold</td><td>Left</td></tr>
        <tr><td>SPEMAN</td><td>10</td><td>2</td><td>8</td></tr>
        </table>
        """

        def _fake_llm(headers):
            return {0: "product_name", 1: "opening_qty", 2: "sales_qty", 3: "closing_qty"}

        with patch(
            "services.html_table.html_table_parser.llm_map_columns",
            side_effect=_fake_llm,
        ) as mocked:
            result = parse_html_table(html, allow_llm=True)
        mocked.assert_called_once()
        self.assertEqual(result["mapping_source"], "llm")
        self.assertEqual(result["line_items"][0]["product_name"], "SPEMAN")
        self.assertEqual(result["line_items"][0]["sales_qty"], 2)

    def test_vl_wrapper_preserves_statement_shape(self):
        from services.vl_html_table import parse_vl_html_table

        result = parse_vl_html_table(PREMIER_HTML, filename="x.jpg", allow_llm=False)
        extra = (result.get("totals") or {}).get("extra") or {}
        self.assertEqual(extra.get("extraction_method"), "vl_html_table_parse")
        self.assertEqual(extra.get("mapping_source"), "template")
        self.assertTrue(extra.get("table_available"))
        self.assertEqual(result["line_items"][0]["sales_qty"], 7)


if __name__ == "__main__":
    unittest.main()
