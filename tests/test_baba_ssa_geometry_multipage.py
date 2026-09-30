"""Multi-page Busy STOCK & SALES ANALYSIS: continuation pages must contribute rows.

Regression for 0000737290 (BABA PHARMACEUTICAL DISTRIBUTORS). Continuation pages
reprint the column header mid-page or in the footer; products above those reprints
must not be discarded.
"""

from __future__ import annotations

import unittest
from pathlib import Path

import fitz

from services.sales_statement_extractor import (
    _parse_stock_sales_analysis_words,
    _ssa_skip_product,
    extract_sales_statement,
)

FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "0000737290_2026_08_ZL_24_5005_07092026090411.PDF"
)


def _pages_from_pdf(path: Path):
    doc = fitz.open(path)
    pages = []
    for index, page in enumerate(doc):
        words = [
            (w[0], w[1], w[2], w[3], w[4]) for w in (page.get_text("words") or [])
        ]
        pages.append(
            {
                "page_index": index,
                "words": words,
                "text": page.get_text("text") or "",
            }
        )
    doc.close()
    return pages


@unittest.skipUnless(FIXTURE.exists(), "BABA multi-page fixture not on this machine")
class TestBabaSsaGeometryMultipage(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pages = _pages_from_pdf(FIXTURE)
        cls.geometry = _parse_stock_sales_analysis_words(cls.pages, FIXTURE.name)
        cls.result = extract_sales_statement(FIXTURE.read_bytes(), FIXTURE.name)

    def test_pdf_has_continuation_pages_with_products(self):
        self.assertGreaterEqual(len(self.pages), 3)
        self.assertTrue(self.pages[0].get("words"))
        self.assertTrue(self.pages[1].get("words"))
        self.assertTrue(self.pages[2].get("words"))

    def test_geometry_merges_all_product_pages(self):
        self.assertIsNotNone(self.geometry)
        extra = ((self.geometry.get("totals") or {}).get("extra") or {})
        self.assertEqual(extra.get("extraction_method"), "stock_sales_analysis_geometry")
        rows_by_page = extra.get("rows_by_page") or []
        self.assertGreaterEqual(len(rows_by_page), 3)
        self.assertGreaterEqual(rows_by_page[0], 30)
        self.assertGreaterEqual(rows_by_page[1], 30)
        self.assertGreaterEqual(rows_by_page[2], 30)
        items = self.geometry.get("line_items") or []
        self.assertGreater(len(items), rows_by_page[0])
        self.assertEqual(len(items), sum(rows_by_page))

    def test_page1_and_continuation_products_present(self):
        names = [
            str(item.get("product_name") or "").upper()
            for item in (self.result.get("line_items") or [])
        ]
        self.assertTrue(any("AACTARIL" in name for name in names))
        self.assertTrue(any("HIMCOLIN" in name for name in names))
        self.assertTrue(any("NEEM" in name for name in names))
        self.assertTrue(any("PILEX" in name for name in names))

    def test_headers_and_footers_not_products(self):
        names = [
            str(item.get("product_name") or "")
            for item in (self.result.get("line_items") or [])
        ]
        for name in names:
            self.assertFalse(_ssa_skip_product(name))
            self.assertNotIn("ITEM DESCRIPTION", name.upper())
            self.assertFalse(name.upper().startswith("STOCK PURCHASES"))
            self.assertNotEqual(name.strip().upper(), "QUANTITY")

    def test_statement_identity_single(self):
        extra = ((self.result.get("totals") or {}).get("extra") or {})
        self.assertEqual(extra.get("extraction_method"), "stock_sales_analysis_geometry")
        stockist = str(self.result.get("stockist_name") or "").upper()
        self.assertIn("BABA", stockist)
        self.assertIn("DISTRIBUT", stockist)
        self.assertNotIn("GATA NO", stockist)
        self.assertIn(
            "HIMALAYA", str(self.result.get("company_name") or "").upper()
        )
        self.assertEqual(self.result.get("period_from"), "2026-08-01")
        self.assertEqual(self.result.get("period_to"), "2026-08-31")


if __name__ == "__main__":
    unittest.main()
