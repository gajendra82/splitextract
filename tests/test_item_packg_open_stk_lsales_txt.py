"""Item Name|Packg|Open.Stk|Receipt|L.Sales|Cur.Sls|Clos.Stk Stock Report TXT."""

from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest import mock

from services.sales_statement_extractor import (
    _match_text_stock_family,
    _parse_text_stock_fallback,
    _strip_printer_controls,
    extract_sales_statement,
)


SAMPLE = """\x12\x0eBAFNA MEDICO, SANGAMNER
\x1bEStock Report from 01/09/26 to 30/09/26(HIMALAYA(ZANDRA))\x1bF
Date : 01/10/26                                                       Page: 1
\x1bM-----------------------------------------------------------------------------------------------------------
Item Name                Packg Open.Stk. Receipt L.Sales Cur.SlsPur.RtnSls.RtnClos.Stk.Above 60 Above 90
-----------------------------------------------------------------------------------------------------------
BONNISAN                 200ML.       39       0       1       0      0      0      39        0       39
BONNISAN DROPS           30ML.        81       0       5       4      0      0      77       27        0
CYSTONE SYP.200ML        200ML.       36      56      11      12      0      0      80        0       24
LIV.52 DS TAB.           60TAB.      100     100      28      47      0      0     153        0        2
STYPLON                  30TAB.        3      50       5       0      0      0      53        2        1
===========================================================================================================
Grand Total                       317589   85865   65705   75429      0    190   328215    27783   118410
"""

OTHER = """
NARAYAN MEDICAL (NEW)
** Daily Stock Summary (Company-wise) **
Product & Pack | Op. Stock | Pur. Qty | Sales Qty | Cl. Stock
AACTARIL SOAP,75GM 62 0 10 52
"""

LIVE = Path(r"C:\Users\Prerana Bhalerao\Downloads\Bafna sep 26 (1).txt")


class TestItemPackgOpenStkLsalesFamily(unittest.TestCase):
    def test_detects_open_stk_lsales_not_daily_summary(self):
        cleaned = _strip_printer_controls(SAMPLE)
        matched = _match_text_stock_family(cleaned)
        self.assertIsNotNone(matched)
        self.assertEqual(matched[0], "item_packg_open_stk_lsales")
        self.assertIsNone(_match_text_stock_family(OTHER))

    def test_kill_switch_disables_family(self):
        cleaned = _strip_printer_controls(SAMPLE)
        with mock.patch.dict(
            os.environ,
            {
                "STOCK_TXT_ITEM_PACKG_OPEN_STK_LSALES": "false",
                "STOCK_PDF_ITEM_PACKG_OPEN_STK_LSALES": "false",
            },
        ):
            self.assertIsNone(_match_text_stock_family(cleaned))

    def test_maps_cur_sls_not_l_sales_and_value_footer(self):
        parsed = _parse_text_stock_fallback(SAMPLE, "stock_report.txt")
        self.assertIsNotNone(parsed)
        items = {item["product_name"]: item for item in parsed["line_items"]}
        self.assertEqual(len(items), 5)
        bonn = items["BONNISAN"]
        self.assertEqual(bonn["packing"], "200ML.")
        self.assertEqual(
            (bonn["opening_qty"], bonn["receipts_qty"], bonn["sales_qty"], bonn["closing_qty"]),
            (39.0, 0.0, 0.0, 39.0),
        )
        self.assertEqual((bonn.get("extra") or {}).get("last_sales_qty"), 1.0)
        self.assertIsNone(bonn.get("sales_value"))
        self.assertEqual(
            (bonn.get("extra") or {}).get("field_source", {}).get("sales_value"),
            "missing",
        )
        drops = items["BONNISAN DROPS"]
        self.assertEqual(drops["sales_qty"], 4.0)
        self.assertEqual((drops.get("extra") or {}).get("last_sales_qty"), 5.0)
        cystone = items["CYSTONE SYP.200ML"]
        self.assertEqual(
            (cystone["opening_qty"], cystone["receipts_qty"], cystone["sales_qty"], cystone["closing_qty"]),
            (36.0, 56.0, 12.0, 80.0),
        )
        liv = items["LIV.52 DS TAB."]
        self.assertEqual(liv["sales_qty"], 47.0)
        self.assertEqual(liv["closing_qty"], 153.0)
        styplon = items["STYPLON"]
        self.assertEqual(styplon["receipts_qty"], 50.0)
        self.assertEqual(styplon["sales_qty"], 0.0)
        self.assertEqual((styplon.get("extra") or {}).get("last_sales_qty"), 5.0)

        extra = (parsed.get("totals") or {}).get("extra") or {}
        self.assertEqual(extra.get("extraction_method"), "txt_stock_fallback")
        self.assertEqual(extra.get("txt_family"), "item_packg_open_stk_lsales")
        self.assertEqual(extra.get("total_row_source"), "item_packg_open_stk_value_footer")
        self.assertEqual(extra.get("opening_value"), 317589.0)
        self.assertEqual(parsed["totals"].get("sales_value"), 75429.0)
        self.assertEqual(parsed["totals"].get("closing_value"), 328215.0)
        self.assertNotIn("opening_qty", parsed["totals"] or {})
        self.assertEqual(parsed["stockist_name"], "BAFNA MEDICO, SANGAMNER")
        self.assertEqual(parsed["company_name"], "HIMALAYA(ZANDRA)")
        self.assertEqual(parsed["period_from"], "2026-09-01")
        self.assertEqual(parsed["period_to"], "2026-09-30")

    def test_extract_sales_statement_keeps_value_footer(self):
        result = extract_sales_statement(SAMPLE.encode("latin-1"), "stock_report.txt")
        self.assertEqual(len(result.get("line_items") or []), 5)
        extra = (result.get("totals") or {}).get("extra") or {}
        self.assertEqual(extra.get("txt_family"), "item_packg_open_stk_lsales")
        self.assertEqual(extra.get("total_row_source"), "item_packg_open_stk_value_footer")
        self.assertEqual(result["totals"].get("sales_value"), 75429.0)
        self.assertEqual(result["totals"].get("closing_value"), 328215.0)
        self.assertEqual(extra.get("opening_value"), 317589.0)


@unittest.skipUnless(LIVE.is_file(), "missing live Bafna Stock Report TXT")
class TestItemPackgOpenStkLsalesLive(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = extract_sales_statement(LIVE.read_bytes(), LIVE.name)
        cls.items = cls.result.get("line_items") or []
        cls.by_name = {item["product_name"]: item for item in cls.items}

    def test_extracts_all_rows_and_footer_values(self):
        extra = (self.result.get("totals") or {}).get("extra") or {}
        self.assertEqual(extra.get("txt_family"), "item_packg_open_stk_lsales")
        self.assertEqual(len(self.items), 35)
        self.assertEqual(self.result["stockist_name"], "BAFNA MEDICO, SANGAMNER")
        self.assertEqual(self.by_name["BONNISAN"]["opening_qty"], 39.0)
        self.assertEqual(self.by_name["BONNISAN DROPS"]["sales_qty"], 4.0)
        self.assertEqual(self.by_name["LIV.52 DS TAB."]["closing_qty"], 153.0)
        self.assertEqual(self.result["totals"].get("sales_value"), 75429.0)
        self.assertEqual(self.result["totals"].get("closing_value"), 328215.0)


if __name__ == "__main__":
    unittest.main()
