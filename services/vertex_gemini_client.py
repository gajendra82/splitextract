"""Vertex AI Gemini client initialization and REST-compatible response helpers."""

from __future__ import annotations

import base64
import json
import logging
import os
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from dataclasses import dataclass
from typing import Any, Dict, Optional

from dotenv import load_dotenv
from google import genai
from google.genai import errors as genai_errors
from google.genai import types

load_dotenv()

logger = logging.getLogger(__name__)

GOOGLE_GENAI_USE_VERTEXAI = os.getenv(
    "GOOGLE_GENAI_USE_VERTEXAI", "true"
).strip().lower() in {"1", "true", "yes", "on"}
GOOGLE_CLOUD_PROJECT = os.getenv(
    "GOOGLE_CLOUD_PROJECT", "pod-ocr-502015"
).strip()
GOOGLE_CLOUD_LOCATION = os.getenv(
    "GOOGLE_CLOUD_LOCATION", "global"
).strip()
GOOGLE_APPLICATION_CREDENTIALS = os.getenv(
    "GOOGLE_APPLICATION_CREDENTIALS", ""
).strip()

_vertex_client: Optional[genai.Client] = None
_vertex_client_initialized = False


class GeminiProviderError(Exception):
    """Retryable Gemini provider throttling (429/503)."""

    def __init__(self, code: int, message: str = ""):
        self.code = code
        super().__init__(message or f"Gemini provider error {code}")


@dataclass
class GeminiRestResponse:
    """Minimal requests.Response-compatible wrapper for existing parsers."""

    status_code: int
    _data: Dict[str, Any]

    def json(self) -> Dict[str, Any]:
        return self._data


def _env_flag(name: str, default: bool = True) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def initialize_vertex_gemini_client() -> genai.Client:
    """Create (or return cached) Vertex AI Gemini client."""
    global _vertex_client, _vertex_client_initialized

    if _vertex_client is not None:
        return _vertex_client

    if not _env_flag("GOOGLE_GENAI_USE_VERTEXAI", True):
        logger.warning(
            "GOOGLE_GENAI_USE_VERTEXAI is disabled; Vertex AI client will still be used."
        )

    if not GOOGLE_CLOUD_PROJECT:
        logger.warning("GOOGLE_CLOUD_PROJECT is not set; using fallback project id.")

    if not GOOGLE_APPLICATION_CREDENTIALS:
        logger.info(
            "GOOGLE_APPLICATION_CREDENTIALS not set; relying on Application Default Credentials."
        )

    _vertex_client = genai.Client(
        vertexai=True,
        project=GOOGLE_CLOUD_PROJECT or "pod-ocr-502015",
        location=GOOGLE_CLOUD_LOCATION or "global",
    )
    _vertex_client_initialized = True

    logger.info("Using Vertex AI Gemini")
    logger.info("Project: %s", GOOGLE_CLOUD_PROJECT or "pod-ocr-502015")
    logger.info("Location: %s", GOOGLE_CLOUD_LOCATION or "global")

    return _vertex_client


def get_vertex_gemini_client() -> genai.Client:
    if _vertex_client is None:
        return initialize_vertex_gemini_client()
    return _vertex_client


def _payload_part_to_sdk(part: Dict[str, Any]) -> types.Part:
    if "text" in part and part["text"] is not None:
        return types.Part(text=str(part["text"]))

    inline = part.get("inline_data") or part.get("inlineData")
    if inline:
        mime_type = (
            inline.get("mime_type")
            or inline.get("mimeType")
            or "image/png"
        )
        raw_data = inline.get("data", "")
        if isinstance(raw_data, str):
            decoded = base64.b64decode(raw_data)
        else:
            decoded = raw_data
        return types.Part.from_bytes(data=decoded, mime_type=mime_type)

    raise ValueError(f"Unsupported Gemini payload part: {part!r}")


def payload_to_generate_config(payload: Dict[str, Any]) -> Optional[types.GenerateContentConfig]:
    gen_cfg = payload.get("generationConfig") or payload.get("generation_config") or {}
    if not gen_cfg:
        return None

    kwargs: Dict[str, Any] = {}
    if "temperature" in gen_cfg:
        kwargs["temperature"] = gen_cfg["temperature"]
    max_tokens = gen_cfg.get("maxOutputTokens", gen_cfg.get("max_output_tokens"))
    if max_tokens is not None:
        kwargs["max_output_tokens"] = max_tokens

    return types.GenerateContentConfig(**kwargs) if kwargs else None


_VERTEX_ERROR_APPROVED_KEYS = {
    "code",
    "status",
    "message",
    "quotametric",
    "quota_metric",
    "quotaid",
    "quota_id",
    "quotalimit",
    "quota_limit",
    "quotalimitvalue",
    "quota_limit_value",
    "retrydelay",
    "retry_delay",
    "retryinfo",
    "retry_info",
    "reason",
    "@type",
}
_VERTEX_ERROR_CONTAINER_KEYS = {
    "error",
    "details",
    "metadata",
    "violations",
}
_VERTEX_ERROR_DENIED_KEYS = {
    "authorization",
    "cookie",
    "set-cookie",
    "set_cookie",
    "private_key",
    "privatekey",
    "private_key_id",
    "api_key",
    "apikey",
    "access_token",
    "id_token",
    "refresh_token",
    "client_secret",
    "password",
    "secret",
    "credential",
    "credentials",
    "bearer",
    "token",
}
_VERTEX_SAFE_HEADER_EXACT = {"retry-after", "x-goog-retry-info"}
_VERTEX_ERROR_VALUE_MAX_LEN = 400


def _vertex_error_key_norm(key: Any) -> str:
    return str(key or "").strip().lower().replace("-", "_")


def _vertex_error_value_is_sensitive(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    text = value.strip()
    if not text:
        return False
    if text.startswith("-----BEGIN"):
        return True
    if text.lower().startswith("bearer "):
        return True
    if text.startswith("AIza"):
        return True
    return False


def _vertex_error_clip(value: Any) -> Any:
    if isinstance(value, str) and len(value) > _VERTEX_ERROR_VALUE_MAX_LEN:
        return value[:_VERTEX_ERROR_VALUE_MAX_LEN] + "…"
    return value


def _sanitize_vertex_error_details(value: Any) -> Any:
    """Keep only quota/capacity diagnostic fields from a Google APIError body."""
    if isinstance(value, dict):
        cleaned: Dict[str, Any] = {}
        for raw_key, raw_val in value.items():
            key = str(raw_key)
            norm = _vertex_error_key_norm(key)
            if norm in _VERTEX_ERROR_DENIED_KEYS or "authorization" in norm:
                continue
            if _vertex_error_value_is_sensitive(raw_val):
                continue
            if norm in _VERTEX_ERROR_APPROVED_KEYS or key == "@type":
                cleaned[key] = _sanitize_vertex_error_details(raw_val)
            elif norm in _VERTEX_ERROR_CONTAINER_KEYS:
                nested = _sanitize_vertex_error_details(raw_val)
                if nested not in (None, {}, []):
                    cleaned[key] = nested
        return cleaned
    if isinstance(value, list):
        items = [
            _sanitize_vertex_error_details(item)
            for item in value
        ]
        return [item for item in items if item not in (None, {}, [])]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return _vertex_error_clip(value)
    return None


def _safe_vertex_error_headers(response: Any) -> Dict[str, str]:
    headers = getattr(response, "headers", None) if response is not None else None
    if headers is None:
        return {}
    try:
        items = headers.items()
    except Exception:
        return {}
    safe: Dict[str, str] = {}
    for raw_key, raw_val in items:
        key = str(raw_key)
        low = key.lower()
        norm = _vertex_error_key_norm(key)
        if norm in _VERTEX_ERROR_DENIED_KEYS or "authorization" in low:
            continue
        if low not in _VERTEX_SAFE_HEADER_EXACT and "quota" not in low:
            continue
        if _vertex_error_value_is_sensitive(raw_val):
            continue
        safe[key] = str(_vertex_error_clip(raw_val))
    return safe


def _vertex_provider_error_diag(exc: Any, model: str) -> Dict[str, Any]:
    """Build a journal-safe diagnostic payload from google-genai APIError."""
    details = _sanitize_vertex_error_details(getattr(exc, "details", None))
    headers = _safe_vertex_error_headers(getattr(exc, "response", None))
    payload: Dict[str, Any] = {
        "event": "VERTEX_GEMINI_PROVIDER_ERROR",
        "http_code": getattr(exc, "code", None),
        "status": getattr(exc, "status", None),
        "message": _vertex_error_clip(getattr(exc, "message", None)),
        "model": model,
        "project": GOOGLE_CLOUD_PROJECT or None,
        "location": GOOGLE_CLOUD_LOCATION or None,
    }
    if details not in (None, {}, []):
        payload["details"] = details
    if headers:
        payload["headers"] = headers
    return payload


def _log_vertex_provider_error(exc: Any, model: str) -> None:
    diag = _vertex_provider_error_diag(exc, model)
    logger.warning(
        "VERTEX_GEMINI_PROVIDER_ERROR %s",
        json.dumps(diag, default=str, sort_keys=True),
    )


def payload_to_contents(payload: Dict[str, Any]):
    contents = payload.get("contents") or []
    sdk_contents = []
    for item in contents:
        parts = item.get("parts") or []
        sdk_parts = [_payload_part_to_sdk(part) for part in parts]
        role = item.get("role") or "user"
        sdk_contents.append(types.Content(role=role, parts=sdk_parts))
    return sdk_contents


def sdk_response_to_rest_dict(response) -> Dict[str, Any]:
    text = (getattr(response, "text", None) or "").strip()
    if not text:
        candidates = getattr(response, "candidates", None) or []
        if candidates:
            content = getattr(candidates[0], "content", None)
            parts = getattr(content, "parts", None) or []
            text = "".join(getattr(part, "text", "") or "" for part in parts).strip()

    usage_metadata: Dict[str, Any] = {}
    usage = getattr(response, "usage_metadata", None)
    if usage is not None:
        usage_metadata = {
            "promptTokenCount": int(getattr(usage, "prompt_token_count", 0) or 0),
            "candidatesTokenCount": int(getattr(usage, "response_token_count", 0) or 0),
        }

    return {
        "candidates": [{"content": {"parts": [{"text": text}]}}],
        "usageMetadata": usage_metadata,
    }


def generate_content_via_vertex(
    model: str,
    payload: Dict[str, Any],
    timeout: int,
) -> GeminiRestResponse:
    """Invoke Vertex AI generate_content and return REST-shaped response."""
    client = get_vertex_gemini_client()
    contents = payload_to_contents(payload)
    config = payload_to_generate_config(payload)

    def _invoke():
        try:
            return client.models.generate_content(
                model=model,
                contents=contents,
                config=config,
            )
        except genai_errors.APIError as exc:
            if exc.code in (429, 503):
                _log_vertex_provider_error(exc, model)
                raise GeminiProviderError(exc.code, str(exc)) from exc
            raise

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(_invoke)
        try:
            response = future.result(timeout=timeout)
        except FuturesTimeoutError as exc:
            raise TimeoutError(f"Gemini request timed out after {timeout}s") from exc

    return GeminiRestResponse(
        status_code=200,
        _data=sdk_response_to_rest_dict(response),
    )
