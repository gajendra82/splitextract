"""Saleable Stock Report: Opn / N.T.Pur / N.T.Sale / Bal qty, Amt=> values.

Last P.Bill Date is a date column, not last-month sales. Printed dashes are
zero. Company Total qty and the Amt=> money row both land on statement totals.
Per-row sales value stays absent — the sheet does not print it.
"""

from __future__ import annotations

import unittest
from pathlib import Path

from services.stock_header_resolver import resolve_columns
from services.stock_native_resolver import (
    _footer_totals_from_page_results,
    fields_to_line_item,
)
from services.stock_native_tables import _sheet_total_row_kind

PDF = Path(
    r"C:\Users\adity\Downloads\Stock-Statement-HIMALAYA ZEAL 9893360811-From 01102026 To 31102026-142116.pdf"
)


def _cell(idx: int, text: str, x: float) -> dict:
    return {
        "col_index": idx,
        "text": text,
        "x_center": x,
        "x0": x - 0.03,
        "x1": x + 0.03,
    }


def _tok(text: str, x: float, idx: int) -> dict:
    return {"text": text, "x": x, "col_index": idx}


_HEADERS = [
    _cell(0, "Particular", 0.12),
    _cell(1, "Packing", 0.33),
    _cell(2, "Last P.Bill Date", 0.42),
    _cell(3, "Opn (Q+F)", 0.54),
    _cell(4, "N.T.Pur (Q+F)", 0.62),
    _cell(5, "N.T.Sale (Q+F)", 0.71),
    _cell(6, "Bal (Q+F)", 0.82),
    _cell(7, "Ord (Q+F)", 0.90),
]


class LastPurchaseBillDateHeaderTests(unittest.TestCase):
    def test_last_pbill_date_is_ignore_not_lms(self):
        columns = resolve_columns(_HEADERS)["columns"]
        by_text = {c["header_text"]: c for c in columns}
        date_col = by_text["Last P.Bill Date"]
        self.assertEqual(date_col["canonical"], "ignore")
        self.assertEqual(date_col["reason"], "last_purchase_bill_date")
        self.assertEqual(by_text["Opn (Q+F)"]["canonical"], "opening_qty")
        self.assertEqual(by_text["N.T.Pur (Q+F)"]["canonical"], "purchase_qty")
        self.assertEqual(by_text["N.T.Sale (Q+F)"]["canonical"], "sales_qty")
        self.assertEqual(by_text["Bal (Q+F)"]["canonical"], "closing_qty")
        self.assertEqual(by_text["Ord (Q+F)"]["canonical"], "ignore")

    def test_bare_last_and_last_month_sales_stay_lms(self):
        last = resolve_columns([_cell(0, "Last", 0.4)])["columns"][0]
        self.assertEqual(last["canonical"], "lms")
        month = resolve_columns(
            [
                _cell(0, "Product", 0.1),
                _cell(1, "Last Month Sales", 0.4),
                _cell(2, "Closing", 0.7),
            ]
        )["columns"]
        by_text = {c["header_text"]: c for c in month}
        self.assertEqual(by_text["Last Month Sales"]["canonical"], "lms")


class AmtBandFooterTests(unittest.TestCase):
    def test_dash_run_is_printed_zero(self):
        item = fields_to_line_item(
            {
                "product_name": "CLARINA face wash",
                "pack": "60 ML",
                "opening_qty": "——",
                "purchase_qty": "50.00",
                "sales_qty": "12.00",
                "closing_qty": "38.00",
            }
        )
        self.assertEqual(item["opening_qty"], 0.0)
        self.assertEqual(item["sales_qty"], 12.0)
        self.assertIsNone(item["sales_value"])
        self.assertEqual(item["packing"], "60 ML")

    def test_company_total_qty_and_amt_value(self):
        company = [
            _tok("Company Total", 0.08, 0),
            _tok("186.00", 0.54, 1),
            _tok("310.00", 0.62, 2),
            _tok("258.00", 0.71, 3),
            _tok("238.00", 0.82, 4),
            _tok("290.50", 0.90, 5),
        ]
        amt = [
            _tok("Amt=>", 0.06, 0),
            _tok("25431.74", 0.529, 1),
            _tok("39334.50", 0.621, 2),
            _tok("30492.06", 0.713, 3),
            _tok("34274.18", 0.804, 4),
            _tok("32917.41", 0.893, 5),
        ]
        self.assertEqual(_sheet_total_row_kind(company), "quantity")
        self.assertEqual(_sheet_total_row_kind(amt), "value")
        footer = _footer_totals_from_page_results(
            [
                {
                    "header_cells": _HEADERS,
                    "data_rows": [company, amt],
                }
            ]
        )
        self.assertEqual(footer.get("sales_qty"), 258.0)
        self.assertEqual(footer.get("opening_qty"), 186.0)
        self.assertEqual(footer.get("receipts_qty"), 310.0)
        self.assertEqual(footer.get("closing_qty"), 238.0)
        self.assertEqual(footer.get("sales_value"), 30492.06)
        self.assertEqual(footer.get("opening_value"), 25431.74)
        self.assertEqual(footer.get("receipts_value"), 39334.50)
        self.assertEqual(footer.get("closing_value"), 34274.18)
        self.assertNotIn("order_value", footer)
        self.assertNotEqual(footer.get("sales_qty"), 32917.41)
        self.assertNotEqual(footer.get("sales_value"), 32917.41)


class LiveSaleableNtSalePdfTests(unittest.TestCase):
    @unittest.skipUnless(PDF.is_file(), "ZEAL saleable stock PDF not on disk")
    def test_nt_sale_qty_and_amt_sales_value(self):
        from services.stock_native_resolver import parse_pdf_free_column_statement

        result = parse_pdf_free_column_statement(PDF.read_bytes(), PDF.name)
        self.assertIsNotNone(result)
        items = result["line_items"]
        by_name = {
            str(it.get("product_name") or ""): it
            for it in items
            if isinstance(it, dict)
        }
        aact = next(it for name, it in by_name.items() if name.startswith("AACTARIL"))
        self.assertEqual(aact.get("product_name"), "AACTARIL SOAP 75 G HIMALAYA")
        self.assertEqual(aact.get("packing"), "75 G")
        self.assertEqual(aact.get("sales_qty"), 13.0)
        self.assertIsNone(aact.get("sales_value"))
        clarina = next(
            it for name, it in by_name.items() if "CLARINA" in name and "face" in name.lower()
        )
        self.assertEqual(
            clarina.get("product_name"), "CLARINA face wash ANTI ACNE"
        )
        self.assertEqual(clarina.get("sales_qty"), 12.0)
        self.assertEqual(clarina.get("packing"), "60 ML")
        self.assertIsNone(clarina.get("sales_value"))
        names = " ".join(by_name)
        self.assertNotIn("Amt", names)
        self.assertNotIn("Company Total", names)
        totals = result["totals"]
        self.assertEqual(totals.get("sales_qty"), 258.0)
        self.assertEqual(totals.get("sales_value"), 30492.06)


if __name__ == "__main__":
    unittest.main()
