# ZA 181 engine decision

Selected engine: **paddleocr-vl**.
Confidence: **moderate**.

This is a practical production choice. It is not a claim that every one of the 181 images has verified ground truth.

## Why this engine won

Correct product and quantity extraction is the first criterion. paddleocr-vl takes 5 of the scored checks.

Source-verified product F1 is 73.9% for PaddleOCR-VL and 48.5% for Tesseract. Quantity accuracy is 73.5% versus 54.3%.
On the 51-image AI proxy, product F1 is 50.8% versus 36.2%. Median per-image F1 is 89.7% versus 58.3%, so the result is not one long statement. PaddleOCR-VL has the higher F1 on 35 images and Tesseract on 9.
Micro quantity accuracy on that proxy is close, 47.5% versus 44.5%, because some large statements are hard for both. The median image is not close: 60.0% versus 37.9%.

The AI proxy is not ground truth. A proposal can repeat an OCR mistake or invent a row. It is only a check that the 2-image result is not an isolated case.

Silent quantity errors are second. On the verified pair, PaddleOCR-VL has 19 (13.8%) and Tesseract has 8 (8.3%). Tesseract's lower count on the photo comes with 0% quantity accuracy on that image. On the proxy, silent errors are 155 (6.7%) for PaddleOCR-VL and 187 (8.4%) for Tesseract.

Runtime is last. Stored timings put PaddleOCR-VL far behind Tesseract. That cost does not outweigh the extraction gap.

No hybrid is selected. Tesseract wins the proxy F1 on a minority of images, including a few where PaddleOCR-VL's text misses the proposed names. Those images do not have source labels, so they do not justify running both engines. Set `OCR_ENGINE=tesseract` when a known batch needs the rollback. Ensemble stays off.

## Evidence

### Source-verified accuracy

Images: 2. Rows: 51. Human-reviewed rows added in this review store: 0.

| Engine | Product precision | Product recall | Product F1 | Quantity accuracy | Silent quantity errors |
| --- | --- | --- | --- | --- | --- |
| tesseract | 50.0% (24/48) | 47.1% (24/51) | 48.5% | 54.3% (88/162) | 8 (8.3%) |
| paddleocr-vl | 64.7% (44/68) | 86.3% (44/51) | 73.9% | 73.5% (119/162) | 19 (13.8%) |

### AI-proposal proxy

Images: 51. These rows stay `PENDING` in the review store. Amount, price, unknown, and blank columns are not scored. A blank is not zero. Duplicate headers for one quantity are dropped.

Columns ignored as non-quantities: 7050. Blank or unparsed quantity cells ignored: 384. Ambiguous duplicate quantities dropped: 225.

The product F1 below uses the same full-name text rule as the verified score. Normalized and fuzzy name alignment is reported separately and is not the decision score.

| Engine | Product precision | Product recall | Product F1 | Median image F1 | Quantity accuracy | Median image quantity accuracy | Silent quantity errors | Images with higher F1 |
| --- | --- | --- | --- | --- | --- | --- | --- | ---: |
| tesseract | 49.3% (529/1073) | 28.6% (529/1848) | 36.2% | 58.3% | 44.5% (2029/4563) | 37.9% | 187 (8.4%) | 9 |
| paddleocr-vl | 59.2% (822/1388) | 44.5% (822/1848) | 50.8% | 89.7% | 47.5% (2167/4563) | 60.0% | 155 (6.7%) | 35 |

| Engine | Exact normalized name matches | Fuzzy name matches | Names missed | Extra extracted names |
| --- | ---: | ---: | ---: | ---: |
| tesseract | 162 | 36 | 1650 | 739 |
| paddleocr-vl | 409 | 85 | 1354 | 710 |

Per-image proxy F1 gives each statement one vote, so one long statement cannot decide the median.

| Image | Proposed rows | Tesseract F1 | Paddle F1 | Tesseract quantity | Paddle quantity | Tesseract silent | Paddle silent | Higher F1 |
| --- | ---: | --- | --- | --- | --- | ---: | ---: | --- |
| 0000700026_2026_08_ZA_06_333_04092026081643.png | 22 | n/a | n/a | 0.0% | 0.0% | 0 | 0 | tie |
| 0000700060_2026_08_ZA_06_617_04092026082801.png | 45 | 2.8% | 79.1% | 1.1% | 72.2% | 2 | 14 | paddleocr-vl |
| 0000700081_2026_08_ZA_06_333_04092026081604.png | 22 | 17.1% | 71.1% | 1.8% | 53.6% | 13 | 21 | paddleocr-vl |
| 0000700082_2026_08_ZA_06_467_04092026082339.png | 64 | 65.2% | 78.4% | 62.9% | 77.7% | 23 | 5 | paddleocr-vl |
| 0000700087_2026_08_ZA_06_333_04092026081228.png | 20 | n/a | n/a | 0.0% | 0.0% | 0 | 0 | tie |
| 0000700974_2026_08_ZA_11_210_07092026105901.png | 19 | 68.3% | 92.7% | 71.1% | 89.5% | 3 | 4 | paddleocr-vl |
| 0000700989_2026_08_ZA_11_210_07092026105922.png | 31 | 38.1% | 93.5% | 37.9% | 96.8% | 1 | 0 | paddleocr-vl |
| 0000701027_2026_08_ZA_11_210_07092026110044.png | 19 | 68.3% | 92.7% | 71.1% | 89.5% | 3 | 4 | paddleocr-vl |
| 0000701385_2026_08_ZA_37_216_01092026072110.png | 38 | 78.9% | 92.1% | 88.8% | 94.7% | 1 | 0 | paddleocr-vl |
| 0000701386_2026_08_ZA_37_216_01092026060705.png | 47 | 51.9% | 2.0% | 64.9% | 2.1% | 3 | 0 | tesseract |
| 0000701486_2026_08_ZA_37_216_01092026062328.png | 51 | 81.6% | 1.9% | 88.2% | 2.0% | 0 | 0 | tesseract |
| 0000701497_2026_08_ZA_37_216_01092026063625.png | 46 | 4.4% | n/a | 81.0% | 2.2% | 3 | 0 | tesseract |
| 0000701518_2026_08_ZA_37_216_01092026064642.png | 57 | 74.3% | n/a | 70.6% | 0.0% | 47 | 0 | tesseract |
| 0000701699_2026_08_ZA_37_216_01092026065925.png | 36 | 83.3% | n/a | 97.2% | 0.0% | 0 | 0 | tesseract |
| 0000701765_2026_08_ZA_37_216_01092026071349.png | 49 | 94.9% | 92.0% | 96.9% | 95.9% | 2 | 0 | tesseract |
| 0000702823_2026_08_ZA_22_235_05092026075405.png | 10 | 63.2% | 100.0% | 70.0% | 100.0% | 0 | 0 | paddleocr-vl |
| 0000702894_2026_08_ZA_22_235_05092026081637.png | 10 | 66.7% | 100.0% | 70.0% | 100.0% | 0 | 0 | paddleocr-vl |
| 0000702898_2026_08_ZA_22_235_05092026081715.png | 10 | 73.7% | 100.0% | 80.0% | 100.0% | 0 | 0 | paddleocr-vl |
| 0000705516_2026_08_ZA_22_235_05092026081504.png | 10 | 63.2% | 100.0% | 70.0% | 100.0% | 0 | 0 | paddleocr-vl |
| 0000708067_2026_08_ZA_24_536_04092026011934.png | 16 | 56.2% | 100.0% | 52.3% | 100.0% | 0 | 0 | paddleocr-vl |
| 0000708111_2026_08_ZA_37_216_01092026061710.png | 49 | 51.0% | 100.0% | 41.3% | 100.0% | 23 | 0 | paddleocr-vl |
| 0000709869_2026_08_ZA_06_8121_04092026080619.png | 34 | 12.7% | 10.8% | 11.8% | 11.8% | 0 | 0 | tesseract |
| 0000713377_2026_08_ZA_37_216_01092026065126.png | 58 | 68.4% | 95.0% | 73.3% | 98.3% | 2 | 0 | paddleocr-vl |
| 0000714739_2026_08_ZA_22_235_05092026081605.png | 10 | 85.7% | 100.0% | 100.0% | 100.0% | 0 | 0 | paddleocr-vl |
| 0000715087_2026_08_ZA_24_293_04092026174549.png | 10 | 42.1% | 60.0% | 25.0% | 60.0% | 6 | 0 | paddleocr-vl |
| 0000715088_2026_08_ZA_24_293_04092026181420.png | 10 | n/a | 100.0% | 0.0% | 100.0% | 0 | 0 | paddleocr-vl |
| 0000715736_2026_08_ZA_24_255_07092026172425.jpg | 63 | 7.8% | n/a | 0.0% | 0.0% | 0 | 0 | tesseract |
| 0000715743_2026_08_ZA_33_701_02092026140448.jpg | 31 | 75.0% | 96.8% | 72.2% | 92.4% | 8 | 3 | paddleocr-vl |
| 0000715753_2026_08_ZA_20_9232_01092026041414.jpg | 58 | n/a | 15.1% | 0.0% | 14.5% | 0 | 3 | paddleocr-vl |
| 0000715762_2026_08_ZA_24_255_07092026102024.jpg | 63 | n/a | n/a | 0.0% | 0.0% | 0 | 0 | tie |
| 0000715763_2026_08_ZA_24_469_05092026085654.jpg | 63 | n/a | n/a | 0.0% | 0.0% | 0 | 0 | tie |
| 0000715773_2026_08_ZA_24_255_07092026165557.jpg | 63 | 7.8% | n/a | n/a | n/a | 0 | 0 | tesseract |
| 0000715787_2026_08_ZA_04_747_03092026110903.jpg | 17 | n/a | 100.0% | 0.0% | 39.1% | 0 | 35 | paddleocr-vl |
| 0000715797_2026_08_ZA_12_8165_02092026163733.jpg | 65 | 59.4% | 98.4% | n/a | n/a | 0 | 0 | paddleocr-vl |
| 0000715818_2026_08_ZA_22_234_05092026065131.jpg | 14 | 32.0% | 61.5% | 28.6% | 34.3% | 4 | 8 | paddleocr-vl |
| 0000715821_2026_08_ZA_33_352_07092026100342.jpg | 44 | 2.3% | 79.5% | 1.8% | 84.8% | 1 | 6 | paddleocr-vl |
| 0000715871_2026_08_ZA_24_8136_04092026052545.jpg | 61 | n/a | n/a | n/a | n/a | 0 | 0 | tie |
| 0000715888_2026_08_ZA_22_236_08092026031608.jpg | 29 | n/a | 86.2% | 0.0% | 70.7% | 0 | 9 | paddleocr-vl |
| 0000716678_2026_08_ZA_24_290_03092026110428.jpg | 26 | 69.1% | 100.0% | 49.0% | 100.0% | 25 | 0 | paddleocr-vl |
| 0000716849_2026_08_ZA_24_256_02092026043001.jpg | 56 | n/a | 3.9% | n/a | n/a | 0 | 0 | paddleocr-vl |
| 0000717464_2026_08_ZA_01_224_03092026021751.jpg | 11 | 57.1% | 72.7% | 70.0% | 30.0% | 0 | 1 | paddleocr-vl |
| 0000717569_2026_08_ZA_35_8162_03092026110952.jpg | 63 | n/a | 87.7% | n/a | n/a | 0 | 0 | paddleocr-vl |
| 0000717878_2026_08_ZA_24_469_06092026021850.jpg | 20 | n/a | 5.0% | 0.0% | 0.0% | 0 | 5 | paddleocr-vl |
| 0000718438_2026_08_ZA_01_223_04092026163207.jpg | 42 | n/a | 7.2% | 0.0% | 4.2% | 0 | 5 | paddleocr-vl |
| 0000718784_2026_08_ZA_24_293_04092026071150.jpg | 10 | 32.0% | 100.0% | 30.0% | 97.5% | 8 | 1 | paddleocr-vl |
| 0000718796_2026_08_ZA_24_262_03092026135208.jpg | 71 | n/a | n/a | n/a | n/a | 0 | 0 | tie |
| 0000718799_2026_08_ZA_12_390_04092026165053.jpg | 63 | n/a | 89.7% | 0.0% | 82.5% | 0 | 2 | paddleocr-vl |
| 0000718881_2026_08_ZA_24_293_04092026074302.jpg | 10 | 40.0% | 60.0% | 5.0% | 50.0% | 9 | 2 | paddleocr-vl |
| 0000718903_2026_08_ZA_24_293_04092026060012.jpg | 10 | 52.6% | 60.0% | 60.0% | 60.0% | 0 | 0 | paddleocr-vl |
| 0000718910_2026_08_ZA_11_214_04092026134311.jpg | 45 | n/a | 65.2% | 0.0% | 34.4% | 0 | 27 | paddleocr-vl |
| 0000718937_2026_08_ZA_04_530_08092026033019.jpg | 63 | n/a | n/a | 0.0% | 0.0% | 0 | 0 | tie |

### Unlabeled images

Proposal failures: 71. Never started: 57. Together: 128. No accuracy is claimed here.

| Engine | Caches present | Missing caches | Empty text | Median characters | Median stored runtime |
| --- | ---: | ---: | ---: | ---: | ---: |
| tesseract | 128 | 0 | 0 | 1781.5 | 6.9 s |
| paddleocr-vl | 128 | 0 | 0 | 6362.0 | 157.2 s |

### Runtime and failures on all 181 cached images

Timings are stored `primary_ms` values from the existing runs. OCR was not rerun.

| Engine | Caches | Empty text | Median | Mean | P95 | Total |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| tesseract | 181 | 0 | 6.4 s | 7.6 s | 17.1 s | 1381.6 s |
| paddleocr-vl | 181 | 0 | 156.2 s | 271.8 s | 606.2 s | 49196.7 s |

## Decision checks

| Check | Tesseract | PaddleOCR-VL | Result |
| --- | ---: | ---: | --- |
| source-verified product F1 | 48.5% | 73.9% | paddleocr-vl |
| source-verified quantity accuracy | 54.3% | 73.5% | paddleocr-vl |
| AI-proxy product F1 | 36.2% | 50.8% | paddleocr-vl |
| AI-proxy median image F1 | 58.3% | 89.7% | paddleocr-vl |
| AI-proxy quantity accuracy | 44.5% | 47.5% | paddleocr-vl |

## Limitations

- Two images have source-verified labels. Zero review rows have been saved as VERIFIED or CORRECTED.
- The 51-image numbers are an AI-proposal proxy. They can favor an engine that agrees with a wrong proposal.
- The 71 failed and 57 unstarted images have OCR caches and no labels. They are excluded from accuracy.
- A quantity hit means the expected number occurs in the row-local OCR window. It does not prove the printed column.
- A missing number is not counted as zero.
- Full-name text matching is the decision rule. Fuzzy matches are diagnostic only.

## Configuration

Production primary remains `paddleocr-vl` through `OCR_ENGINE` in `services/ocr/orchestrator.py`.
Rollback: set `OCR_ENGINE=tesseract`.
Ensemble stays off. Unverified fallback stays off.

Recommended production configuration:

```
OCR_ENGINE=paddleocr-vl
OCR_FALLBACK_ENGINE=tesseract
OCR_ENSEMBLE_ENABLED=false
OCR_ALLOW_UNVERIFIED_FALLBACK=false
```
