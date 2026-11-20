"""Blank SAP ORDER FORM Qty cells must not keep Pack sizes or dash noise."""

from __future__ import annotations

import re
import unittest
from pathlib import Path

from services.sales_statement_extractor import (
    _sap_qty_echoes_packing,
    _sap_scrub_false_order_qty,
    _zip_sap_order_form_rows,
    extract_sales_statement,
)


UNIQUE = Path("0000704436_2026_08_ZA_24_260_03092026113253.jpeg")


class TestSapBlankOrderQty(unittest.TestCase):
    def test_pack_echo_detection(self):
        self.assertTrue(_sap_qty_echoes_packing(200.0, "200 ml"))
        self.assertTrue(_sap_qty_echoes_packing(30.0, "30 g"))
        self.assertFalse(_sap_qty_echoes_packing(42.0, "30 ml"))
        self.assertFalse(_sap_qty_echoes_packing(200.0, "100 ml"))

    def test_scrub_zeros_pack_echo_and_sparse_noise(self):
        items = [
            {
                "product_name": "Liv.52 DS syrup",
                "packing": "200 ml",
                "sales_qty": 200.0,
                "sales_value": 0.0,
            },
            {
                "product_name": "V-Gel",
                "packing": "30 g",
                "sales_qty": 309.0,
                "sales_value": 0.0,
            },
            {
                "product_name": "Arjuna tablets",
                "packing": "60s",
                "sales_qty": 0.0,
                "sales_value": 0.0,
            },
        ]
        _sap_scrub_false_order_qty(items)
        self.assertEqual(items[0]["sales_qty"], 0.0)
        self.assertEqual(items[1]["sales_qty"], 0.0)

    def test_blank_zip_keeps_zero_qty_rows(self):
        products = [
            {"code": f"700{i:04d}", "product_name": f"Product {i}"} for i in range(10)
        ]
        packs = [{"packing": "60s", "qty": None} for _ in range(10)]
        packs[0] = {"packing": "200 ml", "qty": 200}
        packs[1] = {"packing": "30 g", "qty": 309}
        items = _zip_sap_order_form_rows(products, packs)
        self.assertEqual(len(items), 10)
        self.assertTrue(all(item["sales_qty"] == 0.0 for item in items))

    def test_keeps_real_dense_handwritten_qty(self):
        items = [
            {
                "product_name": f"Prod {i}",
                "packing": "60s",
                "sales_qty": float(10 + i),
                "sales_value": 0.0,
            }
            for i in range(8)
        ]
        _sap_scrub_false_order_qty(items)
        self.assertEqual(items[0]["sales_qty"], 10.0)
        self.assertEqual(items[7]["sales_qty"], 17.0)

    @unittest.skipUnless(UNIQUE.is_file(), "missing Unique Medicose blank order form")
    def test_unique_medicose_has_no_false_qty(self):
        result = extract_sales_statement(UNIQUE.read_bytes(), UNIQUE.name)
        items = [i for i in (result.get("line_items") or []) if isinstance(i, dict)]
        self.assertGreaterEqual(len(items), 20)
        filled = [
            (i.get("product_code"), i.get("product_name"), i.get("sales_qty"))
            for i in items
            if (i.get("sales_qty") or 0) not in (0, 0.0, None)
        ]
        self.assertEqual(filled, [], msg=filled[:5])
        liv = next(
            (
                i
                for i in items
                if "LIV.52 DS SYRUP" in str(i.get("product_name") or "").upper()
                and "200" in str(i.get("packing") or "")
            ),
            None,
        )
        self.assertIsNotNone(liv)
        self.assertEqual(liv["sales_qty"], 0.0)
        vgel = next(
            (
                i
                for i in items
                if re.search(r"\bV-?GEL\b", str(i.get("product_name") or ""), re.I)
            ),
            None,
        )
        self.assertIsNotNone(vgel)
        self.assertEqual(vgel["sales_qty"], 0.0)


if __name__ == "__main__":
    unittest.main()
