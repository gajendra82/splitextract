"""PDF Stock Report: Item Name|Packg|Open.Stk|L.Sales|Cur.Sls layout family."""

from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest import mock

from services.sales_statement_extractor import (
    _parse_text_stock_fallback,
    extract_sales_statement,
)
from services.stock_header_resolver import resolve_columns


LIVE = Path(r"C:\Users\Prerana Bhalerao\Downloads\Bafna_Oct_26_edited.pdf")

SAMPLE_TEXT = """BAFNA MEDICO
Stock Report from 01/10/26 to 31/10/26 (HIMALAYA(ZANDRA))
Date: 31/10/26
Item Name Packg Open.Stk. Receipt L.Sales Cur.Sls Pur.Rtn Sls.Rtn Clos.Stk. Above 60 Above 90
BONNISAN 200ML. 39 0 0 6 0 0 33 0 33
BONNISAN DROPS 30ML. 77 0 4 15 0 0 62 0 62
BRESOL NS SALINE 10ML 106 0 1 36 0 0 70 0 70
LIV.52 60ML.DROP 60ML. 130 0 20 43 0 3 90 0 90
EVECARE FORTE TAB 10TAB 81 168 87 102 0 0 147 0 0
STYPLON 30TAB. 53 0 0 6 0 0 47 0 1
Grand Total 328215 33508 75429 74224 0 265 287763 10638 171870
"""


def _cells(*texts):
    return [
        {
            "text": t,
            "x_center": None,
            "col_index": i,
            "subheader_text": None,
        }
        for i, t in enumerate(texts)
    ]


class TestLSalesCurSlsHeaderPair(unittest.TestCase):
    def test_l_sales_and_cur_sls_map_lms_and_sales(self):
        result = resolve_columns(
            _cells(
                "Item Name",
                "Packg",
                "Open.Stk.",
                "Receipt",
                "L.Sales",
                "Cur.Sls",
                "Pur.Rtn",
                "Sls.Rtn",
                "Clos.Stk.",
                "Above 60",
                "Above 90",
            )
        )
        canons = [c["canonical"] for c in result["columns"]]
        self.assertEqual(
            canons[:9],
            [
                "product_name",
                "pack",
                "opening_qty",
                "purchase_qty",
                "lms",
                "sales_qty",
                "purchase_return_qty",
                "sales_return_qty",
                "closing_qty",
            ],
        )
        self.assertEqual(result["columns"][4]["reason"], "l_sales_cur_sls_pair_lms")
        self.assertEqual(result["columns"][5]["reason"], "l_sales_cur_sls_pair_sales")

    def test_kill_switch_keeps_l_sales_as_sales_without_pair(self):
        with mock.patch.dict(os.environ, {"STOCK_HEADER_L_SALES_CUR_SLS": "false"}):
            result = resolve_columns(
                _cells(
                    "Item Name",
                    "Packg",
                    "Open.Stk.",
                    "Receipt",
                    "L.Sales",
                    "Cur.Sls",
                    "Clos.Stk.",
                )
            )
        canons = [c["canonical"] for c in result["columns"]]
        # Without the pair rule Cur.Sls still aliases to sales; L.Sales also
        # hits sales → duplicate demotes one. Pair OFF must not force lms.
        self.assertNotIn("lms", canons)


class TestPdfItemPackgOpenStkLsalesFamily(unittest.TestCase):
    def test_text_family_maps_cur_sls_not_l_sales(self):
        parsed = _parse_text_stock_fallback(SAMPLE_TEXT, "stock_report.pdf")
        self.assertIsNotNone(parsed)
        items = {item["product_name"]: item for item in parsed["line_items"]}
        drops = items["BONNISAN DROPS"]
        self.assertEqual(drops["sales_qty"], 15.0)
        self.assertEqual((drops.get("extra") or {}).get("last_sales_qty"), 4.0)
        self.assertEqual(items["BRESOL NS SALINE"]["sales_qty"], 36.0)
        self.assertEqual(items["EVECARE FORTE TAB"]["sales_qty"], 102.0)
        liv = items["LIV.52 60ML.DROP"]
        self.assertEqual(liv["sales_qty"], 43.0)
        self.assertEqual(liv.get("sales_return_qty"), 3.0)
        self.assertEqual(liv.get("sale_return"), 3.0)
        self.assertEqual(parsed["company_name"], "HIMALAYA(ZANDRA)")
        self.assertEqual(parsed["stockist_name"], "BAFNA MEDICO")
        extra = (parsed.get("totals") or {}).get("extra") or {}
        self.assertEqual(extra.get("txt_family"), "item_packg_open_stk_lsales")
        self.assertEqual(parsed["totals"].get("sales_value"), 74224.0)

    def test_pdf_flag_kill_switch(self):
        with mock.patch.dict(os.environ, {"STOCK_PDF_ITEM_PACKG_OPEN_STK_LSALES": "false"}):
            from services.sales_statement_extractor import (
                _pdf_item_packg_open_stk_lsales_enabled,
            )

            self.assertFalse(_pdf_item_packg_open_stk_lsales_enabled())


@unittest.skipUnless(LIVE.is_file(), "missing live Bafna Stock Report PDF")
class TestPdfItemPackgOpenStkLsalesLive(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = extract_sales_statement(LIVE.read_bytes(), LIVE.name)
        cls.items = cls.result.get("line_items") or []
        cls.by_name = {item["product_name"]: item for item in cls.items}

    def test_cur_sls_sales_and_company(self):
        extra = (self.result.get("totals") or {}).get("extra") or {}
        method = extra.get("extraction_method")
        self.assertIn(
            method,
            {
                "pdf_text_item_packg_open_stk_lsales",
                "pdf_words_free_columns",
            },
        )
        if method == "pdf_text_item_packg_open_stk_lsales":
            self.assertEqual(extra.get("pdf_family"), "item_packg_open_stk_lsales")
        self.assertEqual(len(self.items), 35)
        self.assertEqual(self.result.get("company_name"), "HIMALAYA(ZANDRA)")
        self.assertEqual(self.result.get("stockist_name"), "BAFNA MEDICO")
        drops = self.by_name["BONNISAN DROPS"]
        self.assertEqual(drops["sales_qty"], 15.0)
        lms = (drops.get("extra") or {}).get("last_sales_qty")
        if lms is None:
            lms = (drops.get("extra") or {}).get("lms") or (
                drops.get("extra") or {}
            ).get("lms_qty")
        self.assertEqual(lms, 4.0)
        self.assertEqual(self.by_name["BRESOL NS SALINE"]["sales_qty"], 36.0)
        self.assertEqual(self.by_name["EVECARE FORTE TAB"]["sales_qty"], 102.0)
        self.assertEqual(self.by_name["BONNISAN"]["sales_qty"], 6.0)
        self.assertEqual(extra.get("stock_reconciliation_fail_count"), 0)
        self.assertEqual(extra.get("stock_reconciliation_pass_count"), 35)


if __name__ == "__main__":
    unittest.main()
