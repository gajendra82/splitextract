"""XLS/XLSX bare STOCK column maps to closing_qty (Stock Value → closing_value)."""

from __future__ import annotations

import io
import unittest
from unittest.mock import patch

from openpyxl import Workbook

from services.sales_statement_extractor import (
    _xls_norm_header,
    extract_sales_statement,
)


def _skites_like_xlsx_bytes() -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "STOCKANDSALESTATEMENT"
    ws.append([None, None, None, "STOCKANDSALESTATEMENT"])
    ws.append([])
    ws.append(["Location : KAMOTHE"])
    ws.append([])
    ws.append(
        [
            "From Date : 01-09-2026 , To Date : 30-09-2026 , "
            "Supplier Name : HIMALAYA WELLNESS COMPANY"
        ]
    )
    ws.append(
        [
            "Product Code",
            "Product Name",
            "Packing",
            "Opening Qty",
            "Other Inward",
            "AUGUST-2026 (QTY)",
            "JULY-2026 (QTY)",
            "SALE",
            "Sale Value",
            "Other Outward",
            "STOCK",
            "Stock Value",
            "Location",
        ]
    )
    ws.append(
        [
            95590,
            "KOFLET LOZENGES",
            "15 LOZENGES",
            10,
            31,
            7,
            0,
            41,
            831.07,
            0,
            0,
            0,
            "KAMOTHE",
        ]
    )
    ws.append(
        [
            95590,
            "KOFLET LOZENGES",
            "15 LOZENGES",
            43,
            0,
            0,
            0,
            0,
            0,
            265,
            378,
            7662.06,
            "BHIWANDI",
        ]
    )
    ws.append(
        [
            31603,
            "BONNISAN LIQ SMALL",
            "100 ML",
            2,
            0,
            0,
            0,
            0,
            0,
            10,
            48,
            2884.8,
            "BHIWANDI",
        ]
    )
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


class XlsStockColumnAsClosingTests(unittest.TestCase):
    def test_norm_stock_and_stockvalue(self):
        with patch.dict(
            "os.environ", {"STOCK_XLS_STOCK_COLUMN_AS_CLOSING": "true"}
        ):
            self.assertEqual(_xls_norm_header("STOCK"), "bal")
            self.assertEqual(_xls_norm_header("Stock Value"), "bval")
            self.assertEqual(_xls_norm_header("Total Stock"), "totalstock")

    def test_flag_off_leaves_bare_stock(self):
        with patch.dict(
            "os.environ", {"STOCK_XLS_STOCK_COLUMN_AS_CLOSING": "false"}
        ):
            self.assertEqual(_xls_norm_header("STOCK"), "stock")

    def test_extract_closing_qty_from_stock_column(self):
        with patch.dict(
            "os.environ", {"STOCK_XLS_STOCK_COLUMN_AS_CLOSING": "true"}
        ):
            result = extract_sales_statement(
                _skites_like_xlsx_bytes(), "Skites2026.xlsx"
            )
        items = result.get("line_items") or []
        self.assertEqual(len(items), 3)
        by_key = {
            (it.get("product_name"), it.get("opening_qty")): it for it in items
        }
        kamothe = by_key[("KOFLET LOZENGES", 10.0)]
        self.assertEqual(kamothe.get("sales_qty"), 41.0)
        self.assertEqual(kamothe.get("closing_qty"), 0.0)
        self.assertEqual(kamothe.get("closing_value"), 0.0)
        bhiwandi = by_key[("KOFLET LOZENGES", 43.0)]
        self.assertEqual(bhiwandi.get("closing_qty"), 378.0)
        self.assertEqual(bhiwandi.get("closing_value"), 7662.06)
        bonnisan = by_key[("BONNISAN LIQ SMALL", 2.0)]
        self.assertEqual(bonnisan.get("closing_qty"), 48.0)
        self.assertEqual(bonnisan.get("closing_value"), 2884.8)


if __name__ == "__main__":
    unittest.main()
