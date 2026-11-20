"""INTIMATE PHARMACEUTICALS Item Movement: SaleAmt -> sales_value, not StkValue."""

from __future__ import annotations

import unittest
from pathlib import Path

from services.sales_statement_extractor import (
    _SALES_STATEMENT_TEXT_PROMPT,
    _SALES_STATEMENT_VISION_PROMPT,
    _apply_parsed_sales_json,
    _fix_item_movement_qty_columns,
    _fix_item_movement_stkvalue_in_closing_value,
    _looks_like_item_movement_report,
    _needs_item_movement_saleamt_retry,
    _normalize_item_movement_saleamt_fields,
    _sales_value_looks_like_stkvalue,
    empty_line_item,
    empty_result,
    extract_sales_statement,
)

SAMPLE = Path(r"C:\Users\Admin\Downloads\splitextract-main\Stock Images\IMG-20261104-WA0011.jpg")


class TestItemMovementSaleAmtMapping(unittest.TestCase):
    def test_prompts_forbid_stkvalue_as_sales_value(self):
        for prompt in (_SALES_STATEMENT_VISION_PROMPT, _SALES_STATEMENT_TEXT_PROMPT):
            self.assertIn("SaleAmt", prompt)
            self.assertIn("StkValue", prompt)
            self.assertIn("NEVER use StkValue", prompt)
            self.assertIn("SReturn", prompt)
            self.assertIn("closing_value", prompt)
            self.assertIn("extra.stock_value", prompt)

    def test_normalizer_prefers_saleamt_over_stkvalue(self):
        raw = {
            "product_name": "BONNISAN DROPS",
            "sales_value": 2389.44,  # wrongly StkValue
            "sales_qty": 3,
            "extra": {"stock_value": 2389.44, "sale_amt": 229.48},
        }
        item = empty_line_item()
        item["product_name"] = "BONNISAN DROPS"
        item["sales_value"] = 2389.44
        item["sales_qty"] = 3.0
        item["extra"] = {"stock_value": 2389.44}
        _normalize_item_movement_saleamt_fields(raw, item)
        self.assertEqual(item["sales_value"], 229.48)
        self.assertEqual(item["extra"]["stock_value"], 2389.44)

    def test_normalizer_clears_stkvalue_on_zero_sales_row(self):
        raw = {
            "product_name": "BONNISAN SY 100 ML",
            "sales_value": 3225.05,
            "sales_qty": 0,
            "extra": {"stock_value": 3225.05},
        }
        item = empty_line_item()
        item["product_name"] = "BONNISAN SY 100 ML"
        item["sales_value"] = 3225.05
        item["sales_qty"] = 0.0
        item["extra"] = {"stock_value": 3225.05}
        _normalize_item_movement_saleamt_fields(raw, item)
        self.assertEqual(item["sales_value"], 0.0)
        self.assertEqual(item["extra"]["stock_value"], 3225.05)

    def test_apply_parsed_json_uses_saleamt_and_stock_value(self):
        parsed = {
            "stockist_name": "INTIMATE PHARMACEUTICALS",
            "report_title": "Item Movement () Report [01-10-2026 - 31-10-2026]",
            "line_items": [
                {
                    "product_code": "651",
                    "product_name": "BONNISAN DROPS",
                    "opening_qty": 41,
                    "receipts_qty": 0,
                    "sales_qty": 3,
                    "sales_value": 2389.44,
                    "closing_qty": 38,
                    "closing_value": 0,
                    "sale_amt": 229.48,
                    "stock_value": 2389.44,
                },
                {
                    "product_code": "957",
                    "product_name": "CYSTONE TAB",
                    "opening_qty": 20,
                    "receipts_qty": 101,
                    "sales_qty": 44,
                    "sales_value": 12940.20,
                    "closing_qty": 78,
                    "closing_value": 0,
                    "SaleAmt": 8459.21,
                    "StkValue": 12940.20,
                },
            ],
            "totals": {
                "sales_value": 150194.85,
                "closing_value": None,
                "extra": {"stock_value": 150194.85},
                "SaleAmt": 71685.48,
            },
        }
        result = _apply_parsed_sales_json(empty_result("x.jpg", "jpg"), parsed)
        by_name = {i["product_name"]: i for i in result["line_items"]}
        self.assertEqual(by_name["BONNISAN DROPS"]["sales_value"], 229.48)
        self.assertEqual(by_name["BONNISAN DROPS"]["extra"]["stock_value"], 2389.44)
        self.assertEqual(by_name["CYSTONE TAB"]["sales_value"], 8459.21)
        self.assertEqual(by_name["CYSTONE TAB"]["extra"]["stock_value"], 12940.20)
        self.assertEqual(result["totals"]["sales_value"], 71685.48)
        self.assertEqual(result["totals"]["extra"]["stock_value"], 150194.85)

    def test_op_sale_sa_val_format_not_treated_as_item_movement(self):
        """Op/Sale/Sa val tables must keep Sale qty — not Intimate rewrites."""
        parsed = {
            "stockist_name": "SOME OTHER AGENCY",
            "report_title": "Stock and Sales Statement",
            "line_items": [
                {
                    "product_name": "BONNISAN DROPS",
                    "packing": "30 ML",
                    "opening_qty": 33,
                    "receipts_qty": 0,
                    "sales_qty": 2,
                    "sales_value": 149,
                    "closing_qty": 31,
                    "closing_value": 2014,
                    "extra": {"opening_value": 2147, "stock_value": 2014},
                }
            ],
            "totals": {"sales_value": 149, "closing_value": 2014, "extra": {}},
        }
        result = _apply_parsed_sales_json(empty_result("x.jpg", "jpg"), parsed)
        self.assertFalse(_looks_like_item_movement_report(result))
        item = result["line_items"][0]
        self.assertEqual(item["sales_qty"], 2.0)
        self.assertEqual(item["sales_value"], 149.0)
        self.assertEqual(item["closing_qty"], 31.0)

    def test_detects_intimate_and_stkvalue_bug(self):
        result = empty_result("x.jpg", "jpg")
        result["stockist_name"] = "INTIMATE PHARMACEUTICALS"
        result["report_title"] = "Item Movement () Report"
        result["line_items"] = [
            {
                **empty_line_item(),
                "product_name": f"P{i}",
                "opening_qty": 10,
                "sales_qty": 0,
                "closing_qty": 10,
                "sales_value": 1000.0 + i,
            }
            for i in range(6)
        ]
        self.assertTrue(_looks_like_item_movement_report(result))
        self.assertTrue(_sales_value_looks_like_stkvalue(result))
        self.assertTrue(_needs_item_movement_saleamt_retry(result))

    def test_keeps_stkvalue_as_closing_value(self):
        """StkValue stays in closing_value (and mirrors to extra.stock_value)."""
        parsed = {
            "stockist_name": "INTIMATE PHARMACEUTICALS",
            "report_title": "Item Movement () Report",
            "line_items": [
                {
                    "product_code": "9020",
                    "product_name": "ARJUNA TAB (HIM)",
                    "opening_qty": 0,
                    "receipts_qty": 2,
                    "sales_qty": 3,
                    "sales_value": 0,
                    "closing_qty": 0,
                    "closing_value": 780.0,
                    "extra": {"stock_value": 0},
                },
                {
                    "product_code": "651",
                    "product_name": "BONNISAN DROPS",
                    "opening_qty": 41,
                    "receipts_qty": 0,
                    "sales_qty": 3,
                    "sales_value": 229.48,
                    "closing_qty": 38,
                    "closing_value": 2389.44,
                    "extra": {"stock_value": 0},
                },
                {
                    "product_code": "652",
                    "product_name": "BONNISAN SY 100 ML",
                    "opening_qty": 53,
                    "receipts_qty": 0,
                    "sales_qty": 0,
                    "sales_value": 0,
                    "closing_qty": 53,
                    "closing_value": 3225.05,
                    "extra": {"stock_value": 0},
                },
                {
                    "product_code": "957",
                    "product_name": "CYSTONE TAB",
                    "opening_qty": 20,
                    "receipts_qty": 101,
                    "sales_qty": 44,
                    "sales_value": 8459.21,
                    "closing_qty": 78,
                    "closing_value": 12940.20,
                    "extra": {"stock_value": 0},
                },
            ],
            "totals": {
                "sales_value": 71685.48,
                "closing_value": 150194.85,
                "extra": {"stock_value": 0},
            },
        }
        result = _apply_parsed_sales_json(empty_result("x.jpg", "jpg"), parsed)
        by_name = {i["product_name"]: i for i in result["line_items"]}

        arjuna = by_name["ARJUNA TAB (HIM)"]
        self.assertEqual(arjuna["sales_value"], 780.0)
        self.assertEqual(arjuna["closing_value"], 0.0)
        self.assertNotIn("stock_value", arjuna.get("extra") or {})

        bonni = by_name["BONNISAN DROPS"]
        self.assertEqual(bonni["sales_value"], 229.48)
        self.assertEqual(bonni["closing_value"], 2389.44)
        self.assertEqual(bonni["extra"]["stock_value"], 2389.44)

        sy = by_name["BONNISAN SY 100 ML"]
        self.assertEqual(sy["sales_value"], 0.0)
        self.assertEqual(sy["closing_value"], 3225.05)
        self.assertEqual(sy["extra"]["stock_value"], 3225.05)

        cyst = by_name["CYSTONE TAB"]
        self.assertEqual(cyst["sales_value"], 8459.21)
        self.assertEqual(cyst["closing_value"], 12940.20)
        self.assertEqual(cyst["extra"]["stock_value"], 12940.20)

        self.assertEqual(result["totals"]["sales_value"], 71685.48)
        self.assertEqual(result["totals"]["closing_value"], 150194.85)
        self.assertEqual(result["totals"]["extra"]["stock_value"], 150194.85)

    def test_arjuna_saleamt_recovered_even_if_mirrored_to_stock_value(self):
        parsed = {
            "stockist_name": "INTIMATE PHARMACEUTICALS",
            "report_title": "Item Movement () Report",
            "line_items": [
                {
                    "product_name": "ARJUNA TAB (HIM)",
                    "opening_qty": 0,
                    "receipts_qty": 2,
                    "sales_qty": 3,
                    "sales_value": 0,
                    "closing_qty": 0,
                    "closing_value": 780.0,
                    "extra": {"stock_value": 780.0},
                }
            ],
            "totals": {"sales_value": 0, "closing_value": 780.0, "extra": {}},
        }
        result = _apply_parsed_sales_json(empty_result("x.jpg", "jpg"), parsed)
        item = result["line_items"][0]
        self.assertEqual(item["sales_value"], 780.0)
        self.assertEqual(item["closing_value"], 0.0)
        self.assertNotIn("stock_value", item.get("extra") or {})

    def test_platenza_stkvalue_not_stolen_when_sreturn_misread_as_sqty(self):
        parsed = {
            "stockist_name": "INTIMATE PHARMACEUTICALS",
            "report_title": "Item Movement () Report",
            "line_items": [
                {
                    "product_name": "PLATENZA TAB 20",
                    "opening_qty": 3,
                    "receipts_qty": 0,
                    "sales_qty": 2,  # actually SReturn on the image
                    "sales_value": 0,
                    "closing_qty": 5,
                    "closing_value": 655.95,
                    "extra": {"stock_value": 0},
                }
            ],
            "totals": {
                "sales_value": 71685.48,
                "closing_value": 150194.85,
                "extra": {},
            },
        }
        result = _apply_parsed_sales_json(empty_result("x.jpg", "jpg"), parsed)
        item = result["line_items"][0]
        self.assertEqual(item["sales_value"], 0.0)
        self.assertEqual(item["closing_value"], 655.95)
        self.assertEqual(item["extra"]["stock_value"], 655.95)

    def test_qty_fixes_liv52_menosan_reosto_arjuna_platenza(self):
        parsed = {
            "stockist_name": "INTIMATE PHARMACEUTICALS",
            "report_title": "Item Movement () Report",
            "line_items": [
                {
                    "product_name": "ARJUNA TAB (HIM)",
                    "opening_qty": 0,
                    "receipts_qty": 2,
                    "sales_qty": 3,
                    "sales_value": 780.0,
                    "closing_qty": 0,
                    "closing_value": 0,
                    "extra": {},
                },
                {
                    "product_name": "LIV 52 DS TABS",
                    "opening_qty": 36,
                    "receipts_qty": 6,
                    "sales_qty": 3,
                    "sales_value": 8646.01,
                    "closing_qty": 39,
                    "closing_value": 563.4,
                    "extra": {"stock_value": 563.4},
                },
                {
                    "product_name": "MENOSAN TAB",
                    "opening_qty": 54,
                    "receipts_qty": 0,
                    "sales_qty": 17,
                    "sales_value": 6891.37,
                    "closing_qty": 37,
                    "closing_value": 2816.56,
                    "extra": {"stock_value": 2816.56},
                },
                {
                    "product_name": "REOSTO TAB",
                    "opening_qty": 58,
                    "receipts_qty": 0,
                    "sales_qty": 2,
                    "sales_value": 4805.34,
                    "closing_qty": 35,
                    "closing_value": 5862.85,
                    "extra": {"stock_value": 5862.85},
                },
                {
                    "product_name": "PLATENZA TAB 20",
                    "opening_qty": 3,
                    "receipts_qty": 0,
                    "sales_qty": 2,
                    # StkValue wrongly parked in sales_value by prior recovery
                    "sales_value": 655.95,
                    "closing_qty": 5,
                    "closing_value": 0,
                    "extra": {},
                },
                {
                    "product_name": "BRESSOL TAB",
                    "opening_qty": 23,
                    "receipts_qty": 50,
                    "sales_qty": 20,
                    "sales_value": 3964.0,
                    "closing_qty": 53,
                    "closing_value": 8878.03,
                    "extra": {"stock_value": 8878.03},
                },
                {
                    "product_name": "LUKOL TABS",
                    "opening_qty": 46,
                    "receipts_qty": 11,
                    "sales_qty": 35,
                    "sales_value": 2226.38,
                    "closing_qty": 35,
                    "closing_value": 5679.1,
                    "extra": {"stock_value": 5679.1},
                },
                {
                    "product_name": "CYSTONE TAB",
                    "opening_qty": 20,
                    "receipts_qty": 101,
                    "sales_qty": 44,
                    "sales_value": 8459.21,
                    "closing_qty": 78,
                    "closing_value": 12940.2,
                    "extra": {"stock_value": 12940.2},
                },
            ],
            "totals": {
                "sales_value": 71685.48,
                "closing_value": 150194.85,
                "extra": {},
            },
        }
        result = _apply_parsed_sales_json(empty_result("x.jpg", "jpg"), parsed)
        by_name = {i["product_name"]: i for i in result["line_items"]}

        arjuna = by_name["ARJUNA TAB (HIM)"]
        self.assertEqual(arjuna["opening_qty"], 2.0)
        self.assertEqual(arjuna["receipts_qty"], 1.0)
        self.assertEqual(arjuna["sales_qty"], 3.0)
        self.assertEqual(arjuna["sales_value"], 780.0)

        liv = by_name["LIV 52 DS TABS"]
        self.assertEqual(liv["sales_qty"], 39.0)
        self.assertEqual(liv["closing_qty"], 3.0)

        menosan = by_name["MENOSAN TAB"]
        self.assertEqual(menosan["sales_qty"], 37.0)
        self.assertEqual(menosan["closing_qty"], 17.0)

        reosto = by_name["REOSTO TAB"]
        self.assertEqual(reosto["sales_qty"], 25.0)
        self.assertEqual(reosto["closing_qty"], 35.0)
        self.assertEqual(reosto["extra"]["sales_return_qty"], 2.0)

        platenza = by_name["PLATENZA TAB 20"]
        self.assertEqual(platenza["sales_qty"], 0.0)
        self.assertEqual(platenza["closing_qty"], 5.0)
        self.assertEqual(platenza["sales_value"], 0.0)
        self.assertEqual(platenza["closing_value"], 655.95)
        self.assertEqual(platenza["extra"]["sales_return_qty"], 2.0)

        # Already-correct row must stay unchanged
        bressol = by_name["BRESSOL TAB"]
        self.assertEqual(bressol["sales_qty"], 20.0)
        self.assertEqual(bressol["closing_qty"], 53.0)

        lukol = by_name["LUKOL TABS"]
        self.assertEqual(lukol["receipts_qty"], 0.0)
        self.assertEqual(lukol["sales_qty"], 11.0)
        self.assertEqual(lukol["closing_qty"], 35.0)

        # Near-balanced row must not be rewritten by SReturn inference
        cystone = by_name["CYSTONE TAB"]
        self.assertEqual(cystone["sales_qty"], 44.0)
        self.assertEqual(cystone["closing_qty"], 78.0)
        self.assertNotIn("sales_return_qty", cystone.get("extra") or {})

    def test_fix_helper_promotes_stock_value_to_closing_value(self):
        result = empty_result("x.jpg", "jpg")
        result["stockist_name"] = "INTIMATE PHARMACEUTICALS"
        result["report_title"] = "Item Movement () Report"
        result["line_items"] = [
            {
                **empty_line_item(),
                "product_name": "BONNISAN DROPS",
                "sales_qty": 3,
                "sales_value": 229.48,
                "closing_qty": 38,
                "closing_value": 0.0,
                "extra": {"stock_value": 2389.44},
            }
        ]
        result["totals"]["sales_value"] = 229.48
        result["totals"]["closing_value"] = None
        result["totals"]["extra"]["stock_value"] = 2389.44
        _fix_item_movement_stkvalue_in_closing_value(result)
        item = result["line_items"][0]
        self.assertEqual(item["sales_value"], 229.48)
        self.assertEqual(item["extra"]["stock_value"], 2389.44)
        self.assertEqual(item["closing_value"], 2389.44)
        self.assertEqual(result["totals"]["closing_value"], 2389.44)


@unittest.skipUnless(SAMPLE.exists(), "Intimate sample image not on this machine")
class TestIntimateLiveExtraction(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = extract_sales_statement(SAMPLE.read_bytes(), SAMPLE.name)

    def test_sales_value_is_saleamt_not_stkvalue(self):
        items = {i["product_name"].upper(): i for i in self.result["line_items"]}
        # StkValue bug would put ~2389 into sales_value; SaleAmt is 229.48
        bonni = next(
            (v for k, v in items.items() if "BONNISAN DROPS" in k),
            None,
        )
        self.assertIsNotNone(bonni)
        self.assertAlmostEqual(bonni["sales_value"], 229.48, places=1)
        stock = (bonni.get("extra") or {}).get("stock_value")
        if stock is not None:
            self.assertAlmostEqual(float(stock), 2389.44, places=1)

        total = self.result["totals"].get("sales_value")
        self.assertIsNotNone(total)
        # Must be SaleAmt total (~71685), not StkValue (~150194)
        self.assertLess(abs(float(total) - 71685.48), 50.0)
        self.assertGreater(abs(float(total) - 150194.85), 1000.0)


if __name__ == "__main__":
    unittest.main()
