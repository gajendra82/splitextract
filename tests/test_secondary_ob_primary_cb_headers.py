"""Ob/Primary/Cb/Secondary Qty grids — header resolve + Vision unglue."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from services.stock_header_resolver import resolve_columns
from services.stock_vision_table import _unglue_ob_primary_cb_table


def _cells(*texts):
    return [
        {"text": t, "col_index": i, "x_center": None, "subheader_text": None}
        for i, t in enumerate(texts)
    ]


class SecondaryObPrimaryCbHeaderTests(unittest.TestCase):
    def test_ideal_headers_map_to_stock_fields(self):
        result = resolve_columns(
            _cells(
                "Cust Code",
                "Customer Name",
                "Mat Code",
                "Mat Name",
                "Mrp",
                "Secondary Rate",
                "Ob (Qty)",
                "Primary (Qty)",
                "Cb (Qty)",
                "Secondary Qty Total",
                "Secondary Value",
            )
        )
        by = {c["col_index"]: c["canonical"] for c in result["columns"]}
        self.assertEqual(by[0], "ignore")
        self.assertEqual(by[1], "ignore")
        self.assertEqual(by[2], "product_code")
        self.assertEqual(by[3], "product_name")
        self.assertEqual(by[4], "ignore")  # Mrp demoted; Secondary Rate kept
        self.assertEqual(by[5], "rate")
        self.assertEqual(by[6], "opening_qty")
        self.assertEqual(by[7], "purchase_qty")
        self.assertEqual(by[8], "closing_qty")
        self.assertEqual(by[9], "sales_qty")
        self.assertEqual(by[10], "sales_value")
        self.assertFalse(
            any(
                e.get("code") == "DUPLICATE_CANONICAL"
                and e.get("canonical") == "product_name"
                for e in result["errors"]
                if isinstance(e, dict)
            )
        )

    def test_ocr_truncated_headers_still_map(self):
        result = resolve_columns(
            _cells(
                "Customer Name",
                "Mat Code",
                "Mat Name",
                "Secondary Ra",
                "Ob (Qty)",
                "Primary (Qty)",
                "Cb (Qty)",
                "Secondary Qty Tota",
                "Secondary Valu",
            )
        )
        by = {c["col_index"]: c["canonical"] for c in result["columns"]}
        self.assertEqual(by[0], "ignore")
        self.assertEqual(by[1], "product_code")
        self.assertEqual(by[2], "product_name")
        self.assertEqual(by[3], "rate")
        self.assertEqual(by[4], "opening_qty")
        self.assertEqual(by[5], "purchase_qty")
        self.assertEqual(by[6], "closing_qty")
        self.assertEqual(by[7], "sales_qty")
        self.assertEqual(by[8], "sales_value")


class UnglueObPrimaryCbTests(unittest.TestCase):
    def test_peels_rate_ob_glue_and_splits_primary_cb(self):
        table = {
            "column_count": 6,
            "header_rows": [
                [
                    {"text": "Mat Name", "col_index": 0, "x_center": 0.1},
                    {"text": "Secondary Ra", "col_index": 1, "x_center": 0.3},
                    {"text": "Ob (Qty)", "col_index": 2, "x_center": 0.4},
                    {"text": "Primary (QCb (Qty)", "col_index": 3, "x_center": 0.5},
                    {"text": "Secondary Qty Tota", "col_index": 4, "x_center": 0.7},
                    {"text": "Secondary Valu", "col_index": 5, "x_center": 0.9},
                ]
            ],
            "rows": [
                {
                    "cells": [
                        "ASHWAGANDHA TABLETS 60 S INDIA",
                        "176.51 0",
                        "60",
                        "58",
                        "2",
                        "353.02",
                    ],
                    "is_total_row": False,
                }
            ],
            "proposed_mapping": [],
        }
        with patch.dict(
            "os.environ", {"STOCK_VISION_UNGLUE_OB_PRIMARY_CB": "true"}, clear=False
        ):
            out = _unglue_ob_primary_cb_table(table, {})
        self.assertEqual(out["column_count"], 7)
        headers = {
            int(c["col_index"]): c["text"]
            for c in out["header_rows"][0]
            if isinstance(c, dict)
        }
        self.assertEqual(headers[1], "Secondary Rate")
        self.assertEqual(headers[2], "Ob (Qty)")
        self.assertEqual(headers[3], "Primary (Qty)")
        self.assertEqual(headers[4], "Cb (Qty)")
        self.assertEqual(
            out["rows"][0]["cells"],
            [
                "ASHWAGANDHA TABLETS 60 S INDIA",
                "176.51",
                "0",
                "60",
                "58",
                "2",
                "353.02",
            ],
        )

    def test_flag_off_leaves_table_unchanged(self):
        table = {
            "column_count": 4,
            "header_rows": [
                [
                    {"text": "Secondary Ra", "col_index": 0},
                    {"text": "Ob (Qty)", "col_index": 1},
                    {"text": "Primary (QCb (Qty)", "col_index": 2},
                    {"text": "Secondary Qty Tota", "col_index": 3},
                ]
            ],
            "rows": [{"cells": ["176.51 0", "60", "58", "2"], "is_total_row": False}],
        }
        with patch.dict(
            "os.environ", {"STOCK_VISION_UNGLUE_OB_PRIMARY_CB": "false"}, clear=False
        ):
            out = _unglue_ob_primary_cb_table(table, {})
        self.assertEqual(out["column_count"], 4)
        self.assertEqual(out["rows"][0]["cells"][0], "176.51 0")

    def test_shift_peel_when_primary_cb_separate(self):
        table = {
            "column_count": 6,
            "header_rows": [
                [
                    {"text": "Secondary Ra", "col_index": 0, "x_center": 0.2},
                    {"text": "Ob (Qty)", "col_index": 1, "x_center": 0.35},
                    {"text": "Primary (Q", "col_index": 2, "x_center": 0.5},
                    {"text": "Cb (Qty)", "col_index": 3, "x_center": 0.65},
                    {"text": "Secondary Qty Tota", "col_index": 4, "x_center": 0.8},
                    {"text": "Secondary Valu", "col_index": 5, "x_center": 0.95},
                ]
            ],
            "rows": [
                {
                    "cells": ["176.51 0", "60", "58", "2", "2", "353.02"],
                    "is_total_row": False,
                },
                {
                    # Ob already 0 — do not peel glued Primary into Ob.
                    "cells": ["176.51 5", "0", "5", "0", "0", None],
                    "is_total_row": False,
                },
            ],
            "proposed_mapping": [],
        }
        with patch.dict(
            "os.environ", {"STOCK_VISION_UNGLUE_OB_PRIMARY_CB": "true"}, clear=False
        ):
            out = _unglue_ob_primary_cb_table(table, {})
        self.assertEqual(
            out["rows"][0]["cells"],
            ["176.51", "0", "60", "58", "2", "353.02"],
        )
        self.assertEqual(out["rows"][1]["cells"][0], "176.51 5")
        self.assertEqual(out["rows"][1]["cells"][1], "0")

    def test_shift_peel_dot_and_colon_glue(self):
        table = {
            "column_count": 6,
            "header_rows": [
                [
                    {"text": "Secondary Ra", "col_index": 0},
                    {"text": "Ob (Qty)", "col_index": 1},
                    {"text": "Primary (Q", "col_index": 2},
                    {"text": "Cb (Qty)", "col_index": 3},
                    {"text": "Secondary Qty Tota", "col_index": 4},
                    {"text": "Secondary Valu", "col_index": 5},
                ]
            ],
            "rows": [
                {
                    "cells": ["139 14:0", "100", "98", "2", "2", "278.28"],
                    "is_total_row": False,
                },
                {
                    "cells": ["176.51.0", "60", "57", "2", "3", "529.53"],
                    "is_total_row": False,
                },
                {
                    # Secondary Value parked in Secondary Qty after Ob glue.
                    "cells": ["176.51 0", "60", "58", "2", "353.02", None],
                    "is_total_row": False,
                },
            ],
        }
        with patch.dict(
            "os.environ", {"STOCK_VISION_UNGLUE_OB_PRIMARY_CB": "true"}, clear=False
        ):
            out = _unglue_ob_primary_cb_table(table, {})
        self.assertEqual(
            out["rows"][0]["cells"],
            ["139.14", "0", "100", "98", "2", "278.28"],
        )
        self.assertEqual(
            out["rows"][1]["cells"],
            ["176.51", "0", "60", "57", "3", "529.53"],
        )
        self.assertEqual(
            out["rows"][2]["cells"],
            ["176.51", "0", "60", "58", "2", "353.02"],
        )


if __name__ == "__main__":
    unittest.main()
