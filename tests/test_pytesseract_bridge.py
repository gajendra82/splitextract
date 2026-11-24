"""Prove general OCR goes through the primary engine; whitelist stays Tesseract."""

from __future__ import annotations

import io
import unittest
from unittest import mock

from services.ocr.base import OCRResult, OCRWord
from services.ocr.pytesseract_bridge import (
    PrimaryOCRPytesseractShim,
    clear_paddle_bridge_cache,
)


def _png():
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (24, 16), "white").save(buf, format="PNG")
    return Image.open(io.BytesIO(buf.getvalue()))


class _FakeTess:
    Output = type("O", (), {"DICT": "dict", "STRING": "string"})()

    def __init__(self):
        self.string_calls = 0
        self.data_calls = 0

    def image_to_string(self, image, config="", **kwargs):
        self.string_calls += 1
        return "TESSERACT_TEXT"

    def image_to_data(self, image, *args, **kwargs):
        self.data_calls += 1
        return {"text": ["x"]}


class BridgeRoutingTests(unittest.TestCase):
    def setUp(self):
        clear_paddle_bridge_cache()

    def test_general_string_uses_orchestrator_when_vl_primary(self):
        fake = _FakeTess()
        shim = PrimaryOCRPytesseractShim(fake)
        with mock.patch(
            "services.ocr.runtime.primary_is_paddle_vl", return_value=True
        ), mock.patch(
            "services.ocr.runtime.primary_is_classic_paddle", return_value=False
        ), mock.patch(
            "services.ocr.runtime.ocr_pil_image_text",
            return_value=("VL_TEXT", {"engine": "paddleocr-vl-1.6", "status": "success"}),
        ) as ocr:
            text = shim.image_to_string(_png(), config="--psm 6")
        self.assertEqual(text, "VL_TEXT")
        self.assertEqual(fake.string_calls, 0)
        self.assertFalse(ocr.call_args.kwargs.get("use_fallback", True))

    def test_whitelist_stays_on_tesseract(self):
        fake = _FakeTess()
        shim = PrimaryOCRPytesseractShim(fake)
        with mock.patch(
            "services.ocr.runtime.primary_is_paddle_vl", return_value=True
        ), mock.patch(
            "services.ocr.runtime.primary_is_classic_paddle", return_value=False
        ):
            text = shim.image_to_string(
                _png(), config="--psm 7 -c tessedit_char_whitelist=0123456789"
            )
        self.assertEqual(text, "TESSERACT_TEXT")
        self.assertEqual(fake.string_calls, 1)

    def test_image_to_data_stays_on_tesseract(self):
        fake = _FakeTess()
        shim = PrimaryOCRPytesseractShim(fake)
        with mock.patch(
            "services.ocr.runtime.primary_is_paddle_vl", return_value=True
        ), mock.patch(
            "services.ocr.runtime.primary_is_classic_paddle", return_value=False
        ):
            data = shim.image_to_data(_png())
        self.assertEqual(data["text"], ["x"])
        self.assertEqual(fake.data_calls, 1)

    def test_tesseract_primary_unchanged(self):
        fake = _FakeTess()
        shim = PrimaryOCRPytesseractShim(fake)
        with mock.patch(
            "services.ocr.runtime.primary_is_paddle_vl", return_value=False
        ), mock.patch(
            "services.ocr.runtime.primary_is_classic_paddle", return_value=False
        ):
            text = shim.image_to_string(_png(), config="--psm 6")
        self.assertEqual(text, "TESSERACT_TEXT")
        self.assertEqual(fake.string_calls, 1)

    def test_image_to_data_paddle_primary_converts_words_and_boxes(self):
        fake = _FakeTess()
        shim = PrimaryOCRPytesseractShim(fake)
        sample_words = [
            OCRWord(text="Product Name", bbox=(35.0, 520.0, 115.0, 547.0), confidence=0.995),
            OCRWord(text="5.00", bbox=(343.0, 544.0, 376.0, 568.0), confidence=0.997),
        ]
        sample_result = OCRResult(
            text="Product Name\n5.00",
            words=sample_words,
            confidence=0.996,
            engine="paddle",
        )
        with mock.patch(
            "services.ocr.runtime.primary_is_classic_paddle", return_value=True
        ), mock.patch(
            "services.ocr.paddle_engine.PaddleEngine.recognize", return_value=sample_result
        ) as rec_mock:
            data = shim.image_to_data(_png(), output_type="dict")

        self.assertEqual(fake.data_calls, 0)
        self.assertEqual(rec_mock.call_count, 1)
        self.assertEqual(data["text"], ["Product Name", "5.00"])
        self.assertEqual(data["left"], [35, 343])
        self.assertEqual(data["top"], [520, 544])
        self.assertEqual(data["width"], [80, 33])
        self.assertEqual(data["height"], [27, 24])
        self.assertEqual(data["conf"], [100, 100])
        self.assertEqual(len(data["line_num"]), 2)
        self.assertEqual(len(data["word_num"]), 2)

    def test_image_to_data_reuses_cached_ocr_result(self):
        fake = _FakeTess()
        shim = PrimaryOCRPytesseractShim(fake)
        sample_words = [OCRWord(text="HIORA", bbox=(10.0, 20.0, 50.0, 40.0), confidence=0.98)]
        sample_result = OCRResult(text="HIORA", words=sample_words, confidence=0.98, engine="paddle")

        with mock.patch(
            "services.ocr.runtime.primary_is_classic_paddle", return_value=True
        ), mock.patch(
            "services.ocr.paddle_engine.PaddleEngine.recognize", return_value=sample_result
        ) as rec_mock:
            img = _png()
            text = shim.image_to_string(img)
            data = shim.image_to_data(img, output_type="dict")

        self.assertEqual(text, "HIORA")
        self.assertEqual(data["text"], ["HIORA"])
        # Exactly 1 OCR inference call made despite two calls (image_to_string + image_to_data)
        self.assertEqual(rec_mock.call_count, 1)
        self.assertEqual(fake.string_calls, 0)
        self.assertEqual(fake.data_calls, 0)

    def test_image_to_data_whitelist_delegates_to_tesseract(self):
        fake = _FakeTess()
        shim = PrimaryOCRPytesseractShim(fake)
        with mock.patch(
            "services.ocr.runtime.primary_is_classic_paddle", return_value=True
        ):
            data = shim.image_to_data(
                _png(), config="--psm 7 -c tessedit_char_whitelist=0123456789"
            )
        self.assertEqual(data["text"], ["x"])
        self.assertEqual(fake.data_calls, 1)

    def test_image_to_data_handles_empty_ocr_safely(self):
        fake = _FakeTess()
        shim = PrimaryOCRPytesseractShim(fake)
        empty_result = OCRResult(text="", words=[], confidence=None, engine="paddle")
        with mock.patch(
            "services.ocr.runtime.primary_is_classic_paddle", return_value=True
        ), mock.patch(
            "services.ocr.paddle_engine.PaddleEngine.recognize", return_value=empty_result
        ):
            data = shim.image_to_data(_png(), output_type="dict")
        self.assertEqual(data["text"], [])
        self.assertEqual(data["left"], [])
        self.assertEqual(data["top"], [])

    def test_image_to_data_string_output_type(self):
        fake = _FakeTess()
        shim = PrimaryOCRPytesseractShim(fake)
        sample_words = [OCRWord(text="ITEM", bbox=(5.0, 10.0, 25.0, 20.0), confidence=0.95)]
        sample_result = OCRResult(text="ITEM", words=sample_words, confidence=0.95, engine="paddle")
        with mock.patch(
            "services.ocr.runtime.primary_is_classic_paddle", return_value=True
        ), mock.patch(
            "services.ocr.paddle_engine.PaddleEngine.recognize", return_value=sample_result
        ):
            tsv = shim.image_to_data(_png(), output_type="string")
        self.assertIn("level\tpage_num\tblock_num", tsv)
        self.assertIn("ITEM", tsv)


if __name__ == "__main__":
    unittest.main()
