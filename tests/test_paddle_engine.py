"""Tests for Classic PaddleOCR Engine adapter."""

from __future__ import annotations

import io
import unittest
from unittest import mock

from PIL import Image, ImageDraw

from services.ocr.base import OCRResult, OCRWord
from services.ocr.paddle_engine import PaddleEngine
from services.ocr.pytesseract_bridge import PrimaryOCRPytesseractShim


def _make_text_image(text: str = "PADDLE TEST 123") -> bytes:
    img = Image.new("RGB", (300, 80), color="white")
    draw = ImageDraw.Draw(img)
    draw.text((20, 25), text, fill="black")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


class PaddleEngineTests(unittest.TestCase):
    def test_paddle_engine_interface(self):
        engine = PaddleEngine()
        self.assertEqual(engine.name, "paddle")
        self.assertTrue(engine.available())

    def test_paddle_engine_recognize_synthetic_image(self):
        engine = PaddleEngine()
        if not engine.available():
            self.skipTest("PaddleOCR package not installed")

        image_bytes = _make_text_image("ZYDUS OCR 456")
        result = engine.recognize(image_bytes, page=1)

        self.assertEqual(result.engine, "paddle")
        self.assertTrue(result.metadata.get("available"))
        self.assertIsNone(result.metadata.get("error"))
        self.assertGreaterEqual(len(result.words), 1)
        self.assertTrue(any("ZYDUS" in w.text for w in result.words))

        first_word = result.words[0]
        self.assertIsInstance(first_word, OCRWord)
        self.assertEqual(len(first_word.bbox), 4)
        self.assertGreater(first_word.confidence or 0.0, 0.7)

    def test_paddle_engine_unavailable_graceful(self):
        engine = PaddleEngine()
        with mock.patch.object(engine, "available", return_value=False):
            result = engine.recognize(b"not-an-image", page=1)
        self.assertEqual(result.engine, "paddle")
        self.assertEqual(result.text, "")
        self.assertFalse(result.metadata.get("available"))
        self.assertIn("error", result.metadata)

    def test_bridge_routes_to_paddle_when_primary(self):
        class FakeTess:
            Output = type("O", (), {"DICT": "dict"})()
            def __init__(self):
                self.calls = 0
            def image_to_string(self, image, config="", **kwargs):
                self.calls += 1
                return "TESS"

        fake = FakeTess()
        shim = PrimaryOCRPytesseractShim(fake)
        with mock.patch("services.ocr.runtime.ocr_settings") as mock_settings:
            mock_settings.return_value.primary_engine = "paddle"
            with mock.patch(
                "services.ocr.runtime.ocr_pil_image_text",
                return_value=("PADDLE_TEXT", {"engine": "paddle", "status": "success"}),
            ):
                img = Image.new("RGB", (20, 20), "white")
                out = shim.image_to_string(img)
        self.assertEqual(out, "PADDLE_TEXT")
        self.assertEqual(fake.calls, 0)


if __name__ == "__main__":
    unittest.main()

