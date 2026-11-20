"""HTML Excel (.xls name) with OP.Stock / Rcpts / CL.Stk / CL.Value.

Real BIFF .xls files must not enter this path.
"""
import os
import unittest
from pathlib import Path
from unittest import mock

from services.sales_statement_extractor import (
    _is_excel_html_bytes,
    _is_opstock_rcpts_clstk_header,
    _opstock_rcpts_clstk_colmap,
    _parse_html_excel_opstock_rcpts_clstk,
    extract_sales_statement,
)


HEADER = [
    "Name",
    "Code",
    "Product",
    "Packing",
    "LMS",
    "OP.Stock",
    "Rcpts",
    "Sales",
    "HOS.SALES",
    "CL.Stk",
    "CL.Value",
]

# Optional SL.Value + Free between Sales and HOS.SALES (13-col portal export).
HEADER_WITH_SL_VALUE = [
    "Name",
    "Code",
    "Product",
    "Packing",
    "LMS",
    "OP.Stock",
    "Rcpts",
    "Sales",
    "SL.Value",
    "Free",
    "HOS.SALES",
    "CL.Stk",
    "CL.Value",
]

SAMPLE_HTML = """
<html xmlns:x="urn:schemas-microsoft-com:office:excel"><body><table>
<tr>{ths}</tr>
<tr><td>Himalaya Drug Co Zandra</td><td>125003</td><td>Arjuna Caps</td><td>60s</td>
<td>0</td><td>0</td><td>0</td><td>0</td><td>0</td><td>0</td><td>0.00</td></tr>
<tr><td></td><td>125025</td><td>Bonnisan Drops(IT)</td><td>30ml</td>
<td>11</td><td>49</td><td>0</td><td>14</td><td>0</td><td>35</td><td>2613.10</td></tr>
<tr><td></td><td>125026</td><td>Bonnisan Liquid (3)</td><td>100ml</td>
<td>43</td><td>102</td><td>0</td><td>22</td><td>0</td><td>80</td><td>5424.00</td></tr>
<tr><td></td><td>125028</td><td>Bonnisan Liquid(2)</td><td>200ml</td>
<td>24</td><td>37</td><td>0</td><td>10</td><td>0</td><td>27</td><td>3085.56</td></tr>
<tr><td></td><td></td><td>Total</td><td></td>
<td>40010.78</td><td>152018.20</td><td>175.00</td><td>46322.51</td><td>0</td><td></td><td>156832.16</td></tr>
</table></body></html>
""".format(
    ths="".join(f"<th>{h}</th>" for h in HEADER)
).encode("utf-8")

SAMPLE_HTML_SL_VALUE = """
<html xmlns:x="urn:schemas-microsoft-com:office:excel"><body><table>
<tr>{ths}</tr>
<tr><td>HIMALAYA DRUG-ZANDRA</td><td>310005</td><td>ABANA TAB</td><td>60'S</td>
<td>11</td><td>25</td><td>0</td><td>3</td><td>503.58</td><td>0</td><td>0</td><td>22</td><td>3692.92</td></tr>
<tr><td></td><td>310018</td><td>CLEARVITAL GEL</td><td>30ML</td>
<td>0</td><td>0</td><td>0</td><td>0</td><td>0.00</td><td>0</td><td>0</td><td>0</td><td>0.00</td></tr>
<tr><td></td><td>22326</td><td>DIABECON DS TAB</td><td>60'S</td>
<td>7</td><td>0</td><td>10</td><td>2</td><td>434.28</td><td>0</td><td>0</td><td>8</td><td>1737.12</td></tr>
<tr><td></td><td></td><td>Total</td><td></td>
<td>20030.28</td><td>38128.14</td><td>76.00</td><td></td><td>9713.52</td><td></td><td>0</td><td></td><td>40967.27</td></tr>
</table></body></html>
""".format(
    ths="".join(f"<th>{h}</th>" for h in HEADER_WITH_SL_VALUE)
).encode("utf-8")


class TestHtmlExcelOpstockRcpts(unittest.TestCase):
    def test_detects_html_excel_not_biff(self):
        self.assertTrue(_is_excel_html_bytes(SAMPLE_HTML))
        self.assertFalse(_is_excel_html_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"))
        self.assertFalse(_is_excel_html_bytes(b"PK\x03\x04"))

    def test_header_gate(self):
        self.assertTrue(_is_opstock_rcpts_clstk_header(HEADER))
        self.assertTrue(_is_opstock_rcpts_clstk_header(HEADER_WITH_SL_VALUE))
        self.assertFalse(
            _is_opstock_rcpts_clstk_header(
                ["Product", "Opening", "Receipt", "Issue", "Closing"]
            )
        )

    def test_colmap_finds_cl_stk_after_optional_columns(self):
        m = _opstock_rcpts_clstk_colmap(HEADER_WITH_SL_VALUE)
        self.assertEqual(m["sale"], 7)
        self.assertEqual(m["sval"], 8)
        self.assertEqual(m["free"], 9)
        self.assertEqual(m["hos_sales"], 10)
        self.assertEqual(m["bal"], 11)
        self.assertEqual(m["bval"], 12)

    def test_maps_opstock_sales_closing(self):
        parsed = _parse_html_excel_opstock_rcpts_clstk(
            SAMPLE_HTML, "sample.xls", ".xls"
        )
        self.assertIsNotNone(parsed)
        self.assertEqual(
            parsed["totals"]["extra"]["extraction_method"],
            "html_excel_opstock_rcpts_clstk",
        )
        self.assertEqual(parsed["company_name"], "Himalaya Drug Co Zandra")
        drops = next(
            i for i in parsed["line_items"] if "Bonnisan Drops" in i["product_name"]
        )
        self.assertEqual(drops["opening_qty"], 49.0)
        self.assertEqual(drops["receipts_qty"], 0.0)
        self.assertEqual(drops["sales_qty"], 14.0)
        self.assertEqual(drops["closing_qty"], 35.0)
        self.assertEqual(drops["closing_value"], 2613.10)
        self.assertEqual(parsed["totals"]["closing_value"], 156832.16)

    def test_legacy_indices_misread_sl_value_layout_as_zero_closing(self):
        """Flag OFF keeps fixed 11-col indices — Free/HOS land in closing slots."""
        with mock.patch.dict(os.environ, {"STOCK_HTML_EXCEL_OPSTOCK_HEADER_COLMAP": ""}):
            parsed = _parse_html_excel_opstock_rcpts_clstk(
                SAMPLE_HTML_SL_VALUE, "sl.xls", ".xls"
            )
        abana = next(i for i in parsed["line_items"] if "ABANA" in i["product_name"])
        self.assertEqual(abana["closing_qty"], 0.0)
        self.assertEqual(abana["closing_value"], 0.0)

    def test_header_colmap_reads_closing_with_sl_value_columns(self):
        with mock.patch.dict(
            os.environ, {"STOCK_HTML_EXCEL_OPSTOCK_HEADER_COLMAP": "true"}
        ):
            parsed = _parse_html_excel_opstock_rcpts_clstk(
                SAMPLE_HTML_SL_VALUE, "sl.xls", ".xls"
            )
        self.assertIsNotNone(parsed)
        self.assertTrue(parsed["totals"]["extra"].get("opstock_header_colmap"))
        abana = next(i for i in parsed["line_items"] if "ABANA" in i["product_name"])
        self.assertEqual(abana["opening_qty"], 25.0)
        self.assertEqual(abana["sales_qty"], 3.0)
        self.assertEqual(abana["sales_value"], 503.58)
        self.assertEqual(abana["closing_qty"], 22.0)
        self.assertEqual(abana["closing_value"], 3692.92)
        self.assertEqual(abana["extra"].get("hos_sales_qty"), 0.0)
        self.assertEqual(parsed["totals"]["closing_value"], 40967.27)
        self.assertEqual(parsed["totals"]["sales_value"], 9713.52)

    def test_header_colmap_preserves_legacy_eleven_col(self):
        with mock.patch.dict(
            os.environ, {"STOCK_HTML_EXCEL_OPSTOCK_HEADER_COLMAP": "true"}
        ):
            parsed = _parse_html_excel_opstock_rcpts_clstk(
                SAMPLE_HTML, "sample.xls", ".xls"
            )
        drops = next(
            i for i in parsed["line_items"] if "Bonnisan Drops" in i["product_name"]
        )
        self.assertEqual(drops["closing_qty"], 35.0)
        self.assertEqual(drops["closing_value"], 2613.10)
        self.assertIsNone(drops["sales_value"])
        self.assertEqual(parsed["totals"]["closing_value"], 156832.16)

    def test_live_fixture_no_longer_raises(self):
        path = Path(
            "/var/www/html/splitextract/"
            "0000700547_2026_08_ZA_10_203_04092026132830.xls"
        )
        if not path.exists():
            self.skipTest("fixture missing")
        result = extract_sales_statement(path.read_bytes(), path.name)
        extra = ((result.get("totals") or {}).get("extra") or {})
        self.assertEqual(
            extra.get("extraction_method"), "html_excel_opstock_rcpts_clstk"
        )
        self.assertGreaterEqual(len(result.get("line_items") or []), 50)
        drops = next(
            i
            for i in result["line_items"]
            if "Bonnisan Drops" in str(i.get("product_name") or "")
        )
        self.assertEqual(drops["opening_qty"], 49.0)
        self.assertEqual(drops["sales_qty"], 14.0)
        self.assertEqual(drops["closing_qty"], 35.0)

    def test_live_fixture_sl_value_closing(self):
        path = Path(
            "/var/www/html/splitextract/"
            "0000731822_2026_08_ZA_10_809_03092026065604.xls"
        )
        if not path.exists():
            self.skipTest("fixture missing")
        with mock.patch.dict(
            os.environ, {"STOCK_HTML_EXCEL_OPSTOCK_HEADER_COLMAP": "true"}
        ):
            result = extract_sales_statement(path.read_bytes(), path.name)
        extra = ((result.get("totals") or {}).get("extra") or {})
        self.assertEqual(
            extra.get("extraction_method"), "html_excel_opstock_rcpts_clstk"
        )
        abana = next(
            i
            for i in result["line_items"]
            if "ABANA" in str(i.get("product_name") or "").upper()
        )
        self.assertEqual(abana["opening_qty"], 25.0)
        self.assertEqual(abana["sales_qty"], 3.0)
        self.assertEqual(abana["closing_qty"], 22.0)
        self.assertEqual(abana["closing_value"], 3692.92)


if __name__ == "__main__":
    unittest.main()
