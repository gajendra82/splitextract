import io
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from PIL import Image

import cloud_ocr.app as worker
from services.ocr.base import OCRResult


def _png_bytes():
    image = Image.new("RGB", (16, 12), "white")
    stream = io.BytesIO()
    image.save(stream, format="PNG")
    return stream.getvalue()


class CloudOcrApiTests(unittest.TestCase):
    def test_health_reports_missing_auth_configuration(self):
        with patch.object(worker, "_TOKEN", ""):
            response = TestClient(worker.app).get("/health")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(TestClient(worker.app).get("/live").status_code, 200)

    def test_auth_and_response_evidence_contract(self):
        result = OCRResult(
            text="<table><tr><th>Item</th><th>Qty</th></tr><tr><td>ABC</td><td>7</td></tr></table>",
            page=2,
            engine="paddleocr-vl-1.6",
            metadata={"status": "success", "model": "PaddleOCR-VL-1.6", "image_width": 16, "image_height": 12},
        )
        with patch.object(worker, "_TOKEN", "test-secret"), patch.object(worker._ENGINE, "recognize", return_value=result) as recognize:
            client = TestClient(worker.app)
            files = {"file": ("test.png", _png_bytes(), "image/png")}
            denied = client.post("/v1/ocr", files=files)
            self.assertEqual(denied.status_code, 401)
            response = client.post(
                "/v1/ocr",
                files=files,
                headers={"Authorization": "Bearer test-secret", "X-OCR-Page": "2"},
            )
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertIn("Item", payload["text"])
        self.assertEqual(payload["headers"], ["Item", "Qty"])
        self.assertEqual(payload["rows"], [["ABC", "7"]])
        self.assertEqual(payload["page"], 2)
        self.assertEqual(payload["page_info"], {"page": 2, "width": 16, "height": 12})
        recognize.assert_called_once()


if __name__ == "__main__":
    unittest.main()
