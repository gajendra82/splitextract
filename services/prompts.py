"""Shared OCR text preparation and prompt helpers."""

import re


def prepare_ocr_for_llm(text: str, max_chars: int = 60000) -> str:
    """
    Clean and truncate OCR text before sending to text LLMs.

    Strips duplicate pipe-delimited column dumps that pdfplumber emits on
    multi-column invoices, keeping the compact readable render.
    """
    if not text:
        return ""

    page_sep = re.compile(r"(?=--- Page \d+ ---)")
    parts = page_sep.split(text)

    cleaned_parts = []
    for part in parts:
        pipe_header = re.search(
            r"\bSN\.\s*\|\s*QTY\s*\|\s*FREE\s*\|", part, re.IGNORECASE
        )
        if pipe_header:
            part = part[: pipe_header.start()].rstrip()
        cleaned_parts.append(part)

    cleaned = "\n".join(cleaned_parts)

    if len(cleaned) > max_chars:
        truncated = cleaned[:max_chars]
        last_nl = truncated.rfind("\n")
        if last_nl > max_chars * 0.8:
            truncated = truncated[:last_nl]
        cleaned = truncated + "\n[... OCR truncated ...]"

    return cleaned


STRICT_JSON_RULES = """
RULES:
- Return JSON only. No markdown fences. No commentary.
- Never hallucinate. Missing values must be null.
- Preserve invoice formatting, decimals, GSTIN, and line items exactly as in OCR.
- Do not change field names.
- Extract EVERY line item from the invoice table.
- unit_price MUST come from Rate/S.Rate column, NOT M.R.P or tax percentages.
- total MUST be NET AMOUNT / Grand Total / Invoice Total, not a line item amount.
- Extract vendor_gstin and customer_gstin as 15-character GSTIN codes when present.
- invoice_date must be YYYY-MM-DD when parseable.
"""

STRICT_JSON_SCHEMA = """
JSON SCHEMA:
{
"invoice_no": null,
"vendor": null,
"vendor_gstin": null,
"customer": null,
"customer_address": null,
"customer_gstin": null,
"invoice_date": null,
"invoice_date_raw": null,
"total": null,
"tax": null,
"irn": null,
"line_items": [{
    "product_description": null,
    "quantity": null,
    "unit_price": null,
    "total_amount": null,
    "hsn_code": null,
    "lot_batch_number": null,
    "sku_code": null,
    "unit_of_measure": null,
    "tax_amount": null,
    "discount": null,
    "additional_fields": {
        "mrp": null,
        "mfg": null,
        "expiry_date": null,
        "free_quantity": null,
        "gross_amount": null,
        "discount_percentage": null
    }
}]
}
"""


def build_strict_text_prompt(ocr_text: str, detailed_prompt_body: str = "") -> str:
    """Build a strict text-only extraction prompt."""
    prepared = prepare_ocr_for_llm(ocr_text)
    body = detailed_prompt_body or (STRICT_JSON_RULES + "\n" + STRICT_JSON_SCHEMA)
    return (
        f"{body}\n\n"
        f"INVOICE TEXT:\n{prepared}\n\n"
        "Return ONLY JSON (do not include ocr_text):"
    )


def build_excel_table_prompt(table_text: str) -> str:
    """Multi-invoice GRN/POD spreadsheet prompt. Does not alter PDF invoice prompts."""
    prepared = prepare_ocr_for_llm(table_text, max_chars=40000)
    return f"""Extract EVERY sales / GRN / POD row from this spreadsheet as invoices.

The grid may be:
- one row per product with Invoice Number + Hospital Name / Card Name / Customer Name
- a dispatch list (Customer Name, invoice Date, Item Name, Shipment Qty) with no invoice number
- hospitals as column headers with quantities at the intersections

Rules:
- Return JSON only. No markdown.
- Each distinct invoice number is one invoice. If invoice number is missing, group by hospital/customer name.
- Hospital Name, Card Name, Customer Name, or a hospital-named column is the customer (hospital).
- Shipped Unit / stockist / manufacturer is the vendor when present.
- Skip stock-statement sheets (opening / purchases / closing only).
- Do not invent hospitals or products. Drop rows with quantity 0 or blank.
- invoice_date must be YYYY-MM-DD when parseable.

JSON:
{{
  "invoices": [
    {{
      "invoice_no": null,
      "invoice_date": null,
      "vendor": null,
      "customer": null,
      "customer_address": null,
      "total": null,
      "line_items": [
        {{
          "product_description": null,
          "quantity": null,
          "unit_price": null,
          "total_amount": null,
          "sku_code": null,
          "lot_batch_number": null,
          "unit_of_measure": null
        }}
      ]
    }}
  ]
}}

SPREADSHEET:
{prepared}
"""
