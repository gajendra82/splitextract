"""Mistral OCR provider (benchmark-only).

Uses mistralai.client.Mistral OCR API with include_blocks + table_format=html
and optional word confidence. Credentials from env only.
"""

from __future__ import annotations

import base64
import logging
import os
import time
from typing import Any, Dict, List, Optional, Tuple

from services.stock_ocr_providers.base import (
    OCRBlock,
    OCRDocumentResult,
    OCRPage,
    OCRTable,
    StockDocumentOCRProvider,
    guess_mime,
)

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "mistral-ocr-latest"


def _env_model() -> str:
    return (
        os.getenv("MISTRAL_OCR_MODEL")
        or os.getenv("MISTRAL_OCR_MODEL_NAME")
        or DEFAULT_MODEL
    ).strip() or DEFAULT_MODEL


def _bbox(
    top_left_x: Any,
    top_left_y: Any,
    bottom_right_x: Any,
    bottom_right_y: Any,
) -> Optional[Tuple[float, float, float, float]]:
    try:
        return (
            float(top_left_x),
            float(top_left_y),
            float(bottom_right_x),
            float(bottom_right_y),
        )
    except (TypeError, ValueError):
        return None


def _block_confidence(block: Any) -> Optional[float]:
    scores = getattr(block, "confidence_scores", None)
    if scores is None and isinstance(block, dict):
        scores = block.get("confidence_scores")
    if scores is None:
        return None
    for key in (
        "average_content_confidence_score",
        "minimum_content_confidence_score",
        "block_type_confidence_score",
    ):
        val = getattr(scores, key, None)
        if val is None and isinstance(scores, dict):
            val = scores.get(key)
        if val is not None:
            try:
                return float(val)
            except (TypeError, ValueError):
                continue
    return None


def _as_dict(obj: Any) -> Any:
    if obj is None:
        return None
    if hasattr(obj, "model_dump"):
        try:
            return obj.model_dump(mode="python")
        except Exception:
            pass
    if hasattr(obj, "dict"):
        try:
            return obj.dict()
        except Exception:
            pass
    if isinstance(obj, dict):
        return obj
    return str(obj)


class MistralOCRProvider(StockDocumentOCRProvider):
    name = "mistral"

    def __init__(
        self,
        *,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        timeout_ms: Optional[int] = None,
    ) -> None:
        if api_key is None:
            self.api_key = (os.getenv("MISTRAL_API_KEY") or "").strip()
        else:
            self.api_key = str(api_key).strip()
        self.model = (model or _env_model()).strip()
        try:
            self.timeout_ms = int(
                timeout_ms
                if timeout_ms is not None
                else os.getenv("MISTRAL_OCR_TIMEOUT_MS", "120000")
            )
        except (TypeError, ValueError):
            self.timeout_ms = 120000

    def extract_document(
        self,
        file_bytes: bytes,
        *,
        filename: str = "document",
        mime_type: Optional[str] = None,
    ) -> OCRDocumentResult:
        mime = mime_type or guess_mime(filename, file_bytes)
        meta: Dict[str, Any] = {
            "filename": filename,
            "mime_type": mime,
            "model": self.model,
            "include_blocks": True,
            "table_format": "html",
            "confidence_scores_granularity": "word",
        }
        if not self.api_key:
            return OCRDocumentResult(
                provider=self.name,
                error="MISTRAL_API_KEY_MISSING",
                model=self.model,
                request_metadata=meta,
            )
        if not file_bytes:
            return OCRDocumentResult(
                provider=self.name,
                error="EMPTY_FILE",
                model=self.model,
                request_metadata=meta,
            )

        try:
            from mistralai.client import Mistral
        except ImportError:
            return OCRDocumentResult(
                provider=self.name,
                error="MISTRAL_SDK_MISSING",
                model=self.model,
                request_metadata=meta,
            )

        b64 = base64.b64encode(file_bytes).decode("ascii")
        if mime == "application/pdf":
            document = {
                "type": "document_url",
                "document_url": f"data:application/pdf;base64,{b64}",
            }
        else:
            # image_url accepts data URLs for PNG/JPEG/etc.
            document = {
                "type": "image_url",
                "image_url": f"data:{mime};base64,{b64}",
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
            with Mistral(api_key=self.api_key) as client:
                response = client.ocr.process(
                    model=self.model,
                    document=document,
                    include_blocks=True,
                    table_format="html",
                    confidence_scores_granularity="word",
                    include_image_base64=False,
                    timeout_ms=self.timeout_ms,
                )
            latency_ms = (time.perf_counter() - started) * 1000.0
        except Exception as exc:
            latency_ms = (time.perf_counter() - started) * 1000.0
            logger.info(
                "STOCK_OCR_BENCHMARK mistral_error=%s latency_ms=%.1f",
                type(exc).__name__,
                latency_ms,
            )
            return OCRDocumentResult(
                provider=self.name,
                error=f"MISTRAL_OCR_ERROR:{type(exc).__name__}:{exc}",
                model=self.model,
                latency_ms=latency_ms,
                request_metadata=meta,
                image_width=image_w,
                image_height=image_h,
            )

        pages = self._normalize_pages(response)
        usage: Dict[str, Any] = {}
        usage_info = getattr(response, "usage_info", None)
        if usage_info is not None:
            usage = _as_dict(usage_info) or {}
        model_name = getattr(response, "model", None) or self.model

        logger.info(
            "STOCK_OCR_BENCHMARK provider=mistral model=%s pages=%s latency_ms=%.1f",
            model_name,
            len(pages),
            latency_ms,
        )
        return OCRDocumentResult(
            provider=self.name,
            pages=pages,
            latency_ms=latency_ms,
            model=str(model_name),
            usage=usage if isinstance(usage, dict) else {},
            request_metadata=meta,
            raw_response=_as_dict(response),
            image_width=image_w,
            image_height=image_h,
        )

    def _normalize_pages(self, response: Any) -> List[OCRPage]:
        out: List[OCRPage] = []
        raw_pages = getattr(response, "pages", None) or []
        for page in raw_pages:
            idx = int(getattr(page, "index", 0) or 0)
            dims = getattr(page, "dimensions", None)
            width = height = dpi = None
            if dims is not None:
                width = getattr(dims, "width", None)
                height = getattr(dims, "height", None)
                dpi = getattr(dims, "dpi", None)
            conf = None
            page_conf = getattr(page, "confidence_scores", None)
            if page_conf is not None:
                conf = getattr(page_conf, "average_page_confidence_score", None)
                try:
                    conf = float(conf) if conf is not None else None
                except (TypeError, ValueError):
                    conf = None

            blocks: List[OCRBlock] = []
            for block in getattr(page, "blocks", None) or []:
                btype = getattr(block, "type", None) or "text"
                content = getattr(block, "content", None)
                text = "" if content is None else str(content)
                bbox = _bbox(
                    getattr(block, "top_left_x", None),
                    getattr(block, "top_left_y", None),
                    getattr(block, "bottom_right_x", None),
                    getattr(block, "bottom_right_y", None),
                )
                table_id = getattr(block, "table_id", None)
                blocks.append(
                    OCRBlock(
                        text=text,
                        bbox=bbox,
                        confidence=_block_confidence(block),
                        page=idx + 1,
                        block_type=str(btype),
                        table_id=str(table_id) if table_id is not None else None,
                    )
                )

            tables: List[OCRTable] = []
            for table in getattr(page, "tables", None) or []:
                tid = str(getattr(table, "id", "") or "")
                fmt = getattr(table, "format_", None) or getattr(table, "format", None) or "html"
                content = getattr(table, "content", None) or ""
                wcs = []
                for score in getattr(table, "word_confidence_scores", None) or []:
                    wcs.append(
                        {
                            "text": getattr(score, "text", None),
                            "confidence": getattr(score, "confidence", None),
                            "start_index": getattr(score, "start_index", None),
                        }
                    )
                tables.append(
                    OCRTable(
                        table_id=tid,
                        format=str(fmt),
                        content=str(content),
                        page=idx + 1,
                        word_confidence_scores=wcs,
                    )
                )

            markdown = str(getattr(page, "markdown", "") or "")
            out.append(
                OCRPage(
                    page_number=idx + 1,
                    width=float(width) if width is not None else None,
                    height=float(height) if height is not None else None,
                    dpi=float(dpi) if dpi is not None else None,
                    markdown=markdown,
                    blocks=blocks,
                    tables=tables,
                    raw_text=markdown,
                    confidence=conf,
                )
            )
        return out
