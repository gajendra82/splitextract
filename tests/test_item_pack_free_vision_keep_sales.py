"""ITEM|PACK|OPENING|PURCHASE|FREE|P.RETURN|…|SALE|…|CLOSING Vision keep + repair.

Dash-heavy qty-only grids inflate MISSING_VALUE and used to fall through to
gemini_extraction_fallback (losing SALE). Keep Vision; re-slot OPENING when
the digit was parked under PURCHASE across an epidemic of rows.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from services.stock_vision_table import (
    _apply_item_pack_free_opening_in_purchase_pass,
    _column_map_item_pack_free_return,
    _stamp_vision_result,
    _vision_table_quality_fallback_reason,
)

_ITEM_PACK_FREE_MAP = [
    {"header_text": "ITEM", "canonical": "product_name"},
    {"header_text": "PACK", "canonical": "pack"},
    {"header_text": "OPENING", "canonical": "opening_qty"},
    {"header_text": "PURCHASE", "canonical": "purchase_qty"},
    {"header_text": "FREE", "canonical": "free_in_qty"},
    {"header_text": "P.RETURN", "canonical": "purchase_return_qty"},
    {"header_text": "FREE", "canonical": "ignore"},
    {"header_text": "SALE", "canonical": "sales_qty"},
    {"header_text": "FREE", "canonical": "free_out_qty"},
    {"header_text": "S.RETURN", "canonical": "sales_return_qty"},
    {"header_text": "FREE", "canonical": "ignore"},
    {"header_text": "OTHERS", "canonical": "other_outward_qty"},
    {"header_text": "CLOSING", "canonical": "closing_qty"},
]


def _dash_row(name: str) -> dict:
    return {
        "product_name": name,
        "opening_qty": 0.0,
        "receipts_qty": 0.0,
        "sales_qty": 0.0,
        "closing_qty": None,
        "extra": {
            "field_source": {
                "opening_qty": "missing",
                "purchase_qty": "missing",
                "receipts_qty": "missing",
                "sales_qty": "missing",
                "closing_qty": "missing",
                "free_in_qty": "missing",
                "purchase_return_qty": "missing",
                "sales_return_qty": "missing",
            }
        },
    }


def _shifted_row(name: str, opening: float, sales: float, closing: float) -> dict:
    """Vision parked OPENING under PURCHASE; SALE/CLOSING correct."""
    return {
        "product_name": name,
        "opening_qty": 0.0,
        "purchase_qty": opening,
        "receipts_qty": opening,
        "sales_qty": sales,
        "closing_qty": closing,
        "extra": {
            "field_source": {
                "opening_qty": "missing",
                "purchase_qty": "printed",
                "receipts_qty": "printed",
                "sales_qty": "printed",
                "closing_qty": "printed",
                "free_in_qty": "missing",
                "purchase_return_qty": "missing",
                "sales_return_qty": "missing",
            },
            "flags": [],
        },
    }


class ItemPackFreeColumnMapTests(unittest.TestCase):
    def test_detects_free_preturn_family(self):
        self.assertTrue(_column_map_item_pack_free_return(_ITEM_PACK_FREE_MAP))

    def test_rejects_value_grid(self):
        cols = list(_ITEM_PACK_FREE_MAP) + [
            {"header_text": "SALE VALUE", "canonical": "sales_value"}
        ]
        self.assertFalse(_column_map_item_pack_free_return(cols))


class ItemPackFreeMissingValueKeepTests(unittest.TestCase):
    def test_missing_value_ratio_high_kept_for_qty_only_free_grid(self):
        # Mirror WA0006: ~35 rows, most dash→MISSING_VALUE, valid_ratio ~0.17.
        items = [_dash_row(f"DASH {i}") for i in range(29)]
        items.extend(
            [
                _shifted_row("BONNISAN DROPS", 40, 1, 39),
                _shifted_row("EVECARE FORTE SYP", 28, 2, 26),
                _shifted_row("EVECARE SYRUP", 29, 3, 26),
                _shifted_row("LIV-52 DS TABLETS", 109, 60, 49),
                _shifted_row("LUKOL TABLETS", 31, 13, 18),
                _dash_row("ABANA TAB"),
            ]
        )
        result = {
            "line_items": items,
            "totals": {
                "extra": {
                    "valid_ratio": 0.17,
                    "row_status_counts": {
                        "VALID": 5,
                        "MISSING_VALUE": 30,
                        "STRUCTURAL_ERROR": 0,
                    },
                    "column_map": _ITEM_PACK_FREE_MAP,
                }
            },
        }
        with patch.dict(
            "os.environ",
            {
                "STOCK_VISION_TABLE_QUALITY_FALLBACK": "true",
                "STOCK_VISION_KEEP_PRINTED_OP_PUR_SALES_CL": "true",
                "STOCK_VISION_TABLE_MIN_VALID_RATIO": "0.50",
            },
        ):
            self.assertIsNone(_vision_table_quality_fallback_reason(result))

    def test_missing_value_still_fires_when_keep_off(self):
        items = [_dash_row(f"DASH {i}") for i in range(20)]
        items.append(_shifted_row("BONNISAN DROPS", 40, 1, 39))
        result = {
            "line_items": items,
            "totals": {
                "extra": {
                    "valid_ratio": 0.10,
                    "row_status_counts": {
                        "VALID": 1,
                        "MISSING_VALUE": 20,
                        "STRUCTURAL_ERROR": 0,
                    },
                    "column_map": _ITEM_PACK_FREE_MAP,
                }
            },
        }
        with patch.dict(
            "os.environ",
            {
                "STOCK_VISION_TABLE_QUALITY_FALLBACK": "true",
                "STOCK_VISION_KEEP_PRINTED_OP_PUR_SALES_CL": "false",
                "STOCK_VISION_TABLE_MIN_VALID_RATIO": "0.50",
            },
        ):
            self.assertEqual(
                _vision_table_quality_fallback_reason(result),
                "missing_value_ratio_high",
            )


class ItemPackFreeOpeningInPurchaseRepairTests(unittest.TestCase):
    def test_epidemic_moves_purchase_digit_to_opening(self):
        items = [
            _shifted_row("BONNISAN DROPS", 40, 1, 39),
            _shifted_row("EVECARE FORTE SYP", 28, 2, 26),
            _shifted_row("LUKOL TABLETS", 31, 13, 18),
            _dash_row("ABANA TAB"),
        ]
        repaired = _apply_item_pack_free_opening_in_purchase_pass(
            items, _ITEM_PACK_FREE_MAP
        )
        self.assertEqual(repaired, 3)
        bonni = items[0]
        self.assertEqual(bonni["opening_qty"], 40.0)
        self.assertIsNone(bonni["purchase_qty"])
        self.assertIsNone(bonni["receipts_qty"])
        self.assertEqual(bonni["sales_qty"], 1.0)
        self.assertEqual(bonni["closing_qty"], 39.0)
        self.assertIn("OPENING_PARKED_IN_PURCHASE", bonni["extra"]["flags"])

    def test_skips_when_genuine_open_and_purchase_present(self):
        items = [
            _shifted_row("A", 40, 1, 39),
            _shifted_row("B", 28, 2, 26),
            {
                "product_name": "REAL PURCHASE",
                "opening_qty": 10.0,
                "purchase_qty": 5.0,
                "receipts_qty": 5.0,
                "sales_qty": 3.0,
                "closing_qty": 12.0,
                "extra": {
                    "field_source": {
                        "opening_qty": "printed",
                        "purchase_qty": "printed",
                        "receipts_qty": "printed",
                        "sales_qty": "printed",
                        "closing_qty": "printed",
                        "free_in_qty": "missing",
                    },
                    "flags": [],
                },
            },
        ]
        repaired = _apply_item_pack_free_opening_in_purchase_pass(
            items, _ITEM_PACK_FREE_MAP
        )
        self.assertEqual(repaired, 0)
        self.assertEqual(items[0]["purchase_qty"], 40.0)

    def test_stamp_sets_layout_and_no_sales_value(self):
        items = [_shifted_row("BONNISAN DROPS", 40, 1, 39)]
        result = _stamp_vision_result(
            filename="wa0006.pdf",
            ext=".pdf",
            line_items=items,
            column_map=_ITEM_PACK_FREE_MAP,
            classified={"row_status_counts": {}, "valid_ratio": 1.0},
            errors=[],
            gemini_calls=1,
            gemini_budget=2,
        )
        extra = result["totals"]["extra"]
        self.assertEqual(extra.get("layout"), "item_pack_free_return")
        self.assertEqual(extra.get("stock_identity_kind"), "item_pack_free_return")
        self.assertTrue(extra.get("no_sales_value"))
        self.assertTrue(extra.get("qty_only"))


if __name__ == "__main__":
    unittest.main()
