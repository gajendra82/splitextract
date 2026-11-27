"""Marg ITEM DESCRIPTION sheet with QTY and VALUE columns plus DUMP."""

from __future__ import annotations

import unittest
from pathlib import Path

from services.sales_statement_extractor import (
    _find_marg_erp_xls_header,
    _find_marg_qty_value_dump_xls_header,
    _parse_marg_closing_mexp_xls,
    _parse_marg_opening_receipt_issue,
    _parse_marg_qty_value_dump_xls,
    extract_sales_statement,
)

PLAIN = [
    ["SOME MEDICAL"],
    ["ITEM DESCRIPTION", "OPENING", "RECEIPT", "ISSUE", "CLOSING"],
    ["ABANA TAB              50'S", 10, 0, 2, 8],
]

MEXP_ROWS = [
    ["BHARAT MEDICALS"],
    ["STOCK & SALES ANALYSIS  (HIMALAYA ZEAL) 01-08-2026 - 31-08-2026"],
    ["ITEM DESCRIPTION", "OPENING", "RECEIPT", "ISSUE", "CLOSING M.EXP"],
    ["ABANA TAB              50'S.", 57, 0, 2, "    55  6/27"],
]

# Layout family: OPENING/RECEIPT/ISSUE/CLOSING × QTY|VALUE + DUMP, with Marg's
# "QTY. M.EXP" glue under DUMP (must not fall through to generic Marg).
QTY_MEXP_DUMP_ROWS = [
    ["BHATIA MEDIWAYS"],
    ["ONE HALL ON SECOND FLOOR"],
    ["Phone : 9999999930"],
    ["STOCK & SALES ANALYSIS  (HIMALAYA WELLNESS COMPANY) 01-10-2026 - 31-10-2026"],
    [
        "ITEM DESCRIPTION",
        "   OPENING",
        "",
        "   RECEIPT",
        "",
        "    ISSUE",
        "",
        "   CLOSING",
        "",
        "DUMP",
    ],
    [
        "",
        "QTY.",
        "VALUE",
        "QTY.",
        "VALUE",
        "QTY.",
        "VALUE",
        "QTY.",
        "VALUE",
        "QTY. M.EXP",
    ],
    [],
    ["HIMALAYA WELLNESS COMPANY"],
    ["AACTARIL SOAP 75G             75G", "  -", 0.0, "  -", 0.0, "  -", 0.0, "  -", 0.0, "  -"],
    ["ABANA TAB                     60TAB", 43.0, 5634.29, "  -", 0.0, 2.0, 270.5, 41.0, 5372.23, " 41  1/27"],
    ["LIV-52 DS                     60TAB", 45.0, 8541.0, "  -", 0.0, 36.0, 7351.11, 9.0, 1708.2, "  7  3/29"],
    ["PLATENZA TAB                  3*20", 2.0, 213.3, "  -", 0.0, 2.0, 221.37, "  -", 0.0, "  -"],
    ["TOTAL", 1785.0, 243729.35, 0.0, 0.0, 79.0, 15166.3, 1706.0, 229889.7, 0.0],
]

FIXTURE = Path("0000725772_2026_08_ZA_24_8137_03092026174341.xlsx")
LIVE_REPORT_1 = Path(r"C:\Users\adity\Downloads\REPORT_1.XLS")
LIVE_ZANDRA_CODE = Path(r"C:\Users\adity\Downloads\ZANDRA STM OCT26.XLS")

# Same OPENING/RECEIPT/ISSUE/CLOSING×QTY|VALUE+DUMP family with a leading CODE
# column (Marg often exports CODE before ITEM DESCRIPTION).
CODE_QTY_VALUE_DUMP_ROWS = [
    ["PADMAVATI DISTRIBUTORS"],
    ["SHOP NO. 42, CSC-8,SECTOR-3, ROHINI,NEW DELHI-110085"],
    ["Phone : 011-45629923"],
    ["STOCK & SALES ANALYSIS  (HIM ZANDRA) 01-10-2026 - 31-10-2026"],
    [
        "CODE",
        "ITEM DESCRIPTION",
        "   OPENING",
        "",
        "   RECEIPT",
        "",
        "    ISSUE",
        "",
        "   CLOSING",
        "",
        "DUMP",
    ],
    [
        "",
        "",
        "QTY.",
        "VALUE",
        "QTY.",
        "VALUE",
        "QTY.",
        "VALUE",
        "QTY.",
        "VALUE",
        "QTY.",
    ],
    [],
    ["HIM ZANDRA", "", "", "", "", "", "", "", "", "", ""],
    [
        "]ZIF1945",
        "     HIM.EVECARE SYP.   400ML",
        3.0,
        460.76,
        "  -",
        0.0,
        1.0,
        228.07,
        2.0,
        307.17,
        2.0,
    ],
    [
        "]ZIF707",
        "     HIM HARIDRA CAP    60CAP",
        15.0,
        1885.76,
        "  -",
        0.0,
        2.0,
        414.32,
        13.0,
        1634.32,
        13.0,
    ],
    [
        "00000081",
        "     HIM LIV52 DS TAB.  60",
        29.0,
        5339.07,
        "  -",
        0.0,
        14.0,
        2735.4,
        15.0,
        2761.59,
        15.0,
    ],
    ["TOTAL", "", 47.0, 7685.59, "  -", 0.0, 17.0, 3377.79, 30.0, 4703.08, 0.0],
]


class TestMargQtyValueDump(unittest.TestCase):
    def test_older_marg_headers_are_not_this_layout(self):
        self.assertIsNone(
            _parse_marg_qty_value_dump_xls(PLAIN, "plain.xls", ".xls", "Sheet1")
        )
        self.assertIsNone(
            _parse_marg_qty_value_dump_xls(MEXP_ROWS, "mexp.xls", ".xls", "Sheet1")
        )
        self.assertTrue(
            _parse_marg_opening_receipt_issue(PLAIN, "plain.xls", ".xls", "Sheet1")
        )
        self.assertTrue(
            _parse_marg_closing_mexp_xls(MEXP_ROWS, "mexp.xls", ".xls", "Sheet1")
        )

    def test_dump_qty_mexp_subheader_maps_issue_to_sales(self):
        self.assertEqual(_find_marg_qty_value_dump_xls_header(QTY_MEXP_DUMP_ROWS), 4)
        self.assertIsNone(_find_marg_erp_xls_header(QTY_MEXP_DUMP_ROWS))
        parsed = _parse_marg_qty_value_dump_xls(
            QTY_MEXP_DUMP_ROWS, "REPORT_1.XLS", ".xls", "Sheet1"
        )
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(
            parsed["totals"]["extra"]["extraction_method"], "marg_qty_value_dump_xls"
        )
        self.assertEqual(parsed["stockist_name"], "BHATIA MEDIWAYS")
        self.assertEqual(parsed["company_name"], "HIMALAYA WELLNESS COMPANY")
        self.assertEqual(parsed["period_from"], "2026-10-01")
        self.assertEqual(parsed["period_to"], "2026-10-31")
        names = [item["product_name"] for item in parsed["line_items"]]
        self.assertNotIn("HIMALAYA WELLNESS COMPANY", names)
        by = {i["product_name"]: i for i in parsed["line_items"]}
        abana = by["ABANA TAB"]
        self.assertEqual(abana["packing"], "60TAB")
        self.assertEqual(abana["opening_qty"], 43.0)
        self.assertEqual(abana["sales_qty"], 2.0)
        self.assertEqual(abana["sales_value"], 270.5)
        self.assertEqual(abana["closing_qty"], 41.0)
        self.assertEqual(abana["closing_value"], 5372.23)
        self.assertEqual(abana["extra"]["dump_qty"], 41.0)
        liv = by["LIV-52 DS"]
        self.assertEqual(liv["sales_qty"], 36.0)
        self.assertEqual(liv["sales_value"], 7351.11)
        plat = by["PLATENZA TAB"]
        self.assertEqual(plat["sales_qty"], 2.0)
        self.assertEqual(plat["sales_value"], 221.37)
        self.assertEqual(parsed["totals"]["sales_value"], 15166.3)
        self.assertEqual(parsed["totals"]["closing_value"], 229889.7)
        self.assertEqual(parsed["totals"]["extra"]["sales_qty"], 79.0)

    def test_leading_code_column_maps_issue_to_sales(self):
        self.assertEqual(_find_marg_qty_value_dump_xls_header(CODE_QTY_VALUE_DUMP_ROWS), 4)
        self.assertIsNone(_find_marg_erp_xls_header(CODE_QTY_VALUE_DUMP_ROWS))
        parsed = _parse_marg_qty_value_dump_xls(
            CODE_QTY_VALUE_DUMP_ROWS, "ZANDRA STM OCT26.XLS", ".xls", "Sheet1"
        )
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(
            parsed["totals"]["extra"]["extraction_method"], "marg_qty_value_dump_xls"
        )
        self.assertEqual(parsed["totals"]["extra"].get("name_col"), 1)
        self.assertEqual(parsed["stockist_name"], "PADMAVATI DISTRIBUTORS")
        self.assertEqual(parsed["company_name"], "HIM ZANDRA")
        self.assertEqual(parsed["period_from"], "2026-10-01")
        self.assertEqual(parsed["period_to"], "2026-10-31")
        names = [item["product_name"] for item in parsed["line_items"]]
        self.assertNotIn("HIM ZANDRA", names)
        self.assertEqual(len(parsed["line_items"]), 3)
        by = {i["product_name"]: i for i in parsed["line_items"]}
        evecare = by["HIM.EVECARE SYP."]
        self.assertEqual(evecare["packing"], "400ML")
        self.assertEqual(evecare["sales_qty"], 1.0)
        self.assertEqual(evecare["sales_value"], 228.07)
        self.assertEqual(evecare["closing_qty"], 2.0)
        self.assertEqual(evecare["extra"]["product_code"], "]ZIF1945")
        haridra = by["HIM HARIDRA CAP"]
        self.assertEqual(haridra["sales_qty"], 2.0)
        self.assertEqual(haridra["sales_value"], 414.32)
        liv = by["HIM LIV52 DS TAB."]
        self.assertEqual(liv["sales_qty"], 14.0)
        self.assertEqual(liv["sales_value"], 2735.4)
        self.assertEqual(parsed["totals"]["sales_value"], 3377.79)
        self.assertEqual(parsed["totals"]["closing_value"], 4703.08)
        self.assertEqual(parsed["totals"]["extra"]["sales_qty"], 17.0)

    def test_bombay_medical_store_workbook(self):
        if not FIXTURE.is_file():
            self.skipTest("workbook is not in the workspace")
        result = extract_sales_statement(FIXTURE.read_bytes(), FIXTURE.name)
        items = result["line_items"]
        self.assertEqual(
            (result.get("totals") or {}).get("extra", {}).get("extraction_method"),
            "marg_qty_value_dump_xls",
        )
        self.assertEqual(result["stockist_name"], "BOMBAY MEDICAL STORE")
        self.assertIn("HARDOI", result["stockist_address"])
        self.assertEqual(result["company_name"], "HIMALAYA HEALTHCARE (ZANDRA AMIT)")
        self.assertEqual(result["period_from"], "2026-08-01")
        self.assertEqual(result["period_to"], "2026-08-29")
        self.assertEqual(len(items), 35)
        self.assertNotIn("HIMALAYA HEALTHCARE", [item["product_name"] for item in items])
        first = items[0]
        self.assertEqual(first["product_name"], "BONNISAN DROP")
        self.assertEqual(first["packing"], "30ML")
        self.assertEqual(first["opening_qty"], 27)
        self.assertEqual(first["sales_qty"], 3)
        self.assertEqual(first["sales_value"], 197.1)
        self.assertEqual(first["closing_qty"], 24)
        self.assertEqual(first["closing_value"], 1791.84)
        self.assertEqual(first["extra"]["dump_qty"], 7)
        hiora = next(item for item in items if item["product_name"] == "HIORA PASTE")
        self.assertEqual(hiora["packing"], "100 GM")
        cystone = next(item for item in items if item["product_name"] == "CYSTONE FORTE TAB")
        self.assertEqual(cystone["packing"], "1X60 1X60")
        septillin = next(item for item in items if item["product_name"] == "SEPTILLIN SYP")
        self.assertEqual(septillin["sales_qty"], -4)
        self.assertEqual(septillin["closing_qty"], 21)
        self.assertEqual(result["totals"]["sales_value"], 224760.21)
        self.assertEqual(result["totals"]["closing_value"], 109150.03)
        self.assertEqual(result["totals"]["extra"]["opening_qty"], 695)
        self.assertEqual(result["totals"]["extra"]["sales_qty"], 1302)
        self.assertEqual(result["totals"]["extra"]["closing_qty"], 703)
        self.assertAlmostEqual(
            sum(item["sales_value"] for item in items), 224760.21, places=2
        )
        self.assertAlmostEqual(
            sum(item["opening_qty"] + item["receipts_qty"] - item["sales_qty"] for item in items),
            sum(item["closing_qty"] for item in items),
            places=2,
        )


@unittest.skipUnless(LIVE_REPORT_1.is_file(), "REPORT_1.XLS not on this machine")
class TestMargQtyValueDumpLiveReport1(unittest.TestCase):
    def test_report_1_xls_sales_from_issue(self):
        result = extract_sales_statement(
            LIVE_REPORT_1.read_bytes(), LIVE_REPORT_1.name
        )
        self.assertEqual(
            result["totals"]["extra"]["extraction_method"],
            "marg_qty_value_dump_xls",
        )
        sales = [
            i for i in result["line_items"] if (i.get("sales_qty") or 0) > 0
        ]
        self.assertGreaterEqual(len(sales), 9)
        abana = next(
            i
            for i in result["line_items"]
            if str(i.get("product_name") or "").startswith("ABANA TAB")
        )
        self.assertEqual(abana["sales_qty"], 2.0)
        self.assertEqual(abana["sales_value"], 270.5)
        self.assertEqual(result["totals"]["sales_value"], 15166.3)
        self.assertEqual(result["totals"]["extra"]["sales_qty"], 79.0)


@unittest.skipUnless(LIVE_ZANDRA_CODE.is_file(), "ZANDRA STM OCT26.XLS not on this machine")
class TestMargQtyValueDumpLiveZandraCode(unittest.TestCase):
    def test_zandra_code_column_xls_sales_from_issue(self):
        result = extract_sales_statement(
            LIVE_ZANDRA_CODE.read_bytes(), LIVE_ZANDRA_CODE.name
        )
        self.assertEqual(
            result["totals"]["extra"]["extraction_method"],
            "marg_qty_value_dump_xls",
        )
        self.assertEqual(result["totals"]["extra"].get("name_col"), 1)
        self.assertEqual(result["stockist_name"], "PADMAVATI DISTRIBUTORS")
        self.assertEqual(result["company_name"], "HIM ZANDRA")
        sales = [
            i for i in result["line_items"] if (i.get("sales_qty") or 0) > 0
        ]
        self.assertEqual(len(sales), 7)
        liv = next(
            i
            for i in result["line_items"]
            if str(i.get("product_name") or "").startswith("HIM LIV52 DS TAB")
        )
        self.assertEqual(liv["sales_qty"], 14.0)
        self.assertEqual(liv["sales_value"], 2735.4)
        self.assertEqual(result["totals"]["sales_value"], 4560.73)
        self.assertEqual(result["totals"]["extra"]["sales_qty"], 22.0)
        self.assertEqual(result["totals"]["closing_value"], 79565.13)


if __name__ == "__main__":
    unittest.main()
