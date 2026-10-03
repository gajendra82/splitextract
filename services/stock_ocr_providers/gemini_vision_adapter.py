"""Gemini vision-table adapter for OCR benchmarks only.

Calls the existing extract_stock_table_vision + map_vision_table path without
changing production routing. Used for side-by-side comparison.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

from services.stock_ocr_providers.base import (
    OCRBlock,
    OCRDocumentResult,
    OCRPage,
    StockDocumentOCRProvider,
    guess_mime,
)

logger = logging.getLogger(__name__)


class GeminiVisionBenchmarkProvider(StockDocumentOCRProvider):
    """Benchmark wrapper around the production Gemini vision-table extractor."""

    name = "gemini"

    def extract_document(
        self,
        file_bytes: bytes,
        *,
        filename: str = "document",
        mime_type: Optional[str] = None,
    ) -> OCRDocumentResult:
        mime = mime_type or guess_mime(filename, file_bytes)
        meta = {
            "filename": filename,
            "mime_type": mime,
            "path": "extract_stock_table_vision",
            "benchmark_only": True,
        }
        image_w = image_h = None
        try:
            from PIL import Image
            import io

            if mime.startswith("image/"):
                with Image.open(io.BytesIO(file_bytes)) as im:
                    image_w, image_h = im.size
        except Exception:
            pass

        started = time.perf_counter()
        try:
            from services.stock_vision_table import (
                extract_stock_table_vision,
                map_vision_table,
            )

            raw = extract_stock_table_vision(
                [file_bytes],
                ctx={"request_id": "ocr-benchmark", "label": "stock_ocr_benchmark"},
            )
            latency_ms = (time.perf_counter() - started) * 1000.0
        except Exception as exc:
            latency_ms = (time.perf_counter() - started) * 1000.0
            return OCRDocumentResult(
                provider=self.name,
                error=f"GEMINI_VISION_ERROR:{type(exc).__name__}:{exc}",
                latency_ms=latency_ms,
                request_metadata=meta,
                image_width=image_w,
                image_height=image_h,
            )

        if isinstance(raw, dict) and raw.get("error"):
            return OCRDocumentResult(
                provider=self.name,
                error=str(raw.get("error")),
                latency_ms=latency_ms,
                request_metadata=meta,
                raw_response=raw,
                image_width=image_w,
                image_height=image_h,
            )

        mapped = map_vision_table(raw, request_id="ocr-benchmark")
        items = list(mapped.get("line_items") or [])
        # If mapping dropped rows (e.g. transient Vision shape), keep raw cells
        # available for diagnostics; still expose whatever mapped successfully.
        pages = self._pages_from_vision(raw, mapped, image_w, image_h)
        if not items and pages:
            pages[0].extra["line_items"] = self._items_from_raw_cells(raw, mapped)
            items = pages[0].extra["line_items"]
        else:
            pages[0].extra["line_items"] = items
        return OCRDocumentResult(
            provider=self.name,
            pages=pages,
            latency_ms=latency_ms,
            model=str(meta.get("model") or "gemini-vision-table"),
            request_metadata={
                **meta,
                "mapped_errors": mapped.get("errors"),
                "mapped_item_count": len(items),
            },
            raw_response={"vision": raw, "mapped": mapped},
            image_width=image_w,
            image_height=image_h,
            usage={"gemini_calls": 1},
        )

    def _items_from_raw_cells(
        self, raw: Dict[str, Any], mapped: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        """Last-resort: rebuild items from Vision cells using mapped column_map."""
        from services.stock_ocr_table_reconstructor import fields_to_line_item

        columns = list(mapped.get("column_map") or [])
        if not columns:
            return []
        printed = sorted(
            {
                str(c.get("canonical"))
                for c in columns
                if c.get("canonical") and c.get("canonical") != "ignore"
            }
        )
        tables = raw.get("tables") or []
        table0 = tables[0] if tables else {}
        items: List[Dict[str, Any]] = []
        for row in table0.get("rows") or []:
            if not isinstance(row, dict) or row.get("is_total_row"):
                continue
            cells = list(row.get("cells") or [])
            fields: Dict[str, Optional[str]] = {
                str(c["canonical"]): None
                for c in columns
                if c.get("canonical") and c["canonical"] != "ignore"
            }
            for c in columns:
                canon = c.get("canonical")
                if not canon or canon == "ignore":
                    continue
                idx = int(c.get("col_index") or 0)
                raw_c = cells[idx] if idx < len(cells) else None
                if raw_c is None or str(raw_c).strip() == "":
                    fields[canon] = None
                else:
                    fields[canon] = str(raw_c)
            if not fields.get("product_name"):
                continue
            try:
                ridx = int(row.get("row_index") or len(items))
            except (TypeError, ValueError):
                ridx = len(items)
            items.append(
                fields_to_line_item(
                    fields,
                    printed_fields=printed,
                    row_index=ridx,
                    source_cells=cells,
                )
            )
        return items

    def _pages_from_vision(
        self,
        raw: Dict[str, Any],
        mapped: Dict[str, Any],
        image_w: Optional[int],
        image_h: Optional[int],
    ) -> List[OCRPage]:
        blocks: List[OCRBlock] = []
        tables = (raw.get("tables") or [{}]) if isinstance(raw, dict) else [{}]
        table0 = tables[0] if tables else {}
        for row in table0.get("rows") or []:
            if not isinstance(row, dict):
                continue
            y = row.get("y_center")
            cells = row.get("cells") or []
            for i, cell in enumerate(cells):
                if cell is None or str(cell).strip() == "":
                    continue
                # Synthetic bbox from col index + y_center (no real Gemini cell boxes).
                x0 = float(i) / max(len(cells), 1)
                x1 = float(i + 1) / max(len(cells), 1)
                y0 = float(y) - 0.01 if y is not None else 0.0
                y1 = float(y) + 0.01 if y is not None else 0.02
                # Scale to pixels when known.
                if image_w and image_h:
                    bbox = (x0 * image_w, y0 * image_h, x1 * image_w, y1 * image_h)
                else:
                    bbox = (x0, y0, x1, y1)
                blocks.append(
                    OCRBlock(
                        text=str(cell),
                        bbox=bbox,
                        page=1,
                        block_type="gemini_cell",
                        extra={"col_index": i, "row_index": row.get("row_index")},
                    )
                )
        return [
            OCRPage(
                page_number=1,
                width=float(image_w) if image_w else None,
                height=float(image_h) if image_h else None,
                markdown="",
                blocks=blocks,
                raw_text="",
                extra={
                    "vision_headers": table0.get("header_rows"),
                    "line_items": mapped.get("line_items"),
                    "column_map": mapped.get("column_map"),
                },
            )
        ]
