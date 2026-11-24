# GT-20 OCR Accuracy Root-Cause Investigation

Created: 2026-11-24T09:04:03.025152+00:00

## Scope and method

- **Dataset:** SOURCE_VERIFIED GT-20 (`phase4/ground_truth_50/seed_index_20.json`)
- **OCR input:** stored PaddleOCR-VL pages (`phase4/paddle_vl_benchmark/per_document`)
- **Extraction score:** Phase2-OFF arm from `phase4/phase2_ab_gt20` (`vl_html_table_parse`)
- **Constraint:** read-only; GT/production/benchmark inputs unchanged; no stock-equation guessing; no LLM quantities
- **Artifacts:** `investigation_aggregate.json`, `incorrect_quantity_traces.json`, `investigate_gt20_qty_errors.py`

## Headline finding (confirmed)

Published **field accuracy 51.8%** / **quantity accuracy 46.5%** measure **extraction JSON scoring**, not raw OCR digit quality.

| Evaluation | Quantity accuracy | Source |
|---|---:|---|
| Direct OCR/HTML `match_field` (stored VL) | **99.3%** | `phase4/PADDLE_VL_GROUND_TRUTH_20_REPORT.md` |
| Extraction `score_extraction` Phase2 OFF | **46.5%** (134/288) | `phase4/phase2_ab_gt20/COMPARISON_REPORT.md` |
| Expected qty token present in VL text | **98.3%** (283/288) | this investigation |

Structurally valid VL HTML tables are **not** proof of numeric extraction accuracy: many tables parse, yet column mapping / product matching drop or zero the GT cells.

## Independent stage metrics (quantity fields only)

Denominators are SOURCE_VERIFIED `field_type=quantity` from the Phase2-OFF bench (n=288 unless noted). Metrics are **not** combined.

| Metric | Correct | Total | Accuracy |
|---|---:|---:|---:|
| Raw OCR numeric (expected token in VL text) | 283 | 288 | 98.3% |
| Table cell extraction (HTML cell @ product row + header) | 217 | 288 | 75.3% |
| Row alignment (GT product found in HTML cells) | 278 | 288 | 96.5% |
| Header mapping (scorer OR production matcher) | 265 | 288 | 92.0% |
| Final quantity accuracy (score_extraction) | 134 | 288 | 46.5% |
| Exact product-row accuracy (all qtys correct per GT product) | 29 | 101 | 28.7% |

Notes:
- Expected qty token present in stored VL text (not cell-aligned).
- HTML cell under GT product row + header matches expected.
- GT product string found as a cell in parsed HTML table.
- Printed header maps via scorer `_canonical_field` OR production match_headers.
- Phase2-OFF score_extraction quantity fields (published ~46.5%).
- All quantity fields for a GT product_key correct.
- Bounding boxes: **0 verified** in GT (`bbox` null); coordinates unavailable — marked n/a per field.

## Error classification (154 incorrect quantities)

| Cat | Name | Count | Confirmed | Share of incorrect |
|---|---|---:|---:|---:|
| A | OCR digit recognition | 3 | 0 | 1.9% |
| D | Header/column mapping | 73 | 70 | 47.4% |
| E | Product matching | 66 | 66 | 42.9% |
| G | Ground-truth or scoring mismatch | 9 | 9 | 5.8% |
| H | Other / undetermined | 3 | 0 | 1.9% |

### Confirmed vs suspected

- **Confirmed:** stage evidence shows the failure locus (e.g. extract has correct `opening_qty` but scorer returns UNMAPPED for `OpStk`).
- **Suspected / undetermined:** token-presence heuristics only, or conflicting signals — labeled `confirmed=false` / category H.

### Mechanism breakdown inside top categories

| Cat | Mechanism | Count | Confidence |
|---|---|---:|---|
| D | Product visible; qty columns unmapped / empty extract | 38 | confirmed |
| D | HTML cell correct; mapped line_item field wrong (Free vs Qty / shift) | 24 | confirmed |
| D | OCR garbage headers (`0g/0tv`); only product_name matched | 8 | confirmed |
| D | Expected digit in OCR; extract=0 (shift suspected) | 3 | suspected |
| E | HTML product row exists; missing from extract line_items | 37 | confirmed |
| E | Ambiguous pack / first stem match (e.g. RENALKA 100 vs 200) | 29 | confirmed |
| G | Scorer `_canonical_field` misses `OpStk` / `Bal.` synonyms | 9 | confirmed |
| A | Expected digit absent / disagreed in VL text | 3 | suspected |
| H | Insufficient evidence | 3 | undetermined |

## Pipeline stage map (where accuracy is lost)

```
Source page
  -> stored PaddleOCR-VL text/HTML     ~98% expected digits present
  -> HTML row for GT product           ~97% rows found
  -> cell under GT header              ~75% cells match GT
  -> column_matcher + parse_html_table  <-- major loss (D)
  -> product match into line_items      <-- major loss (E)
  -> score_extraction / _canonical_field <-- false negatives (G)
  -> published quantity accuracy        46.5%
```

Relevant production / bench functions:
- OCR store: `phase4/paddle_vl_benchmark/per_document/*.json`
- HTML extract: `services/vl_html_table.py::parse_vl_html_table`
- Table parse: `services/html_table/html_table_parser.py::parse_html_table`
- Headers: `services/html_table/column_matcher.py::match_headers`
- Cell grid: `services/ground_truth/html_table.py::parse_html_tables`
- Score: `scripts/benchmark_phase2_ab_gt20.py::score_extraction`, `_canonical_field`, `_find_line_item`

## Representative confirmed cases

### G — Scorer false negative (`OpStk`)

- Doc `0000700084...pdf` p1, product `ACTARIL SOAP`, field `OpStk`
- GT `64.0` | OCR/HTML cell `64.0` | extract `opening_qty=64` | scored predicted=null `UNMAPPED_HEADER`
- Cause: bench `_canonical_field` lacks `op.?stk`; production `match_headers` maps it
- Source: `scripts/benchmark_phase2_ab_gt20.py::_canonical_field`

### D — Garbage OCR headers → empty extract

- Doc `0000700124...pdf` p1, product `AACTARIL SOAP`, field `Op Qty`
- VL headers become `0g\n0tv` / `Sales\n0tv` / `Closure\n0tv`
- `match_headers` keeps only `product_name` → **0 line_items**
- GT qty tokens (20/144/40/124) exist on body rows; product OCR also mangled (`ANTIBIOL SOP`)
- Sources: stored VL text; `column_matcher.match_headers`; `html_table_parser.resolve_column_mapping`

### D — Free/Qty column collision

- Docs such as `0000700209`, `0000700275`: multi-header Purchase/Sales Free columns
- Warnings: `Purchase` also matches `receive_qty`; first Free-like column can win
- HTML cell under GT header often correct; line_item canonical field becomes `0`
- Source: `column_matcher.match_headers` first-match policy

### E — Pack ambiguity

- Doc `0000700209...pdf`, `RENALKA SYRUP` Op/Cl GT=12
- HTML has 100ML row (all 0) and 200ML row (12/12)
- `_find_line_item` returns first stem match → predicted 0
- Source: `benchmark_phase2_ab_gt20.py::_find_line_item` (+ extract lacking pack disambiguation)

### E — Product in HTML, absent from extract

- Common on docs with partial mapping / dropped rows / name decoration (`$^{*}$`, truncated names)
- Cell grid still tags `product_key`; sales extract line_items omit the row

## Top five root causes by impact

| Rank | Cause | Impact (incorrect qtys) | Confidence | Smallest targeted fix (DO NOT IMPLEMENT YET) |
|---:|---|---:|---|---|
| 1 | **D — Qty header mapping failures** (unmapped/garbage headers → empty or zeroed extract) | **73** | confirmed (69) | Strengthen header repair for OCR-mangled qty headers; prefer Qty over Free when both match; fail soft to positional stock columns when fuzzy qty set empty |
| 2 | **E — Product matching / pack disambiguation** | **66** | confirmed (66) | Match product+pack (or GT packing); require unique best match; avoid first-stem wins |
| 3 | **Cell→field binding after multi-row headers** (subset of D: correct HTML cell, wrong canonical) | **24** (of the 73) | confirmed | Align extract parser with `parse_html_tables` span-expansion / Free-vs-Qty repairs already used in GT eval |
| 4 | **G — Bench scorer header synonyms** (`OpStk`, `Bal.`) | **9** | confirmed | Add `op.?stk` / bare `Bal.` to `_canonical_field` (eval-only; does not change production) |
| 5 | **A — True OCR digit / residual misses** | **3** | mostly suspected | Low priority vs 1–4; only after mapping/matching fixed |

If ranks 1–4 were fixed without improving OCR, quantity accuracy could rise from **46.5%** toward the **~75% table-cell** / **~98% OCR-token** ceilings measured here — exact uplift depends on overlap between D and E on the same fields.

## Field accuracy context (51.8%)

- Verified fields: **388**; correct: **201** → 51.8%
- Quantity incorrect: **154**; other field misses: **33** (headers/text/unsupported)
- Fixing the quantity path dominates the headline field score

## Per-incorrect-quantity catalog (154)

Full structured traces (raw OCR flags, extract snapshots, column maps, reasons): `incorrect_quantity_traces.json`.

| Doc | Pg | Product | Field | GT | OCR token? | HTML cell | Extracted | Cat | Conf | Bench err | BBox | Source |
|---|---:|---|---|---|---|---|---|---|---|---|---|---|
| 0000700084_2026_08_Z…36.pdf | 1 | ACTARIL SOAP | OpStk | 64.0 | Y | 64.0 |  | G | Y | UNMAPPED_HEADER | n/a | scripts/benchmark_phase2_ab_gt20.py::_canonical_ |
| 0000700084_2026_08_Z…36.pdf | 1 | TALEKT CAP. | OpStk | 35.0 | Y | 35.0 |  | G | Y | UNMAPPED_HEADER | n/a | scripts/benchmark_phase2_ab_gt20.py::_canonical_ |
| 0000700084_2026_08_Z…36.pdf | 1 | TALEKT SYP. | OpStk | 89.0 | Y | 89.0 |  | G | Y | UNMAPPED_HEADER | n/a | scripts/benchmark_phase2_ab_gt20.py::_canonical_ |
| 0000700124_2026_08_Z…03.pdf | 1 | AACTARIL SOAP | Op Qty | 20 | Y |  |  | D | Y | MISSING_PRODUCT | n/a | services/html_table/column_matcher.py::match_hea |
| 0000700124_2026_08_Z…03.pdf | 1 | AACTARIL SOAP | Pur Qty | 144 | Y |  |  | D | Y | MISSING_PRODUCT | n/a | services/html_table/column_matcher.py::match_hea |
| 0000700124_2026_08_Z…03.pdf | 1 | AACTARIL SOAP | Sales Qty | 40 | Y |  |  | D | Y | MISSING_PRODUCT | n/a | services/html_table/column_matcher.py::match_hea |
| 0000700124_2026_08_Z…03.pdf | 1 | AACTARIL SOAP | Closing | 124 | Y |  |  | D | Y | MISSING_PRODUCT | n/a | services/html_table/column_matcher.py::match_hea |
| 0000700124_2026_08_Z…03.pdf | 1 | TALEKT TAB | Op Qty | 30 | Y |  |  | D | Y | MISSING_PRODUCT | n/a | services/html_table/column_matcher.py::match_hea |
| 0000700124_2026_08_Z…03.pdf | 1 | TALEKT TAB | Pur Qty | 50 | Y |  |  | D | Y | MISSING_PRODUCT | n/a | services/html_table/column_matcher.py::match_hea |
| 0000700124_2026_08_Z…03.pdf | 1 | TALEKT TAB | Sales Qty | 3 | Y |  |  | D | Y | MISSING_PRODUCT | n/a | services/html_table/column_matcher.py::match_hea |
| 0000700124_2026_08_Z…03.pdf | 1 | TALEKT TAB | Closing | 77 | Y |  |  | D | Y | MISSING_PRODUCT | n/a | services/html_table/column_matcher.py::match_hea |
| 0000700209_2026_08_Z…13.pdf | 1 | AACTARIL SOAP | Op. Stock | 69 | N |  | 0.0 | A | N | WRONG_VALUE | n/a | services/ocr/paddle_vl_engine.py |
| 0000700209_2026_08_Z…13.pdf | 1 | AACTARIL SOAP | Cl. Stock As On | 69 | N |  | 0.0 | A | N | WRONG_VALUE | n/a | services/ocr/paddle_vl_engine.py |
| 0000700209_2026_08_Z…13.pdf | 1 | RENALKA SYRUP | Op. Stock | 12 | Y | 0 | 0 | E | Y | WRONG_VALUE | n/a | scripts/benchmark_phase2_ab_gt20.py::_find_line_ |
| 0000700209_2026_08_Z…13.pdf | 1 | RENALKA SYRUP | Cl. Stock As On | 12 | Y | 0 | 0 | E | Y | WRONG_VALUE | n/a | scripts/benchmark_phase2_ab_gt20.py::_find_line_ |
| 0000700275_2026_08_Z…15.pdf | 1 | ABANA TAB | Issues | 10 | Y | 10 | 0.0 | D | Y | WRONG_VALUE | n/a | services/html_table/column_matcher.py::match_hea |
| 0000700275_2026_08_Z…15.pdf | 1 | DIABECON TAB | Opening | 76 | Y |  | 0.0 | D | N | WRONG_VALUE | n/a | services/html_table/column_matcher.py::match_hea |
| 0000700275_2026_08_Z…15.pdf | 1 | DIABECON TAB | Issues | 1 | Y |  | 0.0 | D | N | WRONG_VALUE | n/a | services/html_table/column_matcher.py::match_hea |
| 0000700275_2026_08_Z…15.pdf | 1 | DIABECON TAB | Closing | 75 | Y |  | 0.0 | D | N | WRONG_VALUE | n/a | services/html_table/column_matcher.py::match_hea |
| 0000700275_2026_08_Z…15.pdf | 1 | HIMCOLIN GEL | Issues | 2 | Y | 2 | 0.0 | D | Y | WRONG_VALUE | n/a | services/html_table/column_matcher.py::match_hea |
| 0000700275_2026_08_Z…15.pdf | 1 | LIV-52 SYRUP | Issues | 35 | Y | 35 | 0.0 | E | Y | WRONG_VALUE | n/a | scripts/benchmark_phase2_ab_gt20.py::_find_line_ |
| 0000700275_2026_08_Z…15.pdf | 1 | LIV-52 SYRUP | Closing | 37 | Y | 162 | 162 | E | Y | WRONG_VALUE | n/a | scripts/benchmark_phase2_ab_gt20.py::_find_line_ |
| 0000700275_2026_08_Z…15.pdf | 1 | TALEKT TABLETS | Issues | 3 | Y | 3 | 0.0 | E | Y | WRONG_VALUE | n/a | scripts/benchmark_phase2_ab_gt20.py::_find_line_ |
| 0000700308_2026_08_Z…20.jpg | 1 | LIV-52 SYP | Receipt | 70 | Y | 70 | 6 | E | Y | WRONG_VALUE | n/a | scripts/benchmark_phase2_ab_gt20.py::_find_line_ |
| 0000700308_2026_08_Z…20.jpg | 1 | LIV-52 SYP | Issues | 6 | Y | 6 | 0.0 | E | Y | WRONG_VALUE | n/a | scripts/benchmark_phase2_ab_gt20.py::_find_line_ |
| 0000700308_2026_08_Z…20.jpg | 1 | LIV-52 SYP | Closing | 64 | Y | 64 | 0.0 | E | Y | WRONG_VALUE | n/a | scripts/benchmark_phase2_ab_gt20.py::_find_line_ |
| 0000700317_2026_08_Z…43.pdf | 1 | CLARINA FACE WASH GEL | Opening | 90 | Y |  | 0.0 | E | Y | WRONG_VALUE | n/a | scripts/benchmark_phase2_ab_gt20.py::_find_line_ |
| 0000700317_2026_08_Z…43.pdf | 1 | CLARINA FACE WASH GEL | Closing | 90 | Y |  | 0.0 | E | Y | WRONG_VALUE | n/a | scripts/benchmark_phase2_ab_gt20.py::_find_line_ |
| 0000700317_2026_08_Z…43.pdf | 1 | HIMCOLIN GEL | Issues | 3 | Y | 3 | 0.0 | D | Y | WRONG_VALUE | n/a | services/html_table/column_matcher.py::match_hea |
| 0000700317_2026_08_Z…43.pdf | 1 | HIMCOLIN GEL | Closing | 14 | Y | 14 | 0.0 | D | Y | WRONG_VALUE | n/a | services/html_table/column_matcher.py::match_hea |
| 0000700317_2026_08_Z…43.pdf | 1 | LIV 52 TABS | Issues | 12 | Y | 12 | 0.0 | E | Y | WRONG_VALUE | n/a | scripts/benchmark_phase2_ab_gt20.py::_find_line_ |
| 0000700317_2026_08_Z…43.pdf | 1 | LIV 52 TABS | Closing | 68 | Y | 68 | 0.0 | E | Y | WRONG_VALUE | n/a | scripts/benchmark_phase2_ab_gt20.py::_find_line_ |
| 0000700317_2026_08_Z…43.pdf | 1 | LIV-52 SYP. | Opening | 51 | Y | 120 | 120 | E | Y | WRONG_VALUE | n/a | scripts/benchmark_phase2_ab_gt20.py::_find_line_ |
| 0000700317_2026_08_Z…43.pdf | 1 | LIV-52 SYP. | Issues | 49 | Y | 15 | 0.0 | E | Y | WRONG_VALUE | n/a | scripts/benchmark_phase2_ab_gt20.py::_find_line_ |
| 0000700317_2026_08_Z…43.pdf | 1 | LIV-52 SYP. | Closing | 2 | Y | 105 | 0.0 | E | Y | WRONG_VALUE | n/a | scripts/benchmark_phase2_ab_gt20.py::_find_line_ |
| 0000700317_2026_08_Z…43.pdf | 1 | PILEX TABS | Issues | 48 | Y | 48 | 0.0 | E | Y | WRONG_VALUE | n/a | scripts/benchmark_phase2_ab_gt20.py::_find_line_ |
| 0000700317_2026_08_Z…43.pdf | 1 | PILEX TABS | Closing | 66 | Y | 66 | 0.0 | E | Y | WRONG_VALUE | n/a | scripts/benchmark_phase2_ab_gt20.py::_find_line_ |
| 0000700346_2026_08_Z…59.pdf | 1 | AACTARIL SOAP | OB | 0 | Y | 0 |  | D | Y | MISSING_PRODUCT | n/a | services/html_table/column_matcher.py::match_hea |
| 0000700346_2026_08_Z…59.pdf | 1 | AACTARIL SOAP | CB | 0 | Y | 0 |  | D | Y | MISSING_PRODUCT | n/a | services/html_table/column_matcher.py::match_hea |
| 0000700346_2026_08_Z…59.pdf | 1 | BONISAN 200ML | OB | 241 | Y | 241 |  | D | Y | MISSING_PRODUCT | n/a | services/html_table/column_matcher.py::match_hea |
| 0000700346_2026_08_Z…59.pdf | 1 | BONISAN 200ML | SAL | 83 | Y | 83 |  | D | Y | MISSING_PRODUCT | n/a | services/html_table/column_matcher.py::match_hea |
| 0000700346_2026_08_Z…59.pdf | 1 | BONISAN 200ML | CB | 156 | Y | 156 |  | D | Y | MISSING_PRODUCT | n/a | services/html_table/column_matcher.py::match_hea |
| 0000700346_2026_08_Z…59.pdf | 1 | CYSTONE TAB | OB | 27 | Y | 27 |  | D | Y | MISSING_PRODUCT | n/a | services/html_table/column_matcher.py::match_hea |
| 0000700346_2026_08_Z…59.pdf | 1 | CYSTONE TAB | SAL | 14 | Y | 14 |  | D | Y | MISSING_PRODUCT | n/a | services/html_table/column_matcher.py::match_hea |
| 0000700346_2026_08_Z…59.pdf | 1 | CYSTONE TAB | CB | 13 | Y | 13 |  | D | Y | MISSING_PRODUCT | n/a | services/html_table/column_matcher.py::match_hea |
| 0000700346_2026_08_Z…59.pdf | 1 | LIV-52 SYP 200ML | OB | 35 | Y | 35 |  | D | Y | MISSING_PRODUCT | n/a | services/html_table/column_matcher.py::match_hea |
| 0000700346_2026_08_Z…59.pdf | 1 | LIV-52 SYP 200ML | SAL | 33 | Y | 33 |  | D | Y | MISSING_PRODUCT | n/a | services/html_table/column_matcher.py::match_hea |
| 0000700346_2026_08_Z…59.pdf | 1 | LIV-52 SYP 200ML | CB | 2 | Y | 2 |  | D | Y | MISSING_PRODUCT | n/a | services/html_table/column_matcher.py::match_hea |
| 0000700346_2026_08_Z…59.pdf | 1 | LIV 52 DS TAB | OB | 70 | Y | 70 |  | D | Y | MISSING_PRODUCT | n/a | services/html_table/column_matcher.py::match_hea |
| 0000700346_2026_08_Z…59.pdf | 1 | LIV 52 DS TAB | SAL | 16 | Y | 16 |  | D | Y | MISSING_PRODUCT | n/a | services/html_table/column_matcher.py::match_hea |
| 0000700346_2026_08_Z…59.pdf | 1 | LIV 52 DS TAB | CB | 54 | Y | 54 |  | D | Y | MISSING_PRODUCT | n/a | services/html_table/column_matcher.py::match_hea |
| 0000700358_2026_08_Z…26.pdf | 1 | CLARINA ANTI ACNE FACE WA | Issues | 15 | Y | 15 | 0.0 | E | Y | WRONG_VALUE | n/a | scripts/benchmark_phase2_ab_gt20.py::_find_line_ |
| 0000700358_2026_08_Z…26.pdf | 1 | CONFIDO TAB | Issues | 16 | Y | 16 | 0.0 | D | Y | WRONG_VALUE | n/a | services/html_table/column_matcher.py::match_hea |
| 0000700358_2026_08_Z…26.pdf | 1 | LIV-52 SYP | Issues | 117 | Y | 117 | 0.0 | E | Y | WRONG_VALUE | n/a | scripts/benchmark_phase2_ab_gt20.py::_find_line_ |
| 0000700358_2026_08_Z…26.pdf | 1 | LIV-52 SYP 200ML | Receipt | 560 | Y | 70 | 70 | E | Y | WRONG_VALUE | n/a | scripts/benchmark_phase2_ab_gt20.py::_find_line_ |
| 0000700358_2026_08_Z…26.pdf | 1 | LIV-52 SYP 200ML | Issues | 248 | Y | 117 | 0.0 | E | Y | WRONG_VALUE | n/a | scripts/benchmark_phase2_ab_gt20.py::_find_line_ |
| 0000700358_2026_08_Z…26.pdf | 1 | LIV-52 SYP 200ML | Closing | 312 | Y | 23 | 23 | E | Y | WRONG_VALUE | n/a | scripts/benchmark_phase2_ab_gt20.py::_find_line_ |
| 0000700358_2026_08_Z…26.pdf | 1 | PILEX TABS | Issues | 30 | Y | 30 | 0.0 | E | Y | WRONG_VALUE | n/a | scripts/benchmark_phase2_ab_gt20.py::_find_line_ |
| 0000700364_2026_08_Z…56.pdf | 1 | TOTAL | Op.Bal. Qty. | 157673 | Y | 157673 |  | E | Y | MISSING_PRODUCT | n/a | scripts/benchmark_phase2_ab_gt20.py::_find_line_ |
| 0000700364_2026_08_Z…56.pdf | 1 | TOTAL | Closing Balance | 97537 | Y | 97537 |  | E | Y | MISSING_PRODUCT | n/a | scripts/benchmark_phase2_ab_gt20.py::_find_line_ |
| 0000700406_2026_08_Z…11.jpg | 1 | AACTARIL SOAP 75G 75GM | OPENING STOCK | 40 | Y | 40 |  | E | Y | MISSING_PRODUCT | n/a | scripts/benchmark_phase2_ab_gt20.py::_find_line_ |
| 0000700406_2026_08_Z…11.jpg | 1 | AACTARIL SOAP 75G 75GM | RECEIVE QUANTITY | 0 | Y | 0 |  | E | Y | MISSING_PRODUCT | n/a | scripts/benchmark_phase2_ab_gt20.py::_find_line_ |
| 0000700406_2026_08_Z…11.jpg | 1 | AACTARIL SOAP 75G 75GM | ISSUE QUANTITY | 3 | Y | 3 |  | E | Y | MISSING_PRODUCT | n/a | scripts/benchmark_phase2_ab_gt20.py::_find_line_ |
| 0000700406_2026_08_Z…11.jpg | 1 | AACTARIL SOAP 75G 75GM | CLOSING STOCK | 37 | Y | 37 |  | E | Y | MISSING_PRODUCT | n/a | scripts/benchmark_phase2_ab_gt20.py::_find_line_ |
| 0000700406_2026_08_Z…11.jpg | 1 | CONFIDO TAB 60'S | OPENING STOCK | 70 | Y | 70 |  | E | Y | MISSING_PRODUCT | n/a | scripts/benchmark_phase2_ab_gt20.py::_find_line_ |
| 0000700406_2026_08_Z…11.jpg | 1 | CONFIDO TAB 60'S | ISSUE QUANTITY | 1 | Y | 1 |  | E | Y | MISSING_PRODUCT | n/a | scripts/benchmark_phase2_ab_gt20.py::_find_line_ |
| 0000700406_2026_08_Z…11.jpg | 1 | CONFIDO TAB 60'S | CLOSING STOCK | 69 | Y | 69 |  | E | Y | MISSING_PRODUCT | n/a | scripts/benchmark_phase2_ab_gt20.py::_find_line_ |
| 0000700406_2026_08_Z…11.jpg | 1 | LIV 52 SYP 100ML | OPENING STOCK | 40 | Y | 40 | 60 | D | Y | WRONG_VALUE | n/a | services/html_table/column_matcher.py::match_hea |
| 0000700406_2026_08_Z…11.jpg | 1 | LIV 52 SYP 100ML | RECEIVE QUANTITY | 70 | Y | 70 | 4958 | D | Y | WRONG_VALUE | n/a | services/html_table/column_matcher.py::match_hea |
| 0000700406_2026_08_Z…11.jpg | 1 | LIV 52 SYP 100ML | ISSUE QUANTITY | 28 | Y | 28 | 0 | D | Y | WRONG_VALUE | n/a | services/html_table/column_matcher.py::match_hea |
| 0000700406_2026_08_Z…11.jpg | 1 | LIV 52 SYP 100ML | CLOSING STOCK | 82 | Y | 82 | 0 | D | Y | WRONG_VALUE | n/a | services/html_table/column_matcher.py::match_hea |
| 0000700406_2026_08_Z…11.jpg | 1 | LIV 52 TAB 100'S | RECEIVE QUANTITY | 100 | Y | 100 | 4958 | D | Y | WRONG_VALUE | n/a | services/html_table/column_matcher.py::match_hea |
| 0000700406_2026_08_Z…11.jpg | 1 | LIV 52 TAB 100'S | ISSUE QUANTITY | 21 | Y | 21 | 0 | D | Y | WRONG_VALUE | n/a | services/html_table/column_matcher.py::match_hea |
| 0000700406_2026_08_Z…11.jpg | 1 | LIV 52 TAB 100'S | CLOSING STOCK | 111 | Y | 111 | 0 | D | Y | WRONG_VALUE | n/a | services/html_table/column_matcher.py::match_hea |
| 0000700406_2026_08_Z…11.jpg | 1 | PILEX FORTE OINTMENT 30GM | ISSUE QUANTITY | 23 | Y | 23 |  | E | Y | MISSING_PRODUCT | n/a | scripts/benchmark_phase2_ab_gt20.py::_find_line_ |
| 0000700406_2026_08_Z…11.jpg | 1 | PILEX FORTE OINTMENT 30GM | CLOSING STOCK | 2 | Y | 2 |  | E | Y | MISSING_PRODUCT | n/a | scripts/benchmark_phase2_ab_gt20.py::_find_line_ |
| 0000700406_2026_08_Z…11.jpg | 1 | TENTEX FORTE TABS 10'S | OPENING STOCK | 245 | Y | 245 | 60 | D | Y | WRONG_VALUE | n/a | services/html_table/column_matcher.py::match_hea |
| 0000700406_2026_08_Z…11.jpg | 1 | TENTEX FORTE TABS 10'S | ISSUE QUANTITY | 6 | Y | 6 | 0 | D | Y | WRONG_VALUE | n/a | services/html_table/column_matcher.py::match_hea |
| 0000700406_2026_08_Z…11.jpg | 1 | TENTEX FORTE TABS 10'S | CLOSING STOCK | 239 | Y | 239 | 0 | D | Y | WRONG_VALUE | n/a | services/html_table/column_matcher.py::match_hea |
| 0000700406_2026_08_Z…11.jpg | 1 | TOTAL | OPENING STOCK | 1086 | Y | 1086 |  | E | Y | MISSING_PRODUCT | n/a | scripts/benchmark_phase2_ab_gt20.py::_find_line_ |
| 0000700406_2026_08_Z…11.jpg | 1 | TOTAL | RECEIVE QUANTITY | 170 | Y | 170 |  | E | Y | MISSING_PRODUCT | n/a | scripts/benchmark_phase2_ab_gt20.py::_find_line_ |
| 0000700406_2026_08_Z…11.jpg | 1 | TOTAL | ISSUE QUANTITY | 100 | Y | 100 |  | E | Y | MISSING_PRODUCT | n/a | scripts/benchmark_phase2_ab_gt20.py::_find_line_ |
| 0000700406_2026_08_Z…11.jpg | 1 | TOTAL | CLOSING STOCK | 1156 | Y | 1156 |  | E | Y | MISSING_PRODUCT | n/a | scripts/benchmark_phase2_ab_gt20.py::_find_line_ |
| 0000700434_2026_08_Z…03.jpg | 1 | CONFIDO TABS | Bal. | 94 | Y | 94 |  | G | Y | UNMAPPED_HEADER | n/a | scripts/benchmark_phase2_ab_gt20.py::_canonical_ |
| 0000700434_2026_08_Z…03.jpg | 1 | HIMCOLIN GEL | Bal. | 34 | Y | 34 |  | G | Y | UNMAPPED_HEADER | n/a | scripts/benchmark_phase2_ab_gt20.py::_canonical_ |
| 0000700434_2026_08_Z…03.jpg | 1 | LIV 52 SYRUP | Bal. | 78 | Y | 78 |  | G | Y | UNMAPPED_HEADER | n/a | scripts/benchmark_phase2_ab_gt20.py::_canonical_ |
| 0000700434_2026_08_Z…03.jpg | 1 | PILEX FORTE OINTMEN | Bal. | 6 | Y | 6 |  | G | Y | UNMAPPED_HEADER | n/a | scripts/benchmark_phase2_ab_gt20.py::_canonical_ |
| 0000700434_2026_08_Z…03.jpg | 1 | PILEX TABS | Bal. | 31 | Y | 31 |  | G | Y | UNMAPPED_HEADER | n/a | scripts/benchmark_phase2_ab_gt20.py::_canonical_ |
| 0000700434_2026_08_Z…03.jpg | 1 | OPHTHACARE EYE DR | Bal. | 47 | Y | 47 |  | G | Y | UNMAPPED_HEADER | n/a | scripts/benchmark_phase2_ab_gt20.py::_canonical_ |
| 0000700835_2026_08_Z…9.jpeg | 1 | AACTARIL SOAP | Total Qty. | 86 | Y | 86 |  | H | N | UNMAPPED_HEADER | n/a | undetermined |
| 0000700835_2026_08_Z…9.jpeg | 1 | LIV.52 SYRUP. | Op.Bal. Qty. | 136 | Y | 49 |  | E | Y | MISSING_PRODUCT | n/a | scripts/benchmark_phase2_ab_gt20.py::_find_line_ |
| 0000700835_2026_08_Z…9.jpeg | 1 | LIV.52 SYRUP. | Receipt Qty. | 700 | Y | 0 |  | E | Y | MISSING_PRODUCT | n/a | scripts/benchmark_phase2_ab_gt20.py::_find_line_ |
| 0000700835_2026_08_Z…9.jpeg | 1 | LIV.52 SYRUP. | Total Qty. | 836 | Y | 49 |  | E | Y | MISSING_PRODUCT | n/a | scripts/benchmark_phase2_ab_gt20.py::_find_line_ |
| 0000700835_2026_08_Z…9.jpeg | 1 | LIV.52 SYRUP. | Issue Qty. | 567 | Y | 2 |  | E | Y | MISSING_PRODUCT | n/a | scripts/benchmark_phase2_ab_gt20.py::_find_line_ |
| 0000700835_2026_08_Z…9.jpeg | 1 | LIV.52 SYRUP. | Closing Balance | 269 | Y | 47 |  | E | Y | MISSING_PRODUCT | n/a | scripts/benchmark_phase2_ab_gt20.py::_find_line_ |
| 0000700835_2026_08_Z…9.jpeg | 1 | LIV.52 TAB | Op.Bal. Qty. | 551 | Y | 49 |  | E | Y | MISSING_PRODUCT | n/a | scripts/benchmark_phase2_ab_gt20.py::_find_line_ |
| 0000700835_2026_08_Z…9.jpeg | 1 | LIV.52 TAB | Issue Qty. | 200 | Y | 2 |  | E | Y | MISSING_PRODUCT | n/a | scripts/benchmark_phase2_ab_gt20.py::_find_line_ |
| 0000700835_2026_08_Z…9.jpeg | 1 | LIV.52 TAB | Closing Balance | 353 | Y | 47 |  | E | Y | MISSING_PRODUCT | n/a | scripts/benchmark_phase2_ab_gt20.py::_find_line_ |
| 0000701078_2026_08_Z…6.jpeg | 1 | AACTARIL SOAP | Closing Balance | 69 | N | 89 | 89 | A | N | WRONG_VALUE | n/a | services/ocr/paddle_vl_engine.py |
| 0000701078_2026_08_Z…6.jpeg | 1 | LIV 52 SYP/ B | Op.Bal. Qty. | 2713 | Y | 74 |  | E | Y | MISSING_PRODUCT | n/a | scripts/benchmark_phase2_ab_gt20.py::_find_line_ |
| 0000701078_2026_08_Z…6.jpeg | 1 | LIV 52 SYP/ B | Issue Qty. | 1872 | Y | 5 |  | E | Y | MISSING_PRODUCT | n/a | scripts/benchmark_phase2_ab_gt20.py::_find_line_ |
| 0000701078_2026_08_Z…6.jpeg | 1 | LIV 52 SYP/ B | Closing Balance | 841 | Y | 119 |  | E | Y | MISSING_PRODUCT | n/a | scripts/benchmark_phase2_ab_gt20.py::_find_line_ |
| 0000701078_2026_08_Z…6.jpeg | 1 | LIV 52 SYP/ S | Op.Bal. Qty. | 3523 | Y | 74 |  | E | Y | MISSING_PRODUCT | n/a | scripts/benchmark_phase2_ab_gt20.py::_find_line_ |
| 0000701078_2026_08_Z…6.jpeg | 1 | LIV 52 SYP/ S | Issue Qty. | 1816 | Y | 5 |  | E | Y | MISSING_PRODUCT | n/a | scripts/benchmark_phase2_ab_gt20.py::_find_line_ |
| 0000701078_2026_08_Z…6.jpeg | 1 | LIV 52 SYP/ S | Closing Balance | 1707 | Y | 119 |  | E | Y | MISSING_PRODUCT | n/a | scripts/benchmark_phase2_ab_gt20.py::_find_line_ |
| 0000701078_2026_08_Z…6.jpeg | 1 | TALEKT CAPS. | Expiry Breakage | 12 | Y | 0 |  | H | N | UNMAPPED_HEADER | n/a | undetermined |
| 0000701151_2026_08_Z…50.jpg | 1 | TOTAL | Op.Bal. Qty. | 834578 | N | Op.Bal. Qty. |  | E | Y | MISSING_PRODUCT | n/a | scripts/benchmark_phase2_ab_gt20.py::_find_line_ |
| 0000701151_2026_08_Z…50.jpg | 1 | TOTAL | Closing Balance | 775274 | N | Closing Balance |  | E | Y | MISSING_PRODUCT | n/a | scripts/benchmark_phase2_ab_gt20.py::_find_line_ |
| 0000701275_2026_08_Z…16.jpg | 1 | AACTARIL SOAP | OPENING | 72 | Y | 72 | 0.0 | D | Y | WRONG_VALUE | n/a | services/html_table/column_matcher.py::match_hea |
| 0000701275_2026_08_Z…16.jpg | 1 | AACTARIL SOAP | CLOSING | 72 | Y | 72 | 0.0 | D | Y | WRONG_VALUE | n/a | services/html_table/column_matcher.py::match_hea |
| 0000701275_2026_08_Z…16.jpg | 1 | ABANA TABLET | OPENING | 100 | Y | 100 | 0.0 | D | Y | WRONG_VALUE | n/a | services/html_table/column_matcher.py::match_hea |
| 0000701275_2026_08_Z…16.jpg | 1 | ABANA TABLET | ISSUE | 3 | Y | 3 | 0.0 | D | Y | WRONG_VALUE | n/a | services/html_table/column_matcher.py::match_hea |
| 0000701275_2026_08_Z…16.jpg | 1 | ABANA TABLET | CLOSING | 97 | Y | 97 | 3 | D | Y | WRONG_VALUE | n/a | services/html_table/column_matcher.py::match_hea |
| 0000701275_2026_08_Z…16.jpg | 1 | DIABECON TAB | OPENING | 89 | Y | 89 | 0.0 | E | Y | WRONG_VALUE | n/a | scripts/benchmark_phase2_ab_gt20.py::_find_line_ |
| 0000701275_2026_08_Z…16.jpg | 1 | DIABECON TAB | CLOSING | 89 | Y | 89 | 0.0 | E | Y | WRONG_VALUE | n/a | scripts/benchmark_phase2_ab_gt20.py::_find_line_ |
| 0000701275_2026_08_Z…16.jpg | 1 | HIMCOLIN OINTMENT | OPENING | 36 | Y | 36 | 0.0 | D | Y | WRONG_VALUE | n/a | services/html_table/column_matcher.py::match_hea |
| 0000701275_2026_08_Z…16.jpg | 1 | HIMCOLIN OINTMENT | RECEIPT | 50 | Y | 50 | 36 | D | Y | WRONG_VALUE | n/a | services/html_table/column_matcher.py::match_hea |
| 0000701275_2026_08_Z…16.jpg | 1 | HIMCOLIN OINTMENT | ISSUE | 5 | Y | 5 | 50 | D | Y | WRONG_VALUE | n/a | services/html_table/column_matcher.py::match_hea |
| 0000701275_2026_08_Z…16.jpg | 1 | HIMCOLIN OINTMENT | CLOSING | 81 | Y | 81 | 5 | D | Y | WRONG_VALUE | n/a | services/html_table/column_matcher.py::match_hea |
| 0000701275_2026_08_Z…16.jpg | 1 | LIV 52 SYP 100 ML | OPENING | 57 | Y | 57 | 0.0 | E | Y | WRONG_VALUE | n/a | scripts/benchmark_phase2_ab_gt20.py::_find_line_ |
| 0000701275_2026_08_Z…16.jpg | 1 | LIV 52 SYP 100 ML | RECEIPT | 1400 | Y | 1400 | 57 | E | Y | WRONG_VALUE | n/a | scripts/benchmark_phase2_ab_gt20.py::_find_line_ |
| 0000701275_2026_08_Z…16.jpg | 1 | LIV 52 SYP 100 ML | ISSUE | 140 | Y | 140 | 1400 | E | Y | WRONG_VALUE | n/a | scripts/benchmark_phase2_ab_gt20.py::_find_line_ |
| 0000701275_2026_08_Z…16.jpg | 1 | LIV 52 SYP 100 ML | CLOSING | 1317 | Y | 1317 | 140 | E | Y | WRONG_VALUE | n/a | scripts/benchmark_phase2_ab_gt20.py::_find_line_ |
| 0000701275_2026_08_Z…16.jpg | 1 | TOTAL | OPENING | 503184 | Y | 503184 |  | E | Y | MISSING_PRODUCT | n/a | scripts/benchmark_phase2_ab_gt20.py::_find_line_ |
| 0000701275_2026_08_Z…16.jpg | 1 | TOTAL | RECEIPT | 277797 | Y | 277797 |  | E | Y | MISSING_PRODUCT | n/a | scripts/benchmark_phase2_ab_gt20.py::_find_line_ |
| 0000701275_2026_08_Z…16.jpg | 1 | TOTAL | ISSUE | 110666 | Y | 110666 |  | E | Y | MISSING_PRODUCT | n/a | scripts/benchmark_phase2_ab_gt20.py::_find_line_ |
| 0000701275_2026_08_Z…16.jpg | 1 | TOTAL | CLOSING | 670315 | Y | 670315 |  | E | Y | MISSING_PRODUCT | n/a | scripts/benchmark_phase2_ab_gt20.py::_find_line_ |
| 0000701851_2026_08_Z…40.jpg | 1 | AACTARIL SOAP | Total Qty. | 77 | Y | 77 |  | H | N | UNMAPPED_HEADER | n/a | undetermined |
| 0000701864_2026_08_Z…37.jpg | 1 | TOTAL | Op.Bal. Qty. | 95222 | Y | 95222 |  | E | Y | MISSING_PRODUCT | n/a | scripts/benchmark_phase2_ab_gt20.py::_find_line_ |
| 0000701864_2026_08_Z…37.jpg | 1 | TOTAL | Closing Balance | 101577 | Y | 101577 |  | E | Y | MISSING_PRODUCT | n/a | scripts/benchmark_phase2_ab_gt20.py::_find_line_ |
| 0000701904_2026_08_Z…48.jpg | 1 | AACTARIL SOAP | OPENING STOCK | 24 | Y |  |  | D | Y | MISSING_PRODUCT | n/a | services/html_table/column_matcher.py::match_hea |
| 0000701904_2026_08_Z…48.jpg | 1 | AACTARIL SOAP | RECEIVE QUANTITY | 144 | Y |  |  | D | Y | MISSING_PRODUCT | n/a | services/html_table/column_matcher.py::match_hea |
| 0000701904_2026_08_Z…48.jpg | 1 | AACTARIL SOAP | ISSUE QUANTITY | 67 | Y |  |  | D | Y | MISSING_PRODUCT | n/a | services/html_table/column_matcher.py::match_hea |
| 0000701904_2026_08_Z…48.jpg | 1 | AACTARIL SOAP | CLOSING STOCK | 101 | Y |  |  | D | Y | MISSING_PRODUCT | n/a | services/html_table/column_matcher.py::match_hea |
| 0000701904_2026_08_Z…48.jpg | 1 | TALEKT SYRUP | OPENING STOCK | 31 | Y |  |  | D | Y | MISSING_PRODUCT | n/a | services/html_table/column_matcher.py::match_hea |
| 0000701904_2026_08_Z…48.jpg | 1 | TALEKT SYRUP | ISSUE QUANTITY | 3 | Y |  |  | D | Y | MISSING_PRODUCT | n/a | services/html_table/column_matcher.py::match_hea |
| 0000701904_2026_08_Z…48.jpg | 1 | TALEKT SYRUP | CLOSING STOCK | 28 | Y |  |  | D | Y | MISSING_PRODUCT | n/a | services/html_table/column_matcher.py::match_hea |
| 0000701904_2026_08_Z…48.jpg | 1 | TALEKT TAB | OPENING STOCK | 36 | Y |  |  | D | Y | MISSING_PRODUCT | n/a | services/html_table/column_matcher.py::match_hea |
| 0000701904_2026_08_Z…48.jpg | 1 | TALEKT TAB | ISSUE QUANTITY | 7 | Y |  |  | D | Y | MISSING_PRODUCT | n/a | services/html_table/column_matcher.py::match_hea |
| 0000701904_2026_08_Z…48.jpg | 1 | TALEKT TAB | CLOSING STOCK | 29 | Y |  |  | D | Y | MISSING_PRODUCT | n/a | services/html_table/column_matcher.py::match_hea |
| 0000701904_2026_08_Z…48.jpg | 1 | TOTAL | OPENING STOCK | 3174 | Y |  |  | D | Y | MISSING_PRODUCT | n/a | services/html_table/column_matcher.py::match_hea |
| 0000701904_2026_08_Z…48.jpg | 1 | TOTAL | RECEIVE QUANTITY | 1014 | Y |  |  | D | Y | MISSING_PRODUCT | n/a | services/html_table/column_matcher.py::match_hea |
| 0000701904_2026_08_Z…48.jpg | 1 | TOTAL | ISSUE QUANTITY | 1981 | Y |  |  | D | Y | MISSING_PRODUCT | n/a | services/html_table/column_matcher.py::match_hea |
| 0000701904_2026_08_Z…48.jpg | 1 | TOTAL | CLOSING STOCK | 2207 | Y |  |  | D | Y | MISSING_PRODUCT | n/a | services/html_table/column_matcher.py::match_hea |
| 0000704337_2026_08_Z…13.pdf | 1 | Aactaril Soap | Qty. | 80 | Y |  |  | D | Y | MISSING_PRODUCT | n/a | services/html_table/column_matcher.py::match_hea |
| 0000704337_2026_08_Z…13.pdf | 1 | Clarina Anti-acne Cream | Qty. | 1 | Y |  |  | D | Y | MISSING_PRODUCT | n/a | services/html_table/column_matcher.py::match_hea |
| 0000704337_2026_08_Z…13.pdf | 1 | Confido Tablets | Qty. | 2 | Y |  |  | D | Y | MISSING_PRODUCT | n/a | services/html_table/column_matcher.py::match_hea |
| 0000704337_2026_08_Z…13.pdf | 1 | Diabecon DS Tablets | Qty. | 10 | Y |  |  | D | Y | MISSING_PRODUCT | n/a | services/html_table/column_matcher.py::match_hea |
| 0000704337_2026_08_Z…13.pdf | 1 | Liv.52 Syrup | Qty. | 80 | Y |  |  | D | Y | MISSING_PRODUCT | n/a | services/html_table/column_matcher.py::match_hea |
| 0000704337_2026_08_Z…13.pdf | 1 | Liv.52 Tablets | Qty. | 20 | Y |  |  | D | Y | MISSING_PRODUCT | n/a | services/html_table/column_matcher.py::match_hea |
| 0000704337_2026_08_Z…13.pdf | 1 | Ophthacare Eye Drops | Qty. | 5 | Y |  |  | D | Y | MISSING_PRODUCT | n/a | services/html_table/column_matcher.py::match_hea |
| 0000704337_2026_08_Z…13.pdf | 1 | Oxitard Capsules | Qty. | 120 | Y |  |  | D | Y | MISSING_PRODUCT | n/a | services/html_table/column_matcher.py::match_hea |
| 0000704337_2026_08_Z…13.pdf | 1 | Pilex forte Ointment | Qty. | 10 | Y |  |  | D | Y | MISSING_PRODUCT | n/a | services/html_table/column_matcher.py::match_hea |
| 0000704337_2026_08_Z…13.pdf | 1 | Pilex Tablets | Qty. | 10 | Y |  |  | D | Y | MISSING_PRODUCT | n/a | services/html_table/column_matcher.py::match_hea |

### Per-field reason index

For every incorrect quantity, `incorrect_quantity_traces.json` includes: `document_id`, `page`, `product_name`, `field_name`, `ground_truth_value`, `raw_ocr_has_expected_token`, `parsed_table_cell_value`, `final_extracted_value`, `extract_line_item`, `bbox`, `failure_category`, `confirmed`, `reason`, `source_file_function`, `columns_matched`, `columns_unmatched`.

## What was ruled out

- **Primary cause is not raw VL digit OCR** for this published score (OCR-token presence 98.3%; direct OCR eval 99.3%).
- **Phase 2 Gemini recovery** was OFF in the scored arm — not implicated.
- **Stock-equation post-correction** was not used in this investigation.

## Recommended next steps (investigation only — no implementation)

1. Eval-only: extend `_canonical_field` for `OpStk`/`Bal.` to remove G false negatives.
2. Production: Qty-vs-Free disambiguation + mangled-header repair in `column_matcher` / `resolve_column_mapping`.
3. Production: product+pack matching in HTML parse and in bench `_find_line_item`.
4. Re-score GT-20 extraction after (1)–(3); only then invest in OCR digit work for the residual A/H cases.

