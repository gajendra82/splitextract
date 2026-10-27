"""Unit tests for Surya 2 experimental OCR adapter (no heavy model load)."""

from __future__ import annotations

import io
import os
import unittest
from unittest import mock

from services.ocr.base import OCRResult, OCRWord
from services.ocr.orchestrator import OCROrchestrator, OCRSettings, ocr_settings
from services.ocr.surya_engine import (
    ENGINE_NAME,
    MODEL_NAME,
    SuryaEngine,
    evidence_from_result,
)


def _png_bytes(width: int = 32, height: int = 24) -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (width, height), "white").save(buf, format="PNG")
    return buf.getvalue()


class SuryaNormalizeTests(unittest.TestCase):
    def test_normalizes_blocks_with_html_table(self):
        engine = SuryaEngine()

        class Block:
            def __init__(self, **kwargs):
                self.__dict__.update(kwargs)

        class Page:
            blocks = [
                Block(
                    label="Text",
                    html="<p>ADARSH PHARMA</p>",
                    bbox=[10, 20, 110, 40],
                    confidence=0.9,
                    order=0,
                ),
                Block(
                    label="Table",
                    html="<table><tr><td>PROD A</td><td>37</td></tr></table>",
                    bbox=[10, 50, 400, 300],
                    confidence=0.8,
                    order=1,
                ),
            ]

        parsed = engine._normalize_prediction(Page(), page=1)
        self.assertIn("ADARSH PHARMA", parsed["text"])
        self.assertIn("<table", parsed["text"].lower())
        self.assertIn("37", parsed["text"])
        self.assertTrue(parsed["tables"]["available"])
        self.assertEqual(parsed["bbox_stats"]["valid"], 2)

    def test_evidence_shape_when_unavailable(self):
        engine = SuryaEngine()
        with mock.patch.object(engine, "available", return_value=False):
            engine._init_error = "surya_not_installed"
            engine._block_reason = "BLOCKED: surya-ocr package not installed"
            result = engine.recognize(_png_bytes(), page=2)
        evidence = evidence_from_result(result)
        self.assertEqual(evidence["engine"], ENGINE_NAME)
        self.assertEqual(evidence["model"], MODEL_NAME)
        self.assertEqual(evidence["status"], "error")
        self.assertIn("block_reason", evidence)


class SuryaAvailabilityTests(unittest.TestCase):
    def test_backend_probe_reports_blocked_without_backend(self):
        engine = SuryaEngine()
        with mock.patch.dict(
            os.environ,
            {"SURYA_INFERENCE_URL": "", "LLAMA_CPP_BINARY": ""},
            clear=False,
        ), mock.patch.object(
            engine, "_find_llama_server", return_value=None
        ), mock.patch(
            "services.ocr.surya_engine.shutil.which", return_value=None
        ):
            ok, detail = engine._backend_probe()
        self.assertFalse(ok)
        self.assertIn("BLOCKED", detail)


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
            settings = ocr_settings()
        self.assertEqual(settings.primary_engine, "paddleocr-vl")
        self.assertEqual(settings.fallback_engine, "gemini")
        self.assertFalse(settings.ensemble_enabled)

    def test_surya_is_opt_in_only(self):
        orch = OCROrchestrator(
            settings=OCRSettings(
                primary_engine="tesseract",
                fallback_engine="paddle",
                ensemble_enabled=False,
            )
        )
        self.assertIsNotNone(orch.engine("surya-2"))
        self.assertIsNotNone(orch.engine("surya"))
        with mock.patch.object(
            orch.engine("tesseract"),
            "available",
            return_value=True,
        ), mock.patch.object(
            orch.engine("tesseract"),
            "recognize",
            return_value=OCRResult(text="ok", engine="tesseract"),
        ), mock.patch.object(
            orch.engine("surya-2"),
            "recognize",
            side_effect=AssertionError("Surya must not run by default"),
        ):
            bundle = orch.recognize(_png_bytes())
        self.assertEqual(bundle.primary.engine, "tesseract")


class CompareScriptHelpersTests(unittest.TestCase):
    def test_shared_scoring_alignment(self):
        import importlib.util
        from pathlib import Path

        script = (
            Path(__file__).resolve().parents[1]
            / "scripts"
            / "compare_surya_vs_ocr_baselines_gt20.py"
        )
        spec = importlib.util.spec_from_file_location("compare_surya_gt20", script)
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        gt_docs = [
            {
                "document_id": "demo.jpg",
                "page": 1,
                "source": {"file": "demo.jpg", "source_type": "image", "source_verified": True},
                "fields": [
                    {
                        "field_id": "q1",
                        "field_type": "quantity",
                        "printed_header": "Issues",
                        "expected_raw": "37",
                        "verified": True,
                        "source_verified": True,
                        "priority": "high",
                    }
                ],
            }
        ]

        def load_ok(_d, _p):
            return {"status": "success", "text": "Issues 37", "numeric_tokens": ["37"]}

        def load_missing(_d, _p):
            return None

        m1, _, _ = mod._score_engine(gt_docs, engine_name="surya-2", load_page=load_ok)
        m2, _, _ = mod._score_engine(gt_docs, engine_name="tesseract", load_page=load_missing)
        self.assertEqual(len(m1), 1)
        self.assertEqual(len(m2), 1)
        self.assertEqual(m1[0]["field_id"], m2[0]["field_id"])
        self.assertTrue(m1[0]["correct"])
        self.assertEqual(m2[0]["error_type"], "MISSING")


if __name__ == "__main__":
    unittest.main()
