"""Focused stock-row crops. No Gemini calls."""

from __future__ import annotations

import io
import unittest
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from services.stock_row_crops import build_stock_row_crops, load_upright_image

FONT = Path(r"C:\Windows\Fonts\arial.ttf")
JPEG = Path(
    r"C:\Users\amnsa\Downloads\ZL_2026_August"
    r"\0000700077_2026_08_ZL_06_304_06092026141652.jpeg"
)


def _font(size):
    if FONT.exists():
        return ImageFont.truetype(str(FONT), size)
    return ImageFont.load_default()


def _table():
    image = Image.new("RGB", (1680, 260), "white")
    draw = ImageDraw.Draw(image)
    font = _font(28)
    headers = (
        (16, "Item Name"),
        (340, "pack"),
        (470, "Op"),
        (560, "Pur"),
        (660, "SP"),
        (760, "Pur Val"),
        (940, "Sale"),
        (1080, "SS Sp Qty"),
        (1280, "Sale Val"),
        (1440, "Bal"),
        (1540, "Bal Val"),
    )
    for x, text in headers:
        draw.text((x, 24), text, fill="black", font=font)
    draw.text((16, 110), "ALPHA KEEP", fill="black", font=font)
    draw.text((470, 110), "10", fill="black", font=font)
    draw.text((940, 110), "2", fill="black", font=font)
    draw.text((1440, 110), "8", fill="black", font=font)
    draw.text((16, 185), "BETA TARGET", fill="black", font=font)
    draw.text((470, 185), "47", fill="black", font=font)
    draw.text((560, 185), "0", fill="black", font=font)
    draw.text((940, 185), "4", fill="black", font=font)
    draw.text((1080, 185), "0", fill="black", font=font)
    draw.text((1440, 185), "45", fill="black", font=font)
    draw.rectangle((0, 0, 48, 16), fill=(0, 0, 255))
    draw.rectangle((0, 240, 48, 259), fill=(255, 0, 0))
    return image


def _sideways_jpeg(upright: Image.Image) -> bytes:
    stored = upright.transpose(Image.Transpose.ROTATE_90)
    exif = stored.getexif()
    exif[274] = 6
    buffer = io.BytesIO()
    stored.save(buffer, format="JPEG", exif=exif.tobytes(), quality=95)
    return buffer.getvalue()


def _ocr_text(png: bytes) -> str:
    import pytesseract

    program = Path(r"C:\Program Files\Tesseract-OCR\tesseract.exe")
    if program.exists():
        pytesseract.pytesseract.tesseract_cmd = str(program)
    image = Image.open(io.BytesIO(png))
    return pytesseract.image_to_string(image)


class StockRowCropTests(unittest.TestCase):
    def test_exif_orientation_is_applied_before_crop(self):
        upright = _table()
        raw = _sideways_jpeg(upright)
        stored = Image.open(io.BytesIO(raw))
        self.assertEqual(stored.size, (upright.height, upright.width))
        restored = load_upright_image(raw)
        self.assertEqual(restored.size, upright.size)
        top = restored.getpixel((12, 8))
        bottom = restored.getpixel((12, 248))
        self.assertGreater(top[2], 200)
        self.assertLess(top[0], 40)
        self.assertGreater(bottom[0], 200)
        self.assertLess(bottom[2], 40)

    def test_crop_keeps_header_target_row_and_ss_column(self):
        raw = _sideways_jpeg(_table())
        crops = build_stock_row_crops(
            raw,
            "sheet.jpeg",
            [{"product_name": "BETA TARGET", "opening_qty": 47, "receipts_qty": 0, "sales_qty": 4}],
        )
        self.assertIsNotNone(crops)
        self.assertEqual(len(crops), 1)
        crop = crops[0]
        self.assertEqual(crop.product_name, "BETA TARGET")
        self.assertTrue(crop.includes_header)
        self.assertTrue(crop.includes_ss_column)
        self.assertGreater(crop.width, crop.height)
        text = _ocr_text(crop.png).casefold()
        self.assertIn("beta", text)
        self.assertIn("item", text)
        self.assertIn("ss", text)
        self.assertNotIn("alpha", text)

    def test_only_requested_failing_row_is_cropped(self):
        raw = _sideways_jpeg(_table())
        crops = build_stock_row_crops(
            raw,
            "sheet.jpeg",
            [
                {"product_name": "BETA TARGET", "opening_qty": 47, "sales_qty": 4, "receipts_qty": 0},
            ],
        )
        self.assertEqual([crop.product_name for crop in crops], ["BETA TARGET"])

    def test_missing_header_falls_back(self):
        blank = Image.new("RGB", (400, 200), "white")
        buffer = io.BytesIO()
        blank.save(buffer, format="JPEG")
        self.assertIsNone(
            build_stock_row_crops(
                buffer.getvalue(),
                "blank.jpeg",
                [{"product_name": "BETA TARGET"}],
            )
        )

    def test_pdf_and_unreadable_bytes_fall_back(self):
        self.assertIsNone(build_stock_row_crops(b"%PDF-1.4", "page.pdf", [{"product_name": "BETA TARGET"}]))
        self.assertIsNone(build_stock_row_crops(b"not-an-image", "photo.jpeg", [{"product_name": "BETA TARGET"}]))
        self.assertIsNone(build_stock_row_crops(b"", "photo.jpeg", [{"product_name": "BETA TARGET"}]))

    @unittest.skipUnless(JPEG.exists(), "source photo is not on this machine")
    def test_real_photo_crops_the_four_failing_rows(self):
        rows = [
            {"product_name": "H CLARINA ANTI ACNE CREAM", "opening_qty": 47, "receipts_qty": 0, "sales_qty": 4, "closing_qty": 45},
            {"product_name": "H KAPIKACHHU TABLETS", "opening_qty": 40, "receipts_qty": 0, "sales_qty": 1, "closing_qty": 40},
            {"product_name": "H MANJISHTHA TAB", "opening_qty": 48, "receipts_qty": 0, "sales_qty": 8, "closing_qty": 39},
            {"product_name": "H PILEX TABLETS", "opening_qty": 106, "receipts_qty": 100, "sales_qty": 131, "closing_qty": 74},
        ]
        crops = build_stock_row_crops(JPEG.read_bytes(), JPEG.name, rows)
        self.assertIsNotNone(crops)
        self.assertEqual([crop.product_name for crop in crops], [row["product_name"] for row in rows])
        for crop in crops:
            self.assertTrue(crop.includes_header)
            self.assertTrue(crop.includes_ss_column)
            self.assertGreater(crop.width, crop.height)
            self.assertLess(crop.height, 400)


if __name__ == "__main__":
    unittest.main()
