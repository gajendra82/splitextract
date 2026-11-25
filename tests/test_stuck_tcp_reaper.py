"""Unit tests for stuck Send-Q TCP reaper (no live sockets required)."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from services import stuck_tcp_reaper as reaper


class StuckTcpReaperTests(unittest.TestCase):
    def setUp(self):
        with reaper._lock:
            reaper._high_sendq_since.clear()
        reaper.stop_stuck_tcp_reaper()
        reaper._reaper_thread = None

    def tearDown(self):
        with reaper._lock:
            reaper._high_sendq_since.clear()
        reaper.stop_stuck_tcp_reaper()
        reaper._reaper_thread = None

    def test_observe_then_reap_after_min_age(self):
        row = {
            "recv_q": 0,
            "send_q": 102945,
            "local": "200.234.44.215:8002",
            "peer": "20.207.239.62:57458",
            "inode": 4242,
            "line": "stub",
        }
        closes = []

        with patch.object(reaper, "_list_estab_sockets", return_value=[row]), patch.object(
            reaper, "_fd_for_inode", return_value=99
        ), patch.object(
            reaper, "_close_fd", side_effect=lambda fd, peer, send_q: closes.append((fd, peer, send_q)) or True
        ):
            self.assertEqual(reaper.reap_stuck_sendq_once(min_age_seconds=120, now=1000.0), 0)
            self.assertEqual(closes, [])
            self.assertEqual(reaper.reap_stuck_sendq_once(min_age_seconds=120, now=1125.0), 1)
            self.assertEqual(closes, [(99, "20.207.239.62:57458", 102945)])

    def test_low_sendq_clears_tracking(self):
        with reaper._lock:
            reaper._high_sendq_since["20.207.239.62:1"] = 1.0
        row = {
            "recv_q": 0,
            "send_q": 100,
            "local": "200.234.44.215:8002",
            "peer": "20.207.239.62:1",
            "inode": 1,
            "line": "stub",
        }
        with patch.object(reaper, "_list_estab_sockets", return_value=[row]), patch.object(
            reaper, "_close_fd", MagicMock()
        ):
            self.assertEqual(reaper.reap_stuck_sendq_once(now=200.0), 0)
        with reaper._lock:
            self.assertNotIn("20.207.239.62:1", reaper._high_sendq_since)

    def test_runtime_supported_requires_linux_and_ss(self):
        with patch.object(reaper.sys, "platform", "win32"), patch.object(
            reaper.shutil, "which", return_value=r"C:\ss.exe"
        ):
            ok, reason = reaper._reaper_runtime_supported()
            self.assertFalse(ok)
            self.assertTrue(reason.startswith("unsupported_platform:"))

        with patch.object(reaper.sys, "platform", "linux"), patch.object(
            reaper.shutil, "which", return_value=None
        ):
            ok, reason = reaper._reaper_runtime_supported()
            self.assertFalse(ok)
            self.assertEqual(reason, "ss_unavailable")

        with patch.object(reaper.sys, "platform", "linux"), patch.object(
            reaper.shutil, "which", return_value="/usr/bin/ss"
        ):
            ok, reason = reaper._reaper_runtime_supported()
            self.assertTrue(ok)
            self.assertEqual(reason, "ok")

    def test_start_skips_when_unsupported(self):
        with patch.object(reaper, "STUCK_TCP_REAPER_ENABLED", True), patch.object(
            reaper, "_reaper_runtime_supported", return_value=(False, "unsupported_platform:win32")
        ), patch.object(reaper.threading, "Thread") as thread_cls:
            reaper.start_stuck_tcp_reaper()
            thread_cls.assert_not_called()
            self.assertIsNone(reaper._reaper_thread)

    def test_start_launches_on_linux_with_ss(self):
        fake_thread = MagicMock()
        fake_thread.is_alive.return_value = True
        with patch.object(reaper, "STUCK_TCP_REAPER_ENABLED", True), patch.object(
            reaper, "_reaper_runtime_supported", return_value=(True, "ok")
        ), patch.object(reaper.threading, "Thread", return_value=fake_thread) as thread_cls:
            reaper.start_stuck_tcp_reaper()
            thread_cls.assert_called_once()
            fake_thread.start.assert_called_once()
            self.assertIs(reaper._reaper_thread, fake_thread)


if __name__ == "__main__":
    unittest.main()
