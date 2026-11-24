"""Aggregate Phase 3 comparison records. Records are already classified."""

from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any, Dict, Iterable, List, Optional


TRANSITIONS = (
    "PASS → PASS",
    "PASS → RECOVERED",
    "PASS → CHANGED",
    "PASS → WRONG",
    "FAIL → RECOVERED",
    "FAIL → UNRESOLVED",
    "FAIL → PROVIDER_ERROR",
    "FAIL → WRONG",
)


def _blank_counts() -> Dict[str, int]:
    return {name: 0 for name in TRANSITIONS}


def _rate(numerator: int, denominator: int) -> Optional[float]:
    if denominator <= 0:
        return None
    return round(numerator / denominator, 4)


def _bucket(rows: Iterable[Dict[str, Any]], key: str) -> Dict[str, Dict[str, int]]:
    grouped: Dict[str, Dict[str, int]] = {}
    for row in rows:
        label = str(row.get(key) or "unknown")
        bucket = grouped.setdefault(label, _blank_counts())
        transition = str(row.get("transition") or "")
        if transition in bucket:
            bucket[transition] += 1
        bucket.setdefault("documents", 0)
        bucket["documents"] += 1
    return grouped


def aggregate(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    baseline = Counter()
    phase2 = Counter()
    transitions = Counter()
    safety = Counter()
    attempts = []
    provider_calls = []
    durations = []

    for record in records:
        outcome = str(record.get("baseline_outcome") or "ERROR")
        baseline[outcome] += 1
        if record.get("ocr_required"):
            baseline["ocr_required"] += 1
        if record.get("baseline_provider_error"):
            baseline["provider_error"] += 1

        state = str(record.get("recovery_state") or "RECOVERY_NOT_ATTEMPTED")
        phase2[state] += 1
        verification = str(record.get("verification") or "RECOVERY_UNVERIFIED")
        if verification == "RECOVERY_VERIFIED_CORRECT":
            phase2["RECOVERY_VERIFIED_CORRECT"] += 1
        elif verification == "RECOVERY_VERIFIED_WRONG":
            phase2["RECOVERY_VERIFIED_WRONG"] += 1
        elif state != "RECOVERY_NOT_ATTEMPTED":
            phase2["RECOVERY_UNVERIFIED"] += 1
        if state == "RECOVERY_ACCEPTED" and verification == "RECOVERY_UNVERIFIED":
            phase2["unverified_accepted"] += 1
        transition = str(record.get("transition") or "")
        if transition:
            transitions[transition] += 1
        for flag in record.get("safety_flags") or []:
            safety[flag] += 1

        attempt_count = record.get("attempts")
        if isinstance(attempt_count, int):
            attempts.append(attempt_count)
        calls = record.get("provider_calls")
        if isinstance(calls, int):
            provider_calls.append(calls)
        duration = record.get("processing_time_ms")
        if isinstance(duration, (int, float)):
            durations.append(float(duration))

    accepted = phase2["RECOVERY_ACCEPTED"]
    attempted = (
        phase2["RECOVERY_ACCEPTED"]
        + phase2["RECOVERY_REJECTED"]
        + phase2["RECOVERY_PROVIDER_ERROR"]
    )
    verified_correct = phase2["RECOVERY_VERIFIED_CORRECT"]
    verified_wrong = phase2["RECOVERY_VERIFIED_WRONG"]
    verified = verified_correct + verified_wrong

    def _avg(values: List[float]) -> Optional[float]:
        if not values:
            return None
        return round(sum(values) / len(values), 2)

    return {
        "total_documents": len(records),
        "baseline": {
            "PASS": baseline["PASS"],
            "FAIL": baseline["FAIL"],
            "ERROR": baseline["ERROR"],
            "provider_error": baseline["provider_error"],
            "ocr_required": baseline["ocr_required"],
        },
        "phase2": {
            "recovery_attempted": attempted,
            "recovery_accepted": accepted,
            "recovery_rejected": phase2["RECOVERY_REJECTED"],
            "recovery_unresolved": phase2["RECOVERY_UNRESOLVED"],
            "recovery_not_attempted": phase2["RECOVERY_NOT_ATTEMPTED"],
            "provider_error": phase2["RECOVERY_PROVIDER_ERROR"],
            "verified_correct": verified_correct,
            "verified_wrong": verified_wrong,
            "unverified": phase2["RECOVERY_UNVERIFIED"],
            "unverified_accepted": phase2["unverified_accepted"],
        },
        "transitions": {name: transitions[name] for name in TRANSITIONS},
        "safety": {
            "pass_to_wrong": transitions["PASS → WRONG"],
            "fail_to_wrong": transitions["FAIL → WRONG"],
            "null_became_zero": safety["null_became_zero"],
            "unknown_layout_accepted": safety["unknown_layout_accepted"],
            "dynamic_field_changed": safety["dynamic_field_changed"],
            "canonical_values_changed": safety["canonical_values_changed"],
        },
        "quality": {
            "verified_recovery_precision": _rate(verified_correct, verified),
            "recovery_acceptance_rate": _rate(accepted, attempted),
            "recovery_verification_rate": _rate(verified, accepted),
        },
        "breakdown": {
            "file_type": _bucket(records, "file_type"),
            "layout": _bucket(records, "profile"),
            "recovery_strategy": _bucket(records, "recovery_strategy"),
            "failure_type": _bucket(records, "failure_type"),
            "ocr": _bucket(records, "ocr_path"),
        },
        "averages": {
            "recovery_attempts": _avg([float(item) for item in attempts]),
            "provider_calls": _avg([float(item) for item in provider_calls]),
            "processing_time_ms": _avg(durations),
        },
    }


def recommend(metrics: Dict[str, Any]) -> Dict[str, str]:
    """One rollout recommendation from measured safety and verification."""
    safety = metrics.get("safety") or {}
    phase2 = metrics.get("phase2") or {}
    if (
        safety.get("pass_to_wrong")
        or safety.get("fail_to_wrong")
        or safety.get("null_became_zero")
        or safety.get("unknown_layout_accepted")
    ):
        return {
            "recommendation": "STOP",
            "reason": (
                "A safety stop condition was observed: an accepted result became "
                "wrong, null became zero, or an unknown layout was accepted."
            ),
        }
    if phase2.get("verified_wrong"):
        return {
            "recommendation": "STOP",
            "reason": "At least one recovery was verified wrong against trusted ground truth.",
        }
    accepted = int(phase2.get("recovery_accepted") or 0)
    verified_correct = int(phase2.get("verified_correct") or 0)
    verified_wrong = int(phase2.get("verified_wrong") or 0)
    unverified_accepted = int(phase2.get("unverified_accepted") or 0)
    attempted = int(phase2.get("recovery_attempted") or 0)
    if accepted and verified_correct == 0:
        return {
            "recommendation": "INVESTIGATE",
            "reason": (
                "Recovery was accepted, but none of those acceptances were verified "
                "against trusted ground truth. Acceptance is not correctness."
            ),
        }
    if attempted and not accepted:
        return {
            "recommendation": "INVESTIGATE",
            "reason": (
                "Phase 2 attempted recovery on failed documents, but it accepted none. "
                "No PASS to WRONG or verified FAIL to WRONG case was observed. "
                "This batch does not show an accuracy gain."
            ),
        }
    if attempted == 0:
        return {
            "recommendation": "INVESTIGATE",
            "reason": (
                "Phase 2 did not attempt recovery on this batch, so this run does "
                "not evidence broader enablement."
            ),
        }
    if unverified_accepted:
        return {
            "recommendation": "INVESTIGATE",
            "reason": (
                "Some accepted recoveries matched trusted ground truth, but other "
                "accepted recoveries are still unverified."
            ),
        }
    if verified_correct and not verified_wrong and not safety.get("pass_to_wrong"):
        return {
            "recommendation": "CONTINUE",
            "reason": (
                "No PASS → WRONG or verified FAIL → WRONG cases were found, and "
                "every accepted recovery matched trusted ground truth."
            ),
        }
    return {
        "recommendation": "INVESTIGATE",
        "reason": "The batch does not yet support enabling Phase 2 more broadly.",
    }
