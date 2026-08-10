"""Detect uploaded file kinds for the split-and-extract pipeline.

Keeps PDF / image / Excel routing in one place without changing the API contract.
"""

from __future__ import annotations

import os
from enum import Enum
from typing import Optional, Tuple


class FileKind(str, Enum):
    PDF = "pdf"
    IMAGE = "image"
    EXCEL = "excel"
    UNKNOWN = "unknown"


_PDF_EXTENSIONS = {".pdf"}
_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".tiff", ".tif", ".bmp"}
_EXCEL_EXTENSIONS = {".xlsx", ".xls"}

_PDF_MIME = {
    "application/pdf",
}
_IMAGE_MIME = {
    "image/png",
    "image/jpeg",
    "image/jpg",
    "image/tiff",
    "image/bmp",
    "image/x-ms-bmp",
}
_EXCEL_MIME = {
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.ms-excel",
    "application/excel",
    "application/x-excel",
    "application/x-msexcel",
}


class FileTypeDetector:
    """Detect file kind from extension, MIME / content-type, and optional magic bytes."""

    def detect(
        self,
        filename: Optional[str] = None,
        content_type: Optional[str] = None,
        file_bytes: Optional[bytes] = None,
    ) -> Tuple[FileKind, str]:
        """
        Returns (FileKind, normalized_extension including leading dot).
        Extension is best-effort; may be empty for UNKNOWN.
        """
        ext = self._extension_from_filename(filename)
        mime = (content_type or "").split(";")[0].strip().lower()

        kind_from_ext = self._kind_from_extension(ext)
        kind_from_mime = self._kind_from_mime(mime)
        kind_from_magic = self._kind_from_magic(file_bytes)

        # Prefer extension when it is a known invoice input type (Laravel filenames are reliable).
        if kind_from_ext != FileKind.UNKNOWN:
            return kind_from_ext, ext or self._default_ext(kind_from_ext)

        if kind_from_mime != FileKind.UNKNOWN:
            return kind_from_mime, ext or self._default_ext(kind_from_mime)

        if kind_from_magic != FileKind.UNKNOWN:
            return kind_from_magic, ext or self._default_ext(kind_from_magic)

        return FileKind.UNKNOWN, ext

    def is_excel(
        self,
        filename: Optional[str] = None,
        content_type: Optional[str] = None,
        file_bytes: Optional[bytes] = None,
    ) -> bool:
        kind, _ = self.detect(filename, content_type, file_bytes)
        return kind == FileKind.EXCEL

    @staticmethod
    def _extension_from_filename(filename: Optional[str]) -> str:
        if not filename:
            return ""
        return os.path.splitext(filename.strip().lower())[1]

    @staticmethod
    def _kind_from_extension(ext: str) -> FileKind:
        if ext in _PDF_EXTENSIONS:
            return FileKind.PDF
        if ext in _IMAGE_EXTENSIONS:
            return FileKind.IMAGE
        if ext in _EXCEL_EXTENSIONS:
            return FileKind.EXCEL
        return FileKind.UNKNOWN

    @staticmethod
    def _kind_from_mime(mime: str) -> FileKind:
        if not mime:
            return FileKind.UNKNOWN
        if mime in _PDF_MIME:
            return FileKind.PDF
        if mime in _IMAGE_MIME or mime.startswith("image/"):
            return FileKind.IMAGE
        if mime in _EXCEL_MIME:
            return FileKind.EXCEL
        return FileKind.UNKNOWN

    @staticmethod
    def _kind_from_magic(file_bytes: Optional[bytes]) -> FileKind:
        if not file_bytes or len(file_bytes) < 8:
            return FileKind.UNKNOWN
        head = file_bytes[:8]
        if head.startswith(b"%PDF"):
            return FileKind.PDF
        # ZIP-based OOXML (.xlsx)
        if head.startswith(b"PK\x03\x04") or head.startswith(b"PK\x05\x06"):
            # Heuristic only — could be docx/pptx; callers should prefer extension.
            return FileKind.EXCEL
        # OLE compound document (.xls)
        if head.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
            return FileKind.EXCEL
        # Common image signatures
        if head.startswith(b"\x89PNG") or head.startswith(b"\xff\xd8\xff"):
            return FileKind.IMAGE
        if head[:2] == b"BM" or head.startswith(b"II*\x00") or head.startswith(b"MM\x00*"):
            return FileKind.IMAGE
        return FileKind.UNKNOWN

    @staticmethod
    def _default_ext(kind: FileKind) -> str:
        return {
            FileKind.PDF: ".pdf",
            FileKind.IMAGE: ".png",
            FileKind.EXCEL: ".xlsx",
        }.get(kind, "")
