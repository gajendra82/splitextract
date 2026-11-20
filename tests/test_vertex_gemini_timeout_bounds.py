"""Vertex Gemini call timeouts must not re-block on ThreadPoolExecutor shutdown."""

from __future__ import annotations

import time
import unittest
from unittest.mock import patch

from services import vertex_gemini_client as vgc


class TestVertexGeminiTimeoutBounds(unittest.TestCase):
    def test_run_callable_with_timeout_returns_promptly_on_hang(self):
        def hang():
            time.sleep(30)

        t0 = time.monotonic()
        with self.assertRaises(TimeoutError):
            vgc.run_callable_with_timeout(hang, timeout=1, label="test_hang")
        elapsed = time.monotonic() - t0
        # Must NOT wait for the full sleep via shutdown(wait=True).
        self.assertLess(elapsed, 5.0, f"timeout path blocked for {elapsed:.1f}s")

    def test_generate_content_via_vertex_times_out_without_shutdown_hang(self):
        from tests.gemini_offline import (
            ensure_gemini_offline_guard,
            uninstall_gemini_offline_guard,
        )

        class _HangModels:
            def generate_content(self, **_kwargs):
                time.sleep(30)

        class _HangClient:
            models = _HangModels()

        payload = {"contents": [{"role": "user", "parts": [{"text": "ping"}]}]}
        uninstall_gemini_offline_guard()
        t0 = time.monotonic()
        try:
            with patch.object(
                vgc, "get_vertex_gemini_client", return_value=_HangClient()
            ):
                with self.assertRaises(TimeoutError):
                    # Look up via module after uninstall (not a stale import binding).
                    vgc.generate_content_via_vertex(
                        "gemini-2.5-flash-lite",
                        payload,
                        timeout=1,
                    )
        finally:
            ensure_gemini_offline_guard()
        elapsed = time.monotonic() - t0
        self.assertLess(elapsed, 5.0, f"generate_content blocked for {elapsed:.1f}s")

    def test_http_timeout_attached_to_config(self):
        cfg = vgc._attach_http_timeout(None, 12)
        self.assertIsNotNone(cfg.http_options)
        self.assertEqual(cfg.http_options.timeout, 12000)

    def test_bounded_timeout_respects_cap(self):
        with patch.object(vgc, "_VERTEX_HTTP_TIMEOUT_CAP", 5):
            self.assertEqual(vgc._bounded_timeout_seconds(120), 5)
            self.assertEqual(vgc._bounded_timeout_seconds(3), 3)


if __name__ == "__main__":
    unittest.main()
