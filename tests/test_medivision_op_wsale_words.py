"""MediVision Op/Purc/Sale/WSale native PDF words — blank Purc must not slide Sale."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from services.sales_statement_extractor import (
    _is_medivision_op_wsale_header,
    _medivision_op_wsale_collect_header_words,
    _medivision_op_wsale_header_anchors,
    _parse_medivision_op_wsale_words,
)


def _word(x: float, y: float, text: str):
    # PyMuPDF words: x0, y0, x1, y1, text, block, line, word
    return (x, y, x + 10.0, y + 8.0, text, 0, 0, 0)


HEADER_Y = 100.0
ROW_Y = 130.0


def _header_words():
    return [
        _word(28, HEADER_Y, "Product"),
        _word(70, HEADER_Y, "name"),
        _word(125, HEADER_Y, "Unit"),
        _word(170, HEADER_Y, "Op"),
        _word(194, HEADER_Y, "Purc"),
        _word(226, HEADER_Y, "SaleWSaleSep"),
        _word(297, HEADER_Y, "SaAug"),
        _word(328, HEADER_Y, "Sa"),
        _word(342, HEADER_Y, "Sa"),
        _word(357, HEADER_Y, "val"),
        _word(374, HEADER_Y, "Cl"),
        _word(386, HEADER_Y, "qty"),
        _word(407, HEADER_Y, "Cl"),
        _word(419, HEADER_Y, "val"),
        _word(445, HEADER_Y, "NM60D"),
    ]


class MedivisionOpWsaleWordsTests(unittest.TestCase):
    def test_header_anchors_sa_val_under_val_not_prior_sa(self):
        anchors = _medivision_op_wsale_header_anchors(
            [(w[0], w[4]) for w in _header_words()]
        )
        self.assertIsNotNone(anchors)
        assert anchors is not None
        self.assertAlmostEqual(anchors["sales_qty"], 226.0, places=1)
        # Sa val money prints under Sa of "Sa val", not the trailing val glyph.
        self.assertAlmostEqual(anchors["sales_value"], 342.0, places=1)
        self.assertAlmostEqual(anchors["closing_qty"], 386.0, places=1)
        self.assertAlmostEqual(anchors["closing_value"], 407.0, places=1)
        self.assertIn("august_sale_qty", anchors)
        self.assertIn("september_sale_qty", anchors)

    def test_separate_sale_wsale_not_stolen_by_wsale_substring(self):
        # Bare "WSale" contains "sale" — must not become sales_qty.
        words = [
            (80.0, "Product"),
            (110.0, "name"),
            (140.0, "Unit"),
            (225.0, "Op"),
            (238.0, "Purc"),
            (257.0, "Sale"),
            (274.0, "WSale"),
            (298.0, "Sep"),
            (320.0, "Aug"),
            (349.0, "Sa"),
            (359.0, "val"),
            (376.0, "Cl"),
            (373.0, "qty"),
            (400.0, "Cl"),
            (409.0, "val"),
            (422.0, "NM60D"),
            (511.0, "NE"),
        ]
        anchors = _medivision_op_wsale_header_anchors(words)
        self.assertIsNotNone(anchors)
        assert anchors is not None
        self.assertAlmostEqual(anchors["sales_qty"], 257.0, places=1)
        self.assertAlmostEqual(anchors["wholesale_qty"], 274.0, places=1)
        self.assertAlmostEqual(anchors["sales_value"], 349.0, places=1)
        self.assertAlmostEqual(anchors["closing_qty"], 376.0, places=1)
        self.assertAlmostEqual(anchors["closing_value"], 400.0, places=1)
        self.assertIn("september_sale_qty", anchors)
        self.assertIn("august_sale_qty", anchors)
        self.assertIn("ne_qty", anchors)

    def test_stacked_header_bands_merge_cl_qty_and_months(self):
        rows = [
            {
                "y": 142.0,
                "words": [
                    (298.0, "Sep"),
                    (320.0, "Aug"),
                    (376.0, "Cl"),
                    (422.0, "NM60D"),
                ],
            },
            {
                "y": 147.0,
                "words": [
                    (80.0, "Product"),
                    (110.0, "name"),
                    (140.0, "Unit"),
                    (225.0, "Op"),
                    (238.0, "Purc"),
                    (257.0, "Sale"),
                    (274.0, "WSale"),
                    (349.0, "Sa"),
                    (359.0, "val"),
                    (400.0, "Cl"),
                    (409.0, "val"),
                ],
            },
            {
                "y": 152.0,
                "words": [(302.0, "Sa"), (326.0, "Sa"), (373.0, "qty")],
            },
        ]
        merged = _medivision_op_wsale_collect_header_words(rows, 1)
        anchors = _medivision_op_wsale_header_anchors(merged)
        self.assertIsNotNone(anchors)
        assert anchors is not None
        self.assertAlmostEqual(anchors["sales_qty"], 257.0, places=1)
        self.assertAlmostEqual(anchors["closing_qty"], 376.0, places=1)
        self.assertAlmostEqual(anchors["closing_value"], 400.0, places=1)
        self.assertIn("september_sale_qty", anchors)
        self.assertIn("august_sale_qty", anchors)

    def test_stacked_wrap_name_keeps_sale_and_cl_val(self):
        # Multi-line product name + separate Sale/WSale/Sep/Aug header bands.
        header_top = [
            _word(298, 95.0, "Sep"),
            _word(320, 95.0, "Aug"),
            _word(376, 95.0, "Cl"),
            _word(422, 95.0, "NM60D"),
        ]
        header_mid = [
            _word(80, 100.0, "Product"),
            _word(110, 100.0, "name"),
            _word(140, 100.0, "Unit"),
            _word(225, 100.0, "Op"),
            _word(238, 100.0, "Purc"),
            _word(257, 100.0, "Sale"),
            _word(274, 100.0, "WSale"),
            _word(349, 100.0, "Sa"),
            _word(359, 100.0, "val"),
            _word(400, 100.0, "Cl"),
            _word(409, 100.0, "val"),
            _word(511, 100.0, "NE"),
        ]
        header_bot = [
            _word(302, 105.0, "Sa"),
            _word(326, 105.0, "Sa"),
            _word(373, 105.0, "qty"),
        ]
        # BONNISAN / 30 ML nums / DROPS wrap
        row = [
            _word(80, 130.0, "BONNISAN"),
            _word(140, 134.0, "30"),
            _word(155, 134.0, "ML"),
            _word(227, 134.0, "163"),
            _word(263, 134.0, "153"),
            _word(306, 134.0, "3"),
            _word(326, 134.0, "2"),
            _word(344, 134.0, "11423.00"),
            _word(376, 134.0, "10"),
            _word(398, 134.0, "596.00"),
            _word(518, 134.0, "3"),
            _word(80, 140.0, "DROPS"),
        ]
        extras = []
        for i in range(4):
            y = 160.0 + i * 15
            extras.extend(
                [
                    _word(80, y, f"ITEM{i}"),
                    _word(140, y, "60"),
                    _word(227, y, "10"),
                    _word(263, y, "2"),
                    _word(344, y, "100"),
                    _word(376, y, "8"),
                    _word(398, y, "200"),
                ]
            )
        words = header_top + header_mid + header_bot + row + extras

        def _get_text(mode="text"):
            if mode == "words":
                return words
            return (
                "Stock and Sales\nCompany: HIMALAYA ZANDRA\n"
                "01-10-26 to 31-10-26\n"
                "Product name Unit Op Purc Sale WSale Sep Sa Aug Sa Sa val Cl qty Cl val NM60D NE\n"
            )

        page = MagicMock()
        page.get_text.side_effect = _get_text
        result = _parse_medivision_op_wsale_words([page], "zandra.pdf")
        self.assertIsNotNone(result)
        assert result is not None
        drops = next(
            it
            for it in result["line_items"]
            if "BONNISAN" in (it.get("product_name") or "").upper()
        )
        self.assertIn("DROPS", (drops.get("product_name") or "").upper())
        self.assertEqual(float(drops["opening_qty"]), 163.0)
        self.assertEqual(float(drops["sales_qty"]), 153.0)
        self.assertEqual(float(drops["sales_value"]), 11423.0)
        self.assertEqual(float(drops["closing_qty"]), 10.0)
        self.assertEqual(float(drops["closing_value"]), 596.0)

    def test_blank_purc_keeps_sale_and_closing(self):
        # BONNISAN SY-style: Purc blank, Sale=18, side cols, Sa val, Cl.
        row = [
            _word(28, ROW_Y, "BONNISAN"),
            _word(72, ROW_Y, "SY"),
            _word(125, ROW_Y, "100ML"),
            _word(175, ROW_Y, "94"),
            _word(237, ROW_Y, "18"),
            _word(299, ROW_Y, "15"),
            _word(331, ROW_Y, "12"),
            _word(352, ROW_Y, "1221"),
            _word(393, ROW_Y, "74"),
            _word(414, ROW_Y, "4447"),
        ]
        drops = [
            _word(28, ROW_Y + 20, "BONNISAN"),
            _word(72, ROW_Y + 20, "DROPS"),
            _word(125, ROW_Y + 20, "30ML"),
            _word(175, ROW_Y + 20, "13"),
            _word(206, ROW_Y + 20, "50"),
            _word(237, ROW_Y + 20, "38"),
            _word(299, ROW_Y + 20, "47"),
            _word(331, ROW_Y + 20, "40"),
            _word(352, ROW_Y + 20, "2837"),
            _word(393, ROW_Y + 20, "25"),
            _word(414, ROW_Y + 20, "1575"),
        ]
        # Need >=5 products for finalize.
        extras = []
        for i in range(3):
            y = ROW_Y + 40 + i * 15
            extras.extend(
                [
                    _word(28, y, f"ITEM{i}"),
                    _word(72, y, "TAB"),
                    _word(125, y, "60"),
                    _word(175, y, "10"),
                    _word(237, y, "2"),
                    _word(352, y, "100"),
                    _word(393, y, "8"),
                    _word(414, y, "200"),
                ]
            )
        words = _header_words() + row + drops + extras
        page = MagicMock()
        page.get_text.side_effect = lambda mode="text": (
            "\n".join(w[4] for w in words)
            if mode == "text"
            else (words if mode == "words" else "")
        )
        # get_text("text") / get_text("words")
        def _get_text(mode="text"):
            if mode == "words":
                return words
            return (
                "Stock and Sales\nCompany: HIMALAYA\n"
                "01-10-26 to 31-10-26\n"
                "Product name Unit Op Purc SaleWSaleSep SaAug Sa Sa val Cl qty Cl val NM60D\n"
            )

        page.get_text.side_effect = _get_text
        self.assertTrue(
            _is_medivision_op_wsale_header(
                "Stock and Sales\nOp Purc SaleWSaleSep SaAug Sa Sa val Cl qty Cl val"
            )
        )
        result = _parse_medivision_op_wsale_words([page], "MSCDA.pdf")
        self.assertIsNotNone(result)
        assert result is not None
        by_name = {
            (it.get("product_name") or "").upper(): it
            for it in (result.get("line_items") or [])
        }
        sy = by_name["BONNISAN SY"]
        self.assertEqual(float(sy["opening_qty"]), 94.0)
        self.assertTrue(sy.get("receipts_qty") in (None, 0, 0.0))
        self.assertEqual(float(sy["sales_qty"]), 18.0)
        self.assertEqual(float(sy["sales_value"]), 1221.0)
        self.assertEqual(float(sy["closing_qty"]), 74.0)
        self.assertEqual(float(sy["closing_value"]), 4447.0)

        drops_it = by_name["BONNISAN DROPS"]
        self.assertEqual(float(drops_it["opening_qty"]), 13.0)
        self.assertEqual(float(drops_it["receipts_qty"]), 50.0)
        self.assertEqual(float(drops_it["sales_qty"]), 38.0)
        self.assertEqual(float(drops_it["sales_value"]), 2837.0)
        self.assertEqual(float(drops_it["closing_qty"]), 25.0)
        self.assertEqual(float(drops_it["closing_value"]), 1575.0)
        self.assertEqual(
            (result.get("totals") or {}).get("extra", {}).get("extraction_method"),
            "medivision_op_wsale_words",
        )


if __name__ == "__main__":
    unittest.main()
