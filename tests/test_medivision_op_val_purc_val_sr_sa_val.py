"""MediVision Op/Op val/Purc/Purc val/S.R./Sale/WSale/Sa val/P.R./Cl qty.

Blank Purc/WSale cells stay aligned; Sa val must not become closing_qty.
Distinct from DVS Scm stacks and from Op-val/S.R./Aug/Jul/Cl-val/NM sheets.
"""

from __future__ import annotations

import unittest

from services.sales_statement_extractor import (
    _is_medivision_op_purc_nm60d_text,
    _is_medivision_op_wsale_header,
    _parse_medivision_op_purc_nm60d_text,
    _text_has_medivision_op_val_purc_val_sr_sa_val,
    _text_has_medivision_op_val_scm_sa_val,
)

_SAMPLE = """
PRIYA AGENCIES
BEHIND VAIDYA HOSPITAL, JALNA ROAD, BEED
Stock and Sales Statement
Company: HIMALAYA ZANDRA
1/10/2026 to 31/10/2026
Page No. 1
Product Name
Unit
Op
Op val
Purc
Purc val
S.R. qty
S.R. val
Sale
WSale
Sa val
P.R. qty
P.R. val
Cl qty
ARJUNA TAB
60S
0
0.00
0
0.00
0
0.00
0
0
0.00
0
0.00
0
BONNISAN DROPS
30ml
87
5510.00
0
0.00
0
0.00
13
0
971.00
0
0.00
74
BRESOL NS NASEL SOLU.
10ML
11
529.00
50
2406.00
0
0.00
13
0
743.00
0
0.00
48
BRESOL SY.200ML
200ML
580
78073.00
0
0.00
0
0.00
84
0
13104.00
0
0.00
496
CYSTONE TAB
60T
299
44306.00
0
0.00
0
0.00
196
0
36287.00
0
0.00
103
HIMPLASIA TAB.
2*30S
43
6606.00
0
0.00
0
0.00
18
0
6720.00
0
0.00
25
LIV 52 SUGAR FREE SYP 200ML
1
0
0.00
0
0.00
0
0.00
0
0
0.00
0
0.00
0
Continued...

PRIYA AGENCIES
Page No. 2
Stock and Sales Statement
Product Name
Unit
Op
Op val
Purc
Purc val
S.R. qty
S.R. val
Sale
WSale
Sa val
P.R. qty
P.R. val
Cl qty
STYPLON TAB
30T
18
-2218.00
50
5674.00
0
0.00
40
0
5120.00
0
0.00
28
V-GEL
30gm
11
1011.00
0
0.00
0
0.00
6
0
663.00
0
0.00
5
Totals
231262.00
447414.00
21316.00
0.00
0.00
""".strip()

_DVS_SCM = """
DVS ENTERPRISES LLP
Stock and Sales Statement
Product Name
Unit
Op
Op val
Purc
Scm
Purc val
Sale
Scm
Sa val
Cl qty
Cl val
NM90D
qty
NM val
GASEX TAB
100'S
76
10575.00
0
0
0.00
33
0
5180.00
43
5983.00
0
0.00
""".strip()


class TestMedivisionOpValPurcValSrSaVal(unittest.TestCase):
    def test_detector_matches_purc_val_sr_sa_val(self):
        self.assertTrue(_text_has_medivision_op_val_purc_val_sr_sa_val(_SAMPLE))
        self.assertTrue(_is_medivision_op_purc_nm60d_text(_SAMPLE))
        self.assertFalse(_text_has_medivision_op_val_scm_sa_val(_SAMPLE))
        self.assertFalse(_is_medivision_op_wsale_header(_SAMPLE))

    def test_detector_rejects_scm_wide(self):
        self.assertFalse(_text_has_medivision_op_val_purc_val_sr_sa_val(_DVS_SCM))

    def test_sale_and_sa_val_not_slid(self):
        result = _parse_medivision_op_purc_nm60d_text(
            _SAMPLE, "priya oct 2026.pdf", "pdf"
        )
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(
            result["totals"]["extra"]["extraction_method"],
            "medivision_op_val_purc_val_sr_sa_val",
        )
        by = {it["product_name"]: it for it in result["line_items"]}

        drops = by["BONNISAN DROPS"]
        self.assertEqual(drops["opening_qty"], 87.0)
        self.assertEqual(drops["opening_value"], 5510.0)
        self.assertEqual(drops["receipts_qty"], 0.0)
        self.assertEqual(drops["sales_qty"], 13.0)
        self.assertEqual(drops["sales_value"], 971.0)
        self.assertEqual(drops["closing_qty"], 74.0)
        self.assertIsNone(drops["closing_value"])
        self.assertEqual(drops["extra"]["field_source"]["closing_value"], "missing")

        ns = by["BRESOL NS NASEL SOLU."]
        self.assertEqual(ns["receipts_qty"], 50.0)
        self.assertEqual(ns["extra"]["purchase_value"], 2406.0)
        self.assertEqual(ns["sales_qty"], 13.0)
        self.assertEqual(ns["sales_value"], 743.0)
        self.assertEqual(ns["closing_qty"], 48.0)

        # Digit-only unit must not become opening_qty.
        sugar = by["LIV 52 SUGAR FREE SYP 200ML"]
        self.assertEqual(sugar["packing"], "1")
        self.assertEqual(sugar["opening_qty"], 0.0)
        self.assertEqual(sugar["closing_qty"], 0.0)

        himp = by["HIMPLASIA TAB."]
        self.assertEqual(himp["opening_qty"], 43.0)
        self.assertEqual(himp["sales_qty"], 18.0)
        self.assertEqual(himp["sales_value"], 6720.0)
        self.assertEqual(himp["closing_qty"], 25.0)

        expected_sv = round(
            sum(
                float(it["sales_value"])
                for it in result["line_items"]
                if it.get("sales_value") is not None
            ),
            2,
        )
        self.assertAlmostEqual(result["totals"]["sales_value"], expected_sv, places=1)


if __name__ == "__main__":
    unittest.main()
