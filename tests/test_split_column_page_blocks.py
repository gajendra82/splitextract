"""Left-half then right-half PDF pages stitch into full stock rows."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from services.stock_header_resolver import assign_cells, resolve_columns
from services.stock_native_tables import (
    _collapse_split_half_tokens,
    _merge_split_headers,
    _merge_split_row,
    _peel_leading_product_code_token,
    _peel_trailing_manufacturer_token,
    _stitch_split_column_page_blocks,
    resolver_ready,
)
from services.stock_native_resolver import build_items_from_pdf_page_results


def _hdr(text: str, idx: int, x: float | None = None) -> dict:
    cell = {"text": text, "col_index": idx, "subheader_text": None}
    if x is not None:
        cell["x_center"] = x
    return cell


def _tok(text: str, idx: int, x: float | None = None) -> dict:
    tok = {"text": text, "col_index": idx}
    if x is not None:
        tok["x"] = x
    return tok


def _left_page(page_index: int, rows: list) -> dict:
    return {
        "page_index": page_index,
        "header_cells": [
            _hdr("PCode", 0, 0.05),
            _hdr("Product Name", 1, 0.20),
            _hdr("Packing", 2, 0.40),
            _hdr("BOX", 3, 0.50),
            _hdr("OPSTK", 4, 0.65),
            _hdr("OPV", 5, 0.80),
        ],
        "data_rows": rows,
        "resolved": True,
        "structural": False,
    }


def _right_page(page_index: int, rows: list) -> dict:
    return {
        "page_index": page_index,
        "header_cells": [
            _hdr("PURC", 0, 0.10),
            _hdr("PURCV", 1, 0.25),
            _hdr("SALE", 2, 0.40),
            _hdr("SALEV", 3, 0.55),
            _hdr("STOCK", 4, 0.70),
            _hdr("STOCKV", 5, 0.85),
            _hdr("Manufacturer / Division", 6, 0.95),
        ],
        "data_rows": rows,
        "resolved": True,
        "structural": False,
    }


class PeelSplitTokensTests(unittest.TestCase):
    def test_peel_leading_product_code(self):
        tokens = [_tok("664903 AACTARIL SOAP", 0), _tok("75GM", 1)]
        out = _peel_leading_product_code_token(tokens)
        self.assertEqual([t["text"] for t in out], ["664903", "AACTARIL SOAP", "75GM"])

    def test_peel_trailing_manufacturer(self):
        tokens = [_tok("14", 0), _tok("1087.52 THE HIMALAYA DRUG - OTX", 1)]
        out = _peel_trailing_manufacturer_token(tokens)
        self.assertEqual(
            [t["text"] for t in out],
            ["14", "1087.52", "THE HIMALAYA DRUG - OTX"],
        )

    def test_collapse_multiword_name_keeps_box_out_of_opening_value(self):
        from services.stock_header_resolver import assign_cells

        left = [
            _tok("668071", 0),
            _tok("HIMCOCID", 1),
            _tok("SF", 2),
            _tok("SUSP", 3),
            _tok("BANANA", 4),
            _tok("200", 5),
            _tok("ML", 6),
            _tok("28", 7),
            _tok("3", 8),
            _tok("273.24", 9),
        ]
        collapsed = _collapse_split_half_tokens(left, 6, trailing_numeric=2)
        self.assertEqual(
            [t["text"] for t in collapsed],
            ["668071", "HIMCOCID SF SUSP BANANA", "200 ML", "28", "3", "273.24"],
        )
        headers = _merge_split_headers(
            [
                _hdr("PCode", 0),
                _hdr("Product Name", 1),
                _hdr("Packing", 2),
                _hdr("BOX", 3),
                _hdr("OPSTK", 4),
                _hdr("OPV", 5),
            ],
            [
                _hdr("PURC", 0),
                _hdr("PURCV", 1),
                _hdr("SALE", 2),
                _hdr("SALEV", 3),
                _hdr("STOCK", 4),
                _hdr("STOCKV", 5),
                _hdr("Manufacturer / Division", 6),
            ],
        )
        right = [
            _tok("0", 0),
            _tok("0.00", 1),
            _tok("0", 2),
            _tok("0.00", 3),
            _tok("3", 4),
            _tok("273.24 THE HIMALAYA DRUG - OTX", 5),
        ]
        merged = _merge_split_row(left, right, left_width=6, right_width=7)
        ready = resolver_ready(headers)
        fields = assign_cells(merged, ready["columns"])["fields"]
        self.assertEqual(fields["product_name"], "HIMCOCID SF SUSP BANANA")
        self.assertEqual(fields["pack"], "200 ML")
        self.assertEqual(fields["opening_qty"], "3")
        self.assertEqual(fields["opening_value"], "273.24")
        self.assertNotEqual(fields["opening_value"], "28")
        self.assertNotEqual(fields["opening_value"], "200")


class MergeSplitRowTests(unittest.TestCase):
    def test_merge_renumbers_and_drops_geometry(self):
        left = [
            _tok("664903 AACTARIL SOAP", 0, 0.1),
            _tok("75GM", 1, 0.3),
            _tok("72", 2, 0.4),
            _tok("14", 3, 0.6),
            _tok("1087.52", 4, 0.8),
        ]
        right = [
            _tok("0", 0, 0.1),
            _tok("0.00", 1, 0.2),
            _tok("0", 2, 0.4),
            _tok("0.00", 3, 0.5),
            _tok("14", 4, 0.7),
            _tok("1087.52 THE HIMALAYA DRUG - OTX", 5, 0.9),
        ]
        merged = _merge_split_row(left, right)
        self.assertEqual(
            [t["text"] for t in merged],
            [
                "664903",
                "AACTARIL SOAP",
                "75GM",
                "72",
                "14",
                "1087.52",
                "0",
                "0.00",
                "0",
                "0.00",
                "14",
                "1087.52",
                "THE HIMALAYA DRUG - OTX",
            ],
        )
        self.assertEqual([t["col_index"] for t in merged], list(range(13)))
        self.assertTrue(all(t.get("x") is None for t in merged))

    def test_merged_headers_drop_geometry_and_resolve(self):
        left = [
            _hdr("PCode", 0, 0.05),
            _hdr("Product Name", 1, 0.2),
            _hdr("Packing", 2, 0.4),
            _hdr("BOX", 3, 0.5),
            _hdr("OPSTK", 4, 0.65),
            _hdr("OPV", 5, 0.8),
        ]
        right = [
            _hdr("PURC", 0, 0.1),
            _hdr("PURCV", 1, 0.25),
            _hdr("SALE", 2, 0.4),
            _hdr("SALEV", 3, 0.55),
            _hdr("STOCK", 4, 0.7),
            _hdr("STOCKV", 5, 0.85),
            _hdr("Manufacturer / Division", 6, 0.95),
        ]
        headers = _merge_split_headers(left, right)
        self.assertEqual(len(headers), 13)
        self.assertTrue(all(h.get("x_center") is None for h in headers))
        ready = resolver_ready(headers)
        self.assertTrue(ready["resolved"])
        by = {c["header_text"]: c["canonical"] for c in ready["columns"]}
        self.assertEqual(by["OPSTK"], "opening_qty")
        self.assertEqual(by["OPV"], "opening_value")
        self.assertEqual(by["PURC"], "purchase_qty")
        self.assertEqual(by["PURCV"], "purchase_value")
        self.assertEqual(by["SALE"], "sales_qty")
        self.assertEqual(by["SALEV"], "sales_value")
        self.assertEqual(by["STOCK"], "closing_qty")
        self.assertEqual(by["STOCKV"], "closing_value")

        tokens = _merge_split_row(
            [
                _tok("664903 AACTARIL SOAP", 0),
                _tok("75GM", 1),
                _tok("72", 2),
                _tok("14", 3),
                _tok("1087.52", 4),
            ],
            [
                _tok("0", 0),
                _tok("0.00", 1),
                _tok("0", 2),
                _tok("0.00", 3),
                _tok("14", 4),
                _tok("1087.52 THE HIMALAYA DRUG - OTX", 5),
            ],
        )
        assigned = assign_cells(tokens, ready["columns"])
        fields = assigned["fields"]
        self.assertEqual(fields["product_name"], "AACTARIL SOAP")
        self.assertEqual(fields["pack"], "75GM")
        self.assertEqual(fields["opening_qty"], "14")
        self.assertEqual(fields["opening_value"], "1087.52")
        self.assertEqual(fields["purchase_qty"], "0")
        self.assertEqual(fields["purchase_value"], "0.00")
        self.assertEqual(fields["sales_qty"], "0")
        self.assertEqual(fields["sales_value"], "0.00")
        self.assertEqual(fields["closing_qty"], "14")
        self.assertEqual(fields["closing_value"], "1087.52")


class BlockStitchTests(unittest.TestCase):
    def test_block_pairs_lefts_then_rights(self):
        left_rows_a = [
            [_tok("664903 SOAP", 0), _tok("75GM", 1), _tok("1", 2), _tok("14", 3), _tok("10.00", 4)],
            [_tok("664904 OIL", 0), _tok("100ML", 1), _tok("1", 2), _tok("5", 3), _tok("20.00", 4)],
            [_tok("664905 GEL", 0), _tok("50GM", 1), _tok("1", 2), _tok("3", 3), _tok("30.00", 4)],
        ]
        left_rows_b = [
            [_tok("664906 TAB", 0), _tok("60S", 1), _tok("1", 2), _tok("8", 3), _tok("40.00", 4)],
            [_tok("664907 SYP", 0), _tok("100ML", 1), _tok("1", 2), _tok("2", 3), _tok("50.00", 4)],
            [_tok("664908 CAP", 0), _tok("30S", 1), _tok("1", 2), _tok("7", 3), _tok("60.00", 4)],
        ]
        right_rows_a = [
            [_tok("0", 0), _tok("0", 1), _tok("0", 2), _tok("0", 3), _tok("14", 4), _tok("10.00 THE HIMALAYA DRUG", 5)],
            [_tok("1", 0), _tok("5", 1), _tok("2", 2), _tok("8", 3), _tok("4", 4), _tok("16.00 THE HIMALAYA DRUG", 5)],
            [_tok("0", 0), _tok("0", 1), _tok("1", 2), _tok("10", 3), _tok("2", 4), _tok("20.00 THE HIMALAYA DRUG", 5)],
        ]
        right_rows_b = [
            [_tok("0", 0), _tok("0", 1), _tok("0", 2), _tok("0", 3), _tok("8", 4), _tok("40.00 THE HIMALAYA DRUG", 5)],
            [_tok("2", 0), _tok("10", 1), _tok("1", 2), _tok("5", 3), _tok("3", 4), _tok("45.00 THE HIMALAYA DRUG", 5)],
            [_tok("0", 0), _tok("0", 1), _tok("0", 2), _tok("0", 3), _tok("7", 4), _tok("60.00 THE HIMALAYA DRUG", 5)],
        ]
        pages = [
            _left_page(0, left_rows_a),
            _left_page(1, left_rows_b),
            _right_page(2, right_rows_a),
            _right_page(3, right_rows_b),
        ]
        with patch.dict("os.environ", {"STOCK_PDF_SPLIT_COLUMN_PAGES": "true"}, clear=False):
            blocked = _stitch_split_column_page_blocks(pages)
        self.assertIsNotNone(blocked)
        self.assertEqual(len(blocked), 2)
        self.assertEqual(blocked[0]["split_column_pages"], [0, 2])
        self.assertEqual(blocked[1]["split_column_pages"], [1, 3])
        items = build_items_from_pdf_page_results(blocked)
        self.assertEqual(len(items), 6)
        first = items[0]
        self.assertEqual(first["product_name"], "SOAP")
        self.assertEqual(first["opening_qty"], 14.0)
        self.assertEqual(first["opening_value"], 10.0)
        self.assertEqual(first["closing_qty"], 14.0)
        self.assertEqual(first["closing_value"], 10.0)
        second_page_first = items[3]
        self.assertEqual(second_page_first["product_name"], "TAB")
        self.assertEqual(second_page_first["opening_qty"], 8.0)
        self.assertEqual(second_page_first["closing_value"], 40.0)


if __name__ == "__main__":
    unittest.main()
