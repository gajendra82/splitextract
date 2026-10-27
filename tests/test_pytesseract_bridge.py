"""Prove general OCR goes through the primary engine; whitelist stays Tesseract."""

from __future__ import annotations

import io
import unittest
from unittest import mock

from services.ocr.pytesseract_bridge import PrimaryOCRPytesseractShim


def _png():
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (24, 16), "white").save(buf, format="PNG")
    return Image.open(io.BytesIO(buf.getvalue()))


class _FakeTess:
    Output = type("O", (), {"DICT": "dict"})()

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
    def test_general_string_uses_orchestrator_when_vl_primary(self):
        fake = _FakeTess()
        shim = PrimaryOCRPytesseractShim(fake)
        with mock.patch(
            "services.ocr.runtime.primary_is_paddle_vl", return_value=True
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
        ):
            data = shim.image_to_data(_png())
        self.assertEqual(data["text"], ["x"])
        self.assertEqual(fake.data_calls, 1)

    def test_tesseract_primary_unchanged(self):
        fake = _FakeTess()
        shim = PrimaryOCRPytesseractShim(fake)
        with mock.patch(
            "services.ocr.runtime.primary_is_paddle_vl", return_value=False
        ):
            text = shim.image_to_string(_png(), config="--psm 6")
        self.assertEqual(text, "TESSERACT_TEXT")
        self.assertEqual(fake.string_calls, 1)


if __name__ == "__main__":
    unittest.main()
