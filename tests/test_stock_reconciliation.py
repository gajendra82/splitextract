"""Stock reconciliation gate: mark inconsistent rows, never rewrite printed qty."""

from __future__ import annotations

import logging
import os
import unittest
from unittest.mock import patch

from services.stock_header_resolver import resolve_columns
from services.stock_reconciliation import (
    apply_stock_reconciliation,
    compute_closing_formula,
    expected_closing_qty,
    reconcile_row,
)
from services.stock_vision_table import map_vision_table


def _cells(*texts):
    return [
        {
            "text": t,
            "x_center": None,
            "col_index": i,
            "subheader_text": None,
        }
        for i, t in enumerate(texts)
    ]


def _fs(*fields):
    return {f: "printed" for f in fields}


class MonthlySsHeaderTests(unittest.TestCase):
    def test_opening_purchase_goods_ret_sale_purc_ret_balance(self):
        headers = [
            "Code",
            "Product",
            "Pack",
            "Opening Qty",
            "Purchase Qty",
            "Goods Ret. Qty",
            "Total Qty",
            "Sale Qty",
            "Purc. Ret. Qty",
            "Balance Qty",
        ]
        result = resolve_columns(_cells(*headers))
        canons = [c["canonical"] for c in result["columns"]]
        self.assertEqual(
            canons,
            [
                "ignore",
                "product_name",
                "pack",
                "opening_qty",
                "purchase_qty",
                "sales_return_qty",
                "total_qty",
                "sales_qty",
                "purchase_return_qty",
                "closing_qty",
            ],
        )

    def test_purc_ret_never_maps_to_purchase(self):
        for text in ("Purc. Ret", "Purc. Ret. Qty", "Purchase Return"):
            with self.subTest(header=text):
                result = resolve_columns(_cells(text))
                self.assertEqual(result["columns"][0]["canonical"], "purchase_return_qty")

    def test_goods_sales_ret_and_closing_value_headers(self):
        cases = [
            ("Goods Ret", "sales_return_qty"),
            ("Goods Return", "sales_return_qty"),
            ("Sales Ret", "sales_return_qty"),
            ("Sales Return", "sales_return_qty"),
            ("Balance Qty", "closing_qty"),
            ("Closing", "closing_qty"),
            ("Stock", "closing_qty"),
            ("STK VAL", "closing_value"),
            ("Purchase Qty", "purchase_qty"),
            ("Purchased Qty", "purchase_qty"),
        ]
        for text, canon in cases:
            with self.subTest(header=text):
                result = resolve_columns(_cells(text))
                self.assertEqual(result["columns"][0]["canonical"], canon)


class LayoutATests(unittest.TestCase):
    """Opening | Purchase | Total | Sales | Closing → Closing = Total - Sales."""

    def _item(self, closing: float) -> dict:
        return {
            "product_name": "LAYOUT A PRODUCT",
            "opening_qty": 10.0,
            "receipts_qty": 20.0,
            "sales_qty": 5.0,
            "closing_qty": closing,
            "extra": {
                "total_stock": 30.0,
                "field_source": _fs(
                    "opening_qty",
                    "purchase_qty",
                    "total_qty",
                    "sales_qty",
                    "closing_qty",
                ),
            },
        }

    def test_layout_a_closing_total_minus_sales(self):
        item = self._item(25.0)
        expected, meta = compute_closing_formula(item)
        self.assertEqual(expected, 25.0)
        self.assertEqual(meta["base_source"], "total_qty")
        self.assertEqual(meta["adjustments"]["sales_return_qty"], 0.0)
        self.assertEqual(meta["adjustments"]["sales_qty"], 5.0)
        recon = reconcile_row(item)
        self.assertTrue(recon["valid"])
        self.assertEqual(recon["base_source"], "total_qty")


class LayoutBGoodsReturnTests(unittest.TestCase):
    """Opening|Purchase|Total|Goods Ret|Sales|Purc Ret|Closing."""

    def _item(self, closing: float, goods_ret: float = 2.0, pret: float = 1.0) -> dict:
        return {
            "product_name": "GOODS RET PRODUCT",
            "opening_qty": 10.0,
            "receipts_qty": 20.0,
            "sales_qty": 5.0,
            "closing_qty": closing,
            "extra": {
                "total_stock": 30.0,
                "sale_return": goods_ret,
                "purchase_return": pret,
                "field_source": _fs(
                    "opening_qty",
                    "purchase_qty",
                    "total_qty",
                    "sales_return_qty",
                    "sales_qty",
                    "purchase_return_qty",
                    "closing_qty",
                ),
            },
        }

    def test_goods_return_26_passes(self):
        # 30 + 2 - 5 - 1 = 26
        item = self._item(26.0)
        self.assertEqual(expected_closing_qty(item), 26.0)
        recon = reconcile_row(item)
        self.assertTrue(recon["valid"], recon)
        self.assertEqual(recon["base_source"], "total_qty")
        self.assertFalse(recon["sales_return_in_total"])
        self.assertEqual(recon["adjustments"]["sales_return_qty"], 2.0)
        self.assertEqual(recon["adjustments"]["purchase_return_qty"], 1.0)

    def test_goods_return_closing_24_fails_not_rewritten(self):
        item = self._item(24.0)
        out = apply_stock_reconciliation(
            {
                "line_items": [item],
                "totals": {"sales_value": None, "closing_value": None, "extra": {}},
            },
            request_id="goods-bad",
        )
        row = out["line_items"][0]
        self.assertEqual(row["closing_qty"], 24.0)
        self.assertTrue(row["extra"].get("reconciliation_failed"))
        self.assertEqual(row["extra"]["reconciliation"]["expected_closing_qty"], 26.0)

    def test_expiry_and_sample_adjustments(self):
        item = self._item(23.0, goods_ret=2.0, pret=1.0)
        item["extra"]["exp_damage"] = 2.0
        item["extra"]["free_out_qty"] = 1.0
        item["extra"]["field_source"]["expiry_damage_qty"] = "printed"
        # 30 + 2 - 5 - 1 - 2 - 1 = 23
        self.assertEqual(expected_closing_qty(item), 23.0)
        self.assertTrue(reconcile_row(item)["valid"])


class HioraReconciliationTests(unittest.TestCase):
    def _hiora(self, closing: float) -> dict:
        return {
            "product_name": "HIORA K TOOTHPASTE 100 GM",
            "opening_qty": 9.0,
            "receipts_qty": 50.0,
            "sales_qty": 21.0,
            "closing_qty": closing,
            "extra": {
                "total_stock": 59.0,
                "sale_return": 0.0,
                "purchase_return": 0.0,
                "field_source": {
                    "opening_qty": "printed",
                    "purchase_qty": "printed",
                    "total_qty": "printed",
                    "sales_qty": "printed",
                    "sales_return_qty": "printed",
                    "purchase_return_qty": "printed",
                    "closing_qty": "printed",
                },
            },
        }

    def test_hiora_9_50_21_38_passes(self):
        item = self._hiora(38.0)
        self.assertEqual(expected_closing_qty(item), 38.0)
        recon = reconcile_row(item)
        self.assertTrue(recon["valid"])
        self.assertEqual(recon["expected_closing_qty"], 38.0)
        self.assertEqual(recon["base_source"], "total_qty")

    def test_hiora_closing_30_marked_failed_not_rewritten(self):
        item = self._hiora(30.0)
        result = {
            "line_items": [item],
            "totals": {"sales_value": None, "closing_value": None, "extra": {}},
        }
        out = apply_stock_reconciliation(result, request_id="hiora-bad")
        row = out["line_items"][0]
        self.assertEqual(row["closing_qty"], 30.0)
        self.assertTrue(row["extra"].get("reconciliation_failed"))
        self.assertFalse(row["extra"]["reconciliation"]["valid"])
        self.assertEqual(row["extra"]["reconciliation"]["expected_closing_qty"], 38.0)
        self.assertEqual(
            out["totals"]["extra"].get("stock_reconciliation_fail_count"), 1
        )

    def test_vision_map_hiora_row_reconciles(self):
        headers = [
            "Product",
            "Opening Qty",
            "Purchase Qty",
            "Goods Ret. Qty",
            "Total Qty",
            "Sale Qty",
            "Purc. Ret. Qty",
            "Balance Qty",
        ]
        vision = {
            "tables": [
                {
                    "table_index": 0,
                    "x_range": [0.0, 1.0],
                    "header_rows": [
                        [
                            {
                                "text": h,
                                "col_index": i,
                                "x_center": (i + 0.5) / len(headers),
                            }
                            for i, h in enumerate(headers)
                        ]
                    ],
                    "column_count": len(headers),
                    "rows": [
                        {
                            "row_index": 0,
                            "cells": [
                                "HIORA K TOOTHPASTE 100 GM",
                                "9",
                                "50",
                                "0",
                                "59",
                                "21",
                                "0",
                                "38",
                            ],
                            "is_total_row": False,
                        },
                        {
                            "row_index": 1,
                            "cells": [
                                "HIORA K TOOTHPASTE 100 GM BAD",
                                "9",
                                "50",
                                "0",
                                "59",
                                "21",
                                "0",
                                "30",
                            ],
                            "is_total_row": False,
                        },
                    ],
                    "proposed_mapping": [],
                    "unreadable_cells": [],
                }
            ]
        }
        mapped = map_vision_table(vision, request_id="hiora-map")
        out = apply_stock_reconciliation(mapped, request_id="hiora-map")
        good, bad = out["line_items"]
        self.assertEqual(good["closing_qty"], 38.0)
        self.assertFalse(good["extra"].get("reconciliation_failed"))
        self.assertTrue(good["extra"]["reconciliation"]["valid"])
        self.assertEqual(bad["closing_qty"], 30.0)
        self.assertTrue(bad["extra"].get("reconciliation_failed"))


class HioraRecoveryPathTests(unittest.TestCase):
    """Recovery: 30→38 accepted; 30→30 stays failed; no blind rewrite."""

    def setUp(self):
        self._env = {
            k: os.environ.get(k)
            for k in (
                "STOCK_RECONCILIATION_ENFORCE",
                # Keep vision result when identity fails — this suite asserts
                # reconciliation_failed on the returned row, not path fallback.
                "STOCK_VISION_TABLE_QUALITY_FALLBACK",
            )
        }
        os.environ["STOCK_RECONCILIATION_ENFORCE"] = "true"
        os.environ["STOCK_VISION_TABLE_QUALITY_FALLBACK"] = "false"

    def tearDown(self):
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _vision_table(self, closing: str) -> dict:
        headers = [
            "Product",
            "Opening Qty",
            "Purchase Qty",
            "Goods Ret. Qty",
            "Total Qty",
            "Sale Qty",
            "Purc. Ret. Qty",
            "Balance Qty",
        ]
        return {
            "tables": [
                {
                    "table_index": 0,
                    "x_range": [0.0, 1.0],
                    "header_rows": [
                        [
                            {
                                "text": h,
                                "col_index": i,
                                "x_center": (i + 0.5) / len(headers),
                            }
                            for i, h in enumerate(headers)
                        ]
                    ],
                    "column_count": len(headers),
                    "rows": [
                        {
                            "row_index": 0,
                            "cells": [
                                "HIORA K TOOTHPASTE 100 GM",
                                "9",
                                "50",
                                "0",
                                "59",
                                "21",
                                "0",
                                closing,
                            ],
                            "is_total_row": False,
                        }
                    ],
                    "proposed_mapping": [],
                    "unreadable_cells": [],
                }
            ]
        }

    def test_recovery_to_38_accepted(self):
        from services.stock_vision_table import run_vision_table_path

        first = self._vision_table("30")
        second = self._vision_table("38")
        calls = {"n": 0}

        def _extract(images, ctx=None):
            calls["n"] += 1
            return first if calls["n"] == 1 else second

        with patch(
            "services.stock_vision_table.extract_stock_table_vision", side_effect=_extract
        ), patch(
            "services.stock_vision_table._needs_vertical_split", return_value=False
        ), patch(
            "services.stock_vision_table.crop_row_bands", return_value=[b"band"]
        ), patch(
            "services.stock_row_classifier.veto_decision",
            return_value={"veto": False, "flagged_rows": []},
        ):
            out = run_vision_table_path(
                b"img",
                "image",
                {"filename": "h.jpg", "ext": ".jpg", "request_id": "hiora-rec"},
            )
        self.assertEqual(out["status"], "ok")
        self.assertEqual(calls["n"], 2)
        row = out["result"]["line_items"][0]
        self.assertEqual(row["closing_qty"], 38.0)
        self.assertFalse(row["extra"].get("reconciliation_failed"))

    def test_recovery_still_30_keeps_failed(self):
        from services.stock_vision_table import run_vision_table_path

        payload = self._vision_table("30")
        calls = {"n": 0}

        def _extract(images, ctx=None):
            calls["n"] += 1
            return payload

        with patch(
            "services.stock_vision_table.extract_stock_table_vision", side_effect=_extract
        ), patch(
            "services.stock_vision_table._needs_vertical_split", return_value=False
        ), patch(
            "services.stock_vision_table.crop_row_bands", return_value=[b"band"]
        ), patch(
            "services.stock_row_classifier.veto_decision",
            return_value={"veto": False, "flagged_rows": []},
        ):
            out = run_vision_table_path(
                b"img",
                "image",
                {"filename": "h.jpg", "ext": ".jpg", "request_id": "hiora-keep"},
            )
        self.assertEqual(out["status"], "ok")
        self.assertEqual(calls["n"], 2)
        row = out["result"]["line_items"][0]
        self.assertEqual(row["closing_qty"], 30.0)
        self.assertTrue(row["extra"].get("reconciliation_failed"))


class GeminiBudgetPriorityTests(unittest.TestCase):
    def test_recon_recovery_before_identity_reread(self):
        """Identity veto must not consume the only recovery slot before recon."""
        from services.stock_vision_table import run_vision_table_path

        headers = [
            "Product",
            "Opening Qty",
            "Purchase Qty",
            "Total Qty",
            "Sale Qty",
            "Balance Qty",
        ]

        def _table(closing: str) -> dict:
            return {
                "tables": [
                    {
                        "table_index": 0,
                        "x_range": [0.0, 1.0],
                        "header_rows": [
                            [
                                {
                                    "text": h,
                                    "col_index": i,
                                    "x_center": (i + 0.5) / len(headers),
                                }
                                for i, h in enumerate(headers)
                            ]
                        ],
                        "column_count": len(headers),
                        "rows": [
                            {
                                "row_index": 0,
                                "cells": ["PROD A", "10", "5", "15", "3", "12"],
                                "is_total_row": False,
                            },
                            {
                                "row_index": 1,
                                "cells": ["PROD B", "9", "50", "59", "21", closing],
                                "is_total_row": False,
                            },
                        ],
                        "proposed_mapping": [],
                        "unreadable_cells": [],
                    }
                ]
            }

        labels: list = []

        def _extract(images, ctx=None):
            ctx = ctx or {}
            labels.append(ctx.get("label"))
            if ctx.get("label") == "stock_vision_table_recon_recovery":
                return _table("38")
            return _table("30")

        os.environ["STOCK_IDENTITY_VETO"] = "true"
        os.environ["STOCK_IDENTITY_VETO_TYPES"] = "image"
        os.environ["STOCK_RECONCILIATION_ENFORCE"] = "true"
        try:
            with patch(
                "services.stock_vision_table.extract_stock_table_vision",
                side_effect=_extract,
            ), patch(
                "services.stock_vision_table._needs_vertical_split", return_value=True
            ), patch(
                "services.stock_vision_table.crop_row_bands", return_value=[b"band"]
            ), patch(
                "services.stock_row_classifier.veto_decision",
                return_value={"veto": True, "flagged_rows": [0, 1]},
            ):
                out = run_vision_table_path(
                    b"img",
                    "image",
                    {"filename": "tall.jpg", "ext": ".jpg", "request_id": "prio"},
                )
        finally:
            os.environ.pop("STOCK_IDENTITY_VETO", None)
            os.environ.pop("STOCK_IDENTITY_VETO_TYPES", None)

        self.assertEqual(out["status"], "ok")
        self.assertEqual(labels[0], "stock_vision_table")
        self.assertEqual(labels[1], "stock_vision_table_recon_recovery")
        self.assertNotIn("stock_vision_table_reread", labels)
        self.assertEqual(out["gemini_calls"], 2)
        extra = out["result"]["totals"]["extra"]
        self.assertEqual(extra.get("vertical_split_deferred"), "recon_priority")
        # Only failed row B recovered; A stayed valid without full-page re-OCR.
        items = {i["product_name"]: i for i in out["result"]["line_items"]}
        self.assertEqual(items["PROD B"]["closing_qty"], 38.0)
        self.assertFalse(items["PROD B"]["extra"].get("reconciliation_failed"))
        self.assertEqual(items["PROD A"]["closing_qty"], 12.0)

    def test_recon_recovery_prompt_includes_mismatch(self):
        from services.stock_vision_table import build_reconciliation_recovery_prompt

        text = build_reconciliation_recovery_prompt(
            [
                {
                    "row_index": 0,
                    "product_name": "HIORA K TOOTHPASTE 100 GM",
                    "headers": ["Opening", "Purchase", "Sale", "Balance"],
                    "opening_qty": 9,
                    "purchase_qty": 50,
                    "sales_qty": 21,
                    "extracted_closing_qty": 30,
                    "expected_closing_qty": 38,
                    "quantity_difference": -8,
                }
            ]
        )
        self.assertIn("HIORA K TOOTHPASTE 100 GM", text)
        self.assertIn("extracted Closing=30", text)
        self.assertIn("Expected closing mathematically equals 38", text)
        self.assertIn("Do not change the value merely to make the arithmetic balance", text)


class KnownGoodIdentityTests(unittest.TestCase):
    def test_simple_open_purchase_sale_closing(self):
        cases = [
            ("AACTARIL SOAP", 10, 0, 2, 8),
            ("ABANA TAB", 100, 50, 30, 120),
            ("CONFIDO TAB", 20, 10, 5, 25),
            ("BLEMINOR", 5, 5, 3, 7),
            ("LIV 52 SYRUP BIG", 12, 24, 6, 30),
        ]
        for name, op, pur, sale, cls in cases:
            item = {
                "product_name": name,
                "opening_qty": float(op),
                "receipts_qty": float(pur),
                "sales_qty": float(sale),
                "closing_qty": float(cls),
                "extra": {
                    "field_source": {
                        "opening_qty": "printed",
                        "purchase_qty": "printed",
                        "sales_qty": "printed",
                        "closing_qty": "printed",
                    }
                },
            }
            with self.subTest(product=name):
                recon = reconcile_row(item)
                self.assertTrue(recon["valid"], recon)
                self.assertEqual(recon["expected_closing_qty"], float(cls))
                self.assertEqual(
                    recon["base_source"], "opening_plus_purchase_plus_returns"
                )


class GarbageBalanceTokenTests(unittest.TestCase):
    """Vision sometimes emits Balance as p2/p3/p19 instead of digits."""

    def test_p3_balance_not_invented_when_closing_column_present(self):
        from services.stock_vision_table import map_vision_table

        vision = {
            "tables": [
                {
                    "table_index": 0,
                    "x_range": [0.0, 1.0],
                    "header_rows": [
                        [
                            {
                                "text": h,
                                "col_index": i,
                                "x_center": (i + 0.5) / 10,
                            }
                            for i, h in enumerate(
                                [
                                    "Code",
                                    "Product",
                                    "Pack",
                                    "Opening Qty",
                                    "Purchase Qty",
                                    "Goods Ret. Qty",
                                    "Total In Qty",
                                    "Sale Qty",
                                    "Purc. Ret. Qty",
                                    "Balance Qty",
                                ]
                            )
                        ]
                    ],
                    "column_count": 10,
                    "rows": [
                        {
                            "row_index": 0,
                            "cells": [
                                "15983",
                                "ARJUNA TAB",
                                "1X60TAB",
                                "18",
                                "60",
                                "1",
                                "79",
                                "26",
                                None,
                                "p3",
                            ],
                            "is_total_row": False,
                        }
                    ],
                    "proposed_mapping": [],
                    "unreadable_cells": [],
                }
            ]
        }
        mapped = map_vision_table(vision, request_id="p3-bal")
        out = apply_stock_reconciliation(mapped, request_id="p3-bal")
        row = out["line_items"][0]
        # Must NOT invent Balance from arithmetic when the column exists.
        self.assertIsNone(row.get("closing_qty"))
        self.assertEqual(row["extra"]["field_source"].get("closing_qty"), "missing")
        self.assertIn("closing_qty", row["extra"].get("unreadable_fields") or [])
        self.assertEqual(row["extra"].get("calculated_closing_qty"), 53.0)
        self.assertTrue(row["extra"].get("reconciliation_failed"))
        self.assertEqual(row["sales_qty"], 26.0)
        self.assertEqual(row["opening_qty"], 18.0)
        self.assertEqual(row["receipts_qty"], 60.0)

    def test_arjuna_printed_cells_exact_not_identity_only(self):
        """Regression: assert printed digits, not merely open+pur-sale==close."""
        from services.stock_vision_table import map_vision_table

        vision = {
            "tables": [
                {
                    "table_index": 0,
                    "x_range": [0.0, 1.0],
                    "header_rows": [
                        [
                            {
                                "text": h,
                                "col_index": i,
                                "x_center": (i + 0.5) / 10,
                            }
                            for i, h in enumerate(
                                [
                                    "Code",
                                    "Product",
                                    "Pack",
                                    "Opening Qty",
                                    "Purchase Qty",
                                    "Goods Ret. Qty",
                                    "Total In Qty",
                                    "Sale Qty",
                                    "Purc. Ret. Qty",
                                    "Balance Qty",
                                ]
                            )
                        ]
                    ],
                    "column_count": 10,
                    "rows": [
                        {
                            "row_index": 0,
                            "cells": [
                                "115983",
                                "ARJUNA TAB",
                                "1X60TAB",
                                "18",
                                "60",
                                "1",
                                "79",
                                "26",
                                None,
                                "53",
                            ],
                            "is_total_row": False,
                        },
                        {
                            "row_index": 1,
                            "cells": [
                                "112598",
                                "BONNISAN DROPS",
                                "1X30ML",
                                None,
                                "500",
                                "3",
                                "503",
                                "318",
                                None,
                                "185",
                            ],
                            "is_total_row": False,
                        },
                    ],
                    "proposed_mapping": [],
                    "unreadable_cells": [],
                }
            ]
        }
        mapped = map_vision_table(vision, request_id="printed-exact")
        out = apply_stock_reconciliation(mapped, request_id="printed-exact")
        by_name = {i["product_name"]: i for i in out["line_items"]}
        arj = by_name["ARJUNA TAB"]
        self.assertEqual(arj["opening_qty"], 18.0)
        self.assertEqual(arj["receipts_qty"], 60.0)
        self.assertEqual(arj["extra"].get("sale_return"), 1.0)
        self.assertEqual(arj["extra"].get("total_stock"), 79.0)
        self.assertEqual(arj["sales_qty"], 26.0)
        self.assertEqual(arj["closing_qty"], 53.0)
        self.assertFalse(arj["extra"].get("reconciliation_failed"))
        # Must not accept a mathematically consistent wrong pair (29/49).
        self.assertNotEqual(arj["sales_qty"], 29.0)
        self.assertNotEqual(arj["closing_qty"], 49.0)

        bon = by_name["BONNISAN DROPS"]
        self.assertEqual(bon["receipts_qty"], 500.0)
        self.assertEqual(bon["extra"].get("sale_return"), 3.0)
        self.assertEqual(bon["extra"].get("total_stock"), 503.0)
        self.assertEqual(bon["sales_qty"], 318.0)
        self.assertEqual(bon["closing_qty"], 185.0)

    def test_shift_repair_moves_total_and_sale_not_balance(self):
        from services.stock_vision_table import map_vision_table

        vision = {
            "tables": [
                {
                    "table_index": 0,
                    "x_range": [0.0, 1.0],
                    "header_rows": [
                        [
                            {
                                "text": h,
                                "col_index": i,
                                "x_center": (i + 0.5) / 10,
                            }
                            for i, h in enumerate(
                                [
                                    "Code",
                                    "Product",
                                    "Pack",
                                    "Opening Qty",
                                    "Purchase Qty",
                                    "Goods Ret. Qty",
                                    "Total In Qty",
                                    "Sale Qty",
                                    "Purc. Ret. Qty",
                                    "Balance Qty",
                                ]
                            )
                        ]
                    ],
                    "column_count": 10,
                    "rows": [
                        {
                            "row_index": 0,
                            "cells": [
                                "15983",
                                "ARJUNA TAB",
                                "1X60TAB",
                                "18",
                                "po",
                                "79",
                                "26",
                                None,
                                None,
                                "p3",
                            ],
                            "is_total_row": False,
                        }
                    ],
                    "proposed_mapping": [],
                    "unreadable_cells": [],
                }
            ]
        }
        mapped = map_vision_table(vision, request_id="shift")
        row = mapped["line_items"][0]
        self.assertEqual(row["extra"].get("total_stock"), 79.0)
        self.assertEqual(row["sales_qty"], 26.0)
        self.assertIsNone(row["extra"].get("sale_return"))
        self.assertIsNone(row.get("closing_qty"))
        self.assertTrue(row["extra"].get("shift_repair"))

    def test_negative_derived_closing_marked_failed(self):
        item = {
            "product_name": "LUKOL SYP",
            "opening_qty": 0.0,
            "receipts_qty": 20.0,
            "sales_qty": 22.0,
            "closing_qty": -2.0,
            "extra": {
                "total_stock": 20.0,
                "field_source": {
                    "opening_qty": "printed",
                    "purchase_qty": "printed",
                    "total_qty": "printed",
                    "sales_qty": "printed",
                    "closing_qty": "derived",
                },
            },
        }
        out = apply_stock_reconciliation(
            {
                "line_items": [item],
                "totals": {"sales_value": None, "closing_value": None, "extra": {}},
            },
            request_id="neg-close",
        )
        self.assertTrue(out["line_items"][0]["extra"].get("reconciliation_failed"))


if __name__ == "__main__":
    unittest.main()
