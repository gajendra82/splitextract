"""Deadline, retry, and atomic-save helpers for ZA proposal generation.

The proposal queue must not call these helpers during a review session.
They exist so a later proposal run can resume without repeating saved images
and without waiting forever on one hung request.
"""

from __future__ import annotations

import os
import random
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional


class ProposalAttemptError(RuntimeError):
    def __init__(self, failure_class: str, original: BaseException, attempts: int):
        self.failure_class = failure_class
        self.original = original
        self.attempts = attempts
        super().__init__(f"{failure_class} after {attempts} attempt(s): {original}")


def proposal_concurrency(raw: Optional[str] = None) -> int:
    """Conservative default is one image at a time. Values above 3 are rejected."""
    if raw is None:
        raw = os.getenv("ZA_PROPOSAL_CONCURRENCY", "1")
    try:
        value = int(str(raw).strip())
    except ValueError as exc:
        raise ValueError("ZA_PROPOSAL_CONCURRENCY must be an integer") from exc
    if value < 1 or value > 3:
        raise ValueError("ZA_PROPOSAL_CONCURRENCY must be from 1 to 3")
    return value


def classify_request_error(exc: BaseException) -> str:
    if isinstance(exc, TimeoutError) or "timeout" in type(exc).__name__.lower():
        return "timeout"
    if type(exc).__name__ == "GeminiProviderError":
        return "provider"
    text = str(exc).lower()
    if "timed out" in text or "timeout" in text:
        return "timeout"
    if any(
        token in text
        for token in (
            "getaddrinfo",
            "name or service not known",
            "nodename nor servname",
            "temporary failure in name resolution",
            "errno 11001",
        )
    ):
        return "dns"
    if any(
        token in text
        for token in (
            "disconnect",
            "connection reset",
            "connection aborted",
            "connection refused",
            "connectionerror",
            "server disconnected",
        )
    ) or "connection" in text:
        return "connection"
    return "other"


def backoff_seconds(failed_attempt: int, rng: random.Random) -> float:
    base = min(20.0, float(2 ** failed_attempt))
    return base + rng.uniform(0, base * 0.25)


def call_with_retries(
    fn: Callable[[], Any],
    *,
    max_attempts: int = 3,
    sleep: Callable[[float], None] = time.sleep,
    rng: Optional[random.Random] = None,
) -> Any:
    """Call fn at most max_attempts times. Non-transient errors are not retried."""
    if max_attempts < 1:
        raise ValueError("max_attempts must be at least 1")
    generator = rng or random.Random()
    last: BaseException = RuntimeError("no attempt")
    failure_class = "other"
    for attempt in range(max_attempts):
        try:
            return fn()
        except Exception as exc:
            last = exc
            failure_class = classify_request_error(exc)
            retryable = failure_class in {"timeout", "dns", "connection", "provider"}
            if not retryable or attempt + 1 >= max_attempts:
                raise ProposalAttemptError(failure_class, exc, attempt + 1) from exc
            sleep(backoff_seconds(attempt, generator))
    raise ProposalAttemptError(failure_class, last, max_attempts)


def run_with_deadline(
    fn: Callable[[], Any],
    timeout: float,
    cancel: Optional[Callable[[], None]] = None,
) -> Any:
    """Run fn without joining it after the deadline.

    Python cannot kill a thread. cancel() is expected to close the HTTP client
    so the underlying request aborts. The caller returns while a daemon thread
    may still be unwinding.
    """
    if timeout <= 0:
        raise ValueError("timeout must be positive")
    outcome: dict = {}

    def target() -> None:
        try:
            outcome["value"] = fn()
        except BaseException as exc:  # noqa: BLE001
            outcome["error"] = exc

    thread = threading.Thread(target=target, name="za-proposal-deadline", daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        if cancel is not None:
            try:
                cancel()
            except Exception:
                pass
        raise TimeoutError(f"request timed out after {timeout}s")
    if "error" in outcome:
        raise outcome["error"]
    return outcome.get("value")


def atomic_write_text(path: Path, text: str) -> None:
    """Write the full text to a temporary file, then replace the destination."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def generate_content_with_deadline(model: str, payload: dict, timeout: float):
    """Call Vertex with an HTTP timeout and return control when the deadline expires."""
    import httpx
    from google import genai
    from google.genai import errors as genai_errors
    from google.genai import types

    from services.vertex_gemini_client import (
        GOOGLE_CLOUD_LOCATION,
        GOOGLE_CLOUD_PROJECT,
        GeminiProviderError,
        GeminiRestResponse,
        payload_to_contents,
        payload_to_generate_config,
        sdk_response_to_rest_dict,
    )

    http_client = httpx.Client(timeout=httpx.Timeout(timeout))
    client = genai.Client(
        vertexai=True,
        project=GOOGLE_CLOUD_PROJECT or "pod-ocr-502015",
        location=GOOGLE_CLOUD_LOCATION or "global",
        http_options=types.HttpOptions(
            timeout=max(1, int(timeout * 1000)),
            retry_options=types.HttpRetryOptions(attempts=1),
            httpx_client=http_client,
        ),
    )
    contents = payload_to_contents(payload)
    config = payload_to_generate_config(payload)

    def invoke():
        try:
            return client.models.generate_content(model=model, contents=contents, config=config)
        except genai_errors.APIError as exc:
            if getattr(exc, "code", None) in (429, 503):
                raise GeminiProviderError(exc.code, str(exc)) from exc
            raise

    try:
        response = run_with_deadline(invoke, timeout=timeout, cancel=http_client.close)
    finally:
        try:
            http_client.close()
        except Exception:
            pass
    return GeminiRestResponse(status_code=200, _data=sdk_response_to_rest_dict(response))
