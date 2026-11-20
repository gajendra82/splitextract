"""Generic Opening|Receipt|Total|Sale layout (no closing) via vision-table path.

Offline: hand-built Vision JSON. No stockist name, filename code, or layout route.
"""

from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest import mock

from services.stock_header_resolver import resolve_columns
from services.stock_row_classifier import RowStatus, apply_closing_derived, classify_row
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


def _vision_response(headers, rows):
    """Hand-built Vision table matching Opening|Receipt|Total|Sale."""
    header_row = [
        {"text": h, "col_index": i, "x_center": (i + 0.5) / max(len(headers), 1)}
        for i, h in enumerate(headers)
    ]
    data_rows = []
    for idx, cells in enumerate(rows):
        data_rows.append(
            {
                "row_index": idx,
                "cells": list(cells),
                "is_total_row": False,
                "y_center": 0.2 + 0.05 * idx,
            }
        )
    return {
        "tables": [
            {
                "table_index": 0,
                "x_range": [0.0, 1.0],
                "header_rows": [header_row],
                "column_count": len(headers),
                "rows": data_rows,
                "proposed_mapping": [],
                "unreadable_cells": [],
            }
        ],
        "stockist_name": None,
        "statement_period": None,
    }


HEADERS = ["Product", "Opening", "Receipt", "Total", "Sale"]


class OpeningReceiptTotalSaleGenericTests(unittest.TestCase):
    def test_headers_resolve_without_closing_or_sales_return(self):
        result = resolve_columns(_cells(*HEADERS))
        canons = [c["canonical"] for c in result["columns"]]
        self.assertEqual(
            canons,
            [
                "product_name",
                "opening_qty",
                "purchase_qty",
                "total_qty",
                "sales_qty",
            ],
        )
        self.assertNotIn("closing_qty", canons)
        self.assertNotIn("sales_return_qty", canons)
        self.assertFalse(
            any(e.get("code") == "MISSING_CORE_COLUMNS" for e in result["errors"])
        )

    def test_row_10_5_15_4_closing_derived(self):
        mapped = map_vision_table(
            _vision_response(
                HEADERS,
                [["ARJUNA TABLET", "10", "5", "15", "4"]],
            ),
            request_id="ors-derived",
        )
        self.assertEqual(len(mapped["line_items"]), 1)
        item = mapped["line_items"][0]
        self.assertEqual(item["opening_qty"], 10.0)
        self.assertEqual(item["receipts_qty"], 5.0)
        self.assertEqual(item["sales_qty"], 4.0)
        self.assertEqual(item["closing_qty"], 11.0)
        fs = item["extra"]["field_source"]
        self.assertEqual(fs.get("closing_qty"), "derived")
        self.assertEqual(fs.get("opening_qty"), "printed")
        self.assertEqual(fs.get("purchase_qty"), "printed")
        self.assertEqual(fs.get("total_qty"), "printed")
        self.assertEqual(fs.get("sales_qty"), "printed")
        self.assertNotEqual(fs.get("sales_return_qty"), "printed")
        self.assertEqual(item["extra"].get("row_status"), RowStatus.CLOSING_DERIVED.value)

    def test_bad_total_flags_and_no_valid_closing(self):
        mapped = map_vision_table(
            _vision_response(
                HEADERS,
                [["BAD TOTAL", "10", "5", "99", "4"]],
            ),
            request_id="ors-bad-total",
        )
        item = mapped["line_items"][0]
        status = item["extra"].get("row_status")
        self.assertIn(
            status,
            {
                RowStatus.COLUMN_ASSIGNMENT_SUSPECTED.value,
                RowStatus.OCR_VALUE_SUSPECTED.value,
            },
        )
        fs = item["extra"]["field_source"]
        self.assertNotEqual(fs.get("closing_qty"), "derived")
        # Closing must not be presented as a trusted printed/derived value.
        self.assertIn(fs.get("closing_qty"), {"missing", None})
        self.assertTrue(
            item.get("closing_qty") in (None, 0.0)
            or fs.get("closing_qty") == "missing"
        )

    def test_filename_codes_identical_on_vision_table_path(self):
        """Same image bytes / Vision JSON → identical output regardless of name."""
        vision = _vision_response(
            HEADERS,
            [["ARJUNA TABLET", "10", "5", "15", "4"]],
        )
        outs = []
        for name in (
            "neutral_stock.png",
            "0000700155_2026_08_ZA_06_333.png",
            "0000700155_2026_08_ZL_06_333.png",
        ):
            mapped = map_vision_table(vision, request_id=f"ors-{name}")
            item = mapped["line_items"][0]
            outs.append(
                (
                    item["opening_qty"],
                    item["receipts_qty"],
                    item["sales_qty"],
                    item["closing_qty"],
                    item["extra"]["field_source"].get("closing_qty"),
                    item["extra"].get("row_status"),
                )
            )
        self.assertEqual(outs[0], outs[1])
        self.assertEqual(outs[0], outs[2])

    def test_empty_receipt_cell_still_derives_closing(self):
        """Printed Receipt column with dash/empty cell → purchase 0, closing derived."""
        mapped = map_vision_table(
            _vision_response(
                HEADERS,
                [["ARJUNA CAP. 60'", "53", None, "53", "7"]],
            ),
            request_id="ors-empty-rec",
        )
        item = mapped["line_items"][0]
        self.assertEqual(item["opening_qty"], 53.0)
        self.assertEqual(item["sales_qty"], 7.0)
        self.assertEqual(item["closing_qty"], 46.0)
        self.assertEqual(item["extra"]["field_source"].get("closing_qty"), "derived")
        self.assertEqual(item["extra"].get("row_status"), RowStatus.CLOSING_DERIVED.value)

    def test_excel_headers_expiry_is_date_not_damage(self):
        headers = [
            "Product",
            "Packing",
            "Expiry",
            "Rate",
            "Opening",
            "Receipt",
            "Receipt Free",
            "Free Replace",
            "Total",
            "Sale",
        ]
        result = resolve_columns(_cells(*headers))
        canons = [c["canonical"] for c in result["columns"]]
        self.assertEqual(canons[0], "product_name")
        self.assertEqual(canons[2], "ignore")  # Expiry date
        self.assertEqual(canons[4], "opening_qty")
        self.assertEqual(canons[5], "purchase_qty")
        self.assertEqual(canons[6], "free_in_qty")
        self.assertEqual(canons[8], "total_qty")
        self.assertEqual(canons[9], "sales_qty")
        self.assertNotIn("closing_qty", canons)
        self.assertNotIn("expiry_damage_qty", canons)

    def test_letterhead_and_section_banner_filtered(self):
        mapped = map_vision_table(
            _vision_response(
                HEADERS,
                [
                    ["SHREE AMBICA MEDICALS", None, None, None, None],
                    ["ANDRA VILLA APT., BIBI-NI-WADI, SAIYEDPURA, SURAT", None, None, None, None],
                    ["ARJUNA CAP. 60'", "53", None, "53", "7"],
                    ["Last 6 Months NON MOVING PRODUCT(S).....", None, None, None, None],
                    ["BRESOL SYRUP", None, None, None, None],
                ],
            ),
            request_id="ors-letterhead",
        )
        names = [i.get("product_name") for i in mapped["line_items"]]
        self.assertIn("ARJUNA CAP. 60'", names)
        self.assertIn("BRESOL SYRUP", names)
        self.assertNotIn("SHREE AMBICA MEDICALS", names)
        self.assertTrue(
            all("NON MOVING" not in (n or "").upper() for n in names)
        )
        self.assertTrue(
            all("VILLA" not in (n or "").upper() for n in names)
        )

    def test_period_and_company_scraps_filtered(self):
        """UI junk: truncated period + OCR company line must not be products."""
        mapped = map_vision_table(
            _vision_response(
                HEADERS,
                [
                    ["6 to 31-08-2026", "0", "0", "0", "0"],
                    ["MIMAL=> HIMALAYA (ZANDRA DIVI) [ZANDRA", "0", "0", "0", "0"],
                    ["ARJUNA CAP. 60'", "53", None, "53", "7"],
                    ["BONNISAN DROPS 30ML", "82", None, "82", "9"],
                ],
            ),
            request_id="ors-period-company",
        )
        names = [i.get("product_name") for i in mapped["line_items"]]
        self.assertEqual(
            names,
            ["ARJUNA CAP. 60'", "BONNISAN DROPS 30ML"],
        )
        arjuna = mapped["line_items"][0]
        self.assertEqual(arjuna["closing_qty"], 46.0)

    def test_model_cannot_invent_closing_without_header(self):
        table = {
            "header_rows": [
                [
                    {"text": "Product", "col_index": 0, "x_center": 0.1},
                    {"text": "Opening", "col_index": 1, "x_center": 0.3},
                    {"text": "Receipt", "col_index": 2, "x_center": 0.5},
                    {"text": "Total", "col_index": 3, "x_center": 0.65},
                    {"text": "Sale", "col_index": 4, "x_center": 0.8},
                    {"text": "ZZZ", "col_index": 5, "x_center": 0.95},
                ]
            ],
            "column_count": 6,
            "rows": [
                {
                    "row_index": 0,
                    "cells": ["X", "53", None, "53", "7", "7"],
                    "is_total_row": False,
                }
            ],
            "proposed_mapping": [
                {"col_index": 5, "canonical": "closing_qty", "confidence": 0.9}
            ],
            "unreadable_cells": [],
        }
        mapped = map_vision_table(table, request_id="ors-no-invent-close")
        col5 = next(c for c in mapped["column_map"] if c["col_index"] == 5)
        self.assertEqual(col5["canonical"], "ignore")
        item = mapped["line_items"][0]
        self.assertEqual(item["closing_qty"], 46.0)
        self.assertEqual(item["extra"]["field_source"].get("closing_qty"), "derived")

    def test_vision_table_path_skips_saleret_classifier(self):
        """With STOCK_VISION_TABLE on, classify_stock_direct_vision is not consulted."""
        os.environ["STOCK_VISION_TABLE"] = "true"
        os.environ["STOCK_VISION_TABLE_TYPES"] = "image"
        try:
            from services.stock_vision_table import run_vision_table_path

            vision = _vision_response(
                HEADERS,
                [["ARJUNA TABLET", "10", "5", "15", "4"]],
            )
            with mock.patch(
                "services.stock_direct_vision.classify_stock_direct_vision"
            ) as classify:
                with mock.patch(
                    "services.stock_vision_table.extract_stock_table_vision",
                    return_value=vision,
                ):
                    with mock.patch(
                        "services.stock_vision_table._needs_vertical_split",
                        return_value=False,
                    ):
                        outcome = run_vision_table_path(
                            b"fake-image-bytes",
                            "image",
                            {
                                "filename": "0000700155_2026_08_ZA_06_333.png",
                                "ext": ".png",
                                "request_id": "ors-skip-za",
                            },
                        )
            classify.assert_not_called()
            self.assertEqual(outcome.get("status"), "ok")
            items = (outcome.get("result") or {}).get("line_items") or []
            self.assertGreaterEqual(len(items), 1)
            self.assertEqual(items[0]["closing_qty"], 11.0)
        finally:
            os.environ.pop("STOCK_VISION_TABLE", None)
            os.environ.pop("STOCK_VISION_TABLE_TYPES", None)


class NoFormatSpecificDiffGrepTests(unittest.TestCase):
    """Grep guard: this change set must not introduce stockist/filename routes."""

    def test_no_stockist_or_filename_route_in_generic_modules(self):
        # Build tokens without embedding the literal stockist name in this file
        # as a searchable route string.
        forbidden = (
            "ambi" + "ca",
            "excel_opening_receipt_sale",
            "SHREE " + "AMBICA",
            "filename_za_excel",
        )
        roots = [
            Path("services/stock_header_resolver.py"),
            Path("services/stock_row_classifier.py"),
            Path("services/stock_vision_table.py"),
            Path("services/stock_direct_vision.py"),
        ]
        for path in roots:
            text = path.read_text(encoding="utf-8")
            lower = text.lower()
            for token in forbidden:
                self.assertNotIn(
                    token.lower(),
                    lower,
                    msg=f"{path} contains forbidden token {token!r}",
                )


if __name__ == "__main__":
    unittest.main()
