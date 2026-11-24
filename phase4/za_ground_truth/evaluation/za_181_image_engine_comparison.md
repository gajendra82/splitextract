# ZA 181-image engine comparison

This report compares cached Tesseract and PaddleOCR-VL outputs. **Agreement is not accuracy.** The two source-verified ZA documents stay in `phase4/za_ground_truth/REPORT.md` and are not used as the denominator here.

## Executive summary

- Images compared: **181** (unique 181).
- Product-name agreement: **16.2%** (1015/6264 shared names over the union).
- Named quantity-field agreement, one or both values present: **25.4%** (422/1663) across 48/181 images. Both-missing fields are not in this denominator.
- Headerless positional number agreement, kept separate from named fields: **75.4%** (1950/2587) on 17/181 images.
- Exact document agreement: **0.0%** (0/181).
- Paddle extracted more product rows on **95** images; Tesseract on **71**; equal counts on **15**.
- Manual review flagged on **176** images.

## Coverage

- Shared IDs are VL checkpoint status `complete` with both primary OCR caches present.
- Cache SHA-256 matches: 181. Mismatches: 0. Missing hash: 0.
- Structured rows come from Paddle HTML tables when present, otherwise from plain OCR lines.
- Named quantity columns are assigned only from header words printed by that engine. Pack sizes and amount columns are not treated as stock quantities.
- Paddle tables where a packing token sat in the first quantity column, and that token was removed before reading the printed headers: 13.
- Pages with no printed quantity headers are not given opening/receipt/sales/closing labels. Their trailing numbers are compared only as positional tokens.
- Production `line_items` arrays are not stored in the batch artifacts, so they are not the comparison source.

## Aggregate metrics

| Metric | Value | Denominator |
|---|---:|---:|
| Tesseract products | 2996 | 181 images |
| Paddle products | 4283 | 181 images |
| Mean Tesseract products / image | 16.55 | 181 |
| Mean Paddle products / image | 23.66 | 181 |
| Names only in Tesseract | 1981 | 6264 union |
| Names only in Paddle | 3268 | 6264 union |
| Tesseract duplicate names dropped | 75 | extra copies after the first |
| Paddle duplicate names dropped | 284 | extra copies after the first |
| Shared-name order agrees | 61 | 62 images with a shared name |

### Quantity fields

Both-present agreement ignores rows where neither engine has the field. Missing-on-one is a disagreement in the any-present rate, not an agreement.
Both-present named quantity agreement: **59.4%** (422/711).

| Field | Agree | Disagree | Missing Tesseract | Missing Paddle | Both-present agreement |
|---|---:|---:|---:|---:|---:|
| opening_qty | 173 | 68 | 185 | 25 | 71.8% (173/241) |
| receipt_qty | 66 | 75 | 40 | 150 | 46.8% (66/141) |
| sales_qty | 77 | 76 | 93 | 113 | 50.3% (77/153) |
| sales_scheme_qty | 0 | 0 | 40 | 32 | n/a (0/0) |
| stock_out_qty | 32 | 13 | 41 | 5 | 71.1% (32/45) |
| closing_qty | 74 | 57 | 157 | 71 | 56.5% (74/131) |

## Layout metadata

No separate image-quality label exists. The table below uses the Tesseract run's existing text cohort. It is not a visual quality grade.

| Tesseract cohort | Images | Product agreement | Quantity agreement |
|---|---:|---:|---:|
| ambiguous_poor_quality | 4 | 0.0% (0/62) | n/a (0/0) |
| compatible_ssa | 21 | 35.5% (417/1176) | 46.1% (288/625) |
| negative_unresolved | 20 | 23.4% (169/722) | 26.4% (75/284) |
| ordinary | 136 | 10.0% (429/4304) | 7.8% (59/754) |

## Runtime

Timings are the stored `primary_ms` values from each run. They were not measured in one sitting, so the ratio is operational, not a lab benchmark.

| | Tesseract | PaddleOCR-VL |
|---|---:|---:|
| Images with timing | 181 | 181 |
| Total | 1381.6 s | 49196.7 s |
| Mean | 7.6 s | 271.8 s |
| Median | 6.4 s | 156.2 s |
| P95 | 17.1 s | 606.2 s |

Median Paddle / Tesseract ratio: **24.4x**.

## Disagreements

Disagreement rows written: **7152**.

- extra_row: 3268
- missing_row: 1981
- column_alignment: 1332
- quantity_digit: 546
- packing_unit: 24
- other: 1

## Source-verified accuracy, kept separate

Only 2 ZA images have source-verified labels. Those results are in `phase4/za_ground_truth/REPORT.md`. They are not extrapolated to these 181 images.

## Limitations

- A shared product name does not mean either name matches the printed page.
- Plain-text quantity positions are named only when the OCR text prints the stock headers. Other plain lines contribute product names and, when neither engine has a named field, positional numbers.
- A packing token in Paddle's first quantity column is removed only when most rows show that pattern. The stock identity equation is not used.
- HTML column repair belongs to the existing table parser. A shifted cell can still be compared.
- Duplicate normalized product names keep the first row only. A second pack of the same name is not a separate product in this comparison.

## Conclusion

The 181-image result is an output-agreement comparison. It does not name an accuracy winner. PaddleOCR-VL emits more product rows. Tesseract is much faster. Where both engines expose the same quantity, use the both-present rates above; do not read those rates as correctness.
