# ZA 181 accuracy report

This report keeps verified and unverified annotations apart. OCR agreement is not accuracy.

## Coverage

- Images accounted for: 181 (unique 181).
- Manually verified images: 2.
- Verified product rows: 51.
- Images with no source annotation yet: 178.
- AI-proposed images, not verified: 1.
- Proposal failures: 0.
- Uncertain or unreadable fields: 198.
- Source files missing on disk: 0.

The verified images are the two documents already stored in `phase4/za_ground_truth/per_document/`. Their files were copied, not edited.

## Verified accuracy

Denominator: the two source-verified images only. This is not a 181-image accuracy rate.

A product name is correct only when the full printed name occurs in that engine's OCR text. Pack size stays part of the name. Repeated names consume one occurrence each. A quantity is correct when that number occurs in the text after its product name and before the next different labeled product. A missing number is not a zero.

### Tesseract

- Product precision: 50.0% (24/48).
- Product recall: 47.1% (24/51).
- Product F1: 48.5%.
- Missing product rows: 27. Extra product rows: 24.
- Quantity accuracy, missing counted as not correct: 54.3% (88/162).
- Quantity accuracy where both sides have a number: 91.7% (88/96).
- Mean absolute error on both-present quantities: 1.1041666666666667.
- Silent wrong quantities: 8 (8.3% of both-present quantities).
- Image exact match: 0/2.

| Field | Correct | Incorrect | Missing | Accuracy | MAE |
|---|---:|---:|---:|---:|---:|
| opening | 21 | 3 | 6 | 70.0% | 1.833 |
| receipt | 24 | 0 | 6 | 80.0% | 0.000 |
| sales | 21 | 3 | 27 | 41.2% | 1.542 |
| sales_scheme | 0 | 0 | 0 | n/a |  |
| stock_out | 0 | 0 | 0 | n/a |  |
| closing | 22 | 2 | 27 | 43.1% | 1.042 |

### PaddleOCR-VL

- Product precision: 64.7% (44/68).
- Product recall: 86.3% (44/51).
- Product F1: 73.9%.
- Missing product rows: 7. Extra product rows: 24.
- Quantity accuracy, missing counted as not correct: 73.5% (119/162).
- Quantity accuracy where both sides have a number: 86.2% (119/138).
- Mean absolute error on both-present quantities: 1.7318840579710144.
- Silent wrong quantities: 19 (13.8% of both-present quantities).
- Image exact match: 0/2.

| Field | Correct | Incorrect | Missing | Accuracy | MAE |
|---|---:|---:|---:|---:|---:|
| opening | 21 | 4 | 5 | 70.0% | 2.000 |
| receipt | 25 | 0 | 5 | 83.3% | 0.000 |
| sales | 38 | 6 | 7 | 74.5% | 1.455 |
| sales_scheme | 0 | 0 | 0 | n/a |  |
| stock_out | 0 | 0 | 0 | n/a |  |
| closing | 35 | 9 | 7 | 68.6% | 2.841 |

## Preliminary AI-assisted accuracy

These scores use AI-proposed labels only. They are not verified ground truth and they are not added to the verified scores above.

Images in this split: 1.

A named quantity is scored only when the proposal copied a printed column header. Columns without a visible header stay unknown and are excluded.

### Tesseract, preliminary

- Product precision: 0.0% (0/2).
- Product recall: 0.0% (0/22).
- Product F1: n/a.
- Missing product rows: 22. Extra product rows: 2.
- Quantity accuracy, missing counted as not correct: n/a (0/0).
- Quantity accuracy where both sides have a number: n/a (0/0).
- Mean absolute error on both-present quantities: None.
- Silent wrong quantities: 0 (n/a of both-present quantities).
- Image exact match: 0/1.

| Field | Correct | Incorrect | Missing | Accuracy | MAE |
|---|---:|---:|---:|---:|---:|
| opening | 0 | 0 | 0 | n/a |  |
| receipt | 0 | 0 | 0 | n/a |  |
| sales | 0 | 0 | 0 | n/a |  |
| sales_scheme | 0 | 0 | 0 | n/a |  |
| stock_out | 0 | 0 | 0 | n/a |  |
| closing | 0 | 0 | 0 | n/a |  |

### PaddleOCR-VL, preliminary

- Product precision: 0.0% (0/22).
- Product recall: 0.0% (0/22).
- Product F1: n/a.
- Missing product rows: 22. Extra product rows: 22.
- Quantity accuracy, missing counted as not correct: n/a (0/0).
- Quantity accuracy where both sides have a number: n/a (0/0).
- Mean absolute error on both-present quantities: None.
- Silent wrong quantities: 0 (n/a of both-present quantities).
- Image exact match: 0/1.

| Field | Correct | Incorrect | Missing | Accuracy | MAE |
|---|---:|---:|---:|---:|---:|
| opening | 0 | 0 | 0 | n/a |  |
| receipt | 0 | 0 | 0 | n/a |  |
| sales | 0 | 0 | 0 | n/a |  |
| sales_scheme | 0 | 0 | 0 | n/a |  |
| stock_out | 0 | 0 | 0 | n/a |  |
| closing | 0 | 0 | 0 | n/a |  |

## Winner

AI-proposed labels do not decide the 181-image accuracy winner. Only manually verified annotations can do that.

Manually verified images: 2. AI-proposed images still awaiting review: 1. Proposal failures: 0.

On those 2 images, PaddleOCR-VL reads more of the printed product names and more of the labeled quantities. Tesseract has fewer silent-wrong quantities. Neither image is an exact match. Labeled zeros are included in the 162-field denominator. A quantity counts as correct when the expected number appears in the text of that product row. This score does not prove which printed column the number came from. The earlier report at phase4/za_ground_truth/REPORT.md is unchanged.

Neither engine has an exact document match on the verified images (0/2 Tesseract, 0/2 PaddleOCR-VL).

## Resume

python scripts/za_181_accuracy_workflow.py propose
python scripts/za_181_accuracy_workflow.py evaluate
python scripts/za_181_accuracy_workflow.py serve --port 8765

Mark a row verified in the review tool only after comparing it with the original image. Saving an edit without that confirmation keeps the image unverified.
