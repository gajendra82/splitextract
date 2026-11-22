"""ORIC RATE|OPENING grid: insert null when OPENING QTY was dropped."""

from __future__ import annotations

import unittest

from services.stock_vision_table import (
    _align_oric_qv_blank_opening_qty,
    _align_oric_qv_total_dup_opening_value,
    _fill_zero_fields_from_reread,
)


def _oric_rate_columns():
    headers = [
        ("product_name", "ITEM DESCRIPTION"),
        ("rate", "RATE"),
        ("opening_qty", "QTY."),
        ("opening_value", "VALUE"),
        ("purchase_qty", "QTY."),
        ("purchase_value", "VALUE"),
        ("sales_qty", "QTY."),
        ("sales_value", "VALUE"),
        ("closing_qty", "QTY."),
        ("closing_value", "VALUE"),
    ]
    return [
        {
            "col_index": i,
            "canonical": canon,
            "header_text": text,
            "source": "resolver",
        }
        for i, (canon, text) in enumerate(headers)
    ]


class OricBlankOpeningQtyAlignTests(unittest.TestCase):
    def test_inserts_null_when_opening_value_parked_in_qty(self):
        # BONNISAN-style: RATE ok, OPENING QTY dropped, VALUE left-shifted.
        cells = [
            "BONNISAN SYP.",
            "101.31",
            "2127.51",
            "28",
            "3199.84",
            "15",
            "1714.20",
            "34",
            "3444.54",
            "6",
        ]
        out, width = _align_oric_qv_blank_opening_qty(
            cells, _oric_rate_columns(), len(cells)
        )
        self.assertIsNone(out[2])
        self.assertEqual(str(out[3]), "2127.51")
        self.assertEqual(str(out[4]), "28")
        self.assertEqual(str(out[5]), "3199.84")
        self.assertEqual(str(out[6]), "15")
        self.assertEqual(str(out[7]), "1714.20")
        self.assertEqual(str(out[8]), "34")
        self.assertEqual(str(out[9]), "3444.54")
        self.assertGreaterEqual(width, 10)

    def test_moves_value_when_ov_slot_is_zero_blank_receipt(self):
        # HIMPLASIA/HIORA-style: VALUE in QTY, VALUE slot 0.00, blank RECEIPT
        # collapsed to a leading 0, then ISSUE|CLOSING (cv ≈ cq × rate).
        cells = [
            "HIMPLASIA TAB",
            "165.48",
            "10590.72",
            "0.00",
            "1",
            "186.67",
            "63",
            "10425.24",
            "63",
            "0.00",
        ]
        out, _width = _align_oric_qv_blank_opening_qty(
            cells, _oric_rate_columns(), len(cells)
        )
        self.assertIsNone(out[2])
        self.assertEqual(str(out[3]), "10590.72")
        self.assertIsNone(out[4])  # blank receipt qty
        self.assertIsNone(out[5])  # blank receipt value
        self.assertEqual(str(out[6]), "1")
        self.assertEqual(str(out[7]), "186.67")
        self.assertEqual(str(out[8]), "63")
        self.assertEqual(str(out[9]), "10425.24")

    def test_hiora_blank_receipt_issue_closing(self):
        cells = [
            "HIORA GA LIQUID",
            "69.57",
            "3339.36",
            "0.00",
            "11",
            "863.28",
            "37",
            "2574.09",
            "37",
            "0.00",
        ]
        out, _width = _align_oric_qv_blank_opening_qty(
            cells, _oric_rate_columns(), len(cells)
        )
        self.assertIsNone(out[2])
        self.assertEqual(str(out[3]), "3339.36")
        self.assertEqual(str(out[6]), "11")
        self.assertEqual(str(out[7]), "863.28")
        self.assertEqual(str(out[8]), "37")
        self.assertEqual(str(out[9]), "2574.09")

    def test_keeps_row_when_opening_qty_is_whole(self):
        cells = [
            "BONNISAN SYP.",
            "101.31",
            "21",
            "2127.51",
            "28",
            "3199.84",
            "15",
            "1714.20",
            "34",
            "3444.54",
        ]
        out, _width = _align_oric_qv_blank_opening_qty(
            cells, _oric_rate_columns(), len(cells)
        )
        self.assertEqual(out, cells)

    def test_total_drops_duplicate_leading_opening_value(self):
        cells = [
            "TOTAL",
            None,
            None,
            "310983.36",
            "2088",
            "310983.36",
            "112",
            "12778.08",
            "603",
            "101155.78",
            "1597",
            "23582.07",
        ]
        out, _width = _align_oric_qv_total_dup_opening_value(
            cells, _oric_rate_columns(), len(cells)
        )
        self.assertEqual(str(out[2]), "2088")
        self.assertEqual(str(out[3]), "310983.36")
        self.assertEqual(str(out[4]), "112")
        self.assertEqual(str(out[5]), "12778.08")
        self.assertEqual(str(out[6]), "603")
        self.assertEqual(str(out[7]), "101155.78")
        self.assertEqual(str(out[8]), "1597")
        self.assertEqual(str(out[9]), "23582.07")

    def test_reread_does_not_fill_opening_from_degraded_crop(self):
        # First pass: blank oq after VALUE-in-QTY realign; ov+sv printed.
        first = {
            "product_name": "HIMPLASIA TAB",
            "opening_qty": 0.0,
            "receipts_qty": 1.0,
            "sales_qty": 63.0,
            "closing_qty": 63.0,
            "sales_value": 10425.24,
            "opening_value": 10590.72,
            "closing_value": 0.0,
            "extra": {
                "opening_value": 10590.72,
                "field_source": {
                    "opening_qty": "missing",
                    "opening_value": "printed",
                    "sales_qty": "printed",
                    "sales_value": "printed",
                    "closing_qty": "printed",
                },
                "vision_column_headers": [
                    "ITEM",
                    "RATE",
                    "OPENING QTY",
                    "OPENING VALUE",
                    "RECEIPT QTY",
                    "RECEIPT VALUE",
                    "ISSUE QTY",
                    "ISSUE VALUE",
                    "CLOSING QTY",
                    "CLOSING VALUE",
                ],
            },
        }
        # Degraded reread parks receipt digit in opening and drops values.
        reread = {
            "product_name": "HIMPLASIA TAB",
            "opening_qty": 1.0,
            "receipts_qty": 0.0,
            "sales_qty": 63.0,
            "closing_qty": 63.0,
            "sales_value": 0.0,
            "opening_value": 0.0,
            "closing_value": 0.0,
            "extra": {
                "field_source": {
                    "opening_qty": "printed",
                    "opening_value": "printed",
                    "sales_qty": "printed",
                    "sales_value": "printed",
                },
            },
        }
        changed = _fill_zero_fields_from_reread(first, reread)
        self.assertFalse(changed)
        self.assertEqual(float(first["opening_qty"]), 0.0)


if __name__ == "__main__":
    unittest.main()
