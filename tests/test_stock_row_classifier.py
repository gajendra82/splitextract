"""Phase 0: log-only stock row classifier."""

from __future__ import annotations

import copy
import json
import os
import unittest
from pathlib import Path
from unittest import mock

from services.stock_row_classifier import (
    RowStatus,
    classify_result,
    classify_row,
    expected_closing,
    read_row_fields,
    shadow_summary_for_storage,
    would_fallback,
)


def _fields(
    opening=None,
    purchase=None,
    sales=None,
    closing=None,
    sales_return=None,
    expiry_damage=None,
    free_in=None,
    free_out=None,
    purchase_return=None,
):
    return {
        "opening": opening,
        "purchase": purchase,
        "free_in": free_in,
        "sales": sales,
        "free_out": free_out,
        "sales_return": sales_return,
        "purchase_return": purchase_return,
        "expiry_damage": expiry_damage,
        "closing": closing,
    }


def _item(
    name="PRODUCT",
    opening=0,
    receipts=0,
    sales=0,
    closing=0,
    *,
    sales_return=None,
    expiry_damage=None,
    extra=None,
):
    item = {
        "product_name": name,
        "opening_qty": opening,
        "receipts_qty": receipts,
        "sales_qty": sales,
        "closing_qty": closing,
        "sales_value": 0,
        "closing_value": 0,
        "extra": {},
    }
    if sales_return is not None:
        item["extra"]["sale_return"] = sales_return
    if expiry_damage is not None:
        item["extra"]["exp_dmg"] = expiry_damage
    if extra:
        item["extra"].update(extra)
    return item


class StockRowClassifierUnitTests(unittest.TestCase):
    def test_hiora_100gm_valid(self):
        # 9/50/59/21/0/0/38 (op/pur/total/sale/saleret/exp/closing)
        fields = _fields(
            opening=9,
            purchase=50,
            sales=21,
            sales_return=0,
            expiry_damage=0,
            closing=38,
        )
        self.assertEqual(expected_closing(fields), 38.0)
        status, info = classify_row(fields)
        self.assertEqual(status, RowStatus.VALID)
        self.assertEqual(info["candidates"], [])

    def test_hiora_50gm_valid(self):
        fields = _fields(
            opening=80,
            purchase=0,
            sales=5,
            sales_return=0,
            expiry_damage=0,
            closing=75,
        )
        status, info = classify_row(fields)
        self.assertEqual(status, RowStatus.VALID)

    def test_valid_and_minor(self):
        fields = _fields(opening=100, purchase=50, sales=30, closing=120)
        self.assertEqual(classify_row(fields)[0], RowStatus.VALID)
        fields_minor = _fields(opening=100, purchase=50, sales=30, closing=122)
        self.assertEqual(classify_row(fields_minor)[0], RowStatus.MINOR_DISCREPANCY)

    def test_column_assignment_suspected_swap(self):
        # purchase and sales read into each other's columns
        fields = _fields(opening=100, purchase=30, sales=50, closing=120)
        status, info = classify_row(fields)
        self.assertEqual(status, RowStatus.COLUMN_ASSIGNMENT_SUSPECTED)
        self.assertTrue(info["candidates"])
        swapped = False
        for candidate in info["candidates"]:
            proposed = candidate.get("fields") or {}
            if proposed.get("purchase") == 50 and proposed.get("sales") == 30:
                swapped = True
        self.assertTrue(swapped, info["candidates"])

    def test_ocr_value_suspected_dropped_digit(self):
        fields = _fields(opening=100, purchase=50, sales=3, closing=120)
        status, info = classify_row(fields)
        self.assertEqual(status, RowStatus.OCR_VALUE_SUSPECTED)
        found = any(
            (candidate.get("fields") or {}).get("sales") == 30
            for candidate in info["candidates"]
        )
        self.assertTrue(found, info["candidates"])

    def test_missing_closing(self):
        # Closing absent with opening+purchase+sales → CLOSING_DERIVED.
        fields = _fields(opening=100, purchase=50, sales=30, closing=None)
        status, info = classify_row(fields)
        self.assertEqual(status, RowStatus.CLOSING_DERIVED)
        self.assertEqual(info.get("derived_closing"), 120.0)

    def test_closing_derived_with_total_identity(self):
        fields = _fields(opening=10, purchase=5, sales=4, closing=None)
        fields["total"] = 15.0
        status, info = classify_row(fields)
        self.assertEqual(status, RowStatus.CLOSING_DERIVED)
        self.assertEqual(info.get("derived_closing"), 11.0)
        self.assertTrue(info.get("total_identity_ok"))

    def test_bad_total_blocks_closing_derived(self):
        fields = _fields(opening=10, purchase=5, sales=4, closing=None)
        fields["total"] = 99.0
        status, info = classify_row(fields)
        self.assertIn(
            status,
            {
                RowStatus.COLUMN_ASSIGNMENT_SUSPECTED,
                RowStatus.OCR_VALUE_SUSPECTED,
            },
        )
        self.assertFalse(info.get("total_identity_ok"))

    def test_classify_row_never_mutates_input(self):
        fields = _fields(opening=100, purchase=30, sales=50, closing=120)
        before = copy.deepcopy(fields)
        classify_row(fields)
        self.assertEqual(fields, before)

    def test_read_row_fields_aliases(self):
        item = _item(
            opening=9,
            receipts=50,
            sales=21,
            closing=38,
            sales_return=0,
            expiry_damage=0,
        )
        fields = read_row_fields(item)
        self.assertEqual(fields["opening"], 9.0)
        self.assertEqual(fields["purchase"], 50.0)
        self.assertEqual(fields["sales"], 21.0)
        self.assertEqual(fields["sales_return"], 0.0)
        self.assertEqual(fields["expiry_damage"], 0.0)
        self.assertEqual(fields["closing"], 38.0)

    def test_would_fallback_26_of_32(self):
        items = []
        for index in range(6):
            items.append(
                _item(
                    name=f"OK {index}",
                    opening=10,
                    receipts=0,
                    sales=2,
                    closing=8,
                )
            )
        for index in range(26):
            items.append(
                _item(
                    name=f"BAD {index}",
                    opening=10,
                    receipts=0,
                    sales=2,
                    closing=99,
                )
            )
        summary = classify_result({"line_items": items, "totals": {"extra": {}}})
        should, reason = would_fallback(summary)
        self.assertTrue(should)
        self.assertEqual(reason, "valid_ratio_below_0.90")
        self.assertLess(summary["valid_ratio"], 0.90)
        self.assertEqual(summary["classified_rows"], 32)

    def test_structural_error_flag(self):
        status, _info = classify_row(
            _fields(opening=1, purchase=1, sales=1, closing=1),
            structural_error=True,
        )
        self.assertEqual(status, RowStatus.STRUCTURAL_ERROR)

    def test_shadow_summary_omits_rows(self):
        summary = classify_result(
            {
                "line_items": [
                    _item(name="A", opening=10, receipts=0, sales=2, closing=8)
                ],
                "totals": {"extra": {}},
            }
        )
        stored = shadow_summary_for_storage(summary)
        self.assertIn("row_status_counts", stored)
        self.assertIn("valid_ratio", stored)
        self.assertNotIn("rows", stored)


class ShadowPipelineSnapshotTests(unittest.TestCase):
    """ON vs OFF must leave line_items identical for fixtures under tests/fixtures/stock/."""

    FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "stock"

    def _fixture_files(self):
        if not self.FIXTURE_DIR.is_dir():
            return []
        files = []
        for path in sorted(self.FIXTURE_DIR.iterdir()):
            if path.suffix.lower() in {
                ".jpg",
                ".jpeg",
                ".png",
                ".pdf",
                ".txt",
                ".xlsx",
                ".xls",
                ".docx",
                ".doc",
            }:
                files.append(path)
        return files

    def test_shadow_flag_does_not_change_line_items(self):
        fixtures = self._fixture_files()
        if not fixtures:
            self.skipTest("no fixtures in tests/fixtures/stock/ yet")

        from services.sales_statement_extractor import extract_sales_statement

        for path in fixtures:
            data = path.read_bytes()
            with mock.patch.dict(os.environ, {"STOCK_ROW_CLASSIFIER_SHADOW": "true"}):
                on_result = extract_sales_statement(data, path.name)
            with mock.patch.dict(os.environ, {"STOCK_ROW_CLASSIFIER_SHADOW": "false"}):
                off_result = extract_sales_statement(data, path.name)

            on_items = json.loads(json.dumps(on_result.get("line_items") or []))
            off_items = json.loads(json.dumps(off_result.get("line_items") or []))
            # Shadow metadata may appear only when ON; strip before compare if present
            # on line items (classifier must not write per-row extras in Phase 0).
            self.assertEqual(on_items, off_items, msg=path.name)

            on_statements = on_result.get("statements")
            off_statements = off_result.get("statements")
            if on_statements or off_statements:
                def _items(payload):
                    rows = []
                    for statement in payload or []:
                        rows.extend(statement.get("line_items") or [])
                    return json.loads(json.dumps(rows))

                self.assertEqual(_items(on_statements), _items(off_statements), msg=path.name)


if __name__ == "__main__":
    unittest.main()
