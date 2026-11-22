"""STOCK_PHOTO_QUALITY_GATE — reject hopeless photos before OCR (flag default OFF)."""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

import cv2
import numpy as np

from services import stock_photo_quality as pq
from services.sales_statement_extractor import extract_sales_statement


def _jpeg(bgr, quality: int = 90) -> bytes:
    ok, buf = cv2.imencode(".jpg", bgr, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
    assert ok, "jpeg encode failed"
    return buf.tobytes()


def _table_image(width: int = 1000, height: int = 1400) -> np.ndarray:
    """Sharp printed-table stand-in: black grid on white, no LCD speckle."""
    img = np.full((height, width, 3), 245, dtype=np.uint8)
    for y in range(80, height - 40, 36):
        cv2.line(img, (40, y), (width - 40, y), (20, 20, 20), 2)
    for x in range(40, width - 40, 90):
        cv2.line(img, (x, 80), (x, height - 40), (20, 20, 20), 2)
    cv2.putText(
        img,
        "OPENING  RECEIPT  SALES  CLOSING",
        (50, 50),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.9,
        (10, 10, 10),
        2,
        cv2.LINE_AA,
    )
    return img


def _with_gate(enabled: bool):
    env = {"STOCK_PHOTO_QUALITY_GATE": "true" if enabled else "false"}
    return patch.dict(os.environ, env, clear=False)


class FlagDefaultOffTests(unittest.TestCase):
    def test_flag_defaults_off(self):
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("STOCK_PHOTO_QUALITY_GATE", None)
            self.assertFalse(pq.stock_photo_quality_gate_enabled())

    def test_flag_off_does_not_reject_tiny_or_blurry(self):
        tiny = np.full((80, 80, 3), 30, dtype=np.uint8)
        with _with_gate(False):
            self.assertIsNone(
                pq.apply_stock_photo_quality_gate(_jpeg(tiny), "x.jpg", ".jpg")
            )


class AssessPhotoQualityTests(unittest.TestCase):
    def test_sharp_full_frame_table_passes(self):
        got = pq.assess_stock_photo_quality(_jpeg(_table_image()))
        self.assertFalse(got["reject"], got)

    def test_tiny_image_is_too_small(self):
        tiny = np.full((120, 160, 3), 200, dtype=np.uint8)
        got = pq.assess_stock_photo_quality(_jpeg(tiny))
        self.assertTrue(got["reject"])
        self.assertIn("too_small", got["reasons"])

    def test_blurry_image_rejected(self):
        sharp = _table_image()
        blurry = cv2.GaussianBlur(sharp, (51, 51), 18)
        got = pq.assess_stock_photo_quality(_jpeg(blurry, quality=40))
        self.assertTrue(got["reject"], got)
        self.assertIn("blurry", got["reasons"])

    def test_too_far_small_table_in_large_frame(self):
        canvas = np.full((1100, 1400, 3), 40, dtype=np.uint8)
        stamp = cv2.resize(_table_image(400, 500), (160, 200))
        canvas[450:650, 620:780] = stamp
        got = pq.assess_stock_photo_quality(_jpeg(canvas))
        self.assertTrue(got["reject"], got)
        self.assertTrue(
            {"too_far", "blurry"} & set(got["reasons"]),
            got,
        )

    def test_screen_photo_rgb_subpixel_rejected(self):
        # Dimmer "monitor" content so RGB subpixels are not clipped to white.
        img = (_table_image(900, 700).astype(np.float32) * 0.65 + 35).astype(np.uint8)
        h, w = img.shape[:2]
        xs = np.arange(w)[None, :].repeat(h, axis=0)
        pattern = np.zeros_like(img, dtype=np.int16)
        pattern[:, :, 2] = ((xs % 3) == 0).astype(np.int16) * 90
        pattern[:, :, 1] = ((xs % 3) == 1).astype(np.int16) * 90
        pattern[:, :, 0] = ((xs % 3) == 2).astype(np.int16) * 90
        bgr = np.clip(img.astype(np.int16) + pattern, 0, 255).astype(np.uint8)
        bezel = np.full((820, 1080, 3), 18, dtype=np.uint8)
        bezel[40:740, 70:1010] = cv2.resize(bgr, (940, 700))
        got = pq.assess_stock_photo_quality(_jpeg(bezel, quality=85))
        self.assertTrue(got["reject"], got)
        self.assertIn("screen_photo", got["reasons"])
        with _with_gate(True):
            rejected = pq.apply_stock_photo_quality_gate(
                _jpeg(bezel, quality=85), "monitor.jpg", ".jpg"
            )
        self.assertIn(
            "screen",
            rejected["totals"]["extra"]["extraction_failed_message"].lower(),
        )

    def test_digital_screenshot_not_treated_as_screen_photo(self):
        img = _table_image(1000, 800)
        img[0:70, :] = (180, 90, 30)
        cv2.putText(
            img,
            "STOCK STATEMENT",
            (40, 48),
            cv2.FONT_HERSHEY_SIMPLEX,
            1.0,
            (255, 255, 255),
            2,
            cv2.LINE_AA,
        )
        ok, buf = cv2.imencode(".png", img)
        self.assertTrue(ok)
        got = pq.assess_stock_photo_quality(buf.tobytes())
        self.assertFalse(
            got["reject"],
            f"screenshot should pass, got {got}",
        )


class GateApplyTests(unittest.TestCase):
    def test_spreadsheet_skipped_when_flag_on(self):
        with _with_gate(True):
            self.assertIsNone(
                pq.apply_stock_photo_quality_gate(b"not-an-image", "x.xlsx", ".xlsx")
            )

    def test_flag_on_rejects_and_sets_retry_message(self):
        tiny = np.full((80, 90, 3), 10, dtype=np.uint8)
        with _with_gate(True):
            out = pq.apply_stock_photo_quality_gate(_jpeg(tiny), "bad.jpg", ".jpg")
        self.assertIsNotNone(out)
        extra = out["totals"]["extra"]
        self.assertTrue(extra["extraction_failed"])
        self.assertTrue(extra["photo_quality_rejected"])
        self.assertIn("poor_photo_quality", extra["extraction_failed_reasons"])
        self.assertIn("retry", extra["extraction_failed_message"].lower())
        detail = pq.photo_quality_client_detail(extra, "bad.jpg")
        self.assertEqual(detail["error"], "extraction_failed")
        self.assertIn("retry", detail["message"].lower())

    def test_flag_on_passes_sharp_table(self):
        with _with_gate(True):
            self.assertIsNone(
                pq.apply_stock_photo_quality_gate(
                    _jpeg(_table_image()), "ok.jpg", ".jpg"
                )
            )


class ExtractorWiringTests(unittest.TestCase):
    def test_flag_off_still_parses_image(self):
        sentinel = {
            "source_file": "ok.jpg",
            "line_items": [{"product_name": "A"}],
            "totals": {"extra": {"extraction_method": "mock"}},
        }
        with _with_gate(False), patch(
            "services.sales_statement_extractor._parse_image",
            return_value=sentinel,
        ) as parse, patch(
            "services.sales_statement_extractor._fill_printed_report_period_from_ocr",
            side_effect=lambda result, image_bytes=None: result,
        ), patch(
            "services.stock_image_vision.apply_direct_stock_image_gemini",
            side_effect=lambda result, *args, **kwargs: result,
        ):
            out = extract_sales_statement(_jpeg(_table_image()), "ok.jpg")
        parse.assert_called_once()
        extra = ((out.get("totals") or {}).get("extra") or {})
        self.assertFalse(extra.get("photo_quality_rejected"))

    def test_flag_on_skips_parse_for_hopeless_photo(self):
        tiny = np.full((70, 70, 3), 8, dtype=np.uint8)
        with _with_gate(True), patch(
            "services.sales_statement_extractor._parse_image"
        ) as parse:
            out = extract_sales_statement(_jpeg(tiny), "far.jpg")
        parse.assert_not_called()
        extra = out["totals"]["extra"]
        self.assertTrue(extra["photo_quality_rejected"])
        self.assertTrue(extra["extraction_failed"])

    def test_endpoint_returns_422_retry_for_rejected_photo(self):
        import app as app_module
        from fastapi.testclient import TestClient

        tiny = np.full((70, 70, 3), 8, dtype=np.uint8)
        with _with_gate(True):
            failed = pq.apply_stock_photo_quality_gate(_jpeg(tiny), "bad.jpg", ".jpg")
        self.assertIsNotNone(failed)
        with patch.object(
            app_module, "extract_sales_statement", return_value=failed
        ):
            resp = TestClient(app_module.app).post(
                "/extract-sales-statement",
                files={"file": ("bad.jpg", _jpeg(tiny), "image/jpeg")},
            )
        self.assertEqual(resp.status_code, 422)
        detail = resp.json().get("detail") or {}
        self.assertEqual(detail.get("error"), "extraction_failed")
        self.assertIn("poor_photo_quality", detail.get("reasons") or [])
        self.assertIn("retry", (detail.get("message") or "").lower())

    def test_flag_on_does_not_gate_xlsx(self):
        with _with_gate(True), patch(
            "services.sales_statement_extractor._parse_xls",
            return_value={
                "source_file": "ok.xlsx",
                "line_items": [],
                "totals": {"extra": {"extraction_method": "mock_xls"}},
            },
        ) as parse:
            extract_sales_statement(b"PK\x03\x04" + b"\x00" * 40, "ok.xlsx")
        parse.assert_called_once()


if __name__ == "__main__":
    unittest.main()
