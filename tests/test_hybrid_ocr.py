"""Tests for hybrid OCR + LLM routing architecture (Phase 1)."""

import os
import sys
import unittest
from unittest.mock import MagicMock, patch

# Ensure project root is importable
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.config import EXTRACTION_CONFIG, ExtractionConfig
from services.extraction_cache import (
    ExtractionCache,
    MemoryExtractionCache,
    build_cache_key,
    sha256_bytes,
)
from services.ocr_quality import analyze_ocr_quality
from services.llm_router import LLMRouter
from services.gpt_text_extractor import GPTTextExtractor


GOOD_OCR_TEXT = """
TAX INVOICE
Invoice No: INV/2026/001
Date: 07/07/2026
Vendor GSTIN: 24AABCU9603R1ZM
Customer GSTIN: 27AABCT3518Q1ZV

Description          HSN      Qty   Rate    Amount
PANTODAC 40MG TAB    30049099  60   137.18  8229.60
AMOXICILLIN 500 CAP  30041010  20    45.00   900.00

Grand Total: 9129.60
""" + ("Line item filler text for length. " * 40)


POOR_OCR_TEXT = "@@@ ### ??? [garbled] xx"


class TestOCRQualityAnalyzer(unittest.TestCase):
    def test_good_ocr_quality(self):
        result = analyze_ocr_quality(GOOD_OCR_TEXT, confidence=92.0)
        self.assertEqual(result["quality"], "GOOD")
        self.assertGreaterEqual(result["score"], 70)
        self.assertTrue(result["gstin_detected"])
        self.assertTrue(result["invoice_number_detected"])

    def test_poor_ocr_quality(self):
        result = analyze_ocr_quality(POOR_OCR_TEXT, confidence=30.0)
        self.assertEqual(result["quality"], "POOR")
        self.assertGreater(len(result["reason"]), 0)

    def test_missing_gstin_still_can_be_poor_on_short_text(self):
        result = analyze_ocr_quality("short", confidence=50.0)
        self.assertEqual(result["quality"], "POOR")


class TestExtractionCache(unittest.TestCase):
    def test_sha256_and_cache_roundtrip(self):
        data = b"duplicate-file-content"
        file_hash = sha256_bytes(data)
        key = build_cache_key(file_hash, page_num=0, suffix="text")
        cache = ExtractionCache()
        cache.config.enable_extraction_cache = True
        cache._backend = MemoryExtractionCache()
        payload = {"full_data": {"invoice_no": "INV-1"}, "strategy": "gpt_text"}
        cache.set(key, payload)
        cached = cache.get(key)
        self.assertIsNotNone(cached)
        self.assertEqual(cached["full_data"]["invoice_no"], "INV-1")


class TestLLMRouter(unittest.TestCase):
    def test_good_ocr_routes_to_gpt(self):
        mock_gpt = MagicMock(spec=GPTTextExtractor)
        mock_gpt.extract.return_value = ({"invoice_no": "INV/2026/001"}, {"input_tokens": 10, "output_tokens": 20})
        router = LLMRouter(
            config=EXTRACTION_CONFIG,
            gpt_extractor=mock_gpt,
            vision_extractor=None,
            cache=ExtractionCache(),
        )
        router.cache.config.enable_extraction_cache = False
        result, meta = router.extract_from_text(GOOD_OCR_TEXT, ocr_confidence=95.0)
        self.assertIsNotNone(result)
        self.assertEqual(meta["selected_strategy"], "gpt_text")
        mock_gpt.extract.assert_called()

    def test_poor_ocr_skips_text_model(self):
        mock_gpt = MagicMock(spec=GPTTextExtractor)
        router = LLMRouter(
            config=EXTRACTION_CONFIG,
            gpt_extractor=mock_gpt,
            vision_extractor=None,
            cache=ExtractionCache(),
        )
        router.cache.config.enable_extraction_cache = False
        result, meta = router.extract_from_text(POOR_OCR_TEXT, ocr_confidence=20.0)
        self.assertIsNone(result)
        self.assertEqual(meta["ocr_quality"]["quality"], "POOR")
        mock_gpt.extract.assert_not_called()

    def test_gpt_failure_returns_none_for_vision_fallback(self):
        mock_gpt = MagicMock(spec=GPTTextExtractor)
        mock_gpt.extract.return_value = (None, {"input_tokens": 0, "output_tokens": 0})
        cfg = ExtractionConfig()
        cfg.text_retry_count = 1
        router = LLMRouter(
            config=cfg,
            gpt_extractor=mock_gpt,
            vision_extractor=None,
            cache=ExtractionCache(),
        )
        router.cache.config.enable_extraction_cache = False
        result, meta = router.extract_from_text(GOOD_OCR_TEXT, ocr_confidence=95.0)
        self.assertIsNone(result)
        self.assertIn("failed", meta.get("fallback_reason", "").lower())
        self.assertEqual(mock_gpt.extract.call_count, 2)


class TestDuplicateFileCache(unittest.TestCase):
    def test_duplicate_file_hash_cache_hit(self):
        file_hash = sha256_bytes(b"same-invoice-bytes")
        key = build_cache_key(file_hash, page_num=0, suffix="text")
        cache = ExtractionCache()
        cache.config.enable_extraction_cache = True
        cache._backend = MemoryExtractionCache()
        cache.set(key, {"full_data": {"invoice_no": "DUP-1"}, "strategy": "cache"})
        hit = cache.get(key)
        miss = cache.get(build_cache_key(sha256_bytes(b"other"), 0, "text"))
        self.assertIsNotNone(hit)
        self.assertIsNone(miss)


if __name__ == "__main__":
    unittest.main()
