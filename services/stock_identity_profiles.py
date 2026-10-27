"""Shared stock-identity profiles for validation and Phase 2 row targeting.

The formulas already lived in the sales extractor. This module is the one
place that chooses which formula applies. It does not call Gemini or OCR.

An explicit identity token this module does not recognize is UNKNOWN.
UNKNOWN does not run opening + receipts - sales. That equation is only the
historical rule for ordinary stock statements the extractor already
classified. Using it on a declared unknown layout would invent failures and
start stock recovery for a rule the document never established.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

TOLERANCE = 0.05

PROFILE_GENERIC = "generic"
PROFILE_OPBAL = "opbal"
PROFILE_SALERET = "saleret"
PROFILE_SAMPLE = "sample"
PROFILE_MONTHLY_SS = "monthly_ss"
PROFILE_HIMALAYA_DUMP = "himalaya_dump"
PROFILE_PRODUCT_WISE = "product_wise"
PROFILE_PRODUCT_WISE_IMAGE = "product_wise_image"
PROFILE_ITEM_PACK_SRETURN = "item_pack_sreturn_others"
PROFILE_SSA_SALES_FREE = "ssa_sales_free_columns"
PROFILE_MARG = "marg_sale_purchase_analysis"
PROFILE_OP_PI = "stock_sales_op_pi_clqty"
PROFILE_FREE_RETURN = "item_pack_free_return"
PROFILE_OP_PUR_OREC = "op_pur_orec_sale_oiss"
PROFILE_SWIL_RECEIPT = "swil_receipt_issue"
PROFILE_NOT_SCORED = "not_scored"
PROFILE_UNKNOWN = "unknown"

_SALERET_KINDS = {
    "saleret",
    "opening_purchase_sale_saleret_expdmg",
}
_SAMPLE_KINDS = {
    "sample",
    "opening_purchase_sale_sample_expclos",
}
_GENERIC_KINDS = {
    "opening_receipts_sales_closing",
}
_OPBAL_KINDS = {
    "opening_receipt_issue_closing",
}
_ZL_KINDS = {
    "zl_opening_primary_closing",
    "zl_secondary_xlsx",
}
_RECOGNIZED_EXPLICIT_KINDS = (
    _SALERET_KINDS | _SAMPLE_KINDS | _GENERIC_KINDS | _OPBAL_KINDS | _ZL_KINDS
    | {
        "monthly_ss_report",
        "himalaya_wellness_dump_statement",
        "swil_receipt_issue",
    }
)

# Layouts the extractor already scores with a named formula, or deliberately
# does not score. A method outside this set, with no recognized kind, is
# UNKNOWN rather than a silent generic statement.
_NOT_SCORED_METHODS = {
    "medica_opstk_columns",
    "medivision_op_purc_nm60d",
    "summary_rtl_op_amt",
    "batchwise_stock_summary",
    "stock_valuation_stock_rate",
    "stock_valuation_as_on",
    "stock_valuation_mrp_batch",
    "ssa_sale_closing_reorder",
    "zenith_opstk_totalstock",
    "sunderlal_openstk_sale",
    "zl_secondary_xlsx",
    "zl_opening_bal_pdf",
    "zl_opening_bal_sheet_vision",
    "zl_opening_bal_scan_vision",
}
_NOT_SCORED_LAYOUTS = {
    "zl_opening_primary_closing",
}
_KNOWN_GENERIC_METHODS = {
    "pack_opening_receipt_issue_mexp",
    "ps_pharma_parser",
    "swil_stacked_opbal_expiry_near",
    "swil_tax_pack_dump_near",
    "swil_stacked_opbal_rcpt_oth",
    "opbal_issue_closing_parser",
    "swilerp_retrn_sales_stock",
    "opbal_sales_stock_txt",
    "txt_stock_fallback",
    "daily_stock_summary_txt",
    "legacy_doc_text",
    "item_description_stock_sales_analysis_docx",
    "profitmaker_ostk_docx",
    "textbox_stock_sales_analysis_docx",
    "himalaya_wellness_dump_docx",
    "item_opening_receipt_issue_docx",
    "sale_closing_analysis_docx",
    "sale_closing_pack_photo",
    "docx_stock_sales_detail",
    "group_wise_sales_opstock",
    "pod_hospital_wise_sales_xlsx",
    "marg_closing_mexp_xls",
    "marg_closing_mexp_photo",
    "marg_opening_receipt_issue",
    "marg_qty_value_dump_xls",
    "stock_and_sale_analysis_xlsx",
    "prompt_datewise_xls",
    "html_excel_opstock_rcpts_clstk",
    "stock_sales_analysis_geometry",
    "zandra_stock_sale_vision",
    "pack_op_pur_bal_stock_sale_vision",
    "product_wise_stock_sale_vision",
    "rate_qty_value_columns",
    "swil_receipt_pur_value_vision",
    "pdf_split_page_images",
    "prompt_datewise_layout",
    "ostock_purqty_datewise_layout",
    "ezeal_inward_outward_layout",
    "swil_opening_receipt_value",
    "swil_qty_value_pair",
    "swil_landscape_qty_value",
    "swil_scanned_qty_value_pair",
    "code_item_stock_statement",
    "code_item_stock_statement_photo",
    "saleable_stock_report",
    "order_form_stock_statement",
    "ssa_opening_receipt_issue_value",
    "ssa_mexp_stock_sales",
    "ved_stock_sales_statement",
    "daxinsoft_stock_sales",
    "daxinsoft_detailed_stock_sales",
    "item_desc_opening_sales_purchase",
    "purc_sale_cl_layout",
    "medivision_op_wsale_photo",
    "pharma_hub_jun_jul_photo",
    "stock_sales_consolidated_opval",
    "stock_sales_detail_opval",
    "company_wise_stock_ob_pur",
    "stock_sales_opstk_curstk_stkval",
    "stock_and_sales_report_month",
    "stock_sales_analysis",
    "pharmassist_stock_sale",
    "marg_nano_split_columns",
    "monthly_sales_and_stock",
    "packing_opstk_sps_statement",
    "product_wise_stock_statement_pdf",
    "saha_in_out_stock",
    "stock_register_summary",
    "psr_closstock_columns",
    "product_stock_report",
    "product_stock_report_html",
    "semantic_text_table",
    "opn_sold_lsale_analysis",
    "pdf_text_heuristic",
    "tesseract_heuristic",
    "ssa_qty_value_vision",
    "rate_qty_value_vision",
    "ssa_qty_value_vision_pdf",
    "portrait_opening_balance_columns",
    "op_pur_sale_bal_qnty",
    "opening_purchase_sale_balance_qty",
    "medica_stock_statement_vision",
    "main_stock_sales_statement",
    "main_stock_sales_statement_sheets",
    "product_stock_report_vision",
    "ssa_qty_value_opening_repair",
    "prompt_datewise_photo",
    "two_column_closing_stock_photo",
    "opening_purchased_sold_vision",
    "ssa_mexp_photo",
    "particular_packing_stock_report",
    "swilerp_page_split_vision",
    "swilerp_opbal_ocr_pages",
    "gemini_vision",
    "gemini_extraction_fallback",
    "pdf_text_gemini",
    "tesseract_plus_gemini_text",
    # Unit tests use this name for an ordinary stock statement.
    "swil_erp",
}


@dataclass(frozen=True)
class IdentityProfile:
    profile_id: str
    scores: bool
    supports_recovery: bool
    column_mappings: tuple = ()


def _scored(profile_id: str) -> IdentityProfile:
    return IdentityProfile(profile_id, True, True)


def _quiet(profile_id: str) -> IdentityProfile:
    return IdentityProfile(profile_id, False, False)


def _extra(result: Dict[str, Any]) -> Dict[str, Any]:
    totals = result.get("totals") if isinstance(result.get("totals"), dict) else {}
    extra = totals.get("extra") if isinstance(totals.get("extra"), dict) else {}
    return extra


def _method(result: Dict[str, Any]) -> str:
    return str(_extra(result).get("extraction_method") or "")


def _layout(result: Dict[str, Any]) -> str:
    return str(_extra(result).get("layout") or "")


def _explicit_kind(result: Dict[str, Any]) -> Optional[str]:
    extra = _extra(result)
    raw = extra.get("stock_identity_kind") or extra.get("psr_column_layout")
    if raw in (None, ""):
        return None
    return str(raw)


def _known_method(method: str) -> bool:
    if not method:
        return False
    if method in _NOT_SCORED_METHODS or method in _KNOWN_GENERIC_METHODS:
        return True
    if method.startswith("marg_erp_xls_"):
        return True
    if method in {
        "monthly_ss_report",
        "himalaya_wellness_dump_statement",
        "product_wise_stock_statement",
        "product_wise_stock_statement_image",
        "item_pack_sreturn_others",
        "ssa_sales_free_columns",
        "marg_sale_purchase_analysis",
        "stock_sales_op_pi_clqty",
        "item_pack_free_return",
        "op_pur_orec_sale_oiss",
    }:
        return True
    return False


# Receipt / Issue statements. Total Qty is opening + receipt, not sales.
_SWIL_RECEIPT_METHODS = frozenset({
    "swil_receipt_pur_value_vision",
    "swil_opbal_lastpurc_text",
})
_SWIL_RECEIPT_LAYOUTS = frozenset({
    "swil_receipt_pur_value",
    "swil_opbal_lastpurc",
})


def _choose_profile(result: Dict[str, Any]) -> IdentityProfile:
    """Choose the identity rule from parser metadata already on the statement."""
    if not isinstance(result, dict):
        return _quiet(PROFILE_UNKNOWN)

    from services.sales_statement_extractor import (
        _is_himalaya_dump_statement,
        _is_monthly_ss_statement,
        _stock_identity_kind,
    )

    if _is_monthly_ss_statement(result):
        return _scored(PROFILE_MONTHLY_SS)
    if _is_himalaya_dump_statement(result):
        return _scored(PROFILE_HIMALAYA_DUMP)

    method = _method(result)
    layout = _layout(result)
    if method in _NOT_SCORED_METHODS or layout in _NOT_SCORED_LAYOUTS:
        return _quiet(PROFILE_NOT_SCORED)
    if method == "product_wise_stock_statement":
        return _scored(PROFILE_PRODUCT_WISE)
    if method == "product_wise_stock_statement_image":
        return _scored(PROFILE_PRODUCT_WISE_IMAGE)
    if method == "item_pack_sreturn_others":
        return _scored(PROFILE_ITEM_PACK_SRETURN)
    if method == "ssa_sales_free_columns" or layout == "ssa_sales_free_columns":
        return _scored(PROFILE_SSA_SALES_FREE)
    if method == "marg_sale_purchase_analysis":
        return _scored(PROFILE_MARG)
    if method == "stock_sales_op_pi_clqty":
        return _scored(PROFILE_OP_PI)
    if method == "item_pack_free_return":
        return _scored(PROFILE_FREE_RETURN)
    if method == "op_pur_orec_sale_oiss" or layout == "op_pur_orec_sale_oiss":
        return _scored(PROFILE_OP_PUR_OREC)

    explicit = _explicit_kind(result)
    if (
        method in _SWIL_RECEIPT_METHODS
        or layout in _SWIL_RECEIPT_LAYOUTS
        or explicit == PROFILE_SWIL_RECEIPT
    ):
        return _scored(PROFILE_SWIL_RECEIPT)
    if explicit == "unknown" or explicit == "unsupported":
        return _quiet(PROFILE_UNKNOWN)
    if method in {"unknown", "unsupported"} or layout in {"unknown", "unsupported"}:
        return _quiet(PROFILE_UNKNOWN)
    if explicit is not None and explicit not in _RECOGNIZED_EXPLICIT_KINDS:
        # A declared kind this module does not implement is not the generic
        # equation. Leaving it unrecognized avoids scoring a future layout
        # with opening + receipts - sales.
        return _quiet(PROFILE_UNKNOWN)
    if method and not _known_method(method) and explicit not in _GENERIC_KINDS | _OPBAL_KINDS | _SALERET_KINDS | _SAMPLE_KINDS | _ZL_KINDS:
        return _quiet(PROFILE_UNKNOWN)

    kind = _stock_identity_kind(result)
    if kind in _SALERET_KINDS or kind == "saleret":
        return _scored(PROFILE_SALERET)
    if kind in _SAMPLE_KINDS or kind == "sample":
        return _scored(PROFILE_SAMPLE)
    if kind in _ZL_KINDS:
        return _quiet(PROFILE_NOT_SCORED)
    if kind in _OPBAL_KINDS:
        return _scored(PROFILE_OPBAL)
    return _scored(PROFILE_GENERIC)


def select_profile(result: Dict[str, Any]) -> IdentityProfile:
    """Identity rule plus that layout's column labels. Selection order is unchanged."""
    profile = _choose_profile(result)
    if not isinstance(result, dict):
        return profile
    from services.column_mapping import mappings_for_profile

    found = mappings_for_profile(result, profile)
    if not found:
        return profile
    return IdentityProfile(
        profile.profile_id,
        profile.scores,
        profile.supports_recovery,
        tuple(found),
    )


def mark_unknown_identity(result: Dict[str, Any]) -> Dict[str, Any]:
    """Record that no equation was applied. Line items stay as extracted."""
    totals = result.setdefault(
        "totals", {"sales_value": None, "closing_value": None, "extra": {}}
    )
    if not isinstance(totals, dict):
        totals = {"sales_value": None, "closing_value": None, "extra": {}}
        result["totals"] = totals
    extra = totals.get("extra")
    if not isinstance(extra, dict):
        extra = {}
        totals["extra"] = extra
    extra["stock_identity_kind"] = PROFILE_UNKNOWN
    extra["stock_identity_formula"] = (
        "no stock equation; layout has no identity profile"
    )
    extra["stock_identity_fail_count"] = 0
    extra["stock_validation"] = {
        "is_valid": False,
        "reason": "identity_profile_unknown",
    }
    return result


def _num(value: Any) -> float:
    from services.sales_statement_extractor import _to_float

    return _to_float(value)


def _row_extra(item: Dict[str, Any]) -> Dict[str, Any]:
    extra = item.get("extra") if isinstance(item.get("extra"), dict) else {}
    return extra


def _scheme(extra: Dict[str, Any]) -> float:
    return _num(extra.get("sales_scheme_qty") or extra.get("sales_scheme"))


def swil_expected_closing(item: Dict[str, Any]) -> float:
    """Closing = Opening + Receipt − Issue. Issue is sales_qty. Total is not sales."""
    return round(
        _num(item.get("opening_qty"))
        + _num(item.get("receipts_qty"))
        - _num(item.get("sales_qty")),
        2,
    )


def swil_expected_total(item: Dict[str, Any]) -> float:
    """Total Qty is the intermediate Opening + Receipt column."""
    return round(_num(item.get("opening_qty")) + _num(item.get("receipts_qty")), 2)


def swil_row_ok(item: Dict[str, Any]) -> bool:
    if not _within(swil_expected_closing(item), _num(item.get("closing_qty"))):
        return False
    extra = _row_extra(item)
    total = extra.get("total_stock")
    if total in (None, ""):
        total = extra.get("total_stock_qty")
    if total in (None, ""):
        return True
    return _within(_num(total), swil_expected_total(item))


def generic_expected_closing(item: Dict[str, Any]) -> float:
    """opening + receipts - sales - scheme - stock out."""
    extra = _row_extra(item)
    return round(
        _num(item.get("opening_qty"))
        + _num(item.get("receipts_qty"))
        - _num(item.get("sales_qty"))
        - _scheme(extra)
        - _num(extra.get("stock_out_qty")),
        2,
    )


def monthly_expected_closing(item: Dict[str, Any]) -> float:
    """Monthly SS uses the three canonical qtys. Scheme is not subtracted."""
    return round(
        _num(item.get("opening_qty"))
        + _num(item.get("receipts_qty"))
        - _num(item.get("sales_qty")),
        2,
    )


def issues_expected_closing(item: Dict[str, Any]) -> float:
    """Product-wise statements: opening + receipt - issues."""
    return monthly_expected_closing(item)


def himalaya_expected_closing(item: Dict[str, Any]) -> float:
    extra = _row_extra(item)
    return round(
        _num(item.get("opening_qty"))
        + _num(item.get("receipts_qty"))
        - _num(item.get("sales_qty"))
        - _num(extra.get("shortage_qty")),
        2,
    )


def saleret_expected_closing(item: Dict[str, Any]) -> float:
    from services.sales_statement_extractor import _item_extra_qty

    extra = _row_extra(item)
    opening = _num(item.get("opening_qty"))
    receipts = _num(item.get("receipts_qty"))
    sales_qty = _num(item.get("sales_qty"))
    total = _num(extra.get("total_stock"))
    if total <= 0:
        total = opening + receipts
    sale_return = _item_extra_qty(extra, "sale_return", "sale_return_qty", "saleret")
    exp_damage = _item_extra_qty(extra, "exp_damage", "exp_dmg", "expiry_damage")
    return round(total - sales_qty + sale_return - exp_damage, 2)


def sample_expected_closing(item: Dict[str, Any]) -> float:
    extra = _row_extra(item)
    opening = _num(item.get("opening_qty"))
    receipts = _num(item.get("receipts_qty"))
    sales_qty = _num(item.get("sales_qty"))
    total = _num(extra.get("total_stock"))
    if total <= 0:
        total = opening + receipts
    sample = _num(extra.get("sample_qty"))
    expiry = _num(extra.get("expiry_qty"))
    return round(total - sales_qty - sample - expiry, 2)


def expected_closing_for_kind(item: Dict[str, Any], kind: str) -> float:
    if kind in _SALERET_KINDS:
        return saleret_expected_closing(item)
    if kind in _SAMPLE_KINDS:
        return sample_expected_closing(item)
    return generic_expected_closing(item)


def generic_row_ok(item: Dict[str, Any], *, formula) -> bool:
    extra = _row_extra(item)
    printed_total = _num(extra.get("total_stock"))
    expected_total = round(
        _num(item.get("opening_qty")) + _num(item.get("receipts_qty")),
        2,
    )
    if printed_total > 0 and abs(printed_total - expected_total) > TOLERANCE:
        return False
    expected = formula(item)
    return abs(_num(item.get("closing_qty")) - expected) <= TOLERANCE


def _within(left: float, right: float) -> bool:
    return abs(left - right) <= TOLERANCE


def _src(item: Dict[str, Any], field: str, *fallbacks: str) -> float:
    extra = _row_extra(item)
    if field in item and item.get(field) not in (None, ""):
        return _num(item.get(field))
    for name in fallbacks:
        if name in extra and extra.get(name) not in (None, ""):
            return _num(extra.get(name))
        if name in item and item.get(name) not in (None, ""):
            return _num(item.get(name))
    return 0.0


def item_pack_sreturn_figures(item: Dict[str, Any]) -> tuple:
    opening = _src(item, "opening_qty")
    purchase = _src(item, "purchase_qty", "receipts_qty")
    sales_return = _src(item, "sales_return_qty")
    others_in = _src(item, "others_in_qty")
    subtotal = _src(item, "subtotal_qty")
    sale = _src(item, "sales_qty")
    purchase_return = _src(item, "purchase_return_qty")
    others_out = _src(item, "others_out_qty")
    closing = _src(item, "closing_qty")
    expected_subtotal = round(opening + purchase + sales_return + others_in, 2)
    expected_closing = round(subtotal - sale - purchase_return - others_out, 2)
    ok = _within(expected_subtotal, subtotal) and _within(expected_closing, closing)
    return ok, expected_subtotal, expected_closing


def _marg_src(item: Dict[str, Any], field: str) -> float:
    if item.get(field) not in (None, ""):
        return _num(item.get(field))
    extra = _row_extra(item)
    if extra.get(field) not in (None, ""):
        return _num(extra.get(field))
    return 0.0


def marg_figures(item: Dict[str, Any]) -> tuple:
    opening = _marg_src(item, "opening_qty")
    purchase = _marg_src(item, "purchase_qty")
    if item.get("purchase_qty") in (None, ""):
        purchase = _marg_src(item, "receipts_qty")
    sales_return = _marg_src(item, "sales_return_qty")
    others_in = _marg_src(item, "others_in_qty")
    subtotal = _marg_src(item, "subtotal_qty")
    sale = _marg_src(item, "sales_qty")
    purchase_return = _marg_src(item, "purchase_return_qty")
    others_out = _marg_src(item, "others_out_qty")
    closing = _marg_src(item, "closing_qty")
    expected_subtotal = round(opening + purchase + sales_return + others_in, 2)
    expected_closing = round(subtotal - sale - purchase_return - others_out, 2)
    ok = _within(expected_subtotal, subtotal) and _within(expected_closing, closing)
    return ok, expected_subtotal, expected_closing


def _ssa_col(item: Dict[str, Any], name: str) -> float:
    extra = _row_extra(item)
    if name in ("opening_qty", "receipts_qty", "sales_qty", "closing_qty"):
        return _num(item.get(name))
    if extra.get(name) not in (None, ""):
        return _num(extra.get(name))
    return 0.0


_SSA_DEDUCTED = (
    "sales_qty",
    "sales_free",
    "sample_qty",
    "stock_tf_qty",
    "pr_qty",
    "repl_other_out",
)


def ssa_free_expected_closing(item: Dict[str, Any]) -> float:
    total_stock = _ssa_col(item, "total_stock")
    deducted = sum(_ssa_col(item, name) for name in _SSA_DEDUCTED)
    return round(total_stock - deducted, 2)


def _op_pi_move(item: Dict[str, Any], field: str) -> float:
    if field in ("opening_qty", "receipts_qty", "sales_qty", "closing_qty"):
        return _num(item.get(field))
    extra = _row_extra(item)
    if extra.get(field) not in (None, ""):
        return _num(extra.get(field))
    return 0.0


def _free_move(item: Dict[str, Any], field: str, *extra_names: str) -> float:
    if item.get(field) not in (None, ""):
        return _num(item.get(field))
    extra = _row_extra(item)
    for name in (field, *extra_names):
        if extra.get(name) not in (None, ""):
            return _num(extra.get(name))
    return 0.0


def op_pi_expected_closing(item: Dict[str, Any]) -> float:
    return round(
        _op_pi_move(item, "opening_qty")
        + _op_pi_move(item, "receipts_qty")
        + _op_pi_move(item, "st_in_qty")
        + _op_pi_move(item, "in_qty")
        - _op_pi_move(item, "out_qty")
        - _op_pi_move(item, "sales_qty")
        - _op_pi_move(item, "st_out_qty")
        + _op_pi_move(item, "stock_adj_qty"),
        2,
    )


def _present_qty(item: Dict[str, Any], field: str, *extra_names: str) -> Optional[float]:
    """A printed number, including zero. None stays None and is not a zero."""
    extra = _row_extra(item)
    if field in ("opening_qty", "receipts_qty", "sales_qty", "closing_qty"):
        if field not in item or item.get(field) in (None, ""):
            return None
        return _num(item.get(field))
    for name in extra_names:
        if name in extra and extra.get(name) not in (None, ""):
            return _num(extra.get(name))
    return None


def op_pur_orec_expected_closing(item: Dict[str, Any]) -> Optional[float]:
    """Op + Pur + ORec - Sale - OIss. This layout only. Not the generic equation."""
    opening = _present_qty(item, "opening_qty")
    purchase = _present_qty(item, "receipts_qty")
    other_receipt = _present_qty(item, "", "other_receipt_qty")
    sale = _present_qty(item, "sales_qty")
    other_issue = _present_qty(item, "", "other_issue_qty")
    if None in (opening, purchase, other_receipt, sale, other_issue):
        return None
    return round(opening + purchase + other_receipt - sale - other_issue, 2)


def free_return_expected_closing(item: Dict[str, Any]) -> float:
    return round(
        _free_move(item, "opening_qty")
        + _free_move(item, "purchase_qty", "receipts_qty")
        + _free_move(item, "purchase_free_qty")
        - _free_move(item, "purchase_return_qty")
        - _free_move(item, "purchase_return_free_qty")
        - _free_move(item, "sales_qty")
        - _free_move(item, "sale_free_qty")
        + _free_move(item, "sales_return_qty")
        + _free_move(item, "sales_return_free_qty")
        + _free_move(item, "others_qty"),
        2,
    )


def row_fails(result: Dict[str, Any], item: Dict[str, Any]) -> bool:
    """True when this profile scores the row and the row does not agree."""
    if not isinstance(item, dict):
        return False
    profile = select_profile(result)
    if not profile.scores:
        return False
    return not _row_ok(profile, item)


def failed_rows(result: Dict[str, Any]) -> List[Dict[str, Any]]:
    profile = select_profile(result)
    if not profile.supports_recovery:
        return []
    return [
        item
        for item in (result.get("line_items") or [])
        if isinstance(item, dict) and row_fails(result, item)
    ]


def _row_ok(profile: IdentityProfile, item: Dict[str, Any]) -> bool:
    profile_id = profile.profile_id
    extra = _row_extra(item)
    if profile_id in {PROFILE_GENERIC, PROFILE_OPBAL}:
        return generic_row_ok(item, formula=generic_expected_closing)
    if profile_id == PROFILE_SALERET:
        return generic_row_ok(item, formula=saleret_expected_closing)
    if profile_id == PROFILE_SAMPLE:
        return generic_row_ok(item, formula=sample_expected_closing)
    if profile_id == PROFILE_MONTHLY_SS:
        return _within(monthly_expected_closing(item), _num(item.get("closing_qty")))
    if profile_id == PROFILE_HIMALAYA_DUMP:
        return _within(himalaya_expected_closing(item), _num(item.get("closing_qty")))
    if profile_id in {PROFILE_PRODUCT_WISE, PROFILE_PRODUCT_WISE_IMAGE}:
        return _within(issues_expected_closing(item), _num(item.get("closing_qty")))
    if profile_id == PROFILE_ITEM_PACK_SRETURN:
        if extra.get("qty_parse_errors"):
            return False
        ok, _subtotal, _closing = item_pack_sreturn_figures(item)
        return ok
    if profile_id == PROFILE_SSA_SALES_FREE:
        return _within(ssa_free_expected_closing(item), _ssa_col(item, "closing_qty"))
    if profile_id == PROFILE_MARG:
        ok, _subtotal, _closing = marg_figures(item)
        return ok
    if profile_id == PROFILE_OP_PI:
        return _within(op_pi_expected_closing(item), _op_pi_move(item, "closing_qty"))
    if profile_id == PROFILE_FREE_RETURN:
        if extra.get("qty_parse_errors"):
            return False
        return _within(
            free_return_expected_closing(item), _free_move(item, "closing_qty")
        )
    if profile_id == PROFILE_SWIL_RECEIPT:
        return swil_row_ok(item)
    if profile_id == PROFILE_OP_PUR_OREC:
        expected = op_pur_orec_expected_closing(item)
        closing = _present_qty(item, "closing_qty")
        if expected is None or closing is None:
            return False
        return _within(expected, closing)
    return True
