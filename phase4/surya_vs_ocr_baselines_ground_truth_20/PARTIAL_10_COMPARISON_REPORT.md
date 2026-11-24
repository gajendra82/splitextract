# Surya 2 vs Tesseract vs PaddleOCR-VL — PARTIAL (10 of 20 docs)

## Status

**PARTIAL** — full 20-doc run stopped at user request. Only the 10 documents with completed Surya OCR are scored on identical SOURCE_VERIFIED fields.

- Documents scored: **10** / 20
- Identical overlapping fields: **187**

## Shared-field accuracy

| Metric | Tesseract | PaddleOCR-VL | Surya 2 |
| --- | ---: | ---: | ---: |
| Field | 63.1% | 89.3% | 82.4% |
| Numeric | 64.9% | 88.5% | 83.1% |
| Quantity | 69.2% | 87.7% | 83.1% |
| Price | 37.5% | 100.0% | 93.8% |
| Date | 0.0% | 50.0% | 0.0% |
| Text | 56.4% | 92.3% | 79.5% |
| Table/column | 65.8% | 89.0% | 84.2% |
| Silent-wrong | 0.0% | 8.6% | 11.8% |
| Critical-error | 29.9% | 9.1% | 13.4% |
| Precision | 100.0% | 91.3% | 87.5% |
| Recall | 63.1% | 89.3% | 82.4% |
| F1 | 77.4% | 90.3% | 84.8% |

## FULLY_CORRECT docs

- tesseract: **2** / 10
- paddleocr-vl-1.6: **4** / 10
- surya-2: **4** / 10

## Pairwise

```json
{
  "tesseract_vs_paddleocr-vl-1.6": {
    "both_correct": 115,
    "tesseract_only_correct": 3,
    "paddleocr-vl-1.6_only_correct": 52,
    "both_wrong": 17
  },
  "tesseract_vs_surya-2": {
    "both_correct": 111,
    "tesseract_only_correct": 7,
    "surya-2_only_correct": 43,
    "both_wrong": 26
  },
  "paddleocr-vl-1.6_vs_surya-2": {
    "both_correct": 152,
    "paddleocr-vl-1.6_only_correct": 15,
    "surya-2_only_correct": 2,
    "both_wrong": 18
  }
}
```

## Error categories

```json
{
  "tesseract": {
    "CORRECT": 118,
    "MISSING": 69
  },
  "paddleocr-vl-1.6": {
    "CORRECT": 167,
    "MISSING": 4,
    "COLUMN_CONFUSION": 16
  },
  "surya-2": {
    "CORRECT": 154,
    "MISSING": 11,
    "COLUMN_CONFUSION": 22
  }
}
```

## Per-document

- `0000700084_2026_08_ZL_06_8236_03092026155136.pdf` n=15 statuses={'tesseract': 'PARTIALLY_CORRECT', 'paddleocr-vl-1.6': 'PARTIALLY_CORRECT', 'surya-2': 'PARTIALLY_CORRECT'} correct={'tesseract': 10, 'paddleocr-vl-1.6': 14, 'surya-2': 14}
- `0000700124_2026_08_ZL_06_521_02092026204703.pdf` n=12 statuses={'tesseract': 'PARTIALLY_CORRECT', 'paddleocr-vl-1.6': 'PARTIALLY_CORRECT', 'surya-2': 'PARTIALLY_CORRECT'} correct={'tesseract': 4, 'paddleocr-vl-1.6': 10, 'surya-2': 10}
- `0000700209_2026_08_ZL_18_374_04092026060313.pdf` n=16 statuses={'tesseract': 'PARTIALLY_CORRECT', 'paddleocr-vl-1.6': 'PARTIALLY_CORRECT', 'surya-2': 'PARTIALLY_CORRECT'} correct={'tesseract': 1, 'paddleocr-vl-1.6': 5, 'surya-2': 5}
- `0000700275_2026_08_ZL_18_374_04092026171615.pdf` n=21 statuses={'tesseract': 'PARTIALLY_CORRECT', 'paddleocr-vl-1.6': 'PARTIALLY_CORRECT', 'surya-2': 'PARTIALLY_CORRECT'} correct={'tesseract': 18, 'paddleocr-vl-1.6': 18, 'surya-2': 13}
- `0000700308_2026_08_ZL_18_8175_03092026072320.jpg` n=9 statuses={'tesseract': 'FULLY_CORRECT', 'paddleocr-vl-1.6': 'PARTIALLY_CORRECT', 'surya-2': 'FULLY_CORRECT'} correct={'tesseract': 9, 'paddleocr-vl-1.6': 8, 'surya-2': 9}
- `0000700317_2026_08_ZL_18_374_02092026171443.pdf` n=20 statuses={'tesseract': 'OCR_UNAVAILABLE', 'paddleocr-vl-1.6': 'PARTIALLY_CORRECT', 'surya-2': 'PARTIALLY_CORRECT'} correct={'tesseract': 0, 'paddleocr-vl-1.6': 18, 'surya-2': 15}
- `0000700346_2026_08_ZL_18_8189_05092026025159.pdf` n=18 statuses={'tesseract': 'FULLY_CORRECT', 'paddleocr-vl-1.6': 'FULLY_CORRECT', 'surya-2': 'FULLY_CORRECT'} correct={'tesseract': 18, 'paddleocr-vl-1.6': 18, 'surya-2': 18}
- `0000700358_2026_08_ZL_18_8173_05092026185826.pdf` n=21 statuses={'tesseract': 'PARTIALLY_CORRECT', 'paddleocr-vl-1.6': 'FULLY_CORRECT', 'surya-2': 'FULLY_CORRECT'} correct={'tesseract': 19, 'paddleocr-vl-1.6': 21, 'surya-2': 21}
- `0000700364_2026_08_ZL_18_8173_05092026190156.pdf` n=20 statuses={'tesseract': 'PARTIALLY_CORRECT', 'paddleocr-vl-1.6': 'FULLY_CORRECT', 'surya-2': 'FULLY_CORRECT'} correct={'tesseract': 15, 'paddleocr-vl-1.6': 20, 'surya-2': 20}
- `0000700406_2026_08_ZL_10_384_05092026140711.jpg` n=35 statuses={'tesseract': 'PARTIALLY_CORRECT', 'paddleocr-vl-1.6': 'FULLY_CORRECT', 'surya-2': 'PARTIALLY_CORRECT'} correct={'tesseract': 24, 'paddleocr-vl-1.6': 35, 'surya-2': 29}

## Recommendation

On this partial sample (10 of 20 SOURCE_VERIFIED docs / 187 fields with completed Surya OCR), paddleocr-vl-1.6 has the highest measured field accuracy (89.3%). Silent-wrong: tesseract=0.0%, paddleocr-vl-1.6=8.6%, surya-2=11.8%. FULLY_CORRECT: tesseract=2, paddleocr-vl-1.6=4, surya-2=4. This is not a full 20-doc result and not an automatic production winner; the run was stopped after 10 documents.
