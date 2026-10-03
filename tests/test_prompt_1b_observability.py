"""Prompt 1b: offline Gemini guard, joinable shadow logs, corrupt PDF soft-fail."""

from __future__ import annotations

import io
import json
import logging
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.gemini_offline import (
    GeminiOfflineBlocked,
    ensure_gemini_offline_guard,
    gemini_test_call_count,
    live_gemini_allowed,
    reset_gemini_test_call_count,
)


class GeminiOfflineGuardTests(unittest.TestCase):
    def setUp(self):
        # Guard must stay off-by-default; never inherit a live-allow from the env.
        self.assertFalse(
            live_gemini_allowed(),
            "STOCK_TEST_ALLOW_LIVE_GEMINI must not be set during the suite",
        )
        self.assertNotIn(
            os.getenv("STOCK_TEST_ALLOW_LIVE_GEMINI", "").strip().lower(),
            {"1", "true", "yes", "on"},
        )
        ensure_gemini_offline_guard()
        reset_gemini_test_call_count()

    def test_allow_live_flag_defaults_off(self):
        self.assertFalse(live_gemini_allowed())
        example = Path(__file__).resolve().parents[1] / ".env.example"
        if example.is_file():
            text = example.read_text(encoding="utf-8", errors="ignore")
            for line in text.splitlines():
                if "STOCK_TEST_ALLOW_LIVE_GEMINI" in line and not line.strip().startswith(
                    "#"
                ):
                    self.fail(f".env.example must not enable live Gemini: {line}")

    def test_vertex_path_blocked_and_counted(self):
        import services.vertex_gemini_client as vertex_mod

        before = gemini_test_call_count()
        with self.assertRaises(GeminiOfflineBlocked):
            vertex_mod.generate_content_via_vertex(
                model="gemini-test",
                payload={"contents": []},
                timeout=5,
            )
        self.assertEqual(gemini_test_call_count(), before + 1)

    def test_sales_vertex_path_blocked_and_counted(self):
        from services.sales_extraction_runtime import sales_generate_content_via_vertex

        before = gemini_test_call_count()
        with self.assertRaises(GeminiOfflineBlocked):
            sales_generate_content_via_vertex(
                model="gemini-test",
                payload={"contents": []},
                timeout=5,
                label="unit_test",
            )
        self.assertEqual(gemini_test_call_count(), before + 1)

    def test_app_binding_blocked(self):
        import app as app_module

        before = gemini_test_call_count()
        with self.assertRaises(GeminiOfflineBlocked):
            app_module.generate_content_via_vertex(
                model="gemini-test",
                payload={"contents": []},
                timeout=5,
            )
        self.assertEqual(gemini_test_call_count(), before + 1)

    def test_guard_rearms_after_test_replaces_leaf(self):
        import services.vertex_gemini_client as vertex_mod

        vertex_mod.generate_content_via_vertex = lambda **kwargs: {"leaked": True}
        ensure_gemini_offline_guard()
        with self.assertRaises(GeminiOfflineBlocked):
            vertex_mod.generate_content_via_vertex(
                model="gemini-test", payload={"contents": []}, timeout=5
            )


class CorruptPdfSoftFailTests(unittest.TestCase):
    def test_pdf_images_flags_corrupt_bytes(self):
        from services.gemini_extraction_fallback import _pdf_images

        images = _pdf_images(b"%PDF-1.4 not a real pdf")
        self.assertEqual(images, [])

    def test_format_pipeline_fake_pdf_does_not_crash(self):
        from services.sales_statement_extractor import empty_result, extract_sales_statement

        sentinel = empty_result("statement.pdf", "pdf")
        sentinel["line_items"] = [
            {
                "product_name": "PDF ROW",
                "packing": "60",
                "opening_qty": 10.0,
                "receipts_qty": 0.0,
                "sales_qty": 4.0,
                "sales_value": 100.0,
                "closing_qty": 8.0,
                "closing_value": 80.0,
                "extra": {},
            }
        ]
        with patch(
            "services.sales_statement_extractor._parse_pdf", return_value=sentinel
        ), patch(
            "services.sales_statement_extractor._parse_txt"
        ) as txt, patch(
            "services.sales_statement_extractor._parse_htm"
        ) as htm, patch(
            "services.sales_statement_extractor._parse_word"
        ) as word, patch(
            "services.sales_statement_extractor._parse_xls"
        ) as xls, patch(
            "services.sales_statement_extractor._parse_image"
        ) as image:
            result = extract_sales_statement(b"%PDF-1.4", "statement.pdf")
        self.assertEqual(result["line_items"][0]["product_name"], "PDF ROW")
        self.assertEqual(txt.call_count, 0)
        self.assertEqual(htm.call_count, 0)
        self.assertEqual(word.call_count, 0)
        self.assertEqual(xls.call_count, 0)
        self.assertEqual(image.call_count, 0)


class ShadowRequestIdJoinTests(unittest.TestCase):
    def test_shadow_and_final_share_request_id(self):
        from services.sales_statement_extractor import empty_result, extract_sales_statement

        sentinel = empty_result("join.txt", "txt")
        sentinel["line_items"] = [
            {
                "product_name": "JOIN ROW",
                "packing": "10",
                "opening_qty": 10.0,
                "receipts_qty": 5.0,
                "sales_qty": 3.0,
                "sales_value": 30.0,
                "closing_qty": 12.0,
                "closing_value": 0.0,
                "extra": {},
            }
        ]
        sentinel["totals"]["extra"]["extraction_method"] = "fixed_sales_stock_txt"
        sentinel["totals"]["extra"]["stock_identity_kind"] = (
            "opening_receipts_sales_closing"
        )

        log_buf = io.StringIO()
        handler = logging.StreamHandler(log_buf)
        handler.setLevel(logging.INFO)
        root_names = (
            "services.sales_statement_extractor",
            "services.gemini_extraction_fallback",
        )
        loggers = [logging.getLogger(n) for n in root_names]
        for lg in loggers:
            lg.addHandler(handler)
            lg.setLevel(logging.INFO)
        try:
            with patch(
                "services.sales_statement_extractor._parse_txt", return_value=sentinel
            ), patch(
                "services.sales_extraction_runtime.get_sales_runtime_context",
                return_value=None,
            ), patch(
                "services.sales_extraction_runtime.resolve_request_id",
                return_value="req-join-1b",
            ):
                result = extract_sales_statement(b"LIV 52", "join.txt")
        finally:
            for lg in loggers:
                lg.removeHandler(handler)

        text = log_buf.getvalue()
        self.assertIn("ROW_CLASSIFY_SHADOW request_id=req-join-1b", text)
        self.assertIn("FALLBACK_DECISION_CURRENT request_id=req-join-1b", text)
        self.assertIn("ROW_CLASSIFY_FINAL request_id=req-join-1b", text)
        self.assertIn("would_fallback=", text)
        extra = (result.get("totals") or {}).get("extra") or {}
        self.assertEqual(extra.get("request_id"), "req-join-1b")
        self.assertIn("shadow_row_classification", extra)


class ShadowReportScriptTests(unittest.TestCase):
    def test_four_buckets_from_final_lines(self):
        from scripts import shadow_report as sr

        sample = """
INFO ROW_CLASSIFY_FINAL request_id=a1 file=bad.jpg would_fallback=true reason=valid_ratio current_decision=not_called gemini_fallback=not_called final_selected=structured parser=x kind=y counts={} valid_ratio=0.1
INFO ROW_CLASSIFY_FINAL request_id=a2 file=good.jpg would_fallback=false reason=ok current_decision=not_called gemini_fallback=not_called final_selected=structured parser=x kind=y counts={} valid_ratio=1.0
INFO ROW_CLASSIFY_FINAL request_id=a3 file=rescued.jpg would_fallback=true reason=valid_ratio current_decision=success gemini_fallback=success final_selected=gemini parser=x kind=y counts={} valid_ratio=0.2
INFO ROW_CLASSIFY_FINAL request_id=a4 file=surprise.jpg would_fallback=false reason=ok current_decision=success gemini_fallback=success final_selected=gemini parser=x kind=y counts={} valid_ratio=1.0
""".strip().splitlines()
        events = sr.collect_events(sample)
        buckets, counts = sr.classify_events(events)
        self.assertEqual(counts["silent_error"], 1)
        self.assertEqual(counts["correctly_kept"], 1)
        self.assertEqual(counts["correctly_fallback"], 1)
        self.assertEqual(counts["unexpected_gemini"], 1)
        self.assertEqual(buckets["silent_error"][0]["file"], "bad.jpg")


class BaselineFileTests(unittest.TestCase):
    def test_baseline_lists_exact_failure_ids(self):
        path = Path(__file__).resolve().parent / "baselines" / "pre_phase1_failures.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(data["suite_pre_1b"]["failures"], 6)
        self.assertEqual(data["suite_pre_1b"]["errors"], 1)
        self.assertEqual(data["suite_post_1b"]["failures"], 6)
        self.assertEqual(data["suite_post_1b"]["errors"], 0)
        self.assertEqual(len(data["failures"]), 6)
        self.assertEqual(len(data["errors"]), 1)
        self.assertEqual(data["errors"][0]["disposition"], "fixed_in_prompt_1b")
        ids = [row["id"] for row in data["failures"]]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertIn(
            "tests.test_product_stock_report.TestFixedSalesStockTxt."
            "test_value_columns_are_read_when_printed",
            ids,
        )
        self.assertEqual(
            data["errors"][0]["id"].split(".")[-1],
            "test_pdf_xls_xlsx_txt_html_do_not_enter_word",
        )


if __name__ == "__main__":
    unittest.main()
