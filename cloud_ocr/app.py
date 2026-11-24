"""Authenticated PaddleOCR-VL image worker for an isolated GPU host."""

from __future__ import annotations

import hmac
import os
import time
from html.parser import HTMLParser
from io import BytesIO

from fastapi import FastAPI, File, Header, HTTPException, UploadFile
from PIL import Image

os.environ.setdefault("PADDLE_VL_ALLOW_INPROCESS", "1")
os.environ.setdefault("PADDLE_VL_WORKER_CHILD", "1")

from services.ocr.paddle_vl_engine import PaddleVLEngine, evidence_from_result

MAX_IMAGE_BYTES = int(os.getenv("PADDLE_VL_API_MAX_BYTES", str(25 * 1024 * 1024)))
_TOKEN = os.getenv("PADDLE_VL_API_KEY", "")
_ENGINE = PaddleVLEngine(device=os.getenv("PADDLE_VL_DEVICE", "gpu:0"))
app = FastAPI(title="PaddleOCR-VL 1.6 OCR API", version="1.0.0")


class _TableParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tables = []
        self._inside = False
        self._row = None
        self._cell = None

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self._inside = True
            self.tables.append([])
        elif self._inside and tag == "tr":
            self._row = []
        elif self._inside and tag in ("td", "th"):
            self._cell = []
            self._is_header = tag == "th"

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._cell is not None:
            self._row.append(" ".join("".join(self._cell).split()))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if self._row:
                self.tables[-1].append(self._row)
            self._row = None
        elif tag == "table":
            self._inside = False


def _structured_tables(text, layout_tables):
    parsed = _TableParser()
    parsed.feed(text or "")
    out = []
    for index, table in enumerate(parsed.tables):
        headers = table[0] if table else []
        rows = table[1:] if table else []
        out.append({"table_index": index, "headers": headers, "rows": rows, "cells": table})
    if out:
        return out
    markdown_rows = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line.startswith("|") or not line.endswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if cells and all(not cell or set(cell) <= {"-", ":"} for cell in cells):
            continue
        markdown_rows.append(cells)
    if markdown_rows:
        return [{
            "table_index": 0,
            "headers": markdown_rows[0],
            "rows": markdown_rows[1:],
            "cells": markdown_rows,
        }]
    return layout_tables if isinstance(layout_tables, list) else []


@app.get("/health")
def health():
    if not _TOKEN:
        raise HTTPException(status_code=503, detail="worker authentication is not configured")
    return {"status": "ok", "model": "PaddleOCR-VL-1.6", "gpu": os.getenv("PADDLE_VL_DEVICE", "gpu:0")}


@app.get("/live")
def live():
    return {"status": "alive"}


@app.post("/v1/ocr")
async def ocr(file: UploadFile = File(...), authorization: str = Header(default=""), x_ocr_page: int = Header(default=1)):
    if not _TOKEN:
        raise HTTPException(status_code=503, detail="worker authentication is not configured")
    supplied = authorization.removeprefix("Bearer ").strip()
    if not hmac.compare_digest(supplied, _TOKEN):
        raise HTTPException(status_code=401, detail="unauthorized")
    if x_ocr_page < 1:
        raise HTTPException(status_code=400, detail="page must be >= 1")
    data = await file.read(MAX_IMAGE_BYTES + 1)
    if len(data) > MAX_IMAGE_BYTES:
        raise HTTPException(status_code=413, detail="image too large")
    try:
        with Image.open(BytesIO(data)) as im:
            if im.format not in {"JPEG", "PNG"}:
                raise HTTPException(status_code=415, detail="only JPG, JPEG, and PNG are supported")
            im.verify()
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=400, detail="invalid image") from exc
    started = time.perf_counter()
    result = _ENGINE.recognize(data, page=x_ocr_page)
    evidence = evidence_from_result(result)
    evidence["page"] = x_ocr_page
    evidence["page_info"] = {"page": x_ocr_page, "width": result.metadata.get("image_width"), "height": result.metadata.get("image_height")}
    evidence["tables"] = _structured_tables(result.text, result.metadata.get("tables"))
    evidence["headers"] = evidence["tables"][0].get("headers", []) if evidence["tables"] else []
    evidence["rows"] = evidence["tables"][0].get("rows", []) if evidence["tables"] else []
    evidence["latency_ms"] = (time.perf_counter() - started) * 1000.0
    if not evidence.get("text", "").strip():
        raise HTTPException(status_code=502, detail={"error": evidence.get("error") or "empty_ocr_text", "evidence": evidence})
    return evidence
