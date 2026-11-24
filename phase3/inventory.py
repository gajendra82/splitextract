"""Read-only inventory of a real document folder.

Uses the existing document router. Does not extract line items and does not
write inside the dataset directory.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff", ".webp", ".heic"}
EXCEL_SUFFIXES = {".xls", ".xlsx", ".xlsm"}
PDF_SUFFIXES = {".pdf"}


def _page_count(path: Path) -> Optional[int]:
    try:
        import fitz
    except ImportError:
        return None
    try:
        doc = fitz.open(str(path))
    except Exception:
        return None
    try:
        return int(getattr(doc, "page_count", 0) or 0)
    finally:
        try:
            doc.close()
        except Exception:
            pass


def _route(path: Path) -> Dict[str, Any]:
    from services.document_router import route_document

    route = route_document(
        b"",
        path.name,
        requested_product="sales_statement",
        file_path=str(path),
        collect_page_stats=False,
    )
    return route.to_dict()


def load_prior_status(progress_files: Iterable[Path]) -> Dict[str, str]:
    found: Dict[str, str] = {}
    for progress_path in progress_files:
        if not progress_path.is_file():
            continue
        try:
            payload = json.loads(progress_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        for key, value in payload.items():
            status = ""
            if isinstance(value, dict):
                status = str(value.get("status") or "")
            name = Path(str(key)).name
            if name and status:
                found[name] = status
                found[Path(name).stem] = status
    return found


def _duplicate_groups(records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    by_size: Dict[int, List[Dict[str, Any]]] = defaultdict(list)
    for record in records:
        size = record.get("size_bytes")
        if isinstance(size, int):
            by_size[size].append(record)
    groups = []
    for size, rows in by_size.items():
        if len(rows) < 2:
            continue
        hashes: Dict[str, List[str]] = defaultdict(list)
        for row in rows:
            path = Path(row["path"])
            digest = hashlib.sha256()
            try:
                with path.open("rb") as handle:
                    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                        digest.update(chunk)
            except OSError:
                continue
            hashes[digest.hexdigest()].append(row["relative_path"])
        for digest, names in hashes.items():
            if len(names) > 1:
                groups.append(
                    {
                        "sha256": digest,
                        "size_bytes": size,
                        "count": len(names),
                        "files": names,
                    }
                )
    return groups


def inventory_dataset(
    dataset_dir: Path,
    *,
    prior_status: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    dataset_dir = Path(dataset_dir)
    prior_status = prior_status or {}
    records: List[Dict[str, Any]] = []
    extensions: Counter = Counter()
    for path in sorted(dataset_dir.rglob("*"), key=lambda item: str(item).lower()):
        if not path.is_file():
            continue
        suffix = path.suffix.lower()
        extensions[suffix or "(none)"] += 1
        route = _route(path)
        page_count = _page_count(path) if suffix == ".pdf" else None
        rel = str(path.relative_to(dataset_dir))
        records.append(
            {
                "relative_path": rel,
                "path": str(path),
                "filename": path.name,
                "extension": suffix or "(none)",
                "size_bytes": path.stat().st_size,
                "file_kind": route.get("file_kind"),
                "pdf_kind": route.get("pdf_kind"),
                "requires_ocr": bool(route.get("requires_ocr")),
                "document_type": route.get("document_type"),
                "route_confidence": route.get("confidence"),
                "route_reasons": route.get("reasons") or [],
                "page_count": page_count,
                "prior_qa_status": prior_status.get(path.name) or prior_status.get(path.stem),
            }
        )

    pdfs = [row for row in records if row["extension"] == ".pdf"]
    images = [row for row in records if row["extension"] in IMAGE_SUFFIXES]
    excels = [row for row in records if row["extension"] in EXCEL_SUFFIXES]
    page_counts = [row["page_count"] for row in pdfs if isinstance(row["page_count"], int)]
    pdf_kinds = Counter(row.get("pdf_kind") or "unknown" for row in pdfs)
    return {
        "dataset_path": str(dataset_dir),
        "total_files": len(records),
        "extensions": dict(extensions),
        "pdf_count": len(pdfs),
        "image_count": len(images),
        "excel_count": len(excels),
        "other_count": len(records) - len(pdfs) - len(images) - len(excels),
        "pdf_kinds": dict(pdf_kinds),
        "ocr_required": sum(1 for row in records if row["requires_ocr"]),
        "digital_text_candidates": pdf_kinds.get("digital", 0),
        "scanned_candidates": pdf_kinds.get("scanned", 0),
        "page_count_min": min(page_counts) if page_counts else None,
        "page_count_max": max(page_counts) if page_counts else None,
        "page_count_mean": round(sum(page_counts) / len(page_counts), 2) if page_counts else None,
        "previously_processed": sum(1 for row in records if row.get("prior_qa_status")),
        "prior_status_counts": dict(Counter(
            row["prior_qa_status"] for row in records if row.get("prior_qa_status")
        )),
        "duplicate_groups": _duplicate_groups(records),
        "files": records,
    }
