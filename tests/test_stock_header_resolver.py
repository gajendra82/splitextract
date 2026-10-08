"""Phase 2a: shared stock header resolver (offline, unwired)."""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest.mock import patch

from services.stock_header_resolver import (
    assign_cells,
    normalize_header,
    parse_number,
    resolve_columns,
)


def _cells(*texts, subheaders=None):
    subs = subheaders or [None] * len(texts)
    return [
        {
            "text": t,
            "x_center": None,
            "col_index": i,
            "subheader_text": subs[i] if i < len(subs) else None,
        }
        for i, t in enumerate(texts)
    ]


def _canons(result):
    return [c["canonical"] for c in result["columns"]]


class HeaderSamplesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = Path(__file__).resolve().parent / "data" / "header_samples.json"
        cls.samples = json.loads(path.read_text(encoding="utf-8"))

    def test_at_least_ten_formats(self):
        self.assertGreaterEqual(len(self.samples), 10)

    def test_every_sample_resolves(self):
        for name, sample in self.samples.items():
            with self.subTest(format=name):
                result = resolve_columns(sample["header_cells"])
                self.assertEqual(
                    _canons(result),
                    sample["expected"],
                    msg=f"{name}: got {_canons(result)} errors={result['errors']}",
                )


class ResolveColumnsUnitTests(unittest.TestCase):
    def test_op_qty_value_strip(self):
        cells = _cells(
            "Op Qty",
            "Value",
            "Pur Qty",
            "Value",
            "Sale Qty",
            "Value",
            "Cls Qty",
            "Value",
        )
        result = resolve_columns(cells)
        self.assertEqual(
            _canons(result),
            [
                "opening_qty",
                "opening_value",
                "purchase_qty",
                "purchase_value",
                "sales_qty",
                "sales_value",
                "closing_qty",
                "closing_value",
            ],
        )

    def test_two_row_header(self):
        cells = [
            {"text": "Opening", "x_center": None, "col_index": 0, "subheader_text": "Qty"},
            {"text": "Opening", "x_center": None, "col_index": 1, "subheader_text": "Value"},
            {"text": "Purchase", "x_center": None, "col_index": 2, "subheader_text": "Qty"},
            {"text": "Purchase", "x_center": None, "col_index": 3, "subheader_text": "Value"},
            {"text": "Sale", "x_center": None, "col_index": 4, "subheader_text": "Qty"},
            {"text": "Sale", "x_center": None, "col_index": 5, "subheader_text": "Value"},
            {"text": "Closing", "x_center": None, "col_index": 6, "subheader_text": "Qty"},
            {"text": "Closing", "x_center": None, "col_index": 7, "subheader_text": "Value"},
        ]
        result = resolve_columns(cells)
        self.assertEqual(
            _canons(result),
            [
                "opening_qty",
                "opening_value",
                "purchase_qty",
                "purchase_value",
                "sales_qty",
                "sales_value",
                "closing_qty",
                "closing_value",
            ],
        )

    def test_free_neighbour_direction(self):
        after_pur = resolve_columns(
            _cells("Product", "Opening", "Purchase", "Free", "Sale", "Closing")
        )
        self.assertEqual(after_pur["columns"][3]["canonical"], "free_in_qty")
        self.assertEqual(after_pur["columns"][3]["reason"], "neighbour")

        after_sale = resolve_columns(
            _cells("Product", "Opening", "Purchase", "Sale", "Free", "Closing")
        )
        self.assertEqual(after_sale["columns"][4]["canonical"], "free_out_qty")

        alone = resolve_columns(_cells("Product", "Opening", "Free", "Closing"))
        self.assertEqual(alone["columns"][2]["canonical"], "ignore")
        self.assertTrue(
            any(e["code"] == "AMBIGUOUS_FREE" for e in alone["errors"])
        )

    def test_rate_not_value_amt_inherits_closing(self):
        result = resolve_columns(
            _cells("Product", "Opening", "Sale", "Rate", "Cls", "Amt")
        )
        canons = _canons(result)
        self.assertEqual(canons[3], "rate")
        self.assertFalse(result["columns"][3]["is_value"])
        self.assertEqual(canons[4], "closing_qty")
        self.assertEqual(canons[5], "closing_value")

    def test_short_in_token_rules(self):
        inward = resolve_columns(
            _cells("Product", "Opening", "Inward", "Sale", "Closing")
        )
        self.assertEqual(inward["columns"][2]["canonical"], "purchase_qty")

        batch = resolve_columns(
            _cells("Product", "Batch No", "Opening", "Sale", "Closing")
        )
        self.assertEqual(batch["columns"][1]["canonical"], "batch")

        # "Min" must not match short alias "in".
        min_col = resolve_columns(
            _cells("Product", "Min", "Opening", "Sale", "Closing")
        )
        self.assertEqual(min_col["columns"][1]["canonical"], "ignore")

    def test_opening_receipt_total_sale_without_closing(self):
        result = resolve_columns(
            _cells("Opening", "Receipt", "Total", "Sale")
        )
        self.assertEqual(
            _canons(result),
            ["opening_qty", "purchase_qty", "total_qty", "sales_qty"],
        )
        self.assertFalse(
            any(e["code"] == "MISSING_CORE_COLUMNS" for e in result["errors"])
        )

    def test_receipt_aliases_and_total_variants(self):
        for label in ("Receipt", "Receipts", "Rcpt", "Recpt", "Received", "Purchase"):
            result = resolve_columns(
                _cells("Opening", label, "Sale", "Closing")
            )
            self.assertEqual(
                result["columns"][1]["canonical"],
                "purchase_qty",
                msg=label,
            )
        for label in ("Total", "Tot", "Total Qty", "Op+Rec"):
            result = resolve_columns(
                _cells("Opening", "Receipt", label, "Sale")
            )
            self.assertEqual(
                result["columns"][2]["canonical"],
                "total_qty",
                msg=label,
            )

    def test_no_sales_return_without_return_header(self):
        result = resolve_columns(
            _cells("Opening", "Receipt", "Total", "Sale")
        )
        self.assertNotIn("sales_return_qty", _canons(result))

    def test_non_qty_column_before_opening_is_ignored_when_flagged(self):
        with patch.dict("os.environ", {"STOCK_HEADER_IGNORE_NON_QTY": "true"}):
            result = resolve_columns(
                _cells("Product", "NRV", "Opening", "Purchase", "Sale", "Closing")
            )
            self.assertEqual(
                _canons(result),
                [
                    "product_name",
                    "ignore",
                    "opening_qty",
                    "purchase_qty",
                    "sales_qty",
                    "closing_qty",
                ],
            )

            out = assign_cells(
                [
                    {"col_index": 0, "text": "LIV 52"},
                    {"col_index": 1, "text": "123.45"},
                    {"col_index": 2, "text": "8"},
                    {"col_index": 3, "text": "2"},
                    {"col_index": 4, "text": "3"},
                    {"col_index": 5, "text": "7"},
                ],
                result["columns"],
            )
            self.assertEqual(out["fields"]["product_name"], "LIV 52")
            self.assertEqual(out["fields"]["opening_qty"], "8")
            self.assertEqual(out["fields"]["purchase_qty"], "2")
            self.assertEqual(out["fields"]["sales_qty"], "3")
            self.assertEqual(out["fields"]["closing_qty"], "7")
            self.assertNotIn("nrv", out["fields"])

    def test_glued_opening_sto_value_maps_to_opening_qty_when_flagged(self):
        """Vision glues Opening stock + Value; qty must not land in opening_value."""
        with patch.dict("os.environ", {"STOCK_HEADER_IGNORE_NON_QTY": "true"}):
            result = resolve_columns(
                _cells(
                    "Product",
                    "NRV",
                    "Opening sto Value",
                    "Primary sale Value",
                    "Secondary s Value",
                    "Closing stoc Value",
                    "Value",
                )
            )
            by_idx = {c["col_index"]: c["canonical"] for c in result["columns"]}
            self.assertEqual(by_idx[2], "opening_qty")
            self.assertEqual(by_idx[5], "closing_qty")
            self.assertEqual(by_idx[6], "closing_value")
            self.assertNotEqual(by_idx[2], "opening_value")

            # Exact Opening Value still stays a value column.
            classic = resolve_columns(
                _cells("Product", "Opening Value", "Purchase Qty", "Sales Qty", "Closing Qty")
            )
            self.assertEqual(_canons(classic)[1], "opening_value")

    def test_multi_stock_first_opening_last_closing_when_flagged(self):
        """Vision flattens OPENING/CLOSING STOCK into bare STOCK repeats."""
        headers = (
            "ITEM DESCRIPTION",
            "STOCK",
            "PURCHASE",
            "S/R",
            "REPL/",
            "TOTAL",
            "SALES",
            "QTY.",
            "FREE",
            "STOCK",
            "P/R",
            "REPL/",
            "STOCK",
        )
        with patch.dict("os.environ", {"STOCK_HEADER_MULTI_STOCK": "false"}, clear=False):
            off = resolve_columns(_cells(*headers))
            off_by = {c["col_index"]: c["canonical"] for c in off["columns"]}
            self.assertEqual(off_by[1], "closing_qty")
            self.assertEqual(off_by[12], "ignore")

        with patch.dict("os.environ", {"STOCK_HEADER_MULTI_STOCK": "true"}, clear=False):
            on = resolve_columns(_cells(*headers))
            by = {c["col_index"]: c for c in on["columns"]}
            self.assertEqual(by[1]["canonical"], "opening_qty")
            self.assertEqual(by[1]["reason"], "multi_stock_opening")
            self.assertEqual(by[12]["canonical"], "closing_qty")
            self.assertEqual(by[12]["reason"], "multi_stock_closing")
            self.assertEqual(by[9]["canonical"], "ignore")
            self.assertEqual(by[6]["canonical"], "ignore")  # SALES section label
            self.assertEqual(by[7]["canonical"], "sales_qty")
            self.assertEqual(by[2]["canonical"], "purchase_qty")
            self.assertEqual(by[8]["canonical"], "free_out_qty")

            # AACTARIL-style identity: open 53, sale 19, free 2, close 32.
            assigned = assign_cells(
                [
                    {"col_index": 0, "text": "AACTARIL SOAP 750M"},
                    {"col_index": 1, "text": "53"},
                    {"col_index": 2, "text": "0"},
                    {"col_index": 3, "text": "0"},
                    {"col_index": 4, "text": "0"},
                    {"col_index": 5, "text": "0"},
                    {"col_index": 6, "text": "53"},
                    {"col_index": 7, "text": "19"},
                    {"col_index": 8, "text": "2"},
                    {"col_index": 9, "text": "0"},
                    {"col_index": 10, "text": "0"},
                    {"col_index": 11, "text": "0"},
                    {"col_index": 12, "text": "32"},
                ],
                on["columns"],
            )
            fields = assigned["fields"]
            self.assertEqual(fields["opening_qty"], "53")
            self.assertEqual(fields["purchase_qty"], "0")
            self.assertEqual(fields["sales_qty"], "19")
            self.assertEqual(fields["free_out_qty"], "2")
            self.assertEqual(fields["closing_qty"], "32")


class AssignCellsTests(unittest.TestCase):
    def _qty_columns(self, centers):
        labels = [
            ("product_name", "Product"),
            ("opening_qty", "Opening"),
            ("purchase_qty", "Purchase"),
            ("sales_qty", "Issue"),
            ("closing_qty", "Closing"),
        ]
        cols = []
        for i, ((canon, text), x) in enumerate(zip(labels, centers)):
            cols.append(
                {
                    "col_index": i,
                    "header_text": text,
                    "canonical": canon,
                    "confidence": 0.9,
                    "is_value": False,
                    "reason": "test",
                    "x_center": x,
                }
            )
        return cols

    def test_missing_issue_does_not_shift(self):
        # Marg-style: ISSUE token missing — sales_qty None, closing stays correct.
        columns = resolve_columns(
            _cells("Product", "Opening", "Receive", "Issue", "Closing")
        )["columns"]
        # Cell-index tokens: skip issue col (index 3).
        tokens = [
            {"col_index": 0, "text": "LIV 52"},
            {"col_index": 1, "text": "10"},
            {"col_index": 2, "text": "5"},
            {"col_index": 4, "text": "12"},
        ]
        out = assign_cells(tokens, columns)
        self.assertEqual(out["fields"]["product_name"], "LIV 52")
        self.assertEqual(out["fields"]["opening_qty"], "10")
        self.assertEqual(out["fields"]["purchase_qty"], "5")
        self.assertIsNone(out["fields"]["sales_qty"])
        self.assertEqual(out["fields"]["closing_qty"], "12")

    def test_equidistant_ambiguous(self):
        columns = self._qty_columns([0, 100, 200, 300, 400])
        # Midway between opening (100) and purchase (200) => x=150.
        tokens = [
            {"x": 0, "text": "NAME"},
            {"x": 150, "text": "7"},
            {"x": 400, "text": "9"},
        ]
        out = assign_cells(tokens, columns)
        self.assertTrue(any(e["code"] == "AMBIGUOUS_CELL" for e in out["errors"]))
        self.assertIsNone(out["fields"]["opening_qty"])
        self.assertIsNone(out["fields"]["purchase_qty"])
        self.assertEqual(out["fields"]["closing_qty"], "9")

    def test_cell_collision(self):
        columns = self._qty_columns([0, 100, 200, 300, 400])
        tokens = [
            {"x": 100, "text": "1"},
            {"x": 105, "text": "2"},
            {"x": 400, "text": "9"},
        ]
        out = assign_cells(tokens, columns)
        self.assertTrue(any(e["code"] == "CELL_COLLISION" for e in out["errors"]))
        self.assertIsNone(out["fields"]["opening_qty"])


class ParseNumberTests(unittest.TestCase):
    def test_cases(self):
        self.assertEqual(parse_number("1,23,456.00"), 123456.0)
        self.assertEqual(parse_number("(123)"), -123.0)
        self.assertEqual(parse_number("123-"), -123.0)
        self.assertIsNone(parse_number("--"))
        self.assertIsNone(parse_number("-"))
        self.assertIsNone(parse_number(""))
        self.assertIsNone(parse_number("nil"))
        self.assertEqual(parse_number("0"), 0.0)
        self.assertEqual(parse_number("0.00"), 0.0)
        self.assertIsNone(parse_number("1 234"))
        self.assertEqual(parse_number("1 234", allow_internal_spaces=True), 1234.0)
        self.assertIsNone(parse_number("abc"))


class NormalizeTests(unittest.TestCase):
    def test_normalize(self):
        self.assertEqual(normalize_header("Op.Bal."), "op bal")
        self.assertEqual(normalize_header("Exp/Dmg"), "exp dmg")
        self.assertEqual(normalize_header("Stock-Value"), "stock value")


if __name__ == "__main__":
    unittest.main()
