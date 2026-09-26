"""Batch runner: existing extractor, independent ground truth, reports.

PDFs are processed in fixed batches. A completed PASS, FAIL, REVIEW, or ERROR
is skipped on the next run. RATE_LIMITED is saved and retried later.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

COMPLETED_STATUSES = {"PASS", "FAIL", "REVIEW", "ERROR"}
RETRY_BACKOFF_SECONDS = (10, 30, 60, 120)
_PROGRESS_LOCK = threading.Lock()
_GEMINI_LOCK = threading.Lock()
_GEMINI_TLS = threading.local()
_GEMINI_GUARD = {
    "installed": False,
    "delay": 5.0,
    "max_retries": 4,
    "sleep_fn": time.sleep,
    "random_fn": None,
    "original": None,
    "last_end": 0.0,
}


class RateLimitSignal(Exception):
    """Gemini rejected this PDF with HTTP 429 or RESOURCE_EXHAUSTED."""


def discover_pdfs(pdf_dir: Path) -> List[Path]:
    found = []
    if not pdf_dir.exists():
        return found
    for path in pdf_dir.rglob("*"):
        if path.is_file() and path.suffix.lower() == ".pdf":
            found.append(path)
    return sorted(found, key=lambda p: str(p).lower())


def file_id(path: Path, root: Path) -> str:
    try:
        rel = path.relative_to(root)
    except ValueError:
        rel = Path(path.name)
    return str(rel)


def _result_key(path: Path, root: Path) -> str:
    stem = file_id(path, root)
    stem = str(Path(stem).with_suffix(""))
    return stem.replace("\\", "__").replace("/", "__")


def text_is_rate_limit(text: str) -> bool:
    upper = (text or "").upper()
    if "RESOURCE_EXHAUSTED" in upper:
        return True
    if "RATE_LIMIT" in upper or "RATE LIMIT" in upper or "TOO MANY REQUESTS" in upper:
        return True
    if re.search(r"(?<!\d)429(?!\d)", upper) and any(
        token in upper
        for token in ("GEMINI", "RESOURCE", "QUOTA", "EXHAUST", "RATE", "VERTEX", "HTTP")
    ):
        return True
    return False


def is_rate_limit_failure(exc: BaseException) -> bool:
    code = getattr(exc, "code", None)
    if code is None:
        code = getattr(exc, "status_code", None)
    if code == 429:
        return True
    return text_is_rate_limit(f"{type(exc).__name__} {exc}")


def _begin_gemini_pdf(filename: str) -> None:
    _GEMINI_TLS.pdf_name = filename
    _GEMINI_TLS.exhausted = False


def _gemini_rate_limit_exhausted() -> bool:
    return bool(getattr(_GEMINI_TLS, "exhausted", False))


def _mark_gemini_rate_limited() -> None:
    _GEMINI_TLS.exhausted = True


def install_gemini_rate_limit_guard(
    delay_seconds: float = 5.0,
    max_retries: int = 4,
    sleep_fn: Callable[[float], None] = time.sleep,
    random_fn: Optional[Callable[[float, float], float]] = None,
) -> None:
    """Serialize Vertex Gemini calls for batch QA only. Does not edit production files."""
    import random as random_module

    from services import vertex_gemini_client as vgc

    _GEMINI_GUARD["delay"] = max(0.0, float(delay_seconds))
    _GEMINI_GUARD["max_retries"] = max(0, int(max_retries))
    _GEMINI_GUARD["sleep_fn"] = sleep_fn
    _GEMINI_GUARD["random_fn"] = random_fn or random_module.uniform

    if _GEMINI_GUARD["installed"] and _GEMINI_GUARD["original"] is not None:
        return

    original = vgc.generate_content_via_vertex
    _GEMINI_GUARD["original"] = original

    def guarded_generate_content_via_vertex(model: str, payload: dict, timeout: int):
        sleep = _GEMINI_GUARD["sleep_fn"]
        jitter_fn = _GEMINI_GUARD["random_fn"] or random_module.uniform
        delay = float(_GEMINI_GUARD["delay"])
        retries = int(_GEMINI_GUARD["max_retries"])
        pdf_name = getattr(_GEMINI_TLS, "pdf_name", None) or "gemini"

        with _GEMINI_LOCK:
            wait_for = delay + float(jitter_fn(0.0, 2.0))
            elapsed = time.monotonic() - float(_GEMINI_GUARD["last_end"] or 0.0)
            if _GEMINI_GUARD["last_end"] and elapsed < wait_for:
                sleep(wait_for - elapsed)

            print(f"Gemini request: {pdf_name}", flush=True)
            last_exc: Optional[BaseException] = None
            for attempt in range(retries + 1):
                try:
                    response = original(model, payload, timeout)
                    print("Gemini request succeeded.", flush=True)
                    _GEMINI_GUARD["last_end"] = time.monotonic()
                    return response
                except Exception as exc:
                    last_exc = exc
                    if not is_rate_limit_failure(exc):
                        _GEMINI_GUARD["last_end"] = time.monotonic()
                        raise
                    print("429 RESOURCE_EXHAUSTED", flush=True)
                    if attempt >= retries:
                        _mark_gemini_rate_limited()
                        _GEMINI_GUARD["last_end"] = time.monotonic()
                        raise RateLimitSignal(
                            "HTTP 429 RESOURCE_EXHAUSTED after retries"
                        ) from exc
                    backoff = RETRY_BACKOFF_SECONDS[
                        min(attempt, len(RETRY_BACKOFF_SECONDS) - 1)
                    ]
                    print(f"Retry {attempt + 1}/{retries} in {backoff}s", flush=True)
                    sleep(backoff)

            _mark_gemini_rate_limited()
            _GEMINI_GUARD["last_end"] = time.monotonic()
            raise RateLimitSignal(
                str(last_exc) if last_exc else "HTTP 429 RESOURCE_EXHAUSTED"
            )

    vgc.generate_content_via_vertex = guarded_generate_content_via_vertex
    _GEMINI_GUARD["installed"] = True


def _load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def load_progress(path: Path) -> Dict[str, dict]:
    data = _load_json(path)
    progress = {}
    for key, value in data.items():
        if isinstance(value, dict) and value.get("status"):
            progress[str(key)] = value
    return progress


def save_progress(path: Path, progress: Dict[str, dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(progress, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    temporary.replace(path)


def _load_previous(latest_report: Path) -> dict:
    return _load_json(latest_report)


def _regression(previous: dict, current_files: List[dict]) -> dict:
    old = {
        item.get("file"): item.get("status")
        for item in (previous.get("files") or [])
        if isinstance(item, dict)
    }
    newly_failing = []
    newly_passing = []
    for item in current_files:
        name = item.get("file")
        before = old.get(name)
        if before is None:
            continue
        if before == "PASS" and item.get("status") in {"FAIL", "ERROR"}:
            newly_failing.append(name)
        if before in {"FAIL", "ERROR"} and item.get("status") == "PASS":
            newly_passing.append(name)
    return {
        "previous_run": previous.get("run_id"),
        "newly_failing": newly_failing,
        "newly_passing": newly_passing,
    }


def _blank_record(path: Path, root: Path) -> dict:
    return {
        "file": file_id(path, root),
        "path": str(path),
        "format": "Unknown layout",
        "api_method": "",
        "status": "ERROR",
        "expected_rows": 0,
        "extracted_rows": 0,
        "matched_rows": 0,
        "missing_rows": [],
        "extra_rows": [],
        "duplicates": [],
        "mismatches": [],
        "ocr_review": [],
        "accounting": [],
        "confidence": "low",
        "notes": [],
        "result_key": _result_key(path, root),
    }


def _rate_limited_record(path: Path, root: Path, message: str) -> dict:
    record = _blank_record(path, root)
    record["status"] = "RATE_LIMITED"
    record["error"] = message or "Gemini rate limit (HTTP 429 / RESOURCE_EXHAUSTED)"
    print(f"RATE_LIMITED {path.name}", flush=True)
    print("Moving to next PDF.", flush=True)
    return record


def process_pdf(path: Path, root: Path, max_pages: int) -> dict:
    from qa.compare import api_method, compare_statements
    from qa.ground_truth import build_ground_truth
    from services.sales_statement_extractor import extract_sales_statement

    record = _blank_record(path, root)
    try:
        file_bytes = path.read_bytes()
    except OSError as exc:
        record["error"] = f"Could not read PDF: {exc}"
        return record

    extracted = None
    extract_error = None
    _begin_gemini_pdf(path.name)
    try:
        extracted = extract_sales_statement(file_bytes, path.name)
    except RateLimitSignal as exc:
        return _rate_limited_record(
            path,
            root,
            str(exc) or "Gemini rate limit (HTTP 429 / RESOURCE_EXHAUSTED)",
        )
    except Exception as exc:
        if _gemini_rate_limit_exhausted() or is_rate_limit_failure(exc):
            return _rate_limited_record(
                path,
                root,
                str(exc) or "Gemini rate limit (HTTP 429 / RESOURCE_EXHAUSTED)",
            )
        extract_error = exc
        record["error"] = str(exc)
        record["traceback"] = traceback.format_exc()

    if _gemini_rate_limit_exhausted():
        return _rate_limited_record(
            path,
            root,
            "Gemini rate limit (HTTP 429 / RESOURCE_EXHAUSTED)",
        )

    ground = None
    try:
        ground = build_ground_truth(str(path), path.name, max_pages=max_pages)
    except Exception as exc:
        record["error"] = (record.get("error") or "") + f" Ground truth failed: {exc}"
        record["traceback"] = (record.get("traceback") or "") + "\n" + traceback.format_exc()

    record["extracted"] = extracted
    record["ground_truth"] = ground
    if ground:
        record["format"] = ground.get("format") or record["format"]
        record["confidence"] = ground.get("confidence")
        record["notes"] = ground.get("notes") or []
        record["expected_rows"] = len(ground.get("line_items") or [])
    if extracted:
        record["api_method"] = api_method(extracted)
        if record["format"] == "Unknown layout":
            record["format"] = (
                str(extracted.get("report_title") or "")
                or record["api_method"]
                or "Unknown layout"
            )
    if extract_error or ground is None:
        record["status"] = "ERROR"
        return record
    compared = compare_statements(ground, extracted)
    record.update(compared)
    record["result_key"] = _result_key(path, root)
    return record


def process_pdf_with_retries(
    path: Path,
    root: Path,
    max_pages: int,
    max_retries: int,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> dict:
    """Process one PDF. Gemini 429 retries happen inside the Vertex call guard."""
    del max_retries, sleep_fn
    return process_pdf(path, root, max_pages)


def _summary(records: List[dict]) -> dict:
    return {
        "total": len(records),
        "pass": sum(1 for row in records if row.get("status") == "PASS"),
        "fail": sum(1 for row in records if row.get("status") == "FAIL"),
        "review": sum(1 for row in records if row.get("status") == "REVIEW"),
        "error": sum(1 for row in records if row.get("status") == "ERROR"),
        "rate_limited": sum(1 for row in records if row.get("status") == "RATE_LIMITED"),
    }


def _public_record(item: dict) -> dict:
    return {
        key: value
        for key, value in item.items()
        if key not in {"extracted", "ground_truth"}
    }


def _write_report_files(directory: Path, report: dict) -> None:
    from qa.report import dump_json, render_fix_prompt, render_html

    directory.mkdir(parents=True, exist_ok=True)
    dump_json(str(directory / "report.json"), report)
    (directory / "report.html").write_text(render_html(report), encoding="utf-8")
    (directory / "cursor_fix_prompt.txt").write_text(
        render_fix_prompt(report), encoding="utf-8"
    )


def _persist_completed(output_dir: Path, record: dict) -> None:
    from qa.report import dump_json

    key = record.get("result_key") or "result"
    extracted = record.get("extracted")
    ground = record.get("ground_truth")
    if extracted is not None:
        extracted_dir = output_dir / "extracted"
        extracted_dir.mkdir(parents=True, exist_ok=True)
        dump_json(str(extracted_dir / f"{key}.json"), extracted)
    if ground is not None:
        ground_dir = output_dir / "ground_truth"
        ground_dir.mkdir(parents=True, exist_ok=True)
        dump_json(str(ground_dir / f"{key}.json"), ground)
    records_dir = output_dir / "records"
    records_dir.mkdir(parents=True, exist_ok=True)
    dump_json(str(records_dir / f"{key}.json"), _public_record(record))


def reconcile_progress_with_records(output_dir: Path, progress: Dict[str, dict]) -> Dict[str, dict]:
    """Prefer a completed on-disk record over a stale RATE_LIMITED progress row."""
    records_dir = output_dir / "records"
    if not records_dir.exists():
        return progress
    updated = dict(progress)
    for name, entry in list(updated.items()):
        key = entry.get("result_key") or Path(str(name)).stem
        stored = _load_json(records_dir / f"{key}.json")
        status = stored.get("status")
        if status not in COMPLETED_STATUSES:
            continue
        if entry.get("status") == status:
            continue
        updated[name] = {
            "status": status,
            "batch": entry.get("batch") or 1,
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "format": stored.get("format") or entry.get("format") or "",
            "handler": stored.get("api_method") or entry.get("handler") or "",
            "result_key": key,
        }
    return updated


def _load_stored_record(output_dir: Path, path: Path, root: Path) -> Optional[dict]:
    key = _result_key(path, root)
    stored = _load_json(output_dir / "records" / f"{key}.json")
    if not stored:
        return None
    stored["result_key"] = key
    return stored


def _progress_entry(record: dict, batch_number: int) -> dict:
    return {
        "status": record.get("status"),
        "batch": batch_number,
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "format": record.get("format") or "",
        "handler": record.get("api_method") or "",
        "result_key": record.get("result_key") or "",
    }


def _stub_from_progress(file_name: str, entry: dict) -> dict:
    return {
        "file": file_name,
        "format": entry.get("format") or "Unknown layout",
        "api_method": entry.get("handler") or "",
        "status": entry.get("status") or "RATE_LIMITED",
        "expected_rows": 0,
        "extracted_rows": 0,
        "matched_rows": 0,
        "missing_rows": [],
        "extra_rows": [],
        "duplicates": [],
        "mismatches": [],
        "ocr_review": [],
        "accounting": [],
        "notes": [],
        "error": "Gemini rate limit (HTTP 429 / RESOURCE_EXHAUSTED)",
    }


def _records_for_paths(
    paths: List[Path],
    root: Path,
    output_dir: Path,
    progress: Dict[str, dict],
) -> List[dict]:
    records = []
    for path in paths:
        identity = file_id(path, root)
        entry = progress.get(identity) or {}
        status = entry.get("status")
        if status in COMPLETED_STATUSES:
            stored = _load_stored_record(output_dir, path, root)
            if stored is not None:
                records.append(stored)
                continue
        if status:
            records.append(_stub_from_progress(identity, entry))
    return records


def _write_batch_and_overall(
    output_dir: Path,
    pdf_dir: Path,
    batch_number: int,
    batch_paths: List[Path],
    all_paths: List[Path],
    progress: Dict[str, dict],
    previous: dict,
) -> dict:
    batch_id = f"batch_{batch_number:03d}"
    batch_records = _records_for_paths(batch_paths, pdf_dir, output_dir, progress)
    batch_report = {
        "run_id": batch_id,
        "pdf_dir": str(pdf_dir),
        "batch": batch_number,
        "summary": _summary(batch_records),
        "files": [_public_record(row) for row in batch_records],
    }
    _write_report_files(output_dir / "runs" / batch_id, batch_report)

    overall_records = _records_for_paths(all_paths, pdf_dir, output_dir, progress)
    known = {row.get("file") for row in overall_records}
    for identity, entry in progress.items():
        if identity in known:
            continue
        if entry.get("status") in COMPLETED_STATUSES or entry.get("status") == "RATE_LIMITED":
            overall_records.append(_stub_from_progress(identity, entry))
    overall_records.sort(key=lambda row: str(row.get("file") or "").lower())
    public_files = [_public_record(row) for row in overall_records]
    report = {
        "run_id": "cumulative",
        "pdf_dir": str(pdf_dir),
        "latest_batch": batch_id,
        "summary": _summary(public_files),
        "regression": _regression(previous, public_files),
        "files": public_files,
    }
    _write_report_files(output_dir, report)
    (output_dir / "latest_run.txt").write_text(batch_id, encoding="utf-8")
    return report


def _batch_slices(pdfs: List[Path], batch_size: int) -> List[tuple]:
    size = max(1, batch_size)
    slices = []
    for start in range(0, len(pdfs), size):
        number = start // size + 1
        slices.append((number, pdfs[start : start + size]))
    return slices


def _print_batch_totals(batch_number: int, batch_count: int, stats: dict) -> None:
    print(f"Batch {batch_number}/{batch_count}", flush=True)
    print(flush=True)
    print(f"Completed: {stats['completed']}", flush=True)
    print(f"Skipped: {stats['skipped']}", flush=True)
    print(f"Remaining: {stats['remaining']}", flush=True)
    print(flush=True)
    print(f"PASS: {stats['pass']}", flush=True)
    print(f"FAIL: {stats['fail']}", flush=True)
    print(f"REVIEW: {stats['review']}", flush=True)
    print(f"ERROR: {stats['error']}", flush=True)
    print(f"RATE_LIMITED: {stats['rate_limited']}", flush=True)
    print(flush=True)
    print(f"Batch {batch_number} complete.", flush=True)
    print(flush=True)


def run_batch(
    pdf_dir: Path,
    output_dir: Path,
    workers: int = 1,
    limit: Optional[int] = None,
    name_contains: Optional[Iterable[str]] = None,
    max_pages: int = 20,
    batch_size: int = 100,
    max_retries: int = 4,
    gemini_delay: float = 5.0,
    force: bool = False,
    processor: Optional[Callable[..., dict]] = None,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> dict:
    pdfs = discover_pdfs(pdf_dir)
    if name_contains:
        needles = [needle.lower() for needle in name_contains if needle]
        pdfs = [path for path in pdfs if any(needle in path.name.lower() for needle in needles)]
    if limit is not None:
        pdfs = pdfs[: max(0, limit)]

    output_dir.mkdir(parents=True, exist_ok=True)
    progress_path = output_dir / "progress.json"
    with _PROGRESS_LOCK:
        progress = load_progress(progress_path)
        reconciled = reconcile_progress_with_records(output_dir, progress)
        if reconciled != progress:
            progress = reconciled
            save_progress(progress_path, progress)
    previous = _load_previous(output_dir / "report.json")
    batches = _batch_slices(pdfs, batch_size)
    batch_count = len(batches)

    if processor is None:
        install_gemini_rate_limit_guard(
            delay_seconds=gemini_delay,
            max_retries=max_retries,
            sleep_fn=sleep_fn,
        )

    already_done = 0
    if not force:
        for path in pdfs:
            entry = progress.get(file_id(path, pdf_dir)) or {}
            if entry.get("status") in COMPLETED_STATUSES and _load_stored_record(
                output_dir, path, pdf_dir
            ):
                already_done += 1
    if already_done:
        print(f"Found {already_done} already completed PDFs.", flush=True)
        print("Skipping them.", flush=True)
        print(flush=True)

    run_pass = run_fail = run_review = run_error = run_limited = 0
    run_skipped = 0
    run_processed = 0
    report: dict = {
        "run_id": "cumulative",
        "pdf_dir": str(pdf_dir),
        "summary": _summary([]),
        "regression": _regression(previous, []),
        "files": [],
    }
    if not pdfs:
        _write_report_files(output_dir, report)
        report["run_stats"] = {
            "processed": 0,
            "skipped": 0,
            "pass": 0,
            "fail": 0,
            "review": 0,
            "error": 0,
            "rate_limited": 0,
        }
        return report

    def _handle_record(path: Path, batch_number: int, record: dict) -> str:
        nonlocal run_pass, run_fail, run_review, run_error, run_limited, run_processed
        status = str(record.get("status") or "ERROR")
        identity = file_id(path, pdf_dir)
        record["file"] = identity
        record["result_key"] = record.get("result_key") or _result_key(path, pdf_dir)
        if status in COMPLETED_STATUSES:
            _persist_completed(output_dir, record)
        with _PROGRESS_LOCK:
            progress[identity] = _progress_entry(record, batch_number)
            save_progress(progress_path, progress)
        run_processed += 1
        if status == "PASS":
            run_pass += 1
        elif status == "FAIL":
            run_fail += 1
        elif status == "REVIEW":
            run_review += 1
        elif status == "ERROR":
            run_error += 1
        elif status == "RATE_LIMITED":
            run_limited += 1
        return status

    for batch_number, batch_paths in batches:
        print(f"Starting Batch {batch_number}/{batch_count}...", flush=True)
        print(flush=True)
        stats = {
            "completed": 0,
            "skipped": 0,
            "remaining": 0,
            "pass": 0,
            "fail": 0,
            "review": 0,
            "error": 0,
            "rate_limited": 0,
        }
        def _already_done(path: Path) -> bool:
            if force:
                return False
            entry = progress.get(file_id(path, pdf_dir)) or {}
            if entry.get("status") not in COMPLETED_STATUSES:
                return False
            return _load_stored_record(output_dir, path, pdf_dir) is not None

        def _skip(path: Path) -> None:
            nonlocal run_skipped
            print("SKIP already completed:", flush=True)
            print(path.name, flush=True)
            stats["skipped"] += 1
            run_skipped += 1

        def _finish(path: Path, record: dict) -> None:
            status = _handle_record(path, batch_number, record)
            print(
                f"{path.name} {status} "
                f"rows {record.get('expected_rows')}/{record.get('extracted_rows')}",
                flush=True,
            )
            stats[status.lower()] = stats.get(status.lower(), 0) + 1
            if status in COMPLETED_STATUSES:
                stats["completed"] += 1

        def _run_one(path: Path) -> dict:
            if processor is not None:
                record = processor(path, pdf_dir, max_pages)
                if not isinstance(record, dict):
                    record = _blank_record(path, pdf_dir)
                    record["status"] = "ERROR"
                    record["error"] = "Processor did not return a record"
                record.setdefault("file", file_id(path, pdf_dir))
                record.setdefault("result_key", _result_key(path, pdf_dir))
                record.setdefault("status", "ERROR")
                return record
            return process_pdf_with_retries(
                path,
                pdf_dir,
                max_pages,
                max_retries=max_retries,
                sleep_fn=sleep_fn,
            )

        if workers <= 1:
            for path in batch_paths:
                if _already_done(path):
                    _skip(path)
                    continue
                try:
                    record = _run_one(path)
                except Exception as exc:
                    record = _blank_record(path, pdf_dir)
                    record["error"] = str(exc)
                    record["traceback"] = traceback.format_exc()
                _finish(path, record)
        else:
            pending = [path for path in batch_paths if not _already_done(path)]
            for path in batch_paths:
                if _already_done(path):
                    _skip(path)
            if pending:
                with ThreadPoolExecutor(max_workers=workers) as pool:
                    futures = {pool.submit(_run_one, path): path for path in pending}
                    for future in as_completed(futures):
                        path = futures[future]
                        try:
                            record = future.result()
                        except Exception as exc:
                            record = _blank_record(path, pdf_dir)
                            record["error"] = str(exc)
                            record["traceback"] = traceback.format_exc()
                        _finish(path, record)

        for path in batch_paths:
            entry = progress.get(file_id(path, pdf_dir)) or {}
            if entry.get("status") not in COMPLETED_STATUSES:
                stats["remaining"] += 1
        _print_batch_totals(batch_number, batch_count, stats)
        report = _write_batch_and_overall(
            output_dir,
            pdf_dir,
            batch_number,
            batch_paths,
            pdfs,
            progress,
            previous,
        )

    report["run_stats"] = {
        "processed": run_processed,
        "skipped": run_skipped,
        "pass": run_pass,
        "fail": run_fail,
        "review": run_review,
        "error": run_error,
        "rate_limited": run_limited,
    }
    return report


def main(argv: Optional[List[str]] = None) -> int:
    try:
        from dotenv import load_dotenv

        load_dotenv(ROOT / ".env")
    except ImportError:
        pass

    parser = argparse.ArgumentParser(
        description="Resumable batch QA for the existing sales-statement extractor."
    )
    parser.add_argument(
        "--pdf-dir",
        default=str(ROOT / "test_pdfs"),
        help="Folder of PDFs to check, including subfolders. Default: test_pdfs",
    )
    parser.add_argument(
        "--output-dir",
        default=str(ROOT / "qa_results"),
        help="Where to write reports. Default: qa_results",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=int(os.getenv("QA_WORKERS", "1")),
        help="How many PDFs to process at once. Default: 1",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=100,
        help="How many PDFs belong to one batch. Default: 100",
    )
    parser.add_argument(
        "--max-retries",
        type=int,
        default=4,
        help="Gemini 429 retries per request before marking RATE_LIMITED. Default: 4",
    )
    parser.add_argument(
        "--gemini-delay",
        type=float,
        default=5.0,
        help="Seconds to wait before the next Gemini request, plus 0-2s jitter. Default: 5",
    )
    parser.add_argument("--limit", type=int, default=None, help="Process only the first N PDFs.")
    parser.add_argument(
        "--files",
        default="",
        help="Comma-separated filename fragments to include.",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=int(os.getenv("SALES_PDF_MAX_PAGES", "20")),
        help="Page cap, aligned with the extractor. Default: 20",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Reprocess selected PDFs even when a completed result already exists.",
    )
    args = parser.parse_args(argv)
    needles = [part.strip() for part in args.files.split(",") if part.strip()]
    report = run_batch(
        pdf_dir=Path(args.pdf_dir),
        output_dir=Path(args.output_dir),
        workers=max(1, args.workers),
        limit=args.limit,
        name_contains=needles or None,
        max_pages=args.max_pages,
        batch_size=max(1, args.batch_size),
        max_retries=max(0, args.max_retries),
        gemini_delay=max(0.0, args.gemini_delay),
        force=args.force,
    )
    summary = report["summary"]
    stats = report.get("run_stats") or {}
    print(
        f"TOTAL {summary['total']}  PASS {summary['pass']}  "
        f"FAIL {summary['fail']}  REVIEW {summary['review']}  "
        f"ERROR {summary['error']}  RATE_LIMITED {summary.get('rate_limited', 0)}"
    )
    print(
        f"This run processed {stats.get('processed', 0)} "
        f"and skipped {stats.get('skipped', 0)}."
    )
    print(f"Report: {Path(args.output_dir) / 'report.html'}")
    print(f"Fix prompt: {Path(args.output_dir) / 'cursor_fix_prompt.txt'}")
    print(f"Progress: {Path(args.output_dir) / 'progress.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
