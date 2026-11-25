# PaddleOCR-VL 1.6 — Source Ground-Truth Evaluation (20 source-verified documents)

**Verification status:** `COMPLETE` — 20 source-verified documents.

## 1. Dataset

Exact first 50 documents from `phase3_eval/selection.json`, matching the completed PaddleOCR-VL 50-doc benchmark (`phase4/paddle_vl_100.json`).

This report scores the **source-verified subset** (target: 20 documents).
It is **not** accuracy over all 50 benchmark documents.

Error investigation for prior failures: `phase4/ground_truth_50/ERROR_INVESTIGATION_1078.md`.

## 2. Exact 50-document manifest

`phase4/ground_truth_50/manifest.json`

## 3. Pages evaluated

- Benchmark documents: **50**
- OCR-success documents in benchmark: **28**
- Documents with verified ground truth: **20**
- Pages with verified ground truth: **20**

## 4. Ground-truth coverage

Accuracy figures below are verified-field accuracy on the source-verified subset only. They are NOT '50-document OCR accuracy'.

Existing QA artifacts classified:

- `qa_results/ground_truth (embedded PDF pages)` → **SOURCE_VERIFIED**
- `qa_results/ground_truth (OCR-fallback pages)` → **MODEL_DERIVED**
- `qa_results_images/ground_truth` → **MODEL_DERIVED**
- `phase3_eval/records extraction` → **PARSER_DERIVED**
- `Phase 3 PASS/FAIL / stock identity` → **PARSER_DERIVED**
- `Tesseract / Paddle / Gemini outputs` → **MODEL_DERIVED**
- `Human / agent visual inspection of original file` → **SOURCE_VERIFIED**
- `Native spreadsheet cell values read from source file` → **SOURCE_VERIFIED**

`qa_results_images/ground_truth` is MODEL_DERIVED (Tesseract) and was **not** used.
Phase 3 PASS/FAIL / parser extracts were **not** used as truth.

## 5. Number of manually verified fields

388

## 6. Numeric fields verified

312

## 7. Text fields verified

76

## 8. Table cells verified

309

## 9. Bounding boxes verified

0 — NOT ENOUGH VERIFIED BBOX DATA (bboxes intentionally omitted; not invented).

## 10–23. Accuracy metrics (source-verified subset only)

- Numeric accuracy: **99.4%**
- Quantity accuracy: **99.3%**
- Price accuracy: **100.0%**
- Date accuracy: **100.0%**
- Batch accuracy: **n/a (insufficient verified fields)**
- Header accuracy: **98.2%**
- Table/column accuracy: **99.4%**
- Text accuracy: **98.7%**
- Precision: **100.0%**
- Recall: **99.2%**
- F1: **99.6%**
- Silent-wrong rate: **0.0%**
- Critical error rate: **0.5%**
- Document-level status counts: `{'FULLY_CORRECT': 18, 'PARTIALLY_CORRECT': 2}`

## 24. Failure analysis

Error type counts: `{'MISSING': 3}`

## 25. Known OCR failure examples

### `0000700209_2026_08_ZL_18_374_04092026060313.pdf` / `aact_op`

- FIELD / header: `Op. Stock`
- Product: `AACTARIL SOAP`
- GROUND TRUTH: `69`
- PADDLEOCR-VL: `None`
- RESULT: WRONG
- ERROR TYPE: `MISSING`

### `0000700209_2026_08_ZL_18_374_04092026060313.pdf` / `aact_cl`

- FIELD / header: `Cl. Stock As On`
- Product: `AACTARIL SOAP`
- GROUND TRUTH: `69`
- PADDLEOCR-VL: `None`
- RESULT: WRONG
- ERROR TYPE: `MISSING`

### `0000700124_2026_08_ZL_06_521_02092026204703.pdf` / `h_op`

- FIELD / header: `Op Qty`
- Product: `None`
- GROUND TRUTH: `Op Qty`
- PADDLEOCR-VL: `None`
- RESULT: WRONG
- ERROR TYPE: `MISSING`

## 26. Limitations

- Ground truth covers a high-quality OCR subset, not all 50 documents.
- Digital native-text pages were OCR-skipped in the VL benchmark and are out of OCR scoring scope.
- Bounding-box IoU metrics are unavailable until bbox-verified annotations exist.
- Human verification CLI is provided to expand coverage without fabricating values.
- No model comparison was performed.

## Verified document IDs

- `0000700084_2026_08_ZL_06_8236_03092026155136.pdf`
- `0000700124_2026_08_ZL_06_521_02092026204703.pdf`
- `0000700209_2026_08_ZL_18_374_04092026060313.pdf`
- `0000700275_2026_08_ZL_18_374_04092026171615.pdf`
- `0000700308_2026_08_ZL_18_8175_03092026072320.jpg`
- `0000700317_2026_08_ZL_18_374_02092026171443.pdf`
- `0000700346_2026_08_ZL_18_8189_05092026025159.pdf`
- `0000700358_2026_08_ZL_18_8173_05092026185826.pdf`
- `0000700364_2026_08_ZL_18_8173_05092026190156.pdf`
- `0000700406_2026_08_ZL_10_384_05092026140711.jpg`
- `0000700434_2026_08_ZL_10_386_05092026034503.jpg`
- `0000700835_2026_08_ZL_25_447_03092026105819.jpeg`
- `0000701012_2026_08_ZL_11_423_03092026183736.jpg`
- `0000701078_2026_08_ZL_03_362_05092026080346.jpeg`
- `0000701151_2026_08_ZL_14_361_03092026025650.jpg`
- `0000701275_2026_08_ZL_03_360_02092026152716.jpg`
- `0000701851_2026_08_ZL_20_302_03092026134940.jpg`
- `0000701864_2026_08_ZL_20_5015_03092026091037.jpg`
- `0000701904_2026_08_ZL_20_8227_05092026154848.jpg`
- `0000704337_2026_08_ZL_24_259_03092026042213.pdf`

## Document-level results

- `0000700084_2026_08_ZL_06_8236_03092026155136.pdf`: **FULLY_CORRECT** (15/15 fields)
- `0000700124_2026_08_ZL_06_521_02092026204703.pdf`: **PARTIALLY_CORRECT** (11/12 fields)
- `0000700209_2026_08_ZL_18_374_04092026060313.pdf`: **PARTIALLY_CORRECT** (14/16 fields)
- `0000700275_2026_08_ZL_18_374_04092026171615.pdf`: **FULLY_CORRECT** (21/21 fields)
- `0000700308_2026_08_ZL_18_8175_03092026072320.jpg`: **FULLY_CORRECT** (9/9 fields)
- `0000700317_2026_08_ZL_18_374_02092026171443.pdf`: **FULLY_CORRECT** (20/20 fields)
- `0000700346_2026_08_ZL_18_8189_05092026025159.pdf`: **FULLY_CORRECT** (18/18 fields)
- `0000700358_2026_08_ZL_18_8173_05092026185826.pdf`: **FULLY_CORRECT** (21/21 fields)
- `0000700364_2026_08_ZL_18_8173_05092026190156.pdf`: **FULLY_CORRECT** (20/20 fields)
- `0000700406_2026_08_ZL_10_384_05092026140711.jpg`: **FULLY_CORRECT** (35/35 fields)
- `0000700434_2026_08_ZL_10_386_05092026034503.jpg`: **FULLY_CORRECT** (23/23 fields)
- `0000700835_2026_08_ZL_25_447_03092026105819.jpeg`: **FULLY_CORRECT** (29/29 fields)
- `0000701012_2026_08_ZL_11_423_03092026183736.jpg`: **FULLY_CORRECT** (14/14 fields)
- `0000701078_2026_08_ZL_03_362_05092026080346.jpeg`: **FULLY_CORRECT** (18/18 fields)
- `0000701151_2026_08_ZL_14_361_03092026025650.jpg`: **FULLY_CORRECT** (19/19 fields)
- `0000701275_2026_08_ZL_03_360_02092026152716.jpg`: **FULLY_CORRECT** (24/24 fields)
- `0000701851_2026_08_ZL_20_302_03092026134940.jpg`: **FULLY_CORRECT** (22/22 fields)
- `0000701864_2026_08_ZL_20_5015_03092026091037.jpg`: **FULLY_CORRECT** (20/20 fields)
- `0000701904_2026_08_ZL_20_8227_05092026154848.jpg`: **FULLY_CORRECT** (20/20 fields)
- `0000704337_2026_08_ZL_24_259_03092026042213.pdf`: **FULLY_CORRECT** (12/12 fields)