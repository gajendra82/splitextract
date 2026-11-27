from pathlib import Path
import services.sales_statement_extractor as sse

p = Path(r"C:\Users\adity\Downloads\131.jpg")
orig = sse._ocr_image_to_text
sse._ocr_image_to_text = (
    lambda *a, **k: "ARKAY MEDICOS STOCK STATEMENT PRODUCT DESCRIPTION "
    "PACKING OPSTK PURCH SALE SALE VAL IN/OT STOCK STK VAL SEP AUG"
)
try:
    res, used = sse._maybe_early_vision_for_image(
        p.read_bytes(), "131.jpg", ".jpg"
    )
    print("used", used)
    if res:
        items = res.get("line_items") or []
        ex = (res.get("totals") or {}).get("extra") or {}
        print("n", len(items))
        print("method", ex.get("extraction_method"), ex.get("early_vision_reason"))
        print(
            "sales hits",
            sum(
                1
                for it in items
                if float(it.get("sales_qty") or 0) > 0
                or float(it.get("sales_value") or 0) > 0
            ),
        )
    else:
        print("no result")
finally:
    sse._ocr_image_to_text = orig
