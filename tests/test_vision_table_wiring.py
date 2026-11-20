"""Phase 2b-2: STOCK_VISION_TABLE wiring into extract_sales_statement (offline)."""

from __future__ import annotations

import io
import json
import logging
import os
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from services.sales_statement_extractor import empty_result
from tests.gemini_offline import (
    clear_gemini_responses,
    ensure_gemini_offline_guard,
    gemini_test_call_count,
    reset_gemini_test_call_count,
    set_gemini_responses,
)

RESP_DIR = Path(__file__).resolve().parent / "data" / "vision_responses"


def _load(name: str):
    return json.loads((RESP_DIR / name).read_text(encoding="utf-8"))


def _tiny_jpeg() -> bytes:
    return b"\xff\xd8\xff\xd9" + b"\x00" * 64


class VisionTableFlagOffTests(unittest.TestCase):
    def setUp(self):
        ensure_gemini_offline_guard()
        reset_gemini_test_call_count()
        clear_gemini_responses()
        self._env = {
            k: os.environ.get(k)
            for k in ("STOCK_VISION_TABLE", "STOCK_VISION_TABLE_TYPES")
        }
        os.environ["STOCK_VISION_TABLE"] = "false"
        os.environ.pop("STOCK_VISION_TABLE_TYPES", None)

    def tearDown(self):
        clear_gemini_responses()
        for key, value in self._env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_flag_off_never_calls_vision_table(self):
        from services.sales_statement_extractor import extract_sales_statement

        sentinel = empty_result("n.jpg", "jpg")
        sentinel["line_items"] = [
            {
                "product_name": "ROW",
                "packing": "10",
                "opening_qty": 1.0,
                "receipts_qty": 0.0,
                "sales_qty": 0.0,
                "sales_value": 0.0,
                "closing_qty": 1.0,
                "closing_value": 0.0,
                "extra": {},
            }
        ]
        with patch(
            "services.stock_vision_table.run_vision_table_path"
        ) as runner, patch(
            "services.sales_statement_extractor._parse_image", return_value=sentinel
        ):
            out = extract_sales_statement(_tiny_jpeg(), "n.jpg")
        runner.assert_not_called()
        self.assertEqual(out["line_items"][0]["product_name"], "ROW")


class VisionTableImageOkTests(unittest.TestCase):
    def setUp(self):
        ensure_gemini_offline_guard()
        reset_gemini_test_call_count()
        clear_gemini_responses()
        self._env = {
            k: os.environ.get(k)
            for k in (
                "STOCK_VISION_TABLE",
                "STOCK_VISION_TABLE_TYPES",
                "STOCK_IDENTITY_VETO",
                "STOCK_IDENTITY_VETO_TYPES",
                "STOCK_ROW_CLASSIFIER_SHADOW",
            )
        }
        os.environ["STOCK_VISION_TABLE"] = "true"
        os.environ["STOCK_VISION_TABLE_TYPES"] = "image"
        os.environ["STOCK_IDENTITY_VETO"] = "false"
        os.environ["STOCK_ROW_CLASSIFIER_SHADOW"] = "true"

    def tearDown(self):
        clear_gemini_responses()
        for key, value in self._env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_valid_table_one_call_skips_gates(self):
        from services.sales_statement_extractor import extract_sales_statement

        set_gemini_responses([_load("op_pur_sale_cls_qty_value.json")])
        before = gemini_test_call_count()
        with patch(
            "services.sales_statement_extractor._maybe_early_vision_for_image"
        ) as early, patch(
            "services.stock_image_vision.apply_direct_stock_image_gemini"
        ) as direct, patch(
            "services.gemini_extraction_fallback.maybe_apply_gemini_fallback"
        ) as fallback:
            # Force _parse_image to use vision path only — still goes through extract.
            out = extract_sales_statement(_tiny_jpeg(), "sheet.jpg")
        self.assertEqual(gemini_test_call_count(), before + 1)
        extra = (out.get("totals") or {}).get("extra") or {}
        self.assertEqual(extra.get("extraction_method"), "vision_table")
        early.assert_not_called()
        direct.assert_not_called()
        fallback.assert_not_called()
        self.assertTrue(out.get("line_items"))

    def test_filename_codes_identical(self):
        from services.sales_statement_extractor import extract_sales_statement

        payload = _load("op_pur_sale_cls_qty_value.json")
        outs = []
        for name in ("a_ZA_1.jpg", "b_ZL_2.jpg", "neutral.jpg"):
            clear_gemini_responses()
            set_gemini_responses([payload])
            outs.append(extract_sales_statement(_tiny_jpeg(), name))
        keys = []
        for out in outs:
            keys.append(
                [
                    (
                        i.get("product_name"),
                        i.get("opening_qty"),
                        i.get("receipts_qty"),
                        i.get("sales_qty"),
                        i.get("closing_qty"),
                    )
                    for i in out.get("line_items") or []
                ]
            )
        self.assertEqual(keys[0], keys[1])
        self.assertEqual(keys[1], keys[2])

    def test_row_classify_final_parser_vision_table(self):
        from services.sales_statement_extractor import extract_sales_statement

        set_gemini_responses([_load("op_pur_sale_cls_qty_value.json")])
        log_buf = io.StringIO()
        handler = logging.StreamHandler(log_buf)
        logger = logging.getLogger("services.sales_statement_extractor")
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        try:
            extract_sales_statement(_tiny_jpeg(), "sheet.jpg")
        finally:
            logger.removeHandler(handler)
        text = log_buf.getvalue()
        self.assertIn("ROW_CLASSIFY_FINAL", text)
        self.assertIn("parser=vision_table", text)

    def test_structural_error_falls_back(self):
        from services.sales_statement_extractor import extract_sales_statement

        bad = {
            "header_rows": [
                [
                    {"text": "Product", "col_index": 0, "x_center": 0.9},
                    {"text": "Opening", "col_index": 1, "x_center": 0.1},
                    {"text": "Sale", "col_index": 2, "x_center": 0.5},
                    {"text": "Closing", "col_index": 3, "x_center": 0.7},
                ]
            ],
            "column_count": 4,
            "rows": [],
            "proposed_mapping": [],
            "unreadable_cells": [],
        }
        set_gemini_responses([bad])
        sentinel = empty_result("sheet.jpg", "jpg")
        sentinel["line_items"] = [
            {
                "product_name": "FALLBACK ROW",
                "packing": None,
                "opening_qty": 1.0,
                "receipts_qty": 0.0,
                "sales_qty": 0.0,
                "sales_value": 0.0,
                "closing_qty": 1.0,
                "closing_value": 0.0,
                "extra": {},
            }
        ]
        log_buf = io.StringIO()
        handler = logging.StreamHandler(log_buf)
        for name in (
            "services.sales_statement_extractor",
            "services.stock_vision_table",
        ):
            logging.getLogger(name).addHandler(handler)
            logging.getLogger(name).setLevel(logging.INFO)
        try:
            with patch(
                "services.sales_statement_extractor._sideways_full_width_stock_grid",
                return_value=False,
            ), patch(
                "services.sales_statement_extractor._zeal_printed_order_form_anchor",
                return_value=None,
            ), patch(
                "services.stock_direct_vision.detect_stock_handwriting_signals",
                return_value={},
            ), patch(
                "services.sales_statement_extractor._extract_phone_rate_ssa_screenshot",
                return_value=None,
            ), patch(
                # After vision fallback, short-circuit existing image path.
                "services.sales_statement_extractor._maybe_early_vision_for_image",
                return_value=(sentinel, True),
            ), patch(
                "services.stock_direct_vision.is_stock_vision_locked",
                return_value=True,
            ):
                out = extract_sales_statement(_tiny_jpeg(), "sheet.jpg")
        finally:
            for name in (
                "services.sales_statement_extractor",
                "services.stock_vision_table",
            ):
                logging.getLogger(name).removeHandler(handler)
        text = log_buf.getvalue()
        self.assertIn("STRUCTURAL_ERROR", text)
        self.assertIn("status=fallback", text)
        self.assertEqual(out["line_items"][0]["product_name"], "FALLBACK ROW")

    def test_invalid_json_fallback_budget(self):
        from services.sales_statement_extractor import extract_sales_statement

        set_gemini_responses(
            [
                {
                    "candidates": [
                        {"content": {"parts": [{"text": "NOT JSON"}]}}
                    ]
                }
            ]
        )
        sentinel = empty_result("sheet.jpg", "jpg")
        sentinel["line_items"] = [
            {
                "product_name": "OCR ROW",
                "packing": None,
                "opening_qty": 2.0,
                "receipts_qty": 0.0,
                "sales_qty": 0.0,
                "sales_value": 0.0,
                "closing_qty": 2.0,
                "closing_value": 0.0,
                "extra": {},
            }
        ]
        before = gemini_test_call_count()
        with patch(
            "services.sales_statement_extractor._sideways_full_width_stock_grid",
            return_value=False,
        ), patch(
            "services.sales_statement_extractor._zeal_printed_order_form_anchor",
            return_value=None,
        ), patch(
            "services.stock_direct_vision.detect_stock_handwriting_signals",
            return_value={},
        ), patch(
            "services.sales_statement_extractor._extract_phone_rate_ssa_screenshot",
            return_value=None,
        ), patch(
            "services.sales_statement_extractor._maybe_early_vision_for_image",
            return_value=(sentinel, True),
        ), patch(
            "services.stock_direct_vision.is_stock_vision_locked",
            return_value=True,
        ), patch(
            "services.gemini_extraction_fallback.maybe_apply_gemini_fallback",
            side_effect=lambda result, *a, **k: result,
        ):
            out = extract_sales_statement(_tiny_jpeg(), "sheet.jpg")
        # Vision table made 1 call; fallback path should not exceed budget 2.
        self.assertLessEqual(gemini_test_call_count() - before, 2)
        self.assertEqual(out["line_items"][0]["product_name"], "OCR ROW")
        extra = (out.get("totals") or {}).get("extra") or {}
        self.assertGreaterEqual(int(extra.get("gemini_calls") or 0), 1)


class VisionTableRereadTests(unittest.TestCase):
    def setUp(self):
        ensure_gemini_offline_guard()
        reset_gemini_test_call_count()
        clear_gemini_responses()
        self._env = {
            k: os.environ.get(k)
            for k in (
                "STOCK_VISION_TABLE",
                "STOCK_VISION_TABLE_TYPES",
                "STOCK_IDENTITY_VETO",
                "STOCK_IDENTITY_VETO_TYPES",
            )
        }
        os.environ["STOCK_VISION_TABLE"] = "true"
        os.environ["STOCK_VISION_TABLE_TYPES"] = "image"
        os.environ["STOCK_IDENTITY_VETO"] = "true"
        os.environ["STOCK_IDENTITY_VETO_TYPES"] = "image,scanned_pdf"

    def tearDown(self):
        clear_gemini_responses()
        for key, value in self._env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_flagged_rows_trigger_reread(self):
        from services.sales_statement_extractor import extract_sales_statement

        first = _load("op_pur_sale_cls_qty_value.json")
        # Break identity on all product rows.
        for row in first["rows"]:
            if not row.get("is_total_row"):
                cells = list(row["cells"])
                cells[7] = "999"  # closing wrong
                row["cells"] = cells
        # Re-read returns corrected rows for LIV 52 only; ABANA stays bad.
        reread = {
            "header_rows": first["header_rows"],
            "column_count": first["column_count"],
            "rows": [
                {
                    "row_index": 0,
                    "cells": [
                        "LIV 52 DS",
                        "10",
                        "100.00",
                        "5",
                        None,
                        "3",
                        "30.00",
                        "12",
                        "120.00",
                    ],
                    "is_total_row": False,
                },
                {
                    "row_index": 1,
                    "cells": [
                        "ABANA TAB",
                        "0",
                        None,
                        "20",
                        "200.00",
                        "8",
                        "80.00",
                        "999",
                        "120.00",
                    ],
                    "is_total_row": False,
                },
            ],
            "proposed_mapping": first["proposed_mapping"],
            "unreadable_cells": [],
        }
        set_gemini_responses([first, reread])
        log_buf = io.StringIO()
        handler = logging.StreamHandler(log_buf)
        logger = logging.getLogger("services.stock_vision_table")
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        before = gemini_test_call_count()
        try:
            with patch(
                "services.stock_vision_table.build_reconciliation_recovery_prompt",
                wraps=__import__(
                    "services.stock_vision_table",
                    fromlist=["build_reconciliation_recovery_prompt"],
                ).build_reconciliation_recovery_prompt,
            ) as prompt_fn:
                out = extract_sales_statement(_tiny_jpeg(), "sheet.jpg")
        finally:
            logger.removeHandler(handler)
        self.assertEqual(gemini_test_call_count(), before + 2)
        self.assertTrue(prompt_fn.called)
        flagged_arg = prompt_fn.call_args[0][0]
        self.assertEqual(len(flagged_arg), 2)
        names = {r.get("product_name") for r in out.get("line_items") or []}
        self.assertIn("LIV 52 DS", names)
        liv = next(
            i for i in out["line_items"] if i.get("product_name") == "LIV 52 DS"
        )
        self.assertEqual(liv["closing_qty"], 12.0)
        abana = next(
            i for i in out["line_items"] if i.get("product_name") == "ABANA TAB"
        )
        # Invalid re-read stays as candidate; first reading kept (999 closing).
        self.assertEqual(abana["closing_qty"], 999.0)
        self.assertTrue((abana.get("extra") or {}).get("candidates"))


class VisionTablePdfAndOtherFormatsTests(unittest.TestCase):
    def setUp(self):
        ensure_gemini_offline_guard()
        reset_gemini_test_call_count()
        clear_gemini_responses()
        self._env = {
            k: os.environ.get(k)
            for k in ("STOCK_VISION_TABLE", "STOCK_VISION_TABLE_TYPES")
        }

    def tearDown(self):
        clear_gemini_responses()
        for key, value in self._env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_types_image_only_skips_scanned_pdf(self):
        from services.sales_statement_extractor import extract_sales_statement

        os.environ["STOCK_VISION_TABLE"] = "true"
        os.environ["STOCK_VISION_TABLE_TYPES"] = "image"
        sentinel = empty_result("scan.pdf", "pdf")
        sentinel["line_items"] = [
            {
                "product_name": "PDF ROW",
                "packing": None,
                "opening_qty": 1.0,
                "receipts_qty": 0.0,
                "sales_qty": 0.0,
                "sales_value": 0.0,
                "closing_qty": 1.0,
                "closing_value": 0.0,
                "extra": {},
            }
        ]
        with patch(
            "services.stock_vision_table.run_vision_table_path"
        ) as runner, patch(
            "services.sales_statement_extractor._parse_pdf", return_value=sentinel
        ):
            out = extract_sales_statement(b"%PDF-1.4", "scan.pdf")
        runner.assert_not_called()
        self.assertEqual(out["line_items"][0]["product_name"], "PDF ROW")

    def test_six_page_scanned_pdf_batches(self):
        from services.stock_vision_table import run_vision_table_path

        os.environ["STOCK_VISION_TABLE"] = "true"
        os.environ["STOCK_VISION_TABLE_TYPES"] = "image,scanned_pdf"
        os.environ["STOCK_VISION_PAGES_PER_CALL"] = "4"
        data = _load("two_page_continuation.json")
        # Simulate 6 page images; two batches of 4+2.
        pages = [b"\xff\xd8\xff\xd9" + bytes([i]) for i in range(6)]
        # Batch1 uses page1 header; batch2 continuation empty header.
        set_gemini_responses([data["page1"], data["page2"]])
        before = gemini_test_call_count()
        with patch(
            "services.gemini_extraction_fallback._pdf_images", return_value=pages
        ):
            outcome = run_vision_table_path(
                b"%PDF-1.4",
                "scanned_pdf",
                {"filename": "scan.pdf", "ext": ".pdf", "request_id": "t"},
            )
        self.assertEqual(outcome["status"], "ok")
        # 2 batch calls; no veto/reread with identity veto off by default here.
        self.assertEqual(gemini_test_call_count(), before + 2)
        self.assertEqual(len(outcome["result"]["line_items"]), 2)
        self.assertEqual(outcome["gemini_budget"], 3)  # 2 batches + 1 reread

    def test_text_xlsx_txt_docx_unchanged(self):
        from services.sales_statement_extractor import extract_sales_statement

        os.environ["STOCK_VISION_TABLE"] = "true"
        os.environ["STOCK_VISION_TABLE_TYPES"] = "image,scanned_pdf"
        cases = [
            (".txt", b"LIV 52", "_parse_txt"),
            (".xlsx", b"PK", "_parse_xls"),
            (".docx", b"PK", "_parse_word"),
            (".pdf", b"%PDF-1.4 text heavy", "_parse_pdf"),
        ]
        for ext, raw, fn in cases:
            with self.subTest(ext=ext):
                sentinel = empty_result(f"f{ext}", ext.lstrip("."))
                sentinel["line_items"] = [
                    {
                        "product_name": f"KEEP{ext}",
                        "packing": None,
                        "opening_qty": 1.0,
                        "receipts_qty": 0.0,
                        "sales_qty": 0.0,
                        "sales_value": 0.0,
                        "closing_qty": 1.0,
                        "closing_value": 0.0,
                        "extra": {},
                    }
                ]
                with patch(
                    "services.stock_vision_table.run_vision_table_path"
                ) as runner, patch(
                    f"services.sales_statement_extractor.{fn}", return_value=sentinel
                ), patch(
                    "services.sales_statement_extractor._sniff_extension",
                    return_value=ext,
                ):
                    out = extract_sales_statement(raw, f"f{ext}")
                runner.assert_not_called()
                self.assertEqual(out["line_items"][0]["product_name"], f"KEEP{ext}")


if __name__ == "__main__":
    unittest.main()
