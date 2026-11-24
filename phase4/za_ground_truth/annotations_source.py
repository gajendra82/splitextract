"""Source-verified ZA labels from visual inspection of the original images.

Two reads of each image agreed. Handwritten cells and columns that are not
printed on the page are omitted (treated as unresolved / not present).
These labels were not copied from Tesseract or PaddleOCR-VL caches.
"""

from __future__ import annotations

# (product, packing, opening, receipt, issue, closing)
# None means the column is not on this layout.
CHHAYA_ROWS = [
    ("BONNISAN 200ML SYP", "1X200ML", 44, 0, 6, 18),
    ("BONNISAN-100ML SYP", "1X100ML", 1, 0, 0, 1),
    ("BONNISAN-DROP 30ML", "1X30ML", 24, 0, 2, 42),
    ("BONNISPAZ DROPS", "1X15ML", 0, 0, 0, 0),
    ("BRESOL-100ML SYP", "1X100ML", 0, 0, 0, 0),
    ("BRESOL-200ML SYP", "1X200ML", 117, 0, 88, 29),
    ("BRESOL-NS DROP", "1X10ML", 0, 0, 0, 0),
    ("CYSTONE 100ML SYP", "1X100ML", 0, 0, 0, 0),
    ("CYSTONE 200ML SYP", "1X200ML", 58, 0, 48, 10),
    ("CYSTONE SF SYP 200ML", "1X200ML", 28, 0, 0, 28),
    ("CYSTONE TAB", "1X60TAB", 104, 100, 200, 4),
    ("CYSTONE-FORTE TAB", "1X30TAB", 32, 0, 60, 2),
    ("EVECARE 200ML SYP", "1X200ML", 62, 84, 31, 51),
    ("EVECARE-400ML SYP", "1X400ML", 21, 12, 6, 27),
    ("EVECARE-FORTE 200ML", "1X200ML", 37, 56, 3, 90),
    ("EVECARE-FORTE TAB", "1X10TAB", 43, 0, 12, 31),
    ("EVEGEL 30GM", "1X30GM", 0, 0, 0, 0),
    ("HIMPLASIA TAB", "1X30TAB", 90, 0, 5, 85),
    ("LIV-52 DROP 100ML", "1X100ML", 46, 56, 10, 92),
    # LIV-52 DROP 60ML and LIV-52 SYP 200ML were identical in both visual reads.
    # That duplication is not trusted, so both rows are left unresolved.
    ("LIV-52 DS 100ML SYP", "1X100ML", 62, 0, 6, 56),
    ("LIV-52 DS 200ML SYP", "1X200ML", 157, 0, 127, 30),
    ("LIV-52 DS TAB", "1X60TAB", 28, 200, 94, 134),
    ("LUKUL SYP 200ML", "1X200ML", 55, 0, 14, 41),
    ("LUKUL TAB", "1X60TAB", 25, 100, 80, 45),
    ("MENTAT TAB", "1X60TAB", 63, 0, 11, 52),
    ("MENTAT SYP 200ML", "1X200ML", 14, 56, 19, 51),
    ("PLATENZA TAB", "1X30TAB", 114, 0, 114, 0),
    ("PLATENZA-SYP-100ML", "1X100ML", 49, 0, 13, 36),
    ("SEPTILIN TAB", "1X60TAB", 65, 0, 32, 33),
    ("SEPTILIN-200ML SYP", "1X200ML", 40, 0, 8, 32),
]

# (product, out_qty, cl_qty) — this sheet has no opening or receipt columns.
HP_ROWS = [
    ("CYSTONE SYP 200ML", 25.0, 12.0),
    ("CYSTONE TAB", 76.0, 35.0),
    ("EVECARE SYP", 59.0, 29.0),
    ("GALACTOSURE GRAINS 200G", 6.0, 0.0),
    ("LIV 52 60ML DROP", 0.0, 18.0),
    ("LIV 52 DS SYP 200ML", 12.0, 87.0),
    ("LIV 52 DS TAB", 106.0, 4.0),
    ("LUKUL TAB", 27.0, 4.0),
    ("RUMALAYA LINIMENT", 0.0, 0.0),
    ("CONFIDO TAB", 1.0, 47.0),
    ("GASEX 200ML SYP", 6.0, 16.0),
    ("HIMCOCID (BANANA)", 38.0, 23.0),
    ("KOFLET 100ML SYP", 0.0, 0.0),
    ("KOFLET JAR", 28.0, 48.0),
    ("LIV 52 SYP 100ML", 0.0, 3.0),
    ("LIV 52 100ML SUGAR FRE", 148.0, 71.0),
    ("LIV 52 SYP 200ML", 41.0, 98.0),
    ("LIV 52 TAB", 0.0, 85.0),
    ("PILE FORT DINT", 18.0, 12.0),
    ("PILEX TAB", 16.0, 0.0),
    ("RENALKA 200ML", 0.0, 0.0),
]

DOCUMENTS = [
    {
        "document_id": "0000722289_2026_08_ZA_04_589_04092026043529.jpg",
        "source_file": r"C:\Users\amnsa\Downloads\image dataset stock\0000722289_2026_08_ZA_04_589_04092026043529.jpg",
        "layout": "digital_full_stock_statement",
        "quality": "clear_print",
        "headers": {
            "party": "NEW CHHAYA DISTRIBUTORS",
            "period": "01/08/2026 Upto 31/08/2026",
        },
        "absent_columns": ["sales_scheme_qty", "stock_out_qty"],
        "notes": [
            "Page 1 only. Page 2 is not in the image.",
            "Visual inspection, two agreeing reads of the original image.",
            "Not copied from Tesseract or PaddleOCR-VL output.",
            "Zero quantities are labeled but excluded from the primary quantity score.",
            "LIV-52 DROP 60ML and LIV-52 SYP 200ML were not labeled: both visual reads showed the same figures, so those rows are unresolved.",
        ],
        "rows": [
            {
                "row": index + 1,
                "product": name,
                "packing": pack,
                "opening_qty": opening,
                "receipt_qty": receipt,
                "sales_qty": issue,
                "closing_qty": closing,
            }
            for index, (name, pack, opening, receipt, issue, closing) in enumerate(CHHAYA_ROWS)
        ],
    },
    {
        "document_id": "0000719004_2026_08_ZA_04_550_02092026014949.jpg",
        "source_file": r"C:\Users\amnsa\Downloads\image dataset stock\0000719004_2026_08_ZA_04_550_02092026014949.jpg",
        "layout": "photo_summary_out_and_closing",
        "quality": "photo_of_printout",
        "headers": {
            "party": "M/S H.P. DRUG HOUSE",
            "period": "01/08/26 To 31/08/26",
        },
        "absent_columns": ["opening_qty", "receipt_qty", "sales_scheme_qty", "stock_out_qty", "packing"],
        "notes": [
            "This sheet prints OUT QTY and CL QTY only. Opening and receipt are not on the page.",
            "Visual inspection, two agreeing reads of the original image.",
            "Not copied from Tesseract or PaddleOCR-VL output.",
        ],
        "rows": [
            {
                "row": index + 1,
                "product": name,
                "out_qty": out_qty,
                "closing_qty": closing,
            }
            for index, (name, out_qty, closing) in enumerate(HP_ROWS)
        ],
    },
]
