import sys, os, time, json

sys.path.insert(0, r"c:\Users\amnsa\OneDrive\Desktop\zydus v1\splitextract")
os.environ["OCR_ENGINE"] = "paddle"

from services.sales_statement_extractor import extract_sales_statement

docs = [
    ("Doc 1", r"c:\Users\amnsa\OneDrive\Desktop\zydus v1\splitextract\test_images\0000701478_2026_08_ZA_01_224_08092026054726.jpg"),
    ("Doc 2", r"c:\Users\amnsa\OneDrive\Desktop\zydus v1\splitextract\test_images\0000733955_2026_08_ZA_04_286_05092026143428.jpeg"),
    ("Doc 3", r"c:\Users\amnsa\OneDrive\Desktop\zydus v1\splitextract\test_images\0000727190_2026_08_ZA_06_333_04092026081416.png")
]

for name, path in docs:
    t0 = time.time()
    with open(path, "rb") as f:
        data = f.read()
    res = extract_sales_statement(data, filename=os.path.basename(path))
    dur = time.time() - t0
    items = res.get("line_items") or []
    method = res.get("extraction_method")
    val = res.get("validation") or {}
    val_status = val.get("status")
    val_acc = val.get("accepted")
    gr = res.get("source_grounding") or {}
    print(f"=== {name} === ({dur:.2f}s)")
    print(f"Items: {len(items)}, Method: {method}, ValStatus: {val_status}, ValAccepted: {val_acc}, Grounding: {gr.get('status')}")
    if items:
        hiora = next((it for it in items if "HIORA" in (it.get("product_name") or "").upper()), None)
        if hiora:
            print("HIORA:", json.dumps(hiora, indent=2))
        else:
            print("First item:", json.dumps(items[0], indent=2))
    else:
        print(f"Reason: {res.get('error') or res.get('rejection_reason') or 'No items found / safely unresolved'}")