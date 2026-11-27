"""OPENING STOCK|PURCHASES|TOTAL STOCK|SALES grids must not lock SSA qty/value.

Empty OCR + portrait-noise used to force OPENING/RECEIPT/ISSUE dump onto the
qty-only purchase sheet (TOTAL STOCK → sales_qty). Soft-reject + misfile
defer VISION_TABLE past portrait-noise.
"""

from __future__ import annotations

import io
import unittest
from unittest.mock import patch

from PIL import Image

from services.sales_statement_extractor import (
    _peek_suggests_ssa_qty_value,
    _repair_ssa_analysis_item_from_physical_cells,
    _ssa_dump_items_look_like_purchase_qty_misfile,
    _ssa_dump_result_looks_like_purchase_qty_misfile,
    _ssa_fix_spurious_zero_total_shift,
    _ssa_qty_value_result_usable,
    _text_looks_like_ssa_purchase_qty_grid,
)


PURCHASE_QTY_CHROME = (
    "STOCK & SALES ANALYSIS (HIMALYA ZENDRA)\n"
    "ITEM DESCRIPTION OPENING STOCK PURCHASES SALE RETURN REPL./ OTHERS "
    "TOTAL STOCK SALES PURCHASE RETURN REPL./ OTHERS CLOSING STOCK RATE\n"
    "LIV 52 DROP 60ML 12 2100 0 0 2112 2026 0 0 86 97.94\n"
)


def _misfile_items():
    """TOTAL STOCK parked as sales_qty; closing wiped; no per-line VALUE."""
    return [
        {
            "product_name": "ARJUNA CAP",
            "opening_qty": 0.0,
            "receipts_qty": 720.0,
            "sales_qty": 720.0,
            "closing_qty": 0.0,
            "opening_value": 0.0,
            "sales_value": 0.0,
            "closing_value": 0.0,
        },
        {
            "product_name": "CYSTONE CAP",
            "opening_qty": 32.0,
            "receipts_qty": 336.0,
            "sales_qty": 368.0,
            "closing_qty": 0.0,
            "opening_value": 0.0,
            "sales_value": 0.0,
            "closing_value": 0.0,
        },
        {
            "product_name": "SEPTILIN TAB",
            "opening_qty": 65.0,
            "receipts_qty": 600.0,
            "sales_qty": 665.0,
            "closing_qty": 0.0,
            "opening_value": 0.0,
            "sales_value": 0.0,
            "closing_value": 0.0,
        },
        {
            "product_name": "BONNISAN DROP",
            "opening_qty": 0.0,
            "receipts_qty": 56.0,
            "sales_qty": 56.0,
            "closing_qty": 0.0,
            "opening_value": 0.0,
            "sales_value": 0.0,
            "closing_value": 0.0,
        },
        {
            "product_name": "GASEX TAB",
            "opening_qty": 10.0,
            "receipts_qty": 90.0,
            "sales_qty": 100.0,
            "closing_qty": 0.0,
            "opening_value": 0.0,
            "sales_value": 0.0,
            "closing_value": 0.0,
        },
        {
            "product_name": "LUKOL TAB",
            "opening_qty": 5.0,
            "receipts_qty": 45.0,
            "sales_qty": 50.0,
            "closing_qty": 0.0,
            "opening_value": 0.0,
            "sales_value": 0.0,
            "closing_value": 0.0,
        },
        {
            "product_name": "CONFIDO TAB",
            "opening_qty": 8.0,
            "receipts_qty": 42.0,
            "sales_qty": 50.0,
            "closing_qty": 0.0,
            "opening_value": 0.0,
            "sales_value": 0.0,
            "closing_value": 0.0,
        },
        {
            "product_name": "LIV 52 DROP",
            "opening_qty": 0.0,
            "receipts_qty": 0.0,
            "sales_qty": 0.0,
            "closing_qty": 0.0,
            "opening_value": 0.0,
            "sales_value": 0.0,
            "closing_value": 0.0,
        },
        {
            "product_name": "CYSTONE TAB",
            "opening_qty": 0.0,
            "receipts_qty": 0.0,
            "sales_qty": 0.0,
            "closing_qty": 0.0,
            "opening_value": 0.0,
            "sales_value": 0.0,
            "closing_value": 0.0,
        },
        {
            "product_name": "CYSTONE FORTE TAB",
            "opening_qty": 0.0,
            "receipts_qty": 0.0,
            "sales_qty": 0.0,
            "closing_qty": 0.0,
            "opening_value": 0.0,
            "sales_value": 0.0,
            "closing_value": 0.0,
        },
    ]


class SsaPurchaseQtyGridHelpers(unittest.TestCase):
    def test_chrome_detected(self):
        self.assertTrue(_text_looks_like_ssa_purchase_qty_grid(PURCHASE_QTY_CHROME))
        self.assertFalse(
            _text_looks_like_ssa_purchase_qty_grid(
                "STOCK & SALES ANALYSIS OPENING RECEIPT ISSUE CLOSING DUMP VALUE"
            )
        )

    def test_peek_excludes_purchase_qty_grid(self):
        self.assertFalse(_peek_suggests_ssa_qty_value(PURCHASE_QTY_CHROME))
        self.assertTrue(
            _peek_suggests_ssa_qty_value(
                "STOCK & SALES ANALYSIS OPENING QTY OPENING VALUE"
            )
        )

    def test_misfile_items_total_as_sales(self):
        items = _misfile_items()
        self.assertTrue(_ssa_dump_items_look_like_purchase_qty_misfile(items))
        result = {
            "line_items": items,
            "totals": {"extra": {"extraction_method": "ssa_qty_value_vision"}},
        }
        self.assertTrue(_ssa_dump_result_looks_like_purchase_qty_misfile(result))
        self.assertFalse(_ssa_qty_value_result_usable(result))

    def test_flag_stub_without_items(self):
        stub = {
            "line_items": [],
            "totals": {"extra": {"ssa_dump_purchase_qty_grid": True}},
        }
        self.assertTrue(_ssa_dump_result_looks_like_purchase_qty_misfile(stub))


class SsaPurchaseQtyPhysicalRemap(unittest.TestCase):
    def test_spurious_zero_total_shift_liv52(self):
        # Vision: TOTAL=0, then TOTAL/SALES/CLOSING/RATE left-shifted.
        raw = ["12", "2100", "0", "0", "0", "2112", "2026", "86", "97.94"]
        fixed, changed = _ssa_fix_spurious_zero_total_shift(raw)
        self.assertTrue(changed)
        self.assertEqual(
            fixed, ["12", "2100", "0", "0", "2112", "2026", "0", "0", "86"]
        )

    def test_physical_remap_restores_sales_and_closing(self):
        item = {
            "product_name": "LIV 52 DROP",
            "opening_qty": 12.0,
            "receipts_qty": 2100.0,
            "sales_qty": 2112.0,  # TOTAL parked as sales
            "closing_qty": 97.94,  # RATE parked as closing
            "extra": {
                "physical_cells": {
                    "0": "LIV 52 DROP",
                    "1": "12",
                    "2": "2100",
                    "3": "0",
                    "4": "0",
                    "5": "0",
                    "6": "2112",
                    "7": "2026",
                    "8": "86",
                    "9": "97.94",
                }
            },
        }
        self.assertTrue(_repair_ssa_analysis_item_from_physical_cells(item))
        self.assertEqual(item["sales_qty"], 2026.0)
        self.assertEqual(item["closing_qty"], 86.0)
        self.assertEqual(item["opening_qty"], 12.0)
        self.assertEqual(item["receipts_qty"], 2100.0)

    def test_physical_remap_strips_comma_pack_before_shift(self):
        """OCR pack '200,ML' must not absorb opening; SEPTILIN-style shift."""
        item = {
            "product_name": "SEPTILIN TAB",
            "opening_qty": 0.0,
            "receipts_qty": 65.0,
            "sales_qty": 0.0,
            "closing_qty": 44.0,
            "extra": {
                "physical_cells": {
                    "0": "SEPTELIN SYP.",
                    "1": "200,ML",
                    "2": "65",
                    "3": "600",
                    "4": "0",
                    "5": "0",
                    "6": "0",
                    "7": "665",
                    "8": "621",
                    "9": "44",
                    "10": "173.58",
                }
            },
        }
        self.assertTrue(_repair_ssa_analysis_item_from_physical_cells(item))
        self.assertEqual(item["opening_qty"], 65.0)
        self.assertEqual(item["receipts_qty"], 600.0)
        self.assertEqual(item["sales_qty"], 621.0)
        self.assertEqual(item["closing_qty"], 44.0)
        self.assertEqual(item.get("packing"), "200,ML")


class SsaPurchaseQtyGridVtRescue(unittest.TestCase):
    def test_dump_misfile_forces_vt_over_portrait_noise(self):
        from services import sales_statement_extractor as sse

        img = Image.new("RGB", (1200, 1920), color=(255, 255, 255))
        buf = io.BytesIO()
        img.save(buf, format="JPEG")
        data = buf.getvalue()

        misfile = {
            "source_file": "142.jpg",
            "source_format": "jpg",
            "report_title": "STOCK & SALES ANALYSIS",
            "line_items": _misfile_items(),
            "totals": {
                "extra": {
                    "extraction_method": "ssa_qty_value_vision",
                    "layout": "ssa_opening_receipt_issue_value",
                }
            },
        }

        vt_result = {
            "source_file": "142.jpg",
            "source_format": "jpg",
            "line_items": [
                {
                    "product_name": "LIV 52 DROP",
                    "packing": "60ML",
                    "opening_qty": 12.0,
                    "receipts_qty": 2100.0,
                    "sales_qty": 2026.0,
                    "closing_qty": 86.0,
                    "extra": {
                        "vision_column_headers": [
                            "ITEM DESCRIPTION",
                            "OPENING STOCK",
                            "PURCHASES",
                            "SALE RETURN",
                            "REPL./ OTHERS",
                            "TOTAL STOCK",
                            "SALES",
                            "PURCHASE RETURN",
                            "REPL./ OTHERS",
                            "CLOSING STOCK",
                            "RATE",
                        ],
                        "field_source": {
                            "opening_qty": "printed",
                            "sales_qty": "printed",
                            "closing_qty": "printed",
                        },
                    },
                }
            ],
            "totals": {
                "extra": {
                    "extraction_method": "vision_table",
                    "vision_table_final": True,
                    "layout": "stock_sales_analysis",
                    "valid_ratio": 1.0,
                    "row_status_counts": {"VALID": 1, "STRUCTURAL_ERROR": 0},
                }
            },
        }

        with patch.object(sse, "_ocr_image_to_text", return_value=""), patch.object(
            sse,
            "_extract_ssa_opening_receipt_issue_dump_image",
            return_value=misfile,
        ), patch(
            "services.stock_vision_table.vision_table_active_for", return_value=True
        ), patch(
            "services.stock_vision_table.run_vision_table_path",
            return_value={
                "status": "ok",
                "result": vt_result,
                "gemini_calls": 1,
                "gemini_budget": 2,
            },
        ), patch.object(
            sse, "_ssa_dump_portrait_noise_candidate", return_value=True
        ), patch(
            "services.stock_vision_table._needs_vertical_split", return_value=False
        ):
            result = sse._parse_image(data, "142.jpg", ".jpg")

        extra = (result.get("totals") or {}).get("extra") or {}
        self.assertEqual(extra.get("extraction_method"), "vision_table")
        liv = next(
            i
            for i in result["line_items"]
            if "LIV 52" in (i.get("product_name") or "").upper()
        )
        self.assertEqual(liv.get("sales_qty"), 2026.0)
        self.assertEqual(liv.get("closing_qty"), 86.0)


if __name__ == "__main__":
    unittest.main()
