# Verified OCR accuracy

Scores from the images that currently have explicit labels cannot establish which OCR engine is more accurate across all 181 images.

Images scored: 2.
Source-verified images: 2 (51 rows).
Human-reviewed images added: 0 (0 rows).
AI-proposed rows excluded: 1854.
Pending review rows excluded: 1854.
Uncertain review rows excluded: 0.

## Rules

- Source-verified images are scored from the existing labels.
- A review row is scored only when its saved status is VERIFIED or CORRECTED and it has reviewer notes.
- PENDING, UNCERTAIN, and AI_PROPOSED rows are excluded. They are not counted as missing products.
- A product is correct only when the full printed name occurs in that engine's OCR text. One occurrence is consumed per scored row.
- A shorter fragment of a scored name is neither a correct product nor an extra product.
- A quantity is correct when the expected number appears in the text window after that product and before the next different scored product.
- That window does not prove which printed column the number came from.
- A missing number is not treated as zero. A different number in the window is a silent quantity error.
- Unknown, amount, and price columns are not mapped to opening, receipt, sales, or closing.
- Duplicate meanings for the same quantity are rejected before they can be scored.

## Engines

### tesseract

Product precision 50.0% (24/48), recall 47.1% (24/51), F1 48.5%.
Quantity accuracy 54.3% (88/162). Silent quantity errors 8 (8.3% of both-present quantities).
Missing product rows 27. Extra product rows 24.

| Image | Product precision | Product recall | Product F1 | Quantity accuracy | Silent quantity errors | Missing products | Extra products |
| --- | --- | --- | --- | --- | ---: | ---: | ---: |
| 0000719004_2026_08_ZA_04_550_02092026014949.jpg | 0.0% | 0.0% | n/a | 0.0% | 0 | 21 | 1 |
| 0000722289_2026_08_ZA_04_589_04092026043529.jpg | 51.1% | 80.0% | 62.3% | 73.3% | 8 | 6 | 23 |

### paddleocr-vl

Product precision 64.7% (44/68), recall 86.3% (44/51), F1 73.9%.
Quantity accuracy 73.5% (119/162). Silent quantity errors 19 (13.8% of both-present quantities).
Missing product rows 7. Extra product rows 24.

| Image | Product precision | Product recall | Product F1 | Quantity accuracy | Silent quantity errors | Missing products | Extra products |
| --- | --- | --- | --- | --- | ---: | ---: | ---: |
| 0000719004_2026_08_ZA_04_550_02092026014949.jpg | 90.5% | 90.5% | 90.5% | 57.1% | 14 | 2 | 2 |
| 0000722289_2026_08_ZA_04_589_04092026043529.jpg | 53.2% | 83.3% | 64.9% | 79.2% | 5 | 5 | 22 |

