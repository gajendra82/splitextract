"""SAP order-form photos keep the right-hand Qty on that product row."""

import unittest

from services.sales_statement_extractor import (
    _looks_like_unfilled_sap_order_form,
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


if __name__ == "__main__":
    unittest.main()
