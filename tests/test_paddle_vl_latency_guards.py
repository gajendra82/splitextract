"""Regression guards for VL latency/safety fixes (no live model load)."""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

from services.ocr.paddle_vl_engine import (
    _downscale_image_bytes,
    _vl_pipeline_speed_kwargs,
)


def _png(w: int, h: int) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), "white").save(buf, format="PNG")
    return buf.getvalue()


class DownscaleTests(unittest.TestCase):
    def test_downscales_oversized_long_edge(self):
        with mock.patch.dict(os.environ, {"PADDLE_VL_MAX_SIDE": "1024"}, clear=False):
            out, meta = _downscale_image_bytes(_png(3000, 2000))
        self.assertTrue(meta["downscaled"])
        self.assertLessEqual(max(meta["width"], meta["height"]), 1024)
        self.assertGreater(meta["orig_width"], meta["width"])
        self.assertLess(len(out), len(_png(3000, 2000)) * 2)

    def test_identity_when_already_small(self):
        raw = _png(800, 600)
        with mock.patch.dict(os.environ, {"PADDLE_VL_MAX_SIDE": "2048"}, clear=False):
            out, meta = _downscale_image_bytes(raw)
        self.assertFalse(meta["downscaled"])
        self.assertEqual(out, raw)


class PipelineSpeedFlagTests(unittest.TestCase):
    def test_defaults_disable_unused_subpipelines(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            for key in (
                "PADDLE_VL_USE_DOC_ORIENTATION",
                "PADDLE_VL_USE_DOC_UNWARPING",
                "PADDLE_VL_USE_CHART_RECOGNITION",
                "PADDLE_VL_USE_LAYOUT_DETECTION",
            ):
                os.environ.pop(key, None)
            flags = _vl_pipeline_speed_kwargs()
        self.assertFalse(flags["use_doc_orientation_classify"])
        self.assertFalse(flags["use_doc_unwarping"])
        self.assertFalse(flags["use_chart_recognition"])
        self.assertTrue(flags["use_layout_detection"])

    def test_env_can_reenable_orientation(self):
        with mock.patch.dict(
            os.environ, {"PADDLE_VL_USE_DOC_ORIENTATION": "true"}, clear=False
        ):
            flags = _vl_pipeline_speed_kwargs()
        self.assertTrue(flags["use_doc_orientation_classify"])


class PrimaryCacheIsolationTests(unittest.TestCase):
    def test_empty_error_cache_is_not_reused(self):
        """Mirrors run_secondary_repair_controlled_batch._primary_ocr_once policy."""
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "doc.json"
            cache.write_text(
                json.dumps(
                    {
                        "document_id": "doc.jpg",
                        "engine": "paddleocr-vl-1.6",
                        "text": "",
                        "text_len": 0,
                        "status": "error",
                    }
                ),
                encoding="utf-8",
            )
            cached = json.loads(cache.read_text(encoding="utf-8"))
            reusable = (
                cached.get("status") == "success"
                and bool((cached.get("text") or "").strip())
            )
            self.assertFalse(reusable)
            if not reusable:
                cache.unlink()
            self.assertFalse(cache.is_file())


class WorkerEmitTests(unittest.TestCase):
    def test_emit_uses_ascii_safe_json(self):
        from services.ocr import vl_worker

        captured = []

        def fake_print(s, flush=False):
            captured.append(s)

        with mock.patch.object(vl_worker, "print", fake_print):
            vl_worker._emit({"text": "数量", "status": "success"})
        self.assertEqual(len(captured), 1)
        self.assertIn("\\u", captured[0])
        self.assertEqual(json.loads(captured[0])["text"], "数量")


if __name__ == "__main__":
    unittest.main()
