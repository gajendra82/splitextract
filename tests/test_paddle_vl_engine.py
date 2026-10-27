"""Unit tests for PaddleOCR-VL 1.6 adapter (no heavy model load by default)."""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from services.ocr.orchestrator import OCROrchestrator, OCRSettings, ocr_settings
from services.ocr.paddle_vl_engine import (
    ENGINE_NAME,
    MODEL_NAME,
    PaddleVLEngine,
    evidence_from_result,
    extract_numeric_tokens,
    normalize_ocr_text,
)
from services.ocr.paddle_vl_io import load_phase3_manifest, page_needs_ocr


def _png_bytes(width: int = 32, height: int = 24) -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (width, height), "white").save(buf, format="PNG")
    return buf.getvalue()


class NumericTokenTests(unittest.TestCase):
    def test_extracts_pharma_numeric_shapes(self):
        text = "Qty 106 Bal 100 Rate 19892.88 GST 57005.00 glued 3337 and 3752"
        tokens = extract_numeric_tokens(text)
        self.assertIn("106", tokens)
        self.assertIn("100", tokens)
        self.assertIn("19892.88", tokens)
        self.assertIn("57005.00", tokens)
        self.assertIn("3337", tokens)
        self.assertIn("3752", tokens)

    def test_normalize_collapses_whitespace(self):
        self.assertEqual(normalize_ocr_text("  a\n b\t c "), "a b c")


class PaddleVLNormalizeTests(unittest.TestCase):
    def test_normalizes_block_star_keys(self):
        engine = PaddleVLEngine()
        raw = [
            {
                "res": {
                    "parsing_res_list": [
                        {
                            "block_label": "doc_title",
                            "block_content": "ADARSH PHARMA",
                            "block_bbox": [10, 20, 110, 40],
                            "block_order": 1,
                        },
                        {
                            "block_label": "table",
                            "block_content": "Opening 33 Issue 37 glued 3337",
                            "block_bbox": [10, 50, 400, 300],
                            "block_order": 2,
                        },
                    ]
                }
            }
        ]
        parsed = engine._normalize_prediction(raw, page=1)
        self.assertIn("ADARSH PHARMA", parsed["text"])
        self.assertIn("3337", parsed["text"])
        self.assertEqual(parsed["bbox_stats"]["valid"], 2)
        self.assertTrue(parsed["layout"]["available"])
        self.assertTrue(parsed["tables"]["available"])
        self.assertEqual(parsed["reading_order"], [1, 2])

    def test_evidence_shape(self):
        engine = PaddleVLEngine()
        with mock.patch.object(engine, "available", return_value=False):
            result = engine.recognize(_png_bytes(), page=2)
        evidence = evidence_from_result(result)
        self.assertEqual(evidence["engine"], ENGINE_NAME)
        self.assertEqual(evidence["model"], MODEL_NAME)
        self.assertIn("numeric_tokens", evidence)
        self.assertIn("boxes", evidence)
        self.assertIn("layout", evidence)


class ProductionSafetyTests(unittest.TestCase):
    def test_default_ocr_settings_paddleocr_vl_primary(self):
        with mock.patch.dict(
            os.environ,
            {
                "OCR_ENGINE": "",
                "OCR_FALLBACK_ENGINE": "",
                "OCR_ENSEMBLE_ENABLED": "false",
                "OCR_ALLOW_UNVERIFIED_FALLBACK": "false",
            },
            clear=False,
        ):
            # Empty OCR_ENGINE selects the production primary (PaddleOCR-VL).
            settings = ocr_settings()
        self.assertEqual(settings.primary_engine, "paddleocr-vl")
        self.assertEqual(settings.fallback_engine, "gemini")
        self.assertFalse(settings.ensemble_enabled)
        self.assertFalse(settings.allow_unverified_fallback)

    def test_tesseract_rollback_does_not_invoke_vl(self):
        orch = OCROrchestrator(
            settings=OCRSettings(
                primary_engine="tesseract",
                fallback_engine="paddle",
                ensemble_enabled=False,
            )
        )
        self.assertIsNotNone(orch.engine("paddleocr-vl-1.6"))
        self.assertIsNotNone(orch.engine("paddleocr-vl"))
        # Explicit rollback path must not invoke VL.
        with mock.patch.object(
            orch.engine("tesseract"),
            "available",
            return_value=True,
        ), mock.patch.object(
            orch.engine("tesseract"),
            "recognize",
            return_value=__import__(
                "services.ocr.base", fromlist=["OCRResult"]
            ).OCRResult(text="ok", engine="tesseract", metadata={"status": "success"}),
        ), mock.patch.object(
            orch.engine("paddleocr-vl-1.6"),
            "recognize",
            side_effect=AssertionError("VL must not run on tesseract rollback"),
        ):
            bundle = orch.recognize(_png_bytes())
        self.assertEqual(bundle.primary.engine, "tesseract")


class ManifestAndCheckpointTests(unittest.TestCase):
    def test_load_phase3_selection_manifest(self):
        phase3 = Path(__file__).resolve().parents[1] / "phase3_eval"
        items = load_phase3_manifest(phase3)
        self.assertEqual(len(items), 100)
        self.assertTrue(items[0]["document_id"])

    def test_checkpoint_resume_skips_completed(self):
        import importlib.util

        bench_path = (
            Path(__file__).resolve().parents[1]
            / "scripts"
            / "benchmark_paddle_vl_100.py"
        )
        spec = importlib.util.spec_from_file_location(
            "benchmark_paddle_vl_100", bench_path
        )
        bench = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(bench)

        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            per = out / "per_document"
            per.mkdir(parents=True)
            doc_id = "demo_doc.jpg"
            payload = {
                "document_id": doc_id,
                "status": "success",
                "pages": [
                    {
                        "page": 1,
                        "status": "success",
                        "latency_ms": 12.5,
                        "text": "hello 106",
                        "word_count": 2,
                        "numeric_tokens": ["106"],
                        "boxes": [[1, 2, 3, 4]],
                        "confidence": {"mean": None, "available": False},
                        "layout": {"available": False},
                        "tables": {"available": False, "count": 0},
                    }
                ],
            }
            path = per / f"{bench._safe_name(doc_id)}.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            loaded = bench._load_json(path)
            self.assertEqual(loaded["document_id"], doc_id)
            self.assertEqual(loaded["pages"][0]["latency_ms"], 12.5)


class PageRoutingTests(unittest.TestCase):
    def test_image_requires_ocr(self):
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as handle:
            handle.write(_png_bytes())
            path = Path(handle.name)
        try:
            needs, meta = page_needs_ocr(path)
            self.assertTrue(needs)
            self.assertTrue(meta["ocr_required"])
        finally:
            path.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
