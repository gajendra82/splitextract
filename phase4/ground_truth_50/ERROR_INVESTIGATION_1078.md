# Error investigation: `0000701078` (ALEX PHARMA)

Source: original JPEG  
PaddleOCR-VL evidence: `phase4/paddle_vl_benchmark/per_document/0000701078_2026_08_ZL_03_362_05092026080346.jpeg.json`

No document-specific production hacks applied.

## Case 1 — AACTARIL SOAP closing balance

| | |
| --- | --- |
| Field | Closing Balance |
| Ground truth (source) | `69` |
| PaddleOCR-VL cell | `89` |
| Full VL row | `AACTARIL SOAP \| 75GM \| 82 \| 0 \| 0 \| 82 \| 13 \| 0 \| 0 \| **89** \| -50 \| 0` |
| Source row | `82 \| 0 \| 0 \| 82 \| 13 \| 0 \| 0 \| **69** \| -50 \| 0` |

**Column alignment check:** Opening/Receipt/Total/Issue/Reorder match source. Only Closing differs.

**Arithmetic check:** `82 - 13 = 69` (source consistent). VL closing `89` does not fit the row equation.

**Classification:** **CHARACTER_RECOGNITION** (`6` → `8`), not reading-order or column-association failure.

## Case 2 — TALEKT CAPS. expiry breakage

| | |
| --- | --- |
| Field | Expiry Breakage |
| Ground truth (source) | `12` |
| PaddleOCR-VL cell | `0` |
| Full VL row | `TALEKT CAPS. \| 60'S \| 12 \| 0 \| 0 \| 12 \| 0 \| 0 \| **0** \| 0 \| 0 \| 0` |
| Source row | `12 \| 0 \| 0 \| 12 \| 0 \| 0 \| **12** \| 0 \| 0 \| 0` |

**Column alignment check:** Opening=`12`, Total=`12`, Issue=`0`, Closing=`0` all match. Expiry Breakage alone is wrong (`12` → `0`).

**Classification:** **CHARACTER_RECOGNITION / CELL_MISS** on the Expiry Breakage cell (digit run present on source but emitted as `0`). Not a column swap: neighboring columns stay correct; the `12` in Opening is a separate correct cell, not a shifted expiry value.

## Summary

| Case | Expected | Got | Root cause |
| --- | ---: | ---: | --- |
| AACTARIL Closing | 69 | 89 | Character recognition |
| TALEKT Expiry Breakage | 12 | 0 | Character recognition / missed cell |
