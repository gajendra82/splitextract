# PHASE 3.2 — Generic profile forensic audit

Production code was not changed. Phase 2 was not changed. Tesseract was not changed. PaddleOCR was not installed.

Phase 3.1 files were not overwritten.

# 1. Executive summary

The generic profile is not one broken equation. It is the default identity rule for many layouts that do not share one movement set.

Of the 31 generic-profile `STOCK_IDENTITY_FAILURE` documents, five were compared to the source file. They fail at three different layers:

- Three documents drop a printed movement before the equation runs (`COLUMN_DROPPED`).
- Two documents extract a non-quantity, or miss an existing layout reader (`PARSER_EXTRACTION_ERROR`).
- Twenty-six were not row-compared. They do not share the headers of the five, so they stay `UNKNOWN`.

Changing `generic_expected_closing` would not repair these files, and it would put layout-specific movements into every document that already passes the generic rule.

**Classification: G. MULTIPLE_LAYER_FIX, not implemented.**

**Decision: INVESTIGATE_FURTHER.**

# 2. 31-document generic failure distribution

| Root cause | Count | Documents |
| --- | ---: | --- |
| COLUMN_DROPPED | 3 | 0000700017, 0000700238, 0000728682 |
| PARSER_EXTRACTION_ERROR | 2 | 0000701555, 0000734248 |
| PROFILE_SELECTION_ERROR | 0 as the sole cause | 0000700238 is also scored generic, but the numeric gap remains under the product-wise equation |
| IDENTITY_EQUATION_ERROR | 0 as the first wrong step | sale return already has the saleret equation; the column never reaches it |
| OCR_ERROR | 0 | |
| UNKNOWN | 26 | vision and uncompared text readers |

Profile is `generic` on all 31. Extraction methods are heterogeneous: Gemini vision and image fallback account for most of the 26. The five source-compared files are one digital PDF, one product-wise PDF, one Word file, one xlsx, and one xls.

The other six `STOCK_IDENTITY_FAILURE` documents are not in this 31. They use `product_wise_image` (2), `product_wise` (2), `saleret` (1), and `unknown` (1).

# 3. Mapping errors

## 0000700017 — ORec.Qty and OIss.Qty

Printed header, from embedded text:

`Op.Qty.  Op.Val.  Pur.Qty.  Pur.Val.  ORec.Qty.  ORec.Val.  Sale.Qty.  Sale.Val.  OIss.Qty.  OIss.Val.  Cl.Qty.  Cl.Val.`

AACTARIL SOAP:

| Field | Source | Parser |
| --- | ---: | ---: |
| Opening | 40 | 40 |
| Pur.Qty | 0 | receipts 0 |
| ORec.Qty | 2 | absent |
| Sale.Qty | 11 | 11 |
| OIss.Qty | 0 | absent |
| Closing | 31 | 31 |

`40 + 0 + 2 - 11 - 0 = 31`. The generic check does `40 + 0 - 11 = 29`.

All 49 reconstructed source rows satisfy `Op + Pur + ORec - Sale - OIss = Closing`. On 21 rows the gap from `Op + Pur - Sale` equals `ORec - OIss`.

First incorrect transformation:

1. Source text contains both columns.
2. Named text parsers ahead of Gemini do not claim this header.
3. `_SALES_STATEMENT_TEXT_PROMPT` maps purchase/receipt to `receipts_qty` and sale/issue to `sales_qty`. It does not name `ORec.Qty` or `OIss.Qty`.
4. `_apply_parsed_sales_json` copies `receipts_qty` and `sales_qty` only.
5. `generic_expected_closing` then scores the reduced pair.

The drop happens at the prompt schema, before validation. Do not add ORec to the generic equation. Layouts that have only purchase would start adding a column they do not print.

## 0000700238 — free quantities kept out of the canonical fields

Printed header includes Opening, Receipt, Free, Issues, Free, Goods, Closing, Sh.Exp.

`_parse_product_wise_stock_statement_pdf` stores `extra.receipts_free` and `extra.issues_free_repl` and does not add them to `receipts_qty` or `sales_qty`. Its comment says free is not a period movement.

Failing parser rows:

- opening 0, receipts 48, sales 50, receipts_free 2, closing 0. Generic expected −2. Gap +2.
- opening 0, receipts 200, sales 172, issues_free_repl 2, closing 26. Generic expected 28. Gap −2.

`issues_expected_closing` (`opening + receipts - sales`) has the same gap. Selecting the product-wise profile would not make these rows pass.

This method is also in `_KNOWN_GENERIC_METHODS`, so `select_profile` returns generic. That is a profile-selection mismatch with the product-wise PDF, and it is not the first numeric error.

## 0000728682 — Sale_Ret_Qty and Other_Qty

Sheet columns include `Opening_Qty`, `Purchase_Qty`, `Sale_Qty`, `Sale_Ret_Qty`, `Other_Qty`, `Closing_Qty`.

| Product | Identity on the sheet |
| --- | --- |
| HERBOLAX TABS | 47 − 4 + sale return 1 = 44 |
| LIV-52 SYRUP | 82 + 105 − 77 + sale return 2 = 112 |
| PILEX FORTE TAB | 99 − 1 + sale return 1 = 99 |
| VASAKA SYRUP | 55 + Other_Qty −27 = 28 |
| AACTARIL SOAP | 122 − 3 = 119, both extra columns 0 |

`_XLS_HEADER_ALIASES` maps `openingqty`, `purchaseqty`, `saleqty`, and `closingqty`. It has no `saleretqty`. The alias `others` does not match `otherqty`. `_xls_fill_from_rows` never reads those two columns.

`saleret_expected_closing` already does `total - sales + sale_return - exp_damage`. It is not used, because `extra.sale_return` is never set, so `_stock_identity_kind` stays generic.

The +1 on HERBOLAX is `Sale_Ret_Qty`. It already has a profile. The −27 on VASAKA is `Other_Qty`, and that signed column is not in the saleret equation. Mapping only sale return would leave VASAKA failing.

# 4. Equation errors

No generic-profile document failed because the generic equation itself is the first wrong step.

Short-expiry is real, and it is outside this 31. On the `product_wise` PDFs, CONFIDO TABS is opening 66, sales 0, `sh_exp` 3, closing 63. The parser stores `sh_exp`. `issues_expected_closing` does not subtract it. That equation belongs to the product-wise family, not to generic. Do not copy it into `generic_expected_closing`.

Sale return belongs to the existing saleret profile. The xlsx failure is that the column is dropped before that profile can see it.

# 5. Parser errors

## 0000734248

Stock header: PRODUCT DESCRIPTION, OPENING STOCK, RECEIVE QUANTITY, ISSUE QUANTITY, CLOSING STOCK, plus value columns.

AACTARIL SOAP on the sheet is 40 / 0 / 0 / 40. Thirty-four parser rows satisfy opening + receive − issue = closing.

Row 39 is the stock TOTAL. Row 43 is a second table, `PURCHASE DETAIL`, with SUPPLIER NAME, INVOICE NO., DATE, RECEIVE DATE, AMOUNT. Row 45 is:

`HIMALAYA WELLNESS COM INDORE | 5126107167 | 21-08-2026 | 21-08-2026 | 93636`

`_parse_marg_erp_xls` does not stop at that section. It keeps the stock column indexes. The invoice number becomes `opening_qty`. The date `21-08-2026` becomes `receipts_qty` 21.

First wrong step: the product loop continues into a different table. Header detection, the stock column indexes, and the 34 product rows are correct. A parser boundary is the right layer. It was not implemented, because a stop rule was not checked against other Marg workbooks that may contain a second real stock block.

## 0000701555

The paragraph above the table is:

`Product Name Packing O.Stk Purc Tot Sale Qoh Value Age`

The table's first row is a product, not that header.

ABANA TABLETS source cells: `60 TAB`, O.Stk 7, Purc 0, Tot 7, Sale 2, Qoh 5, Value 756.80, Age 148.

`7 + 0 - 2 = 5`. That is the printed identity.

`_parse_profitmaker_ostk_docx` already maps O.Stk, Purc, Sale, and Qoh, but only when those words are on table row 0. This file misses that reader. The fallback stores opening 60 (the pack), sales 5, closing 0, and `sales_scheme` 756.8. The generic equation then subtracts a rupee amount.

First wrong step: the existing layout reader does not see a header that sits outside the table. Do not add a new equation.

# 6. Profile error

`0000701789` is not one of the 31. Its profile is `unknown`.

The reader is `zandra_two_column_order_form`. It sets `sales_qty` from the order quantity and does not set opening or closing. `zandra_two_column_order_form` is not a known stock-identity method, so `select_profile` returns `unknown`. `mark_unknown_identity` sets `is_valid` false with fail count 0. Recovery attempts stay 0.

That is fail-closed behavior for an order form. No existing stock profile describes an order-quantity column. A new profile would be justified only by a reusable stock-identity pattern. This file does not provide one. Do not add a profile to make it pass.

# 7. Unknown cases

The 26 were grouped by extraction method. They do not cluster on ORec, Sale_Ret_Qty, Other_Qty, or the Marg purchase-detail trailer.

| Method | Count | Number source |
| --- | ---: | --- |
| gemini_extraction_fallback | 4 | model image read |
| zandra_stock_sale_vision | 4 | model image read |
| gemini_vision | 3 | model image read |
| swil_receipt_pur_value_vision | 3 | model image read |
| pdf_split_page_images | 3 | model page images |
| pack_op_pur_bal_stock_sale_vision | 3 | model image read |
| pharma_hub_jun_jul_photo | 2 | model image read; Tesseract is a header peek |
| marg_closing_mexp_photo | 1 | model image read |
| pack_opening_receipt_issue_mexp | 1 | Tesseract geometry, row not compared |
| main_stock_sales_statement | 1 | not compared |
| swilerp_retrn_sales_stock | 1 | PyMuPDF text, not compared |

Matched QA on `0000700084` shows 40 rows holding and 9 missing, with deltas such as −29, +4, and +83, and scheme 0. That is not the ORec pattern. They stay `UNKNOWN`.

# 8. Semantic movement matrix

See `generic_movement_matrix.md`.

Opening, purchase or receipt qty, sale or issue qty, and closing are generic-safe when they are those printed columns.

ORec, OIss, sale return, other qty, free, short-expiry, Qoh-versus-value, and invoice numbers are not generic-safe.

# 9. Cross-layout contamination

Phase 2 recovery mappings in `column_mapping.py` are keyed by extraction method, layout, or profile id. There is no universal header dictionary on that path. `ORec.Qty` is not an alias there. A product-wise mapping cannot authorize an ORec cell on another layout through the projection guard. Those guards were not weakened.

The contamination is earlier, at parse time:

- `_XLS_HEADER_ALIASES` is a global spreadsheet dictionary. `sale` becomes `sales_qty` for every sheet that reaches `_xls_fill_from_rows`. `Sale_Ret_Qty` is simply absent, so it is dropped rather than borrowed from another layout.
- `_KNOWN_GENERIC_METHODS` places `product_wise_stock_statement_pdf` on the generic profile even though the printed form is product-wise. That selects the wrong profile family. It does not, by itself, explain the free-column gap.
- The Gemini text prompt is a shared schema. It has one `receipts_qty` and no other-receipt field, so a six-quantity statement is reduced before identity runs.

# 10. Proposed architecture-level changes

Not implemented. Each item is a separate layout, and none is a change to the generic equation.

1. Add a named reader for the Op / Pur / ORec / Sale / OIss / Cl text statement. ORec adds to receipts for that layout only. OIss adds to issues for that layout only. Give that method its own profile, or teach the existing generic profile nothing and score the layout with its own equation.
2. On the semantic spreadsheet path, map `Sale_Ret_Qty` to `extra.sale_return` so the existing saleret profile can run. Decide `Other_Qty` separately. Do not alias every column whose name contains "other".
3. In `_parse_marg_erp_xls`, stop the product loop at a following section whose header is not the stock header (`PURCHASE DETAIL`). Do not change column indexes.
4. Let `_parse_profitmaker_ostk_docx` recognize the O.Stk header when it is in the paragraph above a headerless table. The column semantics already exist.
5. For product-wise PDF free and Sh.Exp, decide the identity inside the product-wise family. Do not fold those columns into generic.

# 11. Exact files that would need modification

Only if a later change is approved:

- `services/sales_statement_extractor.py` for the four parser boundaries above
- `services/stock_identity_profiles.py` only if a new layout profile is added
- `services/column_mapping.py` only to register headers that the new or existing parser already emits

Not `services/recovery_projection.py`. Not the OCR path.

# 12. Exact functions that would need modification

- `_SALES_STATEMENT_TEXT_PROMPT` and the PDF text dispatch, or a new parser called before that prompt
- `_apply_parsed_sales_json` only if the new parser is not used and extra fields must be preserved
- `_XLS_HEADER_ALIASES` and `_xls_fill_from_rows`
- `_parse_marg_erp_xls`
- `_profitmaker_ostk_header_map` / `_parse_profitmaker_ostk_docx`
- `_parse_product_wise_stock_statement_pdf` and `issues_expected_closing` only after a product-wise decision
- `_choose_profile` only to attach a new method to a new profile

# 13. Tests required before any of those edits

- A fixture with Op, Pur, ORec, Sale, OIss, Cl where closing uses ORec, and a second statement that has no ORec and must stay on purchase only
- An xlsx with `Sale_Ret_Qty` and `Other_Qty`, including a row whose other qty is negative and a row where both are zero
- A Marg workbook whose stock rows balance and whose purchase-detail block must not become a line item
- A Word table whose O.Stk header is outside the table, plus an existing Profitmaker table whose header is row 0 and must not change
- A product-wise PDF row whose free qty is non-zero, and a product-wise row whose free qty is zero and already passes
- Phase 1, Phase 2, column mapping, evidence mapping, and recovery projection tests
- A replay of current generic PASS documents on the touched method

# 14. Regression risks

- A global ORec alias would add a receipt on layouts where that token means something else, or would double-count purchase.
- Putting `Other_Qty` into `stock_out_qty` for every sheet would change closing on workbooks that already pass.
- Stopping the Marg parser at the first TOTAL would drop a second stock block if one exists.
- Treating every headerless Word table as Profitmaker would shift columns on other Word layouts.
- Subtracting `sh_exp` or free inside the generic equation would fail product rows that already pass without those terms.

# 15. PASS to WRONG risk

No equation or mapping change was applied, so no passing document was re-scored into a wrong pass.

Any of the five proposals can cause PASS to WRONG if it is applied outside its layout. That is why they stay proposals.

# 16. Recommendation

Keep the generic equation. Do not add a universal alias dictionary. Do not add a profile for the order form.

Investigate the five source-compared layouts as separate readers. Leave the 26 vision documents unknown until their printed columns are read from the page without using the model as ground truth.

# Classification

G. MULTIPLE_LAYER_FIX

The layers are independent: a text-prompt schema, a spreadsheet alias table, a Marg section boundary, a Word header location, and a product-wise free column. One generic-equation edit does not cover them.

# Final decision

INVESTIGATE_FURTHER
