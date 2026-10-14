"""Reap ESTAB sockets on the API port whose Send-Q is wedged (peer not ACKing).

Laravel secondary_sales workers have been observed stuck for tens of minutes after
Python already logged HTTP 200: kernel Send-Q stays ~100KB with TCP retransmits
while /health (other connections) still succeeds. Closing the local socket forces
the HTTP client to error out so the Supervisor worker can pick the next job.
"""

from __future__ import annotations

import logging
import os
import re
import socket
import subprocess
import threading
import time
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

STUCK_TCP_REAPER_ENABLED = os.getenv(
    "STUCK_TCP_REAPER_ENABLED", "true"
).strip().lower() in {"1", "true", "yes", "on"}
STUCK_TCP_REAPER_INTERVAL_SECONDS = max(
    15, int(os.getenv("STUCK_TCP_REAPER_INTERVAL_SECONDS", "30") or "30")
)
STUCK_TCP_SENDQ_BYTES = max(
    4096, int(os.getenv("STUCK_TCP_SENDQ_BYTES", "32768") or "32768")
)
STUCK_TCP_MIN_AGE_SECONDS = max(
    30, int(os.getenv("STUCK_TCP_MIN_AGE_SECONDS", "120") or "120")
)
STUCK_TCP_PORT = int(os.getenv("STUCK_TCP_PORT", "8002") or "8002")

_reaper_thread: Optional[threading.Thread] = None
_stop = threading.Event()
# peer -> first_seen_monotonic when Send-Q exceeded threshold
_high_sendq_since: Dict[str, float] = {}
_lock = threading.Lock()

_SS_LINE = re.compile(
    r"^\s*(\d+)\s+(\d+)\s+\S+:(\d+)\s+(\S+):(\d+)\s+.*?\bino:(\d+)",
    re.MULTILINE,
)


def _env_listen_port() -> int:
    return int(STUCK_TCP_PORT)


def _list_estab_sockets(port: int) -> List[Dict[str, object]]:
    """Return established sockets for local ``port`` via ``ss``."""
    try:
        out = subprocess.check_output(
            [
                "ss",
                "-tnp",
                "state",
                "established",
                f"( sport = :{port} )",
            ],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
    except Exception as exc:
        logger.warning("stuck_tcp_ss_failed error=%s", type(exc).__name__)
        return []

    rows: List[Dict[str, object]] = []
    # ss -tnp without -e lacks ino; re-run with -e when needed
    try:
        out_e = subprocess.check_output(
            [
                "ss",
                "-tnpe",
                "state",
                "established",
                f"( sport = :{port} )",
            ],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
        out = out_e
    except Exception:
        pass

    for line in out.splitlines():
        if "users:" not in line and "ino:" not in line:
            # header or empty
            continue
        parts = line.split()
        if len(parts) < 5:
            continue
        try:
            recv_q = int(parts[0])
            send_q = int(parts[1])
        except ValueError:
            # line may start with Netid
            try:
                recv_q = int(parts[1])
                send_q = int(parts[2])
                local = parts[3]
                peer = parts[4]
            except (ValueError, IndexError):
                continue
        else:
            local = parts[2]
            peer = parts[3]

        if not local.endswith(f":{port}"):
            # IPv6 form [::]:8002 already matched by ss filter; keep
            if f":{port}" not in local:
                continue

        inode = None
        m = re.search(r"\bino:(\d+)", line)
        if m:
            inode = int(m.group(1))

        rows.append(
            {
                "recv_q": recv_q,
                "send_q": send_q,
                "local": local,
                "peer": peer,
                "inode": inode,
                "line": line.strip(),
            }
        )
    return rows


def _fd_for_inode(inode: int) -> Optional[int]:
    fd_dir = f"/proc/{os.getpid()}/fd"
    try:
        names = os.listdir(fd_dir)
    except Exception:
        return None
    target = f"socket:[{inode}]"
    for name in names:
        path = os.path.join(fd_dir, name)
        try:
            if os.readlink(path) == target:
                return int(name)
        except Exception:
            continue
    return None


def _close_fd(fd: int, peer: str, send_q: int) -> bool:
    # Prefer shutdown(2) via libc so we don't duplicate the fd (socket.fromfd does).
    try:
        import ctypes

        ctypes.CDLL(None, use_errno=True).shutdown(fd, socket.SHUT_RDWR)
    except Exception:
        pass
    try:
        os.close(fd)
        logger.error(
            "stuck_tcp_reaped peer=%s send_q=%s fd=%s action=close",
            peer,
            send_q,
            fd,
        )
        return True
    except Exception as exc:
        logger.error(
            "stuck_tcp_close_failed peer=%s fd=%s error=%s",
            peer,
            fd,
            type(exc).__name__,
        )
        return False


def reap_stuck_sendq_once(
    *,
    port: Optional[int] = None,
    sendq_bytes: Optional[int] = None,
    min_age_seconds: Optional[float] = None,
    now: Optional[float] = None,
) -> int:
    """Close local sockets whose Send-Q stayed high for ``min_age_seconds``.

    Returns number of sockets closed.
    """
    port = int(port if port is not None else _env_listen_port())
    sendq_bytes = int(
        sendq_bytes if sendq_bytes is not None else STUCK_TCP_SENDQ_BYTES
    )
    min_age = float(
        min_age_seconds
        if min_age_seconds is not None
        else STUCK_TCP_MIN_AGE_SECONDS
    )
    now = float(now if now is not None else time.monotonic())

    rows = _list_estab_sockets(port)
    closed = 0
    seen_peers = set()

    with _lock:
        for row in rows:
            peer = str(row["peer"])
            send_q = int(row["send_q"] or 0)
            seen_peers.add(peer)
            if send_q < sendq_bytes:
                _high_sendq_since.pop(peer, None)
                continue
            first = _high_sendq_since.get(peer)
            if first is None:
                _high_sendq_since[peer] = now
                logger.warning(
                    "stuck_tcp_sendq_observed peer=%s send_q=%s "
                    "threshold=%s (will reap after %.0fs if still high)",
                    peer,
                    send_q,
                    sendq_bytes,
                    min_age,
                )
                continue
            age = now - first
            if age < min_age:
                continue
            inode = row.get("inode")
            if not inode:
                logger.error(
                    "stuck_tcp_no_inode peer=%s send_q=%s age=%.0fs",
                    peer,
                    send_q,
                    age,
                )
                continue
            fd = _fd_for_inode(int(inode))
            if fd is None:
                logger.error(
                    "stuck_tcp_fd_missing peer=%s inode=%s send_q=%s age=%.0fs",
                    peer,
                    inode,
                    send_q,
                    age,
                )
                continue
            if _close_fd(fd, peer, send_q):
                closed += 1
                _high_sendq_since.pop(peer, None)

        # Drop tracking for peers that are gone
        for peer in list(_high_sendq_since):
            if peer not in seen_peers:
                _high_sendq_since.pop(peer, None)

    return closed


def _reaper_loop() -> None:
    logger.info(
        "stuck_tcp_reaper_started interval=%ss sendq_threshold=%s "
        "min_age=%ss port=%s",
        STUCK_TCP_REAPER_INTERVAL_SECONDS,
        STUCK_TCP_SENDQ_BYTES,
        STUCK_TCP_MIN_AGE_SECONDS,
        STUCK_TCP_PORT,
    )
    while not _stop.wait(STUCK_TCP_REAPER_INTERVAL_SECONDS):
        if not STUCK_TCP_REAPER_ENABLED:
            continue
        try:
            reap_stuck_sendq_once()
        except Exception:
            logger.exception("stuck_tcp_reaper_tick_failed")


def start_stuck_tcp_reaper() -> None:
    global _reaper_thread
    if not STUCK_TCP_REAPER_ENABLED:
        logger.info("stuck_tcp_reaper_disabled")
        return
    if _reaper_thread is not None and _reaper_thread.is_alive():
        return
    _stop.clear()
    _reaper_thread = threading.Thread(
        target=_reaper_loop,
        name="stuck-tcp-reaper",
        daemon=True,
    )
    _reaper_thread.start()


def stop_stuck_tcp_reaper() -> None:
    _stop.set()
