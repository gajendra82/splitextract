# Dataset

Path: C:\Users\amnsa\Downloads\ZL_2026_August
Total files: 2774
Initial batch: 100
Formats: (none) 12, .csv 8, .doc 2, .docx 10, .heic 1, .jpeg 166, .jpg 957, .mht 1, .pdf 1062, .png 50, .rtf 1, .txt 26, .xls 200, .xlsx 278
Layouts: router pdf kinds digital 917, scanned 143, unknown 2
PDF count: 1062
Image count: 1174
Excel count: 478
Other count: 60
OCR-required files in the full dataset: 1320
Digital-text PDF candidates: 917
Scanned PDF candidates: 143
PDF page counts: min 1, max 11, mean 2.13
Duplicate content groups: 45
Previously processed by existing QA checkpoints: 165

# Baseline

PASS: 52
FAIL: 44
Provider errors: 0
OCR required: 52
Extractor errors: 4

# Phase 2

Recovery attempted: 43
Recovery accepted: 0
Recovery rejected: 39
Recovery unresolved: 1
Provider errors: 4
Verified correct: 0
Verified wrong: 0
Unverified: 44

# SAFETY

PASS → WRONG: 0
FAIL → WRONG: 0
null became zero: 0
unknown layout accepted: 0
dynamic field changed: 0

# Quality

Verified recovery precision: n/a
Recovery acceptance rate: 0.0
Recovery verification rate: n/a
Average recovery attempts: 0.81
Average provider calls: 0.81
Average processing time ms: 156124.54

# Breakdown

## By format
- (none): documents 1, PASS → PASS 0, FAIL → RECOVERED 0, FAIL → UNRESOLVED 1, FAIL → WRONG 0, PASS → WRONG 0
- .csv: documents 1, PASS → PASS 0, FAIL → RECOVERED 0, FAIL → UNRESOLVED 1, FAIL → WRONG 0, PASS → WRONG 0
- .doc: documents 1, PASS → PASS 0, FAIL → RECOVERED 0, FAIL → UNRESOLVED 1, FAIL → WRONG 0, PASS → WRONG 0
- .docx: documents 3, PASS → PASS 2, FAIL → RECOVERED 0, FAIL → UNRESOLVED 1, FAIL → WRONG 0, PASS → WRONG 0
- .jpeg: documents 7, PASS → PASS 3, FAIL → RECOVERED 0, FAIL → UNRESOLVED 4, FAIL → WRONG 0, PASS → WRONG 0
- .jpg: documents 21, PASS → PASS 7, FAIL → RECOVERED 0, FAIL → UNRESOLVED 13, FAIL → WRONG 0, PASS → WRONG 0
- .mht: documents 1, PASS → PASS 0, FAIL → RECOVERED 0, FAIL → UNRESOLVED 0, FAIL → WRONG 0, PASS → WRONG 0
- .pdf: documents 36, PASS → PASS 15, FAIL → RECOVERED 0, FAIL → UNRESOLVED 17, FAIL → WRONG 0, PASS → WRONG 0
- .png: documents 9, PASS → PASS 9, FAIL → RECOVERED 0, FAIL → UNRESOLVED 0, FAIL → WRONG 0, PASS → WRONG 0
- .rtf: documents 1, PASS → PASS 0, FAIL → RECOVERED 0, FAIL → UNRESOLVED 0, FAIL → WRONG 0, PASS → WRONG 0
- .txt: documents 1, PASS → PASS 1, FAIL → RECOVERED 0, FAIL → UNRESOLVED 0, FAIL → WRONG 0, PASS → WRONG 0
- .xls: documents 8, PASS → PASS 6, FAIL → RECOVERED 0, FAIL → UNRESOLVED 1, FAIL → WRONG 0, PASS → WRONG 0
- .xlsx: documents 10, PASS → PASS 9, FAIL → RECOVERED 0, FAIL → UNRESOLVED 1, FAIL → WRONG 0, PASS → WRONG 0

## By layout
- generic: documents 72, PASS → PASS 30, FAIL → RECOVERED 0, FAIL → UNRESOLVED 34, FAIL → WRONG 0, PASS → WRONG 0
- not_scored: documents 8, PASS → PASS 8, FAIL → RECOVERED 0, FAIL → UNRESOLVED 0, FAIL → WRONG 0, PASS → WRONG 0
- opbal: documents 3, PASS → PASS 3, FAIL → RECOVERED 0, FAIL → UNRESOLVED 0, FAIL → WRONG 0, PASS → WRONG 0
- product_wise: documents 4, PASS → PASS 2, FAIL → RECOVERED 0, FAIL → UNRESOLVED 2, FAIL → WRONG 0, PASS → WRONG 0
- product_wise_image: documents 11, PASS → PASS 9, FAIL → RECOVERED 0, FAIL → UNRESOLVED 2, FAIL → WRONG 0, PASS → WRONG 0
- saleret: documents 1, PASS → PASS 0, FAIL → RECOVERED 0, FAIL → UNRESOLVED 1, FAIL → WRONG 0, PASS → WRONG 0
- unknown: documents 1, PASS → PASS 0, FAIL → RECOVERED 0, FAIL → UNRESOLVED 1, FAIL → WRONG 0, PASS → WRONG 0

## By recovery strategy
- none: documents 56, PASS → PASS 52, FAIL → RECOVERED 0, FAIL → UNRESOLVED 0, FAIL → WRONG 0, PASS → WRONG 0
- provider_retry: documents 4, PASS → PASS 0, FAIL → RECOVERED 0, FAIL → UNRESOLVED 0, FAIL → WRONG 0, PASS → WRONG 0
- structured_reread: documents 3, PASS → PASS 0, FAIL → RECOVERED 0, FAIL → UNRESOLVED 3, FAIL → WRONG 0, PASS → WRONG 0
- targeted_stock_extraction: documents 37, PASS → PASS 0, FAIL → RECOVERED 0, FAIL → UNRESOLVED 37, FAIL → WRONG 0, PASS → WRONG 0

## By failure type
- EMPTY_OUTPUT: documents 3, PASS → PASS 0, FAIL → RECOVERED 0, FAIL → UNRESOLVED 3, FAIL → WRONG 0, PASS → WRONG 0
- PROVIDER_ERROR: documents 4, PASS → PASS 0, FAIL → RECOVERED 0, FAIL → UNRESOLVED 0, FAIL → WRONG 0, PASS → WRONG 0
- STOCK_IDENTITY_FAILURE: documents 37, PASS → PASS 0, FAIL → RECOVERED 0, FAIL → UNRESOLVED 37, FAIL → WRONG 0, PASS → WRONG 0
- none: documents 56, PASS → PASS 52, FAIL → RECOVERED 0, FAIL → UNRESOLVED 0, FAIL → WRONG 0, PASS → WRONG 0

## By OCR
- digital_text: documents 21, PASS → PASS 14, FAIL → RECOVERED 0, FAIL → UNRESOLVED 5, FAIL → WRONG 0, PASS → WRONG 0
- native_text: documents 5, PASS → PASS 3, FAIL → RECOVERED 0, FAIL → UNRESOLVED 2, FAIL → WRONG 0, PASS → WRONG 0
- ocr_required: documents 52, PASS → PASS 20, FAIL → RECOVERED 0, FAIL → UNRESOLVED 29, FAIL → WRONG 0, PASS → WRONG 0
- other: documents 3, PASS → PASS 0, FAIL → RECOVERED 0, FAIL → UNRESOLVED 1, FAIL → WRONG 0, PASS → WRONG 0
- pymupdf_fallback: documents 1, PASS → PASS 0, FAIL → RECOVERED 0, FAIL → UNRESOLVED 1, FAIL → WRONG 0, PASS → WRONG 0
- spreadsheet: documents 18, PASS → PASS 15, FAIL → RECOVERED 0, FAIL → UNRESOLVED 2, FAIL → WRONG 0, PASS → WRONG 0

# OCR

The current OCR architecture was not changed.
PaddleOCR / PP-OCR is not called anywhere in this repository.
Legacy Tesseract remains in the existing parser, the stock-row crop helper, and the independent QA reader. Phase 3 did not switch engines.
PyMuPDF remains the digital-text and embedded-sparse path reported by the existing router.

# ZL observations

No filename, product, invoice, quantity, or coordinate rules were added.
Recurring baseline failures in this batch, classified by the existing taxonomy:
- STOCK_IDENTITY_FAILURE: 37 (identity-profile problem)
- PROVIDER_ERROR: 4 (provider problem)
- EMPTY_OUTPUT: 3 (parser problem)

# Representative Cases

## Successful baseline
- `0000700029_2026_08_ZL_06_476_02092026113447.pdf` · PASS → PASS · profile `generic` · failure `none` · OCR `digital_text` · verification `RECOVERY_UNVERIFIED`

## Successful recovery
- none in this batch

## Accepted but unverified recovery
- none in this batch

## Unresolved recovery
- `0000701789_2026_08_ZL_20_557_05092026163636.jpg` · FAIL → UNRESOLVED · profile `unknown` · failure `STOCK_IDENTITY_FAILURE` · OCR `ocr_required` · verification `RECOVERY_UNVERIFIED`

## Rejected recovery
- `0000700017_2026_08_ZL_06_476_02092026121836.pdf` · FAIL → UNRESOLVED · profile `generic` · failure `STOCK_IDENTITY_FAILURE` · OCR `digital_text` · verification `RECOVERY_UNVERIFIED`

## Provider error
- `0000700209_2026_08_ZL_18_374_04092026060313.pdf` · FAIL → PROVIDER_ERROR · profile `generic` · failure `PROVIDER_ERROR` · OCR `ocr_required` · verification `RECOVERY_UNVERIFIED`

## Suspicious case
- none in this batch

# Recommendation

INVESTIGATE

Phase 2 attempted recovery on failed documents, but it accepted none. No PASS to WRONG or verified FAIL to WRONG case was observed. This batch does not show an accuracy gain.

This batch stopped after the initial representative sample. The remaining ZL August files were not processed.
