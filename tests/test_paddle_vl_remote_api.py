import json
import unittest
from unittest.mock import patch

from services.ocr.paddle_vl_engine import PaddleVLEngine


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


class PaddleVLRemoteTests(unittest.TestCase):
    @patch.dict("os.environ", {"PADDLE_VL_API_URL": "https://ocr.internal", "PADDLE_VL_API_KEY": "test-token", "OCR_EXECUTION_TIMEOUT_S": "12"}, clear=False)
    @patch("services.ocr.paddle_vl_engine.urllib.request.urlopen")
    def test_remote_result_keeps_model_text_and_table_evidence(self, urlopen):
        urlopen.return_value = _Response({
            "status": "success", "text": "Item | Total | CloseStock\nHIORA SG GEL | 55 | 54",
            "tables": [{"headers": ["Item", "Total", "CloseStock"], "rows": [["HIORA SG GEL", "55", "54"]]}],
            "headers": ["Item", "Total", "CloseStock"],
            "rows": [["HIORA SG GEL", "55", "54"]],
            "page_info": {"page": 1, "width": 1200, "height": 800},
            "page": 1, "model": "PaddleOCR-VL-1.6", "image_width": 1200, "image_height": 800,
        })
        result = PaddleVLEngine().recognize(b"image", page=3)
        self.assertEqual(result.text, "Item | Total | CloseStock\nHIORA SG GEL | 55 | 54")
        self.assertEqual(result.page, 3)
        self.assertEqual(result.metadata["backend"], "remote_api")
        self.assertEqual(result.metadata["tables"][0]["headers"], ["Item", "Total", "CloseStock"])
        self.assertEqual(result.metadata["headers"], ["Item", "Total", "CloseStock"])
        self.assertEqual(result.metadata["rows"], [["HIORA SG GEL", "55", "54"]])
        self.assertEqual(result.metadata["page_info"], {"page": 1, "width": 1200, "height": 800})
        request = urlopen.call_args.args[0]
        self.assertEqual(request.get_header("Authorization"), "Bearer test-token")
        self.assertEqual(request.get_header("X-ocr-page"), "3")

    @patch.dict("os.environ", {"PADDLE_VL_API_URL": "https://ocr.internal", "PADDLE_VL_API_KEY": "token"}, clear=False)
    @patch("services.ocr.paddle_vl_engine.urllib.request.urlopen", side_effect=TimeoutError("deadline"))
    @patch("services.ocr.paddle_vl_engine.time.sleep")
    def test_remote_timeout_returns_error_after_bounded_retries(self, _sleep, urlopen):
        result = PaddleVLEngine(timeout_s=1).recognize(b"image")
        self.assertEqual(result.metadata["status"], "error")
        self.assertIn("TimeoutError", result.metadata["error"])
        self.assertEqual(result.metadata["remote_attempts"], 2)
        self.assertEqual(urlopen.call_count, 2)
        timeouts = [call.kwargs["timeout"] for call in urlopen.call_args_list]
        self.assertTrue(all(0 < value <= 1 for value in timeouts))
        self.assertLessEqual(timeouts[1], timeouts[0])

    @patch.dict("os.environ", {"PADDLE_VL_API_URL": "https://ocr.internal", "PADDLE_VL_API_KEY": "token"}, clear=False)
    @patch("services.ocr.paddle_vl_engine.urllib.request.urlopen")
    def test_non_transient_http_error_is_not_retried(self, urlopen):
        from urllib.error import HTTPError

        urlopen.side_effect = HTTPError("https://ocr.internal/v1/ocr", 401, "unauthorized", {}, None)
        result = PaddleVLEngine(timeout_s=1).recognize(b"image")
        self.assertEqual(result.metadata["error"], "remote_http_401")
        self.assertEqual(result.metadata["remote_attempts"], 1)
        self.assertEqual(urlopen.call_count, 1)


if __name__ == "__main__":
    unittest.main()
