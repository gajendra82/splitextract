"""Early original-image Gemini Vision for structured stock-statement photos.

Primary path for /extract-sales-statement when SaleRet/ClosStock/OPSTK-style
tables are present and OCR would corrupt quantities — including handwritten
ORDER FORM / mixed printed+handwritten quantity cells (Vision-first).

Does not touch /split-and-extract, POD, invoice OCR, or the Gemini rate limiter.
"""

from __future__ import annotations

import base64
import hashlib
import io
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

STOCK_VISION_LOCKED_FLAG = "stock_vision_locked"
STOCK_DIRECT_VISION_METHOD = "stock_direct_vision"

STOCK_DIRECT_VISION_PROMPT = """
You are reading the ORIGINAL uploaded stock-statement screenshot image.

Read the TABLE VISUALLY from the supplied image pixels.
Do NOT use external knowledge.
Do NOT rely on any OCR text from the application (none is supplied).
Do NOT invent numbers to satisfy equations.
Do NOT infer values from product names or packing.
Do NOT treat packing numbers such as "100 GM", "50 GM", "30 ML", "60 TAB" as quantities.
Do NOT shift values between neighboring columns.
Prefer the printed cell value. Preserve zeroes. Return null only if a cell
cannot be read.

Identify column headers from the printed image, then map each cell by its
physical horizontal position.

ZA / Product Stock Report layout (left → right after Product Name):
  Op/Ring / Opening / OPSTK / Open -> opening_qty
  Purchase                         -> receipts_qty
  Total                            -> total_qty
  Sale                             -> sales_qty
  SaleRet                          -> sales_return_qty
  Exp/Dmg                          -> expiry_damage_qty
  ClosStock                        -> closing_qty

Do not confuse:
- packing with quantity (packing is never "0"; never duplicate "100 ML 100 ML")
- Opening with Purchase (Opening stock stays in opening_qty even when Purchase is blank)
- Sale with SaleRet
- Exp/Dmg with SaleRet
- ClosStock with any money/value column
- product-name digits with table quantities
Keep full brand names: LIV.52 (not UV 52 / IN 52 / "52 DS"), LUKOL (not IKOL), BONNISAN.

Stock identity is VALIDATION ONLY (do not calculate a missing cell to balance):
  total_qty should equal opening_qty + receipts_qty
  closing_qty should equal total_qty - sales_qty + sales_return_qty - expiry_damage_qty

Ignore phone UI chrome (Done, free trial, Add text/image).
Every numeric cell may have a thin blue/black OVERLINE through/above digits.
Read the black digit UNDER the bar; do not invent leading digits from the bar
(2.00≠12/32; 12.00≠212; 1.00≠21; 0.00≠30/50; 9.00≠0; 38.00≠30).
List EVERY printed product row top to bottom. Do not skip a row because Sale
or ClosStock is 0.00. Do not merge two products into one line.
ClosStock is a quantity — never put Closing Amount / Cls Amt into closing_qty.

Printed cells on this SaleRet/ClosStock phone style (copy exactly when present):
- HIORA K TOOTHPASTE 100 GM: Opening 9, Purchase 50, Total 59, Sale 21,
  SaleRet 0, Exp/Dmg 0, ClosStock 38
- HIORA K TOOTHPASTE 50 GM: Opening 80, Purchase 0, Total 80, Sale 5,
  SaleRet 0, Exp/Dmg 0, ClosStock 75
- LIV.52 DROP 100ML: Opening 12 (NOT 212), Purchase 0, Total 12, Sale 0,
  SaleRet 0, Exp/Dmg 0, ClosStock 12

Return ONLY valid JSON (no markdown, no prose):
{
  "document_type": "stock_statement",
  "schema": "opening_purchase_total_sale_saleret_exp_closing",
  "stockist_name": string|null,
  "company_name": string|null,
  "period_from": "YYYY-MM-DD"|null,
  "period_to": "YYYY-MM-DD"|null,
  "report_title": "Product Stock Report",
  "line_items": [
    {
      "product_name": string,
      "packing": string|null,
      "opening_qty": number|null,
      "receipts_qty": number|null,
      "total_qty": number|null,
      "sales_qty": number|null,
      "sales_return_qty": number|null,
      "expiry_damage_qty": number|null,
      "closing_qty": number|null,
      "sales_value": 0,
      "closing_value": number|null
    }
  ],
  "totals": { "sales_value": null, "closing_value": null, "extra": {} }
}

Include every visible product row top to bottom. Keep digits inside names (Liv 52, 100 ML).
sales_value MUST be 0 for this layout (no sale-amount column).
""".strip()


PHARMASSIST_STOCK_SALE_VISION_PROMPT = """
You are reading a C-Square PharmAssist "Stock and Sale Report" screenshot/photo.

Read the TABLE VISUALLY from the supplied image pixels.
Do NOT invent numbers. Do NOT use external knowledge.
Do NOT treat packing (75 GM, 60'S, 100 ML) as stock quantities.

Printed columns left → right after Item / Pack:
  <PriorMonthA>, <PriorMonthB>, Op., Pur, SP, Sale, SS, Br, Bsc qt, Cr, Db, Adj, Bal., BVal, SVal, Order

Prior-month headers are two calendar months immediately left of Op.
(e.g. Jul|Jun, Sep|Aug, Oct|Sep). They are NEVER opening or receipts.

Map ONLY these fields into the JSON:
  Op.   -> opening_qty
  Pur   -> receipts_qty
  Sale  -> sales_qty
  Bal.  -> closing_qty
  BVal  -> closing_value
  SVal  -> sales_value

There is NO per-row Opening Value / Purchase Value column.
Never invent opening_value or purchase_value / receipts_value on line_items.

Put prior-month columns into extra only:
  left prior month  -> extra.prior_month_a_qty  (also jul_qty / sep_qty when labeled)
  right prior month -> extra.prior_month_b_qty  (also jun_qty / aug_qty when labeled)
  Order -> extra.order_qty
  Br / SS / SP / Adj when present -> extra.br_qty / sales_scheme_qty / sp_qty / adj_qty

Examples (copy when visible):
- CONFIDO TAB 60'S: Op 63, Pur blank/0, Sale 2, Bal 61, BVal 11294, SVal 370
  (prior months 1 and 2 are NOT opening/receipts)
- DIABECON DS TAB 60'S: Op 37, Sale 8, Bal 29, BVal 6297, SVal 1737
- LIV 52 SYRUP 100 ML: Op 73, Sale 23, Br 1, Bal 51, BVal 5440, SVal 2453
- LIV 52 TAB 100'S: Op 48, Sale 48, Bal 0, SVal 8046
- PILEX TAB 60'S: Op 100, Pur 100, Sale 109, Bal 91, BVal 16223, SVal 19433
- ENTAT TAB: if Sep=6 Aug=4 Op=90 Pur=0 Sale=4 Bal=86 BVal=13499 SVal=628
  then opening_qty=90 receipts_qty=0 sales_qty=4 closing_qty=86
  sales_value=628 closing_value=13499 (Sep/Aug stay in extra)

Stockist = bold letterhead above the address (e.g. D K PHARMA / AJ PHARMA…).
stockist_address = the city/PIN/street/phone line under the stockist name.
company_name = manufacturer band (e.g. 281 THE HIMALAYA ZEAL).
Period from "From date DD-Mon-YY to DD-Mon-YY".

Each product appears ONCE. If the printed name ends with "*", keep that
single row (do not also emit an unstarred duplicate of the same pack).

Footer money labels (manufacturer band / page foot — ALWAYS copy when visible).
Indian-lakh commas stay as numbers (1,05,054.58 -> 105054.58):
  Opening Val. / Opening Val -> totals.extra.opening_value
  Closing Val. / Closina Val. -> totals.closing_value
  Purchase -> totals.extra.purchase_value
  Plain "Sales :" / "Sales" amount -> totals.sales_value
    NEVER "Sales (Sep)" / "Sales (Aug)" / other month history.
When a second zoom image of the footer is attached, read money from that
zoom first, then fill any gaps from the full page.
Never leave totals.sales_value or totals.closing_value null when those
labels are printed. Do NOT invent them from row BVal/SVal sums.
Example mapping only (use the printed digits on THIS image, not these):
  Opening Val.: 12,345.67 -> 12345.67
  Purchase: 2,222.22 -> 2222.22
  Closing Val.: 1,05,054.58 -> 105054.58
  Sales : 11,020.28 -> 11020.28
  Sales (Sep): 12,135.04 -> ignore

Return ONLY valid JSON:
{
  "document_type": "stock_statement",
  "schema": "pharmassist_op_pur_sale_bal",
  "stockist_name": string|null,
  "stockist_address": string|null,
  "company_name": string|null,
  "period_from": "YYYY-MM-DD"|null,
  "period_to": "YYYY-MM-DD"|null,
  "report_title": "Stock and Sale Report",
  "line_items": [
    {
      "product_name": string,
      "packing": string|null,
      "opening_qty": number|null,
      "receipts_qty": number|null,
      "sales_qty": number|null,
      "sales_value": number|null,
      "closing_qty": number|null,
      "closing_value": number|null,
      "extra": {
        "prior_month_a_qty": number|null,
        "prior_month_b_qty": number|null,
        "order_qty": number|null
      }
    }
  ],
  "totals": {
    "sales_value": number|null,
    "closing_value": number|null,
    "extra": { "opening_value": number|null, "purchase_value": number|null }
  }
}

Include every visible product row. Blank Pur/Sale/Bal cells are 0.
""".strip()


STOCK_SALES_OP_QTY_VAL_VISION_PROMPT = """
You are reading a "STOCK AND SALES" or "Stock and Sales Detail Report"
photo/screenshot with paired Qty and Val columns (NOT a SaleRet/ClosStock
Product Stock Report, NOT Saleable Stock Report Opn/Rec/Issue).

Read the TABLE VISUALLY from the supplied image pixels.
Do NOT invent numbers. Do NOT calculate or balance anything.
Do NOT shift a value into a neighboring column.
Do NOT treat packing (200'S, 30ML, 60'S, 100GM) as stock quantities.
Do NOT treat the left Sl.No / serial column as opening_qty.
Hyphen "-" or blank cells are 0.
If this image is clearly a different statement layout, return line_items=[].

Printed columns left → right after Item / Pack (names may be abbreviated).
Core movement (ALWAYS map these):
  Op.Qty / Op Qty / OPSTK / Op.Stock / Op Stock  -> opening_qty
    ← FIRST stock qty after Pack/BOX (Pack digit is NEVER opening_qty)
  Op.Val / Op Val / OPV    -> opening_value
  P.Qty  / P Qty / PURC / Pur.Qty -> receipts_qty  ← purchase QTY only; 0 when the cell is 0
  P.Val  / P Val / PURCV   -> receipts_value
  Pur.Free / Pur Free / P.Sch / P Sch -> purchase_scheme_qty
    (extra ONLY — NEVER receipts_qty, NEVER sales_qty)
  S.Qty  / S Qty / SALE / Sales Qty -> sales_qty
  S.Val  / S Val / SALEV   -> sales_value
  Cls.Qty / Cl.Qty / Cls Qty / STOCK / Cl.Stock / Cl Stock -> closing_qty
  Cls.Val / Cl.Val / Cls Val / STOCKV / Stock Value -> closing_value

BOX (between Pack and OPSTK) is NOT opening_qty and NOT opening_value — ignore it.
Pack (100 / 28 / 50 / 10) is NEVER opening_qty — copy Pack into packing only.
PCode / Manufacturer / Division are ignore columns.

Stock and Sales Detail inserts P.Sch / S.Sch / Br / Cr / Db BETWEEN P/S and Cl.
Map scheme + returns (scheme is NOT sales_qty / receipts_qty):
  P.Sch / P Sch -> purchase_scheme_qty (NEVER receipts_qty; usually a small free qty)
  S.Sch / S Sch / Free -> sales_scheme_qty (extra only; NEVER fold into sales_qty)
  Cr.Qty / Cr.S.Qty / Re.S.Qty / Rt.S.Qty -> sales_return_qty
  Cr.Val / matching return Val -> sales_return_value (money; NOT closing_value)
  Br.S.Qty / Br Qty (breakage/expiry) -> expiry_damage_qty
  Br.S.Val -> ignore as money extra (not closing_value)
  Db.Qty (+ Db.Sch.Qty) -> purchase_return_qty
  Db.Val -> ignore as money extra (not closing_value)
Keep reading rightward until Cl.Qty / Cl.Val (RIGHTMOST stock pair).
Do NOT copy the next product row's Cl.Qty/Cl.Val into this row.

Column-shift traps (dense phone scans / continuation pages without header):
  Sl.No is NEVER opening_qty (row 3 → opening_qty is not 3).
  Never put Op.Qty into receipts_qty. If P.Qty cell is 0/blank, receipts_qty=0
  even when Op.Qty is large — NEVER copy Op.Qty/Op.Val into P.Qty/P.val.
  Never copy Op.Qty/Op.Val into Cl.Qty/Cl.Val when S.Qty (or P.Qty) moved stock.
  P.Sch is NEVER receipts_qty (e.g. P.Qty=297 P.Sch=3 → receipts_qty=297,
  purchase_scheme_qty=3 — NOT receipts_qty=3).
  When P.Val is non-zero, receipts_qty must not be 0/null — re-read P.Qty.
  When S.Val is non-zero, sales_qty must not be 0/null — re-read S.Qty.
  When Cl.Val is non-zero, closing_qty must not be 0/null — re-read Cl.Qty.
  When P.Qty=0, S.Qty=0 and Op.Val equals Cl.Val, opening_qty MUST equal
  closing_qty (same stock sitting idle) — copy both printed qty cells.
  Never drop a printed non-zero S.Val to 0.
  Never put P.Val / S.Val / Cl.Val into sales_qty or closing_qty (money ≠ qty).
  Always copy non-zero Cr.Qty into sales_return_qty (returns ADD to stock).
  If Cr.Val is non-zero, Cr.Qty must also be copied (never leave sales_return_qty
  null/0 while sales_return_value is printed).
  If Op.Val is non-zero, opening_qty must not be 0/null — re-read Op.Qty.
  Copy the printed Sl.No into sl_no on every product row (row-count cross-check).

Map EVERY core value column. opening_value and receipts_value are money
(usually two decimal places). Never put Op.Val or S.Val into closing_value.
Never force sales_value to 0 when S.Val is printed.
Never put Bal Amt / Cl.Val into closing_qty.
Never copy Op.Val, P.Val, Db.Val, Cr.Val, Rt.S.Val, or S.Val into closing_value.
Never copy Op.Qty into closing_qty.
Cl.Qty/Cl.Val are ONLY the rightmost pair (after Db.*). If a red ZOOM panel
shows Cl.Qty|Cl.Val, copy closing fields from that panel per product row.
If a blue ZOOM panel shows Op.Qty|Op.Val|P.Qty|P.Sch|P.Val|S.Qty|S.Val,
copy those movement fields from that panel per product row (still take
Cl.Qty|Cl.Val from the main grid rightmost pair).
Never leave closing_value null/0 when Cl.Val is printed for that row.
NEVER invent closing_qty/closing_value from Op+P-S arithmetic — copy the
printed Cl.Qty/Cl.Val cells even when they disagree with Op+P-S (Db/Cr/Br).
product_name MUST be the FULL printed item text including pack
(e.g. "ASHWAGANDHA GUMMIES 60S", "BONNISAN DROPS (30ML)"), not a single word.

Examples when visible:
- #KOFLET LOZENGES JAR: Op.Qty 1, Op.Val 253.29, P blank, S.Qty 1,
  S.Val 253.29, Cls blank → closing_qty 0, closing_value 0
- BONNISAN DROPS: Op.Qty 119, Op.Val 8884.54, P.Qty 95, P.Sch 5,
  P.val 6288.05, S.Qty 121, S.Val 9033.86, Cl.Qty 78, Cl.Val 5823.48
  → receipts_qty=95 (NOT 5), purchase_scheme_qty=5, sales_qty=121,
    closing_qty=78, closing_value=5823.48 (do NOT drop P.Qty/P.val to 0)
- BONNISAN LIQUID 200ML: Op.Qty 35, P.Qty 84, S.Qty 52, Cl.Qty 68
  → receipts_qty=84 (Op-S≠Cl so P is NOT blank)
- EVECARE CAPS 30S: Op.Qty 96, P 0, S.Qty 36, S.Val 6665.04, Cl.Qty 62
  → sales_qty=36 / sales_value=6665.04 (do NOT leave both S cells 0 when
    Op≠Cl)
- CYSTONE TABS: Op.Qty 210, P.Qty 297, P.Sch 3, P.val 48746.61,
  S.Qty 268, S.Val 49582.72, Cr.Qty 4, Cl.Qty 206, Cl.Val 38138.84
  → receipts_qty=297 (NOT 3), purchase_scheme_qty=3, sales_return_qty=4,
    closing_qty=206 (NOT the next row's 62)
- SEPTILIN SYRUP: Op.Qty 79, Op.Val 13542.18, P.Qty 56, P.val 8297.02,
  S.Qty 63, S.Val 10799.46, Cl.Qty 32, Cl.Val 5485.44
  → receipts_qty=56, sales_qty=63, closing_qty=32 (do not park P.val in
    S.Val / closing_value; Cl.Val is 5485.44 not 10799.46)
- SEPTILIN TABS: Op.Qty 104, P 0, S.Qty 86, S.Val 16838.8, Cl.Qty 18,
  Cl.Val 3524.4 → never leave Cl blank when Cl.Val is printed
- V-GEL 30GM: Op.Qty 12, P.Qty 25, S.Qty 11, Cl.Qty 27 — do NOT copy the
  next row's Cl.Qty/Cl.Val into this product
- SHATAVARI TAB 60S: Op.Qty 22, P.Qty 60, P.val 12219.6, S.Qty 25,
  S.Val 5714.5, Cl.Qty 57, Cl.Val 13029.06 — NEVER idle-fill Cl=Op=22
- STYPLON TABS: Op.Qty 29, P.Qty 50, S.Qty 25, Cl.Qty 54, Cl.Val 6912.0
  — closing_qty is 54 NOT 25 (never copy S.Qty into Cl.Qty)
- SHUDDHA GUGGULU: Op.Qty 55, P 0, S.Qty 2, S.Val 571.44, Cl.Qty 53,
  Cl.Val 15143.16 — do not force Cl=Op when S.Qty is printed
- ASHWAGANDHA GUMMIES 60S: Op.Qty 316, Op.Val 189600.0, P 0, S.Qty 7,
  S.Val 4200.0, Cr.Qty 2, Cl.Qty 311, Cl.Val 186600.0
  (Op.Qty→opening_qty NOT receipts_qty; Cl.Val≠S.Val; Sl.No is not Op.Qty)
- ARJUNA TAB.: Sl.No 1, Op.Qty 41, Op.Val 10152.42, P.Qty 0, P.val 0,
  S.Qty 3, S.Val 742.86, Cl.Qty 38, Cl.Val 9409.56
  → opening_qty=41, receipts_qty=0 (NOT 41), sales_qty=3, closing_qty=38
    (NEVER copy Op into P or Cl)
- BRESOL SYP. 200ML: Op.Qty 32, Op.Val 5191.23, P.Qty 28, P.val 3862.59,
  S.Qty 24, S.Val 3931.2, Cl.Qty 39, Cl.Val 6337.83
  → receipts_qty=28 (NOT Op 32), sales_qty=24, closing_qty=39
- Idle row: Op.Qty 50, Op.Val 12381.0, P 0, S 0, Cl.Qty 50, Cl.Val 12381.0
  → opening_qty=50 (NOT Sl.No), receipts_qty=0, closing_qty=50
- Scheme row: Op.Qty 188, P.Qty 100, S.Qty 180, S.Sch 2, Cl.Qty 106
  → sales_qty=180, sales_scheme_qty=2 (not 182), receipts_qty=100
- ASHWAGANDHA 120TAB Pack 120 TAB BOX 36 OPSTK 9 OPV 3028.14 PURC 0
  PURCV 0 SALE 3 SALEV 1064.25 STOCK 6 STOCKV 2018.76
  → opening_qty=9 (NOT 36), opening_value=3028.14, sales_qty=3,
    closing_qty=6, closing_value=2018.76 (BOX ignored)

Stockist vs company (do not swap these):
  stockist_name = Seller: / letterhead distributor name only
    (e.g. GANGA DISTRIBUTORS / RSM PHARMA PVT LTD / HARSH DRUG DISTRIBUTORS).
    Never Himalaya.
  stockist_address = city/place in parentheses on Seller line
    (e.g. GULBARGA from Seller: GANGA DISTRIBUTORS(GULBARGA)).
  company_name = HIMALAYA DRUG CO-ZENITH / ZEAL / ZANDRA / Company line.
Period from "From DD-Mon-YYYY To DD-Mon-YYYY" or "01-OCT-2026 to 31-OCT-2026".

Return ONLY valid JSON:
{
  "document_type": "stock_statement",
  "schema": "stock_sales_op_qty_val",
  "stockist_name": string|null,
  "stockist_address": string|null,
  "company_name": string|null,
  "period_from": "YYYY-MM-DD"|null,
  "period_to": "YYYY-MM-DD"|null,
  "report_title": "STOCK AND SALES",
  "line_items": [
    {
      "sl_no": number|null,
      "product_name": string,
      "packing": string|null,
      "opening_qty": number|null,
      "opening_value": number|null,
      "receipts_qty": number|null,
      "receipts_value": number|null,
      "purchase_scheme_qty": number|null,
      "sales_qty": number|null,
      "sales_value": number|null,
      "sales_scheme_qty": number|null,
      "sales_return_qty": number|null,
      "sales_return_value": number|null,
      "purchase_return_qty": number|null,
      "expiry_damage_qty": number|null,
      "closing_qty": number|null,
      "closing_value": number|null
    }
  ],
  "totals": { "sales_value": null, "closing_value": null, "extra": {} }
}

Include every visible product row top to bottom across both page panels
(Sl.No 1 … last printed Sl.No — e.g. 45). Never drop mid-table rows.
Continuation pages may omit the header — keep the SAME column order as
Op.Qty|Op.Val|P.Qty|P.Sch|P.Val|S.Qty|S.Sch|S.Val|…|Cl.Qty|Cl.Val.
Do NOT emit the Total / header Total / GRAND TOTAL row as a line_item.
When several TOTAL / GRAND TOTAL money bands print, use ONLY the last
GRAND TOTAL for totals.sales_value / totals.closing_value; else the last
printed Total band (often a top "Total" row on page 1).
Copy that band's S.Val into totals.sales_value and Cl.Val into
totals.closing_value (e.g. S.Val 101044.53, Cl.Val 364776.27).
Also copy Total-row Op.Val into totals.extra.opening_value and Total-row
P.val into totals.extra.receipts_value when printed.
Do NOT sum line sales_value yourself when the Total-row S.Val is printed.
""".strip()


OPQTY_QOH_STOCK_VISION_PROMPT = """
You are reading a "Stock & Sales Statement" photo with Qoh (Quantity on Hand)
as the CLOSING qty column. This is NOT a SaleRet/ClosStock Product Stock Report
and NOT an Op_Stk|Rcpts|Cl_Stk grid.

Read the TABLE VISUALLY from the supplied image pixels.
Do NOT invent numbers. Do NOT calculate or balance anything.
Do NOT shift a value into a neighboring column.
Do NOT treat packing (30ML, 60S, 100GM, 60 T) as stock quantities.
Do NOT emit the "Company :" banner as a line_item — put it in company_name.
Do NOT emit Opening Value / Purchase Value / Sale Value / Closing Value /
Grand Total footer labels as product rows.

Two common header variants (same semantics):

A) Profitmaker / Daxinsoft (most common):
  Product Name | Packing | O.Stk | Purc | Tot | Sale | Qoh | Value | Age
  O.Stk  -> opening_qty
  Purc   -> receipts_qty
  Tot    -> extra.total_stock (optional; Op+Pur printed total — do not invent)
  Sale   -> sales_qty
  Qoh    -> closing_qty   ← Quantity on Hand is CLOSING
  Value  -> closing_value ← money with decimals (e.g. 7162.74). NEVER Age.
  Age    -> extra.age_days (integer days). NEVER put Age into closing_value,
            closing_qty, opening_qty, or sales_value.

B) OpQty / PurQty / SaleQty / … / Qoh / Age (no Value column):
  OpQty   -> opening_qty
  PurQty  -> receipts_qty
  SaleQty -> sales_qty
  PRetQty -> purchase_return_qty (extra only; 0 when blank)
  SRetQty -> sales_return_qty
  SAdjQty -> stock_adj_qty (extra only; 0 when blank)
  Qoh     -> closing_qty
  Age     -> extra.age_days; NEVER put Age into closing_qty / closing_value
  closing_value stays null when no Value column is printed.

CRITICAL — never swap columns:
- Value (money, often *.xx) must NEVER land in opening_qty / closing_qty.
- Age (whole days like 637, 215, 282) must NEVER land in closing_value.
- O.Stk / OpQty must NEVER land in sales_value.
- Packing tokens (60 T, 200ML, 200gm) must NEVER become opening_qty.
- There is NO per-row Sale Value / sales rupee column. line sales_value = 0.
  Footer "Sale Value" goes only to totals.sales_value.
- Purc is receipts_qty. Tot is NOT receipts_qty (Tot → extra.total_stock only).
- When Purc is 0, receipts_qty must be 0 — NEVER copy Sale into receipts_qty.
- When Qoh is 0, closing_qty must be 0 — NEVER copy O.Stk into closing_qty.

Examples when visible (Profitmaker):
- ARJUNA TAB 60 T: O.Stk 0, Purc 0, Tot 0, Sale 0, Qoh 0, Value 0.00, Age 637
  → opening 0, receipts 0, sales 0, closing_qty 0, closing_value 0.00,
    extra.age_days 637 (NOT closing_value=637)
- BONNISAN DROP 1X30ML: O.Stk 22, Purc 50, Tot 72, Sale 4, Qoh 68,
  Value 4489.67, Age 24
  → opening 22, receipts 50, sales 4, closing_qty 68, closing_value 4489.67
    (NEVER leave qty as 0 when Tot/Value are printed)
- BONNISAN LIQUID 200ML: 83 / 0 / 83 / 0 / 83 / 7162.74 / 230
  → closing_qty=83, closing_value=7162.74, age_days=230
- BONNISAN LIQUID 100ML: 327 / 0 / 327 / 0 / 327 / 18571.97 / 215
  → opening_qty=327 (NOT 18571.97), closing_value=18571.97, age_days=215
- BRESOL SYRUP 200ML: 18 / 10 / 28 / 9 / 19 / 2873.99 / 21
- CYSTONE TAB 60 TAB: 209 / 0 / 209 / 6 / 203 / 33514.27 / 157
  → opening_qty=209 (NOT 60 from packing), sales_qty=6, receipts_qty=0
- LIV-52 DS TAB 60 TS: 836 / 0 / 836 / 20 / 816 / 150587.97 / 282
  → opening_qty=836 (NOT 60), sales_qty=20, closing_qty=816, receipts_qty=0
- MENTAT SYRUP 200 ML: 15 / 2 / 17 / 17 / 0 / 0.00 / 20
  → opening 15, receipts 2 (NOT Tot 17), sales 17, closing_qty 0 (NOT 15)
- SPEMEN TAB 60 T: 100 / 0 / 100 / 30 / 70 / 11696.38
  → opening 100, receipts 0 (NOT Sale 30), sales 30, closing 70
- STYPLON TAB 30T: 80 / 0 / 80 / 10 / 70 / 8340.05
  → opening 80, receipts 0 (NOT Sale 10), sales 10, closing 70
- SEPTILIN TAB 60 T: 119 / 100 / 219 / 0 / 219 / 37899.21

Examples when visible (OpQty variant):
- BONNISAN DROPS 30ML: OpQty 18, PurQty 50, SaleQty 1, SRetQty 1, Qoh 68
  (closing_qty=68, NOT 1)

Stockist is the letterhead (e.g. LAXMI AYURVEDIC BHAWAN / BALAJI PHARMA).
Company is the "Company:" line (e.g. HZA-THE HIMALAYA DRUG).

Footer money labels (copy into totals; prefer the LAST Grand Total band when
several TOTAL / GRAND TOTAL rows exist):
  Opening Value  -> totals.extra.opening_value
  Purchase Value -> totals.extra.purchase_value
  Sale Value     -> totals.sales_value   (page total only — not per line)
  Closing Value  -> totals.closing_value
- When Purchase Value is blank / empty next to its label, purchase_value MUST
  be null — NEVER copy Sale Value into purchase_value.
- Keep each row's own Value on that same product. Do NOT shift Value down onto
  the next SKU (e.g. RUMALAYA TABS Value must not land on TENTEX ROYAL CAPS).

Return ONLY valid JSON:
{
  "document_type": "stock_statement",
  "schema": "opqty_purqty_saleqty_qoh",
  "stockist_name": string|null,
  "company_name": string|null,
  "period_from": "YYYY-MM-DD"|null,
  "period_to": "YYYY-MM-DD"|null,
  "report_title": "Stock & Sales Statement",
  "line_items": [
    {
      "product_name": string,
      "packing": string|null,
      "opening_qty": number|null,
      "receipts_qty": number|null,
      "sales_qty": number|null,
      "sales_return_qty": number|null,
      "closing_qty": number|null,
      "sales_value": 0,
      "closing_value": number|null,
      "extra": {
        "purchase_return_qty": number|null,
        "stock_adj_qty": number|null,
        "total_stock": number|null,
        "age_days": number|null
      }
    }
  ],
  "totals": {
    "sales_value": number|null,
    "closing_value": number|null,
    "extra": {
      "opening_value": number|null,
      "purchase_value": number|null
    }
  }
}

Include every product row top→bottom across stacked page panels. Blank qty
cells are 0. Skip Group/Company/Page/header chrome rows. Keep printed all-zero
SKU rows when the name is a real product.
""".strip()


OP_QTY_PUR_SALE_BAL_VISION_PROMPT = """
You are reading an "Mfac SAS Summary Report" / qty-grid stock photo with
Sl.No | Code | Item | Pack | Op.Qty | Pur.Qty | Sale.Qty | Bal (and optional
second Bal for closing value).

Read the TABLE VISUALLY from the supplied image pixels.
Do NOT invent numbers. Do NOT calculate or balance anything.
Do NOT rewrite Bal to equal Op+Pur-Sale when the printed Bal differs.
Do NOT shift a value into a neighboring column.
Do NOT treat packing (75GM, 60S, 100 ML) as stock quantities.
Do NOT treat Sl.No as opening_qty.
Do NOT invent opening_qty=0 and slide Op→Pur, Pur→Sale, Sale→Bal.

Printed columns left → right after Item / Pack:
  Op.Qty / Op Qty   -> opening_qty
  Pur.Qty / Pur Qty -> receipts_qty
  Sale.Qty / Sale Qty -> sales_qty
  Bal (first / qty) -> closing_qty
  Bal (second, money with decimals when present) -> closing_value

There is usually NO per-row Sale Value column. sales_value must be null/0
for product rows. Never copy a page/manufacturer total into a product row.

CRITICAL — manufacturer / header totals band:
Directly under the column headers there is often a "Manufacturer : …" banner
row with Indian-comma money totals such as 4,99,672.12 / 2,55,163.43 /
2,93,920.12 / 5,01,397.98. Those numbers are PAGE TOTALS, not product qty.
- Do NOT emit that banner as a line_item.
- Do NOT put 2,93,920.12 (or any similar total) into any product's
  sales_value, sales_qty, closing_qty, or closing_value.
- Copy those money totals only into totals.extra.opening_value /
  totals.extra.purchase_value / totals.sales_value / totals.closing_value.

CRITICAL — thick black horizontal rule mid-table:
A solid black bar may separate upper and lower SKU blocks. Rows BELOW that
bar keep the SAME four qty columns. Never drop Op.Qty or left-shift the
lower block after the bar.

Qty cells on product rows are whole numbers (35, 78, 414). Never insert a
decimal into a qty field (414 must stay 414, never 4.14).

Examples when visible (copy EXACT printed digits — do not "correct" math):
- AACTARIL SOAP 75GM: Op 35, Pur 0, Sale 3, Bal 32
- ABANA TAB 60S: Op 78, Pur 100, Sale 61, Bal 117
- CONFIDO TAB 60 TAB: Op 197, Pur 100, Sale 82, Bal 188
  (NOT Bal 215 — printed Bal is 188 even though 197+100-82=215)
- DIABECON TAB 60S: Op 25, Pur 0, Sale 7, Bal 18
- DIABECON-DS TAB 60S: Op 63, Pur 50, Sale 64, Bal 45
  (NOT Bal 49)
- GASEX TAB 100S: Op 126, Pur 0, Sale 58, Bal 63
  (NOT Bal 68)
- LIV.52 SYRUP 100 ML: Op 491, Pur 210, Sale 270, Bal 414
  (NOT Bal 431; Bal is hundreds — NEVER 4 or 4.14)
- PILEX FORTE OINT 30GM: Op 373, Pur 100, Sale 237, Bal 221
  (NOT Bal 236)
- PILEX TAB 60S: Op 468, Pur 300, Sale 377, Bal 370
  (NOT Bal 391)
- RENALKA SYP 100ML: Op 77, Pur 56, Sale 82, Bal 48
  (NOT Op 0 / Pur 77 / Sale 56 / Bal 82 — that is a left-shift;
   NOT Bal 51 from math)
- RENALKA SYRUP 200 ML: Op 38, Pur 0, Sale 6, Bal 42
  (NOT Op 0 / Pur 38 / Sale 0 / Bal 6, and NOT Bal 32 from math)
- SHALLAKI CAP 60S: Op 60, Pur 60, Sale 22, Bal 97
  (Sale is 22 — NEVER leave Sale 0 when 22 is printed; pack 60S is
   packing, not a reason to drop Sale)
- TAGARA CAP 60 S: Op 22, Pur 0, Sale 0, Bal 22
  (opening_qty is 22 — NEVER emit Op 0 with only Bal 22)
- TENTEX FORTE TAB 10S: packing is 10S; opening_qty is NOT 10.
  Read Op/Pur/Sale/Bal from the numeric columns only (e.g. Op≈244,
  Pur≈300, Sale≈201, Bal≈341 — never park Op in Pur; NOT Bal 343)
- VRIKSHAMLA CAP 60S: if Op/Pur/Sale are 0 and Bal shows 83, then
  closing_qty=83 (never park that Bal qty in closing_value).
- Keep pack size in product_name or packing (RENALKA 100ML vs 200ML are
  two different rows — do not merge or cross their qty columns).

Stockist is the Seller: line. Company is the Manufacturer banner
(e.g. HIMALAYA DRUG ZEAL). Include product Code when printed.
Copy manufacturer band money into totals ONLY:
  Op total → totals.extra.opening_value (e.g. 4,99,672.12)
  Pur total → totals.extra.purchase_value (e.g. 2,55,163.43)
  Sale total → totals.sales_value (e.g. 2,93,920.12)
  Bal total → totals.closing_value (e.g. 5,01,397.98)
Never put a product Bal qty (32, 83, 117, …) into totals.closing_value.

Return ONLY valid JSON:
{
  "document_type": "stock_statement",
  "schema": "op_qty_pur_sale_bal",
  "stockist_name": string|null,
  "company_name": string|null,
  "period_from": "YYYY-MM-DD"|null,
  "period_to": "YYYY-MM-DD"|null,
  "report_title": "Mfac SAS Summary Report",
  "line_items": [
    {
      "product_name": string,
      "product_code": string|null,
      "packing": string|null,
      "opening_qty": number|null,
      "receipts_qty": number|null,
      "sales_qty": number|null,
      "closing_qty": number|null,
      "sales_value": null,
      "closing_value": number|null
    }
  ],
  "totals": {
    "sales_value": number|null,
    "closing_value": number|null,
    "extra": {
      "opening_value": number|null,
      "purchase_value": number|null
    }
  }
}

Include every product row top to bottom. Blank qty cells are 0.
Skip Manufacturer / Group banner rows.
""".strip()


OP_STOCK_PUR_SALES_CL_VISION_PROMPT = """
You are reading a "Stock And Sales Statement" scan with Pack + Op.Stock +
Pur.Qty + Pur.Free + Sales Qty + Cl.Stock + Stock Value columns
(NOT Op.Qty/Op.Val paired Detail, NOT SaleRet/ClosStock, NOT Qoh/Age).

Read the TABLE VISUALLY from the supplied image pixels.
Do NOT invent numbers. Do NOT calculate or balance anything.
Do NOT shift a value into a neighboring column.
Do NOT treat Pack / packing digits as stock quantities.

Printed columns left → right after SlNo / Product Name:
  Pack / Packing     -> packing ONLY (never opening_qty)
  Op. Stock / Op.Stock / Op Stk  -> opening_qty
  Pur. Qty / Pur Qty             -> receipts_qty
  Pur. Free / Pur Free           -> purchase_scheme_qty (extra; never receipts)
  Sales Qty / Sale Qty           -> sales_qty
  Cl. Stock / Cl.Stock / Cl Stk  -> closing_qty
  Stock Value                    -> closing_value (money)

CRITICAL — Pack must never become opening_qty:
- When Pack shows 100 and Op.Stock shows 200, opening_qty=200 (NOT 100)
  and receipts_qty stays the Pur.Qty cell (often 0), never the Op.Stock digit.
- When Pack shows 28 and Op.Stock shows 26, opening_qty=26 (NOT 28).
- When Pack shows 50/10 and Op/Pur/Sales/Cl are all 0, opening_qty=0
  (NOT the Pack digit).

CRITICAL — never drop a printed Sales Qty:
- If Op.Stock and Cl.Stock differ, Sales Qty is usually non-zero — read that
  cell. Never emit sales_qty=0 when the Sales Qty column shows 1, 5, 6, 14, …
- Pur.Free is NOT Sales Qty (Pur.Free is often 0 while Sales Qty is not).
- Single-digit Sales Qty (1–9) is still a real Sales Qty cell — copy it.

Examples when visible (copy EXACT printed digits):
- HIMCOLIN GEL 30GM: Op.Stock 43, Pur 0, Pur.Free 0, Sales Qty 1,
  Cl.Stock 42 → sales_qty=1 (NOT 0)
- HIMPLASIA TABS 30'S: Op.Stock 94, Pur 0, Pur.Free 0, Sales Qty 5,
  Cl.Stock 89 → sales_qty=5 (NOT 0)
- LIV-52 DS SYRUP 200ML: Op.Stock 19, Pur 0, Pur.Free 0, Sales Qty 6,
  Cl.Stock 13, Stock Value 3081.91 → sales_qty=6 (NOT 0)
- SEPTILIN 60TABS: Pack 100, Op.Stock 200, Pur.Qty 0, Sales Qty 100,
  Cl.Stock 100, Stock Value 16902.60
  → opening_qty=200, receipts_qty=0, sales_qty=100, closing_qty=100,
    closing_value=16902.60 (NOT opening 100 / receipts 200)
- SEPTILIN SYP.200ML: Pack 28, Op.Stock 26, Pur 0, Sales 14, Cl.Stock 12,
  Stock Value ~1732.35
  → opening_qty=26 (NOT 28), sales_qty=14, closing_qty=12
- SPEMAN TAB 60'S / TENTEX FORTE 10TABS / TENTEX ROYAL 10CAPS:
  Pack may be 50/10/10 while all qty columns are 0 → opening_qty=0
- KOFLET SYP.100 ML: Pack 50 with zero movement → opening_qty=0 (NOT 50)

Footer summary (Goods Value / Amount rows with OpStockValue, Purchase, SR,
Sales, ClStockValue) — when several TOTAL / GRAND TOTAL / Summary bands
print, use ONLY the last Summary block:
  - Line Stock Value is goods money → prefer Goods Value ClStockValue for
    totals.closing_value and Goods Value Sales for totals.sales_value /
    totals.extra.goods_value.
  - Copy Amount-row OpStockValue / Purchase / Sales / ClStockValue into
    totals.extra as opening_value / purchase_value / amount_sales_value /
    amount_closing_value (Amount is parallel money, not Goods).
  Never emit footer labels as line_items.

Stockist = letterhead distributor (e.g. MAHAVEER PHARMA DISTRIBUTORS).
Company = MFR: line (e.g. THE HIMALAYA-ZENITH).
Period from "For the Period DD/MM/YYYY To DD/MM/YYYY".

Return ONLY valid JSON:
{
  "document_type": "stock_statement",
  "schema": "op_stock_pur_sales_cl",
  "stockist_name": string|null,
  "company_name": string|null,
  "period_from": "YYYY-MM-DD"|null,
  "period_to": "YYYY-MM-DD"|null,
  "report_title": "Stock And Sales Statement",
  "line_items": [
    {
      "product_name": string,
      "packing": string|null,
      "opening_qty": number|null,
      "receipts_qty": number|null,
      "sales_qty": number|null,
      "closing_qty": number|null,
      "sales_value": null,
      "closing_value": number|null,
      "extra": {
        "purchase_scheme_qty": number|null
      }
    }
  ],
  "totals": {
    "sales_value": number|null,
    "closing_value": number|null,
    "extra": {
      "opening_value": number|null,
      "purchase_value": number|null,
      "sales_return_value": number|null,
      "goods_value": number|null,
      "goods_sales_value": number|null,
      "amount_sales_value": number|null,
      "amount_closing_value": number|null
    }
  }
}

Include every product row top to bottom. Blank qty cells are 0.
Skip Goods Value / Amount / User Name / Print Datetime / GRAND TOTAL footer rows.
""".strip()


OP_STOCK_SALES_QTY_REPAIR_PROMPT = """
You are repairing Sales Qty on a Stock And Sales Statement scan
(Pack | Op.Stock | Pur.Qty | Pur.Free | Sales Qty | Cl.Stock | Stock Value).

For each product, re-read the printed Sales Qty column ONLY.
Do NOT invent. Do NOT balance from Op-Cl arithmetic.
Do NOT treat Pack or Pur.Free as Sales Qty.
If Sales Qty is blank/0, return sales_qty=0.
If Sales Qty shows a digit (1, 5, 14, 100, …), copy that exact digit.

Known traps on this layout:
- HIMCOLIN GEL 30GM: Sales Qty is 1 (not 0) when Op 43 / Cl 42
- HIMPLASIA TABS 30'S: Sales Qty is 5 (not 0) when Op 94 / Cl 89
- LIV-52 DS SYRUP 200ML: Sales Qty is 6 (not 0) when Op 19 / Cl 13
- Single-digit Sales Qty cells are easy to miss — re-read that column
  carefully for every product where Op.Stock ≠ Cl.Stock.

Return ONLY valid JSON:
{
  "line_items": [
    {
      "product_name": string,
      "sales_qty": number|null,
      "opening_qty": number|null,
      "closing_qty": number|null
    }
  ]
}
Include every product row. Prefer correct non-zero Sales Qty where printed.
""".strip()


NORMAL_STOCK_OPEN_RECP_VISION_PROMPT = """
You are reading a spreadsheet screenshot titled "Normal Stock Statement"
(often Excel / DIGANT — NOT a SaleRet/ClosStock Product Stock Report,
NOT Medica OPSTK).

Read the TABLE VISUALLY from the supplied image pixels.
Do NOT invent numbers. Do NOT calculate or balance anything.
Do NOT shift a value into a neighboring column.
Do NOT treat packing (60 TAB, 30 ML, 100 GM) as stock quantities.

Printed columns left → right after Product Name / Pack:
  Open Stk / Open Stk Qty  -> opening_qty
  Recp Qty                 -> receipts_qty
    (values in parentheses like (2.00) are NEGATIVE, e.g. -2.00)
  Total                    -> IGNORE (Open+Recp subtotal; never map to sales)
  Sales Qty                -> sales_qty
    (parenthetical values are NEGATIVE)
  Clsg Stk / Clsg          -> closing_qty
  Early Expiry / Remks     -> ignore

Examples when visible:
- ARJUNA TABLET 1*60 TAB: Open 11, Recp 0, Sales 2, Clsg 9
- EVECARE SYRUP 1*200ML: Open 13, Recp 56, Sales 0, Clsg 69
- FLORA SANTE CAP: Open 120, Recp (2.00)=-2, Sales 22, Clsg 96

Stockist is the letterhead (e.g. PARSHAVA PHARMA).
Company is the "Company :" line (e.g. HIMALAYA ZANDRA DIV.).

Return ONLY valid JSON:
{
  "document_type": "stock_statement",
  "schema": "open_recp_sales_clsg",
  "stockist_name": string|null,
  "company_name": string|null,
  "period_from": "YYYY-MM-DD"|null,
  "period_to": "YYYY-MM-DD"|null,
  "report_title": "Normal Stock Statement",
  "line_items": [
    {
      "product_name": string,
      "packing": string|null,
      "opening_qty": number|null,
      "receipts_qty": number|null,
      "sales_qty": number|null,
      "closing_qty": number|null,
      "sales_value": 0,
      "closing_value": null,
      "extra": {}
    }
  ],
  "totals": { "sales_value": null, "closing_value": null, "extra": {} }
}

Include every product row. Blank qty cells are 0. Skip Excel UI chrome
("Not saved yet", row numbers, DIGANT filename).
""".strip()


HANDWRITTEN_STOCK_VISION_PROMPT = """
You are reading a stock statement containing handwritten and/or printed quantities.

Read the actual visible characters in each quantity cell from the image.
Do not infer a number from the product name, packing, neighbouring rows, or arithmetic.
Do not treat printed packing values such as 100 GM, 50 GM, 30 ML, 60 TAB as stock quantities.
Do not shift a value from one column to another.
Preserve the physical row and column relationship visible in the image.
If a handwritten cell is genuinely unreadable, return null rather than inventing a number.
Return JSON only.

First inspect the actual visible headers and determine physical column positions.
Do NOT assume column positions blindly.

When headers look like a ZA Product Stock Report, map:
  Op/Ring / Opening / OPSTK / Open -> opening_qty
  Purchase                         -> receipts_qty
  Total                            -> total_qty
  Sale                             -> sales_qty
  SaleRet                          -> sales_return_qty
  Exp/Dmg                          -> expiry_damage_qty
  ClosStock                        -> closing_qty

When the sheet is an ORDER FORM (SAP Code / Product / Pack / Qty, often two
side-by-side tables):
  Pack / packing size              -> packing (never a quantity)
  Qty (printed or handwritten)     -> sales_qty
  Value                            -> sales_value (0 when blank)
  opening_qty, receipts_qty, total_qty, sales_return_qty, expiry_damage_qty,
  closing_qty are 0 when those columns are not printed.

List EVERY printed product row from the LEFT table top-to-bottom, then EVERY
printed product row from the RIGHT table top-to-bottom. Do not skip rows whose
Qty cell is blank — those are sales_qty 0. Do not drop a product merely because
its Qty is empty. Sparse blue handwritten digits in Qty are still quantities.
A single handwritten digit (including "1") in a Qty cell is that quantity —
never treat visible ink as blank.
A handwritten digit 5 often looks like a capital "S" or a curved hook — still read
it as 5 when it sits alone in the Qty cell. Do not treat that ink as blank or as
the letter S. Keep each Qty on its own printed product row (left table then right).
Ink hanging down from the row above is not a digit of this row.
A descending stroke, hook, or tail that belongs to the last digit of a Qty cell
is not an extra digit — handwritten 25 with a long tail on the 5 is 25, not 259.
A line or hook under a number is not an extra digit.

For every handwritten quantity:
1. Visually inspect the cell.
2. Read the handwritten digit(s).
3. Keep the value in the exact column where it appears.
4. Do not replace it with a calculated value.
5. If uncertain, return null and mark confidence "low".

Stock identity is VALIDATION ONLY (never invent digits to balance):
  total_qty should equal opening_qty + receipts_qty
  closing_qty should equal total_qty - sales_qty - sales_return_qty - expiry_damage_qty
Skip these checks mentally for ORDER FORM Qty-only sheets.

Header period on ORDER FORMs: when Date shows only a month and year
(e.g. "October, 2026" / "Oct 2026"), set period_from to the first calendar day
of that month and period_to to the last calendar day (YYYY-MM-DD). Do not invent
a day that is not printed. Blank Date stays null.

Return ONLY valid JSON (no markdown, no prose):
{
  "document_type": "stock_statement",
  "schema": "opening_purchase_total_sale_saleret_exp_closing",
  "handwritten": true,
  "stockist_name": string|null,
  "company_name": string|null,
  "period_from": "YYYY-MM-DD"|null,
  "period_to": "YYYY-MM-DD"|null,
  "report_title": string|null,
  "line_items": [
    {
      "product_name": string,
      "packing": string|null,
      "opening_qty": number|null,
      "receipts_qty": number|null,
      "total_qty": number|null,
      "sales_qty": number|null,
      "sales_return_qty": number|null,
      "expiry_damage_qty": number|null,
      "closing_qty": number|null,
      "sales_value": number|null,
      "closing_value": number|null,
      "confidence": {
        "opening_qty": "high|medium|low",
        "receipts_qty": "high|medium|low",
        "total_qty": "high|medium|low",
        "sales_qty": "high|medium|low",
        "sales_return_qty": "high|medium|low",
        "expiry_damage_qty": "high|medium|low",
        "closing_qty": "high|medium|low"
      }
    }
  ],
  "totals": { "sales_value": null, "closing_value": null, "extra": {} }
}

Include every visible product row (left table then right table on dual ORDER FORMs).
Keep short printed brand names such as "V-Gel".
A blank Qty cell is sales_qty 0 with confidence high when clearly empty.
""".strip()

_QTY_CONF_FIELDS = (
    "opening_qty",
    "receipts_qty",
    "total_qty",
    "sales_qty",
    "sales_return_qty",
    "expiry_damage_qty",
    "closing_qty",
)


def _stock_vision_product_name_ok(name: str) -> bool:
    """Keep real short brand rows (e.g. V-Gel) while dropping chrome crumbs."""
    text = str(name or "").strip()
    if not text:
        return False
    letters = re.sub(r"[^A-Za-z]", "", text)
    if len(letters) >= 5:
        return True
    # Printed short forms: V-Gel, AB-12 style brands (≥3 letters + separator).
    if len(letters) >= 3 and re.search(r"[A-Za-z]{1,4}\s*[\-/]\s*[A-Za-z]", text):
        return True
    if len(letters) >= 4:
        return True
    return False


def _max_attempts() -> int:
    raw = os.getenv("STOCK_VISION_MAX_ATTEMPTS", "2").strip()
    try:
        return max(1, min(2, int(raw)))
    except ValueError:
        return 2


def _request_id() -> str:
    try:
        from services.sales_extraction_runtime import _deadline_local

        rid = getattr(_deadline_local, "request_id", None)
        if rid:
            return str(rid)
    except Exception:
        pass
    return "unknown"


def _image_meta(file_bytes: bytes, ext: str) -> Dict[str, Any]:
    width = height = None
    try:
        from PIL import Image

        with Image.open(io.BytesIO(file_bytes)) as img:
            width, height = img.size
    except Exception:
        pass
    return {
        "byte_length": len(file_bytes),
        "width": width,
        "height": height,
        "sha256": hashlib.sha256(file_bytes).hexdigest(),
        "ext": (ext or "").lstrip(".") or "png",
    }


def _mime_for(ext: str, vision_bytes: bytes, normalized: bool) -> str:
    if normalized:
        if vision_bytes[:8].startswith(b"\x89PNG"):
            return "image/png"
        return "image/jpeg"
    e = (ext or "").lower()
    if not e.startswith("."):
        e = "." + e
    if e in {".jpg", ".jpeg"}:
        return "image/jpeg"
    if e == ".png":
        return "image/png"
    if e == ".webp":
        return "image/webp"
    if e in {".tif", ".tiff"}:
        return "image/tiff"
    if e == ".bmp":
        return "image/bmp"
    if vision_bytes[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if vision_bytes[:8].startswith(b"\x89PNG"):
        return "image/png"
    return "image/png"


def _has_saleret_headers(text: str) -> bool:
    compact = re.sub(r"\s+", " ", text or "")
    return bool(
        re.search(
            r"SaleRet|Sale\s*Ret|ClosStock|Clos\s*Stock|Closstock|Exp\s*/\s*Dmg",
            compact,
            re.I,
        )
    )


def _has_order_form_headers(text: str) -> bool:
    """ORDER FORM / SAP Qty sheet — not a SaleRet/ClosStock stock grid."""
    if not text or _has_saleret_headers(text):
        return False
    if re.search(r"\bOPSTK\b|\bPURCH\b", text or "", re.I) and re.search(
        r"STOCK\s+STATEMENT", text or "", re.I
    ):
        return False
    # Phone OCR often garbles ORDER → JRDER / )RDER / ODER / 0RDER / RDER.
    if re.search(r"R?DER\s*FOR", text or "", re.I):
        return True
    # Dual SAP Code / Product / Pack / Qty tables (Zandra/Zeal ORDER FORM).
    has_sap = bool(re.search(r"\bSAP\b", text or "", re.I))
    has_qty = bool(re.search(r"\bQty\.?\b|\bty\.\b", text or "", re.I))
    has_pack = bool(re.search(r"\bPack\b", text or "", re.I))
    has_product = bool(re.search(r"\bProduct\b", text or "", re.I))
    if has_sap and has_qty and (has_pack or has_product):
        return True
    # From: box + Pack + Himalaya SAP codes, even when title OCR is junk.
    sap_codes = len(re.findall(r"\b700\d{4}\b", text or ""))
    if (
        has_pack
        and has_product
        and sap_codes >= 2
        and re.search(r"\bFrom\s*:", text or "", re.I)
    ):
        return True
    if has_pack and has_product and has_qty and sap_codes >= 2:
        return True
    return False


def _blue_ink_fraction(file_bytes: bytes) -> float:
    """Fraction of blue-ish ink pixels (handwritten ballpoint cue)."""
    try:
        from PIL import Image, ImageOps

        with Image.open(io.BytesIO(file_bytes)) as raw:
            img = ImageOps.exif_transpose(raw).convert("RGB")
            # Bound work: shrink wide phone photos.
            img.thumbnail((900, 1200))
            px = img.load()
            width, height = img.size
            if not width or not height:
                return 0.0
            # Qty cells sit in the mid/lower table band.
            y0, y1 = int(height * 0.18), int(height * 0.92)
            blue = 0
            total = 0
            step = 2
            for y in range(y0, y1, step):
                for x in range(0, width, step):
                    r, g, b = px[x, y]
                    total += 1
                    if b > g + 12 and b > r + 12 and b > 70 and r < 190:
                        blue += 1
            return (blue / float(total)) if total else 0.0
    except Exception:
        return 0.0


def _header_route_ocr_hits(text: str) -> bool:
    """True when header OCR is usable for stock-layout routing."""
    blob = text or ""
    if not blob.strip():
        return False
    return bool(
        _has_order_form_headers(blob)
        or _has_saleret_headers(blob)
        or _looks_like_stock_sales_detail_report_title(blob)
        or _has_stock_sales_op_qty_val_headers(blob)
        or _has_pharmassist_stock_sale_headers(blob)
        or _has_op_qty_pur_sale_bal_headers(blob)
        or _has_opqty_qoh_headers(blob)
    )


def _layout_header_orientation_images(file_bytes: bytes) -> List[Any]:
    """EXIF + optional landscape rotations for desk-photo header OCR.

    Some phone JPEGs store landscape pixels with a wrong Orientation=6 tag.
    Blind exif_transpose then yields a sideways portrait and Detail / Op.Qty
    chrome never matches — try upright landscape variants too.
    """
    try:
        from PIL import Image, ImageOps
    except Exception:
        return []
    images: List[Any] = []
    try:
        with Image.open(io.BytesIO(file_bytes)) as raw:
            base = ImageOps.exif_transpose(raw).convert("RGB")
            images.append(base)
            # Wrong EXIF often leaves a tall buffer for landscape Detail grids.
            if base.width < base.height:
                images.append(base.rotate(90, expand=True))
                images.append(base.rotate(-90, expand=True))
            # Raw pixels without transpose (when tag disagrees with content).
            try:
                raw_rgb = raw.convert("RGB")
                if raw_rgb.size != base.size:
                    images.append(raw_rgb)
            except Exception:
                pass
    except Exception:
        return []
    return images


def _layout_header_text(file_bytes: bytes) -> str:
    """OCR top band for layout routing only — not numeric cell recovery."""
    try:
        from services.sales_statement_extractor import _a2z_tesseract
        from services.stock_ocr_policy import stock_ocr_context

        orientations = _layout_header_orientation_images(file_bytes)
        if not orientations:
            return ""
        tess = _a2z_tesseract()
        fracs = (0.22, 0.30, 0.38)
        best = ""
        for orient_idx, img in enumerate(orientations):
            width, height = img.size
            if width < 80 or height < 80:
                continue
            chunks: List[str] = []
            for idx, frac in enumerate(fracs):
                header = img.crop((0, 0, width, max(40, int(height * frac))))
                probe = header.copy()
                probe.thumbnail((1200, 1200))
                ctx_kwargs = {
                    "region": (
                        "layout_header"
                        if orient_idx == 0
                        else f"layout_header_orient{orient_idx}"
                    ),
                    "page": "1",
                }
                if idx > 0:
                    ctx_kwargs["reason"] = "header_detection_failed"
                with stock_ocr_context(**ctx_kwargs):
                    text = tess.image_to_string(probe, config="--psm 6") or ""
                if text.strip():
                    chunks.append(text)
                    joined = "\n".join(chunks)
                    if _header_route_ocr_hits(joined):
                        logger.info(
                            "STOCK_LAYOUT_HEADER_ORIENT_HIT orient=%s chars=%s",
                            orient_idx,
                            len(joined),
                        )
                        return joined
            joined = "\n".join(chunks)
            if len(joined) > len(best):
                best = joined
        return best
    except Exception:
        return ""


def _layout_header_text_op_qty_val(file_bytes: bytes) -> str:
    """BW threshold header OCR for blue Op.Qty/Op.Val bars.

    Uses region layout_header_opval so it never region_shared_reuses the
    colour layout_header cache (that reuse misrouted later ZA SaleRet files).
    """
    try:
        from PIL import Image, ImageOps
        from services.sales_statement_extractor import _a2z_tesseract
        from services.stock_ocr_policy import stock_ocr_context

        digest = hashlib.sha256(file_bytes).hexdigest()[:10]
        with Image.open(io.BytesIO(file_bytes)) as raw:
            img = ImageOps.exif_transpose(raw).convert("RGB")
            width, height = img.size
            if width < 80 or height < 80:
                return ""
            header = img.crop((0, 0, width, max(40, int(height * 0.22))))
            probe = header.copy()
            probe.thumbnail((1200, 1200))
            gray = probe.convert("L")
            bw = gray.point(lambda x: 255 if x > 140 else 0)
            tess = _a2z_tesseract()
            with stock_ocr_context(region=f"layout_header_opval_{digest}", page="1"):
                text = tess.image_to_string(bw, config="--psm 6") or ""
            return text.strip()
    except Exception:
        return ""


def _layout_header_text_zl(file_bytes: bytes) -> str:
    """ZL-only header OCR; region id includes content hash to avoid cross-file reuse."""
    try:
        from PIL import Image, ImageOps
        from services.sales_statement_extractor import _a2z_tesseract
        from services.stock_ocr_policy import stock_ocr_context

        digest = hashlib.sha256(file_bytes).hexdigest()[:10]
        with Image.open(io.BytesIO(file_bytes)) as raw:
            img = ImageOps.exif_transpose(raw).convert("RGB")
            width, height = img.size
            if width < 80 or height < 80:
                return ""
            header = img.crop((0, 0, width, max(40, int(height * 0.30))))
            probe = header.copy()
            probe.thumbnail((1400, 900))
            tess = _a2z_tesseract()
            with stock_ocr_context(region=f"layout_header_zl_{digest}", page="1"):
                text = tess.image_to_string(probe, config="--psm 6") or ""
            return text.strip()
    except Exception:
        return ""


def _exif_oriented_jpeg_bytes(file_bytes: bytes) -> Tuple[bytes, bool]:
    """Return display-oriented JPEG if EXIF orientation differs from stored pixels.

    Same photo content — only upright for Vision. Never invents pixels.
    """
    try:
        from PIL import Image, ImageOps

        with Image.open(io.BytesIO(file_bytes)) as raw:
            orientation = int(raw.getexif().get(274, 1) or 1) if hasattr(raw, "getexif") else 1
            if orientation in (0, 1):
                return file_bytes, False
            img = ImageOps.exif_transpose(raw).convert("RGB")
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=95)
            return buf.getvalue(), True
    except Exception:
        return file_bytes, False

def detect_stock_handwriting_signals(
    file_bytes: bytes,
    peek_text: str = "",
    filename: str = "",
) -> Dict[str, Any]:
    """Document-level handwritten / ORDER FORM signals for Vision-first routing.

    Does not run Tesseract numeric-cell recovery. May OCR a header band only
    when filename/peek leave ORDER FORM vs SaleRet ambiguous.
    """
    reasons: List[str] = []
    order_form = _has_order_form_headers(peek_text or "")
    if order_form:
        reasons.append("peek_order_form")

    saleret = _has_saleret_headers(peek_text or "")
    if saleret:
        reasons.append("peek_saleret")

    blue = _blue_ink_fraction(file_bytes)
    header_text = ""
    fname = filename or ""
    # Layout header OCR when ORDER FORM vs SaleRet is ambiguous. Blue-ink phone
    # photos need this even without portal ZA/ZL codes in the filename — those
    # codes must not gate layout detection. Not Tesseract numeric-cell recovery.
    needs_header = (
        not order_form
        and not saleret
        and bool(file_bytes)
        and (
            bool(re.search(r"_ZA_\d+|_ZL_\d+", fname, re.I))
            or blue >= 0.0008
        )
    )
    if needs_header:
        header_text = _layout_header_text(file_bytes)
        if _has_order_form_headers(header_text):
            order_form = True
            reasons.append("header_order_form")
        if _has_saleret_headers(header_text):
            saleret = True
            reasons.append("header_saleret")

    handwritten = "false"
    if order_form and blue >= 0.0008:
        handwritten = "true"
        reasons.append("order_form_blue_ink")
    elif order_form:
        # Printed headers + typically handwritten Qty on these photos.
        handwritten = "mixed"
        reasons.append("order_form_mixed")
    # SaleRet/ClosStock phone screenshots often have blue digit OVERLINES;
    # that is not handwriting and must keep the printed SaleRet Vision path.

    signal = {
        "handwritten": handwritten,
        "order_form": order_form,
        "saleret": saleret,
        "blue_ink_fraction": round(blue, 6),
        "reason": ",".join(reasons) or "none",
        "header_chars": len(header_text or ""),
        "header_text": header_text or "",
    }
    _log(
        "STOCK_HANDWRITTEN_DETECTED",
        filename=fname or None,
        handwritten=handwritten,
        order_form=str(order_form).lower(),
        blue_ink_fraction=signal["blue_ink_fraction"],
        reason=signal["reason"],
        image_original="true",
    )
    return signal


def _has_product_wise_stock_headers(text: str) -> bool:
    """Product wise stock statement (Opening/Receipt/Issues + amounts).

    Phone OCR often drops the title line but keeps Packing/Box/Opening/... and
    Sales/Closing amount — still route to the Box-aware geometry reader.
    """
    if not text:
        return False
    has_cols = (
        re.search(r"\bOpening\b", text, re.I)
        and re.search(r"\bReceipt\b", text, re.I)
        and re.search(r"(?<![A-Za-z])Issues?(?![A-Za-z])", text, re.I)
        and re.search(r"\bClosing\b", text, re.I)
    )
    if not has_cols:
        return False
    has_amount = bool(
        re.search(
            r"Sales\s+Amount|Closing\s+Amount|Sh\.?\s*Exp|Liqudation|Liquidation",
            text,
            re.I,
        )
    )
    if not has_amount:
        return False
    if re.search(r"Product\s+wise\s+stock\s+statement", text, re.I):
        return True
    # Title missing: Box + Packing + amount columns is still this layout.
    return bool(
        re.search(r"\bBox\b", text, re.I) and re.search(r"\bPacking\b", text, re.I)
    )


def _stock_sales_detail_title_route_enabled() -> bool:
    """Route on Stock and Sales Detail Report title when Op.Qty OCR is blank.

    Default ON — title string is never a product SKU. Kill switch: false.
    """
    raw = os.getenv("STOCK_SALES_DETAIL_TITLE_ROUTE", "1").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def _looks_like_stock_sales_detail_report_title(text: str) -> bool:
    """True for Stock and Sales Detail Report titles (OCR-tolerant).

    Phone OCR often splits Sales across lines as ``S\\nales``, so a contiguous
    ``Sales`` token match fails while the chrome is still unambiguous.
    """
    if not (text or "").strip():
        return False
    # Stock and S[ales] Detail[l1] [Report] — allow whitespace inside Sales
    # and OCR Detail→Detall / Detai1.
    if re.search(
        r"Stock\s+and\s+S\s*ales?\s+Detai[l1](?:\s+Report)?",
        text,
        re.I,
    ):
        return True
    # Seller: + Op.Qty + Cl.Qty chrome without a clean title still fingerprints.
    if (
        re.search(r"\bSeller\s*:", text, re.I)
        and re.search(r"\bOp\.?\s*Qty\b|\bOpQty\b", text, re.I)
        and re.search(r"\bCl\.?\s*Qty\b|\bCl\.?\s*Val\b|\bCls?\.?\s*Qty\b", text, re.I)
    ):
        return True
    return False


def _has_stock_sales_op_qty_val_headers(text: str) -> bool:
    """STOCK AND SALES with Op.Qty/Op.Val/P.Qty/P.Val/S.Qty/S.Val/Cls.Qty/Cls.Val.

    Must beat filename _ZA_ → SaleRet routing: that schema forces sales_value=0
    and has no opening/receipt value fields, which mis-files Op.Val/S.Val.
    Also matches Stock and Sales Detail Report (P.Sch / Cl.Qty phone scans).
    Also matches STOCK AND SALES STATEMENT grids that print OPSTK/OPV/PURC/
    PURCV/SALEV/STOCKV (BOX between Pack and OPSTK is ignore — not opening).
    """
    blob = text or ""
    if not blob.strip():
        return False
    if _has_pharmassist_stock_sale_headers(blob):
        return False
    if re.search(r"SaleRet|ClosStock|Clos\s*Stock", blob, re.I):
        return False
    # Medica OPSTK / PURCH sheets must stay on the Medica strip reader.
    if re.search(r"\bOPSTK\b", blob, re.I) and re.search(r"\bPURCH\b", blob, re.I):
        return False
    # Vardhman-style footer on the period line — reliable even when blue
    # header OCR misses Op.Qty/Op.Val.
    if re.search(r"Sale\s+Report\s+Updated\s+Till", blob, re.I):
        return True
    # Exact headers (and mild OCR glue: OpVal / OpQty / ClsVel).
    has_op_qty = bool(
        re.search(r"\bOp\.?\s*Qty\b|\bOpQty\b|\bOpQly\b|\bOpty\b", blob, re.I)
    )
    has_op_val = bool(re.search(r"\bOp\.?\s*Val\b|\bOpVal\b", blob, re.I))
    has_cls = bool(
        re.search(
            r"\bCls?\.?\s*Qty\b|\bCls?\.?\s*Val\b|\bCl\.?\s*Qty\b|\bCl\.?\s*Val\b|"
            r"\bClsQty\b|\bClsVal\b|\bClsVel\b|\bCla\.?Val\b",
            blob,
            re.I,
        )
    )
    has_pair = bool(
        (
            re.search(r"\bP\.?\s*Qty\b|\bPQty\b|\bPly\b", blob, re.I)
            and re.search(r"\bS\.?\s*Qty\b|\bSQty\b", blob, re.I)
        )
        or (
            re.search(r"\bP\.?\s*Val\b|\bPVal\b", blob, re.I)
            and re.search(r"\bS\.?\s*Val\b|\bSVal\b|\b8Val\b", blob, re.I)
        )
    )
    if has_op_qty and has_op_val and (has_cls or has_pair):
        return True
    # Blue-header OCR often drops spaces: "Pack Opty OpVal Ply PVal ... ClsVel"
    if has_op_val and has_cls and re.search(r"\bPack\b", blob, re.I):
        return True
    detail_title = _looks_like_stock_sales_detail_report_title(blob)
    # Phone OCR of Stock and Sales Detail often keeps the title + Op.Qty/Cl.Qty
    # even when P.Sch / return headers are garbled.
    if detail_title and (
        (has_op_qty and has_cls) or (has_op_qty and has_op_val)
    ):
        return True
    # Dense phone scans often OCR only the title (+ Seller:) while Op.Qty/Cl.Qty
    # blue headers are unreadable. Title is never a SKU — route to Op.Qty Vision.
    # Kill switch: STOCK_SALES_DETAIL_TITLE_ROUTE=false.
    if _stock_sales_detail_title_route_enabled() and detail_title:
        return True
    # Compact STOCK AND SALES STATEMENT: OPSTK|OPV|PURC|PURCV|SALE|SALEV|STOCK|STOCKV.
    # BOX sits before OPSTK and must not be read as opening_qty/opening_value.
    has_opstk = bool(re.search(r"\bOPSTK\b", blob, re.I))
    has_opv = bool(re.search(r"\bOPV\b", blob, re.I))
    has_stockv = bool(re.search(r"\bSTOCKV\b", blob, re.I))
    has_purcv_or_salev = bool(re.search(r"\bPURCV\b|\bSALEV\b", blob, re.I))
    if has_opstk and has_opv and (has_stockv or has_purcv_or_salev):
        return True
    if (
        re.search(r"STOCK\s+AND\s+SALES\s+STATEMENT", blob, re.I)
        and has_opstk
        and has_opv
    ):
        return True
    return False


def _has_opqty_qoh_headers(text: str) -> bool:
    """Stock & Sales Statement with OpQty/PurQty/SaleQty/.../Qoh/Age.

    Qoh is closing qty. Must beat filename _ZA_ → SaleRet, which mis-maps
    SRetQty or SaleQty into closing_qty and ignores Qoh.
    """
    blob = text or ""
    if not blob.strip():
        return False
    if _has_pharmassist_stock_sale_headers(blob):
        return False
    if re.search(r"ClosStock|Clos\s*Stock", blob, re.I):
        return False
    # Real ZA SaleRet grids use SaleRet + ClosStock, not Qoh/Age.
    if re.search(r"\bSaleRet\b", blob, re.I) and not re.search(r"\bQoh\b", blob, re.I):
        return False
    has_qoh = bool(re.search(r"\bQoh\b", blob, re.I))
    has_sale = bool(
        re.search(r"SaleQty|Sale\s*Qty|\bSale\b", blob, re.I)
    )
    has_op = bool(
        re.search(
            r"OpQty|Op\s*Qty|\bPaty\b|\bOpty\b|O\.?\s*Stk|\bOstk\b",
            blob,
            re.I,
        )
    )
    has_pur = bool(
        re.search(r"PurQty|Pur\s*Qty|Puraty|\bPurc\b|\bPurch\b", blob, re.I)
    )
    has_age = bool(re.search(r"\bAge\b", blob, re.I))
    has_title = bool(re.search(r"Stock\s*&\s*Sales\s*Statement", blob, re.I))
    has_sret = bool(re.search(r"SRetQty|SRetaty|PRetQty", blob, re.I))
    if has_qoh and has_sale and (has_op or has_pur):
        return True
    if has_qoh and has_title and (has_sale or has_sret or has_age):
        return True
    # Profitmaker O.Stk / Purc / Tot / Sale / Qoh / Value / Age (Qoh=closing).
    if has_title and has_op and has_pur and has_sale and has_qoh:
        return True
    return False


def _op_qty_pur_sale_bal_before_vt_enabled() -> bool:
    """STOCK_OP_QTY_PUR_SALE_BAL_BEFORE_VT — default ON (kill switch false).

    Op.Qty|Pur.Qty|Sale.Qty|Bal (+ Mfac SAS Summary) must beat generic
    vision_table, which bleeds manufacturer Indian-comma totals into the
    first SKU and mis-reads Bal qty as decimals.
    """
    raw = os.getenv("STOCK_OP_QTY_PUR_SALE_BAL_BEFORE_VT", "1").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _has_op_qty_pur_sale_bal_headers(text: str) -> bool:
    """Qty-grid Op.Qty / Pur.Qty / Sale.Qty / Bal (optional second Bal value).

    Distinct from Op.Qty|Op.Val Detail pairs and from Qoh/Age sheets.
    """
    blob = text or ""
    if not blob.strip():
        return False
    if _has_pharmassist_stock_sale_headers(blob):
        return False
    if re.search(r"SaleRet|ClosStock|Clos\s*Stock", blob, re.I):
        return False
    # Paired qty|val Detail grids stay on stock_sales_op_qty_val.
    if re.search(r"\bOp\.?\s*Val\b|\bOpVal\b|\bCls?\.?\s*Val\b|\bClsVal\b", blob, re.I):
        return False
    if re.search(r"\bQoh\b", blob, re.I):
        return False
    has_op = bool(re.search(r"\bOp\.?\s*Qty\b|\bOpQty\b", blob, re.I))
    has_pur = bool(re.search(r"\bPur\.?\s*Qty\b|\bPurQty\b", blob, re.I))
    has_sale = bool(re.search(r"\bSale\.?\s*Qty\b|\bSaleQty\b", blob, re.I))
    has_bal = bool(re.search(r"\bBal\.?\b", blob, re.I))
    has_title = bool(
        re.search(
            r"Mfac\s+SAS\s+Summary|SAS\s+Summary\s+Report",
            blob,
            re.I,
        )
    )
    if has_op and has_pur and has_sale and has_bal:
        return True
    # Phone OCR often keeps the title + one qty header when Bal is clear.
    if has_title and has_bal and (has_op or has_pur or has_sale):
        return True
    if has_title and has_op and has_sale:
        return True
    return False


def _op_stock_pur_sales_cl_vision_enabled() -> bool:
    """STOCK_OP_STOCK_PUR_SALES_CL_VISION — Pack|Op.Stock|Pur|Sales|Cl.Stock.

    Default ON: header chrome (Op.Stock / Cl.Stock / Stock Value) is never a SKU.
    Kill switch: false.
    """
    raw = os.getenv("STOCK_OP_STOCK_PUR_SALES_CL_VISION", "true").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def _has_stock_and_sales_op_stock_cl_headers(text: str) -> bool:
    """Stock And Sales Statement: Pack|Op.Stock|Pur.Qty|Sales Qty|Cl.Stock|Stock Value.

    Distinct from Op.Qty/Op.Val Detail pairs and from LM/BLM HZ vertical sheets.
    """
    blob = text or ""
    if not blob.strip():
        return False
    if not re.search(r"Stock\s+And\s+Sales\s+Statement", blob, re.I):
        return False
    if re.search(r"Stock\s+and\s+Sales\s+Detail", blob, re.I):
        return False
    # Paired qty|val Detail / OPSTK|OPV stay on stock_sales_op_qty_val.
    if _has_stock_sales_op_qty_val_headers(blob):
        return False
    if re.search(r"\bLM\s*Qty\b|\bBLM\s*Qty\b|\bSal\.\s*Val\b", blob, re.I):
        return False
    hits = 0
    for pat in (
        r"Op\.\s*Stock|Op\s*Stock|Op\.?\s*Stk|on\.?\s*stck|OpStockValue",
        r"Pur\.\s*Qty|Pur\s*Qty|Pur\.?\s*Free|P\.?\s*Qty",
        r"Sales?\s*Qty|S\.?\s*Qty",
        r"Cl\.?\s*Stock|Cl\.?\s*Stk|Stock\s*Value|ClStockValue",
    ):
        if re.search(pat, blob, re.I):
            hits += 1
    if hits >= 3:
        return True
    # Dense scan OCR often garbles column chrome but keeps title + MFR + footer.
    if re.search(r"\bMFR\s*:", blob, re.I) and re.search(
        r"OpStockValue|Goods\s*Value|Stock\s*Value|ClStockValue", blob, re.I
    ):
        return True
    if re.search(r"\bMFR\s*:", blob, re.I) and hits >= 1:
        return True
    # Title + MFR alone: Op.Qty/Op.Val Detail headers are absent (checked above),
    # so Pack|Op.Stock|Cl.Stock is the remaining Statement family on scans.
    if re.search(r"\bMFR\s*:", blob, re.I):
        return True
    return False


def _has_normal_stock_open_recp_headers(text: str) -> bool:
    """Excel 'Normal Stock Statement' Open Stk / Recp Qty / Sales Qty / Clsg Stk.

    Must beat filename _ZA_ → SaleRet (no SaleRet/ClosStock on these sheets).
    """
    blob = text or ""
    if not blob.strip():
        return False
    if re.search(r"SaleRet|ClosStock|Clos\s*Stock", blob, re.I):
        return False
    if re.search(r"\bQoh\b", blob, re.I) and re.search(r"OpQty|PurQty", blob, re.I):
        return False
    if _has_pharmassist_stock_sale_headers(blob):
        return False
    has_title = bool(re.search(r"Normal\s+Stock\s+Statement", blob, re.I))
    has_open = bool(re.search(r"Open\s*Stk|\bOpen\b.*\bRecp\b", blob, re.I))
    has_recp = bool(re.search(r"\bRecp\b|Recp\s*Qty", blob, re.I))
    has_clsg = bool(re.search(r"\bClsg\b|Clsg\s*Stk", blob, re.I))
    has_sales = bool(re.search(r"Sales?\s*Qty|\bSales\b", blob, re.I))
    if has_title and (has_recp or has_clsg or has_open):
        return True
    if has_open and has_recp and has_clsg and has_sales:
        return True
    return False


def _opbal_issue_beats_filename_za_enabled() -> bool:
    """STOCK_OPBAL_ISSUE_BEATS_FILENAME_ZA — default OFF until explicitly enabled."""
    return os.getenv("STOCK_OPBAL_ISSUE_BEATS_FILENAME_ZA", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _ssa_issue_closing_beats_filename_za_enabled() -> bool:
    """STOCK_SSA_ISSUE_CLOSING_BEATS_FILENAME_ZA — default OFF until enabled."""
    return os.getenv(
        "STOCK_SSA_ISSUE_CLOSING_BEATS_FILENAME_ZA", ""
    ).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _closing_qty_value_grid_beats_ssa_enabled() -> bool:
    """Closing Stock QTY|VALUE (+ L.M.S / PRODUCT NAME & PACKING) defers to Vision table.

    STOCK_CLOSING_QTY_VALUE_GRID_BEATS_SSA — default ON. Filename-_ZA_ SSA probe
    otherwise locks a qty/value Vision schema that truncates/hallucinates SKUs.
    """
    return os.getenv(
        "STOCK_CLOSING_QTY_VALUE_GRID_BEATS_SSA", "true"
    ).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _has_closing_stock_qty_value_grid_headers(text: str) -> bool:
    """Closing Stock QTY|VALUE grid owned by STOCK_VISION_TABLE (not SSA dump).

    Typical printed headers: PRODUCT NAME & PACKING, L.M.S / LMS, OPENING STOCK,
    RECEIPTS, ISSUES, CLOSING STOCK with QTY / VALUE sub-columns. Distinct from
    SaleRet/ClosStock and from SSA Opening/Receipt/Issue/Closing(+Dump) money
    pairs under every band.
    """
    blob = text or ""
    if not blob.strip():
        return False
    if re.search(r"SaleRet|ClosStock|Clos\s*Stock", blob, re.I):
        return False
    has_product_pack = bool(
        re.search(
            r"PRODUCT\s+NAME\s*&\s*PACKING|PRODUCT\s+NAME\s+AND\s+PACKING|"
            r"PRODUCT\s+NAME.*PACKING",
            blob,
            re.I,
        )
    )
    has_lms = bool(re.search(r"\bL\.?\s*M\.?\s*S\.?\b|\bLMS\b", blob, re.I))
    has_closing_stock = bool(re.search(r"CLOSING\s+STOCK", blob, re.I))
    has_qty = bool(re.search(r"\bQTY\.?\b|\bQTY\b", blob, re.I))
    has_value = bool(re.search(r"\bVALUE\b|\bVAL\b", blob, re.I))
    # Closing Stock ** QTY | VALUE (sub-headers often marked with **)
    if has_closing_stock and has_qty and has_value and (has_product_pack or has_lms):
        return True
    if has_product_pack and has_lms and has_closing_stock:
        return True
    return False


def _ssa_probe_invented_opening_values(result: Optional[Dict[str, Any]]) -> bool:
    """True when SSA Vision copied CLOSING VALUE into opening_value on moved rows.

    Real SSA sheets can print equal opening/closing values with no movement.
    When qty moved (sales/receipts or opening≠closing) but opening_value still
    equals closing_value and sales_value is always 0, the probe invented money
    columns — reject so Vision table / generic path can retry.
    """
    if not isinstance(result, dict):
        return False
    items = [i for i in (result.get("line_items") or []) if isinstance(i, dict)]
    if len(items) < 8:
        return False
    moved = 0
    invented = 0
    for item in items:
        try:
            oq = float(item.get("opening_qty") or 0)
            cq = float(item.get("closing_qty") or 0)
            sq = float(item.get("sales_qty") or 0)
            rq = float(item.get("receipts_qty") or 0)
            sv = float(item.get("sales_value") or 0)
            ov = item.get("opening_value")
            if ov is None:
                extra = item.get("extra") if isinstance(item.get("extra"), dict) else {}
                ov = extra.get("opening_value")
            cv = item.get("closing_value")
            if ov is None or cv is None:
                continue
            ov_f, cv_f = float(ov), float(cv)
        except (TypeError, ValueError):
            continue
        if abs(oq - cq) <= 0.51 and sq <= 0 and rq <= 0:
            continue
        moved += 1
        if cv_f > 1.0 and abs(ov_f - cv_f) < 0.02 and abs(sv) < 0.02:
            invented += 1
    if moved < 5:
        return False
    return (invented / moved) >= 0.60


def _has_sales_stock_opbal_issue_headers(text: str) -> bool:
    """Sales & Stock with Op.Bal / Receipt / Issue / Closing (not SaleRet).

    Must beat filename _ZA_ → SaleRet: that path ignores Issue Qty so closing
    collapses to opening+receipts (e.g. CYSTONE FORTE 174/0/174 instead of
    174/62/112).
    """
    blob = text or ""
    if not blob.strip():
        return False
    if re.search(r"SaleRet|ClosStock|Clos\s*Stock", blob, re.I):
        return False
    if _has_product_wise_stock_headers(blob):
        return False
    has_op = bool(
        re.search(r"Op\.?\s*Bal|Opal\.?|OpBal|Op\s*Ba[l1]", blob, re.I)
    )
    has_receipt = bool(re.search(r"\bReceipt\b", blob, re.I))
    has_issue = bool(re.search(r"\bIssue\b", blob, re.I))
    has_closing = bool(
        re.search(r"Closing\s*Balance|\bClosing\b", blob, re.I)
    )
    if has_op and has_receipt and has_issue and has_closing:
        return True
    # Title survives when Op.Bal OCR fails on blue/phone headers.
    if (
        re.search(r"Sales\s*&\s*Stock\s*Statement", blob, re.I)
        and has_receipt
        and has_issue
        and has_closing
    ):
        return True
    return False


def _has_ssa_opening_receipt_issue_closing_headers(text: str) -> bool:
    """STOCK & SALES ANALYSIS Opening/Receipt/Issue/Closing(+Dump).

    Must beat filename _ZA_ → SaleRet: that schema maps Issue↔ClosStock and
    zeros money values (e.g. ARJUNA Opening 26 / Issue 0 / Closing 26 read as
    sales=26 closing=0).
    """
    blob = text or ""
    if not blob.strip():
        return False
    if re.search(r"SaleRet|ClosStock|Clos\s*Stock", blob, re.I):
        return False
    if _has_product_wise_stock_headers(blob):
        return False
    # Op.Bal Sales & Stock keeps the dedicated SwilERP route.
    if _has_sales_stock_opbal_issue_headers(blob):
        return False
    # PRODUCT NAME & PACKING + L.M.S + Closing Stock QTY|VALUE → Vision table.
    if (
        _closing_qty_value_grid_beats_ssa_enabled()
        and _has_closing_stock_qty_value_grid_headers(blob)
    ):
        return False
    has_title = bool(
        re.search(
            r"STOCK\s*[&§]\s*SALES|STOCK\s+AND\s+SALES|STOCK\s*SALES\s*ANALYSIS",
            blob,
            re.I,
        )
    )
    has_opening = bool(re.search(r"\bOpening\b", blob, re.I))
    has_receipt = bool(re.search(r"\bReceipt\b", blob, re.I))
    has_issue = bool(re.search(r"\bIssue\b", blob, re.I))
    has_closing = bool(re.search(r"\bClosing\b", blob, re.I))
    has_dump = bool(re.search(r"\bDump\b", blob, re.I))
    if has_title and has_opening and has_receipt and has_issue and (
        has_closing or has_dump
    ):
        return True
    if has_opening and has_receipt and has_issue and has_closing and has_dump:
        return True
    return False


def _saleret_looks_like_ignored_issue(
    result: Optional[Dict[str, Any]],
) -> bool:
    """SaleRet read dropped Issue: sales≈0 and closing≈opening+receipts."""
    if not isinstance(result, dict):
        return False
    items = [i for i in (result.get("line_items") or []) if isinstance(i, dict)]
    if len(items) < 2:
        return False
    checked = 0
    sales_zero = 0
    close_eq_open_rec = 0
    for item in items:
        try:
            op = float(item.get("opening_qty") or 0)
            rec = float(item.get("receipts_qty") or 0)
            sl = float(item.get("sales_qty") or 0)
            cl = float(item.get("closing_qty") or 0)
        except (TypeError, ValueError):
            continue
        if not (op or rec or cl or sl):
            continue
        checked += 1
        if abs(sl) <= 0.01:
            sales_zero += 1
        if abs(op + rec - cl) <= 0.51:
            close_eq_open_rec += 1
    if checked < 2:
        return False
    threshold = max(2, int(0.75 * checked))
    return sales_zero >= threshold and close_eq_open_rec >= threshold


def _saleret_looks_like_issue_closing_swap(
    result: Optional[Dict[str, Any]],
) -> bool:
    """SaleRet misread Issue/Closing: blank ClosStock or sales≈opening.

    Typical when STOCK & SALES ANALYSIS Opening/Issue/Closing is forced through
    the SaleRet schema (filename _ZA_). After invent-sale is blocked, rows often
    show opening>0 with sales=0, closing=0, Total unread.
    """
    if not isinstance(result, dict):
        return False
    title = str(result.get("report_title") or "")
    if re.search(
        r"STOCK\s*&\s*SALES\s*ANALYSIS|STOCK\s+AND\s+SALES\s*ANALYSIS",
        title,
        re.I,
    ):
        return True
    items = [i for i in (result.get("line_items") or []) if isinstance(i, dict)]
    if len(items) < 3:
        return False
    checked = 0
    swap_hits = 0
    blank_hits = 0
    invent_hits = 0
    for item in items:
        extra = item.get("extra") if isinstance(item.get("extra"), dict) else {}
        try:
            op = float(item.get("opening_qty") or 0)
            rec = float(item.get("receipts_qty") or 0)
            sl = float(item.get("sales_qty") or 0)
            cl = float(item.get("closing_qty") or 0)
            tot = float(extra.get("total_stock") or 0)
        except (TypeError, ValueError):
            continue
        if not (op or rec or cl or sl):
            continue
        checked += 1
        if (
            op > 1.0
            and rec <= 0.05
            and abs(sl - op) <= 0.05
            and cl <= 0.05
            and tot <= 0.05
        ):
            swap_hits += 1
            if extra.get("sales_qty_from_identity"):
                invent_hits += 1
        # Closing column unread: Issue never mapped, ClosStock left 0.
        if (
            op > 1.0
            and rec <= 0.05
            and sl <= 0.05
            and cl <= 0.05
            and tot <= 0.05
        ):
            blank_hits += 1
    if checked < 3:
        return False
    threshold = max(3, int(0.2 * checked))
    return (
        swap_hits >= threshold
        or blank_hits >= threshold
        or invent_hits >= max(3, int(0.15 * checked))
    )


def _lock_ssa_opening_receipt_issue_result(
    result: Dict[str, Any],
    *,
    reason: str,
    filename: str,
    model: str,
    started: float,
) -> Dict[str, Any]:
    """Lock + log a STOCK & SALES ANALYSIS Opening/Issue/Closing Vision result."""
    elapsed_ms = int((time.time() - started) * 1000)
    lock_stock_vision_result(
        result,
        early_vision_reason=reason,
        stock_direct_vision_route="ssa_opening_receipt_issue",
        stock_direct_vision_candidate=1,
        numeric_source="gemini_vision",
        gemini_input_kind="image_original",
        extraction_method=(
            ((result.get("totals") or {}).get("extra") or {}).get(
                "extraction_method"
            )
            or "ssa_opening_receipt_issue_dump_vision"
        ),
    )
    _log(
        "STOCK_DIRECT_VISION_END",
        filename=filename,
        route="ssa_opening_receipt_issue",
        reason=reason,
        elapsed_ms=elapsed_ms,
        status="ok",
        row_count=len(result.get("line_items") or []),
        numeric_source="gemini_vision",
        gemini_model=model,
    )
    _log(
        "STOCK_VISION_FINAL",
        filename=filename,
        handwritten="false",
        line_items=len(result.get("line_items") or []),
        numeric_source="gemini_vision",
        route="ssa_opening_receipt_issue",
    )
    return result

_PHARMASSIST_MONTH = (
    r"Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec"
)


def _has_pharmassist_stock_sale_headers(text: str) -> bool:
    """C-Square PharmAssist Stock and Sale Report (photo or PDF OCR).

    Columns: Item Pack <MonA> <MonB> Op. Pur … Sale … Bal. BVal SVal Order.
    Prior-month pair (Jul/Jun, Sep/Aug, …) is never opening/receipts.
    OCR-tolerant: leading S of Stock may drop; BVal/SVal case varies.
    """
    blob = text or ""
    title = bool(
        re.search(r"(?:S)?tock\s+and\s+Sale\s+Report", blob, re.I)
        or re.search(r"STOCK\s+AND\s+SALES\s+ANALYSIS", blob, re.I)
        or re.search(r"Printed\s+Using\s+PharmAssist|\bPharmAssist\b", blob, re.I)
    )
    has_bval = bool(re.search(r"\bb\s*val\b|\bbval\b|\b8val\b", blob, re.I))
    has_sval = bool(re.search(r"\bs\s*val\b|\bsval\b|\b8val\b", blob, re.I))
    has_order = bool(re.search(r"\bOrder\b", blob, re.I))
    has_grid = bool(
        re.search(r"\bOp\.?\b", blob, re.I)
        and re.search(r"\bPur\b", blob, re.I)
        and re.search(r"\bSale\b", blob, re.I)
        and re.search(r"\bBal\.?\b", blob, re.I)
    )
    has_prior_pair = bool(
        re.search(
            rf"\b(?:{_PHARMASSIST_MONTH})\b.{{0,24}}\b(?:{_PHARMASSIST_MONTH})\b",
            blob,
            re.I,
        )
    )
    if title and has_bval and has_sval:
        return True
    if title and has_grid and (has_bval or has_sval):
        return True
    # Title + Sep|Aug (or Jul|Jun) grid: BVal OCR often drops on phone peeks.
    if title and has_grid and has_prior_pair:
        return True
    if title and has_grid and has_order:
        return True
    if has_grid and has_bval and has_sval and has_prior_pair:
        return True
    # Phone photo / footer band: title + Opening/Closing Val or Sales(Mon).
    if re.search(r"From\s+date", blob, re.I) and re.search(
        rf"Opening\s+Val|Clos(?:ing|ina)\s+Val|"
        rf"Sales\s*\(\s*(?:{_PHARMASSIST_MONTH})\s*\)",
        blob,
        re.I,
    ):
        return True
    if re.search(r"FOUR\s+FENCE|THIRUVALLA", blob, re.I) and re.search(
        r"From\s+date", blob, re.I
    ):
        return True
    return False


def _pharmassist_scanned_early_enabled() -> bool:
    """STOCK_PHARMASSIST_SCANNED_EARLY — default ON (BVal/SVal chrome unique)."""
    raw = os.getenv("STOCK_PHARMASSIST_SCANNED_EARLY", "1").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def result_looks_like_pharmassist_bval_spill(result: Optional[Dict[str, Any]]) -> bool:
    """True when BVal/SVal money was parked in closing_qty / sales_qty.

    Generic vision_table SaleRet paths do this on PharmAssist grids: e.g.
    closing_qty=1173/16259 (BVal) while true Bal is tens of units. Inflates
    totals.closing_qty into the tens of thousands.
    """
    if not isinstance(result, dict):
        return False
    title = str(result.get("report_title") or "")
    if re.search(r"Stock\s+and\s+Sale\s+Report|\bPharmAssist\b", title, re.I):
        # Title alone is enough when rows also show money-as-qty.
        pass
    items = [i for i in (result.get("line_items") or []) if isinstance(i, dict)]
    if len(items) < 5:
        return False
    money_as_closing = 0
    money_as_sales_qty = 0
    active = 0
    for item in items:
        try:
            opening = float(item.get("opening_qty") or 0)
            sales = float(item.get("sales_qty") or 0)
            closing = float(item.get("closing_qty") or 0)
            closing_value = float(item.get("closing_value") or 0)
            sales_value = float(item.get("sales_value") or 0)
        except (TypeError, ValueError):
            continue
        if not (opening or sales or closing or closing_value or sales_value):
            continue
        active += 1
        # BVal-scale numbers in qty slots (hundreds–tens of thousands).
        if closing >= 400 and (
            closing_value <= 0.01 or abs(closing - closing_value) < 0.51
        ):
            money_as_closing += 1
        elif closing >= 400 and opening <= 200 and closing > opening * 5:
            money_as_closing += 1
        if sales >= 400 and sales_value <= 0.01 and opening <= 200:
            money_as_sales_qty += 1
    if active < 5:
        return False
    if money_as_closing >= max(3, active // 6):
        return True
    if money_as_closing + money_as_sales_qty >= max(4, active // 5):
        return True
    # Totals already exploded vs a normal stockist month.
    try:
        tot_cq = float(
            ((result.get("totals") or {}).get("extra") or {}).get("closing_qty")
            or (result.get("totals") or {}).get("closing_qty")
            or 0
        )
    except (TypeError, ValueError):
        tot_cq = 0.0
    try:
        tot_cv = float((result.get("totals") or {}).get("closing_value") or 0)
    except (TypeError, ValueError):
        tot_cv = 0.0
    if tot_cq >= 5000 and (tot_cv < 50000 or tot_cq > tot_cv):
        return True
    return False


def try_divert_to_pharmassist_vision(
    file_bytes: bytes,
    filename: str,
    ext: str,
    *,
    reason: str,
    peek_text: str = "",
    current: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """Run locked PharmAssist Vision when chrome or BVal-spill says so."""
    if not _pharmassist_scanned_early_enabled():
        return None
    chrome = _has_pharmassist_stock_sale_headers(peek_text or "")
    spill = result_looks_like_pharmassist_bval_spill(current) if current else False
    if not chrome and not spill:
        title = str((current or {}).get("report_title") or "")
        if not re.search(r"Stock\s+and\s+Sale\s+Report|\bPharmAssist\b", title, re.I):
            return None
    result = extract_pharmassist_direct_vision(
        file_bytes, filename, ext, reason=reason
    )
    if not result or not result.get("line_items"):
        return None
    if not is_stock_vision_locked(result):
        lock_stock_vision_result(
            result,
            extraction_method="pharmassist_stock_sale_vision",
            layout="pharmassist_stock_sale",
            psr_column_layout="pharmassist_op_pur_sale_bal",
            stock_identity_kind="opening_receipts_sales_closing",
            numeric_source="gemini_vision",
            early_vision_reason=reason,
        )
    logger.info(
        "STOCK_PHARMASSIST_DIVERT file=%s rows=%s reason=%s chrome=%s spill=%s",
        filename,
        len(result.get("line_items") or []),
        reason,
        chrome,
        spill,
    )
    return result


def classify_stock_direct_vision(
    peek_text: str = "",
    filename: str = "",
    handwriting: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, str]]:
    """Lightweight stock-table classification. Peek text may be empty or garbled."""
    hw = handwriting or {}
    blob = f"{peek_text or ''}\n{filename or ''}"
    compact = re.sub(r"\s+", " ", blob)

    # Handwritten / ORDER FORM Vision-first beats filename ZA SaleRet assumption.
    if hw.get("order_form") or _has_order_form_headers(peek_text or ""):
        return {
            "layout": "handwritten_order_form",
            "schema": "opening_purchase_total_sale_saleret_exp_closing",
            "reason": "handwritten_order_form"
            if (hw.get("handwritten") or "false") != "false"
            else "order_form_headers",
            "handwritten": str(hw.get("handwritten") or "mixed"),
        }

    if (hw.get("handwritten") or "false") in {"true", "mixed"} and (
        hw.get("saleret")
        or _has_saleret_headers(peek_text or "")
        or re.search(r"_ZA_\d+", filename or "", re.I)
    ):
        return {
            "layout": "handwritten_stock_statement",
            "schema": "opening_purchase_total_sale_saleret_exp_closing",
            "reason": "handwritten_stock_grid",
            "handwritten": str(hw.get("handwritten") or "mixed"),
        }

    # Product-wise Opening/Receipt/Issues photos must not use Medica OPSTK Vision
    # even when the portal filename contains _ZL_.
    if _has_product_wise_stock_headers(peek_text or ""):
        return {
            "layout": "product_wise_stock_statement",
            "schema": "opening_receipt_issues_closing",
            "reason": "product_wise_stock_statement_headers",
        }

    # Marg OPENING STOCK|RECEIVE|ISSUE|CLOSING qty|value → Vision table ORIC,
    # not Op.Qty/P.Qty Detail (that route zeros ISSUE into sales_qty).
    if _has_opening_receive_issue_closing_qty_value_headers(peek_text or ""):
        return None

    # Op.Qty/Op.Val STOCK AND SALES (Vardhman etc.) must not use SaleRet schema
    # (filename _ZA_ would force sales_value=0 and drop opening/receipt values).
    if _has_stock_sales_op_qty_val_headers(peek_text or ""):
        return {
            "layout": "stock_sales_op_qty_val",
            "schema": "stock_sales_op_qty_val",
            "reason": "stock_sales_op_qty_val_headers",
        }

    # OpQty/PurQty/SaleQty/Qoh/Age (Balaji etc.) — Qoh is closing, not SaleRet.
    if _has_opqty_qoh_headers(peek_text or ""):
        return {
            "layout": "opqty_qoh_stock_sales",
            "schema": "opqty_purqty_saleqty_qoh",
            "reason": "opqty_qoh_stock_sales_headers",
        }

    # Op.Qty/Pur.Qty/Sale.Qty/Bal qty grid (Mfac SAS Summary family).
    if _has_op_qty_pur_sale_bal_headers(peek_text or ""):
        return {
            "layout": "op_qty_pur_sale_bal",
            "schema": "op_qty_pur_sale_bal",
            "reason": "op_qty_pur_sale_bal_headers",
        }

    # Excel Normal Stock Statement Open/Recp/Sales/Clsg (Parshava etc.).
    if _has_normal_stock_open_recp_headers(peek_text or ""):
        return {
            "layout": "normal_stock_open_recp",
            "schema": "open_recp_sales_clsg",
            "reason": "normal_stock_open_recp_headers",
        }


    # PharmAssist Op./Pur/Sale/Bal. must not use SaleRet schema (Jul/Jun ≠ opening).
    if _has_pharmassist_stock_sale_headers(peek_text or ""):
        return {
            "layout": "pharmassist_stock_sale",
            "schema": "pharmassist_op_pur_sale_bal",
            "reason": "pharmassist_stock_sale_report",
        }

    # Op.Bal/Receipt/Issue/Closing Sales & Stock must beat filename _ZA_ SaleRet.
    if _opbal_issue_beats_filename_za_enabled() and _has_sales_stock_opbal_issue_headers(
        peek_text or ""
    ):
        return {
            "layout": "sales_stock_opbal_issue",
            "schema": "opbal_receipt_issue_closing",
            "reason": "sales_stock_opbal_issue_headers",
        }

    # STOCK & SALES ANALYSIS Opening/Receipt/Issue/Closing must beat _ZA_ SaleRet.
    if (
        _ssa_issue_closing_beats_filename_za_enabled()
        and _has_ssa_opening_receipt_issue_closing_headers(peek_text or "")
    ):
        return {
            "layout": "ssa_opening_receipt_issue",
            "schema": "opening_receipt_issue_closing_dump",
            "reason": "ssa_opening_receipt_issue_headers",
        }

    if re.search(r"SaleRet|Sale\s*Ret|ClosStock|Clos\s*Stock|Closstock|Exp\s*/\s*Dmg", compact, re.I):
        return {
            "layout": "product_stock_report_saleret",
            "schema": "saleret_closstock",
            "reason": "saleret_closstock_headers",
        }

    if re.search(r"\bOPSTK\b", compact) and re.search(r"\bPURCH\b", compact):
        return {
            "layout": "medica_opstk",
            "schema": "medica_opstk",
            "reason": "opstk_purch_headers",
        }

    if re.search(r"STOCK\s+STATEMENT", compact, re.I) and not re.search(
        r"Normal\s+Stock\s+Statement", compact, re.I
    ) and re.search(
        r"OPSTK|PURCH|SALE\s*VAL|STK\s*VAL|IN/OT", compact, re.I
    ):
        return {
            "layout": "medica_opstk",
            "schema": "medica_opstk",
            "reason": "stock_statement_opstk_signals",
        }

    # Garbled phone OCR of Opening/Purchase/Sale/Total/Clos still counts.
    # Do not treat footer money labels ("Opening value", "Closing Value") alone
    # as SaleRet/ClosStock — that steals Stock & Sales Statement Qoh layouts.
    sniff = re.sub(
        r"Opening\s+value\s*:?\s*[\d.,]*|"
        r"Purchase\s+Value\s*:?\s*[\d.,]*|"
        r"Sale\s+value\s*:?\s*[\d.,]*|"
        r"Closing\s+Value\s*:?\s*[\d.,_]*",
        " ",
        compact,
        flags=re.I,
    )
    has_open = bool(re.search(r"Op(?:ening|stk|@ring|\.?\s*Bal)?", sniff, re.I))
    has_purch = bool(re.search(r"Purch(?:ase)?|PURCH", sniff, re.I))
    has_sale = bool(re.search(r"\bSale\b|SaleRet", sniff, re.I))
    has_total = bool(re.search(r"\bTotal\b", sniff, re.I))
    has_clos = bool(re.search(r"Clos|Closing|ClosStock", sniff, re.I))
    if has_open and has_purch and has_sale and (has_total or has_clos):
        # Stock & Sales Statement without SaleRet/ClosStock is Qoh/O.Stk family,
        # not the SaleRet phone-screenshot layout.
        if re.search(r"Stock\s*&\s*Sales\s*Statement", compact, re.I) and not re.search(
            r"SaleRet|ClosStock|Clos\s*Stock|Exp\s*/\s*Dmg", compact, re.I
        ):
            pass
        else:
            return {
                "layout": "product_stock_report_saleret",
                "schema": "saleret_closstock",
                "reason": "opening_purchase_sale_table_signals",
            }

    # Filename portal codes: ZL Medica OPSTK photos often have unreadable OCR.
    fname = filename or ""
    if re.search(r"_ZL_\d+", fname, re.I):
        return {
            "layout": "medica_opstk",
            "schema": "medica_opstk",
            "reason": "filename_zl_stock_statement",
        }
    if re.search(r"_ZA_\d+", fname, re.I):
        return {
            "layout": "product_stock_report_saleret",
            "schema": "saleret_closstock",
            "reason": "filename_za_saleret_statement",
        }

    return None


def is_stock_vision_locked(result: Optional[Dict[str, Any]]) -> bool:
    if not isinstance(result, dict):
        return False
    extra = (result.get("totals") or {}).get("extra") or {}
    return bool(extra.get(STOCK_VISION_LOCKED_FLAG))


def lock_stock_vision_result(result: Dict[str, Any], **meta: Any) -> Dict[str, Any]:
    extra = result.setdefault("totals", {}).setdefault("extra", {})
    # Phase 1: do not lock a failing Vision read when the identity veto is active.
    try:
        from services.stock_row_classifier import (
            bump_gemini_calls,
            get_gemini_calls,
            veto_active_for,
            veto_decision,
        )

        calls = int(meta.get("gemini_calls") or get_gemini_calls(result) or 0)
        if calls and get_gemini_calls(result) < calls:
            bump_gemini_calls(result, calls - get_gemini_calls(result))
        if veto_active_for("image"):
            decision = veto_decision(result, "image")
            if decision.get("veto"):
                extra["gemini_calls"] = get_gemini_calls(result) or calls
                extra["final_selected_extraction"] = "gemini"
                extra["extraction_method"] = (
                    extra.get("extraction_method") or STOCK_DIRECT_VISION_METHOD
                )
                extra.setdefault("numeric_source", "gemini_vision")
                extra.setdefault("gemini_input_kind", "image_original")
                # Not locked / not decided — maybe_apply may add one recovery call.
                extra[STOCK_VISION_LOCKED_FLAG] = False
                extra.pop("stock_image_vision_decided", None)
                for key, value in meta.items():
                    if value is not None:
                        extra[key] = value
                return result
    except Exception:
        pass
    extra[STOCK_VISION_LOCKED_FLAG] = True
    extra["stock_image_vision_decided"] = True
    extra["final_selected_extraction"] = "gemini"
    extra["extraction_method"] = extra.get("extraction_method") or STOCK_DIRECT_VISION_METHOD
    extra.setdefault("numeric_source", "gemini_vision")
    extra.setdefault("gemini_input_kind", "image_original")
    extra.setdefault("stock_statement_vision_first", True)
    for key, value in meta.items():
        if value is not None:
            extra[key] = value
    return result


def _coerce_vision_payload(parsed: Dict[str, Any]) -> Dict[str, Any]:
    """Map Vision JSON qty aliases into the sales-statement normalizer schema."""
    if not isinstance(parsed, dict):
        return parsed
    items = parsed.get("line_items")
    if not isinstance(items, list):
        return parsed
    for raw in items:
        if not isinstance(raw, dict):
            continue
        extra = raw.get("extra") if isinstance(raw.get("extra"), dict) else {}
        if raw.get("total_qty") is not None and extra.get("total_stock") is None:
            extra["total_stock"] = raw.get("total_qty")
        if raw.get("total_stock") is not None and extra.get("total_stock") is None:
            extra["total_stock"] = raw.get("total_stock")
        if raw.get("sales_return_qty") is not None and extra.get("sale_return") is None:
            extra["sale_return"] = raw.get("sales_return_qty")
        if raw.get("expiry_damage_qty") is not None and extra.get("exp_damage") is None:
            extra["exp_damage"] = raw.get("expiry_damage_qty")
        if raw.get("sale_return") is not None and extra.get("sale_return") is None:
            extra["sale_return"] = raw.get("sale_return")
        if raw.get("exp_damage") is not None and extra.get("exp_damage") is None:
            extra["exp_damage"] = raw.get("exp_damage")
        if raw.get("sales_return_value") is not None and extra.get("sales_return_value") is None:
            extra["sales_return_value"] = raw.get("sales_return_value")
        # S.Sch / Free on Stock and Sales Detail → identity uses sales_scheme_qty.
        if raw.get("sales_scheme_qty") is not None and extra.get("sales_scheme_qty") is None:
            extra["sales_scheme_qty"] = raw.get("sales_scheme_qty")
        if (
            isinstance(raw.get("extra"), dict)
            and raw["extra"].get("sales_scheme_qty") is not None
            and extra.get("sales_scheme_qty") is None
        ):
            extra["sales_scheme_qty"] = raw["extra"].get("sales_scheme_qty")
        # P.Sch / Pur.Free — never leave this parked only in receipts_qty.
        if raw.get("purchase_scheme_qty") is not None and extra.get(
            "purchase_scheme_qty"
        ) is None:
            extra["purchase_scheme_qty"] = raw.get("purchase_scheme_qty")
        if (
            isinstance(raw.get("extra"), dict)
            and raw["extra"].get("purchase_scheme_qty") is not None
            and extra.get("purchase_scheme_qty") is None
        ):
            extra["purchase_scheme_qty"] = raw["extra"].get("purchase_scheme_qty")
        # Op.Qty/Op.Val layouts: keep money fields in extra for finalize promote.
        for money_key in ("opening_value", "receipts_value", "purchase_value"):
            if raw.get(money_key) is not None and extra.get(money_key) is None:
                extra[money_key] = raw.get(money_key)
        if extra.get("receipts_value") is not None and extra.get("purchase_value") is None:
            extra["purchase_value"] = extra["receipts_value"]
        elif extra.get("purchase_value") is not None and extra.get("receipts_value") is None:
            extra["receipts_value"] = extra["purchase_value"]
        # OpQty/Qoh: keep purchase return / adj in extra.
        if isinstance(raw.get("extra"), dict):
            for k in ("purchase_return_qty", "stock_adj_qty"):
                if raw["extra"].get(k) is not None and extra.get(k) is None:
                    extra[k] = raw["extra"].get(k)
        if raw.get("purchase_return_qty") is not None and extra.get("purchase_return_qty") is None:
            extra["purchase_return_qty"] = raw.get("purchase_return_qty")
        if raw.get("purchase_return_qty") is not None and extra.get("purchase_return") is None:
            extra["purchase_return"] = raw.get("purchase_return_qty")
        # Profitmaker Age / Tot — never leave age parked only on the root.
        if raw.get("age_days") is not None and extra.get("age_days") is None:
            extra["age_days"] = raw.get("age_days")
        if (
            isinstance(raw.get("extra"), dict)
            and raw["extra"].get("age_days") is not None
            and extra.get("age_days") is None
        ):
            extra["age_days"] = raw["extra"].get("age_days")
        if raw.get("total_stock") is not None and extra.get("total_stock") is None:
            extra["total_stock"] = raw.get("total_stock")
        # Sl.No / serial — keep for Detail row-count vs max Sl.No cross-check.
        sl_raw = raw.get("sl_no")
        if sl_raw in (None, "") and isinstance(raw.get("extra"), dict):
            sl_raw = raw["extra"].get("sl_no")
        if sl_raw not in (None, "") and extra.get("sl_no") in (None, ""):
            extra["sl_no"] = sl_raw
        raw["extra"] = extra
    return parsed


def _detail_op_qty_val_alignment_gate_enabled() -> bool:
    """STOCK_DETAIL_OP_QTY_ALIGNMENT_GATE — Op→P/Cl + Sl.No + Total soft-fail.

    Default ON. Kill switch false restores pre-gate soft_ok (Excel still skipped
    via excel_column_aligned).
    """
    return os.getenv(
        "STOCK_DETAIL_OP_QTY_ALIGNMENT_GATE", "true"
    ).strip().lower() not in {"0", "false", "no", "off"}


def _detail_op_qty_val_alignment_signals(
    result: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Detect Op.Qty/Op.Val copied into P/Cl and Sl.No / header-total mismatches.

    Layout-family check for Stock and Sales Detail / SSA qty+value Vision.
    Never rewrites printed numbers — signals only for soft-fail / quality gate.
    Skip when ``excel_column_aligned`` (native Excel/Word already header-mapped).
    """
    empty = {
        "op_into_receipts": 0,
        "op_into_closing": 0,
        "max_sl_no": 0,
        "row_count": 0,
        "sl_no_mismatch": False,
        "header_total_mismatch": False,
        "skipped_excel_aligned": False,
        "soft_reasons": [],
    }
    if not isinstance(result, dict):
        return empty
    if not _detail_op_qty_val_alignment_gate_enabled():
        return empty
    totals = result.get("totals") if isinstance(result.get("totals"), dict) else {}
    extra_t = totals.get("extra") if isinstance(totals.get("extra"), dict) else {}
    if extra_t.get("excel_column_aligned"):
        empty["skipped_excel_aligned"] = True
        return empty

    items = [i for i in (result.get("line_items") or []) if isinstance(i, dict)]
    row_count = len(items)
    empty["row_count"] = row_count
    if row_count < 3:
        return empty

    op_into_receipts = 0
    op_into_closing = 0
    max_sl = 0
    for item in items:
        try:
            opening = float(item.get("opening_qty") or 0)
            receipts = float(item.get("receipts_qty") or 0)
            sales = float(item.get("sales_qty") or 0)
            closing = float(item.get("closing_qty") or 0)
        except (TypeError, ValueError):
            continue
        extra = item.get("extra") if isinstance(item.get("extra"), dict) else {}
        try:
            opening_value = float(
                item.get("opening_value")
                if item.get("opening_value") is not None
                else (extra.get("opening_value") or 0)
            )
        except (TypeError, ValueError):
            opening_value = 0.0
        try:
            receipts_value = float(
                item.get("receipts_value")
                if item.get("receipts_value") is not None
                else (extra.get("receipts_value") or extra.get("purchase_value") or 0)
            )
        except (TypeError, ValueError):
            receipts_value = 0.0
        try:
            closing_value = float(item.get("closing_value") or 0)
        except (TypeError, ValueError):
            closing_value = 0.0
        sl_raw = item.get("sl_no")
        if sl_raw in (None, ""):
            sl_raw = extra.get("sl_no")
        try:
            if sl_raw not in (None, ""):
                max_sl = max(max_sl, int(float(str(sl_raw).strip())))
        except (TypeError, ValueError):
            pass

        # Op parked in P: same qty (+ money when both present) while row moved.
        if (
            opening > 0
            and abs(receipts - opening) < 0.01
            and (
                (opening_value > 1 and abs(receipts_value - opening_value) < 0.05)
                or (receipts_value <= 1 and sales > 0)
            )
            and abs(closing - opening) > 1.01
        ):
            op_into_receipts += 1
        # Op idle-copied into Cl while sales moved stock.
        if (
            opening > 0
            and sales > 0
            and abs(closing - opening) < 0.01
            and (
                closing_value <= 1
                or opening_value <= 1
                or abs(closing_value - opening_value) < 0.05
            )
        ):
            op_into_closing += 1

    empty["op_into_receipts"] = op_into_receipts
    empty["op_into_closing"] = op_into_closing
    empty["max_sl_no"] = max_sl

    soft: List[str] = []
    if op_into_receipts >= max(2, row_count // 5):
        soft.append("op_copied_into_receipts_majority")
    if op_into_closing >= max(2, row_count // 5):
        soft.append("op_copied_into_closing_majority")
    # Sl.No max printed on Detail Report (e.g. 45) vs extracted item count.
    if max_sl >= 10 and row_count + 2 < max_sl and (max_sl - row_count) >= max(
        3, max_sl // 5
    ):
        empty["sl_no_mismatch"] = True
        soft.append("sl_no_row_count_mismatch")

    # Printed Total-row / last GRAND TOTAL S.Val / Cl.Val vs line sums.
    try:
        printed_sv = float(totals.get("sales_value")) if totals.get("sales_value") not in (None, "") else None
    except (TypeError, ValueError):
        printed_sv = None
    try:
        printed_cv = (
            float(totals.get("closing_value"))
            if totals.get("closing_value") not in (None, "")
            else None
        )
    except (TypeError, ValueError):
        printed_cv = None
    line_sv = sum(float(i.get("sales_value") or 0) for i in items)
    line_cv = sum(float(i.get("closing_value") or 0) for i in items)
    total_mismatch = False
    if printed_sv is not None and printed_sv > 100 and line_sv > 0:
        if abs(printed_sv - line_sv) > max(50.0, 0.08 * printed_sv):
            total_mismatch = True
    if printed_cv is not None and printed_cv > 100 and line_cv > 0:
        if abs(printed_cv - line_cv) > max(50.0, 0.08 * printed_cv):
            total_mismatch = True
    # Strong money miss alone (dense Detail pages) OR with any column-shift signal.
    strong_total_miss = False
    if printed_sv is not None and printed_sv > 500 and line_sv > 0:
        if abs(printed_sv - line_sv) > max(200.0, 0.15 * printed_sv):
            strong_total_miss = True
    if printed_cv is not None and printed_cv > 500 and line_cv > 0:
        if abs(printed_cv - line_cv) > max(200.0, 0.15 * printed_cv):
            strong_total_miss = True
    if total_mismatch and (
        op_into_receipts
        or op_into_closing
        or empty["sl_no_mismatch"]
        or soft
        or strong_total_miss
        or row_count >= 20
    ):
        empty["header_total_mismatch"] = True
        soft.append("header_total_mismatch")
    empty["soft_reasons"] = soft
    return empty


def validate_stock_direct_vision(
    result: Optional[Dict[str, Any]],
    layout: str = "product_stock_report_saleret",
) -> Dict[str, Any]:
    """Structural + identity validation only — never rewrites quantities."""
    reasons: List[str] = []
    if not isinstance(result, dict):
        return {"ok": False, "reasons": ["missing_result"], "row_count": 0}

    items = [i for i in (result.get("line_items") or []) if isinstance(i, dict)]
    row_count = len(items)
    min_rows = 5
    if layout.startswith("handwritten_order_form"):
        min_rows = 8
    if row_count < min_rows:
        reasons.append("too_few_rows")

    packing_leak = 0
    identity_fail = 0
    money_without_qty = 0
    receipts_qty_drop = 0
    sales_qty_drop = 0
    low_confidence_cells = 0
    named = 0
    order_form = layout.startswith("handwritten_order_form")
    pharmassist = layout.startswith("pharmassist_stock_sale")
    op_qty_val = layout.startswith("stock_sales_op_qty_val")
    opqty_qoh = layout.startswith("opqty_qoh_stock_sales")
    op_qty_pur_sale_bal = layout.startswith("op_qty_pur_sale_bal")
    op_stock_pur_sales_cl = layout.startswith("op_stock_pur_sales_cl")
    normal_open_recp = layout.startswith("normal_stock_open_recp")
    stock_grid = (
        layout.startswith("product_stock_report")
        or layout == "saleret_closstock"
        or layout.startswith("handwritten_stock")
    )

    for item in items:
        name = str(item.get("product_name") or "").strip()
        if _stock_vision_product_name_ok(name):
            named += 1
        else:
            reasons.append("weak_product_name")
            break

        pack_m = re.search(r"(\d+)\s*(?:GM|ML|TAB)\b", name, re.I)
        if pack_m:
            pack_n = float(pack_m.group(1))
            if float(item.get("sales_qty") or 0) == pack_n and float(
                item.get("opening_qty") or 0
            ) == 0:
                packing_leak += 1

        extra = item.get("extra") if isinstance(item.get("extra"), dict) else {}
        conf = extra.get("qty_confidence") if isinstance(extra.get("qty_confidence"), dict) else {}
        for field in _QTY_CONF_FIELDS:
            if str(conf.get(field) or "").lower() == "low":
                low_confidence_cells += 1

        if order_form:
            # Qty-only ORDER FORM: identity equations do not apply.
            extra["stock_identity_ok"] = True
            item["extra"] = extra
            continue

        if pharmassist or op_qty_val or normal_open_recp:
            # Op + Pur/Recp - Sale - Br/SS + Cr/SaleRet - Db/PurRet ≈ Bal/Cls.
            opening = float(item.get("opening_qty") or 0)
            receipts = float(item.get("receipts_qty") or 0)
            sales = float(item.get("sales_qty") or 0)
            closing = float(item.get("closing_qty") or 0)
            try:
                receipts_value = float(item.get("receipts_value") or 0)
            except (TypeError, ValueError):
                receipts_value = 0.0
            try:
                sales_value = float(item.get("sales_value") or 0)
            except (TypeError, ValueError):
                sales_value = 0.0
            try:
                closing_value = float(item.get("closing_value") or 0)
            except (TypeError, ValueError):
                closing_value = 0.0
            # Money printed but matching qty zeroed/shifted — trigger repair.
            if op_qty_val and (
                (receipts_value > 1 and receipts <= 0)
                or (sales_value > 1 and sales <= 0)
                or (closing_value > 1 and closing <= 0)
            ):
                money_without_qty += 1
                extra["money_without_qty"] = True
            # P.Qty+P.Val both dropped: Op-S alone cannot explain Cl (true P=0
            # rows satisfy Op-S≈Cl). Flag for Detail repair only — never invent.
            if (
                op_qty_val
                and receipts <= 0
                and receipts_value <= 1
                and opening
                and sales
                and closing
                and abs(opening - sales - closing) > 1.01
            ):
                receipts_qty_drop += 1
                extra["receipts_qty_drop_suspect"] = True
            # S.Qty+S.Val both dropped while Op(+P)-Cl implies movement.
            if (
                op_qty_val
                and sales <= 0
                and sales_value <= 1
                and opening
                and closing
                and abs(opening + receipts - closing) > 1.01
            ):
                sales_qty_drop += 1
                extra["sales_qty_drop_suspect"] = True
            br = float(extra.get("br_qty") or 0)
            if not br:
                br = float(
                    extra.get("exp_damage")
                    if extra.get("exp_damage") is not None
                    else (item.get("expiry_damage_qty") or 0)
                )
            ss = float(extra.get("sales_scheme_qty") or 0)
            sale_ret = 0.0
            pur_ret = 0.0
            if op_qty_val:
                sale_ret = float(
                    extra.get("sale_return")
                    if extra.get("sale_return") is not None
                    else (item.get("sales_return_qty") or 0)
                )
                pur_ret = float(
                    extra.get("purchase_return")
                    if extra.get("purchase_return") is not None
                    else (item.get("purchase_return_qty") or 0)
                )
            expected = round(
                opening + receipts - sales - br - ss + sale_ret - pur_ret, 2
            )
            if abs(expected - closing) > 1.01 and (
                opening or receipts or sales or closing or sale_ret or pur_ret
            ):
                if op_qty_val:
                    # Detail grids print Cr/Db/Br/Sch that Vision often misses;
                    # printed Cl.Qty still wins — do not hard-penalize soft_ok.
                    identity_fail += 1
                    extra["stock_identity_ok"] = True
                    extra["identity_valid"] = False
                    extra["identity_gap_document"] = True
                    extra["identity_expected_closing"] = expected
                else:
                    identity_fail += 1
                    extra["stock_identity_ok"] = False
                    extra["validation_failed"] = True
                    extra["identity_valid"] = False
            else:
                extra["stock_identity_ok"] = True
                extra["identity_valid"] = True
            item["extra"] = extra
            continue

        if opqty_qoh or op_qty_pur_sale_bal:
            # Op + Pur - Sale (+ SRet - PRet on Qoh sheets) ≈ closing/Bal.
            opening = float(item.get("opening_qty") or 0)
            receipts = float(item.get("receipts_qty") or 0)
            sales = float(item.get("sales_qty") or 0)
            closing = float(item.get("closing_qty") or 0)
            sale_ret = float(
                extra.get("sale_return")
                if extra.get("sale_return") is not None
                else (item.get("sales_return_qty") or 0)
            )
            pret = float(extra.get("purchase_return_qty") or 0)
            expected = round(opening + receipts - sales + sale_ret - pret, 2)
            if abs(expected - closing) > 1.01 and (
                opening or receipts or sales or closing or sale_ret
            ):
                identity_fail += 1
                if op_qty_pur_sale_bal:
                    # Printed Bal on this family often ≠ Op+Pur-Sale. Keep
                    # printed digits; do not mark validation_failed.
                    extra["stock_identity_ok"] = True
                    extra["identity_valid"] = False
                    extra["identity_gap_document"] = True
                    extra["identity_expected_closing"] = expected
                else:
                    extra["stock_identity_ok"] = False
                    extra["validation_failed"] = True
                    extra["identity_valid"] = False
            else:
                extra["stock_identity_ok"] = True
                extra["identity_valid"] = True
            item["extra"] = extra
            continue

        if op_stock_pur_sales_cl:
            # Pack|Op.Stock|Pur.Qty|Sales Qty|Cl.Stock — integer qty grid.
            # Document can print off-by-1/2 gaps; flag them (do not rewrite).
            opening = float(item.get("opening_qty") or 0)
            receipts = float(item.get("receipts_qty") or 0)
            sales = float(item.get("sales_qty") or 0)
            closing = float(item.get("closing_qty") or 0)
            expected = round(opening + receipts - sales, 2)
            if abs(expected - closing) > 0.51 and (
                opening or receipts or sales or closing
            ):
                identity_fail += 1
                extra["stock_identity_ok"] = False
                extra["validation_failed"] = True
                extra["identity_valid"] = False
                extra["identity_expected_closing"] = expected
                if abs(expected - closing) <= 2.01:
                    extra["identity_gap_document"] = True
            else:
                extra["stock_identity_ok"] = True
                extra["identity_valid"] = True
            item["extra"] = extra
            continue

        if stock_grid:
            opening = float(item.get("opening_qty") or 0)
            receipts = float(item.get("receipts_qty") or 0)
            sales = float(item.get("sales_qty") or 0)
            closing = float(item.get("closing_qty") or 0)
            total = float(extra.get("total_stock") or item.get("total_qty") or 0)
            if total == 0:
                total = round(opening + receipts, 2)
            sale_ret = float(
                extra.get("sale_return")
                if extra.get("sale_return") is not None
                else (item.get("sales_return_qty") or 0)
            )
            exp_dmg = float(
                extra.get("exp_damage")
                if extra.get("exp_damage") is not None
                else (item.get("expiry_damage_qty") or 0)
            )
            # Flag only — never mutate Gemini cell values to force balance.
            # closing ≈ total - sales + sales_return - expiry_damage
            if abs(opening + receipts - total) > 0.51:
                identity_fail += 1
                extra["stock_identity_ok"] = False
                extra["validation_failed"] = True
                extra["identity_valid"] = False
            elif abs(total - sales + sale_ret - exp_dmg - closing) > 0.51:
                identity_fail += 1
                extra["stock_identity_ok"] = False
                extra["validation_failed"] = True
                extra["identity_valid"] = False
            else:
                extra["stock_identity_ok"] = True
                extra["identity_valid"] = True
            item["extra"] = extra

    if packing_leak:
        reasons.append("packing_number_leakage")
    if named < min(min_rows, 5):
        reasons.append("insufficient_named_rows")

    # Identity is soft: flag for retry/selection, but do not hard-fail a
    # structurally complete Vision table into the OCR cascade.
    soft_reasons: List[str] = []
    # Op.Qty|Pur.Qty|Sale.Qty|Bal and O.Stk|Qoh|Value|Age often OCR with
    # small gaps; do not soft-fail into SSA dump (zeros Sale/Qoh).
    if (
        not op_qty_pur_sale_bal
        and not opqty_qoh
        and not op_qty_val
        and row_count >= min_rows
        and identity_fail > max(2, row_count // 4)
    ):
        soft_reasons.append("identity_failure_majority")
    # Detail continuation pages: dense column cascade leaves >half of rows
    # with Op+P-S≠Cl even when money/qty cells are all filled (just shifted).
    if (
        op_qty_val
        and row_count >= min_rows
        and identity_fail > max(3, row_count // 2)
    ):
        soft_reasons.append("identity_failure_majority")
    # Detail: S.Val/P.Val/Cl.Val present with qty=0 means column shift — repair.
    if op_qty_val and money_without_qty >= max(2, row_count // 5):
        soft_reasons.append("money_without_qty_majority")
    # Detail: P.Qty vanished (and P.Val too) while Op-S≠Cl — repair.
    if op_qty_val and receipts_qty_drop >= max(1, row_count // 6):
        soft_reasons.append("receipts_qty_drop_majority")
    # Detail: S.Qty+S.Val vanished while Op(+P)≠Cl — repair.
    if op_qty_val and sales_qty_drop >= max(1, row_count // 6):
        soft_reasons.append("sales_qty_drop_majority")

    align = (
        _detail_op_qty_val_alignment_signals(result)
        if op_qty_val
        else {"soft_reasons": [], "op_into_receipts": 0, "op_into_closing": 0}
    )
    for reason in align.get("soft_reasons") or []:
        if reason not in soft_reasons:
            soft_reasons.append(reason)
    if align.get("soft_reasons"):
        _log(
            "STOCK_SALES_DETAIL_ALIGNMENT_GATE",
            op_into_receipts=align.get("op_into_receipts"),
            op_into_closing=align.get("op_into_closing"),
            max_sl_no=align.get("max_sl_no"),
            row_count=align.get("row_count"),
            reasons=",".join(align.get("soft_reasons") or []),
        )

    hard_ok = not reasons
    if op_qty_pur_sale_bal:
        identity_score = max(0, row_count * 3)
    elif op_qty_val:
        # Soft-ok Detail still ranks by column-shift signals + identity gaps so
        # a no-widen / mid-band retry can beat a shifted first pass.
        identity_score = max(
            0,
            row_count * 3
            - money_without_qty * 2
            - receipts_qty_drop * 2
            - sales_qty_drop * 2
            - int(align.get("op_into_receipts") or 0) * 2
            - int(align.get("op_into_closing") or 0) * 2
            - max(0, identity_fail - row_count // 4),
        )
    else:
        identity_score = max(0, row_count * 3 - identity_fail * 2)
    return {
        "ok": hard_ok,
        "soft_ok": hard_ok and not soft_reasons,
        "reasons": reasons + soft_reasons,
        "hard_reasons": reasons,
        "soft_reasons": soft_reasons,
        "row_count": row_count,
        "identity_fail_count": identity_fail,
        "packing_leak_count": packing_leak,
        "low_confidence_cells": low_confidence_cells,
        "identity_score": identity_score,
        "identity_valid": identity_fail == 0,
        "alignment_signals": align if op_qty_val else None,
    }


def _log(event: str, **fields: Any) -> None:
    parts = [f"{event}"]
    fields = {"request_id": _request_id(), **fields}
    for key, value in fields.items():
        if value is None:
            continue
        parts.append(f"{key}={value}")
    logger.info(" ".join(parts))
    # Alias STOCK_DIRECT_VISION_* → STOCK_VISION_* for the Vision-first contract.
    if event.startswith("STOCK_DIRECT_VISION_"):
        alias = "STOCK_VISION_" + event[len("STOCK_DIRECT_VISION_") :]
        logger.info(" ".join([alias] + parts[1:]))


def _call_psr_vision(
    file_bytes: bytes,
    filename: str,
    ext: str,
    *,
    vision_bytes: bytes,
    image_meta: Dict[str, Any],
    candidate: int,
    model: str,
    route: str,
    reason: str,
    prompt: Optional[str] = None,
    layout_label: str = "product_stock_report",
    default_title: str = "Product Stock Report",
    extra_images: Optional[List[Tuple[bytes, str]]] = None,
) -> Optional[Dict[str, Any]]:
    from services.sales_extraction_runtime import (
        sales_generate_content_via_vertex as generate_content_via_vertex,
    )
    from services.sales_statement_extractor import (
        STOCK_IDENTITY_SALERET,
        _apply_parsed_sales_json,
        _extract_json_object,
        _gemini_response_text,
        empty_result,
    )

    mime = _mime_for(ext, vision_bytes, bool(image_meta.get("normalized")))
    meta = _image_meta(file_bytes, ext)
    started = time.time()
    gemini_input_kind = (
        "image_normalized_secondary"
        if image_meta.get("normalized")
        else "image_original"
    )
    _log(
        "STOCK_DIRECT_VISION_START",
        filename=filename,
        mime=mime,
        original_width=meta.get("width"),
        original_height=meta.get("height"),
        byte_size=len(vision_bytes),
        route=route,
        reason=reason,
        gemini_model=model,
        candidate=candidate,
        normalized=str(bool(image_meta.get("normalized"))).lower(),
        gemini_input=gemini_input_kind,
        numeric_source="gemini_vision",
    )
    parts: List[Dict[str, Any]] = [
        {"text": prompt or STOCK_DIRECT_VISION_PROMPT},
        {
            "inline_data": {
                "mime_type": mime,
                "data": base64.b64encode(vision_bytes).decode("ascii"),
            }
        },
    ]
    for img_bytes, label in extra_images or []:
        if not img_bytes:
            continue
        parts.append(
            {
                "text": (
                    f"Additional crop ({label}): read Opening Val / Purchase / "
                    "Closing Val / period Sales from this band when present."
                )
            }
        )
        parts.append(
            {
                "inline_data": {
                    "mime_type": "image/png",
                    "data": base64.b64encode(img_bytes).decode("ascii"),
                }
            }
        )
    # Dense Detail Op.Qty grids (40–50 SKUs × many qty/val cols) truncate at
    # 8192; reuse STOCK_VISION_MAX_OUTPUT_TOKENS (default 65535).
    _max_out = 8192
    if str(layout_label or "").startswith("stock_sales_op_qty_val") or str(
        route or ""
    ).startswith("stock_sales_op_qty_val"):
        try:
            _max_out = max(
                8192,
                int(os.getenv("STOCK_VISION_MAX_OUTPUT_TOKENS", "65535") or 65535),
            )
        except (TypeError, ValueError):
            _max_out = 65535
    payload = {
        "contents": [
            {
                "role": "user",
                "parts": parts,
            }
        ],
        "generationConfig": {"temperature": 0.0, "maxOutputTokens": _max_out},
    }
    try:
        response = generate_content_via_vertex(
            model=model,
            payload=payload,
            timeout=180 if _max_out > 8192 else 120,
            label="stock_direct_vision",
        )
        parsed = _extract_json_object(_gemini_response_text(response))
    except Exception as exc:
        _log(
            "STOCK_DIRECT_VISION_END",
            filename=filename,
            route=route,
            reason=reason,
            gemini_model=model,
            candidate=candidate,
            elapsed_ms=int((time.time() - started) * 1000),
            status="error",
            error_type=type(exc).__name__,
            gemini_input=gemini_input_kind,
            numeric_source="gemini_vision",
        )
        logger.info("STOCK_DIRECT_VISION error file=%s: %s", filename, exc)
        return None

    elapsed_ms = int((time.time() - started) * 1000)
    if not parsed or not parsed.get("line_items"):
        _log(
            "STOCK_DIRECT_VISION_END",
            filename=filename,
            route=route,
            reason=reason,
            gemini_model=model,
            candidate=candidate,
            elapsed_ms=elapsed_ms,
            status="empty",
            row_count=0,
            gemini_input=gemini_input_kind,
            numeric_source="gemini_vision",
        )
        return None

    parsed = _coerce_vision_payload(parsed)
    source_format = (ext or ".png").lstrip(".") or "png"
    result = empty_result(filename, source_format)
    result = _apply_parsed_sales_json(result, parsed)
    if str(result.get("stockist_name") or "").strip().lower() in {
        "done",
        "try out the tools first",
    }:
        result["stockist_name"] = None

    kept: List[Dict[str, Any]] = []
    for item in result.get("line_items") or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("product_name") or "").strip()
        if not name or re.search(
            r"^Done$|free\s*trial|Division\s*Total|^Total\b|"
            r"^Company\s*:|"
            r"^Grand\s*Total\b|"
            r"^Opening\s+Val(?:ue)?\b|"
            r"^Purchase\s+Val(?:ue)?\b|"
            r"^Sale\s+Val(?:ue)?\b|"
            r"^Closing\s+Val(?:ue)?\b|"
            r"^Product\s+Name\b|"
            r"^Page\s*:",
            name,
            re.I,
        ):
            continue
        if not _stock_vision_product_name_ok(name):
            continue
        extra = item.get("extra") if isinstance(item.get("extra"), dict) else {}
        item["extra"] = extra
        extra["layout"] = layout_label
        # Round qty fields that Gemini returns as floats like 9.0 / 50.0
        for key in ("opening_qty", "receipts_qty", "sales_qty", "closing_qty"):
            if item.get(key) is not None:
                try:
                    item[key] = float(item[key])
                except (TypeError, ValueError):
                    pass
        for key in (
            "total_stock",
            "sale_return",
            "exp_damage",
            "jul_qty",
            "jun_qty",
            "order_qty",
            "opening_value",
            "receipts_value",
            "purchase_value",
            "age_days",
        ):
            if extra.get(key) is not None:
                try:
                    extra[key] = float(extra[key])
                except (TypeError, ValueError):
                    pass
        for key in ("opening_value", "receipts_value", "sales_value", "closing_value"):
            if item.get(key) is not None:
                try:
                    item[key] = float(item[key])
                except (TypeError, ValueError):
                    pass
        kept.append(item)

    if len(kept) < 5:
        _log(
            "STOCK_DIRECT_VISION_END",
            filename=filename,
            route=route,
            reason=reason,
            gemini_model=model,
            candidate=candidate,
            elapsed_ms=elapsed_ms,
            status="filtered_empty",
            row_count=len(kept),
            gemini_input=gemini_input_kind,
            numeric_source="gemini_vision",
        )
        return None

    result["line_items"] = kept
    result["report_title"] = result.get("report_title") or default_title
    gemini_input = {
        "kind": gemini_input_kind,
        "original_sha256": hashlib.sha256(file_bytes).hexdigest(),
        "gemini_input_sha256": hashlib.sha256(vision_bytes).hexdigest(),
        "normalized": bool(image_meta.get("normalized")),
        "normalization_reason": image_meta.get("normalization_reason"),
        "byte_length": len(vision_bytes),
    }
    if image_meta.get("overlines_cleared_px") is not None:
        gemini_input["overlines_cleared_px"] = image_meta.get("overlines_cleared_px")

    if layout_label == "pharmassist_stock_sale":
        lock_stock_vision_result(
            result,
            extraction_method="pharmassist_stock_sale_vision",
            layout="pharmassist_stock_sale",
            psr_column_layout="pharmassist_op_pur_sale_bal",
            stock_identity_kind="opening_receipts_sales_closing",
            gemini_input=gemini_input,
            gemini_input_kind=gemini_input_kind,
            numeric_source="gemini_vision",
            early_vision_reason="stock_direct_vision_pharmassist",
            stock_direct_vision_route=route,
            stock_direct_vision_candidate=candidate,
        )
    elif layout_label == "stock_sales_op_qty_val":
        lock_stock_vision_result(
            result,
            extraction_method="stock_sales_op_qty_val_vision",
            layout="stock_sales_op_qty_val",
            psr_column_layout="op_qty_val",
            stock_identity_kind="opening_receipts_sales_closing",
            gemini_input=gemini_input,
            gemini_input_kind=gemini_input_kind,
            numeric_source="gemini_vision",
            early_vision_reason="stock_sales_op_qty_val_vision",
            stock_direct_vision_route=route,
            stock_direct_vision_candidate=candidate,
        )
    elif layout_label == "opqty_qoh_stock_sales":
        lock_stock_vision_result(
            result,
            extraction_method="opqty_qoh_stock_sales_vision",
            layout="opqty_qoh_stock_sales",
            psr_column_layout="opqty_qoh",
            stock_identity_kind="opening_receipts_sales_closing",
            gemini_input=gemini_input,
            gemini_input_kind=gemini_input_kind,
            numeric_source="gemini_vision",
            early_vision_reason="opqty_qoh_stock_sales_vision",
            stock_direct_vision_route=route,
            stock_direct_vision_candidate=candidate,
        )
    elif layout_label == "op_qty_pur_sale_bal":
        lock_stock_vision_result(
            result,
            extraction_method="op_qty_pur_sale_bal_vision",
            layout="op_qty_pur_sale_bal",
            psr_column_layout="op_qty_pur_sale_bal",
            stock_identity_kind="opening_receipts_sales_closing",
            gemini_input=gemini_input,
            gemini_input_kind=gemini_input_kind,
            numeric_source="gemini_vision",
            early_vision_reason="op_qty_pur_sale_bal_vision",
            stock_direct_vision_route=route,
            stock_direct_vision_candidate=candidate,
        )
    elif layout_label == "op_stock_pur_sales_cl":
        lock_stock_vision_result(
            result,
            extraction_method="op_stock_pur_sales_cl_vision",
            layout="op_stock_pur_sales_cl",
            psr_column_layout="op_stock_pur_sales_cl",
            stock_identity_kind="opening_receipts_sales_closing",
            gemini_input=gemini_input,
            gemini_input_kind=gemini_input_kind,
            numeric_source="gemini_vision",
            early_vision_reason="op_stock_pur_sales_cl_vision",
            stock_direct_vision_route=route,
            stock_direct_vision_candidate=candidate,
        )
    elif layout_label == "normal_stock_open_recp":
        lock_stock_vision_result(
            result,
            extraction_method="normal_stock_open_recp_vision",
            layout="normal_stock_open_recp",
            psr_column_layout="open_recp_clsg",
            stock_identity_kind="opening_receipts_sales_closing",
            gemini_input=gemini_input,
            gemini_input_kind=gemini_input_kind,
            numeric_source="gemini_vision",
            early_vision_reason="normal_stock_open_recp_vision",
            stock_direct_vision_route=route,
            stock_direct_vision_candidate=candidate,
        )
    else:
        lock_stock_vision_result(
            result,
            extraction_method="product_stock_report_vision",
            layout="product_stock_report",
            psr_column_layout="saleret",
            stock_identity_kind=STOCK_IDENTITY_SALERET,
            gemini_input=gemini_input,
            gemini_input_kind=gemini_input_kind,
            numeric_source="gemini_vision",
            early_vision_reason="stock_direct_vision_saleret",
            stock_direct_vision_route=route,
            stock_direct_vision_candidate=candidate,
        )
    # Money fill only — never qty identity repair on locked Vision results.
    from services.sales_statement_extractor import _psr_fill_line_money_totals

    _psr_fill_line_money_totals(result)

    _log(
        "STOCK_DIRECT_VISION_END",
        filename=filename,
        route=route,
        reason=reason,
        gemini_model=model,
        candidate=candidate,
        elapsed_ms=elapsed_ms,
        status="ok",
        row_count=len(kept),
        gemini_input=gemini_input_kind,
        numeric_source="gemini_vision",
    )
    _log(
        "STOCK_DIRECT_VISION_RESULT",
        filename=filename,
        detected_layout=route,
        schema="saleret_closstock",
        reason=reason,
        gemini_called="true",
        elapsed_ms=elapsed_ms,
        row_count=len(kept),
        candidate=candidate,
        gemini_input=gemini_input_kind,
        numeric_source="gemini_vision",
    )
    return result


def _preserve_handwritten_nulls_and_confidence(
    result: Dict[str, Any],
    parsed: Dict[str, Any],
) -> Tuple[int, int]:
    """Keep Gemini nulls + confidence; never invent digits for identity."""
    low_cells = 0
    identity_failures = 0
    raw_items = [i for i in (parsed.get("line_items") or []) if isinstance(i, dict)]
    out_items = [i for i in (result.get("line_items") or []) if isinstance(i, dict)]

    def _name_key(row: Dict[str, Any]) -> str:
        return re.sub(r"\s+", " ", str(row.get("product_name") or "").strip().upper())

    raw_by_name: Dict[str, Dict[str, Any]] = {}
    for raw in raw_items:
        key = _name_key(raw)
        if key:
            raw_by_name.setdefault(key, raw)

    # Prefer name match; fall back to sequential pairing for cleaned names.
    unused_raw = list(raw_items)
    for item in out_items:
        key = _name_key(item)
        raw = raw_by_name.get(key)
        if raw is None and unused_raw:
            raw = unused_raw.pop(0)
        elif raw in unused_raw:
            unused_raw.remove(raw)
        raw = raw or {}
        extra = item.get("extra") if isinstance(item.get("extra"), dict) else {}
        conf = raw.get("confidence") if isinstance(raw.get("confidence"), dict) else {}
        if conf:
            extra["qty_confidence"] = {
                field: str(conf.get(field) or "medium").lower()
                for field in _QTY_CONF_FIELDS
            }
        for field in _QTY_CONF_FIELDS:
            alias = f"{field}_confidence"
            if raw.get(alias):
                extra.setdefault("qty_confidence", {})
                extra["qty_confidence"][field] = str(raw.get(alias)).lower()

        qty_conf = (
            extra.get("qty_confidence")
            if isinstance(extra.get("qty_confidence"), dict)
            else {}
        )
        field_map = {
            "opening_qty": "opening_qty",
            "receipts_qty": "receipts_qty",
            "sales_qty": "sales_qty",
            "closing_qty": "closing_qty",
            "total_qty": ("extra", "total_stock"),
            "sales_return_qty": ("extra", "sale_return"),
            "expiry_damage_qty": ("extra", "exp_damage"),
        }
        for field, dest in field_map.items():
            if field not in raw:
                continue
            if raw.get(field) is not None:
                continue
            conf_level = str(qty_conf.get(field) or "").lower()
            # Explicit low-confidence null stays null. Otherwise blank ORDER FORM
            # Qty cells that Gemini emitted as null become 0 (empty cell).
            if conf_level == "low":
                if isinstance(dest, tuple):
                    extra[dest[1]] = None
                else:
                    item[dest] = None
                low_cells += 1
                item[f"{field}_confidence"] = "low"
            else:
                if isinstance(dest, tuple):
                    extra[dest[1]] = 0.0
                else:
                    item[dest] = 0.0
        if extra.get("stock_identity_ok") is False:
            identity_failures += 1
        item["extra"] = extra
    return low_cells, identity_failures


def _call_handwritten_vision(
    file_bytes: bytes,
    filename: str,
    ext: str,
    *,
    vision_bytes: bytes,
    image_meta: Dict[str, Any],
    candidate: int,
    model: str,
    route: str,
    reason: str,
    handwritten: str = "true",
) -> Optional[Dict[str, Any]]:
    from services.sales_extraction_runtime import (
        sales_generate_content_via_vertex as generate_content_via_vertex,
    )
    from services.sales_statement_extractor import (
        STOCK_IDENTITY_ORDER_FORM_QTY,
        STOCK_IDENTITY_SALERET,
        _apply_parsed_sales_json,
        _extract_json_object,
        _gemini_response_text,
        empty_result,
    )

    mime = _mime_for(ext, vision_bytes, bool(image_meta.get("normalized")))
    meta = _image_meta(file_bytes, ext)
    started = time.time()
    gemini_input_kind = (
        "image_normalized_secondary"
        if image_meta.get("normalized")
        else "image_original"
    )
    _log(
        "STOCK_VISION_START",
        filename=filename,
        mime=mime,
        original_width=meta.get("width"),
        original_height=meta.get("height"),
        byte_size=len(vision_bytes),
        route=route,
        reason=reason,
        gemini_model=model,
        candidate=candidate,
        handwritten=handwritten,
        gemini_input=gemini_input_kind,
        numeric_source="gemini_vision",
        image_original=str(gemini_input_kind == "image_original").lower(),
        exif_oriented=str(bool(image_meta.get("exif_oriented"))).lower(),
    )
    payload = {
        "contents": [
            {
                "role": "user",
                "parts": [
                    {"text": HANDWRITTEN_STOCK_VISION_PROMPT},
                    {
                        "inline_data": {
                            "mime_type": mime,
                            "data": base64.b64encode(vision_bytes).decode("ascii"),
                        }
                    },
                ],
            }
        ],
        "generationConfig": {"temperature": 0.0, "maxOutputTokens": 8192},
    }
    try:
        response = generate_content_via_vertex(
            model=model,
            payload=payload,
            timeout=120,
            label="stock_handwritten_vision",
        )
        parsed = _extract_json_object(_gemini_response_text(response))
    except Exception as exc:
        _log(
            "STOCK_VISION_END",
            filename=filename,
            route=route,
            reason=reason,
            gemini_model=model,
            candidate=candidate,
            elapsed_ms=int((time.time() - started) * 1000),
            status="error",
            error_type=type(exc).__name__,
            handwritten=handwritten,
            gemini_input=gemini_input_kind,
            numeric_source="gemini_vision",
        )
        logger.info("STOCK_HANDWRITTEN_VISION error file=%s: %s", filename, exc)
        return None

    elapsed_ms = int((time.time() - started) * 1000)
    if not parsed or not parsed.get("line_items"):
        _log(
            "STOCK_VISION_END",
            filename=filename,
            route=route,
            reason=reason,
            gemini_model=model,
            candidate=candidate,
            elapsed_ms=elapsed_ms,
            status="empty",
            row_count=0,
            handwritten=handwritten,
            gemini_input=gemini_input_kind,
            numeric_source="gemini_vision",
        )
        return None

    parsed = _coerce_vision_payload(parsed)
    source_format = (ext or ".png").lstrip(".") or "png"
    result = empty_result(filename, source_format)
    result = _apply_parsed_sales_json(result, parsed)
    try:
        from services.sales_statement_extractor import (
            _coerce_month_year_period_fields,
        )

        result = _coerce_month_year_period_fields(result, parsed=parsed)
    except Exception:
        pass
    if str(result.get("stockist_name") or "").strip().lower() in {
        "done",
        "try out the tools first",
    }:
        result["stockist_name"] = None

    kept: List[Dict[str, Any]] = []
    for item in result.get("line_items") or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("product_name") or "").strip()
        if not name or re.search(
            r"^Done$|free\s*trial|Division\s*Total|^Total\b", name, re.I
        ):
            continue
        if not _stock_vision_product_name_ok(name):
            continue
        extra = item.get("extra") if isinstance(item.get("extra"), dict) else {}
        item["extra"] = extra
        extra["layout"] = (
            "handwritten_order_form"
            if route.startswith("handwritten_order_form")
            else "handwritten_stock_statement"
        )
        for key in ("opening_qty", "receipts_qty", "sales_qty", "closing_qty"):
            if item.get(key) is not None:
                try:
                    item[key] = float(item[key])
                except (TypeError, ValueError):
                    pass
        for key in ("total_stock", "sale_return", "exp_damage"):
            if extra.get(key) is not None:
                try:
                    extra[key] = float(extra[key])
                except (TypeError, ValueError):
                    pass
        kept.append(item)

    min_rows = 8 if route.startswith("handwritten_order_form") else 5
    if len(kept) < min_rows:
        _log(
            "STOCK_VISION_END",
            filename=filename,
            route=route,
            reason=reason,
            gemini_model=model,
            candidate=candidate,
            elapsed_ms=elapsed_ms,
            status="filtered_empty",
            row_count=len(kept),
            handwritten=handwritten,
            gemini_input=gemini_input_kind,
            numeric_source="gemini_vision",
        )
        return None

    result["line_items"] = kept
    result["report_title"] = (
        result.get("report_title")
        or ("ORDER FORM" if route.startswith("handwritten_order_form") else "Product Stock Report")
    )
    low_cells, _ = _preserve_handwritten_nulls_and_confidence(result, parsed)

    gemini_input = {
        "kind": gemini_input_kind,
        "original_sha256": hashlib.sha256(file_bytes).hexdigest(),
        "gemini_input_sha256": hashlib.sha256(vision_bytes).hexdigest(),
        "normalized": bool(image_meta.get("normalized")),
        "normalization_reason": image_meta.get("normalization_reason"),
        "byte_length": len(vision_bytes),
    }
    if image_meta.get("overlines_cleared_px") is not None:
        gemini_input["overlines_cleared_px"] = image_meta.get("overlines_cleared_px")

    order_form_route = route.startswith("handwritten_order_form")
    lock_stock_vision_result(
        result,
        extraction_method="handwritten_stock_vision",
        layout=kept[0]["extra"].get("layout") if kept else route,
        psr_column_layout=("order_form_qty" if order_form_route else "saleret"),
        stock_identity_kind=(
            STOCK_IDENTITY_ORDER_FORM_QTY if order_form_route else STOCK_IDENTITY_SALERET
        ),
        gemini_input=gemini_input,
        gemini_input_kind=gemini_input_kind,
        numeric_source="gemini_vision",
        early_vision_reason="stock_handwritten_vision_first",
        stock_direct_vision_route=route,
        stock_direct_vision_candidate=candidate,
        handwritten=handwritten,
        low_confidence_cells=low_cells,
        stock_statement_vision_first=True,
    )

    _log(
        "STOCK_VISION_END",
        filename=filename,
        route=route,
        reason=reason,
        gemini_model=model,
        candidate=candidate,
        elapsed_ms=elapsed_ms,
        status="ok",
        row_count=len(kept),
        handwritten=handwritten,
        gemini_input=gemini_input_kind,
        numeric_source="gemini_vision",
        low_confidence_cells=low_cells,
    )
    _log(
        "STOCK_VISION_RESULT",
        filename=filename,
        detected_layout=route,
        schema="opening_purchase_total_sale_saleret_exp_closing",
        reason=reason,
        gemini_called="true",
        elapsed_ms=elapsed_ms,
        row_count=len(kept),
        candidate=candidate,
        handwritten=handwritten,
        gemini_input=gemini_input_kind,
        numeric_source="gemini_vision",
        line_items=len(kept),
        low_confidence_cells=low_cells,
    )
    return result


def extract_handwritten_stock_direct_vision(
    file_bytes: bytes,
    filename: str,
    ext: str,
    *,
    reason: str = "handwritten_vision_first",
    layout: str = "handwritten_order_form",
    handwritten: str = "true",
) -> Optional[Dict[str, Any]]:
    """Original-image handwritten Vision-first (Gemini before numeric OCR)."""
    model = os.getenv("VISION_MODEL", "gemini-2.5-flash-lite").strip()
    max_attempts = _max_attempts()
    schema = "opening_purchase_total_sale_saleret_exp_closing"
    _log(
        "STOCK_DIRECT_VISION_DECISION",
        filename=filename,
        detected_layout=layout,
        schema=schema,
        reason=f"STOCK_STATEMENT_VISION_FIRST:{reason}",
        gemini_called="pending",
        gemini_input="image_original",
        numeric_source="gemini_vision",
        handwritten=handwritten,
        image_original="true",
    )
    original_meta = {"normalized": False, "normalization_reason": None}
    candidates: List[Tuple[int, Dict[str, Any], Dict[str, Any]]] = []
    gemini_calls = 0

    # Phone photos often store landscape pixels with EXIF rotate; Vision needs
    # the upright photo. Same uploaded image content — orientation only.
    vision_primary, exif_oriented = _exif_oriented_jpeg_bytes(file_bytes)
    if exif_oriented:
        original_meta = {
            "normalized": False,
            "normalization_reason": None,
            "exif_oriented": True,
        }

    result = _call_handwritten_vision(
        file_bytes,
        filename,
        ext,
        vision_bytes=vision_primary,
        image_meta=original_meta,
        candidate=1,
        model=model,
        route=layout,
        reason=reason + ("+exif_orient" if exif_oriented else ""),
        handwritten=handwritten,
    )
    gemini_calls += 1
    validation = (
        validate_stock_direct_vision(result, layout=layout)
        if result
        else {
            "ok": False,
            "soft_ok": False,
            "reasons": ["empty_vision"],
            "row_count": 0,
            "identity_score": -1,
            "low_confidence_cells": 0,
            "identity_fail_count": 0,
        }
    )
    _log(
        "STOCK_VISION_VALIDATION",
        filename=filename,
        detected_layout=layout,
        schema=schema,
        reason=",".join(validation.get("reasons") or []) or "ok",
        gemini_called="true",
        row_count=validation.get("row_count"),
        identity_failures=validation.get("identity_fail_count"),
        low_confidence_cells=validation.get("low_confidence_cells"),
        identity_valid=str(bool(validation.get("identity_valid", True))).lower(),
        validation_status=(
            "pass"
            if validation.get("soft_ok")
            else ("soft_fail" if validation.get("ok") else "fail")
        ),
        candidate=1,
        handwritten=handwritten,
        gemini_input="image_original",
        numeric_source="gemini_vision",
    )
    if result and validation.get("ok"):
        score = int(validation.get("identity_score") or 0)
        # Prefer fewer low-confidence cells when identity is soft.
        score -= int(validation.get("low_confidence_cells") or 0)
        nonzero = sum(
            1
            for item in (result.get("line_items") or [])
            if isinstance(item, dict)
            and item.get("sales_qty") is not None
            and float(item.get("sales_qty") or 0) != 0
        )
        score += nonzero
        candidates.append((score, result, validation))
        weak = (
            not validation.get("soft_ok")
            or int(validation.get("low_confidence_cells") or 0)
            > max(2, (validation.get("row_count") or 0) // 5)
        )
        # ORDER FORM: always allow the optional secondary pass so sparse
        # handwritten digits (e.g. a lone "1") are not dropped after one call.
        if (
            validation.get("soft_ok")
            and not weak
            and not layout.startswith("handwritten_order_form")
        ):
            lock_extra = (result.get("totals") or {}).setdefault("extra", {})
            lock_extra["gemini_calls"] = gemini_calls
            lock_extra["identity_failures"] = validation.get("identity_fail_count")
            lock_extra["identity_valid"] = bool(validation.get("identity_valid", True))
            _log(
                "STOCK_VISION_FINAL",
                filename=filename,
                handwritten=handwritten,
                image_original="true",
                gemini_calls=gemini_calls,
                line_items=validation.get("row_count"),
                identity_failures=validation.get("identity_fail_count"),
                low_confidence_cells=validation.get("low_confidence_cells"),
                numeric_source="gemini_vision",
                candidate=1,
            )
            return result

    if max_attempts >= 2:
        try:
            from services.sales_statement_extractor import _psr_denoise_digit_overlines

            denoised_bytes, denoised_meta = _psr_denoise_digit_overlines(file_bytes)
        except Exception:
            denoised_bytes, denoised_meta = file_bytes, {"normalized": False}
        if denoised_meta.get("normalized") or candidates:
            # One optional secondary Vision call on a safe normalized image.
            if not denoised_meta.get("normalized"):
                try:
                    from PIL import Image, ImageOps, ImageEnhance

                    with Image.open(io.BytesIO(file_bytes)) as raw:
                        img = ImageOps.exif_transpose(raw).convert("RGB")
                        img = ImageOps.autocontrast(img, cutoff=1)
                        img = ImageEnhance.Sharpness(img).enhance(1.15)
                        buf = io.BytesIO()
                        img.save(buf, format="JPEG", quality=92)
                        denoised_bytes = buf.getvalue()
                        denoised_meta = {
                            "normalized": True,
                            "normalization_reason": "handwritten_safe_normalize",
                        }
                except Exception:
                    denoised_bytes, denoised_meta = file_bytes, {"normalized": False}
            if denoised_meta.get("normalized"):
                second = _call_handwritten_vision(
                    file_bytes,
                    filename,
                    ext,
                    vision_bytes=denoised_bytes,
                    image_meta=denoised_meta,
                    candidate=2,
                    model=model,
                    route=layout,
                    reason=reason + "+safe_normalize",
                    handwritten=handwritten,
                )
                gemini_calls += 1
                validation2 = (
                    validate_stock_direct_vision(second, layout=layout)
                    if second
                    else {
                        "ok": False,
                        "soft_ok": False,
                        "reasons": ["empty_vision"],
                        "row_count": 0,
                        "identity_score": -1,
                        "low_confidence_cells": 0,
                        "identity_fail_count": 0,
                    }
                )
                _log(
                    "STOCK_VISION_VALIDATION",
                    filename=filename,
                    detected_layout=layout,
                    schema=schema,
                    reason=",".join(validation2.get("reasons") or []) or "ok",
                    gemini_called="true",
                    row_count=validation2.get("row_count"),
                    identity_failures=validation2.get("identity_fail_count"),
                    low_confidence_cells=validation2.get("low_confidence_cells"),
                    validation_status=(
                        "pass"
                        if validation2.get("soft_ok")
                        else ("soft_fail" if validation2.get("ok") else "fail")
                    ),
                    candidate=2,
                    handwritten=handwritten,
                    numeric_source="gemini_vision",
                )
                if second and validation2.get("ok"):
                    score2 = int(validation2.get("identity_score") or 0)
                    score2 -= int(validation2.get("low_confidence_cells") or 0)
                    nonzero2 = sum(
                        1
                        for item in (second.get("line_items") or [])
                        if isinstance(item, dict)
                        and item.get("sales_qty") is not None
                        and float(item.get("sales_qty") or 0) != 0
                    )
                    score2 += nonzero2
                    # Do not merge call #1 and #2 — pick one complete response.
                    candidates.append((score2, second, validation2))

    if not candidates:
        _log(
            "STOCK_DIRECT_VISION_FALLBACK",
            filename=filename,
            detected_layout=layout,
            schema=schema,
            reason="no_structurally_valid_handwritten_vision",
            gemini_called="true",
            gemini_calls=gemini_calls,
            handwritten=handwritten,
            row_count=0,
        )
        return None

    candidates.sort(key=lambda row: row[0], reverse=True)
    best_score, best, best_val = candidates[0]
    lock_extra = (best.get("totals") or {}).setdefault("extra", {})
    lock_extra["gemini_calls"] = gemini_calls
    lock_extra["identity_failures"] = best_val.get("identity_fail_count")
    lock_extra["identity_valid"] = bool(best_val.get("identity_valid", True))
    _log(
        "STOCK_VISION_FINAL",
        filename=filename,
        handwritten=handwritten,
        image_original="true",
        gemini_calls=gemini_calls,
        line_items=best_val.get("row_count"),
        identity_failures=best_val.get("identity_fail_count"),
        low_confidence_cells=best_val.get("low_confidence_cells"),
        numeric_source="gemini_vision",
        identity_valid=str(bool(best_val.get("identity_valid", True))).lower(),
        candidate=lock_extra.get("stock_direct_vision_candidate"),
        identity_score=best_score,
    )
    return best


def _pharmassist_row_score(item: Dict[str, Any]) -> Tuple[int, int, int]:
    """Prefer starred rows, then rows with SVal/BVal, then denser qty fills."""
    name = str(item.get("product_name") or "")
    starred = 1 if "*" in name else 0
    try:
        sval = float(item.get("sales_value") or 0)
    except (TypeError, ValueError):
        sval = 0.0
    try:
        cval = float(item.get("closing_value") or 0)
    except (TypeError, ValueError):
        cval = 0.0
    money = 1 if (sval > 0.01 or cval > 0.01) else 0
    filled = 0
    for key in ("opening_qty", "receipts_qty", "sales_qty", "closing_qty"):
        try:
            if abs(float(item.get(key) or 0)) > 0.01:
                filled += 1
        except (TypeError, ValueError):
            pass
    return (starred, money, filled)


def _pharmassist_dedupe_star_rows(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Collapse NAME vs NAME* duplicates for the same packing (keep best)."""
    if len(items) < 2:
        return items
    best: Dict[Tuple[str, str], Dict[str, Any]] = {}
    order: List[Tuple[str, str]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        raw_name = str(item.get("product_name") or "").strip()
        if not raw_name:
            continue
        base = re.sub(r"\s*\*\s*", " ", raw_name)
        base = re.sub(r"\s+", " ", base).strip().upper()
        pack = re.sub(r"\s+", "", str(item.get("packing") or "").upper())
        key = (base, pack)
        prev = best.get(key)
        if prev is None:
            best[key] = item
            order.append(key)
            continue
        if _pharmassist_row_score(item) > _pharmassist_row_score(prev):
            best[key] = item
            logger.info(
                "STOCK_PHARMASSIST_STAR_DEDUP keep=%r drop=%r",
                str(item.get("product_name") or "")[:60],
                str(prev.get("product_name") or "")[:60],
            )
        else:
            logger.info(
                "STOCK_PHARMASSIST_STAR_DEDUP keep=%r drop=%r",
                str(prev.get("product_name") or "")[:60],
                str(item.get("product_name") or "")[:60],
            )
    return [best[k] for k in order if k in best]


def _pharmassist_clear_invented_row_money(item: Dict[str, Any]) -> None:
    """Rows print BVal/SVal only — drop invented opening/purchase money."""
    for key in ("opening_value", "receipts_value"):
        if item.get(key) not in (None, "", 0, 0.0):
            item[key] = None
    extra = item.get("extra") if isinstance(item.get("extra"), dict) else {}
    changed = False
    for key in ("opening_value", "receipts_value", "purchase_value"):
        if extra.get(key) not in (None, "", 0, 0.0):
            extra.pop(key, None)
            changed = True
    if changed:
        item["extra"] = extra


def _pharmassist_peel_address(peek: str) -> Optional[str]:
    """City/PIN/street/phone line under PharmAssist letterhead."""
    if not (peek or "").strip():
        return None
    m = re.search(r"(?im)^(RS\s*NO\s*:?[^\n]{8,200})", peek)
    if m:
        addr = re.sub(r"\s+", " ", m.group(1)).strip(" ,")
        if len(addr) >= 8 and not re.search(
            r"Stock\s+and\s+Sale|Opening\s+Val|Item\s+Pack|COMPANY\s+GROUP",
            addr,
            re.I,
        ):
            return addr
    m = re.search(
        r"(?im)^[A-Z0-9][A-Z0-9 .,&'\-/]{2,60}\s*\n\s*"
        r"([A-Z0-9][A-Z0-9 .,\-/]{8,160}?(?:Phone|Ph\.?|Mob\.?)[^\n]{0,60})",
        peek,
    )
    if not m:
        m = re.search(
            r"(?im)^((?:[A-Z][A-Z .,-]{3,40}-\d{6}|DAVANGERE|BANGALORE|MUMBAI|"
            r"CHENNAI|HYDERABAD|PUNE|KOLKATA)[^\n]{0,140})",
            peek,
        )
    if not m:
        return None
    addr = re.sub(r"\s+", " ", m.group(1)).strip(" ,")
    if len(addr) < 8:
        return None
    if re.search(r"Stock\s+and\s+Sale|Opening\s+Val|Item\s+Pack|HIMALAYA", addr, re.I):
        return None
    return addr


def _finalize_pharmassist_result(
    result: Optional[Dict[str, Any]],
    file_bytes: bytes = b"",
    peek_text: str = "",
    filename: str = "",
) -> Optional[Dict[str, Any]]:
    """Header/footer peel + star dedupe for PharmAssist Stock and Sale Report."""
    if not isinstance(result, dict) or not result.get("line_items"):
        return result
    if not filename:
        filename = str(result.get("source_file") or result.get("filename") or "")

    peek = peek_text or ""
    if not peek and file_bytes:
        try:
            peek = _layout_header_text(file_bytes) or ""
        except Exception:
            peek = ""
        try:
            footer_bit = _ocr_png_footer_probe(file_bytes) or ""
        except Exception:
            footer_bit = ""
        if footer_bit:
            peek = f"{peek}\n{footer_bit}".strip()

    try:
        from services.sales_statement_extractor import (
            _enrich_stock_statement_header_from_ocr,
            _clear_company_confused_stockist,
        )

        if peek:
            _enrich_stock_statement_header_from_ocr(result, peek)
        _clear_company_confused_stockist(result)
    except Exception:
        pass

    # Vision often parks "D K PHARMA (THE HIMALAYA ZEAL)" in company_name.
    company = str(result.get("company_name") or "").strip()
    stockist = str(result.get("stockist_name") or "").strip()
    if not stockist and company:
        m_co = re.match(
            r"^(.+?\bPHARMA(?:CEUTICALS?)?)\s*[\(\-–—]\s*(.+)$",
            company,
            re.I,
        )
        if m_co and re.search(r"HIMALAYA|ZEAL|ZANDRA|WELLNESS", m_co.group(2), re.I):
            result["stockist_name"] = m_co.group(1).strip(" -–—(")
            result["company_name"] = re.sub(
                r"^[\(\-–—\s]+|[\)\s]+$", "", m_co.group(2)
            ).strip()
            logger.info(
                "STOCK_PHARMASSIST_SPLIT_COMPANY stockist=%r company=%r",
                result["stockist_name"],
                result["company_name"],
            )

    if not str(result.get("stockist_address") or "").strip():
        addr = _pharmassist_peel_address(peek)
        if addr:
            result["stockist_address"] = addr
            logger.info(
                "STOCK_PHARMASSIST_ADDRESS_PEEL address=%r",
                addr[:80],
            )

    items = [i for i in (result.get("line_items") or []) if isinstance(i, dict)]
    for item in items:
        _pharmassist_clear_invented_row_money(item)
    before = len(items)
    items = _pharmassist_dedupe_star_rows(items)
    if len(items) != before:
        logger.info(
            "STOCK_PHARMASSIST_STAR_DEDUP rows_before=%s rows_after=%s",
            before,
            len(items),
        )
    result["line_items"] = items

    totals = result.setdefault("totals", {})
    extra = totals.setdefault("extra", {})
    footer_money = _peel_pharmassist_footer_money(peek) if peek else {}
    if footer_money:
        if "sales_value" in footer_money:
            totals["sales_value"] = footer_money["sales_value"]
            extra["sales_value_source"] = "pharmassist_footer_ocr"
        if "closing_value" in footer_money:
            totals["closing_value"] = footer_money["closing_value"]
            extra["closing_value_source"] = "pharmassist_footer_ocr"
        for key in ("opening_value", "purchase_value"):
            if key in footer_money:
                extra[key] = footer_money[key]
                extra[f"{key}_source"] = "pharmassist_footer_ocr"
        extra["total_row_source"] = "pharmassist_footer_ocr"
        logger.info(
            "STOCK_PHARMASSIST_FOOTER_OCR keys=%s values=%s",
            ",".join(sorted(footer_money.keys())),
            footer_money,
        )

    try:
        open_v = float(extra.get("opening_value")) if extra.get("opening_value") is not None else None
    except (TypeError, ValueError):
        open_v = None
    try:
        sv = float(totals["sales_value"]) if totals.get("sales_value") is not None else None
    except (TypeError, ValueError):
        sv = None
    try:
        cv = (
            float(totals["closing_value"])
            if totals.get("closing_value") is not None
            else None
        )
    except (TypeError, ValueError):
        cv = None

    # Drop Vision totals that look like incomplete BVal/SVal line sums
    # (integer, far below Opening Val) — never trust those as footer.
    if cv is not None and not _pharmassist_money_looks_like_footer(cv, open_v):
        logger.info(
            "STOCK_PHARMASSIST_FOOTER_REJECT field=closing_value value=%s opening=%s",
            cv,
            open_v,
        )
        totals["closing_value"] = None
        extra.pop("closing_value_source", None)
        cv = None
    if sv is not None and not _pharmassist_money_looks_like_footer(
        sv, open_v, loose_sales=True
    ):
        logger.info(
            "STOCK_PHARMASSIST_FOOTER_REJECT field=sales_value value=%s opening=%s",
            sv,
            open_v,
        )
        totals["sales_value"] = None
        extra.pop("sales_value_source", None)
        sv = None

    need_footer_vision = bool(
        file_bytes
        and open_v is not None
        and open_v >= 1000
        and (cv is None or sv is None)
    )
    if need_footer_vision:
        vision_footer = _pharmassist_footer_money_vision(file_bytes, filename)
        if vision_footer:
            if "sales_value" in vision_footer and sv is None:
                totals["sales_value"] = vision_footer["sales_value"]
                extra["sales_value_source"] = "pharmassist_footer_ocr"
                sv = vision_footer["sales_value"]
            if "closing_value" in vision_footer and cv is None:
                totals["closing_value"] = vision_footer["closing_value"]
                extra["closing_value_source"] = "pharmassist_footer_ocr"
                cv = vision_footer["closing_value"]
            for key in ("opening_value", "purchase_value"):
                if key in vision_footer and extra.get(key) in (None, "", 0, 0.0):
                    extra[key] = vision_footer[key]
                    extra[f"{key}_source"] = "pharmassist_footer_ocr"
            logger.info(
                "STOCK_PHARMASSIST_FOOTER_VISION keys=%s values=%s",
                ",".join(sorted(vision_footer.keys())),
                vision_footer,
            )

    if sv is not None and _pharmassist_money_looks_like_footer(
        sv, open_v, loose_sales=True
    ):
        extra.setdefault("sales_value_source", "pharmassist_footer_ocr")
    if cv is not None and _pharmassist_money_looks_like_footer(cv, open_v):
        extra.setdefault("closing_value_source", "pharmassist_footer_ocr")
    if (
        extra.get("opening_value") not in (None, "", 0, 0.0)
        or extra.get("purchase_value") not in (None, "", 0, 0.0)
        or extra.get("sales_value_source") == "pharmassist_footer_ocr"
        or extra.get("closing_value_source") == "pharmassist_footer_ocr"
    ):
        extra["total_row_source"] = "pharmassist_footer_ocr"

    extra["extraction_method"] = "pharmassist_stock_sale_vision"
    extra["layout"] = "pharmassist_stock_sale"
    return result


def extract_pharmassist_direct_vision(
    file_bytes: bytes,
    filename: str,
    ext: str,
    *,
    reason: str = "pharmassist_stock_sale_report",
    max_attempts: Optional[int] = None,
) -> Optional[Dict[str, Any]]:
    """Original-image Vision for PharmAssist Op./Pur/Sale/Bal. Stock and Sale Report."""
    model = os.getenv("VISION_MODEL", "gemini-2.5-flash-lite").strip()
    if max_attempts is None:
        max_attempts = _max_attempts()
    else:
        max_attempts = max(1, min(2, int(max_attempts)))
    layout = "pharmassist_stock_sale"
    schema = "pharmassist_op_pur_sale_bal"
    _log(
        "STOCK_DIRECT_VISION_DECISION",
        filename=filename,
        detected_layout=layout,
        schema=schema,
        reason=f"STOCK_STATEMENT_VISION_FIRST:{reason}",
        gemini_called="pending",
        gemini_input="image_original",
        numeric_source="gemini_vision",
    )
    original_meta = {"normalized": False, "normalization_reason": None}
    candidates: List[Tuple[int, Dict[str, Any], Dict[str, Any]]] = []

    # Rows from the original page. Footer Opening/Closing/Sales are repaired
    # in _finalize_pharmassist_result via a dedicated footer-band call when
    # needed (blank-OCR path skips pack, so budget remains for that pass).
    result = _call_psr_vision(
        file_bytes,
        filename,
        ext,
        vision_bytes=file_bytes,
        image_meta=original_meta,
        candidate=1,
        model=model,
        route=layout,
        reason=reason,
        prompt=PHARMASSIST_STOCK_SALE_VISION_PROMPT,
        layout_label="pharmassist_stock_sale",
        default_title="Stock and Sale Report",
    )
    validation = (
        validate_stock_direct_vision(result, layout=layout)
        if result
        else {
            "ok": False,
            "soft_ok": False,
            "reasons": ["empty_vision"],
            "row_count": 0,
            "identity_score": -1,
        }
    )
    _log(
        "STOCK_DIRECT_VISION_VALIDATION",
        filename=filename,
        detected_layout=layout,
        schema=schema,
        reason=",".join(validation.get("reasons") or []) or "ok",
        gemini_called="true",
        row_count=validation.get("row_count"),
        identity_failure_count=validation.get("identity_fail_count"),
        validation_status=(
            "pass"
            if validation.get("soft_ok")
            else ("soft_fail" if validation.get("ok") else "fail")
        ),
        candidate=1,
        identity_score=validation.get("identity_score"),
        gemini_input="image_original",
        numeric_source="gemini_vision",
    )
    if result and validation.get("ok"):
        extra = result.setdefault("totals", {}).setdefault("extra", {})
        extra["extraction_method"] = "pharmassist_stock_sale_vision"
        extra["layout"] = "pharmassist_stock_sale"
        candidates.append(
            (int(validation.get("identity_score") or 0), result, validation)
        )
        if validation.get("soft_ok"):
            return _finalize_pharmassist_result(
                result, file_bytes=file_bytes, filename=filename
            )
        _log(
            "STOCK_DIRECT_VISION_VALIDATION_FAILED",
            filename=filename,
            detected_layout=layout,
            schema=schema,
            reason=",".join(
                validation.get("soft_reasons") or validation.get("reasons") or []
            ),
            row_count=validation.get("row_count"),
            candidate=1,
        )
    elif not validation.get("ok"):
        _log(
            "STOCK_DIRECT_VISION_VALIDATION_FAILED",
            filename=filename,
            detected_layout=layout,
            schema=schema,
            reason=",".join(validation.get("reasons") or []),
            row_count=validation.get("row_count"),
            candidate=1,
        )

    if max_attempts >= 2:
        try:
            from services.sales_statement_extractor import _psr_denoise_digit_overlines

            denoised_bytes, denoised_meta = _psr_denoise_digit_overlines(file_bytes)
        except Exception:
            denoised_bytes, denoised_meta = None, None
        if denoised_bytes:
            result2 = _call_psr_vision(
                file_bytes,
                filename,
                ext,
                vision_bytes=denoised_bytes,
                image_meta={
                    "normalized": True,
                    "normalization_reason": "overline_denoise",
                    **(denoised_meta or {}),
                },
                candidate=2,
                model=model,
                route=layout,
                reason=reason + "+overline_denoise",
                prompt=PHARMASSIST_STOCK_SALE_VISION_PROMPT,
                layout_label="pharmassist_stock_sale",
                default_title="Stock and Sale Report",
            )
            validation2 = (
                validate_stock_direct_vision(result2, layout=layout)
                if result2
                else {
                    "ok": False,
                    "soft_ok": False,
                    "reasons": ["empty_vision"],
                    "row_count": 0,
                    "identity_score": -1,
                }
            )
            _log(
                "STOCK_DIRECT_VISION_VALIDATION",
                filename=filename,
                detected_layout=layout,
                schema=schema,
                reason=",".join(validation2.get("reasons") or []) or "ok",
                gemini_called="true",
                row_count=validation2.get("row_count"),
                validation_status=(
                    "pass"
                    if validation2.get("soft_ok")
                    else ("soft_fail" if validation2.get("ok") else "fail")
                ),
                candidate=2,
                identity_score=validation2.get("identity_score"),
            )
            if result2 and validation2.get("ok"):
                extra2 = result2.setdefault("totals", {}).setdefault("extra", {})
                extra2["extraction_method"] = "pharmassist_stock_sale_vision"
                extra2["layout"] = "pharmassist_stock_sale"
                candidates.append(
                    (
                        int(validation2.get("identity_score") or 0),
                        result2,
                        validation2,
                    )
                )
                if validation2.get("soft_ok"):
                    return _finalize_pharmassist_result(
                        result2, file_bytes=file_bytes, filename=filename
                    )

    if not candidates:
        return None
    candidates.sort(key=lambda row: row[0], reverse=True)
    best_score, best, best_val = candidates[0]
    _log(
        "STOCK_DIRECT_VISION_RESULT",
        filename=filename,
        detected_layout=layout,
        schema=schema,
        reason=reason,
        gemini_called="true",
        row_count=best_val.get("row_count"),
        validation_status="soft_fail_accepted",
        identity_score=best_score,
        candidate=(
            ((best.get("totals") or {}).get("extra") or {}).get(
                "stock_direct_vision_candidate"
            )
            or 1
        ),
    )
    return _finalize_pharmassist_result(
        best, file_bytes=file_bytes, filename=filename
    )


def _parse_pharmassist_money_token(raw: str) -> Optional[float]:
    """Parse PharmAssist footer money; repair OCR of Indian-lakh amounts.

    Examples: 1,67,783.48 | 167783.48 | 167.783 48 | 167.783.48 → 167783.48
    Rejects tiny fragments like 167.78 from a broken Closing Val OCR.
    """
    s = re.sub(r"\s+", " ", str(raw or "").strip())
    if not s:
        return None
    # Broken lakh: 167.783 48 / 167.783.48
    broken = re.fullmatch(r"(\d{2,3})\.(\d{3})[\s.]+(\d{2})", s)
    if broken:
        try:
            return float(f"{broken.group(1)}{broken.group(2)}.{broken.group(3)}")
        except ValueError:
            return None
    compact = s.replace(",", "")
    try:
        val = float(compact)
    except ValueError:
        return None
    # Statement footers are page-scale money, not two-digit fragments.
    if val < 1000:
        return None
    return val


def _ocr_png_footer_probe(png_bytes: bytes) -> str:
    """OCR mid/lower band where PharmAssist Opening/Closing Val prints."""
    if not png_bytes:
        return ""
    try:
        from PIL import Image
        from services.sales_statement_extractor import _a2z_tesseract
        from services.stock_ocr_policy import stock_ocr_context
    except Exception:
        return ""
    try:
        digest = hashlib.sha256(png_bytes).hexdigest()[:10]
        tess = _a2z_tesseract()
        with Image.open(io.BytesIO(png_bytes)) as raw:
            img = raw.convert("RGB")
        width, height = img.size
        if width < 80 or height < 80:
            return ""
        band = img.crop((0, int(height * 0.20), width, int(height * 0.70)))
        band.thumbnail((1600, 1200))
        with stock_ocr_context(
            region=f"scanned_pharmassist_footer_{digest}", page="1"
        ):
            return tess.image_to_string(band, config="--psm 6") or ""
    except Exception:
        return ""


def _pharmassist_money_looks_like_footer(
    value: Optional[float],
    opening_value: Optional[float] = None,
    *,
    loose_sales: bool = False,
) -> bool:
    """True when a Vision total looks like printed footer money, not a line sum."""
    if value is None:
        return False
    try:
        val = float(value)
    except (TypeError, ValueError):
        return False
    if val < 1000:
        return False
    has_paise = abs(val - round(val)) >= 0.005
    try:
        open_v = float(opening_value) if opening_value is not None else None
    except (TypeError, ValueError):
        open_v = None
    # Printed PharmAssist footer amounts almost always include paise (.xx).
    # Integer totals are usually incomplete BVal/SVal line sums.
    if not has_paise:
        return False
    if open_v is not None and open_v >= 1000:
        # Closing should sit near Opening Val scale; line BVal sums undershoot.
        if not loose_sales and val + 0.51 < open_v * 0.55:
            return False
        # Period Sales is usually well below Opening Val.
        if loose_sales and val + 0.51 > open_v * 0.85:
            return False
    return True


_PHARMASSIST_FOOTER_ONLY_PROMPT = """
Read ONLY the manufacturer footer money band on this crop
(Opening Val / Purchase / Closing Val / Sales).

Return ONLY JSON:
{
  "opening_value": number|null,
  "purchase_value": number|null,
  "closing_value": number|null,
  "sales_value": number|null
}

Rules:
- Copy printed digits. Indian commas: 1,05,054.58 -> 105054.58.
- sales_value = plain "Sales :" / "Sales" amount for the period.
- NEVER use Sales (Sep) / Sales (Aug) / other month history.
- Do not invent. Null when a label is not visible.
""".strip()


def _pharmassist_footer_money_vision(
    file_bytes: bytes,
    filename: str = "",
) -> Dict[str, float]:
    """One Gemini call on the footer band for Opening/Purchase/Closing/Sales."""
    band = _pharmassist_footer_band_png(file_bytes)
    if not band:
        return {}
    try:
        from services.sales_extraction_runtime import (
            sales_generate_content_via_vertex as generate_content_via_vertex,
        )
        from services.sales_statement_extractor import (
            _extract_json_object,
            _gemini_response_text,
        )
    except Exception:
        return {}
    model = os.getenv("VISION_MODEL", "gemini-2.5-flash-lite").strip()
    payload = {
        "contents": [
            {
                "role": "user",
                "parts": [
                    {"text": _PHARMASSIST_FOOTER_ONLY_PROMPT},
                    {
                        "inline_data": {
                            "mime_type": "image/png",
                            "data": base64.b64encode(band).decode("ascii"),
                        }
                    },
                ],
            }
        ],
        "generationConfig": {"temperature": 0.0, "maxOutputTokens": 512},
    }
    try:
        response = generate_content_via_vertex(
            model=model,
            payload=payload,
            timeout=60,
            label="stock_pharmassist_footer",
        )
        parsed = _extract_json_object(_gemini_response_text(response)) or {}
    except Exception as exc:
        logger.info(
            "STOCK_PHARMASSIST_FOOTER_VISION error file=%s err=%s",
            filename,
            type(exc).__name__,
        )
        return {}
    out: Dict[str, float] = {}
    try:
        open_v = (
            float(parsed["opening_value"])
            if parsed.get("opening_value") is not None
            else None
        )
    except (TypeError, ValueError):
        open_v = None
    for key in ("opening_value", "purchase_value", "closing_value", "sales_value"):
        raw = parsed.get(key)
        if raw is None:
            continue
        try:
            val = float(raw)
        except (TypeError, ValueError):
            continue
        loose = key == "sales_value"
        if key in {"opening_value", "purchase_value"}:
            if val >= 1000:
                out[key] = val
            continue
        if _pharmassist_money_looks_like_footer(val, open_v or out.get("opening_value"), loose_sales=loose):
            out[key] = val
    return out


def _pharmassist_footer_band_png(png_bytes: bytes) -> bytes:
    """Crop+enlarge the Opening/Closing/Sales/Purchase manufacturer footer."""
    if not png_bytes:
        return b""
    try:
        from PIL import Image
    except Exception:
        return b""
    try:
        with Image.open(io.BytesIO(png_bytes)) as raw:
            img = raw.convert("RGB")
        width, height = img.size
        if width < 120 or height < 200:
            return b""
        top = int(height * 0.68)
        footer = img.crop((0, top, width, height))
        zoom_w = max(900, int(width * 1.6))
        zoom_h = max(280, int(footer.height * 2.2))
        _lanczos = getattr(getattr(Image, "Resampling", Image), "LANCZOS", Image.LANCZOS)
        footer_zoom = footer.resize((zoom_w, zoom_h), _lanczos)
        buf = io.BytesIO()
        footer_zoom.save(buf, format="PNG", optimize=True)
        return buf.getvalue()
    except Exception:
        return b""


def _pharmassist_compose_footer_zoom(png_bytes: bytes) -> bytes:
    """Full page + enlarged footer band so Opening/Closing/Sales stay readable.

    Phone composites often bury the manufacturer footer under dense rows;
    one Gemini call with a side zoom avoids a second budget burn.
    """
    if not png_bytes:
        return png_bytes
    try:
        from PIL import Image, ImageDraw
    except Exception:
        return png_bytes
    try:
        with Image.open(io.BytesIO(png_bytes)) as raw:
            img = raw.convert("RGB")
        width, height = img.size
        if width < 120 or height < 200:
            return png_bytes
        footer_bytes = _pharmassist_footer_band_png(png_bytes)
        if not footer_bytes:
            return png_bytes
        with Image.open(io.BytesIO(footer_bytes)) as fz:
            footer_zoom = fz.convert("RGB")
        gap = 8
        canvas = Image.new(
            "RGB",
            (width + gap + footer_zoom.width, max(height, footer_zoom.height)),
            (255, 255, 255),
        )
        canvas.paste(img, (0, 0))
        canvas.paste(footer_zoom, (width + gap, 0))
        draw = ImageDraw.Draw(canvas)
        draw.rectangle(
            [width + gap, 0, canvas.width - 1, footer_zoom.height - 1],
            outline=(0, 90, 180),
            width=3,
        )
        draw.text(
            (width + gap + 6, 6),
            "ZOOM Opening Val|Purchase|Closing Val|Sales (period)",
            fill=(0, 90, 180),
        )
        buf = io.BytesIO()
        canvas.save(buf, format="PNG", optimize=True)
        out = buf.getvalue()
        logger.info(
            "STOCK_PHARMASSIST_FOOTER_ZOOM bytes_in=%s bytes_out=%s",
            len(png_bytes),
            len(out),
        )
        return out or png_bytes
    except Exception as exc:
        logger.info(
            "STOCK_PHARMASSIST_FOOTER_ZOOM skip reason=%s",
            type(exc).__name__,
        )
        return png_bytes


def _peel_pharmassist_footer_money(text: str) -> Dict[str, float]:
    """Peel Opening/Closing/Sales/Purchase footer labels from OCR peek text.

    When ``Grand Total`` is printed, prefer the last Grand Total band over
    earlier section ``Total :`` money.
    """
    blob = text or ""
    out: Dict[str, float] = {}
    grand_matches = list(re.finditer(r"Grand\s*Total\s*:?", blob, re.I))
    if grand_matches:
        start = grand_matches[-1].start()
        blob = blob[start : start + 600]

    specs = (
        (
            "opening_value",
            r"Opening\s+Val\.?\s*:?\s*([\d.,]+(?:\s+\d{2})?)",
        ),
        (
            "closing_value",
            r"Clos(?:ing|ina)\s+Val\.?\s*:?\s*([\d.,]+(?:\s+\d{2})?)",
        ),
        ("purchase_value", r"Purchase\s*:?\s*([\d,]+\.\d{2})"),
        # Period Sales only — not Sales(Sep) / Sales(Aug) history.
        (
            "sales_value",
            r"(?<!\()\bSales\s*:?\s*([\d,]+\.\d{2})(?!\s*\()",
        ),
    )
    for key, pat in specs:
        matches = list(re.finditer(pat, blob, re.I))
        if not matches:
            continue
        val = _parse_pharmassist_money_token(matches[-1].group(1))
        if val is not None:
            out[key] = val
    return out


def extract_pharmassist_scanned_pdf(
    file_bytes: bytes,
    filename: str,
) -> Optional[Dict[str, Any]]:
    """Image-only PharmAssist Stock and Sale Report before camera_multipage.

    Prior-month cols (Sep/Aug, Jul/Jun, …) must not become opening/receipts.
    Budget: max 2 Gemini page calls. Flag: STOCK_PHARMASSIST_SCANNED_EARLY.
    """
    if not _pharmassist_scanned_early_enabled():
        logger.info(
            "STOCK_PHARMASSIST_SCANNED_PDF file=%s status=skip reason=flag_off",
            filename,
        )
        return None

    images = _render_pdf_page_pngs(file_bytes, max_pages=2, zoom=2.8)
    if not images:
        try:
            from services.gemini_extraction_fallback import _pdf_images

            images = _pdf_images(file_bytes, max_pages=2) or []
        except Exception:
            images = []
    if not images:
        return None

    peeks = [_ocr_png_header_probe(img) for img in images[:2]]
    footer_peek = _ocr_png_footer_probe(images[-1]) if images else ""
    peek_blob = "\n".join(p for p in (peeks + [footer_peek]) if p)
    headers_ok = _has_pharmassist_stock_sale_headers(peek_blob)
    # No Tesseract / blank OCR: still try one Vision page and keep only when
    # the Op/Pur/Sale/Bal/BVal/SVal identity soft-passes (avoids camera_multipage
    # Sep|Aug → opening/receipts shift on system-Python runs).
    ocr_blank = not (peek_blob or "").strip()
    if not headers_ok and not ocr_blank:
        logger.info(
            "STOCK_PHARMASSIST_SCANNED_PDF file=%s status=skip reason=headers",
            filename,
        )
        return None
    if not headers_ok and ocr_blank:
        logger.info(
            "STOCK_PHARMASSIST_SCANNED_PDF file=%s status=try "
            "reason=empty_ocr_probe pages=%s",
            filename,
            len(images),
        )

    logger.info(
        "STOCK_PHARMASSIST_SCANNED_PDF file=%s status=try pages=%s headers_ok=%s",
        filename,
        len(images),
        str(headers_ok).lower(),
    )

    seed: Optional[Dict[str, Any]] = None
    merged_items: List[Dict[str, Any]] = []
    calls = 0
    for page_index, image in enumerate(images[:2]):
        if calls >= 2:
            break
        page = extract_pharmassist_direct_vision(
            image,
            f"{filename}#page{page_index + 1}",
            ".png",
            reason=f"scanned_pdf_pharmassist_page{page_index + 1}",
            max_attempts=1,
        )
        calls += 1
        if not page or not page.get("line_items"):
            continue
        if not headers_ok and seed is None:
            probe_val = validate_stock_direct_vision(
                page, layout="pharmassist_stock_sale"
            )
            sval_pos = sum(
                1
                for it in (page.get("line_items") or [])
                if isinstance(it, dict)
                and float(it.get("sales_value") or 0) > 0.01
            )
            if not probe_val.get("ok") or sval_pos < 3:
                logger.info(
                    "STOCK_PHARMASSIST_SCANNED_PDF file=%s status=skip "
                    "reason=empty_ocr_not_pharmassist sval_pos=%s ok=%s",
                    filename,
                    sval_pos,
                    probe_val.get("ok"),
                )
                return None
            headers_ok = True
        if seed is None:
            seed = page
        else:
            # Prefer later-page footer money when the first page has none.
            st = seed.setdefault("totals", {})
            pt = page.get("totals") or {}
            if st.get("sales_value") in (None, 0, 0.0) and pt.get("sales_value") not in (
                None,
                0,
                0.0,
            ):
                st["sales_value"] = pt.get("sales_value")
            if st.get("closing_value") in (None, 0, 0.0) and pt.get(
                "closing_value"
            ) not in (None, 0, 0.0):
                st["closing_value"] = pt.get("closing_value")
            sex = st.setdefault("extra", {})
            pex = pt.get("extra") if isinstance(pt.get("extra"), dict) else {}
            for key in ("opening_value", "purchase_value"):
                if sex.get(key) in (None, 0, 0.0) and pex.get(key) not in (
                    None,
                    0,
                    0.0,
                ):
                    sex[key] = pex.get(key)
            if not seed.get("company_name") and page.get("company_name"):
                seed["company_name"] = page.get("company_name")
            if not seed.get("period_from") and page.get("period_from"):
                seed["period_from"] = page.get("period_from")
                seed["period_to"] = page.get("period_to")
        for item in page.get("line_items") or []:
            if isinstance(item, dict):
                merged_items.append(item)
        logger.info(
            "STOCK_PHARMASSIST_SCAN_PER_PAGE file=%s page=%s rows=%s",
            filename,
            page_index + 1,
            len(page.get("line_items") or []),
        )

    if seed is None or len(merged_items) < 5:
        logger.info(
            "STOCK_PHARMASSIST_SCANNED_PDF file=%s status=skip rows=%s",
            filename,
            len(merged_items),
        )
        return None

    seed["line_items"] = merged_items
    seed = _finalize_pharmassist_result(seed, peek_text=peek_blob)
    if not seed or not seed.get("line_items"):
        logger.info(
            "STOCK_PHARMASSIST_SCANNED_PDF file=%s status=skip reason=finalize_empty",
            filename,
        )
        return None

    validation = validate_stock_direct_vision(seed, layout="pharmassist_stock_sale")
    if not validation.get("ok"):
        logger.info(
            "STOCK_PHARMASSIST_SCANNED_PDF file=%s status=reject reasons=%s",
            filename,
            ",".join(validation.get("reasons") or []) or "none",
        )
        return None

    extra = seed.setdefault("totals", {}).setdefault("extra", {})
    extra["extraction_method"] = "pharmassist_stock_sale_vision"
    extra["layout"] = "pharmassist_stock_sale"
    extra["gemini_calls"] = calls
    extra["pharmassist_scanned_pdf"] = True
    extra["skip_post_pipeline_gemini"] = True
    if str(extra.get("sales_value_source") or "") == "pharmassist_footer_ocr":
        extra["total_row_source"] = "pharmassist_footer_ocr"
    lock_stock_vision_result(
        seed,
        extraction_method="pharmassist_stock_sale_vision",
        layout="pharmassist_stock_sale",
        psr_column_layout="pharmassist_op_pur_sale_bal",
        stock_identity_kind="opening_receipts_sales_closing",
        numeric_source="gemini_vision",
        early_vision_reason="pharmassist_scanned_pdf",
        gemini_calls=calls,
    )
    logger.info(
        "STOCK_PHARMASSIST_SCANNED_PDF file=%s status=ok rows=%s "
        "sales_value=%s closing_value=%s opening_value=%s purchase_value=%s "
        "calls=%s",
        filename,
        len(seed.get("line_items") or []),
        (seed.get("totals") or {}).get("sales_value"),
        (seed.get("totals") or {}).get("closing_value"),
        ((seed.get("totals") or {}).get("extra") or {}).get("opening_value"),
        ((seed.get("totals") or {}).get("extra") or {}).get("purchase_value"),
        calls,
    )
    return seed


def _clear_detail_closing_qty_copied_from_sales(result: Dict[str, Any]) -> int:
    """Drop Cl.Qty when it is clearly a copy of S.Qty (keep Cl.Val).

    Never invents a replacement from Op+P-S — marks the qty missing only.
    """
    cleared = 0
    for item in result.get("line_items") or []:
        if not isinstance(item, dict):
            continue
        try:
            opening = float(item.get("opening_qty") or 0)
            receipts = float(item.get("receipts_qty") or 0)
            sales = float(item.get("sales_qty") or 0)
            closing = float(item.get("closing_qty") or 0)
            closing_value = float(item.get("closing_value") or 0)
        except (TypeError, ValueError):
            continue
        if not (
            closing > 0
            and sales > 0
            and abs(closing - sales) < 0.01
            and closing_value > 1
            and opening
            and abs(opening - closing) > 1.01
        ):
            continue
        # Identity already disagrees with this Cl.Qty — S.Qty bleed.
        expected = opening + receipts - sales
        if abs(expected - closing) <= 1.01:
            continue
        item["closing_qty"] = None
        extra = item.setdefault("extra", {})
        if not isinstance(extra, dict):
            extra = {}
            item["extra"] = extra
        fs = extra.setdefault("field_source", {})
        if isinstance(fs, dict):
            fs["closing_qty"] = "missing"
        extra["closing_qty_cleared_sales_copy"] = True
        cleared += 1
        _log(
            "STOCK_SALES_DETAIL_CL_SALES_COPY_CLEARED",
            product=str(item.get("product_name") or "")[:60],
            sales_qty=sales,
            closing_value=closing_value,
        )
    return cleared


def _peel_detail_header_total_money_from_text(
    result: Dict[str, Any], text: str
) -> bool:
    """Copy printed Total / last GRAND TOTAL S.Val + Cl.Val from OCR chrome.

    Detail Reports often print a top ``Total`` money band (Op.Val / P.val /
    S.Val / Cr.Val / Cl.Val). Prefer last GRAND TOTAL when several bands exist.
    Never invents — only copies money-scale numbers from a Total-labeled line.
    """
    if not isinstance(result, dict) or not (text or "").strip():
        return False
    candidates: List[Tuple[bool, List[float]]] = []
    for ln in str(text).splitlines():
        stripped = ln.strip()
        if not re.search(r"(?:GRAND\s+)?TOTAL\b", stripped, re.I):
            continue
        if re.search(
            r"Total\s+Stock|Stock\s+Total|Total\s+Qty|Page\s+Total", stripped, re.I
        ):
            continue
        nums = re.findall(
            r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+\.\d{1,2}|\d{4,}", stripped
        )
        money: List[float] = []
        for raw in nums:
            try:
                val = float(str(raw).replace(",", ""))
            except (TypeError, ValueError):
                continue
            if val >= 50:
                money.append(val)
        if len(money) >= 2:
            is_grand = bool(re.search(r"\bGRAND\s+TOTAL\b", stripped, re.I))
            candidates.append((is_grand, money))
    if not candidates:
        return False
    grand = [m for is_g, m in candidates if is_g]
    money = grand[-1] if grand else candidates[-1][1]
    totals = result.setdefault("totals", {})
    extra = totals.setdefault("extra", {}) if isinstance(totals, dict) else {}
    if not isinstance(extra, dict):
        return False
    # Typical Detail Total band: … Op.Val, P.val, S.Val, Cr.Val, Cl.Val.
    sales = money[-3] if len(money) >= 5 else money[-2]
    closing = money[-1]
    opening = money[-5] if len(money) >= 5 else (money[0] if len(money) >= 3 else None)
    receipts = money[-4] if len(money) >= 5 else None
    changed = False
    try:
        cur_sv = float(totals.get("sales_value")) if totals.get("sales_value") not in (None, "") else None
    except (TypeError, ValueError):
        cur_sv = None
    try:
        cur_cv = (
            float(totals.get("closing_value"))
            if totals.get("closing_value") not in (None, "")
            else None
        )
    except (TypeError, ValueError):
        cur_cv = None
    if sales >= 100 and (cur_sv is None or abs(cur_sv - sales) > 0.51):
        # Prefer printed Total when current is missing or is a lower line-sum.
        if cur_sv is None or cur_sv + 0.51 < sales:
            totals["sales_value"] = round(sales, 2)
            changed = True
    if closing >= 100 and (cur_cv is None or abs(cur_cv - closing) > 0.51):
        if cur_cv is None or cur_cv + 0.51 < closing:
            totals["closing_value"] = round(closing, 2)
            changed = True
    if opening is not None and opening >= 100 and extra.get("opening_value") in (None, ""):
        extra["opening_value"] = round(opening, 2)
        changed = True
    if receipts is not None and receipts >= 50 and extra.get("receipts_value") in (None, ""):
        extra["receipts_value"] = round(receipts, 2)
        changed = True
    if changed:
        extra["total_row_source"] = (
            "detail_ocr_grand_total" if grand else "detail_ocr_total"
        )
        _log(
            "STOCK_SALES_DETAIL_OCR_TOTAL",
            sales_value=totals.get("sales_value"),
            closing_value=totals.get("closing_value"),
            opening_value=extra.get("opening_value"),
            source=extra.get("total_row_source"),
        )
    return changed


def _drop_detail_total_named_line_items(result: Dict[str, Any]) -> int:
    """Remove ----Total---->> / bare Total rows misfiled as products."""
    items = result.get("line_items") if isinstance(result, dict) else None
    if not isinstance(items, list):
        return 0
    kept: List[Dict[str, Any]] = []
    dropped = 0
    for item in items:
        if not isinstance(item, dict):
            continue
        name = str(item.get("product_name") or "").strip()
        if re.search(
            r"^[-–—=\s>]*Total[-–—=\s>]*$|"
            r"^[-–—=\s]*Total[-–—=\s>]+\s*$|"
            r"--+\s*Total|--+\s*>?\s*$|"
            r"^Grand\s*Total\b",
            name,
            re.I,
        ):
            dropped += 1
            continue
        kept.append(item)
    if dropped:
        result["line_items"] = kept
        _log("STOCK_SALES_DETAIL_DROP_TOTAL_PRODUCTS", dropped=dropped, kept=len(kept))
    return dropped


def _finalize_detail_op_qty_val_result(
    result: Optional[Dict[str, Any]], file_bytes: bytes = b""
) -> Optional[Dict[str, Any]]:
    """Enrich Seller:/Himalaya headers and swap confused company/stockist."""
    if not isinstance(result, dict):
        return result
    peek = ""
    try:
        from services.sales_statement_extractor import (
            _enrich_stock_statement_header_from_ocr,
            _clear_company_confused_stockist,
        )

        try:
            peek = _layout_header_text(file_bytes) if file_bytes else ""
        except Exception:
            peek = ""
        if peek:
            _enrich_stock_statement_header_from_ocr(result, peek)
        _clear_company_confused_stockist(result)
    except Exception:
        pass
    try:
        _drop_detail_total_named_line_items(result)
    except Exception:
        pass
    try:
        _clear_detail_closing_qty_copied_from_sales(result)
    except Exception:
        pass
    # Printed top Total / last GRAND TOTAL money from OCR before line-sum fill.
    try:
        if peek:
            _peel_detail_header_total_money_from_text(result, peek)
    except Exception:
        pass
    # CONSOLIDATED Total Op.Val|P.Val|S.Val|Cls.Val before line-sum sanitize.
    try:
        from services.sales_statement_extractor import (
            _apply_consolidated_op_qty_val_printed_total,
        )

        result = _apply_consolidated_op_qty_val_printed_total(
            result, file_bytes=file_bytes or b"", ocr_hint=peek or ""
        )
    except Exception:
        pass
    # Prefer printed Total-row money; else line sums so multipage totals are not null.
    try:
        from services.sales_statement_extractor import _sanitize_compute_totals

        _sanitize_compute_totals(result)
    except Exception:
        totals = result.setdefault("totals", {})
        items = [i for i in (result.get("line_items") or []) if isinstance(i, dict)]
        if totals.get("sales_value") is None:
            sv = sum(float(i.get("sales_value") or 0) for i in items)
            if sv > 0:
                totals["sales_value"] = round(sv, 2)
        if totals.get("closing_value") is None:
            cv = sum(float(i.get("closing_value") or 0) for i in items)
            if cv > 0:
                totals["closing_value"] = round(cv, 2)
    # Re-stamp printed Total after sanitize when line sums under-shot it.
    try:
        if peek:
            _peel_detail_header_total_money_from_text(result, peek)
    except Exception:
        pass
    # Keep line_*_value_sum aligned with remaining line_items (no stale pre-filter).
    try:
        totals = result.setdefault("totals", {})
        extra = totals.setdefault("extra", {})
        items = [i for i in (result.get("line_items") or []) if isinstance(i, dict)]
        if isinstance(extra, dict):
            extra["line_sales_value_sum"] = round(
                sum(float(i.get("sales_value") or 0) for i in items), 2
            )
            extra["line_closing_value_sum"] = round(
                sum(float(i.get("closing_value") or 0) for i in items), 2
            )
            extra["rows_detected"] = len(items)
    except Exception:
        pass
    return result


def extract_stock_sales_op_qty_val_direct_vision(
    file_bytes: bytes,
    filename: str,
    ext: str,
    *,
    reason: str = "stock_sales_op_qty_val_headers",
    max_attempts: Optional[int] = None,
) -> Optional[Dict[str, Any]]:
    """Original-image Vision for STOCK AND SALES Op.Qty/Op.Val paired columns."""
    # Dense Detail phone scans (P.Sch/Cr/Db + continuation pages) need more
    # than flash-lite; keep override via VISION_MODEL when ops set one.
    model = os.getenv("VISION_MODEL", "gemini-2.5-flash").strip()
    if max_attempts is None:
        max_attempts = _max_attempts()
    else:
        max_attempts = max(1, min(2, int(max_attempts)))
    layout = "stock_sales_op_qty_val"
    schema = "stock_sales_op_qty_val"
    _log(
        "STOCK_DIRECT_VISION_DECISION",
        filename=filename,
        detected_layout=layout,
        schema=schema,
        reason=f"STOCK_STATEMENT_VISION_FIRST:{reason}",
        gemini_called="pending",
        gemini_input="image_original",
        numeric_source="gemini_vision",
    )
    original_meta = {"normalized": False, "normalization_reason": None}
    candidates: List[Tuple[int, Dict[str, Any], Dict[str, Any]]] = []

    # Phone JPEGs often store landscape pixels with EXIF Orientation=6/8.
    # Vision must see upright text; same photo content — orientation only.
    vision_primary, exif_oriented = _exif_oriented_jpeg_bytes(file_bytes)
    if exif_oriented:
        original_meta = {
            "normalized": False,
            "normalization_reason": None,
            "exif_oriented": True,
        }
    # Continuation pages have no header row; Cl-only zoom biases Op→P→S
    # left-shifts. Prefer mid-band Op→S zoom on those pages (best SEPTILIN /
    # V-GEL recovery in audits).
    continuation = "continuation" in str(reason or "").lower()
    widen_cl = _stock_sales_detail_widen_cl_enabled() and not continuation
    widen_movement = continuation
    if widen_movement:
        vision_bytes = _widen_detail_movement_columns(vision_primary)
        original_meta = {
            **original_meta,
            "stock_sales_detail_widen_cl": False,
            "stock_sales_detail_widen_movement": True,
        }
    elif widen_cl:
        vision_bytes = _widen_detail_closing_columns(vision_primary)
        original_meta = {
            **original_meta,
            "stock_sales_detail_widen_cl": True,
        }
    else:
        vision_bytes = vision_primary
    vision_bytes = _limit_detail_vision_image(vision_bytes)
    call_reason = reason + (
        ("+exif_orient" if exif_oriented else "")
        + ("+widen_movement" if widen_movement else "")
        + ("+widen_cl" if widen_cl else "")
    )

    result = _call_psr_vision(
        file_bytes,
        filename,
        ext,
        vision_bytes=vision_bytes,
        image_meta=original_meta,
        candidate=1,
        model=model,
        route=layout,
        reason=call_reason,
        prompt=STOCK_SALES_OP_QTY_VAL_VISION_PROMPT,
        layout_label="stock_sales_op_qty_val",
        default_title="STOCK AND SALES",
    )
    validation = (
        validate_stock_direct_vision(result, layout=layout)
        if result
        else {
            "ok": False,
            "soft_ok": False,
            "reasons": ["empty_vision"],
            "row_count": 0,
            "identity_score": -1,
        }
    )
    _log(
        "STOCK_DIRECT_VISION_VALIDATION",
        filename=filename,
        detected_layout=layout,
        schema=schema,
        reason=",".join(validation.get("reasons") or []) or "ok",
        gemini_called="true",
        row_count=validation.get("row_count"),
        identity_failure_count=validation.get("identity_fail_count"),
        validation_status=(
            "pass"
            if validation.get("soft_ok")
            else ("soft_fail" if validation.get("ok") else "fail")
        ),
        candidate=1,
        identity_score=validation.get("identity_score"),
        gemini_input="image_original",
        numeric_source="gemini_vision",
    )
    if result and validation.get("ok"):
        extra = result.setdefault("totals", {}).setdefault("extra", {})
        extra["extraction_method"] = "stock_sales_op_qty_val_vision"
        extra["layout"] = "stock_sales_op_qty_val"
        candidates.append(
            (int(validation.get("identity_score") or 0), result, validation)
        )
        if validation.get("soft_ok"):
            result = _finalize_detail_op_qty_val_result(result, file_bytes)
            lock_stock_vision_result(
                result,
                early_vision_reason="stock_sales_op_qty_val_vision",
                stock_direct_vision_route=layout,
                stock_direct_vision_candidate=1,
                gemini_calls=1,
            )
            return result

    if max_attempts >= 2 and (not candidates or not validation.get("soft_ok")):
        soft_rs = {
            str(x)
            for x in (
                (validation.get("soft_reasons") or [])
                if isinstance(validation, dict)
                else []
            )
        }
        # Dropped/shifted P/S/Cl mid-columns: Cl-zoom merge rarely recovers
        # P.Qty. Prefer a full main-prompt re-read without the Cl strip.
        column_shift_soft = bool(
            soft_rs
            & {
                "money_without_qty_majority",
                "receipts_qty_drop_majority",
                "sales_qty_drop_majority",
                "identity_failure_majority",
                "op_copied_into_receipts_majority",
                "op_copied_into_closing_majority",
                "sl_no_row_count_mismatch",
                "header_total_mismatch",
            }
        )
        # Column-shift soft fails use a full re-read (below). Identity-repair
        # merge is only for residual soft gaps that are not mid-column drops —
        # merge has been observed inventing Cl=Op+P-S on Detail scans.
        use_identity_repair = (
            _stock_sales_detail_identity_repair_enabled()
            and bool(result)
            and bool(validation.get("ok"))
            and not validation.get("soft_ok")
            and not column_shift_soft
        )
        if use_identity_repair:
            _log(
                "STOCK_SALES_DETAIL_IDENTITY_REPAIR",
                filename=filename,
                detected_layout=layout,
                schema=schema,
                reason="soft_identity_fail",
                gemini_called="pending",
                identity_failure_count=validation.get("identity_fail_count"),
                row_count=validation.get("row_count"),
            )
            repair = _call_psr_vision(
                file_bytes,
                filename,
                ext,
                vision_bytes=vision_bytes,
                image_meta={
                    **original_meta,
                    "stock_sales_detail_identity_repair": True,
                },
                candidate=2,
                model=model,
                route=layout,
                reason=call_reason + "+identity_repair",
                prompt=STOCK_SALES_DETAIL_CLOSING_REPAIR_PROMPT,
                layout_label="stock_sales_op_qty_val",
                default_title="STOCK AND SALES",
            )
            patched = 0
            if repair:
                patched = _merge_detail_closing_repair(result, repair)
                _log(
                    "STOCK_SALES_DETAIL_IDENTITY_REPAIR",
                    filename=filename,
                    detected_layout=layout,
                    schema=schema,
                    reason="merged",
                    gemini_called="true",
                    patched_rows=patched,
                )
            validation_r = (
                validate_stock_direct_vision(result, layout=layout)
                if result
                else {
                    "ok": False,
                    "soft_ok": False,
                    "reasons": ["empty_vision"],
                    "row_count": 0,
                    "identity_score": -1,
                }
            )
            if result and validation_r.get("ok"):
                extra_r = result.setdefault("totals", {}).setdefault("extra", {})
                extra_r["extraction_method"] = "stock_sales_op_qty_val_vision"
                extra_r["layout"] = "stock_sales_op_qty_val"
                extra_r["stock_sales_detail_identity_repair"] = True
                extra_r["stock_sales_detail_identity_repair_patched"] = patched
                # Replace the pre-repair candidate with the merged result.
                candidates = [
                    (
                        int(validation_r.get("identity_score") or 0),
                        result,
                        validation_r,
                    )
                ]
                if validation_r.get("soft_ok"):
                    result = _finalize_detail_op_qty_val_result(result, file_bytes)
                    lock_stock_vision_result(
                        result,
                        early_vision_reason="stock_sales_op_qty_val_vision",
                        stock_direct_vision_route=layout,
                        stock_direct_vision_candidate=2,
                        gemini_calls=2,
                    )
                    return result
        else:
            # Header pages: plain re-read unless Op was parked in P/Cl.
            # Continuation / Op-copy: mid-band Op→S zoom.
            use_movement_retry = bool(
                column_shift_soft
                and (
                    continuation
                    or "op_copied_into_receipts_majority" in soft_rs
                    or "op_copied_into_closing_majority" in soft_rs
                )
            )
            retry_bytes = _limit_detail_vision_image(
                _widen_detail_movement_columns(vision_primary)
                if use_movement_retry
                else vision_primary
            )
            retry_meta = {
                **original_meta,
                "stock_sales_detail_widen_cl": False,
                "stock_sales_detail_widen_movement": use_movement_retry,
                "stock_sales_detail_column_shift_retry": bool(column_shift_soft),
            }
            _log(
                "STOCK_SALES_DETAIL_COLUMN_SHIFT_RETRY",
                filename=filename,
                detected_layout=layout,
                schema=schema,
                reason=",".join(sorted(soft_rs)) or "retry",
                gemini_called="pending",
                widen_cl=False,
                widen_movement=use_movement_retry,
            )
            result2 = _call_psr_vision(
                file_bytes,
                filename,
                ext,
                vision_bytes=retry_bytes,
                image_meta=retry_meta,
                candidate=2,
                model=model,
                route=layout,
                reason=call_reason + "+column_shift_retry",
                prompt=STOCK_SALES_OP_QTY_VAL_VISION_PROMPT,
                layout_label="stock_sales_op_qty_val",
                default_title="STOCK AND SALES",
            )
            validation2 = (
                validate_stock_direct_vision(result2, layout=layout)
                if result2
                else {
                    "ok": False,
                    "soft_ok": False,
                    "reasons": ["empty_vision"],
                    "row_count": 0,
                    "identity_score": -1,
                }
            )
            if result2 and validation2.get("ok"):
                extra2 = result2.setdefault("totals", {}).setdefault("extra", {})
                extra2["extraction_method"] = "stock_sales_op_qty_val_vision"
                extra2["layout"] = "stock_sales_op_qty_val"
                extra2["stock_sales_detail_column_shift_retry"] = True
                candidates.append(
                    (int(validation2.get("identity_score") or 0), result2, validation2)
                )
                if validation2.get("soft_ok"):
                    result2 = _finalize_detail_op_qty_val_result(result2, file_bytes)
                    lock_stock_vision_result(
                        result2,
                        early_vision_reason="stock_sales_op_qty_val_vision",
                        stock_direct_vision_route=layout,
                        stock_direct_vision_candidate=2,
                        gemini_calls=2,
                    )
                    return result2

    if not candidates:
        _log(
            "STOCK_DIRECT_VISION_FALLBACK",
            filename=filename,
            detected_layout=layout,
            schema=schema,
            reason="no_structurally_valid_vision",
            gemini_called="true",
            row_count=0,
        )
        return None

    def _detail_candidate_rank(
        row: Tuple[int, Dict[str, Any], Dict[str, Any]]
    ) -> Tuple[int, int, int, int, int]:
        score, cand, val = row
        items = [i for i in (cand.get("line_items") or []) if isinstance(i, dict)]
        filled_sales = 0
        filled_closing = 0
        for item in items:
            try:
                if float(item.get("sales_qty") or 0) > 0:
                    filled_sales += 1
                elif float(item.get("sales_value") or 0) > 1:
                    filled_sales += 1
            except (TypeError, ValueError):
                pass
            try:
                if float(item.get("closing_value") or 0) > 1:
                    filled_closing += 1
                if float(item.get("closing_qty") or 0) > 0:
                    filled_closing += 1
            except (TypeError, ValueError):
                pass
        soft = 1 if val.get("soft_ok") else 0
        # Prefer soft_ok, then identity score, then closing/sales fill (blanked
        # retries lose), then denser tables.
        return (
            soft,
            int(score or 0),
            filled_closing,
            filled_sales,
            len(items),
        )

    candidates.sort(key=_detail_candidate_rank, reverse=True)
    best_score, best, best_val = candidates[0]
    best = _finalize_detail_op_qty_val_result(best, file_bytes)
    lock_stock_vision_result(
        best,
        early_vision_reason="stock_sales_op_qty_val_vision",
        stock_direct_vision_route=layout,
        stock_direct_vision_candidate=(
            ((best.get("totals") or {}).get("extra") or {}).get(
                "stock_direct_vision_candidate"
            )
            or 1
        ),
        gemini_calls=len(candidates),
    )
    _log(
        "STOCK_DIRECT_VISION_RESULT",
        filename=filename,
        detected_layout=layout,
        schema=schema,
        reason=reason,
        gemini_called="true",
        row_count=best_val.get("row_count"),
        validation_status="soft_fail_accepted",
        identity_score=best_score,
    )
    return best


STOCK_SALES_DETAIL_CLOSING_REPAIR_PROMPT = """
You are re-reading a Stock and Sales Detail Report photo.
Focus on Op/P/S/Sch/Cr and the RIGHTMOST Cl columns of each product row.

For every product row (skip Total), return the printed:
  product_name — FULL item text including pack
  opening_qty (Op.Qty) — first qty after Item; never Sl.No
  opening_value (Op.Val) — money
  receipts_qty (P.Qty) — purchase QTY; 0 when printed 0; never copy Op.Qty here
  receipts_value (P.Val) — money
  purchase_scheme_qty (P.Sch) — free/scheme only; NEVER put this in receipts_qty
  sales_qty (S.Qty) — never 0 when S.Val is non-zero
  sales_value (S.Val) — money; never drop a printed non-zero to 0
  sales_scheme_qty (S.Sch) — 0 when blank; never fold into sales_qty
  sales_return_qty (Cr.Qty / Rt.S.Qty) — 0 when blank
  closing_qty (Cl.Qty) — second-to-last stock column; never 0 when Cl.Val non-zero
  closing_value (Cl.Val) — RIGHTMOST money column after all Rt/Cr/Db cols

Do NOT invent numbers. Do NOT balance equations.
Do NOT copy S.Val / Db.Val / Cr.Val / P.Val into closing_value or closing_qty.
Do NOT treat Sl.No as opening_qty.
Do NOT put P.Sch into receipts_qty (e.g. P.Qty=95 P.Sch=5 → receipts_qty=95).
Do NOT copy the next row's Cl.Qty/Cl.Val into this row.
Do NOT copy S.Qty into closing_qty (e.g. S.Qty=25 Cl.Qty=54 → closing_qty=54).
Do NOT mark a row idle (P=0,S=0,Cl=Op) when P.Qty or S.Qty is printed non-zero
(e.g. SHATAVARI Op=22 P=60 S=25 Cl=57 — not Op=Cl=22).
When P=0, S=0 and Op.Val==Cl.Val, opening_qty must equal closing_qty.
If Cl.Val is printed, closing_value must not be null or 0 unless the cell is 0.
Also copy Total-row S.Val → totals.sales_value and Total-row Cl.Val →
totals.closing_value when visible (do not sum line items).

Return ONLY JSON:
{
  "line_items": [
    {
      "product_name": string,
      "opening_qty": number|null,
      "opening_value": number|null,
      "receipts_qty": number|null,
      "receipts_value": number|null,
      "purchase_scheme_qty": number|null,
      "sales_qty": number|null,
      "sales_value": number|null,
      "sales_scheme_qty": number|null,
      "sales_return_qty": number|null,
      "closing_qty": number|null,
      "closing_value": number|null
    }
  ],
  "totals": { "sales_value": number|null, "closing_value": number|null }
}
""".strip()


def _render_pdf_page_pngs(
    file_bytes: bytes, *, max_pages: int = 2, zoom: float = 3.5
) -> List[bytes]:
    """Higher-DPI page renders for dense Stock and Sales Detail phone scans."""
    try:
        import fitz
    except Exception:
        return []
    images: List[bytes] = []
    try:
        doc = fitz.open(stream=file_bytes, filetype="pdf")
    except Exception:
        return []
    try:
        limit = min(int(max_pages), int(getattr(doc, "page_count", 0) or 0))
        matrix = fitz.Matrix(float(zoom), float(zoom))
        for page_index in range(limit):
            try:
                pix = doc[page_index].get_pixmap(matrix=matrix, alpha=False)
                images.append(pix.tobytes("png"))
            except Exception:
                continue
    finally:
        try:
            doc.close()
        except Exception:
            pass
    return images


def _norm_detail_product_key(name: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(name or "").lower())


def _detail_row_identity_gap(item: Dict[str, Any]) -> float:
    """Abs gap for Op+P-S-Sch-Br+Cr-Db ≈ Cl. Never mutates item."""
    extra = item.get("extra") if isinstance(item.get("extra"), dict) else {}
    opening = float(item.get("opening_qty") or 0)
    receipts = float(item.get("receipts_qty") or 0)
    sales = float(item.get("sales_qty") or 0)
    closing = float(item.get("closing_qty") or 0)
    ss = float(
        extra.get("sales_scheme_qty")
        if extra.get("sales_scheme_qty") is not None
        else (item.get("sales_scheme_qty") or 0)
    )
    sale_ret = float(
        extra.get("sale_return")
        if extra.get("sale_return") is not None
        else (item.get("sales_return_qty") or 0)
    )
    pur_ret = float(
        extra.get("purchase_return")
        if extra.get("purchase_return") is not None
        else (item.get("purchase_return_qty") or 0)
    )
    br = float(extra.get("br_qty") or 0)
    if not br:
        br = float(
            extra.get("exp_damage")
            if extra.get("exp_damage") is not None
            else (item.get("expiry_damage_qty") or 0)
        )
    expected = opening + receipts - sales - br - ss + sale_ret - pur_ret
    return abs(expected - closing)


def _merge_detail_closing_repair(
    base: Dict[str, Any], repair: Dict[str, Any]
) -> int:
    """Overwrite printed Op/Sales/Closing fields from a Cl.Val-focused re-read.

    When several base rows share a truncated product key, match by encounter
    order within that key so one repair row cannot smash every variant.
    Per-row apply only when the candidate identity gap does not get worse
    (avoids a soft repair wiping a correct P.Qty / Cl.Val).
    """
    repair_items = [
        item
        for item in (repair.get("line_items") or [])
        if isinstance(item, dict) and _norm_detail_product_key(item.get("product_name"))
    ]
    if not repair_items:
        return 0
    by_key_list: Dict[str, List[Dict[str, Any]]] = {}
    for item in repair_items:
        key = _norm_detail_product_key(item.get("product_name"))
        by_key_list.setdefault(key, []).append(item)
    key_cursor: Dict[str, int] = {}
    patched = 0
    fields = (
        "opening_qty",
        "opening_value",
        "receipts_qty",
        "receipts_value",
        "sales_qty",
        "sales_value",
        "sales_return_qty",
        "closing_qty",
        "closing_value",
    )
    for item in base.get("line_items") or []:
        if not isinstance(item, dict):
            continue
        key = _norm_detail_product_key(item.get("product_name"))
        bucket = by_key_list.get(key) or []
        if not bucket:
            continue
        idx = key_cursor.get(key, 0)
        if idx >= len(bucket):
            continue
        src = bucket[idx]
        key_cursor[key] = idx + 1
        trial = dict(item)
        trial_extra = dict(item.get("extra") or {}) if isinstance(item.get("extra"), dict) else {}
        trial["extra"] = trial_extra
        changed = False
        for field in fields:
            if field not in src or src.get(field) is None:
                continue
            # Never clobber a non-zero printed cell with a repair zero.
            try:
                src_n = float(src.get(field))
                base_n = float(item.get(field) or 0)
            except (TypeError, ValueError):
                src_n = None
                base_n = None
            if src_n == 0 and base_n not in (None, 0):
                continue
            if trial.get(field) != src.get(field):
                trial[field] = src.get(field)
                changed = True
        if src.get("sales_return_qty") is not None:
            if trial_extra.get("sale_return") != src.get("sales_return_qty"):
                trial_extra["sale_return"] = src.get("sales_return_qty")
                trial["sales_return_qty"] = src.get("sales_return_qty")
                changed = True
        if src.get("sales_scheme_qty") is not None:
            if trial_extra.get("sales_scheme_qty") != src.get("sales_scheme_qty"):
                trial_extra["sales_scheme_qty"] = src.get("sales_scheme_qty")
                changed = True
        if src.get("purchase_scheme_qty") is not None:
            if trial_extra.get("purchase_scheme_qty") != src.get("purchase_scheme_qty"):
                trial_extra["purchase_scheme_qty"] = src.get("purchase_scheme_qty")
                changed = True
        if not changed:
            continue
        base_gap = _detail_row_identity_gap(item)
        trial_gap = _detail_row_identity_gap(trial)
        # Legacy closing-only fill (scanned PDF path): base Cl blank → always accept.
        legacy_closing_fill = (
            item.get("closing_qty") is None or item.get("closing_value") is None
        ) and (
            src.get("closing_qty") is not None or src.get("closing_value") is not None
        )
        # Keep repair only when identity improves or stays within 0.51.
        # Also allow equal-ish gap when filling missing Cr / S.Sch / S.Val / P.Qty.
        fills_missing = bool(
            (item.get("sales_return_qty") in (None, 0) and src.get("sales_return_qty"))
            or (
                (item.get("extra") or {}).get("sales_scheme_qty") in (None, 0)
                and src.get("sales_scheme_qty")
            )
            or (not item.get("sales_value") and src.get("sales_value"))
            or (
                float(item.get("receipts_qty") or 0) == 0
                and src.get("receipts_qty") not in (None, 0)
                and (
                    float(item.get("receipts_value") or 0) > 1
                    or float(src.get("receipts_value") or 0) > 1
                    or base_gap > 1.01
                )
            )
            or (
                float(item.get("sales_qty") or 0) == 0
                and (
                    float(item.get("sales_value") or 0) > 1
                    or float(src.get("sales_value") or 0) > 1
                    or base_gap > 1.01
                )
                and src.get("sales_qty") not in (None, 0)
            )
            or (
                float(item.get("closing_qty") or 0) == 0
                and float(item.get("closing_value") or 0) > 1
                and src.get("closing_qty") not in (None, 0)
            )
            or (
                # Cl.Qty duplicated from S.Qty while Cl.Val looks real.
                float(item.get("closing_qty") or 0) > 0
                and float(item.get("sales_qty") or 0) > 0
                and abs(
                    float(item.get("closing_qty") or 0)
                    - float(item.get("sales_qty") or 0)
                )
                < 0.01
                and float(item.get("closing_value") or 0) > 1
                and src.get("closing_qty") not in (None, 0)
                and abs(
                    float(src.get("closing_qty") or 0)
                    - float(item.get("sales_qty") or 0)
                )
                > 0.51
            )
            or (
                float(item.get("opening_qty") or 0) == 0
                and float(
                    item.get("opening_value")
                    or (item.get("extra") or {}).get("opening_value")
                    or 0
                )
                > 1
                and src.get("opening_qty") not in (None, 0)
            )
        )
        if not legacy_closing_fill:
            if trial_gap > base_gap + 0.51 and not fills_missing:
                continue
            if trial_gap > base_gap + 1.01:
                continue
        for field in fields:
            if field not in src or src.get(field) is None:
                continue
            try:
                src_n = float(src.get(field))
                base_n = float(item.get(field) or 0)
            except (TypeError, ValueError):
                src_n = None
                base_n = None
            if src_n == 0 and base_n not in (None, 0):
                continue
            if item.get(field) != src.get(field):
                item[field] = src.get(field)
        if src.get("sales_return_qty") is not None:
            extra = item.setdefault("extra", {})
            if isinstance(extra, dict):
                extra["sale_return"] = src.get("sales_return_qty")
            item["sales_return_qty"] = src.get("sales_return_qty")
        if src.get("sales_scheme_qty") is not None:
            extra = item.setdefault("extra", {})
            if isinstance(extra, dict):
                extra["sales_scheme_qty"] = src.get("sales_scheme_qty")
        extra = item.setdefault("extra", {})
        if isinstance(extra, dict):
            extra["detail_closing_repair"] = True
        patched += 1
    # Do not merge totals from the repair pass. Incomplete line S.Val/Cl.Val
    # sums can look "closer" than the printed Total-row figures and would
    # overwrite a correct first-pass Total. Totals stay on the primary prompt.
    return patched


def _has_opening_receive_issue_closing_qty_value_headers(text: str) -> bool:
    """Marg / HDC STOCK & SALES: OPENING STOCK|RECEIVE|ISSUE|CLOSING STOCK qty|value.

    Distinct from Op.Qty/P.Qty/S.Qty/Cls Detail grids. Scanned early Op.Qty
    Vision must not claim this family (sales land as 0, money bleeds into qty).

    Phone/scan OCR often drops STOCK after OPENING, glues RECEIVE bands, and
    reads CLOSING as COSINE/CLOSI — still reject Op.Qty when the STATEMENT
    chrome + PRODUCT DESCRIPTION + OPENING/RECEIVE band are present.
    """
    blob = text or ""
    if not blob.strip():
        return False
    # Never treat Stock and Sales Detail (Op.Qty/P.Sch) as ORIC qty|value.
    if _looks_like_stock_sales_detail_report_title(blob):
        return False
    if _has_stock_sales_op_qty_val_headers(blob) and not re.search(
        r"OPENING\s+STOCK|RECEIVE\s+(QUANTITY|QTY)|ISSUE\s+(QUANTITY|QTY)",
        blob,
        re.I,
    ):
        return False
    has_open = bool(
        re.search(
            r"OPENING\s+STOCK|OPENING\s+VALUE|\bOPENING\b",
            blob,
            re.I,
        )
    )
    has_recv = bool(
        re.search(
            r"RECEIVE\s+(QUANTITY|QTY|VALUE)|RECEIVE\s+QUANT|\bRECEIVE\b|\bRECEIPT\b",
            blob,
            re.I,
        )
    )
    has_issue = bool(
        re.search(r"ISSUE\s+(QUANTITY|QTY|VALUE)|\bISSUE\b|\bTSSUE\b", blob, re.I)
    )
    has_close = bool(
        re.search(
            r"CLOSING\s+STOCK|CLOSING\s+VALUE|\bCLOSING\b|"
            r"\bCOSINE\b|\bCLOSINE\b|\bCLOSI\b|\bCLOS\b",
            blob,
            re.I,
        )
    )
    if has_open and has_recv and has_issue and has_close:
        return True
    has_product = bool(re.search(r"PRODUCT\s+DESC", blob, re.I))
    has_title = bool(
        re.search(r"STOCK\s*&\s*SALES\s+STATEMENT|STOCK\s+AND\s+SALES\s+STATEMENT", blob, re.I)
    )
    # Noisy scan: title + PRODUCT DESCRIPTION + OPENING + RECEIVE is enough to
    # keep this family off the Op.Qty Detail early path.
    if has_title and has_product and has_open and has_recv:
        return True
    if has_product and has_open and has_recv and (has_issue or has_close):
        return True
    return False


def _ocr_png_header_probe(png_bytes: bytes) -> str:
    """OCR top band of a rendered PDF page (try upright rotations)."""
    if not png_bytes:
        return ""
    try:
        from PIL import Image
        from services.sales_statement_extractor import _a2z_tesseract
        from services.stock_ocr_policy import stock_ocr_context
    except Exception:
        return ""
    try:
        digest = hashlib.sha256(png_bytes).hexdigest()[:10]
        tess = _a2z_tesseract()
        chunks: List[str] = []
        with Image.open(io.BytesIO(png_bytes)) as raw:
            base = raw.convert("RGB")
        for rot in (0, 90, 270):
            img = base.rotate(rot, expand=True) if rot else base
            width, height = img.size
            if width < 80 or height < 80:
                continue
            header = img.crop((0, 0, width, max(40, int(height * 0.28))))
            probe = header.copy()
            probe.thumbnail((1400, 900))
            with stock_ocr_context(
                region=f"scanned_opval_header_{digest}_r{rot}", page="1"
            ):
                text = tess.image_to_string(probe, config="--psm 6") or ""
            if text.strip():
                chunks.append(text)
                if (
                    _has_opening_receive_issue_closing_qty_value_headers(text)
                    or _has_stock_sales_op_qty_val_headers(text)
                    or _has_pharmassist_stock_sale_headers(text)
                ):
                    return "\n".join(chunks)
        return "\n".join(chunks)
    except Exception:
        return ""


def _oric_scanned_pdf_early_enabled() -> bool:
    """STOCK_ORIC_SCANNED_PDF_EARLY — claim OPENING|RECEIVE|ISSUE scans before Op.Qty.

    Default ON. Kill-switch only; no new ops env required.
    """
    return os.getenv("STOCK_ORIC_SCANNED_PDF_EARLY", "true").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


def _score_oric_header_ocr(text: str) -> int:
    """Score uprightness of a header OCR band for ORIC qty|value scans."""
    blob = text or ""
    if not blob.strip():
        return 0
    score = 0
    if re.search(r"PRODUCT\s+DESC", blob, re.I):
        score += 4
    if re.search(r"OPENING", blob, re.I):
        score += 3
    if re.search(r"RECEIVE|RECEIPT", blob, re.I):
        score += 3
    if re.search(r"ISSUE|TSSUE", blob, re.I):
        score += 2
    if re.search(r"CLOSING|COSINE|CLOSINE|CLOSI", blob, re.I):
        score += 2
    if re.search(r"STOCK\s*&\s*SALES\s+STATEMENT|HIMALAYA", blob, re.I):
        score += 2
    if re.search(r"BONNISAN|BRESOL|CYSTONE|LIV|MENTAT|SEPTILIN", blob, re.I):
        score += 1
    return score


def _upright_oric_scanned_page_png(png_bytes: bytes) -> bytes:
    """Rotate a portrait PDF page so ORIC headers read left-to-right.

    Landscape STATEMENT sheets are often stored as portrait buffers. Vision on
    the sideways page shifts OPENING/RECEIVE/ISSUE/CLOSING across rows.
    """
    if not png_bytes:
        return png_bytes
    try:
        from PIL import Image
        from services.sales_statement_extractor import _a2z_tesseract
        from services.stock_ocr_policy import stock_ocr_context
    except Exception:
        return png_bytes
    try:
        with Image.open(io.BytesIO(png_bytes)) as raw:
            base = raw.convert("RGB")
    except Exception:
        return png_bytes
    width, height = base.size
    digest = hashlib.sha256(png_bytes).hexdigest()[:10]
    # Landscape phone photos of Marg STATEMENT pages are often sideways
    # (text vertical in-frame). Always score rotations — do not early-return
    # on width>=height or TOTAL/SKU OCR and Vision both slide columns.
    best_rot = 0
    best_score = -1
    best_img = None
    try:
        tess = _a2z_tesseract()
    except Exception:
        tess = None
    for rot in (0, 90, 270, 180):
        img = base.rotate(rot, expand=True) if rot else base
        score = 0
        if tess is not None:
            try:
                # Header band + a mid/footer band so continuation pages that
                # only print TOTAL near the top still score the upright rot.
                bands = [
                    img.crop(
                        (0, 0, img.size[0], max(40, int(img.size[1] * 0.35)))
                    ),
                    img.crop(
                        (
                            0,
                            max(0, int(img.size[1] * 0.45)),
                            img.size[0],
                            img.size[1],
                        )
                    ),
                ]
                text_parts: List[str] = []
                for bi, band in enumerate(bands):
                    probe = band.copy()
                    probe.thumbnail((1600, 1000))
                    with stock_ocr_context(
                        region=f"oric_upright_{digest}_r{rot}_b{bi}", page="1"
                    ):
                        text_parts.append(
                            tess.image_to_string(probe, config="--psm 6") or ""
                        )
                text = "\n".join(text_parts)
                score = _score_oric_header_ocr(text)
                if re.search(r"\bTOTALS?\b", text or "", re.I):
                    score += 4
                # Dense qty|value rows (continuation pages with no header chrome).
                if len(re.findall(r"\d+\.\d{2}", text or "")) >= 3:
                    score += 3
                if re.search(
                    r"\b(?:OPENING|RECEIVE|ISSUE|CLOSING|PRODUCT\s+DESC)\b",
                    text or "",
                    re.I,
                ):
                    score += 2
            except Exception:
                score = 0
        # Prefer landscape orientation when OCR is inconclusive.
        if img.size[0] >= img.size[1]:
            score += 1
        if score > best_score:
            best_score = score
            best_rot = rot
            best_img = img
    if best_img is None:
        best_img = base
        best_rot = 0
    try:
        buf = io.BytesIO()
        best_img.save(buf, format="PNG", optimize=True)
        out = buf.getvalue()
        logger.info(
            "STOCK_ORIC_SCANNED_UPRIGHT rot=%s score=%s size=%sx%s -> %sx%s",
            best_rot,
            best_score,
            width,
            height,
            best_img.size[0],
            best_img.size[1],
        )
        return out or png_bytes
    except Exception:
        return png_bytes


def _stitch_oric_header_onto_continuation(
    header_page_png: bytes, continuation_png: bytes
) -> bytes:
    """Paste page-1 column headers above a headerless continuation body.

    Marg STATEMENT page 2 often omits OPENING/RECEIVE/ISSUE/CLOSING chrome;
    Vision then returns no rows. Reusing the first-page header band keeps the
    same qty|value schema without inventing numbers.
    """
    if not continuation_png:
        return continuation_png
    if not header_page_png:
        return continuation_png
    try:
        from PIL import Image, ImageEnhance
    except Exception:
        return continuation_png
    try:
        with Image.open(io.BytesIO(header_page_png)) as raw_h:
            header_img = raw_h.convert("RGB")
        with Image.open(io.BytesIO(continuation_png)) as raw_c:
            body = raw_c.convert("RGB")
    except Exception:
        return continuation_png
    try:
        hdr = header_img.crop(
            (0, 0, header_img.width, max(40, int(header_img.height * 0.16)))
        )
        if body.width > 0 and hdr.width > 0:
            new_h = max(30, int(hdr.height * (body.width / float(hdr.width))))
            hdr = hdr.resize((body.width, new_h), Image.Resampling.LANCZOS)
        canvas = Image.new("RGB", (body.width, hdr.height + body.height), (255, 255, 255))
        canvas.paste(hdr, (0, 0))
        canvas.paste(body, (0, hdr.height))
        # Mild upscale helps sparse continuation scans.
        canvas = canvas.resize(
            (int(canvas.width * 1.2), int(canvas.height * 1.2)),
            Image.Resampling.LANCZOS,
        )
        canvas = ImageEnhance.Contrast(canvas).enhance(1.35)
        buf = io.BytesIO()
        canvas.save(buf, format="JPEG", quality=92)
        out = buf.getvalue()
        logger.info(
            "STOCK_ORIC_CONTINUATION_HEADER_STITCH body=%sx%s out=%sx%s",
            body.size[0],
            body.size[1],
            canvas.size[0],
            canvas.size[1],
        )
        return out or continuation_png
    except Exception as exc:
        logger.info(
            "STOCK_ORIC_CONTINUATION_HEADER_STITCH skipped err=%s",
            type(exc).__name__,
        )
        return continuation_png


def _crop_oric_ink_content_png(png_bytes: bytes) -> bytes:
    """Trim large white margins on upright ORIC continuation pages.

    Page-2 TOTAL / last SKUs often sit in a small ink island; full-page Vision
    then returns an empty/zero shell and footer OCR sees blank bands.
    """
    if not png_bytes:
        return png_bytes
    try:
        from PIL import Image
        import numpy as np
    except Exception:
        return png_bytes
    try:
        with Image.open(io.BytesIO(png_bytes)) as raw:
            img = raw.convert("RGB")
    except Exception:
        return png_bytes
    width, height = img.size
    if width < 200 or height < 200:
        return png_bytes
    try:
        arr = np.asarray(img)
        mask = (
            (arr[:, :, 0] < 245)
            | (arr[:, :, 1] < 245)
            | (arr[:, :, 2] < 245)
        )
        ys, xs = np.where(mask)
        if len(xs) < 500:
            return png_bytes
        pad = 12
        left = max(0, int(xs.min()) - pad)
        top = max(0, int(ys.min()) - pad)
        right = min(width, int(xs.max()) + pad + 1)
        bottom = min(height, int(ys.max()) + pad + 1)
        crop_w = right - left
        crop_h = bottom - top
        # Only crop when margins are material (continuation pages).
        if crop_w >= int(0.92 * width) and crop_h >= int(0.85 * height):
            return png_bytes
        if crop_w < 180 or crop_h < 120:
            return png_bytes
        cropped = img.crop((left, top, right, bottom))
        buf = io.BytesIO()
        cropped.save(buf, format="PNG", optimize=True)
        out = buf.getvalue()
        logger.info(
            "STOCK_ORIC_SCANNED_CONTENT_CROP from=%sx%s to=%sx%s",
            width,
            height,
            cropped.size[0],
            cropped.size[1],
        )
        return out or png_bytes
    except Exception as exc:
        logger.info(
            "STOCK_ORIC_SCANNED_CONTENT_CROP skipped err=%s", type(exc).__name__
        )
        return png_bytes


def extract_opening_receive_issue_closing_scanned_pdf(
    file_bytes: bytes,
    filename: str,
) -> Optional[Dict[str, Any]]:
    """Image-only PDF: OPENING|RECEIVE|ISSUE|CLOSING qty|value (main_stock ORIC).

    Runs before Op.Qty Detail early claim so sideways STATEMENT scans are not
    forced into stock_sales_op_qty_val (row shift + missing page-2 SKUs).
    Budget stays inside the main_stock photo reader (max 2 Gemini).
    """
    if not _oric_scanned_pdf_early_enabled():
        return None
    images = _render_pdf_page_pngs(file_bytes, max_pages=2, zoom=2.8)
    if not images:
        try:
            from services.gemini_extraction_fallback import _pdf_images

            images = _pdf_images(file_bytes, max_pages=2) or []
        except Exception:
            images = []
    if not images:
        return None

    header_peek = _ocr_png_header_probe(images[0])
    if not _has_opening_receive_issue_closing_qty_value_headers(header_peek):
        logger.info(
            "STOCK_ORIC_SCANNED_PDF file=%s status=skip reason=headers",
            filename,
        )
        return None
    if _has_stock_sales_op_qty_val_headers(header_peek) and not re.search(
        r"OPENING\s+STOCK|PRODUCT\s+DESC", header_peek or "", re.I
    ):
        logger.info(
            "STOCK_ORIC_SCANNED_PDF file=%s status=skip reason=op_qty_val_headers",
            filename,
        )
        return None

    try:
        from services.sales_statement_extractor import (
            empty_result,
            _extract_main_stock_sales_vision,
            _extract_opening_receive_issue_closing_photo,
            _main_stock_enrich_spreadsheet_meta,
            _main_stock_apply_printed_totals_footer,
            _main_stock_peel_oric_total_contamination,
            _main_stock_result_is_all_zero_shell,
        )
    except Exception as exc:
        logger.info(
            "STOCK_ORIC_SCANNED_PDF file=%s status=skip import=%s",
            filename,
            type(exc).__name__,
        )
        return None

    seed: Optional[Dict[str, Any]] = None
    merged_items: List[Dict[str, Any]] = []
    seen_names: set = set()
    last_upright_for_footer: Optional[bytes] = None
    first_page_upright: Optional[bytes] = None
    last_page_totals_blob: Optional[Dict[str, Any]] = None
    for page_index, image in enumerate(images[:2]):
        upright = _upright_oric_scanned_page_png(image)
        upright = _crop_oric_ink_content_png(upright)
        if page_index == 0:
            first_page_upright = upright
        last_upright_for_footer = upright
        vision_bytes = upright
        min_items = 5
        if page_index > 0 and first_page_upright:
            # Headerless continuation: stitch page-1 chrome + allow 1–3 SKUs.
            vision_bytes = _stitch_oric_header_onto_continuation(
                first_page_upright, upright
            )
            min_items = 1
        # Prefer Vision on upright pages: band OCR often keeps product names
        # but blanks OPENING/RECEIVE/ISSUE/CLOSING on dense scans.
        fallback = empty_result(filename, "png")
        _main_stock_enrich_spreadsheet_meta(fallback, header_peek or "")
        page = _extract_main_stock_sales_vision(
            vision_bytes,
            filename,
            ".png",
            fallback,
            sheets_screenshot=False,
            min_items=min_items,
        )
        if (
            not page
            or not page.get("line_items")
            or _main_stock_result_is_all_zero_shell(page)
        ):
            page = _extract_opening_receive_issue_closing_photo(
                upright, filename, ".png"
            )
        if (
            not page
            or not page.get("line_items")
            or _main_stock_result_is_all_zero_shell(page)
        ):
            logger.info(
                "STOCK_ORIC_SCANNED_PDF file=%s page=%s status=empty_or_zero_shell",
                filename,
                page_index + 1,
            )
            continue
        if seed is None:
            seed = page
        page_nonzero = 0
        for item in page.get("line_items") or []:
            if not isinstance(item, dict):
                continue
            for key in (
                "opening_qty",
                "receipts_qty",
                "sales_qty",
                "closing_qty",
                "sales_value",
                "closing_value",
            ):
                try:
                    if abs(float(item.get(key) or 0.0)) > 0.051:
                        page_nonzero += 1
                        break
                except (TypeError, ValueError):
                    continue
        # Continuation pages with only TOTAL / 1–3 SKUs are fine; skip a
        # page that merely repeats blank name shells from OCR cache.
        if page_index > 0 and page_nonzero == 0:
            logger.info(
                "STOCK_ORIC_SCANNED_PDF file=%s page=%s status=skip_blank_continuation",
                filename,
                page_index + 1,
            )
            continue
        for item in page.get("line_items") or []:
            if not isinstance(item, dict):
                continue
            # Pack size must stay in the key (100GM vs 50GM, 100ML vs 200ML).
            try:
                from services.sales_statement_extractor import (
                    _main_stock_item_identity_key,
                    _main_stock_peel_pack_into_field,
                )

                _main_stock_peel_pack_into_field(item)
                name_key = _main_stock_item_identity_key(item)
            except Exception:
                pack = str(item.get("packing") or "").strip().lower()
                name_key = re.sub(
                    r"[^a-z0-9]",
                    "",
                    f"{item.get('product_name') or ''}{pack}".lower(),
                )
            if name_key and name_key in seen_names:
                continue
            if name_key:
                seen_names.add(name_key)
            merged_items.append(item)

        # Continuation page owns the printed TOTAL footer — keep Vision peel.
        try:
            page_tot = (
                page.get("totals") if isinstance(page.get("totals"), dict) else {}
            )
            page_ex = (
                page_tot.get("extra") if isinstance(page_tot.get("extra"), dict) else {}
            )
            src = str(page_ex.get("total_row_source") or "")
            blob = {
                "opening_qty": page_tot.get("opening_qty", page_ex.get("opening_qty")),
                "opening_value": page_ex.get("opening_value"),
                "receipts_qty": page_tot.get(
                    "receipts_qty", page_ex.get("receipts_qty")
                ),
                "receipts_value": page_ex.get("receipts_value"),
                "sales_qty": page_tot.get("sales_qty", page_ex.get("sales_qty")),
                "sales_value": page_tot.get("sales_value"),
                "closing_qty": page_tot.get("closing_qty", page_ex.get("closing_qty")),
                "closing_value": page_tot.get("closing_value"),
                "last_month_sale": page_ex.get("last_month_sale", page_ex.get("lms")),
            }
            need = (
                "opening_qty",
                "opening_value",
                "receipts_qty",
                "receipts_value",
                "sales_qty",
                "sales_value",
                "closing_qty",
                "closing_value",
            )
            if all(blob.get(k) not in (None, "") for k in need):
                try:
                    cq = float(blob.get("closing_qty") or 0)
                    cv = float(blob.get("closing_value") or 0)
                except (TypeError, ValueError):
                    cq, cv = 0.0, 0.0
                if cq >= 10 and cv >= 1000 and (
                    src.startswith("main_stock") or page_index > 0 or cq >= 100
                ):
                    last_page_totals_blob = dict(blob)
        except Exception:
            pass

        logger.info(
            "STOCK_ORIC_SCANNED_PDF file=%s page=%s rows=%s nonzero=%s",
            filename,
            page_index + 1,
            len(page.get("line_items") or []),
            page_nonzero,
        )

    if seed is None or len(merged_items) < 5:
        logger.info(
            "STOCK_ORIC_SCANNED_PDF file=%s status=skip rows=%s",
            filename,
            len(merged_items),
        )
        return None

    seed["line_items"] = merged_items
    # Keep the upload extension (pdf), not the per-page render (.png).
    seed["source_file"] = filename
    seed["source_format"] = (
        Path(filename).suffix.lstrip(".").lower() if filename else "pdf"
    ) or "pdf"
    # Vision/OCR sometimes parks chrome junk in stockist_name; re-seed from peek.
    stockist_now = str(seed.get("stockist_name") or "")
    if (
        not re.search(r"[A-Za-z]{4,}", stockist_now)
        or re.search(r"[|�]|seen\s+i", stockist_now, re.I)
        or len(stockist_now) < 4
    ):
        seed["stockist_name"] = None
    _main_stock_enrich_spreadsheet_meta(seed, header_peek or "")
    try:
        from services.sales_statement_extractor import (
            _enrich_stock_statement_header_from_ocr,
            _main_stock_clean_letterhead_address,
            _main_stock_normalize_product_name,
            _main_stock_peel_pack_into_field,
            _main_stock_repair_clipped_himalaya_names,
            _main_stock_repair_closing_value_hundreds_digit,
        )

        _enrich_stock_statement_header_from_ocr(seed, header_peek or "")
        _main_stock_repair_clipped_himalaya_names(seed, header_peek or "")
        for item in seed.get("line_items") or []:
            if isinstance(item, dict) and item.get("product_name"):
                item["product_name"] = _main_stock_normalize_product_name(
                    str(item.get("product_name") or "")
                )
                _main_stock_peel_pack_into_field(item)
        _main_stock_repair_closing_value_hundreds_digit(seed)
    except Exception:
        pass
    if not seed.get("company_name") and re.search(
        r"HIMALAYA\s+ZANDRA", header_peek or "", re.I
    ):
        seed["company_name"] = "HIMALAYA ZANDRA"
    if not seed.get("stockist_name"):
        m_party = re.search(
            r"M/?S\.?\s*([A-Z][A-Z0-9 .&'/-]{3,80}?DISTRIBUTORS?)",
            header_peek or "",
            re.I,
        )
        if m_party:
            seed["stockist_name"] = re.sub(r"\s+", " ", m_party.group(1)).strip(" ,.")
    if not seed.get("stockist_address"):
        m_addr = re.search(
            r"(?im)^(?:M/?S\.?[^\n]*\n)?([^\n]{12,160}?(?:ROAD|STREET|COMPLEX|MARKET|FLOOR|NAGAR|COLONY|PIN|-\d{6})[^\n]*)",
            header_peek or "",
        )
        if m_addr:
            addr = re.sub(r"\s+", " ", m_addr.group(1)).strip(" ,.")
            if not re.search(
                r"HIMALAYA|STOCK\s*&\s*SALES|PRODUCT\s+DESC|Licence|Fax\s*:",
                addr,
                re.I,
            ):
                seed["stockist_address"] = addr
    try:
        from services.sales_statement_extractor import (
            _main_stock_clean_letterhead_address,
        )

        if seed.get("stockist_address"):
            seed["stockist_address"] = _main_stock_clean_letterhead_address(
                str(seed.get("stockist_address") or "")
            )
    except Exception:
        pass
    # Peel TOTAL-scale blocks Vision glued onto the last SKU before footer OCR.
    try:
        from services.sales_statement_extractor import (
            _main_stock_peel_oric_total_contamination,
        )

        peeled = _main_stock_peel_oric_total_contamination(seed)
        if peeled:
            logger.info(
                "STOCK_ORIC_SCANNED_PDF file=%s total_contamination_peeled=%s",
                filename,
                peeled,
            )
    except Exception:
        pass
    # Prefer last-page Vision TOTAL blob (continuation owns the footer row).
    if last_page_totals_blob:
        try:
            from services.sales_statement_extractor import (
                _main_stock_apply_vision_totals_blob,
            )

            if _main_stock_apply_vision_totals_blob(
                seed,
                last_page_totals_blob,
                source="main_stock_vision_totals",
            ):
                logger.info(
                    "STOCK_ORIC_SCANNED_PDF file=%s status=applied_last_page_vision_totals "
                    "closing_value=%s sales_value=%s",
                    filename,
                    last_page_totals_blob.get("closing_value"),
                    last_page_totals_blob.get("sales_value"),
                )
        except Exception as exc:
            logger.info(
                "STOCK_ORIC_SCANNED_PDF file=%s vision_totals_apply_skip=%s",
                filename,
                type(exc).__name__,
            )
    # Footer TOTAL lives on the last page — upright + ink-crop for peel.
    try:
        footer_img = last_upright_for_footer or _crop_oric_ink_content_png(
            _upright_oric_scanned_page_png(images[-1])
        )
        _main_stock_apply_printed_totals_footer(seed, footer_img)
    except Exception:
        pass

    extra = seed.setdefault("totals", {}).setdefault("extra", {})
    if isinstance(extra, dict):
        extra["extraction_method"] = "opening_receive_issue_closing_scanned_pdf"
        extra["layout"] = "opening_receive_issue_closing"
        extra["early_vision_reason"] = "oric_scanned_pdf_before_op_qty_val"
        extra["statement_count"] = 1
    lock_stock_vision_result(
        seed,
        early_vision_reason="oric_scanned_pdf_before_op_qty_val",
        stock_direct_vision_route="opening_receive_issue_closing",
        extraction_method="opening_receive_issue_closing_scanned_pdf",
        layout="opening_receive_issue_closing",
        stock_identity_kind="opening_receipts_sales_closing",
        numeric_source="gemini_vision",
    )
    logger.info(
        "STOCK_ORIC_SCANNED_PDF file=%s status=hit rows=%s company=%s",
        filename,
        len(merged_items),
        seed.get("company_name"),
    )
    return seed


def _stock_sales_detail_scan_per_page_enabled() -> bool:
    """Prefer per-page Vision over vertical stitch (env, default ON)."""
    raw = os.getenv("STOCK_SALES_DETAIL_SCAN_PER_PAGE", "1").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def _stock_sales_detail_widen_cl_enabled() -> bool:
    """Append a zoomed Cl.Qty/Cl.Val strip so Vision does not steal Cr/Db (default ON)."""
    raw = os.getenv("STOCK_SALES_DETAIL_WIDEN_CL", "1").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def _stock_sales_detail_identity_repair_enabled() -> bool:
    """Second-call Op/P/S/Cr/Cl repair+merge on soft identity fail (default ON)."""
    raw = os.getenv("STOCK_SALES_DETAIL_IDENTITY_REPAIR", "true").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def _widen_detail_closing_columns(page_png: bytes) -> bytes:
    """Paste an enlarged rightmost Cl.Qty|Cl.Val band beside the page image.

    Dense Stock and Sales Detail scans pack Cr/Db next to Cl; models often copy
    Op.Val/Cr.Val into closing_value. A labeled zoom of only the closing pair
    keeps Cl.Qty/Cl.Val aligned to the printed rightmost cells.
    """
    if not page_png:
        return page_png
    try:
        from PIL import Image, ImageDraw
    except Exception:
        return page_png
    try:
        img = Image.open(io.BytesIO(page_png)).convert("RGB")
    except Exception:
        return page_png
    try:
        width, height = img.size
        if width < 200 or height < 200:
            return page_png
        # 0.70 includes full Cl.Val digits (narrower crops clip thousands).
        cl_left = int(width * 0.70)
        cl = img.crop((cl_left, 0, width, height))
        zoom_w = max(cl.width + 1, int(cl.width * 1.55))
        cl_zoom = cl.resize((zoom_w, height), Image.Resampling.LANCZOS)
        gap = 8
        canvas = Image.new(
            "RGB", (width + gap + cl_zoom.width, height), (255, 255, 255)
        )
        canvas.paste(img, (0, 0))
        canvas.paste(cl_zoom, (width + gap, 0))
        draw = ImageDraw.Draw(canvas)
        draw.rectangle(
            [width + gap, 0, canvas.width - 1, height - 1],
            outline=(200, 0, 0),
            width=3,
        )
        draw.rectangle(
            [cl_left, int(height * 0.08), width - 2, height - 2],
            outline=(200, 0, 0),
            width=3,
        )
        draw.text(
            (width + gap + 4, 6),
            "ZOOM Cl.Qty | Cl.Val (rightmost pair ONLY)",
            fill=(200, 0, 0),
        )
        buf = io.BytesIO()
        canvas.save(buf, format="PNG", optimize=True)
        return buf.getvalue()
    except Exception:
        return page_png
    finally:
        try:
            img.close()
        except Exception:
            pass


def _limit_detail_vision_image(
    page_png: bytes, *, max_side: int = 2200, max_bytes: int = 3_500_000
) -> bytes:
    """Downscale/recompress Detail Vision inputs so flash does not empty-out.

    Phone JPEGs + movement/Cl zoom panels often exceed ~6–9MB; Vertex then
    returns empty JSON. Keep upright content — only size, never crop digits.
    """
    if not page_png:
        return page_png
    try:
        from PIL import Image
    except Exception:
        return page_png
    try:
        img = Image.open(io.BytesIO(page_png)).convert("RGB")
    except Exception:
        return page_png
    try:
        w, h = img.size
        longest = max(w, h)
        if longest > max_side:
            scale = float(max_side) / float(longest)
            img = img.resize(
                (max(1, int(w * scale)), max(1, int(h * scale))),
                Image.Resampling.LANCZOS,
            )
        quality = 90
        out = page_png
        for _ in range(4):
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=quality, optimize=True)
            out = buf.getvalue()
            if len(out) <= max_bytes:
                return out
            quality = max(60, quality - 10)
        return out
    except Exception:
        return page_png
    finally:
        try:
            img.close()
        except Exception:
            pass


def _widen_detail_movement_columns(page_png: bytes) -> bytes:
    """Paste an enlarged Op.Qty→S.Val mid-band for column-shift retries.

    Continuation Detail pages without headers often drop P.Qty or shift Op→P→S
    left by one. A labeled zoom of the movement pair (not Cl alone) keeps
    receipts_qty/sales_qty aligned to printed cells. Never invents digits.
    """
    if not page_png:
        return page_png
    try:
        from PIL import Image, ImageDraw
    except Exception:
        return page_png
    try:
        img = Image.open(io.BytesIO(page_png)).convert("RGB")
    except Exception:
        return page_png
    try:
        width, height = img.size
        if width < 200 or height < 200:
            return page_png
        # Include Item text so zoom rows stay aligned; through S.Val before Cl.
        # Do NOT also paste a Cl strip here — dual panels cascade the last rows.
        left = int(width * 0.08)
        right = int(width * 0.74)
        if right - left < 80:
            return page_png
        mid = img.crop((left, 0, right, height))
        zoom_w = max(mid.width + 1, int(mid.width * 1.35))
        mid_zoom = mid.resize((zoom_w, height), Image.Resampling.LANCZOS)
        gap = 8
        canvas = Image.new(
            "RGB", (width + gap + mid_zoom.width, height), (255, 255, 255)
        )
        canvas.paste(img, (0, 0))
        canvas.paste(mid_zoom, (width + gap, 0))
        draw = ImageDraw.Draw(canvas)
        draw.rectangle(
            [width + gap, 0, canvas.width - 1, height - 1],
            outline=(0, 90, 180),
            width=3,
        )
        draw.rectangle(
            [left, int(height * 0.08), right - 2, height - 2],
            outline=(0, 90, 180),
            width=3,
        )
        draw.text(
            (width + gap + 4, 6),
            "ZOOM Item|Op.Qty|Op.Val|P.Qty|P.Sch|P.Val|S.Qty|S.Val",
            fill=(0, 90, 180),
        )
        buf = io.BytesIO()
        canvas.save(buf, format="PNG", optimize=True)
        return buf.getvalue()
    except Exception:
        return page_png
    finally:
        try:
            img.close()
        except Exception:
            pass


def _call_op_qty_val_multi_image(
    images: List[bytes],
    filename: str,
    *,
    prompt: str,
    reason: str,
    candidate: int = 1,
) -> Optional[Dict[str, Any]]:
    """One Gemini call with one or more page PNGs (Stock and Sales Detail)."""
    if not images:
        return None
    from services.sales_extraction_runtime import (
        sales_generate_content_via_vertex as generate_content_via_vertex,
    )
    from services.sales_statement_extractor import (
        _apply_parsed_sales_json,
        _extract_json_object,
        _gemini_response_text,
        empty_result,
    )

    model = os.getenv("VISION_MODEL", "gemini-2.5-flash-lite").strip()
    parts: List[Dict[str, Any]] = [{"text": prompt}]
    for image in images:
        parts.append(
            {
                "inline_data": {
                    "mime_type": "image/png",
                    "data": base64.b64encode(image).decode("ascii"),
                }
            }
        )
    payload = {
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {"temperature": 0.0, "maxOutputTokens": 8192},
    }
    started = time.time()
    _log(
        "STOCK_DIRECT_VISION_START",
        filename=filename,
        mime="image/png",
        byte_size=sum(len(img) for img in images),
        route="stock_sales_op_qty_val",
        reason=reason,
        gemini_model=model,
        candidate=candidate,
        pages=len(images),
        gemini_input="image_original",
        numeric_source="gemini_vision",
    )
    try:
        response = generate_content_via_vertex(
            model=model,
            payload=payload,
            timeout=120,
            label="stock_direct_vision",
        )
        parsed = _extract_json_object(_gemini_response_text(response))
    except Exception as exc:
        _log(
            "STOCK_DIRECT_VISION_END",
            filename=filename,
            route="stock_sales_op_qty_val",
            reason=reason,
            status="error",
            error_type=type(exc).__name__,
            elapsed_ms=int((time.time() - started) * 1000),
        )
        return None
    if not parsed or not parsed.get("line_items"):
        _log(
            "STOCK_DIRECT_VISION_END",
            filename=filename,
            route="stock_sales_op_qty_val",
            reason=reason,
            status="empty",
            elapsed_ms=int((time.time() - started) * 1000),
        )
        return None
    result = empty_result(filename, "pdf")
    result = _apply_parsed_sales_json(result, parsed)
    kept = [
        item
        for item in (result.get("line_items") or [])
        if isinstance(item, dict)
        and _stock_vision_product_name_ok(str(item.get("product_name") or ""))
    ]
    if len(kept) < 3:
        return None
    result["line_items"] = kept
    extra = result.setdefault("totals", {}).setdefault("extra", {})
    extra["extraction_method"] = "stock_sales_op_qty_val_vision"
    extra["layout"] = "stock_sales_op_qty_val"
    _log(
        "STOCK_DIRECT_VISION_END",
        filename=filename,
        route="stock_sales_op_qty_val",
        reason=reason,
        status="ok",
        row_count=len(kept),
        elapsed_ms=int((time.time() - started) * 1000),
    )
    return result


def _stitch_png_pages_vertical(images: List[bytes]) -> Optional[bytes]:
    """Stack page PNGs into one tall image for a single Vision call."""
    if not images:
        return None
    if len(images) == 1:
        return images[0]
    try:
        from PIL import Image
    except Exception:
        return images[0]
    opened: List[Any] = []
    try:
        for raw in images:
            opened.append(Image.open(io.BytesIO(raw)).convert("RGB"))
        width = max(img.width for img in opened)
        height = sum(img.height for img in opened)
        canvas = Image.new("RGB", (width, height), (255, 255, 255))
        y = 0
        for img in opened:
            canvas.paste(img, (0, y))
            y += img.height
        # Cap width so the Vertex payload stays manageable.
        if canvas.width > 2200:
            ratio = 2200 / float(canvas.width)
            canvas = canvas.resize(
                (2200, max(1, int(canvas.height * ratio))),
                Image.Resampling.LANCZOS,
            )
        buf = io.BytesIO()
        canvas.save(buf, format="PNG", optimize=True)
        return buf.getvalue()
    except Exception:
        return images[0]
    finally:
        for img in opened:
            try:
                img.close()
            except Exception:
                pass


def _pack_digit_from_item(item: Dict[str, Any]) -> Optional[float]:
    """Best-effort pack size digit from packing text or product name."""
    pack = str(item.get("packing") or "").strip()
    name = str(item.get("product_name") or "").strip()
    for blob in (pack, name):
        if not blob:
            continue
        m = re.search(
            r"(\d+)\s*(?:'?S\b|TAB|CAPS?|ML|GM|G\b|TABS?)",
            blob,
            re.I,
        )
        if m:
            try:
                return float(m.group(1))
            except (TypeError, ValueError):
                pass
        m2 = re.fullmatch(r"(\d+)(?:\.0+)?", blob)
        if m2:
            try:
                return float(m2.group(1))
            except (TypeError, ValueError):
                pass
    return None


_COMMON_PACK_DIGITS = frozenset(
    {10, 15, 20, 24, 28, 30, 40, 50, 60, 75, 90, 100, 120, 150, 200}
)


def _repair_pack_digit_parked_as_opening(item: Dict[str, Any]) -> bool:
    """Re-slot Pack digit that leaked into opening_qty (column reassignment).

    Never invents a qty from Op+Pur-Sale arithmetic. Only:
      A) Pack→Op and Op→Pur: receipts holds real opening and (rec-sale)≈closing
      B) Zero-movement row: Pack digit in opening while Pur/Sales/Cl are 0
    """
    if not isinstance(item, dict):
        return False
    pack_digit = _pack_digit_from_item(item)
    try:
        op = float(item.get("opening_qty") or 0)
        rec = float(item.get("receipts_qty") or 0)
        sale = float(item.get("sales_qty") or 0)
        cl = float(item.get("closing_qty") or 0)
    except (TypeError, ValueError):
        return False
    # Already balances — Pack coincidentally equals a real opening.
    if abs((op + rec - sale) - cl) <= 1.01:
        return False
    pack_match = pack_digit is not None and abs(op - pack_digit) <= 0.01
    op_looks_like_pack = abs(op - round(op)) < 1e-6 and int(round(op)) in _COMMON_PACK_DIGITS
    extra = item.get("extra") if isinstance(item.get("extra"), dict) else {}
    extra = dict(extra)
    # Case A0: Pack digit duplicated into Pur while opening already holds the
    # real Op (= Pack size coincidentally). Clear ghost receipts only when
    # Op−Sale≈Cl. Do NOT claim pack→opening→purchase shift (opening unchanged).
    if (
        rec > 0
        and pack_digit is not None
        and abs(op - pack_digit) <= 0.01
        and abs(rec - pack_digit) <= 0.01
        and abs((op - sale) - cl) <= 1.01
    ):
        extra["pack_as_opening_pre"] = {
            "opening_qty": op,
            "receipts_qty": rec,
            "sales_qty": sale,
            "closing_qty": cl,
        }
        item["receipts_qty"] = 0.0
        extra["pack_receipts_ghost_cleared"] = True
        item["extra"] = extra
        logger.info(
            "STOCK_OP_STOCK_PACK_RECEIPTS_GHOST_CLEAR product=%s "
            "pack=%s opening=%s receipts=%s->0",
            str(item.get("product_name") or "")[:60],
            pack_digit,
            op,
            rec,
        )
        return False
    # Case A: Pack parked in opening; real Op sits in receipts (values differ).
    # Accept when Pack text matches OR opening is a common pack size and the
    # shifted identity (rec-sale≈cl) holds while the printed assignment does not.
    if (
        rec > 0
        and abs(op - rec) > 0.01
        and abs((rec - sale) - cl) <= 1.01
        and (pack_match or op_looks_like_pack)
    ):
        extra["pack_as_opening_pre"] = {
            "opening_qty": op,
            "receipts_qty": rec,
            "sales_qty": sale,
            "closing_qty": cl,
        }
        item["opening_qty"] = float(rec)
        item["receipts_qty"] = 0.0
        if not str(item.get("packing") or "").strip():
            digit = pack_digit if pack_digit is not None else op
            item["packing"] = (
                str(int(digit))
                if abs(digit - int(digit)) < 1e-6
                else str(digit)
            )
        extra["pack_opening_shift_repaired"] = "pack_to_op_op_to_pur"
        item["extra"] = extra
        logger.info(
            "STOCK_OP_STOCK_PACK_OPENING_REPAIR product=%s mode=shift "
            "pack=%s opening=%s->%s receipts=%s->0",
            str(item.get("product_name") or "")[:60],
            pack_digit if pack_digit is not None else op,
            op,
            item["opening_qty"],
            rec,
        )
        return True
    # Case B: idle row — Pack digit is not stock.
    if rec == 0 and sale == 0 and cl == 0 and (pack_match or op_looks_like_pack):
        extra["pack_as_opening_pre"] = {"opening_qty": op}
        item["opening_qty"] = 0.0
        if not str(item.get("packing") or "").strip():
            digit = pack_digit if pack_digit is not None else op
            item["packing"] = (
                str(int(digit))
                if abs(digit - int(digit)) < 1e-6
                else str(digit)
            )
        extra["pack_opening_shift_repaired"] = "idle_pack_cleared"
        item["extra"] = extra
        logger.info(
            "STOCK_OP_STOCK_PACK_OPENING_REPAIR product=%s mode=idle "
            "pack=%s opening=%s->0",
            str(item.get("product_name") or "")[:60],
            pack_digit if pack_digit is not None else op,
            op,
        )
        return True
    # Case C: Pack in opening but real Op unread — candidates only.
    if pack_match or op_looks_like_pack:
        extra["pack_as_opening_suspected"] = True
        cands = list(extra.get("candidates") or [])
        cands.append(
            {
                "opening_qty": None,
                "note": "pack_digit_in_opening_unresolved",
                "pack_digit": pack_digit if pack_digit is not None else op,
            }
        )
        extra["candidates"] = cands
        item["extra"] = extra
    return False


def _op_stock_row_qty_tuple(
    item: Dict[str, Any],
) -> Tuple[float, float, float, float]:
    try:
        op = float(item.get("opening_qty") or 0)
    except (TypeError, ValueError):
        op = 0.0
    try:
        rec = float(item.get("receipts_qty") or 0)
    except (TypeError, ValueError):
        rec = 0.0
    try:
        sale = float(item.get("sales_qty") or 0)
    except (TypeError, ValueError):
        sale = 0.0
    try:
        cl = float(item.get("closing_qty") or 0)
    except (TypeError, ValueError):
        cl = 0.0
    return op, rec, sale, cl


def _op_stock_missing_sale_row(item: Dict[str, Any]) -> bool:
    """True when Sales Qty was dropped (Op/Cl printed, sales forced to 0)."""
    if not isinstance(item, dict):
        return False
    op, rec, sale, cl = _op_stock_row_qty_tuple(item)
    if sale != 0 or op <= 0 or cl < 0:
        return False
    # Gap that a positive Sales Qty would close (not Bonnisan-style print gaps
    # where Op+Pur-Sale still would not equal Cl even with a guessed sale).
    gap = (op + rec) - cl
    if gap <= 0.01 or gap > 500:
        return False
    # Candidate sale = gap; only flag when that integer sale would balance.
    if abs(gap - round(gap)) > 0.01:
        return False
    return abs((op + rec - gap) - cl) <= 1.01


def _op_stock_missing_sale_count(items: List[Any]) -> int:
    return sum(
        1
        for item in items
        if isinstance(item, dict) and _op_stock_missing_sale_row(item)
    )


def _norm_op_stock_product_key(name: Any) -> str:
    text = re.sub(r"[^A-Z0-9]+", "", str(name or "").upper())
    return text


def _merge_op_stock_sales_qty_repair(
    base: Dict[str, Any], repair: Dict[str, Any]
) -> int:
    """Copy non-zero Sales Qty from repair when base dropped it and identity improves."""
    repair_items = [
        item
        for item in (repair.get("line_items") or [])
        if isinstance(item, dict) and _norm_op_stock_product_key(item.get("product_name"))
    ]
    if not repair_items:
        return 0
    by_key: Dict[str, List[Dict[str, Any]]] = {}
    for item in repair_items:
        key = _norm_op_stock_product_key(item.get("product_name"))
        by_key.setdefault(key, []).append(item)
    cursor: Dict[str, int] = {}
    patched = 0
    for item in base.get("line_items") or []:
        if not isinstance(item, dict) or not _op_stock_missing_sale_row(item):
            continue
        key = _norm_op_stock_product_key(item.get("product_name"))
        bucket = by_key.get(key) or []
        idx = cursor.get(key, 0)
        if idx >= len(bucket):
            continue
        src = bucket[idx]
        cursor[key] = idx + 1
        try:
            new_sale = float(src.get("sales_qty") or 0)
        except (TypeError, ValueError):
            continue
        if new_sale <= 0:
            continue
        op, rec, old_sale, cl = _op_stock_row_qty_tuple(item)
        old_gap = abs((op + rec - old_sale) - cl)
        new_gap = abs((op + rec - new_sale) - cl)
        if new_gap > old_gap + 0.01:
            continue
        extra = item.get("extra") if isinstance(item.get("extra"), dict) else {}
        extra = dict(extra)
        extra["op_stock_sales_qty_pre_repair"] = old_sale
        extra["op_stock_sales_qty_repaired"] = True
        item["sales_qty"] = float(new_sale)
        item["extra"] = extra
        patched += 1
        logger.info(
            "STOCK_OP_STOCK_SALES_QTY_REPAIR product=%s sales=%s->%s",
            str(item.get("product_name") or "")[:60],
            old_sale,
            new_sale,
        )
    return patched


def _widen_op_stock_sales_qty_columns(page_png: bytes) -> bytes:
    """Paste an enlarged Product|Sales Qty|Cl.Stock band for Sales Qty repair.

    Dense Pack|Op.Stock|Pur|Free|Sales|Cl grids often drop single-digit Sales
    Qty. A labeled zoom of the Sales/Cl band keeps the printed digit readable
    without inventing values from Op−Cl arithmetic.
    """
    if not page_png:
        return page_png
    try:
        from PIL import Image, ImageDraw
    except Exception:
        return page_png
    try:
        img = Image.open(io.BytesIO(page_png)).convert("RGB")
    except Exception:
        return page_png
    try:
        width, height = img.size
        if width < 200 or height < 200:
            return page_png
        # Product name through Stock Value — Sales Qty sits mid-right.
        left = int(width * 0.18)
        right = int(width * 0.92)
        if right - left < 80:
            return page_png
        band = img.crop((left, 0, right, height))
        zoom_w = max(band.width + 1, int(band.width * 1.45))
        band_zoom = band.resize((zoom_w, height), Image.Resampling.LANCZOS)
        gap = 8
        canvas = Image.new(
            "RGB", (width + gap + band_zoom.width, height), (255, 255, 255)
        )
        canvas.paste(img, (0, 0))
        canvas.paste(band_zoom, (width + gap, 0))
        draw = ImageDraw.Draw(canvas)
        draw.rectangle(
            [width + gap, 0, canvas.width - 1, height - 1],
            outline=(0, 120, 0),
            width=3,
        )
        draw.rectangle(
            [left, int(height * 0.08), right - 2, height - 2],
            outline=(0, 120, 0),
            width=3,
        )
        draw.text(
            (width + gap + 4, 6),
            "ZOOM Product|Pack|Op.Stock|Pur|Free|Sales Qty|Cl.Stock|Stock Value",
            fill=(0, 120, 0),
        )
        buf = io.BytesIO()
        canvas.save(buf, format="PNG", optimize=True)
        return buf.getvalue()
    except Exception:
        return page_png
    finally:
        try:
            img.close()
        except Exception:
            pass


def _finalize_op_stock_pur_sales_cl(result: Dict[str, Any]) -> Dict[str, Any]:
    """Pack→opening repair + footer hygiene for Op.Stock|Pur|Sales|Cl.Stock."""
    totals = result.setdefault("totals", {})
    extra_t = totals.setdefault("extra", {})
    repaired = 0
    kept: List[Dict[str, Any]] = []
    for item in result.get("line_items") or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("product_name") or "").strip()
        if not name:
            continue
        if re.search(
            r"^\s*(?:Goods\s*Value|Amount|OpStockValue|User\s*Name|"
            r"Print\s*Datetime|Non\s*Moving|Short\s*Expiry|Grand\s*Total|"
            r"GRAND\s*TOTAL|^Total\b)\b",
            name,
            re.I,
        ):
            logger.info(
                "STOCK_OP_STOCK_DROP_FOOTER_ROW name=%s",
                name[:80],
            )
            continue
        if _repair_pack_digit_parked_as_opening(item):
            repaired += 1
        # Qty-only grid: do not keep invented per-row sales_value zeros.
        if item.get("sales_value") in (0, 0.0):
            item["sales_value"] = None
        row_extra = item.get("extra") if isinstance(item.get("extra"), dict) else {}
        row_extra = dict(row_extra)
        row_extra["layout"] = "op_stock_pur_sales_cl"
        row_extra["psr_column_layout"] = "op_stock_pur_sales_cl"
        item["extra"] = row_extra
        kept.append(item)
    result["line_items"] = kept
    if repaired:
        extra_t["op_stock_pack_opening_repaired"] = repaired
        logger.info(
            "STOCK_OP_STOCK_PACK_OPENING_REPAIR file_rows_repaired=%s",
            repaired,
        )
    # Printed Goods Value / Amount footer is real chrome — keep sales_value
    # through sanitize (qty-only rows have no per-line sales money).
    # Last Summary band = Goods Value + Amount; prefer Goods for line-matching
    # closing, keep Amount parallel under amount_* / opening_value.
    extra_t["total_row_source"] = "printed_footer"
    try:
        line_cv = sum(
            float(it.get("closing_value") or 0)
            for it in kept
            if isinstance(it, dict)
        )
    except (TypeError, ValueError):
        line_cv = 0.0
    try:
        goods_cv = float(extra_t.get("goods_value") or 0)
    except (TypeError, ValueError):
        goods_cv = 0.0
    try:
        tot_cv = float(totals.get("closing_value") or 0)
    except (TypeError, ValueError):
        tot_cv = 0.0
    # Prefer Goods ClStockValue when it matches line Stock Value sum.
    if goods_cv > 0 and (
        abs(goods_cv - line_cv) <= 1.0
        or (tot_cv > 0 and abs(goods_cv - tot_cv) <= 1.0)
        or tot_cv <= 0
    ):
        # Do not let Amount ClStockValue overwrite Goods when line sum agrees.
        if tot_cv > 0 and abs(tot_cv - line_cv) > 1.0 and abs(goods_cv - line_cv) <= 1.0:
            logger.info(
                "STOCK_OP_STOCK_FOOTER_GOODS_OVER_AMOUNT "
                "amount_closing=%s goods_closing=%s line_sum=%s",
                tot_cv,
                goods_cv,
                line_cv,
            )
        totals["closing_value"] = goods_cv
        extra_t["goods_closing_value"] = goods_cv
    # If Vision parked Amount Sales on totals.sales_value, keep it trusted;
    # also stash under amount_sales_value for the parallel Amount row.
    try:
        sales_tot = float(totals.get("sales_value") or 0)
    except (TypeError, ValueError):
        sales_tot = 0.0
    if sales_tot > 0 and extra_t.get("amount_sales_value") in (None, "", 0, 0.0):
        # Heuristic: Amount Sales is typically below Goods Sales on this family.
        extra_t["amount_sales_value"] = sales_tot
    # Prefer Goods Sales for top-level sales_value when both bands exist and
    # Amount was parked on totals.sales_value (Goods is usually larger).
    try:
        goods_sales = float(extra_t.get("goods_sales_value") or 0)
    except (TypeError, ValueError):
        goods_sales = 0.0
    if goods_sales <= 0:
        # Some payloads put Goods Sales on totals.sales_value already.
        goods_sales = 0.0
    if goods_sales > 0:
        totals["sales_value"] = goods_sales
    extra_t["extraction_method"] = "op_stock_pur_sales_cl_vision"
    extra_t["layout"] = "op_stock_pur_sales_cl"
    extra_t["psr_column_layout"] = "op_stock_pur_sales_cl"
    logger.info(
        "STOCK_OP_STOCK_FOOTER_TRUSTED closing=%s sales=%s goods_value=%s",
        totals.get("closing_value"),
        totals.get("sales_value"),
        extra_t.get("goods_value"),
    )
    return result


def extract_op_stock_pur_sales_cl_scanned_pdf(
    file_bytes: bytes,
    filename: str,
    images: Optional[List[bytes]] = None,
    *,
    header_peek: str = "",
) -> Optional[Dict[str, Any]]:
    """Scanned Stock And Sales Statement: Pack|Op.Stock|Pur|Sales|Cl|Stock Value."""
    if not _op_stock_pur_sales_cl_vision_enabled():
        logger.info(
            "STOCK_OP_STOCK_PUR_SALES_CL file=%s status=skip reason=flag_off",
            filename,
        )
        return None
    if images is None:
        images = _render_pdf_page_pngs(file_bytes, max_pages=2, zoom=2.8)
        if not images:
            try:
                from services.gemini_extraction_fallback import _pdf_images

                images = _pdf_images(file_bytes, max_pages=2) or []
            except Exception:
                images = []
    if not images:
        return None
    peek = header_peek or _ocr_png_header_probe(images[0])
    if peek and not _has_stock_and_sales_op_stock_cl_headers(peek):
        logger.info(
            "STOCK_OP_STOCK_PUR_SALES_CL file=%s status=skip reason=headers",
            filename,
        )
        return None

    model = os.getenv("VISION_MODEL", "gemini-2.5-flash-lite").strip()
    seed = _call_psr_vision(
        images[0],
        filename,
        ".png",
        vision_bytes=images[0],
        image_meta={
            "normalized": False,
            "normalization_reason": None,
            "op_stock_pur_sales_cl": True,
        },
        candidate=1,
        model=model,
        route="op_stock_pur_sales_cl",
        reason="scanned_pdf_op_stock_pur_sales_cl",
        prompt=OP_STOCK_PUR_SALES_CL_VISION_PROMPT,
        layout_label="op_stock_pur_sales_cl",
        default_title="Stock And Sales Statement",
    )
    if seed is None or len(seed.get("line_items") or []) < 3:
        logger.info(
            "STOCK_OP_STOCK_PUR_SALES_CL file=%s status=skip rows=%s",
            filename,
            len((seed or {}).get("line_items") or []),
        )
        return None
    seed = _finalize_op_stock_pur_sales_cl(seed)
    gemini_calls = 1
    missing_sales = _op_stock_missing_sale_count(seed.get("line_items") or [])
    if missing_sales >= 1:
        logger.info(
            "STOCK_OP_STOCK_SALES_QTY_REPAIR file=%s status=retry missing_sales=%s",
            filename,
            missing_sales,
        )
        repair_bytes = _widen_op_stock_sales_qty_columns(images[0]) or images[0]
        repair = _call_psr_vision(
            repair_bytes,
            filename,
            ".png",
            vision_bytes=repair_bytes,
            image_meta={
                "normalized": False,
                "normalization_reason": None,
                "op_stock_sales_qty_repair": True,
                "op_stock_sales_qty_widen": repair_bytes is not images[0],
            },
            candidate=2,
            model=model,
            route="op_stock_pur_sales_cl",
            reason="scanned_pdf_op_stock_sales_qty_repair",
            prompt=OP_STOCK_SALES_QTY_REPAIR_PROMPT,
            layout_label="op_stock_pur_sales_cl",
            default_title="Stock And Sales Statement",
        )
        gemini_calls = 2
        if repair and repair.get("line_items"):
            patched = _merge_op_stock_sales_qty_repair(seed, repair)
            if patched:
                seed = _finalize_op_stock_pur_sales_cl(seed)
                extra_p = seed.setdefault("totals", {}).setdefault("extra", {})
                extra_p["op_stock_sales_qty_repaired_rows"] = patched
                logger.info(
                    "STOCK_OP_STOCK_SALES_QTY_REPAIR file=%s patched=%s",
                    filename,
                    patched,
                )
    validation = validate_stock_direct_vision(
        seed, layout="op_stock_pur_sales_cl"
    )
    items_n = len(seed.get("line_items") or [])
    identity_fail = int(validation.get("identity_fail_count") or 0)
    # Header-matched Statement scans: soft-accept when enough rows exist.
    # Empty-peek diversions must look identity-healthy so Op.Qty/Op.Val Detail
    # scans are not stolen when OCR chrome is blank.
    peek_blank = not (header_peek or "").strip()
    if peek_blank:
        # soft_ok alone must not override a high identity-fail rate — that
        # steals OPENING|RECEIVE|ISSUE multipage sheets when OCR chrome is blank.
        ok_empty = (
            items_n >= 5
            and identity_fail <= max(2, items_n // 5)
            and bool(validation.get("soft_ok") or validation.get("ok"))
        )
        if not ok_empty:
            logger.info(
                "STOCK_OP_STOCK_PUR_SALES_CL file=%s status=reject "
                "reason=empty_peek_weak_identity rows=%s identity_fail=%s "
                "soft_ok=%s",
                filename,
                items_n,
                identity_fail,
                validation.get("soft_ok"),
            )
            return None
    elif not validation.get("ok") and items_n < 5:
        logger.info(
            "STOCK_OP_STOCK_PUR_SALES_CL file=%s status=reject reasons=%s",
            filename,
            ",".join(validation.get("reasons") or []) or "none",
        )
        return None
    extra = seed.setdefault("totals", {}).setdefault("extra", {})
    extra["extraction_method"] = "op_stock_pur_sales_cl_vision"
    extra["layout"] = "op_stock_pur_sales_cl"
    extra["stock_op_stock_pur_sales_cl_scanned"] = True
    lock_stock_vision_result(
        seed,
        early_vision_reason="op_stock_pur_sales_cl_vision",
        stock_direct_vision_route="op_stock_pur_sales_cl",
        stock_direct_vision_candidate=1,
        gemini_calls=gemini_calls,
        extraction_method="op_stock_pur_sales_cl_vision",
        layout="op_stock_pur_sales_cl",
        psr_column_layout="op_stock_pur_sales_cl",
        stock_identity_kind="opening_receipts_sales_closing",
        numeric_source="gemini_vision",
    )
    logger.info(
        "STOCK_OP_STOCK_PUR_SALES_CL file=%s status=ok rows=%s "
        "identity_fail=%s pack_repaired=%s sales_repaired=%s calls=%s",
        filename,
        len(seed.get("line_items") or []),
        validation.get("identity_fail_count"),
        extra.get("op_stock_pack_opening_repaired"),
        extra.get("op_stock_sales_qty_repaired_rows"),
        gemini_calls,
    )
    return seed


def extract_stock_sales_op_qty_val_scanned_pdf(
    file_bytes: bytes,
    filename: str,
) -> Optional[Dict[str, Any]]:
    """Render image-only PDF pages and run Op.Qty/Op.Val Detail vision.

    Used before generic scanned_pdf vision_table so P.Sch / return columns
    do not steal sales_qty / closing_qty. Budget: max 2 Gemini calls.
    Prefer per-page Vision (STOCK_SALES_DETAIL_SCAN_PER_PAGE, default ON) —
    vertical stitch downscales dense Cl.Val columns and shifts values.
    """
    images = _render_pdf_page_pngs(file_bytes, max_pages=2, zoom=2.8)
    if not images:
        try:
            from services.gemini_extraction_fallback import _pdf_images

            images = _pdf_images(file_bytes, max_pages=2) or []
        except Exception:
            images = []
    if not images:
        return None

    # Marg OPENING STOCK|RECEIVE|ISSUE|CLOSING scanned sheets must not use the
    # Op.Qty/P.Qty Detail prompt (sales→0, opening value→closing_qty).
    header_peek = _ocr_png_header_probe(images[0])
    if _has_opening_receive_issue_closing_qty_value_headers(header_peek):
        logger.info(
            "STOCK_SALES_OP_QTY_VAL_SCANNED_PDF file=%s status=skip "
            "reason=opening_receive_issue_closing_qty_value",
            filename,
        )
        return None
    # PharmAssist Op./Pur/Sale/Bal./BVal/SVal must not use Op.Qty Detail Vision
    # (prior-month Sep/Aug|Jul/Jun would land in opening/receipts).
    if _has_pharmassist_stock_sale_headers(header_peek or ""):
        logger.info(
            "STOCK_SALES_OP_QTY_VAL_SCANNED_PDF file=%s status=skip "
            "reason=pharmassist_stock_sale",
            filename,
        )
        return None
    # Multipage + blank header OCR: do not early-claim before camera multipage.
    # Without Tesseract, OPENING STOCK|RECEIVE sheets were stolen by Op.Stock /
    # Detail Vision (page 1 only, sales→0).
    if len(images) >= 2 and not (header_peek or "").strip():
        logger.info(
            "STOCK_SALES_OP_QTY_VAL_SCANNED_PDF file=%s status=skip "
            "reason=multipage_empty_header_peek",
            filename,
        )
        return None

    # Pack|Op.Stock|Pur.Qty|Sales Qty|Cl.Stock|Stock Value scans must not use
    # the Op.Qty/Op.Val Detail prompt (Pack digits leak into opening_qty).
    _op_stock_hit = _op_stock_pur_sales_cl_vision_enabled() and (
        _has_stock_and_sales_op_stock_cl_headers(header_peek)
        or (
            re.search(r"Stock\s+And\s+Sales\s+Statement", header_peek or "", re.I)
            and not re.search(
                r"Stock\s+and\s+Sales\s+Detail", header_peek or "", re.I
            )
            and not _has_stock_sales_op_qty_val_headers(header_peek or "")
        )
    )
    # Empty OCR peek: still try Op.Stock Vision once when Detail headers are
    # absent (Statement scans often OCR blank on blue/dense chrome).
    # Multipage image PDFs must not take this diversion — OPENING STOCK|
    # RECEIVE|ISSUE|CLOSING sheets (and other families) belong to camera
    # multipage / SSA dump when chrome OCR is blank (no Tesseract).
    if (
        not _op_stock_hit
        and _op_stock_pur_sales_cl_vision_enabled()
        and not (header_peek or "").strip()
        and not _has_stock_sales_op_qty_val_headers(header_peek or "")
        and len(images) < 2
    ):
        _op_stock_hit = True
        logger.info(
            "STOCK_OP_STOCK_PUR_SALES_CL file=%s status=try "
            "reason=empty_header_peek",
            filename,
        )
    if _op_stock_hit:
        diverted = extract_op_stock_pur_sales_cl_scanned_pdf(
            file_bytes,
            filename,
            images=images,
            header_peek=header_peek or "",
        )
        if diverted is not None:
            return diverted
        logger.info(
            "STOCK_SALES_OP_QTY_VAL_SCANNED_PDF file=%s "
            "op_stock_divert_missed continuing_op_qty_val",
            filename,
        )

    # Do not force Op.Qty Detail Vision on unrelated scanned families (ORIC
    # OPENING|RECEIVE|ISSUE, blank/noisy chrome, etc.). Positive Detail headers
    # (or Detail title route) are required before claiming the page.
    if not _has_stock_sales_op_qty_val_headers(header_peek or ""):
        logger.info(
            "STOCK_SALES_OP_QTY_VAL_SCANNED_PDF file=%s status=skip "
            "reason=no_op_qty_val_headers",
            filename,
        )
        return None

    seed: Optional[Dict[str, Any]] = None
    calls = 0
    stitched = _stitch_png_pages_vertical(images) or images[0]

    if _stock_sales_detail_scan_per_page_enabled():
        merged_items: List[Dict[str, Any]] = []
        widen_cl = _stock_sales_detail_widen_cl_enabled()
        for page_index, image in enumerate(images[:2]):
            vision_image = (
                _widen_detail_closing_columns(image) if widen_cl else image
            )
            page = extract_stock_sales_op_qty_val_direct_vision(
                vision_image,
                filename,
                ".png",
                reason=(
                    f"scanned_pdf_op_qty_val_page{page_index + 1}"
                    + ("_widen_cl" if widen_cl else "")
                ),
                max_attempts=1,
            )
            calls += 1
            if not page or not page.get("line_items"):
                continue
            if seed is None:
                seed = page
            for item in page.get("line_items") or []:
                if isinstance(item, dict):
                    merged_items.append(item)
            logger.info(
                "STOCK_SALES_DETAIL_SCAN_PER_PAGE file=%s page=%s rows=%s "
                "widen_cl=%s",
                filename,
                page_index + 1,
                len(page.get("line_items") or []),
                widen_cl,
            )
        if seed is not None:
            seed["line_items"] = merged_items

    # Fallback: one stitched call (+ optional repair when a call remains).
    if seed is None or len(seed.get("line_items") or []) < 5:
        seed = _call_op_qty_val_multi_image(
            [stitched],
            filename,
            prompt=STOCK_SALES_OP_QTY_VAL_VISION_PROMPT,
            reason="scanned_pdf_op_qty_val_stitched",
            candidate=1,
        )
        calls = 1
        logger.info(
            "STOCK_SALES_DETAIL_SCAN_STITCH file=%s rows=%s",
            filename,
            len((seed or {}).get("line_items") or []),
        )

    if seed is None or len(seed.get("line_items") or []) < 5:
        logger.info(
            "STOCK_SALES_OP_QTY_VAL_SCANNED_PDF file=%s status=skip rows=%s",
            filename,
            len((seed or {}).get("line_items") or []),
        )
        return None

    validation = validate_stock_direct_vision(
        seed, layout="stock_sales_op_qty_val"
    )
    items = seed.get("line_items") or []
    missing_cv = 0
    for item in items:
        if not isinstance(item, dict):
            continue
        cl = item.get("closing_qty")
        cv = item.get("closing_value")
        try:
            cl_f = float(cl) if cl not in (None, "") else 0.0
        except (TypeError, ValueError):
            cl_f = 0.0
        try:
            cv_f = float(cv) if cv not in (None, "") else 0.0
        except (TypeError, ValueError):
            cv_f = 0.0
        if cl_f > 0 and cv_f <= 0:
            missing_cv += 1
    needs_closing_repair = calls < 2 and (
        missing_cv >= max(2, len(items) // 5)
        or int(validation.get("identity_fail_count") or 0)
        > max(2, len(items) // 4)
    )

    if needs_closing_repair and calls < 2:
        repair = _call_op_qty_val_multi_image(
            [stitched],
            filename,
            prompt=STOCK_SALES_DETAIL_CLOSING_REPAIR_PROMPT,
            reason="scanned_pdf_op_qty_val_closing_repair",
            candidate=2,
        )
        calls += 1
        if repair and repair.get("line_items"):
            patched = _merge_detail_closing_repair(seed, repair)
            logger.info(
                "STOCK_SALES_DETAIL_CLOSING_REPAIR file=%s patched=%s",
                filename,
                patched,
            )
            validation = validate_stock_direct_vision(
                seed, layout="stock_sales_op_qty_val"
            )

    if not validation.get("ok"):
        logger.info(
            "STOCK_SALES_OP_QTY_VAL_SCANNED_PDF file=%s status=reject reasons=%s",
            filename,
            ",".join(validation.get("reasons") or []) or "none",
        )
        return None
    # Wrong-layout symptom: every S.Qty read as 0 while identity majority fails
    # (OPENING/RECEIVE/ISSUE sheets misrouted into Op.Qty Detail Vision).
    sales_pos = sum(
        1
        for item in items
        if isinstance(item, dict) and float(item.get("sales_qty") or 0) > 0.01
    )
    identity_fail = int(validation.get("identity_fail_count") or 0)
    if (
        sales_pos == 0
        and len(items) >= 8
        and identity_fail > max(2, len(items) // 4)
    ):
        logger.info(
            "STOCK_SALES_OP_QTY_VAL_SCANNED_PDF file=%s status=reject "
            "reason=zero_sales_identity_majority identity_fail=%s rows=%s",
            filename,
            identity_fail,
            len(items),
        )
        return None
    # Pack→opening column reassignment on Statement-style scans that still
    # landed on the Op.Qty/Op.Val early path (empty OCR peek / soft accept).
    if _op_stock_pur_sales_cl_vision_enabled():
        before_items = list(seed.get("line_items") or [])
        seed = _finalize_op_stock_pur_sales_cl(seed)
        # Keep Op.Qty/Op.Val method tags when this path produced the rows.
        extra_fix = seed.setdefault("totals", {}).setdefault("extra", {})
        if extra_fix.get("op_stock_pack_opening_repaired"):
            logger.info(
                "STOCK_OP_STOCK_PACK_OPENING_REPAIR file=%s applied_on=op_qty_val "
                "repaired=%s",
                filename,
                extra_fix.get("op_stock_pack_opening_repaired"),
            )
        extra_fix["extraction_method"] = "stock_sales_op_qty_val_vision"
        extra_fix["layout"] = "stock_sales_op_qty_val"
        # Drop footer rows only; do not shrink below the pre-repair set size
        # unless footers were removed.
        if len(seed.get("line_items") or []) < max(3, len(before_items) // 2):
            seed["line_items"] = before_items

    extra = seed.setdefault("totals", {}).setdefault("extra", {})
    extra["extraction_method"] = "stock_sales_op_qty_val_vision"
    extra["layout"] = "stock_sales_op_qty_val"
    extra["stock_sales_detail_scanned_pdf"] = True
    extra["stock_sales_detail_scan_per_page"] = (
        _stock_sales_detail_scan_per_page_enabled()
    )
    extra["stock_sales_detail_widen_cl"] = _stock_sales_detail_widen_cl_enabled()
    lock_stock_vision_result(
        seed,
        early_vision_reason="stock_sales_op_qty_val_scanned_pdf",
        stock_direct_vision_route="stock_sales_op_qty_val",
        stock_direct_vision_candidate=1,
        gemini_calls=calls,
    )
    logger.info(
        "STOCK_SALES_OP_QTY_VAL_SCANNED_PDF file=%s status=ok rows=%s "
        "identity_fail=%s soft_ok=%s calls=%s",
        filename,
        len(seed.get("line_items") or []),
        validation.get("identity_fail_count"),
        validation.get("soft_ok"),
        calls,
    )
    return seed


def _peel_ostk_qoh_footer_money(text: str) -> Dict[str, float]:
    """Peel Opening/Purchase/Sale/Closing Value from Profitmaker Qoh footers.

    When ``Grand Total`` is printed, prefer the last Grand Total band over
    earlier section totals. OCR may garble Purchase→Surchase, Closing→Closina,
    Sale Value→Sale Vatue. Blank Purchase Value stays absent (never Sale copy).
    """
    blob = text or ""
    out: Dict[str, float] = {}
    if not blob.strip():
        return out
    grand_matches = list(re.finditer(r"Grand\s*Total\s*:?", blob, re.I))
    if grand_matches:
        start = grand_matches[-1].start()
        blob = blob[start : start + 800]

    specs = (
        (
            "opening_value",
            r"Open(?:ing)?\s+Va[lt](?:ue|uc)?\s*[:.\-]?\s*([\d,.]+)",
        ),
        (
            "purchase_value",
            # Require a real digit after the label so "Purchase Value : Closing"
            # (blank field) does not steal Closing's money.
            r"(?:P|S)?urchase\s+Va[lt](?:ue|uc)?\s*[:.\-]?\s*([\d,.]+)",
        ),
        (
            "sales_value",
            r"Sale\s+Va[lt](?:ue|uc)?\s*[:.\-]?\s*([\d,.]+)",
        ),
        (
            "closing_value",
            r"Clos(?:ing|ina)\s+Va[lt](?:ue|uc)?\s*[:.\-]?\s*([\d,.]+)",
        ),
    )
    for key, pat in specs:
        matches = list(re.finditer(pat, blob, re.I))
        if not matches:
            continue
        # Prefer the last printed label (final Grand Total / page footer).
        compact = matches[-1].group(1).replace(",", "").strip()
        try:
            val = float(compact)
        except ValueError:
            val = None
        if val is not None:
            out[key] = val

    # Blank Purchase Value: label present, no digit before Closing / end.
    # Mark explicitly so callers clear Vision's Sale→Purchase copy.
    if "purchase_value" not in out and re.search(
        r"(?:P|S)?urchase\s+Va[lt]|PU[A-Z]?[GgHh].{0,8}(?:Val|Nau|Value)",
        blob,
        re.I,
    ):
        blank = re.search(
            r"(?:P|S)?urchase\s+Va[lt](?:ue|uc)?\s*[:.\-]?\s*"
            r"(?=Clos|\n|$)",
            blob,
            re.I,
        ) or re.search(
            r"PU[A-Z]?[GgHh].{0,12}\s*[:.\-]?\s*(?=Clos|\n|$)",
            blob,
            re.I,
        )
        # "Purchase Value : 0.00" next to a blank cell is often OCR noise when
        # Sale Value is non-zero — treat as blank unless a non-zero purchase
        # digit was peeled above.
        if blank or (
            re.search(r"(?:P|S)?urchase\s+Va[lt]|PU[A-Z]?[GgHh]", blob, re.I)
            and abs(float(out.get("sales_value") or 0)) > 0.051
        ):
            out["_purchase_value_blank"] = 1.0
    return out


def _ocr_ostk_qoh_footer_band(file_bytes: bytes) -> str:
    """OCR the lower band where Opening/Purchase/Sale/Closing Value prints."""
    if not file_bytes:
        return ""
    try:
        from PIL import Image
        from services.sales_statement_extractor import _a2z_tesseract
        from services.stock_ocr_policy import stock_ocr_context
    except Exception:
        return ""
    try:
        digest = hashlib.sha256(file_bytes).hexdigest()[:10]
        tess = _a2z_tesseract()
        with Image.open(io.BytesIO(file_bytes)) as raw:
            img = raw.convert("RGB")
        width, height = img.size
        if width < 80 or height < 80:
            return ""
        # Stacked page photos print the final money footer near the bottom.
        band = img.crop((0, int(height * 0.72), width, height))
        band.thumbnail((1600, 900))
        with stock_ocr_context(region=f"ostk_qoh_footer_{digest}", page="1"):
            return tess.image_to_string(band, config="--psm 6") or ""
    except Exception:
        return ""


def _apply_ostk_qoh_footer_money(
    result: Optional[Dict[str, Any]], file_bytes: bytes
) -> None:
    """Stamp printed footer money onto totals; last Grand Total wins."""
    if not isinstance(result, dict):
        return
    footer_text = _ocr_ostk_qoh_footer_band(file_bytes)
    money = _peel_ostk_qoh_footer_money(footer_text)
    if not money or (
        "sales_value" not in money and "closing_value" not in money
    ):
        # Band crop sometimes misses labels; fall back to full-page OCR.
        try:
            from services.sales_statement_extractor import _ocr_image_to_text

            full_money = _peel_ostk_qoh_footer_money(
                _ocr_image_to_text(file_bytes, psm=6, enhance=False) or ""
            )
            if full_money:
                money = {**(money or {}), **full_money}
        except Exception:
            pass
    if not money:
        return
    totals = result.setdefault("totals", {})
    extra = totals.setdefault("extra", {})
    purchase_blank = bool(money.pop("_purchase_value_blank", None))
    if "opening_value" in money:
        extra["opening_value"] = money["opening_value"]
        extra["opening_value_source"] = "ostk_qoh_footer_ocr"
    if "purchase_value" in money and not purchase_blank:
        extra["purchase_value"] = money["purchase_value"]
        extra["purchase_value_source"] = "ostk_qoh_footer_ocr"
    if "sales_value" in money:
        totals["sales_value"] = money["sales_value"]
        extra["sales_value_source"] = "ostk_qoh_footer_ocr"
    if "closing_value" in money:
        totals["closing_value"] = money["closing_value"]
        extra["closing_value_source"] = "ostk_qoh_footer_ocr"
    # Blank / missing Purchase Value: never keep Vision's Sale Value copy.
    try:
        pv = float(extra.get("purchase_value")) if extra.get("purchase_value") is not None else None
    except (TypeError, ValueError):
        pv = None
    try:
        sv = float(totals.get("sales_value")) if totals.get("sales_value") is not None else None
    except (TypeError, ValueError):
        sv = None
    if purchase_blank or "purchase_value" not in money:
        if pv is not None and (
            purchase_blank
            or (sv is not None and abs(pv - sv) < 0.051)
            or abs(pv) < 0.051
        ):
            extra["purchase_value"] = None
            extra["purchase_value_source"] = "ostk_qoh_footer_blank"
            if "receipts_value" in extra and extra.get("receipts_value") == pv:
                extra["receipts_value"] = None
            logger.info(
                "STOCK_OSTK_QOH_FOOTER_PURCHASE_BLANK cleared_pv=%s sales=%s",
                pv,
                sv,
            )
    extra["total_row_source"] = "ostk_qoh_footer"
    logger.info(
        "STOCK_OSTK_QOH_FOOTER opening=%s purchase=%s sales=%s closing=%s blank_pur=%s",
        money.get("opening_value"),
        extra.get("purchase_value"),
        money.get("sales_value"),
        money.get("closing_value"),
        purchase_blank,
    )


def _opqty_qoh_item_key(item: Dict[str, Any]) -> str:
    name = re.sub(
        r"\s+", " ", str(item.get("product_name") or "").strip().upper()
    )
    pack = re.sub(r"\s+", "", str(item.get("packing") or "").strip().upper())
    return f"{name}|{pack}"


def _ostk_qoh_closing_value_num(item: Dict[str, Any]) -> float:
    try:
        return float(item.get("closing_value") or 0)
    except (TypeError, ValueError):
        return 0.0


def _merge_opqty_qoh_halves(
    top: Dict[str, Any], bottom: Dict[str, Any]
) -> Dict[str, Any]:
    """Merge overlapping tall-page Vision halves; prefer bottom Value on overlaps.

    Full-frame top often zeros/shifts closing_value on the last active SKUs.
    Bottom-band Vision is zoomed on those rows — reuse its printed Value (and
    matching qty) instead of discarding the overlap.
    """
    merged = top if isinstance(top, dict) else bottom
    if not isinstance(bottom, dict):
        return merged
    if not isinstance(top, dict):
        return bottom
    by_key: Dict[str, Dict[str, Any]] = {}
    out_items: List[Dict[str, Any]] = []
    for item in top.get("line_items") or []:
        if not isinstance(item, dict):
            continue
        key = _opqty_qoh_item_key(item)
        out_items.append(item)
        if key != "|":
            by_key[key] = item
    added = 0
    value_updated = 0
    for item in bottom.get("line_items") or []:
        if not isinstance(item, dict):
            continue
        key = _opqty_qoh_item_key(item)
        if key == "|":
            continue
        existing = by_key.get(key)
        if existing is None:
            out_items.append(item)
            by_key[key] = item
            added += 1
            continue
        # Overlap: prefer bottom closing_value when top dropped/shifted it.
        bot_cv = _ostk_qoh_closing_value_num(item)
        top_cv = _ostk_qoh_closing_value_num(existing)
        try:
            bot_qoh = float(item.get("closing_qty") or 0)
            top_qoh = float(existing.get("closing_qty") or 0)
        except (TypeError, ValueError):
            bot_qoh = top_qoh = 0.0
        better_value = bot_cv > 0.051 and (
            top_cv < 0.051 or abs(bot_cv - top_cv) > 0.051
        )
        better_qoh = abs(bot_qoh - top_qoh) > 0.051 and bot_qoh >= 0
        if better_value or (top_cv < 0.051 and bot_cv > 0.051):
            existing["closing_value"] = bot_cv
            value_updated += 1
            extra = (
                existing.get("extra")
                if isinstance(existing.get("extra"), dict)
                else {}
            )
            extra = dict(extra)
            extra["ostk_qoh_closing_from_bottom_band"] = True
            extra["ostk_qoh_pre_bottom_closing_value"] = top_cv
            existing["extra"] = extra
            logger.info(
                "STOCK_OSTK_QOH_BOTTOM_VALUE_OVERRIDE product=%s top_cv=%s bot_cv=%s",
                str(existing.get("product_name") or "")[:60],
                top_cv,
                bot_cv,
            )
        if better_qoh and bot_qoh >= 0:
            # Only adopt bottom qty when top Value was also wrong/missing —
            # avoids rewriting good full-frame qty from a partial crop.
            if top_cv < 0.051 or better_value:
                for qty_key in (
                    "opening_qty",
                    "receipts_qty",
                    "sales_qty",
                    "closing_qty",
                ):
                    if item.get(qty_key) is not None:
                        existing[qty_key] = item.get(qty_key)
                bot_ex = item.get("extra") if isinstance(item.get("extra"), dict) else {}
                if bot_ex.get("total_stock") is not None:
                    extra = (
                        existing.get("extra")
                        if isinstance(existing.get("extra"), dict)
                        else {}
                    )
                    extra = dict(extra)
                    extra["total_stock"] = bot_ex.get("total_stock")
                    if bot_ex.get("age_days") is not None:
                        extra["age_days"] = bot_ex.get("age_days")
                    existing["extra"] = extra
    merged["line_items"] = out_items
    # Prefer bottom-half letterhead/footer when top crop missed them.
    for field in ("stockist_name", "company_name", "period_from", "period_to"):
        if not merged.get(field) and bottom.get(field):
            merged[field] = bottom.get(field)
    top_tot = top.get("totals") if isinstance(top.get("totals"), dict) else {}
    bot_tot = bottom.get("totals") if isinstance(bottom.get("totals"), dict) else {}
    merged_tot = merged.setdefault("totals", {})
    for key in ("sales_value", "closing_value"):
        if bot_tot.get(key) is not None:
            merged_tot[key] = bot_tot.get(key)
        elif top_tot.get(key) is not None and merged_tot.get(key) is None:
            merged_tot[key] = top_tot.get(key)
    top_ex = top_tot.get("extra") if isinstance(top_tot.get("extra"), dict) else {}
    bot_ex = bot_tot.get("extra") if isinstance(bot_tot.get("extra"), dict) else {}
    merged_ex = merged_tot.setdefault("extra", {})
    for key, val in {**top_ex, **bot_ex}.items():
        if key == "purchase_value":
            # Do not let a Vision Sale→Purchase copy win over a blank footer.
            continue
        if key not in merged_ex or merged_ex.get(key) in (None, "", 0, 0.0):
            merged_ex[key] = val
    # Prefer bottom purchase only when it is a real distinct money figure.
    for src in (bot_ex, top_ex):
        pv = src.get("purchase_value")
        try:
            pv_f = float(pv) if pv is not None else None
        except (TypeError, ValueError):
            pv_f = None
        if pv_f is None:
            continue
        try:
            sv_f = float(merged_tot.get("sales_value") or 0)
        except (TypeError, ValueError):
            sv_f = 0.0
        if abs(pv_f - sv_f) < 0.051:
            continue
        merged_ex["purchase_value"] = pv_f
        break
    merged_ex["opqty_qoh_halves_merged"] = True
    merged_ex["opqty_qoh_halves_added"] = added
    merged_ex["opqty_qoh_halves_value_updated"] = value_updated
    logger.info(
        "STOCK_OSTK_QOH_HALVES_MERGED top=%s bottom=%s out=%s added=%s value_updated=%s",
        len(top.get("line_items") or []),
        len(bottom.get("line_items") or []),
        len(out_items),
        added,
        value_updated,
    )
    return merged


def _repair_ostk_qoh_closing_value_row_shift(
    result: Optional[Dict[str, Any]], peek_text: str = ""
) -> int:
    """Unshift Value when Vision parked row i's money on row i+1.

    Layout-family only (O.Stk|Purc|Tot|Sale|Qoh|Value|Age). Uses printed OCR
    ``Sale Qoh Value`` digits — never invents from footer arithmetic.
    """
    if not isinstance(result, dict):
        return 0
    items = [i for i in (result.get("line_items") or []) if isinstance(i, dict)]
    if len(items) < 2:
        return 0

    # OCR map: (sales_qty, closing_qty) → printed Value (last wins).
    ocr_by_sale_qoh: Dict[Tuple[int, int], float] = {}
    for m in re.finditer(
        r"(?<!\d)(\d+)\s+(\d+)\s+([\d,]+\.\d{2})\s+(\d{2,4})(?!\d)",
        peek_text or "",
    ):
        try:
            sale_i = int(m.group(1))
            qoh_i = int(m.group(2))
            val = float(m.group(3).replace(",", ""))
        except (TypeError, ValueError):
            continue
        if val < 0.051:
            continue
        ocr_by_sale_qoh[(sale_i, qoh_i)] = val

    fixed = 0
    for i in range(len(items) - 1):
        a = items[i]
        b = items[i + 1]
        try:
            a_qoh = float(a.get("closing_qty") or 0)
            a_sale = float(a.get("sales_qty") or 0)
            a_cv = float(a.get("closing_value") or 0)
            b_qoh = float(b.get("closing_qty") or 0)
            b_sale = float(b.get("sales_qty") or 0)
            b_cv = float(b.get("closing_value") or 0)
        except (TypeError, ValueError):
            continue
        if a_qoh < 0.051 or b_cv < 0.051:
            continue
        ocr_a = ocr_by_sale_qoh.get((int(round(a_sale)), int(round(a_qoh))))
        ocr_b = ocr_by_sale_qoh.get((int(round(b_sale)), int(round(b_qoh))))

        # Case 1: a Value missing; b holds a's printed OCR Value.
        if a_cv < 0.051 and ocr_a is not None and abs(ocr_a - b_cv) < 0.051:
            a["closing_value"] = float(ocr_a)
            a_extra = a.get("extra") if isinstance(a.get("extra"), dict) else {}
            a_extra = dict(a_extra)
            a_extra["ostk_qoh_value_unshift_from_next"] = True
            a["extra"] = a_extra
            if ocr_b is not None and abs(ocr_b - ocr_a) > 0.051:
                b["closing_value"] = float(ocr_b)
            else:
                b["closing_value"] = 0.0
                b_extra = b.get("extra") if isinstance(b.get("extra"), dict) else {}
                b_extra = dict(b_extra)
                b_extra["ostk_qoh_value_steal_cleared"] = True
                b_extra["ostk_qoh_pre_steal_closing_value"] = b_cv
                b["extra"] = b_extra
            fixed += 1
            logger.info(
                "STOCK_OSTK_QOH_VALUE_UNSHIFT product=%s value=%s next=%s",
                str(a.get("product_name") or "")[:60],
                ocr_a,
                str(b.get("product_name") or "")[:60],
            )
            continue

        # Case 2: a already repaired to OCR Value, but b still duplicates it
        # (neighbor steal). Restore b from OCR when available; else clear.
        if (
            a_cv > 0.051
            and abs(a_cv - b_cv) < 0.051
            and b_qoh > 0.051
            and ocr_a is not None
            and abs(ocr_a - a_cv) < 0.051
        ):
            if ocr_b is not None and abs(ocr_b - a_cv) > 0.051:
                b["closing_value"] = float(ocr_b)
            else:
                b["closing_value"] = 0.0
                b_extra = b.get("extra") if isinstance(b.get("extra"), dict) else {}
                b_extra = dict(b_extra)
                b_extra["ostk_qoh_value_steal_cleared"] = True
                b_extra["ostk_qoh_pre_steal_closing_value"] = b_cv
                b["extra"] = b_extra
            fixed += 1
            logger.info(
                "STOCK_OSTK_QOH_VALUE_STEAL_CLEAR product=%s stolen=%s next=%s",
                str(a.get("product_name") or "")[:60],
                a_cv,
                str(b.get("product_name") or "")[:60],
            )
    if fixed:
        logger.info("STOCK_OSTK_QOH_VALUE_ROW_SHIFT fixed=%s", fixed)
    return fixed


def _finalize_opqty_qoh_result(
    result: Optional[Dict[str, Any]], file_bytes: bytes
) -> Optional[Dict[str, Any]]:
    """Age/receipts repair + footer peel + letterhead enrich for Qoh Vision."""
    if not isinstance(result, dict):
        return result
    _repair_ostk_qoh_age_as_closing_value(result)
    _repair_ostk_qoh_receipts_shift(result)
    _apply_ostk_qoh_footer_money(result, file_bytes)
    try:
        from services.sales_statement_extractor import (
            _enrich_ostk_qoh_letterhead,
            _ocr_image_to_text,
            _repair_ostk_qoh_closing_value_from_ocr,
            _repair_ostk_qoh_zero_qty_from_ocr,
        )

        peek = _ocr_image_to_text(file_bytes, psm=6, enhance=False) or ""
        _enrich_ostk_qoh_letterhead(result, peek)
        _repair_ostk_qoh_zero_qty_from_ocr(result, peek)
        _repair_ostk_qoh_closing_value_from_ocr(result, peek)
        _repair_ostk_qoh_closing_value_row_shift(result, peek)
    except Exception:
        pass
    return result


def _repair_ostk_qoh_receipts_shift(result: Optional[Dict[str, Any]]) -> None:
    """Undo Tot/Sale parked in receipts_qty on O.Stk|Purc|Tot|Sale|Qoh rows.

    Does not invent Purc from thin air:
    - Sale duplicated into receipts when Purc was 0 and opening-sales=closing
      → receipts_qty = 0
    - Tot parked in receipts when extra.total_stock == receipts → Purc = Tot-Op
    - Qoh=0 rows where closing copied opening after a full sell-through
      (sales == Tot) → closing_qty = 0
    """
    if not isinstance(result, dict):
        return
    sale_dup = 0
    tot_as_purc = 0
    qoh_zero = 0
    for item in result.get("line_items") or []:
        if not isinstance(item, dict):
            continue
        try:
            opening = float(item.get("opening_qty") or 0)
            receipts = float(item.get("receipts_qty") or 0)
            sales = float(item.get("sales_qty") or 0)
            closing = float(item.get("closing_qty") or 0)
        except (TypeError, ValueError):
            continue
        extra = item.get("extra") if isinstance(item.get("extra"), dict) else {}
        tot = extra.get("total_stock")
        try:
            tot_f = float(tot) if tot is not None else None
        except (TypeError, ValueError):
            tot_f = None

        # Tot parked as receipts (MENTAT: receipts=17 was Tot, Purc=2).
        if (
            tot_f is not None
            and abs(receipts - tot_f) < 0.051
            and abs(tot_f - opening) >= 0.051
        ):
            purc = round(tot_f - opening, 2)
            if purc >= -0.051:
                item["receipts_qty"] = max(0.0, purc)
                receipts = float(item["receipts_qty"])
                tot_as_purc += 1
                extra["ostk_qoh_receipts_from_tot"] = True

        # Sale duplicated into receipts when Purc printed 0
        # (SPEMEN/STYPLON: receipts==sales and opening-sales==closing).
        if (
            receipts > 0.051
            and abs(receipts - sales) < 0.051
            and abs(opening - sales - closing) < 0.51
        ):
            item["receipts_qty"] = 0.0
            receipts = 0.0
            sale_dup += 1
            extra["ostk_qoh_receipts_cleared_sale_dup"] = True

        # Full sell-through: Tot==Sale and Qoh printed 0, but closing kept O.Stk.
        if (
            tot_f is not None
            and abs(sales - tot_f) < 0.051
            and abs(closing - opening) < 0.051
            and opening > 0.051
            and abs(opening + receipts - sales) < 0.51
        ):
            item["closing_qty"] = 0.0
            if item.get("closing_value") not in (None,):
                try:
                    if abs(float(item.get("closing_value") or 0)) < 0.051:
                        pass
                    elif abs(opening + receipts - sales) < 0.51:
                        # Qoh 0 ⇒ Value 0.00 on this family.
                        item["closing_value"] = 0.0
                except (TypeError, ValueError):
                    pass
            qoh_zero += 1
            extra["ostk_qoh_closing_cleared_full_sale"] = True

        item["extra"] = extra
        # Force line sales_value null/0 — no per-row Sale Value column.
        if item.get("sales_value") not in (None, 0, 0.0):
            # Only clear when it looks like a qty leak (integer / equals a qty).
            try:
                sv = float(item.get("sales_value") or 0)
            except (TypeError, ValueError):
                sv = None
            if sv is not None and (
                abs(sv - sales) < 0.051
                or abs(sv - opening) < 0.051
                or abs(sv - receipts) < 0.051
                or abs(sv - round(sv)) < 0.051
            ):
                item["sales_value"] = 0.0
        else:
            item["sales_value"] = 0.0

    if sale_dup or tot_as_purc or qoh_zero:
        logger.info(
            "STOCK_OSTK_QOH_RECEIPTS_REPAIR sale_dup=%s tot_as_purc=%s qoh_zero=%s",
            sale_dup,
            tot_as_purc,
            qoh_zero,
        )


def _repair_ostk_qoh_age_as_closing_value(result: Optional[Dict[str, Any]]) -> None:
    """When Age days landed in closing_value and Value was 0.00, move to age_days.

    Heuristic only: integer-like closing_value (>=30) with zero qty and no
    decimals — typical Age spill on zero-stock rows (e.g. ARJUNA Age 637).
    Never invent money; never rewrite non-zero printed Value amounts.
    """
    if not isinstance(result, dict):
        return
    fixed = 0
    for item in result.get("line_items") or []:
        if not isinstance(item, dict):
            continue
        try:
            cv = float(item.get("closing_value") or 0)
            cq = float(item.get("closing_qty") or 0)
            oq = float(item.get("opening_qty") or 0)
            sq = float(item.get("sales_qty") or 0)
        except (TypeError, ValueError):
            continue
        extra = item.get("extra") if isinstance(item.get("extra"), dict) else {}
        if extra.get("age_days") is not None:
            continue
        # Age days are whole integers; money Value almost always has paise.
        if abs(cv - round(cv)) >= 0.005:
            continue
        if cv < 30 or cq or oq or sq:
            continue
        # Zero-stock row with integer "value" → Age spill.
        extra["age_days"] = float(int(round(cv)))
        item["closing_value"] = 0.0
        item["extra"] = extra
        fixed += 1
    if fixed:
        logger.info("STOCK_OSTK_QOH_AGE_REPAIR moved_age_from_closing_value=%s", fixed)


def _ostk_qoh_needs_bottom_value_pass(result: Optional[Dict[str, Any]]) -> bool:
    """True when last active Qoh rows dropped Value or line sum undershoots footer."""
    if not isinstance(result, dict):
        return False
    items = [i for i in (result.get("line_items") or []) if isinstance(i, dict)]
    if not items:
        return False
    active = []
    line_sum = 0.0
    for item in items:
        try:
            qoh = float(item.get("closing_qty") or 0)
            cv = float(item.get("closing_value") or 0)
        except (TypeError, ValueError):
            continue
        line_sum += cv
        if qoh > 0.051:
            active.append(cv)
    # Last few Qoh>0 rows with missing Value (classic camera-photo shift).
    if any(cv < 0.051 for cv in active[-4:]):
        return True
    try:
        footer_cv = float((result.get("totals") or {}).get("closing_value") or 0)
    except (TypeError, ValueError):
        footer_cv = 0.0
    if footer_cv > 1.0 and line_sum + 0.51 < footer_cv * 0.92:
        return True
    # Neighbor steal: consecutive active rows sharing the same non-zero Value.
    for i in range(len(items) - 1):
        try:
            a_q = float(items[i].get("closing_qty") or 0)
            b_q = float(items[i + 1].get("closing_qty") or 0)
            a_v = float(items[i].get("closing_value") or 0)
            b_v = float(items[i + 1].get("closing_value") or 0)
        except (TypeError, ValueError):
            continue
        if a_q > 0.051 and b_q > 0.051 and a_v > 0.051 and abs(a_v - b_v) < 0.051:
            return True
    return False


def extract_opqty_qoh_direct_vision(
    file_bytes: bytes,
    filename: str,
    ext: str,
    *,
    reason: str = "opqty_qoh_stock_sales_headers",
) -> Optional[Dict[str, Any]]:
    """Vision for O.Stk/Qoh/Value/Age and OpQty/Qoh Stock & Sales Statement."""
    model = os.getenv("VISION_MODEL", "gemini-2.5-flash-lite").strip()
    max_attempts = _max_attempts()
    layout = "opqty_qoh_stock_sales"
    schema = "opqty_purqty_saleqty_qoh"
    _log(
        "STOCK_DIRECT_VISION_DECISION",
        filename=filename,
        detected_layout=layout,
        schema=schema,
        reason=f"STOCK_STATEMENT_VISION_FIRST:{reason}",
        gemini_called="pending",
        gemini_input="image_original",
        numeric_source="gemini_vision",
    )
    original_meta = {"normalized": False, "normalization_reason": None}
    candidates: List[Tuple[int, Dict[str, Any], Dict[str, Any]]] = []

    # Prefer one full-frame call first. Tall top/bottom halves often return an
    # empty top band (Gemini budget burned) and drop the first page of SKUs.
    result = _call_psr_vision(
        file_bytes,
        filename,
        ext,
        vision_bytes=file_bytes,
        image_meta=original_meta,
        candidate=1,
        model=model,
        route=layout,
        reason=reason,
        prompt=OPQTY_QOH_STOCK_VISION_PROMPT,
        layout_label="opqty_qoh_stock_sales",
        default_title="Stock & Sales Statement",
    )
    validation = (
        validate_stock_direct_vision(result, layout=layout)
        if result
        else {
            "ok": False,
            "soft_ok": False,
            "reasons": ["empty_vision"],
            "row_count": 0,
            "identity_score": -1,
        }
    )
    _log(
        "STOCK_DIRECT_VISION_VALIDATION",
        filename=filename,
        detected_layout=layout,
        schema=schema,
        reason=",".join(validation.get("reasons") or []) or "ok",
        gemini_called="true",
        row_count=validation.get("row_count"),
        identity_failure_count=validation.get("identity_fail_count"),
        validation_status=(
            "pass"
            if validation.get("soft_ok")
            else ("soft_fail" if validation.get("ok") else "fail")
        ),
        candidate=1,
        identity_score=validation.get("identity_score"),
    )
    if result and validation.get("ok"):
        extra = result.setdefault("totals", {}).setdefault("extra", {})
        extra["extraction_method"] = "opqty_qoh_stock_sales_vision"
        extra["layout"] = "opqty_qoh_stock_sales"
        _finalize_opqty_qoh_result(result, file_bytes)
        candidates.append(
            (int(validation.get("identity_score") or 0), result, validation)
        )
        row_n = len(result.get("line_items") or [])
        needs_value_repair = _ostk_qoh_needs_bottom_value_pass(result)
        # Dense Profitmaker sheets have ~60–90 SKUs; accept full-frame soft_ok
        # unless the last active Value cells were dropped/shifted.
        if (
            validation.get("soft_ok")
            and row_n >= 40
            and not needs_value_repair
        ):
            return result
        if (
            validation.get("soft_ok")
            and row_n >= 20
            and max_attempts < 2
            and not needs_value_repair
        ):
            return result

    first_seed = candidates[0][1] if candidates else result
    need_bottom_values = bool(candidates) and _ostk_qoh_needs_bottom_value_pass(
        first_seed
    )
    if max_attempts >= 2 and (
        not candidates
        or not (validation or {}).get("soft_ok")
        or len((result or {}).get("line_items") or []) < 40
        or need_bottom_values
    ):
        # Second slot: bottom-band supplement (footer SKUs) merged into full frame,
        # or a full-frame retry when the first pass was empty/weak.
        # Phone-portrait photos often skip generic vertical split; still force a
        # bottom crop when last Qoh rows dropped/shifted Value.
        vision2 = file_bytes
        reason2 = reason + "+retry"
        meta2 = original_meta
        try:
            from services.stock_vision_table import (
                _needs_vertical_split,
                _vertical_halves,
            )

            want_bottom = candidates and (
                _needs_vertical_split(file_bytes) or need_bottom_values
            )
            if want_bottom:
                halves = _vertical_halves(file_bytes) or []
                if len(halves) >= 2:
                    vision2 = halves[1]
                    reason2 = reason + (
                        "+bottom_band_value_repair"
                        if need_bottom_values
                        else "+bottom_band_merge"
                    )
                    meta2 = {
                        **original_meta,
                        "normalization_reason": "ostk_qoh_bottom_band",
                    }
                    logger.info(
                        "STOCK_OSTK_QOH_BOTTOM_BAND_MERGE file=%s reason=%s "
                        "value_repair=%s",
                        filename,
                        reason2,
                        need_bottom_values,
                    )
        except Exception:
            pass
        result2 = _call_psr_vision(
            file_bytes,
            filename,
            ext,
            vision_bytes=vision2,
            image_meta=meta2,
            candidate=2,
            model=model,
            route=layout,
            reason=reason2,
            prompt=OPQTY_QOH_STOCK_VISION_PROMPT,
            layout_label="opqty_qoh_stock_sales",
            default_title="Stock & Sales Statement",
        )
        if (
            result2
            and result2.get("line_items")
            and candidates
            and "bottom_band" in reason2
        ):
            result2 = _merge_opqty_qoh_halves(candidates[0][1], result2)
        validation2 = (
            validate_stock_direct_vision(result2, layout=layout)
            if result2
            else {
                "ok": False,
                "soft_ok": False,
                "reasons": ["empty_vision"],
                "row_count": 0,
                "identity_score": -1,
            }
        )
        if result2 and validation2.get("ok"):
            extra2 = result2.setdefault("totals", {}).setdefault("extra", {})
            extra2["extraction_method"] = "opqty_qoh_stock_sales_vision"
            extra2["layout"] = "opqty_qoh_stock_sales"
            _finalize_opqty_qoh_result(result2, file_bytes)
            candidates.append(
                (
                    int(validation2.get("identity_score") or 0),
                    result2,
                    validation2,
                )
            )
            if validation2.get("soft_ok"):
                return result2

    if not candidates:
        _log(
            "STOCK_DIRECT_VISION_FALLBACK",
            filename=filename,
            detected_layout=layout,
            schema=schema,
            reason="no_structurally_valid_vision",
            gemini_called="true",
            row_count=0,
        )
        return None
    candidates.sort(key=lambda row: row[0], reverse=True)
    best = candidates[0][1]
    _finalize_opqty_qoh_result(best, file_bytes)
    return best


def _op_qty_pur_sale_bal_bottom_band_retry_enabled() -> bool:
    """STOCK_OP_QTY_PUR_SALE_BAL_BOTTOM_BAND_RETRY — default OFF.

    When Vision left-shifts Op→Pur after a thick mid-table black rule, retry
    Gemini on the lower band and merge qty fields by product_code.
    """
    return os.getenv(
        "STOCK_OP_QTY_PUR_SALE_BAL_BOTTOM_BAND_RETRY", ""
    ).strip().lower() in {"1", "true", "yes", "on"}


def _op_qty_pur_sale_bal_qty_tuple(
    item: Dict[str, Any],
) -> Tuple[float, float, float, float]:
    def _f(key: str) -> float:
        try:
            return float(item.get(key) or 0)
        except (TypeError, ValueError):
            return 0.0

    return (
        _f("opening_qty"),
        _f("receipts_qty"),
        _f("sales_qty"),
        _f("closing_qty"),
    )


def _op_qty_pur_sale_bal_looks_left_shifted(item: Dict[str, Any]) -> bool:
    """True when Op was dropped to 0 and Op/Pur/Sale sit one column right."""
    op, rec, sale, cl = _op_qty_pur_sale_bal_qty_tuple(item)
    if op != 0 or rec <= 0:
        return False
    # Legitimate zero-open rows still balance Op+Pur-Sale≈Bal. A left-shift
    # parks Sale in Bal and usually breaks that check.
    if abs((op + rec - sale) - cl) <= 1.01:
        return False
    return True


def _op_qty_pur_sale_bal_left_shift_count(items: List[Any]) -> int:
    return sum(
        1
        for item in items
        if isinstance(item, dict) and _op_qty_pur_sale_bal_looks_left_shifted(item)
    )


def _op_qty_pur_sale_bal_missing_sale_count(items: List[Any]) -> int:
    """Rows where Sale was dropped (Op+Pur stay, Bal kept, Sale forced to 0)."""
    n = 0
    for item in items:
        if not isinstance(item, dict):
            continue
        op, rec, sale, cl = _op_qty_pur_sale_bal_qty_tuple(item)
        if sale != 0 or op <= 0 or cl <= 0:
            continue
        if abs((op + rec) - cl) > 1.01:
            n += 1
    return n


def _op_qty_pur_sale_bal_bottom_band_needed(items: List[Any]) -> Tuple[bool, str]:
    left = _op_qty_pur_sale_bal_left_shift_count(items)
    if left >= 1:
        return True, f"left_shift={left}"
    missing = _op_qty_pur_sale_bal_missing_sale_count(items)
    if missing >= 1:
        return True, f"missing_sale={missing}"
    pack_sus = 0
    for item in items:
        if not isinstance(item, dict):
            continue
        extra = item.get("extra") if isinstance(item.get("extra"), dict) else {}
        if extra.get("pack_as_opening_suspected"):
            pack_sus += 1
    if pack_sus >= 1:
        return True, f"pack_as_opening={pack_sus}"
    return False, "ok"


def _crop_op_qty_pur_sale_bal_bottom_band(
    file_bytes: bytes,
) -> Tuple[Optional[bytes], Dict[str, Any]]:
    """Crop from a thick mid/lower black rule (or bottom 42%) for lower SKUs."""
    meta: Dict[str, Any] = {
        "normalized": True,
        "normalization_reason": "op_qty_pur_sale_bal_bottom_band",
    }
    try:
        from PIL import Image

        with Image.open(io.BytesIO(file_bytes)) as raw:
            img = raw.convert("RGB")
            w, h = img.size
            gray = img.convert("L")
            band_y = None
            # Prefer a thick dark horizontal rule in the lower 55% of the page.
            y0 = int(h * 0.45)
            sample_xs = list(range(0, w, max(1, w // 80)))

            def _row_dark_frac(y: int) -> float:
                dark = 0
                for x in sample_xs:
                    if gray.getpixel((x, y)) < 40:
                        dark += 1
                return dark / float(len(sample_xs) or 1)

            y = y0
            while y < h - 4:
                if _row_dark_frac(y) < 0.55:
                    y += 1
                    continue
                span = 1
                while y + span < h and _row_dark_frac(y + span) >= 0.55:
                    span += 1
                if span >= 3:
                    band_y = y + span
                    meta["black_rule_y"] = y
                    meta["black_rule_span"] = span
                    break
                y += max(1, span)
            if band_y is None or band_y >= h - 40:
                band_y = int(h * 0.58)
                meta["black_rule_y"] = None
            crop = img.crop((0, max(0, band_y - 8), w, h))
            buf = io.BytesIO()
            crop.save(buf, format="JPEG", quality=92)
            meta["crop_y0"] = max(0, band_y - 8)
            meta["crop_height"] = crop.size[1]
            return buf.getvalue(), meta
    except Exception as exc:
        logger.info(
            "STOCK_OP_QTY_PUR_SALE_BAL_BOTTOM_BAND_CROP_FAIL err=%s",
            type(exc).__name__,
        )
        return None, {"normalized": False, "normalization_reason": None}


def _merge_op_qty_pur_sale_bal_bottom_band(
    primary: Dict[str, Any],
    bottom: Dict[str, Any],
) -> Tuple[Dict[str, Any], int]:
    """Overlay bottom-band qty fields onto left-shifted primary rows by code."""
    bottom_items = [
        it for it in (bottom.get("line_items") or []) if isinstance(it, dict)
    ]
    by_code: Dict[str, Dict[str, Any]] = {}
    for it in bottom_items:
        code = str(it.get("product_code") or "").strip()
        if code:
            by_code[code] = it
    merged = 0
    for item in primary.get("line_items") or []:
        if not isinstance(item, dict):
            continue
        code = str(item.get("product_code") or "").strip()
        src = by_code.get(code)
        if not src:
            continue
        sop, srec, ssale, scl = _op_qty_pur_sale_bal_qty_tuple(src)
        if _op_qty_pur_sale_bal_looks_left_shifted(src):
            # Bottom read still shifted — skip.
            continue
        if sop == 0 and srec == 0 and ssale == 0 and scl == 0:
            continue
        before = _op_qty_pur_sale_bal_qty_tuple(item)
        if before == (sop, srec, ssale, scl):
            continue
        # Bottom crop is authoritative for codes that appear there.
        item["opening_qty"] = sop
        item["receipts_qty"] = srec
        item["sales_qty"] = ssale
        item["closing_qty"] = scl
        extra = item.get("extra") if isinstance(item.get("extra"), dict) else {}
        extra = dict(extra)
        extra["op_qty_pur_sale_bal_bottom_band_merged"] = True
        extra["op_qty_pur_sale_bal_pre_merge_qty"] = {
            "opening_qty": before[0],
            "receipts_qty": before[1],
            "sales_qty": before[2],
            "closing_qty": before[3],
        }
        item["extra"] = extra
        merged += 1
        logger.info(
            "STOCK_OP_QTY_PUR_SALE_BAL_BOTTOM_BAND_MERGE code=%s "
            "before=%s after=%s",
            code,
            before,
            (sop, srec, ssale, scl),
        )
    return primary, merged


def _finalize_op_qty_pur_sale_bal(result: Dict[str, Any]) -> Dict[str, Any]:
    """Drop manufacturer banner rows and page-total money parked on SKUs."""
    totals = result.setdefault("totals", {})
    extra_t = totals.setdefault("extra", {})
    kept: List[Dict[str, Any]] = []
    bleed_cleared = 0
    for item in result.get("line_items") or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("product_name") or "").strip()
        if not name:
            continue
        if re.search(r"^\s*Manufacturer\b|\bHIMALAYA\s+DRUG\b", name, re.I) and not re.search(
            r"\b(SOAP|TAB|CAP|SYP|SYRUP|CREAM|DROP|OINT|POW)\b", name, re.I
        ):
            logger.info(
                "STOCK_OP_QTY_PUR_SALE_BAL_DROP_BANNER name=%s",
                name[:80],
            )
            continue
        # Page-scale Indian money totals must never sit on a product row.
        # Per-SKU closing_value can be tens of thousands; page totals are 1e5+.
        for money_key, floor in (
            ("sales_value", 50000.0),
            ("closing_value", 100000.0),
            ("opening_value", 100000.0),
        ):
            try:
                money = float(item.get(money_key) or 0)
            except (TypeError, ValueError):
                money = 0.0
            if money < floor:
                continue
            if money_key == "sales_value" and totals.get("sales_value") in (
                None,
                0,
                0.0,
            ):
                totals["sales_value"] = money
            if money_key == "closing_value" and totals.get("closing_value") in (
                None,
                0,
                0.0,
            ):
                totals["closing_value"] = money
            item[money_key] = None
            bleed_cleared += 1
            logger.info(
                "STOCK_OP_QTY_PUR_SALE_BAL_STRIP_HEADER_TOTAL "
                "product=%s field=%s value=%s",
                name[:60],
                money_key,
                money,
            )
        # Qty-only grid: do not keep invented per-row sales_value.
        if item.get("sales_value") in (0, 0.0):
            item["sales_value"] = None
        # Dual Bal: when Bal qty was parked in closing_value and closing_qty is
        # blank/0, move the printed whole number back to closing_qty.
        try:
            cl_q = float(item.get("closing_qty") or 0)
        except (TypeError, ValueError):
            cl_q = 0.0
        try:
            cl_v = float(item.get("closing_value") or 0)
        except (TypeError, ValueError):
            cl_v = 0.0
        if (
            cl_q == 0
            and cl_v > 0
            and cl_v < 10000
            and abs(cl_v - round(cl_v)) < 1e-6
        ):
            item["closing_qty"] = float(int(round(cl_v)))
            item["closing_value"] = None
            logger.info(
                "STOCK_OP_QTY_PUR_SALE_BAL_BAL_QTY_FROM_VALUE "
                "product=%s closing_qty=%s",
                name[:60],
                item["closing_qty"],
            )
        # Pack token (10S / 60S) must not become opening_qty.
        pack = str(item.get("packing") or "")
        pack_m = re.search(
            r"(\d+)\s*(?:S|TAB|CAP|ML|GM|G)\b",
            f"{pack} {name}",
            re.I,
        )
        try:
            op_f = float(item.get("opening_qty") or 0)
            rec_f = float(item.get("receipts_qty") or 0)
            sale_f = float(item.get("sales_qty") or 0)
            cl_f = float(item.get("closing_qty") or 0)
        except (TypeError, ValueError):
            op_f = rec_f = sale_f = cl_f = 0.0
        if (
            pack_m
            and abs(op_f - float(pack_m.group(1))) < 0.01
            and rec_f > 0
            and abs((op_f + rec_f - sale_f) - cl_f) > 1.01
            and abs((rec_f - sale_f) - cl_f) > 1.01
        ):
            # Shift left once: pack leaked into Op, real Op is in Pur, etc.
            # Only when the shifted row balances against printed closing.
            shifted_op, shifted_rec, shifted_sale = rec_f, sale_f, cl_f
            # closing unknown after shift — keep printed cl as sales only when
            # identity would require a fourth printed number we do not invent.
            logger.info(
                "STOCK_OP_QTY_PUR_SALE_BAL_PACK_AS_OPENING product=%s "
                "pack=%s opening=%s receipts=%s (not rewritten)",
                name[:60],
                pack_m.group(0),
                op_f,
                rec_f,
            )
            extra_i = item.get("extra") if isinstance(item.get("extra"), dict) else {}
            extra_i = dict(extra_i)
            extra_i["pack_as_opening_suspected"] = True
            extra_i["candidates"] = list(extra_i.get("candidates") or []) + [
                {
                    "opening_qty": shifted_op,
                    "receipts_qty": shifted_rec,
                    "sales_qty": shifted_sale,
                    "note": "pack_digit_opening_shift",
                }
            ]
            item["extra"] = extra_i
        # Identity gap: keep printed Bal; attach calculated closing as candidate only.
        if (
            (op_f or rec_f or sale_f or cl_f)
            and abs((op_f + rec_f - sale_f) - cl_f) > 1.01
        ):
            extra_i = item.get("extra") if isinstance(item.get("extra"), dict) else {}
            extra_i = dict(extra_i)
            cands = list(extra_i.get("candidates") or [])
            calc_cl = round(op_f + rec_f - sale_f, 2)
            cands.append(
                {
                    "closing_qty": calc_cl,
                    "note": "identity_gap_printed_bal_kept",
                }
            )
            extra_i["candidates"] = cands
            extra_i["identity_gap"] = True
            item["extra"] = extra_i
            logger.info(
                "STOCK_OP_QTY_PUR_SALE_BAL_IDENTITY_GAP product=%s "
                "printed_closing=%s calc_closing=%s",
                name[:60],
                cl_f,
                calc_cl,
            )
        kept.append(item)
    result["line_items"] = kept
    # Manufacturer band money is 1e5+; a product Bal qty (e.g. 83) must not
    # become totals.closing_value / sales_value when opening_value is page-scale.
    try:
        page_open = float(extra_t.get("opening_value") or 0)
    except (TypeError, ValueError):
        page_open = 0.0
    try:
        page_cv = float(totals.get("closing_value") or 0)
    except (TypeError, ValueError):
        page_cv = 0.0
    try:
        page_sv = float(totals.get("sales_value") or 0)
    except (TypeError, ValueError):
        page_sv = 0.0
    if page_open >= 100000 and 0 < page_cv < 10000:
        logger.info(
            "STOCK_OP_QTY_PUR_SALE_BAL_REJECT_TINY_TOTAL_CV value=%s",
            page_cv,
        )
        totals["closing_value"] = None
        extra_t["op_qty_pur_sale_bal_tiny_total_cv_rejected"] = page_cv
    if page_open >= 100000 and 0 < page_sv < 10000:
        logger.info(
            "STOCK_OP_QTY_PUR_SALE_BAL_REJECT_TINY_TOTAL_SV value=%s",
            page_sv,
        )
        totals["sales_value"] = None
        extra_t["op_qty_pur_sale_bal_tiny_total_sv_rejected"] = page_sv
    # Prefer manufacturer Sale total when only opening/purchase money was kept.
    if totals.get("sales_value") in (None, 0, 0.0) and page_open >= 100000:
        # Common printed Sale total on this family (~2.9e5); do not invent —
        # only promote when already present under extra aliases.
        for alias in ("sale_value", "sales_val", "sale_qty_value"):
            alias_v = extra_t.get(alias)
            try:
                alias_f = float(alias_v) if alias_v not in (None, "") else 0.0
            except (TypeError, ValueError):
                alias_f = 0.0
            if alias_f >= 100000:
                totals["sales_value"] = alias_f
                break
    extra_t["extraction_method"] = "op_qty_pur_sale_bal_vision"
    extra_t["layout"] = "op_qty_pur_sale_bal"
    if bleed_cleared:
        extra_t["op_qty_pur_sale_bal_header_total_stripped"] = bleed_cleared
    return result


def extract_op_qty_pur_sale_bal_direct_vision(
    file_bytes: bytes,
    filename: str,
    ext: str,
    *,
    reason: str = "op_qty_pur_sale_bal_headers",
) -> Optional[Dict[str, Any]]:
    """Vision for Op.Qty/Pur.Qty/Sale.Qty/Bal qty grids (Mfac SAS Summary)."""
    model = os.getenv("VISION_MODEL", "gemini-2.5-flash-lite").strip()
    max_attempts = _max_attempts()
    layout = "op_qty_pur_sale_bal"
    schema = "op_qty_pur_sale_bal"
    _log(
        "STOCK_DIRECT_VISION_DECISION",
        filename=filename,
        detected_layout=layout,
        schema=schema,
        reason=f"STOCK_STATEMENT_VISION_FIRST:{reason}",
        gemini_called="pending",
        gemini_input="image_original",
        numeric_source="gemini_vision",
    )
    original_meta = {"normalized": False, "normalization_reason": None}
    candidates: List[Tuple[int, Dict[str, Any], Dict[str, Any]]] = []

    result = _call_psr_vision(
        file_bytes,
        filename,
        ext,
        vision_bytes=file_bytes,
        image_meta=original_meta,
        candidate=1,
        model=model,
        route=layout,
        reason=reason,
        prompt=OP_QTY_PUR_SALE_BAL_VISION_PROMPT,
        layout_label="op_qty_pur_sale_bal",
        default_title="Mfac SAS Summary Report",
    )
    if result:
        result = _finalize_op_qty_pur_sale_bal(result)
    validation = (
        validate_stock_direct_vision(result, layout=layout)
        if result
        else {
            "ok": False,
            "soft_ok": False,
            "reasons": ["empty_vision"],
            "row_count": 0,
            "identity_score": -1,
        }
    )
    _log(
        "STOCK_DIRECT_VISION_VALIDATION",
        filename=filename,
        detected_layout=layout,
        schema=schema,
        reason=",".join(validation.get("reasons") or []) or "ok",
        gemini_called="true",
        row_count=validation.get("row_count"),
        identity_failure_count=validation.get("identity_fail_count"),
        validation_status=(
            "pass"
            if validation.get("soft_ok")
            else ("soft_fail" if validation.get("ok") else "fail")
        ),
        candidate=1,
        identity_score=validation.get("identity_score"),
    )
    if result and validation.get("ok"):
        extra = result.setdefault("totals", {}).setdefault("extra", {})
        extra["extraction_method"] = "op_qty_pur_sale_bal_vision"
        extra["layout"] = "op_qty_pur_sale_bal"
        candidates.append(
            (int(validation.get("identity_score") or 0), result, validation)
        )

    need_bottom, bottom_why = _op_qty_pur_sale_bal_bottom_band_needed(
        (result or {}).get("line_items") or []
    )
    want_bottom = (
        _op_qty_pur_sale_bal_bottom_band_retry_enabled()
        and max_attempts >= 2
        and result is not None
        and need_bottom
    )
    if want_bottom:
        bottom_bytes, bottom_meta = _crop_op_qty_pur_sale_bal_bottom_band(file_bytes)
        if bottom_bytes:
            logger.info(
                "STOCK_OP_QTY_PUR_SALE_BAL_BOTTOM_BAND_RETRY "
                "filename=%s reason=%s crop_y0=%s",
                filename,
                bottom_why,
                bottom_meta.get("crop_y0"),
            )
            result2 = _call_psr_vision(
                file_bytes,
                filename,
                ext,
                vision_bytes=bottom_bytes,
                image_meta=bottom_meta,
                candidate=2,
                model=model,
                route=layout,
                reason=reason + "+bottom_band",
                prompt=OP_QTY_PUR_SALE_BAL_VISION_PROMPT,
                layout_label="op_qty_pur_sale_bal",
                default_title="Mfac SAS Summary Report",
            )
            if result2:
                result2 = _finalize_op_qty_pur_sale_bal(result2)
                result, merged_n = _merge_op_qty_pur_sale_bal_bottom_band(
                    result, result2
                )
                if merged_n:
                    result = _finalize_op_qty_pur_sale_bal(result)
                extra_m = result.setdefault("totals", {}).setdefault("extra", {})
                extra_m["extraction_method"] = "op_qty_pur_sale_bal_vision"
                extra_m["layout"] = "op_qty_pur_sale_bal"
                extra_m["op_qty_pur_sale_bal_bottom_band_merged"] = merged_n
                extra_m["op_qty_pur_sale_bal_bottom_band_reason"] = bottom_why
                validation = validate_stock_direct_vision(result, layout=layout)
                candidates = [
                    (
                        int(validation.get("identity_score") or 0),
                        result,
                        validation,
                    )
                ]
                if validation.get("soft_ok") or merged_n > 0:
                    return result

    if result and validation.get("ok") and validation.get("soft_ok"):
        return result

    if max_attempts >= 2 and (not candidates or not (validation or {}).get("soft_ok")):
        # Skip a second full-page call when the bottom-band retry already ran.
        if not want_bottom:
            result2 = _call_psr_vision(
                file_bytes,
                filename,
                ext,
                vision_bytes=file_bytes,
                image_meta=original_meta,
                candidate=2,
                model=model,
                route=layout,
                reason=reason + "+retry",
                prompt=OP_QTY_PUR_SALE_BAL_VISION_PROMPT,
                layout_label="op_qty_pur_sale_bal",
                default_title="Mfac SAS Summary Report",
            )
            if result2:
                result2 = _finalize_op_qty_pur_sale_bal(result2)
            validation2 = (
                validate_stock_direct_vision(result2, layout=layout)
                if result2
                else {
                    "ok": False,
                    "soft_ok": False,
                    "reasons": ["empty_vision"],
                    "row_count": 0,
                    "identity_score": -1,
                }
            )
            if result2 and validation2.get("ok"):
                extra2 = result2.setdefault("totals", {}).setdefault("extra", {})
                extra2["extraction_method"] = "op_qty_pur_sale_bal_vision"
                extra2["layout"] = "op_qty_pur_sale_bal"
                candidates.append(
                    (int(validation2.get("identity_score") or 0), result2, validation2)
                )
                if validation2.get("soft_ok"):
                    return result2

    if not candidates:
        _log(
            "STOCK_DIRECT_VISION_FALLBACK",
            filename=filename,
            detected_layout=layout,
            schema=schema,
            reason="no_structurally_valid_vision",
            gemini_called="true",
            row_count=0,
        )
        return None
    candidates.sort(key=lambda row: row[0], reverse=True)
    return candidates[0][1]


def _parse_nss_qty_token(token: str) -> Optional[float]:
    """Parse Normal Stock Statement qty: 11,00 / 0.00 / (2.00) / -Nil-."""
    raw = (token or "").strip().replace("_", "")
    if not raw:
        return None
    if re.match(r"(?i)-?nil-?$", raw) or raw in {"-", "–", "—", ".", ".."}:
        return 0.0
    neg = False
    if raw.startswith("(") and raw.endswith(")"):
        neg = True
        raw = raw[1:-1].strip()
    # European decimal: 11,00 → 11.00 (not thousands).
    if re.fullmatch(r"-?\d+,\d{1,2}", raw):
        raw = raw.replace(",", ".")
    else:
        raw = raw.replace(",", "")
    try:
        value = float(raw)
    except ValueError:
        return None
    return -value if neg else value


def _parse_normal_stock_open_recp_ocr_text(text: str) -> List[Dict[str, Any]]:
    """Parse Excel Normal Stock Statement rows from page OCR."""
    items: List[Dict[str, Any]] = []
    qty_token = re.compile(r"\(?-?\d+[.,]\d{2}\)?")
    pack_re = re.compile(
        r"(\d+\s*[*\"'′°xX]\s*\d+\s*[A-Za-z.]{2,6}|\d+\s*(?:TAB|CAP|ML|GM|mi)\b)",
        re.I,
    )
    date_re = re.compile(
        r"\b\d{1,2}-[A-Za-z]{3}\b|\b[A-Za-z]{3}-\d{2}\b|-Nil-",
        re.I,
    )
    for raw_line in (text or "").splitlines():
        line = re.sub(r"\s+", " ", raw_line or "").strip()
        if len(line) < 8:
            continue
        if re.search(
            r"Normal\s+Stock|Product\s+Name|Open\s*Stk|DIGANT|Not saved|PARSHAVA|"
            r"HIMALAYA|Company\s*:|Early\s*Expiry|\bRemks\b",
            line,
            re.I,
        ):
            continue
        # Drop leading excel row / serial noise: "8 1 ARJUNA" / "103 BONNISAN"
        work = re.sub(r"^(?:\d+\s+){1,2}", "", line).strip()
        qtys = qty_token.findall(work)
        if len(qtys) < 5:
            continue
        # Last 5 qty tokens before optional date are Open Recp Total Sales Clsg.
        open_q = _parse_nss_qty_token(qtys[-5])
        recp_q = _parse_nss_qty_token(qtys[-4])
        sales_q = _parse_nss_qty_token(qtys[-2])
        clsg_q = _parse_nss_qty_token(qtys[-1])
        if None in (open_q, recp_q, sales_q, clsg_q):
            continue
        # Text before first of those 5 qty tokens.
        first_qty = qtys[-5]
        idx = work.rfind(first_qty)
        # Prefer leftmost occurrence of the 5-token run.
        search_from = 0
        run_idx = -1
        while True:
            pos = work.find(first_qty, search_from)
            if pos < 0:
                break
            # Verify the following tokens appear in order after pos.
            tail = work[pos:]
            found = qty_token.findall(tail)
            if len(found) >= 5 and found[:5] == qtys[-5:]:
                run_idx = pos
                break
            search_from = pos + 1
        if run_idx < 0:
            run_idx = idx if idx >= 0 else 0
        left = work[:run_idx].strip(" -|_")
        left = date_re.sub("", left).strip()
        pack_m = pack_re.search(left)
        packing = None
        product = left
        if pack_m:
            packing = re.sub(r"\s+", " ", pack_m.group(1)).strip()
            product = left[: pack_m.start()].strip(" -|_")
        product = re.sub(r"^\d+\s+", "", product).strip(" -|_")
        product = re.sub(r"^[\d@#|.]+\s*", "", product).strip(" -|_")
        product = re.sub(r"\s+", " ", product)
        if len(product) < 3 or not re.search(r"[A-Za-z]{3,}", product):
            continue
        if re.search(r"^Total\b|^Done$|Division", product, re.I):
            continue
        items.append(
            {
                "product_name": product.upper() if product == product.title() else product,
                "packing": packing,
                "opening_qty": open_q,
                "receipts_qty": recp_q,
                "sales_qty": sales_q,
                "closing_qty": clsg_q,
                "sales_value": 0.0,
                "closing_value": None,
                "extra": {
                    "field_source": {
                        "opening_qty": "ocr",
                        "receipts_qty": "ocr",
                        "sales_qty": "ocr",
                        "closing_qty": "ocr",
                    },
                    "total_stock": _parse_nss_qty_token(qtys[-3]),
                },
            }
        )
    # Normalize product_name casing lightly.
    for item in items:
        name = str(item.get("product_name") or "").strip()
        item["product_name"] = re.sub(r"\s+", " ", name)
    return items


def _extract_normal_stock_open_recp_from_ocr(
    file_bytes: bytes,
    filename: str,
    ext: str,
) -> Optional[Dict[str, Any]]:
    """OCR-first path for Excel Normal Stock Statement phone screenshots."""
    from services.sales_statement_extractor import (
        _a2z_tesseract,
        _apply_stock_identity_validation,
        _ensure_stock_qty_value_fields,
        empty_result,
    )
    from services.stock_ocr_policy import stock_ocr_context

    try:
        from PIL import Image, ImageOps
    except ImportError:
        return None
    try:
        with Image.open(io.BytesIO(file_bytes)) as raw:
            img = ImageOps.exif_transpose(raw).convert("RGB")
    except Exception:
        return None
    tess = _a2z_tesseract()
    digest = hashlib.sha256(file_bytes).hexdigest()[:10]
    with stock_ocr_context(region=f"normal_stock_open_recp_{digest}", page="1"):
        text = tess.image_to_string(img, config="--psm 6") or ""
    rows = _parse_normal_stock_open_recp_ocr_text(text)
    if len(rows) < 5:
        _log(
            "STOCK_DIRECT_VISION_FALLBACK",
            filename=filename,
            detected_layout="normal_stock_open_recp",
            schema="open_recp_sales_clsg",
            reason="normal_stock_ocr_too_few_rows",
            row_count=len(rows),
            gemini_called="false",
            numeric_source="ocr_geometry",
        )
        return None
    result = empty_result(filename, (ext or ".png").lstrip(".") or "png")
    result["report_title"] = "Normal Stock Statement"
    result["line_items"] = rows
    # Header fields from OCR text.
    stockist_m = re.search(
        r"(?i)\b((?:[A-Z][A-Z0-9&.\' -]{0,30})?(?:PHARMA|DISTRIBUTORS|AGENCY))\b",
        text,
    )
    for m in re.finditer(
        r"(?im)^(?:\d+\s+)?([A-Z][A-Z0-9\s.&'-]{2,40}(?:PHARMA|DISTRIBUTORS|AGENCY))\s*$",
        text,
    ):
        name = re.sub(r"\s+", " ", m.group(1)).strip(" .")
        if not re.search(
            r"HIMALAYA|DIGANT|STOCK\s+STATEMENT|PRODUCT\s+NAME|Not\s+saved",
            name,
            re.I,
        ):
            result["stockist_name"] = name
            break
    if not result.get("stockist_name") and stockist_m:
        cand = re.sub(r"\s+", " ", stockist_m.group(1)).strip(" .")
        if len(cand) >= 6 and not re.search(r"HIMALAYA|DIGANT|Not\s+saved", cand, re.I):
            result["stockist_name"] = cand
    company_m = re.search(
        r"(?i)(?:Company|apany)\s*:\s*(HIMALAYA(?:\s+[A-Z]{2,}[A-Z0-9.&'-]*){0,4})",
        text,
    )
    if company_m:
        result["company_name"] = re.sub(r"\s+", " ", company_m.group(1)).strip(" .")
    period_m = re.search(
        r"(?i)From\s+(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})\s+To\s+(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})",
        text,
    )
    if period_m:
        def _d(s: str) -> Optional[str]:
            parts = re.split(r"[/-]", s)
            if len(parts) != 3:
                return None
            d, mth, y = parts
            if len(y) == 2:
                y = "20" + y
            try:
                return f"{int(y):04d}-{int(mth):02d}-{int(d):02d}"
            except ValueError:
                return None

        result["period_from"] = _d(period_m.group(1))
        result["period_to"] = _d(period_m.group(2))
    result = _apply_stock_identity_validation(result)
    result = _ensure_stock_qty_value_fields(result)
    extra = result.setdefault("totals", {}).setdefault("extra", {})
    extra["extraction_method"] = "normal_stock_open_recp_ocr"
    extra["layout"] = "normal_stock_open_recp"
    extra["numeric_source"] = "ocr_geometry"
    extra["gemini_input_kind"] = "none"
    lock_stock_vision_result(
        result,
        extraction_method="normal_stock_open_recp_ocr",
        layout="normal_stock_open_recp",
        psr_column_layout="open_recp_clsg",
        stock_identity_kind="opening_receipts_sales_closing",
        numeric_source="ocr_geometry",
        gemini_input_kind="none",
        early_vision_reason="normal_stock_open_recp_ocr",
        stock_direct_vision_route="normal_stock_open_recp",
        stock_direct_vision_candidate=1,
    )
    validation = validate_stock_direct_vision(result, layout="normal_stock_open_recp")
    _log(
        "STOCK_DIRECT_VISION_VALIDATION",
        filename=filename,
        detected_layout="normal_stock_open_recp",
        schema="open_recp_sales_clsg",
        reason=",".join(validation.get("reasons") or []) or "ok",
        gemini_called="false",
        row_count=validation.get("row_count"),
        identity_failure_count=validation.get("identity_fail_count"),
        validation_status=(
            "pass"
            if validation.get("soft_ok")
            else ("soft_fail" if validation.get("ok") else "fail")
        ),
        candidate=1,
        identity_score=validation.get("identity_score"),
        numeric_source="ocr_geometry",
    )
    if not validation.get("ok"):
        return None
    _log(
        "STOCK_DIRECT_VISION_END",
        filename=filename,
        route="normal_stock_open_recp",
        reason="normal_stock_open_recp_ocr",
        gemini_model="none",
        candidate=1,
        status="ok",
        row_count=len(rows),
        numeric_source="ocr_geometry",
    )
    return result


def extract_normal_stock_open_recp_direct_vision(
    file_bytes: bytes,
    filename: str,
    ext: str,
    *,
    reason: str = "normal_stock_open_recp_headers",
) -> Optional[Dict[str, Any]]:
    """OCR-first (then Vision) for Excel Normal Stock Statement Open/Recp/Clsg.

    Phone screenshots of DIGANT/Excel grids are readable via Tesseract; Gemini
    flash-lite often returns empty line_items on these tall crops.
    """
    ocr_result = _extract_normal_stock_open_recp_from_ocr(file_bytes, filename, ext)
    if ocr_result and (ocr_result.get("line_items") or []):
        return ocr_result

    model = os.getenv("VISION_MODEL", "gemini-2.5-flash-lite").strip()
    max_attempts = _max_attempts()
    layout = "normal_stock_open_recp"
    schema = "open_recp_sales_clsg"
    _log(
        "STOCK_DIRECT_VISION_DECISION",
        filename=filename,
        detected_layout=layout,
        schema=schema,
        reason=f"STOCK_STATEMENT_VISION_FIRST:{reason}",
        gemini_called="pending",
        gemini_input="image_original",
        numeric_source="gemini_vision",
    )
    original_meta = {"normalized": False, "normalization_reason": None}
    candidates: List[Tuple[int, Dict[str, Any], Dict[str, Any]]] = []

    result = _call_psr_vision(
        file_bytes,
        filename,
        ext,
        vision_bytes=file_bytes,
        image_meta=original_meta,
        candidate=1,
        model=model,
        route=layout,
        reason=reason,
        prompt=NORMAL_STOCK_OPEN_RECP_VISION_PROMPT,
        layout_label="normal_stock_open_recp",
        default_title="Normal Stock Statement",
    )
    validation = (
        validate_stock_direct_vision(result, layout=layout)
        if result
        else {
            "ok": False,
            "soft_ok": False,
            "reasons": ["empty_vision"],
            "row_count": 0,
            "identity_score": -1,
        }
    )
    _log(
        "STOCK_DIRECT_VISION_VALIDATION",
        filename=filename,
        detected_layout=layout,
        schema=schema,
        reason=",".join(validation.get("reasons") or []) or "ok",
        gemini_called="true",
        row_count=validation.get("row_count"),
        identity_failure_count=validation.get("identity_fail_count"),
        validation_status=(
            "pass"
            if validation.get("soft_ok")
            else ("soft_fail" if validation.get("ok") else "fail")
        ),
        candidate=1,
        identity_score=validation.get("identity_score"),
    )
    if result and validation.get("ok"):
        extra = result.setdefault("totals", {}).setdefault("extra", {})
        extra["extraction_method"] = "normal_stock_open_recp_vision"
        extra["layout"] = "normal_stock_open_recp"
        candidates.append(
            (int(validation.get("identity_score") or 0), result, validation)
        )
        if validation.get("soft_ok"):
            return result

    if max_attempts >= 2 and (not candidates or not (validation or {}).get("soft_ok")):
        result2 = _call_psr_vision(
            file_bytes,
            filename,
            ext,
            vision_bytes=file_bytes,
            image_meta=original_meta,
            candidate=2,
            model=model,
            route=layout,
            reason=reason + "+retry",
            prompt=NORMAL_STOCK_OPEN_RECP_VISION_PROMPT,
            layout_label="normal_stock_open_recp",
            default_title="Normal Stock Statement",
        )
        validation2 = (
            validate_stock_direct_vision(result2, layout=layout)
            if result2
            else {
                "ok": False,
                "soft_ok": False,
                "reasons": ["empty_vision"],
                "row_count": 0,
                "identity_score": -1,
            }
        )
        if result2 and validation2.get("ok"):
            extra2 = result2.setdefault("totals", {}).setdefault("extra", {})
            extra2["extraction_method"] = "normal_stock_open_recp_vision"
            extra2["layout"] = "normal_stock_open_recp"
            candidates.append(
                (int(validation2.get("identity_score") or 0), result2, validation2)
            )
            if validation2.get("soft_ok"):
                return result2

    if not candidates:
        _log(
            "STOCK_DIRECT_VISION_FALLBACK",
            filename=filename,
            detected_layout=layout,
            schema=schema,
            reason="no_structurally_valid_vision",
            gemini_called="true",
            row_count=0,
        )
        return None
    candidates.sort(key=lambda row: row[0], reverse=True)
    return candidates[0][1]


def extract_saleret_direct_vision(
    file_bytes: bytes,
    filename: str,
    ext: str,
    *,
    reason: str = "forced_saleret_route",
) -> Optional[Dict[str, Any]]:
    """Original-image SaleRet/ClosStock Vision without re-classifying peek text."""
    model = os.getenv("VISION_MODEL", "gemini-2.5-flash-lite").strip()
    max_attempts = _max_attempts()
    layout = "product_stock_report_saleret"
    schema = "saleret_closstock"
    _log(
        "STOCK_DIRECT_VISION_DECISION",
        filename=filename,
        detected_layout=layout,
        schema=schema,
        reason=f"STOCK_STATEMENT_VISION_FIRST:{reason}",
        gemini_called="pending",
        gemini_input="image_original",
        numeric_source="gemini_vision",
    )
    original_meta = {"normalized": False, "normalization_reason": None}
    candidates: List[Tuple[int, Dict[str, Any], Dict[str, Any]]] = []

    result = _call_psr_vision(
        file_bytes,
        filename,
        ext,
        vision_bytes=file_bytes,
        image_meta=original_meta,
        candidate=1,
        model=model,
        route=layout,
        reason=reason,
    )
    validation = validate_stock_direct_vision(result, layout=layout) if result else {
        "ok": False,
        "soft_ok": False,
        "reasons": ["empty_vision"],
        "row_count": 0,
        "identity_score": -1,
    }
    _log(
        "STOCK_DIRECT_VISION_VALIDATION",
        filename=filename,
        detected_layout=layout,
        schema=schema,
        reason=",".join(validation.get("reasons") or []) or "ok",
        gemini_called="true",
        row_count=validation.get("row_count"),
        identity_failure_count=validation.get("identity_fail_count"),
        validation_status=(
            "pass"
            if validation.get("soft_ok")
            else ("soft_fail" if validation.get("ok") else "fail")
        ),
        candidate=1,
        identity_score=validation.get("identity_score"),
        gemini_input="image_original",
        numeric_source="gemini_vision",
    )
    if result and validation.get("ok"):
        candidates.append(
            (int(validation.get("identity_score") or 0), result, validation)
        )
        if validation.get("soft_ok"):
            return result
        _log(
            "STOCK_DIRECT_VISION_VALIDATION_FAILED",
            filename=filename,
            detected_layout=layout,
            schema=schema,
            reason=",".join(validation.get("soft_reasons") or validation.get("reasons") or []),
            row_count=validation.get("row_count"),
            candidate=1,
        )
    elif not validation.get("ok"):
        _log(
            "STOCK_DIRECT_VISION_VALIDATION_FAILED",
            filename=filename,
            detected_layout=layout,
            schema=schema,
            reason=",".join(validation.get("reasons") or []),
            row_count=validation.get("row_count"),
            candidate=1,
        )

    if max_attempts >= 2:
        try:
            from services.sales_statement_extractor import _psr_denoise_digit_overlines

            denoised_bytes, denoised_meta = _psr_denoise_digit_overlines(file_bytes)
        except Exception:
            denoised_bytes, denoised_meta = file_bytes, {"normalized": False}
        if denoised_meta.get("normalized"):
            second = _call_psr_vision(
                file_bytes,
                filename,
                ext,
                vision_bytes=denoised_bytes,
                image_meta=denoised_meta,
                candidate=2,
                model=model,
                route=layout,
                reason=reason + "+overline_denoise",
            )
            validation2 = (
                validate_stock_direct_vision(second, layout=layout)
                if second
                else {
                    "ok": False,
                    "soft_ok": False,
                    "reasons": ["empty_vision"],
                    "row_count": 0,
                    "identity_score": -1,
                }
            )
            _log(
                "STOCK_DIRECT_VISION_VALIDATION",
                filename=filename,
                detected_layout=layout,
                schema=schema,
                reason=",".join(validation2.get("reasons") or []) or "ok",
                gemini_called="true",
                row_count=validation2.get("row_count"),
                validation_status=(
                    "pass"
                    if validation2.get("soft_ok")
                    else ("soft_fail" if validation2.get("ok") else "fail")
                ),
                candidate=2,
                identity_score=validation2.get("identity_score"),
            )
            if second and validation2.get("ok"):
                candidates.append(
                    (int(validation2.get("identity_score") or 0), second, validation2)
                )
                if not validation2.get("soft_ok"):
                    _log(
                        "STOCK_DIRECT_VISION_VALIDATION_FAILED",
                        filename=filename,
                        detected_layout=layout,
                        schema=schema,
                        reason=",".join(
                            validation2.get("soft_reasons")
                            or validation2.get("reasons")
                            or []
                        ),
                        row_count=validation2.get("row_count"),
                        candidate=2,
                    )

    if not candidates:
        _log(
            "STOCK_DIRECT_VISION_FALLBACK",
            filename=filename,
            detected_layout=layout,
            schema=schema,
            reason="no_structurally_valid_vision",
            gemini_called="true",
            row_count=0,
        )
        return None

    candidates.sort(key=lambda row: row[0], reverse=True)
    best_score, best, best_val = candidates[0]
    _log(
        "STOCK_DIRECT_VISION_RESULT",
        filename=filename,
        detected_layout=layout,
        schema=schema,
        reason=reason,
        gemini_called="true",
        row_count=best_val.get("row_count"),
        validation_status="pass" if best_val.get("soft_ok") else "soft_fail_accepted",
        identity_score=best_score,
        candidate=best.get("totals", {}).get("extra", {}).get(
            "stock_direct_vision_candidate"
        ),
    )
    return best


def try_stock_direct_vision(
    file_bytes: bytes,
    filename: str,
    ext: str,
    peek_text: str = "",
) -> Optional[Dict[str, Any]]:
    """Classify → original-image Gemini (max 2) → validate → lock.

    Returns a locked result or None (caller keeps OCR/fallback path).
    """
    hw = detect_stock_handwriting_signals(file_bytes, peek_text, filename)
    layout_peek = peek_text or ""
    # Filename-only ZL/ZA classify needs a layout header peek so Product-wise
    # sheets / Op.Qty-Op.Val STOCK AND SALES are not forced into Medica/SaleRet.
    if not layout_peek and re.search(r"_ZL_\d+|_ZA_\d+", filename or "", re.I):
        layout_peek = _layout_header_text(file_bytes)
    header_from_hw = str(hw.get("header_text") or "")
    # Only merge layout-header OCR when peek alone would fall through to a
    # filename_* heuristic (otherwise garbled blue-header text can steal
    # a correct OPSTK / SaleRet / Product-wise classification).
    tentative = classify_stock_direct_vision(
        layout_peek or peek_text, filename, handwriting=hw
    )
    needs_header_disambiguation = bool(
        tentative and "filename_" in str(tentative.get("reason") or "")
    )
    if needs_header_disambiguation and header_from_hw and header_from_hw not in layout_peek:
        layout_peek = f"{layout_peek}\n{header_from_hw}".strip()
    if (
        needs_header_disambiguation
        and not _has_stock_sales_op_qty_val_headers(layout_peek)
        and not _has_product_wise_stock_headers(layout_peek)
        and not _has_saleret_headers(layout_peek)
        and not _has_opqty_qoh_headers(layout_peek)
        and not _has_normal_stock_open_recp_headers(layout_peek)
        and not _has_sales_stock_opbal_issue_headers(layout_peek)
        and not _has_ssa_opening_receipt_issue_closing_headers(layout_peek)
        and re.search(r"_ZA_\d+", filename or "", re.I)
    ):
        # Blue Op.Qty/Op.Val headers need a dedicated BW OCR region (not the
        # shared layout_header cache) so SaleRet ZA files stay SaleRet.
        opval_header = _layout_header_text_op_qty_val(file_bytes)
        if opval_header:
            layout_peek = f"{layout_peek}\n{opval_header}".strip()
        # STOCK & SALES ANALYSIS Opening/Issue/Closing often survives in a tall
        # header band when the short ZA peek is garbled.
        if (
            _ssa_issue_closing_beats_filename_za_enabled()
            and not _has_ssa_opening_receipt_issue_closing_headers(layout_peek)
        ):
            ssa_header = _layout_header_text(file_bytes)
            if ssa_header and ssa_header not in layout_peek:
                layout_peek = f"{layout_peek}\n{ssa_header}".strip()
    if (
        re.search(r"_ZL_\d+", filename or "", re.I)
        and not _has_product_wise_stock_headers(layout_peek)
        and not (
            re.search(r"\bOPSTK\b", layout_peek or "", re.I)
            and re.search(r"\bPURCH\b", layout_peek or "", re.I)
        )
        and (
            needs_header_disambiguation
            or _has_saleret_headers(layout_peek)
            or not (layout_peek or "").strip()
        )
    ):
        # Fresh ZL header OCR: layout_header is often poisoned by a prior ZA
        # SaleRet peek via region_shared_reuse. Skip when peek is already a
        # clear Medica OPSTK/PURCH signal (do not cache Medica into this region).
        zl_header = _layout_header_text_zl(file_bytes)
        if zl_header:
            layout_peek = f"{layout_peek}\n{zl_header}".strip()
        if not _has_product_wise_stock_headers(layout_peek):
            # Taller band fallback (dedicated region).
            try:
                from PIL import Image, ImageOps
                from services.sales_statement_extractor import (
                    _a2z_tesseract,
                    _pil_jpeg_bytes,
                )
                from services.stock_ocr_policy import stock_ocr_context

                with Image.open(io.BytesIO(file_bytes)) as raw:
                    img = ImageOps.exif_transpose(raw).convert("RGB")
                    width, height = img.size
                    band = img.crop((0, 0, width, max(40, int(height * 0.22))))
                    band.thumbnail((1400, 900))
                    tess = _a2z_tesseract()
                    digest = hashlib.sha256(file_bytes).hexdigest()[:10]
                    with stock_ocr_context(
                        region=f"layout_header_zl_taller_{digest}", page="1"
                    ):
                        taller = (
                            tess.image_to_string(
                                Image.open(io.BytesIO(_pil_jpeg_bytes(band))),
                                config="--psm 6",
                            )
                            or ""
                        )
                    if taller:
                        layout_peek = f"{layout_peek}\n{taller}".strip()
            except Exception:
                pass

    # Recover OPSTK|OPV|STOCKV when peek already hints at that grid but is
    # incomplete — so STOCK AND SALES STATEMENT is not stolen by Code/Item Vision.
    # Also probe when Stock and Sales Detail title is present but Op.Qty OCR failed.
    _opstk_hint = layout_peek or peek_text or ""
    if not _has_stock_sales_op_qty_val_headers(_opstk_hint) and (
        _looks_like_stock_sales_detail_report_title(_opstk_hint)
        or re.search(
            r"STOCK\s+AND\s+SALES|Stock\s+and\s+S\s*ales?\s+Detail|"
            r"\bOPSTK\b|\bOPV\b|\bPURCV\b|\bSALEV\b|\bSTOCKV\b",
            _opstk_hint,
            re.I,
        )
    ):
        opval_probe = _layout_header_text_op_qty_val(file_bytes)
        if opval_probe and opval_probe not in (layout_peek or ""):
            layout_peek = f"{layout_peek}\n{opval_probe}".strip()
            logger.info(
                "STOCK_DIRECT_VISION_OPSTK_OPV_HEADER_PROBE chars=%s file=%s",
                len(opval_probe),
                filename,
            )
    # Phone Detail photos: colour header OCR often has the title when BW
    # Op.Qty probe is blank — still enough to route once title-route is on.
    if (
        not _has_stock_sales_op_qty_val_headers(layout_peek or peek_text or "")
        and _stock_sales_detail_title_route_enabled()
    ):
        detail_probe = _layout_header_text(file_bytes)
        if detail_probe and detail_probe not in (layout_peek or ""):
            layout_peek = f"{layout_peek}\n{detail_probe}".strip()
            logger.info(
                "STOCK_SALES_DETAIL_TITLE_HEADER_PROBE chars=%s file=%s",
                len(detail_probe),
                filename,
            )

    decision = classify_stock_direct_vision(
        layout_peek or peek_text, filename, handwriting=hw
    )
    if not decision:
        return None

    # Closing Stock QTY|VALUE (+ L.M.S / PRODUCT NAME & PACKING): do not lock
    # SSA dump or SaleRet from filename _ZA_ — STOCK_VISION_TABLE owns this grid.
    if (
        _closing_qty_value_grid_beats_ssa_enabled()
        and _has_closing_stock_qty_value_grid_headers(layout_peek or peek_text or "")
    ):
        _log(
            "STOCK_DIRECT_VISION_FALLBACK",
            filename=filename,
            detected_layout=decision.get("layout"),
            schema=decision.get("schema"),
            reason="closing_qty_value_grid_defer_vision_table",
            peek_chars=len(layout_peek or peek_text or ""),
        )
        return None

    layout = decision["layout"]
    reason = decision["reason"]
    schema = decision["schema"]
    handwritten = str(
        decision.get("handwritten") or hw.get("handwritten") or "false"
    )
    _log(
        "STOCK_DIRECT_VISION_DECISION",
        filename=filename,
        detected_layout=layout,
        schema=schema,
        reason=reason,
        gemini_called="pending",
        peek_chars=len(layout_peek or peek_text or ""),
        peek_preview=re.sub(
            r"\s+", " ", (layout_peek or peek_text or "")[:180]
        ).strip()
        or None,
        handwritten=handwritten,
        image_original="true",
        numeric_source="gemini_vision",
    )

    model = os.getenv("VISION_MODEL", "gemini-2.5-flash-lite").strip()
    max_attempts = _max_attempts()

    if layout in {"handwritten_order_form", "handwritten_stock_statement"}:
        return extract_handwritten_stock_direct_vision(
            file_bytes,
            filename,
            ext,
            reason=reason,
            layout=layout,
            handwritten=handwritten if handwritten != "false" else "mixed",
        )

    if layout == "product_wise_stock_statement":
        started = time.time()
        _log(
            "STOCK_DIRECT_VISION_START",
            filename=filename,
            route="product_wise_stock_statement",
            reason=reason,
            gemini_model="none",
            candidate=1,
            byte_size=len(file_bytes),
            numeric_source="ocr_geometry",
        )
        try:
            from services.sales_statement_extractor import (
                _extract_product_wise_stock_statement_image,
            )

            pwss = _extract_product_wise_stock_statement_image(
                file_bytes,
                filename,
                ext,
                peek_text=layout_peek or peek_text or "",
            )
        except Exception as exc:
            _log(
                "STOCK_DIRECT_VISION_FALLBACK",
                filename=filename,
                detected_layout=layout,
                schema=schema,
                reason="product_wise_image_error",
                error_type=type(exc).__name__,
            )
            return None
        elapsed_ms = int((time.time() - started) * 1000)
        if not pwss or not pwss.get("line_items"):
            _log(
                "STOCK_DIRECT_VISION_FALLBACK",
                filename=filename,
                detected_layout=layout,
                schema=schema,
                reason="product_wise_image_empty",
                elapsed_ms=elapsed_ms,
                row_count=0,
            )
            return None
        # Lock so Medica/Gemini post-pass cannot overwrite column geometry.
        lock_stock_vision_result(
            pwss,
            early_vision_reason="product_wise_stock_statement_image",
            stock_direct_vision_route="product_wise_stock_statement",
            stock_direct_vision_candidate=1,
            numeric_source="ocr_geometry",
            gemini_input_kind="none",
            extraction_method=(
                ((pwss.get("totals") or {}).get("extra") or {}).get(
                    "extraction_method"
                )
                or "product_wise_stock_statement_image"
            ),
        )
        _log(
            "STOCK_DIRECT_VISION_END",
            filename=filename,
            route="product_wise_stock_statement",
            reason=reason,
            elapsed_ms=elapsed_ms,
            status="ok",
            row_count=len(pwss.get("line_items") or []),
            numeric_source="ocr_geometry",
        )
        _log(
            "STOCK_VISION_FINAL",
            filename=filename,
            handwritten="false",
            gemini_calls=0,
            line_items=len(pwss.get("line_items") or []),
            numeric_source="ocr_geometry",
            route="product_wise_stock_statement",
        )
        return pwss

    if layout == "medica_opstk":
        started = time.time()
        _log(
            "STOCK_DIRECT_VISION_START",
            filename=filename,
            route="medica_opstk",
            reason=reason,
            gemini_model=model,
            candidate=1,
            byte_size=len(file_bytes),
        )
        try:
            from services.sales_statement_extractor import (
                _extract_medica_stock_statement_vision,
            )

            medica = _extract_medica_stock_statement_vision(file_bytes, filename, ext)
        except Exception as exc:
            _log(
                "STOCK_DIRECT_VISION_FALLBACK",
                filename=filename,
                detected_layout=layout,
                schema=schema,
                reason="medica_vision_error",
                gemini_called="true",
                elapsed_ms=int((time.time() - started) * 1000),
                error_type=type(exc).__name__,
            )
            return None
        elapsed_ms = int((time.time() - started) * 1000)
        if not medica or not medica.get("line_items"):
            _log(
                "STOCK_DIRECT_VISION_FALLBACK",
                filename=filename,
                detected_layout=layout,
                schema=schema,
                reason="medica_vision_empty",
                gemini_called="true",
                elapsed_ms=elapsed_ms,
                row_count=0,
            )
            return None
        validation = validate_stock_direct_vision(medica, layout="medica_opstk")
        _log(
            "STOCK_DIRECT_VISION_VALIDATION",
            filename=filename,
            detected_layout=layout,
            schema=schema,
            reason=",".join(validation.get("reasons") or []) or "ok",
            gemini_called="true",
            elapsed_ms=elapsed_ms,
            row_count=validation.get("row_count"),
            validation_status="pass" if validation.get("ok") else "fail",
        )
        if not validation.get("ok"):
            _log(
                "STOCK_DIRECT_VISION_VALIDATION_FAILED",
                filename=filename,
                detected_layout=layout,
                schema=schema,
                reason=",".join(validation.get("reasons") or []),
                row_count=validation.get("row_count"),
            )
            _log(
                "STOCK_DIRECT_VISION_FALLBACK",
                filename=filename,
                detected_layout=layout,
                schema=schema,
                reason="validation_failed",
                gemini_called="true",
                elapsed_ms=elapsed_ms,
                row_count=validation.get("row_count"),
            )
            return None
        lock_stock_vision_result(
            medica,
            early_vision_reason="stock_direct_vision_medica",
            stock_direct_vision_route="medica_opstk",
            stock_direct_vision_candidate=1,
            gemini_input={
                "original_sha256": hashlib.sha256(file_bytes).hexdigest(),
                "gemini_input_sha256": hashlib.sha256(file_bytes).hexdigest(),
                "normalized": False,
                "normalization_reason": "medica_statement_strips",
                "byte_length": len(file_bytes),
            },
        )
        _log(
            "STOCK_DIRECT_VISION_END",
            filename=filename,
            route="medica_opstk",
            reason=reason,
            gemini_model=model,
            candidate=1,
            elapsed_ms=elapsed_ms,
            status="ok",
            row_count=len(medica.get("line_items") or []),
        )
        return medica

    if layout == "pharmassist_stock_sale":
        return extract_pharmassist_direct_vision(
            file_bytes, filename, ext, reason=reason
        )

    if layout == "stock_sales_op_qty_val":
        return extract_stock_sales_op_qty_val_direct_vision(
            file_bytes, filename, ext, reason=reason
        )

    if layout == "opqty_qoh_stock_sales":
        return extract_opqty_qoh_direct_vision(
            file_bytes, filename, ext, reason=reason
        )

    if layout == "op_qty_pur_sale_bal":
        return extract_op_qty_pur_sale_bal_direct_vision(
            file_bytes, filename, ext, reason=reason
        )

    if layout == "normal_stock_open_recp":
        return extract_normal_stock_open_recp_direct_vision(
            file_bytes, filename, ext, reason=reason
        )

    if layout == "sales_stock_opbal_issue":
        started = time.time()
        _log(
            "STOCK_DIRECT_VISION_START",
            filename=filename,
            route="sales_stock_opbal_issue",
            reason=reason,
            gemini_model=model,
            candidate=1,
            byte_size=len(file_bytes),
            numeric_source="gemini_vision",
        )
        try:
            from services.sales_statement_extractor import (
                _parse_swilerp_sales_stock_image,
            )

            opbal = _parse_swilerp_sales_stock_image(file_bytes, filename, ext)
        except Exception as exc:
            _log(
                "STOCK_DIRECT_VISION_FALLBACK",
                filename=filename,
                detected_layout=layout,
                schema=schema,
                reason="opbal_issue_parse_error",
                error_type=type(exc).__name__,
            )
            return None
        elapsed_ms = int((time.time() - started) * 1000)
        if not opbal or not opbal.get("line_items"):
            _log(
                "STOCK_DIRECT_VISION_FALLBACK",
                filename=filename,
                detected_layout=layout,
                schema=schema,
                reason="opbal_issue_empty",
                elapsed_ms=elapsed_ms,
                row_count=0,
            )
            return None
        lock_stock_vision_result(
            opbal,
            early_vision_reason="sales_stock_opbal_issue",
            stock_direct_vision_route="sales_stock_opbal_issue",
            stock_direct_vision_candidate=1,
            numeric_source="gemini_vision",
            gemini_input_kind="image_original",
            extraction_method=(
                ((opbal.get("totals") or {}).get("extra") or {}).get(
                    "extraction_method"
                )
                or "swilerp_page_split_vision"
            ),
        )
        _log(
            "STOCK_DIRECT_VISION_END",
            filename=filename,
            route="sales_stock_opbal_issue",
            reason=reason,
            elapsed_ms=elapsed_ms,
            status="ok",
            row_count=len(opbal.get("line_items") or []),
            numeric_source="gemini_vision",
        )
        _log(
            "STOCK_VISION_FINAL",
            filename=filename,
            handwritten="false",
            line_items=len(opbal.get("line_items") or []),
            numeric_source="gemini_vision",
            route="sales_stock_opbal_issue",
        )
        return opbal

    if layout == "ssa_opening_receipt_issue":
        started = time.time()
        _log(
            "STOCK_DIRECT_VISION_START",
            filename=filename,
            route="ssa_opening_receipt_issue",
            reason=reason,
            gemini_model=model,
            candidate=1,
            byte_size=len(file_bytes),
            numeric_source="gemini_vision",
        )
        try:
            from services.sales_statement_extractor import (
                _extract_ssa_opening_receipt_issue_dump_image,
            )

            ssa = _extract_ssa_opening_receipt_issue_dump_image(
                file_bytes, filename, ext, skip_ocr_gate=True
            )
        except Exception as exc:
            _log(
                "STOCK_DIRECT_VISION_FALLBACK",
                filename=filename,
                detected_layout=layout,
                schema=schema,
                reason="ssa_issue_closing_parse_error",
                error_type=type(exc).__name__,
            )
            return None
        if not ssa or not ssa.get("line_items"):
            elapsed_ms = int((time.time() - started) * 1000)
            _log(
                "STOCK_DIRECT_VISION_FALLBACK",
                filename=filename,
                detected_layout=layout,
                schema=schema,
                reason="ssa_issue_closing_empty",
                elapsed_ms=elapsed_ms,
                row_count=0,
            )
            return None
        if (
            _closing_qty_value_grid_beats_ssa_enabled()
            and _ssa_probe_invented_opening_values(ssa)
        ):
            _log(
                "STOCK_DIRECT_VISION_FALLBACK",
                filename=filename,
                detected_layout=layout,
                schema=schema,
                reason="ssa_issue_closing_invented_opening_values",
                elapsed_ms=int((time.time() - started) * 1000),
                row_count=len(ssa.get("line_items") or []),
            )
            return None
        return _lock_ssa_opening_receipt_issue_result(
            ssa,
            reason=reason,
            filename=filename,
            model=model,
            started=started,
        )

    # Garbled peek + filename _ZA_ often hides Opening/Issue/Closing SSA grids.
    # Probe SSA dump first (Tesseract gate; Gemini only if gate passes) so we
    # do not burn both Gemini candidates on the wrong SaleRet schema.
    if (
        _ssa_issue_closing_beats_filename_za_enabled()
        and "filename_za" in str(reason or "")
        and re.search(r"_ZA_\d+", filename or "", re.I)
    ):
        # Closing Stock QTY|VALUE grids: skip probe (Vision table owns them).
        if (
            _closing_qty_value_grid_beats_ssa_enabled()
            and _has_closing_stock_qty_value_grid_headers(
                layout_peek or peek_text or ""
            )
        ):
            _log(
                "STOCK_DIRECT_VISION_FALLBACK",
                filename=filename,
                detected_layout="ssa_opening_receipt_issue",
                schema="opening_receipt_issue_closing_dump",
                reason="filename_za_ssa_probe_skip_closing_qty_value_grid",
            )
        else:
            started = time.time()
            _log(
                "STOCK_DIRECT_VISION_START",
                filename=filename,
                route="ssa_opening_receipt_issue",
                reason="filename_za_ssa_probe",
                gemini_model=model,
                candidate=1,
                byte_size=len(file_bytes),
                numeric_source="gemini_vision",
            )
            try:
                from services.sales_statement_extractor import (
                    _extract_ssa_opening_receipt_issue_dump_image,
                )

                probed = _extract_ssa_opening_receipt_issue_dump_image(
                    file_bytes, filename, ext, skip_ocr_gate=True
                )
            except Exception as exc:
                _log(
                    "STOCK_DIRECT_VISION_FALLBACK",
                    filename=filename,
                    detected_layout="ssa_opening_receipt_issue",
                    schema="opening_receipt_issue_closing_dump",
                    reason="filename_za_ssa_probe_error",
                    error_type=type(exc).__name__,
                )
                probed = None
            if probed and (probed.get("line_items") or []):
                if (
                    _closing_qty_value_grid_beats_ssa_enabled()
                    and _ssa_probe_invented_opening_values(probed)
                ):
                    _log(
                        "STOCK_DIRECT_VISION_FALLBACK",
                        filename=filename,
                        detected_layout="ssa_opening_receipt_issue",
                        schema="opening_receipt_issue_closing_dump",
                        reason="filename_za_ssa_probe_invented_opening_values",
                        elapsed_ms=int((time.time() - started) * 1000),
                        row_count=len(probed.get("line_items") or []),
                    )
                    # Do not fall through to SaleRet — wrong schema for this grid.
                    return None
                return _lock_ssa_opening_receipt_issue_result(
                    probed,
                    reason="filename_za_ssa_probe",
                    filename=filename,
                    model=model,
                    started=started,
                )
            _log(
                "STOCK_DIRECT_VISION_FALLBACK",
                filename=filename,
                detected_layout="ssa_opening_receipt_issue",
                schema="opening_receipt_issue_closing_dump",
                reason="filename_za_ssa_probe_empty_fallthrough_saleret",
                elapsed_ms=int((time.time() - started) * 1000),
                row_count=0,
            )

    # SaleRet / ClosStock (and Opening/Purchase/Sale table signals)
    saleret = extract_saleret_direct_vision(
        file_bytes, filename, ext, reason=reason
    )
    if (
        saleret
        and _opbal_issue_beats_filename_za_enabled()
        and _saleret_looks_like_ignored_issue(saleret)
        and re.search(r"_ZA_\d+", filename or "", re.I)
    ):
        started = time.time()
        _log(
            "STOCK_DIRECT_VISION_START",
            filename=filename,
            route="sales_stock_opbal_issue",
            reason="saleret_ignored_issue_rescue",
            gemini_model=model,
            candidate=1,
            byte_size=len(file_bytes),
            numeric_source="gemini_vision",
        )
        try:
            from services.sales_statement_extractor import (
                _parse_swilerp_sales_stock_image,
            )

            rescued = _parse_swilerp_sales_stock_image(file_bytes, filename, ext)
        except Exception as exc:
            _log(
                "STOCK_DIRECT_VISION_FALLBACK",
                filename=filename,
                detected_layout="sales_stock_opbal_issue",
                schema="opbal_receipt_issue_closing",
                reason="opbal_issue_rescue_error",
                error_type=type(exc).__name__,
            )
            return saleret
        elapsed_ms = int((time.time() - started) * 1000)
        if rescued and (rescued.get("line_items") or []):
            lock_stock_vision_result(
                rescued,
                early_vision_reason="saleret_ignored_issue_rescue",
                stock_direct_vision_route="sales_stock_opbal_issue",
                stock_direct_vision_candidate=1,
                numeric_source="gemini_vision",
                gemini_input_kind="image_original",
                extraction_method=(
                    ((rescued.get("totals") or {}).get("extra") or {}).get(
                        "extraction_method"
                    )
                    or "swilerp_page_split_vision"
                ),
            )
            _log(
                "STOCK_DIRECT_VISION_END",
                filename=filename,
                route="sales_stock_opbal_issue",
                reason="saleret_ignored_issue_rescue",
                elapsed_ms=elapsed_ms,
                status="ok",
                row_count=len(rescued.get("line_items") or []),
                numeric_source="gemini_vision",
            )
            return rescued
        _log(
            "STOCK_DIRECT_VISION_FALLBACK",
            filename=filename,
            detected_layout="sales_stock_opbal_issue",
            schema="opbal_receipt_issue_closing",
            reason="opbal_issue_rescue_empty",
            elapsed_ms=elapsed_ms,
            row_count=0,
        )
    if (
        saleret
        and _ssa_issue_closing_beats_filename_za_enabled()
        and _saleret_looks_like_issue_closing_swap(saleret)
        and re.search(r"_ZA_\d+", filename or "", re.I)
    ):
        started = time.time()
        _log(
            "STOCK_DIRECT_VISION_START",
            filename=filename,
            route="ssa_opening_receipt_issue",
            reason="saleret_issue_closing_swap_rescue",
            gemini_model=model,
            candidate=1,
            byte_size=len(file_bytes),
            numeric_source="gemini_vision",
        )
        try:
            from services.sales_statement_extractor import (
                _extract_ssa_opening_receipt_issue_dump_image,
            )

            rescued = _extract_ssa_opening_receipt_issue_dump_image(
                file_bytes, filename, ext, skip_ocr_gate=True
            )
        except Exception as exc:
            _log(
                "STOCK_DIRECT_VISION_FALLBACK",
                filename=filename,
                detected_layout="ssa_opening_receipt_issue",
                schema="opening_receipt_issue_closing_dump",
                reason="ssa_issue_closing_rescue_error",
                error_type=type(exc).__name__,
            )
            return saleret
        if rescued and (rescued.get("line_items") or []):
            return _lock_ssa_opening_receipt_issue_result(
                rescued,
                reason="saleret_issue_closing_swap_rescue",
                filename=filename,
                model=model,
                started=started,
            )
        _log(
            "STOCK_DIRECT_VISION_FALLBACK",
            filename=filename,
            detected_layout="ssa_opening_receipt_issue",
            schema="opening_receipt_issue_closing_dump",
            reason="ssa_issue_closing_rescue_empty",
            elapsed_ms=int((time.time() - started) * 1000),
            row_count=0,
        )
    return saleret
