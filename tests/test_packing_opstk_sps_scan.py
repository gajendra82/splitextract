"""Scanned Packing / Op Stk / D. Qty grids must not use the Receipt/Pur reader."""

from __future__ import annotations

import unittest

from services.sales_statement_extractor import (
    _is_packing_opstk_sps_scan_text,
    _merge_packing_opstk_scan_results,
    _packing_opstk_scan_result_ok,
    empty_line_item,
    empty_result,
)


OCR_HEADER = (
    "VARDHMAN MEDICAL AGENCY\n"
    "Stock and Sale Statement From 01-Oct-26 to 31-Oct-26\n"
    "Item Name Packing Op Stk PQty PVal SQty SpS SVal CQty D.Qty AQty CiStk Cl Val\n"
)

ZANDRA = (
    "Stock and Sale Statement\n"
    "Item Cd Item Name Op Stk P Qty P Val S Qty S Val Cl Stk Cl Val\n"
)

SWIL = (
    "Sales & Stock Statement\n"
    "PRODUCT NAME PACKING Opening Qty Receipt/Pur Value Issue/Sales Qty Closing Qty\n"
)


def _item(name, **fields):
    item = empty_line_item()
    item["product_name"] = name
    item["packing"] = fields.pop("packing", None)
    for key in (
        "opening_qty",
        "receipts_qty",
        "sales_qty",
        "sales_value",
        "closing_qty",
        "closing_value",
    ):
        if key in fields:
            item[key] = fields[key]
    extra = item["extra"]
    for key in ("purchase_value", "sales_scheme_qty", "c_qty", "d_qty", "a_qty"):
        if key in fields:
            extra[key] = fields[key]
    return item


class TestPackingOpstkScanDetector(unittest.TestCase):
    def test_ocr_header_with_damage_column(self):
        self.assertTrue(_is_packing_opstk_sps_scan_text(OCR_HEADER))

    def test_zandra_and_receipt_pur_stay_out(self):
        self.assertFalse(_is_packing_opstk_sps_scan_text(ZANDRA))
        self.assertFalse(_is_packing_opstk_sps_scan_text(SWIL))
        self.assertFalse(_is_packing_opstk_sps_scan_text(""))


class TestPackingOpstkScanMerge(unittest.TestCase):
    def test_overlap_keeps_sales_qty_and_sales_value(self):
        top = empty_result("scan.pdf", "pdf")
        top["line_items"] = [
            _item(
                "AACTARIL SOAP 75G",
                packing="1",
                opening_qty=8,
                receipts_qty=144,
                sales_qty=42,
                sales_value=3464,
                closing_qty=110,
                closing_value=9607,
                purchase_value=10627,
            )
        ]
        top["totals"]["sales_value"] = None
        bottom = empty_result("scan.pdf", "pdf")
        bottom["stockist_name"] = "VARDHMAN MEDICAL AGENCY"
        bottom["line_items"] = [
            _item(
                "AACTARIL SOAP 75G",
                packing="1",
                opening_qty=8,
                receipts_qty=42,
                sales_qty=0,
                sales_value=0,
                closing_qty=110,
                closing_value=9607,
            ),
            _item(
                "YASHTI-MADHU (60CAP)-CAP",
                packing="60CAP",
                opening_qty=46,
                receipts_qty=0,
                sales_qty=2,
                sales_value=406,
                closing_qty=44,
                closing_value=9219,
            ),
            _item(
                "TRIPHALA SYRUP (200ML)-SYR",
                packing="200ML",
                opening_qty=0,
                receipts_qty=34,
                sales_qty=15,
                sales_value=1746,
                closing_qty=23,
                closing_value=5257,
            ),
            _item(
                "PURCHASED ROW",
                packing="10",
                opening_qty=0,
                receipts_qty=10,
                sales_qty=1,
                sales_value=100,
                purchase_value=800,
            ),
        ]
        bottom["totals"]["sales_value"] = 83464.0
        bottom["totals"]["closing_value"] = 434922.0
        bottom["totals"]["extra"]["purchase_value"] = 125958.0
        # Pad so the quality gate sees a full sheet, not two sample rows.
        for index in range(30):
            bottom["line_items"].append(
                _item(
                    f"PRODUCT {index}",
                    opening_qty=1,
                    sales_qty=1,
                    sales_value=100,
                    closing_qty=1,
                    closing_value=50,
                )
            )

        merged = _merge_packing_opstk_scan_results(
            [top, bottom], "scan.pdf"
        )
        soap = merged["line_items"][0]
        self.assertEqual(soap["sales_qty"], 42)
        self.assertEqual(soap["sales_value"], 3464)
        self.assertEqual(soap["receipts_qty"], 144)
        self.assertEqual(soap["extra"]["purchase_value"], 10627)
        names = [item["product_name"] for item in merged["line_items"]]
        self.assertIn("YASHTI-MADHU (60CAP)-CAP", names)
        syrup = next(
            item
            for item in merged["line_items"]
            if item["product_name"].startswith("TRIPHALA")
        )
        self.assertEqual(syrup["opening_qty"], 34)
        self.assertEqual(syrup["receipts_qty"], 0)
        self.assertEqual(syrup["sales_qty"], 15)
        self.assertEqual(syrup["sales_value"], 1746)
        purchased = next(
            item
            for item in merged["line_items"]
            if item["product_name"] == "PURCHASED ROW"
        )
        self.assertEqual(purchased["opening_qty"], 0)
        self.assertEqual(purchased["receipts_qty"], 10)
        self.assertEqual(purchased["extra"]["purchase_value"], 800)
        self.assertIsNone(merged["totals"]["sales_value"])
        self.assertEqual(merged["totals"]["closing_value"], 434922.0)
        self.assertEqual(
            merged["totals"]["extra"]["extraction_method"],
            "packing_opstk_sps_scan",
        )
        self.assertTrue(_packing_opstk_scan_result_ok(merged))

    def test_sheet_closing_on_last_product_is_removed(self):
        top = empty_result("scan.pdf", "pdf")
        top["line_items"] = [
            _item(
                "AACTARIL SOAP 75G",
                opening_qty=8,
                receipts_qty=144,
                sales_qty=42,
                sales_value=3464,
                closing_qty=110,
                closing_value=9607,
                purchase_value=10627,
            )
        ]
        bottom = empty_result("scan.pdf", "pdf")
        bottom["line_items"] = [
            _item(
                "YASHTI-MADHU (60CAP)-CAP",
                opening_qty=46,
                sales_qty=2,
                sales_value=406,
                closing_qty=2935,
                closing_value=434922,
            )
        ]
        for index in range(30):
            bottom["line_items"].insert(
                0,
                _item(
                    f"PRODUCT {index}",
                    opening_qty=1,
                    sales_qty=1,
                    sales_value=100,
                    closing_qty=10,
                    closing_value=50,
                ),
            )
        merged = _merge_packing_opstk_scan_results([top, bottom], "scan.pdf")
        yashti = merged["line_items"][-1]
        self.assertEqual(yashti["product_name"], "YASHTI-MADHU (60CAP)-CAP")
        self.assertEqual(yashti["opening_qty"], 46)
        self.assertEqual(yashti["sales_qty"], 2)
        self.assertEqual(yashti["sales_value"], 406)
        self.assertEqual(yashti["closing_qty"], 0)
        self.assertEqual(yashti["closing_value"], 0)
        self.assertEqual(merged["totals"]["closing_value"], 434922)


if __name__ == "__main__":
    unittest.main()
