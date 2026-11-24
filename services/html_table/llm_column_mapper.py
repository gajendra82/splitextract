"""LLM fallback column mapper for unknown HTML table headers."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from typing import Any, Dict, List, Optional

from services.html_table.column_matcher import SCHEMA_FIELDS

logger = logging.getLogger(__name__)

_CACHE: Dict[str, Dict[int, str]] = {}

_MODEL = "gemini-2.5-flash-lite"

_PROMPT = """You are a column mapper for pharma stock/sales statements.
Map each header to one of these fields: {fields}.
If no match, use null.
Return JSON only as an object mapping column index (string or int) to field name or null.
Headers (index: text):
{headers}
"""


def _cache_key(headers: List[str]) -> str:
    normalized = [re.sub(r"\s+", " ", str(h or "").strip().casefold()) for h in headers]
    return hashlib.md5("|".join(sorted(normalized)).encode("utf-8")).hexdigest()


def _enabled() -> bool:
    return os.getenv("ENABLE_LLM_COLUMN_MAPPER", "true").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def clear_llm_column_cache() -> None:
    """Test helper: drop in-process cache."""
    _CACHE.clear()


def llm_map_columns(headers: List[str]) -> Dict[int, str]:
    """Map headers via Gemini flash-lite. Results cached by header-set fingerprint."""
    key = _cache_key(headers or [])
    if key in _CACHE:
        return dict(_CACHE[key])

    logger.info(
        "llm_column_mapper: uncached header set key=%s headers=%s",
        key[:12],
        [str(h) for h in (headers or [])],
    )

    if not _enabled():
        logger.info("llm_column_mapper: disabled via ENABLE_LLM_COLUMN_MAPPER")
        _CACHE[key] = {}
        return {}

    mapped = _call_gemini(headers or [])
    _CACHE[key] = dict(mapped)
    return mapped


def _call_gemini(headers: List[str]) -> Dict[int, str]:
    fields = ", ".join(SCHEMA_FIELDS)
    header_lines = "\n".join(f"{i}: {h}" for i, h in enumerate(headers))
    prompt = _PROMPT.format(fields=fields, headers=header_lines)

    try:
        from services.vertex_gemini_client import generate_content_via_vertex
    except Exception:
        logger.warning("llm_column_mapper: vertex client unavailable", exc_info=True)
        return {}

    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0,
            "responseMimeType": "application/json",
        },
    }
    try:
        response = generate_content_via_vertex(
            model=os.getenv("LLM_COLUMN_MAPPER_MODEL", _MODEL).strip() or _MODEL,
            payload=payload,
            timeout=int(os.getenv("LLM_COLUMN_MAPPER_TIMEOUT_S", "30")),
        )
    except Exception:
        logger.warning("llm_column_mapper: Gemini call failed", exc_info=True)
        return {}

    text = _response_text(response)
    return _parse_mapping_json(text, header_count=len(headers))


def _response_text(response: Any) -> str:
    if response is None:
        return ""
    try:
        data = response.json() if hasattr(response, "json") else response
    except Exception:
        return ""
    if not isinstance(data, dict):
        return str(data or "")
    candidates = data.get("candidates") or []
    if not candidates:
        return ""
    parts = ((candidates[0].get("content") or {}).get("parts")) or []
    chunks = [str(p.get("text") or "") for p in parts if isinstance(p, dict)]
    return "".join(chunks).strip()


def _parse_mapping_json(text: str, *, header_count: int) -> Dict[int, str]:
    if not text:
        return {}
    raw = text.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if not match:
            logger.warning("llm_column_mapper: non-JSON response: %s", raw[:200])
            return {}
        try:
            payload = json.loads(match.group(0))
        except json.JSONDecodeError:
            logger.warning("llm_column_mapper: JSON parse failed: %s", raw[:200])
            return {}

    allowed = set(SCHEMA_FIELDS)
    out: Dict[int, str] = {}
    claimed: Dict[str, int] = {}

    items = payload.items() if isinstance(payload, dict) else []
    for key, value in items:
        try:
            index = int(key)
        except (TypeError, ValueError):
            continue
        if index < 0 or index >= header_count:
            continue
        if value is None or str(value).strip().lower() in {"", "null", "none"}:
            continue
        field = str(value).strip()
        if field not in allowed:
            continue
        if field in claimed:
            logger.warning(
                "llm_column_mapper: duplicate field %s at cols %s and %s; keeping first",
                field,
                claimed[field],
                index,
            )
            continue
        out[index] = field
        claimed[field] = index
    return out
