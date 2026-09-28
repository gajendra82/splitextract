"""A missing Tesseract binary must not abort image extraction before vision."""

from __future__ import annotations

import io
import json
import unittest
from unittest.mock import patch

from PIL import Image


class _VisionResponse:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return {
            "candidates": [
                {"content": {"parts": [{"text": json.dumps(self._payload)}]}}
            ]
        }


class TestImageWithoutTesseract(unittest.TestCase):
    def test_missing_tesseract_continues_to_vision(self):
        buf = io.BytesIO()
        Image.new("RGB", (16, 16), "white").save(buf, format="PNG")
        payload = {
            "stockist_name": "DEMO MEDICAL",
            "line_items": [
                {
                    "product_name": "ABANA TAB",
                    "opening_qty": 2,
                    "sales_qty": 1,
                    "closing_qty": 1,
                }
            ],
        }
        from services.sales_statement_extractor import _parse_image

        with patch(
            "services.vertex_gemini_client.generate_content_via_vertex",
            return_value=_VisionResponse(payload),
        ):
            result = _parse_image(buf.getvalue(), "stmt.png", ".png")

        self.assertEqual(
            result["totals"]["extra"].get("extraction_method"), "gemini_vision"
        )
        self.assertEqual(result["line_items"][0]["product_name"], "ABANA TAB")
        self.assertNotIn("tesseract is not installed", str(result["totals"]["extra"]))


if __name__ == "__main__":
    unittest.main()
