"""ITEM / OPENING / SALE RET. / AMOUNT-I / CL.STOCK table (unlabeled pack)."""

from __future__ import annotations

import unittest

from services.sales_statement_extractor import (
    _looks_like_packing_token,
    _parse_semantic_text_table,
    _structure_sales_text,
    _xls_header_colmap,
)


HEADER = (
    "ITEM                        OPENING   PURCHASE  SALE RET.     B/E/PR       "
    "SALE     AMOUNT-I    CL.STOCK     AMOUNT-I  NEAR EXP.   N.E.VALUE % SALE"
)

SAMPLE = f"""GUPTA MEDICAL AGENCY
BAKSHI GALI,TOHANA DISTT.FATEHABAD
COMPANY : HIMALAYA ZANDRA
STOCK REGISTER FROM 01/09/2026 TO 26/09/2026
{HEADER}
ARJUNA TAB                     60'S        0         0         2         2         0      0.00         0      0.00      0.00
BONNISAN DROPS                 30ML       13         0         0         0         0      0.00        13    825.61      0.00
BRESOL TAB                    60TAB       26         0         5         5         5    731.35        21   3071.67     19.23
CYSTONE TAB                   60TAB      101         0         0         0        17   2790.21        84  13627.10      2      225.50     16.83
EVECARE CAPS                    HIM       75         0         2         0        11   1805.43        66  10681.08      2      176.76     14.67
HIORA PAIN GEL               6*10GM        0         0         0         0         0      0.00         0      0.00      0.00
LIV-52 DROPS                   60ML       26         0         1         1        24   2350.56         2    177.17     92.31
MENTAT SYP                    200ML       59         0         0         0        47   8158.73        12   2083.08     79.66
1396        0        22        16       404   67001.78       998  160895.76   1696.43
"""


class TestItemOpeningSaleRetAmount(unittest.TestCase):
    def test_packing_star_unit(self):
        self.assertTrue(_looks_like_packing_token("6*10GM"))
        self.assertTrue(_looks_like_packing_token("60'S"))

    def test_header_maps_sale_ret_and_both_amounts(self):
        cells = [
            "ITEM",
            "OPENING",
            "PURCHASE",
            "SALE RET.",
            "B/E/PR",
            "SALE",
            "AMOUNT-I",
            "CL.STOCK",
            "AMOUNT-I",
            "NEAR EXP.",
            "N.E.VALUE % SALE",
        ]
        colmap = _xls_header_colmap(cells)
        self.assertEqual(colmap["item"], 0)
        self.assertEqual(colmap["op"], 1)
        self.assertEqual(colmap["pur"], 2)
        self.assertEqual(colmap["sret"], 3)
        self.assertEqual(colmap["bepr"], 4)
        self.assertEqual(colmap["sale"], 5)
        self.assertEqual(colmap["sval"], 6)
        self.assertEqual(colmap["bal"], 7)
        self.assertEqual(colmap["bval"], 8)

    def test_semantic_table_peels_pack_and_keeps_printed_qty_value(self):
        result = _parse_semantic_text_table(SAMPLE, "state gupta.pdf", "pdf")
        self.assertIsNotNone(result)
        by_name = {item["product_name"]: item for item in result["line_items"]}

        bonni = by_name["BONNISAN DROPS"]
        self.assertEqual(bonni["packing"], "30ML")
        self.assertEqual(bonni["opening_qty"], 13.0)
        self.assertEqual(bonni["receipts_qty"], 0.0)
        self.assertEqual(bonni["sales_qty"], 0.0)
        self.assertEqual(bonni["closing_qty"], 13.0)
        self.assertEqual(bonni["closing_value"], 825.61)

        bresol = by_name["BRESOL TAB"]
        self.assertEqual(bresol["packing"], "60TAB")
        self.assertEqual(bresol["opening_qty"], 26.0)
        self.assertEqual(bresol["sales_qty"], 5.0)
        self.assertEqual(bresol["sales_value"], 731.35)
        self.assertEqual(bresol["closing_qty"], 21.0)
        self.assertEqual(bresol["closing_value"], 3071.67)
        self.assertEqual(bresol["extra"].get("sale_return_qty"), 5.0)
        self.assertEqual(bresol["extra"].get("exp_damage"), 5.0)

        evecare = by_name["EVECARE CAPS"]
        self.assertEqual(evecare["packing"], "HIM")
        self.assertEqual(evecare["opening_qty"], 75.0)
        self.assertEqual(evecare["sales_qty"], 11.0)
        self.assertEqual(evecare["closing_qty"], 66.0)
        self.assertEqual(evecare["sales_value"], 1805.43)
        self.assertEqual(evecare["closing_value"], 10681.08)

        hiora = by_name["HIORA PAIN GEL"]
        self.assertEqual(hiora["packing"], "6*10GM")
        self.assertEqual(hiora["opening_qty"], 0.0)
        self.assertEqual(hiora["closing_qty"], 0.0)

    def test_structure_prefers_semantic_over_total_row_heuristic(self):
        result = _structure_sales_text(SAMPLE, "state gupta.pdf", "pdf")
        extra = (result.get("totals") or {}).get("extra") or {}
        self.assertEqual(extra.get("extraction_method"), "semantic_text_table")
        self.assertGreaterEqual(len(result["line_items"]), 8)
        liv = next(
            item
            for item in result["line_items"]
            if item["product_name"] == "LIV-52 DROPS"
            and item.get("packing") == "60ML"
        )
        self.assertEqual(liv["opening_qty"], 26.0)
        self.assertEqual(liv["sales_qty"], 24.0)
        self.assertEqual(liv["closing_qty"], 2.0)
        self.assertEqual(liv["sales_value"], 2350.56)
        self.assertEqual(liv["closing_value"], 177.17)


if __name__ == "__main__":
    unittest.main()
