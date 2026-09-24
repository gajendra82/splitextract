"""Himalaya/ZANDRA Product Stock Report — keep Cls Amt / item values."""

from __future__ import annotations

import unittest

from services.sales_statement_extractor import (
    STOCK_IDENTITY_SALERET,
    _PRODUCT_STOCK_REPORT_VISION_PROMPT,
    _apply_stock_identity_validation,
    _is_opbal_issue_closing_format,
    _is_product_stock_report_text,
    _maybe_repair_product_stock_report,
    _parse_opbal_receipt_issue_statement,
    _parse_product_stock_report,
    _parse_product_stock_report_from_rows,
    _product_stock_report_values_missing,
    _psr_fill_missing_sales,
    _psr_overlay_ocr_qty,
    _psr_qty_needs_verify,
    _sanitize_statement_financials,
    _stock_expected_closing,
    empty_result,
    extract_sales_statement,
)


SAMPLE_TEXT = """
Product Stock Report
ATUL MEDICO
Mfg Company: HIMALAYA WELLNESS CO (ZANDRA)
From: 01/08/2026 To: 31/08/2026
Product Name Opening Purchase Total Sale Sample Exp/Clos Closing Cls Amt Closing
BONNISAN DROPS 65.00 50.00 115.00 0.00 0.00 0.00 65.00 1744.11 0.00
BONNISAN LIQ 100 ML 110.00 0.00 110.00 13.00 0.00 0.00 20.00 326.00 0.00
BONNISAN LIQ 200 ML 27.00 0.00 27.00 23.00 0.00 0.00 0.00 196.71 26.00
LIV 52 DROPS 60 ML 66.00 0.00 66.00 10.00 0.00 0.00 66.00 890.50 0.00
CYSTONE TAB 50.00 0.00 50.00 50.00 0.00 0.00 0.00 202.14 0.00
"""

ATUL_SALERET_TEXT = """
Product Stock Report
ATUL MEDICO
Mfg Company: HIMALAYA WELLNESS CO (ZANDRA)
From: 01/08/2026 To: 31/08/2026
Product Name Opening Purchase Total Sale SaleRet Exp/Dmg Closing Stock Cls Amt Order Qty
BONNISAN DROPS 65.00 50.00 115.00 6.00 0.00 0.00 109.00 7214.71 0.00
BONNISAN LIQ 100 ML 101.00 0.00 101.00 13.00 0.00 0.00 88.00 5288.80 0.00
BONNISAN LIQ 200 ML 27.00 0.00 27.00 26.00 2.00 2.00 1.00 101.31 0.00
BONNISPAZ DROPS 17.00 144.00 161.00 27.00 0.00 0.00 134.00 7603.14 0.00
CYSTONE FORTE TAB 7.00 0.00 7.00 2.00 0.00 0.00 5.00 536.95 0.00
EVECARE CAP 90.00 0.00 90.00 10.00 0.00 0.00 80.00 13130.97 0.00
"""

OPBAL_SAMPLE = """
MAHAJAN ASSOCIATES
Sales & Stock From 01-08-2026 Upto 31-08-2026
PRODUCT PACKING OpBal Receipt Total Issue Closing Dump Near
LIV-52 SYP 100ML 10 2 12 3 9 0 0
"""


class TestProductStockReport(unittest.TestCase):
    def test_detector_accepts_product_stock_report_only(self):
        self.assertTrue(_is_product_stock_report_text(SAMPLE_TEXT))
        self.assertFalse(_is_product_stock_report_text(OPBAL_SAMPLE))
        self.assertTrue(_is_opbal_issue_closing_format(OPBAL_SAMPLE))
        self.assertFalse(_is_product_stock_report_text("Stock and Sales Report\nOpening Purchase"))

    def test_cls_amt_mapped_to_closing_value(self):
        result = _parse_product_stock_report(SAMPLE_TEXT, "stmt.png", "png")
        self.assertIsNotNone(result)
        drops = next(i for i in result["line_items"] if i["product_name"] == "BONNISAN DROPS")
        self.assertEqual(drops["opening_qty"], 65.0)
        self.assertEqual(drops["receipts_qty"], 50.0)
        self.assertEqual(drops["sales_qty"], 0.0)
        self.assertEqual(drops["closing_qty"], 65.0)
        self.assertEqual(drops["closing_value"], 1744.11)
        self.assertEqual(drops["sales_value"], 0.0)
        self.assertEqual(drops["extra"]["total_stock"], 115.0)
        self.assertGreater(result["totals"]["closing_value"], 0)

    def test_total_column_is_not_closing_qty(self):
        result = _parse_product_stock_report(SAMPLE_TEXT, "stmt.png", "png")
        drops = next(i for i in result["line_items"] if i["product_name"] == "BONNISAN DROPS")
        self.assertEqual(drops["extra"]["total_stock"], 115.0)
        self.assertEqual(drops["opening_qty"], 65.0)
        self.assertNotEqual(drops["opening_qty"], 115.0)

    def test_liv52_name_keeps_embedded_digits(self):
        result = _parse_product_stock_report(SAMPLE_TEXT, "stmt.png", "png")
        names = [i["product_name"] for i in result["line_items"]]
        self.assertTrue(any(n.startswith("LIV 52 DROPS") for n in names))
        liv = next(i for i in result["line_items"] if "LIV 52" in i["product_name"])
        self.assertEqual(liv["opening_qty"], 66.0)
        self.assertEqual(liv["sales_qty"], 10.0)
        self.assertEqual(liv["closing_value"], 890.50)

    def test_sale_qty_not_zeroed_when_present(self):
        result = _parse_product_stock_report(SAMPLE_TEXT, "stmt.png", "png")
        liq = next(i for i in result["line_items"] if i["product_name"] == "BONNISAN LIQ 100 ML")
        self.assertEqual(liq["sales_qty"], 13.0)
        self.assertEqual(liq["closing_value"], 326.0)
        self.assertNotEqual(liq["opening_qty"], 0.0)

    def test_metadata(self):
        result = _parse_product_stock_report(SAMPLE_TEXT, "stmt.png", "png")
        self.assertEqual(result["report_title"], "Product Stock Report")
        self.assertEqual(result["stockist_name"], "ATUL MEDICO")
        self.assertEqual(result["company_name"], "HIMALAYA WELLNESS CO (ZANDRA)")
        self.assertEqual(result["period_from"], "2026-08-01")
        self.assertEqual(result["period_to"], "2026-08-31")

    def test_html_table_keeps_cls_amt(self):
        rows = [
            ["Product Stock Report"],
            ["ATUL MEDICO"],
            ["Mfg Company: HIMALAYA WELLNESS CO (ZANDRA)"],
            ["From: 01/08/2026 To: 31/08/2026"],
            [
                "Product Name",
                "Opening",
                "Purchase",
                "Total",
                "Sale",
                "Sample",
                "Exp/Clos",
                "Closing",
                "Cls Amt",
                "Closing",
            ],
            ["BONNISAN DROPS", "65.00", "50.00", "115.00", "0.00", "0.00", "0.00", "65.00", "1744.11", "0.00"],
            ["BONNISAN LIQ 100 ML", "110.00", "0.00", "110.00", "13.00", "0.00", "0.00", "20.00", "326.00", "0.00"],
        ]
        page = "\n".join(" ".join(r) for r in rows)
        page = "Product Stock Report\n" + page
        result = _parse_product_stock_report_from_rows(rows, "stmt.htm", "htm", page)
        self.assertIsNotNone(result)
        drops = result["line_items"][0]
        self.assertEqual(drops["product_name"], "BONNISAN DROPS")
        self.assertEqual(drops["closing_value"], 1744.11)
        self.assertEqual(drops["receipts_qty"], 50.0)
        self.assertEqual(result["line_items"][1]["closing_value"], 326.0)

    def test_gemini_zero_values_repaired_from_ocr_text(self):
        gemini = empty_result("stmt.png", "png")
        gemini["report_title"] = "Product Stock Report"
        gemini["stockist_name"] = "ATUL MEDICO"
        gemini["company_name"] = "HIMALAYA WELLNESS CO (ZANDRA)"
        gemini["line_items"] = [
            {
                "product_code": None,
                "product_name": "BONNIS AN DROPS",
                "packing": None,
                "opening_qty": 65,
                "receipts_qty": 50,
                "sales_qty": 0,
                "sales_value": 0,
                "closing_qty": 115,
                "closing_value": 0,
                "extra": [],
            },
            {
                "product_code": None,
                "product_name": "BONNIS AN LIQ 100 ML",
                "packing": None,
                "opening_qty": 0,
                "receipts_qty": 0,
                "sales_qty": 0,
                "sales_value": 0,
                "closing_qty": 0,
                "closing_value": 0,
                "extra": [],
            },
        ]
        gemini["totals"]["extra"]["extraction_method"] = "gemini_vision"
        repaired = _maybe_repair_product_stock_report(
            gemini, SAMPLE_TEXT, "stmt.png", "png"
        )
        values = [i["closing_value"] for i in repaired["line_items"]]
        self.assertTrue(any(v > 0 for v in values))
        liq = next(
            i for i in repaired["line_items"] if "100 ML" in i["product_name"]
        )
        self.assertEqual(liq["closing_value"], 326.0)
        self.assertNotEqual(liq["opening_qty"], 0.0)

    def test_does_not_steal_opbal_format(self):
        self.assertIsNone(
            _parse_product_stock_report(OPBAL_SAMPLE, "opbal.pdf", "pdf")
        )

    def test_extract_sales_statement_txt_path(self):
        result = extract_sales_statement(
            SAMPLE_TEXT.encode("utf-8"), "atul-stock.txt"
        )
        result = _sanitize_statement_financials(result)
        drops = next(i for i in result["line_items"] if "BONNISAN DROPS" in i["product_name"])
        self.assertEqual(drops["closing_value"], 1744.11)
        self.assertGreater(result["totals"]["closing_value"], 0)

    def test_user_payload_is_flagged_for_repair(self):
        gemini = empty_result("tmp.png", "png")
        gemini["report_title"] = "Product Stock Report"
        gemini["line_items"] = [
            {
                "product_name": "BONNIS AN DROPS",
                "opening_qty": 65,
                "receipts_qty": 50,
                "sales_qty": 0,
                "sales_value": 0,
                "closing_qty": 115,
                "closing_value": 0,
                "extra": {},
            },
            {
                "product_name": "BONNIS AN LIQ 100 ML",
                "opening_qty": 0,
                "receipts_qty": 0,
                "sales_qty": 0,
                "sales_value": 0,
                "closing_qty": 0,
                "closing_value": 0,
                "extra": {},
            },
        ]
        self.assertTrue(_product_stock_report_values_missing(gemini))
        self.assertIn("SaleRet", _PRODUCT_STOCK_REPORT_VISION_PROMPT)
        self.assertIn("Sale 26", _PRODUCT_STOCK_REPORT_VISION_PROMPT)

    def test_sales_value_from_cls_amt_rate_when_qty_present(self):
        result = empty_result("tmp.png", "png")
        result["report_title"] = "Product Stock Report"
        result["line_items"] = [
            {
                "product_name": "BONNIS AN LIQ 100 ML",
                "opening_qty": 101.0,
                "receipts_qty": 0.0,
                "sales_qty": 13.0,
                "sales_value": 0.0,
                "closing_qty": 88.0,
                "closing_value": 5288.8,
                "extra": {"total_stock": 101.0},
            },
            {
                "product_name": "BONNIS AN DROPS",
                "opening_qty": 65.0,
                "receipts_qty": 50.0,
                "sales_qty": 0.0,
                "sales_value": 0.0,
                "closing_qty": 115.0,
                "closing_value": 7214.71,
                "extra": {"total_stock": 115.0},
            },
            {
                "product_name": "EVECARE FORTE SYP 200ML",
                "opening_qty": 24.0,
                "receipts_qty": 0.0,
                "sales_qty": 0.0,
                "sales_value": 0.0,
                "closing_qty": 19.0,
                "closing_value": 5298.02,
                "extra": {"total_stock": 24.0},
            },
        ]
        filled = _psr_fill_missing_sales(result)
        liq = next(i for i in filled["line_items"] if "100 ML" in i["product_name"])
        self.assertEqual(liq["sales_qty"], 13.0)
        self.assertAlmostEqual(liq["sales_value"], round(13.0 * 5288.8 / 88.0, 2))
        drops = next(i for i in filled["line_items"] if i["product_name"] == "BONNIS AN DROPS")
        self.assertEqual(drops["sales_qty"], 0.0)
        self.assertEqual(drops["sales_value"], 0.0)
        syp = next(i for i in filled["line_items"] if "FORTE SYP" in i["product_name"])
        self.assertEqual(syp["sales_qty"], 5.0)
        self.assertAlmostEqual(syp["sales_value"], round(5.0 * 5298.02 / 19.0, 2))
        self.assertGreater(filled["totals"]["sales_value"], 0)

    def test_sales_fill_skips_non_product_stock_report(self):
        result = empty_result("other.png", "png")
        result["report_title"] = "Stock and Sales Report"
        result["line_items"] = [
            {
                "product_name": "LIV 52",
                "opening_qty": 10.0,
                "receipts_qty": 0.0,
                "sales_qty": 3.0,
                "sales_value": 0.0,
                "closing_qty": 7.0,
                "closing_value": 700.0,
                "extra": {},
            }
        ]
        filled = _psr_fill_missing_sales(result)
        self.assertEqual(filled["line_items"][0]["sales_value"], 0.0)

    def test_sanitize_fills_user_psr_sales_zeros(self):
        result = empty_result("tmp_625f4a45.png", "png")
        result["report_title"] = "Product Stock Report"
        result["line_items"] = [
            {
                "product_name": "BONNIS AN LIQ 100 ML",
                "opening_qty": 101.0,
                "receipts_qty": 0.0,
                "sales_qty": 13.0,
                "sales_value": 0.0,
                "closing_qty": 88.0,
                "closing_value": 5288.8,
                "extra": {"total_stock": 101.0},
            },
            {
                "product_name": "HIORA K PASTE (50 GM",
                "opening_qty": 0.0,
                "receipts_qty": 50.0,
                "sales_qty": 0.0,
                "sales_value": 0.0,
                "closing_qty": 41.0,
                "closing_value": 2935.19,
                "extra": {"total_stock": 50.0},
            },
        ]
        result["totals"]["extra"]["extraction_method"] = "product_stock_report_vision"
        sanitized = _sanitize_statement_financials(result)
        liq = sanitized["line_items"][0]
        paste = sanitized["line_items"][1]
        self.assertEqual(liq["sales_qty"], 13.0)
        self.assertAlmostEqual(liq["sales_value"], round(13.0 * 5288.8 / 88.0, 2))
        self.assertEqual(paste["sales_qty"], 9.0)
        self.assertAlmostEqual(paste["sales_value"], round(9.0 * 2935.19 / 41.0, 2))
        self.assertGreater(sanitized["totals"]["sales_value"], 0)

    def test_copied_cls_amt_not_used_as_sales_value(self):
        result = empty_result("tmp.png", "png")
        result["report_title"] = "Product Stock Report"
        result["line_items"] = [
            {
                "product_name": "BONNIS AN LIQ 100 ML",
                "opening_qty": 101.0,
                "receipts_qty": 0.0,
                "sales_qty": 13.0,
                "sales_value": 5288.8,
                "closing_qty": 88.0,
                "closing_value": 5288.8,
                "extra": {"total_stock": 101.0},
            },
            {
                "product_name": "BONNIS AN DROPS",
                "opening_qty": 65.0,
                "receipts_qty": 50.0,
                "sales_qty": 0.0,
                "sales_value": 0.0,
                "closing_qty": 115.0,
                "closing_value": 721471.0,
                "extra": {"total_stock": 115.0},
            },
            {
                "product_name": "CYS TONE TAB",
                "opening_qty": 50.0,
                "receipts_qty": 0.0,
                "sales_qty": 50.0,
                "sales_value": 20.9,
                "closing_qty": 0.0,
                "closing_value": 20.9,
                "extra": {"total_stock": 50.0},
            },
        ]
        filled = _sanitize_statement_financials(result)
        liq = next(i for i in filled["line_items"] if "100 ML" in i["product_name"])
        self.assertAlmostEqual(liq["sales_value"], round(13.0 * 5288.8 / 88.0, 2))
        self.assertNotEqual(liq["sales_value"], liq["closing_value"])
        drops = next(i for i in filled["line_items"] if i["product_name"] == "BONNIS AN DROPS")
        self.assertAlmostEqual(drops["closing_value"], 7214.71)
        self.assertEqual(drops["sales_qty"], 0.0)
        self.assertEqual(drops["sales_value"], 0.0)
        tab = next(i for i in filled["line_items"] if "TAB" in i["product_name"])
        self.assertEqual(tab["closing_qty"], 0.0)
        self.assertEqual(tab["sales_value"], 0.0)

    def test_closing_not_left_as_total_when_sale_exists(self):
        result = empty_result("tmp.png", "png")
        result["report_title"] = "Product Stock Report"
        result["line_items"] = [
            {
                "product_name": "LIV 52 DROPS 60 MU",
                "opening_qty": 66.0,
                "receipts_qty": 0.0,
                "sales_qty": 10.0,
                "sales_value": 5484.64,
                "closing_qty": 66.0,
                "closing_value": 5484.64,
                "extra": {"total_stock": 66.0},
            }
        ]
        filled = _psr_fill_missing_sales(result)
        liv = filled["line_items"][0]
        self.assertEqual(liv["sales_qty"], 10.0)
        self.assertEqual(liv["closing_qty"], 66.0)
        self.assertTrue(_psr_qty_needs_verify(filled))

    def _psr_item(self, **kwargs):
        result = empty_result("tmp.png", "png")
        result["report_title"] = "Product Stock Report"
        row = {
            "product_name": "X",
            "opening_qty": 0.0,
            "receipts_qty": 0.0,
            "sales_qty": 0.0,
            "sales_value": 0.0,
            "closing_qty": 0.0,
            "closing_value": 0.0,
            "extra": {},
        }
        row.update(kwargs)
        result["line_items"] = [row]
        return result

    def test_qty_verify_flags_dropped_sale_and_truncated_opening(self):
        drops = self._psr_item(
            product_name="BONNIS AN DROPS",
            opening_qty=65.0,
            receipts_qty=50.0,
            sales_qty=0.0,
            closing_qty=115.0,
            closing_value=7214.71,
            extra={"total_stock": 115.0},
        )
        self.assertTrue(_psr_qty_needs_verify(drops))
        liq = self._psr_item(
            product_name="BONNIS AN LIQ 200 ML",
            opening_qty=2.0,
            receipts_qty=0.0,
            closing_qty=2.0,
            closing_value=101.31,
            extra={"total_stock": 2.0},
        )
        self.assertTrue(_psr_qty_needs_verify(liq))
        bresol = self._psr_item(
            product_name="BRESOL NASAL SOLUTION",
            opening_qty=50.0,
            receipts_qty=0.0,
            closing_qty=50.0,
            closing_value=2532.5,
            extra={"total_stock": 50.0},
        )
        self.assertFalse(_psr_qty_needs_verify(bresol))

    def test_extra_zero_opening_and_sale_repaired(self):
        result = self._psr_item(
            product_name="EVECARE CAP",
            opening_qty=900.0,
            receipts_qty=0.0,
            sales_qty=100.0,
            sales_value=0.0,
            closing_qty=80.0,
            closing_value=13130.97,
            extra={"total_stock": 900.0},
        )
        filled = _psr_fill_missing_sales(result)
        cap = filled["line_items"][0]
        self.assertEqual(cap["opening_qty"], 90.0)
        self.assertEqual(cap["sales_qty"], 10.0)
        self.assertEqual(cap["closing_qty"], 80.0)
        self.assertAlmostEqual(cap["sales_value"], round(10.0 * 13130.97 / 80.0, 2))

    def test_identity_sale_when_closing_below_total(self):
        result = self._psr_item(
            product_name="BONNIS AN DROPS",
            opening_qty=65.0,
            receipts_qty=50.0,
            sales_qty=0.0,
            closing_qty=109.0,
            closing_value=7214.71,
            extra={"total_stock": 115.0, "sale_return": 0.0, "exp_damage": 0.0},
        )
        filled = _psr_fill_missing_sales(result)
        filled = _apply_stock_identity_validation(filled)
        drops = filled["line_items"][0]
        self.assertEqual(drops["sales_qty"], 6.0)
        self.assertEqual(drops["closing_qty"], 109.0)
        self.assertAlmostEqual(drops["sales_value"], round(6.0 * 7214.71 / 109.0, 2))
        self.assertTrue(drops["extra"]["stock_identity_ok"])

    def test_geriforte_sale_not_taken_from_saleret(self):
        result = self._psr_item(
            product_name="GERIFORTE TAB",
            opening_qty=13.0,
            receipts_qty=0.0,
            sales_qty=0.0,
            closing_qty=13.0,
            closing_value=1911.0,
            extra={"total_stock": 13.0, "sale_return": 2.0, "exp_damage": 0.0},
        )
        filled = _psr_fill_missing_sales(result)
        tab = filled["line_items"][0]
        self.assertEqual(tab["sales_qty"], 0.0)
        self.assertEqual(tab["extra"]["sale_return"], 2.0)
        self.assertEqual(tab["closing_qty"], 13.0)

    def test_closing_follows_printed_sale(self):
        result = self._psr_item(
            product_name="FIORAK PASTE (100 GM",
            opening_qty=0.0,
            receipts_qty=50.0,
            sales_qty=21.0,
            sales_value=2133.74,
            closing_qty=20.0,
            closing_value=2133.74,
            extra={"total_stock": 50.0},
        )
        filled = _psr_fill_missing_sales(result)
        paste = filled["line_items"][0]
        self.assertEqual(paste["sales_qty"], 21.0)
        self.assertEqual(paste["closing_qty"], 20.0)
        self.assertTrue(_psr_qty_needs_verify(filled))

    def test_zero_open_row_gets_receipts_from_closing(self):
        result = self._psr_item(
            product_name="CYSTONE FORTE TAB",
            opening_qty=0.0,
            receipts_qty=0.0,
            sales_qty=0.0,
            closing_qty=5.0,
            closing_value=536.95,
            extra={"total_stock": 0.0},
        )
        filled = _psr_fill_missing_sales(result)
        tab = filled["line_items"][0]
        self.assertEqual(tab["receipts_qty"], 0.0)
        self.assertEqual(tab["closing_qty"], 5.0)
        self.assertEqual(tab["sales_qty"], 0.0)

    def test_valid_kaveri_unchanged_by_detector(self):
        kaveri = (
            "KAVERI MEDICALS\n"
            "MONTHLY STOCK & SALES\n"
            "FROM : 01/09/2026 TO 30/09/2026\n"
            "01023 LIV 52 SYP 100ML 10 2 12 3 30.00 9 90.00\n"
        )
        self.assertFalse(_is_product_stock_report_text(kaveri))

    def test_atul_bonnisan_drops_saleret_identity(self):
        result = extract_sales_statement(
            ATUL_SALERET_TEXT.encode("utf-8"), "atul-stock.txt"
        )
        drops = next(i for i in result["line_items"] if "DROPS" in i["product_name"] and "PAZ" not in i["product_name"] and "LIQ" not in i["product_name"])
        self.assertEqual(drops["opening_qty"], 65.0)
        self.assertEqual(drops["receipts_qty"], 50.0)
        self.assertEqual(drops["sales_qty"], 6.0)
        self.assertEqual(drops["extra"]["sale_return"], 0.0)
        self.assertEqual(drops["extra"]["exp_damage"], 0.0)
        self.assertEqual(drops["closing_qty"], 109.0)
        self.assertTrue(drops["extra"]["stock_identity_ok"])
        self.assertEqual(
            result["totals"]["extra"]["stock_identity_kind"], STOCK_IDENTITY_SALERET
        )

    def test_atul_bonnisan_liq_200_ml_regression(self):
        result = extract_sales_statement(
            ATUL_SALERET_TEXT.encode("utf-8"), "atul-stock.txt"
        )
        liq = next(i for i in result["line_items"] if "200 ML" in i["product_name"])
        self.assertEqual(liq["opening_qty"], 27.0)
        self.assertEqual(liq["receipts_qty"], 0.0)
        self.assertEqual(liq["extra"]["total_stock"], 27.0)
        self.assertEqual(liq["sales_qty"], 26.0)
        self.assertEqual(liq["extra"]["sale_return"], 2.0)
        self.assertEqual(liq["extra"]["exp_damage"], 2.0)
        self.assertEqual(liq["closing_qty"], 1.0)
        self.assertEqual(
            _stock_expected_closing(liq, STOCK_IDENTITY_SALERET), 1.0
        )
        self.assertTrue(liq["extra"]["stock_identity_ok"])
        self.assertNotEqual(liq["sales_qty"], 1.0)

    def test_sale_one_is_not_rewritten_to_twenty_six(self):
        result = empty_result("tmp.png", "png")
        result["report_title"] = "Product Stock Report"
        result["totals"]["extra"]["psr_column_layout"] = "saleret"
        result["line_items"] = [
            {
                "product_name": "BONNISAN LIQ 200 ML",
                "opening_qty": 27.0,
                "receipts_qty": 0.0,
                "sales_qty": 1.0,
                "sales_value": 0.0,
                "closing_qty": 1.0,
                "closing_value": 101.31,
                "extra": {"total_stock": 27.0, "sale_return": 0.0, "exp_damage": 0.0},
            }
        ]
        filled = _psr_fill_missing_sales(result)
        filled = _apply_stock_identity_validation(filled)
        liq = filled["line_items"][0]
        self.assertEqual(liq["sales_qty"], 1.0)
        self.assertFalse(liq["extra"]["stock_identity_ok"])
        self.assertTrue(_psr_qty_needs_verify(filled))

    def test_saleret_html_table_maps_columns(self):
        rows = [
            ["Product Stock Report"],
            ["ATUL MEDICO"],
            ["Mfg Company: HIMALAYA WELLNESS CO (ZANDRA)"],
            ["From: 01/08/2026 To: 31/08/2026"],
            [
                "Product Name",
                "Opening",
                "Purchase",
                "Total",
                "Sale",
                "SaleRet",
                "Exp/Dmg",
                "Closing Stock",
                "Cls Amt",
                "Order Qty",
            ],
            ["BONNISAN LIQ 200 ML", "27.00", "0.00", "27.00", "26.00", "2.00", "2.00", "1.00", "101.31", "0.00"],
        ]
        page = "Product Stock Report\n" + "\n".join(" ".join(r) for r in rows)
        result = _parse_product_stock_report_from_rows(rows, "stmt.htm", "htm", page)
        self.assertIsNotNone(result)
        liq = result["line_items"][0]
        self.assertEqual(liq["sales_qty"], 26.0)
        self.assertEqual(liq["extra"]["sale_return"], 2.0)
        self.assertEqual(liq["extra"]["exp_damage"], 2.0)
        self.assertEqual(liq["closing_qty"], 1.0)
        validated = _apply_stock_identity_validation(result)
        self.assertTrue(validated["line_items"][0]["extra"]["stock_identity_ok"])

    def test_exp_dmg_not_merged_into_sale(self):
        result = empty_result("tmp.png", "png")
        result["report_title"] = "Product Stock Report"
        result["totals"]["extra"]["stock_identity_kind"] = STOCK_IDENTITY_SALERET
        result["line_items"] = [
            {
                "product_name": "BONNISAN LIQ 200 ML",
                "opening_qty": 27.0,
                "receipts_qty": 0.0,
                "sales_qty": 26.0,
                "sales_value": 0.0,
                "closing_qty": 1.0,
                "closing_value": 101.31,
                "extra": {
                    "total_stock": 27.0,
                    "sale_return": 2.0,
                    "exp_damage": 2.0,
                },
            }
        ]
        validated = _apply_stock_identity_validation(result)
        item = validated["line_items"][0]
        self.assertEqual(item["sales_qty"], 26.0)
        self.assertEqual(item["extra"]["sale_return"], 2.0)
        self.assertEqual(item["extra"]["exp_damage"], 2.0)
        self.assertTrue(item["extra"]["stock_identity_ok"])

    def test_truncated_opening_recovered_from_printed_total(self):
        result = self._psr_item(
            product_name="BONNIS AN LIQ 200 ML",
            opening_qty=2.0,
            receipts_qty=0.0,
            sales_qty=26.0,
            closing_qty=1.0,
            closing_value=101.31,
            extra={"total_stock": 27.0, "sale_return": 2.0, "exp_damage": 2.0},
        )
        filled = _psr_fill_missing_sales(result)
        filled = _apply_stock_identity_validation(filled)
        liq = filled["line_items"][0]
        self.assertEqual(liq["opening_qty"], 27.0)
        self.assertEqual(liq["extra"]["total_stock"], 27.0)
        self.assertEqual(liq["sales_qty"], 26.0)
        self.assertEqual(liq["closing_qty"], 1.0)
        self.assertTrue(liq["extra"]["stock_identity_ok"])

    def test_truncated_opening_and_total_recovered_from_saleret(self):
        result = self._psr_item(
            product_name="BONNIS AN LIQ 200 ML",
            opening_qty=2.0,
            receipts_qty=0.0,
            sales_qty=26.0,
            closing_qty=1.0,
            closing_value=101.31,
            extra={"total_stock": 2.0, "sale_return": 2.0, "exp_damage": 2.0},
        )
        filled = _psr_fill_missing_sales(result)
        filled = _apply_stock_identity_validation(filled)
        liq = filled["line_items"][0]
        self.assertEqual(liq["opening_qty"], 27.0)
        self.assertEqual(liq["extra"]["total_stock"], 27.0)
        self.assertEqual(liq["sales_qty"], 26.0)
        self.assertTrue(liq["extra"]["stock_identity_ok"])

    def test_truncated_purchase_recovered_from_total(self):
        result = self._psr_item(
            product_name="BONNIS PAZ DROPS",
            opening_qty=17.0,
            receipts_qty=14.0,
            sales_qty=27.0,
            closing_qty=134.0,
            closing_value=7603.14,
            extra={"total_stock": 161.0, "sale_return": 0.0, "exp_damage": 0.0},
        )
        filled = _psr_fill_missing_sales(result)
        filled = _apply_stock_identity_validation(filled)
        paz = filled["line_items"][0]
        self.assertEqual(paz["receipts_qty"], 144.0)
        self.assertEqual(paz["opening_qty"], 17.0)
        self.assertTrue(paz["extra"]["stock_identity_ok"])

    def test_cystone_forte_single_digit_opening_kept(self):
        result = self._psr_item(
            product_name="CYSTONE FORTE TAB",
            opening_qty=7.0,
            receipts_qty=0.0,
            sales_qty=2.0,
            closing_qty=5.0,
            closing_value=536.95,
            extra={"total_stock": 7.0, "sale_return": 0.0, "exp_damage": 0.0},
        )
        filled = _psr_fill_missing_sales(result)
        tab = filled["line_items"][0]
        self.assertEqual(tab["opening_qty"], 7.0)
        self.assertEqual(tab["sales_qty"], 2.0)
        self.assertEqual(tab["closing_qty"], 5.0)

    def test_ocr_qty_overrides_truncated_opening(self):
        vision = self._psr_item(
            product_name="BONNIS AN LIQ 200 ML",
            opening_qty=2.0,
            receipts_qty=0.0,
            sales_qty=0.0,
            closing_qty=2.0,
            closing_value=101.31,
            extra={"total_stock": 2.0},
        )
        ocr = self._psr_item(
            product_name="BONNISAN LIQ 200 ML",
            opening_qty=27.0,
            receipts_qty=0.0,
            sales_qty=26.0,
            closing_qty=1.0,
            closing_value=101.31,
            extra={"total_stock": 27.0, "sale_return": 2.0, "exp_damage": 2.0},
        )
        merged = _psr_overlay_ocr_qty(vision, ocr)
        liq = merged["line_items"][0]
        self.assertEqual(liq["opening_qty"], 27.0)
        self.assertEqual(liq["sales_qty"], 26.0)
        self.assertEqual(liq["closing_qty"], 1.0)
        self.assertEqual(liq["extra"]["total_stock"], 27.0)
        self.assertTrue(liq["extra"]["qty_from_ocr"])

    def test_truncated_digit_flags_verify_not_invented(self):
        liq = self._psr_item(
            product_name="BONNIS AN LIQ 200 ML",
            opening_qty=2.0,
            receipts_qty=0.0,
            sales_qty=0.0,
            closing_qty=2.0,
            closing_value=101.31,
            extra={"total_stock": 2.0},
        )
        filled = _psr_fill_missing_sales(liq)
        self.assertEqual(filled["line_items"][0]["opening_qty"], 2.0)
        self.assertTrue(_psr_qty_needs_verify(filled))

    def test_opbal_fallback_still_parses(self):
        result = _parse_opbal_receipt_issue_statement(
            OPBAL_SAMPLE, "opbal.pdf", "pdf"
        )
        self.assertIsNotNone(result)
        self.assertTrue(result.get("line_items"))
        liv = result["line_items"][0]
        self.assertEqual(liv["opening_qty"], 10.0)
        self.assertEqual(liv["receipts_qty"], 2.0)
        self.assertEqual(liv["sales_qty"], 3.0)
        self.assertEqual(liv["closing_qty"], 9.0)
        validated = _apply_stock_identity_validation(result)
        self.assertTrue(validated["line_items"][0]["extra"]["stock_identity_ok"])


if __name__ == "__main__":
    unittest.main()
