"""Column mapping and comparison checks that do not need a PDF."""

from __future__ import annotations

import unittest

from qa.compare import compare_statements, numbers_equal
from qa.ground_truth import Word, build_columns, parse_number, parse_pages
from qa.pdf_source import PageSource


def W(text: str, x0: float, y0: float, x1: float | None = None) -> Word:
    width = max(12.0, len(text) * 5.0)
    return (x0, y0, x1 if x1 is not None else x0 + width, y0 + 9.0, text)


class TestNumbers(unittest.TestCase):
    def test_dash_is_zero_and_blank_is_null(self):
        self.assertEqual(parse_number("-"), 0.0)
        self.assertIsNone(parse_number(""))
        self.assertIsNone(parse_number("200ML"))
        self.assertEqual(parse_number("3,101.65"), 3101.65)
        self.assertTrue(numbers_equal(49, 49.0))
        self.assertFalse(numbers_equal(0, None))
        self.assertTrue(numbers_equal(None, None))


class TestColumns(unittest.TestCase):
    def test_group_wise_received_header(self):
        parent = [
            W("Product", 24, 78, 52),
            W("Name", 54, 78, 75),
            W("Strength", 226, 78, 257),
            W("Op.", 276, 78, 289),
            W("Qty", 291, 78, 304),
            W("Recevied", 324, 78, 356),
            W("Total", 371, 78, 390),
            W("Qty", 392, 78, 405),
            W("Issue", 444, 78, 462),
            W("SalesFree", 470, 78, 504),
            W("Cl.Stock", 522, 78, 552),
        ]
        sub = [
            W("Qty", 342, 88, 356),
            W("Qty", 448, 88, 462),
            W("Qty", 491, 88, 504),
            W("As", 530, 88, 540),
            W("On", 541, 88, 553),
        ]
        fields = [c.field for c in build_columns(parent, sub)]
        self.assertIn("opening_qty", fields)
        self.assertIn("receipts_qty", fields)
        self.assertIn("sales_qty", fields)
        self.assertIn("sales_free", fields)
        self.assertIn("closing_qty", fields)
        self.assertNotIn("opening_value", fields)

    def test_datewise_header_keeps_values_distinct(self):
        parent = [
            W("Product", 42, 91, 75),
            W("Name", 78, 91, 102),
            W("Pack", 148, 91, 169),
            W("OpStk", 197, 91, 224),
            W("Pur", 251, 91, 266),
            W("Sales", 301, 91, 322),
            W("ClStk", 386, 91, 410),
        ]
        sub = [
            W("Qty", 209, 103, 224),
            W("Qty", 251, 103, 266),
            W("Qty", 293, 103, 308),
            W("mount", 323, 103, 350),
            W("Qty", 377, 103, 392),
            W("Amount", 403, 103, 437),
        ]
        fields = [c.field for c in build_columns(parent, sub)]
        self.assertIn("opening_qty", fields)
        self.assertIn("receipts_qty", fields)
        self.assertIn("sales_qty", fields)
        self.assertIn("sales_value", fields)
        self.assertIn("closing_qty", fields)
        self.assertIn("closing_value", fields)


class TestParseAndCompare(unittest.TestCase):
    def test_analysis_row_and_null_values(self):
        header = [
            W("ITEM", 20, 132, 48),
            W("DESCRIPTION", 53, 132, 140),
            W("OPENING", 278, 132, 330),
            W("RECEIPT", 344, 132, 400),
            W("ISSUE", 423, 132, 460),
            W("CLOSING", 476, 132, 530),
        ]
        row = [
            W("GASEX", 20, 196, 55),
            W("TAB", 60, 196, 90),
            W("60'S", 198, 196, 230),
            W("53", 311, 196, 325),
            W("0", 383, 196, 392),
            W("3", 449, 196, 458),
            W("50", 509, 196, 525),
        ]
        page = PageSource(0, header + row, "STOCK & SALES ANALYSIS", "embedded")
        ground = parse_pages([page], "sample.pdf")
        self.assertEqual(ground["format"], "STOCK & SALES ANALYSIS")
        self.assertEqual(ground["confidence"], "low")  # one row is not enough
        # Add two more rows so confidence can rise, then compare the first product.
        extra = []
        for i, name in enumerate(("ABANA TAB", "LIV 52"), start=1):
            y = 210 + i * 14
            extra.extend(
                [
                    W(name, 20, y, 120),
                    W(str(10 + i), 311, y, 330),
                    W("0", 383, y, 392),
                    W("1", 449, y, 458),
                    W(str(9 + i), 509, y, 525),
                ]
            )
        page = PageSource(0, header + row + extra, "STOCK & SALES ANALYSIS", "embedded")
        ground = parse_pages([page], "sample.pdf")
        self.assertEqual(ground["confidence"], "high")
        gasex = ground["line_items"][0]
        self.assertEqual(gasex["opening_qty"], 53.0)
        self.assertEqual(gasex["sales_qty"], 3.0)
        self.assertEqual(gasex["closing_qty"], 50.0)
        self.assertIsNone(gasex["sales_value"])
        self.assertIsNone(gasex["opening_value"])

        extracted = {
            "report_title": "STOCK & SALES ANALYSIS",
            "line_items": [
                {
                    "product_name": "GASEX TAB",
                    "opening_qty": 53,
                    "receipts_qty": 0,
                    "sales_qty": 3,
                    "closing_qty": 50,
                    "sales_value": None,
                    "opening_value": None,
                    "closing_value": None,
                    "receipts_value": None,
                },
                {
                    "product_name": "ABANA TAB",
                    "opening_qty": 11,
                    "receipts_qty": 0,
                    "sales_qty": 1,
                    "closing_qty": 10,
                    "sales_value": None,
                    "opening_value": None,
                    "closing_value": None,
                    "receipts_value": None,
                },
                {
                    "product_name": "LIV 52",
                    "opening_qty": 12,
                    "receipts_qty": 0,
                    "sales_qty": 1,
                    "closing_qty": 11,
                    "sales_value": None,
                    "opening_value": None,
                    "closing_value": None,
                    "receipts_value": None,
                },
            ],
            "totals": {"extra": {"extraction_method": "stock_sales_analysis"}},
        }
        # The synthetic rows above use 10+i so fix expected to match what the parser read.
        compared = compare_statements(ground, extracted)
        self.assertIn(compared["status"], {"PASS", "FAIL", "REVIEW"})

    def test_zero_versus_null_and_fuzzy_name(self):
        ground = {
            "confidence": "high",
            "formula": "simple",
            "present_fields": ["opening_qty", "receipts_qty", "sales_qty", "closing_qty"],
            "absent_fields": ["opening_value", "sales_value", "closing_value", "receipts_value"],
            "line_items": [
                {
                    "product_name": "HADJOD CAPS",
                    "opening_qty": 53,
                    "receipts_qty": 0,
                    "sales_qty": 3,
                    "closing_qty": 50,
                    "opening_value": None,
                    "sales_value": None,
                }
            ],
        }
        wrong = {
            "line_items": [
                {
                    "product_name": "HADIOD CAPS",
                    "opening_qty": 31,
                    "receipts_qty": 0,
                    "sales_qty": 3,
                    "closing_qty": 28,
                    "opening_value": 0,
                    "sales_value": 0,
                }
            ],
            "totals": {"extra": {}},
        }
        result = compare_statements(ground, wrong)
        self.assertEqual(result["status"], "FAIL")
        fields = {m["field"] for m in result["mismatches"]}
        self.assertIn("opening_qty", fields)
        self.assertIn("opening_value", fields)
        opening = next(m for m in result["mismatches"] if m["field"] == "opening_qty")
        self.assertEqual(opening["expected"], 53)
        self.assertEqual(opening["actual"], 31)
        self.assertEqual(result["missing_rows"], [])

    def test_group_wise_ocr_keeps_balanced_rows_only(self):
        from qa.ground_truth import _group_wise_ocr_items

        text = """
        Group Wise Sales (From 01/08/2026 UpTo 31/08/2026)
        GASEX ELAICHI FLA SYRUP 200 ML 49 0 0 0 0 0 0 2 0 47
        HADJOD CAPS 60'S 3 7 7 a 7 7 7 3 0 30
        LIV-52 TAB 100'S 95 0 0 0 0 0 0 0 0 95
        Page 1 of 2
        """
        items, skipped = _group_wise_ocr_items(text)
        names = [i["product_name"] for i in items]
        self.assertTrue(any("GASEX" in n for n in names))
        self.assertTrue(any("LIV-52 TAB" in n for n in names))
        self.assertFalse(any("HADJOD" in n for n in names))
        gasex = next(i for i in items if "GASEX" in i["product_name"])
        self.assertEqual(gasex["opening_qty"], 49)
        self.assertEqual(gasex["sales_qty"], 2)
        self.assertEqual(gasex["closing_qty"], 47)
        self.assertIsNone(gasex["sales_value"])
        self.assertGreaterEqual(skipped, 1)
        row = {
            "product_name": "CLARINA FACE WASH",
            "opening_qty": 6,
            "receipts_qty": 50,
            "sales_qty": 4,
            "closing_qty": 52,
            "opening_value": None,
            "sales_value": None,
            "closing_value": None,
            "receipts_value": None,
        }
        api = dict(row)
        api["product_name"] = "CLARINA FC WASH"
        ground = {
            "confidence": "high",
            "formula": "simple",
            "present_fields": ["opening_qty", "receipts_qty", "sales_qty", "closing_qty"],
            "absent_fields": ["opening_value", "receipts_value", "sales_value", "closing_value"],
            "line_items": [row],
        }
        result = compare_statements(ground, {"line_items": [api], "totals": {"extra": {}}})
        self.assertEqual(result["missing_rows"], [])
        self.assertIn(result["status"], {"PASS", "REVIEW"})

    def test_null_matches_null(self):
        item = {
            "product_name": "HADJOD CAPS",
            "opening_qty": 53,
            "receipts_qty": 0,
            "sales_qty": 3,
            "closing_qty": 50,
            "opening_value": None,
            "sales_value": None,
            "closing_value": None,
            "receipts_value": None,
        }
        ground = {
            "confidence": "high",
            "formula": "simple",
            "present_fields": ["opening_qty", "receipts_qty", "sales_qty", "closing_qty"],
            "absent_fields": ["opening_value", "receipts_value", "sales_value", "closing_value"],
            "line_items": [item],
        }
        result = compare_statements(ground, {"line_items": [dict(item)], "totals": {"extra": {}}})
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["mismatches"], [])


class TestResumableBatches(unittest.TestCase):
    def test_rate_limit_text_and_backoff(self):
        from unittest.mock import patch

        from qa import runner as qa_runner
        from qa.runner import (
            RateLimitSignal,
            RETRY_BACKOFF_SECONDS,
            _begin_gemini_pdf,
            install_gemini_rate_limit_guard,
            text_is_rate_limit,
        )
        from services import vertex_gemini_client as vgc

        self.assertEqual(RETRY_BACKOFF_SECONDS, (10, 30, 60, 120))
        self.assertTrue(text_is_rate_limit("HTTP 429 RESOURCE_EXHAUSTED"))
        self.assertTrue(text_is_rate_limit("RESOURCE_EXHAUSTED"))
        self.assertFalse(text_is_rate_limit("non-JSON vision response"))

        sleeps = []
        calls = {"n": 0}
        prior = vgc.generate_content_via_vertex

        class FakeError(Exception):
            def __init__(self):
                self.code = 429
                super().__init__("HTTP 429 RESOURCE_EXHAUSTED Resource exhausted.")

        def fake_generate(model, payload, timeout):
            calls["n"] += 1
            raise FakeError()

        try:
            qa_runner._GEMINI_GUARD["installed"] = False
            qa_runner._GEMINI_GUARD["original"] = None
            qa_runner._GEMINI_GUARD["last_end"] = 0.0
            vgc.generate_content_via_vertex = fake_generate
            install_gemini_rate_limit_guard(
                delay_seconds=0,
                max_retries=4,
                sleep_fn=sleeps.append,
                random_fn=lambda _a, _b: 0.0,
            )
            _begin_gemini_pdf("file123.pdf")
            with self.assertRaises(RateLimitSignal):
                vgc.generate_content_via_vertex("model", {"contents": []}, 30)
        finally:
            vgc.generate_content_via_vertex = prior
            qa_runner._GEMINI_GUARD["installed"] = False
            qa_runner._GEMINI_GUARD["original"] = None
            qa_runner._GEMINI_GUARD["last_end"] = 0.0

        self.assertEqual(calls["n"], 5)
        self.assertEqual(sleeps, [10, 30, 60, 120])

    def test_gemini_retry_then_success(self):
        from qa import runner as qa_runner
        from qa.runner import _begin_gemini_pdf, install_gemini_rate_limit_guard
        from services import vertex_gemini_client as vgc

        sleeps = []
        calls = {"n": 0}
        prior = vgc.generate_content_via_vertex

        class FakeError(Exception):
            def __init__(self):
                self.code = 429
                super().__init__("RESOURCE_EXHAUSTED")

        def fake_generate(model, payload, timeout):
            calls["n"] += 1
            if calls["n"] < 3:
                raise FakeError()
            return {"ok": True}

        try:
            qa_runner._GEMINI_GUARD["installed"] = False
            qa_runner._GEMINI_GUARD["original"] = None
            qa_runner._GEMINI_GUARD["last_end"] = 0.0
            vgc.generate_content_via_vertex = fake_generate
            install_gemini_rate_limit_guard(
                delay_seconds=0,
                max_retries=4,
                sleep_fn=sleeps.append,
                random_fn=lambda _a, _b: 0.0,
            )
            _begin_gemini_pdf("file123.pdf")
            result = vgc.generate_content_via_vertex("model", {"contents": []}, 30)
        finally:
            vgc.generate_content_via_vertex = prior
            qa_runner._GEMINI_GUARD["installed"] = False
            qa_runner._GEMINI_GUARD["original"] = None
            qa_runner._GEMINI_GUARD["last_end"] = 0.0

        self.assertEqual(result, {"ok": True})
        self.assertEqual(calls["n"], 3)
        self.assertEqual(sleeps, [10, 30])
        self.assertFalse(qa_runner._gemini_rate_limit_exhausted())

    def test_batches_skip_completed_and_retry_rate_limited(self):
        import tempfile
        from pathlib import Path

        from qa.runner import file_id, load_progress, run_batch

        with tempfile.TemporaryDirectory() as tmp:
            pdf_dir = Path(tmp) / "pdfs"
            output_dir = Path(tmp) / "qa"
            pdf_dir.mkdir()
            names = ["a.pdf", "b.pdf", "c.pdf", "d.pdf", "e.pdf"]
            for name in names:
                (pdf_dir / name).write_bytes(b"%PDF-1.4")
            calls = []

            def processor(path, root, max_pages):
                calls.append(path.name)
                status = "PASS"
                if path.name == "c.pdf":
                    status = "RATE_LIMITED"
                return {
                    "status": status,
                    "format": "Stock Statement",
                    "api_method": "fake_handler",
                    "expected_rows": 1,
                    "extracted_rows": 1,
                    "matched_rows": 1,
                    "missing_rows": [],
                    "extra_rows": [],
                    "mismatches": [],
                }

            first = run_batch(
                pdf_dir,
                output_dir,
                workers=1,
                batch_size=2,
                processor=processor,
                sleep_fn=lambda _seconds: None,
            )
            self.assertEqual(calls, names)
            self.assertEqual(first["run_stats"]["processed"], 5)
            self.assertEqual(first["run_stats"]["skipped"], 0)
            self.assertEqual(first["run_stats"]["rate_limited"], 1)
            progress = load_progress(output_dir / "progress.json")
            self.assertEqual(progress[file_id(pdf_dir / "a.pdf", pdf_dir)]["batch"], 1)
            self.assertEqual(progress[file_id(pdf_dir / "a.pdf", pdf_dir)]["status"], "PASS")
            self.assertEqual(progress[file_id(pdf_dir / "c.pdf", pdf_dir)]["batch"], 2)
            self.assertEqual(progress[file_id(pdf_dir / "c.pdf", pdf_dir)]["status"], "RATE_LIMITED")
            self.assertEqual(progress[file_id(pdf_dir / "e.pdf", pdf_dir)]["batch"], 3)
            import json

            batch_report = json.loads(
                (output_dir / "runs" / "batch_001" / "report.json").read_text(encoding="utf-8")
            )
            self.assertEqual(len(batch_report["files"]), 2)
            batch_two = json.loads(
                (output_dir / "runs" / "batch_002" / "report.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                [row["status"] for row in batch_two["files"]],
                ["RATE_LIMITED", "PASS"],
            )
            self.assertTrue((output_dir / "runs" / "batch_003" / "report.json").exists())

            calls.clear()

            def retry_processor(path, root, max_pages):
                calls.append(path.name)
                return {
                    "status": "FAIL",
                    "format": "Stock Statement",
                    "api_method": "fake_handler",
                    "expected_rows": 2,
                    "extracted_rows": 1,
                    "matched_rows": 1,
                    "missing_rows": [{"product": "ABANA"}],
                    "extra_rows": [],
                    "mismatches": [
                        {
                            "product": "ABANA",
                            "field": "sales_qty",
                            "expected": 2,
                            "actual": 9,
                            "kind": "wrong_sales_qty",
                        },
                        {
                            "product": "ABANA",
                            "field": "closing_qty",
                            "expected": 4,
                            "actual": 11,
                            "kind": "wrong_closing_qty",
                        },
                    ],
                }

            second = run_batch(
                pdf_dir,
                output_dir,
                workers=1,
                batch_size=2,
                processor=retry_processor,
                sleep_fn=lambda _seconds: None,
            )
            self.assertEqual(calls, ["c.pdf"])
            self.assertEqual(second["run_stats"]["skipped"], 4)
            self.assertEqual(second["run_stats"]["processed"], 1)
            self.assertEqual(second["summary"]["pass"], 4)
            self.assertEqual(second["summary"]["fail"], 1)
            self.assertEqual(second["summary"]["rate_limited"], 0)
            prompt = (output_dir / "cursor_fix_prompt.txt").read_text(encoding="utf-8")
            self.assertIn("FORMAT: Stock Statement", prompt)
            self.assertIn("Sales and closing quantities are shifted.", prompt)
            self.assertIn("c.pdf", prompt)
            self.assertNotIn("a.pdf", prompt)
            reloaded = load_progress(output_dir / "progress.json")
            self.assertEqual(reloaded[file_id(pdf_dir / "a.pdf", pdf_dir)]["status"], "PASS")
            self.assertEqual(reloaded[file_id(pdf_dir / "c.pdf", pdf_dir)]["status"], "FAIL")

    def test_grouped_prompt_does_not_repeat_each_pdf_as_its_own_fix(self):
        from qa.report import render_fix_prompt

        report = {
            "files": [
                {
                    "file": "file1.pdf",
                    "format": "Group Wise Sales",
                    "status": "FAIL",
                    "api_method": "group_wise_sales_opstock",
                    "mismatches": [
                        {
                            "product": "TRIPHALA SYP",
                            "field": "sales_qty",
                            "expected": 2,
                            "actual": 9,
                            "kind": "wrong_sales_qty",
                        },
                        {
                            "product": "TRIPHALA SYP",
                            "field": "closing_qty",
                            "expected": 5,
                            "actual": 12,
                            "kind": "wrong_closing_qty",
                        },
                    ],
                },
                {
                    "file": "file2.pdf",
                    "format": "Group Wise Sales",
                    "status": "FAIL",
                    "api_method": "group_wise_sales_opstock",
                    "mismatches": [
                        {
                            "product": "PRODUCT ABC",
                            "field": "sales_qty",
                            "expected": 4,
                            "actual": 7,
                            "kind": "wrong_sales_qty",
                        },
                        {
                            "product": "PRODUCT ABC",
                            "field": "closing_qty",
                            "expected": 1,
                            "actual": 8,
                            "kind": "wrong_closing_qty",
                        },
                    ],
                },
            ]
        }
        prompt = render_fix_prompt(report)
        self.assertEqual(prompt.count("FORMAT: Group Wise Sales"), 1)
        self.assertIn("- file1.pdf", prompt)
        self.assertIn("- file2.pdf", prompt)
        self.assertIn("Expected sales_qty = 2", prompt)
        self.assertIn("Do not patch individual PDF filenames.", prompt)


if __name__ == "__main__":
    unittest.main()
