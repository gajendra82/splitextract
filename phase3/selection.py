"""Pick a representative evaluation batch. Not the first alphabetical files."""

from __future__ import annotations

import hashlib
from collections import defaultdict
from typing import Any, Dict, List


def stratum(record: Dict[str, Any]) -> str:
    kind = str(record.get("file_kind") or "")
    pdf_kind = str(record.get("pdf_kind") or "")
    extension = str(record.get("extension") or "")
    if kind == "pdf":
        if pdf_kind == "digital":
            return "pdf_digital"
        if pdf_kind == "scanned":
            return "pdf_scanned"
        return "pdf_other"
    if kind == "image" or extension in {".jpg", ".jpeg", ".png", ".heic"}:
        return "image"
    if extension == ".xlsx" or kind == "xlsx":
        return "xlsx"
    if extension == ".xls" or kind == "xls":
        return "xls"
    if kind in {"doc", "docx"} or extension in {".doc", ".docx"}:
        return "word"
    if kind in {"txt", "html"} or extension in {".txt", ".csv", ".rtf"}:
        return "text"
    return "other"


def page_bucket(record: Dict[str, Any]) -> str:
    count = record.get("page_count")
    if not isinstance(count, int) or count <= 0:
        return "na"
    if count == 1:
        return "1"
    if count <= 3:
        return "2-3"
    return "4+"


def _stable_key(record: Dict[str, Any]) -> str:
    text = str(record.get("relative_path") or record.get("filename") or "")
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _spread(records: List[Dict[str, Any]], limit: int) -> List[Dict[str, Any]]:
    buckets: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for record in sorted(records, key=_stable_key):
        buckets[page_bucket(record)].append(record)
    keys = sorted(buckets)
    picked: List[Dict[str, Any]] = []
    index = 0
    while len(picked) < limit and any(buckets.values()):
        key = keys[index % len(keys)]
        if buckets[key]:
            picked.append(buckets[key].pop(0))
        index += 1
        if index > (limit + 1) * max(len(keys), 1) + 5:
            break
    return picked


def _prefer_prior(records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    rank = {"FAIL": 0, "PASS": 1, "REVIEW": 2, "ERROR": 3}
    return sorted(
        records,
        key=lambda row: (
            rank.get(str(row.get("prior_qa_status") or ""), 4),
            _stable_key(row),
        ),
    )


def select_batch(records: List[Dict[str, Any]], limit: int = 100) -> List[Dict[str, Any]]:
    """Cover formats, OCR, page counts, and prior QA outcomes.

    Slots are reserved by router stratum first. Inside a stratum, previously
    QA-failed and QA-passed files are preferred, then page-count spread.
    """
    if limit <= 0 or not records:
        return []
    grouped: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for record in records:
        grouped[stratum(record)].append(record)

    floors = {
        "pdf_digital": 20,
        "pdf_scanned": 15,
        "pdf_other": 5,
        "image": 25,
        "xlsx": 10,
        "xls": 8,
        "word": 4,
        "text": 3,
        "other": 5,
    }
    present = [name for name in floors if grouped.get(name)]
    budget = {name: min(floors[name], len(grouped[name])) for name in present}
    while sum(budget.values()) > limit and budget:
        name = max(budget, key=lambda item: budget[item])
        if budget[name] <= 1:
            break
        budget[name] -= 1
    while sum(budget.values()) < limit:
        room = [
            name
            for name in present
            if budget[name] < len(grouped[name])
        ]
        if not room:
            break
        name = max(room, key=lambda item: len(grouped[item]) - budget[item])
        budget[name] += 1

    chosen: List[Dict[str, Any]] = []
    chosen_paths = set()
    for name in present:
        pool = _prefer_prior(grouped[name])
        prior = [row for row in pool if row.get("prior_qa_status") in {"FAIL", "PASS", "REVIEW"}]
        rest = [row for row in pool if row not in prior]
        picked = prior[: budget[name]]
        if len(picked) < budget[name]:
            picked.extend(_spread(rest, budget[name] - len(picked)))
        for record in picked:
            path = record.get("relative_path")
            if path in chosen_paths:
                continue
            chosen.append(record)
            chosen_paths.add(path)
    return chosen[:limit]
