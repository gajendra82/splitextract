"""Shared evidence containers. Parsers are not required to read these yet."""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from services.document_router import DocumentRoute

_MAX_TOKENS_PER_PAGE = 2000
_EMBEDDED_TEXT_MIN_CHARS = 40


@dataclass
class BBox:
    x0: float
    y0: float
    x1: float
    y1: float

    def to_dict(self) -> Dict[str, float]:
        return asdict(self)


@dataclass
class TextToken:
    text: str
    confidence: Optional[float]
    bbox: Optional[BBox]
    page_number: int
    source_engine: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "text": self.text,
            "confidence": self.confidence,
            "bbox": self.bbox.to_dict() if self.bbox else None,
            "page_number": self.page_number,
            "source_engine": self.source_engine,
        }


@dataclass
class TableEvidence:
    page_number: int
    rows: List[List[str]] = field(default_factory=list)
    bbox: Optional[BBox] = None
    source_engine: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "page_number": self.page_number,
            "rows": self.rows,
            "bbox": self.bbox.to_dict() if self.bbox else None,
            "source_engine": self.source_engine,
        }


@dataclass
class PageEvidence:
    page_number: int
    text: str = ""
    image_ref: Optional[str] = None
    width: Optional[float] = None
    height: Optional[float] = None
    extraction_method: str = ""
    tokens: List[TextToken] = field(default_factory=list)
    tables: List[TableEvidence] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self, *, include_tokens: bool = True) -> Dict[str, Any]:
        payload = {
            "page_number": self.page_number,
            "text": self.text,
            "image_ref": self.image_ref,
            "width": self.width,
            "height": self.height,
            "extraction_method": self.extraction_method,
            "metadata": dict(self.metadata),
            "token_count": int(self.metadata.get("token_count", len(self.tokens))),
            "table_count": len(self.tables),
        }
        if include_tokens:
            payload["tokens"] = [token.to_dict() for token in self.tokens]
            payload["tables"] = [table.to_dict() for table in self.tables]
        return payload


@dataclass
class DocumentEvidence:
    document_id: str
    source_type: str
    pages: List[PageEvidence] = field(default_factory=list)
    extraction_engine: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self, *, include_tokens: bool = True) -> Dict[str, Any]:
        return {
            "document_id": self.document_id,
            "source_type": self.source_type,
            "extraction_engine": self.extraction_engine,
            "metadata": dict(self.metadata),
            "pages": [
                page.to_dict(include_tokens=include_tokens) for page in self.pages
            ],
        }

    def summary(self) -> Dict[str, Any]:
        """API-safe view. Omits token text, page text, and image bytes."""
        payload = self.to_dict(include_tokens=False)
        for page in payload["pages"]:
            text = page.pop("text", "") or ""
            recorded = (page.get("metadata") or {}).get("text_length")
            page["text_length"] = int(recorded) if recorded is not None else len(text)
        return payload


def evidence_for_route(
    file_bytes: bytes,
    filename: str,
    route: DocumentRoute,
    *,
    file_path: Optional[str] = None,
    retain_tokens: bool = False,
    retain_text: bool = False,
) -> DocumentEvidence:
    """Build evidence for an API response.

    When the router already measured PDF pages, those counts are reused and
    the PDF is not opened again. Token objects and page text are omitted
    unless a caller explicitly asks to keep them.
    """
    if route.file_kind == "pdf" and route.page_stats and not retain_tokens and not retain_text:
        pages = [
            PageEvidence(
                page_number=int(stat["page_number"]),
                text="",
                width=stat.get("width"),
                height=stat.get("height"),
                extraction_method=str(stat.get("extraction_method") or ""),
                metadata={
                    "text_length": int(stat.get("text_length") or 0),
                    "token_count": int(stat.get("token_count") or 0),
                    "tokens_truncated": bool(stat.get("tokens_truncated")),
                },
            )
            for stat in route.page_stats
        ]
        return DocumentEvidence(
            document_id=_document_id(filename, file_bytes, file_path),
            source_type="pdf",
            pages=pages,
            extraction_engine="pymupdf",
            metadata={
                "requires_ocr": route.requires_ocr,
                "pdf_kind": route.pdf_kind,
                "page_count": len(pages),
                "pages_read": len(pages),
                "image_bytes_stored": False,
            },
        )
    return build_document_evidence(
        file_bytes,
        filename,
        route,
        file_path=file_path,
        retain_tokens=retain_tokens,
        retain_text=retain_text,
    )


def build_document_evidence(
    file_bytes: bytes,
    filename: str,
    route: DocumentRoute,
    *,
    file_path: Optional[str] = None,
    retain_tokens: bool = True,
    retain_text: bool = True,
) -> DocumentEvidence:
    """Fill evidence from embedded PDF text or a light file header.

    Image bytes are not copied. Scanned pages keep a page shell and word
    tokens only when PyMuPDF already has them.
    """
    source_type = route.file_kind or "unsupported"
    document_id = _document_id(filename, file_bytes, file_path)
    if route.file_kind == "pdf":
        pages, engine, meta = _pdf_pages(
            file_bytes,
            file_path,
            retain_tokens=retain_tokens,
            retain_text=retain_text,
        )
    elif route.file_kind == "image":
        pages, engine, meta = _image_page(file_bytes, file_path)
    else:
        pages = [
            PageEvidence(
                page_number=1,
                extraction_method="native",
                metadata={"grid_copied": False},
            )
        ]
        engine = "native"
        meta = {}
    meta["requires_ocr"] = route.requires_ocr
    meta["pdf_kind"] = route.pdf_kind
    return DocumentEvidence(
        document_id=document_id,
        source_type=source_type,
        pages=pages,
        extraction_engine=engine,
        metadata=meta,
    )


def tokens_from_tesseract_data(
    data: Dict[str, Any],
    page_number: int,
    *,
    source_engine: str = "tesseract",
) -> List[TextToken]:
    """Map one pytesseract image_to_data dict into text tokens."""
    texts = data.get("text") or []
    tokens: List[TextToken] = []
    count = len(texts)
    for index in range(count):
        text = str(texts[index] or "").strip()
        if not text:
            continue
        confidence = _optional_confidence(_column(data, "conf", index))
        if confidence is not None and confidence < 0:
            continue
        left = _optional_float(_column(data, "left", index))
        top = _optional_float(_column(data, "top", index))
        width = _optional_float(_column(data, "width", index))
        height = _optional_float(_column(data, "height", index))
        bbox = None
        if None not in {left, top, width, height}:
            bbox = BBox(left, top, left + width, top + height)
        tokens.append(
            TextToken(
                text=text,
                confidence=confidence,
                bbox=bbox,
                page_number=page_number,
                source_engine=source_engine,
            )
        )
    return tokens


def _pdf_pages(
    file_bytes: bytes,
    file_path: Optional[str],
    *,
    retain_tokens: bool = True,
    retain_text: bool = True,
) -> tuple:
    try:
        import fitz
    except ImportError:
        return [], "pymupdf_unavailable", {"error": "pymupdf_unavailable"}

    try:
        if file_path and os.path.exists(file_path):
            doc = fitz.open(file_path)
        elif file_bytes:
            doc = fitz.open(stream=file_bytes, filetype="pdf")
        else:
            return [], "pymupdf", {"error": "pdf_bytes_missing"}
    except Exception as exc:
        return [], "pymupdf", {"error": "pdf_open_failed", "detail": str(exc)}

    pages: List[PageEvidence] = []
    try:
        limit = min(int(doc.page_count or 0), _page_limit())
        for index in range(limit):
            page = doc[index]
            text = page.get_text("text") or ""
            compact = len(re.sub(r"\s+", "", text))
            words = page.get_text("words") or []
            if retain_tokens:
                tokens, truncated = _tokens_from_pymupdf_words(
                    words,
                    page_number=index + 1,
                )
                token_count = len(tokens)
            else:
                tokens = []
                token_count, truncated = _count_words(words)
            rect = page.rect
            method = "pymupdf" if compact >= _EMBEDDED_TEXT_MIN_CHARS else "embedded_sparse"
            metadata: Dict[str, Any] = {
                "tokens_truncated": truncated,
                "token_count": token_count,
            }
            if not retain_text:
                metadata["text_length"] = len(text)
            pages.append(
                PageEvidence(
                    page_number=index + 1,
                    text=text if retain_text else "",
                    image_ref=None,
                    width=float(rect.width),
                    height=float(rect.height),
                    extraction_method=method,
                    tokens=tokens,
                    metadata=metadata,
                )
            )
        meta = {"page_count": int(doc.page_count or 0), "pages_read": limit}
    finally:
        doc.close()
    return pages, "pymupdf", meta


def _count_words(words: List[Any]) -> tuple:
    count = 0
    truncated = False
    for word in words:
        if count >= _MAX_TOKENS_PER_PAGE:
            truncated = True
            break
        if not word or len(word) < 5:
            continue
        if not str(word[4] or "").strip():
            continue
        count += 1
    return count, truncated


def _tokens_from_pymupdf_words(words: List[Any], page_number: int) -> tuple:
    tokens: List[TextToken] = []
    truncated = False
    for word in words:
        if len(tokens) >= _MAX_TOKENS_PER_PAGE:
            truncated = True
            break
        if not word or len(word) < 5:
            continue
        text = str(word[4] or "").strip()
        if not text:
            continue
        tokens.append(
            TextToken(
                text=text,
                confidence=None,
                bbox=BBox(float(word[0]), float(word[1]), float(word[2]), float(word[3])),
                page_number=page_number,
                source_engine="pymupdf",
            )
        )
    return tokens, truncated


def _image_page(file_bytes: bytes, file_path: Optional[str]) -> tuple:
    width = None
    height = None
    try:
        from PIL import Image as PILImage
        import io

        if file_bytes:
            with PILImage.open(io.BytesIO(file_bytes)) as image:
                width, height = image.size
        elif file_path and os.path.exists(file_path):
            with PILImage.open(file_path) as image:
                width, height = image.size
    except Exception:
        width = height = None
    page = PageEvidence(
        page_number=1,
        image_ref="upload",
        width=float(width) if width else None,
        height=float(height) if height else None,
        extraction_method="image_header",
        metadata={"image_bytes_stored": False},
    )
    return [page], "image_header", {"image_bytes_stored": False}


def _document_id(filename: str, file_bytes: bytes, file_path: Optional[str]) -> str:
    size = len(file_bytes or b"")
    head = (file_bytes or b"")[:16]
    if file_path and os.path.exists(file_path) and not head:
        try:
            size = os.path.getsize(file_path)
            with open(file_path, "rb") as handle:
                head = handle.read(16)
        except OSError:
            pass
    digest = hashlib.sha256(
        f"{Path(filename or '').name}|{size}|".encode("utf-8") + head
    ).hexdigest()
    return digest[:16]


def _page_limit() -> int:
    raw = os.getenv("SALES_PDF_MAX_PAGES", "20")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return 20
    return value if value > 0 else 20


def _column(data: Dict[str, Any], name: str, index: int) -> Any:
    values = data.get(name) or []
    if index >= len(values):
        return None
    return values[index]


def _optional_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _optional_confidence(value: Any) -> Optional[float]:
    number = _optional_float(value)
    if number is None:
        return None
    return number
