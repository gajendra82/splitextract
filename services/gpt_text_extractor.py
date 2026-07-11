"""OpenAI GPT text-only invoice extractor."""

import json
import logging
import time
from typing import Callable, Dict, Optional, Tuple

import requests

from services.config import EXTRACTION_CONFIG
from services.prompts import build_strict_text_prompt

logger = logging.getLogger(__name__)

try:
    from openai import APITimeoutError, OpenAI

    _OPENAI_SDK_AVAILABLE = True
    _OPENAI_TIMEOUT_ERRORS = (APITimeoutError,)
except Exception:
    OpenAI = None
    _OPENAI_SDK_AVAILABLE = False
    _OPENAI_TIMEOUT_ERRORS = ()


def _empty_usage() -> Dict[str, int]:
    return {
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "response_time_ms": 0,
    }


def extract_invoice_via_gpt(
    prompt: str, *, api_key: str, model: str, timeout: int
) -> Tuple[Optional[dict], Dict[str, int]]:
    """
    Text-only extraction via the OpenAI Responses API.

    Receives the same prompt used by the Gemini Text path. Returns the raw dict
    matching the Gemini schema, or None on failure. No validation or business logic.
    """
    if not _OPENAI_SDK_AVAILABLE or not api_key or not model:
        return None, _empty_usage()

    started = time.monotonic()
    try:
        client = OpenAI(api_key=api_key, timeout=timeout)
        resp = client.responses.create(
            model=model,
            input=[
                {
                    "role": "system",
                    "content": (
                        "You are an invoice data extraction engine. "
                        "Return valid JSON only. Never hallucinate."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
        )
        elapsed_ms = (time.monotonic() - started) * 1000.0
        usage = getattr(resp, "usage", None)
        token_usage = {
            "input_tokens": int(getattr(usage, "input_tokens", 0) or 0),
            "output_tokens": int(getattr(usage, "output_tokens", 0) or 0),
            "total_tokens": int(getattr(usage, "total_tokens", 0) or 0),
            "response_time_ms": int(round(elapsed_ms)),
        }
        content = (resp.output_text or "").strip()
        if content.startswith("```"):
            content = content.replace("```json", "").replace("```", "").strip()
        parsed = json.loads(content)
        if not isinstance(parsed, dict):
            return None, token_usage
        parsed.pop("ocr_text", None)
        if isinstance(parsed.get("data"), dict):
            parsed["data"].pop("ocr_text", None)
        return parsed, token_usage
    except _OPENAI_TIMEOUT_ERRORS as exc:
        raise TimeoutError(str(exc)) from exc
    except json.JSONDecodeError as exc:
        logger.error("GPT text extraction JSON parse error: %s", exc)
        return None, _empty_usage()
    except Exception as exc:
        logger.error("GPT text extraction error: %s", exc)
        return None, _empty_usage()


class GPTTextExtractor:
    """
    Extract structured invoice JSON from OCR text using a text-only LLM.
    Never sends images.
    """

    def __init__(
        self,
        config=EXTRACTION_CONFIG,
        prompt_builder: Optional[Callable[[str], str]] = None,
    ):
        self.config = config
        self.prompt_builder = prompt_builder or (
            lambda text: build_strict_text_prompt(text)
        )

    def extract(self, ocr_text: str) -> Tuple[Optional[dict], Dict[str, int]]:
        """
        Call the configured text model and return (parsed_json, token_usage).

        Token usage dict keys: input_tokens, output_tokens, total_tokens, response_time_ms.
        """
        if not self.config.enable_text_model:
            return None, _empty_usage()

        if self.config.text_provider != "openai":
            logger.warning("Unsupported text provider: %s", self.config.text_provider)
            return None, _empty_usage()

        if not self.config.openai_api_key:
            logger.warning("OPENAI_API_KEY not configured")
            return None, _empty_usage()

        prompt = self.prompt_builder(ocr_text)
        url = "https://api.openai.com/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.config.openai_api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.config.text_model,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You are an invoice data extraction engine. "
                        "Return valid JSON only. Never hallucinate."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
        }

        started = time.monotonic()
        try:
            response = requests.post(
                url,
                headers=headers,
                json=payload,
                timeout=self.config.text_timeout,
            )
            elapsed_ms = (time.monotonic() - started) * 1000.0
            if response.status_code != 200:
                logger.error(
                    "OpenAI text extraction failed: %s %s",
                    response.status_code,
                    response.text[:300],
                )
                return None, _empty_usage()

            data = response.json()
            usage = data.get("usage", {})
            token_usage = {
                "input_tokens": int(usage.get("prompt_tokens", 0)),
                "output_tokens": int(usage.get("completion_tokens", 0)),
                "total_tokens": int(usage.get("total_tokens", 0)),
                "response_time_ms": int(round(elapsed_ms)),
            }
            content = data["choices"][0]["message"]["content"].strip()
            if content.startswith("```"):
                content = (
                    content.replace("```json", "").replace("```", "").strip()
                )
            parsed = json.loads(content)
            if isinstance(parsed, dict):
                parsed.pop("ocr_text", None)
                if isinstance(parsed.get("data"), dict):
                    parsed["data"].pop("ocr_text", None)
            return parsed, token_usage
        except Exception as exc:
            logger.error("GPT text extraction error: %s", exc)
            return None, _empty_usage()
