"""Regression: STOCK_DIRECT_VISION for ZA SaleRet/ClosStock and ZL OPSTK photos."""

from __future__ import annotations

import base64
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from services.sales_statement_extractor import empty_result, extract_sales_statement
from services.stock_direct_vision import (
    STOCK_DIRECT_VISION_PROMPT,
    classify_stock_direct_vision,
    is_stock_vision_locked,
    lock_stock_vision_result,
    try_stock_direct_vision,
    validate_stock_direct_vision,
)

ZA_IMAGE = Path(
    "/var/www/html/splitextract/"
    "0000735216_2026_08_ZA_06_333_04092026081522 (2) (1).png"
)
ZL_IMAGE = Path(
    "/var/www/html/splitextract/"
    "0000736197_2026_08_ZL_13_281_07092026082501 (1).jpg"
)


def _za_gemini_payload() -> dict:
    return {
        "stockist_name": None,
        "company_name": None,
        "period_from": "2026-08-01",
        "period_to": "2026-08-31",
        "report_title": "Product Stock Report",
        "line_items": [
            {
                "product_name": "HIORA K TOOTHPASTE 100 GM",
                "opening_qty": 9,
                "receipts_qty": 50,
                "sales_qty": 21,
                "sales_value": 0,
                "closing_qty": 38,
                "closing_value": 0,
                "extra": {
                    "total_stock": 59,
                    "sale_return": 0,
                    "exp_damage": 0,
                },
            },
            {
                "product_name": "HIORA K TOOTHPASTE 50 GM",
                "opening_qty": 80,
                "receipts_qty": 0,
                "sales_qty": 5,
                "sales_value": 0,
                "closing_qty": 75,
                "closing_value": 0,
                "extra": {
                    "total_stock": 80,
                    "sale_return": 0,
                    "exp_damage": 0,
                },
            },
            {
                "product_name": "HIORA SG GEL 10GM",
                "opening_qty": 5,
                "receipts_qty": 0,
                "sales_qty": 1,
                "sales_value": 0,
                "closing_qty": 4,
                "closing_value": 0,
                "extra": {"total_stock": 5, "sale_return": 0, "exp_damage": 0},
            },
            {
                "product_name": "KARELA TAB",
                "opening_qty": 57,
                "receipts_qty": 0,
                "sales_qty": 1,
                "sales_value": 0,
                "closing_qty": 56,
                "closing_value": 0,
                "extra": {"total_stock": 57, "sale_return": 0, "exp_damage": 0},
            },
            {
                "product_name": "LASUNA TAB",
                "opening_qty": 38,
                "receipts_qty": 0,
                "sales_qty": 1,
                "sales_value": 0,
                "closing_qty": 37,
                "closing_value": 0,
                "extra": {"total_stock": 38, "sale_return": 0, "exp_damage": 0},
            },
            {
                "product_name": "LIV.52 DROP 100ML",
                "opening_qty": 12,
                "receipts_qty": 0,
                "sales_qty": 0,
                "sales_value": 0,
                "closing_qty": 12,
                "closing_value": 0,
                "extra": {"total_stock": 12, "sale_return": 0, "exp_damage": 0},
            },
        ],
        "totals": {"sales_value": None, "closing_value": None, "extra": {}},
    }


def _gemini_response(payload: dict):
    return SimpleNamespace(
        json=lambda: {
            "candidates": [
                {"content": {"parts": [{"text": json.dumps(payload)}]}}
            ]
        }
    )


class ClassifyStockDirectVisionTests(unittest.TestCase):
    def test_saleret_closstock_headers(self):
        decision = classify_stock_direct_vision(
            "Product Name Opening Purchase Total Sale SaleRet Exp/Dmg ClosStock"
        )
        self.assertIsNotNone(decision)
        self.assertEqual(decision["schema"], "saleret_closstock")

    def test_garbled_phone_ocr_still_classifies(self):
        decision = classify_stock_direct_vision(
            "Done\nProduct Name Op@ring Purchase Total Bad SaleRet Closstock"
        )
        self.assertIsNotNone(decision)

    def test_opstk_purch_classifies_medica(self):
        decision = classify_stock_direct_vision(
            "STOCK STATEMENT\nOPSTK PURCH SALE SALE VAL IN/OT STOCK STK VAL"
        )
        self.assertIsNotNone(decision)
        self.assertEqual(decision["layout"], "medica_opstk")

    def test_empty_peek_does_not_force_vision(self):
        self.assertIsNone(classify_stock_direct_vision(""))

    def test_pharmassist_stock_sale_report_beats_saleret_signals(self):
        """Jul/Jun must not be routed as SaleRet opening/purchase."""
        peek = (
            "FOUR FENCE PHARMA PVT. LTD.\n"
            "Stock and Sale Report ----- From date 01-Aug-26 to 31-Aug-26\n"
            "CONFIDO TAB 60'S 1 2 63 2 61 11294 370 -58\n"
            "Opening Val.: 70260.98 Closing Val.: 61133.16\n"
            "Sales : 38607.82 Sales (Jul): 41978.32 Sales (Jun): 49027.42\n"
        )
        decision = classify_stock_direct_vision(
            peek, "51f649e2-e4e8-4a04-8137-e1753fa0d20a4341114389042671561.jpg"
        )
        self.assertIsNotNone(decision)
        self.assertEqual(decision["layout"], "pharmassist_stock_sale")
        self.assertEqual(decision["schema"], "pharmassist_op_pur_sale_bal")
        self.assertNotEqual(decision["schema"], "saleret_closstock")

    def test_pharmassist_validation_ignores_jul_jun_for_identity(self):
        from services.stock_direct_vision import validate_stock_direct_vision
        from services.sales_statement_extractor import empty_result, empty_line_item

        result = empty_result("x.jpg", "jpg")
        item = empty_line_item()
        item["product_name"] = "CONFIDO TAB"
        item["packing"] = "60'S"
        item["opening_qty"] = 63.0
        item["receipts_qty"] = 0.0
        item["sales_qty"] = 2.0
        item["closing_qty"] = 61.0
        item["sales_value"] = 370.0
        item["closing_value"] = 11294.0
        item["extra"] = {
            "jul_qty": 1.0,
            "jun_qty": 2.0,
            "layout": "pharmassist_stock_sale",
        }
        result["line_items"] = [item] * 8
        validation = validate_stock_direct_vision(
            result, layout="pharmassist_stock_sale"
        )
        self.assertTrue(validation.get("ok"), validation)
        self.assertEqual(validation.get("identity_fail_count"), 0)

    def test_op_qty_val_beats_filename_za_saleret(self):
        """Vardhman Op.Qty/Op.Val must not use SaleRet (sales_value forced 0)."""
        peek = (
            "VARDHMAN MEDISALES PRIVATE LIMITED\n"
            "From 01-Aug-2026 To 31-Aug-2026 (Sale Report Updated Till : 01-Sep-2026)\n"
            "Item Pack Op.Qty Op.Val P.Qty P.Val S.Qty S.Val Cls.Qty Cls.Val\n"
        )
        decision = classify_stock_direct_vision(
            peek, "0000729811_2026_08_ZA_10_202_02092026150348.jpg"
        )
        self.assertIsNotNone(decision)
        self.assertEqual(decision["layout"], "stock_sales_op_qty_val")
        self.assertEqual(decision["schema"], "stock_sales_op_qty_val")
        self.assertNotIn("filename_za", decision["reason"])

    def test_op_qty_val_garbled_blue_header_ocr(self):
        peek = "thom Pack Opty OpVal Ply PVal S8Qy 8Val GeO ClsVel\nDIABECON DS TAB"
        decision = classify_stock_direct_vision(
            peek, "0000729811_2026_08_ZA_10_202_02092026150348.jpg"
        )
        self.assertEqual(decision["layout"], "stock_sales_op_qty_val")

    def test_opqty_qoh_beats_filename_za_saleret(self):
        """Balaji OpQty/.../Qoh must not map SRet into closing via SaleRet."""
        peek = (
            "BALAJI PHARMA DISTRIBUTORS\n"
            "Stock & Sales Statement. From: 01/06/2026 To: 26/06/2026\n"
            "item Name Packing OpQty PurQty SaleQty PRetQty SRetQty SAdjQty Qoh Age\n"
            "BONNISAN DROPS 30ML 18 50 1 0 1 0 68 23\n"
        )
        decision = classify_stock_direct_vision(
            peek, "0000701386_2026_08_ZA_37_216_01092026060705.png"
        )
        self.assertEqual(decision["layout"], "opqty_qoh_stock_sales")
        self.assertNotIn("filename_za", decision["reason"])

    def test_normal_stock_open_recp_beats_filename_za_saleret(self):
        """Parshava Excel Normal Stock Statement must not use SaleRet/ClosStock."""
        peek = (
            "DIGANT-13.xls Not saved yet\n"
            "PARSHAVA PHARMA\n"
            "Normal Stock Statement From 01/08/26 To 31/08/26\n"
            "Company : HIMALAYA ZANDRA DIV.\n"
            "Open Recp Sales Clsg Early Remks\n"
            "Product Name Pack Open Stk Qty Recp Qty Total Sales Qty Clsg Stk\n"
            "ARJUNA TABLET 1*60 TAB 11.00 0.00 11.00 2.00 9.00\n"
        )
        decision = classify_stock_direct_vision(
            peek, "0000728354_2026_08_ZA_06_617_04092026082655.png"
        )
        self.assertEqual(decision["layout"], "normal_stock_open_recp")
        self.assertNotIn("filename_za", decision["reason"])

    def test_opbal_issue_beats_filename_za_saleret(self):
        """JYOSTNA Op.Bal/Receipt/Issue/Closing must not use SaleRet schema."""
        peek = (
            "JYOSTNA DRUG DISTRIBUTORS\n"
            "Sales & Stock Statement(From 01/08/2026 Upto 31/08/2026)\n"
            "PRODUCT NAME PACKING Op.Bal. Qty. Receipt Qty. Total Qty. "
            "Issue Qty. Closing Balance MSR Price\n"
            "CYSTONE FORTE TAB 0 174 0 174 62 112 0.00\n"
        )
        with patch.dict(
            "os.environ", {"STOCK_OPBAL_ISSUE_BEATS_FILENAME_ZA": "true"}
        ):
            decision = classify_stock_direct_vision(
                peek, "0000730196_2026_08_ZA_25_299_07092026181829.jpg"
            )
        self.assertIsNotNone(decision)
        self.assertEqual(decision["layout"], "sales_stock_opbal_issue")
        self.assertEqual(decision["schema"], "opbal_receipt_issue_closing")
        self.assertNotIn("filename_za", decision["reason"])

    def test_opbal_issue_flag_off_keeps_filename_za(self):
        peek = (
            "Sales & Stock Statement\n"
            "PRODUCT NAME PACKING Op.Bal. Qty. Receipt Qty. Total Qty. "
            "Issue Qty. Closing Balance MSR Price\n"
        )
        with patch.dict(
            "os.environ", {"STOCK_OPBAL_ISSUE_BEATS_FILENAME_ZA": "false"}
        ):
            decision = classify_stock_direct_vision(
                peek, "0000730196_2026_08_ZA_25_299_07092026181829.jpg"
            )
        self.assertEqual(decision["layout"], "product_stock_report_saleret")
        self.assertIn("filename_za", decision["reason"])

    def test_opbal_issue_route_calls_swilerp_vision(self):
        """Vision-first locks Op.Bal/Issue path so SaleRet cannot overwrite."""
        from services.sales_statement_extractor import empty_line_item

        peek = (
            "Sales & Stock Statement\n"
            "PRODUCT NAME PACKING Op.Bal. Qty. Receipt Qty. Total Qty. "
            "Issue Qty. Closing Balance MSR Price\n"
            "CYSTONE FORTE TAB 0 174 0 174 62 112 0.00\n"
        )
        result = empty_result("jyostna.jpg", "jpg")
        item = empty_line_item()
        item["product_name"] = "CYSTONE FORTE TAB"
        item["opening_qty"] = 174.0
        item["receipts_qty"] = 0.0
        item["sales_qty"] = 62.0
        item["closing_qty"] = 112.0
        result["line_items"] = [item]
        result["totals"]["extra"]["extraction_method"] = "swilerp_page_split_vision"

        with patch.dict(
            "os.environ", {"STOCK_OPBAL_ISSUE_BEATS_FILENAME_ZA": "true"}
        ), patch(
            "services.sales_statement_extractor._parse_swilerp_sales_stock_image",
            return_value=result,
        ) as mock_parse, patch(
            "services.stock_direct_vision.detect_stock_handwriting_signals",
            return_value={"handwritten": "false"},
        ):
            out = try_stock_direct_vision(
                b"fake-image-bytes",
                "0000730196_2026_08_ZA_25_299_07092026181829.jpg",
                ".jpg",
                peek_text=peek,
            )
        self.assertIsNotNone(out)
        mock_parse.assert_called_once()
        self.assertTrue(is_stock_vision_locked(out))
        cystone = (out.get("line_items") or [])[0]
        self.assertEqual(cystone["opening_qty"], 174.0)
        self.assertEqual(cystone["closing_qty"], 112.0)
        self.assertEqual(cystone["sales_qty"], 62.0)

    def test_filename_za_saleret_rescue_to_opbal_issue(self):
        """Garbled peek + _ZA_ SaleRet with ignored Issue rescues to Op.Bal vision."""
        from services.sales_statement_extractor import empty_line_item
        from services.stock_direct_vision import (
            _saleret_looks_like_ignored_issue,
        )

        bad = empty_result("jyostna.jpg", "jpg")
        for name, op, rec, cl in (
            ("CYSTONE FORTE TAB", 174.0, 0.0, 174.0),
            ("MENTAT DS SYP", 0.0, 120.0, 120.0),
            ("MENTAT SYP", 27.0, 28.0, 55.0),
            ("LIV 52 DS TAB", 1.0, 300.0, 301.0),
        ):
            item = empty_line_item()
            item["product_name"] = name
            item["opening_qty"] = op
            item["receipts_qty"] = rec
            item["sales_qty"] = 0.0
            item["closing_qty"] = cl
            bad["line_items"].append(item)
        self.assertTrue(_saleret_looks_like_ignored_issue(bad))

        good = empty_result("jyostna.jpg", "jpg")
        for name, op, rec, sale, cl in (
            ("CYSTONE FORTE TAB", 174.0, 0.0, 62.0, 112.0),
            ("MENTAT DS SYP", 120.0, 0.0, 64.0, 56.0),
            ("MENTAT SYP", 27.0, 28.0, 10.0, 45.0),
            ("LIV 52 DS TAB", 1.0, 300.0, 212.0, 89.0),
        ):
            item = empty_line_item()
            item["product_name"] = name
            item["opening_qty"] = op
            item["receipts_qty"] = rec
            item["sales_qty"] = sale
            item["closing_qty"] = cl
            good["line_items"].append(item)

        with patch.dict(
            "os.environ", {"STOCK_OPBAL_ISSUE_BEATS_FILENAME_ZA": "true"}
        ), patch(
            "services.stock_direct_vision.extract_saleret_direct_vision",
            return_value=bad,
        ), patch(
            "services.sales_statement_extractor._parse_swilerp_sales_stock_image",
            return_value=good,
        ) as mock_opbal, patch(
            "services.stock_direct_vision.detect_stock_handwriting_signals",
            return_value={"handwritten": "false"},
        ):
            out = try_stock_direct_vision(
                b"fake-image-bytes",
                "0000730196_2026_08_ZA_25_299_07092026181829.jpg",
                ".jpg",
                peek_text="garbled Seeeeeeoeeeonnee header noise only",
            )
        self.assertIsNotNone(out)
        mock_opbal.assert_called_once()
        by = {
            str(i.get("product_name") or "").upper(): i
            for i in (out.get("line_items") or [])
        }
        self.assertEqual(by["CYSTONE FORTE TAB"]["opening_qty"], 174.0)
        self.assertEqual(by["CYSTONE FORTE TAB"]["closing_qty"], 112.0)
        self.assertEqual(by["CYSTONE FORTE TAB"]["sales_qty"], 62.0)
        self.assertEqual(
            ((out.get("totals") or {}).get("extra") or {}).get(
                "stock_direct_vision_route"
            ),
            "sales_stock_opbal_issue",
        )

    def test_ssa_issue_closing_beats_filename_za_saleret(self):
        """HAJI STOCK & SALES ANALYSIS Opening/Issue/Closing must not use SaleRet."""
        peek = (
            "HAJI ENTERPRISES\n"
            "STOCK & SALES ANALYSIS (From 01/08/2026 To 28/08/2026)\n"
            "ITEM DESCRIPTION PACK Opening Qty Value Receipt Qty Value "
            "Issue Qty Value Closing Qty Value Dump\n"
            "ARJUNA TABLET 60's 26 13833.71 0 0.00 0 0.00 26 13833.71 1\n"
        )
        with patch.dict(
            "os.environ", {"STOCK_SSA_ISSUE_CLOSING_BEATS_FILENAME_ZA": "true"}
        ):
            decision = classify_stock_direct_vision(
                peek, "0000733271_2026_08_ZA_09_363_04092026062006.jpeg"
            )
        self.assertIsNotNone(decision)
        self.assertEqual(decision["layout"], "ssa_opening_receipt_issue")
        self.assertEqual(decision["schema"], "opening_receipt_issue_closing_dump")
        self.assertNotIn("filename_za", decision["reason"])

    def test_ssa_issue_closing_flag_off_keeps_filename_za(self):
        peek = (
            "STOCK & SALES ANALYSIS\n"
            "ITEM DESCRIPTION PACK Opening Receipt Issue Closing Dump\n"
        )
        with patch.dict(
            "os.environ", {"STOCK_SSA_ISSUE_CLOSING_BEATS_FILENAME_ZA": "false"}
        ):
            decision = classify_stock_direct_vision(
                peek, "0000733271_2026_08_ZA_09_363_04092026062006.jpeg"
            )
        self.assertEqual(decision["layout"], "product_stock_report_saleret")
        self.assertIn("filename_za", decision["reason"])

    def test_ssa_issue_closing_route_calls_ssa_vision(self):
        from services.sales_statement_extractor import empty_line_item

        peek = (
            "STOCK & SALES ANALYSIS\n"
            "ITEM DESCRIPTION PACK Opening Qty Value Receipt Qty Value "
            "Issue Qty Value Closing Qty Value Dump\n"
            "ARJUNA TABLET 60's 26 13833.71 0 0.00 0 0.00 26 13833.71 1\n"
        )
        result = empty_result("haji.jpeg", "jpeg")
        item = empty_line_item()
        item["product_name"] = "ARJUNA TABLET"
        item["packing"] = "60's"
        item["opening_qty"] = 26.0
        item["opening_value"] = 13833.71
        item["receipts_qty"] = 0.0
        item["sales_qty"] = 0.0
        item["closing_qty"] = 26.0
        item["closing_value"] = 13833.71
        result["line_items"] = [item]
        result["totals"]["extra"][
            "extraction_method"
        ] = "ssa_opening_receipt_issue_dump_vision"

        with patch.dict(
            "os.environ", {"STOCK_SSA_ISSUE_CLOSING_BEATS_FILENAME_ZA": "true"}
        ), patch(
            "services.sales_statement_extractor."
            "_extract_ssa_opening_receipt_issue_dump_image",
            return_value=result,
        ) as mock_ssa, patch(
            "services.stock_direct_vision.detect_stock_handwriting_signals",
            return_value={"handwritten": "false"},
        ):
            out = try_stock_direct_vision(
                b"fake-image-bytes",
                "0000733271_2026_08_ZA_09_363_04092026062006.jpeg",
                ".jpeg",
                peek_text=peek,
            )
        self.assertIsNotNone(out)
        mock_ssa.assert_called_once()
        self.assertTrue(is_stock_vision_locked(out))
        arjuna = (out.get("line_items") or [])[0]
        self.assertEqual(arjuna["opening_qty"], 26.0)
        self.assertEqual(arjuna["sales_qty"], 0.0)
        self.assertEqual(arjuna["closing_qty"], 26.0)
        self.assertEqual(arjuna["opening_value"], 13833.71)

    def test_filename_za_ssa_probe_before_saleret(self):
        """Garbled peek + _ZA_: probe SSA before burning SaleRet Gemini budget."""
        from services.sales_statement_extractor import empty_line_item

        good = empty_result("haji.jpeg", "jpeg")
        item = empty_line_item()
        item["product_name"] = "ARJUNA TABLET"
        item["opening_qty"] = 26.0
        item["opening_value"] = 13833.71
        item["receipts_qty"] = 0.0
        item["sales_qty"] = 0.0
        item["closing_qty"] = 26.0
        item["closing_value"] = 13833.71
        good["line_items"] = [item]
        good["report_title"] = "STOCK & SALES ANALYSIS"

        with patch.dict(
            "os.environ", {"STOCK_SSA_ISSUE_CLOSING_BEATS_FILENAME_ZA": "true"}
        ), patch(
            "services.sales_statement_extractor."
            "_extract_ssa_opening_receipt_issue_dump_image",
            return_value=good,
        ) as mock_ssa, patch(
            "services.stock_direct_vision.extract_saleret_direct_vision",
        ) as mock_saleret, patch(
            "services.stock_direct_vision.detect_stock_handwriting_signals",
            return_value={"handwritten": "false"},
        ):
            out = try_stock_direct_vision(
                b"fake-image-bytes",
                "0000733271_2026_08_ZA_09_363_04092026062006.jpeg",
                ".jpeg",
                peek_text="garbled Seeeeeeoeeeonnee header noise only",
            )
        self.assertIsNotNone(out)
        mock_ssa.assert_called_once()
        kwargs = mock_ssa.call_args.kwargs
        self.assertTrue(kwargs.get("skip_ocr_gate"))
        mock_saleret.assert_not_called()
        arjuna = (out.get("line_items") or [])[0]
        self.assertEqual(arjuna["closing_qty"], 26.0)
        self.assertEqual(arjuna["sales_qty"], 0.0)
        self.assertEqual(arjuna["opening_value"], 13833.71)
        self.assertEqual(
            ((out.get("totals") or {}).get("extra") or {}).get(
                "stock_direct_vision_route"
            ),
            "ssa_opening_receipt_issue",
        )

    def test_blank_closing_and_ssa_title_trigger_swap_detector(self):
        from services.sales_statement_extractor import empty_line_item
        from services.stock_direct_vision import (
            _saleret_looks_like_issue_closing_swap,
        )

        titled = empty_result("haji.jpeg", "jpeg")
        titled["report_title"] = "STOCK & SALES ANALYSIS"
        self.assertTrue(_saleret_looks_like_issue_closing_swap(titled))

        blank = empty_result("haji.jpeg", "jpeg")
        for name, op in (
            ("ARJUNA TABLET", 26.0),
            ("BONNISAN DROPS-30ML", 209.0),
            ("BONNISAN LIQUID", 234.0),
            ("BONNISPAZ DROPS", 60.0),
            ("BRAHMI TABLET 60'S", 60.0),
        ):
            item = empty_line_item()
            item["product_name"] = name
            item["opening_qty"] = op
            item["receipts_qty"] = 0.0
            item["sales_qty"] = 0.0
            item["closing_qty"] = 0.0
            item["extra"] = {"total_stock": 0.0}
            blank["line_items"].append(item)
        self.assertTrue(_saleret_looks_like_issue_closing_swap(blank))

    def test_filename_za_saleret_rescue_to_ssa_issue_closing(self):
        """When SSA probe misses, SaleRet blank-closing still rescues to SSA."""
        from services.sales_statement_extractor import empty_line_item
        from services.stock_direct_vision import (
            _saleret_looks_like_issue_closing_swap,
        )

        bad = empty_result("haji.jpeg", "jpeg")
        bad["report_title"] = "STOCK & SALES ANALYSIS"
        for name, op in (
            ("ARJUNA TABLET", 26.0),
            ("BONNISAN DROPS-30ML", 209.0),
            ("BONNISAN LIQUID", 234.0),
            ("BONNISPAZ DROPS", 60.0),
            ("BRAHMI TABLET 60'S", 60.0),
        ):
            item = empty_line_item()
            item["product_name"] = name
            item["opening_qty"] = op
            item["receipts_qty"] = 0.0
            item["sales_qty"] = 0.0
            item["closing_qty"] = 0.0
            item["extra"] = {
                "total_stock": 0.0,
                "sale_return": 0.0,
                "exp_damage": 0.0,
                "layout": "product_stock_report",
            }
            bad["line_items"].append(item)
        self.assertTrue(_saleret_looks_like_issue_closing_swap(bad))

        good = empty_result("haji.jpeg", "jpeg")
        for name, op, sale, cl in (
            ("ARJUNA TABLET", 26.0, 0.0, 26.0),
            ("BONNISAN DROPS-30ML", 209.0, 0.0, 209.0),
            ("BONNISAN LIQUID", 234.0, 12.0, 222.0),
            ("BONNISPAZ DROPS", 60.0, 0.0, 60.0),
            ("BRAHMI TABLET 60'S", 60.0, 5.0, 55.0),
        ):
            item = empty_line_item()
            item["product_name"] = name
            item["opening_qty"] = op
            item["receipts_qty"] = 0.0
            item["sales_qty"] = sale
            item["closing_qty"] = cl
            item["opening_value"] = 100.0
            item["closing_value"] = 100.0
            good["line_items"].append(item)

        with patch.dict(
            "os.environ", {"STOCK_SSA_ISSUE_CLOSING_BEATS_FILENAME_ZA": "true"}
        ), patch(
            "services.stock_direct_vision.extract_saleret_direct_vision",
            return_value=bad,
        ), patch(
            "services.sales_statement_extractor."
            "_extract_ssa_opening_receipt_issue_dump_image",
            side_effect=[None, good],
        ) as mock_ssa, patch(
            "services.stock_direct_vision.detect_stock_handwriting_signals",
            return_value={"handwritten": "false"},
        ):
            out = try_stock_direct_vision(
                b"fake-image-bytes",
                "0000733271_2026_08_ZA_09_363_04092026062006.jpeg",
                ".jpeg",
                peek_text="garbled Seeeeeeoeeeonnee header noise only",
            )
        self.assertIsNotNone(out)
        self.assertEqual(mock_ssa.call_count, 2)
        self.assertTrue(mock_ssa.call_args.kwargs.get("skip_ocr_gate"))
        by = {
            str(i.get("product_name") or "").upper(): i
            for i in (out.get("line_items") or [])
        }
        self.assertEqual(by["ARJUNA TABLET"]["sales_qty"], 0.0)
        self.assertEqual(by["ARJUNA TABLET"]["closing_qty"], 26.0)
        self.assertEqual(
            ((out.get("totals") or {}).get("extra") or {}).get(
                "stock_direct_vision_route"
            ),
            "ssa_opening_receipt_issue",
        )

    def test_normal_stock_validation_open_recp_sales_clsg(self):
        from services.stock_direct_vision import validate_stock_direct_vision
        from services.sales_statement_extractor import empty_result, empty_line_item

        result = empty_result("parshava.png", "png")
        item = empty_line_item()
        item["product_name"] = "ARJUNA TABLET"
        item["packing"] = "1*60 TAB"
        item["opening_qty"] = 11.0
        item["receipts_qty"] = 0.0
        item["sales_qty"] = 2.0
        item["closing_qty"] = 9.0
        item["extra"] = {"layout": "normal_stock_open_recp"}
        result["line_items"] = [item] * 8
        validation = validate_stock_direct_vision(
            result, layout="normal_stock_open_recp"
        )
        self.assertTrue(validation.get("ok"), validation)
        self.assertEqual(validation.get("identity_fail_count"), 0)

    def test_normal_stock_ocr_parse_parenthetical_recp(self):
        from services.stock_direct_vision import _parse_normal_stock_open_recp_ocr_text

        text = (
            "PARSHAVA PHARMA\n"
            "Normal Stock Statement From 01/08/26 To 31/08/26\n"
            "8 1 ARJUNA TABLET 1*60 TAB 11,00 0.00 11,00 2.00 9.00 27-Sep\n"
            "26 19 FLORA SANTE CAP 10 1*10 CAP 120.00 (2.00) 118.00 22.00 96.00 27-Dec\n"
        )
        rows = _parse_normal_stock_open_recp_ocr_text(text)
        by = {r["product_name"].upper(): r for r in rows}
        self.assertEqual(by["ARJUNA TABLET"]["closing_qty"], 9.0)
        self.assertEqual(by["ARJUNA TABLET"]["sales_qty"], 2.0)
        flora = next(v for k, v in by.items() if "FLORA SANTE" in k)
        self.assertEqual(flora["receipts_qty"], -2.0)
        self.assertEqual(flora["closing_qty"], 96.0)

    def test_opqty_qoh_validation_uses_qoh_not_sret(self):
        from services.stock_direct_vision import validate_stock_direct_vision
        from services.sales_statement_extractor import empty_result, empty_line_item

        result = empty_result("balaji.png", "png")
        item = empty_line_item()
        item["product_name"] = "BONNISAN DROPS"
        item["packing"] = "30ML"
        item["opening_qty"] = 18.0
        item["receipts_qty"] = 50.0
        item["sales_qty"] = 1.0
        item["closing_qty"] = 68.0
        item["extra"] = {"sale_return": 1.0, "layout": "opqty_qoh_stock_sales"}
        result["line_items"] = [item] * 8
        validation = validate_stock_direct_vision(
            result, layout="opqty_qoh_stock_sales"
        )
        self.assertTrue(validation.get("ok"), validation)
        self.assertEqual(validation.get("identity_fail_count"), 0)
        # Wrong closing (= SRet) must fail identity.
        bad = empty_result("balaji.png", "png")
        bad_item = dict(item)
        bad_item["closing_qty"] = 1.0
        bad_item["extra"] = dict(item["extra"])
        bad["line_items"] = [bad_item] * 8
        bad_val = validate_stock_direct_vision(bad, layout="opqty_qoh_stock_sales")
        self.assertGreater(bad_val.get("identity_fail_count") or 0, 0)

    def test_op_qty_val_maps_opening_sales_and_closing_values(self):
        from services.stock_direct_vision import (
            _coerce_vision_payload,
            validate_stock_direct_vision,
        )
        from services.sales_statement_extractor import (
            empty_result,
            _apply_parsed_sales_json,
            _ensure_stock_qty_value_fields,
        )

        payload = {
            "line_items": [
                {
                    "product_name": "#KOFLET LOZENGES JAR",
                    "packing": "200'S",
                    "opening_qty": 1,
                    "opening_value": 253.29,
                    "receipts_qty": 0,
                    "receipts_value": 0,
                    "sales_qty": 1,
                    "sales_value": 253.29,
                    "closing_qty": 0,
                    "closing_value": 0,
                },
                {
                    "product_name": "BONNISAN DROPS",
                    "packing": "30ML",
                    "opening_qty": 43,
                    "opening_value": 3209.47,
                    "receipts_qty": 100,
                    "receipts_value": 7466.0,
                    "sales_qty": 66,
                    "sales_value": 4926.65,
                    "closing_qty": 77,
                    "closing_value": 5748.82,
                },
            ]
            + [
                {
                    "product_name": f"FILLER PRODUCT {i}",
                    "opening_qty": 1,
                    "receipts_qty": 0,
                    "sales_qty": 0,
                    "closing_qty": 1,
                    "opening_value": 10,
                    "receipts_value": 0,
                    "sales_value": 0,
                    "closing_value": 10,
                }
                for i in range(5)
            ]
        }
        parsed = _coerce_vision_payload(payload)
        result = _apply_parsed_sales_json(empty_result("t.jpg", "jpg"), parsed)
        result = _ensure_stock_qty_value_fields(result)
        koflet = result["line_items"][0]
        bonni = result["line_items"][1]
        self.assertEqual(koflet["opening_qty"], 1.0)
        self.assertEqual(koflet["opening_value"], 253.29)
        self.assertEqual(koflet["sales_value"], 253.29)
        self.assertEqual(koflet["closing_value"], 0.0)
        self.assertEqual(bonni["receipts_value"], 7466.0)
        self.assertEqual(bonni["sales_value"], 4926.65)
        self.assertEqual(bonni["closing_value"], 5748.82)
        self.assertEqual(bonni["extra"]["opening_value"], 3209.47)
        validation = validate_stock_direct_vision(
            result, layout="stock_sales_op_qty_val"
        )
        self.assertTrue(validation.get("ok"), validation)
        self.assertEqual(validation.get("identity_fail_count"), 0)

    def test_vardhman_swapped_stockist_company_is_corrected(self):
        """Letterhead is stockist; Company: HIMALAYA line is company_name."""
        from services.sales_statement_extractor import (
            empty_result,
            _apply_parsed_sales_json,
        )

        rows = [
            {
                "product_name": f"PROD {i}",
                "opening_qty": 1,
                "receipts_qty": 0,
                "sales_qty": 0,
                "closing_qty": 1,
                "sales_value": 0,
                "closing_value": 10,
            }
            for i in range(6)
        ]
        swapped = _apply_parsed_sales_json(
            empty_result("vardhman.jpg", "jpg"),
            {
                "stockist_name": "HIMALAYA WELLNESS-1",
                "company_name": "VARDHMAN MEDISALES PRIVATE LIMITED",
                "line_items": rows,
            },
        )
        self.assertIn("VARDHMAN", (swapped.get("stockist_name") or "").upper())
        self.assertIn("HIMALAYA", (swapped.get("company_name") or "").upper())
        correct = _apply_parsed_sales_json(
            empty_result("vardhman.jpg", "jpg"),
            {
                "stockist_name": "VARDHMAN MEDISALES PRIVATE LIMITED",
                "company_name": "HIMALAYA WELLNESS-1",
                "line_items": rows,
            },
        )
        self.assertIn("VARDHMAN", (correct.get("stockist_name") or "").upper())
        self.assertIn("HIMALAYA", (correct.get("company_name") or "").upper())

    def test_filename_za_routes_without_ocr_headers(self):
        decision = classify_stock_direct_vision(
            "", "0000735216_2026_08_ZA_06_333_04092026081522.png"
        )
        self.assertIsNotNone(decision)
        self.assertEqual(decision["schema"], "saleret_closstock")
        self.assertIn("filename_za", decision["reason"])

    def test_filename_zl_routes_without_ocr_headers(self):
        decision = classify_stock_direct_vision(
            "", "0000736197_2026_08_ZL_13_281_07092026082501.jpg"
        )
        self.assertIsNotNone(decision)
        self.assertEqual(decision["layout"], "medica_opstk")
        self.assertIn("filename_zl", decision["reason"])


class ValidateAndLockTests(unittest.TestCase):
    def test_valid_za_payload_passes(self):
        result = empty_result("za.png", "png")
        result["line_items"] = _za_gemini_payload()["line_items"]
        result["totals"]["extra"]["layout"] = "product_stock_report"
        validation = validate_stock_direct_vision(
            result, layout="product_stock_report_saleret"
        )
        self.assertTrue(validation["ok"], validation)
        self.assertEqual(validation.get("identity_fail_count"), 0)

    def test_identity_failure_does_not_rewrite_gemini_values(self):
        result = empty_result("za.png", "png")
        # Intentionally wrong closing (30) vs printed/Gemini 38.
        bad = {
            "product_name": "HIORA K TOOTHPASTE 100 GM",
            "opening_qty": 9,
            "receipts_qty": 50,
            "sales_qty": 21,
            "closing_qty": 30,
            "sales_value": 0,
            "closing_value": 0,
            "extra": {
                "total_stock": 59,
                "sale_return": 0,
                "exp_damage": 0,
                "layout": "product_stock_report",
            },
        }
        result["line_items"] = [bad] + [
            {
                "product_name": f"PRODUCT {i} SYRUP",
                "opening_qty": 10,
                "receipts_qty": 0,
                "sales_qty": 1,
                "closing_qty": 9,
                "sales_value": 0,
                "closing_value": 0,
                "extra": {
                    "total_stock": 10,
                    "sale_return": 0,
                    "exp_damage": 0,
                    "layout": "product_stock_report",
                },
            }
            for i in range(5)
        ]
        before = json.loads(json.dumps(result["line_items"][0]))
        validation = validate_stock_direct_vision(
            result, layout="product_stock_report_saleret"
        )
        self.assertGreaterEqual(validation.get("identity_fail_count") or 0, 1)
        after = result["line_items"][0]
        self.assertEqual(after["opening_qty"], before["opening_qty"])
        self.assertEqual(after["receipts_qty"], before["receipts_qty"])
        self.assertEqual(after["extra"]["total_stock"], before["extra"]["total_stock"])
        self.assertEqual(after["sales_qty"], before["sales_qty"])
        self.assertEqual(after["closing_qty"], 30)
        self.assertTrue(after["extra"].get("validation_failed"))

    def test_locked_result_skips_ocr_qty_repair(self):
        result = empty_result("za.png", "png")
        result["line_items"] = [
            {
                "product_name": "HIORA K TOOTHPASTE 100 GM",
                "opening_qty": 9,
                "receipts_qty": 50,
                "sales_qty": 21,
                "closing_qty": 38,
                "sales_value": 0,
                "closing_value": 0,
                "extra": {
                    "total_stock": 59,
                    "sale_return": 0,
                    "exp_damage": 0,
                    "layout": "product_stock_report",
                },
            }
        ] + [
            {
                "product_name": f"PRODUCT {i} SYRUP",
                "opening_qty": 10,
                "receipts_qty": 0,
                "sales_qty": 1,
                "closing_qty": 9,
                "sales_value": 0,
                "closing_value": 0,
                "extra": {
                    "total_stock": 10,
                    "sale_return": 0,
                    "exp_damage": 0,
                    "layout": "product_stock_report",
                },
            }
            for i in range(5)
        ]
        lock_stock_vision_result(result, layout="product_stock_report")
        self.assertTrue(is_stock_vision_locked(result))
        self.assertEqual(
            ((result.get("totals") or {}).get("extra") or {}).get("numeric_source"),
            "gemini_vision",
        )

        from services.sales_statement_extractor import _psr_fill_missing_sales

        # Corrupt-looking OCR candidates must not overwrite Vision closing 38.
        poisoned = result["line_items"][0]
        poisoned["extra"]["ocr_closing_candidate"] = 30
        poisoned["extra"]["ocr_sales_candidate"] = 100
        poisoned["extra"]["ocr_purchase_candidate"] = 50.09
        poisoned["extra"]["ocr_total_candidate"] = 59.09
        filled = _psr_fill_missing_sales(result)
        row = filled["line_items"][0]
        self.assertEqual(row["opening_qty"], 9)
        self.assertEqual(row["receipts_qty"], 50)
        self.assertEqual(row["extra"]["total_stock"], 59)
        self.assertEqual(row["sales_qty"], 21)
        self.assertEqual(row["closing_qty"], 38)
        self.assertNotEqual(row["sales_qty"], 100)
        self.assertNotEqual(row["closing_qty"], 30)


@unittest.skipUnless(ZA_IMAGE.is_file(), "missing ZA fixture")
class ZaDirectVisionMockTests(unittest.TestCase):
    def test_original_bytes_sent_and_hiora_values(self):
        original = ZA_IMAGE.read_bytes()
        seen = {"calls": 0, "payloads": []}

        def _gen(**kwargs):
            seen["calls"] += 1
            seen["payloads"].append(kwargs.get("payload"))
            return _gemini_response(_za_gemini_payload())

        with patch(
            "services.sales_extraction_runtime.sales_generate_content_via_vertex",
            side_effect=_gen,
        ), patch(
            "services.sales_statement_extractor._ocr_image_to_text",
            return_value=(
                "Done\nProduct Name Opening Purchase Total Sale SaleRet "
                "Exp/Dmg ClosStock\nHIORA"
            ),
        ), patch(
            "services.sales_statement_extractor._ocr_stock_valuation_preview",
            return_value="",
        ):
            result = extract_sales_statement(original, ZA_IMAGE.name)

        self.assertTrue(is_stock_vision_locked(result))
        extra = (result.get("totals") or {}).get("extra") or {}
        self.assertEqual(extra.get("extraction_method"), "product_stock_report_vision")
        self.assertEqual(extra.get("numeric_source"), "gemini_vision")
        self.assertIn(
            (extra.get("gemini_input") or {}).get("kind") or extra.get("gemini_input_kind"),
            {"image_original", "image_normalized_secondary"},
        )
        self.assertLessEqual(seen["calls"], 2)
        self.assertGreaterEqual(seen["calls"], 1)
        inline = seen["payloads"][0]["contents"][0]["parts"][1]["inline_data"]
        self.assertEqual(base64.b64decode(inline["data"]), original)
        prompt = seen["payloads"][0]["contents"][0]["parts"][0]["text"]
        self.assertEqual(prompt, STOCK_DIRECT_VISION_PROMPT)
        self.assertIn("100 GM", prompt)
        self.assertNotIn("OCR TRANSCRIPT", prompt)

        by_name = {
            str(i.get("product_name") or "").upper(): i
            for i in (result.get("line_items") or [])
        }
        h100 = next(v for k, v in by_name.items() if "HIORA" in k and "100" in k)
        h50 = next(v for k, v in by_name.items() if "HIORA" in k and "50" in k)
        self.assertEqual(h100["opening_qty"], 9)
        self.assertEqual(h100["receipts_qty"], 50)
        self.assertEqual((h100.get("extra") or {}).get("total_stock"), 59)
        self.assertEqual(h100["sales_qty"], 21)
        self.assertEqual((h100.get("extra") or {}).get("sale_return"), 0)
        self.assertEqual((h100.get("extra") or {}).get("exp_damage"), 0)
        self.assertEqual(h100["closing_qty"], 38)
        # Packing digits must not become sales.
        self.assertNotEqual(h100["sales_qty"], 100)
        self.assertNotEqual(h50["sales_qty"], 50)
        self.assertEqual(h50["opening_qty"], 80)
        self.assertEqual(h50["receipts_qty"], 0)
        self.assertEqual((h50.get("extra") or {}).get("total_stock"), 80)
        self.assertEqual(h50["sales_qty"], 5)
        self.assertEqual(h50["closing_qty"], 75)

    def test_successful_direct_vision_skips_ocr_probe_cascade(self):
        original = ZA_IMAGE.read_bytes()
        ocr_calls = {"n": 0}

        def _ocr(*args, **kwargs):
            ocr_calls["n"] += 1
            return (
                "Product Name Opening Purchase Total Sale SaleRet Exp/Dmg ClosStock"
            )

        with patch(
            "services.sales_extraction_runtime.sales_generate_content_via_vertex",
            return_value=_gemini_response(_za_gemini_payload()),
        ), patch(
            "services.sales_statement_extractor._ocr_image_to_text",
            side_effect=_ocr,
        ), patch(
            "services.sales_statement_extractor._ocr_stock_valuation_preview",
            return_value="",
        ), patch(
            "services.sales_statement_extractor._extract_phone_rate_ssa_screenshot",
            side_effect=AssertionError("phone rate must not run after direct vision"),
        ):
            result = extract_sales_statement(original, ZA_IMAGE.name)

        self.assertTrue(is_stock_vision_locked(result))
        # ZA SaleRet may use a few layout-header OCR bands; never a numeric cascade.
        self.assertLessEqual(ocr_calls["n"], 3)

    def test_total_qty_alias_maps_into_extra(self):
        from services.stock_direct_vision import _coerce_vision_payload

        payload = {
            "line_items": [
                {
                    "product_name": "HIORA K TOOTHPASTE 100 GM",
                    "opening_qty": 9,
                    "receipts_qty": 50,
                    "total_qty": 59,
                    "sales_qty": 21,
                    "sales_return_qty": 0,
                    "expiry_damage_qty": 0,
                    "closing_qty": 38,
                }
            ]
        }
        coerced = _coerce_vision_payload(payload)
        extra = coerced["line_items"][0]["extra"]
        self.assertEqual(extra["total_stock"], 59)
        self.assertEqual(extra["sale_return"], 0)
        self.assertEqual(extra["exp_damage"], 0)

    def test_gemini_technical_failure_falls_back_without_lock(self):
        original = ZA_IMAGE.read_bytes()

        with patch(
            "services.sales_extraction_runtime.sales_generate_content_via_vertex",
            side_effect=RuntimeError("provider down"),
        ), patch(
            "services.sales_statement_extractor._ocr_image_to_text",
            return_value=(
                "Product Name Opening Purchase Total Sale SaleRet Exp/Dmg ClosStock\n"
                "HIORA K TOOTHPASTE 100 GM 9 50 59 21 0 0 38"
            ),
        ), patch(
            "services.sales_statement_extractor._ocr_stock_valuation_preview",
            return_value="",
        ):
            result = extract_sales_statement(original, ZA_IMAGE.name)

        # Technical Vision failure must not leave a locked Vision result.
        self.assertFalse(is_stock_vision_locked(result))


@unittest.skipUnless(ZL_IMAGE.is_file(), "missing ZL fixture")
class ZlDirectVisionClassificationTests(unittest.TestCase):
    def test_zl_classifies_without_perfect_headers(self):
        # Weak/partial OCR must still route to direct Vision (not require full OPSTK parse).
        decision = classify_stock_direct_vision(
            "STOCK STATEMENT\nHIMALAYA-ZEAL\nOPSTK PURCH SALE"
        )
        self.assertIsNotNone(decision)
        self.assertEqual(decision["layout"], "medica_opstk")

    def test_try_stock_direct_vision_medica_uses_original_bytes_path(self):
        original = ZL_IMAGE.read_bytes()
        medica = empty_result(ZL_IMAGE.name, "jpg")
        medica["line_items"] = [
            {
                "product_name": f"ABANA TAB {i}",
                "opening_qty": 4,
                "receipts_qty": 1,
                "sales_qty": 2,
                "closing_qty": 3,
                "sales_value": 10,
                "closing_value": 15,
                "extra": {"layout": "medica_stock_statement"},
            }
            for i in range(6)
        ]
        medica["totals"]["extra"]["extraction_method"] = "medica_opstk_vision"
        with patch(
            "services.sales_statement_extractor._extract_medica_stock_statement_vision",
            return_value=medica,
        ) as medica_call:
            result = try_stock_direct_vision(
                original,
                ZL_IMAGE.name,
                ".jpg",
                peek_text="STOCK STATEMENT OPSTK PURCH SALE VAL",
            )
        medica_call.assert_called_once()
        self.assertIsNotNone(result)
        self.assertTrue(is_stock_vision_locked(result))
        self.assertEqual(
            medica_call.call_args[0][0],
            original,
        )


HW_IMAGE = Path(
    "/var/www/html/splitextract/"
    "0000734827_2026_08_ZA_24_259_03092026065501.jpg"
)
HW_IMAGE_4860 = Path(
    "/var/www/html/splitextract/"
    "0000734860_2026_08_ZA_24_288_07092026034158.jpg"
)


def _hw_order_form_payload() -> dict:
    conf = {
        "opening_qty": "high",
        "receipts_qty": "high",
        "total_qty": "high",
        "sales_qty": "high",
        "sales_return_qty": "high",
        "expiry_damage_qty": "high",
        "closing_qty": "high",
    }
    rows = [
        ("Bonnisan drops", "30 ml", 0),
        ("Bonnisan liquid", "200 ml", 0),
        ("Bresol syrup", "200 ml", 0),
        ("Evecare syrup", "200 ml", 15),
        ("HiOra-GA gel", "10 gm", 14),
        ("Liv.52 drops", "60 ml", 45),
        ("Liv.52 syrup", "100 ml", 0),
        ("Menosan tablets", "60 tab", 0),
        ("Rumalaya gel", "30 gm", 0),
        ("Septilin syrup", "200 ml", 0),
    ]
    items = []
    for name, pack, qty in rows:
        items.append(
            {
                "product_name": name,
                "packing": pack,
                "opening_qty": 0,
                "receipts_qty": 0,
                "total_qty": 0,
                "sales_qty": qty,
                "sales_return_qty": 0,
                "expiry_damage_qty": 0,
                "closing_qty": 0,
                "sales_value": 0,
                "confidence": dict(conf),
            }
        )
    # One genuinely unreadable handwritten cell → null + low confidence.
    items[3]["sales_qty"] = None
    items[3]["confidence"] = {**conf, "sales_qty": "low"}
    items[3]["sales_qty_confidence"] = "low"
    return {
        "document_type": "stock_statement",
        "schema": "opening_purchase_total_sale_saleret_exp_closing",
        "handwritten": True,
        "report_title": "ORDER FORM",
        "line_items": items,
        "totals": {"sales_value": None, "closing_value": None, "extra": {}},
    }


@unittest.skipUnless(HW_IMAGE.is_file(), "missing handwritten ORDER FORM fixture")
class HandwrittenStockVisionTests(unittest.TestCase):
    def test_handwritten_order_form_routes_vision_first(self):
        from services.stock_direct_vision import (
            classify_stock_direct_vision,
            detect_stock_handwriting_signals,
        )

        hw = detect_stock_handwriting_signals(
            HW_IMAGE.read_bytes(),
            peek_text="",
            filename=HW_IMAGE.name,
        )
        self.assertTrue(hw.get("order_form"))
        self.assertIn(hw.get("handwritten"), {"true", "mixed"})
        decision = classify_stock_direct_vision(
            "", HW_IMAGE.name, handwriting=hw
        )
        self.assertIsNotNone(decision)
        self.assertEqual(decision["layout"], "handwritten_order_form")
        # Must not fall through to filename ZA SaleRet.
        self.assertNotIn("filename_za", decision["reason"])

    def test_original_bytes_sent_before_numeric_ocr(self):
        from services.stock_direct_vision import HANDWRITTEN_STOCK_VISION_PROMPT

        original = HW_IMAGE.read_bytes()
        seen = {"calls": 0, "payloads": [], "numeric_ocr": 0}

        def _gen(**kwargs):
            seen["calls"] += 1
            seen["payloads"].append(kwargs.get("payload"))
            return _gemini_response(_hw_order_form_payload())

        def _ocr(*args, **kwargs):
            # Layout header OCR may run; numeric cell recovery must not.
            return "Zandra\nORDER FORM\nSAP Code Product Pack Qty"

        with patch(
            "services.sales_extraction_runtime.sales_generate_content_via_vertex",
            side_effect=_gen,
        ), patch(
            "services.sales_statement_extractor._ocr_image_to_text",
            side_effect=_ocr,
        ), patch(
            "services.sales_statement_extractor._ocr_stock_valuation_preview",
            return_value="",
        ), patch(
            "services.sales_statement_extractor._read_order_form_qty_cells",
            side_effect=AssertionError("numeric qty OCR must not run before Vision"),
        ), patch(
            "services.sales_statement_extractor._extract_phone_rate_ssa_screenshot",
            side_effect=AssertionError("phone rate must not run after handwritten vision"),
        ):
            result = extract_sales_statement(original, HW_IMAGE.name)

        self.assertTrue(is_stock_vision_locked(result))
        extra = (result.get("totals") or {}).get("extra") or {}
        self.assertEqual(extra.get("numeric_source"), "gemini_vision")
        self.assertEqual(extra.get("gemini_input_kind"), "image_original")
        self.assertIn(extra.get("handwritten"), {"true", "mixed"})
        self.assertEqual(extra.get("extraction_method"), "handwritten_stock_vision")
        self.assertGreaterEqual(seen["calls"], 1)
        self.assertLessEqual(seen["calls"], 2)
        inline = seen["payloads"][0]["contents"][0]["parts"][1]["inline_data"]
        self.assertEqual(base64.b64decode(inline["data"]), original)
        prompt = seen["payloads"][0]["contents"][0]["parts"][0]["text"]
        self.assertEqual(prompt, HANDWRITTEN_STOCK_VISION_PROMPT)
        self.assertIn("handwritten", prompt.lower())
        self.assertNotIn("OCR TRANSCRIPT", prompt)

        by_name = {
            str(i.get("product_name") or "").upper(): i
            for i in (result.get("line_items") or [])
        }
        evecare = next(v for k, v in by_name.items() if "EVECARE" in k)
        # Low-confidence null preserved (not invented for identity).
        self.assertIsNone(evecare.get("sales_qty"))
        self.assertEqual(
            ((evecare.get("extra") or {}).get("qty_confidence") or {}).get("sales_qty"),
            "low",
        )
        hiora = next(v for k, v in by_name.items() if "HIORA" in k)
        self.assertEqual(hiora.get("sales_qty"), 14)
        # Packing 10 gm must not become sales_qty.
        self.assertNotEqual(hiora.get("sales_qty"), 10)
        liv = next(v for k, v in by_name.items() if "LIV" in k and "DROP" in k)
        self.assertEqual(liv.get("sales_qty"), 45)
        self.assertNotEqual(liv.get("sales_qty"), 60)

    def test_identity_validation_does_not_overwrite_null(self):
        result = empty_result("hw.jpg", "jpg")
        result["line_items"] = [
            {
                "product_name": f"PRODUCT {i} SYRUP",
                "opening_qty": 0,
                "receipts_qty": 0,
                "sales_qty": None if i == 0 else float(i),
                "closing_qty": 0,
                "sales_value": 0,
                "closing_value": 0,
                "extra": {
                    "layout": "handwritten_order_form",
                    "qty_confidence": {"sales_qty": "low" if i == 0 else "high"},
                },
            }
            for i in range(10)
        ]
        before = result["line_items"][0]["sales_qty"]
        validation = validate_stock_direct_vision(
            result, layout="handwritten_order_form"
        )
        self.assertTrue(validation["ok"], validation)
        self.assertIsNone(result["line_items"][0]["sales_qty"])
        self.assertEqual(result["line_items"][0]["sales_qty"], before)

    def test_handwritten_gemini_failure_falls_back_without_lock(self):
        original = HW_IMAGE.read_bytes()
        with patch(
            "services.sales_extraction_runtime.sales_generate_content_via_vertex",
            side_effect=RuntimeError("provider down"),
        ), patch(
            "services.sales_statement_extractor._ocr_image_to_text",
            return_value="Zandra ORDER FORM SAP Code Product Pack Qty Bonnisan",
        ), patch(
            "services.sales_statement_extractor._ocr_stock_valuation_preview",
            return_value="",
        ), patch(
            "services.stock_direct_vision._layout_header_text",
            return_value="Zandra ORDER FORM SAP Code Qty",
        ):
            result = extract_sales_statement(original, HW_IMAGE.name)
        self.assertFalse(is_stock_vision_locked(result))

    def test_filename_za_saleret_not_overridden_without_order_form(self):
        decision = classify_stock_direct_vision(
            "",
            "0000735216_2026_08_ZA_06_333_04092026081522.png",
            handwriting={
                "handwritten": "false",
                "order_form": False,
                "saleret": False,
            },
        )
        self.assertEqual(decision["layout"], "product_stock_report_saleret")
        self.assertIn("filename_za", decision["reason"])

    def test_garbled_order_form_ocr_still_detects(self):
        from services.stock_direct_vision import _has_order_form_headers

        self.assertTrue(_has_order_form_headers(")RDER FORM\nProduct Pack"))
        self.assertTrue(_has_order_form_headers("JRDER FORM\nSAP Qty"))
        self.assertTrue(_has_order_form_headers("ORDER FORM"))
        self.assertFalse(
            _has_order_form_headers("SaleRet ClosStock Opening Purchase")
        )

    def test_short_brand_name_v_gel_is_kept(self):
        from services.stock_direct_vision import _stock_vision_product_name_ok

        self.assertTrue(_stock_vision_product_name_ok("V-Gel"))
        self.assertTrue(_stock_vision_product_name_ok("V-Gel cream"))
        self.assertTrue(_stock_vision_product_name_ok("Bonnispaz drops"))
        self.assertFalse(_stock_vision_product_name_ok("AB"))
        self.assertFalse(_stock_vision_product_name_ok(""))

        # Filter path must not drop V-Gel after Vision JSON apply.
        from services.stock_direct_vision import _call_handwritten_vision
        from unittest.mock import patch

        payload = {
            "document_type": "stock_statement",
            "handwritten": True,
            "line_items": [
                {
                    "product_name": "V-Gel",
                    "packing": "30 g",
                    "opening_qty": 0,
                    "receipts_qty": 0,
                    "total_qty": 0,
                    "sales_qty": 0,
                    "sales_return_qty": 0,
                    "expiry_damage_qty": 0,
                    "closing_qty": 0,
                    "confidence": {k: "high" for k in (
                        "opening_qty","receipts_qty","total_qty","sales_qty",
                        "sales_return_qty","expiry_damage_qty","closing_qty",
                    )},
                },
            ]
            + [
                    {
                    "product_name": f"SAMPLE ITEM {i} SYRUP",
                    "packing": "200 ml",
                    "opening_qty": 0,
                    "receipts_qty": 0,
                    "total_qty": 0,
                    "sales_qty": 0,
                    "sales_return_qty": 0,
                    "expiry_damage_qty": 0,
                    "closing_qty": 0,
                }
                for i in range(10)
            ],
        }
        with patch(
            "services.sales_extraction_runtime.sales_generate_content_via_vertex",
            return_value=_gemini_response(payload),
        ):
            result = _call_handwritten_vision(
                b"\xff\xd8\xff",
                "vgel.jpg",
                ".jpg",
                vision_bytes=b"\xff\xd8\xff",
                image_meta={"normalized": False},
                candidate=1,
                model="gemini-2.5-flash-lite",
                route="handwritten_order_form",
                reason="unit",
                handwritten="true",
            )
        self.assertIsNotNone(result)
        names = {
            str(i.get("product_name") or "").upper()
            for i in (result.get("line_items") or [])
        }
        self.assertTrue(any("V-GEL" in n or "VGEL" in n.replace("-", "") for n in names))

    @unittest.skipUnless(HW_IMAGE_4860.is_file(), "missing 4860 handwritten fixture")
    def test_4860_routes_handwritten_not_saleret(self):
        from services.stock_direct_vision import (
            classify_stock_direct_vision,
            detect_stock_handwriting_signals,
        )

        hw = detect_stock_handwriting_signals(
            HW_IMAGE_4860.read_bytes(), "", HW_IMAGE_4860.name
        )
        self.assertTrue(hw.get("order_form"))
        decision = classify_stock_direct_vision(
            "", HW_IMAGE_4860.name, handwriting=hw
        )
        self.assertEqual(decision["layout"], "handwritten_order_form")
        self.assertNotIn("filename_za", decision["reason"])


ZL_PWSS_IMAGE = Path(
    "/var/www/html/splitextract/"
    "0000700400_2026_08_ZL_18_377_04092026044551.png"
)


@unittest.skipUnless(ZL_PWSS_IMAGE.is_file(), "missing ZL product-wise fixture")
class ZlProductWiseStockStatementTests(unittest.TestCase):
    def test_product_wise_beats_filename_zl_medica(self):
        from services.stock_direct_vision import _has_product_wise_stock_headers

        peek = (
            "Product wise stock statement from 01/07/2026 to 29/07/2026\n"
            "Product Name Packing Opening Receipt __Issues Closing "
            "Sh.Exp Liquidation Sales Amount Closing Amount"
        )
        self.assertTrue(_has_product_wise_stock_headers(peek))
        decision = classify_stock_direct_vision(peek, ZL_PWSS_IMAGE.name)
        self.assertEqual(decision["layout"], "product_wise_stock_statement")
        self.assertNotIn("filename_zl", decision["reason"])

    def test_extract_opening_issues_closing_not_medica(self):
        result = extract_sales_statement(
            ZL_PWSS_IMAGE.read_bytes(), ZL_PWSS_IMAGE.name
        )
        extra = (result.get("totals") or {}).get("extra") or {}
        self.assertEqual(
            extra.get("extraction_method"), "product_wise_stock_statement_image"
        )
        by = {
            (
                str(i.get("product_name") or "").upper(),
                str(i.get("packing") or "").upper().replace(" ", ""),
            ): i
            for i in (result.get("line_items") or [])
        }
        clarina = next(
            v for k, v in by.items() if "CLARINA" in k[0] and "60ML" in k[1]
        )
        self.assertEqual(clarina["opening_qty"], 25)
        self.assertEqual(clarina["sales_qty"], 0)
        self.assertEqual(clarina["closing_qty"], 25)
        gasex = next(v for k, v in by.items() if "GASEX" in k[0] and "TAB" in k[0])
        self.assertEqual(gasex["opening_qty"], 35)
        self.assertEqual(gasex["sales_qty"], 5)
        self.assertEqual(gasex["closing_qty"], 30)


class PodUnchangedHandwrittenGuardTests(unittest.TestCase):
    def test_stock_direct_vision_module_not_imported_by_pod_split(self):
        # /split-and-extract must remain untouched by handwritten Vision routing.
        pod_paths = [
            Path("/var/www/html/splitextract/services/pod_extractor.py"),
            Path("/var/www/html/splitextract/services/split_and_extract.py"),
        ]
        for path in pod_paths:
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            self.assertNotIn("stock_direct_vision", text)
            self.assertNotIn("HANDWRITTEN_STOCK_VISION", text)
            self.assertNotIn("STOCK_HANDWRITTEN_DETECTED", text)


if __name__ == "__main__":
    unittest.main()
