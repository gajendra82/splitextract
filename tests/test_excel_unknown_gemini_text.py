"""EXCEL + UNKNOWN_* + usable text → Gemini Text before Vision (narrow cost path)."""

import unittest
from threading import Lock
from types import SimpleNamespace
from unittest.mock import patch

import app as app_module
from services.excel_invoice_extract import build_excel_ocr_text


def _excel_unknown_usable_text(invoice_no: str = "UNKNOWN_6") -> str:
    return build_excel_ocr_text({
        "invoice_no": invoice_no,
        "invoice_date": "20/06/2026",
        "vendor": "TEST VENDOR PVT LTD",
        "vendor_gstin": "29AAAAA0000A1Z5",
        "customer": "TEST HOSPITAL",
        "customer_address": "Bengaluru",
        "customer_gstin": "",
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


def _gemini_full_data_with_items(invoice_no: str = "") -> dict:
    return {
        "data": {
            "invoice_summary": {
                "invoice_no": invoice_no,
                "customer": "TEST HOSPITAL",
                "vendor": "TEST VENDOR PVT LTD",
            },
            "line_items": {
                "items": [{
                    "product_description": "SAMPLE PRODUCT TAB",
                    "quantity": "10",
                    "unit_price": "100",
                    "total_amount": "1000",
                }],
                "count": 1,
            },
        }
    }


class FakePage:
    def __init__(self, page_count: int = 1, native_text: str = ""):
        self.parent = SimpleNamespace(page_count=page_count)
        self._native_text = native_text

    def get_text(self, *_args, **_kwargs):
        return self._native_text

    def get_pixmap(self, **_kwargs):
        return SimpleNamespace(tobytes=lambda _fmt: b"fake-png")


class ExcelUnknownGateTests(unittest.TestCase):
    def test_excel_unknown_usable_text_eligible(self):
        text = _excel_unknown_usable_text()
        self.assertGreater(len(text.strip()), 100)
        self.assertTrue(app_module._text_is_excel_unknown_invoice(text))
        self.assertTrue(
            app_module._should_try_gemini_text_for_excel_unknown(text)
        )

    def test_weak_excel_unknown_text_not_eligible(self):
        text = "SOURCE: EXCEL\nINVOICE NO: UNKNOWN_1"
        self.assertTrue(app_module._text_is_excel_unknown_invoice(text))
        self.assertFalse(
            app_module._should_try_gemini_text_for_excel_unknown(text)
        )

    def test_excel_with_real_invoice_not_unknown_gate(self):
        text = build_excel_ocr_text({
            "invoice_no": "INV12345",
            "invoice_date": "20/06/2026",
            "vendor": "V",
            "customer": "C",
            "line_items": [{"product_description": "P", "quantity": "1",
                            "unit_price": "2", "total_amount": "2",
                            "lot_batch_number": "B", "hsn_code": "",
                            "additional_fields": {}}],
        }) + ("\n" + "x" * 80)
        self.assertFalse(app_module._text_is_excel_unknown_invoice(text))
        self.assertFalse(
            app_module._should_try_gemini_text_for_excel_unknown(text)
        )

    def test_normal_invoice_text_not_eligible(self):
        text = (
            "TAX INVOICE\nInvoice No: 6939\nDate: 01/06/2026\n"
            + ("PRODUCT LINE " * 20)
        )
        self.assertFalse(app_module._text_is_excel_unknown_invoice(text))
        self.assertFalse(
            app_module._should_try_gemini_text_for_excel_unknown(text)
        )

    def test_stockist_hyd26_blocks_excel_unknown_gate(self):
        text = _excel_unknown_usable_text()
        self.assertFalse(
            app_module._should_try_gemini_text_for_excel_unknown(
                text, pdf_path="/tmp/HYD-26-12345.pdf",
            )
        )

    def test_heavy_multipage_scan_unchanged(self):
        page = FakePage(page_count=29, native_text="")
        self.assertTrue(app_module._is_heavy_multipage_image_scan(page))
        self.assertTrue(
            app_module._should_skip_tesseract_for_heavy_scan(
                page, pdf_path="/tmp/Split_1_1_to_29.pdf", ocr_hint="",
            )
        )


class ExcelUnknownUsabilityTests(unittest.TestCase):
    def test_accepts_sentinel_when_line_items_present(self):
        text = _excel_unknown_usable_text("UNKNOWN_6")
        ok, inv = app_module._excel_unknown_gemini_text_usable(
            _gemini_full_data_with_items(""), text
        )
        self.assertTrue(ok)
        self.assertEqual(inv, "UNKNOWN_6")

    def test_rejects_empty_or_no_line_items(self):
        text = _excel_unknown_usable_text()
        ok, _ = app_module._excel_unknown_gemini_text_usable(None, text)
        self.assertFalse(ok)
        ok, _ = app_module._excel_unknown_gemini_text_usable(
            {"data": {"invoice_summary": {}, "line_items": {"items": []}}},
            text,
        )
        self.assertFalse(ok)

    def test_rejects_ungrounded_invented_invoice_keeps_sentinel(self):
        text = _excel_unknown_usable_text("UNKNOWN_3")
        ok, inv = app_module._excel_unknown_gemini_text_usable(
            _gemini_full_data_with_items("HALLUCINATED-999"), text
        )
        self.assertTrue(ok)
        self.assertEqual(inv, "UNKNOWN_3")

    def test_sync_overwrites_hallucinated_summary_invoice_no(self):
        full_data = _gemini_full_data_with_items("HALLUCINATED-999")
        app_module._sync_invoice_no_into_full_data(full_data, "UNKNOWN_6")
        self.assertEqual(full_data.get("invoice_no"), "UNKNOWN_6")
        self.assertEqual(
            full_data["data"]["invoice_summary"]["invoice_no"], "UNKNOWN_6"
        )
        # Downstream canonicalization prefers summary — must match sentinel.
        summary_first = full_data["data"]["invoice_summary"]["invoice_no"]
        self.assertEqual(summary_first, "UNKNOWN_6")
        self.assertNotIn("HALLUCINATED-999", str(full_data))


class ExcelUnknownRoutingIntegrationTests(unittest.TestCase):
    def _stats(self):
        return {}, Lock()

    def test_hallucinated_gemini_invoice_keeps_unknown_in_full_data(self):
        """Pre-deploy bug: page UNKNOWN_* but summary kept HALLUCINATED-999."""
        text = _excel_unknown_usable_text("UNKNOWN_6")
        page = FakePage(page_count=1, native_text=text)
        ocr_stats, lock = self._stats()
        # Gemini invents an invoice# not present in the EXCEL text.
        full_data = _gemini_full_data_with_items("HALLUCINATED-999")

        with patch.object(app_module, "PDFPLUMBER_AVAILABLE", True), \
                patch.object(
                    app_module, "extract_text_with_pdfplumber",
                    return_value=(text, 95.0),
                ), \
                patch.object(
                    app_module, "extract_full_data_from_text_gemini",
                    return_value=full_data,
                ), \
                patch.object(
                    app_module, "extract_full_data_from_image_gemini",
                ) as mock_vision, \
                patch.object(app_module, "TESSERACT_AVAILABLE", False):
            result = app_module.extract_full_invoice_data_combined(
                page, pdf_path="/tmp/excel_unknown.pdf", page_num=0,
                ocr_stats=ocr_stats, ocr_stats_lock=lock,
            )

        self.assertIsNotNone(result)
        self.assertEqual(result.get("invoice_no"), "UNKNOWN_6")
        result_full = result.get("full_data") or {}
        summary_inv = (
            (result_full.get("data") or {})
            .get("invoice_summary", {})
            .get("invoice_no")
        )
        self.assertEqual(summary_inv, "UNKNOWN_6")
        self.assertEqual(result_full.get("invoice_no"), "UNKNOWN_6")
        self.assertNotIn("HALLUCINATED-999", str(result_full))
        mock_vision.assert_not_called()
        # Line items preserved (do not discard good extraction).
        self.assertGreaterEqual(
            app_module._count_extracted_line_items(result_full), 1
        )

    def test_grounded_gemini_invoice_accepted(self):
        # Real invoice number appears in EXCEL text alongside UNKNOWN sentinel label
        # only if we use a document that has a grounded number in body text.
        # Gate requires UNKNOWN_* in INVOICE NO — grounded accept path uses a
        # number that also appears elsewhere in the usable text.
        text = _excel_unknown_usable_text("UNKNOWN_6")
        text = text + "\nALTERNATE DOCUMENT REF: VGP/27/4697\n"
        page = FakePage(page_count=1, native_text=text)
        ocr_stats, lock = self._stats()
        full_data = _gemini_full_data_with_items("VGP/27/4697")

        with patch.object(app_module, "PDFPLUMBER_AVAILABLE", True), \
                patch.object(
                    app_module, "extract_text_with_pdfplumber",
                    return_value=(text, 95.0),
                ), \
                patch.object(
                    app_module, "extract_full_data_from_text_gemini",
                    return_value=full_data,
                ), \
                patch.object(
                    app_module, "extract_full_data_from_image_gemini",
                ) as mock_vision, \
                patch.object(app_module, "TESSERACT_AVAILABLE", False):
            result = app_module.extract_full_invoice_data_combined(
                page, pdf_path="/tmp/excel_grounded.pdf", page_num=0,
                ocr_stats=ocr_stats, ocr_stats_lock=lock,
            )

        self.assertEqual(result.get("invoice_no"), "VGP/27/4697")
        summary_inv = (
            (result.get("full_data") or {})
            .get("data", {})
            .get("invoice_summary", {})
            .get("invoice_no")
        )
        self.assertEqual(summary_inv, "VGP/27/4697")
        mock_vision.assert_not_called()

    def test_excel_unknown_usable_text_uses_gemini_text_not_vision(self):
        text = _excel_unknown_usable_text()
        page = FakePage(page_count=1, native_text=text)
        ocr_stats, lock = self._stats()
        full_data = _gemini_full_data_with_items("")

        with patch.object(app_module, "PDFPLUMBER_AVAILABLE", True), \
                patch.object(
                    app_module, "extract_text_with_pdfplumber",
                    return_value=(text, 95.0),
                ), \
                patch.object(
                    app_module, "extract_full_data_from_text_gemini",
                    return_value=full_data,
                ) as mock_text, \
                patch.object(
                    app_module, "extract_full_data_from_image_gemini",
                ) as mock_vision, \
                patch.object(app_module, "TESSERACT_AVAILABLE", False), \
                patch.object(app_module, "log_pod_ocr_routing") as mock_log:
            result = app_module.extract_full_invoice_data_combined(
                page, pdf_path="/tmp/excel_unknown.pdf", page_num=0,
                ocr_stats=ocr_stats, ocr_stats_lock=lock,
            )

        self.assertIsNotNone(result)
        self.assertEqual(result.get("invoice_no"), "UNKNOWN_6")
        self.assertEqual(result.get("ocr_method"), "pdfplumber")
        mock_text.assert_called()
        mock_vision.assert_not_called()
        reasons = [
            c.kwargs.get("reason") for c in mock_log.call_args_list
            if c.kwargs.get("reason")
        ]
        self.assertIn("excel_unknown_invoice_usable_text", reasons)

    def test_excel_unknown_gemini_text_failure_falls_back_to_vision(self):
        text = _excel_unknown_usable_text()
        page = FakePage(page_count=1, native_text=text)
        ocr_stats, lock = self._stats()
        vision_result = {
            "invoice_no": "UNKNOWN_6",
            "full_data": _gemini_full_data_with_items("UNKNOWN_6"),
            "ocr_text": text,
        }

        with patch.object(app_module, "PDFPLUMBER_AVAILABLE", True), \
                patch.object(
                    app_module, "extract_text_with_pdfplumber",
                    return_value=(text, 95.0),
                ), \
                patch.object(
                    app_module, "extract_full_data_from_text_gemini",
                    return_value=None,
                ), \
                patch.object(
                    app_module, "extract_full_data_from_image_gemini",
                    return_value=vision_result,
                ) as mock_vision, \
                patch.object(app_module, "TESSERACT_AVAILABLE", False), \
                patch.object(app_module, "log_pod_ocr_routing") as mock_log:
            result = app_module.extract_full_invoice_data_combined(
                page, pdf_path="/tmp/excel_unknown.pdf", page_num=0,
                ocr_stats=ocr_stats, ocr_stats_lock=lock,
            )

        self.assertIsNotNone(result)
        mock_vision.assert_called()
        reasons = [
            c.kwargs.get("reason") for c in mock_log.call_args_list
            if c.kwargs.get("reason")
        ]
        self.assertIn("excel_unknown_gemini_text_fallback", reasons)

    def test_normal_invoice_with_number_unchanged(self):
        text = (
            "TAX INVOICE\nInvoice No: 6939\nVendor: ABC PHARMA\n"
            "Customer: Hospital\n" + ("Item A Qty 1 Rate 10 " * 15)
        )
        page = FakePage(page_count=1, native_text=text)
        ocr_stats, lock = self._stats()
        full_data = _gemini_full_data_with_items("6939")

        with patch.object(app_module, "PDFPLUMBER_AVAILABLE", True), \
                patch.object(
                    app_module, "extract_text_with_pdfplumber",
                    return_value=(text, 95.0),
                ), \
                patch.object(
                    app_module, "try_extract_invoice_from_text",
                    return_value="6939",
                ), \
                patch.object(
                    app_module, "extract_full_data_from_text_gemini",
                    return_value=full_data,
                ) as mock_text, \
                patch.object(
                    app_module, "extract_full_data_from_image_gemini",
                ) as mock_vision, \
                patch.object(
                    app_module, "_attempt_excel_unknown_gemini_text",
                ) as mock_excel, \
                patch.object(app_module, "TESSERACT_AVAILABLE", False):
            result = app_module.extract_full_invoice_data_combined(
                page, pdf_path="/tmp/normal_invoice.pdf", page_num=0,
                ocr_stats=ocr_stats, ocr_stats_lock=lock,
            )

        self.assertEqual(result.get("invoice_no"), "6939")
        mock_text.assert_called()
        mock_vision.assert_not_called()
        mock_excel.assert_not_called()

    def test_stockist_forced_tesseract_wins_over_excel_unknown(self):
        text = _excel_unknown_usable_text()
        self.assertIsNotNone(
            app_module._stockist_requires_tesseract_ocr(
                "/tmp/HYD-26-99999.pdf", text
            )
        )
        self.assertFalse(
            app_module._should_try_gemini_text_for_excel_unknown(
                text, pdf_path="/tmp/HYD-26-99999.pdf",
            )
        )
        result, attempted = app_module._attempt_excel_unknown_gemini_text(
            text,
            pdf_path="/tmp/HYD-26-99999.pdf",
            ocr_stats={},
            ocr_stats_lock=Lock(),
            ocr_method="pdfplumber",
            ocr_confidence=90.0,
            page=FakePage(),
            page_num=0,
        )
        self.assertIsNone(result)
        self.assertFalse(attempted)


if __name__ == "__main__":
    unittest.main()
