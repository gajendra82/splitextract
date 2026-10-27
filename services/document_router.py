"""Classify an upload before extraction. Does not parse line items."""

from __future__ import annotations

import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from services.file_type_detector import FileKind, FileTypeDetector

# Same per-page embedded-text cutoff _parse_pdf uses before OCR.
_EMBEDDED_TEXT_MIN_CHARS = 40
_PDF_CLASSIFY_MAX_PAGES = 20

_IMAGE_EXTENSIONS = {
    ".jpg",
    ".jpeg",
    ".png",
    ".bmp",
    ".tif",
    ".tiff",
    ".webp",
}
_EXT_TO_KIND = {
    ".pdf": "pdf",
    ".xls": "xls",
    ".xlsx": "xlsx",
    ".xlsm": "xlsx",
    ".doc": "doc",
    ".docx": "docx",
    ".htm": "html",
    ".html": "html",
    ".txt": "txt",
}
_EXT_TO_KIND.update({ext: "image" for ext in _IMAGE_EXTENSIONS})

_INVOICE_KINDS = {"pdf", "image", "xls", "xlsx"}
_SALES_KINDS = {"pdf", "image", "xls", "xlsx", "doc", "docx", "html", "txt"}
_SALES_ONLY_KINDS = _SALES_KINDS - _INVOICE_KINDS


@dataclass
class DocumentRoute:
    """Which extraction lane an upload should use. Not an extraction result."""

    document_type: str
    file_kind: str
    pdf_kind: Optional[str] = None
    requires_ocr: bool = False
    confidence: float = 0.0
    reasons: List[str] = field(default_factory=list)
    # Page measurements from a single PDF open. Not part of the API route.
    page_stats: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        payload = asdict(self)
        payload.pop("page_stats", None)
        payload["confidence"] = round(float(self.confidence), 4)
        return payload


def route_upload(
    file_path: str,
    filename: str,
    *,
    requested_product: Optional[str] = None,
) -> DocumentRoute:
    """Classify a file already stored on disk. PDF pages are opened by path."""
    return route_document(
        b"",
        filename,
        requested_product=requested_product,
        file_path=file_path,
    )


def route_document(
    file_bytes: bytes,
    filename: str,
    *,
    requested_product: Optional[str] = None,
    file_path: Optional[str] = None,
    collect_page_stats: bool = False,
) -> DocumentRoute:
    """Classify bytes or a path. Does not extract products or call a model."""
    reasons: List[str] = []
    ext = _resolve_extension(
        file_bytes or b"",
        filename,
        file_path,
        reasons,
        allow_csv_text_sniff=False,
    )
    file_kind = _EXT_TO_KIND.get(ext, "")
    if not file_kind:
        file_kind, detector_reason = _kind_from_detector(
            filename, file_bytes, file_path
        )
        if detector_reason:
            reasons.append(detector_reason)
    if not file_kind:
        file_kind = "unsupported"
        reasons.append("unsupported_file_kind")

    if ext == ".xlsm" and file_kind == "xlsx":
        reasons.append("xlsm_treated_as_xlsx")

    pdf_kind: Optional[str] = None
    page_stats: List[Dict[str, Any]] = []
    requires_ocr = False
    confidence = 0.95 if ext in _EXT_TO_KIND else 0.7

    if file_kind == "pdf":
        pdf_kind, requires_ocr, pdf_confidence, pdf_reasons, page_stats = _classify_pdf(
            file_bytes,
            file_path,
            collect_page_stats=collect_page_stats,
        )
        reasons.extend(pdf_reasons)
        confidence = min(confidence, pdf_confidence)
    elif file_kind == "image":
        requires_ocr = True
        reasons.append("image_requires_ocr")
    elif file_kind == "unsupported":
        confidence = 0.2

    document_type, type_reasons, type_confidence = _document_type(
        file_kind, requested_product
    )
    reasons.extend(type_reasons)
    if document_type == "unsupported":
        confidence = min(confidence, type_confidence)

    return DocumentRoute(
        document_type=document_type,
        file_kind=file_kind,
        pdf_kind=pdf_kind,
        requires_ocr=requires_ocr,
        confidence=confidence,
        reasons=reasons,
        page_stats=page_stats if file_kind == "pdf" else [],
    )


def _resolve_extension(
    file_bytes: bytes,
    filename: str,
    file_path: Optional[str],
    reasons: List[str],
    *,
    allow_csv_text_sniff: bool = False,
) -> str:
    ext = Path(filename or "").suffix.lower()
    if ext in _EXT_TO_KIND:
        return ext
    payload = file_bytes
    if not payload and file_path and os.path.exists(file_path):
        try:
            with open(file_path, "rb") as handle:
                payload = handle.read()
        except OSError:
            payload = b""
    try:
        from services.sales_statement_extractor import _sniff_extension

        sniffed = _sniff_extension(payload, filename or "upload")
    except Exception:
        sniffed = ext
        reasons.append("extension_sniff_failed")
    else:
        sniffed = (sniffed or ext or "").lower()
        # A .csv name is not a sales text file. Sniff may still recover a
        # real PDF, image, spreadsheet, or Word payload saved with that name.
        if ext == ".csv" and sniffed in {"", ".csv", ".txt"} and not allow_csv_text_sniff:
            reasons.append("csv_not_supported")
            return ".csv"
        if sniffed and sniffed != ext:
            reasons.append("extension_sniffed")
    return (sniffed or ext or "").lower()


def _kind_from_detector(
    filename: str,
    file_bytes: bytes,
    file_path: Optional[str],
) -> tuple:
    payload = file_bytes
    if not payload and file_path and os.path.exists(file_path):
        try:
            with open(file_path, "rb") as handle:
                payload = handle.read(16)
        except OSError:
            payload = b""
    kind, _detected_ext = FileTypeDetector().detect(
        filename=filename,
        file_bytes=payload or None,
    )
    if kind == FileKind.PDF:
        return "pdf", "file_type_detector"
    if kind == FileKind.IMAGE:
        return "image", "file_type_detector"
    if kind == FileKind.EXCEL:
        ext = Path(filename or "").suffix.lower()
        if ext == ".xls":
            return "xls", "file_type_detector"
        return "xlsx", "file_type_detector"
    return "", ""


def _document_type(
    file_kind: str,
    requested_product: Optional[str],
) -> tuple:
    product = (requested_product or "").strip().lower()
    if product in {"invoice", "sales_statement"}:
        allowed = _INVOICE_KINDS if product == "invoice" else _SALES_KINDS
        if file_kind in allowed:
            return product, [], 0.95
        return (
            "unsupported",
            [f"file_kind_not_supported_for_{product}"],
            0.3,
        )
    if file_kind in _SALES_ONLY_KINDS:
        return "sales_statement", ["inferred_sales_only_format"], 0.9
    if file_kind in _INVOICE_KINDS:
        return "unsupported", ["lane_not_specified"], 0.4
    return "unsupported", ["unsupported_file_kind"], 0.2


def _classify_pdf(
    file_bytes: bytes,
    file_path: Optional[str],
    *,
    collect_page_stats: bool = False,
) -> tuple:
    """Digital vs scanned using embedded text length, matching _parse_pdf."""
    empty: List[Dict[str, Any]] = []
    try:
        import fitz
    except ImportError:
        return "unknown", False, 0.3, ["pymupdf_unavailable"], empty

    try:
        if file_path and os.path.exists(file_path):
            doc = fitz.open(file_path)
        elif file_bytes:
            doc = fitz.open(stream=file_bytes, filetype="pdf")
        else:
            return "unknown", False, 0.3, ["pdf_bytes_missing"], empty
    except Exception:
        return "unknown", False, 0.4, ["pdf_open_failed"], empty

    page_stats: List[Dict[str, Any]] = []
    try:
        page_count = int(getattr(doc, "page_count", 0) or 0)
        if page_count <= 0:
            return "unknown", False, 0.4, ["pdf_has_no_pages"], empty
        limit = min(page_count, _pdf_page_limit())
        short_pages = 0
        for index in range(limit):
            page = doc[index]
            text = page.get_text("text") or ""
            compact = len(re.sub(r"\s+", "", text))
            if compact < _EMBEDDED_TEXT_MIN_CHARS:
                short_pages += 1
            if collect_page_stats:
                token_count, truncated = _count_pdf_words(page.get_text("words") or [])
                rect = page.rect
                page_stats.append(
                    {
                        "page_number": index + 1,
                        "text_length": len(text),
                        "width": float(rect.width),
                        "height": float(rect.height),
                        "extraction_method": (
                            "pymupdf"
                            if compact >= _EMBEDDED_TEXT_MIN_CHARS  
                            else "embedded_sparse"
                        ),
                        "token_count": token_count,
                        "tokens_truncated": truncated,
                    }
                )
    except Exception:
        return "unknown", False, 0.4, ["pdf_text_detection_failed"], empty
    finally:
        try:
            doc.close()
        except Exception:
            pass

    reasons: List[str] = []
    if page_count > limit:
        reasons.append("pdf_kind_from_first_pages")
    if short_pages == 0:
        return "digital", False, 0.95, reasons, page_stats
    if short_pages == limit:
        reasons.append("embedded_text_below_threshold")
        return "scanned", True, 0.9, reasons, page_stats
    reasons.append("mixed_embedded_text")
    if short_pages * 2 >= limit:
        return "scanned", True, 0.6, reasons, page_stats
    return "digital", False, 0.7, reasons, page_stats


def _count_pdf_words(words: List[Any]) -> tuple:
    """Count embedded words without keeping token objects."""
    count = 0
    truncated = False
    for word in words:
        if count >= 2000:
            truncated = True
            break
        if not word or len(word) < 5:
            continue
        if not str(word[4] or "").strip():
            continue
        count += 1
    return count, truncated


def _pdf_page_limit() -> int:
    raw = os.getenv("SALES_PDF_MAX_PAGES", "20")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return _PDF_CLASSIFY_MAX_PAGES
    return value if value > 0 else _PDF_CLASSIFY_MAX_PAGES
