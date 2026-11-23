"""LstSL money footers must leave data_rows and land in footer totals.

Layout family: Product|Pack|LstSL|Open|Recd|Sales|Close (any stockist).
Synthetic tokens mirror SANJIVAN MEDICO-style printed footer labels.
"""

from __future__ import annotations

import os
import unittest
from unittest import mock

from services.stock_native_resolver import (
    _is_labeled_value_footer_name,
    _split_labeled_value_footers,
)


def _tok(text: str) -> dict:
    return {"text": text, "x0": 0.0, "x1": 10.0, "top": 0.0, "bottom": 10.0}


class TestLstslLabeledValueFooters(unittest.TestCase):
    def test_footer_labels_are_recognized(self):
        with mock.patch.dict(
            os.environ, {"STOCK_PDF_WORDS_LSTSL_VALUE_FOOTERS": "true"}
        ):
            for label in (
                "Last Month Sales",
                "Open. Value",
                "Opening Value",
                "Receipt Value",
                "Sales Value",
                "Closing Value",
            ):
                self.assertTrue(
                    _is_labeled_value_footer_name(label), label
                )
            self.assertFalse(
                _is_labeled_value_footer_name("BLEMINOR CREAM")
            )

    def test_split_peels_lstsl_footers_into_totals(self):
        pages = [
            {
                "data_rows": [
                    [
                        _tok("BLEMINOR"),
                        _tok("CREAM"),
                        _tok("1"),
                        _tok("50"),
                        _tok("7"),
                        _tok("44"),
                    ],
                    [_tok("Last Month Sales"), _tok("2,19,224.60")],
                    [_tok("Open. Value"), _tok("2,11,888.53")],
                    [_tok("Receipt Value"), _tok("1,84,261.24")],
                    [_tok("Sales Value"), _tok("2,06,038.56")],
                    [_tok("Closing Value"), _tok("1,89,585.72")],
                ]
            }
        ]
        with mock.patch.dict(
            os.environ, {"STOCK_PDF_WORDS_LSTSL_VALUE_FOOTERS": "true"}
        ):
            cleaned, totals = _split_labeled_value_footers(pages)
        self.assertEqual(len(cleaned[0]["data_rows"]), 1)
        joined = " ".join(
            t["text"] for t in cleaned[0]["data_rows"][0]
        )
        self.assertIn("BLEMINOR", joined)
        self.assertEqual(totals["last_month_sales_value"], 219224.60)
        self.assertEqual(totals["opening_value"], 211888.53)
        self.assertEqual(totals["receipts_value"], 184261.24)
        self.assertEqual(totals["sales_value"], 206038.56)
        self.assertEqual(totals["closing_value"], 189585.72)

    def test_kill_switch_keeps_lstsl_labels_in_rows(self):
        pages = [
            {
                "data_rows": [
                    [_tok("Last Month Sales"), _tok("2,19,224.60")],
                    [_tok("Closing Value"), _tok("1,89,585.72")],
                ]
            }
        ]
        with mock.patch.dict(
            os.environ, {"STOCK_PDF_WORDS_LSTSL_VALUE_FOOTERS": "false"}
        ):
            cleaned, totals = _split_labeled_value_footers(pages)
        self.assertEqual(len(cleaned[0]["data_rows"]), 2)
        self.assertEqual(totals, {})


if __name__ == "__main__":
    unittest.main()
