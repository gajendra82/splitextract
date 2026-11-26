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
    """Use printed value; treat missing/absent columns as 0 (not invent).

    Confirmed blank cells (field_source=missing + null/empty) contribute 0 to
    identity math only — callers must not write this 0 back onto the item.
    """
    extra = item.get("extra") if isinstance(item.get("extra"), dict) else {}
    fs = extra.get("field_source") if isinstance(extra.get("field_source"), dict) else {}

    def _read(key: str, container: Dict[str, Any]) -> Optional[float]:
        if key not in container:
            return None
        val = container.get(key)
        if val in (None, "") and fs.get(key) == "missing":
            # Confirmed blank cell → 0 for recon math only.
            return 0.0
        if val in (None, ""):
            return None
        return _f(val)

    for key in (field,) + tuple(extra_keys):
        got = _read(key, item)
        if got is not None:
            return got
        got = _read(key, extra)
        if got is not None:
            return got
    return 0.0


def _sales_return_qty(item: Dict[str, Any]) -> float:
    sales_return = _printed_or_zero(
        item, "sales_return_qty", "sale_return", "sales_return"
    )
    extra = item.get("extra") if isinstance(item.get("extra"), dict) else {}
    if sales_return == 0.0 and extra.get("sale_return") is not None:
        sales_return = _f(extra.get("sale_return"))
    # ITEM|OPENING|SALE RET.|B/E/PR grids store the printed return here.
    if sales_return == 0.0 and extra.get("sale_return_qty") is not None:
        sales_return = _f(extra.get("sale_return_qty"))
    # Stock and Sales Detail Report: Cr.Qty (+ Cr.Sch.Qty) is the printed return.
    if sales_return == 0.0 and (
        extra.get("credit_qty") is not None or extra.get("credit_scheme_qty") is not None
    ):
        sales_return = _f(extra.get("credit_qty")) + _f(extra.get("credit_scheme_qty"))
    # PharmAssist CR column (extra.cr_qty) is the same printed credit/return.
    if sales_return == 0.0 and extra.get("cr_qty") is not None:
        sales_return = _f(extra.get("cr_qty"))
    return sales_return


def _purchase_return_qty(item: Dict[str, Any]) -> float:
    purchase_return = _printed_or_zero(
        item, "purchase_return_qty", "purchase_return"
    )
    extra = item.get("extra") if isinstance(item.get("extra"), dict) else {}
    if purchase_return == 0.0 and extra.get("purchase_return") is not None:
        purchase_return = _f(extra.get("purchase_return"))
    # Stock and Sales Detail Report: Db.Qty (+ Db.Sch.Qty) is purchase return.
    if purchase_return == 0.0 and (
        extra.get("debit_qty") is not None or extra.get("debit_scheme_qty") is not None
    ):
        purchase_return = _f(extra.get("debit_qty")) + _f(extra.get("debit_scheme_qty"))
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
    if extra.get("sample_qty") is not None:
        return _f(extra.get("sample_qty"))
    return _printed_or_zero(item, "sample_qty", "free_out_qty")


def _sales_free_qty(item: Dict[str, Any]) -> float:
    """Printed SALES FREE qty (Busy SSA); distinct from SAMPLE / free_out."""
    extra = item.get("extra") if isinstance(item.get("extra"), dict) else {}
    if extra.get("sales_free") is not None:
        return _f(extra.get("sales_free"))
    return _printed_or_zero(item, "sales_free_qty", "sales_free")


def stock_br_trf_adaptive_identity_enabled() -> bool:
    """When Br.Trf is printed, pick identity with/without it if either balances.

    Default ON. Does not rewrite printed qtys — only chooses which expected
    closing to compare against.
    """
    value = os.getenv("STOCK_BR_TRF_ADAPTIVE_IDENTITY", "true")
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def stock_recon_trust_total_only_if_op_pur_enabled() -> bool:
    """Ignore extra.total_stock when it is not Op+Pur(+ret) / Total-In.

    Default ON. Vision sometimes parks Stock-in-Transit qty into total_stock
    (e.g. 37); using that as the closing base yields expected=-25 and
    difference=180 while Op/Sales/Cl (217/62/155) are already correct.
    Does not rewrite printed numbers — only refuses an untrusted base.
    """
    value = os.getenv("STOCK_RECON_TRUST_TOTAL_ONLY_IF_OP_PUR", "true")
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _trusted_printed_total(item: Dict[str, Any]) -> Optional[float]:
    """Printed TOTAL STOCK only when it matches Op+Pur(+returns) or Total-In."""
    total = _printed_total(item)
    if total is None:
        return None
    if not stock_recon_trust_total_only_if_op_pur_enabled():
        return total
    if _header_marks_total_in(item):
        return total
    opening = _printed_or_zero(item, "opening_qty")
    purchase = _printed_or_zero(item, "receipts_qty", "purchase_qty")
    free_in = _free_in_qty(item)
    open_pur = opening + purchase + free_in
    sret = _sales_return_qty(item)
    tol = _qty_tolerance(total, open_pur, sret)
    if abs(total - open_pur) <= tol or abs(total - (open_pur + sret)) <= tol:
        return total
    logger.info(
        "STOCK_RECON_IGNORE_UNTRUSTED_TOTAL total=%s open_pur=%s sales_return=%s "
        "product=%s",
        total,
        open_pur,
        sret,
        str(item.get("product_name") or "")[:80],
    )
    return None


def _br_trf_qty(item: Dict[str, Any]) -> float:
    """Printed branch-transfer qty (Br.Trf.), if present."""
    extra = item.get("extra") if isinstance(item.get("extra"), dict) else {}
    if extra.get("br_trf") is not None:
        return _f(extra.get("br_trf"))
    if extra.get("branch_transfer_qty") is not None:
        return _f(extra.get("branch_transfer_qty"))
    return 0.0


def _others_qty(item: Dict[str, Any]) -> float:
    """Printed signed OTHERS adjustment (negative = outward)."""
    extra = item.get("extra") if isinstance(item.get("extra"), dict) else {}
    if extra.get("others_qty") is not None:
        return _f(extra.get("others_qty"))
    return 0.0


def _others_out_qty_printed(item: Dict[str, Any]) -> Optional[float]:
    """Printed OTHERS-out column (ITEM/PACK/.../P.RETURN/OTHERS/CLOSING)."""
    extra = item.get("extra") if isinstance(item.get("extra"), dict) else {}
    if extra.get("others_out_qty") is not None and extra.get("others_out_qty") != "":
        return _f(extra.get("others_out_qty"))
    if item.get("others_out_qty") is not None and item.get("others_out_qty") != "":
        return _f(item.get("others_out_qty"))
    return None


def _others_in_qty_printed(item: Dict[str, Any]) -> float:
    """Printed OTHERS-in column before SUB TOTAL (inward adjustment)."""
    extra = item.get("extra") if isinstance(item.get("extra"), dict) else {}
    if extra.get("others_in_qty") is not None and extra.get("others_in_qty") != "":
        return _f(extra.get("others_in_qty"))
    if item.get("others_in_qty") is not None and item.get("others_in_qty") != "":
        return _f(item.get("others_in_qty"))
    return 0.0


def _other_out_qty(item: Dict[str, Any], *, include_br_trf: bool = True) -> float:
    """Outward adjustments (scheme / other / optional Br.Trf.)."""
    extra = item.get("extra") if isinstance(item.get("extra"), dict) else {}
    other = 0.0
    br = _br_trf_qty(item)
    others_out = _others_out_qty_printed(item)
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
            # others_out_qty may duplicate other_outward_qty on dual-OTHERS grids.
            if others_out is not None and abs(o - others_out) > 1e-9:
                other += others_out
    elif others_out is not None:
        other += others_out
        if include_br_trf and br > 0:
            other += br
    elif include_br_trf and br > 0:
        other += br
    if extra.get("sales_scheme_qty") is not None:
        other += _f(extra.get("sales_scheme_qty"))
    # Stock and Sales Detail: Br.S.Qty (branch/break sales) is an outward.
    # Skip when already mirrored into other_outward_qty above.
    if extra.get("break_sales_qty") is not None and extra.get("other_outward_qty") is None:
        other += _f(extra.get("break_sales_qty"))
    # Busy SSA trailing out-columns when printed (STOCK T/F, P/R, REPL/OTHER).
    for key in ("stock_tf_qty", "pr_qty", "repl_other_out"):
        if extra.get(key) is not None:
            other += _f(extra.get(key))
    # PharmAssist ADJ. / stock_adj is a printed outward adjustment (not rewrite).
    adj = None
    if extra.get("adj_qty") is not None:
        adj = _f(extra.get("adj_qty"))
    elif extra.get("stock_adj_qty") is not None:
        adj = _f(extra.get("stock_adj_qty"))
    if adj is not None:
        mirrored = (
            extra.get("other_outward_qty") is not None
            and abs(_f(extra.get("other_outward_qty")) - adj) <= 1e-9
        )
        if not mirrored and not (
            others_out is not None and abs(others_out - adj) <= 1e-9
        ):
            other += adj
    return other


def _free_in_qty(item: Dict[str, Any]) -> float:
    extra = item.get("extra") if isinstance(item.get("extra"), dict) else {}
    free = 0.0
    if extra.get("free_in_qty") is not None:
        free = _f(extra.get("free_in_qty"))
    elif extra.get("purchase_free") is not None:
        free = _f(extra.get("purchase_free"))
    # Dual-OTHERS grids: OTHERS before SUBTOTAL is an inward (not free).
    others_in = _others_in_qty_printed(item)
    return free + others_in

def _sales_return_column_present(item: Dict[str, Any]) -> bool:
    extra = item.get("extra") if isinstance(item.get("extra"), dict) else {}
    fs = extra.get("field_source") if isinstance(extra.get("field_source"), dict) else {}
    if fs.get("sales_return_qty") == "printed":
        return True
    if extra.get("sale_return") is not None or extra.get("sales_return") is not None:
        return True
    if extra.get("sale_return_qty") is not None:
        return True
    if extra.get("credit_qty") is not None or extra.get("credit_scheme_qty") is not None:
        return True
    if extra.get("cr_qty") is not None:
        return True
    if item.get("sales_return_qty") is not None:
        return True
    return False


def _printed_total(item: Dict[str, Any]) -> Optional[float]:
    extra = item.get("extra") if isinstance(item.get("extra"), dict) else {}
    fs = extra.get("field_source") if isinstance(extra.get("field_source"), dict) else {}
    if fs.get("total_qty") == "missing":
        return None
    if extra.get("total_stock") is not None and extra.get("total_stock") != "":
        # Busy SSA and Layout-A sheets store printed TOTAL STOCK here.
        return _f(extra.get("total_stock"))
    if fs.get("total_qty") == "printed" and extra.get("total_qty") is not None:
        return _f(extra.get("total_qty"))
    # ITEM/PACK/.../SUBTOTAL grids print SUBTOTAL = Op+Pur+S.Return+Others In.
    if extra.get("subtotal_qty") is not None and extra.get("subtotal_qty") != "":
        return _f(extra.get("subtotal_qty"))
    if item.get("subtotal_qty") is not None and item.get("subtotal_qty") != "":
        return _f(item.get("subtotal_qty"))
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
    total = _trusted_printed_total(item)
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


def _open_pur_ret_sales_close_layout(item: Dict[str, Any]) -> bool:
    """PRODUCT|OPEN|PUR.|RETURN|PUR.RET|SALES|CLOSE qty grid.

    On this printed layout PUR.RET contributes positively to closing
    (Clarina 30+50+2-13=69, Himcolin 6+2-4=4). Do not use the generic
    purchase-return-outward formula for these rows only.
    """
    extra = item.get("extra") if isinstance(item.get("extra"), dict) else {}
    layout = str(extra.get("layout") or "")
    kind = str(extra.get("stock_identity_kind") or "")
    if layout == "stock_statement_open_pur_ret_sales_close":
        return True
    if kind == "open_pur_ret_sales_close":
        return True
    if str(extra.get("pur_ret_sign") or "").lower() == "inward":
        return True
    return False


def compute_closing_formula(item: Dict[str, Any]) -> Tuple[float, Dict[str, Any]]:
    """Return (expected_closing, formula metadata). Never mutates item."""
    opening = _printed_or_zero(item, "opening_qty")
    purchase = _printed_or_zero(item, "receipts_qty", "purchase_qty")
    sales = _printed_or_zero(item, "sales_qty")
    sales_return = _sales_return_qty(item)
    purchase_return = _purchase_return_qty(item)
    expiry = _expiry_qty(item)
    sample = _sample_qty(item)
    sales_free = _sales_free_qty(item)
    br = _br_trf_qty(item)
    other_plain = _other_out_qty(item, include_br_trf=False)
    others_signed = _others_qty(item)
    free_in = _free_in_qty(item)
    open_pur = opening + purchase + free_in
    total = _trusted_printed_total(item)
    sret_present = _sales_return_column_present(item)
    total_in = _header_marks_total_in(item)

    # Layout-specific: OPEN+PUR+RETURN+PUR.RET−SALES=CLOSE (PUR.RET inward).
    if _open_pur_ret_sales_close_layout(item):
        base = open_pur + sales_return + purchase_return
        expected = round(base - sales - sales_free - expiry - sample - other_plain, 4)
        meta = {
            "base_source": "opening_plus_purchase_plus_return_plus_pur_ret",
            "sales_return_in_total": False,
            "base_qty": round(base, 4),
            "formula_variant": "open_pur_ret_sales_close_pur_ret_inward",
            "br_trf_in_identity": False,
            "adjustments": {
                "sales_return_qty": round(sales_return, 4),
                "sales_qty": round(sales, 4),
                "sales_free_qty": round(sales_free, 4),
                # Positive contribution — recorded for evidence, not subtracted.
                "purchase_return_qty": round(purchase_return, 4),
                "purchase_return_sign": "inward",
                "expiry_damage_qty": round(expiry, 4),
                "sample_qty": round(sample, 4),
                "other_outward_qty": round(other_plain, 4),
                "others_qty": round(others_signed, 4),
                "br_trf_qty": round(br, 4),
            },
            "expected_closing_qty": expected,
            "expected_closing_without_br_trf": expected,
            "expected_closing_with_br_trf": expected,
        }
        return expected, meta

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

    # SALES FREE is a separate Busy SSA outward (not SAMPLE / free_out).
    core = (
        base
        + sret_adj
        + others_signed
        - sales
        - sales_free
        - purchase_return
        - expiry
        - sample
        - other_plain
    )
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
            "sales_free_qty": round(sales_free, 4),
            "purchase_return_qty": round(purchase_return, 4),
            "expiry_damage_qty": round(expiry, 4),
            "sample_qty": round(sample, 4),
            "other_outward_qty": round(other_out, 4),
            "others_qty": round(others_signed, 4),
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
    valid = abs(diff) <= max(_TOL, 1.0)
    basis = "opening_value+purchase_value-sales_value"
    # Many Product|Rate grids print Closing Value = Closing Qty × Rate even
    # when open+purchase−sales money differs. Accept that as diagnostic-valid
    # without rewriting any printed number.
    rate = extra.get("unit_rate")
    closing_qty = item.get("closing_qty")
    rate_valued = None
    try:
        if rate not in (None, "") and closing_qty not in (None, ""):
            rate_valued = round(float(rate) * float(closing_qty), 4)
            if abs(closing_v - rate_valued) <= max(_TOL, 1.0):
                if not valid:
                    basis = "closing_qty*unit_rate"
                    expected = rate_valued
                    diff = round(closing_v - expected, 4)
                valid = True
    except (TypeError, ValueError):
        rate_valued = None
    out = {
        "basis": basis,
        "enforced": False,
        "expected_closing_value": expected,
        "actual_closing_value": closing_v,
        "value_difference": diff,
        "valid": valid,
    }
    if rate_valued is not None:
        out["rate_valued_closing"] = rate_valued
    return out


def _sale_closing_only_row(item: Dict[str, Any], fs: Dict[str, Any]) -> bool:
    """True when Opening/Receipt columns are absent and Sale+Closing are printed.

    Opening+Receipt−Sales=Closing does not apply; keep printed sales/closing.
    Does not rewrite numbers or change the identity formula for other layouts.
    """
    opening_missing = fs.get("opening_qty") == "missing"
    purchase_missing = (
        fs.get("purchase_qty") == "missing" or fs.get("receipts_qty") == "missing"
    )
    sales_printed = fs.get("sales_qty") == "printed" or (
        item.get("sales_qty") not in (None, "") and fs.get("sales_qty") != "missing"
    )
    closing_printed = fs.get("closing_qty") == "printed" or (
        item.get("closing_qty") not in (None, "") and fs.get("closing_qty") != "missing"
    )
    return bool(opening_missing and purchase_missing and sales_printed and closing_printed)


def _order_form_qty_only_row(item: Dict[str, Any]) -> bool:
    """True for ORDER FORM Qty-only rows (no opening/receipts/closing printed)."""
    extra = item.get("extra") if isinstance(item.get("extra"), dict) else {}
    layout = str(extra.get("layout") or "")
    if layout in {
        "zandra_two_column_order_form",
        "handwritten_order_form",
        "sap_order_form",
        "sap_right_qty_order_form",
        "order_form",
    }:
        return True
    if str(extra.get("psr_column_layout") or "") == "order_form_qty":
        return True
    return False


def _op_qty_pur_sale_bal_doc_gap_recon_enabled() -> bool:
    """STOCK_OP_QTY_PUR_SALE_BAL_DOC_GAP_RECON — default ON (kill switch).

    Op.Qty|Pur.Qty|Sale.Qty|Bal qty grids often print Bal ≠ Op+Pur-Sale with no
    return/free column. Soft-skip recon so we keep printed digits and do not
    rewrite qty to force identity.
    """
    raw = os.getenv("STOCK_OP_QTY_PUR_SALE_BAL_DOC_GAP_RECON", "1").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _op_qty_pur_sale_bal_row(item: Dict[str, Any]) -> bool:
    """True for Op.Qty|Pur.Qty|Sale.Qty|Bal / Mfac SAS Summary qty-grid rows."""
    extra = item.get("extra") if isinstance(item.get("extra"), dict) else {}
    layout = str(extra.get("layout") or "")
    if layout.startswith("op_qty_pur_sale_bal"):
        return True
    if str(extra.get("psr_column_layout") or "") == "op_qty_pur_sale_bal":
        return True
    if str(extra.get("schema") or "") == "op_qty_pur_sale_bal":
        return True
    return False


def _op_stock_pur_sales_cl_row(item: Dict[str, Any]) -> bool:
    """True for Pack|Op.Stock|Pur.Qty|Sales Qty|Cl.Stock Statement rows."""
    extra = item.get("extra") if isinstance(item.get("extra"), dict) else {}
    layout = str(extra.get("layout") or "")
    if layout.startswith("op_stock_pur_sales_cl"):
        return True
    if str(extra.get("psr_column_layout") or "") == "op_stock_pur_sales_cl":
        return True
    if str(extra.get("schema") or "") == "op_stock_pur_sales_cl":
        return True
    return False


def _short_op_pi_clqty_row(item: Dict[str, Any]) -> bool:
    """True for short OP/PI/Sale/CLQty grids without ST In/Out columns.

    Dhruvi-style Stock & Sales XLSX: printed CLQty often ≠ OP+PI−Sale because
    transfers are omitted. Keep printed PI/CLQty; soft-skip recon.
    """
    extra = item.get("extra") if isinstance(item.get("extra"), dict) else {}
    if str(extra.get("layout") or "") != "stock_sales_op_pi_clqty":
        return False
    if extra.get("stock_identity_skipped") == "short_op_pi_clqty_doc_gap":
        return True
    # No ST / In / Out movement parked on the row → short layout.
    for key in ("st_in_qty", "st_out_qty", "in_qty", "out_qty", "stock_adj_qty"):
        if extra.get(key) not in (None, "", 0, 0.0):
            return False
    return True


def reconcile_row(item: Dict[str, Any]) -> Dict[str, Any]:
    """Return reconciliation dict; does not mutate quantities."""
    extra = item.get("extra") if isinstance(item.get("extra"), dict) else {}
    fs = extra.get("field_source") if isinstance(extra.get("field_source"), dict) else {}

    opening = _printed_or_zero(item, "opening_qty")
    purchase = _printed_or_zero(item, "receipts_qty", "purchase_qty")
    sales = _printed_or_zero(item, "sales_qty")
    closing = item.get("closing_qty")

    # ORDER FORM Qty-only: closing identity does not apply.
    if _order_form_qty_only_row(item):
        logger.info(
            "STOCK_RECONCILIATION_SKIP reason=order_form_qty product=%s sales_qty=%s",
            str(item.get("product_name") or "")[:80],
            sales,
        )
        return {
            "expected_total_qty": None,
            "expected_closing_qty": None,
            "actual_closing_qty": None,
            "quantity_difference": 0.0,
            "total_ok": True,
            "closing_ok": True,
            "valid": True,
            "skipped": "order_form_qty",
            "opening_qty": None,
            "purchase_qty": None,
            "sales_qty": sales,
            "extracted_closing_qty": None,
            "base_source": "order_form_qty",
            "sales_return_in_total": False,
            "base_qty": None,
            "adjustments": {},
        }

    # Op.Qty|Pur.Qty|Sale.Qty|Bal: keep printed Bal; document often unbalanced.
    if _op_qty_pur_sale_bal_doc_gap_recon_enabled() and _op_qty_pur_sale_bal_row(item):
        closing_f = None if closing in (None, "") else _f(closing)
        exp_closing, formula = compute_closing_formula(item)
        qty_diff = None
        if closing_f is not None:
            qty_diff = round(float(closing_f) - exp_closing, 4)
        logger.info(
            "STOCK_RECONCILIATION_SKIP reason=op_qty_pur_sale_bal_doc_gap "
            "product=%s opening_qty=%s purchase_qty=%s sales_qty=%s "
            "extracted_closing_qty=%s expected_closing_qty=%s difference=%s",
            str(item.get("product_name") or "")[:80],
            opening,
            purchase,
            sales,
            closing_f,
            exp_closing,
            qty_diff,
        )
        return {
            "expected_total_qty": expected_total_qty(item),
            "expected_closing_qty": exp_closing,
            "actual_closing_qty": closing_f,
            "quantity_difference": qty_diff,
            "total_ok": True,
            "closing_ok": True,
            "valid": True,
            "skipped": "op_qty_pur_sale_bal_doc_gap",
            "opening_qty": opening,
            "purchase_qty": purchase,
            "sales_qty": sales,
            "extracted_closing_qty": closing_f,
            "base_source": "op_qty_pur_sale_bal_doc_gap",
            "sales_return_in_total": formula.get("sales_return_in_total"),
            "base_qty": formula.get("base_qty"),
            "adjustments": formula.get("adjustments"),
        }

    # Short OP/PI/Sale/CLQty (no ST In/Out): keep printed CLQty as source truth.
    # Classify reconciled vs discrepancy from OP+PI−Sale vs CLQty — never invent
    # adjustments, and never auto-pass every row as skipped/valid.
    if _short_op_pi_clqty_row(item):
        closing_f = None if closing in (None, "") else _f(closing)
        # Short-grid expected close: opening + PI − Sale (ST columns absent).
        exp_closing = round(opening + purchase - sales, 4)
        if closing_f is None:
            classification = "skipped"
            qty_diff = None
            closing_ok = False
            valid = False
            skip_reason = "short_op_pi_clqty_missing_closing"
        else:
            qty_diff = round(float(closing_f) - exp_closing, 4)
            tol = _qty_tolerance(opening, purchase, sales, closing_f, exp_closing)
            if abs(qty_diff) <= tol:
                classification = "reconciled"
                closing_ok = True
                valid = True
                skip_reason = None
            else:
                classification = "discrepancy"
                closing_ok = False
                valid = False
                skip_reason = None
        logger.info(
            "STOCK_RECONCILIATION_%s reason=short_op_pi_clqty "
            "product=%s opening_qty=%s purchase_qty=%s sales_qty=%s "
            "extracted_closing_qty=%s expected_closing_qty=%s difference=%s "
            "classification=%s",
            "PASS" if valid else ("SKIP" if classification == "skipped" else "FAIL"),
            str(item.get("product_name") or "")[:80],
            opening,
            purchase,
            sales,
            closing_f,
            exp_closing,
            qty_diff,
            classification,
        )
        out = {
            "expected_total_qty": round(opening + purchase, 4),
            "expected_closing_qty": exp_closing,
            "actual_closing_qty": closing_f,
            "quantity_difference": qty_diff,
            "total_ok": True,
            "closing_ok": closing_ok,
            "valid": valid,
            "classification": classification,
            "opening_qty": opening,
            "purchase_qty": purchase,
            "sales_qty": sales,
            "extracted_closing_qty": closing_f,
            "base_source": "opening_plus_pi_minus_sale",
            "sales_return_in_total": False,
            "base_qty": round(opening + purchase, 4),
            "adjustments": {
                "sales_qty": round(sales, 4),
                "purchase_return_qty": 0.0,
                "sales_return_qty": 0.0,
            },
            "formula": "closing=opening+PI-Sale; printed CLQty preserved",
        }
        if skip_reason:
            out["skipped"] = skip_reason
        return out

    # SALE+CLOSING-only sheets: identity formula is not applicable.
    if _sale_closing_only_row(item, fs):
        closing_f = None if closing in (None, "") else _f(closing)
        logger.info(
            "STOCK_RECONCILIATION_SKIP reason=sale_closing_only product=%s "
            "sales_qty=%s closing_qty=%s",
            str(item.get("product_name") or "")[:80],
            sales,
            closing_f,
        )
        return {
            "expected_total_qty": None,
            "expected_closing_qty": closing_f,
            "actual_closing_qty": closing_f,
            "quantity_difference": 0.0,
            "total_ok": True,
            "closing_ok": True,
            "valid": True,
            "skipped": "sale_closing_only",
            "opening_qty": None,
            "purchase_qty": None,
            "sales_qty": sales,
            "extracted_closing_qty": closing_f,
            "base_source": "sale_closing_only",
            "sales_return_in_total": False,
            "base_qty": None,
            "adjustments": {},
        }

    exp_total = expected_total_qty(item)
    exp_closing, formula = compute_closing_formula(item)

    total_printed = _trusted_printed_total(item)
    total_ok = True
    if total_printed is not None:
        total_ok = abs(total_printed - exp_total) <= _qty_tolerance(
            opening, purchase, total_printed, exp_total
        )

    closing_ok = True
    qty_diff = None
    closing_f: Optional[float] = None
    # Missing closing must not be treated as printed 0.0 from empty defaults.
    # Use _qty_tolerance (same as TOTAL / classifier VALID): allow off-by-1
    # printed ledger noise without rewriting qty cells. Stock-and-Sales Detail
    # (Cr.Qty layout) also allows classifier MINOR floor (±2) — a few printed
    # rows on that sheet are off-by-1/2 with every column correctly assigned.
    # Pack|Op.Stock|Pur|Sales|Cl Statement grids are integer qty — keep half-unit
    # OCR noise only; do not soft-pass non-zero quantity_difference as valid.
    def _closing_tol(actual: float) -> float:
        if _op_stock_pur_sales_cl_row(item):
            return _TOL
        tol = _qty_tolerance(opening, purchase, sales, actual, exp_closing)
        layout = str(extra.get("layout") or "")
        if layout == "stock_sales_detail_opval" or extra.get("credit_qty") is not None:
            tol = max(tol, 2.0)
        return tol

    if fs.get("closing_qty") == "derived":
        closing_f = _f(item.get("closing_qty"))
        qty_diff = round(closing_f - exp_closing, 4)
        closing_ok = abs(closing_f - exp_closing) <= _closing_tol(closing_f)
    elif fs.get("closing_qty") == "missing" or closing is None:
        # Blank / unread closing is fine when identity expects ~0 (zero-activity
        # rows, or opening+receipts-sales already balanced with nothing printed).
        if abs(exp_closing) <= _TOL:
            closing_f = 0.0
            qty_diff = 0.0
            closing_ok = True
        else:
            closing_f = None
            closing_ok = False
            qty_diff = None
    else:
        closing_f = _f(closing)
        qty_diff = round(float(closing_f) - exp_closing, 4)
        closing_ok = abs(float(closing_f) - exp_closing) <= _closing_tol(closing_f)

    # Derived negative closing is not accepted (forces recovery). Printed
    # negative closing (Tally oversold) is valid when it matches identity.
    if (
        closing_f is not None
        and float(closing_f) < -_TOL
        and fs.get("closing_qty") == "derived"
    ):
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
    # Source blank receipt/purchase stays null on the item; recon uses 0 only.
    if fs.get("receipts_qty") == "missing" or fs.get("purchase_qty") == "missing":
        out["purchase_qty"] = 0.0
        out["purchase_qty_source"] = "blank"
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
    skip_n = 0
    discrepancy_n = 0
    reconciled_n = 0
    aggregate_variance = 0.0
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
        classification = str(recon.get("classification") or "")
        if classification:
            extra["reconciliation_classification"] = classification
        skipped = bool(recon.get("skipped"))
        # Explicit classification=skipped (e.g. missing CLQty on short OP/PI)
        # is never a pass. Legacy soft-skips that still set valid=True
        # (order_form / bal doc_gap) keep pass-compatible identity.
        hard_skip = classification == "skipped" or (
            skipped and not recon.get("valid")
        )
        if hard_skip:
            extra["stock_identity_ok"] = False
            extra["reconciliation_skipped"] = True
            extra.pop("reconciliation_failed", None)
        else:
            extra["stock_identity_ok"] = bool(recon.get("valid"))
            extra.pop("reconciliation_skipped", None)
        # Keep identity_valid aligned with recon for Pack|Op.Stock|Sales|Cl grids
        # so off-by-1 document gaps are not reported as recon.valid=true while
        # identity_valid=false (or the reverse).
        if _op_stock_pur_sales_cl_row(item):
            extra["identity_valid"] = bool(recon.get("valid")) and not hard_skip
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

        qty_diff = recon.get("quantity_difference")
        try:
            if qty_diff is not None:
                aggregate_variance += float(qty_diff)
        except (TypeError, ValueError):
            pass

        if hard_skip:
            skip_n += 1
            logger.info(
                "STOCK_RECONCILIATION_SKIP request_id=%s page=%s row=%s "
                "product_name=%s reason=%s recovery_attempt=%s",
                request_id,
                page,
                idx,
                name[:80],
                recon.get("skipped") or classification,
                recovery_attempt,
            )
        elif classification == "discrepancy" or not recon.get("valid"):
            fail += 1
            if classification == "discrepancy" or not classification:
                discrepancy_n += 1
            extra["reconciliation_failed"] = True
            logger.info(
                "STOCK_RECONCILIATION_FAIL request_id=%s page=%s row=%s "
                "product_name=%s opening_qty=%s purchase_qty=%s sales_qty=%s "
                "sales_return_qty=%s expiry_damage_qty=%s sample_qty=%s "
                "extracted_closing_qty=%s expected_closing_qty=%s difference=%s "
                "base_source=%s classification=%s recovery_attempt=%s",
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
                classification or "fail",
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
        else:
            pass_n += 1
            if classification == "reconciled" or not classification:
                reconciled_n += 1
            extra.pop("reconciliation_failed", None)
            logger.info(
                "STOCK_RECONCILIATION_PASS request_id=%s page=%s row=%s "
                "product_name=%s opening_qty=%s purchase_qty=%s sales_qty=%s "
                "extracted_closing_qty=%s expected_closing_qty=%s difference=%s "
                "base_source=%s classification=%s recovery_attempt=%s",
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
                classification or "reconciled",
                recovery_attempt,
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
    extra_t["stock_reconciliation_skip_count"] = skip_n
    extra_t["stock_reconciliation_discrepancy_count"] = discrepancy_n
    extra_t["stock_reconciliation_reconciled_count"] = reconciled_n
    # Short OP/PI document-gap sheets: discrepancies stay on recon counters
    # only. Overwriting stock_identity_fail_count would tank quality and make
    # Laravel report "No products were extracted" despite valid line_items.
    if (
        str(extra_t.get("extraction_method") or "") == "stock_sales_op_pi_clqty"
        or extra_t.get("stock_document_gap")
        or str(extra_t.get("stock_identity_kind") or "") == "stock_sales_op_pi_clqty"
    ):
        extra_t["stock_identity_fail_count"] = 0
        extra_t["stock_document_gap"] = True
        extra_t["stock_identity_discrepancy_count"] = discrepancy_n
    else:
        extra_t["stock_identity_fail_count"] = fail
    extra_t["aggregate_qty_variance"] = round(aggregate_variance, 4)
    # Keep stock_validation mirrors in sync with current line_items + recon gate
    # (identity validation may have run with a softer fail count earlier).
    try:
        opening_sum = 0.0
        receipt_sum = 0.0
        sales_sum = 0.0
        closing_sum = 0.0
        for it in items:
            if not isinstance(it, dict):
                continue
            opening_sum += _f(it.get("opening_qty"))
            if it.get("receipts_qty") not in (None, ""):
                receipt_sum += _f(it.get("receipts_qty"))
            else:
                receipt_sum += _f(it.get("purchase_qty"))
            sales_sum += _f(it.get("sales_qty"))
            closing_sum += _f(it.get("closing_qty"))
        expected_total = round(opening_sum + receipt_sum, 2)
        # Prefer sum of per-row recon expected closings so dual-OTHERS /
        # returns / free adjustments are not dropped from the rollup.
        recon_closing_sum = 0.0
        recon_closing_n = 0
        for it in items:
            if not isinstance(it, dict):
                continue
            row_extra = it.get("extra") if isinstance(it.get("extra"), dict) else {}
            recon = (
                row_extra.get("reconciliation")
                if isinstance(row_extra.get("reconciliation"), dict)
                else {}
            )
            if recon.get("expected_closing_qty") is not None:
                recon_closing_sum += _f(recon.get("expected_closing_qty"))
                recon_closing_n += 1
            else:
                recon_closing_sum += _f(it.get("closing_qty"))
        if recon_closing_n > 0:
            calculated_closing = round(recon_closing_sum, 2)
        else:
            calculated_closing = round(expected_total - sales_sum, 2)
        # Saleable Stock Report Firm/Grand/Company Total is the printed footer —
        # do not replace it with a line sum (blank-Bal derived closings inflate it).
        # Same for Excel Saleable Opn/Rec/Issue/Bal grids (xls_total / xls_grand_total):
        # Firm Total can exceed company line sums on the sheet.
        preserve_saleable_footer_qty = str(extra_t.get("total_row_source") or "") in {
            "saleable_grand_total",
            "saleable_firm_total",
            "saleable_company_total",
            "xls_grand_total",
            "xls_total",
            "xls_company_total",
            "xls_firm_total",
        }
        footer_closing = extra_t.get("closing_qty")
        footer_sales = extra_t.get("sales_qty")
        try:
            footer_closing_f = (
                float(footer_closing) if footer_closing is not None else None
            )
        except (TypeError, ValueError):
            footer_closing_f = None
        try:
            footer_sales_f = float(footer_sales) if footer_sales is not None else None
        except (TypeError, ValueError):
            footer_sales_f = None
        extracted_closing = (
            footer_closing_f
            if preserve_saleable_footer_qty and footer_closing_f is not None
            else closing_sum
        )
        extracted_sales = (
            footer_sales_f
            if preserve_saleable_footer_qty and footer_sales_f is not None
            else sales_sum
        )
        qty_variance = round(
            float(extracted_closing) - float(calculated_closing), 4
        )
        prev = extra_t.get("stock_validation")
        prev = dict(prev) if isinstance(prev, dict) else {}
        prev.update(
            {
                "opening_plus_purchase": expected_total,
                "expected_total": expected_total,
                "extracted_opening": round(opening_sum, 2),
                "extracted_purchase": round(receipt_sum, 2),
                "extracted_sales_qty": round(extracted_sales, 2),
                "calculated_closing": calculated_closing,
                "extracted_closing": round(float(extracted_closing), 2),
                "aggregate_qty_variance": round(aggregate_variance, 4),
                "closing_qty_variance": qty_variance,
                "row_discrepancy_count": discrepancy_n,
                "row_reconciled_count": reconciled_n,
                "row_skipped_count": skip_n,
                # Row-level and aggregate must agree: any discrepancy or
                # non-zero aggregate variance fails validation.
                "is_valid": fail == 0
                and skip_n == 0
                and abs(calculated_closing - float(extracted_closing)) < 0.05
                and abs(aggregate_variance) < 0.05,
            }
        )
        extra_t["stock_validation"] = prev
        extra_t["line_sales_qty_sum"] = round(sales_sum, 2)
        extra_t["line_closing_qty_sum"] = round(closing_sum, 2)
        if preserve_saleable_footer_qty:
            logger.info(
                "STOCK_RECON_PRESERVE_SALEABLE_FOOTER_QTY sales=%s closing=%s "
                "line_sales=%s line_closing=%s",
                footer_sales_f,
                footer_closing_f,
                sales_sum,
                closing_sum,
            )
        else:
            if extra_t.get("sales_qty") is not None or sales_sum:
                extra_t["sales_qty"] = round(sales_sum, 2)
            if extra_t.get("closing_qty") is not None or closing_sum:
                extra_t["closing_qty"] = round(closing_sum, 2)
    except Exception:
        logger.exception("STOCK_RECONCILIATION_VALIDATION_SYNC_FAILED")
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
