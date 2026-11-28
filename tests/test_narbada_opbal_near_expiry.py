"""NARBADA ENTERPRISES 230.jpg — Op.Bal/Receipt/Total/Issue/Closing/Near Expiry.

Regression for request 7785c4100d31:
- Issue Qty must not land in receipts_qty
- Opening must not shift into receipts_qty (PILEX FORTE TAB)
- near_expiry_qty is a dedicated printed column
- Printed TOTAL row is authoritative
- Op_Stk|Rcpts reader must not claim this layout
- Quality must reflect identity / column-assignment failures
"""

from __future__ import annotations

import unittest
from pathlib import Path

from services.extraction_quality import evaluate_extraction_quality
from services.sales_statement_extractor import (
    _apply_total_row_to_result,
    _is_opbal_issue_closing_format,
    _is_swilerp_sales_stock_statement,
    _looks_like_op_stk_rcpts_grid_text,
    _opbal_layout,
    _parse_opbal_receipt_issue_statement,
    _parse_statement_total_row,
    empty_line_item,
    empty_result,
    extract_sales_statement,
)
from services.stock_image_vision import assess_stock_structured_quality

FIXTURE_TXT = (
    Path(__file__).resolve().parent / "fixtures" / "narbada_230_opbal_near.txt"
)
FIXTURE_JPG = Path(__file__).resolve().parent / "fixtures" / "narbada_230.jpg"

# Printed values verified against C:\\Users\\amnsa\\Downloads\\230.jpg
EXPECTED = {
    "CLARINA ANTI ACN": (28.0, 0.0, 28.0, 1.0, 27.0, 0.0),
    "CONFIDO TAB": (66.0, 0.0, 66.0, 11.0, 55.0, 0.0),
    "DIABECON DS TAB": (23.0, 0.0, 23.0, 1.0, 22.0, 22.0),
    "HADJOD TABLETS": (42.0, 0.0, 42.0, 2.0, 40.0, 0.0),
    "HIMCOLIN GEL": (18.0, 0.0, 18.0, 1.0, 17.0, 0.0),
    "KAPIKACHHU CAP": (48.0, 0.0, 48.0, 1.0, 47.0, 0.0),
    "PILEX FORTE TAB": (49.0, 0.0, 49.0, 2.0, 47.0, 0.0),
    "PURIM TAB": (57.0, 0.0, 57.0, 6.0, 51.0, 0.0),
    "SHALLAKI TAB": (42.0, 0.0, 42.0, 5.0, 37.0, 0.0),
    "TENTEX FORTE TAB": (278.0, 0.0, 278.0, 60.0, 218.0, 0.0),
    "TRIPHALA TABLETS": (27.0, 0.0, 27.0, 1.0, 26.0, 0.0),
}
PRINTED_TOTALS = {
    "opening_qty": 212899.0,
    "receipts_qty": 82495.0,
    "total_stock_qty": 295394.0,
    "sales_qty": 79634.0,
    "closing_qty": 207273.0,
    "near_expiry_qty": 12279.0,
}


def _row_tuple(item: dict) -> tuple:
    extra = item.get("extra") or {}
    total = extra.get("total_stock_qty")
    if total is None:
        total = extra.get("total_stock")
    near = extra.get("near_expiry_qty")
    return (
        float(item.get("opening_qty") or 0),
        float(item.get("receipts_qty") or 0),
        float(total) if total is not None else float(item.get("opening_qty") or 0)
        + float(item.get("receipts_qty") or 0),
        float(item.get("sales_qty") or 0),
        float(item.get("closing_qty") or 0),
        float(near) if near is not None else None,
    )


def _find(items, needle: str):
    needle_u = needle.upper()
    for item in items:
        name = str(item.get("product_name") or "").upper()
        if needle_u in name:
            return item
    return None


class TestNarbadaLayoutRouting(unittest.TestCase):
    def test_layout_is_issue_closing_near(self):
        text = FIXTURE_TXT.read_text(encoding="utf-8")
        self.assertEqual(_opbal_layout(text), "issue_closing_near")
        self.assertTrue(_is_opbal_issue_closing_format(text))
        self.assertTrue(_is_swilerp_sales_stock_statement(text))
        self.assertFalse(_looks_like_op_stk_rcpts_grid_text(text))


class TestNarbadaOpBalParser(unittest.TestCase):
    def test_issue_not_receipt_and_near_expiry_preserved(self):
        text = FIXTURE_TXT.read_text(encoding="utf-8")
        result = _parse_opbal_receipt_issue_statement(text, "narbada_230.txt", "txt")
        self.assertIsNotNone(result)
        items = result.get("line_items") or []
        self.assertGreaterEqual(len(items), 12)

        for needle, expected in EXPECTED.items():
            item = _find(items, needle)
            self.assertIsNotNone(item, msg=f"missing row {needle}")
            got = _row_tuple(item)
            self.assertEqual(
                got,
                expected,
                msg=f"{needle}: got {got} expected {expected}",
            )
            # Issue must never equal a non-zero receipt when source Receipt=0
            if expected[1] == 0.0 and expected[3] != 0.0:
                self.assertNotEqual(
                    float(item.get("receipts_qty") or 0),
                    float(item.get("sales_qty") or 0),
                    msg=f"{needle}: Issue parked in receipts_qty",
                )
            fs = (item.get("extra") or {}).get("field_source") or {}
            self.assertEqual(fs.get("near_expiry_qty"), "printed")

    def test_pilex_opening_not_shifted_to_receipt(self):
        text = FIXTURE_TXT.read_text(encoding="utf-8")
        result = _parse_opbal_receipt_issue_statement(text, "narbada_230.txt", "txt")
        item = _find(result.get("line_items") or [], "PILEX FORTE TAB")
        self.assertIsNotNone(item)
        self.assertEqual(float(item.get("opening_qty") or 0), 49.0)
        self.assertEqual(float(item.get("receipts_qty") or 0), 0.0)
        self.assertEqual(float(item.get("sales_qty") or 0), 2.0)
        self.assertEqual(float(item.get("closing_qty") or 0), 47.0)

    def test_printed_total_row_six_columns(self):
        text = FIXTURE_TXT.read_text(encoding="utf-8")
        parsed = _parse_statement_total_row(text)
        self.assertIsNotNone(parsed)
        for key, value in PRINTED_TOTALS.items():
            self.assertEqual(
                float(parsed.get(key)),
                value,
                msg=f"total {key}",
            )
        result = empty_result("narbada_230.txt", "txt")
        result["line_items"] = [
            {
                "product_name": "X",
                "opening_qty": 1.0,
                "receipts_qty": 0.0,
                "sales_qty": 0.0,
                "closing_qty": 1.0,
            }
        ]
        result["totals"]["extra"]["extraction_method"] = "opbal_issue_closing_parser"
        result["totals"]["extra"]["qty_only"] = True
        _apply_total_row_to_result(result, text)
        for key, value in PRINTED_TOTALS.items():
            self.assertEqual(float(result["totals"].get(key)), value)
        self.assertEqual(
            result["totals"]["extra"].get("total_row_source"), "footer_total"
        )


class TestNarbadaQualityDecision(unittest.TestCase):
    def test_op_stk_identity_fails_downgrade_confidence(self):
        result = empty_result("narbada.jpg", "jpg")
        result["totals"]["extra"]["extraction_method"] = (
            "op_stk_rcpts_stock_sale_vision"
        )
        result["totals"]["extra"]["stock_identity_fail_count"] = 7
        # Simulate Issue parked in receipts for several rows
        for name, issue in (
            ("CONFIDO TAB", 11.0),
            ("PURIM TAB", 6.0),
            ("TENTEX FORTE TAB", 60.0),
        ):
            item = empty_line_item()
            item["product_name"] = name
            item["opening_qty"] = 50.0
            item["receipts_qty"] = issue  # defect
            item["sales_qty"] = issue
            item["closing_qty"] = 50.0 - issue
            result["line_items"].append(item)
        for _ in range(4):
            item = empty_line_item()
            item["product_name"] = "OK ROW"
            item["opening_qty"] = 10.0
            item["receipts_qty"] = 0.0
            item["sales_qty"] = 1.0
            item["closing_qty"] = 9.0
            result["line_items"].append(item)

        decision = assess_stock_structured_quality(result)
        self.assertIn("identity_failure_rate", decision.get("reasons") or [])
        self.assertFalse(decision.get("trusted_structured_reader"))
        self.assertLess(float(decision.get("extraction_confidence") or 1.0), 0.9)

        quality = evaluate_extraction_quality(result)
        self.assertIn("stock_identity_failure", quality.get("reasons") or [])
        self.assertLess(int(quality.get("score") or 100), 100)


class TestNarbadaTextExtract(unittest.TestCase):
    def test_txt_extract_preserves_mapping(self):
        raw = FIXTURE_TXT.read_bytes()
        result = extract_sales_statement(raw, "narbada_230_opbal_near.txt")
        items = result.get("line_items") or []
        self.assertGreaterEqual(len(items), 12)
        pilex = _find(items, "PILEX FORTE TAB")
        self.assertIsNotNone(pilex)
        self.assertEqual(float(pilex.get("opening_qty") or 0), 49.0)
        self.assertEqual(float(pilex.get("receipts_qty") or 0), 0.0)
        confido = _find(items, "CONFIDO")
        self.assertIsNotNone(confido)
        self.assertEqual(float(confido.get("receipts_qty") or 0), 0.0)
        self.assertEqual(float(confido.get("sales_qty") or 0), 11.0)
        near = (confido.get("extra") or {}).get("near_expiry_qty")
        self.assertIsNotNone(near)
        totals = result.get("totals") or {}
        # Prefer printed footer when attached
        if totals.get("opening_qty") is not None:
            self.assertEqual(float(totals.get("opening_qty")), 212899.0)
            self.assertEqual(float(totals.get("near_expiry_qty")), 12279.0)


@unittest.skipUnless(FIXTURE_JPG.exists(), "narbada_230.jpg fixture missing")
class TestNarbadaImageFixturePresent(unittest.TestCase):
    def test_fixture_bytes_nonempty(self):
        self.assertGreater(FIXTURE_JPG.stat().st_size, 10_000)


if __name__ == "__main__":
    unittest.main()
