"""SAP order-form photos keep the right-hand Qty on that product row."""

import unittest

from pathlib import Path

from PIL import Image

from services.sales_statement_extractor import (
    _looks_like_unfilled_sap_order_form,
    _overlay_closer_sap_qty,
    _sap_order_form_pixel_boxes,
    _zip_sap_order_form_rows,
)


class SapOrderFormZipTests(unittest.TestCase):
    def test_right_qty_stays_on_its_product(self):
        products = [
            {"code": "7002239", "product_name": "Liv.52 DS syrup"},
            {"code": "7000256", "product_name": "Liv.52 DS tablets"},
            {"code": "7001730", "product_name": "Lukol syrup"},
            {"code": "7000269", "product_name": "Lukol tablets"},
            {"code": "7000276", "product_name": "Menosan tablets"},
            {"code": "7000283", "product_name": "Mentat syrup"},
            {"code": "7000288", "product_name": "Mentat tablets"},
            {"code": "7005605", "product_name": "Mentat gummies"},
        ]
        packs = [
            {"packing": "200 ml", "qty": 198},
            {"packing": "60s", "qty": 248},
            {"packing": "200 ml", "qty": 34},
            {"packing": "60s", "qty": 56},
            {"packing": "30s", "qty": None},
            {"packing": "200 ml", "qty": 36},
            {"packing": "60s", "qty": 19},
            {"packing": "30s", "qty": None},
        ]
        items = _zip_sap_order_form_rows(products, packs)
        self.assertEqual(items[0]["packing"], "200 ml")
        self.assertEqual(items[0]["sales_qty"], 198.0)
        self.assertEqual(items[1]["packing"], "60s")
        self.assertEqual(items[1]["sales_qty"], 248.0)
        self.assertEqual(items[4]["sales_qty"], 0.0)
        self.assertEqual(items[4]["sales_value"], 0.0)
        self.assertEqual(items[0]["opening_qty"], 0.0)
        self.assertEqual(items[0]["closing_qty"], 0.0)

    def test_mismatched_row_counts_are_not_zipped(self):
        products = [{"code": "7002239", "product_name": "Liv.52 DS syrup"}] * 8
        packs = [{"packing": "200 ml", "qty": 1}] * 7
        self.assertEqual(_zip_sap_order_form_rows(products, packs), [])

    def test_detector_ignores_forms_that_already_have_qty(self):
        items = [
            {"product_code": f"700{i:04d}", "sales_qty": 2}
            for i in range(10)
        ]
        self.assertFalse(
            _looks_like_unfilled_sap_order_form({"line_items": items})
        )

    def test_detector_matches_unfilled_sap_rows(self):
        items = [
            {"product_code": f"700{i:04d}", "sales_qty": 0}
            for i in range(10)
        ]
        self.assertTrue(
            _looks_like_unfilled_sap_order_form({"line_items": items})
        )

    def test_tall_photo_crop_includes_the_first_sap_row(self):
        path = Path("0000732417_2026_08_ZA_07_322_06092026011854.jpeg")
        if not path.exists():
            self.skipTest("order form photo is not in the workspace")
        image = Image.open(path).convert("RGB")
        boxes = _sap_order_form_pixel_boxes(image)
        self.assertIsNotNone(boxes)
        _x0, top, _x1, bottom = boxes["products"]
        self.assertLess(top, 140)
        self.assertGreater(bottom, top + 200)
        self.assertGreater(boxes["packs"][0], boxes["products"][2] - 5)

    def test_closer_qty_fills_a_blank_and_can_replace_a_lower_misread(self):
        full = [
            {"packing": "200 ml", "qty": 12},
            {"packing": "60s", "qty": None},
            {"packing": "200 ml", "qty": None},
        ]
        _overlay_closer_sap_qty(
            full,
            [
                {"packing": "200 ml", "qty": 17},
                {"packing": "60s", "qty": 198},
                {"packing": "200 ml", "qty": 40},
            ],
            overwrite=False,
        )
        self.assertEqual([row["qty"] for row in full], [12, 198, 40])
        lower = [
            {"packing": "60s", "qty": 56},
            {"packing": "60s", "qty": None},
            {"packing": "30 g", "qty": None},
        ]
        _overlay_closer_sap_qty(
            lower,
            [
                {"packing": "60s", "qty": 86},
                {"packing": "60s", "qty": None},
                {"packing": "30 g", "qty": None},
            ],
            overwrite=True,
        )
        self.assertEqual(lower[0]["qty"], 86)


if __name__ == "__main__":
    unittest.main()
