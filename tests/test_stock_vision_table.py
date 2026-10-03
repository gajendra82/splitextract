"""Phase 2b-1: stock_vision_table offline acceptance tests."""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

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


class MapVisionTableTests(unittest.TestCase):
    def test_op_pur_sale_cls_qty_value(self):
        from services.stock_vision_table import map_vision_table

        table = _load("op_pur_sale_cls_qty_value.json")
        mapped = map_vision_table(table)
        canons = [c["canonical"] for c in mapped["column_map"]]
        self.assertEqual(
            canons,
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
        self.assertEqual(len(mapped["line_items"]), 2)
        first = mapped["line_items"][0]
        self.assertEqual(first["product_name"], "LIV 52 DS")
        self.assertEqual(first["opening_qty"], 10.0)
        self.assertEqual(first["receipts_qty"], 5.0)
        self.assertEqual(first["sales_qty"], 3.0)
        self.assertEqual(first["closing_qty"], 12.0)
        # Empty purchase value cell -> missing, not printed 0 in field_source.
        self.assertEqual(first["extra"]["field_source"]["purchase_value"], "missing")
        self.assertIsNone(first["extra"].get("purchase_value"))
        self.assertEqual(first["extra"]["field_source"]["opening_qty"], "printed")

    def test_saleret_exp_dmg_and_total_row(self):
        from services.stock_vision_table import map_vision_table

        mapped = map_vision_table(_load("saleret_exp_dmg_total.json"))
        canons = [c["canonical"] for c in mapped["column_map"]]
        self.assertEqual(canons[3], "total_qty")
        self.assertEqual(canons[5], "sales_return_qty")
        self.assertEqual(canons[6], "expiry_damage_qty")
        self.assertEqual(len(mapped["line_items"]), 1)
        self.assertEqual(len(mapped["totals_rows"]), 1)
        item = mapped["line_items"][0]
        self.assertEqual(item["extra"]["sale_return"], 0.0)
        self.assertEqual(item["extra"]["field_source"]["sales_return_qty"], "printed")
        self.assertEqual(item["extra"]["total_stock"], 115.0)

    def test_two_row_header(self):
        from services.stock_vision_table import map_vision_table

        mapped = map_vision_table(_load("two_row_opening_qty_value.json"))
        canons = [c["canonical"] for c in mapped["column_map"]]
        self.assertEqual(
            canons,
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
        item = mapped["line_items"][0]
        self.assertEqual(item["extra"]["field_source"]["purchase_value"], "missing")
        self.assertIn("purchase_value", item["extra"]["unreadable_fields"])

    def test_page2_continuation_and_mismatch(self):
        from services.stock_vision_table import map_vision_table

        data = _load("two_page_continuation.json")
        page1 = map_vision_table(data["page1"])
        self.assertTrue(page1["header_used"])
        page2 = map_vision_table(data["page2"], carry_header=page1["header_used"])
        self.assertEqual(len(page2["line_items"]), 1)
        self.assertEqual(page2["line_items"][0]["product_name"], "PAGE2 ROW")
        self.assertNotIn("STRUCTURAL_ERROR", page2["errors"])

        bad = map_vision_table(
            data["page2_mismatched"], carry_header=page1["header_used"]
        )
        self.assertIn("STRUCTURAL_ERROR", bad["errors"])
        self.assertEqual(bad["line_items"], [])


class LiveShapeNormalizeTests(unittest.TestCase):
    """Live Gemini often returns flat headers and field/mapping keys."""

    def test_flat_header_and_field_key(self):
        from services.stock_vision_table import map_vision_table

        table = {
            "header_rows": [
                {"text": "Product", "col_index": 0, "x_center": 0.1},
                {"text": "Opening", "col_index": 1, "x_center": 0.3},
                {"text": "Purchase", "col_index": 2, "x_center": 0.5},
                {"text": "Sale", "col_index": 3, "x_center": 0.7},
                {"text": "ClosStock", "col_index": 4, "x_center": 0.9},
            ],
            "column_count": 5,
            "rows": [
                {
                    "row_index": 0,
                    "cells": ["HIORA K", "9.00", "50.00", "21.00", "38.00"],
                    "is_total_row": False,
                }
            ],
            "proposed_mapping": [
                {"col_index": 0, "field": "product_name", "confidence": 1.0},
                {"col_index": 1, "field": "opening_qty", "confidence": 1.0},
                {"col_index": 2, "field": "purchase_qty", "confidence": 1.0},
                {"col_index": 3, "field": "sales_qty", "confidence": 1.0},
                {"col_index": 4, "field": "closing_qty", "confidence": 1.0},
            ],
            "unreadable_cells": [],
        }
        mapped = map_vision_table(table)
        self.assertNotIn("MISSING_CORE_COLUMNS", mapped.get("errors") or [])
        item = mapped["line_items"][0]
        self.assertEqual(item["product_name"], "HIORA K")
        self.assertEqual(item["opening_qty"], 9.0)
        self.assertEqual(item["receipts_qty"], 50.0)
        self.assertEqual(item["sales_qty"], 21.0)
        self.assertEqual(item["closing_qty"], 38.0)

    def test_cells_wrapper_and_dict_cells(self):
        from services.stock_vision_table import map_vision_table

        table = {
            "header_rows": [
                {
                    "cells": [
                        {"text": "Product Name", "col_index": 0, "x_center": 0.1},
                        {"text": "Opening", "col_index": 1, "x_center": 0.3},
                        {"text": "Receipt", "col_index": 2, "x_center": 0.5},
                        {"text": "Issues", "col_index": 3, "x_center": 0.7},
                        {"text": "Closing", "col_index": 4, "x_center": 0.9},
                    ]
                }
            ],
            "column_count": 5,
            "rows": [
                {
                    "row_index": 0,
                    "cells": [
                        {"text": "LIV 52", "col_index": 0},
                        {"text": "10", "col_index": 1},
                        {"text": "5", "col_index": 2},
                        {"text": "3", "col_index": 3},
                        {"text": "12", "col_index": 4},
                    ],
                    "is_total_row": False,
                }
            ],
            "proposed_mapping": [
                {"col_index": 0, "mapping": "product_name", "confidence": 1.0},
            ],
            "unreadable_cells": [],
        }
        mapped = map_vision_table(table)
        item = mapped["line_items"][0]
        self.assertEqual(item["product_name"], "LIV 52")
        self.assertEqual(item["opening_qty"], 10.0)
        self.assertEqual(item["receipts_qty"], 5.0)
        self.assertEqual(item["sales_qty"], 3.0)
        self.assertEqual(item["closing_qty"], 12.0)

    def test_vision_max_output_tokens_env(self):
        import os
        from services import stock_vision_table as svt

        old = os.environ.get("STOCK_VISION_MAX_OUTPUT_TOKENS")
        try:
            os.environ["STOCK_VISION_MAX_OUTPUT_TOKENS"] = "32768"
            self.assertEqual(svt._vision_max_output_tokens(), 32768)
            os.environ["STOCK_VISION_MAX_OUTPUT_TOKENS"] = "not-int"
            self.assertEqual(svt._vision_max_output_tokens(), 65535)
            os.environ["STOCK_VISION_MAX_OUTPUT_TOKENS"] = "999999"
            self.assertEqual(svt._vision_max_output_tokens(), 65535)
        finally:
            if old is None:
                os.environ.pop("STOCK_VISION_MAX_OUTPUT_TOKENS", None)
            else:
                os.environ["STOCK_VISION_MAX_OUTPUT_TOKENS"] = old

    def test_looks_truncated_json(self):
        from services.stock_vision_table import _looks_truncated_json

        self.assertTrue(_looks_truncated_json('{"rows":[{"a":1},', ""))
        self.assertTrue(_looks_truncated_json("{}", "MAX_TOKENS"))
        self.assertFalse(_looks_truncated_json('{"rows":[]}', "STOP"))


class MappingPolicyTests(unittest.TestCase):
    def test_model_overrides_only_low_confidence_ignore(self):
        from services.stock_vision_table import map_vision_table

        table = {
            "header_rows": [
                [
                    {"text": "Product", "col_index": 0, "x_center": 0.1},
                    {"text": "Opening", "col_index": 1, "x_center": 0.3},
                    {"text": "ZZZCOL", "col_index": 2, "x_center": 0.5},
                    {"text": "Sale", "col_index": 3, "x_center": 0.7},
                    {"text": "Closing", "col_index": 4, "x_center": 0.9},
                ]
            ],
            "column_count": 5,
            "rows": [
                {
                    "row_index": 0,
                    "cells": ["X", "10", "5", "3", "12"],
                    "is_total_row": False,
                }
            ],
            "proposed_mapping": [
                {"col_index": 2, "canonical": "purchase_qty", "confidence": 0.7}
            ],
            "unreadable_cells": [],
        }
        mapped = map_vision_table(table)
        col2 = next(c for c in mapped["column_map"] if c["col_index"] == 2)
        self.assertEqual(col2["canonical"], "purchase_qty")
        self.assertEqual(col2["source"], "model")

    def test_mapping_disagree_when_confident_resolver_wins(self):
        from services.stock_vision_table import map_vision_table

        table = {
            "header_rows": [
                [
                    {"text": "Product", "col_index": 0, "x_center": 0.1},
                    {"text": "Opening", "col_index": 1, "x_center": 0.3},
                    {"text": "Purchase", "col_index": 2, "x_center": 0.5},
                    {"text": "Sale", "col_index": 3, "x_center": 0.7},
                    {"text": "Closing", "col_index": 4, "x_center": 0.9},
                ]
            ],
            "column_count": 5,
            "rows": [
                {
                    "row_index": 0,
                    "cells": ["X", "10", "5", "3", "12"],
                    "is_total_row": False,
                }
            ],
            "proposed_mapping": [
                {"col_index": 2, "canonical": "sales_qty", "confidence": 0.99}
            ],
            "unreadable_cells": [],
        }
        mapped = map_vision_table(table)
        col2 = next(c for c in mapped["column_map"] if c["col_index"] == 2)
        self.assertEqual(col2["canonical"], "purchase_qty")
        self.assertEqual(col2["source"], "resolver")
        self.assertTrue(
            any(e.startswith("MAPPING_DISAGREE col=2") for e in mapped["errors"])
        )

    def test_x_order_violation_structural(self):
        from services.stock_vision_table import map_vision_table

        table = {
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
        mapped = map_vision_table(table)
        self.assertIn("STRUCTURAL_ERROR", mapped["errors"])
        self.assertEqual(mapped["line_items"], [])


class MergeRereadTests(unittest.TestCase):
    def _item(self, name, opening, purchase, sales, closing, row_index, ok=True):
        return {
            "product_name": name,
            "packing": None,
            "opening_qty": float(opening),
            "receipts_qty": float(purchase),
            "sales_qty": float(sales),
            "sales_value": 0.0,
            "closing_qty": float(closing),
            "closing_value": 0.0,
            "extra": {
                "vision_row_index": row_index,
                "field_source": {},
                "stock_identity_ok": ok,
            },
        }

    def test_valid_reread_replaces(self):
        from services.stock_vision_table import merge_reread

        first = [self._item("LIV 52", 10, 5, 3, 99, 0, ok=False)]
        reread = [self._item("LIV 52", 10, 5, 3, 12, 0, ok=True)]
        out = merge_reread(first, reread)
        self.assertEqual(out[0]["closing_qty"], 12.0)
        self.assertFalse((out[0].get("extra") or {}).get("candidates"))

    def test_invalid_reread_becomes_candidate(self):
        from services.stock_vision_table import merge_reread

        first = [self._item("LIV 52", 10, 5, 3, 12, 0, ok=True)]
        reread = [self._item("LIV 52", 10, 5, 3, 99, 0, ok=False)]
        out = merge_reread(first, reread)
        self.assertEqual(out[0]["closing_qty"], 12.0)
        candidates = (out[0].get("extra") or {}).get("candidates") or []
        self.assertTrue(candidates)
        self.assertEqual(candidates[0]["fields"]["closing_qty"], 99.0)

    def test_no_cell_mix(self):
        from services.stock_vision_table import merge_reread

        first = [self._item("LIV 52", 10, 5, 3, 12, 0)]
        # Valid re-read with different opening — whole row replaces, no mix.
        reread = [self._item("LIV 52", 20, 0, 0, 20, 0)]
        out = merge_reread(first, reread)
        self.assertEqual(out[0]["opening_qty"], 20.0)
        self.assertEqual(out[0]["receipts_qty"], 0.0)
        self.assertEqual(out[0]["closing_qty"], 20.0)


class ExtractVisionCallTests(unittest.TestCase):
    def setUp(self):
        ensure_gemini_offline_guard()
        reset_gemini_test_call_count()
        clear_gemini_responses()

    def tearDown(self):
        clear_gemini_responses()

    def test_invalid_json_one_call(self):
        from services.stock_vision_table import extract_stock_table_vision

        # Offline guard returns this as JSON text inside candidates — make it invalid
        # by returning a non-object payload string via a fake response shape.
        set_gemini_responses(
            [
                {
                    "candidates": [
                        {"content": {"parts": [{"text": "not-json-at-all"}]}}
                    ]
                }
            ]
        )
        before = gemini_test_call_count()
        out = extract_stock_table_vision([b"\xff\xd8\xff\xd9" + b"\x00" * 20])
        self.assertEqual(gemini_test_call_count(), before + 1)
        self.assertEqual(out.get("error"), "INVALID_VISION_JSON")
        self.assertIn("raw", out)

    def test_valid_response_parses(self):
        from services.stock_vision_table import extract_stock_table_vision

        payload = _load("op_pur_sale_cls_qty_value.json")
        set_gemini_responses([payload])
        before = gemini_test_call_count()
        out = extract_stock_table_vision([b"\xff\xd8\xff\xd9" + b"\x00" * 20])
        self.assertEqual(gemini_test_call_count(), before + 1)
        self.assertNotIn("error", out)
        self.assertEqual(out.get("column_count"), 9)


class ProbeScriptTests(unittest.TestCase):
    def test_probe_without_live_zero_calls(self):
        ensure_gemini_offline_guard()
        reset_gemini_test_call_count()
        before = gemini_test_call_count()
        script = (
            Path(__file__).resolve().parents[1] / "scripts" / "vision_table_probe.py"
        )
        # Tiny fake image path — script should refuse before any Gemini use.
        fake = Path("/tmp/vision_probe_dummy.jpg")
        fake.write_bytes(b"\xff\xd8\xff\xd9")
        proc = subprocess.run(
            [sys.executable, str(script), str(fake)],
            capture_output=True,
            text=True,
            cwd=str(Path(__file__).resolve().parents[1]),
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("--live", proc.stdout + proc.stderr)
        self.assertEqual(gemini_test_call_count(), before)


class PromptTests(unittest.TestCase):
    def test_build_table_prompt_contains_fields(self):
        from services.stock_vision_table import build_table_prompt
        from services.stock_header_resolver import CANONICAL_FIELDS

        text = build_table_prompt()
        self.assertIn("Do not interpret or calculate", text)
        self.assertIn("opening_qty", text)
        self.assertIn("tables", text)
        self.assertIn("Never merge two tables", text)
        self.assertIn(CANONICAL_FIELDS[0], text)

    def test_reread_prompt_lists_rows_without_expected_values(self):
        from services.stock_vision_table import build_reread_prompt

        text = build_reread_prompt(
            [{"row_index": 2, "product_name": "LIV 52"}]
        )
        self.assertIn("Re-read ONLY these rows", text)
        self.assertIn("2:LIV 52", text)
        self.assertIn("misread digits", text.lower())
        self.assertNotIn("expected", text.lower())
        self.assertNotIn("identity", text.lower())
        # Must not include a prior reading or balancing target.
        self.assertNotIn("80/0", text)


if __name__ == "__main__":
    unittest.main()
