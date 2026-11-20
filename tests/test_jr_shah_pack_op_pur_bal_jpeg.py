"""Regression: J R SHAH pack/Op/Pur/Bal Val Stock and Sale Statement JPEG.

Fixture: 0000700077_2026_08_ZL_06_304_06092026141652.jpeg

Class-A failure mode this guards against:
- Generic early Vision fills only the first few rows (AACTARIL/ABANA/AMALAKI)
  then zeros Pur / Sale Val / Bal. for the rest of the page.
- Dates like 28-Jul-26 must stay 2026, not invent 2023.
"""

from __future__ import annotations

import unittest
from pathlib import Path

from services.sales_statement_extractor import (
    _looks_like_pack_op_pur_bal_stock_sale_text,
    _pack_op_pur_bal_coverage_incomplete,
    _pack_op_pur_bal_stock_sale_money_weak,
    empty_result,
    extract_sales_statement,
)

FIX = Path(__file__).resolve().parent / "fixtures" / (
    "0000700077_2026_08_ZL_06_304_06092026141652.jpeg"
)

# Spot-check rows transcribed from the printed source image.
EXPECTED = {
    "AACTARIL": {
        "opening_qty": 53.0,
        "receipts_qty": 0.0,
        "sales_qty": 22.0,
        "sales_value": 1919.0,
        "closing_qty": 31.0,
        "closing_value": 2167.0,
        "purchase_value": 0.0,
    },
    "ABANA": {
        "opening_qty": 15.0,
        "receipts_qty": 100.0,
        "sales_qty": 27.0,
        "sales_value": 4311.0,
        "closing_qty": 88.0,
        "closing_value": 12423.0,
        "purchase_value": 14117.0,
    },
    "CONFIDO": {
        "opening_qty": 100.0,
        "receipts_qty": 0.0,
        "sales_qty": 76.0,
        "sales_value": 13995.0,
        "closing_qty": 24.0,
        "closing_value": 3645.0,
        "purchase_value": 0.0,
    },
    "DIABECON DS": {
        "opening_qty": 150.0,
        "receipts_qty": 0.0,
        "sales_qty": 147.0,
        "sales_value": 29955.0,
        "closing_qty": 3.0,
        "closing_value": 577.0,
        "purchase_value": 0.0,
    },
    "HADJOD": {
        "opening_qty": 3.0,
        "receipts_qty": 60.0,
        "sales_qty": 21.0,
        "closing_qty": 42.0,
        "purchase_value": 12576.0,
    },
    "LIV 52 TABLET": {
        "opening_qty": 96.0,
        "receipts_qty": 200.0,
        "sales_qty": 239.0,
        "closing_qty": 57.0,
    },
    "PILEX FORTE": {
        "opening_qty": 200.0,
        "sales_qty": 47.0,
        "closing_qty": 153.0,
    },
}


def _find(items, needle: str):
    needle_u = needle.upper()
    for item in items:
        name = str(item.get("product_name") or "").upper()
        if needle_u in name:
            return item
    return None


class TestJrShahPackOpPurBalJpeg(unittest.TestCase):
    def test_detector_matches_jr_shah_header(self):
        self.assertTrue(
            _looks_like_pack_op_pur_bal_stock_sale_text(
                "J R SHAH AND COMPANY\n"
                "Stock and Sale Statement From 28-Jul-26 to 28-Aug-26\n"
                "Item Name pack Op Pur SP Pur Val Sale SS Sp Qty Sale Val Bal. Bal Val\n"
            )
        )

    def test_coverage_incomplete_flags_first_rows_only(self):
        """Simulate the Class-A failure: first 3 rows filled, rest zeroed."""
        result = empty_result(FIX.name, "jpeg")
        result["report_title"] = "Stock and Sale Statement"
        rows = []
        for i, (name, vals) in enumerate(
            [
                ("H AACTARIL SOAP", EXPECTED["AACTARIL"]),
                ("H ABANA", EXPECTED["ABANA"]),
                ("H AMALAKI TAB", {
                    "opening_qty": 89.0,
                    "receipts_qty": 0.0,
                    "sales_qty": 9.0,
                    "sales_value": 1919.0,
                    "closing_qty": 80.0,
                    "closing_value": 14419.0,
                    "purchase_value": 0.0,
                }),
            ]
        ):
            rows.append(
                {
                    "product_name": name,
                    "packing": "60TAB" if i else "75GM",
                    "opening_qty": vals["opening_qty"],
                    "receipts_qty": vals["receipts_qty"],
                    "sales_qty": vals["sales_qty"],
                    "sales_value": vals["sales_value"],
                    "closing_qty": vals["closing_qty"],
                    "closing_value": vals["closing_value"],
                    "extra": {"purchase_value": vals.get("purchase_value") or 0},
                }
            )
        for i in range(20):
            rows.append(
                {
                    "product_name": f"H PRODUCT {i}",
                    "packing": "60TAB",
                    "opening_qty": 50.0 if i % 2 == 0 else 0.0,
                    "receipts_qty": 0.0,
                    "sales_qty": 10.0 if i % 2 == 0 else 0.0,
                    "sales_value": 0.0,
                    "closing_qty": 40.0 if i % 2 == 0 else 0.0,
                    "closing_value": 0.0,
                    "extra": {"purchase_value": 0},
                }
            )
        result["line_items"] = rows
        self.assertTrue(_pack_op_pur_bal_coverage_incomplete(result))
        # Fully money-filled active rows should not be incomplete.
        for item in rows[3:]:
            if float(item["sales_qty"] or 0) > 0:
                item["sales_value"] = 1000.0
                item["closing_value"] = 2000.0
        self.assertFalse(_pack_op_pur_bal_coverage_incomplete(result))

    @unittest.skipUnless(FIX.is_file(), "JR SHAH JPEG fixture missing")
    def test_live_jpeg_extracts_all_rows_not_first_three_only(self):
        result = extract_sales_statement(FIX.read_bytes(), FIX.name)
        extra = (result.get("totals") or {}).get("extra") or {}
        method = extra.get("extraction_method")
        self.assertEqual(method, "pack_op_pur_bal_stock_sale_vision")
        self.assertNotEqual(method, "gemini_extraction_fallback")
        self.assertEqual(result.get("period_from"), "2026-07-28")
        self.assertEqual(result.get("period_to"), "2026-08-28")

        items = [i for i in (result.get("line_items") or []) if isinstance(i, dict)]
        self.assertGreaterEqual(len(items), 40)

        for needle, exp in EXPECTED.items():
            row = _find(items, needle)
            self.assertIsNotNone(row, f"missing row matching {needle}")
            for field, expected in exp.items():
                if field == "purchase_value":
                    got = float((row.get("extra") or {}).get("purchase_value") or 0)
                else:
                    got = float(row.get(field) or 0)
                self.assertEqual(
                    got,
                    float(expected),
                    f"{needle}.{field}: got {got} expected {expected}",
                )

        # Printed SS=1 must land in sales_scheme_qty (not SP / purchase_scheme_qty).
        for needle in ("MANJISHTHA", "PILEX TABLETS"):
            row = _find(items, needle)
            self.assertIsNotNone(row, needle)
            # Prefer exact product when PILEX FORTE also matches a looser needle.
            if needle == "PILEX TABLETS":
                row = next(
                    (
                        i
                        for i in items
                        if "PILEX TABLETS" in str(i.get("product_name") or "").upper()
                        and "FORTE" not in str(i.get("product_name") or "").upper()
                        and "FORT " not in str(i.get("product_name") or "").upper()
                    ),
                    row,
                )
            extra_row = row.get("extra") or {}
            self.assertEqual(
                float(extra_row.get("sales_scheme_qty") or 0),
                1.0,
                f"{needle} SS should be sales_scheme_qty=1",
            )
            self.assertEqual(
                float(extra_row.get("purchase_scheme_qty") or 0),
                0.0,
                f"{needle} SS must not remain in purchase_scheme_qty/SP",
            )

        self.assertEqual(result.get("totals", {}).get("sales_value"), 209502.0)
        self.assertEqual(result.get("totals", {}).get("closing_value"), 278899.0)
        self.assertEqual(float(extra.get("purchase_value") or 0), 125277.0)
        # Row Bal sum may be 2039 while printed footer Bal is 2035 — do not force.
        line_bal = sum(float(i.get("closing_qty") or 0) for i in items)
        self.assertGreaterEqual(line_bal, 2000)
        footer_bal = float(extra.get("closing_qty") or 0)
        self.assertIn(footer_bal, {2035.0, line_bal})

        self.assertFalse(_pack_op_pur_bal_stock_sale_money_weak(result))
        self.assertFalse(_pack_op_pur_bal_coverage_incomplete(result))

        identity_fails = []
        for item in items:
            opening = float(item.get("opening_qty") or 0)
            receipts = float(item.get("receipts_qty") or 0)
            sales = float(item.get("sales_qty") or 0)
            closing = float(item.get("closing_qty") or 0)
            ss = float((item.get("extra") or {}).get("sales_scheme_qty") or 0)
            if abs((opening + receipts - sales - ss) - closing) > 0.01:
                identity_fails.append(item.get("product_name"))
        # KAPIKACHHU (and similar) can be source-inconsistent; allow a small residue.
        self.assertLessEqual(
            len(identity_fails),
            2,
            f"stock identity failures: {identity_fails[:10]}",
        )


if __name__ == "__main__":
    unittest.main()
