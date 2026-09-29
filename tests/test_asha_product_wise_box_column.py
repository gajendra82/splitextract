"""Asha Traders Product wise stock statement (Box + Opening/Receipt/Issues/Closing).

The Box column must not be read as Opening. A prior qty-only parser shadowed the
Box-aware parser and set every opening_qty to 1.
"""

from __future__ import annotations

import unittest
from pathlib import Path

from services.sales_statement_extractor import (
    _is_product_wise_qty_only_stock_statement_text,
    _is_product_wise_stock_statement_text,
    _parse_product_wise_stock_statement,
    extract_sales_statement,
)

SAMPLE = Path(
    r"C:\Users\anuja\Downloads\ZA_2026_August"
    r"\0000700220_2026_08_ZA_18_8140_03092026185354.pdf"
)


class _FakePage:
    def __init__(self, text: str, words=None):
        self._text = text
        self._words = words or []

    def get_text(self, kind: str = "text"):
        if kind == "words":
            return self._words
        return self._text


class _FakeDoc(list):
    pass


class TestAshaProductWiseBoxColumn(unittest.TestCase):
    def test_detector_requires_sales_amount(self):
        text = (
            "ASHA TRADERS, JARAKA\n"
            "Product wise stock statement from 01/08/2026 to 31/08/2026\n"
            "Product Name Packing Box Opening Receipt Issues Closing Sh.Exp Sales amount\n"
        )
        self.assertTrue(_is_product_wise_stock_statement_text(text))
        # Qty-only detector must refuse Sales-amount layouts.
        self.assertFalse(_is_product_wise_qty_only_stock_statement_text(text))
        qty_only = (
            "Product wise stock statement\n"
            "Product Name Packing Opening Receipt Issues Closing\n"
        )
        self.assertFalse(_is_product_wise_stock_statement_text(qty_only))
        self.assertTrue(_is_product_wise_qty_only_stock_statement_text(qty_only))

    @unittest.skipUnless(SAMPLE.exists(), "Asha Traders fixture PDF missing")
    def test_live_pdf_opening_not_box_one(self):
        result = extract_sales_statement(SAMPLE.read_bytes(), SAMPLE.name)
        method = ((result.get("totals") or {}).get("extra") or {}).get(
            "extraction_method"
        )
        self.assertEqual(method, "product_wise_stock_statement")
        self.assertEqual(result["stockist_name"], "ASHA TRADERS, JARAKA")
        self.assertEqual(result["period_from"], "2026-08-01")
        self.assertEqual(result["period_to"], "2026-08-31")
        items = result.get("line_items") or []
        self.assertGreaterEqual(len(items), 40)
        by = {i["product_name"]: i for i in items}

        arjuna = by["ARJUNA TABLETS 60'S"]
        self.assertEqual(arjuna["packing"], "PC")
        self.assertEqual(arjuna["opening_qty"], 33.0)  # not Box=1
        self.assertEqual(arjuna["receipts_qty"], 0.0)
        self.assertEqual(arjuna["sales_qty"], 4.0)
        self.assertEqual(arjuna["closing_qty"], 29.0)
        self.assertEqual(arjuna["sales_value"], 977.61)

        bonn = by["BONNISAN DROP"]
        self.assertEqual(bonn["opening_qty"], 89.0)
        self.assertEqual(bonn["sales_qty"], 24.0)
        self.assertEqual(bonn["closing_qty"], 65.0)

        liv = by["LIV-52 DS 60'S TABLETS"]
        self.assertEqual(liv["opening_qty"], 206.0)
        self.assertEqual(liv["receipts_qty"], 100.0)
        self.assertEqual(liv["sales_qty"], 61.0)
        self.assertEqual(liv["closing_qty"], 245.0)

        # No row should treat the Box column (printed "1") as Opening.
        ones_as_opening = [
            i["product_name"]
            for i in items
            if i.get("opening_qty") == 1.0 and i.get("closing_qty", 0) > 5
        ]
        self.assertEqual(ones_as_opening, [])

        ok = sum(
            1
            for i in items
            if ((i.get("extra") or {}).get("stock_identity_ok") is True)
        )
        self.assertEqual(ok, len(items))


if __name__ == "__main__":
    unittest.main()
