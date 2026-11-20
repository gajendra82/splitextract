"""COMPANY STOCK STATEMENT (SR): OP_QNTY / RCV-QNT / ISUUE-Q / RQNTY / BAL-QNT."""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from services.stock_header_resolver import (
    decode_pdf_pseudo_digits,
    resolve_columns,
)
from services.stock_native_tables import _split_glued_header_text


def _cells(*texts):
    n = max(len(texts), 1)
    return [
        {
            "text": t,
            "col_index": i,
            "x_center": (i + 0.5) / n,
            "subheader_text": None,
        }
        for i, t in enumerate(texts)
    ]


class CompanyStockStatementSrHeaderTests(unittest.TestCase):
    def test_op_rcv_issue_rtn_bal_headers_map(self):
        cols = resolve_columns(
            _cells(
                "PRODUCT",
                "UNIT",
                "OP_QNTY",
                "OP-AMT",
                "RCV-QNT",
                "RCV-AMT",
                "ISUUE-Q",
                "ISSUE-AMT",
                "RQNTY",
                "RTN-AMT",
                "BAL-QNT",
                "BAL-AMT",
            )
        )["columns"]
        by = {c["header_text"]: c["canonical"] for c in cols}
        self.assertEqual(by["OP_QNTY"], "opening_qty")
        self.assertEqual(by["OP-AMT"], "opening_value")
        self.assertEqual(by["RCV-QNT"], "purchase_qty")
        self.assertEqual(by["RCV-AMT"], "purchase_value")
        self.assertEqual(by["ISUUE-Q"], "sales_qty")
        self.assertEqual(by["ISSUE-AMT"], "sales_value")
        self.assertEqual(by["RQNTY"], "sales_return_qty")
        self.assertEqual(by["BAL-QNT"], "closing_qty")
        self.assertEqual(by["BAL-AMT"], "closing_value")

    def test_delimited_movement_headers_are_not_unglued(self):
        with patch.dict(
            "os.environ",
            {"STOCK_PDF_KEEP_DELIMITED_MOVEMENT_HEADERS": "true"},
            clear=False,
        ):
            for token in (
                "OP_QNTY",
                "OP-AMT",
                "RCV-QNT",
                "ISSUE-AMT",
                "BAL-AMT",
                "ISUUE-Q",
            ):
                self.assertIsNone(_split_glued_header_text(token), token)
            # QtyLM / FreeSales must still unglue.
            self.assertEqual(_split_glued_header_text("QtyLM"), ("Qty", "LM"))
            self.assertEqual(_split_glued_header_text("FreeSales"), ("Free", "Sales"))

    def test_cid_glyph_stripped(self):
        self.assertEqual(
            decode_pdf_pseudo_digits("(cid:10)H BONNISAN"),
            "H BONNISAN",
        )


@unittest.skipUnless(
    Path("/var/www/html/splitextract/PDFReport01 (1).pdf").is_file()
    or Path("PDFReport01 (1).pdf").is_file(),
    "PDFReport01 (1).pdf fixture missing",
)
class PdfReport01FixtureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = Path("/var/www/html/splitextract/PDFReport01 (1).pdf")
        if not path.is_file():
            path = Path("PDFReport01 (1).pdf")
        cls.raw = path.read_bytes()

    def test_pdf_words_extracts_54_rows_and_footer_values(self):
        from services.stock_native_resolver import parse_pdf_free_column_statement

        with patch.dict(
            "os.environ",
            {
                "STOCK_PDF_KEEP_DELIMITED_MOVEMENT_HEADERS": "true",
                "ENABLE_GEMINI_EXTRACTION_FALLBACK": "false",
            },
            clear=False,
        ):
            result = parse_pdf_free_column_statement(
                self.raw, "PDFReport01 (1).pdf"
            )
            self.assertIsNotNone(result)
            items = result.get("line_items") or []
            self.assertEqual(len(items), 54)
            names = [it.get("product_name") for it in items]
            self.assertTrue(any(n and "BONNISAN 100ML" in n for n in names))
            self.assertTrue(any(n and "SPEMEN TAB" in n for n in names))
            self.assertFalse(any(n and "cid:" in n.lower() for n in names if n))
            # No glued multi-SKU names from all-zero pending merges.
            self.assertFalse(
                any(n and n.count(" H ") >= 2 for n in names if n)
            )
            bon = next(
                it
                for it in items
                if it.get("product_name") and "BONNISAN 100ML" in it["product_name"]
            )
            self.assertEqual(bon.get("opening_qty"), 151.0)
            self.assertEqual(bon.get("sales_qty"), 12.0)
            self.assertEqual(bon.get("closing_qty"), 140.0)
            self.assertEqual(bon.get("packing"), "1X100M")
            self.assertEqual(bon.get("sale_return"), 1.0)
            self.assertAlmostEqual(
                sum(it.get("opening_value") or 0 for it in items), 332847.25, places=1
            )
            self.assertAlmostEqual(
                sum(it.get("closing_value") or 0 for it in items), 315153.85, places=1
            )


if __name__ == "__main__":
    unittest.main()
