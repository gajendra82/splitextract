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


if __name__ == "__main__":
    unittest.main()
