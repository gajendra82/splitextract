"""Decision rules for the ZA engine choice. They do not call a model."""

from __future__ import annotations

import unittest

from scripts.za_181_engine_decision import choose_engine, proxy_rows_from_review
from services.ocr.orchestrator import ocr_settings


def _summary(f1, quantity, silent_rate=0.1, scored=100, both=50, rows=10):
    return {
        "product_f1": f1,
        "quantity_accuracy": quantity,
        "product_rows": rows,
        "quantity_scored": scored,
        "quantity_both_present": both,
        "silent_wrong_rate": silent_rate,
        "median_image_f1": f1,
        "median_ms": 1000,
    }


def _metrics(tess_f1, paddle_f1, tess_qty, paddle_qty, tess_silent=0.2, paddle_silent=0.1):
    return {
        "source_verified": {
            "tesseract": _summary(tess_f1, tess_qty, tess_silent),
            "paddleocr-vl": _summary(paddle_f1, paddle_qty, paddle_silent),
        },
        "ai_proposal_proxy": {
            "images": 51,
            "tesseract": _summary(tess_f1, tess_qty, tess_silent),
            "paddleocr-vl": _summary(paddle_f1, paddle_qty, paddle_silent),
        },
        "unlabeled_cache": {
            "tesseract": {"median_ms": 6000},
            "paddleocr-vl": {"median_ms": 150000},
        },
    }


class DecisionRuleTests(unittest.TestCase):
    def test_proxy_ignores_price_blank_and_duplicate_quantities(self):
        rows, stats = proxy_rows_from_review(
            {
                "rows": [
                    {
                        "row_id": "r01",
                        "origin": "ai_proposed",
                        "proposed_product_name": "Cystone Tab",
                        "columns": [
                            {"semantic": "not_quantity", "proposed_raw": "10", "printed_header": "Amount"},
                            {"semantic": "closing_qty", "proposed_raw": "", "printed_header": "Closing"},
                            {"semantic": "opening_qty", "proposed_raw": "4", "printed_header": "Opening"},
                            {"semantic": "opening_qty", "proposed_raw": "9", "printed_header": "Opening"},
                        ],
                    }
                ]
            }
        )
        self.assertEqual(rows[0]["product_name"]["status"], "ai_proposed")
        self.assertEqual(rows[0]["closing_qty"]["status"], "absent")
        self.assertIsNone(rows[0]["closing_qty"]["normalized"])
        self.assertEqual(rows[0]["opening_qty"]["status"], "absent")
        self.assertEqual(stats["non_quantity_columns_ignored"], 1)
        self.assertEqual(stats["blank_quantities_ignored"], 1)
        self.assertEqual(stats["ambiguous_quantity_columns"], 1)

    def test_higher_extraction_scores_select_paddle_and_keep_tesseract_rollback(self):
        decision = choose_engine(_metrics(0.36, 0.51, 0.44, 0.47))
        self.assertEqual(decision["selected_engine"], "paddleocr-vl")
        self.assertEqual(decision["configuration"]["OCR_ENGINE"], "paddleocr-vl")
        self.assertEqual(decision["configuration"]["OCR_FALLBACK_ENGINE"], "gemini")
        self.assertEqual(decision["rollback_engine"], "tesseract")
        self.assertNotIn("OCR_ENSEMBLE_ENABLED", decision["configuration"])
        self.assertGreater(decision["points"]["paddleocr-vl"], decision["points"]["tesseract"])

    def test_tied_extraction_prefers_the_lower_silent_rate(self):
        decision = choose_engine(_metrics(0.5, 0.5, 0.5, 0.5, tess_silent=0.05, paddle_silent=0.2))
        self.assertEqual(decision["selected_engine"], "tesseract")

    def test_production_primary_matches_the_written_decision(self):
        import os
        from unittest.mock import patch

        from scripts.za_181_engine_decision import DECISION_MD

        text = DECISION_MD.read_text(encoding="utf-8")
        self.assertIn("Selected engine: **paddleocr-vl**.", text)
        with patch.dict(os.environ, {"OCR_ENGINE": "", "OCR_FALLBACK_ENGINE": "", "OCR_ENSEMBLE_ENABLED": "false"}, clear=False):
            settings = ocr_settings()
        self.assertEqual(settings.primary_engine, "paddleocr-vl")
        self.assertEqual(settings.fallback_engine, "gemini")
        self.assertFalse(settings.ensemble_enabled)


if __name__ == "__main__":
    unittest.main()
