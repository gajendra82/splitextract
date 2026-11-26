"""Regression: dhruvi pharma.xlsx short OP/PI/Sale/CLQty Stock & Sales grid.

Core extract must stay correct (98 rows + 8 qty/value totals). Also locks
footer label mapping, discrepancy classification, and pass/fail counters —
skipped rows are never counted as passes, and the 543-unit aggregate gap
is reported without inventing adjustments.
"""

from __future__ import annotations

import unittest
from pathlib import Path

from openpyxl import load_workbook

from services.extraction_quality import evaluate_extraction_quality
from services.sales_statement_extractor import (
    _find_stock_sales_op_pi_clqty_header,
    extract_sales_statement,
)
from services.stock_reconciliation import reconcile_row

FIXTURE = Path(r"C:\Users\amnsa\Downloads\dhruvi pharma.xlsx")

# Printed grand Total Value (not division subtots).
FOOTER_OPENING_VALUE = 966354.87
FOOTER_RECEIPTS_VALUE = 991405.50
FOOTER_SALES_VALUE = 941026.84
FOOTER_CLOSING_VALUE = 1025556.84

# Line-sum quantity / value totals (must match source workbook).
LINE_OPENING_QTY = 7888.0
LINE_RECEIPTS_QTY = 6313.0
LINE_SALES_QTY = 5921.0
LINE_CLOSING_QTY = 7737.0
AGGREGATE_QTY_VARIANCE = -543.0  # CLQty − (OP+PI−Sale)


def _source_product_rows():
    wb = load_workbook(FIXTURE, data_only=True)
    ws = wb.active
    rows = []
    for r in range(7, ws.max_row + 1):
        name = ws.cell(r, 1).value
        if name is None:
            continue
        text = str(name).strip()
        if not text or text.lower().startswith("division") or text.lower().startswith(
            "total"
        ):
            continue
        rows.append(
            {
                "product_name": text,
                "packing": ws.cell(r, 2).value,
                "opening_qty": float(ws.cell(r, 3).value or 0),
                "opening_value": float(ws.cell(r, 4).value or 0),
                "receipts_qty": float(ws.cell(r, 5).value or 0),
                "receipts_value": float(ws.cell(r, 6).value or 0),
                "sales_qty": float(ws.cell(r, 7).value or 0),
                "sales_value": float(ws.cell(r, 8).value or 0),
                "closing_qty": float(ws.cell(r, 9).value or 0),
                "closing_value": float(ws.cell(r, 10).value or 0),
            }
        )
    return rows


class TestDhruviOpPiHeader(unittest.TestCase):
    def test_short_header_maps_roles(self):
        found = _find_stock_sales_op_pi_clqty_header(
            [
                [
                    "Product Name",
                    "Packing",
                    "OP",
                    "OPVal",
                    "PI",
                    "PIVal",
                    "Sale",
                    "SI Value",
                    "CLQty",
                    "CLValue",
                ]
            ]
        )
        self.assertIsNotNone(found)
        _idx, roles = found
        self.assertEqual(roles["opening_qty"], 2)
        self.assertEqual(roles["purchase_qty"], 4)
        self.assertEqual(roles["sales_value"], 7)
        self.assertEqual(roles["closing_qty"], 8)
        self.assertEqual(roles["closing_value"], 9)


@unittest.skipUnless(FIXTURE.is_file(), "dhruvi pharma.xlsx not on this machine")
class TestDhruviStockSalesOpPiClqty(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = extract_sales_statement(FIXTURE.read_bytes(), FIXTURE.name)
        cls.extra = (cls.result.get("totals") or {}).get("extra") or {}
        cls.items = cls.result.get("line_items") or []
        cls.source = _source_product_rows()

    def test_engine_and_row_count(self):
        self.assertEqual(self.extra.get("extraction_method"), "stock_sales_op_pi_clqty")
        self.assertEqual(self.extra.get("parser_engine"), "openpyxl")
        self.assertFalse(self.extra.get("ocr_used"))
        self.assertFalse(self.extra.get("llm_used"))
        self.assertEqual(len(self.items), 98)
        self.assertEqual(len(self.source), 98)

    def test_all_rows_match_source_quantities_and_values(self):
        # Compare in sheet order — extractor collapses double spaces in names.
        self.assertEqual(len(self.items), len(self.source))
        for src, item in zip(self.source, self.items):
            label = src["product_name"]
            self.assertEqual(item["opening_qty"], src["opening_qty"], label)
            self.assertEqual(item["receipts_qty"], src["receipts_qty"], label)
            self.assertEqual(item["sales_qty"], src["sales_qty"], label)
            self.assertEqual(item["closing_qty"], src["closing_qty"], label)
            self.assertAlmostEqual(
                float(item["opening_value"]), src["opening_value"], places=2, msg=label
            )
            self.assertAlmostEqual(
                float(item["receipts_value"]), src["receipts_value"], places=2, msg=label
            )
            self.assertAlmostEqual(
                float(item["sales_value"]), src["sales_value"], places=2, msg=label
            )
            self.assertAlmostEqual(
                float(item["closing_value"]), src["closing_value"], places=2, msg=label
            )

    def test_eight_quantity_and_value_totals_unchanged(self):
        opening_qty = sum(float(i.get("opening_qty") or 0) for i in self.items)
        receipts_qty = sum(float(i.get("receipts_qty") or 0) for i in self.items)
        sales_qty = sum(float(i.get("sales_qty") or 0) for i in self.items)
        closing_qty = sum(float(i.get("closing_qty") or 0) for i in self.items)
        opening_value = sum(float(i.get("opening_value") or 0) for i in self.items)
        receipts_value = sum(float(i.get("receipts_value") or 0) for i in self.items)
        sales_value = sum(float(i.get("sales_value") or 0) for i in self.items)
        closing_value = sum(float(i.get("closing_value") or 0) for i in self.items)

        self.assertEqual(opening_qty, LINE_OPENING_QTY)
        self.assertEqual(receipts_qty, LINE_RECEIPTS_QTY)
        self.assertEqual(sales_qty, LINE_SALES_QTY)
        self.assertEqual(closing_qty, LINE_CLOSING_QTY)
        self.assertAlmostEqual(opening_value, FOOTER_OPENING_VALUE, places=2)
        self.assertAlmostEqual(receipts_value, FOOTER_RECEIPTS_VALUE, places=2)
        self.assertAlmostEqual(sales_value, FOOTER_SALES_VALUE, places=2)
        self.assertAlmostEqual(closing_value, FOOTER_CLOSING_VALUE, places=2)

        totals = self.result["totals"]
        self.assertEqual(totals.get("sales_value"), FOOTER_SALES_VALUE)
        self.assertEqual(totals.get("closing_value"), FOOTER_CLOSING_VALUE)
        self.assertEqual(self.extra.get("opening_value"), FOOTER_OPENING_VALUE)
        self.assertEqual(self.extra.get("receipts_value"), FOOTER_RECEIPTS_VALUE)
        self.assertEqual(self.extra.get("purchase_value"), FOOTER_RECEIPTS_VALUE)

    def test_footer_maps_only_labeled_value_columns(self):
        self.assertEqual(
            self.extra.get("footer_financial_source"), "xls_op_pi_clqty_total_value"
        )
        self.assertEqual(
            self.extra.get("footer_detected_columns"),
            ["opening_value", "receipts_value", "sales_value", "closing_value"],
        )
        self.assertEqual(self.extra.get("footer_financial_missing_columns"), [])
        self.assertTrue(self.extra.get("footer_financial_complete"))
        # No unlabeled alias substitution from OPVal / product cells.
        self.assertIsNone(self.extra.get("outward_value"))
        self.assertNotEqual(self.result["totals"].get("closing_value"), FOOTER_OPENING_VALUE)
        footer = self.extra.get("footer_financials") or {}
        self.assertEqual(footer.get("opening_value"), FOOTER_OPENING_VALUE)
        self.assertEqual(footer.get("receipts_value"), FOOTER_RECEIPTS_VALUE)
        self.assertEqual(footer.get("sales_value"), FOOTER_SALES_VALUE)
        self.assertEqual(footer.get("closing_value"), FOOTER_CLOSING_VALUE)
        self.assertNotIn("outward_value", footer)

    def test_quality_footer_flags_are_consistent(self):
        quality = evaluate_extraction_quality(self.result)
        fin = (quality.get("dimension_scores") or {}).get("financial") or {}
        self.assertTrue(fin.get("complete"))
        self.assertEqual(fin.get("score"), 100)
        self.assertNotIn("missing_footer_financials", quality.get("reasons") or [])
        # Stamped complete must not conflict with quality reasons.
        self.assertTrue(self.extra.get("footer_financial_complete"))

    def test_discrepancy_classification_and_pass_fail_counters(self):
        reconciled = 0
        discrepancy = 0
        skipped = 0
        for item in self.items:
            recon = reconcile_row(item)
            classification = recon.get("classification")
            extra = item.get("extra") or {}
            self.assertEqual(
                extra.get("reconciliation_classification"), classification
            )
            # Printed CLQty is never rewritten to force balance.
            self.assertEqual(
                recon.get("extracted_closing_qty"), float(item["closing_qty"])
            )
            if classification == "reconciled":
                reconciled += 1
                self.assertTrue(recon.get("valid"))
            elif classification == "discrepancy":
                discrepancy += 1
                # Printed CLQty is document truth — discrepancy is informational.
                self.assertTrue(recon.get("valid"))
                self.assertTrue(recon.get("closing_ok"))
                self.assertIsNotNone(recon.get("quantity_difference"))
                self.assertGreater(abs(float(recon["quantity_difference"])), 0.05)
            elif classification == "skipped":
                skipped += 1
                self.assertFalse(recon.get("valid"))
            else:
                self.fail(f"unexpected classification {classification!r}")

        self.assertEqual(reconciled + discrepancy + skipped, 98)
        self.assertEqual(reconciled, 54)
        self.assertEqual(discrepancy, 44)
        self.assertEqual(skipped, 0)

        # All printed rows pass; discrepancy_count still reports formula gaps.
        self.assertEqual(self.extra.get("stock_reconciliation_pass_count"), 98)
        self.assertEqual(self.extra.get("stock_reconciliation_fail_count"), 0)
        self.assertEqual(self.extra.get("stock_reconciliation_skip_count"), skipped)
        self.assertEqual(
            self.extra.get("stock_reconciliation_discrepancy_count"), discrepancy
        )
        self.assertEqual(
            self.extra.get("stock_reconciliation_reconciled_count"), reconciled
        )
        self.assertEqual(
            self.extra.get("stock_reconciliation_pass_count")
            + self.extra.get("stock_reconciliation_fail_count")
            + self.extra.get("stock_reconciliation_skip_count"),
            98,
        )

    def test_aggregate_543_unit_variance_reported(self):
        self.assertEqual(self.extra.get("aggregate_qty_variance"), AGGREGATE_QTY_VARIANCE)
        validation = self.extra.get("stock_validation") or {}
        self.assertEqual(validation.get("aggregate_qty_variance"), AGGREGATE_QTY_VARIANCE)
        self.assertEqual(validation.get("closing_qty_variance"), AGGREGATE_QTY_VARIANCE)
        self.assertEqual(validation.get("extracted_closing"), LINE_CLOSING_QTY)
        self.assertEqual(
            validation.get("calculated_closing"),
            LINE_OPENING_QTY + LINE_RECEIPTS_QTY - LINE_SALES_QTY,
        )
        # Printed document values are authoritative — formula gap is informational.
        self.assertTrue(validation.get("is_valid"))
        self.assertTrue(validation.get("printed_values_authoritative"))
        self.assertEqual(validation.get("row_discrepancy_count"), 44)
        self.assertEqual(validation.get("row_reconciled_count"), 54)
        self.assertEqual(validation.get("row_skipped_count"), 0)
        # Document-gap discrepancies live on recon counters; identity fail
        # stays 0 so quality/Laravel do not drop products or recalculate.
        self.assertEqual(self.extra.get("stock_identity_fail_count"), 0)
        self.assertTrue(self.extra.get("stock_document_gap"))
        self.assertTrue(self.extra.get("do_not_recalculate_totals"))
        self.assertEqual(self.extra.get("stock_reconciliation_fail_count"), 0)
        self.assertEqual(self.extra.get("stock_reconciliation_pass_count"), 98)
        self.assertEqual(self.extra.get("stock_reconciliation_discrepancy_count"), 44)
        # Every printed product stays displayable (not identity-failed).
        for item in self.items:
            self.assertTrue(
                (item.get("extra") or {}).get("stock_identity_ok"),
                item.get("product_name"),
            )
            self.assertNotEqual(
                (item.get("extra") or {}).get("reconciliation_failed"), True
            )
            recon = (item.get("extra") or {}).get("reconciliation") or {}
            self.assertTrue(recon.get("valid"), item.get("product_name"))


class TestShortOpPiReconcileUnit(unittest.TestCase):
    def test_balanced_row_is_reconciled(self):
        item = {
            "product_name": "BALANCED",
            "opening_qty": 10.0,
            "receipts_qty": 5.0,
            "sales_qty": 3.0,
            "closing_qty": 12.0,
            "extra": {"layout": "stock_sales_op_pi_clqty"},
        }
        recon = reconcile_row(item)
        self.assertEqual(recon.get("classification"), "reconciled")
        self.assertTrue(recon.get("valid"))
        self.assertEqual(recon.get("quantity_difference"), 0.0)

    def test_gap_row_is_discrepancy_keeps_printed_closing(self):
        item = {
            "product_name": "GAP",
            "opening_qty": 67.0,
            "receipts_qty": 0.0,
            "sales_qty": 8.0,
            "closing_qty": 49.0,  # printed; expected would be 59
            "extra": {
                "layout": "stock_sales_op_pi_clqty",
                "stock_identity_skipped": "short_op_pi_clqty_doc_gap",
            },
        }
        recon = reconcile_row(item)
        self.assertEqual(recon.get("classification"), "discrepancy")
        self.assertTrue(recon.get("valid"))
        self.assertTrue(recon.get("closing_ok"))
        self.assertTrue(recon.get("printed_values_authoritative"))
        self.assertTrue(recon.get("document_qty_gap"))
        self.assertEqual(recon.get("extracted_closing_qty"), 49.0)
        self.assertEqual(recon.get("expected_closing_qty"), 59.0)
        self.assertEqual(recon.get("quantity_difference"), -10.0)

    def test_missing_closing_is_skipped_not_valid(self):
        item = {
            "product_name": "NO CLOSE",
            "opening_qty": 1.0,
            "receipts_qty": 0.0,
            "sales_qty": 0.0,
            "closing_qty": None,
            "extra": {"layout": "stock_sales_op_pi_clqty"},
        }
        recon = reconcile_row(item)
        self.assertEqual(recon.get("classification"), "skipped")
        self.assertFalse(recon.get("valid"))
        self.assertEqual(recon.get("skipped"), "short_op_pi_clqty_missing_closing")


if __name__ == "__main__":
    unittest.main()
