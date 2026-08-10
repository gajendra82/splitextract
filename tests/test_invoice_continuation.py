"""Invoice continuation + money-like invoice-number rejection."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app  # noqa: E402


INV4567_PAGE1 = """
Tax Invoice
Seller Details Details of Invoice
GSTIN : 27AASPH3785P1ZP Invoice Number : INV4567
ROUNAK MEDICARE & SPECIALITY Invoice Date : 01-Jul-2026
Buyer Details (Bill To)
GSTIN : 27AAHCA1186B1Z0
MAX PHARMACY
A UNIT OF ALEXIS MULTISPECIALI
HOSPITAL PRIVATE LIMITED
SI HSN / SAC - Description GST Taxable Value
1 30049079 - ZYTANIX 5 TAB 15TAB 5 960.30
Quantity: 4 Unit Price: 247.50 Gross Amount: 990.00
Total Amount : 101,867.00
"""

INV4567_PAGE2 = """
SI HSN / SAC - Description GST Taxable Value
NO. Rate CGST
SGST
11 30049087 - LINID TAB 10TAB 5 7,100.40
Quantity: 48 Unit: OTH Unit Price: 152.50 Gross Amount: 7,320.00
12 30049099 - IBSET TAB 15TAB 5 1,571.40
Quantity: 4 Unit Price: 405.00 Gross Amount: 1,620.00
Total Taxable Value 97,015.70
Total Invoice Value 101,867.00
Authorized Signatory
ROUNAK MEDICARE & SPECIALITY
"""

INV_B_PAGE = """
Tax Invoice
GSTIN : 27AASPH3785P1ZP Invoice Number : INV9999
ROUNAK MEDICARE & SPECIALITY Invoice Date : 02-Jul-2026
Buyer Details MAX PHARMACY
SI HSN Description Quantity Unit Price
1 30049099 - OTHER TAB 10TAB 5 100.00
Quantity: 2 Unit Price: 50.00 Gross Amount: 100.00
"""

INV_B_CONTINUATION = """
SI HSN / SAC - Description GST Taxable Value
2 30049099 - FOLLOWUP TAB 10TAB 5 200.00
Quantity: 4 Unit Price: 50.00 Gross Amount: 200.00
Total Invoice Value 300.00
"""

DIFF_CUSTOMER_PAGE = """
SI HSN / SAC - Description GST Taxable Value
1 30049099 - OTHER TAB 10TAB 5 100.00
Quantity: 2 Unit Price: 50.00 Gross Amount: 100.00
Buyer Details
CITY CARE HOSPITAL PVT LTD
GSTIN : 27BBBBB1111B1Z6
"""


def _group_pages(pages):
    """Mirror production grouping without OCR/Gemini."""
    groups = []
    current_invoice = None
    current_pages = []
    current_ocr = ""
    current_data = None
    for idx, (inv_no, ocr, full_data) in enumerate(pages):
        inv_no = app._sanitize_grouping_invoice_no(idx, inv_no)
        if idx == 0:
            current_invoice = inv_no
            current_pages = [idx]
            current_ocr = ocr
            current_data = full_data
            continue
        if app._should_attach_page_to_current_invoice_group(
            idx,
            inv_no,
            ocr,
            current_invoice,
            current_ocr,
            page_full_data=full_data,
            current_full_data=current_data,
        ):
            current_pages.append(idx)
            current_ocr += "\n" + ocr
        else:
            groups.append({
                "invoice_no": current_invoice,
                "pages": current_pages[:],
            })
            current_invoice = inv_no
            current_pages = [idx]
            current_ocr = ocr
            current_data = full_data
    if current_pages:
        groups.append({
            "invoice_no": current_invoice,
            "pages": current_pages[:],
        })
    return groups


class TestMoneyLikeInvoiceNumbers(unittest.TestCase):
    def test_rejects_western_grouped_amount(self):
        self.assertTrue(app._looks_like_monetary_amount("101,867.00"))
        self.assertTrue(app._is_suspicious_invoice_number("101,867.00"))
        self.assertIsNone(app._valid_invoice_no_or_none("101,867.00"))
        self.assertIsNone(app._resolve_page_invoice_for_grouping(
            1, "101,867.00", INV4567_PAGE2))

    def test_rejects_indian_lakh_and_plain_decimal(self):
        self.assertTrue(app._looks_like_monetary_amount("1,00,000.00"))
        self.assertTrue(app._looks_like_monetary_amount("100.00"))
        self.assertTrue(app._is_suspicious_invoice_number("1,00,000.00"))
        self.assertTrue(app._is_suspicious_invoice_number("100.00"))

    def test_rejects_currency_prefixed_ocr_variants(self):
        for value in (
            "₹101,867.00",
            "₹ 101,867.00",
            "Rs. 101,867.00",
            "INR 101,867.00",
            "Rs101867.00",
        ):
            self.assertTrue(
                app._looks_like_monetary_amount(value),
                msg=f"should reject {value!r}",
            )
            self.assertIsNone(app._valid_invoice_no_or_none(value))

    def test_accepts_real_invoice_number(self):
        self.assertFalse(app._looks_like_monetary_amount("INV4567"))
        self.assertFalse(app._is_suspicious_invoice_number("INV4567"))
        self.assertEqual(app._valid_invoice_no_or_none("INV4567"), "INV4567")


class TestTwoPageContinuation(unittest.TestCase):
    def test_header_only_on_page_1_is_one_invoice(self):
        groups = _group_pages([
            ("INV4567", INV4567_PAGE1, {
                "data": {"invoice_summary": {
                    "invoice_no": "INV4567",
                    "vendor": "ROUNAK MEDICARE & SPECIALITY",
                    "customer": "MAX PHARMACY",
                    "total": "101867.00",
                }}
            }),
            (None, INV4567_PAGE2, {"data": {"invoice_summary": {}}}),
        ])
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["invoice_no"], "INV4567")
        self.assertEqual(groups[0]["pages"], [0, 1])

    def test_money_like_page2_invoice_no_is_continuation(self):
        self.assertTrue(app._should_attach_page_to_current_invoice_group(
            1, "101,867.00", INV4567_PAGE2, "INV4567", INV4567_PAGE1,
        ))
        groups = _group_pages([
            ("INV4567", INV4567_PAGE1, None),
            ("101,867.00", INV4567_PAGE2, {
                "invoice_no": "101,867.00",
                "data": {"invoice_summary": {"invoice_no": "101,867.00"}},
            }),
        ])
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["invoice_no"], "INV4567")
        self.assertEqual(groups[0]["pages"], [0, 1])

    def test_page2_missing_header_with_line_items_is_continuation(self):
        self.assertTrue(app._detect_invoice_continuation_page(
            1, None, INV4567_PAGE2, "INV4567", INV4567_PAGE1,
        ))


class TestDoNotBlindlyInherit(unittest.TestCase):
    def test_valid_different_invoice_number_starts_new_invoice(self):
        self.assertFalse(app._should_attach_page_to_current_invoice_group(
            1, "INV9999", INV_B_PAGE, "INV4567", INV4567_PAGE1,
        ))
        groups = _group_pages([
            ("INV4567", INV4567_PAGE1, None),
            ("INV9999", INV_B_PAGE, None),
        ])
        self.assertEqual(len(groups), 2)
        self.assertEqual(groups[0]["invoice_no"], "INV4567")
        self.assertEqual(groups[1]["invoice_no"], "INV9999")

    def test_different_customer_is_not_continuation(self):
        page2_data = {
            "data": {
                "invoice_summary": {
                    "customer": "CITY CARE HOSPITAL PVT LTD",
                    "vendor": "ROUNAK MEDICARE & SPECIALITY",
                }
            }
        }
        page1_data = {
            "data": {
                "invoice_summary": {
                    "invoice_no": "INV4567",
                    "customer": "MAX PHARMACY",
                    "vendor": "ROUNAK MEDICARE & SPECIALITY",
                }
            }
        }
        self.assertFalse(app._should_attach_page_to_current_invoice_group(
            1,
            None,
            DIFF_CUSTOMER_PAGE,
            "INV4567",
            INV4567_PAGE1,
            page_full_data=page2_data,
            current_full_data=page1_data,
        ))


class TestMultiInvoiceDocument(unittest.TestCase):
    def test_new_invoice_after_continuation_keeps_separate_groups(self):
        groups = _group_pages([
            ("INV4567", INV4567_PAGE1, None),
            ("101,867.00", INV4567_PAGE2, None),
            ("INV9999", INV_B_PAGE, None),
            (None, INV_B_CONTINUATION, None),
        ])
        self.assertEqual(len(groups), 2)
        self.assertEqual(groups[0]["invoice_no"], "INV4567")
        self.assertEqual(groups[0]["pages"], [0, 1])
        self.assertEqual(groups[1]["invoice_no"], "INV9999")
        self.assertEqual(groups[1]["pages"], [2, 3])

    def test_continuation_attaches_to_latest_invoice_not_first(self):
        groups = _group_pages([
            ("INVA", INV4567_PAGE1.replace("INV4567", "INVA"), None),
            ("INVB", INV_B_PAGE.replace("INV9999", "INVB"), None),
            (None, INV_B_CONTINUATION, None),
        ])
        self.assertEqual(len(groups), 2)
        self.assertEqual(groups[0]["invoice_no"], "INVA")
        self.assertEqual(groups[0]["pages"], [0])
        self.assertEqual(groups[1]["invoice_no"], "INVB")
        self.assertEqual(groups[1]["pages"], [1, 2])


class TestJacksonContinuationStillWorks(unittest.TestCase):
    def test_jackson_signature_unchanged(self):
        page1 = "JACKSON MEDICALS Inv.No. D4151 ST.THOMAS HOSPITAL Page 1 of 2 BILYPSA TAB"
        page2 = (
            "JACKSON MEDICALS Inv.No. D4151 DUPLICATE COPY Page 2 of 2 "
            "AMLOPIN 5MG TAB 10 20.32 203.20 SUPRADYN DAILY TAB"
        )
        self.assertTrue(app._detect_invoice_continuation_page(
            1, "INV/2023/00010", page2, "D4151", page1
        ))


if __name__ == "__main__":
    unittest.main()
