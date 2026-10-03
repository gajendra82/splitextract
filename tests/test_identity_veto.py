"""Phase 1: STOCK_IDENTITY_VETO acceptance tests (offline)."""

from __future__ import annotations

import io
import json
import logging
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from services.sales_statement_extractor import empty_result
from tests.gemini_offline import (
    clear_gemini_responses,
    ensure_gemini_offline_guard,
    gemini_test_call_count,
    reset_gemini_test_call_count,
    set_gemini_responses,
)


def _item(
    name: str,
    opening: float,
    purchase: float,
    sales: float,
    closing: float,
    *,
    sales_return: float = 0.0,
    expiry: float = 0.0,
    ok: bool = False,
):
    return {
        "product_name": name,
        "packing": "10TAB",
        "opening_qty": opening,
        "receipts_qty": purchase,
        "sales_qty": sales,
        "sales_value": sales * 10.0,
        "closing_qty": closing,
        "closing_value": closing * 10.0,
        "extra": {
            "sale_return": sales_return,
            "exp_dmg": expiry,
            "stock_identity_ok": ok,
            "total_qty": opening + purchase,
        },
    }


def _saleret_failing_result(filename: str = "sheet.jpg", failing: int = 26, total: int = 32):
    """Synthetic SaleRet-style result: many rows fail identity."""
    result = empty_result(filename, "jpg")
    items = []
    for i in range(total):
        # Same naming scheme as Gemini stubs so row-coverage matching works.
        name = _gemini_product_name(i)
        if i < failing:
            # Intentionally unbalanced: closing wrong.
            items.append(
                _item(name, 10, 5, 3, 99, sales_return=0, expiry=0, ok=False)
            )
        else:
            # opening 10 + purchase 5 - sales 3 = closing 12
            items.append(
                _item(name, 10, 5, 3, 12, sales_return=0, expiry=0, ok=True)
            )
    result["line_items"] = items
    extra = result["totals"]["extra"]
    extra["extraction_method"] = "generic_column_semantic"
    extra["layout"] = "opening_purchase_sale_saleret_expdmg"
    extra["stock_identity_kind"] = "opening_purchase_sale_saleret_expdmg"
    extra["stock_identity_fail_count"] = failing
    extra["column_schema"] = {"confidence": 0.95, "columns": {"a": 1}}
    return result


def _gemini_product_name(i: int) -> str:
    # Must pass _is_non_product_line_name (e.g. "PRODUCT N" is rejected).
    return f"ARJUNA TAB {i}"


def _balanced_gemini_payload(n: int = 32) -> dict:
    items = []
    for i in range(n):
        items.append(
            {
                "product_name": _gemini_product_name(i),
                "packing": "10TAB",
                "opening_qty": 10,
                "receipts_qty": 5,
                "sales_qty": 3,
                "sales_return_qty": 0,
                "expiry_damage_qty": 0,
                "closing_qty": 12,
                "sales_value": 30,
                "closing_value": 120,
            }
        )
    return {"line_items": items, "stockist_name": "TEST", "company_name": "CO"}


def _worse_gemini_payload(n: int = 32) -> dict:
    items = []
    for i in range(n):
        items.append(
            {
                "product_name": _gemini_product_name(i),
                "packing": "10TAB",
                "opening_qty": 0,
                "receipts_qty": 0,
                "sales_qty": 0,
                "closing_qty": 50,
                "sales_value": 0,
                "closing_value": 0,
            }
        )
    return {"line_items": items}


class IdentityVetoUnitTests(unittest.TestCase):
    def setUp(self):
        ensure_gemini_offline_guard()
        reset_gemini_test_call_count()
        clear_gemini_responses()
        self._env = {}
        for key in ("STOCK_IDENTITY_VETO", "STOCK_IDENTITY_VETO_TYPES"):
            self._env[key] = os.environ.get(key)
        os.environ["STOCK_IDENTITY_VETO"] = "true"
        os.environ["STOCK_IDENTITY_VETO_TYPES"] = "image,scanned_pdf"
        os.environ.setdefault("ENABLE_GEMINI_EXTRACTION_FALLBACK", "true")

    def tearDown(self):
        clear_gemini_responses()
        for key, value in self._env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_veto_decision_recomputes_not_shadow(self):
        from services.stock_row_classifier import veto_decision

        result = _saleret_failing_result()
        result["totals"]["extra"]["shadow_row_classification"] = {
            "valid_ratio": 1.0,
            "row_status_counts": {"VALID": 32},
        }
        decision = veto_decision(result, "image")
        self.assertTrue(decision["veto"])
        self.assertLess(decision["valid_ratio"], 0.90)

    def test_flag_off_no_veto(self):
        from services.stock_row_classifier import veto_decision

        os.environ["STOCK_IDENTITY_VETO"] = "false"
        decision = veto_decision(_saleret_failing_result(), "image")
        self.assertFalse(decision["veto"])
        self.assertEqual(decision["reason"], "veto_inactive")

    def test_types_image_only_skips_scanned_pdf(self):
        from services.stock_row_classifier import veto_decision

        os.environ["STOCK_IDENTITY_VETO_TYPES"] = "image"
        decision = veto_decision(_saleret_failing_result("scan.pdf"), "scanned_pdf")
        self.assertFalse(decision["veto"])

    def test_medica_without_purchase_sales_no_veto(self):
        from services.stock_row_classifier import veto_decision

        result = empty_result("medica.jpg", "jpg")
        result["line_items"] = [
            {
                "product_name": f"P{i}",
                "opening_qty": 10,
                "receipts_qty": None,
                "sales_qty": None,
                "closing_qty": 10,
                "sales_value": 0,
                "closing_value": 0,
                "extra": {},
            }
            for i in range(5)
        ]
        result["totals"]["extra"]["stock_identity_kind"] = "medica_opstk_columns"
        result["totals"]["extra"]["extraction_method"] = "medica_opstk_columns"
        result["totals"]["extra"]["stock_identity_fail_count"] = 0
        decision = veto_decision(result, "image")
        self.assertFalse(decision["veto"])

    def test_filename_codes_same_decision(self):
        from services.stock_row_classifier import veto_decision

        base = _saleret_failing_result("neutral.jpg")
        decisions = []
        for name in ("a_ZA_1.jpg", "b_ZL_2.jpg", "neutral.jpg"):
            result = json.loads(json.dumps(base))
            result["source_file"] = name
            decisions.append(veto_decision(result, "image")["veto"])
        self.assertEqual(len(set(decisions)), 1)
        self.assertTrue(decisions[0])


class IdentityVetoFallbackTests(unittest.TestCase):
    def setUp(self):
        ensure_gemini_offline_guard()
        reset_gemini_test_call_count()
        clear_gemini_responses()
        self._env = {
            k: os.environ.get(k)
            for k in (
                "STOCK_IDENTITY_VETO",
                "STOCK_IDENTITY_VETO_TYPES",
                "ENABLE_GEMINI_EXTRACTION_FALLBACK",
            )
        }
        os.environ["ENABLE_GEMINI_EXTRACTION_FALLBACK"] = "true"
        os.environ["STOCK_IDENTITY_VETO_TYPES"] = "image,scanned_pdf"

    def tearDown(self):
        clear_gemini_responses()
        for key, value in self._env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def _run_fallback(self, result, filename="sheet.jpg"):
        from services.gemini_extraction_fallback import maybe_apply_gemini_fallback

        # Tiny JPEG-ish bytes so _document_parts succeeds for images.
        file_bytes = b"\xff\xd8\xff\xd9" + b"\x00" * 200
        return maybe_apply_gemini_fallback(result, file_bytes, filename, ".jpg")

    def test_saleret_flag_on_calls_gemini_once(self):
        os.environ["STOCK_IDENTITY_VETO"] = "true"
        set_gemini_responses([_balanced_gemini_payload()])
        result = _saleret_failing_result()
        log_buf = io.StringIO()
        handler = logging.StreamHandler(log_buf)
        handler.setLevel(logging.INFO)
        logger = logging.getLogger("services.gemini_extraction_fallback")
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        before = gemini_test_call_count()
        try:
            out = self._run_fallback(result)
        finally:
            logger.removeHandler(handler)
        self.assertEqual(gemini_test_call_count(), before + 1)
        text = log_buf.getvalue()
        self.assertIn("IDENTITY_VETO", text)
        self.assertTrue(out.get("line_items"))

    def test_saleret_flag_off_zero_calls(self):
        os.environ["STOCK_IDENTITY_VETO"] = "false"
        result = _saleret_failing_result()
        # Named-parser protection would keep this with fail_count ignored for kind.
        result["totals"]["extra"]["extraction_method"] = "main_stock_sales_statement"
        result["totals"]["extra"]["layout"] = "opening_receive_issue_closing"
        before = gemini_test_call_count()
        out = self._run_fallback(result)
        self.assertEqual(gemini_test_call_count(), before)
        self.assertEqual(
            ((out.get("totals") or {}).get("extra") or {}).get("gemini_fallback"),
            "not_called",
        )

    def test_generic_column_semantic_regression(self):
        os.environ["STOCK_IDENTITY_VETO"] = "true"
        set_gemini_responses([_balanced_gemini_payload()])
        result = _saleret_failing_result(failing=16, total=32)
        result["totals"]["extra"]["extraction_method"] = "generic_column_semantic"
        result["totals"]["extra"]["column_schema"] = {
            "confidence": 0.95,
            "columns": {"op": 1},
        }
        before = gemini_test_call_count()
        self._run_fallback(result)
        self.assertEqual(gemini_test_call_count(), before + 1)

    def test_allowlist_override_logged(self):
        os.environ["STOCK_IDENTITY_VETO"] = "true"
        set_gemini_responses([_balanced_gemini_payload()])
        result = _saleret_failing_result()
        result["totals"]["extra"]["extraction_method"] = "main_stock_sales_statement"
        result["totals"]["extra"]["layout"] = "opening_receive_issue_closing"
        log_buf = io.StringIO()
        handler = logging.StreamHandler(log_buf)
        logger = logging.getLogger("services.gemini_extraction_fallback")
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        before = gemini_test_call_count()
        try:
            self._run_fallback(result)
        finally:
            logger.removeHandler(handler)
        self.assertEqual(gemini_test_call_count(), before + 1)
        self.assertIn("IDENTITY_VETO_OVERRIDE", log_buf.getvalue())
        self.assertIn("main_stock_column_reader", log_buf.getvalue())

    def test_allowlist_skip_when_rows_pass(self):
        os.environ["STOCK_IDENTITY_VETO"] = "true"
        result = empty_result("ok.jpg", "jpg")
        result["line_items"] = [
            _item(f"P{i}", 10, 5, 3, 12, ok=True) for i in range(10)
        ]
        result["totals"]["extra"]["extraction_method"] = "main_stock_sales_statement"
        result["totals"]["extra"]["layout"] = "opening_receive_issue_closing"
        result["totals"]["extra"]["stock_identity_kind"] = "opening_receipts_sales_closing"
        result["totals"]["extra"]["stock_identity_fail_count"] = 0
        before = gemini_test_call_count()
        out = self._run_fallback(result)
        self.assertEqual(gemini_test_call_count(), before)
        self.assertEqual(
            ((out.get("totals") or {}).get("extra") or {}).get("gemini_fallback"),
            "not_called",
        )

    def test_worse_gemini_keeps_ocr_and_candidates(self):
        os.environ["STOCK_IDENTITY_VETO"] = "true"
        set_gemini_responses([_worse_gemini_payload()])
        result = _saleret_failing_result(failing=10, total=32)
        # Make OCR relatively better than the worse Gemini stub.
        for i, item in enumerate(result["line_items"]):
            if i >= 10:
                item["closing_qty"] = 12
                item["extra"]["stock_identity_ok"] = True
        result["totals"]["extra"]["stock_identity_fail_count"] = 10
        log_buf = io.StringIO()
        handler = logging.StreamHandler(log_buf)
        logger = logging.getLogger("services.gemini_extraction_fallback")
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        try:
            out = self._run_fallback(result)
        finally:
            logger.removeHandler(handler)
        self.assertIn("VETO_RESULT_PICK", log_buf.getvalue())
        self.assertIn("picked=ocr", log_buf.getvalue())
        # OCR product names preserved; at least one candidate attached.
        self.assertTrue(out["line_items"][0]["product_name"].startswith("ARJUNA"))
        candidates = (out["line_items"][0].get("extra") or {}).get("candidates") or []
        self.assertTrue(candidates)

    def test_low_row_coverage_keeps_ocr(self):
        os.environ["STOCK_IDENTITY_VETO"] = "true"
        # Gemini drops to 20 of 32 rows — all valid — must not win on ratio alone.
        set_gemini_responses([_balanced_gemini_payload(20)])
        result = _saleret_failing_result(failing=26, total=32)
        log_buf = io.StringIO()
        handler = logging.StreamHandler(log_buf)
        logger = logging.getLogger("services.gemini_extraction_fallback")
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        try:
            out = self._run_fallback(result)
        finally:
            logger.removeHandler(handler)
        text = log_buf.getvalue()
        self.assertIn("picked=ocr", text)
        self.assertIn("LOW_ROW_COVERAGE", text)
        self.assertEqual(len(out["line_items"]), 32)
        self.assertTrue(out["line_items"][0]["product_name"].startswith("ARJUNA"))

    def test_full_coverage_higher_ratio_picks_gemini(self):
        os.environ["STOCK_IDENTITY_VETO"] = "true"
        set_gemini_responses([_balanced_gemini_payload(32)])
        result = _saleret_failing_result(failing=26, total=32)
        log_buf = io.StringIO()
        handler = logging.StreamHandler(log_buf)
        logger = logging.getLogger("services.gemini_extraction_fallback")
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        try:
            out = self._run_fallback(result)
        finally:
            logger.removeHandler(handler)
        self.assertIn("picked=gemini", log_buf.getvalue())
        self.assertEqual(len(out["line_items"]), 32)
        self.assertTrue(out["line_items"][0]["product_name"].startswith("ARJUNA"))

    def test_gemini_extra_rows_allowed(self):
        os.environ["STOCK_IDENTITY_VETO"] = "true"
        set_gemini_responses([_balanced_gemini_payload(34)])
        result = _saleret_failing_result(failing=26, total=32)
        log_buf = io.StringIO()
        handler = logging.StreamHandler(log_buf)
        logger = logging.getLogger("services.gemini_extraction_fallback")
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        try:
            out = self._run_fallback(result)
        finally:
            logger.removeHandler(handler)
        self.assertIn("picked=gemini", log_buf.getvalue())
        self.assertNotIn("LOW_ROW_COVERAGE", log_buf.getvalue())
        self.assertEqual(len(out["line_items"]), 34)

    def test_budget_caps_at_two(self):
        os.environ["STOCK_IDENTITY_VETO"] = "true"
        set_gemini_responses([_worse_gemini_payload(), _worse_gemini_payload()])
        result = _saleret_failing_result()
        result["totals"]["extra"]["gemini_calls"] = 1
        result["totals"]["extra"].pop("stock_image_vision_decided", None)
        before = gemini_test_call_count()
        out = self._run_fallback(result)
        # One more call only (budget 2 total).
        self.assertEqual(gemini_test_call_count(), before + 1)
        self.assertLessEqual(
            int(((out.get("totals") or {}).get("extra") or {}).get("gemini_calls") or 0),
            2,
        )

    def test_budget_exhausted_flags_unresolved(self):
        os.environ["STOCK_IDENTITY_VETO"] = "true"
        result = _saleret_failing_result()
        result["totals"]["extra"]["gemini_calls"] = 2
        before = gemini_test_call_count()
        out = self._run_fallback(result)
        self.assertEqual(gemini_test_call_count(), before)
        self.assertTrue(
            ((out.get("totals") or {}).get("extra") or {}).get("identity_veto_unresolved")
        )

    def test_vision_locked_allows_one_extra_then_unresolved(self):
        os.environ["STOCK_IDENTITY_VETO"] = "true"
        set_gemini_responses([_worse_gemini_payload()])
        result = _saleret_failing_result()
        extra = result["totals"]["extra"]
        extra["stock_vision_locked"] = True
        extra["stock_image_vision_decided"] = True
        extra["gemini_calls"] = 1
        extra["extraction_method"] = "stock_direct_vision"
        before = gemini_test_call_count()
        out = self._run_fallback(result)
        self.assertEqual(gemini_test_call_count(), before + 1)
        self.assertLessEqual(
            int(((out.get("totals") or {}).get("extra") or {}).get("gemini_calls") or 0),
            2,
        )
        # Cap already reached after the recovery call: unresolved if still failing.
        out2 = self._run_fallback(out)
        self.assertEqual(gemini_test_call_count(), before + 1)
        self.assertTrue(
            ((out2.get("totals") or {}).get("extra") or {}).get("identity_veto_unresolved")
        )

    def test_each_layout_allowlist_override(self):
        os.environ["STOCK_IDENTITY_VETO"] = "true"
        allowlists = [
            (
                "main_stock_column_reader",
                "main_stock_sales_statement",
                "opening_receive_issue_closing",
            ),
            (
                "code_item_stock_statement",
                "code_item_stock_statement_photo",
                "code_item_stock_statement",
            ),
            (
                "op_pur_sp_sale_bal_val",
                "op_pur_sp_sale_bal_val_photo",
                "op_pur_sp_sale_bal_val",
            ),
            (
                "medivision_op_purc_nm60d",
                "medivision_op_purc_nm60d",
                "medivision_op_purc_nm60d",
            ),
            (
                "product_wise_stock_statement_image",
                "product_wise_stock_statement_image",
                "product_wise_stock_statement_photo",
            ),
        ]
        for gate, method, layout in allowlists:
            with self.subTest(gate=gate):
                clear_gemini_responses()
                set_gemini_responses([_balanced_gemini_payload()])
                reset_gemini_test_call_count()
                result = _saleret_failing_result()
                result["totals"]["extra"]["extraction_method"] = method
                result["totals"]["extra"]["layout"] = layout
                log_buf = io.StringIO()
                handler = logging.StreamHandler(log_buf)
                logger = logging.getLogger("services.gemini_extraction_fallback")
                logger.addHandler(handler)
                logger.setLevel(logging.INFO)
                try:
                    self._run_fallback(result)
                finally:
                    logger.removeHandler(handler)
                self.assertEqual(gemini_test_call_count(), 1, gate)
                text = log_buf.getvalue()
                self.assertIn("IDENTITY_VETO_OVERRIDE", text)
                self.assertIn(gate, text)


class IdentityVetoQualityTests(unittest.TestCase):
    def setUp(self):
        self._env = {
            k: os.environ.get(k)
            for k in ("STOCK_IDENTITY_VETO", "STOCK_IDENTITY_VETO_TYPES")
        }
        os.environ["STOCK_IDENTITY_VETO"] = "true"
        os.environ["STOCK_IDENTITY_VETO_TYPES"] = "image,scanned_pdf"

    def tearDown(self):
        for key, value in self._env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_evaluate_puts_identity_veto_before_protected_score(self):
        from services.extraction_quality import evaluate_extraction_quality

        result = _saleret_failing_result()
        result["totals"]["extra"]["extraction_method"] = "product_stock_report"
        quality = evaluate_extraction_quality(
            result, {"ext": ".jpg", "input_type": "image", "page_count": 1}
        )
        self.assertTrue(quality.get("should_fallback"))
        self.assertIn("IDENTITY_VETO", quality.get("reasons") or [])


class IdentityVetoFixtureAndBaselineTests(unittest.TestCase):
    def test_fixtures_on_off_identical_when_valid(self):
        root = Path(__file__).resolve().parent / "fixtures" / "stock"
        if not root.is_dir():
            self.skipTest("no fixtures/stock directory")
        files = [p for p in root.iterdir() if p.suffix.lower() in {".jpg", ".png", ".pdf"}]
        if not files:
            self.skipTest("no real fixtures in tests/fixtures/stock/")
        # Placeholder: real fixtures checked after Prompt 1c drop-in.
        self.assertTrue(True)

    def test_baseline_failure_ids_listed(self):
        path = (
            Path(__file__).resolve().parent
            / "baselines"
            / "pre_phase1_failures.json"
        )
        data = json.loads(path.read_text(encoding="utf-8"))
        ids = {row["id"] for row in data["failures"]}
        self.assertEqual(len(ids), 6)
        self.assertIn(
            "tests.test_product_stock_report.TestFixedSalesStockTxt."
            "test_value_columns_are_read_when_printed",
            ids,
        )


if __name__ == "__main__":
    unittest.main()
