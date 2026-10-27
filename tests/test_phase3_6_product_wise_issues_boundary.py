"""Issues digits just past the Issues header are sales_qty, not sh_exp."""

from __future__ import annotations

import unittest
from pathlib import Path

from services.sales_statement_extractor import (
    _parse_product_wise_stock_statement,
    _parse_product_wise_stock_statement_pdf,
)

DATASET = Path(r"C:\Users\amnsa\Downloads\ZL_2026_August")
DOC_0283 = DATASET / "0000700283_2026_08_ZL_18_377_01092026063405.pdf"
DOC_0398 = DATASET / "0000700398_2026_08_ZL_18_377_01092026063708.pdf"
DOC_0238 = DATASET / "0000700238_2026_08_ZL_18_377_03092026154026.pdf"


def _word(text, x0, x1, y):
    return (x0, y, x1, y + 8, text)


def _header(y=10):
    return [
        _word("Product", 15.6, 43.8, y),
        _word("Name", 46.1, 68.0, y),
        _word("Packing", 143.1, 172.3, y),
        _word("Opening", 185.1, 219.1, y),
        _word("Receipt", 237.3, 267.9, y),
        _word("Issues", 284.6, 310.6, y),
        _word("Closing", 323.9, 353.9, y),
        _word("Sh.Exp", 369.6, 398.6, y),
        _word("Liquidation", 404.9, 444.5, y),
        _word("Sales", 479.7, 498.1, y),
        _word("Amount", 500.2, 525.6, y),
        _word("Closing", 541.0, 565.6, y),
        _word("Amount", 567.7, 593.1, y),
    ]


class _Page:
    def __init__(self, words):
        self._words = words

    def get_text(self, kind="text"):
        if kind == "words":
            return self._words
        return (
            "Product wise stock statement from 01/08/2026 to 31/08/2026\n"
            "Product Name Packing Opening Receipt Issues Closing "
            "Sh.Exp Liquidation Sales amount Closing amount\n"
        )


class _Doc(list):
    pass


def _parse(words):
    return _parse_product_wise_stock_statement(_Doc([_Page(words)]), "synthetic.pdf")


def _by_name(result, name):
    for item in result["line_items"]:
        if item["product_name"] == name:
            return item
    raise AssertionError(name)


def _sh_exp_empty(item):
    value = (item.get("extra") or {}).get("sh_exp")
    return value in (None, 0, 0.0)


class TestProductWiseIssuesBoundary(unittest.TestCase):
    def test_geometry_keeps_closing_and_liquidation_apart(self):
        words = _header()
        words.extend(
            [
                _word("CONFIDO", 15.6, 52.5, 40),
                _word("TABS", 54.7, 75.6, 40),
                _word("60'S", 143.1, 158.9, 40),
                _word("66", 219.7, 228.6, 40),
                _word("3", 314.9, 319.4, 40),
                _word("63", 358.5, 367.4, 40),
                _word("567", 424.3, 437.7, 40),
                _word("Days", 439.9, 458.1, 40),
                _word("550.15", 500.4, 524.8, 40),
                _word("12254.13", 557.5, 593.1, 40),
                _word("IDLE", 15.6, 40.0, 60),
                _word("ROW", 42.0, 60.0, 60),
                _word("10", 219.7, 228.6, 60),
                _word("10", 358.5, 367.4, 60),
                _word("WITH", 15.6, 40.0, 80),
                _word("EXP", 42.0, 60.0, 80),
                _word("10", 219.7, 228.6, 80),
                _word("2", 314.9, 319.4, 80),
                _word("8", 358.5, 367.4, 80),
                _word("4", 386.0, 395.0, 80),
            ]
        )
        result = _parse(words)
        confido = _by_name(result, "CONFIDO TABS")
        self.assertEqual(confido["opening_qty"], 66.0)
        self.assertEqual(confido["sales_qty"], 3.0)
        self.assertEqual(confido["closing_qty"], 63.0)
        self.assertTrue(_sh_exp_empty(confido))
        self.assertNotEqual(confido["sales_qty"], 567.0)
        self.assertNotEqual(confido["closing_qty"], 567.0)
        self.assertEqual(confido["sales_value"], 550.15)

        idle = _by_name(result, "IDLE ROW")
        self.assertEqual(idle["opening_qty"], 10.0)
        self.assertEqual(idle["sales_qty"], 0.0)
        self.assertEqual(idle["closing_qty"], 10.0)
        self.assertTrue(_sh_exp_empty(idle))

        with_exp = _by_name(result, "WITH EXP")
        self.assertEqual(with_exp["sales_qty"], 2.0)
        self.assertEqual(with_exp["closing_qty"], 8.0)
        self.assertEqual(with_exp["extra"]["sh_exp"], 4.0)
        self.assertTrue(with_exp["extra"]["stock_identity_ok"])

    @unittest.skipUnless(DOC_0283.is_file(), "0283 fixture missing")
    def test_0000700283_issues_are_sales_qty(self):
        import fitz

        doc = fitz.open(DOC_0283)
        result = _parse_product_wise_stock_statement(doc, DOC_0283.name)
        doc.close()
        by_name = {item["product_name"]: item for item in result["line_items"]}
        confido = by_name["CONFIDO TABS"]
        self.assertEqual(confido["opening_qty"], 66.0)
        self.assertEqual(confido["sales_qty"], 3.0)
        self.assertEqual(confido["closing_qty"], 63.0)
        self.assertTrue(_sh_exp_empty(confido))
        liv = next(
            item
            for item in result["line_items"]
            if item["product_name"].startswith("LIV-52 SYP")
            and item["opening_qty"] == 42.0
        )
        self.assertEqual(liv["receipts_qty"], 70.0)
        self.assertEqual(liv["sales_qty"], 69.0)
        self.assertEqual(liv["closing_qty"], 43.0)
        self.assertTrue(_sh_exp_empty(liv))
        self.assertTrue(
            all((item.get("extra") or {}).get("stock_identity_ok") for item in result["line_items"])
        )

    @unittest.skipUnless(DOC_0398.is_file(), "0398 fixture missing")
    def test_0000700398_issues_are_sales_qty(self):
        import fitz

        doc = fitz.open(DOC_0398)
        result = _parse_product_wise_stock_statement(doc, DOC_0398.name)
        doc.close()
        him = next(
            item
            for item in result["line_items"]
            if item["product_name"].startswith("HIMCOCID SUSP")
            and item["opening_qty"] == 23.0
        )
        self.assertEqual(him["sales_qty"], 6.0)
        self.assertEqual(him["closing_qty"], 17.0)
        self.assertTrue(_sh_exp_empty(him))
        liv = next(
            item
            for item in result["line_items"]
            if item["product_name"].startswith("LIV 52 TAB")
        )
        self.assertEqual(liv["opening_qty"], 1.0)
        self.assertEqual(liv["receipts_qty"], 30.0)
        self.assertEqual(liv["sales_qty"], 1.0)
        self.assertEqual(liv["closing_qty"], 30.0)
        self.assertTrue(_sh_exp_empty(liv))
        self.assertTrue(
            all((item.get("extra") or {}).get("stock_identity_ok") for item in result["line_items"])
        )

    @unittest.skipUnless(DOC_0238.is_file(), "0238 fixture missing")
    def test_0000700238_free_columns_stay_outside_this_parser(self):
        import fitz

        doc = fitz.open(DOC_0238)
        self.assertIsNone(_parse_product_wise_stock_statement(doc, DOC_0238.name))
        parsed = _parse_product_wise_stock_statement_pdf(doc, DOC_0238.name)
        doc.close()
        method = parsed["totals"]["extra"]["extraction_method"]
        self.assertEqual(method, "product_wise_stock_statement_pdf")
        diarex = next(
            item
            for item in parsed["line_items"]
            if item["product_name"].startswith("DIAREX TABLETS")
        )
        self.assertEqual(diarex["receipts_qty"], 48.0)
        self.assertEqual(diarex["sales_qty"], 50.0)
        self.assertEqual(diarex["extra"]["receipts_free"], 2.0)
        self.assertNotEqual(diarex["receipts_qty"], 50.0)
        liv = next(
            item
            for item in parsed["line_items"]
            if item["product_name"].startswith("LIV.52 TABLETS")
        )
        self.assertEqual(liv["extra"]["issues_free_repl"], 2.0)
        self.assertEqual(liv["sales_qty"], 172.0)
        self.assertFalse(
            all(
                (item.get("extra") or {}).get("stock_identity_ok")
                for item in parsed["line_items"]
            )
        )
