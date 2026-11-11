"""STOCK & SALES ANALYSIS: OPENING|RECEIPT|ISSUE|CLOSING × QTY|VALUE header fanout.

Vision often emits a flat dual header where parent bands restart the child
QTY/VALUE row at col_index 1 (not 0). Without split+fanout, CLOSING lands on
ignore and numbers shift to the wrong end of the row.
"""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from services.stock_header_resolver import resolve_columns
from services.stock_vision_table import (
    _flatten_header_cells,
    _normalize_header_rows,
)


def _flat_truncated_parent_child_headers():
    return [
        {"text": "ITEM DESCRIPTION", "col_index": 0},
        {"text": "OPENIN", "col_index": 1},
        {"text": "RECEI", "col_index": 2},
        {"text": "ISSU", "col_index": 3},
        {"text": "CLOSI", "col_index": 4},
        {"text": "QTY.", "col_index": 1},
        {"text": "VALUE", "col_index": 2},
        {"text": "QTY.", "col_index": 3},
        {"text": "VALUE", "col_index": 4},
        {"text": "QTY.", "col_index": 5},
        {"text": "VALUE", "col_index": 6},
        {"text": "QTY.", "col_index": 7},
        {"text": "VALUE", "col_index": 8},
    ]


class SsaGroupQtyValueFanoutTests(unittest.TestCase):
    def test_flat_header_splits_when_child_row_starts_at_one(self):
        rows = _normalize_header_rows(_flat_truncated_parent_child_headers(), {})
        self.assertEqual(len(rows), 2)
        self.assertEqual(
            [c["text"] for c in rows[0]],
            ["ITEM DESCRIPTION", "OPENIN", "RECEI", "ISSU", "CLOSI"],
        )
        self.assertEqual(rows[1][0]["col_index"], 1)
        self.assertEqual(rows[1][0]["text"], "QTY.")

    def test_fanout_keeps_item_description_on_child_row(self):
        """Parents on row1 (OPENING@1); ITEM DESCRIPTION stays on child row0."""
        from services.stock_vision_table import _try_fanout_group_qty_value_header_rows

        rows = [
            [
                {"col_index": 0, "text": "ITEM DESCRIPTION"},
                {"col_index": 1, "text": "QTY."},
                {"col_index": 2, "text": "VALUE"},
                {"col_index": 3, "text": "QTY."},
                {"col_index": 4, "text": "VALUE"},
                {"col_index": 5, "text": "QTY."},
                {"col_index": 6, "text": "VALUE"},
                {"col_index": 7, "text": "QTY."},
                {"col_index": 8, "text": "VALUE"},
                {"col_index": 9, "text": "QTY."},
                {"col_index": 10, "text": "VALUE"},
            ],
            [
                {"col_index": 1, "text": "OPENING"},
                {"col_index": 3, "text": "RECEIPT"},
                {"col_index": 5, "text": "ISSUE"},
                {"col_index": 7, "text": "CLOSING"},
                {"col_index": 9, "text": "DUMP"},
            ],
        ]
        with patch.dict(
            os.environ,
            {
                "STOCK_VISION_GROUP_QTY_VALUE_FANOUT": "true",
                "STOCK_VISION_FANOUT_CHILD_LEADING": "true",
            },
            clear=False,
        ):
            fanned = _try_fanout_group_qty_value_header_rows(rows)
        self.assertIsNotNone(fanned)
        self.assertEqual(fanned[0]["text"], "ITEM DESCRIPTION")
        self.assertEqual(fanned[1]["text"], "QTY.")
        self.assertEqual(fanned[1].get("subheader_text"), "OPENING")
        resolved = resolve_columns(fanned)
        by = {c["col_index"]: c["canonical"] for c in resolved["columns"]}
        self.assertEqual(by[0], "product_name")
        self.assertEqual(by[1], "opening_qty")
        self.assertEqual(by[8], "closing_value")

    def test_fanout_maps_closing_qty_and_value(self):
        with patch.dict(
            os.environ, {"STOCK_VISION_GROUP_QTY_VALUE_FANOUT": "true"}, clear=False
        ):
            rows = _normalize_header_rows(_flat_truncated_parent_child_headers(), {})
            cells = _flatten_header_cells(rows, 9)
        resolved = resolve_columns(cells)
        by = {c["col_index"]: c["canonical"] for c in resolved["columns"]}
        self.assertEqual(by[0], "product_name")
        self.assertEqual(by[1], "opening_qty")
        self.assertEqual(by[2], "opening_value")
        self.assertEqual(by[3], "purchase_qty")
        self.assertEqual(by[4], "purchase_value")
        self.assertEqual(by[5], "sales_qty")
        self.assertEqual(by[6], "sales_value")
        self.assertEqual(by[7], "closing_qty")
        self.assertEqual(by[8], "closing_value")

    def test_fanout_off_does_not_assign_full_oric_pair_grid(self):
        with patch.dict(
            os.environ, {"STOCK_VISION_GROUP_QTY_VALUE_FANOUT": "false"}, clear=False
        ):
            rows = _normalize_header_rows(_flat_truncated_parent_child_headers(), {})
            cells = _flatten_header_cells(rows, 9)
        resolved = resolve_columns(cells)
        canons = [c["canonical"] for c in resolved["columns"]]
        self.assertNotEqual(
            canons,
            [
                "product_name",
                "opening_qty",
                "opening_value",
                "purchase_qty",
                "purchase_value",
                "sales_qty",
                "sales_value",
                "closing_qty",
                "closing_value",
            ],
        )

    def test_bare_qty_value_headers_layout_assigns_closing(self):
        """Child-only QTY/VALUE row (parents dropped) still maps ORIC pairs."""
        from services.stock_vision_table import _apply_cell_layout_qty_value_repair

        columns = [
            {
                "col_index": i,
                "header_text": h,
                "canonical": "product_name" if i == 0 else "ignore",
                "confidence": 1.0 if i == 0 else 0.0,
                "is_value": False,
                "reason": "alias" if i == 0 else "unknown",
            }
            for i, h in enumerate(
                [
                    "ITEM DESCRIPTION",
                    "QTY.",
                    "VALUE",
                    "QTY.",
                    "VALUE",
                    "QTY.",
                    "VALUE",
                    "QTY.",
                    "VALUE",
                ]
            )
        ]
        rows = [
            {
                "cells": [
                    "AACTARIL SOAP",
                    "-11",
                    "-793.43",
                    "-",
                    "0.00",
                    "-",
                    "0.00",
                    "-11",
                    "-793.43",
                ]
            },
            {
                "cells": [
                    "CONFIDO TAB",
                    "45",
                    "6667.92",
                    "0",
                    "0.00",
                    "11",
                    "1778.11",
                    "34",
                    "5044.00",
                ]
            },
        ]
        errors: list = []
        with patch.dict(
            os.environ,
            {
                "STOCK_VISION_GROUP_QTY_VALUE_FANOUT": "true",
                "STOCK_VISION_CELL_LAYOUT_REPAIR": "false",
            },
            clear=False,
        ):
            repaired = _apply_cell_layout_qty_value_repair(
                columns, rows, 9, errors, force=False
            )
        by = {c["col_index"]: c["canonical"] for c in repaired}
        self.assertEqual(by[1], "opening_qty")
        self.assertEqual(by[7], "closing_qty")
        self.assertEqual(by[8], "closing_value")
        self.assertIn("CELL_LAYOUT_QTY_VALUE_REPAIR", errors)

    def test_exif_default_transposes_phone_portrait_landscape_buffer(self):
        """Landscape buffer + Orientation=6 must upright (CLOSING at right end)."""
        import io

        from PIL import Image

        from services.stock_vision_table import _exif_orient_image_bytes

        im = Image.new("RGB", (800, 600), color=(255, 255, 255))
        exif = im.getexif()
        exif[274] = 6
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=90, exif=exif.tobytes())
        raw = buf.getvalue()
        with patch.dict(
            os.environ,
            {
                "STOCK_VISION_EXIF_TRANSPOSE": "true",
                "STOCK_VISION_EXIF_SKIP_LANDSCAPE_BUFFER": "false",
            },
            clear=False,
        ):
            out = _exif_orient_image_bytes(raw)
        with Image.open(io.BytesIO(out)) as oriented:
            self.assertEqual(oriented.size, (600, 800))

    def test_pack_peel_six_cells_pads_issue_so_closing_stays_closing(self):
        """open|mid|close (open=close) → pad blank ISSUE; no sales↔closing shuffle."""
        from services.stock_vision_table import _align_oric_qv_cells_after_pack_peel

        columns = [
            {"col_index": i, "canonical": c}
            for i, c in enumerate(
                [
                    "product_name",
                    "opening_qty",
                    "opening_value",
                    "purchase_qty",
                    "purchase_value",
                    "sales_qty",
                    "sales_value",
                    "closing_qty",
                    "closing_value",
                ]
            )
        ]
        # After pack peel: open, receipt 0/0, closing (issue dropped).
        cells = [
            "AACTARIL SOAP",
            "-11",
            "-793.43",
            "0.00",
            "0.00",
            "-11",
            "-793.43",
            None,
            None,
        ]
        with patch.dict(
            os.environ, {"STOCK_VISION_ORIC_QV_PACK_CELL_ALIGN": "true"}, clear=False
        ):
            out, width = _align_oric_qv_cells_after_pack_peel(
                cells, columns, 9, peeled_pack=True
            )
        self.assertEqual(width, 9)
        self.assertEqual(out[1], "-11")
        self.assertEqual(out[2], "-793.43")
        self.assertEqual(out[3], "0.00")
        self.assertEqual(out[4], "0.00")
        self.assertIsNone(out[5])
        self.assertIsNone(out[6])
        self.assertEqual(out[7], "-11")
        self.assertEqual(out[8], "-793.43")

    def test_pack_peel_seven_cells_pads_blank_receipt_value(self):
        """Blank receipt VALUE dropped → insert null so ISSUE/CLOSING stay put."""
        from services.stock_vision_table import _align_oric_qv_cells_after_pack_peel

        columns = [
            {"col_index": i, "canonical": c}
            for i, c in enumerate(
                [
                    "product_name",
                    "opening_qty",
                    "opening_value",
                    "purchase_qty",
                    "purchase_value",
                    "sales_qty",
                    "sales_value",
                    "closing_qty",
                    "closing_value",
                ]
            )
        ]
        cells = [
            "CONFIDO TAB",
            "45",
            "6667.92",
            "0.00",
            "11",
            "1778.11",
            "34",
            "5037.98",
            None,
        ]
        with patch.dict(
            os.environ, {"STOCK_VISION_ORIC_QV_PACK_CELL_ALIGN": "true"}, clear=False
        ):
            out, _width = _align_oric_qv_cells_after_pack_peel(
                cells, columns, 9, peeled_pack=True
            )
        self.assertEqual(out[3], "0.00")
        self.assertIsNone(out[4])
        self.assertEqual(out[5], "11")
        self.assertEqual(out[6], "1778.11")
        self.assertEqual(out[7], "34")
        self.assertEqual(out[8], "5037.98")

    def test_pack_peel_six_cells_sellthrough_pads_receipt_value(self):
        """ABANA: open|rq|iq|iv|cq with blank rv — ISSUE/CLOSING must not slide left."""
        from services.stock_vision_table import _align_oric_qv_cells_after_pack_peel

        columns = [
            {"col_index": i, "canonical": c}
            for i, c in enumerate(
                [
                    "product_name",
                    "opening_qty",
                    "opening_value",
                    "purchase_qty",
                    "purchase_value",
                    "sales_qty",
                    "sales_value",
                    "closing_qty",
                    "closing_value",
                ]
            )
        ]
        cells = [
            "ABANA TAB",
            "29",
            "4309.40",
            "0.00",
            "29",
            "4309.40",
            "0.00",
            None,
            None,
        ]
        with patch.dict(
            os.environ, {"STOCK_VISION_ORIC_QV_PACK_CELL_ALIGN": "true"}, clear=False
        ):
            out, width = _align_oric_qv_cells_after_pack_peel(
                cells, columns, 8, peeled_pack=True
            )
        self.assertEqual(width, 9)
        self.assertEqual(out[5], "29")
        self.assertEqual(out[6], "4309.40")
        self.assertEqual(out[7], "0.00")
        self.assertIsNone(out[4])

    def test_pack_peel_six_cells_negative_open_close_restores_closing(self):
        """Negative open=close: duplicate pair under ISSUE → closing, not sales."""
        from services.stock_vision_table import _align_oric_qv_cells_after_pack_peel

        columns = [
            {"col_index": i, "canonical": c}
            for i, c in enumerate(
                [
                    "product_name",
                    "opening_qty",
                    "opening_value",
                    "purchase_qty",
                    "purchase_value",
                    "sales_qty",
                    "sales_value",
                    "closing_qty",
                    "closing_value",
                ]
            )
        ]
        cells = [
            "CLARINA FACE",
            "-3",
            "-173.52",
            "0.00",
            "-3",
            "-173.52",
            "0.00",
            None,
            None,
        ]
        with patch.dict(
            os.environ, {"STOCK_VISION_ORIC_QV_PACK_CELL_ALIGN": "true"}, clear=False
        ):
            out, _width = _align_oric_qv_cells_after_pack_peel(
                cells, columns, 8, peeled_pack=True
            )
        self.assertIsNone(out[5])
        self.assertIsNone(out[6])
        self.assertEqual(out[7], "-3")
        self.assertEqual(out[8], "-173.52")

    def test_pack_peel_five_cells_dash_open_and_blank_issue_qty(self):
        """HIMALAYA ZEAL-style: open dash omitted + ISSUE qty dash → rq|rv|iv|cq|cv."""
        from services.stock_vision_table import _align_oric_qv_cells_after_pack_peel

        columns = [
            {"col_index": i, "canonical": c}
            for i, c in enumerate(
                [
                    "product_name",
                    "opening_qty",
                    "opening_value",
                    "purchase_qty",
                    "purchase_value",
                    "sales_qty",
                    "sales_value",
                    "closing_qty",
                    "closing_value",
                ]
            )
        ]
        # After pack peel Vision dropped opening 0.00 and left ISSUE qty null.
        cells = [
            "HIMALAYA ZEAL",
            None,
            "72",
            "5313.31",
            None,
            "0.00",
            "72",
            "5578.97",
            None,
        ]
        with patch.dict(
            os.environ, {"STOCK_VISION_ORIC_QV_PACK_CELL_ALIGN": "true"}, clear=False
        ):
            out, width = _align_oric_qv_cells_after_pack_peel(
                cells, columns, 9, peeled_pack=True
            )
        self.assertEqual(width, 9)
        self.assertIsNone(out[1])
        self.assertIsNone(out[2])
        self.assertEqual(out[3], "72")
        self.assertEqual(out[4], "5313.31")
        self.assertIsNone(out[5])
        self.assertEqual(out[6], "0.00")
        self.assertEqual(out[7], "72")
        self.assertEqual(out[8], "5578.97")

    def test_pack_peel_six_cells_dash_open_qty_blank_issue_qty(self):
        """Dash OPENING qty with printed open VALUE; blank ISSUE qty."""
        from services.stock_vision_table import _align_oric_qv_cells_after_pack_peel

        columns = [
            {"col_index": i, "canonical": c}
            for i, c in enumerate(
                [
                    "product_name",
                    "opening_qty",
                    "opening_value",
                    "purchase_qty",
                    "purchase_value",
                    "sales_qty",
                    "sales_value",
                    "closing_qty",
                    "closing_value",
                ]
            )
        ]
        cells = [
            "PRODUCT X",
            None,
            "0.00",
            "72",
            "5313.31",
            "0.00",
            "72",
            "5578.97",
            None,
        ]
        with patch.dict(
            os.environ, {"STOCK_VISION_ORIC_QV_PACK_CELL_ALIGN": "true"}, clear=False
        ):
            out, _width = _align_oric_qv_cells_after_pack_peel(
                cells, columns, 9, peeled_pack=True
            )
        self.assertIsNone(out[1])
        self.assertEqual(out[2], "0.00")
        self.assertEqual(out[3], "72")
        self.assertEqual(out[4], "5313.31")
        self.assertIsNone(out[5])
        self.assertEqual(out[6], "0.00")
        self.assertEqual(out[7], "72")
        self.assertEqual(out[8], "5578.97")

    def test_ocr_gh_pack_peels_so_opening_stays_opening(self):
        """Phone OCR '30GH' (GM) must peel like 30GM on ORIC qty|value grids."""
        from services.stock_vision_table import (
            _classify_layout_cell,
            _extract_unlabeled_pack_token,
        )

        self.assertEqual(_classify_layout_cell("30GH"), "pack")
        columns = [
            {"col_index": i, "canonical": c}
            for i, c in enumerate(
                [
                    "product_name",
                    "opening_qty",
                    "opening_value",
                    "purchase_qty",
                    "purchase_value",
                    "sales_qty",
                    "sales_value",
                    "closing_qty",
                    "closing_value",
                    "ignore",
                ]
            )
        ]
        cells = [
            "ALITHEA LOTION",
            "30GH",
            "33",
            "5713.82",
            None,
            "0.00",
            "1",
            "197.89",
            "32",
            "5540.68",
        ]
        out, tok = _extract_unlabeled_pack_token(cells, columns, 10, inplace=False)
        self.assertEqual(tok, "30GH")
        self.assertEqual(out[1], "33")
        self.assertEqual(out[2], "5713.82")
        self.assertEqual(out[7], "32")
        self.assertEqual(out[8], "5540.68")

    def test_pack_peel_seven_cells_leading_money_pads_missing_open_qty(self):
        """After pack peel, ov|rq|rv|iq|iv|cq|cv must not park money in opening_qty."""
        from services.stock_vision_table import _align_oric_qv_cells_after_pack_peel

        columns = [
            {"col_index": i, "canonical": c}
            for i, c in enumerate(
                [
                    "product_name",
                    "opening_qty",
                    "opening_value",
                    "purchase_qty",
                    "purchase_value",
                    "sales_qty",
                    "sales_value",
                    "closing_qty",
                    "closing_value",
                ]
            )
        ]
        cells = [
            "CLARINA F/W",
            "6184.78",
            "300",
            "49085.55",
            "96",
            "15717.04",
            "240",
            "41231.88",
            None,
        ]
        with patch.dict(
            os.environ, {"STOCK_VISION_ORIC_QV_PACK_CELL_ALIGN": "true"}, clear=False
        ):
            out, _width = _align_oric_qv_cells_after_pack_peel(
                cells, columns, 9, peeled_pack=True
            )
        self.assertIsNone(out[1])
        self.assertEqual(out[2], "6184.78")
        self.assertEqual(out[3], "300")
        self.assertEqual(out[5], "96")
        self.assertEqual(out[7], "240")
        self.assertEqual(out[8], "41231.88")

    def test_himalaya_zeal_sku_with_qty_not_dropped_as_banner(self):
        """Bare HIMALAYA ZEAL with printed movement is a SKU, not a division banner."""
        from services.sales_statement_extractor import _product_wise_drop_banner_items

        result = {
            "line_items": [
                {
                    "product_name": "HIMALAYA ZEAL",
                    "receipts_qty": 72.0,
                    "closing_qty": 72.0,
                    "closing_value": 5578.97,
                    "extra": {
                        "field_source": {
                            "purchase_qty": "printed",
                            "closing_qty": "printed",
                            "closing_value": "printed",
                        },
                        "purchase_value": 5313.31,
                    },
                },
                {
                    "product_name": "HIMALAYA DRUG (ZEAL)--(INZMAM)",
                    "opening_qty": 0.0,
                    "extra": {"field_source": {}},
                },
            ]
        }
        with patch.dict(
            os.environ, {"STOCK_KEEP_HIMALAYA_ZEAL_SKU": "true"}, clear=False
        ):
            out = _product_wise_drop_banner_items(result)
        names = [i.get("product_name") for i in out.get("line_items") or []]
        self.assertEqual(names, ["HIMALAYA ZEAL"])

    def test_keep_oric_qty_value_skips_missing_value_fallback(self):
        """ORIC money hits must keep Vision even when MISSING_VALUE ratio is high."""
        from services.stock_vision_table import _vision_table_quality_fallback_reason

        result = {
            "line_items": [
                {
                    "product_name": "A",
                    "opening_qty": 1.0,
                    "closing_value": 10.5,
                    "extra": {
                        "field_source": {
                            "opening_qty": "printed",
                            "closing_value": "printed",
                        }
                    },
                },
                {
                    "product_name": "B",
                    "opening_qty": 2.0,
                    "closing_value": 20.5,
                    "extra": {
                        "field_source": {
                            "opening_qty": "printed",
                            "closing_value": "printed",
                        }
                    },
                },
                {
                    "product_name": "C",
                    "opening_qty": 3.0,
                    "sales_value": 30.5,
                    "extra": {
                        "field_source": {
                            "opening_qty": "printed",
                            "sales_value": "printed",
                        }
                    },
                },
                {
                    "product_name": "D",
                    "extra": {
                        "field_source": {
                            "opening_qty": "missing",
                            "closing_qty": "missing",
                        }
                    },
                },
                {
                    "product_name": "E",
                    "extra": {
                        "field_source": {
                            "opening_qty": "missing",
                            "closing_qty": "missing",
                        }
                    },
                },
                {
                    "product_name": "F",
                    "extra": {
                        "field_source": {
                            "opening_qty": "missing",
                            "closing_qty": "missing",
                        }
                    },
                },
            ],
            "totals": {
                "extra": {
                    "valid_ratio": 0.2,
                    "row_status_counts": {"MISSING_VALUE": 4, "VALID": 2},
                    "column_map": [
                        {"canonical": c}
                        for c in (
                            "product_name",
                            "opening_qty",
                            "opening_value",
                            "purchase_qty",
                            "purchase_value",
                            "sales_qty",
                            "sales_value",
                            "closing_qty",
                            "closing_value",
                        )
                    ],
                }
            },
        }
        with patch.dict(
            os.environ, {"STOCK_VISION_KEEP_ORIC_QTY_VALUE": "true"}, clear=False
        ):
            self.assertIsNone(_vision_table_quality_fallback_reason(result))

    def test_oric_qv_reread_does_not_drop_printed_closing(self):
        """Recovery crop that loses CLOSING must not replace the first pass."""
        from services.stock_row_classifier import RowStatus
        from services.stock_vision_table import merge_reread

        first = {
            "product_name": "CONFIDO TAB",
            "opening_qty": 10.0,
            "receipts_qty": 150.0,
            "sales_qty": 71.0,
            "closing_qty": 89.0,
            "closing_value": 15168.68,
            "sales_value": 13144.57,
            "extra": {
                "field_source": {
                    "opening_qty": "printed",
                    "purchase_qty": "printed",
                    "sales_qty": "printed",
                    "closing_qty": "printed",
                    "closing_value": "printed",
                    "sales_value": "printed",
                },
                "vision_column_headers": [
                    "ITEM",
                    "QTY.",
                    "VALUE",
                    "QTY.",
                    "VALUE",
                    "QTY.",
                    "VALUE",
                    "QTY.",
                    "VALUE",
                ],
                "purchase_value": 27429.82,
            },
        }
        reread = {
            "product_name": "CONFIDO TAB",
            "opening_qty": 10.0,
            "receipts_qty": 150.0,
            "sales_qty": 71.0,
            "closing_qty": None,
            "closing_value": 0.0,
            "sales_value": 13144.57,
            "extra": {
                "field_source": {
                    "opening_qty": "printed",
                    "purchase_qty": "printed",
                    "sales_qty": "printed",
                    "closing_qty": "missing",
                    "closing_value": "missing",
                    "sales_value": "printed",
                },
                "vision_column_headers": first["extra"]["vision_column_headers"],
                "purchase_value": 27429.82,
                "vision_row_index": 7,
            },
        }
        with patch.dict(
            os.environ,
            {"STOCK_VISION_ORIC_QV_KEEP_PRINTED_CLOSING": "true"},
            clear=False,
        ):
            with patch(
                "services.stock_vision_table.classify_row",
                return_value=(RowStatus.CLOSING_DERIVED, {"derived_closing": 79.0}),
            ):
                out = merge_reread([first], [reread])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].get("closing_qty"), 89.0)
        self.assertEqual(out[0].get("closing_value"), 15168.68)

    def test_rate_opening_glue_peels_secondaryrate_opening(self):
        """Secondaryrate '176 51 38' + Opening '0.00' → open=38, primary=0, close=30."""
        from services.stock_vision_table import (
            _repair_cells_rate_opening_glue,
            _split_rate_opening_glued_token,
            map_vision_table,
        )

        self.assertEqual(
            _split_rate_opening_glued_token("176 51 38"), ("176.51", "38")
        )
        self.assertEqual(
            _split_rate_opening_glued_token("101.31 0"), ("101.31", "0")
        )
        self.assertEqual(
            _split_rate_opening_glued_token("79.02 49"), ("79.02", "49")
        )
        columns = [
            {"col_index": i, "canonical": c, "header_text": h}
            for i, (c, h) in enumerate(
                [
                    ("ignore", "Customer name"),
                    ("product_code", "Material code"),
                    ("product_name", "Material name"),
                    ("rate", "Mrp"),
                    ("ignore", "Secondaryrate"),
                    ("opening_qty", "Opening balqty"),
                    ("purchase_qty", "Primary qty"),
                    ("closing_qty", "Closing balqty"),
                ]
            )
        ]
        cells = [
            "ANIL MEDICAL AGENCY",
            "7002304",
            "ASHWAGANDHA TABLETS 60 S INDIA",
            "260",
            "176 51 38",
            "0.00",
            "30",
            "30",
        ]
        with patch.dict(
            os.environ, {"STOCK_VISION_RATE_OPENING_GLUE": "true"}, clear=False
        ):
            out, width = _repair_cells_rate_opening_glue(cells, columns, 8)
        self.assertEqual(width, 8)
        self.assertEqual(out[4], "176.51")
        self.assertEqual(out[5], "38")
        self.assertEqual(out[6], "0.00")
        self.assertEqual(out[7], "30")

        headers = [
            {"col_index": i, "text": t}
            for i, t in enumerate(
                [
                    "Customer name",
                    "Material code",
                    "Material name",
                    "Mrp",
                    "Secondaryrate",
                    "Opening balqty",
                    "Primary qty",
                    "Closing balqty",
                ]
            )
        ]
        table = {
            "tables": [
                {
                    "column_count": 8,
                    "header_rows": [headers],
                    "rows": [
                        {
                            "row_index": 0,
                            "cells": [
                                "ANIL MEDICAL AGENCY",
                                "7002304",
                                "ASHWAGANDHA TABLETS 60 S INDIA",
                                "260",
                                "176 51 38",
                                "0.00",
                                "30",
                                "30",
                            ],
                        },
                        {
                            "row_index": 1,
                            "cells": [
                                "ANIL MEDICAL AGENCY",
                                "7000143",
                                "GASEX SYRUP ELAICHI FLVR 200ml",
                                "150",
                                "101.31 0",
                                "28 00",
                                "20",
                                "20",
                            ],
                        },
                    ],
                }
            ]
        }
        with patch.dict(
            os.environ,
            {
                "STOCK_VISION_RATE_OPENING_GLUE": "true",
            },
            clear=False,
        ):
            mapped = map_vision_table(table, request_id="rate-open-glue")
        items = mapped.get("line_items") or []
        self.assertEqual(len(items), 2)
        ash = items[0]
        self.assertEqual(ash.get("opening_qty"), 38.0)
        self.assertEqual(ash.get("receipts_qty"), 0.0)
        self.assertEqual(ash.get("closing_qty"), 30.0)
        gas = items[1]
        self.assertEqual(gas.get("opening_qty"), 0.0)
        self.assertEqual(gas.get("receipts_qty"), 28.0)
        self.assertEqual(gas.get("closing_qty"), 20.0)

    def test_map_vision_closing_value_glued_to_expiry_date(self):
        """CLOSING VALUE cell '1904.62 01-Dec-28' must parse money, not go missing."""
        from services.stock_vision_table import map_vision_table

        headers = [
            {"col_index": i, "text": t}
            for i, t in enumerate(
                [
                    "PRODUCT DESCRIPTION",
                    "OPENING STOCK",
                    "OPENING VALUE",
                    "RECEIVE QUANTITY",
                    "RECEIVE VALUE",
                    "ISSUE QUANTITY",
                    "ISSUE VALUE",
                    "CLOSING STOCK",
                    "CLOSING VALUE",
                    "EXPIRY STOCK",
                ]
            )
        ]
        table = {
            "tables": [
                {
                    "column_count": 10,
                    "header_rows": [headers],
                    "rows": [
                        {
                            "row_index": 0,
                            "cells": [
                                "BLEMINOR CREAM 30ML",
                                "5",
                                "865.73",
                                "10",
                                "1649.01",
                                "4",
                                "760.29",
                                "11",
                                "1904.62 01-Dec-28",
                                None,
                            ],
                        },
                        {
                            "row_index": 1,
                            "cells": [
                                "CLARINA ANTI ACNE CREAM 30GM",
                                "11",
                                "1422.9",
                                "0",
                                "0",
                                "0",
                                "0",
                                "11",
                                "1422.9 01-Nov-28",
                                None,
                            ],
                        },
                    ],
                }
            ]
        }
        with patch.dict(
            os.environ, {"STOCK_PARSE_PEEL_TRAILING_EXPIRY": "true"}, clear=False
        ):
            mapped = map_vision_table(table, request_id="cv-expiry-glue")
        items = mapped.get("line_items") or []
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0].get("closing_qty"), 11.0)
        self.assertEqual(items[0].get("closing_value"), 1904.62)
        self.assertEqual(items[1].get("closing_qty"), 11.0)
        self.assertEqual(items[1].get("closing_value"), 1422.9)

    def test_pcs_oric_null_pad_densifies_arctic_row(self):
        """Phone ORIC: UNIT/PCS + null between each printed digit → re-slot money."""
        from services.stock_vision_table import _repair_pcs_unit_oric_null_pad

        item = {
            "product_name": "ARCTIC BAR SOAP 75GM",
            "opening_qty": 148.0,
            "opening_value": 0.0,
            "receipts_qty": 0.0,
            "sales_qty": 21.0,
            "sales_value": 0.0,
            "closing_qty": 0.0,
            "closing_value": 0.0,
            "packing": "PCS",
            "extra": {
                "purchase_value": None,
                "field_source": {
                    "opening_qty": "printed",
                    "opening_value": "missing",
                    "purchase_qty": "printed",
                    "purchase_value": "missing",
                    "sales_qty": "printed",
                    "sales_value": "missing",
                    "closing_qty": "missing",
                    "closing_value": "missing",
                    "pack": "printed",
                },
                "physical_cells": {
                    "0": "ARCTIC BAR SOAP 75GM",
                    "1": "PCS",
                    "2": "148",
                    "3": None,
                    "4": "10921.87",
                    "5": None,
                    "6": "0",
                    "7": None,
                    "8": "0",
                    "9": None,
                    "10": "21",
                    "11": None,
                    "12": "3476.70",
                    "13": None,
                    "14": "158",
                    "15": None,
                    "16": "10185.85",
                    "17": None,
                    "18": "01-Jan",
                },
            },
        }
        with patch.dict(
            os.environ, {"STOCK_VISION_PCS_ORIC_NULL_PAD": "true"}, clear=False
        ):
            self.assertTrue(_repair_pcs_unit_oric_null_pad(item))
        self.assertEqual(item["opening_qty"], 148.0)
        self.assertEqual(item["opening_value"], 10921.87)
        self.assertEqual(item["receipts_qty"], 0.0)
        self.assertEqual(item["extra"]["purchase_value"], 0.0)
        self.assertEqual(item["sales_qty"], 21.0)
        self.assertEqual(item["sales_value"], 3476.70)
        self.assertEqual(item["closing_qty"], 158.0)
        self.assertEqual(item["closing_value"], 10185.85)
        self.assertEqual(item["extra"]["expiry_stock"], "01-Jan")
        self.assertIn("PCS_ORIC_NULL_PAD", item["extra"]["flags"])

    def test_pcs_oric_null_pad_skips_when_values_already_printed(self):
        from services.stock_vision_table import _repair_pcs_unit_oric_null_pad

        item = {
            "product_name": "ARCTIC BAR SOAP 75GM",
            "opening_qty": 148.0,
            "opening_value": 10921.87,
            "receipts_qty": 0.0,
            "sales_qty": 21.0,
            "sales_value": 3476.70,
            "closing_qty": 158.0,
            "closing_value": 10185.85,
            "extra": {
                "purchase_value": 0.0,
                "field_source": {
                    "opening_value": "printed",
                    "sales_value": "printed",
                    "closing_value": "printed",
                    "purchase_value": "printed",
                },
                "physical_cells": {
                    "0": "ARCTIC BAR SOAP 75GM",
                    "1": "PCS",
                    "2": "148",
                    "3": None,
                    "4": "10921.87",
                    "5": None,
                    "6": "0",
                    "7": None,
                    "8": "0",
                    "9": None,
                    "10": "21",
                    "11": None,
                    "12": "3476.70",
                    "13": None,
                    "14": "158",
                    "15": None,
                    "16": "10185.85",
                    "17": None,
                    "18": "01-Jan",
                },
            },
        }
        with patch.dict(
            os.environ, {"STOCK_VISION_PCS_ORIC_NULL_PAD": "true"}, clear=False
        ):
            self.assertFalse(_repair_pcs_unit_oric_null_pad(item))

    def test_pcs_oric_null_pad_six_nums_when_closing_unread(self):
        """EXP parked where CLOSING should be → keep oq|ov|rq|rv|iq|iv only."""
        from services.stock_vision_table import _repair_pcs_unit_oric_null_pad

        item = {
            "product_name": "CLARINA ANTI-ACNE CREAM 50GM",
            "opening_qty": 71.0,
            "opening_value": 0.0,
            "receipts_qty": 44.0,
            "sales_qty": 190.0,
            "sales_value": 0.0,
            "closing_qty": None,
            "closing_value": 0.0,
            "packing": "PCS",
            "extra": {
                "field_source": {
                    "opening_qty": "printed",
                    "opening_value": "missing",
                    "purchase_qty": "printed",
                    "sales_qty": "printed",
                    "sales_value": "missing",
                    "closing_qty": "missing",
                    "closing_value": "missing",
                },
                "physical_cells": {
                    "0": "CLARINA ANTI-ACNE CREAM 50GM",
                    "1": "PCS",
                    "2": "71",
                    "3": None,
                    "4": "6911.14",
                    "5": None,
                    "6": "44",
                    "7": None,
                    "8": "8013.42",
                    "9": None,
                    "10": "190",
                    "11": None,
                    "12": "117655.01",
                    "13": None,
                    "14": "01-Nov",
                    "15": None,
                    "16": None,
                    "17": None,
                    "18": None,
                },
            },
        }
        with patch.dict(
            os.environ, {"STOCK_VISION_PCS_ORIC_NULL_PAD": "true"}, clear=False
        ):
            self.assertTrue(_repair_pcs_unit_oric_null_pad(item))
        self.assertEqual(item["opening_value"], 6911.14)
        self.assertEqual(item["extra"]["purchase_value"], 8013.42)
        self.assertEqual(item["sales_value"], 117655.01)
        self.assertEqual(item["extra"]["expiry_stock"], "01-Nov")
        self.assertIsNone(item.get("closing_qty"))

    def test_merge_reread_pcs_name_mismatch_still_fills_values(self):
        """Index-matched recovery with OCR-typo SKU still fills hollow first row."""
        from services.stock_vision_table import merge_reread

        first = {
            "product_name": "BLEMINOR ANTIBACTERIA CREAM 50ML",
            "opening_qty": 0.0,
            "opening_value": 0.0,
            "receipts_qty": 0.0,
            "sales_qty": 0.0,
            "sales_value": 0.0,
            "closing_qty": None,
            "closing_value": 0.0,
            "extra": {
                "vision_row_index": 4,
                "field_source": {
                    "opening_qty": "missing",
                    "opening_value": "missing",
                    "purchase_qty": "missing",
                    "sales_qty": "missing",
                    "sales_value": "missing",
                    "closing_qty": "missing",
                    "closing_value": "missing",
                },
            },
        }
        recovery = {
            "product_name": "BLEMINOR ANTIBACTERIL CREAM 50ML",
            "opening_qty": 14.0,
            "opening_value": 2300.67,
            "receipts_qty": 50.0,
            "sales_qty": 19.0,
            "sales_value": 2152.42,
            "closing_qty": 45.0,
            "closing_value": 29063.29,
            "extra": {
                "vision_row_index": 4,
                "purchase_value": 6758.8,
                "flags": ["PCS_ORIC_NULL_PAD"],
                "field_source": {
                    "opening_qty": "printed",
                    "opening_value": "printed",
                    "purchase_qty": "printed",
                    "purchase_value": "printed",
                    "sales_qty": "printed",
                    "sales_value": "printed",
                    "closing_qty": "printed",
                    "closing_value": "printed",
                },
            },
        }
        with patch.dict(
            os.environ, {"STOCK_VISION_PCS_ORIC_NULL_PAD": "true"}, clear=False
        ):
            out = merge_reread([first], [recovery], prefer_reconciled=True)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].get("product_name"), "BLEMINOR ANTIBACTERIA CREAM 50ML")
        self.assertEqual(out[0].get("opening_qty"), 14.0)
        self.assertEqual(out[0].get("opening_value"), 2300.67)
        self.assertEqual(out[0].get("sales_value"), 2152.42)
        self.assertEqual(out[0].get("closing_value"), 29063.29)

    def test_map_vision_word_split_oric_null_pad_recovers_values(self):
        """Recovery-shaped word-split headers + null pads → VALUES not zero."""
        from services.stock_vision_table import map_vision_table

        headers = [
            {"col_index": 0, "text": "PRODUCT DESCRIPTION"},
            {"col_index": 1, "text": "UNIT"},
            {"col_index": 2, "text": "OPENING"},
            {"col_index": 3, "text": "STOCK"},
            {"col_index": 4, "text": "OPENING"},
            {"col_index": 5, "text": "VALUE"},
            {"col_index": 6, "text": "RECEIVE"},
            {"col_index": 7, "text": "QUANTITY"},
            {"col_index": 8, "text": "RECEIVE"},
            {"col_index": 9, "text": "VALUE"},
            {"col_index": 10, "text": "ISSUE"},
            {"col_index": 11, "text": "QUANTITY"},
            {"col_index": 12, "text": "ISSUE"},
            {"col_index": 13, "text": "VALUE"},
            {"col_index": 14, "text": "CLOSING"},
            {"col_index": 15, "text": "STOCK"},
            {"col_index": 16, "text": "CLOSING"},
            {"col_index": 17, "text": "VALUE"},
            {"col_index": 18, "text": "EX-STO"},
        ]
        cells = [
            "ARCTIC BAR SOAP 75GM",
            "PCS",
            "148",
            None,
            "10921.87",
            None,
            "0",
            None,
            "0",
            None,
            "21",
            None,
            "3476.70",
            None,
            "158",
            None,
            "10185.85",
            None,
            "01-Jan",
        ]
        table = {
            "tables": [
                {
                    "column_count": 19,
                    "header_rows": [headers],
                    "rows": [{"row_index": 0, "cells": cells}],
                }
            ]
        }
        with patch.dict(
            os.environ, {"STOCK_VISION_PCS_ORIC_NULL_PAD": "true"}, clear=False
        ):
            mapped = map_vision_table(table, request_id="pcs-null-pad")
        items = mapped.get("line_items") or []
        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual(item.get("opening_qty"), 148.0)
        self.assertEqual(item.get("opening_value"), 10921.87)
        self.assertEqual(item.get("receipts_qty"), 0.0)
        self.assertEqual(item.get("sales_qty"), 21.0)
        self.assertEqual(item.get("sales_value"), 3476.70)
        self.assertEqual(item.get("closing_qty"), 158.0)
        self.assertEqual(item.get("closing_value"), 10185.85)
        self.assertIn("PCS_ORIC_NULL_PAD", (item.get("extra") or {}).get("flags") or [])


if __name__ == "__main__":
    unittest.main()
