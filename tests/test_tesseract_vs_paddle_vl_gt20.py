"""Tests for Tesseract vs PaddleOCR-VL ground-truth comparison helpers."""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional

from services.ground_truth.metrics import aggregate_metrics

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "compare_tesseract_paddle_vl_gt20.py"


def _load_compare_module():
    spec = importlib.util.spec_from_file_location("compare_tesseract_paddle_vl_gt20", SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class CompareHelpersTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_compare_module()

    def test_score_engine_aligns_fields_when_ocr_missing(self):
        gt_docs = [
            {
                "document_id": "doc_a.pdf",
                "page": 1,
                "source": {"file": "doc_a.pdf", "source_type": "pdf", "source_verified": True},
                "fields": [
                    {
                        "field_id": "q1",
                        "field_type": "quantity",
                        "printed_header": "Issues",
                        "expected_raw": "12",
                        "verified": True,
                        "source_verified": True,
                        "priority": "high",
                    },
                    {
                        "field_id": "p1",
                        "field_type": "price",
                        "printed_header": "Rate",
                        "expected_raw": "10.50",
                        "verified": True,
                        "source_verified": True,
                        "priority": "high",
                    },
                ],
            }
        ]

        def load_missing(_doc_id: str, _page: int) -> Optional[Dict[str, Any]]:
            return None

        matches, per_doc, metrics = self.mod._score_engine(
            gt_docs, engine_name="tesseract", load_page=load_missing
        )
        self.assertEqual(len(matches), 2)
        self.assertTrue(all(m.get("error_type") == "MISSING" for m in matches))
        self.assertEqual(per_doc[0]["status"], "OCR_UNAVAILABLE")
        self.assertEqual(metrics["verified_fields"], 2)
        self.assertEqual(metrics["correct_fields"], 0)

    def test_shared_field_keys_identical_across_engines(self):
        gt_docs = [
            {
                "document_id": "doc_b.jpg",
                "page": 1,
                "source": {"file": "doc_b.jpg", "source_type": "image", "source_verified": True},
                "fields": [
                    {
                        "field_id": "qty_issues",
                        "field_type": "quantity",
                        "printed_header": "Issues",
                        "product_key": "PROD A",
                        "expected_raw": "37",
                        "verified": True,
                        "source_verified": True,
                        "priority": "high",
                    }
                ],
            }
        ]

        def load_vl(_doc_id: str, _page: int) -> Dict[str, Any]:
            return {
                "status": "success",
                "text": "<table><tr><td>PROD A</td><td>Issues</td><td>37</td></tr></table>",
                "numeric_tokens": ["37"],
            }

        def load_tess(_doc_id: str, _page: int) -> Dict[str, Any]:
            return {
                "status": "success",
                "text": "PROD A Issues 37",
                "numeric_tokens": ["37"],
            }

        vl_matches, _, _ = self.mod._score_engine(
            gt_docs, engine_name="paddleocr-vl-1.6", load_page=load_vl
        )
        tess_matches, _, _ = self.mod._score_engine(
            gt_docs, engine_name="tesseract", load_page=load_tess
        )
        vl_keys = {(m["document_id"], m["field_id"]) for m in vl_matches}
        tess_keys = {(m["document_id"], m["field_id"]) for m in tess_matches}
        self.assertEqual(vl_keys, tess_keys)
        self.assertEqual(len(vl_keys), 1)

    def test_aggregate_never_invents_empty_type_accuracy(self):
        matches: List[Dict[str, Any]] = [
            {
                "field_type": "quantity",
                "correct": True,
                "silent_wrong": False,
                "critical": False,
                "predicted_raw": "1",
            }
        ]
        metrics = aggregate_metrics(matches)
        self.assertIsNone(metrics["price_accuracy"])
        self.assertIsNone(metrics["date_accuracy"])
        self.assertEqual(metrics["quantity_accuracy"], 1.0)

    def test_delta_helper(self):
        self.assertAlmostEqual(self.mod._delta(0.9, 0.4), 0.5)
        self.assertIsNone(self.mod._delta(None, 0.4))
        self.assertIsNone(self.mod._delta(0.9, None))


if __name__ == "__main__":
    unittest.main()
