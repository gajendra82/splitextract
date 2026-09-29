"""Qty-only STOCK & SALES ANALYSIS. A dash in OPENING is 0, not a skipped cell."""

from __future__ import annotations

import unittest
from pathlib import Path

from services.sales_statement_extractor import (
    _is_qty_only_issue_ssa,
    _qty_only_issue_ssa_is_usable,
    empty_line_item,
    empty_result,
)


PHOTO = Path(
    r"C:\Users\adity\Downloads\ZL_2026_August\0000734334_2026_08_ZL_34_354_06092026114923.jpg"
)


def _row(name, packing, opening, receipts, sales, closing, **extra):
    item = empty_line_item()
    item["product_name"] = name
    item["packing"] = packing
    item["opening_qty"] = opening
    item["receipts_qty"] = receipts
    item["sales_qty"] = sales
    item["closing_qty"] = closing
    item["sales_value"] = extra.get("sales_value", 0)
    item["closing_value"] = extra.get("closing_value", 0)
    item["extra"] = extra.get("item_extra") or {}
    return item


def _sheet(items, title="STOCK & SALES ANALYSIS (HIMALAYA)"):
    result = empty_result("alpha.jpg", "jpg")
    result["report_title"] = title
    result["company_name"] = "HIMALAYA"
    result["line_items"] = items
    return result


class TestQtyOnlyIssueSsa(unittest.TestCase):
    def test_detector_is_this_sheet_only(self):
        packs = ["1X100ML", "1X200ML", "1X60", "1X20", "1X10", "1X30"]
        items = [
            _row(f"ITEM {index}", packs[index % len(packs)], 5, 0, 0, 5)
            for index in range(8)
        ]
        self.assertTrue(_is_qty_only_issue_ssa(_sheet(items)))

        valued = [
            _row(f"ITEM {index}", "1X60", 5, 0, 1, 4, sales_value=100 + index)
            for index in range(8)
        ]
        self.assertFalse(_is_qty_only_issue_ssa(_sheet(valued)))

        mexp = [
            _row(
                f"ITEM {index}",
                "1X60",
                5,
                0,
                0,
                5,
                item_extra={"m_exp": "4/28"},
            )
            for index in range(8)
        ]
        self.assertFalse(_is_qty_only_issue_ssa(_sheet(mexp)))
        other = _sheet(items, title="STOCK STATEMENT")
        other["company_name"] = "GANESH AGENCY"
        self.assertFalse(_is_qty_only_issue_ssa(other))

    def test_leading_dash_must_stay_opening_zero(self):
        kept = [
            _row(f"ITEM {index}", "1X60", 10, 0, 2, 8) for index in range(16)
        ]
        kept.append(_row("LIV 52 200ML SYP", "1X100ML", 0, 315, 32, 283))
        kept.append(_row("RUMALAYA FORTE TAB", "1X30", 0, 2000, 0, 2000))
        self.assertTrue(_qty_only_issue_ssa_is_usable(_sheet(kept)))

        shifted = [
            _row(f"ITEM {index}", "1X60", 10, 0, 2, 8) for index in range(16)
        ]
        shifted.append(_row("LIV 52 200ML SYP", "1X100ML", 315, 32, 283, 0))
        shifted.append(_row("RUMALAYA FORTE TAB", "1X30", 2000, 0, 2000, 0))
        self.assertFalse(_qty_only_issue_ssa_is_usable(_sheet(shifted)))

    @unittest.skipUnless(PHOTO.is_file(), "missing Alpha Distributors photo")
    def test_product_rows_are_separated(self):
        from PIL import Image, ImageOps

        from services.sales_statement_extractor import _qty_only_issue_ssa_row_centers

        image = ImageOps.exif_transpose(Image.open(PHOTO)).convert("RGB")
        centers = _qty_only_issue_ssa_row_centers(image)
        self.assertGreaterEqual(len(centers), 30)
        self.assertLessEqual(len(centers), 55)
        gaps = [centers[i + 1] - centers[i] for i in range(len(centers) - 1)]
        gaps.sort()
        self.assertGreaterEqual(gaps[len(gaps) // 2], 30)
        self.assertLessEqual(gaps[len(gaps) // 2], 55)
        self.assertLess(centers[0] / image.height, 0.30)
        self.assertGreater(centers[-1] / image.height, 0.65)

    @unittest.skipUnless(PHOTO.is_file(), "missing Alpha Distributors photo")
    def test_each_band_is_one_product_row(self):
        import io

        from PIL import Image, ImageOps

        from services.sales_statement_extractor import (
            _qty_only_issue_ssa_band_jpegs,
            _qty_only_issue_ssa_row_centers,
        )

        image = ImageOps.exif_transpose(Image.open(PHOTO)).convert("RGB")
        centers = _qty_only_issue_ssa_row_centers(image)
        bands = _qty_only_issue_ssa_band_jpegs(image, centers)
        self.assertEqual(len(bands), len(centers))
        gaps = [centers[i + 1] - centers[i] for i in range(len(centers) - 1)]
        pitch = sorted(gaps)[len(gaps) // 2]
        for raw in bands[1:]:
            band = Image.open(io.BytesIO(raw))
            self.assertLess(band.height, pitch * 8)

    @unittest.skipUnless(PHOTO.is_file(), "missing Alpha Distributors photo")
    def test_photo_is_not_an_order_form(self):
        from services.sales_statement_extractor import (
            _jpeg_needs_quarter_turn,
            _zeal_printed_order_form_anchor,
        )

        page = PHOTO.read_bytes()
        self.assertTrue(_jpeg_needs_quarter_turn(page))
        self.assertIsNone(_zeal_printed_order_form_anchor(page))


if __name__ == "__main__":
    unittest.main()
