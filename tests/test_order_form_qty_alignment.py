"""Row alignment for handwritten Qty cells on Zandra order forms."""
import unittest
from pathlib import Path

from PIL import Image, ImageOps

from services.sales_statement_extractor import (
    _order_form_light_pen_window,
    _order_form_qty_window,
    _order_form_row_bands,
    _order_form_sparse_qty_window,
    _order_form_upright_image,
    _order_form_vertical_rules,
)

_FORMS = Path(r"C:\Users\Prerana Bhalerao\Downloads\ZA_2026_August")
_SIDEWAYS = _FORMS / "0000737792_2026_08_ZA_04_9307_08092026064119.jpg"
_CLEAR = _FORMS / "0000737332_2026_08_ZA_24_255_07092026075852.jpg"
_SMALL = _FORMS / "0000737608_2026_08_ZA_04_740_07092026134857.jpeg"


def _open(path: Path):
    image = ImageOps.exif_transpose(Image.open(path)).convert("RGB")
    image = _order_form_upright_image(image)
    return image, image.load(), image.size


def _windows(image):
    px = image.load()
    width, height = image.size
    bands = _order_form_row_bands(px, width, height)
    y0, y1 = bands[0][0], bands[-1][1]
    left = _order_form_qty_window(
        px, _order_form_vertical_rules(px, 40, width // 2, y0, y1, 55), bands, 50
    )
    right = _order_form_qty_window(
        px,
        _order_form_vertical_rules(px, width // 2, width - 30, y0, y1, 100),
        bands,
        78,
    )
    if not left:
        left = _order_form_sparse_qty_window(px, bands, int(width * 0.08), width // 2, width)
    if not right:
        right = _order_form_sparse_qty_window(
            px, bands, width // 2, int(width * 0.90), width
        )

    def outside(found, limit):
        if not found or found[1][1] <= found[1][0]:
            return True
        return found[1][0] < width * limit

    if outside(left, 0.22):
        left = _order_form_light_pen_window(px, bands, int(width * 0.20), width // 2, width)
    if outside(right, 0.72):
        light = _order_form_light_pen_window(px, bands, width // 2, int(width * 0.90), width)
        if light:
            right = light
    return bands, left, right


class TestOrderFormQtyAlignment(unittest.TestCase):
    def test_sideways_form_includes_the_first_right_table_rows(self):
        if not _SIDEWAYS.exists():
            self.skipTest("source photo is not on this machine")
        image, _, _ = _open(_SIDEWAYS)
        self.assertGreater(image.size[1], image.size[0])
        self.assertEqual(image.size, (2448, 3264))
        bands, left, right = _windows(image)
        self.assertEqual(len(bands), 35)
        self.assertIsNone(left)
        self.assertIsNotNone(right)
        indexes = right[2]
        self.assertIn(0, indexes)
        self.assertIn(1, indexes)

    def test_clear_scan_keeps_its_qty_columns(self):
        if not _CLEAR.exists():
            self.skipTest("source photo is not on this machine")
        image, _, _ = _open(_CLEAR)
        bands, left, right = _windows(image)
        self.assertEqual(len(bands), 35)
        self.assertEqual(bands[0], (803, 854))
        self.assertEqual(bands[-1], (2618, 2678))
        self.assertEqual(left[1], (899, 998))
        self.assertEqual(left[2], [9, 10, 11, 21, 24, 25, 26, 34])
        self.assertEqual(right[1], (2020, 2116))
        self.assertEqual(right[2], [6, 10, 16, 17, 18, 24, 25])

    def test_small_photo_keeps_its_qty_columns(self):
        if not _SMALL.exists():
            self.skipTest("source photo is not on this machine")
        image, _, _ = _open(_SMALL)
        bands, left, right = _windows(image)
        self.assertEqual(len(bands), 35)
        self.assertEqual(bands[0], (414, 431))
        self.assertEqual(bands[-1], (1086, 1106))
        self.assertEqual(left[1], (332, 364))
        self.assertEqual(left[2], [13, 26])
        self.assertEqual(right[1], (761, 793))
        self.assertEqual(right[2], [0])


if __name__ == "__main__":
    unittest.main()
