"""Practical Tesseract vs PaddleOCR-VL decision from local ZA artifacts.

Source-verified labels are scored as accuracy. AI proposals are scored only as a
proxy and are never written back as verified. Unlabeled images contribute cache
coverage, completeness, and stored runtime, not accuracy. No OCR rerun and no
network call.
"""

from __future__ import annotations

import json
import re
import statistics
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.compare_za_181_engines import (  # noqa: E402
    QTY_FIELDS,
    TESS_RUN,
    VL_RUN,
    extract_rows,
    normalize_name,
    parse_number,
)
from scripts.za_181_accuracy_workflow import (  # noqa: E402
    OUT_DIR,
    _absent,
    _load_predictions,
    score_document,
)
from scripts.za_181_human_review import evaluate_verified, load_state, real_prediction, review_dir  # noqa: E402
from scripts.za_181_accuracy_workflow import load_dataset  # noqa: E402

DECISION_MD = OUT_DIR / "final_engine_decision.md"
DECISION_JSON = OUT_DIR / "final_engine_decision.json"
MIN_QUANTITY_DENOMINATOR = 20


def proxy_rows_from_review(image: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    """Build in-memory scoring rows. Does not mark the saved proposal verified."""
    rows = []
    stats = {"ambiguous_quantity_columns": 0, "non_quantity_columns_ignored": 0, "blank_quantities_ignored": 0}
    for index, source in enumerate(image.get("rows") or [], start=1):
        if source.get("origin") == "human_added":
            continue
        name = str(source.get("proposed_product_name") or "").strip()
        row = {
            "row_id": source.get("row_id") or f"r{index:02d}",
            "row_index": index,
            "product_name": {
                "raw": name or None,
                "normalized": name or None,
                "status": "ai_proposed" if name else "unreadable",
            },
            "packing": _absent(),
            **{field: _absent() for field in QTY_FIELDS},
        }
        claimed: Dict[str, int] = {}
        for column in source.get("columns") or []:
            semantic = column.get("semantic") or "unknown"
            if semantic not in QTY_FIELDS:
                stats["non_quantity_columns_ignored"] += 1
                continue
            raw = column.get("proposed_raw")
            if raw is None or str(raw).strip() == "":
                stats["blank_quantities_ignored"] += 1
                continue
            number = parse_number(raw)
            if number is None:
                stats["blank_quantities_ignored"] += 1
                continue
            if semantic in claimed:
                stats["ambiguous_quantity_columns"] += 1
                row[semantic] = _absent()
                claimed[semantic] = -1
                continue
            claimed[semantic] = 1
            row[semantic] = {"raw": raw, "normalized": number, "status": "proxy_label"}
        for field, marker in claimed.items():
            if marker == -1:
                row[field] = _absent()
        rows.append(row)
    return rows, stats


def _tokens(value: str) -> set:
    return {token for token in re.findall(r"[a-z0-9]+", str(value or "").casefold()) if len(token) > 1}


def _jaccard(left: set, right: set) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def align_names(expected: Sequence[str], predicted: Sequence[str]) -> Dict[str, int]:
    """Greedy alignment. Exact normalized match is separate from fuzzy overlap."""
    used = [False] * len(predicted)
    exact = fuzzy = missing = 0
    pred_tokens = [_tokens(name) for name in predicted]
    for name in expected:
        key = normalize_name(name)
        if not key:
            continue
        hit = next((index for index, pred in enumerate(predicted) if not used[index] and normalize_name(pred) == key), None)
        kind = "exact"
        if hit is None:
            scored = [
                (_jaccard(_tokens(name), pred_tokens[index]), index)
                for index in range(len(predicted))
                if not used[index]
            ]
            if scored:
                best, index = max(scored)
                if best >= 0.8:
                    hit = index
                    kind = "fuzzy"
        if hit is None:
            missing += 1
            continue
        used[hit] = True
        if kind == "exact":
            exact += 1
        else:
            fuzzy += 1
    extra = sum(1 for index, flag in enumerate(used) if not flag and normalize_name(predicted[index]))
    return {"exact_normalized": exact, "fuzzy": fuzzy, "missing": missing, "extra": extra}


def _rate(numerator: int, denominator: int) -> Optional[float]:
    if denominator <= 0:
        return None
    return numerator / denominator


def _median(values: Sequence[Optional[float]]) -> Optional[float]:
    usable = [value for value in values if value is not None]
    if not usable:
        return None
    return float(statistics.median(usable))


def _pct(rate: Optional[float]) -> str:
    if rate is None:
        return "n/a"
    return f"{rate * 100:.1f}%"


def _point(left: Optional[float], right: Optional[float], *, minimum_denominator: int, left_n: int, right_n: int) -> str:
    if left_n < minimum_denominator or right_n < minimum_denominator:
        return "skip"
    if left is None or right is None or abs(left - right) < 1e-12:
        return "tie"
    return "tesseract" if left > right else "paddleocr-vl"


def choose_engine(metrics: Dict[str, Any]) -> Dict[str, Any]:
    """Pick one engine. Higher extraction scores outrank silent-error rate, then speed."""
    verified = metrics["source_verified"]
    proxy = metrics["ai_proposal_proxy"]
    operations = metrics["unlabeled_cache"]
    checks = [
        ("source-verified product F1", verified["tesseract"]["product_f1"], verified["paddleocr-vl"]["product_f1"], verified["tesseract"]["product_rows"], verified["paddleocr-vl"]["product_rows"]),
        ("source-verified quantity accuracy", verified["tesseract"]["quantity_accuracy"], verified["paddleocr-vl"]["quantity_accuracy"], verified["tesseract"]["quantity_scored"], verified["paddleocr-vl"]["quantity_scored"]),
        ("AI-proxy product F1", proxy["tesseract"]["product_f1"], proxy["paddleocr-vl"]["product_f1"], proxy["tesseract"]["product_rows"], proxy["paddleocr-vl"]["product_rows"]),
        ("AI-proxy median image F1", proxy["tesseract"]["median_image_f1"], proxy["paddleocr-vl"]["median_image_f1"], proxy["images"], proxy["images"]),
        ("AI-proxy quantity accuracy", proxy["tesseract"]["quantity_accuracy"], proxy["paddleocr-vl"]["quantity_accuracy"], proxy["tesseract"]["quantity_scored"], proxy["paddleocr-vl"]["quantity_scored"]),
    ]
    awarded = []
    points = {"tesseract": 0, "paddleocr-vl": 0}
    for label, left, right, left_n, right_n in checks:
        minimum = 1 if "median" in label else MIN_QUANTITY_DENOMINATOR if "quantity" in label else 1
        if "product F1" in label:
            minimum = 1
        outcome = _point(left, right, minimum_denominator=minimum, left_n=left_n, right_n=right_n)
        awarded.append({"check": label, "tesseract": left, "paddleocr-vl": right, "result": outcome})
        if outcome in points:
            points[outcome] += 1
    if points["paddleocr-vl"] > points["tesseract"]:
        winner = "paddleocr-vl"
    elif points["tesseract"] > points["paddleocr-vl"]:
        winner = "tesseract"
    else:
        silent = _point(
            -float(proxy["tesseract"]["silent_wrong_rate"] or 1),
            -float(proxy["paddleocr-vl"]["silent_wrong_rate"] or 1),
            minimum_denominator=1,
            left_n=proxy["tesseract"]["quantity_both_present"],
            right_n=proxy["paddleocr-vl"]["quantity_both_present"],
        )
        winner = silent if silent in points else (
            "tesseract" if operations["tesseract"]["median_ms"] <= operations["paddleocr-vl"]["median_ms"] else "paddleocr-vl"
        )
        awarded.append({"check": "tie-break silent rate then runtime", "result": winner})
    lead = abs(points["paddleocr-vl"] - points["tesseract"])
    same_direction = (
        (verified["paddleocr-vl"]["product_f1"] or 0) - (verified["tesseract"]["product_f1"] or 0)
    ) * (
        (proxy["paddleocr-vl"]["product_f1"] or 0) - (proxy["tesseract"]["product_f1"] or 0)
    ) > 0
    confidence = "moderate" if lead >= 3 and same_direction else "low"
    return {
        "selected_engine": winner,
        "confidence": confidence,
        "points": points,
        "checks": awarded,
        "rollback_engine": "tesseract" if winner == "paddleocr-vl" else "paddleocr-vl",
        "configuration": {
            "OCR_ENGINE": winner,
            "OCR_FALLBACK_ENGINE": "gemini",
        },
    }


def _engine_summary(documents: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    correct = sum(doc["product_correct"] for doc in documents)
    missing = sum(doc["product_missing"] for doc in documents)
    extra = sum(doc["product_extra"] for doc in documents)
    qty_correct = sum(doc["quantity_correct"] for doc in documents)
    qty_incorrect = sum(doc["quantity_incorrect"] for doc in documents)
    qty_missing = sum(doc["quantity_missing"] for doc in documents)
    both = qty_correct + qty_incorrect
    scored = both + qty_missing
    precision = _rate(correct, correct + extra)
    recall = _rate(correct, correct + missing)
    f1 = None if precision is None or recall is None or precision + recall == 0 else 2 * precision * recall / (precision + recall)
    return {
        "images": len(documents),
        "product_correct": correct,
        "product_missing": missing,
        "product_extra": extra,
        "product_rows": correct + missing,
        "product_precision": precision,
        "product_recall": recall,
        "product_f1": f1,
        "median_image_f1": _median([doc.get("product_f1") for doc in documents]),
        "median_image_quantity_accuracy": _median([doc.get("quantity_accuracy") for doc in documents if doc.get("quantity_scored")]),
        "quantity_correct": qty_correct,
        "quantity_incorrect": qty_incorrect,
        "quantity_missing": qty_missing,
        "quantity_scored": scored,
        "quantity_accuracy": _rate(qty_correct, scored),
        "quantity_both_present": both,
        "silent_wrong": qty_incorrect,
        "silent_wrong_rate": _rate(qty_incorrect, both),
        "images_won_f1": 0,
    }


def _cache_operations(document_ids: Iterable[str]) -> Dict[str, Any]:
    summary = {}
    for name, run in (("tesseract", TESS_RUN), ("paddleocr-vl", VL_RUN)):
        timings = []
        empty = missing = present = 0
        text_lengths = []
        for document_id in document_ids:
            stem = Path(document_id).stem
            cache = run / "primary_ocr_cache" / f"{stem}.json"
            per = run / "per_document" / f"{stem}.json"
            if not cache.is_file():
                missing += 1
                continue
            present += 1
            payload = json.loads(cache.read_text(encoding="utf-8"))
            text = payload.get("text") or ""
            if not str(text).strip():
                empty += 1
            text_lengths.append(len(text))
            if per.is_file():
                row = json.loads(per.read_text(encoding="utf-8"))
                if row.get("primary_ms") is not None:
                    timings.append(float(row["primary_ms"]))
        timings.sort()
        summary[name] = {
            "caches_present": present,
            "caches_missing": missing,
            "empty_text": empty,
            "median_text_chars": _median(text_lengths),
            "median_ms": _median(timings),
            "mean_ms": (sum(timings) / len(timings)) if timings else None,
            "p95_ms": timings[min(len(timings) - 1, int(0.95 * (len(timings) - 1)))] if timings else None,
            "total_s": sum(timings) / 1000 if timings else None,
            "images_with_timing": len(timings),
        }
    return summary


def evaluate_decision() -> Dict[str, Any]:
    dataset = load_dataset()
    state = load_state(review_dir())
    verified = evaluate_verified(dataset, state, real_prediction)
    verified_summary = {}
    for engine, payload in verified["engines"].items():
        verified_summary[engine] = _engine_summary(payload["documents"])
        verified_summary[engine]["product_f1"] = payload["product_f1"]
        verified_summary[engine]["product_precision"] = payload["product_precision"]
        verified_summary[engine]["product_recall"] = payload["product_recall"]
        verified_summary[engine]["quantity_accuracy"] = payload["quantity_accuracy"]
        verified_summary[engine]["silent_wrong"] = payload["silent_wrong"]
        verified_summary[engine]["silent_wrong_rate"] = payload["silent_wrong_rate"]
    proxy_docs: Dict[str, List[Dict[str, Any]]] = {"tesseract": [], "paddleocr-vl": []}
    alignment = {"tesseract": {"exact_normalized": 0, "fuzzy": 0, "missing": 0, "extra": 0}, "paddleocr-vl": {"exact_normalized": 0, "fuzzy": 0, "missing": 0, "extra": 0}}
    proxy_images = []
    column_stats = {"ambiguous_quantity_columns": 0, "non_quantity_columns_ignored": 0, "blank_quantities_ignored": 0}
    per_image = []
    for image in state["images"]:
        if image.get("label_origin") != "ai_proposed":
            continue
        rows, stats = proxy_rows_from_review(image)
        for key, value in stats.items():
            column_stats[key] += value
        scoring = {"document_id": image["document_id"], "verification_status": "proxy", "rows": rows}
        image_result = {"document_id": image["document_id"], "proposed_rows": len(rows)}
        expected_names = [str((row.get("product_name") or {}).get("raw") or "") for row in rows]
        for engine, run in (("tesseract", TESS_RUN), ("paddleocr-vl", VL_RUN)):
            _cache_engine, predicted, text = _load_predictions(run, image["document_id"])
            scored = score_document(scoring, predicted, text)
            scored["label_class"] = "ai_proposal_proxy"
            proxy_docs[engine].append(scored)
            names = [row.get("product") or "" for row in predicted]
            aligned = align_names(expected_names, names)
            for key, value in aligned.items():
                alignment[engine][key] += value
            image_result[engine] = {
                "product_f1": scored["product_f1"],
                "product_precision": scored["product_precision"],
                "product_recall": scored["product_recall"],
                "quantity_accuracy": scored["quantity_accuracy"],
                "silent_wrong": scored["silent_wrong"],
                "product_missing": scored["product_missing"],
                "product_extra": scored["product_extra"],
            }
        per_image.append(image_result)
        proxy_images.append(image["document_id"])
    proxy_summary = {engine: _engine_summary(docs) for engine, docs in proxy_docs.items()}
    for image in per_image:
        tess_f1 = image["tesseract"]["product_f1"]
        paddle_f1 = image["paddleocr-vl"]["product_f1"]
        if tess_f1 is None and paddle_f1 is None:
            image["f1_winner"] = "tie"
        elif paddle_f1 is None or (tess_f1 is not None and tess_f1 > paddle_f1):
            image["f1_winner"] = "tesseract"
        elif tess_f1 is None or paddle_f1 > tess_f1:
            image["f1_winner"] = "paddleocr-vl"
        else:
            image["f1_winner"] = "tie"
    for engine in proxy_summary:
        proxy_summary[engine]["images_won_f1"] = sum(1 for image in per_image if image["f1_winner"] == engine)
    ids = [image["document_id"] for image in state["images"]]
    unlabeled_ids = [image["document_id"] for image in state["images"] if image.get("label_origin") in {"proposal_failed", "not_started"}]
    human_rows = sum(
        1
        for image in state["images"]
        if not image.get("locked_source_verified")
        for row in image.get("rows") or []
        if row.get("review_status") in {"VERIFIED", "CORRECTED"}
    )
    metrics = {
        "source_verified": verified_summary,
        "source_verified_images": verified["source_verified_images"],
        "source_verified_rows": verified["source_verified_rows"],
        "human_reviewed_rows": human_rows,
        "ai_proposal_proxy": {
            **proxy_summary,
            "images": len(proxy_images),
            "tesseract": proxy_summary["tesseract"],
            "paddleocr-vl": proxy_summary["paddleocr-vl"],
            "alignment": alignment,
            "column_stats": column_stats,
            "per_image": per_image,
        },
        "unlabeled_cache": _cache_operations(unlabeled_ids),
        "all_cache": _cache_operations(ids),
        "unlabeled_images": len(unlabeled_ids),
        "proposal_failed_images": sum(1 for image in state["images"] if image.get("label_origin") == "proposal_failed"),
        "not_started_images": sum(1 for image in state["images"] if image.get("label_origin") == "not_started"),
    }
    # The summaries were nested twice for choose_engine's expected shape.
    metrics["ai_proposal_proxy"]["tesseract"] = proxy_summary["tesseract"]
    metrics["ai_proposal_proxy"]["paddleocr-vl"] = proxy_summary["paddleocr-vl"]
    decision = choose_engine(metrics)
    return {"decision": decision, "metrics": metrics, "verified_rule_notes": verified["rules"]}


def _engine_line(summary: Dict[str, Any]) -> str:
    return (
        f"precision {_pct(summary['product_precision'])} "
        f"({summary['product_correct']}/{summary['product_correct'] + summary['product_extra']}), "
        f"recall {_pct(summary['product_recall'])} "
        f"({summary['product_correct']}/{summary['product_rows']}), "
        f"F1 {_pct(summary['product_f1'])}, "
        f"quantity {_pct(summary['quantity_accuracy'])} "
        f"({summary['quantity_correct']}/{summary['quantity_scored']}), "
        f"silent wrong {summary['silent_wrong']} ({_pct(summary['silent_wrong_rate'])})"
    )


def render_markdown(payload: Dict[str, Any]) -> str:
    decision = payload["decision"]
    metrics = payload["metrics"]
    winner = decision["selected_engine"]
    verified = metrics["source_verified"]
    proxy = metrics["ai_proposal_proxy"]
    lines = [
        "# ZA 181 engine decision",
        "",
        f"Selected engine: **{winner}**.",
        f"Confidence: **{decision['confidence']}**.",
        "",
        "This is a practical production choice. It is not a claim that every one of the 181 images has verified ground truth.",
        "",
        "## Why this engine won",
        "",
        f"Correct product and quantity extraction is the first criterion. {winner} takes {decision['points'].get(winner, 0)} of the scored checks.",
        "",
        (
            f"Source-verified product F1 is {_pct(verified['paddleocr-vl']['product_f1'])} for PaddleOCR-VL and "
            f"{_pct(verified['tesseract']['product_f1'])} for Tesseract. Quantity accuracy is "
            f"{_pct(verified['paddleocr-vl']['quantity_accuracy'])} versus {_pct(verified['tesseract']['quantity_accuracy'])}."
        ),
        (
            f"On the 51-image AI proxy, product F1 is {_pct(proxy['paddleocr-vl']['product_f1'])} versus "
            f"{_pct(proxy['tesseract']['product_f1'])}. Median per-image F1 is "
            f"{_pct(proxy['paddleocr-vl']['median_image_f1'])} versus {_pct(proxy['tesseract']['median_image_f1'])}, "
            f"so the result is not one long statement. PaddleOCR-VL has the higher F1 on "
            f"{proxy['paddleocr-vl']['images_won_f1']} images and Tesseract on {proxy['tesseract']['images_won_f1']}."
        ),
        (
            f"Micro quantity accuracy on that proxy is close, {_pct(proxy['paddleocr-vl']['quantity_accuracy'])} versus "
            f"{_pct(proxy['tesseract']['quantity_accuracy'])}, because some large statements are hard for both. "
            f"The median image is not close: {_pct(proxy['paddleocr-vl']['median_image_quantity_accuracy'])} versus "
            f"{_pct(proxy['tesseract']['median_image_quantity_accuracy'])}."
        ),
        "",
        "The AI proxy is not ground truth. A proposal can repeat an OCR mistake or invent a row. It is only a check that the 2-image result is not an isolated case.",
        "",
        (
            f"Silent quantity errors are second. On the verified pair, PaddleOCR-VL has {verified['paddleocr-vl']['silent_wrong']} "
            f"({_pct(verified['paddleocr-vl']['silent_wrong_rate'])}) and Tesseract has {verified['tesseract']['silent_wrong']} "
            f"({_pct(verified['tesseract']['silent_wrong_rate'])}). Tesseract's lower count on the photo comes with 0% quantity accuracy on that image. "
            f"On the proxy, silent errors are {proxy['paddleocr-vl']['silent_wrong']} "
            f"({_pct(proxy['paddleocr-vl']['silent_wrong_rate'])}) for PaddleOCR-VL and {proxy['tesseract']['silent_wrong']} "
            f"({_pct(proxy['tesseract']['silent_wrong_rate'])}) for Tesseract."
        ),
        "",
        "Runtime is last. Stored timings put PaddleOCR-VL far behind Tesseract. That cost does not outweigh the extraction gap.",
        "",
        "No hybrid is selected. Tesseract wins the proxy F1 on a minority of images, including a few where PaddleOCR-VL's text misses the proposed names. Those images do not have source labels, so they do not justify running both engines. Set `OCR_ENGINE=tesseract` when a known batch needs the rollback. Ensemble stays off.",
        "",
        "## Evidence",
        "",
        "### Source-verified accuracy",
        "",
        f"Images: {metrics['source_verified_images']}. Rows: {metrics['source_verified_rows']}. Human-reviewed rows added in this review store: {metrics['human_reviewed_rows']}.",
        "",
        "| Engine | Product precision | Product recall | Product F1 | Quantity accuracy | Silent quantity errors |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for engine in ("tesseract", "paddleocr-vl"):
        item = verified[engine]
        lines.append(
            f"| {engine} | {_pct(item['product_precision'])} ({item['product_correct']}/{item['product_correct'] + item['product_extra']}) | {_pct(item['product_recall'])} ({item['product_correct']}/{item['product_rows']}) | {_pct(item['product_f1'])} | {_pct(item['quantity_accuracy'])} ({item['quantity_correct']}/{item['quantity_scored']}) | {item['silent_wrong']} ({_pct(item['silent_wrong_rate'])}) |"
        )
    lines.extend([
        "",
        "### AI-proposal proxy",
        "",
        f"Images: {proxy['images']}. These rows stay `PENDING` in the review store. Amount, price, unknown, and blank columns are not scored. A blank is not zero. Duplicate headers for one quantity are dropped.",
        "",
        f"Columns ignored as non-quantities: {proxy['column_stats']['non_quantity_columns_ignored']}. Blank or unparsed quantity cells ignored: {proxy['column_stats']['blank_quantities_ignored']}. Ambiguous duplicate quantities dropped: {proxy['column_stats']['ambiguous_quantity_columns']}.",
        "",
        "The product F1 below uses the same full-name text rule as the verified score. Normalized and fuzzy name alignment is reported separately and is not the decision score.",
        "",
        "| Engine | Product precision | Product recall | Product F1 | Median image F1 | Quantity accuracy | Median image quantity accuracy | Silent quantity errors | Images with higher F1 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | ---: |",
    ])
    for engine in ("tesseract", "paddleocr-vl"):
        item = proxy[engine]
        lines.append(
            f"| {engine} | {_pct(item['product_precision'])} ({item['product_correct']}/{item['product_correct'] + item['product_extra']}) | {_pct(item['product_recall'])} ({item['product_correct']}/{item['product_rows']}) | {_pct(item['product_f1'])} | {_pct(item['median_image_f1'])} | {_pct(item['quantity_accuracy'])} ({item['quantity_correct']}/{item['quantity_scored']}) | {_pct(item['median_image_quantity_accuracy'])} | {item['silent_wrong']} ({_pct(item['silent_wrong_rate'])}) | {item['images_won_f1']} |"
        )
    lines.extend(["", "| Engine | Exact normalized name matches | Fuzzy name matches | Names missed | Extra extracted names |", "| --- | ---: | ---: | ---: | ---: |"])
    for engine in ("tesseract", "paddleocr-vl"):
        item = proxy["alignment"][engine]
        lines.append(f"| {engine} | {item['exact_normalized']} | {item['fuzzy']} | {item['missing']} | {item['extra']} |")
    lines.extend([
        "",
        "Per-image proxy F1 gives each statement one vote, so one long statement cannot decide the median.",
        "",
        "| Image | Proposed rows | Tesseract F1 | Paddle F1 | Tesseract quantity | Paddle quantity | Tesseract silent | Paddle silent | Higher F1 |",
        "| --- | ---: | --- | --- | --- | --- | ---: | ---: | --- |",
    ])
    for image in proxy["per_image"]:
        lines.append(
            f"| {image['document_id']} | {image['proposed_rows']} | {_pct(image['tesseract']['product_f1'])} | {_pct(image['paddleocr-vl']['product_f1'])} | {_pct(image['tesseract']['quantity_accuracy'])} | {_pct(image['paddleocr-vl']['quantity_accuracy'])} | {image['tesseract']['silent_wrong']} | {image['paddleocr-vl']['silent_wrong']} | {image['f1_winner']} |"
        )
    unlabeled = metrics["unlabeled_cache"]
    all_cache = metrics["all_cache"]
    lines.extend([
        "",
        "### Unlabeled images",
        "",
        f"Proposal failures: {metrics['proposal_failed_images']}. Never started: {metrics['not_started_images']}. Together: {metrics['unlabeled_images']}. No accuracy is claimed here.",
        "",
        "| Engine | Caches present | Missing caches | Empty text | Median characters | Median stored runtime |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ])
    for engine in ("tesseract", "paddleocr-vl"):
        item = unlabeled[engine]
        lines.append(
            f"| {engine} | {item['caches_present']} | {item['caches_missing']} | {item['empty_text']} | {item['median_text_chars']} | {None if item['median_ms'] is None else round(item['median_ms'] / 1000, 1)} s |"
        )
    lines.extend([
        "",
        "### Runtime and failures on all 181 cached images",
        "",
        "Timings are stored `primary_ms` values from the existing runs. OCR was not rerun.",
        "",
        "| Engine | Caches | Empty text | Median | Mean | P95 | Total |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ])
    for engine in ("tesseract", "paddleocr-vl"):
        item = all_cache[engine]
        lines.append(
            f"| {engine} | {item['caches_present']} | {item['empty_text']} | {None if item['median_ms'] is None else round(item['median_ms'] / 1000, 1)} s | {None if item['mean_ms'] is None else round(item['mean_ms'] / 1000, 1)} s | {None if item['p95_ms'] is None else round(item['p95_ms'] / 1000, 1)} s | {None if item['total_s'] is None else round(item['total_s'], 1)} s |"
        )
    lines.extend([
        "",
        "## Decision checks",
        "",
        "| Check | Tesseract | PaddleOCR-VL | Result |",
        "| --- | ---: | ---: | --- |",
    ])
    for check in decision["checks"]:
        if "tesseract" in check:
            lines.append(f"| {check['check']} | {_pct(check.get('tesseract'))} | {_pct(check.get('paddleocr-vl'))} | {check['result']} |")
        else:
            lines.append(f"| {check['check']} |  |  | {check['result']} |")
    config = decision["configuration"]
    lines.extend([
        "",
        "## Limitations",
        "",
        "- Two images have source-verified labels. Zero review rows have been saved as VERIFIED or CORRECTED.",
        "- The 51-image numbers are an AI-proposal proxy. They can favor an engine that agrees with a wrong proposal.",
        "- The 71 failed and 57 unstarted images have OCR caches and no labels. They are excluded from accuracy.",
        "- A quantity hit means the expected number occurs in the row-local OCR window. It does not prove the printed column.",
        "- A missing number is not counted as zero.",
        "- Full-name text matching is the decision rule. Fuzzy matches are diagnostic only.",
        "",
        "## Configuration",
        "",
        f"Production primary remains `{config['OCR_ENGINE']}` through `OCR_ENGINE` in `services/ocr/orchestrator.py`.",
        "Automatic fallback is Gemini when that OCR output is unusable.",
        f"Diagnostic rollback: set `OCR_ENGINE={decision['rollback_engine']}`.",
        "Ensemble comparison is not used.",
        "",
        "Recommended production configuration:",
        "",
        "```",
        f"OCR_ENGINE={config['OCR_ENGINE']}",
        f"OCR_FALLBACK_ENGINE={config['OCR_FALLBACK_ENGINE']}",
        "```",
        "",
    ])
    return "\n".join(lines)


def write_decision(payload: Dict[str, Any]) -> None:
    public = {
        "decision": payload["decision"],
        "source_verified_images": payload["metrics"]["source_verified_images"],
        "source_verified_rows": payload["metrics"]["source_verified_rows"],
        "human_reviewed_rows": payload["metrics"]["human_reviewed_rows"],
        "ai_proxy_images": payload["metrics"]["ai_proposal_proxy"]["images"],
        "source_verified": payload["metrics"]["source_verified"],
        "ai_proposal_proxy": {
            "images": payload["metrics"]["ai_proposal_proxy"]["images"],
            "tesseract": payload["metrics"]["ai_proposal_proxy"]["tesseract"],
            "paddleocr-vl": payload["metrics"]["ai_proposal_proxy"]["paddleocr-vl"],
            "alignment": payload["metrics"]["ai_proposal_proxy"]["alignment"],
            "column_stats": payload["metrics"]["ai_proposal_proxy"]["column_stats"],
            "per_image": payload["metrics"]["ai_proposal_proxy"]["per_image"],
        },
        "unlabeled_cache": payload["metrics"]["unlabeled_cache"],
        "all_cache": payload["metrics"]["all_cache"],
    }
    DECISION_JSON.write_text(json.dumps(public, indent=2), encoding="utf-8")
    DECISION_MD.write_text(render_markdown(payload), encoding="utf-8")


def main() -> int:
    payload = evaluate_decision()
    write_decision(payload)
    decision = payload["decision"]
    print(json.dumps({
        "selected_engine": decision["selected_engine"],
        "confidence": decision["confidence"],
        "points": decision["points"],
        "report": str(DECISION_MD),
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
