"""PaddleOCR-VL primary with a mocked Gemini fallback. No live API calls."""

from __future__ import annotations

import logging
import time
import unittest
from unittest import mock

from services.ocr.base import OCRResult
from services.ocr.gemini_fallback import GeminiOCRFallback
from services.ocr.orchestrator import OCROrchestrator, OCRSettings


class _Fake:
    def __init__(self, name: str, text: str = "ok", *, status: str = "success", available: bool = True, error: str = ""):
        self.name = name
        self.text = text
        self.status = status
        self._available = available
        self.error = error
        self.calls = 0
        self.raise_exc = None

    def available(self) -> bool:
        return self._available

    def recognize(self, image_bytes, *, page=1, metadata=None):
        self.calls += 1
        if self.raise_exc is not None:
            raise self.raise_exc
        meta = {"status": self.status}
        if self.error:
            meta["error"] = self.error
        return OCRResult(
            text=self.text,
            words=[],
            confidence=0.2,
            page=page,
            engine=self.name,
            metadata=meta,
        )


def _orch(primary: _Fake, fallback: _Fake) -> OCROrchestrator:
    return OCROrchestrator(
        settings=OCRSettings(primary_engine="paddleocr-vl", fallback_engine="gemini"),
        paddle_vl=primary,
        gemini=fallback,
    )


class GeminiFallbackTests(unittest.TestCase):
    def test_valid_paddle_does_not_call_gemini(self):
        paddle = _Fake("paddleocr-vl-1.6", "Opening 10")
        gemini = _Fake("gemini", "should not run")
        bundle = _orch(paddle, gemini).recognize(b"img")
        self.assertEqual(paddle.calls, 1)
        self.assertEqual(gemini.calls, 0)
        self.assertEqual(bundle.primary.engine, "paddleocr-vl-1.6")
        self.assertEqual(bundle.primary.text, "Opening 10")
        self.assertFalse(bundle.unverified_fallback)
        self.assertEqual(bundle.metadata["status"], "success")

    def test_low_confidence_does_not_call_gemini(self):
        paddle = _Fake("paddleocr-vl-1.6", "Opening 10")
        gemini = _Fake("gemini", "should not run")
        bundle = _orch(paddle, gemini).recognize(b"img", low_confidence=True)
        self.assertEqual(gemini.calls, 0)
        self.assertEqual(bundle.primary.engine, "paddleocr-vl-1.6")

    def test_empty_paddle_calls_gemini_and_labels_it(self):
        paddle = _Fake("paddleocr-vl-1.6", "   ")
        gemini = _Fake("gemini", "Printed line")
        bundle = _orch(paddle, gemini).recognize(b"img")
        self.assertEqual(gemini.calls, 1)
        self.assertEqual(bundle.primary.engine, "gemini")
        self.assertEqual(bundle.primary.text, "Printed line")
        self.assertEqual(bundle.secondary.engine, "paddleocr-vl-1.6")
        self.assertTrue(bundle.unverified_fallback)
        self.assertEqual(bundle.metadata["fallback_reason"], "empty_output")
        self.assertEqual(bundle.metadata["fallback_to"], "gemini")

    def test_paddle_error_and_exception_call_gemini(self):
        errored = _Fake("paddleocr-vl-1.6", "", status="error", error="worker_failed")
        gemini = _Fake("gemini", "from error")
        bundle = _orch(errored, gemini).recognize(b"img")
        self.assertEqual(bundle.primary.text, "from error")
        self.assertIn("worker_failed", bundle.metadata["fallback_reason"])

        crashed = _Fake("paddleocr-vl-1.6", "unused")
        crashed.raise_exc = RuntimeError("vl crashed")
        gemini2 = _Fake("gemini", "from exception")
        bundle = _orch(crashed, gemini2).recognize(b"img")
        self.assertEqual(gemini2.calls, 1)
        self.assertEqual(bundle.primary.engine, "gemini")
        self.assertIn("vl crashed", bundle.metadata["fallback_reason"])

    def test_both_engines_fail_returns_error_without_paddle_label(self):
        paddle = _Fake("paddleocr-vl-1.6", "", status="error", error="empty")
        gemini = _Fake("gemini", "", status="error", error="gemini down")
        bundle = _orch(paddle, gemini).recognize(b"img")
        self.assertEqual(bundle.primary.text, "")
        self.assertEqual(bundle.primary.engine, "gemini")
        self.assertNotEqual(bundle.primary.engine, "paddleocr-vl-1.6")
        self.assertEqual(bundle.metadata["status"], "ocr_error")
        self.assertFalse(bundle.unverified_fallback)
        self.assertFalse(bundle.metadata.get("unverified_fallback"))
        self.assertEqual(bundle.secondary.engine, "paddleocr-vl-1.6")
        self.assertEqual(bundle.secondary.metadata["error"], "empty")
        self.assertEqual(bundle.metadata["error"], "gemini down")
        self.assertIn("gemini", bundle.engines_used)
        self.assertIn("paddleocr-vl-1.6", bundle.engines_used)

    def test_fallback_log_names_engine_and_reason(self):
        paddle = _Fake("paddleocr-vl-1.6", "")
        gemini = _Fake("gemini", "text")
        with self.assertLogs("services.ocr.orchestrator", level="INFO") as logs:
            _orch(paddle, gemini).recognize(b"img")
        joined = "\n".join(logs.output)
        self.assertIn("ocr_fallback from=paddleocr-vl to=gemini reason=empty_output", joined)
        self.assertIn("ocr_engine_used engine=gemini fallback=true", joined)

    def test_layout_calls_can_disable_fallback(self):
        paddle = _Fake("paddleocr-vl-1.6", "")
        gemini = _Fake("gemini", "should not run")
        bundle = _orch(paddle, gemini).recognize(b"img", use_fallback=False)
        self.assertEqual(gemini.calls, 0)
        self.assertEqual(bundle.metadata["status"], "ocr_error")
        self.assertEqual(bundle.metadata["fallback_skipped"], "caller_disabled")

    def test_gemini_timeout_returns_without_waiting(self):
        def hang(_image, _timeout):
            time.sleep(5)
            return "late"

        engine = GeminiOCRFallback(invoke=hang, timeout_s=0.2)
        started = time.monotonic()
        result = engine.recognize(b"\x89PNG\r\n")
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 1.0)
        self.assertEqual(result.engine, "gemini")
        self.assertEqual(result.text, "")
        self.assertEqual(result.metadata["status"], "error")
        self.assertIn("timed out", result.metadata["error"])

    def test_gemini_text_is_not_reported_as_paddle(self):
        engine = GeminiOCRFallback(invoke=lambda _image, _timeout: "  line one\nline two  ", timeout_s=1)
        result = engine.recognize(b"img")
        self.assertEqual(result.engine, "gemini")
        self.assertEqual(result.text, "line one\nline two")
        self.assertEqual(result.metadata["source"], "gemini_ocr_fallback")
        self.assertNotIn("paddle", result.engine)

    def test_document_evidence_labels_gemini_and_keeps_paddle_secondary(self):
        from services.ocr.runtime import document_evidence_from_bundle

        paddle = _Fake("paddleocr-vl-1.6", "   ")
        gemini = _Fake("gemini", "Printed line")
        bundle = _orch(paddle, gemini).recognize(b"img", page=3)
        evidence = document_evidence_from_bundle(bundle, document_id="doc-1")
        self.assertEqual(evidence.engine, "gemini")
        self.assertTrue(evidence.unverified_fallback)
        self.assertEqual(evidence.metadata["status"], "fallback")
        self.assertEqual(evidence.metadata["fallback_reason"], "empty_output")
        self.assertEqual(evidence.pages[0].engine, "gemini")
        self.assertEqual(evidence.pages[0].text, "Printed line")
        self.assertEqual(bundle.secondary.engine, "paddleocr-vl-1.6")
        self.assertEqual(evidence.pages[0].page, 3)


class CallSiteSafetyTests(unittest.TestCase):
    def _png(self) -> bytes:
        import io

        from PIL import Image

        buf = io.BytesIO()
        Image.new("RGB", (12, 12), "white").save(buf, format="PNG")
        return buf.getvalue()

    def test_sales_image_probe_does_not_request_gemini_fallback(self):
        from services.sales_statement_extractor import _ocr_image_to_text

        with mock.patch(
            "services.ocr.runtime.primary_is_paddle_vl", return_value=True
        ), mock.patch(
            "services.ocr.runtime.ocr_pil_image_text",
            return_value=(
                "PADDLE",
                {
                    "engine": "paddleocr-vl-1.6",
                    "status": "success",
                    "unverified_fallback": False,
                },
            ),
        ) as ocr:
            text = _ocr_image_to_text(self._png())
        self.assertEqual(text, "PADDLE")
        self.assertIs(ocr.call_args.kwargs.get("use_fallback"), False)

    def test_layout_crop_text_does_not_request_gemini_fallback(self):
        import io

        from PIL import Image

        from services.sales_statement_extractor import _ocr_pil_text

        image = Image.open(io.BytesIO(self._png()))
        with mock.patch(
            "services.ocr.runtime.primary_is_paddle_vl", return_value=True
        ), mock.patch(
            "services.ocr.runtime.ocr_pil_image_text",
            return_value=("ROW", {"engine": "paddleocr-vl-1.6", "status": "success"}),
        ) as ocr:
            text = _ocr_pil_text(image, psm=7)
        self.assertEqual(text, "ROW")
        self.assertIs(ocr.call_args.kwargs.get("use_fallback"), False)

    def test_digit_whitelist_crop_stays_on_tesseract(self):
        import io

        from PIL import Image

        from services.sales_statement_extractor import _ocr_pil_text

        image = Image.open(io.BytesIO(self._png()))
        tess = mock.Mock()
        tess.image_to_string.return_value = "12"
        with mock.patch(
            "services.ocr.runtime.primary_is_paddle_vl", return_value=True
        ), mock.patch(
            "services.ocr.runtime.ocr_pil_image_text",
            side_effect=AssertionError("gemini path must not run"),
        ), mock.patch(
            "services.sales_statement_extractor._a2z_tesseract", return_value=tess
        ):
            text = _ocr_pil_text(image, psm=7, whitelist="0123456789")
        self.assertEqual(text, "12")
        self.assertIn("tessedit_char_whitelist", tess.image_to_string.call_args.kwargs["config"])

    def test_document_page_entry_leaves_fallback_enabled(self):
        from services.document_intelligence.pipeline import DocumentIntelligencePipeline

        class _Recorder:
            def __init__(self):
                self.kwargs = None

            def recognize(self, image_bytes, **kwargs):
                self.kwargs = kwargs
                return image_bytes

        recorder = _Recorder()
        pipeline = DocumentIntelligencePipeline(ocr=recorder)
        pipeline.recognize_image(b"page")
        self.assertNotIn("use_fallback", recorder.kwargs)


if __name__ == "__main__":
    logging.basicConfig(level="INFO")
    unittest.main()
