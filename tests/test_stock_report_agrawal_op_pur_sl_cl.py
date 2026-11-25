"""Regression: Particular|Packing|Op/Pur/Sl/Cl qty+value STOCK REPORT (Agrawal).

Native-text PDF. Must keep Op.Value / Pur.Value whole (no unglue), map qty vs
value columns without left-shift on blank cells, peel packing, and skip the
period banner as a product.
"""

from __future__ import annotations

import unittest
from pathlib import Path

from services.stock_header_resolver import (
    _direct_value_field,
    _is_batch_type_marker_token,
    normalize_header,
    resolve_columns,
)
from services.stock_native_resolver import _TRAILING_PACK_RE
from services.stock_native_tables import _exact_header_alias, _split_glued_header_text

ROOT = Path(__file__).resolve().parents[1]
PDF = ROOT / "tests" / "fixtures" / "stock_report_agrawal_himalaya_oct2026.pdf"
DOWNLOADS_PDF = Path(r"C:\Users\amnsa\Downloads\Stock_Report（1）（1）.PDF")


def _pdf_bytes() -> bytes:
    path = PDF if PDF.is_file() else DOWNLOADS_PDF
    if not path.is_file():
        raise unittest.SkipTest("Agrawal Himalaya stock report PDF not available")
    return path.read_bytes()


class OpValueUnglueTests(unittest.TestCase):
    def test_op_value_and_pur_value_stay_whole(self):
        for token in ("Op.Value", "Pur.Value", "Op.Qnt.", "Sl.Value", "Cl.Value"):
            self.assertIsNone(
                _split_glued_header_text(token),
                msg=f"{token} must not unglue",
            )
        self.assertEqual(_direct_value_field(normalize_header("Op.Value")), "opening_value")
        self.assertEqual(_direct_value_field(normalize_header("Pur.Value")), "purchase_value")
        self.assertTrue(_exact_header_alias("Op.Value"))
        self.assertTrue(_exact_header_alias("Pur.Value"))

    def test_resolve_ten_column_grid(self):
        cols = resolve_columns(
            [
                {"text": t, "col_index": i, "x_center": None, "subheader_text": None}
                for i, t in enumerate(
                    [
                        "Particular",
                        "Packing",
                        "Op.Qnt.",
                        "Op.Value",
                        "Pur.Qnt.",
                        "Pur.Value",
                        "Sl.Qnt.",
                        "Sl.Value",
                        "Cl.Qnt.",
                        "Cl.Value",
                    ]
                )
            ]
        )["columns"]
        by = {c["header_text"]: c["canonical"] for c in cols}
        self.assertEqual(by["Op.Qnt."], "opening_qty")
        self.assertEqual(by["Op.Value"], "opening_value")
        self.assertEqual(by["Pur.Qnt."], "purchase_qty")
        self.assertEqual(by["Pur.Value"], "purchase_value")
        self.assertEqual(by["Sl.Qnt."], "sales_qty")
        self.assertEqual(by["Sl.Value"], "sales_value")
        self.assertEqual(by["Cl.Qnt."], "closing_qty")
        self.assertEqual(by["Cl.Value"], "closing_value")
        self.assertEqual(by["Packing"], "pack")

    def test_batch_marker_helper_exists(self):
        self.assertTrue(_is_batch_type_marker_token("BATCH"))
        self.assertFalse(_is_batch_type_marker_token("75 GM"))

    def test_trailing_pack_allows_ml_dot_and_units(self):
        self.assertTrue(_TRAILING_PACK_RE.match("HIMCOCID SYP. 200 ML."))
        self.assertTrue(_TRAILING_PACK_RE.match("BONNISAN SYRUP 100 ML."))
        self.assertTrue(_TRAILING_PACK_RE.match("POB BAG UNITS"))
        self.assertTrue(_TRAILING_PACK_RE.match("ASHVAGANDHA CAPSULES 60 TAB"))


class AgrawalStockReportIntegrationTests(unittest.TestCase):
    def test_pdf_words_extracts_qty_value_grid(self):
        from services.stock_native_resolver import parse_pdf_free_column_statement

        result = parse_pdf_free_column_statement(_pdf_bytes(), "Stock_Report.pdf")
        self.assertIsNotNone(result)
        assert result is not None
        items = result.get("line_items") or []
        self.assertEqual(len(items), 73)
        self.assertEqual(result.get("stockist_name"), "AGRAWAL TRADERS")
        self.assertIn("ADITYA PLACE", str(result.get("stockist_address") or "").upper())
        self.assertIn("HIMALAYA", str(result.get("company_name") or "").upper())
        self.assertEqual(result.get("period_from"), "2026-10-01")
        self.assertEqual(result.get("period_to"), "2026-10-31")

        names = [str(i.get("product_name") or "") for i in items]
        self.assertFalse(any("01/10/26" in n for n in names))
        self.assertFalse(any(n.upper().startswith("QNT") for n in names))

        by = {str(i.get("product_name") or ""): i for i in items}
        aact = by.get("AACTARIL SOAP")
        self.assertIsNotNone(aact)
        assert aact is not None
        self.assertEqual(aact.get("packing"), "75 GM")
        self.assertEqual(aact.get("opening_qty"), -27.0)
        self.assertEqual(aact.get("receipts_qty"), 144.0)
        self.assertTrue(aact.get("sales_qty") in (None, 0, 0.0))
        self.assertEqual(aact.get("closing_qty"), 117.0)
        self.assertEqual(aact.get("purchase_value") or aact.get("receipts_value"), 10627.2)
        self.assertEqual(aact.get("closing_value"), 8634.6)

        abana = by.get("ABANA TAB.")
        self.assertIsNotNone(abana)
        assert abana is not None
        self.assertEqual(abana.get("packing"), "60'S")
        self.assertEqual(abana.get("opening_qty"), 65.0)
        self.assertEqual(abana.get("opening_value"), 9659.0)
        self.assertTrue(abana.get("receipts_qty") in (None, 0, 0.0))
        self.assertEqual(abana.get("sales_qty"), 8.0)
        self.assertEqual(abana.get("closing_qty"), 57.0)

        bonni = [
            i
            for i in items
            if str(i.get("product_name") or "") == "BONNISAN SYRUP"
            and "100" in str(i.get("packing") or "")
        ]
        self.assertEqual(len(bonni), 1)
        b = bonni[0]
        self.assertEqual(b.get("opening_qty"), 80.0)
        self.assertEqual(b.get("receipts_qty"), 112.0)
        self.assertEqual(b.get("sales_qty"), 56.0)
        self.assertEqual(b.get("closing_qty"), 136.0)

        def _f(v):
            try:
                return float(v) if v not in (None, "") else 0.0
            except Exception:
                return 0.0

        sums = {
            "opening_qty": 0.0,
            "receipts_qty": 0.0,
            "sales_qty": 0.0,
            "closing_qty": 0.0,
            "opening_value": 0.0,
            "purchase_value": 0.0,
            "sales_value": 0.0,
            "closing_value": 0.0,
        }
        for it in items:
            sums["opening_qty"] += _f(it.get("opening_qty"))
            sums["receipts_qty"] += _f(it.get("receipts_qty") or it.get("purchase_qty"))
            sums["sales_qty"] += _f(it.get("sales_qty"))
            sums["closing_qty"] += _f(it.get("closing_qty"))
            sums["opening_value"] += _f(it.get("opening_value"))
            sums["purchase_value"] += _f(
                it.get("purchase_value") or it.get("receipts_value")
            )
            sums["sales_value"] += _f(it.get("sales_value"))
            sums["closing_value"] += _f(it.get("closing_value"))

        # Monetary company totals (footer) match line-item sums.
        self.assertAlmostEqual(sums["opening_value"], 1127544.00, places=2)
        self.assertAlmostEqual(sums["purchase_value"], 174884.44, places=2)
        self.assertAlmostEqual(sums["sales_value"], 198454.47, places=2)
        self.assertAlmostEqual(sums["closing_value"], 889288.65, places=2)

        # Qty footer omits the printed HIMCOCID adjustment row (-37 / 4 / -41).
        # Line-item fidelity keeps that row; sums therefore differ from footer qty.
        him = by.get("HIMCOCID SYP.")
        self.assertIsNotNone(him)
        assert him is not None
        self.assertEqual(him.get("opening_qty"), -37.0)
        self.assertEqual(him.get("sales_qty"), 4.0)
        self.assertEqual(him.get("closing_qty"), -41.0)
        self.assertAlmostEqual(sums["opening_qty"] - (-37.0), 8537.0, places=2)
        self.assertAlmostEqual(sums["receipts_qty"], 1566.0, places=2)
        self.assertAlmostEqual(sums["sales_qty"] - 4.0, 1555.0, places=2)
        self.assertAlmostEqual(sums["closing_qty"] - (-41.0), 6856.0, places=2)

        null_pack = [i for i in items if not str(i.get("packing") or "").strip()]
        self.assertEqual(null_pack, [])


if __name__ == "__main__":
    unittest.main()
