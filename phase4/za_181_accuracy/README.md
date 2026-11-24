# ZA 181 source-accuracy workflow

This directory holds the ground-truth review set for the 181 ZA images that have both Tesseract and PaddleOCR-VL caches.

Verified labels are copied from `phase4/za_ground_truth/per_document/`. Those source files are not edited. Images without a source reading are stored as `not_annotated`. OCR text is not used as a label.

## Commands

From `splitextract`:

```
python scripts/za_181_accuracy_workflow.py propose
python scripts/za_181_accuracy_workflow.py evaluate
python scripts/za_181_accuracy_workflow.py serve --port 8765
```

`propose` reads each remaining original image with a vision model and stores `AI_PROPOSED` rows. It does not send Tesseract or PaddleOCR-VL text. It skips the two source-verified images. Progress is written after every image, so the same command resumes. Named quantities are filled only when the transcript includes a printed header. Blank cells stay blank.

`init` builds the dataset only when `ground_truth.json` is absent. It refuses to rebuild over existing proposals.

`evaluate` scores the current `ground_truth.json` again. Use it after a review session.

`serve` opens a local review page at `http://127.0.0.1:8765`. A save stays unverified unless you check the verification box and write what you compared on the original image. Editing one of the two already verified images requires the page to send `confirm_edit_existing`; the page does that only for those images.

## Files

- `ground_truth.json` — one record per image
- `ground_truth_review.csv` — one field per verified cell, plus one line per image that has no rows
- `ground_truth_progress.json` — counts and the unverified image list
- `tesseract_evaluation.json` — verified metrics, with an empty preliminary split
- `paddleocr_vl_evaluation.json` — same split for PaddleOCR-VL
- `accuracy_comparison.csv` — side-by-side metrics with numerators and denominators
- `ACCURACY_REPORT.md` — verified results and the 181-image conclusion

## Scoring

- Product names match after case and punctuation normalization. Pack size stays in the name.
- Repeated product rows are matched in order and are not collapsed.
- Uncertain, unreadable, absent, and blank cells are not scored.
- A missing OCR number is not treated as zero.
- The verified split is the only accuracy result. It currently contains 2 images.
