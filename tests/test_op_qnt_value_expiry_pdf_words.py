"""Pipe-ruled STOCK REPORT: Particular / Op.Qnt / Op.Value / Expiry / Pur / Sl / Cl."""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from services.stock_header_resolver import parse_number, resolve_columns
from services.stock_native_tables import _is_footer_or_total_tokens


def _cells(*texts):
    return [
        {"text": t, "x_center": None, "col_index": i, "subheader_text": None}
        for i, t in enumerate(texts)
    ]


class OpQntValueExpiryHeaderTests(unittest.TestCase):
    def test_headers_map_expiry_ignore_and_cl_qnt_closing(self):
        cols = resolve_columns(
            _cells(
                "Particular",
                "Op.Qnt.",
                "Op.Value",
                "Expiry",
                "Pur.Qnt.",
                "Pur.Value",
                "Sl.Qnt.",
                "Sl.Value",
                "Cl.Qnt.",
                "Cl.Value",
            )
        )["columns"]
        by = {c["header_text"]: c["canonical"] for c in cols}
        self.assertEqual(by["Op.Qnt."], "opening_qty")
        self.assertEqual(by["Op.Value"], "opening_value")
        self.assertEqual(by["Expiry"], "ignore")
        self.assertEqual(by["Pur.Qnt."], "purchase_qty")
        self.assertEqual(by["Sl.Qnt."], "sales_qty")
        self.assertEqual(by["Cl.Qnt."], "closing_qty")
        self.assertEqual(by["Cl.Value"], "closing_value")


class PipeGutterNumberTests(unittest.TestCase):
    def test_pipe_ruled_amounts(self):
        self.assertEqual(parse_number("33.00|"), 33.0)
        self.assertEqual(parse_number("1884.14|05/2029|"), 1884.14)


class ProductTotalRowKeepTests(unittest.TestCase):
    def test_product_total_with_numbers_is_not_footer(self):
        tokens = [
            {"text": "Total :", "x": 0.2},
            {"text": "33.00", "x": 0.28},
            {"text": "1884.14", "x": 0.34},
            {"text": "56.00", "x": 0.47},
            {"text": "3197.32", "x": 0.54},
            {"text": "31.00", "x": 0.61},
            {"text": "1769.95", "x": 0.68},
            {"text": "58.00", "x": 0.76},
            {"text": "3311.51", "x": 0.82},
        ]
        self.assertFalse(_is_footer_or_total_tokens(tokens))

    def test_company_total_is_footer(self):
        tokens = [
            {"text": "COMPANY Total :", "x": 0.3},
            {"text": "259042.59", "x": 0.5},
            {"text": "114784.70", "x": 0.6},
        ]
        self.assertTrue(_is_footer_or_total_tokens(tokens))


class MithilaPdfWordsIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        root = Path(__file__).resolve().parents[1]
        candidates = [
            Path(r"C:\Users\adity\Downloads\Mithila Sept26.PDF"),
            root / "debug_uploads" / "5290b4e414e490b7_Mithila_Sept26.PDF",
        ]
        cls.pdf_path = next((p for p in candidates if p.is_file()), None)

    def test_extracts_op_pur_sl_cl_with_expiry_column(self):
        if self.pdf_path is None:
            self.skipTest("Mithila Sept26.PDF not available")
        from services.stock_native_resolver import parse_pdf_free_column_statement

        with patch.dict(
            "os.environ",
            {
                "STOCK_PDF_WORDS_FREE_COLUMNS": "true",
                "STOCK_PDF_RESOLVED_MOVEMENT_COLUMNS": "true",
            },
            clear=False,
        ):
            result = parse_pdf_free_column_statement(
                self.pdf_path.read_bytes(), self.pdf_path.name
            )
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(
            (result.get("totals") or {}).get("extra", {}).get("extraction_method"),
            "pdf_words_free_columns",
        )
        self.assertIn("MITHILA", str(result.get("stockist_name") or "").upper())
        by_name = {
            str(i.get("product_name") or ""): i for i in result.get("line_items") or []
        }
        bonison = by_name.get("BONISON 100 ML")
        self.assertIsNotNone(bonison)
        assert bonison is not None
        self.assertEqual(bonison.get("opening_qty"), 33.0)
        self.assertEqual(bonison.get("receipts_qty"), 56.0)
        self.assertEqual(bonison.get("sales_qty"), 31.0)
        self.assertEqual(bonison.get("closing_qty"), 58.0)
        self.assertEqual(bonison.get("opening_value"), 1884.14)
        self.assertEqual(bonison.get("closing_value"), 3311.51)

        multi = by_name.get("BONISON 200ML")
        self.assertIsNotNone(multi)
        assert multi is not None
        self.assertEqual(multi.get("opening_qty"), 20.0)
        self.assertEqual(multi.get("receipts_qty"), 56.0)
        self.assertEqual(multi.get("closing_qty"), 56.0)

        pilex = by_name.get("PILEX FORT TAB")
        self.assertIsNotNone(pilex)
        assert pilex is not None
        self.assertEqual(pilex.get("opening_qty"), 48.0)
        self.assertEqual(pilex.get("sales_qty"), 4.0)
        self.assertEqual(pilex.get("closing_qty"), 44.0)


if __name__ == "__main__":
    unittest.main()
