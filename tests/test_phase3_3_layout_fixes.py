"""Layout-scoped fixes from the Phase 3.2 audit. The generic equation stays put."""

from __future__ import annotations

import io
import unittest

from openpyxl import Workbook

from services.sales_statement_extractor import (
    _apply_stock_identity_validation,
    _find_marg_erp_xls_header,
    _is_op_pur_orec_sale_oiss_text,
    _parse_marg_erp_xls,
    _parse_op_pur_orec_sale_oiss,
    _parse_profitmaker_ostk_docx,
    extract_sales_statement,
)
from services.stock_identity_profiles import (
    generic_expected_closing,
    issues_expected_closing,
    op_pur_orec_expected_closing,
    select_profile,
)


def _orec_text(rows):
    header = [
        "#",
        "Item Name",
        "Op.Qty.",
        "Op.Val.",
        "Pur.Qty.",
        "Pur.Val.",
        "ORec.Qty.",
        "ORec.Val.",
        "Sale.Qty.",
        "Sale.Val.",
        "OIss.Qty.",
        "OIss.Val.",
        "Cl.Qty.",
        "Cl.Val.",
    ]
    lines = list(header)
    for index, name, values in rows:
        lines.append(str(index))
        lines.append(name)
        lines.extend(values)
    return "\n".join(lines)


class TestOpPurOrecLayout(unittest.TestCase):
    def test_aactaril_identity_keeps_orec_off_receipts(self):
        text = _orec_text(
            [
                (
                    1,
                    "AACTARIL SOAP",
                    ["40", "3101.65", "-", "-", "2", "-", "11", "963.82", "-", "-", "31", "2247.17"],
                )
            ]
        )
        result = _parse_op_pur_orec_sale_oiss(text, "statement.pdf", "pdf")
        result = _apply_stock_identity_validation(result)
        item = result["line_items"][0]
        self.assertEqual(item["opening_qty"], 40.0)
        self.assertEqual(item["receipts_qty"], 0.0)
        self.assertEqual(item["extra"]["other_receipt_qty"], 2.0)
        self.assertEqual(item["sales_qty"], 11.0)
        self.assertEqual(item["extra"]["other_issue_qty"], 0.0)
        self.assertEqual(item["closing_qty"], 31.0)
        self.assertEqual(op_pur_orec_expected_closing(item), 31.0)
        self.assertNotEqual(generic_expected_closing(item), 31.0)
        self.assertEqual(select_profile(result).profile_id, "op_pur_orec_sale_oiss")
        self.assertTrue(result["totals"]["extra"]["stock_validation"]["is_valid"])

    def test_oiss_nonzero_and_both_zero(self):
        text = _orec_text(
            [
                (1, "WITH ISSUE", ["10", "0", "1", "0", "3", "0", "4", "0", "2", "0", "8", "0"]),
                (2, "BOTH ZERO", ["5", "0", "0", "0", "-", "0", "1", "0", "-", "0", "4", "0"]),
            ]
        )
        result = _apply_stock_identity_validation(
            _parse_op_pur_orec_sale_oiss(text, "statement.pdf", "pdf")
        )
        issue, zero = result["line_items"]
        self.assertEqual(issue["extra"]["other_issue_qty"], 2.0)
        self.assertEqual(op_pur_orec_expected_closing(issue), 8.0)
        self.assertEqual(zero["extra"]["other_receipt_qty"], 0.0)
        self.assertEqual(zero["extra"]["other_issue_qty"], 0.0)
        self.assertEqual(op_pur_orec_expected_closing(zero), 4.0)
        self.assertTrue(result["totals"]["extra"]["stock_validation"]["is_valid"])

    def test_null_other_receipt_is_not_zero_and_not_receipts(self):
        item = {
            "opening_qty": 40.0,
            "receipts_qty": 0.0,
            "sales_qty": 11.0,
            "closing_qty": 31.0,
            "extra": {"other_receipt_qty": None, "other_issue_qty": 0.0},
        }
        self.assertIsNone(op_pur_orec_expected_closing(item))
        self.assertEqual(item["receipts_qty"], 0.0)

    def test_unrelated_statement_stays_off_this_layout(self):
        text = "Item Name\nOp.Qty.\nSale.Qty.\nCl.Qty.\n1\nABANA\n10\n2\n8\n"
        self.assertFalse(_is_op_pur_orec_sale_oiss_text(text))
        self.assertIsNone(_parse_op_pur_orec_sale_oiss(text, "other.pdf", "pdf"))

    def test_reprinted_header_does_not_drop_later_rows(self):
        first = _orec_text(
            [(1, "AACTARIL SOAP", ["40", "0", "-", "0", "2", "0", "11", "0", "-", "0", "31", "0"])]
        )
        second = _orec_text(
            [(2, "ABANA TAB", ["104", "0", "-", "0", "2", "0", "49", "0", "-", "0", "57", "0"])]
        )
        text = first + "\n12/2027\n-\n" + second
        result = _apply_stock_identity_validation(
            _parse_op_pur_orec_sale_oiss(text, "statement.pdf", "pdf")
        )
        names = [item["product_name"] for item in result["line_items"]]
        self.assertEqual(names, ["AACTARIL SOAP", "ABANA TAB"])
        self.assertTrue(result["totals"]["extra"]["stock_validation"]["is_valid"])


class TestSaleRetQtyMapping(unittest.TestCase):
    def _workbook(self, rows):
        book = Workbook()
        sheet = book.active
        sheet.append(
            [
                "Product_Desc",
                "Opening_Qty",
                "Purchase_Qty",
                "Sale_Qty",
                "Sale_Ret_Qty",
                "Other_Qty",
                "Closing_Qty",
            ]
        )
        for row in rows:
            sheet.append(row)
        buffer = io.BytesIO()
        book.save(buffer)
        return buffer.getvalue()

    def test_herbolax_uses_saleret_and_keeps_other_qty(self):
        payload = self._workbook(
            [
                ["HERBOLAX TABS", 47, 0, 4, 1, 0, 44],
                ["ZERO RETURN", 10, 0, 2, 0, 0, 8],
                ["VASAKA SYRUP", 55, 0, 0, 0, -27, 28],
            ]
        )
        result = extract_sales_statement(payload, "stock.xlsx")
        by_name = {item["product_name"]: item for item in result["line_items"]}
        herbolax = by_name["HERBOLAX TABS"]
        self.assertEqual(herbolax["extra"]["sale_return"], 1.0)
        self.assertEqual(herbolax["extra"]["other_qty"], 0.0)
        self.assertNotIn("stock_out_qty", herbolax["extra"])
        self.assertTrue(herbolax["extra"]["stock_identity_ok"])
        self.assertEqual(
            result["totals"]["extra"]["stock_identity_kind"],
            "opening_purchase_sale_saleret_expdmg",
        )
        zero = by_name["ZERO RETURN"]
        self.assertEqual(zero["extra"]["sale_return"], 0.0)
        self.assertTrue(zero["extra"]["stock_identity_ok"])
        vasaka = by_name["VASAKA SYRUP"]
        self.assertEqual(vasaka["extra"]["other_qty"], -27.0)
        self.assertNotIn("stock_out_qty", vasaka["extra"])
        self.assertNotEqual(vasaka["extra"]["other_qty"], vasaka["extra"]["sale_return"])
        self.assertFalse(vasaka["extra"]["stock_identity_ok"])


class TestMargPurchaseDetailBoundary(unittest.TestCase):
    def _rows(self, extra):
        header = [
            "PRODUCT DESCRIPTION",
            "OPENING STOCK",
            "OPENING VALUE",
            "RECEIVE QUANTITY",
            "RECEIVE VALUE",
            "ISSUE QUANTITY",
            "ISSUE VALUE",
            "CLOSING STOCK",
            "CLOSING VALUE",
        ]
        stock = [
            ["AACTARIL SOAP", 40, 1, 0, 0, 0, 0, 40, 1],
            ["TOTAL", 40, 1, 0, 0, 0, 0, 40, 1],
        ]
        return [["S.K.MEDICOSE"]] + [header] + stock + extra

    def test_purchase_detail_is_not_a_stock_row(self):
        rows = self._rows(
            [
                ["PURCHASE DETAIL"],
                ["SUPPLIER NAME", "INVOICE NO.", "DATE", "RECEIVE DATE", "AMOUNT"],
                ["HIMALAYA WELLNESS COM INDORE", "5126107167", "21-08-2026", "21-08-2026", 93636],
            ]
        )
        found = _find_marg_erp_xls_header(rows)
        result = _apply_stock_identity_validation(
            _parse_marg_erp_xls(rows, "stock.xls", "xls", found)
        )
        names = [item["product_name"] for item in result["line_items"]]
        self.assertEqual(names, ["AACTARIL SOAP"])
        self.assertNotIn(5126107167, [item["opening_qty"] for item in result["line_items"]])
        self.assertTrue(result["totals"]["extra"]["stock_validation"]["is_valid"])

    def test_second_stock_section_after_total_still_parses(self):
        rows = self._rows(
            [
                [
                    "PRODUCT DESCRIPTION",
                    "OPENING STOCK",
                    "OPENING VALUE",
                    "RECEIVE QUANTITY",
                    "RECEIVE VALUE",
                    "ISSUE QUANTITY",
                    "ISSUE VALUE",
                    "CLOSING STOCK",
                    "CLOSING VALUE",
                ],
                ["LIV 52 TAB", 10, 1, 5, 1, 3, 1, 12, 1],
            ]
        )
        found = _find_marg_erp_xls_header(rows)
        result = _parse_marg_erp_xls(rows, "stock.xls", "xls", found)
        names = [item["product_name"] for item in result["line_items"]]
        self.assertEqual(names, ["AACTARIL SOAP", "LIV 52 TAB"])


class TestProfitmakerParagraphHeader(unittest.TestCase):
    def test_header_above_table_maps_qoh_and_keeps_value_monetary(self):
        from docx import Document

        doc = Document()
        doc.add_paragraph("Stock & Sales Statement")
        doc.add_paragraph(
            "Product NamePacking O.StkPurcTotSaleQohValue Age"
        )
        table = doc.add_table(rows=1, cols=9)
        cells = ["ABANA TABLETS", "60 TAB", "7", "0", "7", "2", "5", "756.80", "148"]
        for index, value in enumerate(cells):
            table.rows[0].cells[index].text = value
        buffer = io.BytesIO()
        doc.save(buffer)
        result = extract_sales_statement(buffer.getvalue(), "profitmaker.docx")
        item = result["line_items"][0]
        self.assertEqual(result["totals"]["extra"]["extraction_method"], "profitmaker_ostk_docx")
        self.assertEqual(item["opening_qty"], 7.0)
        self.assertEqual(item["receipts_qty"], 0.0)
        self.assertEqual(item["sales_qty"], 2.0)
        self.assertEqual(item["closing_qty"], 5.0)
        self.assertEqual(item["closing_value"], 756.80)
        self.assertEqual(item["extra"]["age_days"], 148.0)
        self.assertNotIn("sales_scheme", item["extra"])
        self.assertNotEqual(item["sales_value"], 756.80)
        self.assertTrue(result["totals"]["extra"]["stock_validation"]["is_valid"])

    def test_company_line_between_header_and_merged_table(self):
        from docx import Document

        doc = Document()
        doc.add_paragraph("Product NamePacking O.StkPurcTotSaleQohValue Age")
        doc.add_paragraph("DAXINSOFT Company : HIMALAYA ZEAL DIVISO")
        table = doc.add_table(rows=1, cols=10)
        values = [
            "ABANA TABLETS",
            "ABANA TABLETS",
            "60 TAB",
            "7",
            "0",
            "7",
            "2",
            "5",
            "756.80",
            "148",
        ]
        for index, value in enumerate(values):
            table.rows[0].cells[index].text = value
        table.rows[0].cells[0].merge(table.rows[0].cells[1])
        buffer = io.BytesIO()
        doc.save(buffer)
        result = _parse_profitmaker_ostk_docx(buffer.getvalue(), "profitmaker.docx")
        item = result["line_items"][0]
        self.assertEqual(item["opening_qty"], 7.0)
        self.assertEqual(item["receipts_qty"], 0.0)
        self.assertEqual(item["sales_qty"], 2.0)
        self.assertEqual(item["closing_qty"], 5.0)
        self.assertEqual(item["closing_value"], 756.80)
        self.assertNotEqual(item["sales_value"], 756.80)

    def test_unrelated_paragraph_between_header_and_table_is_not_claimed(self):
        from docx import Document

        doc = Document()
        doc.add_paragraph("Product NamePacking O.StkPurcTotSaleQohValue Age")
        doc.add_paragraph("This is a different report")
        table = doc.add_table(rows=1, cols=9)
        for index, value in enumerate(
            ["ABANA TABLETS", "60 TAB", "7", "0", "7", "2", "5", "756.80", "148"]
        ):
            table.rows[0].cells[index].text = value
        buffer = io.BytesIO()
        doc.save(buffer)
        self.assertIsNone(
            _parse_profitmaker_ostk_docx(buffer.getvalue(), "other.docx")
        )

    def test_headerless_table_without_profitmaker_paragraph_is_not_claimed(self):
        from docx import Document

        doc = Document()
        doc.add_paragraph("Some other report")
        table = doc.add_table(rows=1, cols=4)
        for index, value in enumerate(["ABANA", "10", "2", "8"]):
            table.rows[0].cells[index].text = value
        buffer = io.BytesIO()
        doc.save(buffer)
        self.assertIsNone(
            _parse_profitmaker_ostk_docx(buffer.getvalue(), "other.docx")
        )


class TestProductWiseFreeStaysSeparate(unittest.TestCase):
    def test_free_is_not_folded_into_canonical_quantities(self):
        item = {
            "opening_qty": 0.0,
            "receipts_qty": 48.0,
            "sales_qty": 50.0,
            "closing_qty": 0.0,
            "extra": {"receipts_free": 2.0, "issues_free_repl": 0.0},
        }
        self.assertEqual(item["receipts_qty"], 48.0)
        self.assertEqual(generic_expected_closing(item), -2.0)
        self.assertEqual(issues_expected_closing(item), -2.0)


if __name__ == "__main__":
    unittest.main()
