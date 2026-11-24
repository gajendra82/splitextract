"""Opt-in check against printed rows. Live OCR and Gemini stay off.

RUN_CONTROLLED_VALIDATION=1 scores the Narayan Daily Stock Summary rows
that are written in this file. Those three rows are the ground truth.

RUN_LIVE_EXTRACTION=1 is required before any OCR or Gemini call. This
module does not call them. It records that the live section was not run.
"""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

_CONTROLLED = os.environ.get("RUN_CONTROLLED_VALIDATION", "").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
_LIVE = os.environ.get("RUN_LIVE_EXTRACTION", "").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}

# Printed Daily Stock Summary. Cl. Stock = Op + Pur - Sales Retu. - Sales + Adj./Dmg.
_NARAYAN = """
NARAYAN MEDICAL (NEW)                                                02/09/2026
BHANDERHATI, HOOGHLY                                                 17:26:01
** Daily Stock Summary (Company-wise) **          From 01/08/2026 to 31/08/2026
Company:HIMALAYA ZEUS [M177]
        Product & Pack        |  Op. | Pur. |Sales|Sales| Adj./| Cl.  | Order |
                              | Stock| Qty. |Retu.| Qty.| Dmg. |Stock | Qty.  |
AACTARIL SOAP,75GM                 62      0     0    10      0     52     -52
CLARINA ANTI ACNE FACEWAS,60ML     51      0     0     9     -1     41     -41
HIORA K TOOTH PASTE,100GM           0     50     0    10      0     40     -40
"""

_TRUTH = {
    "AACTARIL SOAP": {
        "opening_qty": 62.0,
        "receipts_qty": 0.0,
        "sales_qty": 10.0,
        "closing_qty": 52.0,
        "sales_return_qty": 0.0,
        "adj_dmg_qty": 0.0,
    },
    "CLARINA ANTI ACNE FACEWAS": {
        "opening_qty": 51.0,
        "receipts_qty": 0.0,
        "sales_qty": 9.0,
        "closing_qty": 41.0,
        "sales_return_qty": 0.0,
        "adj_dmg_qty": -1.0,
    },
    "HIORA K TOOTH PASTE": {
        "opening_qty": 0.0,
        "receipts_qty": 50.0,
        "sales_qty": 10.0,
        "closing_qty": 40.0,
        "sales_return_qty": 0.0,
        "adj_dmg_qty": 0.0,
    },
}


@unittest.skipUnless(
    _CONTROLLED,
    "controlled validation is opt-in via RUN_CONTROLLED_VALIDATION=1",
)
class ControlledPrintedValidation(unittest.TestCase):
    def test_narayan_printed_rows(self):
        from services.sales_statement_extractor import extract_sales_statement

        def boom(_payload):
            raise AssertionError("controlled text validation must not call Gemini")

        with patch("services.recovery_runner._live_gemini_caller", side_effect=boom):
            result = extract_sales_statement(_NARAYAN.encode("utf-8"), "narayan.txt")
        by_name = {item["product_name"]: item for item in result["line_items"]}
        self.assertEqual(set(by_name), set(_TRUTH))
        fields = 0
        mismatches = 0
        for name, expected in _TRUTH.items():
            item = by_name[name]
            extra = item.get("extra") or {}
            for field, value in expected.items():
                fields += 1
                actual = item.get(field) if field in item else extra.get(field)
                if actual != value:
                    mismatches += 1
        grounding = ((result.get("totals") or {}).get("extra") or {}).get(
            "source_grounding"
        ) or {}
        decision = (result.get("document_intelligence") or {}).get("decision") or {}
        self.assertEqual(mismatches, 0)
        self.assertEqual(fields, 18)
        self.assertEqual(grounding.get("missing_rows") or 0, 0)
        self.assertEqual(grounding.get("duplicate_rows") or 0, 0)
        self.assertEqual(grounding.get("discrepancy_count") or 0, 0)
        self.assertEqual(decision.get("status"), "VERIFIED")
        self.assertTrue(decision.get("verified"))
        self.assertIn((result.get("phase2") or {}).get("status"), (None, "not_needed"))


@unittest.skipUnless(
    _LIVE,
    "live OCR and Gemini recovery are opt-in via RUN_LIVE_EXTRACTION=1",
)
class LiveOcrAndRecoveryEvaluation(unittest.TestCase):
    def test_live_calls_stay_at_zero_without_a_corpus(self):
        self.skipTest(
            "no authorized live corpus; ocr_calls=0; gemini_calls=0; "
            "field accuracy, false acceptance, and latency were not measured"
        )
