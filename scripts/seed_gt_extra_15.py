"""Seed 15 additional SOURCE_VERIFIED GT docs (first-50 OCR subset).

Values verified by visual inspection of original files / rendered pages.
Does not use parser or OCR-engine output as truth.
Preserves the existing 5 verified documents.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.ground_truth.schema import validate_ground_truth_document

OUT = ROOT / "phase4" / "ground_truth_50" / "per_document"
DATASET = Path(r"C:\Users\amnsa\Downloads\ZL_2026_August")

EXISTING = {
    "0000700406_2026_08_ZL_10_384_05092026140711.jpg",
    "0000700835_2026_08_ZL_25_447_03092026105819.jpeg",
    "0000700434_2026_08_ZL_10_386_05092026034503.jpg",
    "0000701078_2026_08_ZL_03_362_05092026080346.jpeg",
    "0000700358_2026_08_ZL_18_8173_05092026185826.pdf",
}


def F(
    field_id: str,
    field_type: str,
    header: str,
    raw: Any,
    *,
    product: Optional[str] = None,
    page: int = 1,
    priority: str = "high",
    notes: Optional[str] = None,
) -> Dict[str, Any]:
    norm = None
    if field_type in {"quantity", "price", "numeric", "percentage"}:
        try:
            norm = float(str(raw).replace(",", "").replace(" ", ""))
        except ValueError:
            norm = None
    return {
        "field_id": field_id,
        "field_type": field_type,
        "printed_header": header,
        "expected_raw": str(raw),
        "expected_normalized": norm,
        "page": page,
        "product_key": product,
        "bbox": None,
        "bbox_verified": False,
        "verified": True,
        "source_verified": True,
        "priority": priority,
        "notes": notes,
    }


def doc(
    document_id: str,
    *,
    source_type: str,
    fields: List[Dict[str, Any]],
    notes: List[str],
    page: int = 1,
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
        "fields": fields,
        "notes": notes,
    }
    ok, errors = validate_ground_truth_document(payload)
    if not ok:
        raise ValueError(f"{document_id}: {errors}")
    return payload


def build() -> List[Dict[str, Any]]:
    docs: List[Dict[str, Any]] = []

    # 1 MUKESH MEDICAL — clean digital photo table
    d = "0000701904_2026_08_ZL_20_8227_05092026154848.jpg"
    docs.append(
        doc(
            d,
            source_type="image",
            notes=["Visual inspection of original JPG."],
            fields=[
                F("vendor", "text", "vendor", "MUKESH MEDICAL AGENCIES", priority="medium"),
                F("h_open", "header", "OPENING STOCK", "OPENING STOCK"),
                F("h_issue", "header", "ISSUE QUANTITY", "ISSUE QUANTITY"),
                F("h_close", "header", "CLOSING STOCK", "CLOSING STOCK"),
                F("aact_op", "quantity", "OPENING STOCK", "24", product="AACTARIL SOAP"),
                F("aact_rec", "quantity", "RECEIVE QUANTITY", "144", product="AACTARIL SOAP"),
                F("aact_iss", "quantity", "ISSUE QUANTITY", "67", product="AACTARIL SOAP"),
                F("aact_cl", "quantity", "CLOSING STOCK", "101", product="AACTARIL SOAP"),
                F("talekt_syp_op", "quantity", "OPENING STOCK", "31", product="TALEKT SYRUP"),
                F("talekt_syp_iss", "quantity", "ISSUE QUANTITY", "3", product="TALEKT SYRUP"),
                F("talekt_syp_cl", "quantity", "CLOSING STOCK", "28", product="TALEKT SYRUP"),
                F("talekt_tab_op", "quantity", "OPENING STOCK", "36", product="TALEKT TAB"),
                F("talekt_tab_iss", "quantity", "ISSUE QUANTITY", "7", product="TALEKT TAB"),
                F("talekt_tab_cl", "quantity", "CLOSING STOCK", "29", product="TALEKT TAB"),
                F("tot_open", "quantity", "OPENING STOCK", "3174", product="TOTAL"),
                F("tot_rec", "quantity", "RECEIVE QUANTITY", "1014", product="TOTAL"),
                F("tot_iss", "quantity", "ISSUE QUANTITY", "1981", product="TOTAL"),
                F("tot_cl", "quantity", "CLOSING STOCK", "2207", product="TOTAL"),
                F("tot_open_val", "price", "OPENING VALUE", "420409.45", product="TOTAL"),
                F("tot_cl_val", "price", "CLOSING VALUE", "297782.21", product="TOTAL"),
            ],
        )
    )

    # 2 HINDUSTAN DRUG SUPPLIERS
    d = "0000701851_2026_08_ZL_20_302_03092026134940.jpg"
    docs.append(
        doc(
            d,
            source_type="image",
            notes=["Visual inspection of original JPG."],
            fields=[
                F("vendor", "text", "vendor", "HINDUSTAN DRUG SUPPLIERS", priority="medium"),
                F("h_op", "header", "Op.Bal. Qty.", "Op.Bal. Qty."),
                F("h_iss", "header", "Issue Qty.", "Issue Qty."),
                F("h_cl", "header", "Closing Balance", "Closing Balance"),
                F("aact_op", "quantity", "Op.Bal. Qty.", "5", product="AACTARIL SOAP"),
                F("aact_rec", "quantity", "Receipt Qty.", "72", product="AACTARIL SOAP"),
                F("aact_tot", "quantity", "Total Qty.", "77", product="AACTARIL SOAP"),
                F("aact_cl", "quantity", "Closing Balance", "77", product="AACTARIL SOAP"),
                F("abana_op", "quantity", "Op.Bal. Qty.", "71", product="ABANA TAB"),
                F("abana_iss", "quantity", "Issue Qty.", "4", product="ABANA TAB"),
                F("abana_cl", "quantity", "Closing Balance", "67", product="ABANA TAB"),
                F("livhb_op", "quantity", "Op.Bal. Qty.", "80", product="LIV-52 HB CAP"),
                F("livhb_rec", "quantity", "Receipt Qty.", "84", product="LIV-52 HB CAP"),
                F("livhb_iss", "quantity", "Issue Qty.", "75", product="LIV-52 HB CAP"),
                F("livhb_cl", "quantity", "Closing Balance", "89", product="LIV-52 HB CAP"),
                F("liv200_op", "quantity", "Op.Bal. Qty.", "120", product="LIV-52 SYP."),
                F("liv200_rec", "quantity", "Receipt Qty.", "525", product="LIV-52 SYP."),
                F("liv200_iss", "quantity", "Issue Qty.", "441", product="LIV-52 SYP."),
                F("liv200_cl", "quantity", "Closing Balance", "204", product="LIV-52 SYP."),
                F("tent_op", "quantity", "Op.Bal. Qty.", "180", product="TENTEX FORTE"),
                F("tent_iss", "quantity", "Issue Qty.", "65", product="TENTEX FORTE"),
                F("tent_cl", "quantity", "Closing Balance", "115", product="TENTEX FORTE"),
            ],
        )
    )

    # 3 TIRTHAMOYEE — monospace stock & sales
    d = "0000701275_2026_08_ZL_03_360_02092026152716.jpg"
    docs.append(
        doc(
            d,
            source_type="image",
            notes=["Visual inspection of original JPG (STOCK & SALES ANALYSIS)."],
            fields=[
                F("vendor", "text", "vendor", "TIRTHAMOYEE DRUGS", priority="medium"),
                F("h_open", "header", "OPENING", "OPENING"),
                F("h_issue", "header", "ISSUE", "ISSUE"),
                F("h_close", "header", "CLOSING", "CLOSING"),
                F("aact_op", "quantity", "OPENING", "72", product="AACTARIL SOAP"),
                F("aact_cl", "quantity", "CLOSING", "72", product="AACTARIL SOAP"),
                F("aact_exp", "date", "M.EXP", "12/27", product="AACTARIL SOAP"),
                F("abana_op", "quantity", "OPENING", "100", product="ABANA TABLET"),
                F("abana_iss", "quantity", "ISSUE", "3", product="ABANA TABLET"),
                F("abana_cl", "quantity", "CLOSING", "97", product="ABANA TABLET"),
                F("diab_op", "quantity", "OPENING", "89", product="DIABECON TAB"),
                F("diab_cl", "quantity", "CLOSING", "89", product="DIABECON TAB"),
                F("him_op", "quantity", "OPENING", "36", product="HIMCOLIN OINTMENT"),
                F("him_rec", "quantity", "RECEIPT", "50", product="HIMCOLIN OINTMENT"),
                F("him_iss", "quantity", "ISSUE", "5", product="HIMCOLIN OINTMENT"),
                F("him_cl", "quantity", "CLOSING", "81", product="HIMCOLIN OINTMENT"),
                F("liv100_op", "quantity", "OPENING", "57", product="LIV 52 SYP 100 ML"),
                F("liv100_rec", "quantity", "RECEIPT", "1400", product="LIV 52 SYP 100 ML"),
                F("liv100_iss", "quantity", "ISSUE", "140", product="LIV 52 SYP 100 ML"),
                F("liv100_cl", "quantity", "CLOSING", "1317", product="LIV 52 SYP 100 ML"),
                F("tot_open", "quantity", "OPENING", "503184", product="TOTAL"),
                F("tot_rec", "quantity", "RECEIPT", "277797", product="TOTAL"),
                F("tot_iss", "quantity", "ISSUE", "110666", product="TOTAL"),
                F("tot_cl", "quantity", "CLOSING", "670315", product="TOTAL"),
            ],
        )
    )

    # 4 PRESAGE PHARMA ZEAL.TXT screenshot
    d = "0000701012_2026_08_ZL_11_423_03092026183736.jpg"
    docs.append(
        doc(
            d,
            source_type="image",
            notes=["Visual inspection of original JPG (STOCK STATUS REPORT screenshot)."],
            fields=[
                F("vendor", "text", "vendor", "PRESAGE PHARMA", priority="medium"),
                F("h_cl", "header", "CLSTOCK", "CLSTOCK"),
                F("h_sales", "header", "SALES", "SALES"),
                F("aact_cl", "quantity", "CLSTOCK", "88", product="AACTARIL SOAP"),
                F("aact_sales", "quantity", "SALES", "8", product="AACTARIL SOAP"),
                F("aact_clval", "price", "CLVALUE", "7710.56", product="AACTARIL SOAP"),
                F("abana_cl", "quantity", "CLSTOCK", "79", product="ABANA TABLETS"),
                F("abana_sales", "quantity", "SALES", "3", product="ABANA TABLETS"),
                F("liv_cl", "quantity", "CLSTOCK", "848", product="LIV 52 TAB"),
                F("liv_sales", "quantity", "SALES", "179", product="LIV 52 TAB"),
                F("pilex_cl", "quantity", "CLSTOCK", "832", product="PILEX TAB"),
                F("pilex_sales", "quantity", "SALES", "451", product="PILEX TAB"),
                F("tot_clval", "price", "CLVALUE", "549791.98", product="TOTAL"),
                F("tot_sval", "price", "SALESVALUE", "205371.75", product="TOTAL"),
            ],
        )
    )

    # 5 NAYAN PHARMACEUTICAL — scanned product-wise photo
    d = "0000700317_2026_08_ZL_18_374_02092026171443.pdf"
    docs.append(
        doc(
            d,
            source_type="pdf",
            notes=["Rendered page-1 visually inspected."],
            fields=[
                F("vendor", "text", "vendor", "NAYAN PHARMACEUTICAL AGENCIES", priority="medium"),
                F("h_open", "header", "Opening", "Opening"),
                F("h_issues", "header", "Issues", "Issues"),
                F("h_close", "header", "Closing", "Closing"),
                F("clar_op", "quantity", "Opening", "90", product="CLARINA FACE WASH GEL"),
                F("clar_cl", "quantity", "Closing", "90", product="CLARINA FACE WASH GEL"),
                F("him_op", "quantity", "Opening", "17", product="HIMCOLIN GEL"),
                F("him_iss", "quantity", "Issues", "3", product="HIMCOLIN GEL"),
                F("him_cl", "quantity", "Closing", "14", product="HIMCOLIN GEL"),
                F("livtab_op", "quantity", "Opening", "80", product="LIV 52 TABS"),
                F("livtab_iss", "quantity", "Issues", "12", product="LIV 52 TABS"),
                F("livtab_cl", "quantity", "Closing", "68", product="LIV 52 TABS"),
                F("liv200_op", "quantity", "Opening", "51", product="LIV-52 SYP."),
                F("liv200_iss", "quantity", "Issues", "49", product="LIV-52 SYP."),
                F("liv200_cl", "quantity", "Closing", "2", product="LIV-52 SYP."),
                F("pilex_op", "quantity", "Opening", "114", product="PILEX TABS"),
                F("pilex_iss", "quantity", "Issues", "48", product="PILEX TABS"),
                F("pilex_cl", "quantity", "Closing", "66", product="PILEX TABS"),
                F("tot_sales", "price", "Sales amount", "22973.00", product="TOTAL"),
                F("tot_close", "price", "Closing amount", "89086.00", product="TOTAL"),
            ],
        )
    )

    # 6 KALINGA — use printed values (ignore handwritten overrides for GT)
    d = "0000700275_2026_08_ZL_18_374_04092026171615.pdf"
    docs.append(
        doc(
            d,
            source_type="pdf",
            notes=[
                "Rendered page-1 visually inspected.",
                "GT uses printed values; handwritten blue overrides noted but not used as printed truth.",
            ],
            fields=[
                F("vendor", "text", "vendor", "KALINGA PHARMACEUTICALS", priority="medium"),
                F("h_open", "header", "Opening", "Opening"),
                F("h_issues", "header", "Issues", "Issues"),
                F("h_close", "header", "Closing", "Closing"),
                F("abana_op", "quantity", "Opening", "68", product="ABANA TAB"),
                F("abana_iss", "quantity", "Issues", "10", product="ABANA TAB"),
                F("abana_cl", "quantity", "Closing", "58", product="ABANA TAB"),
                F("diab_op", "quantity", "Opening", "76", product="DIABECON TAB"),
                F("diab_iss", "quantity", "Issues", "1", product="DIABECON TAB"),
                F("diab_cl", "quantity", "Closing", "75", product="DIABECON TAB"),
                F("him_op", "quantity", "Opening", "45", product="HIMCOLIN GEL"),
                F("him_iss", "quantity", "Issues", "2", product="HIMCOLIN GEL"),
                F("him_cl", "quantity", "Closing", "43", product="HIMCOLIN GEL"),
                F("liv100_op", "quantity", "Opening", "72", product="LIV-52 SYRUP"),
                F("liv100_iss", "quantity", "Issues", "35", product="LIV-52 SYRUP"),
                F(
                    "liv100_cl",
                    "quantity",
                    "Closing",
                    "37",
                    product="LIV-52 SYRUP",
                    notes="Printed closing before handwritten strike-through.",
                ),
                F("talekt_op", "quantity", "Opening", "47", product="TALEKT TABLETS"),
                F("talekt_iss", "quantity", "Issues", "3", product="TALEKT TABLETS"),
                F("talekt_cl", "quantity", "Closing", "44", product="TALEKT TABLETS"),
                F("tent_op", "quantity", "Opening", "152", product="TENTEX FORTE TAB"),
                F("tent_cl", "quantity", "Closing", "152", product="TENTEX FORTE TAB"),
            ],
        )
    )

    # 7 SANTOSH MEDICAL AGENCY
    d = "0000700364_2026_08_ZL_18_8173_05092026190156.pdf"
    docs.append(
        doc(
            d,
            source_type="pdf",
            notes=["Rendered page-1 visually inspected."],
            fields=[
                F("vendor", "text", "vendor", "SANTOSH MEDICAL AGENCY", priority="medium"),
                F("h_op", "header", "Op.Bal. Qty.", "Op.Bal. Qty."),
                F("h_iss", "header", "Issue Qty.", "Issue Qty."),
                F("h_cl", "header", "Closing Balance", "Closing Balance"),
                F("aact_op", "quantity", "Op.Bal. Qty.", "22", product="AACTARIL SOAP"),
                F("aact_iss", "quantity", "Issue Qty.", "8", product="AACTARIL SOAP"),
                F("aact_cl", "quantity", "Closing Balance", "14", product="AACTARIL SOAP"),
                F("conf_op", "quantity", "Op.Bal. Qty.", "21", product="CONFIDO TABLET"),
                F("conf_rec", "quantity", "Receipt Qty.", "100", product="CONFIDO TABLET"),
                F("conf_iss", "quantity", "Issue Qty.", "32", product="CONFIDO TABLET"),
                F("conf_cl", "quantity", "Closing Balance", "89", product="CONFIDO TABLET"),
                F("liv100_op", "quantity", "Op.Bal. Qty.", "76", product="LIV 52 SYRUP"),
                F("liv100_rec", "quantity", "Receipt Qty.", "70", product="LIV 52 SYRUP"),
                F("liv100_iss", "quantity", "Issue Qty.", "92", product="LIV 52 SYRUP"),
                F("liv100_cl", "quantity", "Closing Balance", "54", product="LIV 52 SYRUP"),
                F("tent_op", "quantity", "Op.Bal. Qty.", "185", product="TENTEX FORTE TAB"),
                F("tent_iss", "quantity", "Issue Qty.", "48", product="TENTEX FORTE TAB"),
                F("tent_cl", "quantity", "Closing Balance", "137", product="TENTEX FORTE TAB"),
                F("tot_op", "quantity", "Op.Bal. Qty.", "157673", product="TOTAL"),
                F("tot_cl", "quantity", "Closing Balance", "97537", product="TOTAL"),
            ],
        )
    )

    # 8 PATEL DRUG AG
    d = "0000700124_2026_08_ZL_06_521_02092026204703.pdf"
    docs.append(
        doc(
            d,
            source_type="pdf",
            notes=["Rendered page-1 visually inspected (PATEL DRUG AG)."],
            fields=[
                F("vendor", "text", "vendor", "PATEL DRUG AG", priority="medium"),
                F("h_op", "header", "Op Qty", "Op Qty"),
                F("h_sales", "header", "Sales Qty", "Sales Qty"),
                F("h_cl", "header", "Closing", "Closing"),
                F("aact_op", "quantity", "Op Qty", "20", product="AACTARIL SOAP"),
                F("aact_pur", "quantity", "Pur Qty", "144", product="AACTARIL SOAP"),
                F("aact_sales", "quantity", "Sales Qty", "40", product="AACTARIL SOAP"),
                F("aact_cl", "quantity", "Closing", "124", product="AACTARIL SOAP"),
                F("talekt_tab_op", "quantity", "Op Qty", "30", product="TALEKT TAB"),
                F("talekt_tab_pur", "quantity", "Pur Qty", "50", product="TALEKT TAB"),
                F("talekt_tab_sales", "quantity", "Sales Qty", "3", product="TALEKT TAB"),
                F("talekt_tab_cl", "quantity", "Closing", "77", product="TALEKT TAB"),
            ],
        )
    )

    # 9 JAJRA DRUG AGENCY
    d = "0000701864_2026_08_ZL_20_5015_03092026091037.jpg"
    docs.append(
        doc(
            d,
            source_type="image",
            notes=["Visual inspection of original JPG."],
            fields=[
                F("vendor", "text", "vendor", "JAJRA DRUG AGENCY", priority="medium"),
                F("h_op", "header", "Op.Bal. Qty.", "Op.Bal. Qty."),
                F("h_iss", "header", "Issue Qty.", "Issue Qty."),
                F("h_cl", "header", "Closing Balance", "Closing Balance"),
                F("abana_op", "quantity", "Op.Bal. Qty.", "49", product="ABANA"),
                F("abana_iss", "quantity", "Issue Qty.", "11", product="ABANA"),
                F("abana_cl", "quantity", "Closing Balance", "38", product="ABANA"),
                F("conf_op", "quantity", "Op.Bal. Qty.", "3", product="CONFIDO TAB"),
                F("conf_rec", "quantity", "Receipt Qty.", "100", product="CONFIDO TAB"),
                F("conf_iss", "quantity", "Issue Qty.", "13", product="CONFIDO TAB"),
                F("conf_cl", "quantity", "Closing Balance", "90", product="CONFIDO TAB"),
                F("liv_op", "quantity", "Op.Bal. Qty.", "69", product="LIV-52 SYRUP"),
                F("liv_rec", "quantity", "Receipt Qty.", "70", product="LIV-52 SYRUP"),
                F("liv_iss", "quantity", "Issue Qty.", "54", product="LIV-52 SYRUP"),
                F("liv_cl", "quantity", "Closing Balance", "85", product="LIV-52 SYRUP"),
                F("pilex_op", "quantity", "Op.Bal. Qty.", "43", product="PILEX TAB"),
                F("pilex_iss", "quantity", "Issue Qty.", "38", product="PILEX TAB"),
                F("pilex_cl", "quantity", "Closing Balance", "5", product="PILEX TAB"),
                F("tot_op", "quantity", "Op.Bal. Qty.", "95222", product="TOTAL"),
                F("tot_cl", "quantity", "Closing Balance", "101577", product="TOTAL"),
            ],
        )
    )

    # 10 JAIPUR MEDICAL HALL
    d = "0000701151_2026_08_ZL_14_361_03092026025650.jpg"
    docs.append(
        doc(
            d,
            source_type="image",
            notes=["Visual inspection of original JPG (two-page collage; page1+page2)."],
            fields=[
                F("vendor", "text", "vendor", "JAIPUR MEDICAL HALL", priority="medium"),
                F("h_op", "header", "Op.Bal. Qty.", "Op.Bal. Qty."),
                F("h_iss", "header", "Issue Qty.", "Issue Qty."),
                F("h_cl", "header", "Closing Balance", "Closing Balance"),
                F("aact_op", "quantity", "Op.Bal. Qty.", "68", product="AACTARIL SOAP"),
                F("aact_iss", "quantity", "Issue Qty.", "14", product="AACTARIL SOAP"),
                F("aact_cl", "quantity", "Closing Balance", "54", product="AACTARIL SOAP"),
                F("clar_op", "quantity", "Op.Bal. Qty.", "75", product="CLARINA 30GM CREA"),
                F("clar_iss", "quantity", "Issue Qty.", "6", product="CLARINA 30GM CREA"),
                F("clar_cl", "quantity", "Closing Balance", "69", product="CLARINA 30GM CREA"),
                F("talekt_op", "quantity", "Op.Bal. Qty.", "31", product="TALEKT CAPS"),
                F("talekt_iss", "quantity", "Issue Qty.", "1", product="TALEKT CAPS"),
                F("talekt_cl", "quantity", "Closing Balance", "30", product="TALEKT CAPS"),
                F("yash_op", "quantity", "Op.Bal. Qty.", "74", product="YASHTIMADHU CAPS"),
                F("yash_rec", "quantity", "Receipt Qty.", "60", product="YASHTIMADHU CAPS"),
                F("yash_iss", "quantity", "Issue Qty.", "45", product="YASHTIMADHU CAPS"),
                F("yash_cl", "quantity", "Closing Balance", "89", product="YASHTIMADHU CAPS"),
                F("tot_op", "quantity", "Op.Bal. Qty.", "834578", product="TOTAL"),
                F("tot_cl", "quantity", "Closing Balance", "775274", product="TOTAL"),
            ],
        )
    )

    # 11 AMAR PHARMACEUTICAL — group wise sales (wrinkled)
    d = "0000700209_2026_08_ZL_18_374_04092026060313.pdf"
    docs.append(
        doc(
            d,
            source_type="pdf",
            notes=["Rendered page-1 visually inspected (wrinkled photo)."],
            fields=[
                F("vendor", "text", "vendor", "AMAR PHARMACEUTICAL AGENCIES", priority="medium"),
                F("h_op", "header", "Op. Stock", "Op. Stock"),
                F("h_cl", "header", "Cl. Stock As On", "Cl. Stock As On"),
                F("aact_op", "quantity", "Op. Stock", "69", product="AACTARIL SOAP"),
                F("aact_cl", "quantity", "Cl. Stock As On", "69", product="AACTARIL SOAP"),
                F("gasex_op", "quantity", "Op. Stock", "49", product="GASEX ELAICHI"),
                F("gasex_sales", "quantity", "Sales", "2", product="GASEX ELAICHI"),
                F("gasex_cl", "quantity", "Cl. Stock As On", "47", product="GASEX ELAICHI"),
                F("liv200_op", "quantity", "Op. Stock", "615", product="LIV-52 SYRUP"),
                F("liv200_sales", "quantity", "Sales", "123", product="LIV-52 SYRUP"),
                F("liv200_cl", "quantity", "Cl. Stock As On", "492", product="LIV-52 SYRUP"),
                F("tent_op", "quantity", "Op. Stock", "73", product="TENTEX FORTE TAB"),
                F("tent_sales", "quantity", "Sales", "10", product="TENTEX FORTE TAB"),
                F("tent_cl", "quantity", "Cl. Stock As On", "63", product="TENTEX FORTE TAB"),
                F("ren_op", "quantity", "Op. Stock", "12", product="RENALKA SYRUP"),
                F("ren_cl", "quantity", "Cl. Stock As On", "12", product="RENALKA SYRUP"),
            ],
        )
    )

    # 12 JIGAR PHARMA stock statement
    d = "0000700084_2026_08_ZL_06_8236_03092026155136.pdf"
    docs.append(
        doc(
            d,
            source_type="pdf",
            notes=["Rendered page-1 visually inspected."],
            fields=[
                F("vendor", "text", "vendor", "JIGAR PHARMA", priority="medium"),
                F("h_op", "header", "OpStk", "OpStk"),
                F("h_sale", "header", "Sale Qty", "Sale Qty"),
                F("h_cl", "header", "Clstk", "Clstk"),
                F("act_op", "quantity", "OpStk", "64.0", product="ACTARIL SOAP"),
                F("act_sale", "quantity", "Sale Qty", "20.0", product="ACTARIL SOAP"),
                F("act_cl", "quantity", "Clstk", "44.0", product="ACTARIL SOAP"),
                F("act_exp", "date", "Early Exp.", "04/2029", product="ACTARIL SOAP"),
                F("talekt_cap_op", "quantity", "OpStk", "35.0", product="TALEKT CAP."),
                F("talekt_cap_cl", "quantity", "Clstk", "35.0", product="TALEKT CAP."),
                F("talekt_syp_op", "quantity", "OpStk", "89.0", product="TALEKT SYP."),
                F("talekt_syp_cl", "quantity", "Clstk", "89.0", product="TALEKT SYP."),
                F("tot_op_rs", "price", "Op.Rs.", "155431.48", product="TOTAL"),
                F("tot_sale_rs", "price", "Sale Rs.", "61250.78", product="TOTAL"),
                F("tot_cl_rs", "price", "Cl. Rs.", "141755.46", product="TOTAL"),
            ],
        )
    )

    # 13 MOHAN PHARMACEUTICALS — sparse photographed table
    d = "0000700308_2026_08_ZL_18_8175_03092026072320.jpg"
    docs.append(
        doc(
            d,
            source_type="image",
            notes=["Visual inspection of original JPG (folded photo; sparse table)."],
            fields=[
                F("vendor", "text", "vendor", "MOHAN PHARMACEUTICALS", priority="medium"),
                F("h_open", "header", "Opening", "Opening"),
                F("h_issues", "header", "Issues", "Issues"),
                F("h_close", "header", "Closing", "Closing"),
                F("liv_rec", "quantity", "Receipt", "70", product="LIV-52 SYP"),
                F("liv_iss", "quantity", "Issues", "6", product="LIV-52 SYP"),
                F("liv_cl", "quantity", "Closing", "64", product="LIV-52 SYP"),
                F("tot_sales", "price", "Total sales", "554.00", product="TOTAL"),
                F("tot_close", "price", "Total closing stock", "5933.00", product="TOTAL"),
            ],
        )
    )

    # 14 RAJARAM AGENCIES stock report
    d = "0000700346_2026_08_ZL_18_8189_05092026025159.pdf"
    docs.append(
        doc(
            d,
            source_type="pdf",
            notes=["Rendered page-1 visually inspected."],
            fields=[
                F("vendor", "text", "vendor", "RAJARAM AGENCIES", priority="medium"),
                F("h_ob", "header", "OB", "OB"),
                F("h_sal", "header", "SAL", "SAL"),
                F("h_cb", "header", "CB", "CB"),
                F("aact_ob", "quantity", "OB", "0", product="AACTARIL SOAP"),
                F("aact_cb", "quantity", "CB", "0", product="AACTARIL SOAP"),
                F("bon_ob", "quantity", "OB", "241", product="BONISAN 200ML"),
                F("bon_sal", "quantity", "SAL", "83", product="BONISAN 200ML"),
                F("bon_cb", "quantity", "CB", "156", product="BONISAN 200ML"),
                F("cyst_ob", "quantity", "OB", "27", product="CYSTONE TAB"),
                F("cyst_sal", "quantity", "SAL", "14", product="CYSTONE TAB"),
                F("cyst_cb", "quantity", "CB", "13", product="CYSTONE TAB"),
                F("liv200_ob", "quantity", "OB", "35", product="LIV-52 SYP 200ML"),
                F("liv200_sal", "quantity", "SAL", "33", product="LIV-52 SYP 200ML"),
                F("liv200_cb", "quantity", "CB", "2", product="LIV-52 SYP 200ML"),
                F("livds_ob", "quantity", "OB", "70", product="LIV 52 DS TAB"),
                F("livds_sal", "quantity", "SAL", "16", product="LIV 52 DS TAB"),
                F("livds_cb", "quantity", "CB", "54", product="LIV 52 DS TAB"),
            ],
        )
    )

    # 15 ORDER FORM — printed products + clear handwritten qty
    d = "0000704337_2026_08_ZL_24_259_03092026042213.pdf"
    docs.append(
        doc(
            d,
            source_type="pdf",
            notes=[
                "Rendered page-1 visually inspected (pink ORDER FORM).",
                "Qty values are handwritten on the source; verified as written.",
            ],
            fields=[
                F("h_order", "header", "ORDER FORM", "ORDER FORM"),
                F("h_qty", "header", "Qty.", "Qty."),
                F("aact_qty", "quantity", "Qty.", "80", product="Aactaril Soap"),
                F("clar_qty", "quantity", "Qty.", "1", product="Clarina Anti-acne Cream"),
                F("conf_qty", "quantity", "Qty.", "2", product="Confido Tablets"),
                F("diab_qty", "quantity", "Qty.", "10", product="Diabecon DS Tablets"),
                F("liv_syp_qty", "quantity", "Qty.", "80", product="Liv.52 Syrup"),
                F("liv_tab_qty", "quantity", "Qty.", "20", product="Liv.52 Tablets"),
                F("oph_qty", "quantity", "Qty.", "5", product="Ophthacare Eye Drops"),
                F("oxi_qty", "quantity", "Qty.", "120", product="Oxitard Capsules"),
                F("pilex_oint_qty", "quantity", "Qty.", "10", product="Pilex forte Ointment"),
                F("pilex_tab_qty", "quantity", "Qty.", "10", product="Pilex Tablets"),
            ],
        )
    )

    assert len(docs) == 15, len(docs)
    for payload in docs:
        assert payload["document_id"] not in EXISTING
    return docs


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    docs = build()
    for payload in docs:
        path = OUT / f"{payload['document_id']}.json"
        if path.exists() and payload["document_id"] in EXISTING:
            raise SystemExit(f"refusing to overwrite existing verified doc {path}")
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print("wrote", path.name, "fields", len(payload["fields"]))

    # Candidates file for human CLI resume/expansion
    candidates = {"items": []}
    for payload in docs:
        for field in payload["fields"]:
            candidates["items"].append(
                {
                    "document_id": payload["document_id"],
                    "source_file": payload["source"]["file"],
                    "source_type": payload["source"]["source_type"],
                    "page": payload["page"],
                    **{k: field[k] for k in ("field_id", "field_type", "printed_header", "expected_raw", "product_key", "priority")},
                }
            )
    cand_path = OUT.parent / "verification_candidates.json"
    cand_path.write_text(json.dumps(candidates, indent=2), encoding="utf-8")

    all_ids = sorted(EXISTING | {d["document_id"] for d in docs})
    index = {
        "source_verified_documents": len(all_ids),
        "previous_verified": sorted(EXISTING),
        "newly_verified": [d["document_id"] for d in docs],
        "document_ids": all_ids,
        "new_fields": sum(len(d["fields"]) for d in docs),
        "provenance": "SOURCE_VERIFIED",
        "verification_method": "source_visual_inspection",
        "status": "COMPLETE" if len(all_ids) >= 20 else "PARTIAL",
    }
    (OUT.parent / "seed_index_20.json").write_text(json.dumps(index, indent=2), encoding="utf-8")
    print(json.dumps(index, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
