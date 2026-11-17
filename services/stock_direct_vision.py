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
  Jul, Jun, Op., Pur, SP, Sale, SS, Br, Bsc qt, Cr, Db, Adj, Bal., BVal, SVal, Order

Map ONLY these fields into the JSON:
  Op.   -> opening_qty
  Pur   -> receipts_qty
  Sale  -> sales_qty
  Bal.  -> closing_qty
  BVal  -> closing_value
  SVal  -> sales_value

Put Jul and Jun into extra only (prior-month sales). NEVER into opening_qty or receipts_qty.
  Jul -> extra.jul_qty
  Jun -> extra.jun_qty
  Order -> extra.order_qty
  Br / SS / SP / Adj when present -> extra.br_qty / sales_scheme_qty / sp_qty / adj_qty

Examples (copy when visible):
- CONFIDO TAB 60'S: Op 63, Pur blank/0, Sale 2, Bal 61, BVal 11294, SVal 370
  (Jul=1 and Jun=2 are NOT opening/receipts)
- DIABECON DS TAB 60'S: Op 37, Sale 8, Bal 29, BVal 6297, SVal 1737
- LIV 52 SYRUP 100 ML: Op 73, Sale 23, Br 1, Bal 51, BVal 5440, SVal 2453
- LIV 52 TAB 100'S: Op 48, Sale 48, Bal 0, SVal 8046
- PILEX TAB 60'S: Op 100, Pur 100, Sale 109, Bal 91, BVal 16223, SVal 19433

Stockist is the company under the browser chrome (e.g. FOUR FENCE PHARMA PVT. LTD.).
Period from "From date DD-Mon-YY to DD-Mon-YY".

Return ONLY valid JSON:
{
  "document_type": "stock_statement",
  "schema": "pharmassist_op_pur_sale_bal",
  "stockist_name": string|null,
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
        "jul_qty": number|null,
        "jun_qty": number|null,
        "order_qty": number|null
      }
    }
  ],
  "totals": { "sales_value": null, "closing_value": null, "extra": {} }
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
  P.Qty  / P Qty / PURC / Pur.Qty -> receipts_qty  ← purchase only; 0 when the cell is 0
  P.Val  / P Val / PURCV   -> receipts_value
  Pur.Free / Pur Free      -> purchase_scheme_qty (extra; never receipts_qty)
  S.Qty  / S Qty / SALE / Sales Qty -> sales_qty
  S.Val  / S Val / SALEV   -> sales_value
  Cls.Qty / Cl.Qty / Cls Qty / STOCK / Cl.Stock / Cl Stock -> closing_qty
  Cls.Val / Cl.Val / Cls Val / STOCKV / Stock Value -> closing_value

BOX (between Pack and OPSTK) is NOT opening_qty and NOT opening_value — ignore it.
Pack (100 / 28 / 50 / 10) is NEVER opening_qty — copy Pack into packing only.
PCode / Manufacturer / Division are ignore columns.

Stock and Sales Detail often inserts scheme / return columns BETWEEN S and Cl.
Map returns AND scheme qty (scheme is NOT sales_qty / receipts_qty):
  P.Sch / P Sch -> ignore as purchase scheme (do not add into receipts_qty)
  S.Sch / S Sch / Free -> sales_scheme_qty (extra only; NEVER fold into sales_qty)
  Cr.Qty / Cr.S.Qty / Re.S.Qty / Rt.S.Qty -> sales_return_qty
  Cr.Val / matching return Val -> sales_return_value (money; NOT closing_value)
  Br.S.Qty / Br Qty (breakage/expiry) -> expiry_damage_qty
  Br.S.Val -> ignore as money extra (not closing_value)
  Db.Qty (+ Db.Sch.Qty) -> purchase_return_qty
  Db.Val -> ignore as money extra (not closing_value)
Keep reading rightward until Cl.Qty / Cl.Val (RIGHTMOST stock pair).

Column-shift traps (dense phone scans):
  Sl.No is NEVER opening_qty (row 3 → opening_qty is not 3).
  Never put Op.Qty into receipts_qty. If P.Qty cell is 0/blank, receipts_qty=0
  even when Op.Qty is large.
  When P.Qty=0, S.Qty=0 and Op.Val equals Cl.Val, opening_qty MUST equal
  closing_qty (same stock sitting idle) — copy both printed qty cells.
  Never drop a printed non-zero S.Val to 0.
  Always copy non-zero Cr.Qty into sales_return_qty (returns ADD to stock).
  If Cr.Val is non-zero, Cr.Qty must also be copied (never leave sales_return_qty
  null/0 while sales_return_value is printed).
  If Op.Val is non-zero, opening_qty must not be 0/null — re-read Op.Qty.

Map EVERY core value column. opening_value and receipts_value are money
(usually two decimal places). Never put Op.Val or S.Val into closing_value.
Never force sales_value to 0 when S.Val is printed.
Never put Bal Amt / Cl.Val into closing_qty.
Never copy Op.Val, P.Val, Db.Val, Cr.Val, Rt.S.Val, or S.Val into closing_value.
Never copy Op.Qty into closing_qty.
Cl.Qty/Cl.Val are ONLY the rightmost pair (after Db.*). If a red ZOOM panel
shows Cl.Qty|Cl.Val, copy closing fields from that panel per product row.
Never leave closing_value null/0 when Cl.Val is printed for that row.
NEVER invent closing_qty/closing_value from Op+P-S arithmetic — copy the
printed Cl.Qty/Cl.Val cells even when they disagree with Op+P-S (Db/Cr/Br).
product_name MUST be the FULL printed item text including pack
(e.g. "ASHWAGANDHA GUMMIES 60S", "BONNISAN DROPS (30ML)"), not a single word.

Examples when visible:
- #KOFLET LOZENGES JAR: Op.Qty 1, Op.Val 253.29, P blank, S.Qty 1,
  S.Val 253.29, Cls blank → closing_qty 0, closing_value 0
- BONNISAN DROPS (30ML): Op.Qty 0, P.Qty 300, P.Val 18864.0, S.Qty 167,
  S.Val 12468.22, Cr.Qty 5, Cl.Qty 138, Cl.Val 10303.08
- ASHWAGANDHA GUMMIES 60S: Op.Qty 316, Op.Val 189600.0, P 0, S.Qty 7,
  S.Val 4200.0, Cr.Qty 2, Cl.Qty 311, Cl.Val 186600.0
  (Op.Qty→opening_qty NOT receipts_qty; Cl.Val≠S.Val; Sl.No is not Op.Qty)
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
    (e.g. GANGA DISTRIBUTORS / RSM PHARMA PVT LTD). Never Himalaya.
  stockist_address = city/place in parentheses on Seller line
    (e.g. GULBARGA from Seller: GANGA DISTRIBUTORS(GULBARGA)).
  company_name = HIMALAYA DRUG CO-ZENITH / ZEAL / Company line.
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
      "product_name": string,
      "packing": string|null,
      "opening_qty": number|null,
      "opening_value": number|null,
      "receipts_qty": number|null,
      "receipts_value": number|null,
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

Include every visible product row top to bottom across both page panels.
Do NOT emit the Total / header Total row as a line_item.
Copy the printed Total-row S.Val into totals.sales_value and Total-row
Cl.Val into totals.closing_value when those cells are visible.
Do NOT sum line sales_value yourself when the Total-row S.Val is printed.
""".strip()


OPQTY_QOH_STOCK_VISION_PROMPT = """
You are reading a "Stock & Sales Statement" photo with OpQty / PurQty / SaleQty
and Qoh columns (NOT a SaleRet/ClosStock Product Stock Report).

Read the TABLE VISUALLY from the supplied image pixels.
Do NOT invent numbers. Do NOT calculate or balance anything.
Do NOT shift a value into a neighboring column.
Do NOT treat packing (30ML, 60S, 100GM) as stock quantities.

Printed columns left → right after Item Name / Packing:
  OpQty   -> opening_qty
  PurQty  -> receipts_qty
  SaleQty -> sales_qty
  PRetQty -> purchase_return_qty (extra only; 0 when blank)
  SRetQty -> sales_return_qty
  SAdjQty -> stock_adj_qty (extra only; 0 when blank)
  Qoh     -> closing_qty   ← Quantity on Hand is CLOSING. Never use Age.
  Age     -> ignore (days); NEVER put Age into closing_qty

Examples when visible:
- BONNISAN DROPS 30ML: OpQty 18, PurQty 50, SaleQty 1, SRetQty 1, Qoh 68
  (closing_qty=68, NOT 1)
- BONNISAN SYP 100ML: OpQty 162, SaleQty 8, Qoh 154
- HIORA K TOOTHPASTE 100GM: OpQty 1, PurQty 50, SaleQty 25, Qoh 26

Stockist is the letterhead (e.g. BALAJI PHARMA DISTRIBUTORS).
Company is the "Company:" line (e.g. HIMALAYA ZANDRA).

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
      "closing_value": null,
      "extra": {
        "purchase_return_qty": number|null,
        "stock_adj_qty": number|null
      }
    }
  ],
  "totals": { "sales_value": null, "closing_value": null, "extra": {} }
}

Include every product row. Blank qty cells are 0. Skip Group/Company banner rows.
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
  cell. Never emit sales_qty=0 when the Sales Qty column shows 1, 5, 14, …
- Pur.Free is NOT Sales Qty (Pur.Free is often 0 while Sales Qty is not).

Examples when visible (copy EXACT printed digits):
- HIMCOLIN GEL 30GM: Op.Stock 43, Pur 0, Pur.Free 0, Sales Qty 1,
  Cl.Stock 42 → sales_qty=1 (NOT 0)
- HIMPLASIA TABS 30'S: Op.Stock 94, Pur 0, Pur.Free 0, Sales Qty 5,
  Cl.Stock 89 → sales_qty=5 (NOT 0)
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
Sales, ClStockValue):
  - Line Stock Value is goods money → prefer Goods Value ClStockValue for
    totals.closing_value and Goods Value Sales for totals.sales_value.
  - Copy Amount-row Sales / OpStockValue / ClStockValue into totals.extra
    (amount_sales_value / opening_value / amount_closing_value) when printed.
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
      "goods_value": number|null
    }
  }
}

Include every product row top to bottom. Blank qty cells are 0.
Skip Goods Value / Amount / User Name / Print Datetime footer rows.
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


def _layout_header_text(file_bytes: bytes) -> str:
    """OCR top band for layout routing only — not numeric cell recovery."""
    try:
        from PIL import Image, ImageOps
        from services.sales_statement_extractor import _a2z_tesseract
        from services.stock_ocr_policy import stock_ocr_context

        with Image.open(io.BytesIO(file_bytes)) as raw:
            img = ImageOps.exif_transpose(raw).convert("RGB")
            width, height = img.size
            if width < 80 or height < 80:
                return ""
            # One named region for all header depths (initial + optional recovery).
            chunks: List[str] = []
            fracs = (0.22, 0.30, 0.38)
            tess = _a2z_tesseract()
            for idx, frac in enumerate(fracs):
                header = img.crop((0, 0, width, max(40, int(height * frac))))
                probe = header.copy()
                probe.thumbnail((1200, 1200))
                ctx_kwargs = {"region": "layout_header", "page": "1"}
                if idx > 0:
                    ctx_kwargs["reason"] = "header_detection_failed"
                with stock_ocr_context(**ctx_kwargs):
                    text = tess.image_to_string(probe, config="--psm 6") or ""
                if text.strip():
                    chunks.append(text)
                    if _has_order_form_headers(text) or _has_saleret_headers(text):
                        return "\n".join(chunks)
            return "\n".join(chunks)
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
    # Phone OCR of Stock and Sales Detail often keeps the title + Op.Qty/Cl.Qty
    # even when P.Sch / return headers are garbled.
    if re.search(r"Stock\s+and\s+Sales\s+Detail", blob, re.I) and (
        (has_op_qty and has_cls) or (has_op_qty and has_op_val)
    ):
        return True
    # Dense phone scans often OCR only the title (+ Seller:) while Op.Qty/Cl.Qty
    # blue headers are unreadable. Title is never a SKU — route to Op.Qty Vision.
    # Kill switch: STOCK_SALES_DETAIL_TITLE_ROUTE=false.
    if _stock_sales_detail_title_route_enabled() and re.search(
        r"Stock\s+and\s+Sales\s+Detail\s+Report", blob, re.I
    ):
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

def _has_pharmassist_stock_sale_headers(text: str) -> bool:
    """C-Square PharmAssist Stock and Sale Report (photo or PDF OCR).

    Columns: Item Pack Jul Jun Op. Pur … Sale … Bal. BVal SVal Order.
    Jul/Jun are prior-month sales — never opening/receipts.
    """
    blob = text or ""
    if not re.search(r"Stock\s+and\s+Sale\s+Report", blob, re.I):
        return False
    if re.search(r"PharmAssist", blob, re.I) and (
        re.search(r"\bBVal\b", blob) or re.search(r"\bSVal\b", blob)
    ):
        return True
    if re.search(r"\bBVal\b", blob) and re.search(r"\bSVal\b", blob):
        return True
    # Phone photo of the PDF: title survives; column headers often do not.
    if re.search(r"From\s+date", blob, re.I) and re.search(
        r"Opening\s+Val|Closing\s+Val|Sales\s*\(\s*Jul\s*\)|Sales\s*\(\s*Jun\s*\)",
        blob,
        re.I,
    ):
        return True
    if re.search(r"FOUR\s+FENCE|THIRUVALLA", blob, re.I) and re.search(
        r"From\s+date", blob, re.I
    ):
        return True
    return False


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
        raw["extra"] = extra
    return parsed


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
    low_confidence_cells = 0
    named = 0
    order_form = layout.startswith("handwritten_order_form")
    pharmassist = layout.startswith("pharmassist_stock_sale")
    op_qty_val = layout.startswith("stock_sales_op_qty_val")
    opqty_qoh = layout.startswith("opqty_qoh_stock_sales")
    op_qty_pur_sale_bal = layout.startswith("op_qty_pur_sale_bal")
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
    # Op.Qty|Pur.Qty|Sale.Qty|Bal ledgers often print Bal ≠ Op+Pur-Sale
    # (document gap). Penalizing identity here rewards Gemini for inventing
    # Sale/Bal so the math works — keep score structural only for this family.
    if (
        not op_qty_pur_sale_bal
        and row_count >= min_rows
        and identity_fail > max(2, row_count // 4)
    ):
        soft_reasons.append("identity_failure_majority")

    hard_ok = not reasons
    if op_qty_pur_sale_bal:
        identity_score = max(0, row_count * 3)
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
    payload = {
        "contents": [
            {
                "role": "user",
                "parts": [
                    {"text": prompt or STOCK_DIRECT_VISION_PROMPT},
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
            r"^Done$|free\s*trial|Division\s*Total|^Total\b", name, re.I
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


def extract_pharmassist_direct_vision(
    file_bytes: bytes,
    filename: str,
    ext: str,
    *,
    reason: str = "pharmassist_stock_sale_report",
) -> Optional[Dict[str, Any]]:
    """Original-image Vision for PharmAssist Op./Pur/Sale/Bal. Stock and Sale Report."""
    model = os.getenv("VISION_MODEL", "gemini-2.5-flash-lite").strip()
    max_attempts = _max_attempts()
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
            return result
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
                    return result2

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
    return best


def extract_stock_sales_op_qty_val_direct_vision(
    file_bytes: bytes,
    filename: str,
    ext: str,
    *,
    reason: str = "stock_sales_op_qty_val_headers",
    max_attempts: Optional[int] = None,
) -> Optional[Dict[str, Any]]:
    """Original-image Vision for STOCK AND SALES Op.Qty/Op.Val paired columns."""
    model = os.getenv("VISION_MODEL", "gemini-2.5-flash-lite").strip()
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
    widen_cl = _stock_sales_detail_widen_cl_enabled()
    vision_bytes = (
        _widen_detail_closing_columns(vision_primary)
        if widen_cl
        else vision_primary
    )
    if widen_cl:
        original_meta = {
            **original_meta,
            "stock_sales_detail_widen_cl": True,
        }
    call_reason = reason + (
        ("+exif_orient" if exif_oriented else "")
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
            lock_stock_vision_result(
                result,
                early_vision_reason="stock_sales_op_qty_val_vision",
                stock_direct_vision_route=layout,
                stock_direct_vision_candidate=1,
                gemini_calls=1,
            )
            return result

    if max_attempts >= 2 and (not candidates or not validation.get("soft_ok")):
        use_identity_repair = (
            _stock_sales_detail_identity_repair_enabled()
            and bool(result)
            and bool(validation.get("ok"))
            and not validation.get("soft_ok")
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
                    lock_stock_vision_result(
                        result,
                        early_vision_reason="stock_sales_op_qty_val_vision",
                        stock_direct_vision_route=layout,
                        stock_direct_vision_candidate=2,
                        gemini_calls=2,
                    )
                    return result
        else:
            result2 = _call_psr_vision(
                file_bytes,
                filename,
                ext,
                vision_bytes=vision_bytes,
                image_meta=original_meta,
                candidate=2,
                model=model,
                route=layout,
                reason=call_reason + "+retry",
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
                candidates.append(
                    (int(validation2.get("identity_score") or 0), result2, validation2)
                )
                if validation2.get("soft_ok"):
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

    candidates.sort(key=lambda row: row[0], reverse=True)
    best_score, best, best_val = candidates[0]
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
  receipts_qty (P.Qty) — 0 when printed 0; never copy Op.Qty here
  sales_qty (S.Qty)
  sales_value (S.Val) — money; never drop a printed non-zero to 0
  sales_scheme_qty (S.Sch) — 0 when blank; never fold into sales_qty
  sales_return_qty (Cr.Qty / Rt.S.Qty) — 0 when blank
  closing_qty (Cl.Qty) — second-to-last stock column
  closing_value (Cl.Val) — RIGHTMOST money column after all Rt/Cr/Db cols

Do NOT invent numbers. Do NOT balance equations.
Do NOT copy S.Val / Db.Val / Cr.Val into closing_value.
Do NOT treat Sl.No as opening_qty.
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
        # Also allow equal-ish gap when filling missing Cr / S.Sch / S.Val.
        fills_missing = bool(
            (item.get("sales_return_qty") in (None, 0) and src.get("sales_return_qty"))
            or (
                (item.get("extra") or {}).get("sales_scheme_qty") in (None, 0)
                and src.get("sales_scheme_qty")
            )
            or (not item.get("sales_value") and src.get("sales_value"))
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
    """
    blob = text or ""
    if not blob.strip():
        return False
    has_open = bool(re.search(r"OPENING\s+STOCK", blob, re.I))
    has_recv = bool(
        re.search(r"RECEIVE\s+(QUANTITY|QTY|VALUE)|RECEIVE\s+QUANT", blob, re.I)
    )
    has_issue = bool(re.search(r"ISSUE\s+(QUANTITY|QTY|VALUE)", blob, re.I))
    has_close = bool(re.search(r"CLOSING\s+STOCK", blob, re.I))
    return has_open and has_recv and has_issue and has_close


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
                if _has_opening_receive_issue_closing_qty_value_headers(
                    text
                ) or _has_stock_sales_op_qty_val_headers(text):
                    return "\n".join(chunks)
        return "\n".join(chunks)
    except Exception:
        return ""


def _stock_sales_detail_scan_per_page_enabled() -> bool:
    """Prefer per-page Vision over vertical stitch (env, default ON)."""
    raw = os.getenv("STOCK_SALES_DETAIL_SCAN_PER_PAGE", "1").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def _stock_sales_detail_widen_cl_enabled() -> bool:
    """Append a zoomed Cl.Qty/Cl.Val strip so Vision does not steal Cr/Db (default ON)."""
    raw = os.getenv("STOCK_SALES_DETAIL_WIDEN_CL", "1").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def _stock_sales_detail_identity_repair_enabled() -> bool:
    """Second-call Op/P/S/Cr/Cl repair+merge on soft identity fail (default OFF)."""
    raw = os.getenv("STOCK_SALES_DETAIL_IDENTITY_REPAIR", "0").strip().lower()
    return raw in {"1", "true", "yes", "on"}


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
    # Case A: Pack parked in opening; real Op sits in receipts.
    # Accept when Pack text matches OR opening is a common pack size and the
    # shifted identity (rec-sale≈cl) holds while the printed assignment does not.
    if (
        rec > 0
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
            r"Print\s*Datetime|Non\s*Moving|Short\s*Expiry)\b",
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
    ):
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
    extra_t["extraction_method"] = "op_stock_pur_sales_cl_vision"
    extra_t["layout"] = "op_stock_pur_sales_cl"
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
        repair = _call_psr_vision(
            images[0],
            filename,
            ".png",
            vision_bytes=images[0],
            image_meta={
                "normalized": False,
                "normalization_reason": None,
                "op_stock_sales_qty_repair": True,
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
    validation = validate_stock_direct_vision(seed, layout="op_qty_pur_sale_bal")
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


def extract_opqty_qoh_direct_vision(
    file_bytes: bytes,
    filename: str,
    ext: str,
    *,
    reason: str = "opqty_qoh_stock_sales_headers",
) -> Optional[Dict[str, Any]]:
    """Vision for OpQty/PurQty/SaleQty/Qoh Stock & Sales Statement (Balaji etc.)."""
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
            prompt=OPQTY_QOH_STOCK_VISION_PROMPT,
            layout_label="opqty_qoh_stock_sales",
            default_title="Stock & Sales Statement",
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
            extra2["extraction_method"] = "opqty_qoh_stock_sales_vision"
            extra2["layout"] = "opqty_qoh_stock_sales"
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
    if not _has_stock_sales_op_qty_val_headers(_opstk_hint) and re.search(
        r"STOCK\s+AND\s+SALES|Stock\s+and\s+Sales\s+Detail|"
        r"\bOPSTK\b|\bOPV\b|\bPURCV\b|\bSALEV\b|\bSTOCKV\b",
        _opstk_hint,
        re.I,
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
