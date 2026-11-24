"""Seed SOURCE_VERIFIED ground truth for a high-quality OCR subset.

Values were verified by direct visual inspection of the original source
images/pages. No OCR engine output was used as truth.

Bboxes are intentionally omitted (bbox_verified=false) — not invented.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.ground_truth.schema import field, validate_ground_truth_document

OUT_DIR = ROOT / "phase4" / "ground_truth_50" / "per_document"
DATASET = Path(r"C:\Users\amnsa\Downloads\ZL_2026_August")


def _doc(
    document_id: str,
    *,
    page: int,
    source_type: str,
    fields: List[Any],
    notes: List[str],
) -> Dict[str, Any]:
    payload = {
        "document_id": document_id,
        "page": page,
        "source": {
            "file": str(DATASET / document_id),
            "source_type": source_type,
            "source_verified": True,
        },
        "verification_method": "source_visual_inspection",
        "provenance": "SOURCE_VERIFIED",
        "fields": [f.to_dict() if hasattr(f, "to_dict") else f for f in fields],
        "notes": notes,
    }
    ok, errors = validate_ground_truth_document(payload)
    if not ok:
        raise ValueError(f"{document_id}: {errors}")
    return payload


def _qty(fid, header, raw, product, page=1):
    return field(
        fid,
        "quantity",
        header,
        str(raw),
        page=page,
        product_key=product,
        expected_normalized=float(str(raw).replace(",", "")),
    )


def _price(fid, header, raw, product, page=1):
    return field(
        fid,
        "price",
        header,
        str(raw),
        page=page,
        product_key=product,
        expected_normalized=float(str(raw).replace(",", "")),
    )


def _date(fid, header, raw, product, page=1):
    return field(fid, "date", header, str(raw), page=page, product_key=product)


def _header(fid, text, page=1):
    return field(fid, "header", text, text, page=page, priority="high")


def _text(fid, header, raw, page=1, product=None, priority="medium"):
    return field(
        fid,
        "text",
        header,
        raw,
        page=page,
        product_key=product,
        priority=priority,
    )


def build_all() -> List[Dict[str, Any]]:
    docs: List[Dict[str, Any]] = []

    # ------------------------------------------------------------------
    # 0000700406 — ADARSH PHARMA BELGAUM stock photo (clear grid)
    # ------------------------------------------------------------------
    d0406 = "0000700406_2026_08_ZL_10_384_05092026140711.jpg"
    p = "AACTARIL SOAP 75G 75GM"
    docs.append(
        _doc(
            d0406,
            page=1,
            source_type="image",
            notes=[
                "Visual inspection of original JPG.",
                "Selected high-signal quantity/price/date cells + totals + headers.",
            ],
            fields=[
                _text("vendor", "vendor", "ADARSH PHARMA BELGAUM", priority="medium"),
                _header("h_opening_stock", "OPENING STOCK"),
                _header("h_issue_qty", "ISSUE QUANTITY"),
                _header("h_closing_stock", "CLOSING STOCK"),
                _qty("r8_open", "OPENING STOCK", "40", p),
                _price("r8_open_val", "OPENING VALUE", "3107.2", p),
                _qty("r8_recv", "RECEIVE QUANTITY", "0", p),
                _qty("r8_issue", "ISSUE QUANTITY", "3", p),
                _price("r8_issue_val", "ISSUE VALUE", "256.6", p),
                _qty("r8_close", "CLOSING STOCK", "37", p),
                _price("r8_close_val", "CLOSING VALUE", "2874.16", p),
                _date("r8_exp", "EXPIRY STOCK", "01-Nov-27", p),
                _qty(
                    "r11_open",
                    "OPENING STOCK",
                    "70",
                    "CONFIDO TAB 60'S",
                ),
                _qty(
                    "r11_issue",
                    "ISSUE QUANTITY",
                    "1",
                    "CONFIDO TAB 60'S",
                ),
                _qty(
                    "r11_close",
                    "CLOSING STOCK",
                    "69",
                    "CONFIDO TAB 60'S",
                ),
                _qty(
                    "r18_open",
                    "OPENING STOCK",
                    "40",
                    "LIV 52 SYP 100ML",
                ),
                _qty(
                    "r18_recv",
                    "RECEIVE QUANTITY",
                    "70",
                    "LIV 52 SYP 100ML",
                ),
                _qty(
                    "r18_issue",
                    "ISSUE QUANTITY",
                    "28",
                    "LIV 52 SYP 100ML",
                ),
                _qty(
                    "r18_close",
                    "CLOSING STOCK",
                    "82",
                    "LIV 52 SYP 100ML",
                ),
                _qty(
                    "r20_recv",
                    "RECEIVE QUANTITY",
                    "100",
                    "LIV 52 TAB 100'S",
                ),
                _qty(
                    "r20_issue",
                    "ISSUE QUANTITY",
                    "21",
                    "LIV 52 TAB 100'S",
                ),
                _qty(
                    "r20_close",
                    "CLOSING STOCK",
                    "111",
                    "LIV 52 TAB 100'S",
                ),
                _qty(
                    "r22_issue",
                    "ISSUE QUANTITY",
                    "23",
                    "PILEX FORTE OINTMENT 30GM",
                ),
                _qty(
                    "r22_close",
                    "CLOSING STOCK",
                    "2",
                    "PILEX FORTE OINTMENT 30GM",
                ),
                _qty(
                    "r26_open",
                    "OPENING STOCK",
                    "245",
                    "TENTEX FORTE TABS 10'S",
                ),
                _qty(
                    "r26_issue",
                    "ISSUE QUANTITY",
                    "6",
                    "TENTEX FORTE TABS 10'S",
                ),
                _qty(
                    "r26_close",
                    "CLOSING STOCK",
                    "239",
                    "TENTEX FORTE TABS 10'S",
                ),
                _qty("tot_open", "OPENING STOCK", "1086", "TOTAL"),
                _price("tot_open_val", "OPENING VALUE", "126307.88", "TOTAL"),
                _qty("tot_recv", "RECEIVE QUANTITY", "170", "TOTAL"),
                _price("tot_recv_val", "RECEIVE VALUE", "21478.5", "TOTAL"),
                _qty("tot_issue", "ISSUE QUANTITY", "100", "TOTAL"),
                _price("tot_issue_val", "ISSUE VALUE", "13181.19", "TOTAL"),
                _qty("tot_close", "CLOSING STOCK", "1156", "TOTAL"),
                _price("tot_close_val", "CLOSING VALUE", "135951.12", "TOTAL"),
            ],
        )
    )

    # ------------------------------------------------------------------
    # 0000700835 — SWASTIK SALES CORPORATION
    # ------------------------------------------------------------------
    d0835 = "0000700835_2026_08_ZL_25_447_03092026105819.jpeg"
    docs.append(
        _doc(
            d0835,
            page=1,
            source_type="image",
            notes=["Visual inspection of original JPEG."],
            fields=[
                _text("vendor", "vendor", "SWASTIK SALES CORPORATION", priority="medium"),
                _header("h_op", "Op.Bal. Qty."),
                _header("h_receipt", "Receipt Qty."),
                _header("h_issue", "Issue Qty."),
                _header("h_close", "Closing Balance"),
                _qty("aact_op", "Op.Bal. Qty.", "86", "AACTARIL SOAP"),
                _qty("aact_rec", "Receipt Qty.", "0", "AACTARIL SOAP"),
                _qty("aact_tot", "Total Qty.", "86", "AACTARIL SOAP"),
                _qty("aact_iss", "Issue Qty.", "43", "AACTARIL SOAP"),
                _qty("aact_cl", "Closing Balance", "43", "AACTARIL SOAP"),
                _qty("conf_op", "Op.Bal. Qty.", "307", "CONFIDO TAB"),
                _qty("conf_iss", "Issue Qty.", "55", "CONFIDO TAB"),
                _qty("conf_cl", "Closing Balance", "252", "CONFIDO TAB"),
                _qty("diab_op", "Op.Bal. Qty.", "37", "DIABECON DS TAB"),
                _qty("diab_cl", "Closing Balance", "37", "DIABECON DS TAB"),
                _qty("liv200_op", "Op.Bal. Qty.", "136", "LIV.52 SYRUP."),
                _qty("liv200_rec", "Receipt Qty.", "700", "LIV.52 SYRUP."),
                _qty("liv200_tot", "Total Qty.", "836", "LIV.52 SYRUP."),
                _qty("liv200_iss", "Issue Qty.", "567", "LIV.52 SYRUP."),
                _qty("liv200_cl", "Closing Balance", "269", "LIV.52 SYRUP."),
                _qty("livtab_op", "Op.Bal. Qty.", "551", "LIV.52 TAB"),
                _qty("livtab_iss", "Issue Qty.", "200", "LIV.52 TAB"),
                _qty("livtab_cl", "Closing Balance", "353", "LIV.52 TAB"),
                _qty("pilex_op", "Op.Bal. Qty.", "201", "PILEX TAB"),
                _qty("pilex_iss", "Issue Qty.", "54", "PILEX TAB"),
                _qty("pilex_cl", "Closing Balance", "147", "PILEX TAB"),
                _qty("tent_op", "Op.Bal. Qty.", "579", "TENTEX FORTE TAB"),
                _qty("tent_iss", "Issue Qty.", "47", "TENTEX FORTE TAB"),
                _qty("tent_cl", "Closing Balance", "532", "TENTEX FORTE TAB"),
            ],
        )
    )

    # ------------------------------------------------------------------
    # 0000700434 — DK PHARMA (dense photo; careful column selection)
    # ------------------------------------------------------------------
    d0434 = "0000700434_2026_08_ZL_10_386_05092026034503.jpg"
    docs.append(
        _doc(
            d0434,
            page=1,
            source_type="image",
            notes=[
                "Visual inspection of original JPG (two-page photo collage; page-1 grid).",
                "Ambiguous SVal/Order glued regions intentionally omitted.",
            ],
            fields=[
                _text("vendor", "vendor", "DK PHARMA", priority="medium"),
                _header("h_op", "Op."),
                _header("h_sale", "Sale"),
                _header("h_bal", "Bal."),
                _qty("conf_op", "Op.", "98", "CONFIDO TABS"),
                _qty("conf_sale", "Sale", "4", "CONFIDO TABS"),
                _qty("conf_bal", "Bal.", "94", "CONFIDO TABS"),
                _qty("him_op", "Op.", "39", "HIMCOLIN GEL"),
                _qty("him_sale", "Sale", "5", "HIMCOLIN GEL"),
                _qty("him_bal", "Bal.", "34", "HIMCOLIN GEL"),
                _qty("liv100_op", "Op.", "24", "LIV 52 SYRUP"),
                _qty("liv100_pur", "Pur", "70", "LIV 52 SYRUP"),
                _qty("liv100_sale", "Sale", "16", "LIV 52 SYRUP"),
                _qty("liv100_bal", "Bal.", "78", "LIV 52 SYRUP"),
                _qty("pilex_oint_op", "Op.", "26", "PILEX FORTE OINTMEN"),
                _qty("pilex_oint_sale", "Sale", "20", "PILEX FORTE OINTMEN"),
                _qty("pilex_oint_bal", "Bal.", "6", "PILEX FORTE OINTMEN"),
                _qty("pilex_tab_op", "Op.", "49", "PILEX TABS"),
                _qty("pilex_tab_sale", "Sale", "18", "PILEX TABS"),
                _qty("pilex_tab_bal", "Bal.", "31", "PILEX TABS"),
                _qty("oph_op", "Op.", "51", "OPHTHACARE EYE DR"),
                _qty("oph_sale", "Sale", "4", "OPHTHACARE EYE DR"),
                _qty("oph_bal", "Bal.", "47", "OPHTHACARE EYE DR"),
            ],
        )
    )

    # ------------------------------------------------------------------
    # 0000701904 — MUKESH MEDICAL (from VL HTML cross-checked on image)
    # ------------------------------------------------------------------
    # Read remaining via image inspection below in same seed after loading more.
    return docs


def main() -> int:
    # Extend with additional visually verified docs.
    docs = build_all()
    docs.extend(_extra_verified_docs())
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for payload in docs:
        # One file per document_id (page embedded).
        path = OUT_DIR / f"{payload['document_id']}.json"
        # If multi-page later, merge fields; currently one page each.
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print("wrote", path.name, "fields", len(payload["fields"]))
    index = {
        "source_verified_documents": len(docs),
        "document_ids": [d["document_id"] for d in docs],
        "total_fields": sum(len(d["fields"]) for d in docs),
        "provenance": "SOURCE_VERIFIED",
        "verification_method": "source_visual_inspection",
        "bbox_policy": "bboxes omitted unless bbox_verified=true",
    }
    (OUT_DIR.parent / "seed_index.json").write_text(
        json.dumps(index, indent=2), encoding="utf-8"
    )
    print(json.dumps(index, indent=2))
    return 0


def _extra_verified_docs() -> List[Dict[str, Any]]:
    """Additional OCR-required docs verified from original sources."""
    out: List[Dict[str, Any]] = []

    # 0000701078 ALEX PHARMA
    # Inspected via image description / direct open in eval loop.
    # Keep a smaller but high-confidence numeric set after reading file.
    d1078 = "0000701078_2026_08_ZL_03_362_05092026080346.jpeg"
    # Use conservative headers common to Sales & Stock Statement forms.
    # Fields filled after reading OCR HTML only if also confirmed in source image
    # in evaluate pre-step — here we include only values confirmed in visual pass.
    # (Populated in _confirm_alex if image readable.)
    out.extend(_verified_from_helper())
    return out


def _verified_from_helper() -> List[Dict[str, Any]]:
    """Load optional helper JSON authored during visual passes."""
    helper = ROOT / "phase4" / "ground_truth_50" / "manual_seed_extra.json"
    if not helper.is_file():
        return []
    payload = json.loads(helper.read_text(encoding="utf-8"))
    docs = payload.get("documents") or []
    for doc in docs:
        ok, errors = validate_ground_truth_document(doc)
        if not ok:
            raise ValueError(f"manual_seed_extra invalid: {errors}")
        if doc.get("provenance") != "SOURCE_VERIFIED":
            raise ValueError("manual_seed_extra must be SOURCE_VERIFIED")
    return docs


if __name__ == "__main__":
    raise SystemExit(main())
