"""SwilERP Tax Rate + Packing + Closing/Dump/Near (Apex Agencies)."""

from __future__ import annotations

import unittest
from pathlib import Path

from services.sales_statement_extractor import (
    _is_swil_stacked_opbal_expiry_near_format,
    _is_swil_tax_pack_dump_near_text,
    _parse_swil_stacked_opbal_expiry_near_statement,
    extract_sales_statement,
)


APEX_PDF = Path(
    r"C:\Users\anuja\Downloads\ZA_2026_August"
    r"\0000700218_2026_08_ZA_18_613_08092026041706.PDF"
)

APEX_HEADER = """
APEX AGENCIES
DHENKIKOTE,
Page No.1
Sales & Stock Statement(From 01/08/2026 Upto 29/08/2026)
HIMALAYA WELLNESS CO (ZANDRA)
PRODUCT NAME
Shelf
Tax
PACKING
Op.Bal.
Receipt
Total
Issue
Closing
Dump
Near
MSR
ID
Rate
Qty.
Qty.
Qty.
Qty.
Balance
Stock
Expiry
Price
ARJUNA TABLETS 60S
5.00
60TAB
11
0
11
11
0
0
0
0.00
"""

EXPIRY_NEAR_HEADER = """
MEDIKING
Sales & Stock Statement(From 01/08/2026 Upto 31/08/2026)
HIMALAYA ZEAL
PRODUCT NAME         PACKING
Op.Bal.
Receipt
Total
Issue
Expiry
Closing
Near
Qty.
Breakage  Balance
Expiry
CONFIDO TAB
1X60
0
3
3
2
0
1
0
"""


class TestSwilTaxPackDumpNear(unittest.TestCase):
    def test_gate_accepts_apex_rejects_expiry_near(self):
        self.assertTrue(_is_swil_tax_pack_dump_near_text(APEX_HEADER))
        self.assertFalse(_is_swil_stacked_opbal_expiry_near_format(APEX_HEADER))
        self.assertFalse(_is_swil_tax_pack_dump_near_text(EXPIRY_NEAR_HEADER))
        self.assertTrue(_is_swil_stacked_opbal_expiry_near_format(EXPIRY_NEAR_HEADER))

    def test_expiry_near_parser_does_not_claim_apex_text(self):
        self.assertIsNone(
            _parse_swil_stacked_opbal_expiry_near_statement(
                APEX_HEADER, "apex.pdf", "pdf"
            )
        )

    def test_live_apex_pdf_product_names_and_qtys(self):
        if not APEX_PDF.exists():
            self.skipTest("Apex fixture PDF not present")
        result = extract_sales_statement(APEX_PDF.read_bytes(), APEX_PDF.name)
        method = ((result.get("totals") or {}).get("extra") or {}).get(
            "extraction_method"
        )
        self.assertEqual(method, "swil_tax_pack_dump_near")
        self.assertEqual(result.get("stockist_name"), "APEX AGENCIES")
        items = result.get("line_items") or []
        self.assertGreaterEqual(len(items), 70)
        by_name = {i["product_name"]: i for i in items}
        self.assertIn("ARJUNA TABLETS 60S", by_name)
        arjuna = by_name["ARJUNA TABLETS 60S"]
        self.assertEqual(arjuna["packing"], "60TAB")
        self.assertEqual(arjuna["opening_qty"], 11.0)
        self.assertEqual(arjuna["receipts_qty"], 0.0)
        self.assertEqual(arjuna["sales_qty"], 11.0)
        self.assertEqual(arjuna["closing_qty"], 0.0)
        cystone = by_name["CYSTONE TABLETS 60S"]
        self.assertEqual(cystone["opening_qty"], 98.0)
        self.assertEqual(cystone["sales_qty"], 5.0)
        self.assertEqual(cystone["closing_qty"], 93.0)
        # Must not treat packing tokens as product names.
        pack_names = {
            i["product_name"]
            for i in items
            if i["product_name"]
            in {"60TAB", "60 TAB", "30TAB", "100GMS", "10CAP", "30 CAP"}
        }
        self.assertEqual(pack_names, set())
        self.assertFalse(
            any("SwilERP" in (i.get("product_name") or "") for i in items)
        )


if __name__ == "__main__":
    unittest.main()
