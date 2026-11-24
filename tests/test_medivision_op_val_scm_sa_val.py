"""MediVision Op/Op val/Purc/Scm/Purc val/Sale/Scm/Sa val/Cl qty/Cl val.

NM90D-only sheets that also print Op val + Scm must not use the compact
Op/Purc/Sale/Sa val map (that parks Op val in Purc and Sale in Cl val).
"""

from __future__ import annotations

import unittest

from services.sales_statement_extractor import (
    _is_medivision_op_purc_nm60d_text,
    _parse_medivision_op_purc_nm60d_text,
    _text_has_medivision_op_val_scm_sa_val,
    _text_has_medivision_nm90d_only,
)

_SAMPLE = """
DVS ENTERPRISES LLP
RENUKA COMPLEX NEAR CH.SHIVAJI STATUE JAFRABAD ROAD BHOKARDAN, DIST - JALNA. 431114
Stock and Sales Statement
Company: HIMALAYA (OTX)
1/10/2026 to 31/10/2026
Page No. 1
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
ASHVAGANDHA TAB
60'S
0
0.00
0
0
0.00
0
0
0.00
0
0.00
0
0.00
GASEX  TAB
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
GASEX SYP
200ML
29
2938.00
0
0
0.00
3
0
343.00
26
2634.00
0
0.00
HIMCOCID SF SUSP
200ML
34
3193.00
0
0
0.00
4
0
442.00
30
2801.00
0
0.00
KOFLET SYP
100ML
55
4129.00
0
0
0.00
15
0
1337.00
40
3003.00
0
0.00
Continued...

DVS ENTERPRISES LLP
Page No. 2
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
RUMALAYA GEL
30GM
48
4085.00
0
0
0.00
2
0
213.00
46
3915.00
0
0.00
RUMALAYA LINIMENT
60ML
76
8340.00
0
0
0.00
33
0
4224.00
43
4647.00
0
0.00
TENTEX ROYAL CAP
10'S
46
8740.00
0
0
0.00
12
0
2569.00
34
6459.00
0
0.00
Totals
53546.00
0.00
15563.00
39875.00
0.00
"""


class MedivisionOpValScmSaValTests(unittest.TestCase):
    def test_detects_wide_op_val_scm_layout(self):
        self.assertTrue(_text_has_medivision_op_val_scm_sa_val(_SAMPLE))
        self.assertTrue(_text_has_medivision_nm90d_only(_SAMPLE))
        self.assertTrue(_is_medivision_op_purc_nm60d_text(_SAMPLE))

    def test_maps_opening_sales_closing_not_compact_slide(self):
        result = _parse_medivision_op_purc_nm60d_text(_SAMPLE, "DVS.pdf")
        self.assertIsNotNone(result)
        assert result is not None
        method = (result.get("totals") or {}).get("extra", {}).get("extraction_method")
        self.assertEqual(method, "medivision_op_val_scm_sa_val")
        by_name = {
            (it.get("product_name") or "").strip().upper(): it
            for it in (result.get("line_items") or [])
        }
        gasex = by_name["GASEX TAB"]
        self.assertEqual(float(gasex["opening_qty"]), 76.0)
        self.assertEqual(float(gasex.get("opening_value") or 0), 10575.0)
        self.assertEqual(float(gasex["receipts_qty"]), 0.0)
        self.assertEqual(float(gasex["sales_qty"]), 33.0)
        self.assertEqual(float(gasex["sales_value"]), 5180.0)
        self.assertEqual(float(gasex["closing_qty"]), 43.0)
        self.assertEqual(float(gasex["closing_value"]), 5983.0)

        him = by_name["HIMCOCID SF SUSP"]
        self.assertEqual(float(him["opening_qty"]), 34.0)
        self.assertEqual(float(him["sales_qty"]), 4.0)
        self.assertEqual(float(him["closing_qty"]), 30.0)
        self.assertEqual(float(him["closing_value"]), 2801.0)

        tentex = by_name["TENTEX ROYAL CAP"]
        self.assertEqual(float(tentex["opening_qty"]), 46.0)
        self.assertEqual(float(tentex["sales_qty"]), 12.0)
        self.assertEqual(float(tentex["sales_value"]), 2569.0)
        self.assertEqual(float(tentex["closing_qty"]), 34.0)
        self.assertEqual(float(tentex["closing_value"]), 6459.0)

    def test_compact_sample_without_op_val_still_compact(self):
        compact = """
Stock and Sales Statement
Company: HIMALAYA
1/1/2026 to 31/1/2026
Page No. 1
Product Name
Unit
Op
Purc
Sale
Sa val
Cl qty
Cl val
NM90D
qty
NM val
ALPHA TAB
60'S
10
5
3
100.00
12
200.00
0
0.00
BETA SYP
100ML
20
0
5
50.00
15
150.00
0
0.00
GAMMA CAP
30'S
8
2
1
20.00
9
40.00
0
0.00
"""
        self.assertFalse(_text_has_medivision_op_val_scm_sa_val(compact))
        result = _parse_medivision_op_purc_nm60d_text(compact, "compact.pdf")
        self.assertIsNotNone(result)
        assert result is not None
        method = (result.get("totals") or {}).get("extra", {}).get("extraction_method")
        self.assertEqual(method, "medivision_op_purc_nm90d_compact")
        alpha = next(
            it
            for it in result["line_items"]
            if "ALPHA" in (it.get("product_name") or "").upper()
        )
        self.assertEqual(float(alpha["opening_qty"]), 10.0)
        self.assertEqual(float(alpha["receipts_qty"]), 5.0)
        self.assertEqual(float(alpha["sales_qty"]), 3.0)
        self.assertEqual(float(alpha["sales_value"]), 100.0)
        self.assertEqual(float(alpha["closing_qty"]), 12.0)


if __name__ == "__main__":
    unittest.main()
