"""STOCK STATEMENT PRODUCT|OPEN|PUR.|RETURN|PUR.RET|SALES|CLOSE word-x parser.

Blank PUR./RETURN/PUR.RET cells must not slide SALES into receipts_qty.
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from services.sales_statement_extractor import (
    _parse_stock_statement_open_pur_ret_sales_close_words,
    _stock_open_pur_ret_sales_close_header_centres,
    _text_has_stock_statement_open_pur_ret_sales_close,
)


def _word(x0: float, y: float, text: str, width: float = 10.0):
    return (x0, y, x0 + width, y + 8.0, text, 0, 0, 0)


HEADER_Y = 50.0


def _header_words():
    # Centres match StockHDR.Pdf: OPEN 224, PUR. 287, RETURN 340,
    # PUR.RET 419, SALES 484, CLOSE 547.
    return [
        _word(0.0, HEADER_Y, "PRODUCT", 40),
        _word(210.0, HEADER_Y, "OPEN", 28),
        _word(275.0, HEADER_Y, "PUR.", 24),
        _word(320.0, HEADER_Y, "RETURN", 40),
        _word(400.0, HEADER_Y, "PUR.RET", 36),
        _word(470.0, HEADER_Y, "SALES", 28),
        _word(535.0, HEADER_Y, "CLOSE", 28),
    ]


class StockStatementOpenPurRetSalesCloseTests(unittest.TestCase):
    def test_gate_requires_pur_ret_band(self):
        ok = (
            "STOCK STATEMENT : ACME\n"
            "PRODUCT OPEN PUR. RETURN PUR.RET SALES CLOSE\n"
        )
        self.assertTrue(_text_has_stock_statement_open_pur_ret_sales_close(ok))
        self.assertFalse(
            _text_has_stock_statement_open_pur_ret_sales_close(
                "STOCK STATEMENT\nPRODUCT OPEN PUR. SALES CLOSE\n"
            )
        )
        self.assertFalse(
            _text_has_stock_statement_open_pur_ret_sales_close(
                "Stock and Sales\nOp Purc Sale Cl qty\n"
            )
        )

    def test_header_centres(self):
        pairs = [
            ((w[0] + w[2]) / 2.0, w[4], w[0]) for w in _header_words()
        ]
        centres = _stock_open_pur_ret_sales_close_header_centres(pairs)
        self.assertIsNotNone(centres)
        assert centres is not None
        self.assertIn("opening_qty", centres)
        self.assertIn("open_left", centres)
        self.assertIn("receipts_qty", centres)
        self.assertIn("sales_return_qty", centres)
        self.assertIn("purchase_return_qty", centres)
        self.assertIn("sales_qty", centres)
        self.assertIn("closing_qty", centres)
        self.assertLess(centres["open_left"], centres["opening_qty"])
        self.assertLess(centres["receipts_qty"], centres["sales_return_qty"])
        self.assertLess(centres["sales_return_qty"], centres["purchase_return_qty"])
        self.assertLess(centres["purchase_return_qty"], centres["sales_qty"])

    def test_full_grid_blank_cells_and_sales_return(self):
        rows = [
            # Abana: OPEN=49, SALES=14, CLOSE=35 (blank PUR/RETURN/PUR.RET)
            (
                88.0,
                [
                    ("Abana", 0.0),
                    ("Tab", 45.0),
                    ("50", 172.0),
                    ("49", 232.0),
                    ("14", 502.0),
                    ("35", 569.0),
                ],
            ),
            # Confido: OPEN=48, PUR=500, SALES=317, CLOSE=231
            (
                237.0,
                [
                    ("Confido", 0.0),
                    ("Tab", 60.0),
                    ("60", 172.0),
                    ("48", 232.0),
                    ("500", 292.0),
                    ("317", 494.0),
                    ("231", 562.0),
                ],
            ),
            # Cystone Forte: OPEN=78, SALES=2, CLOSE=76
            (
                249.0,
                [
                    ("Cystone", 0.0),
                    ("Forte", 60.0),
                    ("Tab", 105.0),
                    ("30'S", 157.0),
                    ("78", 232.0),
                    ("2", 509.0),
                    ("76", 569.0),
                ],
            ),
            # Cystone Sf: OPEN=16, CLOSE=16
            (
                261.0,
                [
                    ("Cystone", 0.0),
                    ("Sf", 60.0),
                    ("Syrup", 82.0),
                    ("200m", 157.0),
                    ("16", 232.0),
                    ("16", 569.0),
                ],
            ),
            # Liv 52 Ds: OPEN=65, RETURN=8, SALES=28, CLOSE=45
            (
                280.0,
                [
                    ("Liv", 0.0),
                    ("52", 30.0),
                    ("Ds", 50.0),
                    ("100", 80.0),
                    ("Syp", 110.0),
                    ("100m", 157.0),
                    ("65", 232.0),
                    ("8", 360.0),
                    ("28", 502.0),
                    ("45", 569.0),
                ],
            ),
            # Aactaril: blank OPEN, PUR=72, SALES=2, CLOSE=70
            (
                76.0,
                [
                    ("Aactaril", 0.0),
                    ("Soap", 67.0),
                    ("75gm", 157.0),
                    ("72", 299.0),
                    ("2", 509.0),
                    ("70", 569.0),
                ],
            ),
        ]
        words = list(_header_words())
        for y, cells in rows:
            for text, x in cells:
                words.append(_word(x, y, text))
        # Footer money under SALES/CLOSE
        words.append(_word(470.0, 720.0, "1163753.46", 40))
        words.append(_word(540.0, 720.0, "1493138.15", 40))

        text_blob = (
            "STOCK STATEMENT : THE HIMALAYA DRUG COMPANY "
            "01/10/2026-31/10/2026\n"
            "PRODUCT OPEN PUR. RETURN PUR.RET SALES CLOSE\n"
        )
        page = MagicMock()
        page.get_text = MagicMock(
            side_effect=lambda kind="text": text_blob if kind == "text" else words
        )
        result = _parse_stock_statement_open_pur_ret_sales_close_words(
            [page], "StockHDR.Pdf"
        )
        self.assertIsNotNone(result)
        assert result is not None
        by_name = {
            str(i["product_name"]): i for i in result["line_items"]
        }
        self.assertIn("Aactaril Soap", by_name)
        aac = by_name["Aactaril Soap"]
        self.assertIsNone(aac["opening_qty"])
        self.assertEqual(aac["receipts_qty"], 72.0)
        self.assertEqual(aac["sales_qty"], 2.0)
        self.assertEqual(aac["closing_qty"], 70.0)
        self.assertEqual(aac["packing"], "75gm")
        self.assertEqual(aac["extra"]["field_source"]["opening_qty"], "missing")

        aba = by_name["Abana Tab"]
        self.assertEqual(aba["opening_qty"], 49.0)
        self.assertIsNone(aba["receipts_qty"])
        self.assertEqual(aba["sales_qty"], 14.0)
        self.assertEqual(aba["closing_qty"], 35.0)
        self.assertEqual(aba["packing"], "50")

        conf = by_name["Confido Tab"]
        self.assertEqual(conf["opening_qty"], 48.0)
        self.assertEqual(conf["receipts_qty"], 500.0)
        self.assertEqual(conf["sales_qty"], 317.0)
        self.assertEqual(conf["closing_qty"], 231.0)

        liv = by_name["Liv 52 Ds 100 Syp"]
        self.assertEqual(liv["opening_qty"], 65.0)
        self.assertEqual(liv["sales_return_qty"], 8.0)
        self.assertEqual(liv["sales_qty"], 28.0)
        self.assertEqual(liv["closing_qty"], 45.0)
        self.assertEqual(liv["packing"], "100m")

        self.assertEqual(result["totals"]["sales_value"], 1163753.46)
        self.assertEqual(result["totals"]["closing_value"], 1493138.15)
        self.assertEqual(
            result["totals"]["extra"]["extraction_method"],
            "stock_statement_open_pur_ret_sales_close_words",
        )
        self.assertEqual(result["company_name"], "THE HIMALAYA DRUG COMPANY")


if __name__ == "__main__":
    unittest.main()
