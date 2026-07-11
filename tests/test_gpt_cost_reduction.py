"""Tests for GPT cost-reduction helpers (no extraction behaviour changes)."""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.gpt_response_cache import GPTResponseCache, gpt_cache_key
from services.ocr_text_normalize import normalize_ocr_whitespace


class TestOCRWhitespaceNormalization(unittest.TestCase):
    def test_collapses_repeated_spaces(self):
        raw = "Invoice   No:  123"
        normalized = normalize_ocr_whitespace(raw)
        self.assertEqual(normalized, "Invoice No: 123")

    def test_collapses_excess_blank_lines(self):
        raw = "Line1\n\n\n\nLine2"
        normalized = normalize_ocr_whitespace(raw)
        self.assertEqual(normalized, "Line1\n\nLine2")

    def test_strips_trailing_spaces(self):
        raw = "Amount: 100.00   \n"
        normalized = normalize_ocr_whitespace(raw)
        self.assertEqual(normalized, "Amount: 100.00")

    def test_preserves_invoice_content(self):
        raw = "HSN 30049099\nPANTODAC TAB\nQty 10 Rate 128.52"
        self.assertEqual(normalize_ocr_whitespace(raw), raw)


class TestGPTResponseCache(unittest.TestCase):
    def test_cache_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = GPTResponseCache(tmp, ttl_seconds=3600)
            ocr = "Invoice 123\nProduct A"
            key = gpt_cache_key(ocr)
            payload = {"data": {"invoice_summary": {"invoice_no": "123"}}}
            self.assertIsNone(cache.get(key))
            cache.set(key, payload)
            hit = cache.get(key)
            self.assertIsNotNone(hit)
            self.assertEqual(hit["data"]["invoice_summary"]["invoice_no"], "123")

    def test_same_ocr_same_key(self):
        a = gpt_cache_key("Invoice 123")
        b = gpt_cache_key("Invoice 123")
        c = gpt_cache_key("Invoice 124")
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)


if __name__ == "__main__":
    unittest.main()
