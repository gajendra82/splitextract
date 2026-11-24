"""Regressions for the August 2026 10-document audit.

Each case is a synthetic stand-in for a confirmed mismatch. The Downloads
folder is not required.
"""

from __future__ import annotations

import io
import os
import unittest
from unittest.mock import patch

from docx import Document
from PIL import Image

from services.base_validator import validate_sales_result
from services.document_intelligence.decisions import (
    RECOVERED_AND_VERIFIED,
    UNRESOLVED,
    VERIFIED,
    decide_final_status,
)
from services.document_intelligence.verification import independently_verify
from services.ocr.orchestrator import OCROrchestrator
from services.ocr.paddle_vl_engine import reset_paddle_init_state
from services.sales_statement_extractor import (
    _ensure_stock_qty_value_fields,
    _maybe_apply_phase2_recovery,
    _parse_htm,
    _parse_product_stock_report,
    _parse_pro_age_sales_stock,
    _parse_sale_closing_analysis_lines,
    _xls_header_colmap,
    empty_line_item,
)
from services.source_grounding import (
    _rows_from_matrix,
    _rows_from_text,
    apply_source_grounding,
)


def _balanced_statement():
    item = empty_line_item()
    item["product_name"] = "BONNISAN DROPS"
    item["packing"] = "30ML"
    item["opening_qty"] = 6.0
    item["receipts_qty"] = 0.0
    item["sales_qty"] = 1.0
    item["closing_qty"] = 5.0
    item["extra"] = {}
    return {
        "source_file": "statement.doc",
        "source_format": "doc",
        "line_items": [item],
        "totals": {
            "sales_value": 0.0,
            "closing_value": 8189.23,
            "extra": {
                "extraction_method": "legacy_doc_text",
                "stock_validation": {"is_valid": True, "fail_count": 0},
                "stock_identity_fail_count": 0,
                "source_grounding": {
                    "status": "insufficient_source_evidence",
                    "reader": ".doc",
                    "discrepancy_count": 0,
                    "blocks_verification": False,
                },
            },
        },
    }


class LegacyDocVerificationTests(unittest.TestCase):
    def test_insufficient_evidence_blocks_verified_when_identity_matches(self):
        result = _balanced_statement()
        validation = validate_sales_result(result)
        self.assertIs(validation["source_evidence"], False)
        self.assertIsNone(validation["source_agreement"])
        self.assertNotEqual(validation["status"], "pass")
        verification = independently_verify(result, validation=validation)
        self.assertIn("insufficient_source_evidence", verification["issues"])
        self.assertFalse(verification["passed"])
        decision = decide_final_status(
            validation=validation,
            verification={"passed": True},
            recovery={"status": "recovered", "accepted": True},
        )
        self.assertEqual(decision["status"], UNRESOLVED)
        self.assertFalse(decision["verified"])
        self.assertEqual(decision["reason"], "insufficient_source_evidence")
        self.assertNotEqual(decision["status"], VERIFIED)
        self.assertNotEqual(decision["status"], RECOVERED_AND_VERIFIED)

    def test_checked_source_with_no_discrepancy_can_still_verify(self):
        decision = decide_final_status(
            validation={
                "accepted": True,
                "source_evidence": True,
                "business_validation": {"status": "consistent"},
            },
            verification={"passed": True},
            recovery={},
        )
        self.assertEqual(decision["status"], VERIFIED)
        self.assertTrue(decision["verified"])

    def test_legacy_doc_headers_ground_opening_purchase_sale_and_qoh(self):
        text = (
            "Product Name              Pack      O.Stk    Purc     Tot    Sale     Qoh        Value  Age\n"
            "BONNISAN DROPS            30ML      6        0        6      1        5          100    10\n"
        )
        rows = _rows_from_text(text)
        self.assertEqual(len(rows), 1)
        self.assertEqual(float(rows[0]["opening_qty"]), 6)
        self.assertEqual(float(rows[0]["receipts_qty"]), 0)
        self.assertEqual(float(rows[0]["sales_qty"]), 1)
        self.assertEqual(float(rows[0]["closing_qty"]), 5)
        self.assertNotIn("total_stock", rows[0])


class ColumnMappingTests(unittest.TestCase):
    def test_html_sale_is_not_jun_sa_and_blank_is_not_zero(self):
        html = """
        <table>
          <tr>
            <th>Product name</th><th>Unit</th><th>Op</th><th>Purc</th><th>Sale</th>
            <th>WSale</th><th>Jul Sa</th><th>Jun Sa</th><th>Sa val</th>
            <th>Cl qty</th><th>Cl val</th>
          </tr>
          <tr>
            <td>BONNISAN LIQ</td><td>100ML</td><td>59</td><td></td><td>52</td>
            <td></td><td>25</td><td>20</td><td>3390</td><td>7</td><td>400</td>
          </tr>
          <tr>
            <td>CYSTONE</td><td>TAB</td><td>700</td><td>-</td><td>-</td>
            <td></td><td></td><td></td><td>0</td><td>700</td><td>100</td>
          </tr>
        </table>
        """
        result = _parse_htm(html.encode("utf-8"), "medivision.html")
        rows = {item["product_name"]: item for item in result["line_items"]}
        liq = rows["BONNISAN LIQ"]
        self.assertEqual(liq["sales_qty"], 52.0)
        self.assertIsNone(liq["receipts_qty"])
        self.assertEqual(liq["opening_qty"], 59.0)
        self.assertEqual(liq["closing_qty"], 7.0)
        self.assertEqual(liq["extra"]["june_sale_qty"], 20.0)
        self.assertEqual(liq["extra"]["july_sale_qty"], 25.0)
        self.assertIsNone(liq["extra"]["wholesale_qty"])
        cystone = rows["CYSTONE"]
        self.assertIsNone(cystone["sales_qty"])
        self.assertIsNone(cystone["receipts_qty"])
        self.assertEqual(cystone["sales_value"], 0.0)
        self.assertEqual(cystone["opening_qty"], 700.0)

    def test_xls_rec_iss_clos_headers_map_to_receipts_issue_and_closing(self):
        headers = [
            "item_name",
            "item_unit",
            "op_stock",
            "op_value",
            "rec_qty",
            "rec_value",
            "iss_qty",
            "iss_value",
            "clos_qty",
            "clos_value",
        ]
        colmap = _xls_header_colmap(headers)
        self.assertEqual(colmap["op"], 2)
        self.assertEqual(colmap["pur"], 4)
        self.assertEqual(colmap["sale"], 6)
        self.assertEqual(colmap["bal"], 8)
        rows = _rows_from_matrix(
            [
                headers,
                ["BONNISAN DROP 30ML", "30ML", 6, 10, 0, 0, 1, 5, 5, 20],
            ]
        )
        self.assertEqual(rows[0]["opening_qty"], 6)
        self.assertEqual(rows[0]["receipts_qty"], 0)
        self.assertEqual(rows[0]["sales_qty"], 1)
        self.assertEqual(rows[0]["closing_qty"], 5)

    def test_closestk_is_closing_and_total_is_not(self):
        text = "\n".join(
            [
                "Product Name Opening Purchase Total Sale SaleRet Exp/Dmg CloseStock",
                "HIORA SG GEL 5 50 55 1 0 0 54",
            ]
        )
        result = _parse_product_stock_report(text, "stock.png", "png")
        self.assertIsNotNone(result)
        item = result["line_items"][0]
        self.assertEqual(item["opening_qty"], 5.0)
        self.assertEqual(item["receipts_qty"], 50.0)
        self.assertEqual(item["extra"]["total_stock"], 55.0)
        self.assertEqual(item["sales_qty"], 1.0)
        self.assertEqual(item["closing_qty"], 54.0)
        self.assertNotEqual(item["closing_qty"], 55.0)

    def test_closstock_photo_uses_primary_text_when_geometry_misses(self):
        import services.sales_statement_extractor as sse

        text = "\n".join(
            [
                "Product Name Opening Purchase Total Sale SaleRet Exp/Dmg CloseStock",
                "HIORA SG GEL 5 50 55 1 0 0 54",
                "LASUNA TAB 13 0 13 0 0 0 13",
            ]
        )
        with patch.object(sse, "_ocr_image_to_text", return_value=text):
            with patch.object(sse, "_extract_psr_closstock_image", return_value=None):
                early, skip_probes = sse._maybe_early_vision_for_image(
                    b"png-bytes", "stock.png", ".png"
                )
        self.assertTrue(skip_probes)
        self.assertIsNotNone(early)
        by_name = {i["product_name"]: i for i in early["line_items"]}
        hiora = by_name["HIORA SG GEL"]
        self.assertEqual(hiora["opening_qty"], 5.0)
        self.assertEqual(hiora["receipts_qty"], 50.0)
        self.assertEqual(hiora["sales_qty"], 1.0)
        self.assertEqual(hiora["closing_qty"], 54.0)
        self.assertEqual(hiora["extra"]["total_stock"], 55.0)
        self.assertEqual(by_name["LASUNA TAB"]["closing_qty"], 13.0)

    def test_amount_total_footer_is_not_a_stock_row(self):
        text = "\n".join(
            [
                "Product Name Opening Purchase Total Sale SaleRet Exp/Dmg CloseStock",
                "HIORA SG GEL 5 50 55 1 0 0 54",
                "Amount TOTAL 241027 32536 89600 192238 273563",
            ]
        )
        result = _parse_product_stock_report(text, "stock.png", "png")
        names = [i["product_name"] for i in result["line_items"]]
        self.assertEqual(names, ["HIORA SG GEL"])
        self.assertEqual(result["line_items"][0]["closing_qty"], 54.0)

    def test_pro_age_is_not_packing(self):
        text = "\n".join(
            [
                "SALES & STOCK REPORT",
                "DESCRIPTION/PACKING    Pro. OPENING   PUR.  SALE  CR.NO. DR.NO. CLOSING",
                "                        Age    QTY.   QTY.   QTY.",
                "BLEMINOR CREAM          51      8      0      0      0      0      8",
                "BONNISAN DROPS                118      0     80      0      0     38",
                "LIV 52 DROPS 100 ML            52      0      1      0      0     51",
                "EVECARE SYRUP                  28      0     27      1      0      2",
                "LUKOL TAB                      54      0      7      0      3     44",
            ]
        )
        result = _parse_pro_age_sales_stock(text, "krishna.txt")
        rows = {item["product_name"]: item for item in result["line_items"]}
        cream = rows["BLEMINOR CREAM"]
        self.assertIsNone(cream["packing"])
        self.assertEqual(cream["extra"]["product_age"], 51.0)
        self.assertEqual(
            (cream["opening_qty"], cream["receipts_qty"], cream["sales_qty"], cream["closing_qty"]),
            (8.0, 0.0, 0.0, 8.0),
        )
        drops = rows["BONNISAN DROPS"]
        self.assertIsNone(drops["extra"]["product_age"])
        self.assertEqual(drops["opening_qty"], 118.0)
        self.assertEqual(drops["sales_qty"], 80.0)
        self.assertEqual(drops["closing_qty"], 38.0)
        liv = rows["LIV 52 DROPS 100 ML"]
        self.assertEqual(
            (liv["opening_qty"], liv["receipts_qty"], liv["sales_qty"], liv["closing_qty"]),
            (52.0, 0.0, 1.0, 51.0),
        )
        self.assertIsNone(liv["extra"]["product_age"])
        evecare = rows["EVECARE SYRUP"]
        self.assertEqual(evecare["extra"]["credit_note_qty"], 1.0)
        self.assertEqual(evecare["closing_qty"], 2.0)
        lukol = rows["LUKOL TAB"]
        self.assertEqual(lukol["extra"]["debit_note_qty"], 3.0)
        self.assertEqual(lukol["closing_qty"], 44.0)
        from services.stock_identity_profiles import failed_rows

        self.assertEqual(failed_rows(result), [])
        from services.sales_statement_extractor import _apply_stock_identity_validation

        checked = _apply_stock_identity_validation(result)
        self.assertTrue(checked["totals"]["extra"]["stock_validation"]["is_valid"])
        self.assertEqual(checked["totals"]["extra"]["stock_identity_fail_count"], 0)
        from services.source_grounding import _pro_age_observations

        observed = _pro_age_observations(text)
        self.assertEqual(observed[0]["product_name"], "BLEMINOR CREAM")
        self.assertEqual(observed[0]["opening_qty"], 8.0)
        self.assertNotIn("packing", observed[0])

    def test_docx_sale_closing_leaves_absent_opening_null(self):
        lines = [
            "LACHMI AGENCIES",
            "STOCK & SALES ANALYSIS (HIMALAYA) 01/08/2026 - 31/08/2026",
            "ITEM DESCRIPTION          PACKING        UNIT     <===SALE===>     <==CLOSING==>",
            "QTY. VALUE QTY. VALUE",
            "ARJUNA TAB                 1*60TAB         Pcs      15    3496       1     208",
            "BONNISAN DROPS             30ML            Pcs       2     100       3      50",
            "LIV 52 TAB                 60TAB           Pcs       4     200       6      80",
        ]
        result = _ensure_stock_qty_value_fields(
            _parse_sale_closing_analysis_lines(lines, "lachmi.docx")
        )
        arjuna = result["line_items"][0]
        self.assertEqual(arjuna["product_name"], "ARJUNA TAB")
        self.assertEqual(arjuna["sales_qty"], 15.0)
        self.assertEqual(arjuna["closing_qty"], 1.0)
        self.assertIsNone(arjuna["opening_qty"])
        self.assertIsNone(arjuna["receipts_qty"])
        document = Document()
        for line in lines:
            document.add_paragraph(line)
        buffer = io.BytesIO()
        document.save(buffer)
        apply_source_grounding(result, buffer.getvalue(), "lachmi.docx")
        report = result["totals"]["extra"]["source_grounding"]
        self.assertEqual(report["status"], "checked")
        self.assertEqual(report["reader"], "docx_paragraphs")
        self.assertEqual(report["discrepancy_count"], 0)
        from services.stock_identity_profiles import failed_rows

        self.assertEqual(failed_rows(result), [])
        from services.sales_statement_extractor import _apply_stock_identity_validation

        checked = _apply_stock_identity_validation(result)
        self.assertTrue(checked["totals"]["extra"]["stock_validation"]["is_valid"])
        from openpyxl import Workbook

        from services.sales_statement_extractor import extract_sales_statement

        book = Workbook()
        sheet = book.active
        sheet.append(
            [
                "ITEM", "PACK", "OPENING", "PURCHASE", "S.RETURN", "OTHERS",
                "SUB TOTAL", "SALE", "P.RETURN", "OTHERS", "CLOSING",
            ]
        )
        sheet.append(["CYSTONE TABLETS", "1X60", 700, "             -", "             -", "             -", 700, "             -", "             -", "             -", 700])
        sheet.append(["EVECARE SYRUP 200 ML", "200 ML", 196, "-", "-", "-", 196, 196, "-", "-", "-"])
        sheet.append(["EXPLICIT ZERO", "1", 0, 0, 0, 0, 0, 0, 0, 0, 0])
        buffer = io.BytesIO()
        book.save(buffer)
        result = extract_sales_statement(buffer.getvalue(), "stock.xlsx")
        rows = {item["product_name"]: item for item in result["line_items"]}
        cystone = rows["CYSTONE TABLETS"]
        self.assertEqual(cystone["opening_qty"], 700)
        self.assertIsNone(cystone["receipts_qty"])
        self.assertIsNone(cystone["sales_qty"])
        self.assertEqual(cystone["closing_qty"], 700)
        self.assertEqual(cystone["extra"]["qty_cell_state"]["receipts_qty"], "dash")
        self.assertEqual(cystone["extra"]["qty_cell_state"]["sales_qty"], "dash")
        evecare = rows["EVECARE SYRUP 200 ML"]
        self.assertEqual(evecare["opening_qty"], 196)
        self.assertEqual(evecare["sales_qty"], 196)
        self.assertIsNone(evecare["closing_qty"])
        self.assertEqual(evecare["extra"]["qty_cell_state"]["closing_qty"], "dash")
        zero = rows["EXPLICIT ZERO"]
        self.assertEqual(zero["opening_qty"], 0)
        self.assertEqual(zero["extra"]["qty_cell_state"]["opening_qty"], "explicit_zero")
        from services.stock_identity_profiles import failed_rows

        self.assertEqual(failed_rows(result), [])

    def test_html_totals_row_is_not_a_product(self):
        html = """
        <table>
          <tr><th>Product name</th><th>Unit</th><th>Op</th><th>Purc</th><th>Sale</th><th>WSale</th><th>Jul Sa</th><th>Jun Sa</th><th>Sa val</th><th>Cl qty</th><th>Cl val</th></tr>
          <tr><td>BONNISAN LIQ 100ML</td><td>100ML</td><td>59</td><td></td><td>52</td><td></td><td>25</td><td>20</td><td>3390</td><td>7</td><td>400</td></tr>
          <tr><td></td><td></td><td></td><td></td><td></td><td></td><td></td><td>Totals:</td><td>71429</td><td></td><td>155887</td></tr>
        </table>
        """
        result = _parse_htm(html.encode("utf-8"), "medivision.html")
        names = [item["product_name"] for item in result["line_items"]]
        self.assertEqual(names, ["BONNISAN LIQ 100ML"])
        apply_source_grounding(result, html.encode("utf-8"), "medivision.html")
        report = result["totals"]["extra"]["source_grounding"]
        self.assertEqual(report["discrepancy_count"], 0)
        self.assertNotIn("Totals:", [item.get("product_name") for item in report.get("discrepancies") or []])

    def test_medivision_scm_pdf_keeps_scheme_and_printed_zero(self):
        from services.sales_statement_extractor import _parse_medivision_scm_pdf
        from services.stock_identity_profiles import failed_rows

        observed = [
            {
                "product_name": "EVECARE FORTE LIQUID",
                "packing": "200ML",
                "opening_qty": "18",
                "receipts_qty": "28",
                "sales_qty": "14",
                "closing_qty": "31",
                "scheme_out_qty": "1",
            },
            {
                "product_name": "FLORASANTE CAP",
                "packing": "10'S",
                "opening_qty": "9",
                "receipts_qty": "0",
                "sales_qty": "0",
                "closing_qty": "0",
            },
            {
                "product_name": "BONNISAN DROP",
                "packing": "30ML",
                "opening_qty": "50",
                "receipts_qty": "0",
                "sales_qty": "0",
                "closing_qty": "50",
            },
        ]
        with patch(
            "services.source_grounding.medivision_scm_observations",
            return_value=observed,
        ):
            result = _parse_medivision_scm_pdf(object(), "sample.pdf")
        self.assertEqual(
            result["totals"]["extra"]["extraction_method"],
            "medivision_op_scm_pdf",
        )
        rows = {item["product_name"]: item for item in result["line_items"]}
        evecare = rows["EVECARE FORTE LIQUID"]
        self.assertEqual(evecare["closing_qty"], 31.0)
        self.assertEqual(evecare["extra"]["scheme_out_qty"], 1.0)
        florasante = rows["FLORASANTE CAP"]
        self.assertEqual(florasante["closing_qty"], 0.0)
        self.assertNotEqual(florasante["closing_qty"], florasante["opening_qty"])
        failed = {item["product_name"] for item in failed_rows(result)}
        self.assertNotIn("EVECARE FORTE LIQUID", failed)
        self.assertIn("FLORASANTE CAP", failed)

    def test_sale_scheme_changes_the_pdf_closing_equation(self):
        from services.stock_identity_profiles import medivision_scm_expected_closing

        item = {
            "opening_qty": 18,
            "receipts_qty": 28,
            "sales_qty": 14,
            "closing_qty": 31,
            "extra": {"scheme_in_qty": 0, "scheme_out_qty": 1, "sales_return_qty": 0, "add_qty": 0, "less_qty": 0},
        }
        self.assertEqual(medivision_scm_expected_closing(item), 31)

    def test_printed_closing_zero_is_still_an_identity_failure(self):
        from services.stock_identity_profiles import medivision_scm_expected_closing

        item = {
            "opening_qty": 9,
            "receipts_qty": 0,
            "sales_qty": 0,
            "closing_qty": 0,
            "extra": {
                "scheme_in_qty": 0,
                "scheme_out_qty": 0,
                "sales_return_qty": 0,
                "add_qty": 0,
                "less_qty": 0,
            },
        }
        self.assertEqual(medivision_scm_expected_closing(item), 9.0)

    def test_doc_qoh_is_not_the_value_column(self):
        from services.sales_statement_extractor import _parse_text_stock_fallback
        from services.source_grounding import _ostk_purc_qoh_observations

        text = "\n".join(
            [
                "Stock & Sales Statement",
                "Product Name              Pack      O.Stk    Purc     Tot    Sale     Qoh        Value  Age",
                "CYSTONE FORTE TAB.<STRIP> 30`S        12      15       27      12      15      1717.16   27",
                "Opening Value  :   171570.78 Sale Value    :    43496.35",
                "Purchase Value :    46376.70 Closing Value :   176820.28",
            ]
        )
        observed = _ostk_purc_qoh_observations(text)
        self.assertEqual(
            [row["product_name"] for row in observed],
            ["CYSTONE FORTE TAB.<STRIP>"],
        )
        row = observed[0]
        self.assertEqual(row["opening_qty"], "12")
        self.assertEqual(row["receipts_qty"], "15")
        self.assertEqual(row["sales_qty"], "12")
        self.assertEqual(row["closing_qty"], "15")
        parsed = _parse_text_stock_fallback(text, "stock.doc")
        with patch(
            "services.sales_statement_extractor._extract_legacy_doc_text",
            return_value=text,
        ):
            apply_source_grounding(parsed, b"doc", "stock.doc")
        report = parsed["totals"]["extra"]["source_grounding"]
        self.assertEqual(report["discrepancy_count"], 0)
        self.assertEqual(report["missing_rows"], 0)

    def setUp(self):
        reset_paddle_init_state()

    def tearDown(self):
        reset_paddle_init_state()
        os.environ.pop("CUDA_VISIBLE_DEVICES", None)

    def test_cudnn_failure_retries_cpu_once_then_stops(self):
        calls = []

        def fake_serve(_python, _image, *, page, device, timeout_s):
            del page, timeout_s
            calls.append(device)
            if "tensor" in str(device):
                error = "partially initialized module 'paddle' has no attribute 'tensor'"
            else:
                error = "[WinError 127] cudnn_cnn64_9.dll The specified procedure could not be found"
            return {"status": "error", "error": error}

        image = io.BytesIO()
        Image.new("RGB", (8, 8), "white").save(image, format="PNG")
        payload = image.getvalue()
        reset_paddle_init_state()
        with patch.dict(
            os.environ,
            {
                "OCR_ENGINE": "paddleocr-vl",
                "PADDLE_VL_ALLOW_INPROCESS": "",
                "PADDLE_VL_WORKER_CHILD": "",
            },
            clear=False,
        ):
            with patch("services.ocr.paddle_vl_engine._serve_request", side_effect=fake_serve):
                orchestrator = OCROrchestrator()
                for _ in range(66):
                    bundle = orchestrator.recognize(payload, page=1, use_fallback=False)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1], "cpu")
        self.assertEqual(bundle.metadata.get("status"), "ocr_error")
        self.assertTrue(bundle.primary.metadata.get("persistent_init_failure"))

    def test_blocked_crop_does_not_call_the_orchestrator(self):
        from services.ocr.paddle_vl_engine import _mark_persistent_init_failure
        from services.ocr.runtime import recognize_image

        _mark_persistent_init_failure("cudnn_cnn64_9.dll")
        image = io.BytesIO()
        Image.new("RGB", (8, 8), "white").save(image, format="PNG")
        with patch("services.ocr.orchestrator.OCROrchestrator.recognize") as recognize:
            for _ in range(66):
                bundle = recognize_image(image.getvalue(), use_fallback=False)
        recognize.assert_not_called()
        self.assertTrue(bundle.metadata.get("persistent_init_failure"))


class Phase2EnvTests(unittest.TestCase):
    def test_explicit_process_env_is_not_rewritten(self):
        item = empty_line_item()
        item["product_name"] = "AMOX"
        item["opening_qty"] = 10
        item["receipts_qty"] = 0
        item["sales_qty"] = 5
        item["closing_qty"] = 3
        statement = {
            "line_items": [item],
            "totals": {"extra": {"extraction_method": "generic_excel_grid"}},
            "phase1": {"validation": {"accepted": False, "reasons": ["stock_identity_failure"]}},
        }
        with patch.dict(os.environ, {"PHASE2_RECOVERY_ENABLED": "false"}):
            before = os.environ["PHASE2_RECOVERY_ENABLED"]
            same = _maybe_apply_phase2_recovery(statement, b"abc", "statement.xlsx")
            self.assertEqual(os.environ["PHASE2_RECOVERY_ENABLED"], before)
        self.assertIs(same, statement)
        self.assertNotIn("phase2", same)
        with patch.dict(os.environ, {"PHASE2_RECOVERY_ENABLED": "true"}):
            with patch(
                "services.recovery_runner.apply_phase2_recovery",
                return_value={"phase2": {"status": "recovered"}},
            ) as mocked:
                out = _maybe_apply_phase2_recovery(statement, b"abc", "statement.pdf")
        self.assertEqual(out["phase2"]["status"], "recovered")
        mocked.assert_called_once()
