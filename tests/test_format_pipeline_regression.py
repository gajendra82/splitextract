"""Each format stays on its own parser. Word changes must not take over the others."""

from __future__ import annotations

import io
import unittest
from unittest.mock import patch

from services.sales_statement_extractor import (
    empty_result,
    extract_sales_statement,
)


def _statement(filename: str, source_format: str, product: str, sales_qty: float):
    result = empty_result(filename, source_format)
    result["stockist_name"] = f"{source_format.upper()} STOCKIST"
    result["company_name"] = "HIMALAYA"
    result["period_from"] = "2026-08-01"
    result["period_to"] = "2026-08-31"
    item = {
        "product_code": None,
        "product_name": product,
        "packing": "60",
        "opening_qty": 10.0,
        "receipts_qty": 0.0,
        "sales_qty": sales_qty,
        "sales_value": 100.0,
        "closing_qty": 8.0,
        "closing_value": 80.0,
        "extra": {},
    }
    result["line_items"] = [item]
    result["totals"]["sales_value"] = 100.0
    result["totals"]["closing_value"] = 80.0
    return result


class TestFormatDispatchStaysSeparate(unittest.TestCase):
    def _assert_only(self, filename: str, payload: bytes, parser_name: str, product: str):
        sentinel = _statement(filename, parser_name, product, 4)
        targets = {
            "_parse_txt": "txt",
            "_parse_htm": "html",
            "_parse_word": "word",
            "_parse_pdf": "pdf",
            "_parse_xls": "xls",
            "_parse_image": "image",
        }
        with patch(
            "services.sales_statement_extractor._parse_txt", return_value=sentinel
        ) as txt, patch(
            "services.sales_statement_extractor._parse_htm", return_value=sentinel
        ) as htm, patch(
            "services.sales_statement_extractor._parse_word", return_value=sentinel
        ) as word, patch(
            "services.sales_statement_extractor._parse_pdf", return_value=sentinel
        ) as pdf, patch(
            "services.sales_statement_extractor._parse_xls", return_value=sentinel
        ) as xls, patch(
            "services.sales_statement_extractor._parse_image", return_value=sentinel
        ) as image:
            calls = {
                "txt": txt,
                "html": htm,
                "word": word,
                "pdf": pdf,
                "xls": xls,
                "image": image,
            }
            result = extract_sales_statement(payload, filename)
            chosen = targets[parser_name]
            self.assertEqual(calls[chosen].call_count, 1, filename)
            for name, mocked in calls.items():
                if name != chosen:
                    self.assertEqual(mocked.call_count, 0, f"{filename} called {name}")
            self.assertEqual(result["line_items"][0]["product_name"], product)
            self.assertNotIn("statements", result)

    def test_pdf_xls_xlsx_txt_html_do_not_enter_word(self):
        self._assert_only("statement.pdf", b"%PDF-1.4", "_parse_pdf", "PDF ROW")
        self._assert_only("statement.xls", b"\xd0\xcf\x11\xe0", "_parse_xls", "XLS ROW")
        self._assert_only("statement.xlsx", b"PK\x03\x04", "_parse_xls", "XLSX ROW")
        self._assert_only("statement.txt", b"LIV 52", "_parse_txt", "TXT ROW")
        self._assert_only("statement.html", b"<table></table>", "_parse_htm", "HTML ROW")
        self._assert_only("statement.docx", b"PK\x03\x04", "_parse_word", "DOCX ROW")
        self._assert_only("statement.doc", b"\xd0\xcf\x11\xe0", "_parse_word", "DOC ROW")

    def test_txt_daily_stock_values_stay_on_the_txt_parser(self):
        text = """
NARAYAN MEDICAL (NEW)                                                02/09/2026
BHANDERHATI, HOOGHLY                                                 17:26:01
** Daily Stock Summary (Company-wise) **          From 01/08/2026 to 31/08/2026
Company:HIMALAYA ZEUS [M177]
        Product & Pack        |  Op. | Pur. |Sales|Sales| Adj./| Cl.  | Order |
                              | Stock| Qty. |Retu.| Qty.| Dmg. |Stock | Qty.  |
AACTARIL SOAP,75GM                 62      0     0    10      0     52     -52
CLARINA ANTI ACNE FACEWAS,60ML     51      0     0     9     -1     41     -41
HIORA K TOOTH PASTE,100GM           0     50     0    10      0     40     -40
"""
        result = extract_sales_statement(text.encode("utf-8"), "narayan.txt")
        self.assertEqual(result["source_format"], "txt")
        self.assertEqual(result["stockist_name"], "NARAYAN MEDICAL (NEW)")
        self.assertEqual(result["period_from"], "2026-08-01")
        self.assertEqual(result["period_to"], "2026-08-31")
        soap = result["line_items"][0]
        self.assertEqual(soap["product_name"], "AACTARIL SOAP")
        self.assertEqual(soap["packing"], "75GM")
        self.assertEqual(soap["opening_qty"], 62)
        self.assertEqual(soap["sales_qty"], 10)
        self.assertEqual(soap["closing_qty"], 52)
        self.assertNotIn("statements", result)

    def test_html_product_stock_report_stays_on_the_html_parser(self):
        html = """
        <table>
          <tr><td>Product Stock Report</td></tr>
          <tr><td>ATUL MEDICO</td></tr>
          <tr><td>Mfg Company: HIMALAYA WELLNESS CO (ZANDRA)</td></tr>
          <tr><td>From: 01/08/2026 To: 31/08/2026</td></tr>
          <tr>
            <td>Product Name</td><td>Opening</td><td>Purchase</td><td>Total</td>
            <td>Sale</td><td>Sample</td><td>Exp/Clos</td><td>Closing</td>
            <td>Cls Amt</td><td>Closing</td>
          </tr>
          <tr>
            <td>BONNISAN DROPS</td><td>65.00</td><td>50.00</td><td>115.00</td>
            <td>0.00</td><td>0.00</td><td>0.00</td><td>65.00</td><td>1744.11</td><td>0.00</td>
          </tr>
        </table>
        """
        result = extract_sales_statement(html.encode("utf-8"), "atul.html")
        self.assertEqual(result["source_format"], "htm")
        self.assertEqual(result["stockist_name"], "ATUL MEDICO")
        self.assertEqual(result["period_from"], "2026-08-01")
        self.assertEqual(result["period_to"], "2026-08-31")
        drops = result["line_items"][0]
        self.assertEqual(drops["product_name"], "BONNISAN DROPS")
        self.assertEqual(drops["opening_qty"], 65)
        self.assertEqual(drops["closing_qty"], 65)
        self.assertEqual(drops["closing_value"], 1744.11)

    def test_xlsx_header_mapping_stays_on_the_excel_parser(self):
        from openpyxl import Workbook

        wb = Workbook()
        sheet = wb.active
        sheet.append(["Item", "Op.", "Sale", "Bal."])
        sheet.append(["LIV 52 TAB", 10, 2, 8])
        buf = io.BytesIO()
        wb.save(buf)
        result = extract_sales_statement(buf.getvalue(), "row1.xlsx")
        self.assertEqual(result["source_format"], "xlsx")
        self.assertEqual(result["line_items"][0]["product_name"], "LIV 52 TAB")
        self.assertEqual(result["line_items"][0]["opening_qty"], 10)
        self.assertEqual(result["line_items"][0]["sales_qty"], 2)
        self.assertEqual(result["line_items"][0]["closing_qty"], 8)
        self.assertEqual(result["totals"]["extra"]["header_row"], 0)


if __name__ == "__main__":
    unittest.main()
