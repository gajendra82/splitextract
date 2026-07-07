"""OpenAI GPT text-only invoice extractor."""

import json
import logging
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


def extract_invoice_via_gpt(
    prompt: str, *, api_key: str, model: str, timeout: int
) -> Optional[dict]:
    """
    Text-only extraction via the OpenAI Responses API.

    Receives the same prompt used by the Gemini Text path. Returns the raw dict
    matching the Gemini schema, or None on failure. No validation or business logic.
    """
    if not _OPENAI_SDK_AVAILABLE or not api_key or not model:
        return None

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
        content = (resp.output_text or "").strip()
        if content.startswith("```"):
            content = content.replace("```json", "").replace("```", "").strip()
        parsed = json.loads(content)
        if not isinstance(parsed, dict):
            return None
        parsed.pop("ocr_text", None)
        if isinstance(parsed.get("data"), dict):
            parsed["data"].pop("ocr_text", None)
        return parsed
    except _OPENAI_TIMEOUT_ERRORS as exc:
        raise TimeoutError(str(exc)) from exc
    except json.JSONDecodeError as exc:
        logger.error("GPT text extraction JSON parse error: %s", exc)
        return None
    except Exception as exc:
        logger.error("GPT text extraction error: %s", exc)
        return None


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

        Token usage dict keys: input_tokens, output_tokens.
        """
        if not self.config.enable_text_model:
            return None, {"input_tokens": 0, "output_tokens": 0}

        if self.config.text_provider != "openai":
            logger.warning("Unsupported text provider: %s", self.config.text_provider)
            return None, {"input_tokens": 0, "output_tokens": 0}

        if not self.config.openai_api_key:
            logger.warning("OPENAI_API_KEY not configured")
            return None, {"input_tokens": 0, "output_tokens": 0}

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

        try:
            response = requests.post(
                url,
                headers=headers,
                json=payload,
                timeout=self.config.text_timeout,
            )
            if response.status_code != 200:
                logger.error(
                    "OpenAI text extraction failed: %s %s",
                    response.status_code,
                    response.text[:300],
                )
                return None, {"input_tokens": 0, "output_tokens": 0}

            data = response.json()
            usage = data.get("usage", {})
            token_usage = {
                "input_tokens": int(usage.get("prompt_tokens", 0)),
                "output_tokens": int(usage.get("completion_tokens", 0)),
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
            return None, {"input_tokens": 0, "output_tokens": 0}
