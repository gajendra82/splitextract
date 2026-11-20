# Stock-statement extraction pipeline — architecture investigation (read-only)

Date: 2026-10-01. Scope: `/var/www/html/splitextract` (branch `dilip`, HEAD a380ace + uncommitted work). No code, .env, service, or DB changes were made. Live PID 3664315 was not touched.

## 0. Headline findings

1. The `fallback_reason=TRIAGE_GENERIC_SEMANTIC` case was produced by a code generation that is NOT in the current tree. Journal shows `services.stock_statement_document_triage` TRIAGE_DECISION lines only from 2026-09-29 19:37 to 2026-09-30 06:33 (PIDs 2331829…2713948). The source tree was wiped on Sep 30 (`/var/www/html/splitextract_RECOVERED/RECOVERY_REPORT.md`); the restored tree does not contain `stock_statement_document_triage.py`, `stock_statement_column_resolver.py`, `stock_statement_reconciliation.py`, `stock_statement_numeric*.py`, `generic_column_semantic_fallback.py`, `hybrid_ocr_decision.py`, `hybrid_paddle_fallback.py`, `ocr_comparison.py`. Only signature skeletons, string constants and bytecode disassembly survive under `splitextract_RECOVERED/`.
2. Exact bug recovered from bytecode (`from_inspect_v2/dis/services_gemini_extraction_fallback__should_use_generic_vision_fallback.dis.txt`, source lines 406–422 of the wiped `gemini_extraction_fallback.py`):
   ```python
   if get_triage_recommended_path() == "GENERIC_SEMANTIC":
       extra = result["totals"]["extra"]
       if extra.get("extraction_method") == "generic_column_semantic":
           schema = extra.get("column_schema") or {}
           schema_conf = float(schema.get("confidence") or 0)
           cols = schema.get("columns") or {}
           row_count = len(line_items)
           if schema_conf >= 0.72 and cols and row_count >= 3:
               return {"use": False, "reason": "TRIAGE_GENERIC_SEMANTIC", ...}
   skip = _protected_parser_skip(result)          # -> PROTECTED_PARSER_SKIP
   if not quality.get("should_fallback"): ...     # -> ALREADY_GOOD
   ```
   The triage short-circuit ran BEFORE the quality gate, so `stock_identity_fail_count=26` (2 valid / 11 invalid / 19 ambiguous of 32 rows) was never consulted. Header confidence (0.72) was treated as proof that the numbers were right.
3. The current tree has the same class of bypass in different clothes (section 4): identity failures are ignored for every non-generic `stock_identity_kind` (including SaleRet), named parsers get score 100 unconditionally, five layout allowlists skip Gemini even when the gate says fallback, and the image post-pass sets `stock_image_vision_decided` which disables `maybe_apply_gemini_fallback` entirely.
4. At least 14 functions rewrite quantities using arithmetic without marking them derived (section 7).
5. Column assignment in the OCR paths is overwhelmingly positional ("last N numeric tokens") or alias-order based; only 6 readers use header x-centres from `image_to_data`.

---

## 1. `/extract-sales-statement` — call graph

```
POST /extract-sales-statement                       app.py:30110 extract_sales_statement_endpoint
  resolve_request_id / begin_sales_progress          sales_extraction_runtime.py:195/280
  acquire_sales_extraction_slot (MAX_CONCURRENT_EXTRACTIONS=2, SALES_QUEUE_TIMEOUT)   :1263
  asyncio.to_thread(run_sales_extract_with_deadline)                                  :1947
    start_sales_deadline(SALES_EXTRACTION_MAX_EXECUTION_SECONDS=600)                  :865
    classify_sales_document (image|text_pdf|image_pdf|xlsx|xls|docx|doc|txt|html)     :240
    extract_sales_statement(file_bytes, filename)            sales_statement_extractor.py:4826
      _sniff_extension
      dispatch:
        .txt  -> _parse_txt                 :5860
        .htm  -> _parse_htm
        .doc/.docx -> _parse_word           :6201 -> _parse_docx :7555 | _extract_legacy_doc_text :6279 -> _parse_txt
        .pdf  -> _parse_pdf                 :27045
        .xls/.xlsx/.xlsm -> _parse_xls      :11211
        image -> _parse_image               :41734
      post-pipeline (all types):
        _sanitize_statement_financials      :3184
        _apply_stock_identity_validation    :3566   (flag only; sets stock_identity_kind/formula/fail_count/stock_validation)
        _ensure_stock_qty_value_fields      :4647   (missing columns -> 0 / null)
        _product_wise_drop_banner_items
        [image only] apply_direct_stock_image_gemini  stock_image_vision.py:637  (unless stock_vision_locked)
        maybe_apply_gemini_fallback         gemini_extraction_fallback.py:484 (skipped if stock_image_vision_decided)
        _log_sales_reader                   :4816
        _group_statements_by_stockist_month :585
  endpoint quality / extraction_failed gate                                  app.py:~30345
  finish_sales_progress; JSON response (or POD wrapper for hospital xlsx)
```

### 1a. Image branch (`_parse_image` :41734) in execution order

1. `detect_stock_handwriting_signals` → if order form / handwritten → `try_stock_direct_vision` (original bytes → Gemini) → locked → return.
2. `_sideways_full_width_stock_grid` → `_extract_sideways_rate_ssa_photo`; `_zeal_printed_order_form_anchor` → `_extract_zandra_two_column_order_photo`.
3. `_maybe_early_vision_for_image` (:40503):
   - `classify_stock_direct_vision("", filename)` — filename `_ZL_`/`_ZA_` classify with zero OCR; otherwise 1 OCR sample (`_ocr_image_to_text`, psm 6).
   - `assess_source_text_quality(sample)` → score 0–100, threshold `SALES_STATEMENT_OCR_QUALITY_THRESHOLD=75`.
   - `try_stock_direct_vision(file_bytes, filename, ext, peek_text)` (stock_direct_vision.py:2098) → locked Vision result → `STOCK_STATEMENT_VISION_FIRST`, return (direct, skip_ocr_probes=True).
   - `_image_known_ocr_native_format(sample)` (:40473, 22 regex detectors) → `KEEP_OCR_PARSER reason=KNOWN_IMAGE_FORMAT` → (None, False).
   - rotated STOCK VALUATION preview → `KEEP_OCR_PARSER reason=STOCK_VALUATION_ROTATED`.
   - legacy SaleRet → `_extract_product_stock_report_image_vision`.
   - `KEEP_OCR_PATH` if quality good → (None, False); else specialised early Vision readers (pack/Op/Pur/Bal, code_item, Medica OPSTK, …) then generic early Vision.
4. `_extract_phone_rate_ssa_screenshot`, Pharma Hub, qty-only SSA probes.
5. `_pack_op_pur_bal_header_hint` → `_extract_pack_op_pur_bal_stock_sale_vision`.
6. OCR probe cascade (if not skip_ocr_probes), each guarded by `check_sales_deadline` + `sales_ocr_budget_exhausted` (SALES_OCR_MAX_CALLS_PER_REQUEST=500): SwilERP, Batchwise, Product-wise photo, Stock valuation, Summary RTL, Opening/Receive/Issue, A2Z, SSA sales-free, M.EXP, packing report, Zandra order, OP/PUR/SALE/BAL qnty, Opening/purchase/sale/balance, rate qty/value (OCR then Vision), Marg screenshot, SSA dump, Marg M.EXP, Stock valuation photo. **First non-empty result wins** — no cross-candidate scoring.
7. Generic paid Gemini Vision (`_SALES_STATEMENT_VISION_PROMPT`, 3 attempts) → `_apply_parsed_sales_json` → ~25 "looks like X → re-read with reader X" repairs (:42166–42397).
8. Tesseract → Gemini text structuring (`tesseract_plus_gemini_text`) → `_parse_vikash_ocr_text` heuristic.
9. Back in `extract_sales_statement`: `apply_direct_stock_image_gemini` (second quality gate `assess_stock_structured_quality`; may call PSR Vision / Medica Vision / `STOCK_IMAGE_VISION_PROMPT`), then (only if not decided) `maybe_apply_gemini_fallback`.

All Tesseract calls go through `GatedPytesseract._stock_ocr_call` → `stock_ocr_policy` (1 initial + 1 recovery per page/region/operation, reuse otherwise) — added in the previous fix.

### 1b. PDF branch (`_parse_pdf` :27045)

~50 format-specific parsers tried in sequence on embedded text (`fitz` `page.get_text`), first match wins, most set `statement_count=1`. Then `_maybe_early_vision_for_image_only_pdf` (:25789, embedded chars < 40 → OCR sample → quality → `KEEP_OCR_PARSER` for known scan formats, else Gemini early escape). Then generic per-page path (:27453): per page ≤ `SALES_PDF_MAX_PAGES=20`, embedded text or `_ocr_pdf_page_text` (:12651, zoom `SALES_PDF_RENDER_ZOOM=2.0`), `_group_pdf_pages_by_stockist` (:12666), `_extract_statement_from_pdf_group` (:15745) per group; >1 group → `multi_statement` + `statements[]`.

### 1c. TXT (`_parse_txt` :5860)

`_parse_daily_stock_summary_txt` (:5674, Op./Pur./Cl. Stock, can emit multi-statement by month) → `_parse_fixed_sales_stock_statement` (:4983; true fixed-width using header character windows `_sales_stock_column_plan` :4910) → `_parse_product_stock_report` (:28829; header alias colmap `_psr_colmap_from_headers` :28214, else positional `[Opening, Purchase, Total, Sale, SaleRet, Exp/Dmg, Closing, Cls Amt, Order Qty]` :28309) → Kaveri regex rows (:5958) → `_parse_text_stock_fallback` (:5496). No OCR, no Gemini (`unavailable_text_only`).

### 1d. DOC/DOCX (`_parse_word` :6201)

`.docx`/PK → `_parse_docx` (:7555): specialised readers (item_description, profitmaker, wellness dump, opening/receipt/issue, textbox SSA, sale_closing) else python-docx paragraphs+tables with `_map_stock_sales_detail_headers`. Legacy `.doc`: `_convert_doc_to_docx_bytes` is **Windows Word COM only** (always `None` on this Linux host), so it falls to `_extract_legacy_doc_text` (olefile UTF-16 runs) → `_parse_txt` with `extraction_method=legacy_doc_text` (a weak method). Embedded images are **not** read in the primary path; only `gemini_extraction_fallback._word_parts` (:235) renders text+tables to JPEG and attaches `doc.part.rels` image blobs for Vision.

### 1e. XLS/XLSX (`_parse_xls` :11211)

HTML-masquerading `.xls` → `_parse_html_excel_opstock_rcpts_clstk` / `_parse_htm`. openpyxl POD hospital early exit. Per sheet (`_xls_iter_sheets` :9626; xlrd for .xls, openpyxl for .xlsx): prompt datewise, ZL secondary, Marg ERP, Stock&Sale analysis, Marg M.EXP, Marg opening/receipt/issue, Marg qty-value dump, else `_xls_fill_from_rows` (:12042) with `_xls_best_header` (:9494, score ≥ 6) + `_XLS_HEADER_ALIASES` (:9017). Visible sheets merged by product-name superset; `_xls_finalize_result` (:9687). **No OCR ever.** Gemini only via `maybe_apply_gemini_fallback._sheet_image` (first 60×10 cells rasterised to a JPEG — i.e. Vision reads a synthetic picture of the spreadsheet, not the cells).

---

## 2. `/split-and-extract` — call graph (for comparison only; not to be changed)

```
split_and_extract_invoices                     app.py:27672
  request_processing_semaphore (MAX_CONCURRENT_REQUESTS=1, REQUEST_QUEUE_TIMEOUT=3600 → 429)   :369/27765
  begin_request_progress / heartbeat (REQUEST_STUCK_THRESHOLD_SECONDS=1800)                      :381–560
  ingest file | split_raw_url | split_raw_blob_path; Excel → build_split_extract_response_from_excel; image → PDF
  fitz.open; ThreadPoolExecutor(effective_ocr_pool_workers ≤ MAX_TESSERACT_CONCURRENCY=6)
    per page: extract_full_invoice_data_combined                                  :23554
      T1 extract_text_with_pdfplumber (>100 chars, conf 95)                       :1029
      T2 page.get_text (>100 chars, conf 90)                                       :23669
         → extract_full_data_from_text_gemini (build_invoice_prompt, call_gemini_with_quota) :25211/24740/885
      T3 _quick_page_quality_check (zoom 1.5, top 30 %, viable = chars>30 ∧ conf>55 ∧ keyword) :23177
         extract_text_with_tesseract (zoom 2.5, threshold 150, default PSM; accept chars>100 ∧ conf>60) :1076
         extract_text_with_tesseract_relaxed (never rejects; 180° retry)            :1203
         post-OCR issue gate (brackets>20 / corrupted header / alnum<0.4; ≥2 → Vision) :23823
         Gemini text + line-item math validation (qty×price≈total ±10 %, ≥40 % verified) :23889
      T4 extract_full_data_from_image_gemini (zoom 1.5 render → Vision)             :25794
  collect_page_futures (OCR_PAGE_FUTURE_TIMEOUT_SECONDS=120)                      :27932
  group pages → invoices (_should_attach_page_to_current_invoice_group :16650, split_ocr_by_invoices)
  multi-page re-extract with combined OCR text                                     :28249
  enforce_schema (GSTIN clean, IRN recovery, qty/price swap fixes, reconcile_items_with_taxable_total) :18120
  build_pdf_from_pages :26732; upload_split_pdf_to_blob :27590; JSONResponse :29013
```

Reusable infrastructure: PDF open/render (`fitz.Matrix(2.5)`), `tesseract_ocr_slot`, `run_tesseract_call` (`services/reliability.py:803`), `collect_page_futures`, progress heartbeat, `call_gemini_with_quota` + 429 cooldown (shared via `sales_generate_content_via_vertex`), page-grouping/continuation logic. Not reusable: invoice prompt, GSTIN/IRN/qty×price rules, vendor-specific fixes.

---

## 3. Side-by-side comparison

| Dimension | `/split-and-extract` (POD) | `/extract-sales-statement` (stock) |
|---|---|---|
| Entry concurrency | 1 request; page pool ≤ 6 | 2 requests (`MAX_CONCURRENT_EXTRACTIONS`); OCR gated per request |
| Text-native detection | pdfplumber > 100 chars → conf 95; PyMuPDF > 100 → 90 | `classify_sales_document` ≥ 40 chars; `_parse_pdf` 40-char OCR trigger |
| OCR stages | probe → full → relaxed (3 bounded stages, fixed order) | up to ~25 format probes + bands/crops; now bounded by `stock_ocr_policy` (1+1 per region) |
| OCR acceptance | explicit char/conf thresholds (chars>100 ∧ conf>60) | `assess_source_text_quality` heuristic score (header hits, noise, numeric density); per-parser regexes |
| Gemini trigger | deterministic: probe fail, ≥2 OCR issues, unverifiable line items, 0 items | `evaluate_extraction_quality.should_fallback`, then 5 layout allowlists, then `stock_image_vision` second gate; many bypasses |
| Gemini input | rendered page PNG (zoom 1.5) | original bytes (direct vision), rendered PDF page (zoom 2.5), or synthetic JPEG of spreadsheet/text |
| Result validation | line-item arithmetic cross-check vs OCR text, GSTIN/IRN regex | stock identity (flag), quality score; identity ignored for non-generic kinds |
| Multi-page | page futures → group by invoice continuation → re-extract combined | PDF: group by stockist header; images: single page only |
| Retry/timeout | page future 120 s, Tesseract call timeout optional, Gemini 60 s | 600 s request deadline, Tesseract 60 s, Gemini `sales_gemini_timeout_seconds(120)` |
| Caching | none | per-request OCR cache + policy ledger |
| Output | invoice wrapper JSON | `empty_result` contract (+ `statements[]`) |

---

## 4. Every path where OCR is accepted and Gemini is NOT called

### 4a. Current tree

| # | Condition | Code | Why it is a risk |
|---|---|---|---|
| 1 | `evaluate_extraction_quality` returns `should_fallback=False` | `gemini_extraction_fallback.py:498-500` → `_stamp(..., "not_called")` | the only numeric check inside is identity, and identity is disabled for most kinds (#2, #3) |
| 2 | Identity only counts for kind `""`/`opening_receipts_sales_closing` | `extraction_quality.py:208-214 _identity_applies` | SaleRet (`opening_purchase_sale_saleret_expdmg`), Sample, OPBAL, ZL, Medica… never produce `stock_identity_failure` no matter how many rows fail |
| 3 | Protected named parser → score 100 | `extraction_quality.py:421-455` | any method not in `_GENERIC_METHODS ∪ _WEAK_OCR_METHODS` with ≥1 valid name and no header-as-product rows is "good" regardless of numbers |
| 4 | Identity gate 20 % (`GEMINI_EXTRACTION_MAX_IDENTITY_FAILURE_PERCENT`) | `extraction_quality.py:369,431-447` | up to 1 in 5 rows may fail silently |
| 5 | Layout allowlists after `should_fallback=True` | `gemini_extraction_fallback.py:505-568` | `main_stock_sales_statement`, `opening_receive_issue_closing`, `code_item_*`, `op_pur_sp_sale_bal_val*`, `pack_op_pur_bal_stock_sale*`, `medivision_op_purc_nm60d`, `product_wise_stock_statement_image` skip Gemini even when the gate says fallback |
| 6 | `stock_image_vision_decided` short-circuit | `sales_statement_extractor.py:4863-4867`; `stock_image_vision.py:669-671` | once `assess_stock_structured_quality` says `needs_direct_gemini=False` (`final_selected_extraction=structured`), `maybe_apply_gemini_fallback` is never run — no `gemini_fallback` stamp at all |
| 7 | Trusted methods/layouts in `stock_image_vision` | `stock_image_vision.py:115-147, 328-345` | identity rate ≥ 0.25 only triggers Gemini for non-trusted & non-vision methods |
| 8 | `KEEP_OCR_PARSER reason=KNOWN_IMAGE_FORMAT` | `sales_statement_extractor.py:40601-40625` | 22 regex detectors on a psm-6 sample route to OCR readers before any quality check |
| 9 | `KEEP_OCR_PATH` | `:40663-40673`, PDF `:25869-25881` | OCR text "looks like" a stock statement (≥2 header hits, low noise) → whole probe cascade trusted |
| 10 | First-match-wins probe cascade | `_parse_image :41938-42123`, `_parse_pdf :27059-27450` | a wrong but non-empty parser result ends the search; no candidate comparison |
| 11 | Vision-locked results | `stock_direct_vision.py:704-723` | correct by design (Vision already ran) but `validate_stock_direct_vision` is flag-only, so a column-shifted Vision answer is also final |
| 12 | `ENABLE_GEMINI_EXTRACTION_FALLBACK=false` | `gemini_extraction_fallback.py:491` | returns silently without stamp |
| 13 | TXT/HTML | `:570-577` | `unavailable_text_only` (correct — no visual page) |
| 14 | Medica / MediVision / RTL / batchwise / ZL branches force `stock_identity_fail_count=0` | `sales_statement_extractor.py:3644,3699,3747,3783,3823,3850,4005,4078,4565` | identity disabled by construction, so #1 can never fire |

### 4b. The production case (`TRIAGE_GENERIC_SEMANTIC`)

Produced by the wiped Sep-29/30 generation. Sequence (reconstructed from journal + bytecode):
1. `triage_stock_statement_image` ran 1–2 baseline OCRs, `_semantic_from_baseline_words` matched headers `Opening/Purchase/Total/Sale/SaleRet/Exp/ClosStock` → `semantic_schema=opening_purchase_total_sale_saleret_exp_closing`, `confidence=0.99`, `recommended_path=GENERIC_SEMANTIC` (journal: `TRIAGE_DECISION path=GENERIC_SEMANTIC … confidence=0.990 reasons=phone_portrait,semantic_schema:…`).
2. `try_generic_semantic_from_triage` → `stock_statement_column_resolver` (header x-ranges, greedy left-to-right) + `stock_statement_numeric` (token → nearest column) + `stock_statement_reconciliation` (`identity_status ∈ {VALID, RECOVERED, AMBIGUOUS, INVALID}`, "derive exactly one MISSING field when all other inputs are trusted") → `extraction_method=generic_column_semantic`, `document_type=generic_opening_purchase_total`.
3. `should_use_generic_vision_fallback` returned `use=False, reason=TRIAGE_GENERIC_SEMANTIC` because `column_schema.confidence ≥ 0.72`, columns non-empty, rows ≥ 3 — evaluated before `quality.should_fallback`, so 26 identity failures were irrelevant.

Root cause in one sentence: header-detection confidence was used as a proxy for numeric correctness, and the identity validator's output had no veto. The current tree does not have that module, but rows #2, #3, #5, #6 above reproduce the same failure mode for different layouts.

---

## 5. Vision-first routing rules (current)

`classify_stock_direct_vision` (stock_direct_vision.py:600-701), evaluated in order:

| Order | Signal | Route | Gemini input |
|---|---|---|---|
| 1 | order-form headers / `hw.order_form` | `handwritten_order_form` | original bytes (+ EXIF-upright candidate) |
| 2 | handwritten ∧ (saleret headers ∨ `_ZA_`) | `handwritten_stock_statement` | original |
| 3 | Product-wise headers (Opening/Receipt/Issues/Closing) | `product_wise_stock_statement` → **OCR geometry**, no Gemini | n/a |
| 4 | PharmAssist "Stock and Sale Report" + BVal/SVal | `pharmassist_stock_sale` | original |
| 5 | regex `SaleRet|ClosStock|Exp/Dmg` | `product_stock_report_saleret` | original (2 candidates) |
| 6 | `OPSTK` ∧ `PURCH`, or `STOCK STATEMENT` + OPSTK/PURCH/SALE VAL | `medica_opstk` | strips |
| 7 | garbled Open+Purch+Sale+(Total|Clos) | `product_stock_report_saleret` | original |
| 8 | filename `_ZL_\d+` | `medica_opstk` (after a header-band OCR peek to exclude Product-wise) | strips |
| 9 | filename `_ZA_\d+` | `product_stock_report_saleret` | original |
| — | none | return None → OCR path | — |

Other image routes to Vision: `_maybe_early_vision_for_image` specialised readers when `should_fallback` (pack/Op/Pur/Bal, code_item, Medica), the generic paid Vision at the end of `_parse_image`, and `apply_direct_stock_image_gemini` when `assess_stock_structured_quality` fails.

Assessment: routing is too dependent on (a) filename portal codes and (b) regex header spelling in a single psm-6 OCR sample. Rules 8–9 pick a Vision prompt/schema for the whole file from the filename; rules 3 and 5–7 depend on Tesseract spelling "SaleRet"/"OPSTK" correctly. Rule 3 sends Product-wise sheets to OCR geometry, not Vision. Image quality is used only to decide OCR vs Vision after the OCR sample, not to pick the Vision schema. Extension is used only for the top dispatch. There is no document-type classifier that works from the image itself.

---

## 6. Column-assignment risk (the biggest accuracy risk)

Short answer: yes — in every OCR path numeric tokens become authoritative field values before the column set is conclusively known. Per path:

| Q | Fixed-width TXT (`_sales_stock_column_plan`) | Product Stock Report text/OCR (`_map_psr_numbers`) | Marg main stock OCR (`_main_stock_parse_ocr_line`) | OpBal / LstSL / qnty photos | Header-centre readers (pwss, A2Z, portrait balance, ezeal, pack_mexp) | Excel (`_xls_fill_from_rows`) | Direct Vision |
|---|---|---|---|---|---|---|---|
| 1 Product name | text before first column window | text before first numeric run (`_split_psr_name_and_numbers` :28250) | leading text | leading text | words left of first header centre | alias `product`/`item` column | model |
| 2 Opening | header token "Op/Opening" → char window | alias colmap by header order, else position 0 | **last 8 tokens, slot 0** (:36054) | trailing pool slot 0 | nearest header centre `OPENING` | alias | model + prompt order |
| 3 Purchase/Receipt | "Receipt" window | alias / position 1 | slot 2 | slot 1 | `RECEIPT` centre | alias | model |
| 4 Total | "Total" window | alias / position 2 | n/a | OpBal slot 2 (Total ← Op+Receipt when truncated, :2508) | n/a | alias | `total_qty` |
| 5 Sale | "Issue" window | alias / position 3 | slot 4 | slot 3 | `ISSUE` centre | alias | model |
| 6 Sale Return | n/a | alias / position 4 | n/a | n/a (SwilERP Retrn parser only) | n/a | alias `saleret` | `sales_return_qty` |
| 7 Exp/Dmg | n/a | alias / position 5 | n/a | n/a | n/a | alias | `expiry_damage_qty` |
| 8 Closing | "Closing" window | alias / position 6 | slot 6 | last slot | `CLOSING` centre | alias | model |
| 9 Value vs qty | sub-header "Qty/Value/Amt/Rate" text | Cls Amt position 7; `_psr_repair_cls_amt_scale` ÷10/100 | odd slots = value | decimals/size heuristics | not distinguished (qty-only layouts) | alias (`*_val`, `amount`) | prompt |
| 10 Token → column | char offset | token order | token order | token order | x-centre nearest within `pitch*0.65` | cell index | visual |
| 11 Adjacent ambiguity | none: wrong window silently | none: shift all right of a dropped cell | none: a dropped cell shifts every later slot | `_repair_opbal_qty_tuple` / `_a2z_balanced_qtys` pick values that balance | nearest-centre tie → first | none | prompt says "do not shift"; `validate_stock_direct_vision` flags only |

Concrete wrong-column-with-right-digits cases visible in code: 
- `_main_stock_parse_ocr_line` last-8 rule: a dropped ISSUE cell makes CLOSE qty land on ISSUE and CLOSE VAL on CLOSE qty; `_main_stock_repair_issue_qty` then "fixes" it arithmetically.
- `_map_psr_numbers` positional fallback: when `Total` is not printed the whole row shifts one column left (Sale read as Total …).
- Generic Vision `_vision_filed_sale_qty_as_receipt` / `_refile_sale_qty_held_as_receipt`, `_looks_like_stock_valuation_misfiled → _remap_stock_valuation_sales_to_closing`, `_looks_like_code_item_photo_misread`: each is an admission that the generic prompt misfiles columns and is patched after the fact by heuristics.
- PharmAssist Jul/Jun → opening/receipts (fixed yesterday) was exactly this class.
- `stock_image_vision._apply_detected_cells` trusts Gemini `detected_columns[].semantic` without checking x-order monotonicity.

---

## 7. Stock identity validation

### 7a. Formulas implemented (`_apply_stock_identity_validation` :3566, kinds :27545-27548, `_stock_expected_closing` :27652)

| Kind / layout | Formula | Lines |
|---|---|---|
| `opening_purchase_sale_saleret_expdmg` (SaleRet) | total=opening+purchase; closing=total−sale+sale_return−exp_damage | 3593, 27660 |
| `opening_purchase_sale_sample_expclos` | closing=total−sale−sample−exp_clos | 3597, 27664 |
| `opening_receipt_issue_closing` (OpBal / ps_pharma) | closing=opening+receipt−issue | 3601 |
| generic `opening_receipts_sales_closing` [+scheme] | closing=opening+receipts−sales[−sales_scheme] | 3605-3617 |
| Monthly SS | diagnostic only | 3404-3459 |
| Himalaya dump | closing=opening+receipts−sales−shortage | 3482-3555 |
| Medica OPSTK, MediVision NM60D, summary RTL, batchwise, valuation, ZL opening/primary/closing | printed closing trusted, `fail_count=0` | 3622-3856, 4524-4581 |
| item_pack_sreturn_others / Marg sale-purchase analysis | subtotal=op+pur+sret+others_in; closing=subtotal−sale−pret−others_out | 3902-3952, 4301-4366 |
| Product-wise | closing=opening+receipt−issues | 4098-4194 |
| ssa_sales_free | closing=total−sales−free−sample−tf−pr−repl | 4214-4279 |
| stock_sales_op_pi_clqty | op+PI+st_in+in−out−sale−st_out+adj | 4379-4410 |
| Vision PSR grid / PharmAssist | flag only, tolerance 1.01 | stock_direct_vision.py:810-865 |

Different formats legitimately need different formulas (SaleRet/Exp columns, Free/Scheme, Shortage, NM60D near-expiry, qty-only or closing-only sheets). The current design handles this by `stock_identity_kind`, but the quality gate only honours one kind (section 4 #2), so the richer formulas have no effect on routing.

Classification today is binary (`stock_identity_ok` true/false, aggregate `stock_validation.is_valid`). There is no VALID / MINOR_DISCREPANCY / COLUMN_ASSIGNMENT_SUSPECTED / OCR_VALUE_SUSPECTED / MISSING_VALUE / STRUCTURAL_ERROR classification in the current tree. (The wiped reconciliation module had VALID/RECOVERED/AMBIGUOUS/INVALID.)

Does the implementation accept structurally wrong extractions because the score stays high? Yes: protected parsers score 100 before identity is looked at for non-generic kinds; `assess_stock_structured_quality` gives trusted readers `extraction_confidence=0.9` and suppresses identity for them; `_result_is_weak` only considers emptiness/noise reasons, never identity.

### 7b. Code that rewrites values from arithmetic (section 11 of the request)

| Function | Lines | Behaviour | Marked derived? |
|---|---|---|---|
| `_main_stock_repair_issue_qty` | 36335-36379 | sets `sales_qty = opening+receipts−closing`; sets `opening_qty = closing+sales−receipts`; digit-drop replacement (34→4 ⇒ implied); zero-movement `opening=closing` | **No** |
| `_psr_repair_qty_identity` | 27847-27986 | opening/receipts/sales/total from identity or truncation | yes (`*_from_total`, `sales_qty_from_identity`) |
| `_psr_overlay_ocr_qty` | 27993-28037 | overlays OCR digits on Vision qty | `qty_from_ocr` |
| `_psr_fill_missing_sales` | 28066-28116 | sales_value = Cls Amt rate × sales_qty | `sales_value_from_cls_amt_rate` |
| `_psr_repair_cls_amt_scale` | 27743-27769 | closing_value ÷10 / ÷100 | `cls_amt_scale_repaired` |
| `_repair_pack_op_pur_bal_dropped_receipts` | 14293-14324 | receipts = implied purchase | `receipts_qty_repaired` |
| `_repair_pack_op_pur_bal_money_as_qty` | 14327-14367 | zeros qty / moves to purchase_value | `money_as_qty_*` |
| `_repair_code_item_photo_qty_columns` | 18567-18623 | swaps/fills opening/receipts/sales/closing when a permutation balances | **No** |
| `_repair_marg_mexp_missing_issue` | 10408-10440 | opening or sales from identity | **No** |
| `_repair_medivision_nm60_closing` | 20670-20690 | closing ← Op+Purc−Sale | **No** |
| `_repair_medivision_op_wsale_sales` | 20841-20870 | sales from whichever side column balances | **No** |
| `_repair_pharma_hub_jun_jul_sales` | 21080-21106 | sales from Jun/Jul/stock_out if balances | **No** |
| `_a2z_balanced_qtys` | 37316-37398 | fills missing O/R/I/C so identity holds | **No** |
| `_pwss_photo_repair_row` | 26485-26528 | closing digit-drop fix | **No** |
| `_repair_opbal_qty_tuple` | 2508-2529 | Total ← Op+Receipt | **No** |
| `_refile_sale_qty_held_as_receipt`, `_remap_stock_valuation_sales_to_closing` | 42218, 42289 | column moves driven by heuristics | partial (`extraction_method`) |

Explicitly non-rewriting (good): `_apply_stock_identity_validation`, `validate_stock_direct_vision`, `normalize_stock_gemini_extraction`, `_FALLBACK_PROMPT` ("Do not calculate").

---

## 8. Multi-page handling

- PDF: `_group_pdf_pages_by_stockist` groups pages by stockist header text; continuation pages inherit (commit b6463db). `statement_count`/`statements[]` built at :27502-27508, SwilERP Retrn :2929-2944, company-wise :22091, daily TXT :5851-5857; `_group_statements_by_stockist_month` (:585) merges same stockist+month after the fact. Column headers are not carried from page 1 to page N in the generic path (the wiped column resolver had `no_headers_inherited_prior_page`; nothing equivalent exists now).
- Gemini fallback sends at most `GEMINI_EXTRACTION_FALLBACK_MAX_PAGES=4` pages per call (`_pdf_images`), so a 10-page scan is truncated silently.
- Images: always single page; no multi-image statement support.

## 9. XLS/XLSX

Deterministic (openpyxl/xlrd); alias header detection (`_xls_best_header` score ≥ 6); hidden sheets flagged; multi-sheet merge by product-name superset (can merge unrelated sheets). Formula cells read via `data_only=True` (cached values; empty if the file was never recalculated). No OCR. Gemini fallback rasterises 60×10 cells — loses everything beyond column J / row 60. Recommendation: never rasterise; if a sheet cannot be mapped, return STRUCTURAL_ERROR with header candidates rather than invoking Vision.

## 10. TXT fixed-width

`_parse_fixed_sales_stock_statement` is the only true column-window parser (uses header offsets + stride, sub-header Qty/Value). `_parse_daily_stock_summary_txt` and `_parse_product_stock_report` are regex/token-order. Risk: proportional-font exports or tab-separated files break the stride assumption; `_parse_text_stock_fallback` (:5496) then guesses. Gemini correctly not used.

## 11. DOC/DOCX

`.docx` tables parsed natively (good). `.doc` on Linux: COM conversion is dead code → OLE text scrape → TXT heuristics, method `legacy_doc_text` (weak) → Gemini fallback renders text to an image. Journal today shows `0000736672_…ZA_37_215….doc parser=legacy_doc_text OCR_quality=100 decision=KEEP_PARSER` — scraped text accepted with score 100. Embedded images inside Word (scanned statements pasted into .doc) are never extracted in the primary path. Recommendation: add LibreOffice `soffice --headless --convert-to docx` (or `antiword`/`textract`) for .doc, and treat image-only Word documents as images.

## 12. Output contract

`empty_result` (:73): `source_file, source_format, stockist_name, stockist_address, company_name, period_from, period_to, report_title, line_items[], totals{sales_value, closing_value, extra{}}`. `empty_line_item` (:92): `product_code, product_name, packing, opening_qty, receipts_qty, sales_qty, sales_value, closing_qty, closing_value, extra{}` — note defaults are `0.0`, so "not printed" and "zero" are indistinguishable unless the parser explicitly nulls. `total_qty`, `sales_return_qty`, `expiry_damage_qty` live in `line_items[].extra` (keys vary: `sale_return`, `sale_return_qty`, `saleret`, `exp_damage`, `exp_dmg`, `expiry_damage`). Multi-statement: `{multi_statement: true, statement_count, statements: [empty_result…]}`. `totals.extra` metadata is free-form (`extraction_method`, `layout`, `numeric_source`, `gemini_fallback`, `extraction_quality`, `stock_identity_*`, `stock_validation`, `quality_decision`, `final_selected_extraction`, `gemini_input`, `GEMINI_RAW_EXTRACTION`, …). Laravel contract depends on the top-level keys and `line_items[]`; it does not depend on `extra` contents (per endpoint docstring).

## 13. Performance vs accuracy

- OCR is now bounded (policy ledger), so wall time is dominated by Gemini: 429 cooldown loops observed up to 411 s per request yesterday; `MAX_CONCURRENT_GEMINI_REQUESTS=2`.
- The image path can issue up to 5 Gemini calls per file today (direct vision ×2, generic vision, format-specific re-read, stock_image_vision post-pass, fallback). A canonical Vision-first design would be ≤ 2 (primary + targeted recovery), i.e. fewer calls than now.
- Deterministic parsers (XLSX/TXT/DOCX/text PDF) are milliseconds and should never spend a Gemini call.
- PDF rendering at zoom 2.5 ×4 pages is cheap relative to Gemini latency.

---

## 14. Recommended canonical architecture

```
DOCUMENT
  └─ Stage 0  Document classifier (no OCR): extension + magic bytes; for PDF: embedded-text ratio per page;
              for images/scan pages: cheap photo-quality + "is table" signal (edge density), NOT filename codes.
  ├─ Native lane (deterministic, no Gemini):
  │     XLSX/XLS → cell grid → header resolver (alias + x-order check) → rows → validation
  │     TXT     → fixed-width window / regex parsers → validation
  │     DOCX    → python-docx tables → header resolver → validation
  │     text-PDF (table structure reliable: ≥ N numeric columns aligned by x) → pdfplumber words with x → header resolver → rows → validation
  │     .doc    → soffice --convert-to docx → DOCX lane; if image-only → Visual lane
  └─ Visual lane (JPG/PNG, scanned PDF pages, text-PDF whose structure check fails, Word with embedded scans):
        original bytes (or page render at zoom 2.5 for PDF)
          → Gemini Vision with ONE schema-agnostic strict prompt: return headers_as_printed[], rows[] with
            cells keyed by header text + x-order, plus canonical mapping proposal
          → deterministic header→canonical mapping (one shared resolver, same one as the native lane)
          → schema/column validation (monotonic x-order, no duplicate canonical, qty vs value by header)
          → identity validation → row classification
               VALID | MINOR_DISCREPANCY (|Δ| ≤ 1) | COLUMN_ASSIGNMENT_SUSPECTED (a permutation balances)
               | OCR_VALUE_SUSPECTED (one digit-drop/insertion balances) | MISSING_VALUE (null cell) | STRUCTURAL_ERROR
          → targeted second Vision ONLY for rows classified SUSPECTED/MISSING (send page + row list, ask to re-read those rows)
          → never rewrite from arithmetic; store classification + alternatives in extra
  Shared: /split-and-extract infrastructure — fitz render, run_tesseract_call (only for the Stage-0 probe if ever),
          sales_generate_content_via_vertex (+ 429 cooldown), progress heartbeat, deadline.
  Output: empty_result contract + canonical extras:
          totals.extra.schema, columns_detected[], row_status_counts{}, gemini_calls, numeric_source, derived=false always.
```

Can Gemini Vision be canonical for images/scanned PDFs/visual PDFs? Yes, with caveats already demonstrated by `stock_direct_vision`: (a) it must receive original pixels (not OCR text); (b) the prompt must ask for printed headers and cell positions rather than a pre-chosen schema (today the schema is chosen from filename/regex); (c) a deterministic validator must have veto power; (d) a targeted second call must be row-scoped. Evidence: HIORA 100 GM 9/50/59/21/0/0/38 and 50 GM 80/0/80/5/0/0/75, ZL product-wise, PharmAssist CONFIDO 63/0/2/61 all came out right under this pattern, while every OCR geometry reader needed layout-specific repair code. The existing 25 "looks like X → re-read" branches and 14 arithmetic repair functions are the cost of OCR-first.

### Per-file-type strategy

| Type | Primary truth | Secondary | Validation | Gemini when |
|---|---|---|---|---|
| A JPG/PNG | Gemini Vision on original bytes (schema-agnostic prompt) | targeted second Vision for flagged rows | header resolver + identity classifier | always (1 call), +1 targeted if flagged rows |
| B scanned PDF | per-page render → Vision (≤ 4 pages per call, iterate all pages) | targeted second Vision | same + page continuity (carry headers) | always |
| C text-native PDF | pdfplumber words with x-coordinates → header resolver | Vision on rendered page if structure check fails or STRUCTURAL_ERROR | same | only on structure failure |
| D DOC/DOCX | python-docx tables (after soffice conversion for .doc) | Vision on embedded images / rendered page | same | only if no tables or image-only |
| E TXT | fixed-width / regex parsers | none | identity classifier | never |
| F XLS/XLSX | cell grid + header resolver | none (return STRUCTURAL_ERROR with candidates) | identity classifier | never |

---

## 15. Files / functions to change (when approved) and tests

Change list (no edits made):
- `services/extraction_quality.py` — `_identity_applies` (208-239): honour all `stock_identity_kind`s; `evaluate_extraction_quality` (421-455): remove unconditional protected-parser score 100; add row classification output.
- `services/gemini_extraction_fallback.py` — `maybe_apply_gemini_fallback` (505-568): replace five layout allowlists with the row-classification veto; `_result_is_weak` (399-427): include identity; `_pdf_images` (173-186): iterate all pages in batches.
- `services/sales_statement_extractor.py` — `extract_sales_statement` (4855-4867): single post-pipeline gate instead of two (`apply_direct_stock_image_gemini` + `maybe_apply_gemini_fallback`); `_parse_image` (41734-42507): collapse probe cascade behind the classifier; `_maybe_early_vision_for_image` (40503+): stop using filename codes to pick schema; the 14 repair functions in section 7b: convert to classifiers that write `extra.row_status`/`alternatives` instead of mutating; `_parse_word` (6201-6224): Linux conversion; `empty_line_item` (92): consider `None` defaults with explicit zeros from parsers (contract change — needs Laravel sign-off).
- `services/stock_direct_vision.py` — `classify_stock_direct_vision` (600-701) → schema-agnostic prompt; `validate_stock_direct_vision` (753-893) → emit 6-way row classification; add `extract_rows_targeted_vision(file_bytes, rows)`.
- `services/stock_image_vision.py` — `assess_stock_structured_quality` (285-411): remove trusted-method identity suppression; `_apply_detected_cells` (461-501): enforce x-order monotonicity.
- New shared module (single header→canonical resolver used by XLSX/TXT/DOCX/PDF-text/Vision) — replaces `_XLS_HEADER_ALIASES`, `_psr_colmap_from_headers`, `_sales_stock_label_kind`, per-reader centre finders.
- `app.py` — none required; `/split-and-extract` untouched.

Tests to add (fixtures already on disk: `0000734827_…ZA….jpg`, `0000734860_…ZA….jpg`, `0000700400_…ZL….png`, `51f649e2-….jpg`, `0000736197_…ZL….jpg`):
1. Identity veto: a protected-parser result with SaleRet kind and 26/32 failing rows must produce `should_fallback=True` and a Gemini call (reproduces the TRIAGE_GENERIC_SEMANTIC case).
2. Row classification golden: VALID / MINOR / COLUMN_ASSIGNMENT_SUSPECTED / OCR_VALUE_SUSPECTED / MISSING / STRUCTURAL each with one synthetic row.
3. No-rewrite invariant: for every repair function retained, assert line_items qty unchanged and `extra.alternatives` populated.
4. HIORA 100 GM / 50 GM and PharmAssist CONFIDO/LIV 52/PILEX values preserved (already exist; keep).
5. Gemini call budget per image ≤ 2; OCR calls per image ≤ 2 (policy ledger already tested).
6. Multi-page scanned PDF (6 pages) → all pages processed, headers carried.
7. `.doc` on Linux converts via soffice; image-only `.docx` routed to Visual lane.
8. XLSX with 15 columns / 200 rows must never call Gemini.
9. Filename-independence: same image under `ZA`, `ZL` and neutral names yields identical output.

## 16. Phased migration plan

Phase 0 (no behaviour change): add row classifier + `row_status_counts` to `totals.extra`; log-only veto decisions (`WOULD_FALLBACK`) alongside current decisions; collect a week of production comparisons. Also add `soffice` availability check.
Phase 1: turn the identity veto on in `evaluate_extraction_quality`/`maybe_apply_gemini_fallback` for image and scanned-PDF inputs only; keep allowlists but make them log when overridden. Regression suite from section 15.
Phase 2: schema-agnostic Vision prompt + targeted second Vision; route A/B file types Vision-first; drop filename-based schema selection; collapse `apply_direct_stock_image_gemini` + `maybe_apply_gemini_fallback` into one gate.
Phase 3: convert the 14 arithmetic repair functions to classifiers (values unchanged, alternatives recorded); shared header resolver for XLSX/TXT/DOCX/text-PDF.
Phase 4: retire the OCR probe cascade for images (keep OCR only for the optional Stage-0 probe), remove dead Word COM path, batch multi-page Vision.
Each phase gated by: HIORA/ZL/PharmAssist fixtures unchanged, ≤ 2 Gemini calls per file, zero `/split-and-extract` diffs, and the production comparison from Phase 0 showing no regressions on named formats.
