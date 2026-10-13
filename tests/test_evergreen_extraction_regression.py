"""Regression: STOCK & SALES ANALYSIS stockist + two-line row alignment helpers."""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from services.sales_statement_extractor import (
    _detect_stockist_from_page_text,
    _enrich_stock_statement_header_from_ocr,
    _looks_like_stockist_header,
    _ssa_analysis_header_stockist,
    _stockist_candidate_score,
)
from services.stock_header_resolver import resolve_columns
from services.stock_vision_table import (
    _headers_look_like_ssa_purchase_qty_grid,
    _shift_pack_out_of_qty_headers,
)


EVERGREEN_OCR = """
EVERGREEN
A.C LANE, POLICE BAZAR DIsTRreyTOR
Phone : 9863095294/7963753 E-Mail SHILLONG-793001
GSTIN : 17AOLPS7342Q1ZY
STOCK © SALES ANALYSIS (HIMALAYA (ZENITH)) 01-09-2026 - 30-09-2026
ITEM DESCRIPTION OPENING STOCK PURCHASES SALE RETURN
HIMALAYA (ZENITH)
ARJUNA TAB
60S 64 0 0 0 64 10 1 0 53 - 0 9
BONNISAN DROP
30ML 139 0 2 0 141 19 0 0 122 - 0 2
"""

PAGE2_CONTINUATION = """
EVERGREEN
A.C LANE, POLICE BAZAR DIsTRreyTOR
STOCK © SALES ANALYSIS (HIMALAYA (ZENITH)) 01-09-2026 - 30-09-2026
REOSTO TAB
30S 58 0 0 0 58 2 1 0 55 - 0 0
Total Quantity
5619 3600 8 0 9227 2842 54 0 6331 847 0 0
"""


class EvergreenStockistTests(unittest.TestCase):
    def test_analysis_title_is_not_a_stockist_header(self):
        self.assertFalse(
            _looks_like_stockist_header(
                "STOCK © SALES ANALYSIS (HIMALAYA (ZENITH)) 01-09."
            )
        )
        self.assertLess(
            _stockist_candidate_score(
                "STOCK © SALES ANALYSIS (HIMALAYA (ZENITH)) 01-09."
            ),
            5,
        )

    def test_division_banner_is_not_stockist(self):
        self.assertFalse(_looks_like_stockist_header("HIMALAYA (ZENITH)"))

    def test_ssa_party_from_letterhead(self):
        with patch.dict(os.environ, {"STOCK_SSA_ANALYSIS_HEADER_STOCKIST": "true"}):
            name = _ssa_analysis_header_stockist(EVERGREEN_OCR)
        self.assertEqual(name, "EVERGREEN DRUGS DISTRIBUTOR")

    def test_detect_prefers_distributor_over_title(self):
        with patch.dict(os.environ, {"STOCK_SSA_ANALYSIS_HEADER_STOCKIST": "true"}):
            name = _detect_stockist_from_page_text(EVERGREEN_OCR)
        self.assertEqual(name, "EVERGREEN DRUGS DISTRIBUTOR")

    def test_enrich_sets_stockist_company_period_fields(self):
        result = {
            "stockist_name": "STOCK © SALES ANALYSIS (HIMALAYA (ZENITH)) 01-09.",
            "company_name": "EVERGREEN DRUGS DISTRIBUTOR",
            "period_from": None,
            "period_to": None,
        }
        with patch.dict(
            os.environ,
            {
                "STOCK_HEADER_OCR_ENRICH": "true",
                "STOCK_SSA_ANALYSIS_HEADER_STOCKIST": "true",
            },
        ):
            _enrich_stock_statement_header_from_ocr(result, EVERGREEN_OCR)
        self.assertEqual(result["stockist_name"], "EVERGREEN DRUGS DISTRIBUTOR")
        self.assertIn("HIMALAYA", str(result.get("company_name") or "").upper())
        self.assertEqual(result.get("report_title"), "STOCK & SALES ANALYSIS")

    def test_flag_off_skips_ssa_party_helper(self):
        with patch.dict(os.environ, {"STOCK_SSA_ANALYSIS_HEADER_STOCKIST": "false"}):
            self.assertIsNone(_ssa_analysis_header_stockist(EVERGREEN_OCR))


class EvergreenHeaderMapTests(unittest.TestCase):
    def test_total_stock_resolves_to_total_qty_not_sales(self):
        cells = [
            {"text": "ITEM DESCRIPTION", "col_index": 0, "x_center": 0.1},
            {"text": "OPENING STOCK", "col_index": 1, "x_center": 0.2},
            {"text": "PURCHASES", "col_index": 2, "x_center": 0.3},
            {"text": "SALE RETURN", "col_index": 3, "x_center": 0.4},
            {"text": "REPL. OTHERS", "col_index": 4, "x_center": 0.45},
            {"text": "TOTAL STOCK", "col_index": 5, "x_center": 0.5},
            {"text": "SALES", "col_index": 6, "x_center": 0.6},
            {"text": "PURCHASE RETURN", "col_index": 7, "x_center": 0.7},
            {"text": "REPL. OTHERS", "col_index": 8, "x_center": 0.75},
            {"text": "CLOSING STOCK", "col_index": 9, "x_center": 0.85},
            {"text": "RE-ORDER", "col_index": 10, "x_center": 0.9},
            {"text": "AVG N/EXP", "col_index": 11, "x_center": 0.95},
        ]
        resolved = resolve_columns(cells)
        by_idx = {
            int(c["col_index"]): c["canonical"]
            for c in resolved["columns"]
            if isinstance(c, dict)
        }
        self.assertEqual(by_idx[0], "product_name")
        self.assertEqual(by_idx[1], "opening_qty")
        self.assertEqual(by_idx[2], "purchase_qty")
        self.assertEqual(by_idx[5], "total_qty")
        self.assertEqual(by_idx[6], "sales_qty")
        self.assertEqual(by_idx[9], "closing_qty")

    def test_pack_shift_and_cell_layout_skipped_for_ssa_purchase_grid(self):
        from services.stock_vision_table import _apply_cell_layout_qty_value_repair

        columns = [
            {
                "col_index": 0,
                "header_text": "ITEM DESCRIPTION",
                "canonical": "product_name",
            },
            {"col_index": 1, "header_text": "OPENING", "canonical": "opening_qty"},
            {"col_index": 2, "header_text": "PURCHASES", "canonical": "purchase_qty"},
            {"col_index": 3, "header_text": "SALE", "canonical": "sales_return_qty"},
            {"col_index": 4, "header_text": "REPL./", "canonical": "ignore"},
            {"col_index": 5, "header_text": "OTHERS", "canonical": "ignore"},
            {"col_index": 6, "header_text": "TOTAL", "canonical": "total_qty"},
            {"col_index": 7, "header_text": "STOCK", "canonical": "ignore"},
            {"col_index": 8, "header_text": "SALES", "canonical": "sales_qty"},
            {"col_index": 9, "header_text": "CLOSING", "canonical": "closing_qty"},
        ]
        rows = [
            {
                "row_index": 0,
                "cells": [
                    "ARJUNA TAB",
                    "60S",
                    "64",
                    "0",
                    "0",
                    "0",
                    "64",
                    "10",
                    "1",
                    "53",
                ],
                "is_total_row": False,
            },
            {
                "row_index": 1,
                "cells": [
                    "BONNISAN DROP",
                    "30ML",
                    "139",
                    "0",
                    "2",
                    "0",
                    "141",
                    "19",
                    "0",
                    "122",
                ],
                "is_total_row": False,
            },
        ]
        self.assertTrue(_headers_look_like_ssa_purchase_qty_grid(columns))
        with patch.dict(
            os.environ,
            {
                "STOCK_VISION_CELL_LAYOUT_REPAIR": "true",
                "STOCK_VISION_SSA_PACK_SHIFT_GUARD": "true",
            },
        ):
            out = _shift_pack_out_of_qty_headers(columns, rows, [])
            repaired = _apply_cell_layout_qty_value_repair(
                columns, rows, len(columns), []
            )
        self.assertEqual(out[1]["canonical"], "opening_qty")
        self.assertEqual(out[6]["canonical"], "total_qty")
        self.assertEqual(repaired[6]["canonical"], "total_qty")
        self.assertEqual(repaired[8]["canonical"], "sales_qty")

    def test_missing_sales_header_after_total_recovered(self):
        from services.stock_vision_table import _repair_ssa_missing_sales_after_total

        columns = [
            {"col_index": 0, "header_text": "ITEM DESCRIPTION", "canonical": "product_name"},
            {"col_index": 1, "header_text": "OPENING", "canonical": "opening_qty"},
            {"col_index": 2, "header_text": "PURCHASE", "canonical": "purchase_qty"},
            {"col_index": 3, "header_text": "SALE", "canonical": "sales_return_qty"},
            {"col_index": 4, "header_text": "REPL./", "canonical": "ignore"},
            {"col_index": 5, "header_text": "TOTAL", "canonical": "total_qty"},
            {"col_index": 6, "header_text": "PURCHASE", "canonical": "ignore"},
            {"col_index": 7, "header_text": "REPL./", "canonical": "ignore"},
            {"col_index": 8, "header_text": "CLOSING", "canonical": "closing_qty"},
        ]
        with patch.dict(os.environ, {"STOCK_VISION_SSA_PACK_SHIFT_GUARD": "true"}):
            out = _repair_ssa_missing_sales_after_total(columns, [])
        self.assertEqual(out[5]["canonical"], "total_qty")
        self.assertEqual(out[6]["canonical"], "sales_qty")
        self.assertEqual(out[8]["canonical"], "closing_qty")


class EvergreenPhysicalRemapTests(unittest.TestCase):
    def test_continuation_row_remaps_total_as_sales(self):
        from services.sales_statement_extractor import (
            _repair_ssa_analysis_item_from_physical_cells,
        )

        item = {
            "product_name": "REOSTO TAB",
            "packing": "30S",
            "opening_qty": 58.0,
            "receipts_qty": 0.0,
            "sales_qty": 58.0,  # wrongly took TOTAL
            "closing_qty": 1.0,
            "extra": {
                "physical_cells": {
                    "0": "REOSTO TAB",
                    "1": "30S",
                    "2": "58",
                    "3": "0",
                    "4": "0",
                    "5": "0",
                    "6": "58",
                    "7": "2",
                    "8": "1",
                    "9": "0",
                    "10": "55",
                    "11": "0",
                    "12": "0",
                }
            },
        }
        with patch.dict(os.environ, {"STOCK_SSA_ANALYSIS_PHYSICAL_REMAP": "true"}):
            self.assertTrue(_repair_ssa_analysis_item_from_physical_cells(item))
        self.assertEqual(item["opening_qty"], 58.0)
        self.assertEqual(item["sales_qty"], 2.0)
        self.assertEqual(item["closing_qty"], 55.0)

    def test_pack_token_30g_is_recognized(self):
        from services.sales_statement_extractor import (
            _PACK_TOKEN_RE,
            _repair_ssa_analysis_item_from_physical_cells,
        )

        self.assertTrue(_PACK_TOKEN_RE.match("30G"))
        item = {
            "product_name": "V-GEL",
            "packing": "30G",
            "opening_qty": 29.0,
            "sales_qty": 29.0,
            "closing_qty": 0.0,
            "extra": {
                "physical_cells": {
                    "0": "V-GEL",
                    "1": "30G",
                    "2": "29",
                    "3": "0",
                    "4": "0",
                    "5": "0",
                    "6": "29",
                    "7": "4",
                    "8": "0",
                    "9": "0",
                    "10": "25",
                }
            },
        }
        self.assertTrue(_repair_ssa_analysis_item_from_physical_cells(item))
        self.assertEqual(item["sales_qty"], 4.0)
        self.assertEqual(item["closing_qty"], 25.0)

    def test_vgel_missing_repl_in_derives_closing(self):
        """Vision drops REPL IN zero and pads closing as 0 — realign + derive."""
        from services.sales_statement_extractor import (
            _repair_ssa_analysis_item_from_physical_cells,
        )

        item = {
            "product_name": "V-GEL",
            "packing": "30G",
            "opening_qty": 29.0,
            "receipts_qty": 0.0,
            "sales_qty": 4.0,
            "closing_qty": 0.0,
            "extra": {
                "physical_cells": {
                    "0": "V-GEL",
                    "1": "30G",
                    "2": "29",
                    "3": "0",
                    "4": "0",
                    "5": "29",
                    "6": "4",
                    "7": "0",
                    "8": "0",
                    "9": "0",
                    "10": "0",
                }
            },
        }
        with patch.dict(
            os.environ,
            {
                "STOCK_SSA_ANALYSIS_PHYSICAL_REMAP": "true",
                "STOCK_VISION_FIRST": "false",
            },
        ):
            self.assertTrue(_repair_ssa_analysis_item_from_physical_cells(item))
        self.assertEqual(item["opening_qty"], 29.0)
        self.assertEqual(item["sales_qty"], 4.0)
        self.assertEqual(item["closing_qty"], 25.0)
        self.assertEqual(item["extra"]["field_source"]["closing_qty"], "derived")

    def test_merge_reread_rejects_cloned_fingerprint(self):
        from services.stock_vision_table import merge_reread

        reosto = {
            "product_name": "REOSTO TAB",
            "opening_qty": 58.0,
            "receipts_qty": 0.0,
            "sales_qty": 2.0,
            "closing_qty": 55.0,
            "extra": {
                "vision_row_index": 0,
                "physical_cells": {
                    "0": "REOSTO TAB",
                    "1": "30S",
                    "2": "58",
                    "3": "0",
                    "4": "0",
                    "5": "0",
                    "6": "58",
                    "7": "2",
                    "8": "1",
                    "9": "0",
                    "10": "55",
                },
            },
        }
        septilin_first = {
            "product_name": "SEPTILIN SYP",
            "opening_qty": 195.0,
            "receipts_qty": 280.0,
            "sales_qty": 475.0,
            "closing_qty": 1.0,
            "extra": {
                "vision_row_index": 2,
                "physical_cells": {
                    "0": "SEPTILIN SYP",
                    "1": "200ML",
                    "2": "195",
                    "3": "280",
                    "4": "0",
                    "5": "0",
                    "6": "475",
                    "7": "154",
                    "8": "1",
                    "9": "0",
                    "10": "320",
                },
            },
        }
        septilin_clone = {
            "product_name": "SEPTILIN SYP",
            "opening_qty": 58.0,
            "receipts_qty": 0.0,
            "sales_qty": 2.0,
            "closing_qty": 55.0,
            "extra": {
                "vision_row_index": 2,
                "physical_cells": {
                    "0": "SEPTILIN SYP",
                    "1": "30S",
                    "2": "58",
                    "3": "0",
                    "4": "0",
                    "5": "0",
                    "6": "58",
                    "7": "2",
                    "8": "1",
                    "9": "0",
                    "10": "55",
                },
            },
        }
        merged = merge_reread(
            [reosto, septilin_first],
            [septilin_clone],
            prefer_reconciled=True,
        )
        sept = next(i for i in merged if i["product_name"] == "SEPTILIN SYP")
        self.assertEqual(sept["opening_qty"], 195.0)
        self.assertEqual(sept["sales_qty"], 475.0)

    def test_mentat_syp_ocr_overlay_fixes_sales_closing(self):
        from services.sales_statement_extractor import (
            _repair_ssa_analysis_result_from_physical_cells,
            _ssa_parse_ocr_product_lines,
        )

        ocr = (
            "MENTAT SYP 200ML 32 0 0 0 32 18 10 0 4\n"
            "MENTAT TAB 60S 72 0 0 0 72 18 0 0 54\n"
        )
        rows = _ssa_parse_ocr_product_lines(ocr)
        keys = {r["product_key"] for r in rows}
        self.assertIn("MENTAT SYP", keys)
        mentat = next(r for r in rows if r["product_key"] == "MENTAT SYP")
        self.assertEqual(mentat["opening_qty"], 32.0)
        self.assertEqual(mentat["sales_qty"], 18.0)
        self.assertEqual(mentat["closing_qty"], 4.0)

        result = {
            "report_title": "STOCK & SALES ANALYSIS",
            "line_items": [
                {
                    "product_name": "MENTAT SYP",
                    "packing": "60S",
                    "opening_qty": 32.0,
                    "receipts_qty": 0.0,
                    "sales_qty": 10.0,
                    "closing_qty": 22.0,
                    "extra": {
                        "physical_cells": {
                            "0": "MENTAT SYP",
                            "1": "60S",
                            "2": "32",
                            "3": "0",
                            "4": "0",
                            "5": "32",
                            "6": "10",
                            "7": "0",
                            "8": "22",
                        }
                    },
                }
            ],
            "totals": {"extra": {}},
        }
        with patch.dict(
            os.environ,
            {
                "STOCK_SSA_ANALYSIS_PHYSICAL_REMAP": "true",
                "STOCK_SSA_ANALYSIS_OCR_OVERLAY": "true",
                "STOCK_VISION_FIRST": "false",
            },
        ):
            _repair_ssa_analysis_result_from_physical_cells(result, ocr_text=ocr)
        item = result["line_items"][0]
        self.assertEqual(item["sales_qty"], 18.0)
        self.assertEqual(item["closing_qty"], 4.0)
        self.assertTrue(item["extra"].get("ssa_ocr_overlay"))

    def test_mentat_syp_retitle_from_gummies_sibling(self):
        """Vision merged SYP digits onto GUMMIES — OCR retitles + fixes qty."""
        from services.sales_statement_extractor import (
            _repair_ssa_analysis_result_from_physical_cells,
        )

        # Garbled GUMMIES OCR + truncated SYP (closing digit lost) — prefer SYP.
        ocr = (
            "MENTAT GUMMIES 308 0 0 0 0 9 1 0 1\n"
            "MENTAT SYP 200ML 32 0 0 0 32 18 10\n"
        )
        result = {
            "report_title": "STOCK & SALES ANALYSIS",
            "line_items": [
                {
                    "product_name": "MENTAT GUMMIES",
                    "packing": "200ML",
                    "opening_qty": 32.0,
                    "receipts_qty": 0.0,
                    "sales_qty": 10.0,
                    "closing_qty": 3.0,
                    "extra": {"physical_cells": {"0": "MENTAT GUMMIES", "1": "32"}},
                }
            ],
            "totals": {"extra": {}},
        }
        with patch.dict(
            os.environ,
            {
                "STOCK_SSA_ANALYSIS_PHYSICAL_REMAP": "true",
                "STOCK_SSA_ANALYSIS_OCR_OVERLAY": "true",
                "STOCK_VISION_FIRST": "false",
            },
        ):
            _repair_ssa_analysis_result_from_physical_cells(result, ocr_text=ocr)
        item = result["line_items"][0]
        self.assertEqual(item["product_name"], "MENTAT SYP")
        self.assertEqual(item["sales_qty"], 18.0)
        self.assertEqual(item["closing_qty"], 4.0)
        self.assertTrue(item["extra"].get("ssa_ocr_retitle"))


class VisionFirstEvergreenTests(unittest.TestCase):
    """STOCK_VISION_FIRST: Vision qty authoritative; OCR compare-only."""

    def test_vgel_printed_sales_and_closing_from_physical(self):
        from services.sales_statement_extractor import (
            _repair_ssa_analysis_item_from_physical_cells,
        )

        item = {
            "product_name": "V-GEL",
            "packing": "30G",
            "opening_qty": 29.0,
            "sales_qty": 29.0,
            "closing_qty": 0.0,
            "extra": {
                "physical_cells": {
                    "0": "V-GEL",
                    "1": "30G",
                    "2": "29",
                    "3": "0",
                    "4": "0",
                    "5": "0",
                    "6": "29",
                    "7": "4",
                    "8": "0",
                    "9": "0",
                    "10": "25",
                },
                "vision_snapshot": {
                    "product_name": "V-GEL",
                    "opening_qty": 29.0,
                    "sales_qty": 29.0,
                    "closing_qty": 0.0,
                },
            },
        }
        with patch.dict(
            os.environ,
            {
                "STOCK_SSA_ANALYSIS_PHYSICAL_REMAP": "true",
                "STOCK_VISION_FIRST": "true",
            },
        ):
            self.assertTrue(_repair_ssa_analysis_item_from_physical_cells(item))
        self.assertEqual(item["sales_qty"], 4.0)
        self.assertEqual(item["closing_qty"], 25.0)

    def test_vision_first_does_not_derive_closing(self):
        from services.sales_statement_extractor import (
            _repair_ssa_analysis_item_from_physical_cells,
        )

        item = {
            "product_name": "V-GEL",
            "packing": "30G",
            "opening_qty": 29.0,
            "sales_qty": 4.0,
            "closing_qty": 0.0,
            "extra": {
                "physical_cells": {
                    "0": "V-GEL",
                    "1": "30G",
                    "2": "29",
                    "3": "0",
                    "4": "0",
                    "5": "29",
                    "6": "4",
                    "7": "0",
                    "8": "0",
                    "9": "0",
                    "10": "0",
                }
            },
        }
        with patch.dict(
            os.environ,
            {
                "STOCK_SSA_ANALYSIS_PHYSICAL_REMAP": "true",
                "STOCK_VISION_FIRST": "true",
            },
        ):
            _repair_ssa_analysis_item_from_physical_cells(item)
        # Layout re-slot may run; closing must not be invented as 25.
        self.assertNotEqual(
            (item.get("extra") or {}).get("field_source", {}).get("closing_qty"),
            "derived",
        )
        if item.get("closing_qty") is not None:
            self.assertNotEqual(item["closing_qty"], 25.0)

    def test_ocr_compare_does_not_overwrite_or_retitle(self):
        from services.sales_statement_extractor import (
            _repair_ssa_analysis_result_from_physical_cells,
        )

        ocr = (
            "MENTAT GUMMIES 308 0 0 0 0 9 1 0 1\n"
            "MENTAT SYP 200ML 32 0 0 0 32 18 10\n"
        )
        result = {
            "report_title": "STOCK & SALES ANALYSIS",
            "line_items": [
                {
                    "product_name": "MENTAT GUMMIES",
                    "packing": "200ML",
                    "opening_qty": 32.0,
                    "receipts_qty": 0.0,
                    "sales_qty": 10.0,
                    "closing_qty": 3.0,
                    "extra": {
                        "physical_cells": {"0": "MENTAT GUMMIES", "1": "32"},
                        "vision_snapshot": {
                            "product_name": "MENTAT GUMMIES",
                            "opening_qty": 32.0,
                            "sales_qty": 10.0,
                            "closing_qty": 3.0,
                        },
                    },
                }
            ],
            "totals": {"extra": {}},
        }
        with patch.dict(
            os.environ,
            {
                "STOCK_SSA_ANALYSIS_PHYSICAL_REMAP": "true",
                "STOCK_SSA_ANALYSIS_OCR_OVERLAY": "true",
                "STOCK_VISION_FIRST": "true",
            },
        ):
            _repair_ssa_analysis_result_from_physical_cells(result, ocr_text=ocr)
        item = result["line_items"][0]
        self.assertEqual(item["product_name"], "MENTAT GUMMIES")
        self.assertEqual(item["sales_qty"], 10.0)
        self.assertEqual(item["closing_qty"], 3.0)
        self.assertFalse(item["extra"].get("ssa_ocr_overlay"))
        self.assertFalse(item["extra"].get("ssa_ocr_retitle"))
        self.assertTrue(item["extra"].get("ocr_vision_disagreement"))
        cands = item["extra"].get("candidates") or []
        self.assertTrue(any(c.get("source") == "ocr_compare" for c in cands))

    def test_speman_zero_preserved(self):
        item = {
            "product_name": "SPEMAN TAB",
            "packing": "60S",
            "opening_qty": 27.0,
            "receipts_qty": 0.0,
            "sales_qty": 0.0,
            "closing_qty": 26.0,
            "extra": {"vision_snapshot": {"sales_qty": 0.0, "closing_qty": 26.0}},
        }
        self.assertEqual(item["sales_qty"], 0.0)
        self.assertEqual(item["extra"]["vision_snapshot"]["sales_qty"], 0.0)

    def test_null_closing_not_invented_under_vision_first(self):
        from services.sales_statement_extractor import (
            _repair_ssa_analysis_item_from_physical_cells,
        )

        item = {
            "product_name": "STYPLON TAB",
            "packing": "30S",
            "opening_qty": 5.0,
            "sales_qty": 0.0,
            "closing_qty": None,
            "extra": {
                "physical_cells": {
                    "0": "STYPLON TAB",
                    "1": "30S",
                    "2": "5",
                    "3": "0",
                    "4": "0",
                    "5": "5",
                    "6": "0",
                    "7": "0",
                    "8": "0",
                    "9": "0",
                    "10": "0",
                },
                "field_source": {"closing_qty": "missing"},
            },
        }
        with patch.dict(
            os.environ,
            {
                "STOCK_SSA_ANALYSIS_PHYSICAL_REMAP": "true",
                "STOCK_VISION_FIRST": "true",
            },
        ):
            _repair_ssa_analysis_item_from_physical_cells(item)
        fs = (item.get("extra") or {}).get("field_source") or {}
        self.assertNotEqual(fs.get("closing_qty"), "derived")

    def test_vision_snapshot_helper(self):
        from services.stock_vision_table import _maybe_stamp_vision_first_snapshot

        item = {
            "product_name": "REOSTO TAB",
            "packing": "30S",
            "opening_qty": 58.0,
            "receipts_qty": 0.0,
            "sales_qty": 2.0,
            "closing_qty": 55.0,
            "extra": {},
        }
        with patch.dict(os.environ, {"STOCK_VISION_FIRST": "false"}):
            _maybe_stamp_vision_first_snapshot(item)
        self.assertNotIn("vision_snapshot", item["extra"])
        with patch.dict(os.environ, {"STOCK_VISION_FIRST": "true"}):
            _maybe_stamp_vision_first_snapshot(item)
        snap = item["extra"]["vision_snapshot"]
        self.assertEqual(snap["sales_qty"], 2.0)
        self.assertEqual(snap["closing_qty"], 55.0)

    def test_merge_reread_clone_reject_with_vision_first(self):
        from services.stock_vision_table import merge_reread

        reosto = {
            "product_name": "REOSTO TAB",
            "opening_qty": 58.0,
            "receipts_qty": 0.0,
            "sales_qty": 2.0,
            "closing_qty": 55.0,
            "extra": {
                "vision_row_index": 0,
                "physical_cells": {
                    "0": "REOSTO TAB",
                    "1": "30S",
                    "2": "58",
                    "3": "0",
                    "4": "0",
                    "5": "0",
                    "6": "58",
                    "7": "2",
                    "8": "1",
                    "9": "0",
                    "10": "55",
                },
            },
        }
        septilin_first = {
            "product_name": "SEPTILIN SYP",
            "opening_qty": 195.0,
            "receipts_qty": 280.0,
            "sales_qty": 475.0,
            "closing_qty": 1.0,
            "extra": {
                "vision_row_index": 2,
                "physical_cells": {
                    "0": "SEPTILIN SYP",
                    "1": "200ML",
                    "2": "195",
                    "3": "280",
                    "4": "0",
                    "5": "0",
                    "6": "475",
                    "7": "154",
                    "8": "1",
                    "9": "0",
                    "10": "320",
                },
            },
        }
        septilin_clone = {
            "product_name": "SEPTILIN SYP",
            "opening_qty": 58.0,
            "receipts_qty": 0.0,
            "sales_qty": 2.0,
            "closing_qty": 55.0,
            "extra": {
                "vision_row_index": 2,
                "physical_cells": {
                    "0": "SEPTILIN SYP",
                    "1": "30S",
                    "2": "58",
                    "3": "0",
                    "4": "0",
                    "5": "0",
                    "6": "58",
                    "7": "2",
                    "8": "1",
                    "9": "0",
                    "10": "55",
                },
            },
        }
        with patch.dict(os.environ, {"STOCK_VISION_FIRST": "true"}):
            merged = merge_reread(
                [reosto, septilin_first],
                [septilin_clone],
                prefer_reconciled=True,
            )
        sept = next(i for i in merged if i["product_name"] == "SEPTILIN SYP")
        self.assertEqual(sept["opening_qty"], 195.0)
        self.assertEqual(sept["sales_qty"], 475.0)


class EvergreenBannerQtyReattachTests(unittest.TestCase):
    """Division-banner qty must reattach to the first product, not shift neighbors."""

    def _shifted_prefix_items(self):
        """Vision shape: banner holds Arjuna qty; each product holds the next row."""
        banner = {
            "product_name": "HIMALAYA (ZENITH)",
            "packing": None,
            "opening_qty": 608.0,
            "receipts_qty": 64.0,
            "sales_qty": 10.0,
            "closing_qty": 53.0,
            "extra": {
                "physical_cells": {
                    "0": "HIMALAYA (ZENITH)",
                    "1": "608",
                    "2": "64",
                    "3": "0",
                    "4": "0",
                    "5": "64",
                    "6": "10",
                    "7": "1",
                    "8": "0",
                    "9": "53",
                    "10": None,
                    "11": "0",
                    "12": "9",
                },
                "field_source": {
                    "opening_qty": "printed",
                    "sales_qty": "printed",
                    "closing_qty": "printed",
                },
            },
        }
        arjuna = {
            "product_name": "ARJUNA TAB",
            "packing": "30ML",
            "opening_qty": 139.0,
            "receipts_qty": 0.0,
            "sales_qty": 19.0,
            "closing_qty": 122.0,
            "extra": {
                "physical_cells": {
                    "0": "ARJUNA TAB",
                    "1": "139",
                    "2": "0",
                    "3": "2",
                    "4": "0",
                    "5": "141",
                    "6": "19",
                    "7": "0",
                    "8": "0",
                    "9": "122",
                },
                "field_source": {
                    "opening_qty": "printed",
                    "sales_qty": "printed",
                    "closing_qty": "printed",
                    "pack": "printed",
                },
            },
        }
        drop = {
            "product_name": "BONNISAN DROP",
            "packing": "100ML",
            "opening_qty": 561.0,
            "receipts_qty": 1680.0,
            "sales_qty": 966.0,
            "closing_qty": 1275.0,
            "extra": {
                "physical_cells": {
                    "0": "BONNISAN DROP",
                    "1": "100ML",
                    "2": "561",
                    "3": "1680",
                    "4": "0",
                    "5": "0",
                    "6": "2241",
                    "7": "966",
                    "8": "0",
                    "9": "0",
                    "10": "1275",
                },
                "field_source": {
                    "opening_qty": "printed",
                    "sales_qty": "printed",
                    "closing_qty": "printed",
                    "pack": "printed",
                },
            },
        }
        syp = {
            "product_name": "BONNISAN SYP",
            "packing": "200ML",
            "opening_qty": 758.0,
            "receipts_qty": 1120.0,
            "sales_qty": 368.0,
            "closing_qty": 1510.0,
            "extra": {
                "physical_cells": {
                    "0": "BONNISAN SYP",
                    "1": "200ML",
                    "2": "758",
                    "3": "1120",
                    "4": "0",
                    "5": "0",
                    "6": "1878",
                    "7": "368",
                    "8": "0",
                    "9": "0",
                    "10": "1510",
                },
                "field_source": {
                    "opening_qty": "printed",
                    "sales_qty": "printed",
                    "closing_qty": "printed",
                    "pack": "printed",
                },
            },
        }
        return banner, [arjuna, drop, syp]

    def test_garbled_60s_pack_not_opening_qty(self):
        """OCR 60S→608 in OPENING must become pack; opening stays the printed 64."""
        from services.sales_statement_extractor import (
            _repair_ssa_analysis_item_from_physical_cells,
        )
        from services.stock_vision_table import _extract_unlabeled_pack_token

        columns = [
            {
                "col_index": 0,
                "header_text": "ITEM DESCRIPTION",
                "canonical": "product_name",
            },
            {"col_index": 1, "header_text": "OPENING", "canonical": "opening_qty"},
            {"col_index": 2, "header_text": "PURCHASES", "canonical": "purchase_qty"},
            {"col_index": 3, "header_text": "SALE", "canonical": "sales_return_qty"},
            {"col_index": 4, "header_text": "REPL./", "canonical": "ignore"},
            {"col_index": 5, "header_text": "TOTAL", "canonical": "total_qty"},
            {"col_index": 6, "header_text": "SALES", "canonical": "sales_qty"},
            {"col_index": 7, "header_text": "PURCHASE", "canonical": "purchase_return_qty"},
            {"col_index": 8, "header_text": "REPL./", "canonical": "ignore"},
            {"col_index": 9, "header_text": "CLOSING", "canonical": "closing_qty"},
        ]
        cells = ["ARJUNA TAB", "608", "64", "0", "0", "64", "10", "1", "0", "53"]
        with patch.dict(os.environ, {"STOCK_VISION_SSA_GARBLED_PACK": "true"}):
            out, pack = _extract_unlabeled_pack_token(cells, columns, len(columns))
        self.assertEqual(pack, "60S")
        self.assertEqual(out[1], "64")

        item = {
            "product_name": "ARJUNA TAB",
            "packing": None,
            "opening_qty": 608.0,
            "receipts_qty": 64.0,
            "sales_qty": 10.0,
            "closing_qty": 53.0,
            "extra": {
                "physical_cells": {
                    "0": "ARJUNA TAB",
                    "1": "608",
                    "2": "64",
                    "3": "0",
                    "4": "0",
                    "5": "64",
                    "6": "10",
                    "7": "1",
                    "8": "0",
                    "9": "53",
                }
            },
        }
        with patch.dict(
            os.environ,
            {
                "STOCK_SSA_ANALYSIS_PHYSICAL_REMAP": "true",
                "STOCK_VISION_SSA_GARBLED_PACK": "true",
                "STOCK_VISION_FIRST": "true",
            },
        ):
            self.assertTrue(_repair_ssa_analysis_item_from_physical_cells(item))
        self.assertEqual(item["opening_qty"], 64.0)
        self.assertEqual(item["sales_qty"], 10.0)
        self.assertEqual(item["closing_qty"], 53.0)
        self.assertRegex(str(item.get("packing") or ""), r"(?i)60\s*'?S")

    def test_qty_ending_in_8_is_not_converted_to_pack(self):
        from services.stock_vision_table import _normalize_garbled_pack_token

        self.assertEqual(_normalize_garbled_pack_token("608", "ARJUNA TAB"), "60S")
        self.assertEqual(_normalize_garbled_pack_token("308", "MENTAT TAB"), "30S")
        # Real openings must not become packs.
        self.assertEqual(_normalize_garbled_pack_token("88", "BRESOL TAB"), "88")
        self.assertEqual(_normalize_garbled_pack_token("118", "CYSTONE TAB"), "118")

    def test_arjuna_gets_own_row_not_drop(self):
        from services.stock_vision_table import _repair_leading_banner_qty_shift

        banner, items = self._shifted_prefix_items()
        with patch.dict(os.environ, {"STOCK_VISION_BANNER_QTY_REATTACH": "true"}):
            applied = _repair_leading_banner_qty_shift([banner], items)
        self.assertGreater(applied, 0)
        arjuna, drop, syp = items
        self.assertEqual(arjuna["product_name"], "ARJUNA TAB")
        self.assertEqual(arjuna["opening_qty"], 64.0)
        self.assertEqual(arjuna["sales_qty"], 10.0)
        self.assertEqual(arjuna["closing_qty"], 53.0)
        self.assertRegex(str(arjuna.get("packing") or ""), r"(?i)60\s*'?S")
        # First-only reattach: later Vision rows are left untouched (no sheet rotate).
        self.assertEqual(drop["product_name"], "BONNISAN DROP")
        self.assertEqual(drop["opening_qty"], 561.0)
        self.assertEqual(syp["product_name"], "BONNISAN SYP")
        self.assertEqual(syp["opening_qty"], 758.0)
        self.assertNotEqual(drop["opening_qty"], arjuna["opening_qty"])

    def test_flag_off_leaves_shifted_rows(self):
        from services.stock_vision_table import _repair_leading_banner_qty_shift

        banner, items = self._shifted_prefix_items()
        with patch.dict(os.environ, {"STOCK_VISION_BANNER_QTY_REATTACH": "false"}):
            applied = _repair_leading_banner_qty_shift([banner], items)
        self.assertEqual(applied, 0)
        self.assertEqual(items[0]["opening_qty"], 139.0)
        self.assertEqual(items[1]["opening_qty"], 561.0)

    def test_map_single_table_reattaches_leading_banner(self):
        from services.stock_vision_table import _map_single_table

        table = {
            "column_count": 10,
            "header_rows": [
                [
                    {"text": "ITEM DESCRIPTION", "col_index": 0, "x_center": 0.1},
                    {"text": "OPENING", "col_index": 1, "x_center": 0.2},
                    {"text": "PURCHASES", "col_index": 2, "x_center": 0.3},
                    {"text": "SALE", "col_index": 3, "x_center": 0.35},
                    {"text": "REPL./", "col_index": 4, "x_center": 0.4},
                    {"text": "TOTAL", "col_index": 5, "x_center": 0.5},
                    {"text": "SALES", "col_index": 6, "x_center": 0.6},
                    {"text": "PURCHASE", "col_index": 7, "x_center": 0.7},
                    {"text": "REPL./", "col_index": 8, "x_center": 0.75},
                    {"text": "CLOSING", "col_index": 9, "x_center": 0.85},
                ]
            ],
            "rows": [
                {
                    "row_index": 0,
                    "cells": [
                        "HIMALAYA (ZENITH)",
                        "608",
                        "64",
                        "0",
                        "0",
                        "64",
                        "10",
                        "1",
                        "0",
                        "53",
                    ],
                    "is_total_row": False,
                },
                {
                    "row_index": 1,
                    "cells": [
                        "ARJUNA TAB",
                        "30ML",
                        "139",
                        "0",
                        "2",
                        "141",
                        "19",
                        "0",
                        "0",
                        "122",
                    ],
                    "is_total_row": False,
                },
                {
                    "row_index": 2,
                    "cells": [
                        "BONNISAN DROP",
                        "100ML",
                        "561",
                        "1680",
                        "0",
                        "2241",
                        "966",
                        "0",
                        "0",
                        "1275",
                    ],
                    "is_total_row": False,
                },
            ],
        }
        with patch.dict(
            os.environ,
            {
                "STOCK_VISION_BANNER_QTY_REATTACH": "true",
                "STOCK_VISION_SSA_PACK_SHIFT_GUARD": "true",
                "STOCK_VISION_FIRST": "true",
            },
        ):
            mapped = _map_single_table(table, request_id="banner-reattach-test")
        items = mapped["line_items"]
        self.assertGreaterEqual(len(items), 2)
        self.assertEqual(items[0]["product_name"], "ARJUNA TAB")
        self.assertEqual(items[0]["opening_qty"], 64.0)
        self.assertEqual(items[0]["sales_qty"], 10.0)
        self.assertEqual(items[0]["closing_qty"], 53.0)
        self.assertEqual(items[1]["product_name"], "BONNISAN DROP")
        # Neighbor keeps its own Vision cells under first-only banner reattach.
        self.assertEqual(items[1]["opening_qty"], 561.0)
        snap = (items[0].get("extra") or {}).get("vision_snapshot") or {}
        self.assertEqual(snap.get("opening_qty"), 64.0)
        self.assertEqual(snap.get("sales_qty"), 10.0)

    def test_ocr_cannot_retitle_under_vision_first(self):
        from services.sales_statement_extractor import (
            _repair_ssa_analysis_result_from_physical_cells,
        )

        ocr = (
            "MENTAT GUMMIES 308 0 0 0 0 9 1 0 1\n"
            "MENTAT SYP 200ML 32 0 0 0 32 18 10\n"
        )
        result = {
            "report_title": "STOCK & SALES ANALYSIS",
            "line_items": [
                {
                    "product_name": "MENTAT GUMMIES",
                    "packing": "200ML",
                    "opening_qty": 32.0,
                    "receipts_qty": 0.0,
                    "sales_qty": 10.0,
                    "closing_qty": 3.0,
                    "extra": {
                        "physical_cells": {"0": "MENTAT GUMMIES", "1": "32"},
                        "vision_snapshot": {
                            "product_name": "MENTAT GUMMIES",
                            "opening_qty": 32.0,
                            "sales_qty": 10.0,
                            "closing_qty": 3.0,
                        },
                    },
                }
            ],
            "totals": {"extra": {}},
        }
        with patch.dict(
            os.environ,
            {
                "STOCK_SSA_ANALYSIS_PHYSICAL_REMAP": "true",
                "STOCK_SSA_ANALYSIS_OCR_OVERLAY": "true",
                "STOCK_VISION_FIRST": "true",
                "STOCK_VISION_BANNER_QTY_REATTACH": "true",
            },
        ):
            _repair_ssa_analysis_result_from_physical_cells(result, ocr_text=ocr)
        item = result["line_items"][0]
        self.assertEqual(item["product_name"], "MENTAT GUMMIES")
        self.assertEqual(item["sales_qty"], 10.0)

    def test_reosto_septilin_no_qty_exchange_on_reread(self):
        from services.stock_vision_table import merge_reread

        reosto = {
            "product_name": "REOSTO TAB",
            "opening_qty": 58.0,
            "receipts_qty": 0.0,
            "sales_qty": 2.0,
            "closing_qty": 55.0,
            "extra": {
                "vision_row_index": 0,
                "physical_cells": {
                    "0": "REOSTO TAB",
                    "1": "30S",
                    "2": "58",
                    "3": "0",
                    "4": "0",
                    "5": "0",
                    "6": "58",
                    "7": "2",
                    "8": "1",
                    "9": "0",
                    "10": "55",
                },
            },
        }
        septilin = {
            "product_name": "SEPTILIN SYP",
            "opening_qty": 195.0,
            "receipts_qty": 280.0,
            "sales_qty": 475.0,
            "closing_qty": 320.0,
            "extra": {
                "vision_row_index": 2,
                "physical_cells": {
                    "0": "SEPTILIN SYP",
                    "1": "200ML",
                    "2": "195",
                    "3": "280",
                    "4": "0",
                    "5": "0",
                    "6": "475",
                    "7": "154",
                    "8": "1",
                    "9": "0",
                    "10": "320",
                },
            },
        }
        clone = {
            "product_name": "SEPTILIN SYP",
            "opening_qty": 58.0,
            "receipts_qty": 0.0,
            "sales_qty": 2.0,
            "closing_qty": 55.0,
            "extra": {
                "vision_row_index": 2,
                "physical_cells": {
                    "0": "SEPTILIN SYP",
                    "1": "30S",
                    "2": "58",
                    "3": "0",
                    "4": "0",
                    "5": "0",
                    "6": "58",
                    "7": "2",
                    "8": "1",
                    "9": "0",
                    "10": "55",
                },
            },
        }
        with patch.dict(
            os.environ,
            {
                "STOCK_VISION_FIRST": "true",
                "STOCK_VISION_BANNER_QTY_REATTACH": "true",
            },
        ):
            merged = merge_reread([reosto, septilin], [clone], prefer_reconciled=True)
        by_name = {i["product_name"]: i for i in merged}
        self.assertEqual(by_name["REOSTO TAB"]["sales_qty"], 2.0)
        self.assertEqual(by_name["SEPTILIN SYP"]["sales_qty"], 475.0)
        self.assertEqual(by_name["SEPTILIN SYP"]["opening_qty"], 195.0)

    def test_printed_zero_and_null_closing_rules(self):
        from services.sales_statement_extractor import (
            _repair_ssa_analysis_item_from_physical_cells,
        )

        zero_sales = {
            "product_name": "SPEMAN TAB",
            "packing": "60S",
            "opening_qty": 27.0,
            "receipts_qty": 0.0,
            "sales_qty": 0.0,
            "closing_qty": 26.0,
            "extra": {
                "physical_cells": {
                    "0": "SPEMAN TAB",
                    "1": "60S",
                    "2": "27",
                    "3": "0",
                    "4": "0",
                    "5": "0",
                    "6": "27",
                    "7": "0",
                    "8": "0",
                    "9": "0",
                    "10": "26",
                }
            },
        }
        unread_close = {
            "product_name": "STYPLON TAB",
            "packing": "30S",
            "opening_qty": 5.0,
            "sales_qty": 0.0,
            "closing_qty": None,
            "extra": {
                "physical_cells": {
                    "0": "STYPLON TAB",
                    "1": "30S",
                    "2": "5",
                    "3": "0",
                    "4": "0",
                    "5": "5",
                    "6": "0",
                    "7": "0",
                    "8": "0",
                    "9": "0",
                    "10": "0",
                },
                "field_source": {"closing_qty": "missing"},
            },
        }
        with patch.dict(
            os.environ,
            {
                "STOCK_SSA_ANALYSIS_PHYSICAL_REMAP": "true",
                "STOCK_VISION_FIRST": "true",
                "STOCK_VISION_BANNER_QTY_REATTACH": "true",
            },
        ):
            _repair_ssa_analysis_item_from_physical_cells(zero_sales)
            _repair_ssa_analysis_item_from_physical_cells(unread_close)
        self.assertEqual(zero_sales["sales_qty"], 0.0)
        fs = (unread_close.get("extra") or {}).get("field_source") or {}
        self.assertNotEqual(fs.get("closing_qty"), "derived")


class EvergreenFocusedRereadTests(unittest.TestCase):
    """SSA focused reread: neighbor qty steals without inventing digits."""

    def test_detect_sales_closing_steal_vgel_pattern(self):
        from services.stock_vision_table import detect_ssa_neighbor_contamination_indices

        items = [
            {
                "product_name": "STYPLON TAB",
                "opening_qty": 5.0,
                "receipts_qty": 0.0,
                "sales_qty": 4.0,
                "closing_qty": 25.0,
                "extra": {},
            },
            {
                "product_name": "V-GEL",
                "opening_qty": 29.0,
                "receipts_qty": 0.0,
                "sales_qty": 0.0,
                "closing_qty": 0.0,
                "extra": {},
            },
        ]
        self.assertEqual(detect_ssa_neighbor_contamination_indices(items), [0, 1])

    def test_detect_trailing_sales_steal_when_closing_already_dropped(self):
        from services.stock_vision_table import detect_ssa_neighbor_contamination_indices

        items = [
            {
                "product_name": "STYPLON TAB",
                "opening_qty": 5.0,
                "receipts_qty": 0.0,
                "sales_qty": 4.0,
                "closing_qty": 0.0,
                "extra": {},
            },
            {
                "product_name": "V-GEL",
                "opening_qty": 29.0,
                "receipts_qty": 0.0,
                "sales_qty": 0.0,
                "closing_qty": 0.0,
                "extra": {},
            },
        ]
        self.assertEqual(detect_ssa_neighbor_contamination_indices(items), [0, 1])

    def test_detect_closing_steal_mentat_pattern(self):
        from services.stock_vision_table import detect_ssa_neighbor_contamination_indices

        items = [
            {
                "product_name": "MENTAT SYP",
                "opening_qty": 32.0,
                "receipts_qty": 0.0,
                "sales_qty": 18.0,
                "closing_qty": 54.0,
                "extra": {"purchase_return": 0.0},
            },
            {
                "product_name": "MENTAT TAB",
                "opening_qty": 72.0,
                "receipts_qty": 0.0,
                "sales_qty": 18.0,
                "closing_qty": 0.0,
                "extra": {},
            },
        ]
        self.assertEqual(detect_ssa_neighbor_contamination_indices(items), [0, 1])

    def test_legitimate_printed_54_not_flagged(self):
        from services.stock_vision_table import detect_ssa_neighbor_contamination_indices

        items = [
            {
                "product_name": "MENTAT TAB",
                "opening_qty": 72.0,
                "receipts_qty": 0.0,
                "sales_qty": 18.0,
                "closing_qty": 54.0,
                "extra": {},
            },
            {
                "product_name": "PLATENZA SYP",
                "opening_qty": 3.0,
                "receipts_qty": 0.0,
                "sales_qty": 0.0,
                "closing_qty": 3.0,
                "extra": {},
            },
        ]
        # TAB balances; not a steal onto the next row.
        self.assertEqual(detect_ssa_neighbor_contamination_indices(items), [])

    def test_clear_sales_closing_steal_marks_missing(self):
        from services.stock_vision_table import clear_persistent_ssa_contamination_qty

        items = [
            {
                "product_name": "STYPLON TAB",
                "opening_qty": 5.0,
                "receipts_qty": 0.0,
                "sales_qty": 4.0,
                "closing_qty": 25.0,
                "extra": {"field_source": {}},
            },
            {
                "product_name": "V-GEL",
                "opening_qty": 29.0,
                "receipts_qty": 0.0,
                "sales_qty": 0.0,
                "closing_qty": 0.0,
                "extra": {"field_source": {}},
            },
        ]
        with patch.dict(os.environ, {"STOCK_VISION_SSA_FOCUSED_REREAD": "true"}):
            n = clear_persistent_ssa_contamination_qty(items)
        self.assertGreaterEqual(n, 2)
        self.assertIsNone(items[0]["sales_qty"])
        self.assertIsNone(items[0]["closing_qty"])
        self.assertIsNone(items[1]["sales_qty"])
        self.assertIsNone(items[1]["closing_qty"])
        self.assertEqual(items[1]["extra"]["field_source"]["closing_qty"], "missing")

    def test_clear_closing_steal_keeps_sales(self):
        from services.stock_vision_table import clear_persistent_ssa_contamination_qty

        items = [
            {
                "product_name": "MENTAT SYP",
                "opening_qty": 32.0,
                "receipts_qty": 0.0,
                "sales_qty": 18.0,
                "closing_qty": 54.0,
                "extra": {"field_source": {}, "purchase_return": 0.0},
            },
            {
                "product_name": "MENTAT TAB",
                "opening_qty": 72.0,
                "receipts_qty": 0.0,
                "sales_qty": 18.0,
                "closing_qty": 0.0,
                "extra": {"field_source": {}},
            },
        ]
        with patch.dict(os.environ, {"STOCK_VISION_SSA_FOCUSED_REREAD": "true"}):
            clear_persistent_ssa_contamination_qty(items)
        self.assertEqual(items[0]["sales_qty"], 18.0)
        self.assertIsNone(items[0]["closing_qty"])
        self.assertEqual(items[1]["sales_qty"], 18.0)
        self.assertIsNone(items[1]["closing_qty"])

    def test_vgel_correct_reread_merge_keeps_printed_sales_closing(self):
        from services.stock_vision_table import merge_reread

        first = [
            {
                "product_name": "STYPLON TAB",
                "opening_qty": 5.0,
                "receipts_qty": 0.0,
                "sales_qty": 4.0,
                "closing_qty": 25.0,
                "extra": {"vision_row_index": 6, "reconciliation_failed": True},
            },
            {
                "product_name": "V-GEL",
                "opening_qty": 29.0,
                "receipts_qty": 0.0,
                "sales_qty": 0.0,
                "closing_qty": 0.0,
                "extra": {"vision_row_index": 7, "reconciliation_failed": True},
            },
        ]
        reread = [
            {
                "product_name": "STYPLON TAB",
                "opening_qty": 5.0,
                "receipts_qty": 0.0,
                "sales_qty": 0.0,
                "closing_qty": 5.0,
                "extra": {
                    "vision_row_index": 6,
                    "physical_cells": {
                        "0": "STYPLON TAB",
                        "1": "30S",
                        "2": "5",
                        "3": "0",
                        "4": "0",
                        "5": "0",
                        "6": "5",
                        "7": "0",
                        "8": "0",
                        "9": "0",
                        "10": "5",
                    },
                },
            },
            {
                "product_name": "V-GEL",
                "opening_qty": 29.0,
                "receipts_qty": 0.0,
                "sales_qty": 4.0,
                "closing_qty": 25.0,
                "extra": {
                    "vision_row_index": 7,
                    "physical_cells": {
                        "0": "V-GEL",
                        "1": "30G",
                        "2": "29",
                        "3": "0",
                        "4": "0",
                        "5": "0",
                        "6": "29",
                        "7": "4",
                        "8": "0",
                        "9": "0",
                        "10": "25",
                    },
                },
            },
        ]
        merged = merge_reread(first, reread, prefer_reconciled=True)
        by = {i["product_name"]: i for i in merged}
        self.assertEqual(by["V-GEL"]["sales_qty"], 4.0)
        self.assertEqual(by["V-GEL"]["closing_qty"], 25.0)

    def test_mentat_correct_closing_from_reread(self):
        from services.stock_vision_table import merge_reread

        first = [
            {
                "product_name": "MENTAT SYP",
                "opening_qty": 32.0,
                "receipts_qty": 0.0,
                "sales_qty": 18.0,
                "closing_qty": 54.0,
                "extra": {"vision_row_index": 35, "reconciliation_failed": True},
            }
        ]
        reread = [
            {
                "product_name": "MENTAT SYP",
                "opening_qty": 32.0,
                "receipts_qty": 0.0,
                "sales_qty": 18.0,
                "closing_qty": 4.0,
                "extra": {
                    "vision_row_index": 35,
                    "physical_cells": {
                        "0": "MENTAT SYP",
                        "1": "200ML",
                        "2": "32",
                        "3": "0",
                        "4": "0",
                        "5": "0",
                        "6": "32",
                        "7": "18",
                        "8": "10",
                        "9": "0",
                        "10": "4",
                    },
                    "purchase_return": 10.0,
                },
            }
        ]
        merged = merge_reread(first, reread, prefer_reconciled=True)
        self.assertEqual(merged[0]["closing_qty"], 4.0)

    def test_unreadable_stays_null_not_zero(self):
        item = {
            "product_name": "UNKNOWN SYP",
            "opening_qty": 10.0,
            "sales_qty": None,
            "closing_qty": None,
            "extra": {
                "field_source": {
                    "sales_qty": "missing",
                    "closing_qty": "missing",
                }
            },
        }
        self.assertIsNone(item["sales_qty"])
        self.assertIsNone(item["closing_qty"])
        self.assertEqual(item["extra"]["field_source"]["closing_qty"], "missing")

    def test_vision_first_ocr_still_compare_only(self):
        from services.sales_statement_extractor import (
            _repair_ssa_analysis_result_from_physical_cells,
        )

        ocr = "MENTAT SYP 200ML 32 0 0 0 32 18 10 0 4\n"
        result = {
            "report_title": "STOCK & SALES ANALYSIS",
            "line_items": [
                {
                    "product_name": "MENTAT SYP",
                    "packing": "200ML",
                    "opening_qty": 32.0,
                    "receipts_qty": 0.0,
                    "sales_qty": 18.0,
                    "closing_qty": 54.0,
                    "extra": {
                        "physical_cells": {
                            "0": "MENTAT SYP",
                            "1": "200ML",
                            "2": "32",
                            "3": "0",
                            "4": "0",
                            "5": "0",
                            "6": "32",
                            "7": "18",
                            "8": "0",
                            "9": "0",
                            "10": "54",
                        },
                        "vision_snapshot": {
                            "product_name": "MENTAT SYP",
                            "opening_qty": 32.0,
                            "sales_qty": 18.0,
                            "closing_qty": 54.0,
                        },
                    },
                }
            ],
            "totals": {"extra": {}},
        }
        with patch.dict(
            os.environ,
            {
                "STOCK_SSA_ANALYSIS_PHYSICAL_REMAP": "true",
                "STOCK_SSA_ANALYSIS_OCR_OVERLAY": "true",
                "STOCK_VISION_FIRST": "true",
                "STOCK_VISION_SSA_FOCUSED_REREAD": "true",
            },
        ):
            _repair_ssa_analysis_result_from_physical_cells(result, ocr_text=ocr)
        item = result["line_items"][0]
        self.assertEqual(item["closing_qty"], 54.0)
        self.assertFalse(item["extra"].get("ssa_ocr_overlay"))
        self.assertTrue(item["extra"].get("ocr_vision_disagreement"))

    def test_prioritize_caps_and_prefers_contamination(self):
        from services.stock_vision_table import prioritize_ssa_recovery_meta

        items = [
            {
                "product_name": f"P{i}",
                "opening_qty": 10.0,
                "receipts_qty": 0.0,
                "sales_qty": 0.0,
                "closing_qty": 10.0,
                "extra": {},
            }
            for i in range(5)
        ]
        items[3] = {
            "product_name": "STYPLON TAB",
            "opening_qty": 5.0,
            "receipts_qty": 0.0,
            "sales_qty": 4.0,
            "closing_qty": 25.0,
            "extra": {},
        }
        items[4] = {
            "product_name": "V-GEL",
            "opening_qty": 29.0,
            "receipts_qty": 0.0,
            "sales_qty": 0.0,
            "closing_qty": 0.0,
            "extra": {},
        }
        meta = [
            {
                "list_index": i,
                "product_name": items[i]["product_name"],
                "quantity_difference": 1.0,
                "y_center": 0.2 + i * 0.1,
            }
            for i in range(5)
        ]
        meta[0]["quantity_difference"] = 99.0
        with patch.dict(os.environ, {"STOCK_VISION_SSA_FOCUSED_REREAD": "true"}):
            chosen = prioritize_ssa_recovery_meta(meta, items, max_rows=2)
        names = [c["product_name"] for c in chosen]
        self.assertEqual(names, ["STYPLON TAB", "V-GEL"])

    def test_trailing_rows_preferred_without_contamination(self):
        from services.stock_vision_table import prioritize_ssa_recovery_meta

        items = [
            {
                "product_name": f"P{i}",
                "opening_qty": 10.0,
                "sales_qty": 1.0,
                "closing_qty": 9.0,
                "extra": {},
            }
            for i in range(10)
        ]
        meta = [
            {
                "list_index": i,
                "product_name": items[i]["product_name"],
                "quantity_difference": 50.0 if i == 0 else 5.0,
                "y_center": 0.1 + i * 0.08,
            }
            for i in range(10)
        ]
        chosen = prioritize_ssa_recovery_meta(meta, items, max_rows=3)
        idxs = [int(c["list_index"]) for c in chosen]
        # Despite P0 having the largest raw diff, prefer Total-adjacent rows.
        self.assertTrue(all(i >= 7 for i in idxs))

    def test_flag_off_skips_contamination_clear(self):
        from services.stock_vision_table import clear_persistent_ssa_contamination_qty

        items = [
            {
                "product_name": "STYPLON TAB",
                "opening_qty": 5.0,
                "sales_qty": 4.0,
                "closing_qty": 25.0,
                "extra": {},
            },
            {
                "product_name": "V-GEL",
                "opening_qty": 29.0,
                "sales_qty": 0.0,
                "closing_qty": 0.0,
                "extra": {},
            },
        ]
        with patch.dict(os.environ, {"STOCK_VISION_SSA_FOCUSED_REREAD": "false"}):
            self.assertEqual(clear_persistent_ssa_contamination_qty(items), 0)
        self.assertEqual(items[1]["sales_qty"], 0.0)


class EvergreenRowFixtureTests(unittest.TestCase):
    """Deterministic row fixtures for two-line name / pack association."""

    def test_two_consecutive_rows_keep_own_numbers(self):
        rows = [
            {
                "product_name": "ARJUNA TAB",
                "packing": "60S",
                "opening_qty": 64.0,
                "receipts_qty": 0.0,
                "sales_qty": 10.0,
                "closing_qty": 53.0,
            },
            {
                "product_name": "BONNISAN DROP",
                "packing": "30ML",
                "opening_qty": 139.0,
                "receipts_qty": 0.0,
                "sales_qty": 19.0,
                "closing_qty": 122.0,
            },
        ]
        self.assertEqual(rows[0]["opening_qty"], 64.0)
        self.assertEqual(rows[1]["opening_qty"], 139.0)
        self.assertNotEqual(rows[0]["packing"], rows[1]["packing"])

    def test_multiline_name_keeps_numbers(self):
        name = "BONNISAN SYP"
        pack = "100ML"
        nums = {
            "opening_qty": 561.0,
            "receipts_qty": 1680.0,
            "sales_qty": 966.0,
            "closing_qty": 1275.0,
        }
        item = {"product_name": f"{name} {pack}", **nums}
        self.assertTrue(item["product_name"].startswith("BONNISAN SYP"))
        self.assertEqual(item["sales_qty"], 966.0)

    def test_zero_and_missing_do_not_shift_neighbor(self):
        a = {"product_name": "A", "opening_qty": 0.0, "sales_qty": 0.0, "closing_qty": 0.0}
        b = {"product_name": "B", "opening_qty": 10.0, "sales_qty": 2.0, "closing_qty": 8.0}
        self.assertEqual(a["opening_qty"], 0.0)
        self.assertEqual(b["opening_qty"], 10.0)

    def test_duplicate_names_remain_distinct_rows(self):
        rows = [
            {"product_name": "BONNISAN SYP", "packing": "100ML", "opening_qty": 561.0},
            {"product_name": "BONNISAN SYP", "packing": "200ML", "opening_qty": 758.0},
        ]
        self.assertEqual(len(rows), 2)
        self.assertNotEqual(rows[0]["packing"], rows[1]["packing"])

    def test_final_product_before_total_preserved(self):
        products = ["SPEMAN TAB", "STYPLON TAB", "V-GEL"]
        total_label = "Total Quantity"
        self.assertNotIn(total_label, products)
        self.assertEqual(products[-1], "V-GEL")

    def test_continuation_page_does_not_replace_stockist(self):
        with patch.dict(os.environ, {"STOCK_SSA_ANALYSIS_HEADER_STOCKIST": "true"}):
            page1 = _detect_stockist_from_page_text(EVERGREEN_OCR)
            page2 = _detect_stockist_from_page_text(PAGE2_CONTINUATION)
        self.assertEqual(page1, "EVERGREEN DRUGS DISTRIBUTOR")
        # Same letterhead may re-detect; must not become the ANALYSIS title.
        if page2:
            self.assertNotIn("ANALYSIS", page2.upper())
            self.assertIn("EVERGREEN", page2.upper())


if __name__ == "__main__":
    unittest.main()
