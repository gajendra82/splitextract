import io
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from services.ocr.tesseract_engine import TesseractEngine, _page_segmentation_mode


class TesseractPsmTests(unittest.TestCase):
    def test_default_and_invalid_values_preserve_psm6(self):
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(_page_segmentation_mode(), 6)
        for value in ("bad", "14", "-1"):
            with patch.dict("os.environ", {"TESSERACT_PSM": value}, clear=True):
                self.assertEqual(_page_segmentation_mode(), 6)

    def test_opt_in_psm4_is_used_for_text_and_word_boxes(self):
        from PIL import Image

        image = io.BytesIO()
        Image.new("RGB", (10, 10), "white").save(image, format="PNG")
        calls = []
        fake = SimpleNamespace(
            Output=SimpleNamespace(DICT="dict"),
            image_to_data=lambda *_args, **kwargs: calls.append(("data", kwargs["config"])) or {"text": [], "conf": [], "left": [], "top": [], "width": [], "height": []},
            image_to_string=lambda *_args, **kwargs: calls.append(("text", kwargs["config"])) or "OCR text",
        )
        with patch.dict("os.environ", {"TESSERACT_PSM": "4"}, clear=True), patch(
            "services.sales_statement_extractor._a2z_tesseract", return_value=fake
        ):
            result = TesseractEngine().recognize(image.getvalue())
        self.assertEqual(result.text, "OCR text")
        self.assertEqual(result.metadata["page_segmentation_mode"], 4)
        self.assertEqual(calls, [("data", "--psm 4"), ("text", "--psm 4")])


if __name__ == "__main__":
    unittest.main()
