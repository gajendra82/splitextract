"""Portrait phone Excel STOCK & SALES (FREE/P.RETURN) must reach Vision table.

Empty OCR + SSA dump miss used to burn Gemini on LstSL, then skip VT for
ssa_portrait_noise — losing SALE/CLOSING. Defer VT past portrait-noise.
"""

from __future__ import annotations

import io
import unittest
from unittest.mock import patch

from PIL import Image


class ItemPackFreePortraitVtRescueTests(unittest.TestCase):
    def test_dump_miss_defers_vt_and_skips_lstsl(self):
        from services import sales_statement_extractor as sse

        img = Image.new("RGB", (1200, 1920), color=(255, 255, 255))
        buf = io.BytesIO()
        img.save(buf, format="JPEG")
        data = buf.getvalue()

        vt_result = {
            "source_file": "54.jpg",
            "source_format": "jpg",
            "line_items": [
                {
                    "product_name": "CONFIDO TAB",
                    "packing": "60'S",
                    "opening_qty": 66.0,
                    "receipts_qty": 0.0,
                    "sales_qty": 11.0,
                    "closing_qty": 55.0,
                    "extra": {
                        "vision_column_headers": [
                            "ITEM",
                            "PACK",
                            "OPENING",
                            "PURCHASE",
                            "FREE",
                            "P.RETURN",
                            "FREE",
                            "SALE",
                            "FREE",
                            "S.RETURN",
                            "FREE",
                            "OTHERS",
                            "CLOSING",
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
                    "layout": "item_pack_free_return",
                    "valid_ratio": 1.0,
                    "row_status_counts": {"VALID": 1, "STRUCTURAL_ERROR": 0},
                }
            },
        }

        lstsl_calls = []

        def _no_lstsl(*_a, **_k):
            lstsl_calls.append(1)
            return None

        with patch.object(sse, "_ocr_image_to_text", return_value=""), patch.object(
            sse, "_extract_ssa_opening_receipt_issue_dump_image", return_value=None
        ), patch.object(
            sse, "_extract_lstsl_open_recd_vision", side_effect=_no_lstsl
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
        ), patch.object(
            sse, "_ssa_before_lstsl_empty_ocr_enabled", return_value=True
        ), patch(
            "services.stock_vision_table._needs_vertical_split", return_value=False
        ):
            result = sse._parse_image(data, "54.jpg", ".jpg")

        self.assertEqual(lstsl_calls, [])
        items = result.get("line_items") or []
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["sales_qty"], 11.0)
        self.assertEqual(items[0]["closing_qty"], 55.0)
        extra = (result.get("totals") or {}).get("extra") or {}
        self.assertTrue(extra.get("vision_table_final"))


if __name__ == "__main__":
    unittest.main()
