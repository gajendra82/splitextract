"""ZL Opening_bal_qty PDF — stockist is the first header line before Material_name."""

from __future__ import annotations

import unittest
from pathlib import Path

from services.sales_statement_extractor import (
    _is_zl_opening_bal_pdf_text,
    _parse_zl_opening_bal_pdf_doc,
    extract_sales_statement,
)


FIXTURE = Path(
    "/var/www/html/splitextract/"
    "0000704724_2026_08_ZL_19_553_04092026111705.pdf"
)

SAMPLE = """
VERMA MEDICAL STORE
Material_name Mrp Secondaryrate Opening_bal_qty Primary_qty Closing_bal_qty
AACTARIL SOAP 75G INDIA 115 77.68 58 0.00 41
ABANA TABS 60 s 220 148.6 63 0.00 42
CLARINA ANTI ACNE CREAM 30g 192 129.68 10 0.00 7
CONFIDO TABS (FC) 60 s (AQ) 255 172.23 15 100.00 79
"""


class _FakePage:
    def __init__(self, text: str, words=None):
        self._text = text
        self._words = words or []

    def get_text(self, kind: str = "text"):
        if kind == "text":
            return self._text
        if kind == "words":
            return self._words
        return []


class _FakeDoc(list):
    pass


class TestZlOpeningBalPdf(unittest.TestCase):
    def test_gate_accepts_material_opening_headers(self):
        self.assertTrue(_is_zl_opening_bal_pdf_text(SAMPLE))
        self.assertFalse(
            _is_zl_opening_bal_pdf_text(
                "Sales & Stock Statement Op.Bal Receipt Issue Closing\n"
            )
        )

    def test_live_verma_pdf_captures_stockist(self):
        if not FIXTURE.exists():
            self.skipTest("fixture PDF not present")
        result = extract_sales_statement(FIXTURE.read_bytes(), FIXTURE.name)
        method = ((result.get("totals") or {}).get("extra") or {}).get(
            "extraction_method"
        )
        self.assertEqual(method, "zl_opening_bal_pdf")
        self.assertEqual(result.get("stockist_name"), "VERMA MEDICAL STORE")
        # Must not park the stockist under company_name only.
        self.assertNotEqual(result.get("company_name"), "VERMA MEDICAL STORE")
        items = result.get("line_items") or []
        self.assertGreaterEqual(len(items), 20)
        by_name = {i["product_name"]: i for i in items}
        aact = by_name["AACTARIL SOAP 75G INDIA"]
        self.assertEqual(aact["opening_qty"], 58.0)
        self.assertEqual(aact["receipts_qty"], 0.0)
        self.assertEqual(aact["closing_qty"], 41.0)
        conf = by_name["CONFIDO TABS (FC) 60 s (AQ)"]
        self.assertEqual(conf["opening_qty"], 15.0)
        self.assertEqual(conf["receipts_qty"], 100.0)
        self.assertEqual(conf["closing_qty"], 79.0)


if __name__ == "__main__":
    unittest.main()
