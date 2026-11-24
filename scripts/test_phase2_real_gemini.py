"""One-off Phase 2 structured Gemini smoke test.

Not production. Does not run the sales parser cascade or the vision fallback.
"""

from __future__ import annotations

import copy
import json
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s")

from services.base_validator import validate_sales_result
from services.recovery_runner import apply_phase2_recovery

JPEG = Path(
    r"C:\Users\amnsa\Downloads\ZL_2026_August"
    r"\0000700077_2026_08_ZL_06_304_06092026141652.jpeg"
)
SAVED = (
    Path(__file__).resolve().parents[1]
    / "qa_results_images"
    / "extracted"
    / "0000700077_2026_08_ZL_06_304_06092026141652.json"
)


def _qty(item):
    return {
        "product_name": item.get("product_name"),
        "opening_qty": item.get("opening_qty"),
        "receipts_qty": item.get("receipts_qty"),
        "sales_qty": item.get("sales_qty"),
        "closing_qty": item.get("closing_qty"),
    }


def main() -> None:
    original = json.loads(SAVED.read_text(encoding="utf-8"))
    validation = validate_sales_result(original)
    original["phase1"] = {"validation": validation, "route": {}}
    before_items = copy.deepcopy(original.get("line_items"))
    before_identity = copy.deepcopy(
        ((original.get("totals") or {}).get("extra") or {}).get("stock_validation")
    )
    print(
        "PRECHECK failure_type=%s accepted=%s fail_count=%s identity=%s"
        % (
            validation.get("failure_type"),
            validation.get("accepted"),
            ((original.get("totals") or {}).get("extra") or {}).get(
                "stock_identity_fail_count"
            ),
            before_identity,
        )
    )
    recovered = apply_phase2_recovery(
        original,
        file_bytes=JPEG.read_bytes(),
        filename=JPEG.name,
    )
    preserved = original.get("line_items") == before_items
    after_extra = ((recovered.get("totals") or {}).get("extra") or {})
    after_validation = ((recovered.get("phase1") or {}).get("validation") or {})
    print("RESULT phase2=%s" % json.dumps(recovered.get("phase2"), default=str))
    print("RESULT original_line_items_preserved=%s" % preserved)
    print("RESULT extraction_failed=%s" % after_extra.get("extraction_failed"))
    print(
        "RESULT stock_identity_after=%s"
        % json.dumps(after_extra.get("stock_validation"), default=str)
    )
    print(
        "RESULT validation_after accepted=%s failure_type=%s"
        % (after_validation.get("accepted"), after_validation.get("failure_type"))
    )
    names = (
        "CLARINA ANTI ACNE CREAM",
        "KAPIKACHHU",
        "MANJISHTHA",
        "PILEX TABLETS",
        "QUISTA ACTIVE 200GM MIX",
        "VASAKA SYRUP",
        "VASAKA TABLET",
    )
    for item in recovered.get("line_items") or []:
        name = str(item.get("product_name") or "")
        if any(token in name for token in names):
            print("ROW %s" % json.dumps(_qty(item)))


if __name__ == "__main__":
    main()
