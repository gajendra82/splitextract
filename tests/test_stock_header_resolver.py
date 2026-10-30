"""Phase 2a: shared stock header resolver (offline, unwired)."""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest.mock import patch

from services.stock_header_resolver import (
    assign_cells,
    decode_pdf_pseudo_digits,
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
        # Samples encode default-OFF mappings; flag-ON covered by MedicaInOtFreeInTests.
        with patch.dict("os.environ", {"STOCK_MEDICA_IN_OT_AS_FREE_IN": "false"}):
            for name, sample in self.samples.items():
                with self.subTest(format=name):
                    result = resolve_columns(sample["header_cells"])
                    self.assertEqual(
                        _canons(result),
                        sample["expected"],
                        msg=f"{name}: got {_canons(result)} errors={result['errors']}",
                    )


class ResolveColumnsUnitTests(unittest.TestCase):
    def test_p_return_is_purchase_return_not_sales_return(self):
        result = resolve_columns(
            _cells(
                "ITEM",
                "PACK",
                "OPENING",
                "PURCHASE",
                "P.RETURN",
                "SALE",
                "S.RETURN",
                "OTHERS",
                "CLOSING",
                "REORDER - QTY",
                "ITEMCODE",
            )
        )
        self.assertEqual(
            _canons(result),
            [
                "product_name",
                "pack",
                "opening_qty",
                "purchase_qty",
                "purchase_return_qty",
                "sales_qty",
                "sales_return_qty",
                "ignore",
                "closing_qty",
                "order_qty",
                "ignore",
            ],
        )
        self.assertEqual(result["columns"][4]["canonical"], "purchase_return_qty")
        self.assertEqual(result["columns"][10]["reason"], "item_code")

    def test_pr_and_replace_others_and_expiry_stock(self):
        result = resolve_columns(
            _cells(
                "PRODUCT DESCRIPTION",
                "UNIT",
                "OPENING STOCK",
                "PURCHASE QUANTITY",
                "SALE RETURN QUANTITY",
                "REPLACE+ OTHERS",
                "TOTAL RECEIVE",
                "SALE QUANTITY",
                "P/R QUANTITY",
                "REPLACE+ OTHERS",
                "CLOSING STOCK",
                "RATE",
                "LAST MONTH SALE",
                "EXPIRY STOCK",
            )
        )
        canons = _canons(result)
        self.assertEqual(canons[0], "product_name")
        self.assertEqual(canons[1], "pack")
        self.assertEqual(canons[2], "opening_qty")
        self.assertEqual(canons[3], "purchase_qty")
        self.assertEqual(canons[4], "sales_return_qty")
        self.assertEqual(canons[6], "total_qty")
        self.assertEqual(canons[7], "sales_qty")
        self.assertEqual(canons[8], "purchase_return_qty")
        self.assertEqual(canons[10], "closing_qty")
        self.assertEqual(canons[11], "rate")
        self.assertEqual(canons[12], "lms")
        self.assertEqual(canons[13], "ignore")
        self.assertEqual(result["columns"][13]["reason"], "date_or_near_expiry")
        others = [c for c in canons if c == "other_outward_qty"]
        self.assertEqual(len(others), 1)

    def test_gap_token_prefers_product_and_closing_not_rate(self):
        from services.stock_header_resolver import _disambiguate_gap_token

        product = {"canonical": "product_name", "col_index": 0, "x_center": 0.2}
        pack = {"canonical": "pack", "col_index": 1, "x_center": 0.45}
        self.assertEqual(
            _disambiguate_gap_token("60", product, pack)["canonical"],
            "product_name",
        )
        closing = {"canonical": "closing_qty", "col_index": 10, "x_center": 1.48}
        rate = {"canonical": "rate", "col_index": 11, "x_center": 1.56}
        self.assertEqual(
            _disambiguate_gap_token("7", closing, rate)["canonical"],
            "closing_qty",
        )
        self.assertEqual(
            _disambiguate_gap_token("141.17", closing, rate)["canonical"],
            "rate",
        )
        sret = {"canonical": "sales_return_qty", "col_index": 4, "x_center": 0.715}
        others = {"canonical": "other_outward_qty", "col_index": 5, "x_center": 0.818}
        self.assertEqual(
            _disambiguate_gap_token("1", sret, others, 0.766)["canonical"],
            "sales_return_qty",
        )

    def test_parse_number_pdf_pseudo_digits(self):
        # τυ-Sep-φϊ visual 01-Sep-26; φτ visual 20; ωϋ.φϋ visual 57.27
        self.assertEqual(decode_pdf_pseudo_digits("φτ"), "20")
        self.assertEqual(decode_pdf_pseudo_digits("ωϋ.φϋ"), "57.27")
        self.assertEqual(parse_number("φτ"), 20.0)
        self.assertEqual(parse_number("ωϋ.φϋ"), 57.27)
        self.assertEqual(parse_number("20"), 20.0)

    def test_wrapped_receive_quant_issue_quant_closing_stoc(self):
        result = resolve_columns(
            _cells(
                "PRODUCT DESCRIPTION",
                "OPENING STOCK",
                "OPENING VALUE",
                "RECEIVE QUANT",
                "RECEIVE VALUE",
                "ISSUE QUANTITY",
                "ISSUE VALUE",
                "CLOSING STOC",
                "CLOSING VALUE",
            )
        )
        self.assertEqual(
            _canons(result),
            [
                "product_name",
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

    def test_cl_qnt_not_branch_split(self):
        """Cl.Qnt. is closing qty; Cl Panaji stays a non-stock branch split."""
        result = resolve_columns(
            _cells(
                "Particular",
                "Op.Qnt.",
                "Op.Value",
                "Expiry",
                "Pur.Qnt.",
                "Pur.Value",
                "Sl.Qnt.",
                "Sl.Value",
                "Cl.Qnt.",
                "Cl.Value",
            )
        )
        self.assertEqual(
            _canons(result),
            [
                "product_name",
                "opening_qty",
                "opening_value",
                "ignore",
                "purchase_qty",
                "purchase_value",
                "sales_qty",
                "sales_value",
                "closing_qty",
                "closing_value",
            ],
        )
        self.assertEqual(result["columns"][3]["reason"], "date_or_near_expiry")
        branch = resolve_columns(_cells("Product", "Opening", "Cl Panaji", "Cl Mapusa"))
        self.assertEqual(branch["columns"][2]["canonical"], "ignore")
        self.assertEqual(branch["columns"][2]["reason"], "branch_split_header")

    def test_glued_op_value_expiry_prefers_opening_value(self):
        """Clustered 'Op.Value Expiry' is opening value, not expiry_damage qty."""
        result = resolve_columns(
            _cells(
                "Particular",
                "Op.Qnt.",
                "Op.Value Expiry",
                "Pur.Qnt.",
                "Pur.Value",
                "Sl.Qnt.",
                "Sl.Value",
                "Cl.Qnt.",
                "Cl.Value",
            )
        )
        self.assertEqual(result["columns"][2]["canonical"], "opening_value")
        self.assertNotEqual(result["columns"][2]["canonical"], "expiry_damage_qty")

    def test_parse_number_strips_pipe_gutter(self):
        self.assertEqual(parse_number("33.00|"), 33.0)
        self.assertEqual(parse_number("1884.14|05/2029|"), 1884.14)
        self.assertIsNone(parse_number("05/2029"))

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

    def test_ostk_purc_tot_sale_qoh_value_age(self):
        """Profitmaker Stock & Sales: O.Stk / Purc / Tot / Sale / Qoh / Value / Age."""
        with patch.dict("os.environ", {"STOCK_HEADER_IGNORE_NON_QTY": "true"}):
            result = resolve_columns(
                _cells(
                    "Product Name",
                    "Packing",
                    "O.Stk",
                    "Purc",
                    "Tot",
                    "Sale",
                    "Qoh",
                    "Value",
                    "Age",
                )
            )
        self.assertEqual(
            _canons(result),
            [
                "product_name",
                "pack",
                "opening_qty",
                "purchase_qty",
                "total_qty",
                "sales_qty",
                "closing_qty",
                "closing_value",
                "ignore",
            ],
        )
        self.assertEqual(result["columns"][7]["reason"], "inherit_left_qty")
        out = assign_cells(
            [
                {"col_index": 0, "text": "BONNISAN DROPS"},
                {"col_index": 1, "text": "30ML"},
                {"col_index": 2, "text": "50"},
                {"col_index": 3, "text": "150"},
                {"col_index": 4, "text": "200"},
                {"col_index": 5, "text": "39"},
                {"col_index": 6, "text": "161"},
                {"col_index": 7, "text": "10668.09"},
                {"col_index": 8, "text": "3"},
            ],
            result["columns"],
        )
        self.assertEqual(out["fields"]["product_name"], "BONNISAN DROPS")
        self.assertEqual(out["fields"]["opening_qty"], "50")
        self.assertEqual(out["fields"]["purchase_qty"], "150")
        self.assertEqual(out["fields"]["total_qty"], "200")
        self.assertEqual(out["fields"]["sales_qty"], "39")
        self.assertEqual(out["fields"]["closing_qty"], "161")
        self.assertEqual(out["fields"]["closing_value"], "10668.09")

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

    def test_group_wise_sales_sequence_maps_sales_qty_not_first_sales(self):
        """Repeated bare Sales headers: Sales Qty is before Cl.Stock, not first Sales."""
        headers = (
            "Product Name",
            "Strength",
            "Op.",
            "Purchase",
            "Free",
            "Purchase",
            "Purchase",
            "Sales",
            "Sales",
            "Sales",
            "Sales",
            "Cl.Stock",
        )
        with patch.dict(
            "os.environ", {"STOCK_HEADER_GROUP_WISE_SEQUENCE": "false"}, clear=False
        ):
            off = resolve_columns(_cells(*headers))
            off_by = {c["col_index"]: c["canonical"] for c in off["columns"]}
            self.assertEqual(off_by[1], "pack")  # Strength alias always on
            self.assertEqual(off_by[7], "sales_qty")  # first Sales kept
            self.assertEqual(off_by[9], "ignore")

        with patch.dict(
            "os.environ", {"STOCK_HEADER_GROUP_WISE_SEQUENCE": "true"}, clear=False
        ):
            on = resolve_columns(_cells(*headers))
            by = {c["col_index"]: c for c in on["columns"]}
            self.assertEqual(by[1]["canonical"], "pack")
            self.assertEqual(by[2]["canonical"], "opening_qty")
            self.assertEqual(by[3]["canonical"], "purchase_qty")
            self.assertEqual(by[4]["canonical"], "free_in_qty")
            self.assertEqual(by[5]["canonical"], "purchase_return_qty")
            self.assertEqual(by[7]["canonical"], "sales_return_qty")
            self.assertEqual(by[9]["canonical"], "sales_qty")
            self.assertEqual(by[10]["canonical"], "free_out_qty")
            self.assertEqual(by[11]["canonical"], "closing_qty")
            self.assertTrue(
                any(
                    e.get("code") == "GROUP_WISE_SALES_SEQUENCE"
                    for e in on["errors"]
                    if isinstance(e, dict)
                )
            )

            hair = assign_cells(
                [
                    {"col_index": 0, "text": "HAIR ZONE SOLUTION"},
                    {"col_index": 1, "text": "60'S"},
                    {"col_index": 2, "text": "53"},
                    {"col_index": 3, "text": "0"},
                    {"col_index": 4, "text": "0"},
                    {"col_index": 5, "text": "0"},
                    {"col_index": 6, "text": "0"},
                    {"col_index": 7, "text": "0"},
                    {"col_index": 8, "text": "0"},
                    {"col_index": 9, "text": "3"},
                    {"col_index": 10, "text": "0"},
                    {"col_index": 11, "text": "50"},
                ],
                on["columns"],
            )["fields"]
            self.assertEqual(hair["opening_qty"], "53")
            self.assertEqual(hair["sales_qty"], "3")
            self.assertEqual(hair["closing_qty"], "50")
            self.assertEqual(hair["pack"], "60'S")


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

    def test_pack_token_in_opening_band_does_not_null_qty(self):
        columns = self._qty_columns([15, 119, 157, 295, 374])
        tokens = [
            {"x": 15, "text": "BONNISAN"},
            {"x": 48, "text": "DROPS"},
            {"x": 86, "text": "30ML"},
            {"x": 125, "text": "13"},
            {"x": 183, "text": "0"},
            {"x": 307, "text": "0"},
            {"x": 396, "text": "13"},
        ]
        out = assign_cells(tokens, columns)
        self.assertEqual(out["fields"]["opening_qty"], "13")
        self.assertEqual(out["fields"]["pack"], "30ML")
        self.assertFalse(any(e["code"] == "CELL_COLLISION" for e in out["errors"]))

    def test_truncated_pack_fragment_does_not_null_opening_qty(self):
        columns = self._qty_columns([0.267, 0.383, 0.500, 0.618, 0.734])
        tokens = [
            {"x": 0.221, "text": "OPTHACARE"},
            {"x": 0.278, "text": "EYE"},
            {"x": 0.314, "text": "DROP"},
            {"x": 0.349, "text": "10M"},
            {"x": 0.395, "text": "444"},
            {"x": 0.514, "text": "0"},
            {"x": 0.632, "text": "156"},
            {"x": 0.746, "text": "288"},
        ]
        out = assign_cells(tokens, columns)
        self.assertEqual(out["fields"]["opening_qty"], "444")
        self.assertEqual(out["fields"]["sales_qty"], "156")
        self.assertEqual(out["fields"]["closing_qty"], "288")
        self.assertFalse(any(e["code"] == "CELL_COLLISION" for e in out["errors"]))


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


class OpenBalCloseBalBillAmtTests(unittest.TestCase):
    """OpenBalQty must not become closing via infix 'bal qty'; Amt cols are values."""

    _HDR = (
        "ITEM NAME",
        "OpenBalQty",
        "PurchBillQty",
        "PurchBillAmt",
        "Sales BillQty",
        "Sales BillAmt",
        "CloseBalQty",
        "ClosingBal tQty",
        "atestSales Rate",
    )

    def test_openbalqty_is_opening_not_closing(self):
        cols = resolve_columns(_cells(*self._HDR))["columns"]
        by_text = {c["header_text"]: c["canonical"] for c in cols}
        self.assertEqual(by_text["OpenBalQty"], "opening_qty")
        self.assertEqual(by_text["CloseBalQty"], "closing_qty")
        self.assertEqual(by_text["PurchBillAmt"], "purchase_value")
        self.assertEqual(by_text["Sales BillAmt"], "sales_value")
        self.assertEqual(by_text["ClosingBal tQty"], "closing_value")
        self.assertEqual(
            _canons(resolve_columns(_cells(*self._HDR))),
            [
                "product_name",
                "opening_qty",
                "purchase_qty",
                "purchase_value",
                "sales_qty",
                "sales_value",
                "closing_qty",
                "closing_value",
                "rate",
            ],
        )

    def test_assign_cells_maps_open_sale_close(self):
        resolved = resolve_columns(_cells(*self._HDR))
        # BONNISAN DROPS: open 58, sale 6, close 52, sale amt 447.96, close amt 3882.32
        tokens = [
            {"col_index": 0, "text": "BONNISAN DROPS"},
            {"col_index": 1, "text": "58.000"},
            {"col_index": 4, "text": "6.000"},
            {"col_index": 5, "text": "447.96"},
            {"col_index": 6, "text": "52.000"},
            {"col_index": 7, "text": "3882.320"},
        ]
        out = assign_cells(tokens, resolved["columns"])
        fields = out["fields"]
        self.assertEqual(fields["product_name"], "BONNISAN DROPS")
        self.assertEqual(fields["opening_qty"], "58.000")
        self.assertEqual(fields["sales_qty"], "6.000")
        self.assertEqual(fields["closing_qty"], "52.000")
        self.assertEqual(fields["sales_value"], "447.96")
        self.assertEqual(fields["closing_value"], "3882.320")
        self.assertIsNone(fields.get("purchase_qty"))


class MedicaInOtFreeInTests(unittest.TestCase):
    _HDR = ("OPSTK", "PURCH", "SALE", "SALE VAL", "IN/OT", "STOCK", "STK VAL")

    def test_flag_off_in_ot_ignored(self):
        with patch.dict("os.environ", {"STOCK_MEDICA_IN_OT_AS_FREE_IN": "false"}):
            cols = resolve_columns(_cells(*self._HDR))["columns"]
        self.assertEqual(cols[4]["canonical"], "ignore")
        self.assertEqual(cols[4]["reason"], "medica_in_ot")

    def test_flag_on_in_ot_is_free_in_and_stock_is_closing(self):
        with patch.dict("os.environ", {"STOCK_MEDICA_IN_OT_AS_FREE_IN": "true"}):
            for in_ot in ("IN/OT", "IN/OUT", "INOT"):
                hdr = list(self._HDR)
                hdr[4] = in_ot
                cols = resolve_columns(_cells(*hdr))["columns"]
                self.assertEqual(cols[4]["canonical"], "free_in_qty")
                self.assertEqual(cols[4]["reason"], "medica_in_ot")
                self.assertEqual(cols[5]["canonical"], "closing_qty")

    def test_flag_on_glued_in_ot_stock_stays_closing(self):
        hdr = list(self._HDR[:4]) + ["INOTSTOCK", "STK VAL"]
        with patch.dict("os.environ", {"STOCK_MEDICA_IN_OT_AS_FREE_IN": "true"}):
            cols = resolve_columns(_cells(*hdr))["columns"]
        self.assertEqual(cols[4]["canonical"], "closing_qty")
        self.assertIn("medica_in_ot_glued", cols[4]["reason"])


if __name__ == "__main__":
    unittest.main()
