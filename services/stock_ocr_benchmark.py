"""Isolated stock OCR benchmark orchestration (not production)."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from services.stock_ocr_providers import get_provider
from services.stock_ocr_table_reconstructor import (
    column_shift_detail,
    detect_column_shift,
    reconstruct_stock_table,
    validate_row_identity,
)

logger = logging.getLogger(__name__)

# Printed expectations for the known problem statement (benchmark assertions only).
ARJUNA_EXPECTED: Dict[str, Optional[float]] = {
    "opening_qty": 18.0,
    "purchase_qty": 60.0,
    "sales_return_qty": 1.0,
    "total_qty": 79.0,
    "sales_qty": 26.0,
    "purchase_return_qty": 0.0,
    "closing_qty": 53.0,
}

# Crop-verified from source image qty-band crops (benchmark ground truth).
# Note: a briefed Sale=618/Balance=0 for BONNISAN DROPS does not match the
# printed cells (318/185); providers are scored against crop-verified values.
BONNISAN_DROPS_EXPECTED: Dict[str, Optional[float]] = {
    "opening_qty": 0.0,
    "purchase_qty": 500.0,
    "sales_return_qty": 3.0,
    "total_qty": 503.0,
    "sales_qty": 318.0,
    "purchase_return_qty": 0.0,
    "closing_qty": 185.0,
}

# User-briefed alternate for BONNISAN DROPS (section 8) — scored separately.
BONNISAN_DROPS_USER_BRIEFED: Dict[str, Optional[float]] = {
    "opening_qty": 0.0,
    "purchase_qty": 500.0,
    "sales_return_qty": 3.0,
    "total_qty": 503.0,
    "sales_qty": 618.0,
    "purchase_return_qty": 0.0,
    "closing_qty": 0.0,
}

BONNISAN_100_EXPECTED: Dict[str, Optional[float]] = {
    "opening_qty": 37.0,
    "purchase_qty": 224.0,
    "sales_return_qty": 1.0,
    "total_qty": 262.0,
    "sales_qty": 199.0,
    "purchase_return_qty": 1.0,
    "closing_qty": 62.0,
}

BONNISAN_200_EXPECTED: Dict[str, Optional[float]] = {
    "opening_qty": 22.0,
    "purchase_qty": 113.0,
    "sales_return_qty": 4.0,
    "total_qty": 139.0,
    "sales_qty": 87.0,
    "purchase_return_qty": 0.0,
    "closing_qty": 52.0,
}

DEFAULT_EXPECTATIONS: Dict[str, Dict[str, Optional[float]]] = {
    "ARJUNA TAB": ARJUNA_EXPECTED,
    "BONNISAN DROPS": BONNISAN_DROPS_EXPECTED,
    "BONNISAN SYRUP 100ML": BONNISAN_100_EXPECTED,
    "BONNISAN SYRUP 200ML": BONNISAN_200_EXPECTED,
}

COMPARE_FIELDS = (
    "opening_qty",
    "purchase_qty",
    "sales_return_qty",
    "total_qty",
    "sales_qty",
    "purchase_return_qty",
    "closing_qty",
)


def _norm_name(name: Any) -> str:
    return re_sub_name(str(name or ""))


def re_sub_name(name: str) -> str:
    import re

    return re.sub(r"[^a-z0-9]", "", name.lower())


def _get_field(item: Dict[str, Any], field: str) -> Optional[float]:
    extra = item.get("extra") if isinstance(item.get("extra"), dict) else {}
    fs = extra.get("field_source") if isinstance(extra.get("field_source"), dict) else {}
    mapping = {
        "opening_qty": ("opening_qty", None),
        "purchase_qty": ("receipts_qty", None),
        "sales_qty": ("sales_qty", None),
        "closing_qty": ("closing_qty", None),
        "sales_return_qty": (None, "sale_return"),
        "purchase_return_qty": (None, "purchase_return"),
        "total_qty": (None, "total_stock"),
    }
    top, ex = mapping[field]
    if fs.get(field) == "missing":
        # blank printed → treat as 0 for purchase_return/opening when expected 0
        if field in {"purchase_return_qty", "opening_qty"}:
            return 0.0
        if field == "closing_qty":
            return None
        return None
    if top and item.get(top) is not None and item.get(top) != "":
        try:
            return float(item.get(top))
        except (TypeError, ValueError):
            return None
    if ex and extra.get(ex) is not None and extra.get(ex) != "":
        try:
            return float(extra.get(ex))
        except (TypeError, ValueError):
            return None
    if field == "closing_qty":
        return None
    if field in {"opening_qty", "purchase_qty", "sales_qty"}:
        try:
            return float(item.get(top) or 0.0)
        except (TypeError, ValueError):
            return 0.0
    return None


def find_product(items: Sequence[Dict[str, Any]], product: str) -> Optional[Dict[str, Any]]:
    target = _norm_name(product)
    for item in items:
        if _norm_name(item.get("product_name")) == target:
            return item
    # prefix / contains
    for item in items:
        n = _norm_name(item.get("product_name"))
        if target in n or n in target:
            return item
    return None


def compare_cells(
    provider: str,
    items: Sequence[Dict[str, Any]],
    expectations: Dict[str, Dict[str, Optional[float]]],
) -> Dict[str, Any]:
    rows_out: List[Dict[str, Any]] = []
    total = correct = incorrect = missing = 0
    row_pass = 0
    shift_count = 0
    arith_mismatch = 0
    blank_total = blank_correct = 0
    field_stats: Dict[str, Dict[str, int]] = {
        f: {"total": 0, "correct": 0, "incorrect": 0, "missing": 0}
        for f in COMPARE_FIELDS
    }
    product_name_hits = product_name_total = 0

    for product, expected in expectations.items():
        item = find_product(items, product)
        row_ok = True
        product_name_total += 1
        if item is not None:
            product_name_hits += 1
        shift = column_shift_detail(item) if item else {}
        if shift.get("column_shift_suspected"):
            shift_count += 1
        identity = validate_row_identity(item) if item else None
        if identity and identity.get("reconciliation_failed"):
            arith_mismatch += 1
        for field in COMPARE_FIELDS:
            exp = expected.get(field)
            got = _get_field(item, field) if item else None
            total += 1
            field_stats[field]["total"] += 1
            status = "PASS"
            # blank-cell tracking: expected 0 / blank purchase_return
            if exp == 0.0 and field in {"purchase_return_qty", "opening_qty"}:
                blank_total += 1
            if got is None and exp is not None and field == "closing_qty":
                status = "MISSING"
                missing += 1
                field_stats[field]["missing"] += 1
                row_ok = False
            elif got is None and exp == 0.0:
                status = "PASS"
                correct += 1
                field_stats[field]["correct"] += 1
                if field in {"purchase_return_qty", "opening_qty"}:
                    blank_correct += 1
            elif got is None:
                status = "MISSING"
                missing += 1
                field_stats[field]["missing"] += 1
                row_ok = False
            elif exp is None:
                status = "PASS" if got is None else "INCORRECT"
                if status == "PASS":
                    correct += 1
                    field_stats[field]["correct"] += 1
                else:
                    incorrect += 1
                    field_stats[field]["incorrect"] += 1
                    row_ok = False
            elif abs(float(got) - float(exp)) <= 0.51:
                status = "PASS"
                correct += 1
                field_stats[field]["correct"] += 1
                if exp == 0.0 and field in {"purchase_return_qty", "opening_qty"}:
                    blank_correct += 1
            else:
                status = "INCORRECT"
                incorrect += 1
                field_stats[field]["incorrect"] += 1
                row_ok = False
            rows_out.append(
                {
                    "provider": provider,
                    "product": product,
                    "field": field,
                    "expected": exp,
                    "extracted": got,
                    "correct": status,
                    "column_shift_suspected": bool(shift.get("column_shift_suspected")),
                    "shift_patterns": shift.get("patterns") or [],
                }
            )
        if row_ok and item is not None:
            row_pass += 1

    n_products = len(expectations) or 1
    failed_rows = [
        r["product"]
        for r in {
            row["product"]: row
            for row in rows_out
            if row["correct"] != "PASS"
        }.values()
    ]
    per_field_accuracy = {
        f: round(100.0 * s["correct"] / s["total"], 2) if s["total"] else None
        for f, s in field_stats.items()
    }
    return {
        "provider": provider,
        "cell_rows": rows_out,
        "total_cells": total,
        "correct_cells": correct,
        "incorrect_cells": incorrect,
        "missing_cells": missing,
        "cell_accuracy_pct": round(100.0 * correct / total, 2) if total else 0.0,
        "row_accuracy_pct": round(100.0 * row_pass / n_products, 2),
        "rows_passed": row_pass,
        "rows_total": n_products,
        "column_shift_count": shift_count,
        "arithmetic_mismatch_count": arith_mismatch,
        "failed_products": sorted(set(failed_rows)),
        "product_name_accuracy_pct": round(
            100.0 * product_name_hits / product_name_total, 2
        )
        if product_name_total
        else None,
        "per_field_accuracy_pct": per_field_accuracy,
        "blank_cell_accuracy_pct": round(100.0 * blank_correct / blank_total, 2)
        if blank_total
        else None,
        "field_stats": field_stats,
    }


def reconciliation_summary(items: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    passed = failed = 0
    for item in items:
        extra = item.get("extra") if isinstance(item.get("extra"), dict) else {}
        # Prefer validation-only identity over stock engine when present later.
        identity = validate_row_identity(item)
        if not identity.get("reconciliation_failed"):
            passed += 1
        else:
            failed += 1
        # Also keep engine flag if set
        _ = extra.get("reconciliation")
    total = passed + failed
    return {
        "rows_checked": total,
        "pass": passed,
        "fail": failed,
        "pass_pct": round(100.0 * passed / total, 2) if total else None,
        "fail_pct": round(100.0 * failed / total, 2) if total else None,
    }


def _structured_row(item: Dict[str, Any]) -> Dict[str, Any]:
    extra = item.get("extra") if isinstance(item.get("extra"), dict) else {}
    shift = column_shift_detail(item)
    identity = validate_row_identity(item)
    return {
        "product_code": item.get("product_code"),
        "product_name": item.get("product_name"),
        "pack": item.get("packing"),
        "opening_qty": _get_field(item, "opening_qty"),
        "purchase_qty": _get_field(item, "purchase_qty"),
        "sales_return_qty": _get_field(item, "sales_return_qty"),
        "total_in_qty": _get_field(item, "total_qty"),
        "sales_qty": _get_field(item, "sales_qty"),
        "purchase_return_qty": _get_field(item, "purchase_return_qty"),
        "closing_qty": _get_field(item, "closing_qty"),
        "calculated_closing_qty": extra.get("calculated_closing_qty"),
        "raw_cells": extra.get("ocr_raw_cells") or extra.get("vision_raw_cells"),
        "cell_bboxes": extra.get("ocr_cell_bboxes"),
        "field_source": extra.get("field_source"),
        "y_center": extra.get("y_center"),
        "column_shift_suspected": shift.get("column_shift_suspected"),
        "column_shift_affected": shift.get("affected_columns"),
        "column_shift_patterns": shift.get("patterns"),
        "identity_validation": identity,
        "reconciliation_failed": bool(
            identity.get("reconciliation_failed")
            or extra.get("reconciliation_failed")
        ),
        "engine_reconciliation": extra.get("reconciliation"),
    }


def run_provider_benchmark(
    file_bytes: bytes,
    *,
    filename: str,
    provider: str,
    expectations: Optional[Dict[str, Dict[str, Optional[float]]]] = None,
) -> Dict[str, Any]:
    expectations = expectations or DEFAULT_EXPECTATIONS
    prov = get_provider(provider)
    ocr = prov.extract_document(file_bytes, filename=filename)
    reconstructed = reconstruct_stock_table(ocr)
    items = reconstructed.get("line_items") or []
    comparison = compare_cells(provider, items, expectations)
    recon_sum = reconciliation_summary(items)
    comparison["reconciliation_pass_pct"] = recon_sum.get("pass_pct")

    structured_all = [_structured_row(it) for it in items if isinstance(it, dict)]
    focus_rows = []
    for product in expectations:
        item = find_product(items, product)
        if not item:
            focus_rows.append({"product_name": product, "found": False})
            continue
        row = _structured_row(item)
        row["found"] = True
        focus_rows.append(row)

    # Alternate score vs user-briefed BONNISAN values (does not change primary metrics).
    briefed_cmp = None
    if "BONNISAN DROPS" in expectations:
        briefed_cmp = compare_cells(
            provider,
            items,
            {"BONNISAN DROPS": BONNISAN_DROPS_USER_BRIEFED},
        )

    # Truncate raw for JSON — keep shape + sample
    raw = ocr.raw_response
    raw_summary: Any
    try:
        raw_s = json.dumps(raw, default=str)
        if len(raw_s) > 50000:
            raw_summary = {
                "truncated": True,
                "chars": len(raw_s),
                "preview": raw_s[:8000],
            }
        else:
            raw_summary = raw
    except Exception:
        raw_summary = str(raw)[:8000]

    return {
        "provider": provider,
        "status": reconstructed.get("status"),
        "error": ocr.error or reconstructed.get("error"),
        "strategy": reconstructed.get("strategy"),
        "model": ocr.model or reconstructed.get("model"),
        "provider_latency_ms": round(float(ocr.latency_ms or 0.0), 1),
        "ocr_confidence": reconstructed.get("ocr_confidence"),
        "usage": ocr.usage,
        "request_metadata": ocr.request_metadata,
        "image_width": ocr.image_width,
        "image_height": ocr.image_height,
        "api_calls": int((ocr.usage or {}).get("gemini_calls") or (1 if not ocr.error else 0)),
        "rows": focus_rows,
        "rows_all_count": len(structured_all),
        "rows_preview": focus_rows,
        "comparison": comparison,
        "reconciliation_summary": recon_sum,
        "failed_rows": comparison.get("failed_products"),
        "column_map": reconstructed.get("column_map"),
        "resolved_headers": [
            {
                "col_index": c.get("col_index"),
                "header_text": c.get("header_text"),
                "canonical": c.get("canonical"),
                "reason": c.get("reason"),
            }
            for c in (reconstructed.get("column_map") or [])
            if isinstance(c, dict)
        ],
        "line_item_count": len(items),
        "metrics": {
            "product_name_accuracy_pct": comparison.get("product_name_accuracy_pct"),
            "per_field_accuracy_pct": comparison.get("per_field_accuracy_pct"),
            "cell_accuracy_pct": comparison.get("cell_accuracy_pct"),
            "row_accuracy_pct": comparison.get("row_accuracy_pct"),
            "reconciliation_pass_pct": recon_sum.get("pass_pct"),
            "reconciliation_fail_pct": recon_sum.get("fail_pct"),
            "column_shift_count": comparison.get("column_shift_count"),
            "blank_cell_accuracy_pct": comparison.get("blank_cell_accuracy_pct"),
            "processing_time_ms": round(float(ocr.latency_ms or 0.0), 1),
            "api_calls": int(
                (ocr.usage or {}).get("gemini_calls") or (1 if not ocr.error else 0)
            ),
            "usage": ocr.usage,
            "ocr_confidence": reconstructed.get("ocr_confidence"),
        },
        "bonnisan_user_briefed_score": briefed_cmp,
        "raw_provider_response": raw_summary,
    }


def run_benchmark(
    file_path: str | Path,
    *,
    providers: Sequence[str] = ("mistral",),
    expectations: Optional[Dict[str, Dict[str, Optional[float]]]] = None,
) -> Dict[str, Any]:
    path = Path(file_path)
    data = path.read_bytes()
    results = []
    for name in providers:
        logger.info("STOCK_OCR_BENCHMARK start provider=%s file=%s", name, path.name)
        results.append(
            run_provider_benchmark(
                data,
                filename=path.name,
                provider=name,
                expectations=expectations,
            )
        )

    by_provider = {r["provider"]: r for r in results}
    comparison = {
        "cell_accuracy": {
            p: (by_provider.get(p) or {}).get("metrics", {}).get("cell_accuracy_pct")
            for p in providers
        },
        "row_accuracy": {
            p: (by_provider.get(p) or {}).get("metrics", {}).get("row_accuracy_pct")
            for p in providers
        },
        "reconciliation_pass_rate": {
            p: (by_provider.get(p) or {}).get("metrics", {}).get(
                "reconciliation_pass_pct"
            )
            for p in providers
        },
        "column_shift_count": {
            p: (by_provider.get(p) or {}).get("metrics", {}).get("column_shift_count")
            for p in providers
        },
        "processing_time": {
            p: (by_provider.get(p) or {}).get("metrics", {}).get("processing_time_ms")
            for p in providers
        },
        "cost": {
            p: (by_provider.get(p) or {}).get("usage") for p in providers
        },
        "per_field_accuracy": {
            p: (by_provider.get(p) or {}).get("metrics", {}).get("per_field_accuracy_pct")
            for p in providers
        },
    }

    return {
        "document": str(path),
        "file": str(path),
        "providers": {p: by_provider.get(p) for p in providers},
        "provider_list": list(providers),
        "results": results,
        "comparison": comparison,
        "expectations_note": (
            "Primary expectations use crop-verified printed cells. "
            "BONNISAN DROPS Sale=318 Balance=185 (not briefed 618/0). "
            "See bonnisan_user_briefed_score for alternate scoring."
        ),
        "production_switched": False,
        "note": "Benchmark only — /extract-sales-statement unchanged.",
    }


def write_benchmark_reports(
    report: Dict[str, Any],
    *,
    json_path: str | Path = "/tmp/stock_statement_mistral_vs_gemini.json",
    txt_path: str | Path = "/tmp/stock_statement_mistral_vs_gemini.txt",
) -> Tuple[Path, Path]:
    json_p = Path(json_path)
    txt_p = Path(txt_path)
    json_p.write_text(json.dumps(report, indent=2, default=str))

    lines: List[str] = []
    lines.append("STOCK STATEMENT OCR BENCHMARK — Gemini vs Mistral")
    lines.append("=" * 72)
    lines.append(f"Document: {report.get('document')}")
    lines.append(f"Production switched: {report.get('production_switched')}")
    lines.append(str(report.get("expectations_note") or ""))
    lines.append("")
    lines.append(format_comparison_table(report))
    lines.append("")
    cmp_ = report.get("comparison") or {}
    lines.append("--- Aggregate comparison ---")
    for key in (
        "cell_accuracy",
        "row_accuracy",
        "reconciliation_pass_rate",
        "column_shift_count",
        "processing_time",
        "per_field_accuracy",
    ):
        lines.append(f"{key}: {json.dumps(cmp_.get(key), default=str)}")
    lines.append("")
    providers = report.get("providers") or {}
    for name, result in providers.items():
        if not result:
            continue
        lines.append(f"=== PROVIDER {name} ===")
        lines.append(
            f"status={result.get('status')} error={result.get('error')} "
            f"strategy={result.get('strategy')} latency_ms={result.get('provider_latency_ms')}"
        )
        lines.append(f"metrics={json.dumps(result.get('metrics'), default=str)}")
        for row in result.get("rows") or []:
            lines.append(json.dumps(row, ensure_ascii=False, default=str))
        lines.append("")
    txt_p.write_text("\n".join(lines))
    return json_p, txt_p


def format_comparison_table(report: Dict[str, Any]) -> str:
    lines = [
        "Provider | Product | Field | Expected | Extracted | Correct",
        "---|---|---|---:|---:|---",
    ]
    for result in report.get("results") or []:
        for row in (result.get("comparison") or {}).get("cell_rows") or []:
            lines.append(
                f"{row['provider']} | {row['product']} | {row['field']} | "
                f"{row['expected']} | {row['extracted']} | {row['correct']}"
            )
    return "\n".join(lines)
