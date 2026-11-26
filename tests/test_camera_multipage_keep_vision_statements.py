"""Camera multipage must not discard vision_table item_pack_free payloads."""

from __future__ import annotations

import unittest

from services.sales_statement_extractor import _camera_multipage_item_pack_free_vision


def _item(name: str, opening: float, sales: float, closing: float) -> dict:
    return {
        "product_name": name,
        "opening_qty": opening,
        "sales_qty": sales,
        "closing_qty": closing,
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
            ]
        },
    }


class CameraMultipageItemPackFreeVisionTests(unittest.TestCase):
    def test_flattens_over_split_vision_groups_keeping_sales(self):
        multipage = {
            "multi_statement": True,
            "statements": [
                {
                    "line_items": [_item("BONNISAN DROPS", 40.0, 1.0, 39.0)],
                    "totals": {"extra": {"layout": "item_pack_free_return"}},
                },
                {
                    "line_items": [_item("LUKOL TABLETS", 31.0, 13.0, 18.0)],
                    "totals": {"extra": {"layout": "item_pack_free_return"}},
                },
            ],
            "totals": {
                "extra": {
                    "vision_table_final": True,
                    "extraction_method": "vision_table",
                    "page_count": 2,
                }
            },
        }
        out = _camera_multipage_item_pack_free_vision(multipage)
        self.assertIsNotNone(out)
        assert out is not None
        self.assertFalse(out.get("multi_statement"))
        by_name = {i["product_name"]: i for i in out["line_items"]}
        self.assertEqual(by_name["BONNISAN DROPS"]["sales_qty"], 1.0)
        self.assertEqual(by_name["LUKOL TABLETS"]["sales_qty"], 13.0)
        extra = out["totals"]["extra"]
        self.assertTrue(extra["vision_table_final"])
        self.assertTrue(extra["camera_multipage_flattened"])
        self.assertEqual(extra["layout"], "item_pack_free_return")
        self.assertTrue(extra["no_sales_value"])

    def test_leaves_unrelated_multi_statement_alone(self):
        multipage = {
            "multi_statement": True,
            "statements": [
                {
                    "line_items": [
                        {
                            "product_name": "ZANDRA ROW",
                            "sales_qty": 2.0,
                            "extra": {},
                        }
                    ],
                    "totals": {"extra": {}},
                }
            ],
            "totals": {
                "extra": {
                    "vision_table_final": True,
                    "extraction_method": "vision_table",
                }
            },
        }
        self.assertIsNone(_camera_multipage_item_pack_free_vision(multipage))


if __name__ == "__main__":
    unittest.main()
