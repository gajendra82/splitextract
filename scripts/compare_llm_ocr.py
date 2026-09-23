"""
Evaluation only. Does not change production extraction routing.

For one PDF, run:
  1. Tesseract OCR + Gemini
  2. PaddleOCR + Gemini
  3. Tesseract OCR + local Qwen  (POST /v1/invoice/batch)
  4. PaddleOCR + local Qwen

Local request body matches the Qwen service:

  POST {base}/v1/invoice/batch
  {"items":[{"id":"...","ocr_text":"..."}]}

Usage:
  python scripts/compare_llm_ocr.py path\\to\\invoice.pdf
  python scripts/compare_llm_ocr.py path\\to\\invoice.pdf --local-url http://HOST:8000
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any, Dict, List, Optional, Tuple


CANONICAL_FIELDS = [
    ("invoice_number", ("invoice_no", "invoice_number", "bill_no", "bill_number")),
    ("invoice_date", ("invoice_date", "date")),
    ("vendor_gstin", ("vendor_gstin", "supplier_gstin", "seller_gstin")),
    ("customer_gstin", ("customer_gstin", "buyer_gstin")),
    ("vendor", ("vendor", "supplier", "seller", "seller_name")),
    ("customer", ("customer", "buyer", "buyer_name")),
    ("subtotal", ("subtotal", "sub_total", "taxable_value", "taxable_amount", "gross_amount")),
    ("cgst", ("cgst", "cgst_amount", "cgst_amt")),
    ("sgst", ("sgst", "sgst_amount", "sgst_amt")),
    ("igst", ("igst", "igst_amount", "igst_amt")),
    ("total_amount", ("total", "total_amount", "grand_total", "net_amount", "invoice_total")),
    ("tax", ("tax", "total_tax", "gst_amount")),
    ("irn", ("irn",)),
]


def _project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _blank(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str) and value.strip().lower() in {"", "none", "null", "n/a", "na", "-"}:
        return True
    if value == [] or value == {}:
        return True
    return False


def _canon(value: Any) -> Any:
    if _blank(value):
        return None
    if isinstance(value, str):
        return value.strip()
    return value


def _same(a: Any, b: Any) -> bool:
    return json.dumps(_canon(a), sort_keys=True, default=str) == json.dumps(
        _canon(b), sort_keys=True, default=str
    )


def _as_dict(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _summary_block(payload: Any) -> Dict[str, Any]:
    obj = _as_dict(payload)
    data = obj.get("data") if isinstance(obj.get("data"), dict) else obj
    summary = data.get("invoice_summary")
    if isinstance(summary, dict):
        merged = dict(summary)
        for key, val in data.items():
            if key not in merged and key not in {"line_items", "invoice_summary", "ocr_text"}:
                merged[key] = val
        return merged
    return {k: v for k, v in data.items() if k not in {"line_items", "ocr_text"}}


def _line_items(payload: Any) -> List[Dict[str, Any]]:
    obj = _as_dict(payload)
    data = obj.get("data") if isinstance(obj.get("data"), dict) else obj
    raw = data.get("line_items")
    if isinstance(raw, dict):
        raw = raw.get("items")
    if not isinstance(raw, list):
        return []
    return [row for row in raw if isinstance(row, dict)]


def _first_present(sources: List[Dict[str, Any]], names: Tuple[str, ...]) -> Any:
    for source in sources:
        for name in names:
            if name in source and not _blank(source.get(name)):
                return source.get(name)
    for source in sources:
        for name in names:
            if name in source:
                return source.get(name)
    return None


def _item_field(item: Dict[str, Any], names: Tuple[str, ...]) -> Any:
    extra = item.get("additional_fields") if isinstance(item.get("additional_fields"), dict) else {}
    return _first_present([item, extra], names)


def _project_fields(payload: Any) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        return {name: None for name, _aliases in CANONICAL_FIELDS} | {
            "line_items": None,
            "quantities": None,
            "rates": None,
            "line_taxes": None,
        }

    summary = _summary_block(payload)
    items = _line_items(payload)
    projected: Dict[str, Any] = {}
    for name, aliases in CANONICAL_FIELDS:
        projected[name] = _first_present([summary, payload], aliases)

    projected["line_items"] = [
        {
            "product_description": _item_field(
                item, ("product_description", "product_name", "description", "item_name")
            ),
            "quantity": _item_field(item, ("quantity", "qty")),
            "unit_price": _item_field(item, ("unit_price", "rate", "price")),
            "total_amount": _item_field(item, ("total_amount", "amount", "line_total")),
            "hsn_code": _item_field(item, ("hsn_code", "hsn")),
            "lot_batch_number": _item_field(item, ("lot_batch_number", "batch", "batch_no")),
            "tax_amount": _item_field(item, ("tax_amount", "gst_amount", "tax")),
        }
        for item in items
    ] or None
    projected["quantities"] = [
        row.get("quantity") for row in (projected["line_items"] or [])
    ] or None
    projected["rates"] = [
        row.get("unit_price") for row in (projected["line_items"] or [])
    ] or None
    projected["line_taxes"] = [
        row.get("tax_amount") for row in (projected["line_items"] or [])
    ] or None

    known = {alias for _name, aliases in CANONICAL_FIELDS for alias in aliases}
    known.update({"line_items", "invoice_summary", "ocr_text", "data", "additional_fields"})
    for key, val in summary.items():
        if key not in known and not isinstance(val, (dict, list)):
            projected[key] = val
    return projected


def _ocr_page(pdf_path: Path, engine: str) -> Dict[str, Any]:
    import fitz

    import app as app_mod
    from services.ocr_quality import analyze_ocr_quality

    doc = fitz.open(pdf_path)
    try:
        if doc.page_count < 1:
            raise RuntimeError("PDF has no pages")
        page = doc.load_page(0)
        started = time.perf_counter()
        if engine == "tesseract":
            text, confidence = app_mod.extract_text_with_tesseract(page, page_num=0)
            method = "tesseract"
        elif engine == "paddleocr":
            os.environ["PADDLE_OCR_ENABLED"] = "true"
            from services.paddle_ocr import extract_text_with_paddleocr

            text, confidence = extract_text_with_paddleocr(page, page_num=0)
            method = "paddleocr"
        else:
            raise ValueError(engine)
        elapsed = time.perf_counter() - started
    finally:
        doc.close()

    text = text or ""
    quality = analyze_ocr_quality(text, float(confidence or 0.0))
    return {
        "ocr_method": method,
        "ocr_text": text,
        "ocr_character_count": len(text),
        "ocr_confidence": None if confidence is None else round(float(confidence), 2),
        "ocr_processing_time_seconds": round(elapsed, 3),
        "ocr_quality": quality,
    }


def _call_gemini(ocr_text: str) -> Tuple[Optional[dict], Optional[float], List[str]]:
    import app as app_mod

    errors: List[str] = []
    records: List[str] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record.getMessage())

    handler = _Capture()
    logger = logging.getLogger()
    logger.addHandler(handler)
    started = time.perf_counter()
    try:
        parsed = app_mod.extract_full_data_from_text_gemini(
            ocr_text, app_mod.create_ocr_stats(), Lock()
        )
    except Exception as exc:
        parsed = None
        errors.append(f"{type(exc).__name__}: {exc}")
    finally:
        logger.removeHandler(handler)
    elapsed = round(time.perf_counter() - started, 3)

    if parsed is None:
        creds = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "")
        if creds and not Path(creds).is_file():
            errors.append(
                "Gemini credentials file is missing: "
                f"{creds}"
            )
        for message in records:
            if "Gemini" in message or "credential" in message.lower() or "Error" in message:
                errors.append(message)
        if not errors:
            errors.append("Gemini text extraction returned null.")
    return parsed if isinstance(parsed, dict) else None, elapsed, errors


def _call_local_batch(
    base_url: str,
    item_id: str,
    ocr_text: str,
    timeout: float,
) -> Tuple[Optional[dict], Optional[float], List[str], Optional[dict]]:
    url = base_url.rstrip("/") + "/v1/invoice/batch"
    body = json.dumps({"items": [{"id": item_id, "ocr_text": ocr_text}]}).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
            status = response.status
    except urllib.error.HTTPError as exc:
        elapsed = round(time.perf_counter() - started, 3)
        detail = exc.read().decode("utf-8", errors="replace")
        return None, elapsed, [f"HTTP {exc.code} from {url}: {detail[:2000]}"], None
    except Exception as exc:
        elapsed = round(time.perf_counter() - started, 3)
        return None, elapsed, [f"{type(exc).__name__} calling {url}: {exc}"], None

    elapsed = round(time.perf_counter() - started, 3)
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        return None, elapsed, [f"Local LLM response was not JSON (HTTP {status}): {exc}"], None

    results = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(results, list) or not results:
        return None, elapsed, ["Local LLM batch response had no results."], payload

    row = results[0]
    if not isinstance(row, dict) or not row.get("success"):
        err = row.get("error") if isinstance(row, dict) else "unknown"
        return None, elapsed, [f"Local LLM item failed: {err}"], payload

    result = row.get("result")
    infer_s = row.get("inference_time")
    llm_time = float(infer_s) if isinstance(infer_s, (int, float)) else elapsed
    if not isinstance(result, dict):
        return None, llm_time, ["Local LLM result was not an object."], payload
    return result, round(llm_time, 3), [], payload


def _result_record(
    ocr: Dict[str, Any],
    provider: str,
    final_json: Optional[dict],
    llm_time: Optional[float],
    errors: List[str],
    raw_response: Optional[dict] = None,
) -> Dict[str, Any]:
    return {
        "ocr_method": ocr["ocr_method"],
        "llm_provider": provider,
        "ocr_text": ocr["ocr_text"],
        "ocr_character_count": ocr["ocr_character_count"],
        "ocr_confidence": ocr["ocr_confidence"],
        "ocr_processing_time_seconds": ocr["ocr_processing_time_seconds"],
        "ocr_quality": ocr["ocr_quality"],
        "llm_processing_time_seconds": llm_time,
        "llm_success": final_json is not None and not errors,
        "json_valid": isinstance(final_json, dict),
        "final_json": final_json,
        "raw_llm_response": raw_response,
        "errors": errors,
    }


def _missing(projected: Dict[str, Any]) -> List[str]:
    names = [name for name, _aliases in CANONICAL_FIELDS]
    names.extend(["line_items", "quantities", "rates"])
    return [name for name in names if _blank(projected.get(name))]


def _build_comparison(runs: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    order = [
        "tesseract_gemini",
        "paddleocr_gemini",
        "tesseract_local_llm",
        "paddleocr_local_llm",
    ]
    projected = {key: _project_fields((runs[key].get("final_json"))) for key in order}
    field_names: List[str] = []
    for name, _aliases in CANONICAL_FIELDS:
        field_names.append(name)
    field_names.extend(["line_items", "quantities", "rates", "line_taxes"])
    extras = []
    for key in order:
        for name in projected[key]:
            if name not in field_names and name not in extras:
                extras.append(name)
    field_names.extend(extras)

    rows = []
    for name in field_names:
        values = {key: projected[key].get(name) for key in order}
        rows.append({
            "field": name,
            "tesseract_gemini": values["tesseract_gemini"],
            "paddleocr_gemini": values["paddleocr_gemini"],
            "tesseract_local_llm": values["tesseract_local_llm"],
            "paddleocr_local_llm": values["paddleocr_local_llm"],
            "values_match": all(_same(values[order[0]], values[key]) for key in order[1:]),
        })

    differing = [row["field"] for row in rows if not row["values_match"]]
    summary_rows = []
    for key in order:
        run = runs[key]
        summary_rows.append({
            "ocr": run["ocr_method"],
            "llm": run["llm_provider"],
            "json_valid": run["json_valid"],
            "llm_success": run["llm_success"],
            "missing_fields": _missing(projected[key]) if run["json_valid"] else ["llm_call_failed"],
            "missing_field_count": (
                len(_missing(projected[key])) if run["json_valid"] else None
            ),
            "field_differences_across_runs": differing,
            "field_difference_count": len(differing),
            "ocr_time_seconds": run["ocr_processing_time_seconds"],
            "llm_time_seconds": run["llm_processing_time_seconds"],
            "ocr_confidence": run["ocr_confidence"],
            "ocr_quality": (run.get("ocr_quality") or {}).get("quality"),
            "ocr_quality_score": (run.get("ocr_quality") or {}).get("score"),
            "errors": run["errors"],
        })

    successful = [key for key in order if runs[key]["json_valid"]]
    return {
        "pdf": runs["tesseract_gemini"].get("pdf"),
        "ran_at_utc": datetime.now(timezone.utc).isoformat(),
        "note": (
            "Winner is based on structured JSON completeness and cross-run agreement, "
            "not OCR confidence."
        ),
        "fields": rows,
        "summary": summary_rows,
        "successful_runs": successful,
        "field_difference_count": len(differing),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("pdf")
    parser.add_argument("--out-dir", default="comparison_results")
    parser.add_argument(
        "--local-url",
        default=os.environ.get("LOCAL_LLM_BASE_URL", "http://127.0.0.1:8000"),
    )
    parser.add_argument("--local-timeout", type=float, default=180.0)
    args = parser.parse_args()

    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    os.environ.setdefault("FLAGS_use_mkldnn", "0")
    os.environ.setdefault("FLAGS_onednn", "0")
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass

    root = str(_project_root())
    if root not in sys.path:
        sys.path.insert(0, root)
    os.chdir(root)

    pdf_path = Path(args.pdf)
    if not pdf_path.is_file():
        print(f"PDF not found: {pdf_path}", file=sys.stderr)
        return 1

    out_dir = Path(args.out_dir)
    if not out_dir.is_absolute():
        out_dir = _project_root() / out_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    print("OCR tesseract...")
    tesseract = _ocr_page(pdf_path, "tesseract")
    print(
        f"  chars={tesseract['ocr_character_count']} "
        f"conf={tesseract['ocr_confidence']} time={tesseract['ocr_processing_time_seconds']}s"
    )
    print("OCR paddleocr...")
    paddle = _ocr_page(pdf_path, "paddleocr")
    print(
        f"  chars={paddle['ocr_character_count']} "
        f"conf={paddle['ocr_confidence']} time={paddle['ocr_processing_time_seconds']}s"
    )

    runs: Dict[str, Dict[str, Any]] = {}
    for key, ocr, provider in (
        ("tesseract_gemini", tesseract, "gemini"),
        ("paddleocr_gemini", paddle, "gemini"),
    ):
        print(f"Gemini on {ocr['ocr_method']}...")
        final_json, llm_time, errors = _call_gemini(ocr["ocr_text"])
        record = _result_record(ocr, provider, final_json, llm_time, errors)
        record["pdf"] = str(pdf_path)
        runs[key] = record
        print(f"  json_valid={record['json_valid']} llm_time={llm_time}s errors={len(errors)}")

    for key, ocr in (
        ("tesseract_local_llm", tesseract),
        ("paddleocr_local_llm", paddle),
    ):
        print(f"Local Qwen on {ocr['ocr_method']} -> {args.local_url}")
        final_json, llm_time, errors, raw = _call_local_batch(
            args.local_url, ocr["ocr_method"], ocr["ocr_text"], args.local_timeout
        )
        record = _result_record(ocr, "local_qwen", final_json, llm_time, errors, raw)
        record["pdf"] = str(pdf_path)
        record["local_llm_url"] = args.local_url.rstrip("/") + "/v1/invoice/batch"
        runs[key] = record
        print(f"  json_valid={record['json_valid']} errors={errors[:1]}")

    names = {
        "tesseract_gemini": "tesseract_gemini.json",
        "paddleocr_gemini": "paddleocr_gemini.json",
        "tesseract_local_llm": "tesseract_local_llm.json",
        "paddleocr_local_llm": "paddleocr_local_llm.json",
    }
    for key, filename in names.items():
        path = out_dir / filename
        path.write_text(json.dumps(runs[key], indent=2, ensure_ascii=False, default=str), encoding="utf-8")
        print(f"Saved {path}")

    comparison = _build_comparison(runs)
    comparison["pdf"] = str(pdf_path)
    comparison["local_llm_url"] = args.local_url.rstrip("/") + "/v1/invoice/batch"
    comparison_path = out_dir / "llm_ocr_comparison.json"
    comparison_path.write_text(
        json.dumps(comparison, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )
    print(f"Saved {comparison_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
