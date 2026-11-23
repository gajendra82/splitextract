"""Stacked OPENI/NG/STOCK headers and qty+value cells stay distinct columns."""

from __future__ import annotations

import unittest

from services.stock_native_tables import (
    _FitzWordPage,
    _glue_header_text,
    from_pdf_words,
    resolver_ready,
)


def _word(x0, top, x1, bottom, text):
    return {"x0": x0, "top": top, "x1": x1, "bottom": bottom, "text": text}


class GlueHeaderFragmentTests(unittest.TestCase):
    def test_openi_ng_stock(self):
        self.assertEqual(_glue_header_text("OPENI", "NG", prepend=False), "OPENING")
        self.assertEqual(
            _glue_header_text("OPENING", "STOCK", prepend=False), "OPENING STOCK"
        )

    def test_quant_ity(self):
        self.assertEqual(_glue_header_text("QUANT", "ITY", prepend=False), "QUANTITY")
        self.assertEqual(
            _glue_header_text("ISSUE QUANT", "ITY", prepend=False), "ISSUE QUANTITY"
        )


class WrappedOpeningReceiveIssueHeaderTests(unittest.TestCase):
    def test_three_line_header_resolves_qty_and_value(self):
        words = [
            _word(158.1, 149.6, 212.5, 161.9, "PRODUCT"),
            _word(215.5, 149.6, 290.6, 161.9, "DESCRIPTION"),
            _word(305.4, 149.0, 339.6, 161.3, "OPENI"),
            _word(348.9, 149.0, 391.0, 161.3, "OPENIN"),
            _word(399.9, 149.0, 440.8, 161.3, "RECEIV"),
            _word(449.4, 149.0, 490.3, 161.3, "RECEIV"),
            _word(504.1, 149.0, 537.1, 161.3, "ISSUE"),
            _word(553.6, 149.0, 586.6, 161.3, "ISSUE"),
            _word(600.9, 149.0, 634.5, 161.3, "CLOSI"),
            _word(645.1, 149.0, 686.6, 161.3, "CLOSIN"),
            _word(314.4, 161.8, 330.9, 174.1, "NG"),
            _word(366.1, 161.8, 374.7, 174.1, "G"),
            _word(417.1, 161.8, 424.5, 174.1, "E"),
            _word(466.6, 161.8, 474.0, 174.1, "E"),
            _word(508.0, 161.8, 540.0, 174.1, "QUANT"),
            _word(614.4, 161.8, 630.9, 174.1, "NG"),
            _word(662.1, 161.8, 670.7, 174.1, "G"),
            _word(303.1, 174.5, 341.4, 186.8, "STOCK"),
            _word(351.9, 174.5, 388.3, 186.8, "VALUE"),
            _word(399.9, 174.5, 440.8, 186.8, "QUANT"),
            _word(449.4, 174.5, 490.3, 186.8, "VALUE"),
            _word(512.0, 174.5, 532.0, 186.8, "ITY"),
            _word(553.6, 174.5, 586.6, 186.8, "VALUE"),
            _word(600.9, 174.5, 634.5, 186.8, "STOC"),
            _word(645.1, 174.5, 686.6, 186.8, "VALUE"),
            _word(151.6, 200.0, 270.0, 212.0, "ARJUNA"),
            _word(272.0, 200.0, 300.0, 212.0, "TAB"),
            _word(335.3, 200.0, 341.4, 212.0, "0"),
            _word(387.8, 200.0, 393.9, 212.0, "0"),
            _word(428.9, 200.0, 441.1, 212.0, "180"),
            _word(447.8, 200.0, 493.6, 212.0, "39056.82"),
            _word(528.7, 200.0, 540.9, 212.0, "90"),
            _word(548.4, 200.0, 593.4, 212.0, "19284.64"),
            _word(624.7, 200.0, 636.9, 212.0, "90"),
            _word(649.7, 200.0, 689.4, 212.0, "19859.4"),
        ]
        built = from_pdf_words(_FitzWordPage(words, 841.0, 595.0))
        ready = resolver_ready(built.get("header_cells") or [])
        canons = [c.get("canonical") for c in ready.get("columns") or []]
        self.assertEqual(
            canons,
            [
                "product_name",
                "opening_qty",
                "opening_value",
                "purchase_qty",
                "purchase_value",
                "sales_qty",
                "sales_value",
                "closing_qty",
                "closing_value",
            ],
        )
        rows = built.get("data_rows") or []
        self.assertTrue(rows)
        first = " ".join(str(t.get("text") or "") for t in rows[0])
        self.assertIn("ARJUNA", first)
        self.assertNotIn("TOTAL", first.upper())
        texts = [str(t.get("text") or "") for t in rows[0]]
        self.assertIn("180", texts)
        self.assertIn("90", texts)


class QtyValueGridSkipsReceiptIssueTests(unittest.TestCase):
    def test_issue_layout_skips_opening_and_receive_value_pairs(self):
        from services.stock_native_resolver import _issue_column_layout

        columns = [
            {"canonical": name, "header_text": label}
            for name, label in (
                ("product_name", "PRODUCT DESCRIPTION"),
                ("opening_qty", "OPENING STOCK"),
                ("opening_value", "OPENING VALUE"),
                ("purchase_qty", "RECEIVE QUANT"),
                ("purchase_value", "RECEIVE VALUE"),
                ("sales_qty", "ISSUE QUANTITY"),
                ("sales_value", "ISSUE VALUE"),
                ("closing_qty", "CLOSING STOC"),
                ("closing_value", "CLOSING VALUE"),
            )
        ]
        self.assertFalse(_issue_column_layout(columns))

    def test_resolved_columns_keep_receipt_issue_and_drop_total(self):
        from services.stock_native_resolver import _items_from_resolved_word_columns
        from services.stock_native_tables import from_pdf_words, resolver_ready

        words = [
            _word(158.1, 149.6, 212.5, 161.9, "PRODUCT"),
            _word(215.5, 149.6, 290.6, 161.9, "DESCRIPTION"),
            _word(305.4, 149.0, 339.6, 161.3, "OPENI"),
            _word(348.9, 149.0, 391.0, 161.3, "OPENIN"),
            _word(399.9, 149.0, 440.8, 161.3, "RECEIV"),
            _word(449.4, 149.0, 490.3, 161.3, "RECEIV"),
            _word(504.1, 149.0, 537.1, 161.3, "ISSUE"),
            _word(553.6, 149.0, 586.6, 161.3, "ISSUE"),
            _word(600.9, 149.0, 634.5, 161.3, "CLOSI"),
            _word(645.1, 149.0, 686.6, 161.3, "CLOSIN"),
            _word(314.4, 161.8, 330.9, 174.1, "NG"),
            _word(366.1, 161.8, 374.7, 174.1, "G"),
            _word(417.1, 161.8, 424.5, 174.1, "E"),
            _word(466.6, 161.8, 474.0, 174.1, "E"),
            _word(508.0, 161.8, 540.0, 174.1, "QUANT"),
            _word(614.4, 161.8, 630.9, 174.1, "NG"),
            _word(662.1, 161.8, 670.7, 174.1, "G"),
            _word(303.1, 174.5, 341.4, 186.8, "STOCK"),
            _word(351.9, 174.5, 388.3, 186.8, "VALUE"),
            _word(399.9, 174.5, 440.8, 186.8, "QUANT"),
            _word(449.4, 174.5, 490.3, 186.8, "VALUE"),
            _word(512.0, 174.5, 532.0, 186.8, "ITY"),
            _word(553.6, 174.5, 586.6, 186.8, "VALUE"),
            _word(600.9, 174.5, 634.5, 186.8, "STOC"),
            _word(645.1, 174.5, 686.6, 186.8, "VALUE"),
            _word(151.6, 188.0, 250.0, 198.0, "AACTARIL"),
            _word(252.0, 188.0, 300.0, 198.0, "SOAP"),
            _word(335.3, 188.0, 341.4, 198.0, "52"),
            _word(351.9, 188.0, 388.3, 198.0, "3837.44"),
            _word(428.9, 188.0, 441.1, 198.0, "0"),
            _word(447.8, 188.0, 493.6, 198.0, "0"),
            _word(528.7, 188.0, 540.9, 198.0, "0"),
            _word(548.4, 188.0, 593.4, 198.0, "0"),
            _word(624.7, 188.0, 636.9, 198.0, "52"),
            _word(649.7, 188.0, 689.4, 198.0, "3837.44"),
            _word(151.6, 200.0, 270.0, 212.0, "ARJUNA"),
            _word(272.0, 200.0, 300.0, 212.0, "TAB"),
            _word(335.3, 200.0, 341.4, 212.0, "0"),
            _word(387.8, 200.0, 393.9, 212.0, "0"),
            _word(428.9, 200.0, 441.1, 212.0, "180"),
            _word(447.8, 200.0, 493.6, 212.0, "39056.82"),
            _word(528.7, 200.0, 540.9, 212.0, "90"),
            _word(548.4, 200.0, 593.4, 212.0, "19284.64"),
            _word(624.7, 200.0, 636.9, 212.0, "90"),
            _word(649.7, 200.0, 689.4, 212.0, "19859.4"),
            _word(151.6, 220.0, 270.0, 232.0, "VRIKSAMLA"),
            _word(272.0, 220.0, 300.0, 232.0, "TAB"),
            _word(335.3, 220.0, 341.4, 232.0, "47"),
            _word(351.9, 220.0, 388.3, 232.0, "9851.13"),
            _word(428.9, 220.0, 441.1, 232.0, "0"),
            _word(447.8, 220.0, 493.6, 232.0, "0"),
            _word(528.7, 220.0, 540.9, 232.0, "0"),
            _word(548.4, 220.0, 593.4, 232.0, "0"),
            _word(624.7, 220.0, 636.9, 232.0, "47"),
            _word(649.7, 220.0, 689.4, 232.0, "9851.13"),
            _word(151.6, 240.0, 200.0, 252.0, "TOTAL"),
            _word(335.3, 240.0, 341.4, 252.0, "7567"),
            _word(351.9, 240.0, 388.3, 252.0, "945242.25"),
            _word(428.9, 240.0, 441.1, 252.0, "4944"),
            _word(447.8, 240.0, 493.6, 252.0, "707577.62"),
            _word(528.7, 240.0, 540.9, 252.0, "6539"),
            _word(548.4, 240.0, 593.4, 252.0, "902881.34"),
            _word(624.7, 240.0, 636.9, 252.0, "5972"),
            _word(649.7, 240.0, 689.4, 252.0, "777273.27"),
        ]
        built = from_pdf_words(_FitzWordPage(words, 841.0, 595.0))
        ready = resolver_ready(built.get("header_cells") or [])
        self.assertTrue(ready.get("resolved"))
        parsed = _items_from_resolved_word_columns(built.get("page_results") or [])
        self.assertIsNotNone(parsed)
        by_name = {
            item["product_name"]: item for item in (parsed or {}).get("line_items") or []
        }
        self.assertIn("ARJUNA TAB", by_name)
        arjuna = by_name["ARJUNA TAB"]
        self.assertEqual(arjuna["opening_qty"], 0.0)
        self.assertEqual(arjuna["receipts_qty"], 180.0)
        self.assertEqual(arjuna["sales_qty"], 90.0)
        self.assertEqual(arjuna["closing_qty"], 90.0)
        vrik = next(item for key, item in by_name.items() if "VRIKSAMLA" in key)
        self.assertEqual(vrik["opening_qty"], 47.0)
        self.assertEqual(vrik["receipts_qty"], 0.0)
        self.assertEqual(vrik["sales_qty"], 0.0)
        self.assertEqual(vrik["closing_qty"], 47.0)
        self.assertNotEqual(vrik["opening_qty"], 7567.0)
        self.assertTrue(all("TOTAL" not in (name or "").upper() for name in by_name))


class OpeningReceiptIssueQtyValuePairTests(unittest.TestCase):
    def test_qty_value_child_row_stays_separate_columns(self):
        from services.stock_header_resolver import assign_cells

        words = [
            _word(20.8, 132.7, 42.4, 144.4, "ITEM"),
            _word(47.8, 132.7, 107.2, 144.4, "DESCRIPTION"),
            _word(274.5, 132.7, 312.3, 144.4, "OPENING"),
            _word(387.9, 132.7, 425.7, 144.4, "RECEIPT"),
            _word(501.3, 132.7, 528.3, 144.4, "ISSUE"),
            _word(604.0, 132.7, 641.8, 144.4, "CLOSING"),
            _word(679.6, 132.7, 701.2, 144.4, "DUMP"),
            _word(252.9, 143.2, 274.5, 154.9, "QTY."),
            _word(312.3, 143.2, 339.3, 154.9, "VALUE"),
            _word(366.3, 143.2, 387.9, 154.9, "QTY."),
            _word(425.7, 143.2, 452.7, 154.9, "VALUE"),
            _word(474.3, 143.2, 495.9, 154.9, "QTY."),
            _word(533.7, 143.2, 560.7, 154.9, "VALUE"),
            _word(582.3, 143.2, 604.0, 154.9, "QTY."),
            _word(641.8, 143.2, 668.8, 154.9, "VALUE"),
            _word(679.6, 143.2, 701.2, 154.9, "QTY."),
            _word(706.6, 143.2, 733.6, 154.9, "SCRAP"),
            _word(20.8, 195.7, 74.8, 207.4, "ASHWAGANDA"),
            _word(80.2, 195.7, 96.4, 207.4, "TAB"),
            _word(101.8, 195.7, 150.3, 207.4, "60'S(HIM)"),
            _word(188.1, 195.7, 209.7, 207.4, "60'S"),
            _word(269.1, 195.7, 274.5, 207.4, "0"),
            _word(317.7, 195.7, 339.3, 207.4, "0.00"),
            _word(366.3, 195.7, 387.9, 207.4, "5.00"),
            _word(420.3, 195.7, 452.7, 207.4, "990.50"),
            _word(490.5, 195.7, 495.9, 207.4, "0"),
            _word(539.1, 195.7, 560.7, 207.4, "0.00"),
            _word(582.3, 195.7, 604.0, 207.4, "5.00"),
            _word(636.4, 195.7, 668.8, 207.4, "990.50"),
            _word(695.8, 195.7, 701.2, 207.4, "0"),
            _word(728.2, 195.7, 733.6, 207.4, "0"),
            _word(20.8, 217.0, 74.8, 228.7, "GASEX"),
            _word(80.2, 217.0, 120.0, 228.7, "ELACHI"),
            _word(125.0, 217.0, 160.0, 228.7, "200ML"),
            _word(182.7, 217.0, 209.7, 228.7, "200ML"),
            _word(252.9, 217.0, 274.5, 228.7, "28.00"),
            _word(312.3, 217.0, 339.3, 228.7, "2836.40"),
            _word(382.5, 217.0, 387.9, 228.7, "0"),
            _word(431.1, 217.0, 452.7, 228.7, "0.00"),
            _word(474.3, 217.0, 495.9, 228.7, "3.00"),
            _word(533.7, 217.0, 560.7, 228.7, "342.84"),
            _word(582.3, 217.0, 604.0, 228.7, "25.00"),
            _word(636.4, 217.0, 668.8, 228.7, "2532.50"),
            _word(679.6, 217.0, 701.2, 228.7, "25.00"),
            _word(728.2, 217.0, 733.6, 228.7, "0"),
        ]
        built = from_pdf_words(_FitzWordPage(words, 841.5, 594.75))
        ready = resolver_ready(built.get("header_cells") or [])
        canons = [c.get("canonical") for c in ready.get("columns") or []]
        self.assertIn("opening_qty", canons)
        self.assertIn("opening_value", canons)
        self.assertIn("purchase_qty", canons)
        self.assertIn("purchase_value", canons)
        self.assertIn("sales_qty", canons)
        self.assertIn("sales_value", canons)
        self.assertIn("closing_qty", canons)
        self.assertIn("closing_value", canons)
        columns = ready.get("columns") or []
        rows = built.get("data_rows") or []
        ash = next(
            r
            for r in rows
            if any("ASHWAGANDA" in str(t.get("text") or "") for t in r)
        )
        fields = assign_cells(ash, columns).get("fields") or {}
        self.assertEqual(fields.get("opening_qty"), "0")
        self.assertEqual(fields.get("opening_value"), "0.00")
        self.assertEqual(fields.get("purchase_qty"), "5.00")
        self.assertEqual(fields.get("purchase_value"), "990.50")
        self.assertEqual(fields.get("closing_qty"), "5.00")
        self.assertEqual(fields.get("closing_value"), "990.50")
        gasex = next(
            r for r in rows if any("GASEX" in str(t.get("text") or "") for t in r)
        )
        gfields = assign_cells(gasex, columns).get("fields") or {}
        self.assertEqual(gfields.get("opening_qty"), "28.00")
        self.assertEqual(gfields.get("opening_value"), "2836.40")
        self.assertEqual(gfields.get("sales_qty"), "3.00")
        self.assertEqual(gfields.get("sales_value"), "342.84")
        self.assertEqual(gfields.get("closing_qty"), "25.00")
        self.assertEqual(gfields.get("closing_value"), "2532.50")


if __name__ == "__main__":
    unittest.main()
