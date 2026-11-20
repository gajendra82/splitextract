#!/usr/bin/env python3
"""Bucket Phase-0 shadow decisions from production logs / journal exports.

Reads lines containing ``ROW_CLASSIFY_FINAL`` (preferred) or joins
``ROW_CLASSIFY_SHADOW`` + ``FALLBACK_DECISION_CURRENT`` on ``request_id``.

Four buckets
------------
silent_error
    Identity/classifier would fall back, but Gemini was never called.
    This is the Phase-1 win size.
correctly_kept
    Classifier OK and Gemini not called.
correctly_fallback
    Classifier would fall back and Gemini was called.
unexpected_gemini
    Classifier OK but Gemini was still called.

Usage
-----
  python scripts/shadow_report.py /var/log/splitextract/*.log
  journalctl -u split-extract --since today | python scripts/shadow_report.py -
  python scripts/shadow_report.py journal.txt --json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

_KV = re.compile(r"(\w+)=(\S+)")

_GEMINI_CALLED = frozenset(
    {
        "success",
        "early_success",
        "used",
        "replaced",
        "gemini",
        "gemini_vision",
        "gemini_fallback",
        "gemini_vision_fallback",
    }
)
_GEMINI_NOT_CALLED = frozenset(
    {
        "not_called",
        "not_evaluated",
        "unavailable",
        "unavailable_text_only",
        "disabled",
        "keep_parser",
        "skip_fallback",
        "structured",
        "pre_gate",
        "-",
        "",
    }
)


def _parse_kv(line: str) -> Dict[str, str]:
    return {m.group(1): m.group(2) for m in _KV.finditer(line)}


def _truthy(value: Optional[str]) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _gemini_was_called(fields: Dict[str, str]) -> bool:
    gf = str(fields.get("gemini_fallback") or "").strip().lower()
    decision = str(fields.get("current_decision") or fields.get("decision") or "").strip().lower()
    final_selected = str(fields.get("final_selected") or "").strip().lower()

    for token in (gf, decision, final_selected):
        if token in _GEMINI_CALLED:
            return True
        if "gemini" in token and "not_called" not in token and "unavailable" not in token:
            return True
        if token == "gemini_vision_fallback" or token.endswith("_success"):
            return True

    if gf in _GEMINI_NOT_CALLED and decision in _GEMINI_NOT_CALLED:
        return False
    if gf in _GEMINI_NOT_CALLED:
        return False
    if decision in {"keep_parser", "skip_fallback", "unavailable"}:
        return False
    if gf and gf not in _GEMINI_NOT_CALLED:
        # Unknown non-empty stamp that is not an explicit skip → treat as called.
        return True
    return False


def _bucket(would_fallback: bool, gemini_called: bool) -> str:
    if would_fallback and not gemini_called:
        return "silent_error"
    if not would_fallback and not gemini_called:
        return "correctly_kept"
    if would_fallback and gemini_called:
        return "correctly_fallback"
    return "unexpected_gemini"


def _iter_lines(paths: List[str]) -> Iterable[str]:
    if not paths or paths == ["-"]:
        for line in sys.stdin:
            yield line
        return
    for path in paths:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
        for line in text.splitlines():
            yield line


def collect_events(lines: Iterable[str]) -> Dict[str, Dict[str, str]]:
    """Map request_id (or file fallback key) -> merged fields from log lines."""
    by_id: Dict[str, Dict[str, str]] = {}
    shadow_by_id: Dict[str, Dict[str, str]] = {}
    decision_by_id: Dict[str, Dict[str, str]] = {}

    for raw in lines:
        line = raw.rstrip("\n")
        if "ROW_CLASSIFY_FINAL" in line:
            fields = _parse_kv(line)
            rid = fields.get("request_id") or fields.get("file") or f"anon-{len(by_id)}"
            # Prefer FINAL when present.
            prev = by_id.get(rid) or {}
            prev.update(fields)
            prev["_source"] = "final"
            by_id[rid] = prev
            continue
        if "ROW_CLASSIFY_SHADOW" in line:
            fields = _parse_kv(line)
            # Skip pure pre_gate lines when we will also see FINAL; still store.
            rid = fields.get("request_id") or fields.get("file") or f"shadow-{len(shadow_by_id)}"
            shadow_by_id[rid] = fields
            continue
        if "FALLBACK_DECISION_CURRENT" in line:
            fields = _parse_kv(line)
            rid = fields.get("request_id") or fields.get("file") or f"decision-{len(decision_by_id)}"
            decision_by_id[rid] = fields
            continue

    # Seed from FINAL first.
    for rid, fields in list(by_id.items()):
        if rid in decision_by_id and "decision" not in fields:
            fields["decision"] = decision_by_id[rid].get("decision", "")

    # Join SHADOW + DECISION when FINAL missing.
    for rid, shadow in shadow_by_id.items():
        if rid in by_id and by_id[rid].get("_source") == "final":
            continue
        merged = dict(shadow)
        decision = decision_by_id.get(rid) or {}
        if decision:
            merged["decision"] = decision.get("decision", "")
            merged.setdefault("gemini_fallback", decision.get("decision", ""))
            if decision.get("reason"):
                merged["decision_reason"] = decision["reason"]
        # Prefer post-gate shadow current_decision over pre_gate.
        if str(merged.get("current_decision") or "").lower() == "pre_gate":
            if decision.get("decision"):
                merged["current_decision"] = decision["decision"]
        by_id[rid] = merged

    return by_id


def classify_events(
    events: Dict[str, Dict[str, str]],
) -> Tuple[Dict[str, List[Dict[str, str]]], Dict[str, int]]:
    buckets: Dict[str, List[Dict[str, str]]] = defaultdict(list)
    for rid, fields in events.items():
        would = _truthy(fields.get("would_fallback"))
        called = _gemini_was_called(fields)
        name = _bucket(would, called)
        row = {
            "request_id": rid,
            "file": fields.get("file", "-"),
            "would_fallback": would,
            "gemini_called": called,
            "current_decision": fields.get("current_decision") or fields.get("decision") or "-",
            "gemini_fallback": fields.get("gemini_fallback") or "-",
            "reason": fields.get("reason") or "-",
            "parser": fields.get("parser") or "-",
            "kind": fields.get("kind") or "-",
            "valid_ratio": fields.get("valid_ratio") or "-",
        }
        buckets[name].append(row)
    counts = {k: len(v) for k, v in buckets.items()}
    for key in (
        "silent_error",
        "correctly_kept",
        "correctly_fallback",
        "unexpected_gemini",
    ):
        counts.setdefault(key, 0)
    return buckets, counts


def collect_native_shadow(lines: Iterable[str]) -> List[Dict[str, str]]:
    """Collect NATIVE_RESOLVER_SHADOW log lines for the Phase 3b report section."""
    rows: List[Dict[str, str]] = []
    for raw in lines:
        if "NATIVE_RESOLVER_SHADOW" not in raw:
            continue
        fields = _parse_kv(raw.rstrip("\n"))
        rows.append(fields)
    return rows


def summarise_native(rows: List[Dict[str, str]]) -> Dict[str, Any]:
    by_parser: Dict[str, List[Dict[str, str]]] = defaultdict(list)
    for row in rows:
        by_parser[str(row.get("parser") or "-")].append(row)

    sections: Dict[str, Any] = {}
    for parser, items in sorted(by_parser.items()):
        resolved_n = sum(
            1 for r in items if str(r.get("resolved") or "").lower() in {"true", "1"}
        )
        old_ratios: List[float] = []
        new_ratios: List[float] = []
        coverages: List[float] = []
        field_diff_totals: Dict[str, int] = defaultdict(int)
        improved: List[Dict[str, str]] = []
        worsened: List[Dict[str, str]] = []
        for r in items:
            try:
                old_r = float(r.get("old_valid_ratio") or 0)
                new_r = float(r.get("new_valid_ratio") or 0)
            except (TypeError, ValueError):
                old_r, new_r = 0.0, 0.0
            old_ratios.append(old_r)
            new_ratios.append(new_r)
            try:
                coverages.append(float(r.get("coverage") or 0))
            except (TypeError, ValueError):
                coverages.append(0.0)
            try:
                diffs = json.loads(r.get("field_diffs") or "{}")
            except Exception:
                diffs = {}
            if isinstance(diffs, dict):
                for k, v in diffs.items():
                    if k.startswith("_"):
                        continue
                    try:
                        field_diff_totals[k] += int(v)
                    except (TypeError, ValueError):
                        pass
            entry = {
                "request_id": r.get("request_id") or "-",
                "file": r.get("file") or "-",
                "old_valid_ratio": old_r,
                "new_valid_ratio": new_r,
                "coverage": coverages[-1],
            }
            if new_r > old_r:
                improved.append(entry)
            elif new_r < old_r:
                worsened.append(entry)
        improved.sort(key=lambda e: e["new_valid_ratio"] - e["old_valid_ratio"], reverse=True)
        worsened.sort(key=lambda e: e["old_valid_ratio"] - e["new_valid_ratio"], reverse=True)
        top_fields = sorted(
            field_diff_totals.items(), key=lambda kv: -kv[1]
        )[:10]
        sections[parser] = {
            "files": len(items),
            "resolved_pct": round(100.0 * resolved_n / len(items), 2) if items else 0.0,
            "mean_old_valid_ratio": round(sum(old_ratios) / len(old_ratios), 4)
            if old_ratios
            else 0.0,
            "mean_new_valid_ratio": round(sum(new_ratios) / len(new_ratios), 4)
            if new_ratios
            else 0.0,
            "mean_coverage": round(sum(coverages) / len(coverages), 4)
            if coverages
            else 0.0,
            "top_field_diffs": top_fields,
            "improved_examples": improved[:20],
            "worsened_examples": worsened[:20],
        }
    return sections


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "paths",
        nargs="*",
        default=["-"],
        help="Log files, or '-' for stdin (default)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON",
    )
    parser.add_argument(
        "--list-bucket",
        choices=(
            "silent_error",
            "correctly_kept",
            "correctly_fallback",
            "unexpected_gemini",
            "all",
        ),
        default="all",
        help="Which bucket to list (default: all summary)",
    )
    args = parser.parse_args(argv)

    all_lines = list(_iter_lines(list(args.paths)))
    events = collect_events(all_lines)
    buckets, counts = classify_events(events)
    total = sum(counts.values())
    native_rows = collect_native_shadow(all_lines)
    native = summarise_native(native_rows)

    if args.json:
        payload = {
            "total": total,
            "counts": counts,
            "silent_error_rate": (
                round(counts["silent_error"] / total, 4) if total else 0.0
            ),
            "buckets": {k: buckets.get(k, []) for k in counts},
            "native": native,
        }
        if args.list_bucket != "all":
            payload["buckets"] = {args.list_bucket: buckets.get(args.list_bucket, [])}
        json.dump(payload, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 0

    print(f"events={total}")
    print(
        f"silent_error={counts['silent_error']}  "
        f"correctly_kept={counts['correctly_kept']}  "
        f"correctly_fallback={counts['correctly_fallback']}  "
        f"unexpected_gemini={counts['unexpected_gemini']}"
    )
    if total:
        print(
            f"silent_error_rate={counts['silent_error'] / total:.2%}  "
            "(identity failed, Gemini never called — Phase 1 target)"
        )

    show = (
        list(counts.keys())
        if args.list_bucket == "all"
        else [args.list_bucket]
    )
    for name in show:
        rows = buckets.get(name) or []
        if not rows:
            continue
        print(f"\n## {name} ({len(rows)})")
        for row in rows[:50]:
            print(
                f"  request_id={row['request_id']} file={row['file']} "
                f"would_fallback={row['would_fallback']} "
                f"decision={row['current_decision']} "
                f"gemini_fallback={row['gemini_fallback']} "
                f"reason={row['reason']}"
            )
        if len(rows) > 50:
            print(f"  ... {len(rows) - 50} more")

    if native:
        print("\n## native")
        for parser_name, sec in native.items():
            print(
                f"  parser={parser_name} files={sec['files']} "
                f"resolved_pct={sec['resolved_pct']} "
                f"mean_old={sec['mean_old_valid_ratio']} "
                f"mean_new={sec['mean_new_valid_ratio']} "
                f"mean_coverage={sec.get('mean_coverage', 0)}"
            )
            if sec["top_field_diffs"]:
                print(f"    top_field_diffs={sec['top_field_diffs']}")
            if sec["improved_examples"]:
                print("    improved (new>old):")
                for ex in sec["improved_examples"][:5]:
                    print(
                        f"      request_id={ex['request_id']} "
                        f"old={ex['old_valid_ratio']} new={ex['new_valid_ratio']}"
                    )
            if sec["worsened_examples"]:
                print("    worsened (new<old):")
                for ex in sec["worsened_examples"][:5]:
                    print(
                        f"      request_id={ex['request_id']} "
                        f"old={ex['old_valid_ratio']} new={ex['new_valid_ratio']}"
                    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
