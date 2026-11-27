"""SSA dump soft-skip for PRODUCT|PACK|…|FREE|P.RETURN ANALYSIS chrome."""

from __future__ import annotations

import unittest

from services.sales_statement_extractor import (
    _FREE_RETURN_TOTAL_VALUE_NAME,
    _ssa_dump_result_looks_like_item_pack_free_misfile,
    _text_looks_like_item_pack_free_return_chrome,
)


class ItemPackFreeChromeSkipTests(unittest.TestCase):
    def test_detects_stock_and_sales_analysis_headers(self):
        text = (
            "AASTHA TRADERS\nSTOCK AND SALES ANALYSIS\n"
            "PRODUCT PACK OPENING PURCHASE FREE P.RETURN FREE SALE "
            "FREE S.RETURN FREE OTHERS CLOSING"
        )
        self.assertTrue(_text_looks_like_item_pack_free_return_chrome(text))

    def test_detects_soft_ocr_analysis_title_partial_headers(self):
        text = "STOCK AND SALES ANALYSIS\nPACK OPENING SALE FREE"
        self.assertTrue(_text_looks_like_item_pack_free_return_chrome(text))

    def test_rejects_op_pur_gd_chrome(self):
        text = (
            "Stock and Sale Report\nOp Pur Gd.In Tot In Sale Closing "
            "Purchase Sale Stock"
        )
        self.assertFalse(_text_looks_like_item_pack_free_return_chrome(text))

    def test_stub_flag_marks_misfile(self):
        result = {
            "line_items": [],
            "totals": {
                "extra": {"ssa_dump_item_pack_free_return": True},
            },
        }
        self.assertTrue(_ssa_dump_result_looks_like_item_pack_free_misfile(result))

    def test_total_colon_matches_free_return_total_name(self):
        self.assertTrue(_FREE_RETURN_TOTAL_VALUE_NAME.match("TOTAL:"))
        self.assertTrue(_FREE_RETURN_TOTAL_VALUE_NAME.match("Total Value"))
        self.assertFalse(_FREE_RETURN_TOTAL_VALUE_NAME.match("Total Value (00)"))


if __name__ == "__main__":
    unittest.main()
