"""Headerless Name|Pack|Opening|Receipt|Sales|Closing: keep hyphen zeros."""

from __future__ import annotations

import os
import unittest

from services.stock_vision_table import (
    _image_is_clean_text_document,
    _needs_vertical_split,
    _vision_header_cells_look_like_name_pack_four_qty,
    map_vision_table,
)


def _cells(values):
    return [{"col_index": i, "text": t} for i, t in enumerate(values)]


class TestNamePackFourQtyHeader(unittest.TestCase):
    def test_detects_hyphen_qty_header(self):
        self.assertTrue(
            _vision_header_cells_look_like_name_pack_four_qty(
                _cells(["ARJUNA TAB.", "60 'S", "-", "-", "-", "-"])
            )
        )

    def test_rejects_shifted_three_qty_row(self):
        self.assertFalse(
            _vision_header_cells_look_like_name_pack_four_qty(
                _cells(["ARJUNA TAB.", "60 'S", "24", "8", "16"])
            )
        )

    def test_map_keeps_hyphen_as_zero_and_four_qtys(self):
        table = {
            "tables": [
                {
                    "column_count": 6,
                    "header_rows": [
                        _cells(["ARJUNA TAB.", "60 'S", "-", "-", "-", "-"])
                    ],
                    "rows": [
                        {
                            "row_index": 0,
                            "cells": [
                                "BONNISAN DROP",
                                "30 ML.",
                                "24",
                                "-",
                                "8",
                                "16",
                            ],
                        },
                        {
                            "row_index": 1,
                            "cells": [
                                "BRESOL SYP.",
                                "200 ML.",
                                "15",
                                "28",
                                "8",
                                "35",
                            ],
                        },
                    ],
                }
            ]
        }
        mapped = map_vision_table(table, request_id="name-pack-4qty")
        items = mapped.get("line_items") or []
        self.assertEqual(len(items), 3)
        arjuna = next(i for i in items if "ARJUNA" in i["product_name"])
        self.assertEqual(arjuna["opening_qty"], 0.0)
        self.assertEqual(arjuna["receipts_qty"], 0.0)
        self.assertEqual(arjuna["sales_qty"], 0.0)
        self.assertEqual(arjuna["closing_qty"], 0.0)
        drop = next(i for i in items if "BONNISAN DROP" in i["product_name"])
        self.assertEqual(drop["packing"], "30 ML.")
        self.assertEqual(drop["opening_qty"], 24.0)
        self.assertEqual(drop["receipts_qty"], 0.0)
        self.assertEqual(drop["sales_qty"], 8.0)
        self.assertEqual(drop["closing_qty"], 16.0)
        bresol = next(i for i in items if "BRESOL" in i["product_name"])
        self.assertEqual(bresol["opening_qty"], 15.0)
        self.assertEqual(bresol["receipts_qty"], 28.0)
        self.assertEqual(bresol["sales_qty"], 8.0)
        self.assertEqual(bresol["closing_qty"], 35.0)

    def test_header_rows_promoted_when_body_empty(self):
        doc = {
            "tables": [
                {
                    "table_index": 0,
                    "x_range": [0, 1],
                    "column_count": 6,
                    "header_rows": [
                        _cells(["BONNISPAZ DROPS", "15 ML", "—", "—", "—", "—"]),
                        _cells(["BRESOL SYP.", "200 ML.", "15", "28", "8", "35"]),
                        _cells(["BRESOL TAB.", "60 'S", "18", "50", "8", "60"]),
                    ],
                    "rows": [],
                    "proposed_mapping": [],
                }
            ]
        }
        result = map_vision_table(doc, request_id="name-pack-promote")
        items = result["line_items"]
        by_name = {it["product_name"]: it for it in items}
        syp = by_name["BRESOL SYP."]
        self.assertEqual(syp["opening_qty"], 15.0)
        self.assertEqual(syp["receipts_qty"], 28.0)
        self.assertEqual(syp["sales_qty"], 8.0)
        self.assertEqual(syp["closing_qty"], 35.0)
        tab = by_name["BRESOL TAB."]
        self.assertEqual(tab["opening_qty"], 18.0)
        self.assertEqual(tab["closing_qty"], 60.0)

    def test_phantom_columns_keep_closing(self):
        doc = {
            "tables": [
                {
                    "table_index": 0,
                    "x_range": [0, 1],
                    "column_count": 9,
                    "header_rows": [
                        _cells(
                            [
                                "HIMPLASIA TAB.",
                                "30 'S",
                                "",
                                "35",
                                "",
                                "7",
                                "",
                                "28",
                                "",
                            ]
                        )
                    ],
                    "rows": [
                        {
                            "row_index": 0,
                            "cells": [
                                "HIMPLASIA TAB.",
                                "30 'S",
                                None,
                                "35",
                                None,
                                "7",
                                None,
                                "28",
                                None,
                            ],
                            "is_total_row": False,
                        },
                        {
                            "row_index": 1,
                            "cells": [
                                "STYPLON TAB.",
                                "30 'S",
                                None,
                                "11",
                                "50",
                                "7",
                                None,
                                "54",
                                None,
                            ],
                            "is_total_row": False,
                        },
                        {
                            "row_index": 2,
                            "cells": [
                                "VGEL",
                                "30 G.",
                                None,
                                "3",
                                "25",
                                "6",
                                None,
                                "22",
                                None,
                            ],
                            "is_total_row": False,
                        },
                        {
                            "row_index": 3,
                            "cells": [
                                "HIORA GA GEL",
                                "15 ML.",
                                None,
                                "7",
                                "50",
                                "8",
                                "2",
                                "49",
                                None,
                            ],
                            "is_total_row": False,
                        },
                    ],
                    "proposed_mapping": [],
                }
            ]
        }
        result = map_vision_table(doc, request_id="name-pack-phantom")
        by_name = {it["product_name"]: it for it in result["line_items"]}
        self.assertEqual(by_name["HIMPLASIA TAB."]["opening_qty"], 35.0)
        self.assertEqual(by_name["HIMPLASIA TAB."]["sales_qty"], 7.0)
        self.assertEqual(by_name["HIMPLASIA TAB."]["closing_qty"], 28.0)
        self.assertEqual(by_name["STYPLON TAB."]["receipts_qty"], 50.0)
        self.assertEqual(by_name["STYPLON TAB."]["closing_qty"], 54.0)
        self.assertEqual(by_name["VGEL"]["closing_qty"], 22.0)
        self.assertEqual(by_name["HIORA GA GEL"]["opening_qty"], 7.0)
        self.assertEqual(by_name["HIORA GA GEL"]["receipts_qty"], 50.0)
        self.assertEqual(by_name["HIORA GA GEL"]["sales_qty"], 8.0)
        self.assertEqual(by_name["HIORA GA GEL"]["closing_qty"], 49.0)

    def test_bare_total_qty_stays_out_of_value(self):
        from services.stock_vision_table import _remap_total_values_money_parked_in_qty

        row = {
            "product_name": "TOTAL",
            "opening_qty": 197727,
            "receipts_qty": 78042,
            "sales_qty": 63751,
            "closing_qty": 212018,
        }
        _remap_total_values_money_parked_in_qty(row)
        self.assertEqual(row["sales_qty"], 63751)
        self.assertIsNone(row.get("sales_value"))
        self.assertEqual(row["closing_qty"], 212018)


class TestCleanTextDocumentSplit(unittest.TestCase):
    def test_white_text_page_splits_phone_portrait_photo_does_not(self):
        import io

        from PIL import Image

        def jpeg(bg, ink):
            im = Image.new("L", (2000, 3200), bg)
            px = im.load()
            for y in range(80, 3100, 18):
                for x in range(40, 900, 7):
                    px[x, y] = ink
            buf = io.BytesIO()
            im.convert("RGB").save(buf, format="JPEG", quality=85)
            return buf.getvalue()

        page = jpeg(255, 10)
        photo = jpeg(30, 200)
        self.assertTrue(_image_is_clean_text_document(page))
        self.assertFalse(_image_is_clean_text_document(photo))
        self.assertTrue(_needs_vertical_split(page))
        self.assertFalse(_needs_vertical_split(photo))

    @unittest.skipUnless(
        os.path.isfile(r"C:\Users\adity\Downloads\132.jpg"),
        "132.jpg absent",
    )
    def test_132_upscaled_page_needs_split(self):
        from services.stock_vision_table import _upscale_phone_wide_qty_grid_image

        with open(r"C:\Users\adity\Downloads\132.jpg", "rb") as fh:
            raw = fh.read()
        up = _upscale_phone_wide_qty_grid_image(raw)
        self.assertIs(up, raw)
        self.assertTrue(_image_is_clean_text_document(raw))
        self.assertTrue(_needs_vertical_split(raw))


if __name__ == "__main__":
    unittest.main()
