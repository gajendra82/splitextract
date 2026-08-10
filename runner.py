"""
Infrastructure-only uvicorn entrypoint.

Creates the listening socket with TCP keepalive so long-running OCR POSTs
do not go completely silent on the wire (Azure → Hostinger idle drops).

No OCR, FastAPI, middleware, or business logic.
"""

from __future__ import annotations

import logging
import os
import socket
import sys

from uvicorn import Config, Server

# Mirror production CLI exactly:
#   python3 -m uvicorn app:app --host 0.0.0.0 --port 8001
#       --workers 1 --timeout-keep-alive 1200
APP = "app:app"
HOST = "0.0.0.0"
PORT = 8001
WORKERS = 1
TIMEOUT_KEEP_ALIVE = 1200
BACKLOG = 2048
PROXY_HEADERS = True
ACCESS_LOG = True
LIFESPAN = "auto"

DEFAULT_KEEPIDLE = 60
DEFAULT_KEEPINTVL = 30
DEFAULT_KEEPCNT = 5

logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s:%(name)s:%(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("keepalive.runner")


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return int(str(raw).strip())
    except ValueError:
        logger.warning(
            "Invalid %s=%r — using default %s", name, raw, default
        )
        return default


def _try_setsockopt(
    sock: socket.socket, level: int, optname: int, value: int, label: str
) -> None:
    try:
        sock.setsockopt(level, optname, value)
    except (AttributeError, OSError, ValueError) as exc:
        logger.warning(
            "Unsupported/failed socket option %s=%s (%s) — continuing startup",
            label,
            value,
            exc,
        )


def _safe_getsockopt(sock: socket.socket, level: int, optname: int, label: str):
    try:
        return sock.getsockopt(level, optname)
    except (AttributeError, OSError, ValueError) as exc:
        logger.warning("Could not read socket option %s (%s)", label, exc)
        return None


def create_listening_socket(
    host: str,
    port: int,
    backlog: int,
    keepidle: int,
    keepintvl: int,
    keepcnt: int,
) -> socket.socket:
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    sock = socket.socket(family=family, type=socket.SOCK_STREAM)
    try:
        _try_setsockopt(sock, socket.SOL_SOCKET, socket.SO_REUSEADDR, 1, "SO_REUSEADDR")
        _try_setsockopt(sock, socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1, "SO_KEEPALIVE")

        if hasattr(socket, "TCP_KEEPIDLE"):
            _try_setsockopt(
                sock, socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, keepidle, "TCP_KEEPIDLE"
            )
        else:
            logger.warning("TCP_KEEPIDLE not available on this platform — continuing")

        if hasattr(socket, "TCP_KEEPINTVL"):
            _try_setsockopt(
                sock,
                socket.IPPROTO_TCP,
                socket.TCP_KEEPINTVL,
                keepintvl,
                "TCP_KEEPINTVL",
            )
        else:
            logger.warning("TCP_KEEPINTVL not available on this platform — continuing")

        if hasattr(socket, "TCP_KEEPCNT"):
            _try_setsockopt(
                sock, socket.IPPROTO_TCP, socket.TCP_KEEPCNT, keepcnt, "TCP_KEEPCNT"
            )
        else:
            logger.warning("TCP_KEEPCNT not available on this platform — continuing")

        sock.bind((host, port))
        sock.listen(backlog)
        sock.set_inheritable(True)
    except BaseException:
        try:
            sock.close()
        except OSError:
            pass
        raise

    actual_ka = _safe_getsockopt(
        sock, socket.SOL_SOCKET, socket.SO_KEEPALIVE, "SO_KEEPALIVE"
    )
    actual_idle = (
        _safe_getsockopt(sock, socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, "TCP_KEEPIDLE")
        if hasattr(socket, "TCP_KEEPIDLE")
        else None
    )
    actual_intvl = (
        _safe_getsockopt(
            sock, socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, "TCP_KEEPINTVL"
        )
        if hasattr(socket, "TCP_KEEPINTVL")
        else None
    )
    actual_cnt = (
        _safe_getsockopt(sock, socket.IPPROTO_TCP, socket.TCP_KEEPCNT, "TCP_KEEPCNT")
        if hasattr(socket, "TCP_KEEPCNT")
        else None
    )

    # Sole intentional infrastructure startup log.
    logger.info(
        "[KEEPALIVE] configured SO_KEEPALIVE=1 TCP_KEEPIDLE=%s TCP_KEEPINTVL=%s "
        "TCP_KEEPCNT=%s | actual SO_KEEPALIVE=%s TCP_KEEPIDLE=%s TCP_KEEPINTVL=%s "
        "TCP_KEEPCNT=%s",
        keepidle,
        keepintvl,
        keepcnt,
        actual_ka,
        actual_idle,
        actual_intvl,
        actual_cnt,
    )
    return sock


def main() -> None:
    keepidle = _env_int("TCP_KEEPIDLE_SECONDS", DEFAULT_KEEPIDLE)
    keepintvl = _env_int("TCP_KEEPINTVL_SECONDS", DEFAULT_KEEPINTVL)
    keepcnt = _env_int("TCP_KEEPCNT", DEFAULT_KEEPCNT)

    # Config before bind — failure here leaves no listening socket.
    config = Config(
        app=APP,
        host=HOST,
        port=PORT,
        workers=WORKERS,
        timeout_keep_alive=TIMEOUT_KEEP_ALIVE,
        backlog=BACKLOG,
        proxy_headers=PROXY_HEADERS,
        access_log=ACCESS_LOG,
        lifespan=LIFESPAN,
    )

    sock = create_listening_socket(
        host=HOST,
        port=PORT,
        backlog=BACKLOG,
        keepidle=keepidle,
        keepintvl=keepintvl,
        keepcnt=keepcnt,
    )
    try:
        Server(config=config).run(sockets=[sock])
    except BaseException:
        logger.exception("keepalive runner failed after listen; closing socket")
        raise
    finally:
        try:
            sock.close()
        except OSError:
            pass


if __name__ == "__main__":
    main()
