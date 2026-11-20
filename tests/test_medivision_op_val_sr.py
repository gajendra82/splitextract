"""MediVision Op / Op val / Purc / S.R. qty / Sale column map.

Sheets that print S.R. qty beside Sale must not treat sales return as sales_qty,
and Op val must not become receipts_qty.
"""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from services.sales_statement_extractor import (
    _is_medivision_op_purc_nm60d_text,
    _parse_medivision_op_purc_nm60d_text,
    _text_has_medivision_op_val_sr,
)

_SAMPLE = """
GAYATRI MEDICAL DISTRIBUTORS PVT LTD
1ST FLOOR SHOP, KADAM PLAZA, PUNE
Stock and Sales Statement
Company: HIMALAYA - ZEAL (DIRECT)
1/9/2026 to 30/9/2026
Page No. 1
Product Name
Unit
Op
Op val
Purc S.R. qty
Sale
WSale
Aug sale
Jul sale
Cl qty
Cl val
NM60D
qty
NM90D
qty
NM val
NE
AACTARIL SOAP ###
75GM
93
6665.00
0
1
4
0
3
3
90
6432.00
0
0
0.00
0
ABANA ###
60TAB
17
2526.00
40
0
22
0
123
16
35
4990.00
0
0
0.00
0
DIABECON DS ###
60 TAB
33
6347.00
0
1
14
0
14
7
20
3858.00
0
0
0.00
0
LIV 52 SYR 100ML ###
100 ML
36
3404.00
138
12
71
0
65
396
115
10880.00
0
0
0.00
0
""".strip()

_CLASSIC_SCM = """
SUPEKAR AGENCIES
Stock and Sales Statement
Company: HIMALAYA
1/9/2026 to 30/9/2026
Product Name
Unit
Comp
Op
Purc
Scm
Sale
Scm
Jul sale
Jun sale
Sa val
Cl qty
Cl val
NM60D
qty
NM90D
qty
NM val
NE
AACTARIL SOAP
75GM
HIMAL
7
0
0
0
0
0
20
0.00
7
486.00
0
0
0.00
0
ABANA TAB
60S
HIMAL
28
0
0
15
0
8
5
2514.00
13
1932.00
0
0
0.00
0
LIV 52 SYP
100ML
HIMAL
6
70
0
75
0
41
20
7999.00
1
95.00
0
0
0.00
0
""".strip()


class TestMedivisionOpValSr(unittest.TestCase):
    def test_detector_op_val_sr(self):
        self.assertTrue(_text_has_medivision_op_val_sr(_SAMPLE))
        self.assertTrue(_is_medivision_op_purc_nm60d_text(_SAMPLE))
        self.assertFalse(_text_has_medivision_op_val_sr(_CLASSIC_SCM))

    @patch.dict(os.environ, {"STOCK_MEDIVISION_OP_VAL_SR": "true"}, clear=False)
    def test_sale_not_sr_and_purc_not_op_val(self):
        result = _parse_medivision_op_purc_nm60d_text(
            _SAMPLE, "StockAndSalesReport.pdf", "pdf"
        )
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(
            result["totals"]["extra"]["extraction_method"],
            "medivision_op_val_sr_nm60d",
        )
        by = {it["product_name"]: it for it in result["line_items"]}
        aact = by["AACTARIL SOAP ###"]
        self.assertEqual(aact["opening_qty"], 93.0)
        self.assertEqual(aact["receipts_qty"], 0.0)
        self.assertEqual(aact["sales_qty"], 4.0)
        self.assertIsNone(aact["sales_value"])
        self.assertEqual(aact["closing_qty"], 90.0)
        self.assertEqual(aact["closing_value"], 6432.0)
        self.assertEqual(aact["extra"]["sales_return_qty"], 1.0)
        self.assertEqual(aact["extra"]["opening_value"], 6665.0)
        self.assertEqual(aact["extra"]["field_source"]["sales_value"], "missing")

        liv = by["LIV 52 SYR 100ML ###"]
        self.assertEqual(liv["receipts_qty"], 138.0)
        self.assertEqual(liv["sales_qty"], 71.0)
        self.assertEqual(liv["extra"]["sales_return_qty"], 12.0)
        self.assertEqual(liv["closing_qty"], 115.0)

    @patch.dict(os.environ, {"STOCK_MEDIVISION_OP_VAL_SR": "false"}, clear=False)
    def test_flag_off_keeps_legacy_indices(self):
        result = _parse_medivision_op_purc_nm60d_text(
            _SAMPLE, "StockAndSalesReport.pdf", "pdf"
        )
        self.assertIsNotNone(result)
        assert result is not None
        aact = result["line_items"][0]
        # Legacy map: nums[1]=Op val → receipts, nums[3]=S.R. → sales.
        self.assertEqual(aact["receipts_qty"], 6665.0)
        self.assertEqual(aact["sales_qty"], 1.0)

    @patch.dict(os.environ, {"STOCK_MEDIVISION_OP_VAL_SR": "true"}, clear=False)
    def test_classic_scm_layout_unchanged(self):
        result = _parse_medivision_op_purc_nm60d_text(
            _CLASSIC_SCM, "supekar.pdf", "pdf"
        )
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(
            result["totals"]["extra"]["extraction_method"],
            "medivision_op_purc_nm60d",
        )
        abana = next(
            it for it in result["line_items"] if "ABANA" in it["product_name"]
        )
        self.assertEqual(abana["opening_qty"], 28.0)
        self.assertEqual(abana["receipts_qty"], 0.0)
        self.assertEqual(abana["sales_qty"], 15.0)
        self.assertEqual(abana["sales_value"], 2514.0)
        self.assertEqual(abana["closing_qty"], 13.0)

    @unittest.skipUnless(
        os.path.isfile(
            os.path.join(
                os.path.dirname(__file__),
                "..",
                "StockAndSalesReport.pdf",
            )
        ),
        "fixture PDF not present",
    )
    @patch.dict(os.environ, {"STOCK_MEDIVISION_OP_VAL_SR": "true"}, clear=False)
    def test_live_pdf(self):
        import fitz

        from services.sales_statement_extractor import (
            _parse_medivision_op_purc_nm60d_from_doc,
        )

        path = os.path.join(
            os.path.dirname(__file__), "..", "StockAndSalesReport.pdf"
        )
        doc = fitz.open(path)
        try:
            result = _parse_medivision_op_purc_nm60d_from_doc(
                doc, "StockAndSalesReport.pdf"
            )
        finally:
            doc.close()
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(
            result["totals"]["extra"]["extraction_method"],
            "medivision_op_val_sr_nm60d",
        )
        self.assertGreaterEqual(len(result["line_items"]), 40)
        aact = next(
            it
            for it in result["line_items"]
            if "AACTARIL" in (it.get("product_name") or "")
        )
        self.assertEqual(aact["sales_qty"], 4.0)
        self.assertEqual(aact["receipts_qty"], 0.0)
        self.assertEqual(aact["extra"]["sales_return_qty"], 1.0)
        self.assertIsNone(aact["sales_value"])


if __name__ == "__main__":
    unittest.main()
