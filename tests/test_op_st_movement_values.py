"""Op-St / Receipt / S.Ret / Sale / Pur.Ret / Cl. Stock qty+value bands."""

from __future__ import annotations

import unittest

from services.sales_statement_extractor import _parse_ocr_word_boxes_op_st_pairs
from services.stock_header_resolver import assign_cells, resolve_columns
from services.stock_photo_deskew import maybe_deskew_stock_photo, measure_table_rule_angle
from services.stock_vision_table import build_table_prompt


def _cells(*texts):
    return [
        {"text": t, "x_center": None, "col_index": i, "subheader_text": None}
        for i, t in enumerate(texts)
    ]


_HEADERS = (
    "Product Name",
    "Packing",
    "Op-St.",
    "",
    "Receipt",
    "",
    "S.Ret.",
    "",
    "Sale",
    "",
    "Pur.Ret.",
    "",
    "Cl. Stock",
    "AMOUNT",
)


class OpStMovementValueTests(unittest.TestCase):
    def test_op_st_is_opening_and_amount_follows_qty(self):
        resolved = resolve_columns(_cells(*_HEADERS))
        canons = [c["canonical"] for c in resolved["columns"]]
        self.assertEqual(
            canons,
            [
                "product_name",
                "pack",
                "opening_qty",
                "opening_value",
                "purchase_qty",
                "purchase_value",
                "sales_return_qty",
                "ignore",
                "sales_qty",
                "sales_value",
                "purchase_return_qty",
                "ignore",
                "closing_qty",
                "closing_value",
            ],
        )
        # BONNISAN DROP: qty then money under each band. Closing amount is 0.
        # 16581.0 is the receipt and sale value, not the closing amount.
        row = [
            "BONNISAN DROP",
            "PHI 30ML",
            "0",
            "0.0",
            "250",
            "16581.0",
            "0",
            "0.0",
            "250",
            "16581.0",
            "0",
            "0.0",
            "0",
            "0.0",
        ]
        tokens = [{"col_index": i, "text": text} for i, text in enumerate(row)]
        fields = assign_cells(tokens, resolved["columns"])["fields"]
        self.assertEqual(fields["opening_qty"], "0")
        self.assertEqual(fields["opening_value"], "0.0")
        self.assertEqual(fields["purchase_qty"], "250")
        self.assertEqual(fields["purchase_value"], "16581.0")
        self.assertEqual(fields["sales_return_qty"], "0")
        self.assertEqual(fields["sales_qty"], "250")
        self.assertEqual(fields["sales_value"], "16581.0")
        self.assertEqual(fields["purchase_return_qty"], "0")
        self.assertEqual(fields["closing_qty"], "0")
        self.assertEqual(fields["closing_value"], "0.0")

    def test_repeated_band_header_is_the_value_column(self):
        resolved = resolve_columns(
            _cells(
                "Product Name",
                "Packing",
                "Op-St.",
                "Op-St.",
                "Receipt",
                "Receipt",
                "S.Ret.",
                "Sale",
                "Sale",
                "Pur.Ret.",
                "Cl. Stock",
                "Cl. Stock",
            )
        )
        by_index = {c["col_index"]: c["canonical"] for c in resolved["columns"]}
        self.assertEqual(by_index[2], "opening_qty")
        self.assertEqual(by_index[3], "opening_value")
        self.assertEqual(by_index[4], "purchase_qty")
        self.assertEqual(by_index[5], "purchase_value")
        self.assertEqual(by_index[8], "sales_value")
        self.assertEqual(by_index[11], "closing_value")

    def test_word_boxes_keep_each_product_on_its_own_row(self):
        words = []

        def add(text, x, y, page_w=1400, page_h=900, **flags):
            words.append(
                {
                    "text": text,
                    "x": float(x),
                    "y": float(y),
                    "h": 12,
                    "conf": 90,
                    "page_w": page_w,
                    "page_h": page_h,
                    **flags,
                }
            )

        add("SHUBHAM MEDICAL AGENCY", 560, 40)
        add("KARAMA BAZAR SONBHADRA", 560, 70)
        add("01/09/2026 30/09/2026", 800, 100, )
        add("Packing", 420, 180)
        add("Op-St.", 490, 180)
        add("Receipt", 604, 181)
        add("S.Ret.", 760, 180)
        add("Sale", 876, 182)
        add("Pur.Ret.", 1000, 183)
        add("Cl.Stock", 1120, 184)
        # Two products. Values sit to the right of each quantity.
        add("BONNISAN", 180, 240)
        add("DROP", 250, 241)
        add("PHI", 377, 240)
        add("30ML", 413, 240)
        add("0", 494, 240)
        add("0.0", 570, 240)
        add("250", 622, 240)
        add("16581.0", 690, 240)
        add("0", 765, 240)
        add("0.0", 820, 240)
        add("250", 900, 240)
        add("16581.0", 960, 240)
        add("0", 1020, 240)
        add("0.0", 1080, 240)
        add("0", 1140, 240)
        add("0.0", 1200, 240)
        add("CYSTONE", 180, 262)
        add("TAB", 250, 263)
        add("PHI", 377, 262)
        add("60TAB", 416, 262)
        add("900", 494, 262)
        add("148898.7", 560, 262)
        add("400", 626, 262)
        add("66866.6", 696, 262)
        add("0", 765, 262)
        add("0.0", 820, 262)
        add("400", 900, 262)
        add("66866.6", 960, 262)
        add("0", 1020, 262)
        add("0.0", 1080, 262)
        add("900", 1140, 262)
        add("148898.7", 1200, 262)
        add("EVECARE", 170, 284)
        add("SYP", 240, 285)
        add("PHI", 377, 284)
        add("200ML", 420, 284)
        add("0", 494, 284)
        add("0.0", 570, 284)
        add("112", 630, 284)
        add("16222.9", 700, 284)
        add("0", 765, 284)
        add("0.0", 820, 284)
        add("112", 900, 284)
        add("16222.9", 960, 284)
        add("0", 1020, 284)
        add("0.0", 1080, 284)
        add("0", 1140, 284)
        add("0.0", 1200, 284)
        # Name-column OCR sits above the qty line. The row itself only kept "TAB".
        add("PILEX", 160, 296, name_column=True)
        add("FORT", 220, 297, name_column=True)
        add("TAB", 270, 310)
        add("0", 494, 310)
        add("0.0", 570, 310)
        add("10", 630, 310)
        add("100.0", 700, 310)
        add("0", 765, 310)
        add("0.0", 820, 310)
        add("10", 900, 310)
        add("100.0", 960, 310)
        add("0", 1020, 310)
        add("0.0", 1080, 310)
        add("0", 1140, 310)
        add("0.0", 1200, 310)

        result = _parse_ocr_word_boxes_op_st_pairs(words, "108.jpg", ".jpg")
        self.assertIsNotNone(result)
        self.assertEqual(result.get("stockist_name"), "SHUBHAM MEDICAL AGENCY")
        self.assertEqual(result.get("stockist_address"), "KARAMA BAZAR SONBHADRA")
        items = result["line_items"]
        self.assertEqual(len(items), 4)
        by_name = {item["product_name"]: item for item in items}
        bonn = by_name["BONNISAN DROP"]
        self.assertEqual(bonn["opening_qty"], 0.0)
        self.assertEqual(bonn["receipts_qty"], 250.0)
        self.assertEqual(bonn["sales_qty"], 250.0)
        self.assertEqual(bonn["closing_qty"], 0.0)
        self.assertEqual(bonn["sales_value"], 16581.0)
        self.assertEqual(bonn["closing_value"], 0.0)
        cyst = by_name["CYSTONE TAB"]
        self.assertEqual(cyst["opening_qty"], 900.0)
        self.assertEqual(cyst["receipts_qty"], 400.0)
        self.assertEqual(cyst["closing_qty"], 900.0)
        self.assertEqual(cyst["closing_value"], 148898.7)
        self.assertIn("PILEX FORT TAB", by_name)

    def test_prompt_keeps_products_on_separate_rows(self):
        text = build_table_prompt()
        self.assertIn("Op-St.", text)
        self.assertIn("Never join several products", text)

    def test_deskew_straightens_tilted_rules_and_leaves_level_pages(self):
        import cv2
        import numpy as np

        img = np.full((420, 860, 3), 255, np.uint8)
        for y in range(30, 390, 18):
            cv2.line(img, (16, y), (840, y), (0, 0, 0), 2)
        ok, straight_buf = cv2.imencode(".jpg", img)
        self.assertTrue(ok)
        straight = straight_buf.tobytes()
        self.assertEqual(maybe_deskew_stock_photo(straight), straight)

        matrix = cv2.getRotationMatrix2D((430, 210), 3.0, 1.0)
        tilted = cv2.warpAffine(
            img, matrix, (860, 420), borderValue=(255, 255, 255)
        )
        ok, tilted_buf = cv2.imencode(".jpg", tilted)
        self.assertTrue(ok)
        raw = tilted_buf.tobytes()
        before = measure_table_rule_angle(raw)
        self.assertIsNotNone(before)
        self.assertGreater(abs(before), 1.5)
        fixed = maybe_deskew_stock_photo(raw)
        self.assertNotEqual(fixed, raw)
        after = measure_table_rule_angle(fixed)
        self.assertTrue(after is None or abs(after) < 0.8)


if __name__ == "__main__":
    unittest.main()
