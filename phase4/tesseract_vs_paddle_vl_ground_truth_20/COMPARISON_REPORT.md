# Tesseract vs PaddleOCR-VL 1.6 — Source Ground-Truth Comparison (20 docs)

## Scope

- Benchmark pool: **50** documents (Phase 3 first-50)
- Source-verified documents scored: **20**
- Verified fields: **388**
- Identical overlapping fields used for comparison: **388**

Ground truth = human/visual inspection of original documents (`SOURCE_VERIFIED`).
Parser output, Phase 3 PASS/FAIL, and OCR engine output were **not** used as truth.

## Tesseract evidence availability

- Prior page-level Tesseract dumps in repo: **False**
- Comparison-folder cache hits: **0**
- Fresh TesseractEngine runs for this comparison: **20**

Missing / incompatible artifacts:

- `qa_results*/ground_truth/*.json` — MODEL_DERIVED structured extraction; not page OCR evidence; not SOURCE_VERIFIED truth
- `qa_results*/extracted/*.json` — Parser/API extract; not raw OCR text
- `phase3_eval/records/*.json` — Phase 3 telemetry/outcomes; no raw OCR text
- No page-level Tesseract OCR text dumps were found under qa_results/, qa_results_images/, or phase3_eval/ for these 20 documents.
- qa_results*_ground_truth is MODEL_DERIVED structured GT (Tesseract-backed for some files) and is NOT used as OCR evidence or as scoring truth.
- Fresh TesseractEngine runs are stored only under this comparison folder.

## Engine evidence sources

- PaddleOCR-VL: `phase4/paddle_vl_benchmark/per_document (reused, not rerun)`
- Tesseract: `C:\Users\amnsa\OneDrive\Desktop\zydus v1\splitextract\phase4\tesseract_vs_paddle_vl_ground_truth_20\tesseract_per_document`

## Shared-field accuracy (fair comparison)

| Metric | Tesseract | PaddleOCR-VL | Δ (VL − Tess) |
| --- | ---: | ---: | ---: |
| Field accuracy | 68.6% | 87.6% | 19.1% |
| Numeric | 73.4% | 85.6% | 12.2% |
| Quantity | 76.4% | 85.1% | 8.7% |
| Price | 42.9% | 100.0% | 57.1% |
| Date | 0.0% | 33.3% | 33.3% |
| Text | 48.7% | 96.1% | 47.4% |
| Header | 40.4% | 94.7% | n/a |
| Table/column | 74.1% | 86.1% | 12.0% |
| Silent-wrong rate | 0.0% | 10.8% | 10.8% |
| Critical-error rate | 22.4% | 11.6% | -10.8% |
| Precision | 100.0% | 89.0% | n/a |
| Recall | 68.6% | 87.6% | n/a |
| F1 | 81.3% | 88.3% | n/a |

## Document-level (FULLY_CORRECT)

- Tesseract: **2** / 20
- PaddleOCR-VL: **10** / 20
- Tesseract status counts: `{'PARTIALLY_CORRECT': 17, 'FULLY_CORRECT': 2, 'OCR_UNAVAILABLE': 1}`
- PaddleOCR-VL status counts: `{'PARTIALLY_CORRECT': 10, 'FULLY_CORRECT': 10}`

## Pairwise field outcomes (shared fields)

- Both correct: **237**
- VL only correct: **103**
- Tesseract only correct: **29**
- Both wrong: **19**

## Limitations

- Prior page-level Tesseract OCR dumps were not available; Tesseract evidence was collected for this comparison using the existing TesseractEngine adapter.
- PaddleOCR-VL often emits HTML tables enabling column-aware matching; Tesseract plain text usually does not — table/column gaps partly reflect evidence structure.
- Accuracy is verified-field accuracy on the 20 SOURCE_VERIFIED documents, not 50-document dataset accuracy.
- No bbox IoU comparison (insufficient verified bbox GT).

## Important

These figures are **verified-field accuracy on the 20 SOURCE_VERIFIED documents**,
not overall accuracy on the 50-document benchmark pool.
