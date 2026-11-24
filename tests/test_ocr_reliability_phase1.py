"""Regression: OCR page cache, timeout policy, empty-not-success, recovery order."""

from __future__ import annotations

import os
import unittest
from unittest.mock import MagicMock, patch

from services.ocr.base import OCRResult
from services.ocr.orchestrator import OCRBundle
from services.ocr.page_cache import (
    clear_page_cache,
    get_cached_page,
    page_cache_key,
    put_cached_page,
)
from services.ocr.timeout_policy import describe_policy, ocr_timeout_policy


class TimeoutPolicyTests(unittest.TestCase):
    def test_default_per_page_is_180(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("OCR_PER_PAGE_TIMEOUT_S", None)
            os.environ.pop("OCR_EXECUTION_TIMEOUT_S", None)
            os.environ.pop("PADDLE_VL_TIMEOUT_S", None)
            policy = ocr_timeout_policy()
        self.assertEqual(policy.per_page_timeout_s, 180.0)
        self.assertFalse(policy.retry_on_timeout)
        desc = describe_policy(policy)
        self.assertEqual(desc["effective_page_timeout_s"], 180.0)

    def test_page_timeout_capped_by_execution(self):
        with patch.dict(
            os.environ,
            {
                "OCR_PER_PAGE_TIMEOUT_S": "600",
                "OCR_EXECUTION_TIMEOUT_S": "90",
            },
            clear=False,
        ):
            policy = ocr_timeout_policy()
        self.assertEqual(policy.effective_page_timeout_s, 90.0)


class PageCacheTests(unittest.TestCase):
    def setUp(self):
        clear_page_cache(disk=False)
        os.environ["OCR_PAGE_CACHE"] = "true"
        os.environ["OCR_PAGE_CACHE_DIR"] = "off"

    def tearDown(self):
        clear_page_cache(disk=False)

    def test_never_caches_empty(self):
        key = page_cache_key(b"img-empty", engine="paddleocr-vl", page=1)
        empty = OCRBundle(
            primary=OCRResult(text="", engine="paddleocr-vl", page=1),
            engines_used=["paddleocr-vl"],
            metadata={"status": "success"},
        )
        self.assertFalse(put_cached_page(key, empty))
        self.assertIsNone(get_cached_page(key))

    def test_never_caches_timeout(self):
        key = page_cache_key(b"img-timeout", engine="paddleocr-vl", page=1)
        timed = OCRBundle(
            primary=OCRResult(
                text="Product Name Pack Open Recd Sales Close enough text here",
                engine="paddleocr-vl",
                page=1,
                timed_out=True,
                metadata={"status": "timeout"},
            ),
            engines_used=["paddleocr-vl"],
            metadata={"status": "timeout"},
        )
        self.assertFalse(put_cached_page(key, timed))
        self.assertIsNone(get_cached_page(key))

    def test_roundtrip_usable(self):
        key = page_cache_key(b"img-ok", engine="paddleocr-vl", page=1)
        text = "Product Name Pack Open Recd Sales Close\nGASEX 100TAB 10 5 3 12"
        ok = OCRBundle(
            primary=OCRResult(
                text=text,
                engine="paddleocr-vl",
                page=1,
                metadata={"status": "success"},
            ),
            engines_used=["paddleocr-vl"],
            metadata={"status": "success"},
        )
        self.assertTrue(put_cached_page(key, ok))
        hit = get_cached_page(key)
        self.assertIsNotNone(hit)
        self.assertEqual(hit.primary.text, text)
        self.assertTrue(hit.metadata.get("ocr_cache_hit"))


class RecognizePageResilientTests(unittest.TestCase):
    def setUp(self):
        clear_page_cache(disk=False)
        os.environ["OCR_PAGE_CACHE"] = "true"
        os.environ["OCR_PAGE_CACHE_DIR"] = "off"
        os.environ["OCR_PAGE_RETRY"] = "false"
        os.environ["OCR_RETRY_ON_TIMEOUT"] = "false"
        os.environ["OCR_PER_PAGE_TIMEOUT_S"] = "30"
        os.environ["OCR_RECOVERY_CHAIN"] = "tesseract,gemini"

    def tearDown(self):
        clear_page_cache(disk=False)

    def test_kills_serve_on_page_timeout_then_recovers_tesseract(self):
        from services.document_intelligence.pipeline import _recognize_page_resilient
        from services.ocr.bounded import OcrExecutionTimeout

        tess = MagicMock()
        tess.available.return_value = True
        tess.recognize.return_value = OCRResult(
            text="Product Name Pack Open Recd Sales Close\nROW 1 2 3 4 5",
            engine="tesseract",
            page=1,
        )
        gem = MagicMock()
        gem.available.return_value = True

        ocr = MagicMock()
        ocr.settings.primary_engine = "paddleocr-vl"
        ocr.recognize.side_effect = OcrExecutionTimeout(30.0, "ocr_page_1")
        ocr.engine.side_effect = lambda name: {"tesseract": tess, "gemini": gem}.get(name)

        with patch(
            "services.ocr.timeout_policy.kill_primary_vl_on_timeout", return_value=True
        ) as kill:
            with patch(
                "services.ocr.page_preprocess.preprocess_page_image",
                return_value=(b"raw", {"ok": True}),
            ):
                with patch(
                    "services.ocr.page_preprocess.prepare_for_ocr",
                    side_effect=lambda b: b,
                ):
                    bundle, meta = _recognize_page_resilient(ocr, b"page-bytes", page_no=1)

        self.assertTrue(kill.called)
        self.assertEqual(getattr(bundle.primary, "engine", None), "tesseract")
        self.assertFalse(meta.get("timed_out"))
        self.assertEqual((meta.get("ocr_recovery") or {}).get("accepted_engine"), "tesseract")
        # Empty primary timeout must not have been cached as success for VL.
        self.assertNotEqual(meta.get("outcome"), "success")

    def test_cache_hit_skips_recognize(self):
        from services.document_intelligence.pipeline import _recognize_page_resilient

        text = "Product Name Pack Open Recd Sales Close\nCACHED ROW 1 2 3 4"
        ocr = MagicMock()
        ocr.settings.primary_engine = "paddleocr-vl"

        with patch(
            "services.ocr.page_preprocess.preprocess_page_image",
            return_value=(b"same-bytes", {"ok": True}),
        ):
            with patch(
                "services.ocr.page_preprocess.prepare_for_ocr",
                side_effect=lambda b: b,
            ):
                # Seed cache via first call success
                ocr.recognize.return_value = OCRBundle(
                    primary=OCRResult(
                        text=text,
                        engine="paddleocr-vl",
                        page=1,
                        metadata={"status": "success"},
                    ),
                    engines_used=["paddleocr-vl"],
                    metadata={"status": "success"},
                )
                first, meta1 = _recognize_page_resilient(ocr, b"page", page_no=1)
                self.assertFalse(meta1.get("cache_hit"))
                self.assertEqual(first.primary.text, text)

                ocr.recognize.reset_mock()
                second, meta2 = _recognize_page_resilient(ocr, b"page", page_no=1)
                self.assertTrue(meta2.get("cache_hit"))
                self.assertEqual(second.primary.text, text)
                ocr.recognize.assert_not_called()


class StampEvidenceTests(unittest.TestCase):
    def test_does_not_silently_mark_timeout_success(self):
        from services.document_intelligence.pipeline import (
            _stamp_ocr_evidence_from_extraction,
        )

        evidence = {
            "pages": [
                {
                    "page": 1,
                    "status": "timeout",
                    "error": "ocr timed out",
                    "text_length": 0,
                    "tables": {"available": False, "count": 0},
                }
            ]
        }
        result = {
            "stockist_name": "OM PHARMA",
            "company_name": "HIMALAYA",
            "line_items": [{"product_name": "GASEX", "opening_qty": 1}],
            "totals": {
                "extra": {
                    "ocr_table_html": "<table><tr><td>Product</td></tr></table>",
                    "ocr_source": "paddleocr-vl",
                    "source_evidence": True,
                }
            },
        }
        with patch(
            "services.source_evidence.has_valid_source_evidence", return_value=True
        ):
            _stamp_ocr_evidence_from_extraction(result, evidence, evidence)
        self.assertEqual(evidence["pages"][0]["status"], "timeout")
        self.assertTrue(evidence["pages"][0].get("extraction_has_source_evidence"))
        self.assertTrue(evidence["pages"][0]["tables"]["available"])


class RecoveryOrderPreservedTests(unittest.TestCase):
    def test_default_chain_tess_then_gemini(self):
        from services.ocr.recovery_chain import recovery_chain_engines

        with patch.dict(os.environ, {"OCR_RECOVERY_CHAIN": ""}, clear=False):
            os.environ.pop("OCR_RECOVERY_CHAIN", None)
            self.assertEqual(recovery_chain_engines(), ["tesseract", "gemini"])


class Phase2DefaultOffTests(unittest.TestCase):
    def test_phase2_still_off_by_default(self):
        from services.recovery_runner import phase2_recovery_enabled

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PHASE2_RECOVERY_ENABLED", None)
            self.assertFalse(phase2_recovery_enabled())


if __name__ == "__main__":
    unittest.main()
