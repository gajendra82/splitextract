"""Stock quantity/value reconciliation gate for /extract-sales-statement.

Validates per-row stock identity AFTER extraction (including Gemini Vision).
Does NOT rewrite printed numbers to force the formula to balance.
Marks rows that fail as reconciliation_failed and logs structured events.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

_TOL = 0.51  # qty cells are usually integers; allow half-unit OCR noise


def _qty_tolerance(*values: float) -> float:
    """Match classifier total-identity tolerance (off-by-1 on small opens)."""
    mag = 0.0
    for v in values:
        try:
            mag = max(mag, abs(float(v)))
        except (TypeError, ValueError):
            continue
    return max(1.0, 0.005 * mag)


def stock_reconciliation_enforce_enabled() -> bool:
    """Default ON — task requires reconcile-before-Laravel for stock statements."""
    value = os.getenv("STOCK_RECONCILIATION_ENFORCE")
    if value is None or str(value).strip() == "":
        return True
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _f(value: Any) -> float:
    try:
        if value is None or value == "":
            return 0.0
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _printed_or_zero(item: Dict[str, Any], field: str, *extra_keys: str) -> float:
    """Use printed value; treat missing/absent columns as 0 (not invent)."""
    extra = item.get("extra") if isinstance(item.get("extra"), dict) else {}
    fs = extra.get("field_source") if isinstance(extra.get("field_source"), dict) else {}
    if field in item and item.get(field) not in (None, ""):
        if fs.get(field) == "missing" and _f(item.get(field)) == 0.0:
            return 0.0
        return _f(item.get(field))
    for key in extra_keys:
        if key in extra and extra.get(key) not in (None, ""):
            if fs.get(key) == "missing" and _f(extra.get(key)) == 0.0:
                return 0.0
            return _f(extra.get(key))
    return 0.0


def _sales_return_qty(item: Dict[str, Any]) -> float:
    sales_return = _printed_or_zero(
        item, "sales_return_qty", "sale_return", "sales_return"
    )
    extra = item.get("extra") if isinstance(item.get("extra"), dict) else {}
    if sales_return == 0.0 and extra.get("sale_return") is not None:
        sales_return = _f(extra.get("sale_return"))
    return sales_return


def _purchase_return_qty(item: Dict[str, Any]) -> float:
    purchase_return = _printed_or_zero(
        item, "purchase_return_qty", "purchase_return"
    )
    extra = item.get("extra") if isinstance(item.get("extra"), dict) else {}
    if purchase_return == 0.0 and extra.get("purchase_return") is not None:
        purchase_return = _f(extra.get("purchase_return"))
    return purchase_return


def _expiry_qty(item: Dict[str, Any]) -> float:
    expiry = _printed_or_zero(item, "expiry_damage_qty", "exp_damage", "exp_dmg")
    extra = item.get("extra") if isinstance(item.get("extra"), dict) else {}
    if expiry == 0.0 and extra.get("exp_damage") is not None:
        expiry = _f(extra.get("exp_damage"))
    return expiry


def _sample_qty(item: Dict[str, Any]) -> float:
    extra = item.get("extra") if isinstance(item.get("extra"), dict) else {}
    if extra.get("free_out_qty") is not None:
        return _f(extra.get("free_out_qty"))
    return _printed_or_zero(item, "sample_qty", "free_out_qty")


def stock_br_trf_adaptive_identity_enabled() -> bool:
    """When Br.Trf is printed, pick identity with/without it if either balances.

    Default ON. Does not rewrite printed qtys — only chooses which expected
    closing to compare against.
    """
    value = os.getenv("STOCK_BR_TRF_ADAPTIVE_IDENTITY", "true")
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _br_trf_qty(item: Dict[str, Any]) -> float:
    """Printed branch-transfer qty (Br.Trf.), if present."""
    extra = item.get("extra") if isinstance(item.get("extra"), dict) else {}
    if extra.get("br_trf") is not None:
        return _f(extra.get("br_trf"))
    if extra.get("branch_transfer_qty") is not None:
        return _f(extra.get("branch_transfer_qty"))
    return 0.0


def _other_out_qty(item: Dict[str, Any], *, include_br_trf: bool = True) -> float:
    """Outward adjustments (scheme / other / optional Br.Trf.)."""
    extra = item.get("extra") if isinstance(item.get("extra"), dict) else {}
    other = 0.0
    br = _br_trf_qty(item)
    if extra.get("other_outward_qty") is not None:
        o = _f(extra.get("other_outward_qty"))
        # Native parser often mirrors br_trf into other_outward_qty — count once.
        if br > 0 and abs(o - br) <= 1e-9:
            if include_br_trf:
                other += br
        else:
            other += o
            if include_br_trf and br > 0:
                other += br
    elif include_br_trf and br > 0:
        other += br
    if extra.get("sales_scheme_qty") is not None:
        other += _f(extra.get("sales_scheme_qty"))
    return other


def _free_in_qty(item: Dict[str, Any]) -> float:
    extra = item.get("extra") if isinstance(item.get("extra"), dict) else {}
    if extra.get("free_in_qty") is not None:
        return _f(extra.get("free_in_qty"))
    return 0.0


def _sales_return_column_present(item: Dict[str, Any]) -> bool:
    extra = item.get("extra") if isinstance(item.get("extra"), dict) else {}
    fs = extra.get("field_source") if isinstance(extra.get("field_source"), dict) else {}
    if fs.get("sales_return_qty") == "printed":
        return True
    if extra.get("sale_return") is not None or extra.get("sales_return") is not None:
        return True
    if item.get("sales_return_qty") is not None:
        return True
    return False


def _printed_total(item: Dict[str, Any]) -> Optional[float]:
    extra = item.get("extra") if isinstance(item.get("extra"), dict) else {}
    fs = extra.get("field_source") if isinstance(extra.get("field_source"), dict) else {}
    if fs.get("total_qty") == "printed" and extra.get("total_stock") is not None:
        return _f(extra.get("total_stock"))
    return None


def _header_marks_total_in(item: Dict[str, Any]) -> bool:
    """True when column_map / headers indicate Total In (includes goods ret)."""
    extra = item.get("extra") if isinstance(item.get("extra"), dict) else {}
    if extra.get("total_in_layout"):
        return True
    for col in extra.get("column_map") or []:
        if not isinstance(col, dict):
            continue
        if col.get("canonical") != "total_qty":
            continue
        text = normalize_header_text(col.get("header_text"))
        if "in" in text.split() or "total in" in text:
            return True
    return False


def normalize_header_text(text: Any) -> str:
    s = str(text or "").lower()
    s = re.sub(r"[._/\-+]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _sales_return_included_in_total(
    *,
    total: float,
    open_pur: float,
    sales_return: float,
    sales_return_present: bool,
    total_in_layout: bool = False,
) -> bool:
    """True when printed TOTAL already embeds Goods Ret / Sales Ret.

    Total In Qty layouts: TOTAL = OPENING + PURCHASE + GOODS_RET.
    Layout A (Total = Open+Purchase): returns are post-total adjustments.
    Prefer the closer numeric match when both are within tolerance.
    """
    tol = _qty_tolerance(total, open_pur, sales_return)
    dist_open = abs(total - open_pur)
    dist_with = abs(total - (open_pur + sales_return))
    matches_open_pur = dist_open <= tol
    matches_with_ret = dist_with <= tol
    if matches_with_ret and matches_open_pur:
        if dist_with + 1e-9 < dist_open:
            return True
        if abs(sales_return) <= tol:
            return bool(total_in_layout)
        return dist_with <= dist_open
    if matches_with_ret and not matches_open_pur and abs(sales_return) > tol:
        return True
    if matches_open_pur:
        return False
    if total_in_layout:
        return True
    if sales_return_present:
        return False
    return False


def expected_total_qty(item: Dict[str, Any]) -> float:
    """TOTAL identity: OPENING + PURCHASE (+ free_in), and + goods ret for Total In."""
    opening = _printed_or_zero(item, "opening_qty")
    purchase = _printed_or_zero(item, "receipts_qty", "purchase_qty")
    free_in = _free_in_qty(item)
    base = opening + purchase + free_in
    total = _printed_total(item)
    sret = _sales_return_qty(item)
    total_in = _header_marks_total_in(item)
    if total is not None and _sales_return_included_in_total(
        total=total,
        open_pur=opening + purchase + free_in,
        sales_return=sret,
        sales_return_present=_sales_return_column_present(item),
        total_in_layout=total_in,
    ):
        return round(base + sret, 4)
    if total_in:
        return round(base + sret, 4)
    return round(base, 4)


def compute_closing_formula(item: Dict[str, Any]) -> Tuple[float, Dict[str, Any]]:
    """Return (expected_closing, formula metadata). Never mutates item."""
    opening = _printed_or_zero(item, "opening_qty")
    purchase = _printed_or_zero(item, "receipts_qty", "purchase_qty")
    sales = _printed_or_zero(item, "sales_qty")
    sales_return = _sales_return_qty(item)
    purchase_return = _purchase_return_qty(item)
    expiry = _expiry_qty(item)
    sample = _sample_qty(item)
    br = _br_trf_qty(item)
    other_plain = _other_out_qty(item, include_br_trf=False)
    free_in = _free_in_qty(item)
    open_pur = opening + purchase + free_in
    total = _printed_total(item)
    sret_present = _sales_return_column_present(item)
    total_in = _header_marks_total_in(item)

    sret_in_total = False
    if total is not None:
        sret_in_total = _sales_return_included_in_total(
            total=total,
            open_pur=open_pur,
            sales_return=sales_return,
            sales_return_present=sret_present,
            total_in_layout=total_in,
        )
        base = total
        base_source = "total_qty"
        sret_adj = 0.0 if sret_in_total else sales_return
    else:
        base = open_pur + sales_return
        base_source = "opening_plus_purchase_plus_returns"
        sret_adj = 0.0  # already in base

    core = base + sret_adj - sales - purchase_return - expiry - sample - other_plain
    expected_plain = round(core, 4)
    expected_with_br = round(core - br, 4)

    # Adaptive: some CONSOLIDATED sheets print Br.Trf. that is already
    # reflected in Cls.Qty; others need Br.Trf. as an outward. Prefer the
    # variant that matches printed closing; never rewrite printed numbers.
    br_in_identity = False
    formula_variant = "opening_plus_purchase_minus_sales"
    closing_printed = item.get("closing_qty")
    tol = _qty_tolerance(opening, purchase, sales, br, expected_plain)
    if (
        stock_br_trf_adaptive_identity_enabled()
        and br > tol
        and closing_printed is not None
        and closing_printed != ""
    ):
        cl = _f(closing_printed)
        match_br = abs(cl - expected_with_br) <= tol
        match_plain = abs(cl - expected_plain) <= tol
        if match_br:
            expected = expected_with_br
            br_in_identity = True
            formula_variant = "opening_plus_purchase_minus_sales_minus_br_trf"
        elif match_plain:
            expected = expected_plain
            br_in_identity = False
            formula_variant = "opening_plus_purchase_minus_sales_br_trf_excluded"
        else:
            # Layout default: Br.Trf. is an outward when neither matches.
            expected = expected_with_br
            br_in_identity = True
            formula_variant = "opening_plus_purchase_minus_sales_minus_br_trf"
    else:
        expected = expected_with_br if br > 0 else expected_plain
        br_in_identity = bool(br > 0)
        if br_in_identity:
            formula_variant = "opening_plus_purchase_minus_sales_minus_br_trf"

    other_out = other_plain + (br if br_in_identity else 0.0)
    meta = {
        "base_source": base_source,
        "sales_return_in_total": bool(sret_in_total),
        "base_qty": round(base, 4),
        "formula_variant": formula_variant,
        "br_trf_in_identity": bool(br_in_identity),
        "adjustments": {
            "sales_return_qty": round(sret_adj, 4),
            "sales_qty": round(sales, 4),
            "purchase_return_qty": round(purchase_return, 4),
            "expiry_damage_qty": round(expiry, 4),
            "sample_qty": round(sample, 4),
            "other_outward_qty": round(other_out, 4),
            "br_trf_qty": round(br, 4),
        },
        "expected_closing_qty": expected,
        "expected_closing_without_br_trf": expected_plain,
        "expected_closing_with_br_trf": expected_with_br,
    }
    return expected, meta


def expected_closing_qty(item: Dict[str, Any]) -> float:
    """CLOSING from detected layout semantics (see compute_closing_formula)."""
    expected, _meta = compute_closing_formula(item)
    return expected


def _value_reconciliation_diagnostic(item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Diagnostic-only value check when related value columns are all printed.

    Never rejects the qty result. Only records metadata when the layout
    clearly exposes opening/purchase/sales/closing values.
    """
    extra = item.get("extra") if isinstance(item.get("extra"), dict) else {}
    fs = extra.get("field_source") if isinstance(extra.get("field_source"), dict) else {}

    def _val(field: str, *alt: str) -> Optional[float]:
        if fs.get(field) == "missing":
            return None
        if item.get(field) is not None and item.get(field) != "":
            return _f(item.get(field))
        for key in alt:
            if extra.get(key) is not None and extra.get(key) != "":
                return _f(extra.get(key))
        return None

    opening_v = _val("opening_value")
    purchase_v = _val("purchase_value", "receipts_value")
    sales_v = _val("sales_value")
    closing_v = _val("closing_value")
    if None in (opening_v, purchase_v, sales_v, closing_v):
        return None

    expected = round(opening_v + purchase_v - sales_v, 4)
    diff = round(closing_v - expected, 4)
    return {
        "basis": "opening_value+purchase_value-sales_value",
        "enforced": False,
        "expected_closing_value": expected,
        "actual_closing_value": closing_v,
        "value_difference": diff,
        "valid": abs(diff) <= max(_TOL, 1.0),
    }


def reconcile_row(item: Dict[str, Any]) -> Dict[str, Any]:
    """Return reconciliation dict; does not mutate quantities."""
    extra = item.get("extra") if isinstance(item.get("extra"), dict) else {}
    fs = extra.get("field_source") if isinstance(extra.get("field_source"), dict) else {}

    opening = _printed_or_zero(item, "opening_qty")
    purchase = _printed_or_zero(item, "receipts_qty", "purchase_qty")
    sales = _printed_or_zero(item, "sales_qty")
    closing = item.get("closing_qty")

    exp_total = expected_total_qty(item)
    exp_closing, formula = compute_closing_formula(item)

    total_printed = _printed_total(item)
    total_ok = True
    if total_printed is not None:
        total_ok = abs(total_printed - exp_total) <= _qty_tolerance(
            opening, purchase, total_printed, exp_total
        )

    closing_ok = True
    qty_diff = None
    closing_f: Optional[float] = None
    # Missing closing must not be treated as printed 0.0 from empty defaults.
    if fs.get("closing_qty") == "derived":
        closing_f = _f(item.get("closing_qty"))
        closing_ok = abs(closing_f - exp_closing) <= _TOL
        qty_diff = round(closing_f - exp_closing, 4)
    elif fs.get("closing_qty") == "missing" or closing is None:
        closing_f = None
        closing_ok = False
        qty_diff = None
    else:
        closing_f = _f(closing)
        qty_diff = round(float(closing_f) - exp_closing, 4)
        closing_ok = abs(float(closing_f) - exp_closing) <= _TOL

    # Never accept a negative stock closing as reconciled.
    if closing_f is not None and float(closing_f) < -_TOL:
        closing_ok = False

    valid = bool(total_ok and closing_ok)
    # Value identity ignores Br.Trf.; skip when transfer qty is printed.
    value_diag = None
    if _br_trf_qty(item) <= _TOL:
        value_diag = _value_reconciliation_diagnostic(item)

    out: Dict[str, Any] = {
        "expected_total_qty": exp_total,
        "expected_closing_qty": exp_closing,
        "actual_closing_qty": closing_f,
        "quantity_difference": qty_diff,
        "total_ok": total_ok,
        "closing_ok": closing_ok,
        "valid": valid,
        "opening_qty": opening,
        "purchase_qty": purchase,
        "sales_qty": sales,
        "extracted_closing_qty": closing_f,
        "base_source": formula.get("base_source"),
        "sales_return_in_total": formula.get("sales_return_in_total"),
        "base_qty": formula.get("base_qty"),
        "adjustments": formula.get("adjustments"),
        "formula_variant": formula.get("formula_variant"),
        "br_trf_in_identity": formula.get("br_trf_in_identity"),
    }
    if value_diag is not None:
        out["value"] = value_diag
    # Alternate expected when Br.Trf. adaptive considered both sides.
    if formula.get("expected_closing_with_br_trf") is not None:
        out["expected_closing_with_br_trf"] = formula.get(
            "expected_closing_with_br_trf"
        )
    if formula.get("expected_closing_without_br_trf") is not None:
        out["expected_closing_without_br_trf"] = formula.get(
            "expected_closing_without_br_trf"
        )
    return out


def sync_br_trf_other_outward_for_identity(item: Dict[str, Any]) -> None:
    """Align extra.other_outward_qty with adaptive Br.Trf. identity choice.

    Keeps printed ``br_trf`` always. Clears mirrored ``other_outward_qty`` when
    the plain Op+P-S formula matches closing (so classifiers do not subtract
    Br.Trf. twice / incorrectly). Never rewrites opening/sales/closing qtys.
    """
    if not isinstance(item, dict):
        return
    if not stock_br_trf_adaptive_identity_enabled():
        return
    br = _br_trf_qty(item)
    if br <= _TOL:
        return
    _expected, meta = compute_closing_formula(item)
    extra = item.setdefault("extra", {})
    if not isinstance(extra, dict):
        extra = {}
        item["extra"] = extra
    if meta.get("br_trf_in_identity"):
        extra["other_outward_qty"] = br
        extra["br_trf_identity"] = "included"
    else:
        mirrored = extra.get("other_outward_qty")
        if mirrored is not None and abs(_f(mirrored) - br) <= 1e-9:
            extra.pop("other_outward_qty", None)
        extra["br_trf_identity"] = "excluded"
    logger.info(
        "STOCK_BR_TRF_IDENTITY product=%r br_trf=%s role=%s variant=%s",
        str(item.get("product_name") or "")[:60],
        br,
        extra.get("br_trf_identity"),
        meta.get("formula_variant"),
    )


def apply_stock_reconciliation(
    result: Dict[str, Any],
    *,
    request_id: str = "-",
    page: int = 1,
    recovery_attempt: int = 0,
) -> Dict[str, Any]:
    """Mark each product row's reconciliation status. Never rewrites qty/value.

    Failed rows get ``extra.reconciliation_failed = True`` and a
    ``extra.reconciliation`` payload. Totals.extra gets fail counts.
    """
    if not isinstance(result, dict):
        return result
    if not stock_reconciliation_enforce_enabled():
        return result

    if result.get("multi_statement") and isinstance(result.get("statements"), list):
        result["statements"] = [
            apply_stock_reconciliation(
                stmt,
                request_id=request_id,
                page=page,
                recovery_attempt=recovery_attempt,
            )
            for stmt in result["statements"]
            if isinstance(stmt, dict)
        ]
        return result

    logger.info(
        "STOCK_RECONCILIATION_START request_id=%s page=%s recovery_attempt=%s",
        request_id,
        page,
        recovery_attempt,
    )

    items = result.get("line_items") or []
    fail = 0
    pass_n = 0
    for idx, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        name = str(item.get("product_name") or "").strip()
        if not name:
            continue
        # Skip obvious total rows.
        if name.upper().startswith("TOTAL") or name.upper().startswith("GRAND"):
            continue

        # Align Br.Trf. ↔ other_outward before scoring so identity + classifier agree.
        sync_br_trf_other_outward_for_identity(item)
        recon = reconcile_row(item)
        extra = item.setdefault("extra", {})
        if not isinstance(extra, dict):
            extra = {}
            item["extra"] = extra
        extra["reconciliation"] = recon
        extra["stock_identity_ok"] = bool(recon.get("valid"))
        if recon.get("br_trf_in_identity") is not None:
            extra["br_trf_in_identity"] = bool(recon.get("br_trf_in_identity"))
        # Candidate alternate when adaptive considered both Br formulas.
        if (
            not recon.get("valid")
            and recon.get("expected_closing_with_br_trf") is not None
            and recon.get("expected_closing_without_br_trf") is not None
        ):
            cands = extra.setdefault("candidates", [])
            if isinstance(cands, list):
                cands.append(
                    {
                        "source": "br_trf_adaptive_identity",
                        "fields": {},
                        "reason": "printed_closing_matches_neither_br_trf_variant",
                        "would_balance": False,
                        "expected_with_br_trf": recon.get(
                            "expected_closing_with_br_trf"
                        ),
                        "expected_without_br_trf": recon.get(
                            "expected_closing_without_br_trf"
                        ),
                    }
                )

        if recon.get("valid"):
            pass_n += 1
            extra.pop("reconciliation_failed", None)
            logger.info(
                "STOCK_RECONCILIATION_PASS request_id=%s page=%s row=%s "
                "product_name=%s opening_qty=%s purchase_qty=%s sales_qty=%s "
                "extracted_closing_qty=%s expected_closing_qty=%s difference=%s "
                "base_source=%s recovery_attempt=%s",
                request_id,
                page,
                idx,
                name[:80],
                recon.get("opening_qty"),
                recon.get("purchase_qty"),
                recon.get("sales_qty"),
                recon.get("extracted_closing_qty"),
                recon.get("expected_closing_qty"),
                recon.get("quantity_difference"),
                recon.get("base_source"),
                recovery_attempt,
            )
        else:
            fail += 1
            extra["reconciliation_failed"] = True
            logger.info(
                "STOCK_RECONCILIATION_FAIL request_id=%s page=%s row=%s "
                "product_name=%s opening_qty=%s purchase_qty=%s sales_qty=%s "
                "sales_return_qty=%s expiry_damage_qty=%s sample_qty=%s "
                "extracted_closing_qty=%s expected_closing_qty=%s difference=%s "
                "base_source=%s recovery_attempt=%s",
                request_id,
                page,
                idx,
                name[:80],
                recon.get("opening_qty"),
                recon.get("purchase_qty"),
                recon.get("sales_qty"),
                _sales_return_qty(item),
                _expiry_qty(item),
                _sample_qty(item),
                recon.get("extracted_closing_qty"),
                recon.get("expected_closing_qty"),
                recon.get("quantity_difference"),
                recon.get("base_source"),
                recovery_attempt,
            )
            if recovery_attempt > 0:
                logger.info(
                    "STOCK_RECONCILIATION_FINAL_FAIL request_id=%s page=%s row=%s "
                    "product_name=%s difference=%s",
                    request_id,
                    page,
                    idx,
                    name[:80],
                    recon.get("quantity_difference"),
                )

    totals = result.setdefault(
        "totals", {"sales_value": None, "closing_value": None, "extra": {}}
    )
    extra_t = totals.setdefault("extra", {})
    if not isinstance(extra_t, dict):
        extra_t = {}
        totals["extra"] = extra_t
    extra_t["stock_reconciliation_fail_count"] = fail
    extra_t["stock_reconciliation_pass_count"] = pass_n
    extra_t["stock_identity_fail_count"] = fail
    return result


def flagged_row_indices(result: Dict[str, Any]) -> List[int]:
    out: List[int] = []
    for idx, item in enumerate(result.get("line_items") or []):
        if not isinstance(item, dict):
            continue
        extra = item.get("extra") if isinstance(item.get("extra"), dict) else {}
        if extra.get("reconciliation_failed"):
            out.append(idx)
    return out


def flagged_row_recovery_meta(
    result: Dict[str, Any],
    column_map: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """Build per-failed-row recovery context (product, headers, mismatch)."""
    headers: List[str] = []
    for col in column_map or []:
        if not isinstance(col, dict):
            continue
        text = col.get("header_text") or col.get("canonical") or ""
        if text:
            headers.append(str(text))
    meta: List[Dict[str, Any]] = []
    for idx, item in enumerate(result.get("line_items") or []):
        if not isinstance(item, dict):
            continue
        extra = item.get("extra") if isinstance(item.get("extra"), dict) else {}
        if not extra.get("reconciliation_failed"):
            continue
        recon = extra.get("reconciliation") if isinstance(extra.get("reconciliation"), dict) else {}
        meta.append(
            {
                "row_index": int(extra.get("vision_row_index") if extra.get("vision_row_index") is not None else idx),
                "list_index": idx,
                "product_name": item.get("product_name"),
                "y_center": extra.get("y_center"),
                "headers": list(headers),
                "opening_qty": item.get("opening_qty"),
                "purchase_qty": item.get("receipts_qty", item.get("purchase_qty")),
                "sales_qty": item.get("sales_qty"),
                "sales_return_qty": _sales_return_qty(item),
                "purchase_return_qty": _purchase_return_qty(item),
                "expiry_damage_qty": _expiry_qty(item),
                "sample_qty": _sample_qty(item),
                "extracted_closing_qty": recon.get("extracted_closing_qty", item.get("closing_qty")),
                "expected_closing_qty": recon.get("expected_closing_qty"),
                "quantity_difference": recon.get("quantity_difference"),
                "base_source": recon.get("base_source"),
                "adjustments": recon.get("adjustments") or {},
            }
        )
    return meta
