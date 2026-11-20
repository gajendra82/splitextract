from pathlib import Path
import io
from PIL import Image, ImageOps
from dotenv import load_dotenv

load_dotenv()
from services import sales_statement_extractor as s

path = Path(r"C:\Users\adity\Downloads\1000343838.png")
data = path.read_bytes()
image = ImageOps.exif_transpose(Image.open(io.BytesIO(data))).convert("RGB")
bands = s._particular_packing_row_bands(image)
print("bands", len(bands), bands[:5], "...", bands[-3:] if bands else None)
print("size", image.size)

fake_header = """
SURESH MEDICAL HALL
JHANJHARPUR BAZAR, MADHUBANI, JHANJHARPUR
STOCK REPORT
01/10/26 To :28/10/26
For HIMALAYA DRUG - LIV SYP
"""
print("gate ZEAL-only", s._particular_packing_stock_report_text(fake_header))

# Force extract internals
if len(bands) >= 8:
    opening_jpeg, purchase_jpeg, sales_jpeg, closing_jpeg = s._particular_packing_ruled_halves(
        image, bands
    )
    Path("_tmp_pp_open.jpg").write_bytes(opening_jpeg)
    Path("_tmp_pp_pur.jpg").write_bytes(purchase_jpeg)
    Path("_tmp_pp_sl.jpg").write_bytes(sales_jpeg)
    Path("_tmp_pp_cl.jpg").write_bytes(closing_jpeg)
    row_count = len(bands)
    opening_rows = s._particular_packing_vision_rows(
        opening_jpeg,
        f"This image has exactly {row_count} rows. The last row is Company Total. "
        "Red lines divide four columns: product name, packing, opening quantity, opening value. "
        "Copy the opening numbers that are printed. The first opening quantity is greater than zero. "
        "The white left margin shows the row index. "
        f"Return a JSON array of exactly {row_count} objects with "
        "row, product_name, packing, opening_qty, opening_value.",
    )
    print("opening_rows", len(opening_rows))
    for r in opening_rows[:5]:
        print(" ", r)
    print(" ...")
    for r in opening_rows[-3:]:
        print(" ", r)
