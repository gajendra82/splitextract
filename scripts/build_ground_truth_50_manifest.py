"""Build exact first-50 ground-truth manifest matching the PaddleOCR-VL run."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SELECTION = ROOT / "phase3_eval" / "selection.json"
VL_SUMMARY = ROOT / "phase4" / "paddle_vl_100.json"
VL_PER = ROOT / "phase4" / "paddle_vl_benchmark" / "per_document"
OUT = ROOT / "phase4" / "ground_truth_50" / "manifest.json"


def _safe(name: str) -> str:
    return name.replace("/", "_").replace("\\", "_").replace(" ", "_")


def main() -> int:
    selection = json.loads(SELECTION.read_text(encoding="utf-8"))
    files = (selection.get("files") or [])[:50]
    if len(files) != 50:
        raise SystemExit(f"expected 50 selection entries, got {len(files)}")

    vl_summary = {}
    if VL_SUMMARY.is_file():
        vl_summary = json.loads(VL_SUMMARY.read_text(encoding="utf-8"))
    if int(vl_summary.get("documents") or 0) not in {0, 50}:
        print(
            f"warning: paddle_vl_100.json documents={vl_summary.get('documents')}"
        )

    documents = []
    for entry in files:
        doc_id = entry.get("relative_path") or entry.get("filename")
        safe = _safe(str(doc_id))
        per_path = VL_PER / f"{safe}.json"
        pages_meta = []
        ocr_required_pages = []
        if per_path.is_file():
            payload = json.loads(per_path.read_text(encoding="utf-8"))
            for page in payload.get("pages") or []:
                page_no = int(page.get("page") or 1)
                pages_meta.append(
                    {
                        "page": page_no,
                        "status": page.get("status"),
                        "paddle_vl_status": page.get("paddle_vl_status")
                        or page.get("status"),
                    }
                )
                if page.get("status") not in {"skipped_native_text"} and page.get(
                    "ocr_required", True
                ):
                    if page.get("status") == "success" or page.get("ocr_required"):
                        ocr_required_pages.append(page_no)
                if page.get("status") == "success":
                    if page_no not in ocr_required_pages:
                        ocr_required_pages.append(page_no)
        documents.append(
            {
                "document_id": doc_id,
                "source_file": entry.get("path"),
                "file_kind": entry.get("file_kind"),
                "pdf_kind": entry.get("pdf_kind"),
                "manifest_requires_ocr": entry.get("requires_ocr"),
                "pages": pages_meta,
                "ocr_required_pages": ocr_required_pages,
                "paddle_vl_checkpoint": str(per_path) if per_path.is_file() else None,
            }
        )

    # Exact ID match vs paddle_vl_benchmark/manifest.json when present.
    bench_manifest = ROOT / "phase4" / "paddle_vl_benchmark" / "manifest.json"
    if bench_manifest.is_file():
        bench = json.loads(bench_manifest.read_text(encoding="utf-8"))
        bench_ids = bench.get("document_ids") or []
        these_ids = [d["document_id"] for d in documents]
        if bench_ids[:50] != these_ids:
            raise SystemExit(
                "first-50 document IDs do not match paddle_vl_benchmark/manifest.json"
            )

    payload = {
        "source_manifest": str(SELECTION),
        "paddle_vl_summary": str(VL_SUMMARY),
        "document_count": 50,
        "documents": documents,
        "document_ids": [d["document_id"] for d in documents],
        "note": (
            "Exact first 50 entries from phase3_eval/selection.json, "
            "matching the completed PaddleOCR-VL 50-doc benchmark."
        ),
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(OUT)
    print("documents", 50)
    print("ocr_docs", sum(1 for d in documents if d["ocr_required_pages"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
