"""Run PaddleOCR-VL 1.6 on 5 representative Phase 3 documents (OCR only).

Does not run parser / Gemini / Phase 2.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.ocr.paddle_vl_engine import PaddleVLEngine, evidence_from_result
from services.ocr.paddle_vl_io import (
    iter_document_pages,
    load_page_image,
    load_phase3_manifest,
    page_needs_ocr,
    resolve_dataset_path,
)

DEFAULT_DATASET = Path(r"C:\Users\amnsa\Downloads\ZL_2026_August")
KNOWN_DIFFICULT = {
    # product-wise photo with glued issue subcolumns (3337 / 3752 patterns)
    "0000700259_2026_08_ZL_18_379_03092026174312.jpg",
}


def _pick_five(manifest: List[Dict[str, Any]], dataset: Path) -> List[Dict[str, Any]]:
    """Prefer pharma photo, scanned PDF, table-ish image, numeric-hard, invoice-like."""
    by_id = {item["document_id"]: item for item in manifest}
    chosen: List[Dict[str, Any]] = []
    seen = set()

    def add(doc_id: str) -> None:
        if doc_id in seen:
            return
        item = by_id.get(doc_id)
        if not item:
            return
        path = resolve_dataset_path(
            dataset, item["document_id"], explicit_path=item.get("path")
        )
        if path is None or not path.is_file():
            return
        chosen.append({**item, "resolved_path": str(path)})
        seen.add(doc_id)

    for doc_id in KNOWN_DIFFICULT:
        add(doc_id)

    buckets = {
        "image": [],
        "scanned_pdf": [],
        "digital_skip": [],
        "other_image": [],
    }
    for item in manifest:
        path = resolve_dataset_path(
            dataset, item["document_id"], explicit_path=item.get("path")
        )
        if path is None:
            continue
        ext = path.suffix.lower()
        entry = {**item, "resolved_path": str(path)}
        if ext in {".jpg", ".jpeg", ".png", ".webp"}:
            buckets["image"].append(entry)
        elif ext == ".pdf" and (
            item.get("ocr_required") or item.get("pdf_kind") == "scanned"
        ):
            buckets["scanned_pdf"].append(entry)
        elif ext == ".pdf":
            buckets["digital_skip"].append(entry)

    for key in ("image", "scanned_pdf", "image", "image"):
        for entry in buckets[key]:
            if entry["document_id"] not in seen:
                chosen.append(entry)
                seen.add(entry["document_id"])
                break
        if len(chosen) >= 5:
            break

    # Fill remaining from any OCR-required visual docs.
    if len(chosen) < 5:
        for entry in buckets["image"] + buckets["scanned_pdf"]:
            if entry["document_id"] not in seen:
                chosen.append(entry)
                seen.add(entry["document_id"])
            if len(chosen) >= 5:
                break
    return chosen[:5]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument(
        "--phase3", type=Path, default=ROOT / "phase3_eval"
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "phase4" / "paddle_vl_benchmark" / "five_doc_smoke.json",
    )
    parser.add_argument("--max-pages", type=int, default=1)
    args = parser.parse_args(argv)

    manifest = load_phase3_manifest(args.phase3)
    docs = _pick_five(manifest, args.dataset)
    if len(docs) < 5:
        print(f"Could only resolve {len(docs)} documents; need 5.")
        return 2

    engine = PaddleVLEngine()
    if not engine.available():
        print("PADDLEOCR-VL unavailable:", engine.model_info())
        return 2
    init_started = time.perf_counter()
    info = engine.model_info()
    if info.get("backend") != "subprocess":
        engine._client()
    init_ms = (time.perf_counter() - init_started) * 1000.0

    rows: List[Dict[str, Any]] = []
    print("=" * 50)
    print("PADDLEOCR-VL 1.6 FIVE-DOCUMENT TEST")
    print("=" * 50)
    print(f"Init latency ms: {init_ms:.1f}")

    for doc in docs:
        path = Path(doc["resolved_path"])
        pages = iter_document_pages(path, max_pages=args.max_pages)
        for page_info in pages:
            page = int(page_info["page"])
            page_index = int(page_info["page_index"])
            needs, native_meta = page_needs_ocr(path, page_index=page_index)
            record: Dict[str, Any] = {
                "document": doc["document_id"],
                "page": page,
                "path": str(path),
                "native_meta": native_meta,
            }
            if not needs:
                record.update(
                    {
                        "status": "skipped_native_text",
                        "paddle_vl_status": "skipped_native_text",
                        "text_length": 0,
                        "word_count": 0,
                        "numeric_token_count": 0,
                        "latency_ms": 0,
                        "bbox_count": 0,
                        "errors": None,
                    }
                )
                rows.append(record)
                print(
                    f"[skip native] {doc['document_id']} p{page} "
                    f"chars={native_meta.get('embedded_text_chars')}"
                )
                continue
            image_bytes, image_meta = load_page_image(path, page_index=page_index)
            if not image_bytes:
                record.update(
                    {
                        "status": "error",
                        "errors": image_meta.get("error"),
                        "image_meta": image_meta,
                    }
                )
                rows.append(record)
                continue
            result = engine.recognize(
                image_bytes,
                page=page,
                metadata={"source_path": str(path), **image_meta},
            )
            evidence = evidence_from_result(result)
            numeric = evidence.get("numeric_tokens") or []
            record.update(
                {
                    "status": evidence.get("status"),
                    "paddle_vl_status": evidence.get("status"),
                    "image_width": evidence.get("image_width"),
                    "image_height": evidence.get("image_height"),
                    "image_meta": image_meta,
                    "text_length": len(evidence.get("text") or ""),
                    "word_count": evidence.get("word_count"),
                    "line_count": evidence.get("line_count"),
                    "numeric_token_count": len(numeric),
                    "numeric_tokens_sample": numeric[:40],
                    "latency_ms": evidence.get("latency_ms"),
                    "mean_confidence": (evidence.get("confidence") or {}).get("mean"),
                    "bbox_count": len(evidence.get("boxes") or []),
                    "bbox_stats": (result.metadata or {}).get("bbox_stats"),
                    "layout_available": bool(
                        ((evidence.get("layout") or {}).get("available"))
                    ),
                    "tables_available": bool(
                        ((evidence.get("tables") or {}).get("available"))
                    ),
                    "errors": evidence.get("error"),
                    "text_preview": (evidence.get("text") or "")[:400],
                }
            )
            rows.append(record)
            print(
                f"[{record['status']}] {doc['document_id']} p{page} "
                f"text={record['text_length']} words={record['word_count']} "
                f"nums={record['numeric_token_count']} "
                f"latency_ms={record['latency_ms']} "
                f"boxes={record['bbox_count']}"
            )

    args.output.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "engine": "paddleocr-vl-1.6",
        "init_latency_ms": init_ms,
        "documents": [d["document_id"] for d in docs],
        "results": rows,
    }
    args.output.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    print(f"Wrote {args.output}")

    ocr_rows = [r for r in rows if r.get("status") not in {"skipped_native_text"}]
    successes = [r for r in ocr_rows if r.get("status") == "success" and r.get("text_length", 0) > 0]
    if len(docs) >= 5 and successes:
        print("PADDLEOCR-VL 1.6 FIVE-DOCUMENT TEST: PASS")
        return 0
    print("PADDLEOCR-VL 1.6 FIVE-DOCUMENT TEST: BLOCKED")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
