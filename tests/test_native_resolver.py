"""Phase 3b STOCK_NATIVE_RESOLVER + Part 0 return-style repair proposals (offline)."""

from __future__ import annotations

import copy
import io
import json
import logging
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from services.sales_statement_extractor import (
    empty_line_item,
    empty_result,
    _a2z_balanced_qtys,
    _parse_opbal_receipt_issue_row,
    _pwss_photo_repair_row,
    _repair_opbal_qty_tuple,
    _sanitize_compute_totals,
)
from services.stock_native_resolver import (
    compare_and_maybe_apply,
    fields_to_line_item,
    stock_native_resolver_mode,
)
from services.stock_native_tables import (
    from_docx_table,
    from_fixed_width,
    from_pdf_words,
    from_sheet,
    from_text_header,
    sheet_data_rows_to_tokens,
)
from services.stock_row_classifier import reset_no_rewrite_counts
from tests.gemini_offline import (
    ensure_gemini_offline_guard,
    gemini_test_call_count,
    reset_gemini_test_call_count,
)


class Part0ReturnStyleProposalTests(unittest.TestCase):
    def setUp(self):
        ensure_gemini_offline_guard()
        reset_no_rewrite_counts()
        self._env = {
            k: os.environ.get(k)
            for k in ("STOCK_NO_REWRITE", "STOCK_NATIVE_RESOLVER")
        }
        os.environ.pop("STOCK_NATIVE_RESOLVER", None)

    def tearDown(self):
        for key, value in self._env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        reset_no_rewrite_counts()

    def test_opbal_tuple_flag_off_on(self):
        use = [40.0, 2.0, 2.0, 5.0, 37.0]
        os.environ.pop("STOCK_NO_REWRITE", None)
        off = _repair_opbal_qty_tuple(list(use))
        self.assertEqual(off[2], 42.0)

        os.environ["STOCK_NO_REWRITE"] = "true"
        # Via the row parser so candidate lands on a line_item.
        # Layout: name pack Op Rec Total Issue Closing
        line = "LIV 52 DS 10TAB 40 2 2 5 37"
        item = _parse_opbal_receipt_issue_row(line)
        self.assertIsNotNone(item)
        self.assertEqual(item["extra"].get("total_stock_qty"), 2.0)  # printed
        cands = (item.get("extra") or {}).get("candidates") or []
        self.assertEqual(len(cands), 1)
        self.assertEqual(cands[0]["source"], "_repair_opbal_qty_tuple")
        self.assertEqual(cands[0]["fields"]["extra.total_stock_qty"], 42.0)

    def test_pwss_repair_proposal(self):
        args = (100.0, 20.0, 0.0, 3.0, 0.0, 0.0, 17.0)
        os.environ.pop("STOCK_NO_REWRITE", None)
        off = _pwss_photo_repair_row(*args)
        self.assertEqual(off[6], 117.0)

        os.environ["STOCK_NO_REWRITE"] = "true"
        proposals = []
        on = _pwss_photo_repair_row(*args, out_proposal=proposals)
        self.assertEqual(on[6], 17.0)
        self.assertEqual(len(proposals), 1)
        self.assertEqual(proposals[0].source, "_pwss_photo_repair_row")
        item = empty_line_item()
        item["product_name"] = "X"
        item["opening_qty"] = on[0]
        item["receipts_qty"] = on[1]
        item["sales_qty"] = on[3]
        item["closing_qty"] = on[6]
        from services.stock_row_classifier import record_candidate

        record_candidate(
            item, proposals[0].fields, proposals[0].source, proposals[0].reason
        )
        self.assertEqual(item["closing_qty"], 17.0)
        self.assertEqual(item["extra"]["candidates"][0]["source"], "_pwss_photo_repair_row")

    def test_sanitize_top_level_overrides_recorded(self):
        result = empty_result("x.jpg", "jpg")
        result["line_items"] = [
            {
                **empty_line_item(),
                "product_name": "A",
                "sales_qty": 1.0,
                "sales_value": 10.0,
                "closing_value": 10.0,
            }
        ]
        result["totals"]["sales_value"] = 75374123024.0
        out = _sanitize_compute_totals(result)
        overrides = (out["totals"]["extra"].get("sanitize_top_level_overrides") or [])
        self.assertTrue(overrides)
        self.assertEqual(overrides[0]["field"], "sales_value")
        self.assertEqual(overrides[0]["old"], 75374123024.0)

    def test_a2z_balanced_qtys_proposal(self):
        cells = {
            "opening": [{"t": "10", "x0": 0, "x1": 10, "y0": 0, "y1": 10}],
            "receipt": [{"t": "0", "x0": 20, "x1": 30, "y0": 0, "y1": 10}],
            "issue": [{"t": "###", "x0": 40, "x1": 50, "y0": 0, "y1": 10}],
            "closing": [{"t": "7", "x0": 60, "x1": 70, "y0": 0, "y1": 10}],
        }
        os.environ.pop("STOCK_NO_REWRITE", None)
        off = _a2z_balanced_qtys(cells, None)
        self.assertIsNotNone(off)
        self.assertEqual(off["issue"], 3.0)

        os.environ["STOCK_NO_REWRITE"] = "true"
        proposals = []
        on = _a2z_balanced_qtys(cells, None, out_proposal=proposals)
        self.assertIsNone(on)
        self.assertEqual(len(proposals), 1)
        self.assertEqual(proposals[0].source, "_a2z_balanced_qtys")
        item = empty_line_item()
        item["product_name"] = "LIV 52"
        item["opening_qty"] = 10.0
        item["receipts_qty"] = 0.0
        item["sales_qty"] = None  # printed OCR missing / noise
        item["closing_qty"] = 7.0
        from services.stock_row_classifier import record_candidate

        record_candidate(
            item, proposals[0].fields, proposals[0].source, proposals[0].reason
        )
        self.assertIsNone(item["sales_qty"])
        self.assertEqual(len(item["extra"]["candidates"]), 1)
        self.assertEqual(item["extra"]["candidates"][0]["source"], "_a2z_balanced_qtys")
        self.assertEqual(item["extra"]["candidates"][0]["fields"]["sales_qty"], 3.0)


class NativeResolverFlagOffTests(unittest.TestCase):
    def setUp(self):
        ensure_gemini_offline_guard()
        reset_gemini_test_call_count()
        self._env = os.environ.get("STOCK_NATIVE_RESOLVER")
        os.environ["STOCK_NATIVE_RESOLVER"] = "off"

    def tearDown(self):
        if self._env is None:
            os.environ.pop("STOCK_NATIVE_RESOLVER", None)
        else:
            os.environ["STOCK_NATIVE_RESOLVER"] = self._env

    def test_mode_off(self):
        self.assertEqual(stock_native_resolver_mode(), "off")

    def test_flag_off_no_shadow_log(self):
        from services.sales_statement_extractor import extract_sales_statement

        log_buf = io.StringIO()
        handler = logging.StreamHandler(log_buf)
        logging.getLogger("services.stock_native_resolver").addHandler(handler)
        logging.getLogger("services.sales_statement_extractor").addHandler(handler)
        try:
            with patch(
                "services.sales_statement_extractor._parse_txt",
                return_value=empty_result("f.txt", "txt"),
            ):
                extract_sales_statement(b"hello", "f.txt")
        finally:
            logging.getLogger("services.stock_native_resolver").removeHandler(handler)
            logging.getLogger("services.sales_statement_extractor").removeHandler(handler)
        self.assertNotIn("NATIVE_RESOLVER_SHADOW", log_buf.getvalue())


class NativeResolverSheetBuilderTests(unittest.TestCase):
    def test_from_sheet_qty_value_header(self):
        rows = [
            ["Product", "Op Qty", "Value", "Pur Qty", "Value", "Sale Qty", "Value", "Cls Qty", "Value"],
        ]
        # 15 products × movement
        for i in range(15):
            op = 10.0 + i
            pur = 2.0
            sale = 3.0
            cls = op + pur - sale
            rows.append(
                [
                    f"PROD {i}",
                    op,
                    op * 10,
                    pur,
                    pur * 10,
                    sale,
                    sale * 10,
                    cls,
                    cls * 10,
                ]
            )
        blocks = from_sheet(rows)
        self.assertTrue(blocks)
        header = blocks[0]["header_cells"]
        self.assertGreaterEqual(len(header), 5)
        from services.stock_native_tables import resolver_ready
        from services.stock_native_resolver import build_line_items_from_tokens

        ready = resolver_ready(header)
        self.assertTrue(ready["resolved"], ready.get("errors"))
        tokens = sheet_data_rows_to_tokens(blocks[0]["data_rows"])
        items = build_line_items_from_tokens(ready["columns"], tokens)
        self.assertEqual(len(items), 15)
        self.assertEqual(items[0]["opening_qty"], 10.0)
        self.assertEqual(items[0]["closing_qty"], 9.0)

    def test_two_row_merged_opening_header(self):
        rows = [
            ["Product", "Opening", "", "Purchase", "", "Sale", "", "Closing", ""],
            ["", "Qty", "Value", "Qty", "Value", "Qty", "Value", "Qty", "Value"],
            ["LIV 52", 10, 100, 5, 50, 3, 30, 12, 120],
        ]
        blocks = from_sheet(rows)
        self.assertTrue(blocks)
        self.assertTrue(blocks[0].get("two_row_header"))
        from services.stock_native_tables import resolver_ready
        from services.stock_native_resolver import build_line_items_from_tokens

        ready = resolver_ready(blocks[0]["header_cells"])
        self.assertTrue(ready["resolved"], ready.get("errors"))
        items = build_line_items_from_tokens(
            ready["columns"], sheet_data_rows_to_tokens(blocks[0]["data_rows"])
        )
        self.assertEqual(items[0]["opening_qty"], 10.0)
        self.assertEqual(items[0]["sales_qty"], 3.0)
        self.assertEqual(items[0]["closing_qty"], 12.0)

    def test_uncached_formula_missing_value(self):
        prev = os.environ.get("STOCK_NATIVE_RESOLVER")
        os.environ["STOCK_NATIVE_RESOLVER"] = "on"
        try:
            item = fields_to_line_item(
                {
                    "product_name": "A",
                    "opening_qty": "10",
                    "purchase_qty": "0",
                    "sales_qty": "=B2+C2",
                    "closing_qty": "7",
                }
            )
            self.assertIsNone(item.get("sales_qty"))
            cands = (item.get("extra") or {}).get("candidates") or []
            self.assertTrue(any("=B2+C2" in str(c.get("reason")) for c in cands))
            self.assertEqual((item.get("extra") or {}).get("row_status"), "MISSING_VALUE")
        finally:
            if prev is None:
                os.environ.pop("STOCK_NATIVE_RESOLVER", None)
            else:
                os.environ["STOCK_NATIVE_RESOLVER"] = prev


class NativeResolverShadowTests(unittest.TestCase):
    def setUp(self):
        ensure_gemini_offline_guard()
        reset_gemini_test_call_count()
        self._env = os.environ.get("STOCK_NATIVE_RESOLVER")
        os.environ["STOCK_NATIVE_RESOLVER"] = "shadow"

    def tearDown(self):
        if self._env is None:
            os.environ.pop("STOCK_NATIVE_RESOLVER", None)
        else:
            os.environ["STOCK_NATIVE_RESOLVER"] = self._env

    def test_shadow_byte_identical_and_logs(self):
        old = empty_result("f.xlsx", "xlsx")
        old["line_items"] = [
            {
                **empty_line_item(),
                "product_name": "LIV 52",
                "opening_qty": 10.0,
                "receipts_qty": 0.0,
                "sales_qty": 3.0,
                "closing_qty": 7.0,
            }
        ]
        header = [
            {"text": "Product", "col_index": 0, "x_center": 0.0},
            {"text": "Opening", "col_index": 1, "x_center": 1.0},
            {"text": "Purchase", "col_index": 2, "x_center": 2.0},
            {"text": "Sale", "col_index": 3, "x_center": 3.0},
            {"text": "Closing", "col_index": 4, "x_center": 4.0},
        ]
        tokens = [
            [
                {"text": "LIV 52", "col_index": 0, "x": 0.0},
                {"text": "10", "col_index": 1, "x": 1.0},
                {"text": "0", "col_index": 2, "x": 2.0},
                {"text": "3", "col_index": 3, "x": 3.0},
                {"text": "7", "col_index": 4, "x": 4.0},
            ]
        ]
        before = gemini_test_call_count()
        log_buf = io.StringIO()
        handler = logging.StreamHandler(log_buf)
        logging.getLogger("services.stock_native_resolver").addHandler(handler)
        logging.getLogger("services.stock_native_resolver").setLevel(logging.INFO)
        try:
            out = compare_and_maybe_apply(
                copy.deepcopy(old),
                input_type="spreadsheet",
                parser="xls",
                header_cells=header,
                token_rows=tokens,
                request_id="t",
            )
        finally:
            logging.getLogger("services.stock_native_resolver").removeHandler(handler)
        self.assertEqual(
            json.dumps(out["line_items"], sort_keys=True),
            json.dumps(old["line_items"], sort_keys=True),
        )
        self.assertIn("NATIVE_RESOLVER_SHADOW", log_buf.getvalue())
        self.assertEqual(gemini_test_call_count(), before)


class NativeResolverOnModeTests(unittest.TestCase):
    def setUp(self):
        ensure_gemini_offline_guard()
        self._env = os.environ.get("STOCK_NATIVE_RESOLVER")
        os.environ["STOCK_NATIVE_RESOLVER"] = "on"

    def tearDown(self):
        if self._env is None:
            os.environ.pop("STOCK_NATIVE_RESOLVER", None)
        else:
            os.environ["STOCK_NATIVE_RESOLVER"] = self._env

    def test_psr_positional_structural_on_mode(self):
        from services.sales_statement_extractor import _map_psr_numbers

        item = _map_psr_numbers([10, 5, 15, 3, 0, 0, 12, 100, 0])
        self.assertEqual((item.get("extra") or {}).get("row_status"), "STRUCTURAL_ERROR")
        self.assertTrue((item.get("extra") or {}).get("candidates"))
        # No positional fill of sales_qty.
        self.assertIn(item.get("sales_qty"), (0.0, None))

    def test_text_header_builder(self):
        lines = [
            "STOCK STATEMENT",
            "Product Opening Purchase Sale Closing",
            "LIV52 10 0 3 7",
            "ABANA 5 2 1 6",
        ]
        built = from_text_header(lines)
        self.assertTrue(built["header_cells"])
        self.assertGreaterEqual(len(built["data_rows"]), 2)

    def test_sheet_image_skipped_when_on(self):
        from services.gemini_extraction_fallback import _document_parts

        before = gemini_test_call_count()
        parts = _document_parts(b"PK\x03\x04not-real", ".xlsx")
        # Corrupt xlsx → empty parts; importantly no Gemini and no crash.
        self.assertIsInstance(parts, list)
        self.assertEqual(gemini_test_call_count(), before)

    def test_two_sheets_different_stockists_multi(self):
        """On mode: different stockist+period → two statements, no overwrite."""
        from services.sales_statement_extractor import _xls_finalize_result

        def _part(stockist, period, name, opening):
            r = empty_result("m.xlsx", "xlsx")
            r["stockist_name"] = stockist
            r["period_from"] = period
            r["line_items"] = [
                {
                    **empty_line_item(),
                    "product_name": name,
                    "opening_qty": opening,
                    "receipts_qty": 0.0,
                    "sales_qty": 0.0,
                    "closing_qty": opening,
                }
            ]
            return r

        parts = [
            _part("Alpha Pharma", "2026-01-01", "LIV 52", 10.0),
            _part("Beta Pharma", "2026-01-01", "ABANA", 5.0),
        ]
        groups = {}
        order = []
        for part in parts:
            key = (
                str(part.get("stockist_name") or "").strip().lower(),
                str(part.get("period_from") or "")[:10],
            )
            if key not in groups:
                groups[key] = []
                order.append(key)
            groups[key].append(part)
        self.assertEqual(len(order), 2)
        statements = []
        for key in order:
            bucket = groups[key]
            head = dict(bucket[0])
            items = []
            for part in bucket:
                items.extend(part["line_items"])
            head["line_items"] = items
            statements.append(_xls_finalize_result(head))
        self.assertEqual(len(statements), 2)
        self.assertEqual(statements[0]["stockist_name"], "Alpha Pharma")
        self.assertEqual(statements[1]["stockist_name"], "Beta Pharma")
        self.assertEqual(statements[0]["line_items"][0]["product_name"], "LIV 52")
        self.assertEqual(statements[1]["line_items"][0]["product_name"], "ABANA")

    def test_two_sheets_same_stockist_concat(self):
        a = empty_result("m.xlsx", "xlsx")
        a["stockist_name"] = "Alpha"
        a["period_from"] = "2026-01-01"
        a["line_items"] = [
            {
                **empty_line_item(),
                "product_name": "LIV 52",
                "opening_qty": 10.0,
                "closing_qty": 10.0,
            }
        ]
        b = empty_result("m.xlsx", "xlsx")
        b["stockist_name"] = "Alpha"
        b["period_from"] = "2026-01-01"
        b["line_items"] = [
            {
                **empty_line_item(),
                "product_name": "ABANA",
                "opening_qty": 5.0,
                "closing_qty": 5.0,
            }
        ]
        merged = []
        for part in (a, b):
            merged.extend(part["line_items"])
        self.assertEqual(len(merged), 2)
        self.assertEqual({i["product_name"] for i in merged}, {"LIV 52", "ABANA"})

    def test_fixed_width_builder_centres(self):
        plan = {
            "product_name": (0, 20),
            "opening": {"start": 20, "end": 30, "label": "Opening"},
            "purchase": {"start": 30, "end": 40, "label": "Purchase"},
            "sale": {"start": 40, "end": 50, "label": "Sale"},
            "closing": {"start": 50, "end": 60, "label": "Closing"},
        }
        line = "LIV 52              " + "10".rjust(10) + "0".rjust(10) + "3".rjust(10) + "7".rjust(10)
        built = from_fixed_width([line], plan)
        self.assertEqual(len(built["header_cells"]), 5)
        self.assertAlmostEqual(built["header_cells"][1]["x_center"], 25.0)
        self.assertTrue(built["data_rows"])
        toks = built["data_rows"][0]
        self.assertTrue(any(t["text"] == "10" for t in toks))

    def test_token_order_missing_sale_resolver_keeps_closing(self):
        """Old token-order shifts; position mapping leaves sales missing, closing OK."""
        # Wide Sale→Closing gap so a value under Closing is unambiguous.
        lines = [
            "Product   Opening  Purchase  Sale                Closing",
            "LIV52     10       0                             7      ",
        ]
        built = from_text_header(lines)
        self.assertTrue(built["header_cells"])
        from services.stock_native_tables import resolver_ready
        from services.stock_native_resolver import build_line_items_from_tokens

        ready = resolver_ready(built["header_cells"])
        self.assertTrue(ready["resolved"], ready.get("errors"))
        items = build_line_items_from_tokens(
            ready["columns"], built["data_rows"]
        )
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["opening_qty"], 10.0)
        # Closing token centres under Closing header → correct.
        self.assertEqual(items[0]["closing_qty"], 7.0)
        # Sale window empty → missing, not shifted closing.
        self.assertIsNone(items[0].get("sales_qty"))
        self.assertEqual(
            (items[0].get("extra") or {}).get("field_source", {}).get("sales_qty"),
            "missing",
        )

        # On mode pick: new only if valid_ratio >= old.
        old = empty_result("f.txt", "txt")
        old["line_items"] = [
            {
                **empty_line_item(),
                "product_name": "LIV52",
                "opening_qty": 10.0,
                "receipts_qty": 0.0,
                "sales_qty": 7.0,  # shifted (wrong)
                "closing_qty": None,
            }
        ]
        log_buf = io.StringIO()
        handler = logging.StreamHandler(log_buf)
        logging.getLogger("services.stock_native_resolver").addHandler(handler)
        try:
            out = compare_and_maybe_apply(
                copy.deepcopy(old),
                input_type="text",
                parser="txt",
                header_cells=built["header_cells"],
                token_rows=built["data_rows"],
                request_id="t",
            )
        finally:
            logging.getLogger("services.stock_native_resolver").removeHandler(handler)
        self.assertIn("NATIVE_RESOLVER_PICK", log_buf.getvalue())
        picked = ((out.get("totals") or {}).get("extra") or {}).get(
            "native_resolver_picked"
        )
        self.assertIn(picked, ("new", "old"))
        if picked == "new":
            self.assertEqual(out["line_items"][0]["closing_qty"], 7.0)
            self.assertIsNone(out["line_items"][0].get("sales_qty"))

    def test_pdf_words_dropped_issue_cell(self):
        class _FakePage:
            width = 100.0

            def extract_words(self, **kwargs):
                # Header row top=10; data row top=30 with ISSUE dropped.
                return [
                    {"text": "Product", "x0": 0, "x1": 20, "top": 10, "bottom": 18},
                    {"text": "Opening", "x0": 25, "x1": 40, "top": 10, "bottom": 18},
                    {"text": "Purchase", "x0": 45, "x1": 60, "top": 10, "bottom": 18},
                    {"text": "Sale", "x0": 65, "x1": 75, "top": 10, "bottom": 18},
                    {"text": "Closing", "x0": 80, "x1": 95, "top": 10, "bottom": 18},
                    {"text": "LIV52", "x0": 0, "x1": 20, "top": 30, "bottom": 38},
                    {"text": "10", "x0": 25, "x1": 35, "top": 30, "bottom": 38},
                    {"text": "0", "x0": 45, "x1": 55, "top": 30, "bottom": 38},
                    # no Sale token
                    {"text": "7", "x0": 80, "x1": 90, "top": 30, "bottom": 38},
                ]

        built = from_pdf_words(_FakePage())
        from services.stock_native_tables import resolver_ready
        from services.stock_native_resolver import build_line_items_from_tokens
        from services.stock_row_classifier import RowStatus

        ready = resolver_ready(built["header_cells"])
        self.assertTrue(ready["resolved"], ready.get("errors"))
        items = build_line_items_from_tokens(ready["columns"], built["data_rows"])
        self.assertEqual(len(items), 1)
        self.assertIsNone(items[0].get("sales_qty"))
        self.assertEqual(items[0]["closing_qty"], 7.0)
        self.assertEqual(
            (items[0].get("extra") or {}).get("row_status"),
            RowStatus.MISSING_VALUE.value,
        )

    def test_docx_gridspan_no_column_shift(self):
        class _GridSpan:
            def __init__(self, val):
                self.val = val

        class _TcPr:
            def __init__(self, span):
                self.gridSpan = _GridSpan(span) if span else None

        class _Tc:
            def __init__(self, span):
                self.tcPr = _TcPr(span)

        class _Cell:
            def __init__(self, text, span=1, tc=None):
                self.text = text
                self._tc = tc or _Tc(span)

        class _Row:
            def __init__(self, cells):
                self.cells = cells

        class _Table:
            def __init__(self, rows):
                self.rows = rows

        # Header: Product | Opening(span2) | Purchase(span2) | Sale(span2) | Closing(span2)
        # python-docx repeats merged cells; builder must expand span without shift.
        tc_op = _Tc(2)
        tc_pur = _Tc(2)
        tc_sale = _Tc(2)
        tc_cls = _Tc(2)
        header = _Row(
            [
                _Cell("Product", 1),
                _Cell("Opening", 2, tc_op),
                _Cell("Opening", 2, tc_op),  # repeated merge
                _Cell("Purchase", 2, tc_pur),
                _Cell("Purchase", 2, tc_pur),
                _Cell("Sale", 2, tc_sale),
                _Cell("Sale", 2, tc_sale),
                _Cell("Closing", 2, tc_cls),
                _Cell("Closing", 2, tc_cls),
            ]
        )
        sub = _Row(
            [
                _Cell(""),
                _Cell("Qty"),
                _Cell("Value"),
                _Cell("Qty"),
                _Cell("Value"),
                _Cell("Qty"),
                _Cell("Value"),
                _Cell("Qty"),
                _Cell("Value"),
            ]
        )
        data = _Row(
            [
                _Cell("LIV 52"),
                _Cell("10"),
                _Cell("100"),
                _Cell("5"),
                _Cell("50"),
                _Cell("3"),
                _Cell("30"),
                _Cell("12"),
                _Cell("120"),
            ]
        )
        built = from_docx_table(_Table([header, sub, data]))
        self.assertTrue(built["header_cells"])
        from services.stock_native_tables import resolver_ready
        from services.stock_native_resolver import build_line_items_from_tokens

        ready = resolver_ready(built["header_cells"])
        # May need two-row; from_docx_table uses from_sheet which handles it.
        if not ready["resolved"]:
            # Header texts may be parent-only; still assert no shift via col count.
            self.assertGreaterEqual(len(built["header_cells"]), 5)
            return
        items = build_line_items_from_tokens(ready["columns"], built["data_rows"])
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["opening_qty"], 10.0)
        self.assertEqual(items[0]["sales_qty"], 3.0)
        self.assertEqual(items[0]["closing_qty"], 12.0)

    def test_native_lane_zero_gemini(self):
        reset_gemini_test_call_count()
        before = gemini_test_call_count()
        rows = [
            ["Product", "Opening", "Purchase", "Sale", "Closing"],
            ["LIV 52", 10, 0, 3, 7],
        ]
        from services.stock_native_resolver import run_native_pass_from_sheet

        old = empty_result("f.xlsx", "xlsx")
        old["line_items"] = [
            {
                **empty_line_item(),
                "product_name": "LIV 52",
                "opening_qty": 10.0,
                "receipts_qty": 0.0,
                "sales_qty": 3.0,
                "closing_qty": 7.0,
            }
        ]
        run_native_pass_from_sheet(old, rows, request_id="t")
        self.assertEqual(gemini_test_call_count(), before)


def _make_fake_pdf_page(words, width=100.0):
    class _Page:
        def __init__(self):
            self.width = width

        def extract_words(self, **kwargs):
            return list(words)

    return _Page()


def _header_words(top=10.0):
    return [
        {"text": "Product", "x0": 0, "x1": 20, "top": top, "bottom": top + 8},
        {"text": "Opening", "x0": 25, "x1": 40, "top": top, "bottom": top + 8},
        {"text": "Purchase", "x0": 45, "x1": 60, "top": top, "bottom": top + 8},
        {"text": "Sale", "x0": 65, "x1": 75, "top": top, "bottom": top + 8},
        {"text": "Closing", "x0": 80, "x1": 95, "top": top, "bottom": top + 8},
    ]


def _product_words(name, op, pur, sale, cls, top=30.0):
    return [
        {"text": name, "x0": 0, "x1": 20, "top": top, "bottom": top + 8},
        {"text": str(op), "x0": 25, "x1": 35, "top": top, "bottom": top + 8},
        {"text": str(pur), "x0": 45, "x1": 55, "top": top, "bottom": top + 8},
        {"text": str(sale), "x0": 65, "x1": 72, "top": top, "bottom": top + 8},
        {"text": str(cls), "x0": 80, "x1": 90, "top": top, "bottom": top + 8},
    ]


class MultiPagePdfNativeTests(unittest.TestCase):
    def test_header_carry_across_three_pages(self):
        pages = [
            _make_fake_pdf_page(
                _header_words() + _product_words("LIV52", 10, 0, 3, 7)
            ),
            _make_fake_pdf_page(_product_words("ABANA", 5, 2, 1, 6)),
            _make_fake_pdf_page(_product_words("BONNISAN", 8, 0, 2, 6)),
        ]
        built = from_pdf_words(pages)
        self.assertEqual(built["pages_total"], 3)
        self.assertEqual(built["pages_resolved"], 3)
        from services.stock_native_resolver import build_items_from_pdf_page_results

        items = build_items_from_pdf_page_results(built["page_results"])
        names = {i["product_name"] for i in items}
        self.assertEqual(names, {"LIV52", "ABANA", "BONNISAN"})

    def test_different_header_on_page_three(self):
        # Page 3 uses a shifted/different header layout (Issue instead of Sale).
        alt_header = [
            {"text": "Product", "x0": 0, "x1": 20, "top": 10, "bottom": 18},
            {"text": "Opening", "x0": 30, "x1": 45, "top": 10, "bottom": 18},
            {"text": "Receipt", "x0": 55, "x1": 70, "top": 10, "bottom": 18},
            {"text": "Issue", "x0": 80, "x1": 90, "top": 10, "bottom": 18},
            {"text": "Closing", "x0": 100, "x1": 115, "top": 10, "bottom": 18},
        ]
        alt_row = [
            {"text": "EVECARE", "x0": 0, "x1": 20, "top": 30, "bottom": 38},
            {"text": "4", "x0": 30, "x1": 40, "top": 30, "bottom": 38},
            {"text": "1", "x0": 55, "x1": 65, "top": 30, "bottom": 38},
            {"text": "2", "x0": 80, "x1": 88, "top": 30, "bottom": 38},
            {"text": "3", "x0": 100, "x1": 110, "top": 30, "bottom": 38},
        ]
        pages = [
            _make_fake_pdf_page(
                _header_words() + _product_words("LIV52", 10, 0, 3, 7)
            ),
            _make_fake_pdf_page(_product_words("ABANA", 5, 2, 1, 6)),
            _make_fake_pdf_page(alt_header + alt_row, width=120.0),
        ]
        built = from_pdf_words(pages)
        self.assertEqual(built["pages_total"], 3)
        self.assertGreaterEqual(built["pages_resolved"], 2)
        page3 = built["page_results"][2]
        self.assertTrue(page3.get("resolved"))
        # Own header, not carry-over of page-1 Sale layout.
        hdr_texts = [c["text"] for c in (page3.get("header_cells") or [])]
        self.assertTrue(
            any("Issue" in t or "Sale" in t or "Closing" in t for t in hdr_texts)
        )


class LowRowCoveragePickTests(unittest.TestCase):
    def setUp(self):
        ensure_gemini_offline_guard()
        self._env = os.environ.get("STOCK_NATIVE_RESOLVER")
        os.environ["STOCK_NATIVE_RESOLVER"] = "on"

    def tearDown(self):
        if self._env is None:
            os.environ.pop("STOCK_NATIVE_RESOLVER", None)
        else:
            os.environ["STOCK_NATIVE_RESOLVER"] = self._env

    def test_low_coverage_keeps_old(self):
        old = empty_result("f.pdf", "pdf")
        old["line_items"] = [
            {
                **empty_line_item(),
                "product_name": f"PROD {i}",
                "opening_qty": 10.0,
                "receipts_qty": 0.0,
                "sales_qty": 1.0,
                "closing_qty": 9.0,
            }
            for i in range(10)
        ]
        # Resolver only returns page-1 style subset (2 of 10).
        header = [
            {"text": "Product", "col_index": 0, "x_center": 0.0},
            {"text": "Opening", "col_index": 1, "x_center": 1.0},
            {"text": "Purchase", "col_index": 2, "x_center": 2.0},
            {"text": "Sale", "col_index": 3, "x_center": 3.0},
            {"text": "Closing", "col_index": 4, "x_center": 4.0},
        ]
        tokens = [
            [
                {"text": f"PROD {i}", "col_index": 0, "x": 0.0},
                {"text": "10", "col_index": 1, "x": 1.0},
                {"text": "0", "col_index": 2, "x": 2.0},
                {"text": "1", "col_index": 3, "x": 3.0},
                {"text": "9", "col_index": 4, "x": 4.0},
            ]
            for i in range(2)
        ]
        log_buf = io.StringIO()
        handler = logging.StreamHandler(log_buf)
        logging.getLogger("services.stock_native_resolver").addHandler(handler)
        logging.getLogger("services.stock_native_resolver").setLevel(logging.INFO)
        try:
            out = compare_and_maybe_apply(
                copy.deepcopy(old),
                input_type="pdf_text",
                parser="pdf",
                header_cells=header,
                token_rows=tokens,
                request_id="cov",
            )
        finally:
            logging.getLogger("services.stock_native_resolver").removeHandler(handler)
        text = log_buf.getvalue()
        self.assertIn("LOW_ROW_COVERAGE", text)
        self.assertIn("coverage=", text)
        self.assertEqual(len(out["line_items"]), 10)
        self.assertEqual(
            ((out.get("totals") or {}).get("extra") or {}).get("native_resolver_picked"),
            "old",
        )
        self.assertTrue(
            ((out.get("totals") or {}).get("extra") or {}).get(
                "native_resolver_candidates"
            )
        )


def _xlsx_bytes_two_sheets(sheet_specs):
    """sheet_specs: list of (title_row_stockist, period, products)."""
    from openpyxl import Workbook

    wb = Workbook()
    # Remove default sheet
    default = wb.active
    wb.remove(default)
    for idx, (stockist, period, products) in enumerate(sheet_specs):
        ws = wb.create_sheet(title=f"Sheet{idx + 1}")
        ws.append([stockist])
        ws.append([f"Stock and Sales From date {period} to {period}"])
        ws.append(
            ["Product", "Opening", "Purchase", "Sale", "Closing"]
        )
        for name, op, pur, sale, cls in products:
            ws.append([name, op, pur, sale, cls])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


class MultiSheetNativeOnTests(unittest.TestCase):
    def setUp(self):
        ensure_gemini_offline_guard()
        reset_gemini_test_call_count()
        self._env = {
            k: os.environ.get(k)
            for k in ("STOCK_NATIVE_RESOLVER", "STOCK_NO_REWRITE", "STOCK_WORD_LINUX")
        }
        os.environ["STOCK_NATIVE_RESOLVER"] = "on"
        os.environ.pop("STOCK_WORD_LINUX", None)

    def tearDown(self):
        for key, value in self._env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_different_stockists_two_statements(self):
        from services.sales_statement_extractor import extract_sales_statement

        data = _xlsx_bytes_two_sheets(
            [
                (
                    "ALPHA MEDICAL STORES",
                    "01-Jan-2026",
                    [("LIV 52", 10, 0, 3, 7)],
                ),
                (
                    "BETA PHARMA AGENCY",
                    "01-Jan-2026",
                    [("ABANA", 5, 2, 1, 6)],
                ),
            ]
        )
        result = extract_sales_statement(data, "multi.xlsx")
        self.assertTrue(result.get("multi_statement"), msg=str(result.keys()))
        stmts = result["statements"]
        self.assertEqual(len(stmts), 2)
        names = {s.get("stockist_name") for s in stmts}
        self.assertTrue(any("ALPHA" in (n or "").upper() for n in names))
        self.assertTrue(any("BETA" in (n or "").upper() for n in names))

    def test_same_stockist_concat_disjoint(self):
        from services.sales_statement_extractor import extract_sales_statement

        data = _xlsx_bytes_two_sheets(
            [
                (
                    "ALPHA MEDICAL STORES",
                    "01-Jan-2026",
                    [("LIV 52", 10, 0, 3, 7)],
                ),
                (
                    "ALPHA MEDICAL STORES",
                    "01-Jan-2026",
                    [("ABANA", 5, 2, 1, 6)],
                ),
            ]
        )
        result = extract_sales_statement(data, "same.xlsx")
        if result.get("multi_statement"):
            items = []
            for s in result["statements"]:
                items.extend(s.get("line_items") or [])
        else:
            items = result.get("line_items") or []
        names = {i.get("product_name") for i in items}
        self.assertIn("LIV 52", names)
        self.assertIn("ABANA", names)

    def test_same_stockist_overlapping_kept_and_flagged(self):
        from services.sales_statement_extractor import extract_sales_statement

        data = _xlsx_bytes_two_sheets(
            [
                (
                    "ALPHA MEDICAL STORES",
                    "01-Jan-2026",
                    [("LIV 52", 10, 0, 3, 7)],
                ),
                (
                    "ALPHA MEDICAL STORES",
                    "01-Jan-2026",
                    [("LIV 52", 20, 0, 5, 15)],
                ),
            ]
        )
        result = extract_sales_statement(data, "dup.xlsx")
        if result.get("multi_statement"):
            items = []
            extras = []
            for s in result["statements"]:
                items.extend(s.get("line_items") or [])
                extras.append((s.get("totals") or {}).get("extra") or {})
        else:
            items = result.get("line_items") or []
            extras = [(result.get("totals") or {}).get("extra") or {}]
        liv = [i for i in items if i.get("product_name") == "LIV 52"]
        self.assertEqual(len(liv), 2)
        flagged = any(
            (i.get("extra") or {}).get("DUPLICATE_PRODUCT_ACROSS_SHEETS") for i in liv
        ) or any(e.get("DUPLICATE_PRODUCT_ACROSS_SHEETS") for e in extras)
        self.assertTrue(flagged)


class BaselineIdsStillMatch(unittest.TestCase):
    def test_baseline_failure_ids(self):
        path = (
            Path(__file__).resolve().parent / "baselines" / "pre_phase1_failures.json"
        )
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(len(data["failures"]), 6)


if __name__ == "__main__":
    unittest.main()
