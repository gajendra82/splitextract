"""P0 accuracy and OCR runtime safety.

Nullable cells, Gemini quantities without source columns, CloseStock versus
Total, headerless photos, blank cells, and a bounded OCR timeout.
"""

from __future__ import annotations

import threading
import unittest
from unittest.mock import patch

from services.base_validator import validate_sales_result
from services.document_intelligence.decisions import (
    PROVIDER_ERROR,
    UNRESOLVED,
    VERIFIED,
    decide_final_status,
)
from services.document_intelligence.pipeline import provider_error_from_ocr
from services.document_intelligence.verification import (
    gemini_vision_lacks_source_evidence,
    independently_verify,
)
from services.ocr.base import OCRResult
from services.ocr.orchestrator import OCROrchestrator, OCRSettings, ocr_readiness
from services.sales_statement_extractor import (
    _parse_htm,
    _parse_product_stock_report,
    _to_float,
    _to_nullable_float,
    empty_line_item,
)


def _gemini_photo(product: str = "SPEMAN Tab") -> dict:
    item = empty_line_item()
    item["product_name"] = product
    item["opening_qty"] = 0.0
    item["receipts_qty"] = 0.0
    item["sales_qty"] = 0.0
    item["closing_qty"] = 0.0
    item["sales_value"] = 15.0
    item["closing_value"] = 15.0
    return {
        "source_file": "photo.jpg",
        "source_format": "jpg",
        "line_items": [item],
        "totals": {"extra": {"extraction_method": "gemini_vision"}},
    }


class NullableNumberTests(unittest.TestCase):
    def test_empty_markers_and_mixed_text_are_not_zero(self):
        for value in ("", "   ", "NA", "N/A", "NULL", "NONE", "--", "-", "TAB 10", "30`S"):
            self.assertIsNone(_to_nullable_float(value), value)
        self.assertIsNone(_to_nullable_float(None))
        self.assertIsNone(_to_nullable_float(True))
        self.assertEqual(_to_nullable_float(0), 0.0)
        self.assertEqual(_to_nullable_float("0"), 0.0)
        self.assertEqual(_to_nullable_float("1,250.50"), 1250.50)
        self.assertEqual(_to_float("TAB 10"), 0.0)
        self.assertEqual(_to_float("N/A"), 0.0)
        self.assertEqual(_to_float("1,250"), 1250.0)

    def test_blank_html_cell_stays_null(self):
        html = """
        <table>
          <tr>
            <th>Product name</th><th>Unit</th><th>Op</th><th>Purc</th><th>Sale</th>
            <th>WSale</th><th>Jul Sa</th><th>Jun Sa</th><th>Sa val</th>
            <th>Cl qty</th><th>Cl val</th>
          </tr>
          <tr>
            <td>BONNISAN LIQ</td><td>100ML</td><td>59</td><td></td><td>52</td>
            <td></td><td></td><td></td><td>3390</td><td>7</td><td>400</td>
          </tr>
        </table>
        """
        result = _parse_htm(html.encode("utf-8"), "blank.html")
        item = result["line_items"][0]
        self.assertEqual(item["opening_qty"], 59.0)
        self.assertIsNone(item["receipts_qty"])
        self.assertEqual(item["sales_qty"], 52.0)
        self.assertEqual(item["closing_qty"], 7.0)


class GeminiEvidenceTests(unittest.TestCase):
    def test_headerless_jpg_quantities_are_not_verified(self):
        text = "SPEMAN Tab 15.00 15.00"
        self.assertIsNone(_parse_product_stock_report(text, "photo.jpg", "jpg"))
        result = _gemini_photo()
        self.assertTrue(gemini_vision_lacks_source_evidence(result))
        validation = validate_sales_result(result)
        verification = independently_verify(result, validation=validation)
        decision = decide_final_status(
            validation=validation,
            verification=verification,
            recovery={"status": "recovered", "accepted": True},
        )
        self.assertEqual(decision["status"], UNRESOLVED)
        self.assertFalse(decision["verified"])
        self.assertEqual(decision["reason"], "gemini_vision_without_source_evidence")
        self.assertNotEqual(decision["status"], VERIFIED)

    def test_png_total_is_not_closestk_and_ungrounded_vision_stays_unresolved(self):
        text = "\n".join(
            [
                "Product Name Opening Purchase Total Sale SaleRet Exp/Dmg CloseStock",
                "HIORA SG GEL 5 50 55 1 0 0 54",
            ]
        )
        parsed = _parse_product_stock_report(text, "stock.png", "png")
        item = parsed["line_items"][0]
        self.assertEqual(item["extra"]["total_stock"], 55.0)
        self.assertEqual(item["closing_qty"], 54.0)
        self.assertNotEqual(item["closing_qty"], item["extra"]["total_stock"])

        vision = _gemini_photo("HIORA SG GEL")
        vision["line_items"][0]["opening_qty"] = 5.0
        vision["line_items"][0]["receipts_qty"] = 50.0
        vision["line_items"][0]["sales_qty"] = 1.0
        vision["line_items"][0]["closing_qty"] = 55.0
        validation = validate_sales_result(vision)
        decision = decide_final_status(
            validation=validation,
            verification=independently_verify(vision, validation=validation),
            recovery={},
        )
        self.assertEqual(decision["status"], UNRESOLVED)
        self.assertFalse(decision["verified"])

    def test_checked_source_with_ocr_text_is_not_blocked_as_ungrounded_vision(self):
        result = _gemini_photo("HIORA SG GEL")
        result["line_items"][0]["closing_qty"] = 54.0
        result["_grounding_ocr_text"] = (
            "Product Name Opening Purchase Total Sale CloseStock\n"
            "HIORA SG GEL 5 50 55 1 54"
        )
        result["totals"]["extra"]["source_grounding"] = {
            "status": "checked",
            "compared_rows": 1,
            "discrepancy_count": 0,
        }
        self.assertFalse(gemini_vision_lacks_source_evidence(result))

    def test_native_statement_can_still_verify(self):
        decision = decide_final_status(
            validation={
                "accepted": True,
                "source_evidence": True,
                "business_validation": {"status": "consistent"},
                "reasons": [],
            },
            verification={"passed": True, "issues": []},
            recovery={},
        )
        self.assertEqual(decision["status"], VERIFIED)
        self.assertTrue(decision["verified"])


class OcrTimeoutTests(unittest.TestCase):
    def test_recognize_timeout_is_a_provider_error(self):
        release = threading.Event()

        class SlowEngine:
            name = "slow"

            def available(self) -> bool:
                return True

            def recognize(self, image_bytes, *, page=1, metadata=None):
                del image_bytes, metadata
                release.wait(2)
                return OCRResult(text="late", words=[], page=page, engine="slow")

        orchestrator = OCROrchestrator(
            settings=OCRSettings(primary_engine="slow", fallback_engine="none")
        )
        orchestrator._engines["slow"] = SlowEngine()
        try:
            with patch(
                "services.ocr.orchestrator.ocr_execution_timeout_s",
                return_value=0.05,
            ):
                bundle = orchestrator.recognize(b"png", page=1)
        finally:
            release.set()
        self.assertEqual(bundle.metadata.get("status"), "ocr_error")
        self.assertEqual(bundle.primary.metadata.get("error"), "ocr_timeout")
        self.assertTrue(bundle.primary.metadata.get("provider_error"))
        self.assertNotEqual(bundle.primary.text, "late")
        ocr_bundle = {
            "invoked_here": True,
            "status": bundle.metadata.get("status"),
            "error": bundle.primary.metadata.get("error"),
            "timed_out": True,
            "unverified_fallback": False,
        }
        self.assertEqual(provider_error_from_ocr(ocr_bundle, ocr_text=""), "ocr_timeout")
        decision = decide_final_status(
            validation={"accepted": True, "business_validation": {"status": "consistent"}},
            verification={"passed": True, "issues": []},
            recovery={},
            provider_error=provider_error_from_ocr(ocr_bundle, ocr_text=""),
        )
        self.assertEqual(decision["status"], PROVIDER_ERROR)
        self.assertEqual(decision["reason"], "ocr_timeout")
        self.assertFalse(decision["verified"])


class OcrReadinessTests(unittest.TestCase):
    def test_health_reports_ocr_readiness_separately_from_live(self):
        from fastapi.testclient import TestClient

        import app as app_module

        client = TestClient(app_module.app)
        unavailable = {
            "engine": "paddleocr-vl",
            "usable": False,
            "reason": "cudnn_cnn64_9.dll",
        }
        available = {"engine": "paddleocr-vl", "usable": True, "reason": None}
        with patch("services.ocr.orchestrator.ocr_readiness", return_value=unavailable):
            live = client.get("/live")
            health = client.get("/health")
            ready = client.get("/ready")
        self.assertEqual(live.status_code, 200)
        self.assertEqual(live.json(), {"alive": True})
        self.assertNotIn("ocr", live.json())
        self.assertEqual(health.status_code, 200)
        self.assertEqual(health.json().get("status"), "healthy")
        self.assertFalse(health.json().get("ocr_ready"))
        self.assertEqual(health.json().get("ocr"), unavailable)
        self.assertFalse(ready.json().get("ready"))
        self.assertFalse(ready.json().get("ocr_ready"))

        with patch("services.ocr.orchestrator.ocr_readiness", return_value=available):
            health_ok = client.get("/health")
            ready_ok = client.get("/ready")
            live_ok = client.get("/live")
        self.assertTrue(health_ok.json().get("ocr_ready"))
        self.assertTrue(ready_ok.json().get("ready"))
        self.assertEqual(live_ok.json(), {"alive": True})
        self.assertIn("engine", ocr_readiness())
