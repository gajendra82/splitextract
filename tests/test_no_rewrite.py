"""Phase 3a STOCK_NO_REWRITE + Part 0 vision-safe sanitize (offline)."""

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
    _main_stock_repair_issue_qty,
    _psr_fill_missing_sales,
    _psr_overlay_ocr_qty,
    _psr_repair_cls_amt_scale,
    _psr_repair_qty_identity,
    _repair_code_item_photo_qty_columns,
    _repair_marg_mexp_missing_issue,
    _repair_medivision_nm60_closing,
    _repair_medivision_op_wsale_sales,
    _repair_opbal_qty_tuple,
    _repair_pack_op_pur_bal_dropped_receipts,
    _repair_pack_op_pur_bal_footer_leaked_into_rows,
    _repair_pack_op_pur_bal_money_as_qty,
    _repair_pack_op_pur_bal_ss_filed_as_sp,
    _repair_pharma_hub_jun_jul_sales,
    _sanitize_compute_totals,
    _sanitize_numeric_overrides,
    _sanitize_rows_and_text,
    _sanitize_statement_financials,
    _sanitize_statement_financials_vision_safe,
    _a2z_balanced_qtys,
    _pwss_photo_repair_row,
)
from services.stock_row_classifier import (
    apply_repairs,
    record_candidate,
    reset_no_rewrite_counts,
    take_no_rewrite_counts,
)
from tests.gemini_offline import ensure_gemini_offline_guard


def _qty_snapshot(item):
    return {
        "opening_qty": item.get("opening_qty"),
        "receipts_qty": item.get("receipts_qty"),
        "sales_qty": item.get("sales_qty"),
        "sales_value": item.get("sales_value"),
        "closing_qty": item.get("closing_qty"),
        "closing_value": item.get("closing_value"),
    }


class SanitizeSplitTests(unittest.TestCase):
    def test_old_path_byte_identical_to_helpers(self):
        result = empty_result("x.jpg", "jpg")
        result["line_items"] = [
            {
                **empty_line_item(),
                "product_name": "LIV 52",
                "opening_qty": 10.0,
                "sales_qty": 3.0,
                "closing_qty": 7.0,
                "sales_value": 75374123024.0,  # implausible
                "closing_value": 100.0,
            }
        ]
        result["totals"]["sales_value"] = 75374123024.0
        result["totals"]["closing_value"] = 100.0
        a = copy.deepcopy(result)
        b = copy.deepcopy(result)
        out_a = _sanitize_statement_financials(a)
        # Manual composition must match the orchestrator.
        b = _sanitize_rows_and_text(b)
        b = _sanitize_numeric_overrides(b, report_only=False, stage="psr")
        b = _sanitize_compute_totals(b)
        b = _sanitize_numeric_overrides(b, report_only=False, stage="lines")
        self.assertEqual(json.dumps(out_a, sort_keys=True), json.dumps(b, sort_keys=True))
        self.assertEqual(out_a["line_items"][0]["sales_value"], 0.0)

    def test_vision_safe_keeps_line_money_and_records_candidates(self):
        result = empty_result("x.jpg", "jpg")
        result["totals"]["extra"]["extraction_method"] = "vision_table"
        result["totals"]["extra"]["vision_table_final"] = True
        result["line_items"] = [
            {
                **empty_line_item(),
                "product_name": "LIV 52",
                "opening_qty": 10.0,
                "sales_qty": 3.0,
                "closing_qty": 7.0,
                "sales_value": 75374123024.0,
                "closing_value": 50.0,
            }
        ]
        result["totals"]["sales_value"] = 75374123024.0
        out = _sanitize_statement_financials_vision_safe(copy.deepcopy(result))
        item = out["line_items"][0]
        self.assertEqual(item["sales_value"], 75374123024.0)
        cands = (item.get("extra") or {}).get("candidates") or []
        self.assertTrue(cands)
        self.assertEqual(cands[0]["source"], "_sanitize_numeric_overrides")
        self.assertEqual(cands[0]["fields"].get("sales_value"), 0.0)
        # (b) totals present
        self.assertIn("line_sales_value_sum", out["totals"]["extra"])
        self.assertIn("sales_qty", out["totals"]["extra"])

    def test_vision_and_old_top_level_key_sets(self):
        base = empty_result("x.jpg", "jpg")
        base["line_items"] = [
            {
                **empty_line_item(),
                "product_name": "A",
                "opening_qty": 5.0,
                "sales_qty": 2.0,
                "closing_qty": 3.0,
                "sales_value": 20.0,
                "closing_value": 30.0,
            }
        ]
        old = _sanitize_statement_financials(copy.deepcopy(base))
        vision = copy.deepcopy(base)
        vision["totals"]["extra"]["extraction_method"] = "vision_table"
        vision["totals"]["extra"]["vision_table_final"] = True
        vision = _sanitize_statement_financials_vision_safe(vision)
        self.assertEqual(set(old.keys()), set(vision.keys()))
        self.assertEqual(set(old["totals"].keys()), set(vision["totals"].keys()))
        # Laravel-facing keys always present after sanitize.
        for key in (
            "source_file",
            "stockist_name",
            "period_from",
            "period_to",
            "line_items",
            "totals",
        ):
            self.assertIn(key, vision)


class NoRewriteHelpersTests(unittest.TestCase):
    def setUp(self):
        ensure_gemini_offline_guard()
        reset_no_rewrite_counts()
        self._env = os.environ.get("STOCK_NO_REWRITE")
        os.environ.pop("STOCK_NO_REWRITE", None)

    def tearDown(self):
        if self._env is None:
            os.environ.pop("STOCK_NO_REWRITE", None)
        else:
            os.environ["STOCK_NO_REWRITE"] = self._env
        reset_no_rewrite_counts()

    def test_apply_repairs_tracks_flag(self):
        os.environ.pop("STOCK_NO_REWRITE", None)
        self.assertTrue(apply_repairs())
        os.environ["STOCK_NO_REWRITE"] = "true"
        self.assertFalse(apply_repairs())

    def test_column_absent_fill_vs_empty_cell(self):
        os.environ["STOCK_NO_REWRITE"] = "true"
        # No sales column at all → fill with derived.
        item = empty_line_item()
        item["product_name"] = "X"
        item["opening_qty"] = 10.0
        item["receipts_qty"] = 0.0
        item["sales_qty"] = None
        item["closing_qty"] = 7.0
        # read_row_fields treats None as missing; after fill sales=3 → VALID.
        item.pop("sales_qty", None)
        record_candidate(
            item,
            {"sales_qty": 3.0},
            "_test_absent",
            "fill_absent_sales",
            column_absent_fields=("sales_qty",),
        )
        self.assertEqual(item["sales_qty"], 3.0)
        self.assertEqual((item["extra"].get("field_source") or {}).get("sales_qty"), "derived")
        self.assertTrue(item["extra"].get("candidates"))

        # Sales column present but empty cell → must NOT fill; MISSING_VALUE.
        item2 = empty_line_item()
        item2["product_name"] = "Y"
        item2["opening_qty"] = 10.0
        item2["receipts_qty"] = None  # no purchase column / empty
        item2["sales_qty"] = None  # sales column present, empty cell
        item2["closing_qty"] = 7.0
        before = _qty_snapshot(item2)
        record_candidate(
            item2,
            {"sales_qty": 3.0},
            "_test_empty_cell",
            "would_fill",
            column_absent_fields=(),
        )
        self.assertEqual(_qty_snapshot(item2), before)
        self.assertEqual(item2["extra"].get("row_status"), "MISSING_VALUE")
        self.assertTrue(item2["extra"].get("candidates"))


def _run_flag_off_on(fn, build_input, apply_fn):
    """Return (off_out, on_out, on_item) for a repair that mutates items."""
    os.environ.pop("STOCK_NO_REWRITE", None)
    reset_no_rewrite_counts()
    off_in = build_input()
    apply_fn(off_in)
    off_out = copy.deepcopy(off_in)

    os.environ["STOCK_NO_REWRITE"] = "true"
    reset_no_rewrite_counts()
    on_in = build_input()
    printed = copy.deepcopy(on_in)
    apply_fn(on_in)
    return off_out, on_in, printed


class RepairFunctionParamTests(unittest.TestCase):
    def setUp(self):
        ensure_gemini_offline_guard()
        reset_no_rewrite_counts()
        self._env = os.environ.get("STOCK_NO_REWRITE")

    def tearDown(self):
        if self._env is None:
            os.environ.pop("STOCK_NO_REWRITE", None)
        else:
            os.environ["STOCK_NO_REWRITE"] = self._env
        reset_no_rewrite_counts()

    def test_main_stock_repair_issue_qty(self):
        def build():
            item = empty_line_item()
            item["product_name"] = "GEL"
            item["opening_qty"] = 10.0
            item["receipts_qty"] = 0.0
            item["sales_qty"] = 0.0
            item["closing_qty"] = 7.0
            return item

        os.environ.pop("STOCK_NO_REWRITE", None)
        off = build()
        _main_stock_repair_issue_qty(off)
        self.assertEqual(off["sales_qty"], 3.0)

        os.environ["STOCK_NO_REWRITE"] = "true"
        on = build()
        _main_stock_repair_issue_qty(on)
        self.assertEqual(on["sales_qty"], 0.0)
        cands = (on.get("extra") or {}).get("candidates") or []
        self.assertEqual(len(cands), 1)
        self.assertEqual(cands[0]["source"], "_main_stock_repair_issue_qty")
        self.assertEqual(cands[0]["fields"]["sales_qty"], 3.0)
        self.assertNotEqual((on.get("extra") or {}).get("row_status"), "VALID")

    def test_main_stock_missing_issue_regression(self):
        """Flag ON leaves parser values; old arithmetic guess is a candidate."""
        os.environ["STOCK_NO_REWRITE"] = "true"
        item = empty_line_item()
        item["product_name"] = "TAB"
        item["opening_qty"] = 52.0
        item["receipts_qty"] = 0.0
        item["sales_qty"] = 0.0  # missing ISSUE token
        item["closing_qty"] = 18.0
        printed = _qty_snapshot(item)
        _main_stock_repair_issue_qty(item)
        self.assertEqual(_qty_snapshot(item), printed)
        cands = (item.get("extra") or {}).get("candidates") or []
        self.assertEqual(cands[0]["fields"]["sales_qty"], 34.0)
        self.assertNotEqual(item["extra"].get("row_status"), "VALID")

    def test_repair_code_item_photo_qty_columns(self):
        def build():
            result = empty_result("x.jpg", "jpg")
            item = empty_line_item()
            item["product_name"] = "A"
            item["opening_qty"] = 0.0
            item["receipts_qty"] = 10.0
            item["sales_qty"] = 3.0
            item["closing_qty"] = 7.0
            result["line_items"] = [item]
            return result

        os.environ.pop("STOCK_NO_REWRITE", None)
        off = build()
        _repair_code_item_photo_qty_columns(off)
        self.assertEqual(off["line_items"][0]["opening_qty"], 10.0)
        self.assertEqual(off["line_items"][0]["receipts_qty"], 0.0)

        os.environ["STOCK_NO_REWRITE"] = "true"
        on = build()
        printed = _qty_snapshot(on["line_items"][0])
        _repair_code_item_photo_qty_columns(on)
        self.assertEqual(_qty_snapshot(on["line_items"][0]), printed)
        cands = on["line_items"][0]["extra"]["candidates"]
        self.assertEqual(cands[0]["source"], "_repair_code_item_photo_qty_columns")

    def test_repair_marg_mexp_missing_issue(self):
        def build():
            item = empty_line_item()
            item["opening_qty"] = 0.0
            item["receipts_qty"] = 0.0
            item["sales_qty"] = 2.0
            item["closing_qty"] = 0.0
            return [item]

        os.environ.pop("STOCK_NO_REWRITE", None)
        off = build()
        _repair_marg_mexp_missing_issue(off)
        self.assertEqual(off[0]["opening_qty"], 2.0)

        os.environ["STOCK_NO_REWRITE"] = "true"
        on = build()
        _repair_marg_mexp_missing_issue(on)
        self.assertEqual(on[0]["opening_qty"], 0.0)
        self.assertEqual(
            on[0]["extra"]["candidates"][0]["source"],
            "_repair_marg_mexp_missing_issue",
        )

    def test_repair_medivision_nm60_closing(self):
        def build():
            item = empty_line_item()
            item["opening_qty"] = 10.0
            item["receipts_qty"] = 0.0
            item["sales_qty"] = 3.0
            item["closing_qty"] = 60.0  # NM60 parked here
            item["extra"] = {"nm60_qty": 60.0}
            return item

        os.environ.pop("STOCK_NO_REWRITE", None)
        off = build()
        _repair_medivision_nm60_closing(off)
        self.assertEqual(off["closing_qty"], 7.0)

        os.environ["STOCK_NO_REWRITE"] = "true"
        on = build()
        _repair_medivision_nm60_closing(on)
        self.assertEqual(on["closing_qty"], 60.0)
        self.assertEqual(
            on["extra"]["candidates"][0]["source"], "_repair_medivision_nm60_closing"
        )

    def test_repair_medivision_op_wsale_sales(self):
        def build():
            item = empty_line_item()
            item["opening_qty"] = 10.0
            item["receipts_qty"] = 0.0
            item["sales_qty"] = 5.0  # wrong side column
            item["closing_qty"] = 10.0
            item["extra"] = {"wholesale_qty": 5.0}
            return item

        os.environ.pop("STOCK_NO_REWRITE", None)
        off = build()
        _repair_medivision_op_wsale_sales(off)
        self.assertEqual(off["sales_qty"], 0.0)

        os.environ["STOCK_NO_REWRITE"] = "true"
        on = build()
        _repair_medivision_op_wsale_sales(on)
        self.assertEqual(on["sales_qty"], 5.0)
        self.assertEqual(
            on["extra"]["candidates"][0]["source"],
            "_repair_medivision_op_wsale_sales",
        )

    def test_repair_pharma_hub_jun_jul_sales(self):
        def build():
            item = empty_line_item()
            item["opening_qty"] = 10.0
            item["receipts_qty"] = 0.0
            item["sales_qty"] = 4.0
            item["closing_qty"] = 10.0
            item["extra"] = {"june_sale_qty": 4.0}
            return item

        os.environ.pop("STOCK_NO_REWRITE", None)
        off = build()
        _repair_pharma_hub_jun_jul_sales(off)
        self.assertEqual(off["sales_qty"], 0.0)

        os.environ["STOCK_NO_REWRITE"] = "true"
        on = build()
        _repair_pharma_hub_jun_jul_sales(on)
        self.assertEqual(on["sales_qty"], 4.0)
        self.assertEqual(
            on["extra"]["candidates"][0]["source"],
            "_repair_pharma_hub_jun_jul_sales",
        )

    def test_pack_op_pur_bal_dropped_receipts(self):
        def build():
            result = empty_result("x.jpg", "jpg")
            item = empty_line_item()
            item["opening_qty"] = 100.0
            item["receipts_qty"] = 0.0
            item["sales_qty"] = 20.0
            item["closing_qty"] = 130.0  # implies Pur=50
            result["line_items"] = [item]
            return result

        os.environ.pop("STOCK_NO_REWRITE", None)
        off = build()
        _repair_pack_op_pur_bal_dropped_receipts(off)
        self.assertEqual(off["line_items"][0]["receipts_qty"], 50.0)

        os.environ["STOCK_NO_REWRITE"] = "true"
        on = build()
        _repair_pack_op_pur_bal_dropped_receipts(on)
        self.assertEqual(on["line_items"][0]["receipts_qty"], 0.0)
        self.assertEqual(
            on["line_items"][0]["extra"]["candidates"][0]["source"],
            "_repair_pack_op_pur_bal_dropped_receipts",
        )

    def test_pack_op_pur_bal_ss_filed_as_sp(self):
        def build():
            result = empty_result("x.jpg", "jpg")
            item = empty_line_item()
            item["opening_qty"] = 10.0
            item["receipts_qty"] = 0.0
            item["sales_qty"] = 2.0
            item["closing_qty"] = 7.0  # needs SS=1
            item["extra"] = {"purchase_scheme_qty": 1.0, "sales_scheme_qty": 0.0}
            result["line_items"] = [item]
            return result

        os.environ.pop("STOCK_NO_REWRITE", None)
        off = build()
        _repair_pack_op_pur_bal_ss_filed_as_sp(off)
        self.assertEqual(off["line_items"][0]["extra"]["sales_scheme_qty"], 1.0)

        os.environ["STOCK_NO_REWRITE"] = "true"
        on = build()
        _repair_pack_op_pur_bal_ss_filed_as_sp(on)
        self.assertEqual(on["line_items"][0]["extra"]["sales_scheme_qty"], 0.0)
        self.assertEqual(
            on["line_items"][0]["extra"]["candidates"][0]["source"],
            "_repair_pack_op_pur_bal_ss_filed_as_sp",
        )

    def test_psr_repair_cls_amt_scale(self):
        def build():
            item = empty_line_item()
            item["closing_qty"] = 10.0
            item["closing_value"] = 721471.0  # should be 7214.71
            item2 = empty_line_item()
            item2["closing_qty"] = 10.0
            item2["closing_value"] = 600.0
            return [item, item2]

        os.environ.pop("STOCK_NO_REWRITE", None)
        off = build()
        _psr_repair_cls_amt_scale(off)
        self.assertLess(off[0]["closing_value"], 10000)

        os.environ["STOCK_NO_REWRITE"] = "true"
        on = build()
        _psr_repair_cls_amt_scale(on)
        self.assertEqual(on[0]["closing_value"], 721471.0)
        self.assertEqual(
            on[0]["extra"]["candidates"][0]["source"], "_psr_repair_cls_amt_scale"
        )

    def test_repair_opbal_qty_tuple_no_rewrite(self):
        use = [40.0, 2.0, 2.0, 5.0, 37.0]  # total 2 truncated from 42
        os.environ.pop("STOCK_NO_REWRITE", None)
        off = _repair_opbal_qty_tuple(list(use))
        self.assertEqual(off[2], 42.0)

        os.environ["STOCK_NO_REWRITE"] = "true"
        on = _repair_opbal_qty_tuple(list(use))
        self.assertEqual(on[2], 2.0)

    def test_pwss_photo_repair_row_no_rewrite(self):
        # Closing digit drop 117 → 17
        args = (100.0, 20.0, 0.0, 3.0, 0.0, 0.0, 17.0)
        os.environ.pop("STOCK_NO_REWRITE", None)
        off = _pwss_photo_repair_row(*args)
        self.assertEqual(off[6], 117.0)

        os.environ["STOCK_NO_REWRITE"] = "true"
        on = _pwss_photo_repair_row(*args)
        self.assertEqual(on[6], 17.0)

    def test_no_rewrite_log_line(self):
        from services.sales_statement_extractor import extract_sales_statement

        os.environ["STOCK_NO_REWRITE"] = "true"
        reset_no_rewrite_counts()
        log_buf = io.StringIO()
        handler = logging.StreamHandler(log_buf)
        logger = logging.getLogger("services.sales_statement_extractor")
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        item = empty_line_item()
        item["opening_qty"] = 10.0
        item["sales_qty"] = 0.0
        item["closing_qty"] = 7.0
        try:
            with patch(
                "services.sales_statement_extractor._parse_image",
                side_effect=lambda *a, **k: (
                    _main_stock_repair_issue_qty(item) or None,
                    empty_result("x.jpg", "jpg"),
                )[1],
            ):
                # Drive extract so the NO_REWRITE flush runs; seed a suppression first.
                _main_stock_repair_issue_qty(item)
                sentinel = empty_result("x.jpg", "jpg")
                sentinel["line_items"] = [item]
                with patch(
                    "services.sales_statement_extractor._parse_image",
                    return_value=sentinel,
                ):
                    extract_sales_statement(b"\xff\xd8\xff\xd9", "x.jpg")
        finally:
            logger.removeHandler(handler)
        text = log_buf.getvalue()
        self.assertIn("NO_REWRITE", text)
        self.assertIn("suppressed=", text)
        self.assertIn("_main_stock_repair_issue_qty", text)


class BaselineIdsStillMatch(unittest.TestCase):
    def test_baseline_failure_ids(self):
        path = (
            Path(__file__).resolve().parent / "baselines" / "pre_phase1_failures.json"
        )
        data = json.loads(path.read_text(encoding="utf-8"))
        ids = {row["id"] for row in data["failures"]}
        self.assertEqual(len(ids), 6)


if __name__ == "__main__":
    unittest.main()
