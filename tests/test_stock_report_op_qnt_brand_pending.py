"""Op.Qnt/Pur/Sl/Cl STOCK REPORT: brand banners + pack-size dead zone."""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from services.stock_header_resolver import assign_cells, resolve_columns
from services.stock_native_resolver import (
    _is_brand_section_pending_name,
    build_items_from_pdf_page_results,
    _items_from_resolved_word_columns,
)


def _hdr(text: str, x0: float, x1: float, idx: int, width: float = 594.7):
    return {
        "text": text,
        "col_index": idx,
        "x0": x0,
        "x1": x1,
        "x_center": ((x0 + x1) / 2.0) / width,
        "subheader_text": None,
    }


class BrandSectionPendingTests(unittest.TestCase):
    def test_brand_section_detector(self):
        self.assertTrue(_is_brand_section_pending_name("HIMALAYA ZENITH"))
        self.assertTrue(_is_brand_section_pending_name("HIMALYA ZANDRA"))
        self.assertTrue(_is_brand_section_pending_name("HIMALYA DRUG"))
        self.assertFalse(_is_brand_section_pending_name("AMALAKI TAB"))
        self.assertFalse(_is_brand_section_pending_name("GENTLE BABY POWDER"))

    def test_pending_brand_banner_not_prepended(self):
        width = 594.7
        headers = [
            _hdr("Particular", 19.2, 61.2, 0),
            _hdr("Op.Qnt.", 162.0, 191.4, 1),
            _hdr("Op.Value", 204.0, 237.6, 2),
            _hdr("Pur.Qnt.", 250.2, 283.8, 3),
            _hdr("Pur.Value", 292.2, 330.0, 4),
            _hdr("Sl.Qnt.", 346.8, 376.2, 5),
            _hdr("Sl.Value", 388.8, 422.4, 6),
            _hdr("Cl.Qnt.", 439.2, 468.6, 7),
            _hdr("Cl.Value", 481.2, 514.8, 8),
        ]
        page = {
            "header_cells": headers,
            "data_rows": [
                [
                    {"text": "HIMALAYA", "x0": 19.0, "x1": 53.0, "x": 19.0 / width},
                    {"text": "ZENITH", "x0": 57.0, "x1": 82.0, "x": 57.0 / width},
                ],
                [
                    {"text": "AMALAKI TAB", "x0": 19.0, "x1": 65.0, "x": 19.0 / width},
                    {"text": "7.00", "x0": 175.0, "x1": 191.0, "x": 175.0 / width},
                    {"text": "1070.24", "x0": 208.0, "x1": 238.0, "x": 208.0 / width},
                    {"text": "7.00", "x0": 452.0, "x1": 469.0, "x": 452.0 / width},
                    {"text": "1070.24", "x0": 485.0, "x1": 515.0, "x": 485.0 / width},
                ],
                [
                    {"text": "HIMALYA", "x0": 19.0, "x1": 49.0, "x": 19.0 / width},
                    {"text": "ZANDRA", "x0": 53.0, "x1": 78.0, "x": 53.0 / width},
                ],
                [
                    {"text": "ABANA TAB", "x0": 19.0, "x1": 65.0, "x": 19.0 / width},
                    {"text": "71.00", "x0": 170.0, "x1": 191.0, "x": 170.0 / width},
                    {"text": "10023.07", "x0": 204.0, "x1": 238.0, "x": 204.0 / width},
                    {"text": "25.00", "x0": 355.0, "x1": 376.0, "x": 355.0 / width},
                    {"text": "3529.25", "x0": 393.0, "x1": 422.0, "x": 393.0 / width},
                    {"text": "46.00", "x0": 448.0, "x1": 469.0, "x": 448.0 / width},
                    {"text": "6493.82", "x0": 485.0, "x1": 515.0, "x": 485.0 / width},
                ],
            ],
        }
        with patch.dict(
            "os.environ",
            {"STOCK_PDF_WORDS_SKIP_BRAND_SECTION_PENDING": "true"},
            clear=False,
        ):
            items = build_items_from_pdf_page_results([page])
            names = [it.get("product_name") for it in items]
            self.assertIn("AMALAKI TAB", names)
            self.assertIn("ABANA TAB", names)
            self.assertFalse(any(n and n.upper().startswith("HIMAL") for n in names))


class PackInProductDeadZoneTests(unittest.TestCase):
    def test_pack_size_between_particular_and_op_qnt(self):
        width = 594.7
        headers = [
            _hdr("Particular", 19.2, 61.2, 0),
            _hdr("Op.Qnt.", 162.0, 191.4, 1),
            _hdr("Op.Value", 204.0, 237.6, 2),
            _hdr("Pur.Qnt.", 250.2, 283.8, 3),
            _hdr("Pur.Value", 292.2, 330.0, 4),
            _hdr("Sl.Qnt.", 346.8, 376.2, 5),
            _hdr("Sl.Value", 388.8, 422.4, 6),
            _hdr("Cl.Qnt.", 439.2, 468.6, 7),
            _hdr("Cl.Value", 481.2, 514.8, 8),
        ]
        with patch.dict(
            "os.environ",
            {"STOCK_HEADER_PACK_IN_PRODUCT_DEAD_ZONE": "true"},
            clear=False,
        ):
            cols = resolve_columns(headers)["columns"]
            by_idx = {int(h["col_index"]): h for h in headers}
            for c in cols:
                h = by_idx.get(int(c["col_index"]))
                if h:
                    c["x_center"] = h["x_center"]
            # 400GM sits in the Particular|Op.Qnt dead zone (~0.184).
            tokens = [
                {"text": "GENTLE BABY POWDER", "x": 19.0 / width},
                {"text": "400GM", "x": 99.0 / width},
                {"text": "14.00", "x": 170.0 / width},
                {"text": "3261.99", "x": 208.0 / width},
                {"text": "10.00", "x": 355.0 / width},
                {"text": "2329.99", "x": 393.0 / width},
                {"text": "4.00", "x": 452.0 / width},
                {"text": "932.00", "x": 490.0 / width},
            ]
            out = assign_cells(tokens, cols)
            fields = out["fields"]
            self.assertFalse(
                any(e.get("code") == "AMBIGUOUS_CELL" for e in out.get("errors") or [])
            )
            self.assertEqual(fields.get("product_name"), "GENTLE BABY POWDER 400GM")
            self.assertEqual(fields.get("opening_qty"), "14.00")
            self.assertEqual(fields.get("sales_qty"), "10.00")
            self.assertEqual(fields.get("closing_qty"), "4.00")


@unittest.skipUnless(
    Path("/var/www/html/splitextract/Stock_Report (2).PDF").is_file()
    or Path("Stock_Report (2).PDF").is_file(),
    "Stock_Report (2).PDF fixture missing",
)
class StockReport2PdfFixtureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = Path("/var/www/html/splitextract/Stock_Report (2).PDF")
        if not path.is_file():
            path = Path("Stock_Report (2).PDF")
        cls.raw = path.read_bytes()

    def test_pdf_words_recovers_128_products_and_footer_qty(self):
        from services.stock_native_tables import from_fitz_pdf_bytes

        with patch.dict(
            "os.environ",
            {
                "STOCK_PDF_WORDS_SKIP_BRAND_SECTION_PENDING": "true",
                "STOCK_HEADER_PACK_IN_PRODUCT_DEAD_ZONE": "true",
                "ENABLE_GEMINI_EXTRACTION_FALLBACK": "false",
            },
            clear=False,
        ):
            built = from_fitz_pdf_bytes(self.raw)
            parsed = _items_from_resolved_word_columns(built["page_results"])
            items = (parsed or {}).get("line_items") or []
            names = [it.get("product_name") for it in items]
            self.assertEqual(len(items), 128)
            self.assertIn("AMALAKI TAB", names)
            self.assertIn("GENTLE BABY POWDER 400GM", names)
            self.assertIn("GENTLE BABY POWDER 700GM", names)
            self.assertIn("ABANA TAB", names)
            self.assertIn("BABY CARE GIFT PACK(JAR)", names)
            self.assertFalse(
                any(n and n.upper().startswith("HIMAL") for n in names)
            )
            self.assertEqual(sum(it.get("opening_qty") or 0 for it in items), 6267.0)
            self.assertEqual(sum(it.get("receipts_qty") or 0 for it in items), 1413.0)
            self.assertEqual(sum(it.get("sales_qty") or 0 for it in items), 3509.0)
            self.assertEqual(sum(it.get("closing_qty") or 0 for it in items), 4179.0)


if __name__ == "__main__":
    unittest.main()
