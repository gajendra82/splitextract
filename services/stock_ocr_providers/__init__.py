"""Provider registry for stock OCR benchmarks."""

from __future__ import annotations

from typing import Dict, Type

from services.stock_ocr_providers.base import StockDocumentOCRProvider
from services.stock_ocr_providers.gemini_vision_adapter import GeminiVisionBenchmarkProvider
from services.stock_ocr_providers.mistral_ocr import MistralOCRProvider

PROVIDERS: Dict[str, Type[StockDocumentOCRProvider]] = {
    "mistral": MistralOCRProvider,
    "gemini": GeminiVisionBenchmarkProvider,
}


def get_provider(name: str, **kwargs) -> StockDocumentOCRProvider:
    key = str(name or "").strip().lower()
    if key not in PROVIDERS:
        raise ValueError(f"Unknown OCR provider: {name!r}. Choose from {sorted(PROVIDERS)}")
    return PROVIDERS[key](**kwargs)
