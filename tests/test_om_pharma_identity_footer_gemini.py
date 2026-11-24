"""Regression: OM PHARMA identity, footer filter, Gemini column alignment."""

from __future__ import annotations

import unittest

from services.html_table.html_table_parser import parse_html_table
from services.ocr.gemini_fallback import GeminiOCRFallback, validate_pipe_table_alignment
from services.vl_html_table import parse_vl_html_table, resolve_vl_or_text_extraction


OM_PHARMA_OCR = """OM PHARMA
Stock & Sales from 01/10/2026 to 31/10/2026 for : HIMALAYA WELLNESS COMPANY (DIV.: HIM-OTX-DIVI)
Page : 1
<table><tr><td>Product Name</td><td>Pack</td><td>LstSL</td><td>Open</td><td>Recd.</td><td>Sales</td><td>Close</td><td>Order</td><td>Pend</td></tr>
<tr><td>GASEX TABLETS</td><td>100TAB</td><td>70</td><td>30</td><td>100</td><td>26</td><td>104</td><td>-104</td><td>-</td></tr>
<tr><td>HIMCOCID-SF(MINT FLAVOUR)</td><td>200 ML</td><td>2</td><td>5</td><td>-</td><td>4</td><td>1</td><td>-1</td><td>-</td></tr>
<tr><td>Last Month Sales</td><td>25,091.69</td><td colspan="2">Open. Value</td><td colspan="5">16,574.03</td></tr>
<tr><td>Receipt Value</td><td>35,609.11</td><td colspan="2">Sales Value</td><td colspan="5">11,917.17</td></tr>
<tr><td>Closing Value</td><td>35,975.98</td><td colspan="7">[Order Value +0.00 -34262.84 ]</td></tr>
</table>
"""


class FooterFilterTests(unittest.TestCase):
    def test_summary_labels_not_line_items(self):
        parsed = parse_html_table(OM_PHARMA_OCR, allow_llm=False)
        names = [i["product_name"] for i in parsed["line_items"]]
        self.assertEqual(names, ["GASEX TABLETS", "HIMCOCID-SF(MINT FLAVOUR)"])
        totals = parsed.get("totals") or {}
        summaries = (totals.get("extra") or {}).get("summary_values") or {}
        self.assertTrue(summaries)
        self.assertGreaterEqual((totals.get("extra") or {}).get("summary_rows_filtered", 0), 1)

    def test_vl_parse_keeps_summary_out_of_products(self):
        result = parse_vl_html_table(OM_PHARMA_OCR, filename="om.jpg", allow_llm=False)
        names = [i["product_name"] for i in result["line_items"]]
        self.assertNotIn("Last Month Sales", names)
        self.assertNotIn("Receipt Value", names)
        self.assertNotIn("Closing Value", names)
        self.assertIn("GASEX TABLETS", names)


class IdentityFromVlTextTests(unittest.TestCase):
    def test_resolve_keeps_stockist_company_period(self):
        result = resolve_vl_or_text_extraction(
            OM_PHARMA_OCR, filename="om.jpg", source_format="jpg"
        )
        self.assertIsNotNone(result)
        self.assertEqual(result.get("stockist_name"), "OM PHARMA")
        self.assertIn("HIMALAYA", str(result.get("company_name") or "").upper())
        self.assertTrue(result.get("period_from"))
        self.assertTrue(result.get("period_to"))


class GeminiAlignmentTests(unittest.TestCase):
    def test_pipe_rows_flag_column_shift(self):
        bad = (
            "Product Name|Pack|LstSL|Open|Recd.|Sales|Close\n"
            "MINT|200 ML|2|5|4|1\n"  # missing Recd dash → 6 cols vs 7
        )
        meta = validate_pipe_table_alignment(bad)
        self.assertTrue(meta["pipe_table_detected"])
        self.assertFalse(meta["column_alignment_ok"])
        self.assertTrue(meta["ambiguous_rows"])

    def test_pipe_rows_ok_with_dashes(self):
        good = (
            "Product Name|Pack|LstSL|Open|Recd.|Sales|Close\n"
            "MINT|200 ML|2|5|-|4|1\n"
            "SAUNF|200 ML|3|6|-|6|0\n"
        )
        meta = validate_pipe_table_alignment(good)
        self.assertTrue(meta["column_alignment_ok"])
        self.assertEqual(meta["checked_rows"], 2)

    def test_recognize_marks_ambiguous_metadata(self):
        shifted = (
            "OM PHARMA\n"
            "Product Name|Pack|Open|Recd.|Sales|Close\n"
            "MINT|200 ML|5|4|1\n"
        )
        eng = GeminiOCRFallback(invoke=lambda _b, _t: shifted, timeout_s=5)
        result = eng.recognize(b"\xff\xd8\xff", page=1)
        self.assertTrue(result.metadata.get("column_alignment_ambiguous"))
        self.assertIn("MINT", result.text)


class ClassicPaddleTorchOrderTests(unittest.TestCase):
    def test_preload_helper_importable(self):
        from services.ocr.paddle_engine import preload_torch_before_paddle

        # May return version or None if torch absent; must not raise.
        preload_torch_before_paddle()


if __name__ == "__main__":
    unittest.main()
