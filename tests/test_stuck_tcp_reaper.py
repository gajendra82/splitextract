"""Unit tests for stuck Send-Q TCP reaper (no live sockets required)."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from services import stuck_tcp_reaper as reaper


class StuckTcpReaperTests(unittest.TestCase):
    def setUp(self):
        with reaper._lock:
            reaper._high_sendq_since.clear()

    def tearDown(self):
        with reaper._lock:
            reaper._high_sendq_since.clear()

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

    def test_start_skips_when_ss_missing(self):
        previous = reaper._reaper_thread
        reaper._reaper_thread = None
        try:
            with patch.object(reaper, "STUCK_TCP_REAPER_ENABLED", True), patch.object(
                reaper.shutil, "which", return_value=None
            ), patch.object(reaper.threading, "Thread") as thread:
                reaper.start_stuck_tcp_reaper()
                thread.assert_not_called()
            self.assertIsNone(reaper._reaper_thread)
        finally:
            reaper._reaper_thread = previous


if __name__ == "__main__":
    unittest.main()
