"""Word-position parse of the Swil Op.Bal / LastPurc / Issue statement.

Expected quantities are the cells read from SEP HIMAL.PDF page 2
(S.B.AGENCY, September 2026). The fixture places those words at the
same column positions. It is not a copy of the source file.
"""

from __future__ import annotations

import unittest
from pathlib import Path

import fitz

from services.sales_statement_extractor import (
    _apply_stock_identity_validation,
    _ensure_stock_qty_value_fields,
    _extract_statement_from_pdf_group,
    _parse_pdf,
    _parse_swil_opbal_lastpurc_issue_pdf,
)
from services.schemas import normalize_sales_statement


def _put(page, x, y, text):
    page.insert_text((x, y), text, fontsize=6, fontname="helv")


def _fixture_doc():
    doc = fitz.open()
    page = doc.new_page(width=612, height=792)
    _put(page, 48, 40, "S.B.AGENCY")
    _put(page, 48, 70, "Sales & Stock Statement(From 01/09/2026 Upto 30/09/2026)")
    for x, text in (
        (48, "PRODUCT"),
        (88, "NAME"),
        (132, "PACKING"),
        (186, "Op.Bal."),
        (223, "Receipt"),
        (255, "LastPurc"),
        (302, "Total"),
        (339, "Issue"),
        (367, "Shortage"),
        (413, "Expiry"),
        (446, "Closing"),
        (496, "Near"),
    ):
        _put(page, x, 142, text)
    rows = [
        # name parts, pack, op, receipt, date, total, issue, shortage, expiry, close, near
        (182, [("HIORA-GA", 48), ("GEL", 90)], "15ML", "34", "0", "18/07/26", "34", "12", "0", "0", "22", "0"),
        (200, [("LIV", 48), ("52", 64), ("DRO", 94)], "60 ML", "31", "0", "18/07/26", "31", "23", "0", "0", "8", "0"),
        (218, [("LIV", 48), ("52", 66), ("DS", 86), ("TAB", 108)], "1X60", "162", "0", "31/08/26", "162", "43", "0", "0", "119", "0"),
        (236, [("LIV", 48), ("52", 66), ("DS", 86), ("200", 108)], "200 ML", "171", "105", "22/09/26", "276", "191", "0", "0", "85", "4"),
        (254, [("SEPTILIN", 48), ("SYP", 101)], "200 ML", "26", "112", "26/09/26", "138", "43", "0", "0", "95", "1"),
        (272, [("HIORA-D", 48), ("TOOTHPASTE100GM", 90)], "", "0", "0", "", "0", "0", "0", "0", "0", "0"),
        (290, [("ZERO", 48), ("ISSUE", 80)], "10 ML", "4", "0", "", "4", "0", "0", "0", "4", "0"),
        (308, [("MISSING", 48), ("ISSUE", 90)], "10 ML", "4", "1", "", "5", None, "0", "0", "5", "0"),
        # Size drawn across the PACKING edge, or fully left of it.
        (326, [("OPHTHACARE", 48), ("EYE", 104), ("D10ML", 125)], "", "0", "0", "", "0", "0", "0", "0", "0", "0"),
        (344, [("HIMCOCID", 48), ("SF", 90), ("BANANA200", 108)], "ML", "0", "0", "", "0", "0", "0", "0", "0", "0"),
        (362, [("RUMALAYA", 48), ("LINIMENT60", 100)], ("ML", 146), "1", "0", "05/10/24", "1", "0", "0", "0", "1", "0"),
        (380, [("LIV", 48), ("52", 64), ("120", 74), ("ML", 88), ("DRO", 108)], "100 ML", "0", "0", "", "0", "0", "0", "0", "0", "0"),
    ]
    qty_x = (190, 220, 255, 300, 336, 380, 420, 450, 500)
    for y, names, pack, *nums in rows:
        for text, x in names:
            _put(page, x, y, text)
        pack_text, pack_x = pack if isinstance(pack, tuple) else (pack, 132)
        if pack_text:
            _put(page, pack_x, y, pack_text)
        for x, value in zip(qty_x, nums):
            if value:
                _put(page, x, y, value)
    return doc


class SwilOpbalLastpurcTests(unittest.TestCase):
    def test_verified_rows_keep_issue_separate_from_closing(self):
        doc = _fixture_doc()
        parsed = _parse_swil_opbal_lastpurc_issue_pdf(doc, "fixture.pdf")
        self.assertIsNotNone(parsed)
        by_name = {item["product_name"]: item for item in parsed["line_items"]}
        expected = {
            "HIORA-GA GEL": ("15ML", 34, 0, 12, 22, 34),
            "LIV 52 DRO": ("60 ML", 31, 0, 23, 8, 31),
            "LIV 52 DS TAB": ("1X60", 162, 0, 43, 119, 162),
            "LIV 52 DS 200": ("200 ML", 171, 105, 191, 85, 276),
            "SEPTILIN SYP": ("200 ML", 26, 112, 43, 95, 138),
        }
        for name, (pack, opening, receipt, issue, closing, total) in expected.items():
            row = by_name[name]
            self.assertEqual(row["packing"], pack)
            self.assertEqual(row["opening_qty"], opening)
            self.assertEqual(row["receipts_qty"], receipt)
            self.assertEqual(row["sales_qty"], issue)
            self.assertEqual(row["closing_qty"], closing)
            self.assertEqual(row["extra"]["total_stock"], total)
            self.assertEqual(row["extra"]["shortage_qty"], 0)
            self.assertEqual(row["extra"]["source_columns"]["sales_qty"], "Issue Qty.")
        checked = _apply_stock_identity_validation(parsed)
        self.assertTrue(checked["totals"]["extra"]["stock_validation"]["is_valid"])
        self.assertEqual(
            checked["totals"]["extra"]["stock_identity_kind"],
            "swil_receipt_issue",
        )
        self.assertEqual(checked["totals"]["extra"]["stock_identity_fail_count"], 0)

    def test_glued_pack_stays_out_of_the_product_name(self):
        doc = _fixture_doc()
        parsed = _parse_swil_opbal_lastpurc_issue_pdf(doc, "fixture.pdf")
        row = next(
            item
            for item in parsed["line_items"]
            if item["product_name"] == "HIORA-D TOOTHPASTE"
        )
        self.assertEqual(row["packing"], "100GM")
        self.assertNotIn("100GM", row["product_name"])
        self.assertIn("TOOTHPASTE", row["product_name"])

    def test_printed_zero_is_kept_and_a_blank_issue_stays_missing(self):
        doc = _fixture_doc()
        parsed = _parse_swil_opbal_lastpurc_issue_pdf(doc, "fixture.pdf")
        by_name = {item["product_name"]: item for item in parsed["line_items"]}
        self.assertEqual(by_name["ZERO ISSUE"]["sales_qty"], 0.0)
        self.assertIsNone(by_name["MISSING ISSUE"]["sales_qty"])
        self.assertIsNone(by_name["ZERO ISSUE"]["sales_value"])

    def test_page_group_keeps_word_issue_when_a_page_image_is_present(self):
        doc = _fixture_doc()
        page = doc[0]
        group = {
            "pages": [
                {
                    "page_index": 0,
                    "text": page.get_text("text") or "",
                    "words": page.get_text("words") or [],
                    "image_bytes": b"not-a-real-image",
                }
            ]
        }
        parsed = _extract_statement_from_pdf_group(group, "SEP HIMAL.PDF")
        by_name = {item["product_name"]: item for item in parsed["line_items"]}
        self.assertEqual(by_name["HIORA-GA GEL"]["sales_qty"], 12)
        self.assertEqual(by_name["LIV 52 DS 200"]["sales_qty"], 191)
        self.assertEqual(by_name["LIV 52 DS 200"]["closing_qty"], 85)
        method = parsed["totals"]["extra"]["extraction_method"]
        self.assertEqual(method, "swil_opbal_lastpurc_text")

    def test_parse_pdf_returns_the_word_parser_before_vision(self):
        doc = _fixture_doc()
        parsed = _parse_pdf(doc.tobytes(), "SEP HIMAL.PDF")
        by_name = {item["product_name"]: item for item in parsed["line_items"]}
        self.assertEqual(by_name["HIORA-GA GEL"]["sales_qty"], 12)
        self.assertEqual(
            parsed["totals"]["extra"]["extraction_method"],
            "swil_opbal_lastpurc_text",
        )

    def test_pack_size_follows_the_column_edge(self):
        doc = _fixture_doc()
        parsed = _parse_swil_opbal_lastpurc_issue_pdf(doc, "fixture.pdf")
        by_name = {item["product_name"]: item for item in parsed["line_items"]}
        self.assertEqual(by_name["OPHTHACARE EYE"]["packing"], "D10ML")
        self.assertNotIn("D10ML", by_name["OPHTHACARE EYE"]["product_name"])
        self.assertEqual(by_name["HIMCOCID SF BANANA"]["packing"], "200 ML")
        self.assertNotIn("200", by_name["HIMCOCID SF BANANA"]["product_name"])
        self.assertEqual(by_name["RUMALAYA LINIMENT"]["packing"], "60 ML")
        self.assertEqual(by_name["RUMALAYA LINIMENT"]["opening_qty"], 1)
        self.assertEqual(by_name["RUMALAYA LINIMENT"]["closing_qty"], 1)
        kept = by_name["LIV 52 120 ML DRO"]
        self.assertEqual(kept["product_name"], "LIV 52 120 ML DRO")
        self.assertEqual(kept["packing"], "100 ML")
        self.assertEqual(by_name["LIV 52 DS 200"]["product_name"], "LIV 52 DS 200")
        self.assertEqual(by_name["LIV 52 DS 200"]["packing"], "200 ML")

    def test_missing_money_stays_null_and_explicit_zero_stays_zero(self):
        doc = _fixture_doc()
        parsed = _parse_swil_opbal_lastpurc_issue_pdf(doc, "fixture.pdf")
        parsed["line_items"][0]["sales_value"] = 0.0
        parsed["line_items"][0]["closing_value"] = 0.0
        ensured = _ensure_stock_qty_value_fields(parsed)
        printed = ensured["line_items"][0]
        missing = ensured["line_items"][1]
        self.assertEqual(printed["sales_value"], 0.0)
        self.assertEqual(printed["closing_value"], 0.0)
        self.assertIsNone(missing["sales_value"])
        self.assertIsNone(missing["closing_value"])
        self.assertEqual(printed["sales_qty"], 12)
        projected = normalize_sales_statement(ensured)
        self.assertEqual(projected["line_items"][0]["sales_value"], 0.0)
        self.assertIsNone(projected["line_items"][1]["sales_value"])
        self.assertIsNone(projected["line_items"][1]["closing_value"])
        other = _ensure_stock_qty_value_fields(
            {
                "line_items": [
                    {
                        "product_name": "OTHER",
                        "opening_qty": 1,
                        "receipts_qty": 0,
                        "sales_qty": 0,
                        "closing_qty": 1,
                        "sales_value": None,
                        "closing_value": None,
                        "extra": {},
                    }
                ],
                "totals": {"sales_value": None, "closing_value": None, "extra": {"extraction_method": "swil_erp"}},
            }
        )
        self.assertEqual(other["line_items"][0]["sales_value"], 0.0)
        self.assertEqual(other["line_items"][0]["closing_value"], 0.0)

    def test_source_pdf_keeps_all_quantities(self):
        path = Path(r"C:\Users\amnsa\Downloads\SEP HIMAL.PDF")
        if not path.is_file():
            self.skipTest("SEP HIMAL.PDF is not available")
        parsed = _parse_pdf(path.read_bytes(), path.name)
        checked = _apply_stock_identity_validation(parsed)
        ensured = _ensure_stock_qty_value_fields(checked)
        items = ensured["line_items"]
        self.assertEqual(len(items), 80)
        actual = [
            (item["opening_qty"], item["receipts_qty"], item["sales_qty"], item["closing_qty"])
            for item in items
        ]
        self.assertEqual(actual, _SOURCE_QTY)
        by_name = {item["product_name"]: item for item in items}
        self.assertEqual(by_name["OPHTHACARE EYE"]["packing"], "D10ML")
        self.assertEqual(by_name["HIMCOCID SF BANANA"]["packing"], "200 ML")
        self.assertEqual(by_name["RUMALAYA LINIMENT"]["packing"], "60 ML")
        self.assertEqual(by_name["RUMALAYA LINIMENT"]["opening_qty"], 1.0)
        self.assertEqual(by_name["RUMALAYA LINIMENT"]["sales_qty"], 0.0)
        self.assertEqual(by_name["RUMALAYA LINIMENT"]["closing_qty"], 1.0)
        self.assertEqual(by_name["LIV 52 120 ML DRO"]["packing"], "100 ML")
        self.assertEqual(by_name["GALACTOSURE GRAN"]["packing"], "200GM")
        self.assertEqual(by_name["LIV 52 DS SUGAR FREE"]["packing"], "100ML")
        self.assertIn("FREE", by_name["LIV 52 DS SUGAR FREE"]["product_name"])
        self.assertEqual(by_name["LIV 52 DS 200"]["sales_qty"], 191.0)
        self.assertEqual(by_name["HIORA-GA GEL"]["sales_qty"], 12.0)
        for item in items:
            self.assertIsNone(item["sales_value"])
            self.assertIsNone(item["closing_value"])
        self.assertTrue(ensured["totals"]["extra"]["stock_validation"]["is_valid"])
        self.assertEqual(ensured["totals"]["extra"]["stock_identity_fail_count"], 0)


# Quantities read from SEP HIMAL.PDF before the packing split. Order is the
# statement order. Names are not part of this lock.
_SOURCE_QTY = [
    (0.0, 0.0, 0.0, 0.0),
    (110.0, 1.0, 16.0, 95.0),
    (1.0, 0.0, 0.0, 1.0),
    (33.0, 0.0, 2.0, 31.0),
    (27.0, 0.0, 11.0, 16.0),
    (1269.0, 0.0, 420.0, 849.0),
    (157.0, 0.0, 47.0, 110.0),
    (215.0, 350.0, 250.0, 315.0),
    (64.0, 0.0, 3.0, 61.0),
    (17.0, 0.0, 0.0, 17.0),
    (8.0, 0.0, 2.0, 6.0),
    (0.0, 0.0, 0.0, 0.0),
    (49.0, 0.0, 8.0, 41.0),
    (26.0, 0.0, 6.0, 20.0),
    (113.0, 0.0, 37.0, 76.0),
    (80.0, 0.0, 54.0, 26.0),
    (0.0, 0.0, 0.0, 0.0),
    (40.0, 0.0, 13.0, 27.0),
    (0.0, 0.0, 0.0, 0.0),
    (4.0, 0.0, 0.0, 4.0),
    (56.0, 0.0, 0.0, 56.0),
    (49.0, 0.0, 26.0, 23.0),
    (0.0, 0.0, 0.0, 0.0),
    (8.0, 0.0, 0.0, 8.0),
    (3.0, 0.0, 0.0, 3.0),
    (21.0, 0.0, 0.0, 21.0),
    (70.0, 400.0, 278.0, 192.0),
    (5.0, 56.0, 43.0, 18.0),
    (3.0, 56.0, 32.0, 27.0),
    (89.0, 0.0, 44.0, 45.0),
    (2.0, 12.0, 0.0, 14.0),
    (45.0, 0.0, 34.0, 11.0),
    (0.0, 0.0, 0.0, 0.0),
    (10.0, 0.0, 5.0, 5.0),
    (45.0, 0.0, 5.0, 40.0),
    (0.0, 0.0, 0.0, 0.0),
    (34.0, 0.0, 12.0, 22.0),
    (1.0, 0.0, 0.0, 1.0),
    (0.0, 0.0, 0.0, 0.0),
    (0.0, 0.0, 0.0, 0.0),
    (31.0, 0.0, 23.0, 8.0),
    (162.0, 0.0, 43.0, 119.0),
    (171.0, 105.0, 191.0, 85.0),
    (31.0, 0.0, 0.0, 31.0),
    (38.0, 0.0, 6.0, 32.0),
    (77.0, 0.0, 5.0, 72.0),
    (8.0, 0.0, 4.0, 4.0),
    (22.0, 0.0, 0.0, 22.0),
    (0.0, 0.0, 0.0, 0.0),
    (25.0, 0.0, 6.0, 19.0),
    (26.0, 112.0, 43.0, 95.0),
    (1.0, 0.0, 0.0, 1.0),
    (0.0, 0.0, 0.0, 0.0),
    (0.0, 0.0, 0.0, 0.0),
    (0.0, 0.0, 0.0, 0.0),
    (0.0, 0.0, 0.0, 0.0),
    (0.0, 0.0, 0.0, 0.0),
    (0.0, 0.0, 0.0, 0.0),
    (0.0, 0.0, 0.0, 0.0),
    (0.0, 0.0, 0.0, 0.0),
    (0.0, 0.0, 0.0, 0.0),
    (0.0, 0.0, 0.0, 0.0),
    (0.0, 0.0, 0.0, 0.0),
    (0.0, 0.0, 0.0, 0.0),
    (0.0, 0.0, 0.0, 0.0),
    (0.0, 0.0, 0.0, 0.0),
    (0.0, 0.0, 0.0, 0.0),
    (0.0, 0.0, 0.0, 0.0),
    (0.0, 0.0, 0.0, 0.0),
    (0.0, 0.0, 0.0, 0.0),
    (0.0, 0.0, 0.0, 0.0),
    (0.0, 0.0, 0.0, 0.0),
    (0.0, 0.0, 0.0, 0.0),
    (0.0, 0.0, 0.0, 0.0),
    (0.0, 0.0, 0.0, 0.0),
    (0.0, 0.0, 0.0, 0.0),
    (1.0, 0.0, 0.0, 1.0),
    (0.0, 0.0, 0.0, 0.0),
    (0.0, 0.0, 0.0, 0.0),
    (0.0, 0.0, 0.0, 0.0),
]


if __name__ == "__main__":
    unittest.main()
