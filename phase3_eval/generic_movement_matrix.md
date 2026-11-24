# Generic movement matrix

Built from printed headers that were read from the source file, and from the identity code those headers actually hit. A column is not generic-safe just because it can be forced into `opening + receipts - sales - scheme - stock_out`.

The generic equation is `generic_expected_closing` in `services/stock_identity_profiles.py`:

`opening_qty + receipts_qty - sales_qty - sales_scheme - stock_out_qty`

Nulls become 0 inside `_num`. Sale return, other receipt, other issue, free, short-expiry, and signed other adjustments are not terms.

| Printed movement | Where seen | Canonical field today | Existing mapping | Identity role | Generic safe? |
| --- | --- | --- | --- | --- | --- |
| Op.Qty. / Opening_Qty / OPENING STOCK / O.Stk | 0000700017, xlsx, Marg xls, docx | `opening_qty` | yes, per layout | added | yes, when the cell is the opening quantity |
| Pur.Qty. / Purchase_Qty / Receipt Qty | 0000700017, xlsx, product-wise PDF | `receipts_qty` | yes | added | yes, for the purchase or receipt qty column only |
| RECEIVE QUANTITY | Marg xls stock grid | `receipts_qty` | yes, Marg header map | added | yes, inside the stock grid |
| ORec.Qty. | 0000700017 | none | prompt does not name it | other receipt, added on that layout | no |
| OIss.Qty. | 0000700017 | none | prompt does not name it | other issue, subtracted on that layout | no |
| Sale.Qty. / Sale_Qty / Issues Qty / ISSUE QUANTITY | the source-compared generic files | `sales_qty` | yes | subtracted | yes, for the sale or issue qty column |
| Sale_Ret_Qty | xlsx 0000728682 | none in the spreadsheet alias table | `saleret_expected_closing` already adds `sale_return` when the extra field exists | added back | no. Layout-scoped. The saleret profile already represents this. |
| Other_Qty | xlsx 0000728682 | none (`others` does not match `otherqty`) | no identity term | signed adjustment. VASAKA is −27 and closing drops by 27 | no |
| Receipt Free | product-wise PDF 0000700238 | `extra.receipts_free` only | parser comment says it is not a period receipt | the failing-row gap equals this column | no. Product-wise family, not generic. |
| Issues Free | product-wise PDF 0000700238 | `extra.issues_free_repl` only | same | subtracted on the failing row | no |
| Sh.Exp | product-wise PDF header; used on the product_wise-profile PDFs, which are outside this 31 | `extra.sh_exp` | product-wise equation does not subtract it | short-expiry reduction | no |
| Tot | docx O.Stk layout | `extra.total_stock` in the Profitmaker reader | checksum of opening + purchase | not a separate movement | no |
| Qoh | docx | `closing_qty` in the Profitmaker reader | closing result | result | yes, only when that reader runs |
| Value | docx | `closing_value` in the Profitmaker reader | rupees | not a quantity. Fallback stored it as `sales_scheme`, which the generic equation subtracts | no |
| Age | docx | `extra.age_days` in the Profitmaker reader | not a movement | none | no |
| INVOICE NO. under PURCHASE DETAIL | Marg xls row 45 | none | stock column index reused | not a quantity | no |
| Closing / Closing_Qty / CLOSING STOCK / Cl.Qty. | the source-compared files | `closing_qty` | yes | result | yes |

Photo folding of Receipt Free, Issues Free, and Goods into `receipts_qty` and `sales_qty` happens in `_extract_product_wise_stock_statement_image`. Those two documents are `product_wise_image`, not generic, so they are not in this 31. The product-wise PDF reader does the opposite: it keeps free beside the canonical quantity and does not fold it.
