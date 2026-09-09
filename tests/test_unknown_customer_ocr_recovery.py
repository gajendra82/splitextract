"""UNKNOWN_* customer recovery: OCR/native text before Vision (no extra Gemini)."""

import unittest
from threading import Lock
from unittest.mock import Mock, patch

import app as app_module
from services.excel_invoice_extract import build_excel_ocr_text


VALID_CUSTOMER_GSTIN = "27AASPH3785P1ZP"
VALID_VENDOR_GSTIN = "29AAHCA1186B1Z0"
MALFORMED_GSTIN = "27ABCDEFGHIJKLM"


def _excel_ocr(
    invoice_no="UNKNOWN_6",
    customer="TEST HOSPITAL",
    customer_address="12 MG Road, Bengaluru 560001",
    customer_gstin=VALID_CUSTOMER_GSTIN,
    vendor="TEST VENDOR PVT LTD",
    vendor_gstin=VALID_VENDOR_GSTIN,
):
    return build_excel_ocr_text({
        "invoice_no": invoice_no,
        "invoice_date": "20/06/2026",
        "vendor": vendor,
        "vendor_gstin": vendor_gstin,
        "customer": customer,
        "customer_address": customer_address,
        "customer_gstin": customer_gstin,
        "tax": "100",
        "total": "1100",
        "irn": "",
        "line_items": [{
            "product_description": "SAMPLE PRODUCT TAB",
            "quantity": "10",
            "unit_price": "100",
            "total_amount": "1000",
            "lot_batch_number": "B1",
            "hsn_code": "3004",
            "additional_fields": {"mrp": "120"},
        }],
    })


def _bill_to_ocr(gstin=VALID_CUSTOMER_GSTIN):
    return (
        "TAX INVOICE\n"
        "TEST VENDOR PVT LTD\n"
        f"VENDOR GSTIN: {VALID_VENDOR_GSTIN}\n"
        "BILL TO\n"
        "CITY CARE HOSPITAL PVT LTD\n"
        "45 Residency Road, Bengaluru 560025\n"
        f"GSTIN: {gstin}\n"
        "HSN 3004\n"
    )


def _run_like_grouping(
    invoice_no,
    ocr_text,
    vision,
    current_customer="",
    current_customer_address="",
    current_customer_gstin="",
    current_vendor="TEST VENDOR PVT LTD",
    current_vendor_gstin=VALID_VENDOR_GSTIN,
):
    """Mirrors the grouping gate: OCR first for UNKNOWN_*; else existing Vision."""
    ocr_hit = app_module._unknown_invoice_customer_from_ocr_if_sufficient(
        invoice_no,
        ocr_text,
        current_customer=current_customer,
        current_customer_address=current_customer_address,
        current_customer_gstin=current_customer_gstin,
        current_vendor=current_vendor,
        current_vendor_gstin=current_vendor_gstin,
        page_num=1,
    )
    if ocr_hit is not None:
        return ocr_hit
    return vision(
        b"fake-png",
        current_customer,
        current_customer_address,
        current_customer_gstin,
        current_vendor,
        ocr_text,
        {},
        Lock(),
    )


class UnknownCustomerOcrRecoveryTests(unittest.TestCase):
    def test_unknown_labeled_gstin_skips_vision(self):
        vision = Mock(return_value={"customer": "FROM_VISION"})
        result = _run_like_grouping(
            "UNKNOWN_6", _excel_ocr(), vision)
        self.assertEqual(result["customer"], "TEST HOSPITAL")
        self.assertEqual(result["customer_gstin"], VALID_CUSTOMER_GSTIN)
        self.assertIn("Bengaluru", result["customer_address"])
        vision.assert_not_called()

    def test_unknown_bill_to_name_address_gstin_skips_vision(self):
        vision = Mock()
        result = _run_like_grouping("UNKNOWN_3", _bill_to_ocr(), vision)
        self.assertEqual(result["customer"], "CITY CARE HOSPITAL PVT LTD")
        self.assertEqual(result["customer_gstin"], VALID_CUSTOMER_GSTIN)
        self.assertIn("Residency Road", result["customer_address"])
        vision.assert_not_called()

    def test_unknown_name_address_plus_existing_gstin_skips_vision(self):
        """Name/address from OCR is sufficient when GSTIN is already on the summary."""
        vision = Mock()
        ocr = _excel_ocr(customer_gstin="")
        result = _run_like_grouping(
            "UNKNOWN_1",
            ocr,
            vision,
            current_customer="",
            current_customer_address="",
            current_customer_gstin=VALID_CUSTOMER_GSTIN,
        )
        self.assertEqual(result["customer"], "TEST HOSPITAL")
        self.assertEqual(result["customer_gstin"], VALID_CUSTOMER_GSTIN)
        vision.assert_not_called()

    def test_unknown_insufficient_ocr_calls_existing_vision(self):
        vision_result = {
            "customer": "VISION HOSPITAL",
            "customer_address": "Vision Street",
            "customer_gstin": VALID_CUSTOMER_GSTIN,
        }
        vision = Mock(return_value=vision_result)
        result = _run_like_grouping(
            "UNKNOWN_9",
            "NOISE ONLY\nPAGE 1\nNO BUYER BLOCK",
            vision,
        )
        self.assertEqual(result, vision_result)
        vision.assert_called_once()

    def test_unknown_malformed_gstin_falls_back_to_vision(self):
        vision = Mock(return_value={
            "customer": "VISION HOSPITAL",
            "customer_address": "Vision Street",
            "customer_gstin": VALID_CUSTOMER_GSTIN,
        })
        ocr = _excel_ocr(customer_gstin=MALFORMED_GSTIN)
        result = _run_like_grouping("UNKNOWN_6", ocr, vision)
        vision.assert_called_once()
        self.assertEqual(result["customer"], "VISION HOSPITAL")
        extracted = app_module._extract_reliable_customer_data_from_existing_ocr(
            ocr, current_vendor_gstin=VALID_VENDOR_GSTIN)
        self.assertEqual(extracted["customer_gstin"], "")

    def test_real_invoice_number_does_not_use_ocr_gate(self):
        vision = Mock(return_value={"customer": "FROM_VISION"})
        result = _run_like_grouping("INV-26-12345", _excel_ocr(), vision)
        vision.assert_called_once()
        self.assertEqual(result["customer"], "FROM_VISION")
        self.assertIsNone(
            app_module._unknown_invoice_customer_from_ocr_if_sufficient(
                "INV-26-12345",
                _excel_ocr(),
                current_vendor="TEST VENDOR PVT LTD",
                current_vendor_gstin=VALID_VENDOR_GSTIN,
            )
        )

    def test_stockist_rules_unchanged(self):
        self.assertEqual(
            app_module._stockist_requires_tesseract_ocr(
                "/tmp/HYD-26-12345.pdf", ""),
            "hyd26_filename",
        )
        with patch.object(
            app_module, "ocr_suggests_novacare_stockist", return_value=True
        ):
            self.assertEqual(
                app_module._stockist_requires_tesseract_ocr(None, "hint"),
                "novacare_del26",
            )
        with patch.object(
            app_module, "ocr_suggests_bharath_medical", return_value=True
        ):
            self.assertEqual(
                app_module._stockist_requires_tesseract_ocr(None, "hint"),
                "bharath_medical",
            )
        self.assertFalse(
            app_module._should_skip_tesseract_for_heavy_scan(
                None, pdf_path="/tmp/Split_1_1_to_29.pdf", ocr_hint="",
            )
        )

    def test_excel_unknown_text_gate_unchanged(self):
        text = _excel_ocr()
        self.assertTrue(app_module._text_is_excel_unknown_invoice(text))
        self.assertTrue(
            app_module._should_try_gemini_text_for_excel_unknown(text)
        )
        result, attempted = app_module._attempt_excel_unknown_gemini_text(
            text,
            pdf_path="/tmp/HYD-26-99999.pdf",
            ocr_stats={},
            ocr_stats_lock=Lock(),
            ocr_method="pdfplumber",
            ocr_confidence=90.0,
            page=object(),
            page_num=0,
        )
        self.assertIsNone(result)
        self.assertFalse(attempted)

    def test_insufficient_ocr_keeps_vision_payload(self):
        vision_result = {
            "customer": "KEEP ME",
            "customer_address": "Keep Address",
            "customer_gstin": VALID_CUSTOMER_GSTIN,
        }
        vision = Mock(return_value=vision_result)
        self.assertEqual(
            _run_like_grouping("UNKNOWN_2", "   ", vision),
            vision_result,
        )

    def test_does_not_hallucinate_customer_from_unlabeled_noise(self):
        extracted = app_module._extract_reliable_customer_data_from_existing_ocr(
            "TOTAL 1200\nHSN 30049099\nPAGE 2 OF 2\nSOME RANDOM CLINIC WORD",
            current_vendor="TEST VENDOR PVT LTD",
        )
        self.assertEqual(extracted["customer"], "")
        self.assertEqual(extracted["customer_address"], "")
        self.assertEqual(extracted["customer_gstin"], "")

    def test_no_additional_gemini_text_call(self):
        with patch.object(
            app_module, "extract_full_data_from_text_gemini"
        ) as mock_text:
            app_module._unknown_invoice_customer_from_ocr_if_sufficient(
                "UNKNOWN_6",
                _excel_ocr(),
                current_vendor="TEST VENDOR PVT LTD",
                current_vendor_gstin=VALID_VENDOR_GSTIN,
            )
            mock_text.assert_not_called()

    def test_vendor_gstin_not_used_as_customer_gstin(self):
        ocr = _excel_ocr(customer_gstin=VALID_VENDOR_GSTIN)
        extracted = app_module._extract_reliable_customer_data_from_existing_ocr(
            ocr, current_vendor_gstin=VALID_VENDOR_GSTIN)
        self.assertEqual(extracted["customer_gstin"], "")

    def test_arbitrary_15_char_string_rejected(self):
        self.assertEqual(
            app_module._strict_indian_gstin("ABCDEFGHIJKLMNO"), "")
        self.assertEqual(
            app_module._strict_indian_gstin("123456789012345"), "")
        self.assertEqual(
            app_module._strict_indian_gstin(VALID_CUSTOMER_GSTIN),
            VALID_CUSTOMER_GSTIN,
        )

    def test_sufficient_log_reason_has_no_pii(self):
        with self.assertLogs("app", level="INFO") as cm:
            app_module._unknown_invoice_customer_from_ocr_if_sufficient(
                "UNKNOWN_6",
                _excel_ocr(),
                current_vendor="TEST VENDOR PVT LTD",
                current_vendor_gstin=VALID_VENDOR_GSTIN,
            )
        joined = "\n".join(cm.output)
        self.assertIn("reason=unknown_customer_ocr_sufficient", joined)
        self.assertIn("strategy=ocr", joined)
        self.assertIn("fields=name,address,gstin", joined)
        self.assertNotIn(VALID_CUSTOMER_GSTIN, joined)
        self.assertNotIn("TEST HOSPITAL", joined)
        self.assertNotIn("Bengaluru", joined)

    def test_insufficient_log_reason(self):
        with self.assertLogs("app", level="INFO") as cm:
            app_module._unknown_invoice_customer_from_ocr_if_sufficient(
                "UNKNOWN_6",
                "unreadable scan",
            )
        joined = "\n".join(cm.output)
        self.assertIn("reason=unknown_customer_ocr_insufficient", joined)
        self.assertIn("strategy=vision", joined)


if __name__ == "__main__":
    unittest.main()
