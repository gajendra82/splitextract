"""QUANTUM HEALTH CARE unlabeled buyer block (Invoice No then hospital/pharmacy)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import (  # noqa: E402
    extract_quantum_health_care_customer_details,
    ocr_suggests_quantum_health_care,
    enforce_schema,
)


QHC_17619 = """
Quantum 1EALTH CARE v TAX INVOICE
No.26,1st Floor,Ist Street,Sakthi Nagar,3rd Cross,E.B.Colony Extension,
Virudhambattu,Katpadi,Vellore-632006,
DLNO: TN-08-20B/21B-00215, TN-08-20/21-08438. Cell: 9003915030, 9003915038.
Email Id: quantumcare2020@ gmail.com GST No ; 33AUMPA8021HIZY
Terms: _D-CREDIT BILL Invoice No: 26-27D17619 Invoice Date: 28/05/2026 Page No: m7
PHARMACY STORES-CMC HOSPITAL RANIPET CAMPUS Code 107 | __L.no.| TN/VLR/20B.21B/00451
UNIT OF CMC VELLORE ASSOCIATION
ROOM NO-00D02, BASEMENT FLOOR.D-BLOCK CMC VELLORE-RANIPET CAMPUS,KILMINNAL [Order No:| 670095
BILYPSA 4 MG TAB (BOTTLE PACK) 1A01788B 1546.60 30049099 123728.00
ITEMS: 1 QTY: 80 AMOUNT: 129914.40
for QUANTUM HEALTH CARE
"""

QHC_17621 = """
QUANTUM HEALTH CARE & TAX INVOICE
No.26,lst Floor,1st Street Sakthi Nagar.3rd Cross,E.B.Colony Extension,
Virudhambattu,Katpadi,Vellore-632006.
GST No: 33AUMPA8021HIZY
Terms: __D-CREDIT BILL Invoice No: 26-27D17621 Invoice Date: 28/05/2026 Page No: 7
PHARMACY STORES-CMC HOSPITAL RANIPET CAMPUS Code: 107 TN/VLR/20B,21B/00451
UNIT OF CMC VELLORE ASSOCIATION [GstNo:| 33AAATC1278NIZN
ROOM NO-00D02,BASEMENT FLOOR,D-BLOCK CMC VELLORE-RANIPET CAMPUS,KILMINNAL
ENTEHEP 0.5 MG TAB SB00153A 24950.00
ITEMS: 1 QTY: 50 AMOUNT: 26197.50
for QUANTUM HEALTH CARE
"""

QHC_17636 = """
QUANTUM HEALTH CARE
No.26,1st Floor, Ist Street, Sakthi Nagar,3rd Cross,E.B.Colony Extension,
Virudhambattu,Katpadi, Vellore-632006.
GST No: 33AUMPA8021HIZY
Terms: _D-CREDIT BILL Invoice No: 26-27D17636 Invoice Date: 28/05/2026 Page No: m7
CMC VELLORE ASSOCIATION PHARMACY SERVICES Code: 06/VLV/163/20/21
CHRISTIAN MEDICAL COLLEGE 33AAATC1278NIZN
IDA SCUDDER ROAD VELLORE - 632004 [Order No:| 670464
BILYPSA 4 MG TAB (BOTTLE PACK) 30932.00
ITEMS: 1 QTY: 20 AMOUNT: 32478.60
for QUANTUM HEALTH CARE
"""

QHC_17229 = """
QUANTUM HEALTH CARE
GST No: 33AUMPA8021HIZY
Terms: _D-CREDIT BILL Invoice No: 26-27D17229 Invoice Date: 09/05/2026 Page No: mi
CMC VELLORE ASSOCIATION PHARMACY SERVICES CHITTOOR Code: 89
190,CMC VELLORE-CHITTOOR CAMPUS.RAMAPURAM 37AAATC1278NIZF
ENTEHEP 0.5 MG TAB 998.00
ITEMS: 1 AMOUNT: 1047.90
for QUANTUM HEALTH CARE
"""


class TestQuantumHealthCareCustomer(unittest.TestCase):
    def test_detects_quantum_layout(self):
        self.assertTrue(ocr_suggests_quantum_health_care(QHC_17619))
        self.assertTrue(ocr_suggests_quantum_health_care(
            "TAX INVOICE", vendor="QUANTUM HEALTH CARE"))
        self.assertFalse(ocr_suggests_quantum_health_care(
            "BETA AGENCIES INV NO 2351 DATE: 12/05/26"))

    def test_recovers_17619_pharmacy_stores_cmc(self):
        details = extract_quantum_health_care_customer_details(QHC_17619)
        cust = details.get("customer", "").upper()
        self.assertIn("PHARMACY STORES", cust)
        self.assertIn("CMC", cust)
        self.assertNotIn("CODE", cust)
        self.assertNotIn("QUANTUM", cust)
        addr = (details.get("customer_address") or "").upper()
        self.assertTrue("RANIPET" in addr or "VELLORE" in addr or "CMC" in addr)

    def test_recovers_sibling_17621_without_merging_logic(self):
        details = extract_quantum_health_care_customer_details(QHC_17621)
        cust = details.get("customer", "").upper()
        self.assertIn("PHARMACY STORES", cust)
        self.assertNotIn("CODE", cust)
        self.assertEqual(details.get("customer_gstin"), "33AAATC1278NIZN")

    def test_recovers_sibling_17636_cmc_vellore_association(self):
        details = extract_quantum_health_care_customer_details(QHC_17636)
        cust = details.get("customer", "").upper()
        self.assertIn("CMC VELLORE", cust)
        self.assertIn("PHARMACY", cust)
        self.assertNotIn("CODE", cust)

    def test_recovers_sibling_17229_chittoor(self):
        details = extract_quantum_health_care_customer_details(QHC_17229)
        cust = details.get("customer", "").upper()
        self.assertIn("CMC VELLORE", cust)
        self.assertIn("CHITTOOR", cust)

    def test_enforce_schema_fills_empty_customer_for_17619(self):
        raw = {
            "data": {
                "ocr_text": QHC_17619,
                "invoice_summary": {
                    "invoice_no": "26-27D17619",
                    "vendor": "QUANTUM HEALTH CARE",
                    "vendor_gstin": "33AUMPA8021HIZY",
                    "customer": "",
                    "customer_address": "",
                    "customer_gstin": "",
                    "total": "129914.40",
                    "tax": "6186.40",
                    "invoice_date": "2026-05-28",
                },
                "line_items": {
                    "items": [{
                        "product_description": "BILYPSA 4 MG TAB (BOTTLE PACK)",
                        "quantity": "80",
                        "unit_price": "1546.60",
                        "total_amount": "123728.00",
                    }]
                },
            }
        }
        formatted = enforce_schema(raw)
        summary = formatted["data"]["invoice_summary"]
        cust = str(summary.get("customer") or "").upper()
        self.assertIn("PHARMACY STORES", cust)
        self.assertNotIn("CODE", cust)
        self.assertTrue(summary.get("customer_address"))
        items = formatted["data"]["line_items"]["items"]
        self.assertGreaterEqual(len(items), 1)
        self.assertEqual(str(summary.get("total")), "129914.40")


if __name__ == "__main__":
    unittest.main()
