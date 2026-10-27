"""Document/page loading helpers for PaddleOCR-VL benchmarks.

Reuses existing routing and PDF rendering conventions. Not used in production
OCR priority.
"""

from __future__ import annotations

import io
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from services.document_router import route_document

_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff", ".webp", ".heic"}
_PDF_RENDER_ZOOM = 2.0


def load_phase3_manifest(
    phase3_eval_dir: Path,
    *,
    prefer_selection: bool = True,
) -> List[Dict[str, Any]]:
    """Exact Phase 3 representative 100-document set.

    Prefer ``phase3_eval/selection.json`` (the authoritative select_batch
    output). Fall back to ``phase3_eval/records/*.json`` when needed.
    """
    import json

    phase3_eval_dir = Path(phase3_eval_dir)
    selection_path = phase3_eval_dir / "selection.json"
    if prefer_selection and selection_path.is_file():
        payload = json.loads(selection_path.read_text(encoding="utf-8"))
        files = payload.get("files") or []
        items: List[Dict[str, Any]] = []
        for entry in files:
            relative = (
                entry.get("relative_path")
                or entry.get("filename")
                or entry.get("document_id")
            )
            items.append(
                {
                    "document_id": str(relative),
                    "relative_path": str(relative),
                    "path": entry.get("path"),
                    "ocr_required": entry.get("requires_ocr"),
                    "pdf_kind": entry.get("pdf_kind"),
                    "file_type": entry.get("file_kind") or entry.get("extension"),
                    "extension": entry.get("extension"),
                    "page_count": entry.get("page_count"),
                    "source_manifest": str(selection_path),
                }
            )
        return items

    records_dir = phase3_eval_dir / "records"
    if not records_dir.is_dir():
        records_dir = phase3_eval_dir
    items = []
    for path in sorted(records_dir.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        doc_id = str(payload.get("document_id") or path.name.replace(".json", ""))
        items.append(
            {
                "document_id": doc_id,
                "relative_path": payload.get("relative_path") or doc_id,
                "ocr_required": payload.get("ocr_required")
                or payload.get("requires_ocr"),
                "pdf_kind": payload.get("pdf_kind"),
                "file_type": payload.get("file_type") or payload.get("file_kind"),
                "record_path": str(path),
                "source_manifest": "records",
            }
        )
    return items


def resolve_dataset_path(
    dataset: Path, document_id: str, *, explicit_path: Optional[str] = None
) -> Optional[Path]:
    if explicit_path:
        candidate = Path(explicit_path)
        if candidate.is_file():
            return candidate
    direct = dataset / document_id
    if direct.is_file():
        return direct
    stem = Path(document_id).name
    matches = list(dataset.glob(stem))
    if matches:
        return matches[0]
    if not dataset.is_dir():
        return None
    lower = stem.lower()
    for path in dataset.iterdir():
        if path.is_file() and path.name.lower() == lower:
            return path
    return None


def _orientation(image) -> Optional[int]:
    try:
        return image.getexif().get(274)
    except Exception:
        return None


def load_image_bytes(path: Path) -> Tuple[bytes, Dict[str, Any]]:
    from PIL import Image, ImageOps

    raw = path.read_bytes()
    with Image.open(io.BytesIO(raw)) as original:
        orientation = _orientation(original)
        original_size = original.size
        transposed = ImageOps.exif_transpose(original)
        processed = transposed.convert("RGB")
        processed_size = processed.size
        buf = io.BytesIO()
        processed.save(buf, format="PNG")
        return buf.getvalue(), {
            "original_width": original_size[0],
            "original_height": original_size[1],
            "processed_width": processed_size[0],
            "processed_height": processed_size[1],
            "orientation": orientation,
            "preprocessing": ["exif_transpose", "rgb"],
        }


def render_pdf_page(path: Path, page_index: int = 0, zoom: float = _PDF_RENDER_ZOOM) -> Tuple[bytes, Dict[str, Any]]:
    import fitz

    doc = fitz.open(path)
    try:
        page = doc[page_index]
        pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
        data = pix.tobytes("png")
        return data, {
            "original_width": float(page.rect.width),
            "original_height": float(page.rect.height),
            "processed_width": int(pix.width),
            "processed_height": int(pix.height),
            "orientation": None,
            "preprocessing": [f"pymupdf_render_zoom_{zoom}"],
            "page_index": page_index,
            "page_count": int(doc.page_count),
        }
    finally:
        doc.close()


def page_needs_ocr(path: Path, page_index: int = 0) -> Tuple[bool, Dict[str, Any]]:
    """Mirror document_router embedded-text rule for a single PDF page."""
    suffix = path.suffix.lower()
    if suffix in _IMAGE_EXTS:
        return True, {"native_text": False, "ocr_required": True, "reason": "image"}
    if suffix != ".pdf":
        return False, {
            "native_text": False,
            "ocr_required": False,
            "reason": "unsupported_for_ocr_benchmark",
            "file_kind": suffix.lstrip(".") or "unknown",
        }
    try:
        import fitz
    except ImportError:
        return True, {
            "native_text": False,
            "ocr_required": True,
            "reason": "pymupdf_unavailable",
        }
    try:
        if path.stat().st_size <= 0:
            return False, {
                "native_text": False,
                "ocr_required": False,
                "reason": "empty_file",
            }
        doc = fitz.open(path)
    except Exception as exc:
        return False, {
            "native_text": False,
            "ocr_required": False,
            "reason": "pdf_open_failed",
            "error": str(exc),
            "error_type": type(exc).__name__,
        }
    try:
        page = doc[page_index]
        text = page.get_text("text") or ""
        compact = len(re.sub(r"\s+", "", text))
        reliable = compact >= 40
        return (not reliable), {
            "native_text": reliable,
            "ocr_required": not reliable,
            "embedded_text_chars": compact,
            "reason": "native_text" if reliable else "embedded_text_below_threshold",
            "page_count": int(doc.page_count),
        }
    finally:
        doc.close()


def iter_document_pages(path: Path, *, max_pages: Optional[int] = None) -> List[Dict[str, Any]]:
    """Return page descriptors for OCR benchmark (images = 1 page)."""
    suffix = path.suffix.lower()
    if suffix in _IMAGE_EXTS:
        return [{"page": 1, "page_index": 0, "kind": "image"}]
    if suffix != ".pdf":
        route = route_document(path.read_bytes(), path.name, file_path=str(path))
        return [
            {
                "page": 1,
                "page_index": 0,
                "kind": route.file_kind,
                "route": route.to_dict(),
                "skip_reason": "not_visual_document",
            }
        ]
    import fitz

    try:
        if path.stat().st_size <= 0:
            return [
                {
                    "page": 1,
                    "page_index": 0,
                    "kind": "pdf",
                    "skip_reason": "empty_file",
                }
            ]
        doc = fitz.open(path)
        try:
            count = int(doc.page_count)
        finally:
            doc.close()
    except Exception as exc:
        return [
            {
                "page": 1,
                "page_index": 0,
                "kind": "pdf",
                "skip_reason": f"pdf_open_failed:{type(exc).__name__}",
                "error": str(exc),
            }
        ]
    if max_pages is not None:
        count = min(count, max_pages)
    return [
        {"page": index + 1, "page_index": index, "kind": "pdf"}
        for index in range(count)
    ]


def load_page_image(
    path: Path, page_index: int = 0
) -> Tuple[Optional[bytes], Dict[str, Any]]:
    suffix = path.suffix.lower()
    if suffix in _IMAGE_EXTS:
        data, meta = load_image_bytes(path)
        return data, meta
    if suffix == ".pdf":
        data, meta = render_pdf_page(path, page_index=page_index)
        return data, meta
    return None, {"error": "unsupported_file_kind", "suffix": suffix}
