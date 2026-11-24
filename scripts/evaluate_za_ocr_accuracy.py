"""Score Tesseract and PaddleOCR-VL on the source-verified ZA subset.

Primary quantity score uses a row-local window after the product name.
A number found anywhere else in the document is not counted as correct.
Expected zeros are reported separately and excluded from the primary quantity rate.
Unresolved and absent columns are not in the ground-truth files.
"""

from __future__ import annotations

import json
import math
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.ground_truth.matching import normalize_text

GT_DIR = ROOT / "phase4" / "za_ground_truth" / "per_document"
RUNS = {
    "tesseract": ROOT
    / "phase4"
    / "secondary_repair_canary"
    / "controlled_batch"
    / "20261013T385tess",
    "paddleocr-vl": ROOT
    / "phase4"
    / "secondary_repair_canary"
    / "controlled_batch"
    / "20261013T385vl",
}
OUT = ROOT / "phase4" / "za_ground_truth" / "evaluation"


def _norm_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", normalize_text(value))


def _tokens(text: str) -> List[str]:
    return re.findall(r"-?\d+(?:\.\d+)?", text or "")


def _window(text: str, product: str, later_products: List[str]) -> Optional[str]:
    hay = normalize_text(text)
    needle = normalize_text(product)
    idx = hay.find(needle)
    if idx < 0:
        compact_hay = _norm_name(text)
        compact_needle = _norm_name(product)
        cidx = compact_hay.find(compact_needle)
        if cidx < 0:
            return None
        # Compact match cannot recover a character offset reliably.
        # Fall back to a short scan around the first compact hit by
        # searching the original casefolded text for the first 12 letters.
        probe = normalize_text(product)[:12]
        idx = hay.find(probe)
        if idx < 0:
            return None
    end = idx + len(needle) + 220
    for other in later_products:
        other_norm = normalize_text(other)
        if not other_norm or other_norm == needle:
            continue
        pos = hay.find(other_norm, idx + len(needle))
        if pos > idx:
            end = min(end, pos)
    return hay[idx:end]


def _qty_hit(window: str, expected: float) -> bool:
    for token in _tokens(window):
        try:
            if abs(float(token) - float(expected)) < 1e-9:
                return True
        except ValueError:
            continue
    return False


def _load_text(run: Path, document_id: str) -> Tuple[str, str, Optional[float]]:
    stem = Path(document_id).stem
    cache = run / "primary_ocr_cache" / f"{stem}.json"
    per = run / "per_document" / f"{stem}.json"
    if not cache.is_file():
        raise FileNotFoundError(cache)
    payload = json.loads(cache.read_text(encoding="utf-8"))
    engine = str(payload.get("engine") or "")
    text = payload.get("text") or ""
    latency = None
    if per.is_file():
        row = json.loads(per.read_text(encoding="utf-8"))
        latency = row.get("primary_ms")
        if row.get("primary_engine") and row.get("primary_engine") != engine:
            engine = f"{engine}|artifact:{row.get('primary_engine')}"
    return engine, text, latency


def _score_doc(gt: Dict[str, Any], text: str) -> List[Dict[str, Any]]:
    products = [
        f["expected_raw"]
        for f in gt["fields"]
        if f["field_type"] == "text" and f["field_id"].endswith(".product_name")
    ]
    rows = []
    for field in gt["fields"]:
        kind = field["field_type"]
        expected_raw = str(field["expected_raw"])
        product = field.get("product_key")
        record = {
            "document_id": gt["document_id"],
            "layout": gt.get("layout"),
            "quality": gt.get("quality"),
            "field_id": field["field_id"],
            "field_type": kind,
            "expected_raw": expected_raw,
            "expected_normalized": field.get("expected_normalized"),
            "product_key": product,
            "zero_quantity": kind == "quantity" and float(field["expected_normalized"] or 0) == 0.0,
        }
        if kind in {"header", "text"} and not field["field_id"].endswith(".product_name") and kind != "header":
            target = normalize_text(expected_raw)
            record["correct"] = target in normalize_text(text)
            record["error"] = None if record["correct"] else "MISSING"
            record["scored"] = True
            rows.append(record)
            continue
        if kind == "header" or field["field_id"].endswith(".product_name") or field["field_id"].endswith(".packing"):
            target = normalize_text(expected_raw)
            compact_ok = _norm_name(expected_raw) in _norm_name(text)
            exact = target in normalize_text(text)
            record["correct"] = exact
            record["fuzzy_only"] = (not exact) and compact_ok
            record["error"] = None if exact else "MISSING"
            record["scored"] = True
            rows.append(record)
            continue
        if kind == "quantity":
            later = [name for name in products if name != product]
            window = _window(text, product or "", later)
            record["product_found"] = window is not None
            if window is None:
                record["correct"] = False
                record["error"] = "PRODUCT_MISSING"
                record["silent_wrong"] = False
            else:
                hit = _qty_hit(window, float(field["expected_normalized"]))
                record["correct"] = hit
                record["error"] = None if hit else "QTY_NOT_IN_ROW_WINDOW"
                others = [t for t in _tokens(window) if abs(float(t) - float(field["expected_normalized"])) >= 1e-9]
                record["silent_wrong"] = (not hit) and bool(others) and not record["zero_quantity"]
            record["scored"] = not record["zero_quantity"]
            rows.append(record)
            continue
        record["correct"] = False
        record["error"] = "UNSCORED_TYPE"
        record["scored"] = False
        rows.append(record)
    return rows


def _binom_mcnemar(b: int, c: int) -> Optional[float]:
    """Two-sided exact McNemar p-value on discordant pairs."""
    n = b + c
    if n == 0:
        return None
    k = min(b, c)
    total = 0.0
    for i in range(k + 1):
        total += math.comb(n, i)
    p = min(1.0, 2.0 * total / (2**n))
    return p


def _rate(correct: int, total: int) -> Optional[float]:
    if total == 0:
        return None
    return correct / total


def _summarize(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    def take(pred):
        chosen = [r for r in records if pred(r)]
        correct = sum(1 for r in chosen if r["correct"])
        return {"correct": correct, "total": len(chosen), "rate": _rate(correct, len(chosen))}

    scored_qty = [r for r in records if r["field_type"] == "quantity" and r["scored"]]
    products = [r for r in records if str(r["field_id"]).endswith(".product_name")]
    headers = [r for r in records if r["field_type"] == "header"]
    silent = sum(1 for r in scored_qty if r.get("silent_wrong"))
    by_layout = {}
    for layout in sorted({r["layout"] for r in records}):
        subset = [r for r in scored_qty if r["layout"] == layout]
        by_layout[layout] = take(lambda r, subset=subset: r in subset)
    docs = defaultdict(list)
    for r in products + scored_qty:
        docs[r["document_id"]].append(r)
    exact_docs = sum(1 for rows in docs.values() if rows and all(r["correct"] for r in rows))
    return {
        "product_name": take(lambda r: str(r["field_id"]).endswith(".product_name")),
        "packing": take(lambda r: str(r["field_id"]).endswith(".packing")),
        "header": take(lambda r: r["field_type"] == "header"),
        "quantity_nonzero": take(lambda r: r["field_type"] == "quantity" and r["scored"]),
        "quantity_zero_excluded_from_primary": take(
            lambda r: r["field_type"] == "quantity" and r.get("zero_quantity")
        ),
        "silent_wrong_nonzero_qty": {"count": silent, "denominator": len(scored_qty)},
        "document_exact": {"correct": exact_docs, "total": len(docs)},
        "by_layout_nonzero_qty": by_layout,
        "fuzzy_product_not_counted_correct": sum(1 for r in products if r.get("fuzzy_only")),
    }


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    gt_paths = sorted(GT_DIR.glob("*.json"))
    if not gt_paths:
        raise SystemExit("No ZA ground truth. Run scripts/validate_za_ground_truth.py first.")
    per_field = []
    engines_seen = {}
    latency = {}
    for path in gt_paths:
        gt = json.loads(path.read_text(encoding="utf-8"))
        doc_id = gt["document_id"]
        by_engine = {}
        for name, run in RUNS.items():
            engine, text, ms = _load_text(run, doc_id)
            if name == "tesseract" and engine != "tesseract":
                raise SystemExit(f"{doc_id} tesseract cache engine={engine}")
            if name == "paddleocr-vl" and "paddleocr-vl" not in engine:
                raise SystemExit(f"{doc_id} VL cache engine={engine}")
            engines_seen.setdefault(name, set()).add(engine)
            latency.setdefault(name, {})[doc_id] = ms
            scored = _score_doc(gt, text)
            by_engine[name] = scored
            for row in scored:
                row["engine_run"] = name
                row["cache_engine"] = engine
                per_field.append(row)
        # paired rows
        left = {r["field_id"]: r for r in by_engine["tesseract"]}
        right = {r["field_id"]: r for r in by_engine["paddleocr-vl"]}
        for field_id, a in left.items():
            b = right[field_id]
            if not a["scored"] or not b["scored"]:
                continue
            a["paired_other_correct"] = b["correct"]
            b["paired_other_correct"] = a["correct"]

    summary = {}
    for name in RUNS:
        summary[name] = _summarize([r for r in per_field if r["engine_run"] == name])

    def discordant(field_pred):
        b = c = both = neither = 0
        seen = set()
        for row in per_field:
            if row["engine_run"] != "tesseract" or not field_pred(row):
                continue
            key = (row["document_id"], row["field_id"])
            if key in seen:
                continue
            seen.add(key)
            other = next(
                r
                for r in per_field
                if r["engine_run"] == "paddleocr-vl"
                and r["document_id"] == row["document_id"]
                and r["field_id"] == row["field_id"]
            )
            if row["correct"] and other["correct"]:
                both += 1
            elif row["correct"] and not other["correct"]:
                b += 1
            elif other["correct"] and not row["correct"]:
                c += 1
            else:
                neither += 1
        return {
            "tesseract_only_correct": b,
            "paddle_only_correct": c,
            "both_correct": both,
            "both_wrong": neither,
            "mcnemar_exact_p": _binom_mcnemar(b, c),
        }

    paired = {
        "nonzero_quantity": discordant(
            lambda r: r["field_type"] == "quantity" and r["scored"]
        ),
        "product_name": discordant(lambda r: str(r["field_id"]).endswith(".product_name")),
    }
    report = {
        "documents": [p.name for p in gt_paths],
        "engines_seen": {k: sorted(v) for k, v in engines_seen.items()},
        "latency_ms": latency,
        "summary": summary,
        "paired": paired,
        "scoring": {
            "primary_quantity": "nonzero expected quantity must appear as its own number in the text window after the product name and before the next labeled product",
            "zeros": "labeled, excluded from primary quantity accuracy",
            "unresolved": "not written into the ground-truth files, so they are not scored",
            "fuzzy": "compact alphanumeric containment is reported and is not counted as exact product accuracy",
            "document_independence": "fields inside one document are dependent; McNemar here is field-level and overstates precision",
        },
        "fields": per_field,
    }
    (OUT / "za_accuracy_comparison.json").write_text(
        json.dumps(report, indent=2), encoding="utf-8"
    )
    print(json.dumps({"summary": summary, "paired": paired, "latency_ms": latency}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
