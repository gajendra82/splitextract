"""Registry wrapping existing named parsers without rewriting them."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence

from services.document_router import DocumentRoute
from services.layout.detector import LayoutUnderstanding


ExtractFn = Callable[[bytes, str], Dict[str, Any]]


@dataclass
class ExtractionStrategy:
    name: str
    kind: str
    supports_fn: Callable[[DocumentRoute, LayoutUnderstanding], bool]
    confidence_fn: Callable[[DocumentRoute, LayoutUnderstanding], float]
    extract_fn: Optional[ExtractFn] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def supports(
        self, route: DocumentRoute, layout: LayoutUnderstanding
    ) -> bool:
        return bool(self.supports_fn(route, layout))

    def confidence(
        self, route: DocumentRoute, layout: LayoutUnderstanding
    ) -> float:
        return float(self.confidence_fn(route, layout))


class ParserRegistry:
    """Ordered strategies. Explicit/high-confidence parsers win."""

    def __init__(self, strategies: Optional[Sequence[ExtractionStrategy]] = None):
        self._strategies: List[ExtractionStrategy] = list(strategies or [])

    def register(self, strategy: ExtractionStrategy) -> None:
        self._strategies.append(strategy)

    def strategies(self) -> List[ExtractionStrategy]:
        return list(self._strategies)

    def select(
        self, route: DocumentRoute, layout: LayoutUnderstanding
    ) -> Dict[str, Any]:
        ranked = []
        for strategy in self._strategies:
            if not strategy.supports(route, layout):
                continue
            ranked.append(
                {
                    "name": strategy.name,
                    "kind": strategy.kind,
                    "confidence": strategy.confidence(route, layout),
                    "metadata": dict(strategy.metadata),
                }
            )
        ranked.sort(key=lambda item: item["confidence"], reverse=True)
        selected = ranked[0] if ranked else {
            "name": "generic_structured",
            "kind": "generic",
            "confidence": 0.2,
            "metadata": {"fallback": True},
        }
        return {
            "selected": selected,
            "candidates": ranked,
            "known_parser_preferred": selected.get("kind")
            in {"named_parser", "known_layout"},
        }


def default_registry() -> ParserRegistry:
    """Wrap existing cascade entry points by file kind.

    Actual extraction still runs through ``extract_sales_statement`` body /
    named parsers. The registry is an orchestration abstraction only.
    """
    registry = ParserRegistry()

    def _ext_in(*exts: str):
        wanted = {e.lower() for e in exts}

        def supports(route: DocumentRoute, layout: LayoutUnderstanding) -> bool:
            del layout
            kind = (route.file_kind or "").lower()
            if kind in wanted:
                return True
            # Router may use image/pdf/spreadsheet buckets.
            if "pdf" in wanted and kind == "pdf":
                return True
            if "image" in wanted and kind == "image":
                return True
            if {"xls", "xlsx"} & wanted and kind in {"xls", "xlsx", "spreadsheet"}:
                return True
            return False

        return supports

    registry.register(
        ExtractionStrategy(
            name="named_pdf_cascade",
            kind="named_parser",
            supports_fn=_ext_in("pdf"),
            confidence_fn=lambda route, layout: 0.95
            if not route.requires_ocr
            else 0.85,
            metadata={"note": "Existing _parse_pdf cascade"},
        )
    )
    registry.register(
        ExtractionStrategy(
            name="named_image_cascade",
            kind="named_parser",
            supports_fn=_ext_in("image"),
            confidence_fn=lambda route, layout: 0.8,
            metadata={"note": "Existing _parse_image cascade"},
        )
    )
    registry.register(
        ExtractionStrategy(
            name="named_spreadsheet_cascade",
            kind="named_parser",
            supports_fn=_ext_in("xls", "xlsx"),
            confidence_fn=lambda route, layout: 0.92,
            metadata={"note": "Existing _parse_xls cascade"},
        )
    )
    registry.register(
        ExtractionStrategy(
            name="named_text_cascade",
            kind="named_parser",
            supports_fn=_ext_in("txt", "htm", "html", "docx", "doc"),
            confidence_fn=lambda route, layout: 0.9,
            metadata={"note": "Existing text/word parsers"},
        )
    )
    registry.register(
        ExtractionStrategy(
            name="known_layout",
            kind="known_layout",
            supports_fn=lambda route, layout: bool(layout.known_layout),
            confidence_fn=lambda route, layout: max(0.7, layout.confidence),
            metadata={"note": "Layout-scoped known profile"},
        )
    )
    registry.register(
        ExtractionStrategy(
            name="generic_structured",
            kind="generic",
            supports_fn=lambda route, layout: True,
            confidence_fn=lambda route, layout: 0.25
            if not layout.known_layout
            else 0.1,
            metadata={"note": "Unknown-layout structural fallback"},
        )
    )
    registry.register(
        ExtractionStrategy(
            name="vision_structural",
            kind="vision",
            supports_fn=lambda route, layout: bool(
                route.requires_ocr or route.file_kind == "image"
            ),
            confidence_fn=lambda route, layout: 0.15,
            metadata={"note": "Targeted Gemini/vision only when needed"},
        )
    )
    registry.register(
        ExtractionStrategy(
            name="recovery",
            kind="recovery",
            supports_fn=lambda route, layout: True,
            confidence_fn=lambda route, layout: 0.05,
            metadata={"note": "Phase 2 recovery path"},
        )
    )
    return registry
