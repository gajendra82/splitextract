# ZA accuracy comparison — Tesseract vs PaddleOCR-VL 1.6

**Date:** 2026-10-23  
**Source images:** `C:\Users\amnsa\Downloads\image dataset stock`  
**Existing runs reused:** `20261013T385tess`, `20261013T385vl`  
**ZL ground truth was not used.** Engine outputs were not used as labels.

## What was verified

The 385-image ZA archive still has no source labels except the subset created here.

| Item | Count |
|---|---:|
| ZA images | 385 |
| Tesseract artifacts | 385 |
| PaddleOCR-VL documents completed | 181 |
| Documents with both caches | 181 |
| Source-verified ZA documents | **2** |
| Source-verified fields | **247** |
| Overlap with the 20 ZL ground-truth files | 0 |

The two labeled pages were chosen as different layouts, then read from the original images twice. Cells that did not agree, handwritten order quantities, and columns that are not printed were left unresolved and are not scored.

| Document | Layout | Quality |
|---|---|---|
| `0000722289_...43529.jpg` | Full digital stock statement (opening, receipt, issue, closing) | Clear print |
| `0000719004_...14949.jpg` | Photographed summary (OUT QTY and CL QTY only) | Photo of a printout |

Absent on these pages, so not invented: sales-scheme quantity, stock-out quantity. Opening and receipt are absent on the photo summary.

Labels: `phase4/za_ground_truth/per_document/`  
Validator: `scripts/validate_za_ground_truth.py`  
Scorer: `scripts/evaluate_za_ocr_accuracy.py`  
Field dump: `phase4/za_ground_truth/evaluation/za_accuracy_comparison.json`

## Scoring rules

- A product name is correct only when the printed name appears exactly after whitespace normalization.
- A nonzero quantity is correct only when that number appears in the text window after its product name and before the next labeled product. A matching number elsewhere in the document does not count.
- Expected zeros are labeled and reported, and they are excluded from the primary quantity rate.
- Unresolved fields are not in the files, so they are not in any denominator.
- Compact/fuzzy name containment is reported and is not counted as correct. On this subset that extra count was 0 for both engines.
- Secondary repair is not part of this score. The comparison uses each run's primary OCR cache.

## Results

### Nonzero quantities (primary)

| Engine | Correct | Total | Accuracy |
|---|---:|---:|---:|
| Tesseract | 52 | 111 | 46.8% |
| PaddleOCR-VL 1.6 | 80 | 111 | 72.1% |

Difference: **+25.2 percentage points for PaddleOCR-VL.**

Paired nonzero quantity fields:

| Outcome | Fields |
|---|---:|
| Both correct | 50 |
| Paddle only correct | 30 |
| Tesseract only correct | 2 |
| Both wrong | 29 |

Field-level exact McNemar p = 2.5e-7. Fields inside a document are not independent, and there are only 2 documents, so this p-value is not a 385-document significance claim.

### Product names

| Engine | Correct | Total | Accuracy |
|---|---:|---:|---:|
| Tesseract | 24 | 51 | 47.1% |
| PaddleOCR-VL 1.6 | 44 | 51 | 86.3% |

Difference: **+39.2 percentage points for PaddleOCR-VL.**

Paired product names: both correct 22, Paddle only 22, Tesseract only 2, both wrong 5.

### Other rates

| Metric | Tesseract | PaddleOCR-VL |
|---|---:|---:|
| Packing exact | 0/30 (0%) | 0/30 (0%) |
| Header exact | 2/4 | 3/4 |
| Document exact (all scored names and nonzero qty) | 0/2 | 0/2 |
| Nonzero qty with a different nearby number (silent-wrong proxy) | 8/111 (7.2%) | 14/111 (12.6%) |
| Zero quantities, excluded from the primary rate | 36/51 | 39/51 |

### By layout (nonzero quantity)

| Layout | Tesseract | PaddleOCR-VL |
|---|---:|---:|
| Clear digital stock statement | 52/80 (65.0%) | 61/80 (76.3%) |
| Photo summary | 0/31 (0%) | 19/31 (61.3%) |

Tesseract failed every nonzero quantity on the photographed summary. Paddle still missed about 4 in 10 of those quantities.

## Runtime on the same two images

| Document | Tesseract | PaddleOCR-VL |
|---|---:|---:|
| Photo summary | 1.2 s | 110 s |
| Digital statement | 5.1 s | 214 s |

## Answers

1. **On this source-verified slice, PaddleOCR-VL 1.6 is more accurate** for product names and nonzero stock quantities.
2. **+39.2 points** on product names (44/51 vs 24/51) and **+25.2 points** on nonzero quantities (80/111 vs 52/111).
3. **The gap is large on these two documents. It is not a statistically defensible winner for all 385 ZA images.** Only 2 documents were source-verified, and the field-level test treats dependent cells as independent observations.
4. **Tesseract has fewer silent-wrong quantity proxies** (8/111 vs 14/111). Paddle has fewer misses. Neither document is fully correct.
5. **Tesseract fails the photo summary** (0/31 nonzero quantities). Both engines are closer on the clear digital statement (65% vs 76%). Neither engine matched packing strings exactly (0/30).
6. **The accuracy gain is real on this slice and the runtime cost is also real:** Paddle took about 40–90 times longer on these two pages.
7. **Do not change the production default from this study alone.** The slice supports keeping PaddleOCR-VL when the goal is reading ZA stock rows, especially photographs. It does not justify calling the 385-image archive decided.
8. **Manual review is still required** for photographed summaries, any row whose figures could not be read consistently (including the unresolved Liv-52 rows on the digital page), and every document that is not in this 2-page set.

## Limitation

The labels come from two agreeing visual reads of the original images in this session, not from a second person. Where the two reads duplicated the same figures onto two different products, those rows were left unresolved instead of being scored.
