"""PRODUCT WISE STOCK AND SALE CONSOLIDATED xlsx — GRAND TOTALS is not a product."""

from __future__ import annotations

import io
import unittest

from openpyxl import Workbook

from services.sales_statement_extractor import (
    _xls_norm_header,
    extract_sales_statement,
)


def _himalya_consolidated_xlsx_bytes() -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Report"
    ws.append(
        [
            "PRODUCT WISE STOCK AND SALE CONSOLIDATED "
            "From 01/09/2026 to 31/03/2027"
        ]
    )
    ws.append(
        [
            "SNO.",
            "COMPANY NAME",
            "ITEM NAME",
            "SALE QTY",
            "SALE VALUE",
            "CL STOCK",
            "CLOSING VALUE ON BASIC",
        ]
    )
    ws.append(
        [
            1,
            "HIMALAYA WELLNES COMPNY ZANDRA",
            "BONNISAN 200ML SYP",
            9,
            1045.66,
            35,
            3418.01,
        ]
    )
    ws.append(
        [
            2,
            "HIMALAYA WELLNES COMPNY ZANDRA",
            "CYSTONE 60 TAB.",
            41,
            7761.15,
            41,
            8571.39,
        ]
    )
    # ITEM NAME blank — label sits in COMPANY NAME (live Himalya footer).
    ws.append([None, "GRAND TOTALS", None, 50, 8806.81, 76, 11989.40])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


class ProductWiseStockSaleXlsxGrandTotalsTests(unittest.TestCase):
    def test_closing_value_on_basic_aliases_to_bval(self):
        self.assertEqual(_xls_norm_header("CLOSING VALUE ON BASIC"), "bval")

    def test_grand_totals_not_a_line_item(self):
        result = extract_sales_statement(
            _himalya_consolidated_xlsx_bytes(), "himalya 2026.xlsx"
        )
        names = [str(i.get("product_name") or "") for i in result.get("line_items") or []]
        self.assertEqual(len(names), 2)
        self.assertTrue(all("TOTAL" not in n.upper() for n in names))
        self.assertIn("BONNISAN 200ML SYP", names)
        self.assertIn("CYSTONE 60 TAB.", names)

        totals = result.get("totals") or {}
        self.assertEqual(totals.get("sales_qty"), 50.0)
        self.assertEqual(totals.get("sales_value"), 8806.81)
        self.assertEqual(totals.get("closing_qty"), 76.0)
        self.assertEqual(totals.get("closing_value"), 11989.40)

        for item in result.get("line_items") or []:
            self.assertIsNotNone(item.get("closing_value"))
            self.assertGreater(float(item.get("closing_value") or 0), 0)


if __name__ == "__main__":
    unittest.main()
