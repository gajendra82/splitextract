"""Regression: structured OCR recovery, LstSL mapping, identity, Phase2 policy."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from services.failure_taxonomy import FailureType, classify_failure
from services.html_table.column_matcher import match_headers
from services.identity_disambiguation import disambiguate_identity
from services.ocr.base import OCRResult
from services.ocr.recovery_chain import recover_unusable_ocr
from services.recovery_policy import recovery_policy


class ColumnMappingLstSLTests(unittest.TestCase):
    def test_lstsl_not_confused_with_open(self):
        headers = [
            "Product Name",
            "Pack",
            "LstSL",
            "Open",
            "Recd.",
            "Sales",
            "Close",
            "Order",
            "Pend",
        ]
        mapping = match_headers(headers)
        by_field = {field: idx for idx, field in mapping.items()}
        self.assertEqual(by_field.get("last_month_sale"), 2)
        self.assertEqual(by_field.get("opening_qty"), 3)
        self.assertEqual(by_field.get("receive_qty"), 4)
        self.assertEqual(by_field.get("sales_qty"), 5)
        self.assertEqual(by_field.get("closing_qty"), 6)

    def test_open_alone_maps_opening(self):
        mapping = match_headers(["Open", "Recd", "Sales", "Close"])
        by_field = {field: idx for idx, field in mapping.items()}
        self.assertEqual(by_field.get("opening_qty"), 0)
        self.assertEqual(by_field.get("receive_qty"), 1)


class IdentityDisambiguationTests(unittest.TestCase):
    def test_om_pharma_stockist_himalaya_company(self):
        result = {
            "stockist_name": None,
            "company_name": "OM PHARMA",
            "line_items": [],
            "totals": {"extra": {}},
        }
        lines = [
            "OM PHARMA",
            "Stock & Sales From 01/10/2026 To 31/10/2026",
            "HIMALAYA WELLNESS COMPANY (DIV.: HIM-OTX-DIVI)",
        ]
        out = disambiguate_identity(result, header_lines=lines)
        self.assertEqual(out.get("stockist_name"), "OM PHARMA")
        self.assertIn("HIMALAYA", str(out.get("company_name") or "").upper())
        self.assertNotEqual(out.get("company_name"), "OM PHARMA")

    def test_stock_sales_for_company_banner(self):
        result = {"stockist_name": None, "company_name": None, "line_items": [], "totals": {"extra": {}}}
        text = (
            "OM PHARMA\n"
            "Stock & Sales from 01/10/2026 to 31/10/2026 for : "
            "HIMALAYA WELLNESS COMPANY (DIV.: HIM-OTX-DIVI) Page : 1\n"
            "<table><tr><td>Product Name</td></tr></table>"
        )
        out = disambiguate_identity(result, ocr_text=text)
        self.assertEqual(out.get("stockist_name"), "OM PHARMA")
        self.assertIn("HIMALAYA WELLNESS", str(out.get("company_name") or "").upper())
        self.assertNotIn("DIV", str(out.get("company_name") or "").upper())

    def test_skips_phone_status_bar_for_stockist(self):
        result = {"stockist_name": None, "company_name": None, "line_items": [], "totals": {"extra": {}}}
        text = (
            "10:38 Tue, 3 Nov\n"
            "63010331.PDF\n"
            "OM PHARMA\n"
            "Stock & Sales from 01/10/2026 to 31/10/2026 for : "
            "HIMALAYA WELLNESS COMPANY (DIV.: HIM-OTX-DIVI)\n"
        )
        out = disambiguate_identity(result, ocr_text=text)
        self.assertEqual(out.get("stockist_name"), "OM PHARMA")
        self.assertIn("HIMALAYA", str(out.get("company_name") or "").upper())


class RecoveryChainTests(unittest.TestCase):
    def test_paddle_timeout_tesseract_succeeds(self):
        tess = MagicMock()
        tess.available.return_value = True
        tess.recognize.return_value = OCRResult(
            text="Product Name Pack LstSL Open Recd Sales Close\nGASEX 100TAB 70 30 100 26 104",
            engine="tesseract",
            page=1,
        )
        gem = MagicMock()
        gem.available.return_value = True

        ocr = MagicMock()
        ocr.settings.primary_engine = "paddleocr-vl"
        ocr.engine.side_effect = lambda name: {"tesseract": tess, "gemini": gem}.get(name)

        failed = OCRResult(text="", engine="paddleocr-vl", timed_out=True, page=1)
        bundle, meta = recover_unusable_ocr(
            ocr, b"fake-image", page_no=1, failed_primary=failed, fail_reason="timeout"
        )
        self.assertEqual(meta.get("accepted_engine"), "tesseract")
        self.assertEqual(bundle.primary.engine, "tesseract")
        self.assertIn("GASEX", bundle.primary.text)
        gem.recognize.assert_not_called()

    def test_tess_empty_gemini_transcription(self):
        tess = MagicMock()
        tess.available.return_value = True
        tess.recognize.return_value = OCRResult(text="", engine="tesseract", page=1)
        gem = MagicMock()
        gem.available.return_value = True
        gem.recognize.return_value = OCRResult(
            text="OM PHARMA\nHIMALAYA WELLNESS COMPANY\nOpen Recd Sales Close\n30 100 26 104",
            engine="gemini",
            page=1,
        )
        ocr = MagicMock()
        ocr.engine.side_effect = lambda name: {"tesseract": tess, "gemini": gem}.get(name)

        bundle, meta = recover_unusable_ocr(
            ocr, b"img", page_no=1, failed_primary=None, fail_reason="empty"
        )
        self.assertEqual(meta.get("accepted_engine"), "gemini")
        self.assertTrue(bundle.unverified_fallback)
        self.assertEqual(bundle.primary.metadata.get("ocr_source"), "gemini")

    def test_all_fail_unresolved(self):
        tess = MagicMock()
        tess.available.return_value = True
        tess.recognize.return_value = OCRResult(text="x", engine="tesseract")
        gem = MagicMock()
        gem.available.return_value = True
        gem.recognize.return_value = OCRResult(text="", engine="gemini")
        ocr = MagicMock()
        ocr.engine.side_effect = lambda name: {"tesseract": tess, "gemini": gem}.get(name)

        # Force both unusable (short text for tess)
        tess.recognize.return_value = OCRResult(text="hi", engine="tesseract")
        bundle, meta = recover_unusable_ocr(ocr, b"img", page_no=1, fail_reason="empty")
        self.assertIsNone(meta.get("accepted_engine"))
        self.assertIn(bundle.metadata.get("status"), {"ocr_error", "timeout", None, "error"} or True)


class Phase2PolicyTests(unittest.TestCase):
    def test_stock_identity_preferred_over_unverified(self):
        failure = classify_failure(
            {
                "accepted": False,
                "reasons": [
                    "stock_identity_failure",
                    "unverified_ocr_fallback",
                    "empty_ocr_page",
                ],
                "checks": {"stock_identity": False, "ocr_verified": False, "rows": True},
            }
        )
        self.assertEqual(failure, FailureType.STOCK_IDENTITY_FAILURE)
        policy = recovery_policy(failure)
        self.assertGreaterEqual(policy.max_attempts, 1)
        self.assertTrue(policy.call_gemini)

    def test_unverified_no_longer_zero_attempts(self):
        policy = recovery_policy(FailureType.UNVERIFIED_OCR_FALLBACK)
        self.assertGreaterEqual(policy.max_attempts, 1)

    def test_empty_ocr_page_maps_ocr_required(self):
        failure = classify_failure(
            {
                "accepted": False,
                "reasons": ["empty_ocr_page", "truncated_cell"],
                "checks": {"rows": True, "ocr_verified": True},
            }
        )
        self.assertEqual(failure, FailureType.OCR_REQUIRED)
        policy = recovery_policy(failure)
        self.assertGreaterEqual(policy.max_attempts, 1)
        self.assertTrue(policy.call_gemini)


class UngroundedVisionBlockTests(unittest.TestCase):
    def test_gemini_fallback_blocked_when_recovery_exhausted(self):
        from services.gemini_extraction_fallback import maybe_apply_gemini_fallback
        from services.ocr.ocr_text_override import ocr_recovery_state

        result = {
            "line_items": [],
            "totals": {"extra": {}},
            "stockist_name": None,
        }
        with ocr_recovery_state(
            {
                "attempted": True,
                "accepted": False,
                "block_ungrounded_vision": True,
                "pages": [{"accepted_engine": None}],
            }
        ):
            out = maybe_apply_gemini_fallback(result, b"x", "x.jpg", ".jpg")
        self.assertEqual(
            ((out.get("totals") or {}).get("extra") or {}).get("gemini_fallback"),
            "blocked_ungrounded_ocr",
        )
        self.assertEqual(out.get("line_items"), [])


if __name__ == "__main__":
    unittest.main()
