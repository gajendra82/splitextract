"""Resumable human verification CLI for source ground truth.

Shows source path + candidate fields. Does NOT invent values from OCR engines.

Commands per field:
  c = CONFIRM current candidate / expected
  e = EDIT expected raw value
  n = NOT_VISIBLE
  a = AMBIGUOUS
  s = SKIP
  q = quit (progress saved)
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.ground_truth.schema import validate_ground_truth_document

GT_DIR = ROOT / "phase4" / "ground_truth_50"
PROGRESS = GT_DIR / "verification_progress.json"


def _load_progress() -> Dict[str, Any]:
    if PROGRESS.is_file():
        return json.loads(PROGRESS.read_text(encoding="utf-8"))
    return {"completed_field_keys": [], "decisions": {}}


def _save_progress(payload: Dict[str, Any]) -> None:
    PROGRESS.parent.mkdir(parents=True, exist_ok=True)
    PROGRESS.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _key(doc_id: str, field_id: str) -> str:
    return f"{doc_id}::{field_id}"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Source GT verification CLI")
    parser.add_argument(
        "--document",
        type=str,
        default=None,
        help="Optional document_id to verify",
    )
    parser.add_argument(
        "--candidates",
        type=Path,
        default=GT_DIR / "verification_candidates.json",
        help="JSON list of candidate fields awaiting human confirmation",
    )
    args = parser.parse_args(argv)

    if not args.candidates.is_file():
        print(
            "No candidates file. Create verification_candidates.json with proposed "
            "fields, or expand seed_source_verified_gt_50.py after visual inspection."
        )
        print("Progress file:", PROGRESS)
        return 0

    candidates = json.loads(args.candidates.read_text(encoding="utf-8"))
    items = candidates.get("items") or candidates
    if args.document:
        items = [i for i in items if i.get("document_id") == args.document]

    progress = _load_progress()
    done = set(progress.get("completed_field_keys") or [])

    for item in items:
        doc_id = item["document_id"]
        field_id = item["field_id"]
        key = _key(doc_id, field_id)
        if key in done:
            continue
        print("=" * 60)
        print("DOCUMENT:", doc_id)
        print("SOURCE:", item.get("source_file"))
        print("PAGE:", item.get("page", 1))
        print("FIELD:", field_id)
        print("PRINTED HEADER:", item.get("printed_header"))
        print("PRODUCT:", item.get("product_key"))
        print("CANDIDATE EXPECTED:", item.get("expected_raw"))
        print("NOTE: Confirm only what you see on the original document.")
        choice = input("[c]onfirm [e]dit [n]ot_visible [a]mbiguous [s]kip [q]uit > ").strip().lower()
        decision = {"document_id": doc_id, "field_id": field_id}
        if choice == "q":
            _save_progress(progress)
            print("Saved progress.")
            return 0
        if choice == "s":
            decision["status"] = "SKIP"
        elif choice == "n":
            decision["status"] = "NOT_VISIBLE"
            done.add(key)
        elif choice == "a":
            decision["status"] = "AMBIGUOUS"
            done.add(key)
        elif choice == "e":
            new_val = input("expected_raw > ").strip()
            decision["status"] = "EDIT"
            decision["expected_raw"] = new_val
            decision["source_verified"] = True
            done.add(key)
            _upsert_gt(item, new_val, verified=True)
        elif choice == "c":
            decision["status"] = "CONFIRM"
            decision["expected_raw"] = item.get("expected_raw")
            decision["source_verified"] = True
            done.add(key)
            _upsert_gt(item, item.get("expected_raw"), verified=True)
        else:
            print("Unrecognized; skipped.")
            decision["status"] = "SKIP"
        progress.setdefault("decisions", {})[key] = decision
        progress["completed_field_keys"] = sorted(done)
        _save_progress(progress)
    print("Verification pass complete.")
    return 0


def _upsert_gt(item: Dict[str, Any], expected_raw: Any, *, verified: bool) -> None:
    path = GT_DIR / "per_document" / f"{item['document_id']}.json"
    if path.is_file():
        payload = json.loads(path.read_text(encoding="utf-8"))
    else:
        payload = {
            "document_id": item["document_id"],
            "page": item.get("page", 1),
            "source": {
                "file": item.get("source_file"),
                "source_type": item.get("source_type") or "image",
                "source_verified": True,
            },
            "verification_method": "human_cli_confirmation",
            "provenance": "SOURCE_VERIFIED",
            "fields": [],
            "notes": ["Created/updated via verify_ground_truth_cli.py"],
        }
    fields: List[Dict[str, Any]] = list(payload.get("fields") or [])
    fields = [f for f in fields if f.get("field_id") != item["field_id"]]
    fields.append(
        {
            "field_id": item["field_id"],
            "field_type": item.get("field_type") or "numeric",
            "printed_header": item.get("printed_header"),
            "expected_raw": str(expected_raw),
            "expected_normalized": item.get("expected_normalized"),
            "page": item.get("page", 1),
            "product_key": item.get("product_key"),
            "bbox": None,
            "bbox_verified": False,
            "verified": verified,
            "source_verified": True,
            "priority": item.get("priority") or "high",
            "notes": "human_cli_confirmation",
        }
    )
    payload["fields"] = fields
    ok, errors = validate_ground_truth_document(payload)
    if not ok:
        raise SystemExit(f"invalid gt after upsert: {errors}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
