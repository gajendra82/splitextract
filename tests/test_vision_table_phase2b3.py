"""Phase 2b-3: Vision table hardening acceptance tests (offline)."""

from __future__ import annotations

import json
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
FIXTURE_5 = (
    Path(__file__).resolve().parent / "fixtures" / "stock" / "rollout_5" / "expected.json"
)


def _rollout_paths():
    return sorted(
        p
        for p in RESP_DIR.glob("rollout_*.json")
        if "handcheck" not in str(p)
    )


def _schema_conformant_table():
    return {
        "tables": [
            {
                "table_index": 0,
                "x_range": [0.0, 1.0],
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
                        "y_center": 0.25,
                        "cells": ["HIORA K", "9", "50", "21", "38"],
                        "is_total_row": False,
                    }
                ],
                "proposed_mapping": [
                    {"col_index": 0, "canonical": "product_name", "confidence": 1.0},
                    {"col_index": 1, "canonical": "opening_qty", "confidence": 1.0},
                    {"col_index": 2, "canonical": "purchase_qty", "confidence": 1.0},
                    {"col_index": 3, "canonical": "sales_qty", "confidence": 1.0},
                    {"col_index": 4, "canonical": "closing_qty", "confidence": 1.0},
                ],
            }
        ],
        "unreadable_cells": [],
        "stockist_name": None,
        "statement_period": None,
    }


class RolloutReplayTests(unittest.TestCase):
    """Replay REAL saved probe responses (shape bug regression)."""

    def test_every_rollout_replays_without_shape_missing_core(self):
        from services.stock_vision_table import map_vision_table

        paths = _rollout_paths()
        self.assertGreaterEqual(len(paths), 10)
        for path in paths:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if raw.get("error"):
                continue
            mapped = map_vision_table(raw, request_id="replay")
            coercions = mapped.get("coercions") or {}
            # Shape coercions are expected on old live responses.
            self.assertTrue(
                coercions.get("legacy_single_table_root")
                or coercions.get("header_rows_flat")
                or coercions.get("header_rows_cells_wrapper")
                or coercions.get("proposed_mapping_alias_key")
                or coercions.get("dict_cells"),
                msg=f"{path.name} expected shape coercions, got {coercions}",
            )
            items = mapped.get("line_items") or []
            # Shape bug before normalize left column_map empty → all-zero qtys.
            # After normalize, a non-empty header must produce a column_map whenever
            # the raw JSON had header_rows / tables.
            raw_has_headers = bool(
                raw.get("header_rows")
                or any(
                    isinstance(t, dict) and t.get("header_rows")
                    for t in (raw.get("tables") or [])
                )
            )
            if raw_has_headers and items:
                self.assertTrue(
                    mapped.get("column_map"),
                    msg=f"{path.name}: rows present but empty column_map (shape bug)",
                )

    def test_rollout_5_matches_expected_exactly(self):
        from services.stock_vision_table import map_vision_table

        path = RESP_DIR / (
            "rollout_5_0000735216_2026_08_ZA_06_333_04092026081522__2___1_.png.json"
        )
        self.assertTrue(path.is_file())
        self.assertTrue(FIXTURE_5.is_file())
        raw = json.loads(path.read_text(encoding="utf-8"))
        expected = json.loads(FIXTURE_5.read_text(encoding="utf-8"))
        mapped = map_vision_table(raw, request_id="replay5")
        self.assertNotIn("MISSING_CORE_COLUMNS", mapped.get("errors") or [])
        got = mapped.get("line_items") or []
        exp_items = expected.get("line_items") or []
        self.assertEqual(len(got), len(exp_items))
        for g, e in zip(got, exp_items):
            self.assertEqual(g.get("product_name"), e.get("product_name"))
            self.assertEqual(g.get("opening_qty"), e.get("opening_qty"))
            self.assertEqual(g.get("receipts_qty"), e.get("receipts_qty"))
            self.assertEqual(g.get("sales_qty"), e.get("sales_qty"))
            self.assertEqual(g.get("closing_qty"), e.get("closing_qty"))


class DualColumnTests(unittest.TestCase):
    def test_rollout_1_splits_into_two_tables(self):
        from services.stock_vision_table import map_vision_table

        path = RESP_DIR / (
            "rollout_1_0000734827_2026_08_ZA_24_259_03092026065501.jpg.json"
        )
        raw = json.loads(path.read_text(encoding="utf-8"))
        mapped = map_vision_table(raw, request_id="dual1")
        self.assertEqual(mapped.get("tables_found"), 2)
        self.assertEqual(mapped.get("split_events"), 1)
        items = mapped.get("line_items") or []
        # Single-table map had ~39 rows; split keeps both halves (~78).
        self.assertGreater(len(items), 50)
        names = [it.get("product_name") for it in items if it.get("product_name")]
        # Products from both left and right columns present; none merged away.
        self.assertGreater(len(names), 40)

    def test_hand_built_wide_table_fallback_splitter(self):
        from services.stock_vision_table import map_vision_table

        # One wide table: left Product..Closing, right Product..Closing.
        headers = []
        for i, text in enumerate(
            ["Product", "Opening", "Purchase", "Sale", "Closing"]
        ):
            headers.append({"text": text, "col_index": i, "x_center": 0.1 + i * 0.08})
        for i, text in enumerate(
            ["Product", "Opening", "Purchase", "Sale", "Closing"]
        ):
            headers.append(
                {"text": text, "col_index": 5 + i, "x_center": 0.55 + i * 0.08}
            )
        table = {
            "header_rows": [headers],
            "column_count": 10,
            "rows": [
                {
                    "row_index": 0,
                    "cells": [
                        "LEFT A",
                        "1",
                        "2",
                        "0",
                        "3",
                        "RIGHT B",
                        "4",
                        "5",
                        "1",
                        "8",
                    ],
                    "is_total_row": False,
                }
            ],
            "proposed_mapping": [],
        }
        mapped = map_vision_table(table, request_id="split")
        self.assertEqual(mapped.get("tables_found"), 2)
        self.assertEqual(mapped.get("split_events"), 1)
        names = [it.get("product_name") for it in mapped.get("line_items") or []]
        self.assertIn("LEFT A", names)
        self.assertIn("RIGHT B", names)
        # Same name in both halves kept twice + flagged.
        table2 = {
            "header_rows": [headers],
            "column_count": 10,
            "rows": [
                {
                    "row_index": 0,
                    "cells": [
                        "SAME",
                        "1",
                        "0",
                        "0",
                        "1",
                        "SAME",
                        "2",
                        "0",
                        "0",
                        "2",
                    ],
                    "is_total_row": False,
                }
            ],
            "proposed_mapping": [],
        }
        mapped2 = map_vision_table(table2, request_id="dup")
        items = mapped2.get("line_items") or []
        self.assertEqual(len(items), 2)
        self.assertIn("DUPLICATE_PRODUCT_IN_PAGE", mapped2.get("errors") or [])
        for it in items:
            self.assertIn(
                "DUPLICATE_PRODUCT_IN_PAGE",
                (it.get("extra") or {}).get("flags") or [],
            )


class CropDedupeTests(unittest.TestCase):
    def test_overlap_exact_deduped_conflict_kept(self):
        from services.stock_vision_table import dedupe_crop_overlap_rows

        items = [
            {
                "product_name": "A",
                "opening_qty": 1.0,
                "receipts_qty": 0.0,
                "sales_qty": 0.0,
                "closing_qty": 1.0,
                "extra": {},
            },
            {
                "product_name": "B",
                "opening_qty": 2.0,
                "receipts_qty": 0.0,
                "sales_qty": 0.0,
                "closing_qty": 2.0,
                "extra": {},
            },
            {
                "product_name": "A",  # exact overlap duplicate
                "opening_qty": 1.0,
                "receipts_qty": 0.0,
                "sales_qty": 0.0,
                "closing_qty": 1.0,
                "extra": {},
            },
            {
                "product_name": "B",  # conflict overlap
                "opening_qty": 9.0,
                "receipts_qty": 0.0,
                "sales_qty": 0.0,
                "closing_qty": 9.0,
                "extra": {},
            },
            {
                "product_name": "C",
                "opening_qty": 3.0,
                "receipts_qty": 0.0,
                "sales_qty": 0.0,
                "closing_qty": 3.0,
                "extra": {},
            },
        ]
        out, errs = dedupe_crop_overlap_rows(items)
        names = [it["product_name"] for it in out]
        self.assertEqual(names.count("A"), 1)
        self.assertEqual(names.count("B"), 2)
        self.assertEqual(names.count("C"), 1)
        self.assertIn("CROP_OVERLAP_CONFLICT", errs)
        b_flags = [
            (it.get("extra") or {}).get("flags")
            for it in out
            if it["product_name"] == "B"
        ]
        self.assertTrue(all("CROP_OVERLAP_CONFLICT" in (f or []) for f in b_flags))


class RereadBandTests(unittest.TestCase):
    def setUp(self):
        ensure_gemini_offline_guard()
        reset_gemini_test_call_count()
        clear_gemini_responses()

    def tearDown(self):
        clear_gemini_responses()

    def test_reread_prompt_has_rows_no_expected(self):
        from services.stock_vision_table import build_reread_prompt

        text = build_reread_prompt(
            [{"row_index": 1, "product_name": "HIORA K 50 GM"}]
        )
        self.assertIn("1:HIORA K 50 GM", text)
        low = text.lower()
        self.assertNotIn("expected", low)
        self.assertNotIn("identity", low)
        self.assertNotIn("80/0", text)

    def test_crop_row_bands_uses_y_center(self):
        from PIL import Image
        import io
        from services.stock_vision_table import crop_row_bands

        im = Image.new("RGB", (200, 400), color=(255, 255, 255))
        buf = io.BytesIO()
        im.save(buf, format="PNG")
        img = buf.getvalue()
        crops = crop_row_bands(
            img,
            [{"row_index": 0, "product_name": "X", "y_center": 0.5}],
            scale=2.0,
        )
        self.assertEqual(len(crops), 1)
        with Image.open(io.BytesIO(crops[0])) as out:
            # Upscaled: width 400, height = header+row band * 2
            self.assertEqual(out.size[0], 400)
            self.assertGreater(out.size[1], 100)

    def test_merge_reread_valid_replaces_invalid_becomes_candidate(self):
        from services.stock_vision_table import merge_reread
        from services.stock_row_classifier import RowStatus, classify_row, read_row_fields

        first = [
            {
                "product_name": "HIORA K 50 GM",
                "opening_qty": 20.0,
                "receipts_qty": 50.0,
                "sales_qty": 5.0,
                "closing_qty": 75.0,
                "extra": {"vision_row_index": 1},
            }
        ]
        # Valid identity: 80+0-5=75
        good = {
            "product_name": "HIORA K 50 GM",
            "opening_qty": 80.0,
            "receipts_qty": 0.0,
            "sales_qty": 5.0,
            "closing_qty": 75.0,
            "extra": {"vision_row_index": 1},
        }
        status, _ = classify_row(read_row_fields(good))
        self.assertIn(status, {RowStatus.VALID, RowStatus.MINOR_DISCREPANCY})
        merged = merge_reread(first, [good])
        self.assertEqual(merged[0]["opening_qty"], 80.0)

        # Invalid re-read → keep first, store candidate
        bad = {
            "product_name": "HIORA K 50 GM",
            "opening_qty": 1.0,
            "receipts_qty": 2.0,
            "sales_qty": 3.0,
            "closing_qty": 99.0,
            "extra": {"vision_row_index": 1},
        }
        merged2 = merge_reread(first, [bad])
        self.assertEqual(merged2[0]["opening_qty"], 20.0)
        cands = (merged2[0].get("extra") or {}).get("candidates") or []
        self.assertTrue(any(c.get("source") == "vision_reread" for c in cands))


class CoercionCounterTests(unittest.TestCase):
    def test_schema_conformant_zero_coercions(self):
        from services.stock_vision_table import _normalize_vision_table, map_vision_table

        doc = _schema_conformant_table()
        _norm, coercions = _normalize_vision_table(doc, request_id="t", log=False)
        self.assertEqual(coercions, {})
        mapped = map_vision_table(doc, request_id="t")
        self.assertEqual(mapped.get("coercions") or {}, {})

    def test_flat_shape_nonzero_coercions(self):
        from services.stock_vision_table import _normalize_vision_table

        flat = {
            "header_rows": [
                {"text": "Product", "col_index": 0, "x_center": 0.1},
                {"text": "Opening", "col_index": 1, "x_center": 0.3},
            ],
            "column_count": 2,
            "rows": [{"row_index": 0, "cells": ["A", "1"], "is_total_row": False}],
            "proposed_mapping": [
                {"col_index": 0, "field": "product_name", "confidence": 1.0}
            ],
        }
        _norm, coercions = _normalize_vision_table(flat, request_id="t", log=False)
        self.assertGreater(sum(coercions.values()), 0)
        self.assertIn("header_rows_flat", coercions)
        self.assertIn("proposed_mapping_alias_key", coercions)


class TallCropBudgetTests(unittest.TestCase):
    def setUp(self):
        ensure_gemini_offline_guard()
        reset_gemini_test_call_count()
        clear_gemini_responses()

    def tearDown(self):
        clear_gemini_responses()
        import os

        for k in (
            "STOCK_VISION_SPLIT_MIN_HEIGHT",
            "STOCK_IDENTITY_VETO",
            "STOCK_VISION_VERTICAL_SPLIT_EAGER",
        ):
            os.environ.pop(k, None)

    def test_vertical_split_sets_reread_skipped_budget(self):
        import os
        from services.stock_vision_table import run_vision_table_path

        os.environ["STOCK_VISION_SPLIT_MIN_HEIGHT"] = "100"
        os.environ["STOCK_IDENTITY_VETO"] = "true"
        os.environ["STOCK_RECONCILIATION_ENFORCE"] = "true"
        # Keep recon-priority deferral even if .env enables eager split.
        os.environ["STOCK_VISION_VERTICAL_SPLIT_EAGER"] = "false"

        # Tiny JPEG that would previously trigger an eager split.
        from PIL import Image
        import io

        im = Image.new("RGB", (200, 400), color=(240, 240, 240))
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=85)
        img = buf.getvalue()

        payload = _schema_conformant_table()
        # Single initial extract; vertical split deferred for recon priority.
        set_gemini_responses([payload])

        halves_mock = patch(
            "services.stock_vision_table._vertical_halves",
            return_value=[img, img],
        )
        with patch(
            "services.stock_vision_table._needs_vertical_split", return_value=True
        ), halves_mock as halves_fn, patch(
            "services.stock_row_classifier.veto_decision",
            return_value={"veto": False, "flagged_rows": []},
        ):
            out = run_vision_table_path(
                img,
                "image",
                {"filename": "tall.jpg", "ext": ".jpg", "request_id": "t"},
            )
        self.assertEqual(out.get("status"), "ok")
        extra = ((out.get("result") or {}).get("totals") or {}).get("extra") or {}
        # Initial extract only; split deferred so recon recovery slot is reserved.
        self.assertEqual(out.get("gemini_calls"), 1)
        self.assertEqual(out.get("gemini_budget"), 2)
        self.assertEqual(extra.get("vertical_split_deferred"), "recon_priority")
        halves_fn.assert_not_called()


class ProbeReplayCliTests(unittest.TestCase):
    def test_replay_zero_gemini_calls(self):
        import subprocess
        import sys

        ensure_gemini_offline_guard()
        reset_gemini_test_call_count()
        before = gemini_test_call_count()
        script = Path(__file__).resolve().parents[1] / "scripts" / "vision_table_probe.py"
        name = "rollout_5_0000735216_2026_08_ZA_06_333_04092026081522__2___1_.png"
        proc = subprocess.run(
            [sys.executable, str(script), "--replay", name],
            capture_output=True,
            text=True,
            cwd=str(Path(__file__).resolve().parents[1]),
        )
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        self.assertIn("gemini_calls=0", proc.stdout)
        self.assertIn("HIORA K TOOTHPASTE 100 GM", proc.stdout)
        self.assertEqual(gemini_test_call_count(), before)


if __name__ == "__main__":
    unittest.main()
