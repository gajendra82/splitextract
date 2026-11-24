"""Debug product-line OCR location for residual docs."""
from services.ocr.secondary_repair import (
    _ocr,
    _ocr_product_digit_line,
    load_source_image,
    resolve_source_path,
)
from services.sales_statement_extractor import _a2z_tesseract

_a2z_tesseract()

CASES = [
    (
        "0000701078_2026_08_ZL_03_362_05092026080346.jpeg",
        [
            {"product_key": "AACTARIL SOAP", "packing": "75GM"},
            {"product_key": "TALEKT CAPS.", "packing": "60'S"},
            {"product_key": "TALEKT CAPS", "packing": "60'S"},
        ],
        ["82 0 0 82", "AACTARIL", "TALEKT", "12 0 0 12 0 0 12"],
    ),
    (
        "0000700275_2026_08_ZL_18_374_04092026171615.pdf",
        [
            {
                "product_key": "LIV-52 SYRUP",
                "packing": "100ML",
                "y_hint_token": "100ML",
            },
        ],
        ["72 35 37", "LIV-52", "LIV52"],
    ),
]

for doc, args_list, needles in CASES:
    path = resolve_source_path(doc)
    img = load_source_image(path)
    print("=" * 60, doc[:40], img.size)
    for args in args_list:
        line = _ocr_product_digit_line(img, **args)
        print(args["product_key"], args.get("packing"), "=>", repr((line or "")[:220]))
    raw = _ocr(img, psm=6)
    norm = raw.replace("O", "0")
    for needle in needles:
        idx = norm.upper().find(needle.upper())
        print("needle", needle, "idx", idx, "snippet", repr(norm[max(0, idx - 20) : idx + 60] if idx >= 0 else ""))
