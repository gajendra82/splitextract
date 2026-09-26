"""ITEM / PACK / OPENING / PURCHASE / S.RETURN / OTHERS / SUB TOTAL / SALE / P.RETURN / OTHERS / CLOSING.

Zero-quantity source rows stay. Both OTHERS columns stay. Other Excel layouts
still use the generic reader.
"""

import io
import os
import unittest

from openpyxl import Workbook

from services.sales_statement_extractor import extract_sales_statement


_SOURCE_XLS = (
    r"C:\Users\Prerana Bhalerao\Downloads"
    r"\0000736228_2026_08_ZA_30_313_05092026123417.xls"
)


def _xlsx(rows):
    book = Workbook()
    sheet = book.active
    for row in rows:
        sheet.append(row)
    buffer = io.BytesIO()
    book.save(buffer)
    return buffer.getvalue()


def _by_name_pack(result):
    found = {}
    for item in result["line_items"]:
        found[(item["product_name"], item.get("packing"))] = item
    return found


class TestItemPackSreturnOthers(unittest.TestCase):
    def test_zero_rows_and_both_others_columns_are_kept(self):
        payload = _xlsx(
            [
                ["PERFECT MEDICAL AGENCY"],
                ["STOCK & SALES"],
                ["Company : HIMALYA"],
                ["From: 01-Aug-26  To: 31-Aug-26"],
                [
                    "ITEM", "PACK", "OPENING", "PURCHASE", "S.RETURN", "OTHERS",
                    "SUB TOTAL", "SALE", "P.RETURN", "OTHERS", "CLOSING", "ITEMCODE",
                ],
                ["Division : 00"],
                ["ARJUNA TAB", "60TAB", 40, "-", "-", "-", 40, 10, "-", "-", 30, 12878],
                ["BONNISAN DROPS", "30ML", "-", "-", "-", "-", "-", "-", "-", "-", "-", 12879],
                ["LIV 52 DS TAB", "60 TAB", 2, 1046, "-", "-", 1048, 994, "-", 46, 8, 12786],
                ["LIV 52 SYP", "100 ML", 1, "-", "-", "-", 1, "-", "-", "-", 1, 12776],
                ["LIV 52 SYP", "200 ML", "-", 360, "-", "-", 360, 296, "-", 10, 54, 12777],
                ["LIV 52 TAB", "100 TAB", 1000, "-", "-", "-", 1000, 886, "-", "-", 114, 12798],
                ["GUDUCHI TAB", "60 TABS", "N/A", "-", "-", "-", "-", "-", "-", "-", "-", 9113],
                ["Total Value (00)", "", 1, 2, 0, "", "", 3, 0, 4, 5],
            ]
        )
        result = extract_sales_statement(payload, "perfect.xlsx")
        extra = result["totals"]["extra"]
        self.assertEqual(extra["extraction_method"], "item_pack_sreturn_others")
        names = [item["product_name"] for item in result["line_items"]]
        self.assertEqual(
            names,
            [
                "ARJUNA TAB",
                "BONNISAN DROPS",
                "LIV 52 DS TAB",
                "LIV 52 SYP",
                "LIV 52 SYP",
                "LIV 52 TAB",
                "GUDUCHI TAB",
            ],
        )
        self.assertNotIn("Total Value (00)", names)
        rows = _by_name_pack(result)

        arjuna = rows[("ARJUNA TAB", "60TAB")]
        self.assertEqual(arjuna["source_product_name"], "ARJUNA TAB")
        self.assertEqual(arjuna["source_packing"], "60TAB")
        self.assertEqual(arjuna["opening_qty"], 40)
        self.assertEqual(arjuna["sales_qty"], 10)
        self.assertEqual(arjuna["closing_qty"], 30)
        self.assertEqual(arjuna["receipts_qty"], 0)
        self.assertEqual(arjuna["others_out_qty"], 0)

        drops = rows[("BONNISAN DROPS", "30ML")]
        for field in (
            "opening_qty",
            "purchase_qty",
            "sales_return_qty",
            "others_in_qty",
            "subtotal_qty",
            "sales_qty",
            "purchase_return_qty",
            "others_out_qty",
            "closing_qty",
        ):
            self.assertEqual(drops[field], 0, field)
        self.assertTrue(drops["extra"]["qty_reconcile_ok"])

        ds_tab = rows[("LIV 52 DS TAB", "60 TAB")]
        self.assertEqual(ds_tab["opening_qty"], 2)
        self.assertEqual(ds_tab["purchase_qty"], 1046)
        self.assertEqual(ds_tab["receipts_qty"], 1046)
        self.assertEqual(ds_tab["sales_qty"], 994)
        self.assertEqual(ds_tab["others_out_qty"], 46)
        self.assertEqual(ds_tab["closing_qty"], 8)
        self.assertEqual(ds_tab["subtotal_qty"], 1048)
        self.assertEqual(ds_tab["extra"]["expected_closing"], 8)
        self.assertNotEqual(ds_tab["extra"]["expected_closing"], 54)
        self.assertTrue(ds_tab["extra"]["stock_identity_ok"])
        self.assertTrue(ds_tab["extra"]["validation"]["is_valid"])
        self.assertTrue(ds_tab["extra"]["qty_reconcile_ok"])

        syp_100 = rows[("LIV 52 SYP", "100 ML")]
        syp_200 = rows[("LIV 52 SYP", "200 ML")]
        self.assertEqual(syp_100["opening_qty"], 1)
        self.assertEqual(syp_100["closing_qty"], 1)
        self.assertEqual(syp_200["opening_qty"], 0)
        self.assertEqual(syp_200["purchase_qty"], 360)
        self.assertEqual(syp_200["sales_qty"], 296)
        self.assertEqual(syp_200["others_out_qty"], 10)
        self.assertEqual(syp_200["closing_qty"], 54)
        self.assertEqual(syp_200["extra"]["expected_closing"], 54)
        self.assertTrue(syp_200["extra"]["stock_identity_ok"])

        liv_tab = rows[("LIV 52 TAB", "100 TAB")]
        self.assertEqual(liv_tab["opening_qty"], 1000)
        self.assertEqual(liv_tab["sales_qty"], 886)
        self.assertEqual(liv_tab["closing_qty"], 114)

        guduchi = rows[("GUDUCHI TAB", "60 TABS")]
        self.assertIsNone(guduchi["opening_qty"])
        self.assertIn("opening_qty", guduchi["extra"]["qty_parse_errors"])
        self.assertFalse(guduchi["extra"]["qty_reconcile_ok"])
        self.assertEqual(extra["qty_parse_error_rows"], 1)
        self.assertEqual(extra["qty_mismatch_rows"], 0)
        self.assertEqual(extra["zero_qty_rows"], 1)
        self.assertIsNone(guduchi["extra"].get("expected_closing"))
        self.assertEqual(
            guduchi["extra"]["validation"]["reason"],
            "Quantity cell could not be parsed",
        )

    def test_source_closing_is_kept_when_others_out_is_omitted_from_the_old_formula(self):
        payload = _xlsx(
            [
                [
                    "ITEM", "PACK", "OPENING", "PURCHASE", "S.RETURN", "OTHERS",
                    "SUB TOTAL", "SALE", "P.RETURN", "OTHERS", "CLOSING",
                ],
                ["LIV 52 DS TAB", "60 TAB", 2, 1046, "-", "-", 1048, 994, "-", 46, 99],
            ]
        )
        result = extract_sales_statement(payload, "mismatch.xlsx")
        item = result["line_items"][0]
        self.assertEqual(item["closing_qty"], 99)
        self.assertEqual(item["others_out_qty"], 46)
        self.assertEqual(item["extra"]["expected_closing"], 8)
        self.assertFalse(item["extra"]["stock_identity_ok"])
        self.assertEqual(
            item["extra"]["validation"],
            {"is_valid": False, "reason": "Source values do not reconcile"},
        )

    def test_generic_excel_still_skips_all_dash_rows(self):
        payload = _xlsx(
            [
                ["SOME MEDICAL AGENCY"],
                ["Item", "Pack", "Opening", "Purchase", "Sale", "Closing"],
                ["LIV 52 TAB", "100 TAB", 10, 0, 4, 6],
                ["BONNISAN DROPS", "30ML", "-", "-", "-", "-"],
            ]
        )
        result = extract_sales_statement(payload, "generic.xlsx")
        self.assertNotEqual(
            result["totals"]["extra"].get("extraction_method"),
            "item_pack_sreturn_others",
        )
        names = [item["product_name"] for item in result["line_items"]]
        self.assertEqual(names, ["LIV 52 TAB"])
        self.assertEqual(result["line_items"][0]["opening_qty"], 10)
        self.assertEqual(result["line_items"][0]["sales_qty"], 4)
        self.assertEqual(result["line_items"][0]["closing_qty"], 6)


@unittest.skipUnless(os.path.exists(_SOURCE_XLS), "source workbook is not on this machine")
class TestPerfectMedicalSourceWorkbook(unittest.TestCase):
    def test_page_126_source_rows(self):
        with open(_SOURCE_XLS, "rb") as handle:
            result = extract_sales_statement(handle.read(), os.path.basename(_SOURCE_XLS))
        items = result["line_items"]
        extra = result["totals"]["extra"]
        self.assertEqual(extra["extraction_method"], "item_pack_sreturn_others")
        self.assertEqual(len(items), 42)
        self.assertEqual(extra["zero_qty_rows"], 12)
        self.assertEqual(extra["qty_mismatch_rows"], 0)
        self.assertEqual(extra["qty_parse_error_rows"], 0)
        self.assertEqual(extra["stock_validation"]["calculated_closing"], 983)
        self.assertTrue(extra["stock_validation"]["is_valid"])
        rows = _by_name_pack(result)
        self.assertEqual(rows[("ARJUNA TAB", "60TAB")]["opening_qty"], 40)
        self.assertEqual(rows[("ARJUNA TAB", "60TAB")]["sales_qty"], 10)
        self.assertEqual(rows[("ARJUNA TAB", "60TAB")]["closing_qty"], 30)
        ds = rows[("LIV 52 DS 200ML", "200 ML")]
        self.assertEqual(ds["opening_qty"], 416)
        self.assertEqual(ds["sales_qty"], 411)
        self.assertEqual(ds["closing_qty"], 5)
        ds_tab = rows[("LIV 52 DS TAB", "60 TAB")]
        self.assertEqual(ds_tab["opening_qty"], 2)
        self.assertEqual(ds_tab["purchase_qty"], 1046)
        self.assertEqual(ds_tab["sales_qty"], 994)
        self.assertEqual(ds_tab["others_out_qty"], 46)
        self.assertEqual(ds_tab["closing_qty"], 8)
        self.assertEqual(ds_tab["extra"]["expected_total"], 1048)
        self.assertEqual(ds_tab["extra"]["expected_closing"], 8)
        self.assertTrue(ds_tab["extra"]["stock_identity_ok"])
        self.assertTrue(ds_tab["extra"]["validation"]["is_valid"])
        syp = rows[("LIV 52 SYP", "200 ML")]
        self.assertEqual(syp["opening_qty"], 0)
        self.assertEqual(syp["purchase_qty"], 360)
        self.assertEqual(syp["sales_qty"], 296)
        self.assertEqual(syp["others_out_qty"], 10)
        self.assertEqual(syp["closing_qty"], 54)
        self.assertEqual(syp["extra"]["expected_closing"], 54)
        self.assertTrue(syp["extra"]["stock_identity_ok"])
        liv = rows[("LIV 52 TAB", "100 TAB")]
        self.assertEqual(liv["opening_qty"], 1000)
        self.assertEqual(liv["sales_qty"], 886)
        self.assertEqual(liv["closing_qty"], 114)
        for name, pack in (
            ("BONNISAN DROPS", "30ML"),
            ("CYSTONE SYP", "200 ML"),
            ("CYSTONE TAB", "1*60TAB"),
            ("GUDUCHI TAB", "60 TABS"),
            ("LIV 52 DS SYP", "100 ML"),
            ("LIV.52 DS S.F SYP 200ML", "200ML"),
            ("LUKOL SYP", "200 ML"),
            ("PILEX FORTE OINT", "30 GM"),
            ("PILEX TAB", "60 TABS"),
            ("SEPTILIN SYP", "200 ML"),
            ("SERADIC-P TAB", "10 TABS"),
            ("SPEMAN TAB", "60 TAB"),
        ):
            item = rows[(name, pack)]
            self.assertEqual(item["source_product_name"], name)
            self.assertEqual(
                [
                    item[field]
                    for field in (
                        "opening_qty",
                        "purchase_qty",
                        "sales_return_qty",
                        "others_in_qty",
                        "subtotal_qty",
                        "sales_qty",
                        "purchase_return_qty",
                        "others_out_qty",
                        "closing_qty",
                    )
                ],
                [0, 0, 0, 0, 0, 0, 0, 0, 0],
            )


if __name__ == "__main__":
    unittest.main()
