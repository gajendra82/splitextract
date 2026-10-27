"""Extraction strategy registry. Named parsers remain load-bearing."""

from services.extraction.parser_registry import (
    ExtractionStrategy,
    ParserRegistry,
    default_registry,
)

__all__ = [
    "ExtractionStrategy",
    "ParserRegistry",
    "default_registry",
]
