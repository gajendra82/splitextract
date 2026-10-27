"""Process-local OCR text override for DI → core extraction wiring.

When DocumentIntelligencePipeline has already run primary OCR (+ optional
secondary repair), core extraction can reuse that text instead of re-OCRing.
Default is unset (no override) so OFF / non-DI callers keep existing behavior.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar, Token
from typing import Iterator, Optional

_OCR_TEXT_OVERRIDE: ContextVar[Optional[str]] = ContextVar(
    "splitextract_ocr_text_override", default=None
)


def get_ocr_text_override() -> Optional[str]:
    return _OCR_TEXT_OVERRIDE.get()


@contextmanager
def ocr_text_override(text: Optional[str]) -> Iterator[None]:
    """Temporarily supply precomputed OCR text to ``_ocr_image_to_text``."""
    token: Token = _OCR_TEXT_OVERRIDE.set(text)
    try:
        yield
    finally:
        _OCR_TEXT_OVERRIDE.reset(token)
