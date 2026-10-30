"""MR Report / Stock and Sales: OPENING STK QTY|…|CLOSING VALUE (PUR RATE).

Landscape photos with bogus EXIF Orientation=6 must not be rotated to portrait.
Header aliases and bare OPENING|OPENING qty|value pairs must resolve.
"""

from __future__ import annotations

import io
import os
import unittest
from unittest.mock import patch

from PIL import Image

from services.stock_header_resolver import parse_number, resolve_columns
from services.stock_vision_table import (
    _exif_orient_image_bytes,
    _is_non_product_vision_row,
    _recover_product_name_from_serial_cell,
    _repair_paisa_parked_in_qty_fields,
    _repair_pur_free_sale_closing_left_shift,
    _repair_qty_copied_into_value_fields,
    build_table_prompt,
)


def _cells(*texts: str):
    return [
        {"text": t, "x_center": None, "col_index": i, "subheader_text": None}
        for i, t in enumerate(texts)
    ]


def _jpeg_with_orientation(w: int, h: int, orientation: int) -> bytes:
    im = Image.new("RGB", (w, h), color=(255, 255, 255))
    exif = im.getexif()
    exif[274] = orientation
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=90, exif=exif.tobytes())
    return buf.getvalue()


class MrReportOpeningStkQtyValueTests(unittest.TestCase):
    def test_aliases_opening_stk_and_closing_value_pur_rate(self):
        result = resolve_columns(
            _cells(
                "SNO.",
                "ITEM NAME",
                "PACK/SIZE",
                "OPENING STK QTY",
                "OPENING VALUE",
                "PURCHASE QTY",
                "PURCHASE VALUE",
                "PUR FREE QTY",
                "SALE QTY",
                "SALE VALUE",
                "SALE FREE QTY",
                "CLOSING STOCK",
                "CLOSING VALUE (PUR RATE)",
            )
        )
        by = {c["col_index"]: c["canonical"] for c in result["columns"]}
        self.assertEqual(by[3], "opening_qty")
        self.assertEqual(by[4], "opening_value")
        self.assertEqual(by[5], "purchase_qty")
        self.assertEqual(by[6], "purchase_value")
        self.assertEqual(by[7], "free_in_qty")
        self.assertEqual(by[8], "sales_qty")
        self.assertEqual(by[9], "sales_value")
        self.assertEqual(by[10], "free_out_qty")
        self.assertEqual(by[11], "closing_qty")
        self.assertEqual(by[12], "closing_value")

    def test_bare_opening_purchase_pair_promotes_value(self):
        """Vision flattens OPENING STK QTY|OPENING VALUE into two OPENING labels."""
        result = resolve_columns(
            _cells(
                "ITEM NAME",
                "OPENING",
                "OPENING",
                "PURCHASE",
                "PURCHASE",
                "PUR FREE",
                "SALE QTY",
                "SALE VALUE",
                "SALE FREE",
                "CLOSING",
                "CLOSING VALUE",
            )
        )
        by = {c["col_index"]: c for c in result["columns"]}
        self.assertEqual(by[1]["canonical"], "opening_qty")
        self.assertEqual(by[2]["canonical"], "opening_value")
        self.assertEqual(by[2]["reason"], "adjacent_qty_value_pair")
        self.assertEqual(by[3]["canonical"], "purchase_qty")
        self.assertEqual(by[4]["canonical"], "purchase_value")
        self.assertEqual(by[4]["reason"], "adjacent_qty_value_pair")
        self.assertEqual(by[5]["canonical"], "free_in_qty")
        self.assertEqual(by[8]["canonical"], "free_out_qty")
        self.assertEqual(by[9]["canonical"], "closing_qty")
        self.assertEqual(by[10]["canonical"], "closing_value")

    def test_exif_skips_landscape_buffer_that_would_become_portrait(self):
        raw = _jpeg_with_orientation(800, 600, orientation=6)
        with patch.dict(
            os.environ,
            {
                "STOCK_VISION_EXIF_TRANSPOSE": "true",
                "STOCK_VISION_EXIF_SKIP_LANDSCAPE_BUFFER": "true",
            },
            clear=False,
        ):
            out = _exif_orient_image_bytes(raw)
        with Image.open(io.BytesIO(out)) as im:
            self.assertEqual(im.size, (800, 600))

    def test_exif_still_transposes_portrait_buffer(self):
        raw = _jpeg_with_orientation(600, 800, orientation=6)
        with patch.dict(
            os.environ,
            {
                "STOCK_VISION_EXIF_TRANSPOSE": "true",
                "STOCK_VISION_EXIF_SKIP_LANDSCAPE_BUFFER": "true",
            },
            clear=False,
        ):
            out = _exif_orient_image_bytes(raw)
        with Image.open(io.BytesIO(out)) as im:
            # Orientation 6 on portrait buffer → landscape after transpose.
            self.assertEqual(im.size, (800, 600))

    def test_prompt_mentions_opening_stk_qty_grid(self):
        prompt = build_table_prompt()
        self.assertIn("OPENING STK QTY", prompt)
        self.assertIn("CLOSING VALUE (PUR RATE)", prompt)

    def test_pur_free_blank_shift_reslots_sale_and_closing(self):
        item = {
            "product_name": "SPEMAN TABS 60",
            "opening_qty": 19.0,
            "receipts_qty": 50.0,
            "sales_qty": 167.51,
            "sales_value": None,
            "closing_qty": 11390.68,
            "closing_value": None,
            "extra": {
                "free_in_qty": 1.0,
                "free_out_qty": 68.0,
                "field_source": {
                    "opening_qty": "printed",
                    "purchase_qty": "printed",
                    "free_in_qty": "printed",
                    "sales_qty": "printed",
                    "sales_value": "missing",
                    "free_out_qty": "printed",
                    "closing_qty": "printed",
                    "closing_value": "missing",
                },
            },
        }
        with patch.dict(
            os.environ, {"STOCK_VISION_PUR_FREE_SALE_CLOSING_SHIFT": "true"}, clear=False
        ):
            self.assertTrue(_repair_pur_free_sale_closing_left_shift(item))
        self.assertEqual(item["sales_qty"], 1.0)
        self.assertEqual(item["sales_value"], 167.51)
        self.assertEqual(item["closing_qty"], 68.0)
        self.assertEqual(item["closing_value"], 11390.68)
        self.assertIsNone(item["extra"]["free_in_qty"])
        self.assertIsNone(item["extra"]["free_out_qty"])

    def test_sale_free_blank_shift_reslots_closing_only(self):
        item = {
            "product_name": "VGEL 30G",
            "opening_qty": 18.0,
            "receipts_qty": 0.0,
            "sales_qty": 0.0,
            "sales_value": None,
            "closing_qty": 1316.52,
            "closing_value": None,
            "extra": {
                "free_out_qty": 18.0,
                "field_source": {
                    "opening_qty": "printed",
                    "sales_qty": "missing",
                    "free_out_qty": "printed",
                    "closing_qty": "printed",
                    "closing_value": "missing",
                },
            },
        }
        with patch.dict(
            os.environ, {"STOCK_VISION_PUR_FREE_SALE_CLOSING_SHIFT": "true"}, clear=False
        ):
            self.assertTrue(_repair_pur_free_sale_closing_left_shift(item))
        self.assertEqual(item["closing_qty"], 18.0)
        self.assertEqual(item["closing_value"], 1316.52)
        self.assertIsNone(item["extra"]["free_out_qty"])

    def test_company_section_banner_is_non_product(self):
        item = {
            "product_name": "HIMALAYA WELNESS COM 11",
            "opening_qty": 0.0,
            "receipts_qty": 0.0,
            "sales_qty": 0.0,
            "closing_qty": None,
            "extra": {
                "field_source": {
                    "opening_qty": "missing",
                    "purchase_qty": "missing",
                    "sales_qty": "missing",
                    "closing_qty": "missing",
                }
            },
        }
        self.assertTrue(_is_non_product_vision_row(item))
        # Real SKU with Himalaya in the name still counts as a product when qty printed.
        sku = {
            "product_name": "HIMALAYA BONNISAN DROPS",
            "opening_qty": 50.0,
            "sales_qty": 13.0,
            "closing_qty": 37.0,
            "extra": {
                "field_source": {
                    "opening_qty": "printed",
                    "sales_qty": "printed",
                    "closing_qty": "printed",
                }
            },
        }
        self.assertFalse(_is_non_product_vision_row(sku))

    def test_recover_product_name_from_sno_cell(self):
        fields: dict = {"product_name": None, "opening_qty": "19"}
        columns = [
            {"col_index": 0, "header_text": "SNO.", "canonical": "ignore", "reason": "serial_number_header"},
            {"col_index": 1, "header_text": "ITEM NAME", "canonical": "product_name"},
        ]
        cells = ["38 SPEMAN TABS 60", None, "1 60", "19"]
        self.assertTrue(
            _recover_product_name_from_serial_cell(fields, cells, columns)
        )
        self.assertEqual(fields["product_name"], "SPEMAN TABS 60")

    def test_pur_free_shift_case_c_sales_value_holds_closing_qty(self):
        item = {
            "product_name": "SPEMAN TABS 60",
            "opening_qty": 19.0,
            "receipts_qty": 50.0,
            "sales_qty": 167.51,
            "sales_value": 68.0,
            "closing_qty": None,
            "closing_value": None,
            "extra": {
                "free_in_qty": 1.0,
                "free_out_qty": 11390.68,
                "field_source": {
                    "opening_qty": "printed",
                    "purchase_qty": "printed",
                    "free_in_qty": "printed",
                    "sales_qty": "printed",
                    "sales_value": "printed",
                    "free_out_qty": "printed",
                    "closing_qty": "missing",
                    "closing_value": "missing",
                },
            },
        }
        with patch.dict(
            os.environ, {"STOCK_VISION_PUR_FREE_SALE_CLOSING_SHIFT": "true"}, clear=False
        ):
            self.assertTrue(_repair_pur_free_sale_closing_left_shift(item))
        self.assertEqual(item["sales_qty"], 1.0)
        self.assertEqual(item["sales_value"], 167.51)
        self.assertEqual(item["closing_qty"], 68.0)
        self.assertEqual(item["closing_value"], 11390.68)

    def test_qty_copied_into_value_cleared(self):
        item = {
            "product_name": "SPEMAN TABS 60",
            "opening_qty": 19.0,
            "opening_value": 19.0,
            "receipts_qty": 50.0,
            "closing_qty": 68.0,
            "closing_value": 11390.68,
            "extra": {
                "purchase_value": 50.0,
                "field_source": {
                    "opening_value": "printed",
                    "purchase_value": "printed",
                    "closing_value": "printed",
                },
            },
        }
        with patch.dict(
            os.environ, {"STOCK_VISION_QTY_COPIED_INTO_VALUE": "true"}, clear=False
        ):
            self.assertTrue(_repair_qty_copied_into_value_fields(item))
        self.assertIsNone(item["opening_value"])
        self.assertIsNone(item["extra"]["purchase_value"])
        self.assertEqual(item["closing_value"], 11390.68)

    def test_trailing_dot_closing_value_parses(self):
        """Vision truncates CLOSING VALUE as '3,123.' / '9,877.' — keep integer part."""
        self.assertEqual(parse_number("3,123."), 3123.0)
        self.assertEqual(parse_number("9,877."), 9877.0)
        self.assertEqual(parse_number("804."), 804.0)
        self.assertEqual(parse_number("9,877.40"), 9877.4)
        self.assertIsNone(parse_number("."))

    def test_paisa_skips_when_free_out_holds_closing_qty(self):
        """Do not clear sales/closing qty when free columns still hold the shift."""
        item = {
            "product_name": "SPEMAN TABS 60",
            "opening_qty": 19.0,
            "receipts_qty": 50.0,
            "sales_qty": 167.51,
            "sales_value": None,
            "closing_qty": 11390.68,
            "closing_value": None,
            "extra": {
                "free_in_qty": 1.0,
                "free_out_qty": 68.0,
                "field_source": {
                    "opening_qty": "printed",
                    "purchase_qty": "printed",
                    "free_in_qty": "printed",
                    "sales_qty": "printed",
                    "free_out_qty": "printed",
                    "closing_qty": "printed",
                    "closing_value": "missing",
                },
            },
        }
        with patch.dict(
            os.environ, {"STOCK_VISION_CLOSING_MONEY_IN_QTY_REPAIR": "true"}, clear=False
        ):
            self.assertFalse(_repair_paisa_parked_in_qty_fields(item))
        self.assertEqual(item["sales_qty"], 167.51)
        self.assertEqual(item["closing_qty"], 11390.68)
        with patch.dict(
            os.environ, {"STOCK_VISION_PUR_FREE_SALE_CLOSING_SHIFT": "true"}, clear=False
        ):
            self.assertTrue(_repair_pur_free_sale_closing_left_shift(item))
        self.assertEqual(item["sales_qty"], 1.0)
        self.assertEqual(item["sales_value"], 167.51)
        self.assertEqual(item["closing_qty"], 68.0)
        self.assertEqual(item["closing_value"], 11390.68)


if __name__ == "__main__":
    unittest.main()
