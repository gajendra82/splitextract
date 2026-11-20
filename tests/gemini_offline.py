"""Block live Vertex/Gemini calls during unit tests and count attempts.

Installed from ``tests/__init__.py`` and re-armed on every ``TestCase.run``
so a test that never imports this module still cannot hit the network, and a
test that temporarily patches the leaf restores the guard afterward.

Opt out only with ``STOCK_TEST_ALLOW_LIVE_GEMINI=1`` (never set by default).
"""

from __future__ import annotations

import logging
import os
import threading
import unittest
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

_LOCK = threading.Lock()
_CALL_COUNT = 0
_INSTALLED = False
_ORIGINALS: dict[str, Callable[..., Any]] = {}
_HOOKED_RUN = False
_ORIGINAL_TESTCASE_RUN: Optional[Callable[..., Any]] = None
_RESPONSE_QUEUE: list = []

_GUARD_ATTR = "_stock_gemini_offline_guard"


class GeminiOfflineBlocked(RuntimeError):
    """Raised when a test path tries to hit live Gemini without a local mock."""

    def __init__(self, label: str = "gemini"):
        self.label = label
        super().__init__(
            f"Live Gemini blocked in tests ({label}). "
            "Patch the Gemini client in the test, or set "
            "STOCK_TEST_ALLOW_LIVE_GEMINI=1 to allow real calls."
        )


def live_gemini_allowed() -> bool:
    return os.getenv("STOCK_TEST_ALLOW_LIVE_GEMINI", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def gemini_test_call_count() -> int:
    with _LOCK:
        return _CALL_COUNT


def reset_gemini_test_call_count() -> None:
    global _CALL_COUNT
    with _LOCK:
        _CALL_COUNT = 0


def set_gemini_responses(responses: list) -> None:
    """Queue recorded Gemini JSON payloads returned by the offline guard.

    Each entry may be:
    - a dict already shaped as Vertex ``candidates[0].content.parts[0].text``
      (full generateContent body), or
    - a dict of extraction fields (``line_items``, …) which is JSON-encoded
      into the text part automatically.
    """
    global _RESPONSE_QUEUE
    with _LOCK:
        _RESPONSE_QUEUE = list(responses or [])


def clear_gemini_responses() -> None:
    set_gemini_responses([])


def _wrap_response(payload: Any) -> Any:
    import json as _json

    from services.vertex_gemini_client import GeminiRestResponse

    if isinstance(payload, GeminiRestResponse):
        return payload
    if isinstance(payload, dict) and "candidates" in payload:
        body = payload
    else:
        body = {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {
                                "text": _json.dumps(
                                    payload if isinstance(payload, dict) else {}
                                )
                            }
                        ]
                    }
                }
            ],
            "usageMetadata": {"promptTokenCount": 1, "candidatesTokenCount": 1},
        }
    return GeminiRestResponse(status_code=200, _data=body)


def _bump(label: str) -> int:
    global _CALL_COUNT
    with _LOCK:
        _CALL_COUNT += 1
        count = _CALL_COUNT
    logger.info("GEMINI_TEST_BLOCKED label=%s call_count=%s", label, count)
    return count


def _resolve_attr(dotted: str):
    import importlib

    mod_name, _, attr = dotted.rpartition(".")
    module = importlib.import_module(mod_name)
    return module, attr, getattr(module, attr)


def _is_our_guard(fn: Any) -> bool:
    return bool(getattr(fn, _GUARD_ATTR, False))


def _make_blocker(label: str) -> Callable[..., Any]:
    def _blocked(*args: Any, **kwargs: Any) -> Any:
        _bump(str(kwargs.get("model") or kwargs.get("label") or label))
        with _LOCK:
            if _RESPONSE_QUEUE:
                payload = _RESPONSE_QUEUE.pop(0)
                return _wrap_response(payload)
        raise GeminiOfflineBlocked(label)

    setattr(_blocked, _GUARD_ATTR, True)
    _blocked.__name__ = f"blocked_{label}"
    return _blocked


def _targets() -> list[str]:
    return [
        "services.vertex_gemini_client.generate_content_via_vertex",
        "app.generate_content_via_vertex",
    ]


def install_gemini_offline_guard(*, force: bool = False) -> bool:
    """Install in-place wrappers on Vertex leaf bindings."""
    global _INSTALLED
    if live_gemini_allowed() and not force:
        logger.info("GEMINI_TEST_GUARD skipped reason=STOCK_TEST_ALLOW_LIVE_GEMINI")
        return False

    installed = 0
    for target in _targets():
        try:
            module, attr, current = _resolve_attr(target)
        except Exception as exc:
            logger.info(
                "GEMINI_TEST_GUARD resolve_failed target=%s error=%s",
                target,
                type(exc).__name__,
            )
            continue
        if _is_our_guard(current) and not force:
            installed += 1
            continue
        if target not in _ORIGINALS and not _is_our_guard(current):
            _ORIGINALS[target] = current
        setattr(module, attr, _make_blocker(target.rsplit(".", 1)[-1]))
        installed += 1

    _INSTALLED = installed > 0
    if _INSTALLED:
        logger.info("GEMINI_TEST_GUARD installed targets=%s", installed)
    return _INSTALLED


def ensure_gemini_offline_guard() -> bool:
    """Re-arm the guard if a test replaced the leaf and did not restore it."""
    if live_gemini_allowed():
        return False
    needs = False
    for target in _targets():
        try:
            _, _, current = _resolve_attr(target)
        except Exception:
            needs = True
            break
        if not _is_our_guard(current):
            needs = True
            break
    if needs or not _INSTALLED:
        return install_gemini_offline_guard(force=True)
    return True


def uninstall_gemini_offline_guard() -> None:
    global _INSTALLED
    for target, original in list(_ORIGINALS.items()):
        try:
            module, attr, _ = _resolve_attr(target)
            setattr(module, attr, original)
        except Exception:
            pass
    _INSTALLED = False


def _hook_unittest_testcase_run() -> None:
    """Ensure every TestCase re-arms the guard before running, package-wide."""
    global _HOOKED_RUN, _ORIGINAL_TESTCASE_RUN
    if _HOOKED_RUN:
        return
    _ORIGINAL_TESTCASE_RUN = unittest.TestCase.run

    def run(self, result=None):  # type: ignore[no-untyped-def]
        ensure_gemini_offline_guard()
        assert _ORIGINAL_TESTCASE_RUN is not None
        return _ORIGINAL_TESTCASE_RUN(self, result)

    unittest.TestCase.run = run  # type: ignore[assignment]
    _HOOKED_RUN = True


class GeminiOfflineTestCase(unittest.TestCase):
    """Optional base class; the TestCase.run hook already covers all tests."""

    @classmethod
    def setUpClass(cls):
        ensure_gemini_offline_guard()
        super().setUpClass()

    def setUp(self):
        ensure_gemini_offline_guard()
        super().setUp()


def bootstrap_gemini_offline_guard() -> bool:
    """Called from tests/__init__.py."""
    _hook_unittest_testcase_run()
    return install_gemini_offline_guard()
