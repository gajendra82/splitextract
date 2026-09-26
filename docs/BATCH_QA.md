# Batch QA

The QA runner checks the existing sales-statement extractor. It does not replace that extractor and it does not edit production handlers.

## Where to put PDFs

Put PDFs in `test_pdfs/`. Subfolders are included.

```
test_pdfs/
    file1.pdf
    folder_a/
        file2.pdf
```

## How to run

From the `splitextract` directory:

```
python scripts/batch_qa.py
```

Useful options:

```
python scripts/batch_qa.py --pdf-dir "C:\path\to\pdfs" --batch-size 100 --workers 1 --gemini-delay 5 --max-retries 4
python scripts/batch_qa.py --files 0000700209,0000700284
python scripts/batch_qa.py --limit 5
```

PDFs are handled in batches of 100 (`--batch-size`). One command walks each batch and saves after every PDF. Run the same command again to resume. PASS, FAIL, REVIEW, and ERROR are skipped. RATE_LIMITED is retried. `--force` reprocesses completed PDFs.

`--workers` defaults to 1. Gemini requests are always serialized to one at a time, even if workers is higher. `--gemini-delay` waits about 5 seconds (plus 0–2s jitter) before the next Gemini call. `--max-retries` defaults to 4, with waits of 10, 30, 60, and 120 seconds after HTTP 429 or RESOURCE_EXHAUSTED.

The command, for every PDF:

1. Calls the existing `extract_sales_statement` function.
2. Reads the PDF again with PyMuPDF word positions, or Tesseract when the page has no embedded text.
3. Maps printed columns from that PDF's own header.
4. Compares rows and writes the reports.

## Where results are stored

Latest combined results:

```
qa_results/progress.json
qa_results/report.json
qa_results/report.html
qa_results/cursor_fix_prompt.txt
qa_results/extracted/
qa_results/ground_truth/
qa_results/records/
```

Each batch is kept:

```
qa_results/runs/batch_001/
qa_results/runs/batch_002/
```

A later batch does not delete an earlier one. `cursor_fix_prompt.txt` groups failures from every completed batch by format and shared cause. `progress.json` is updated after each PDF, so a stopped run can continue.

## PASS, FAIL, REVIEW, ERROR

- **PASS** — extracted rows match the independent ground truth.
- **FAIL** — a product is missing or extra, or a printed quantity/value does not match. `0` and `null` are different. A column that is not in the PDF must be `null`.
- **REVIEW** — the PDF text/OCR is too ambiguous to decide, or two product names are similar and the quantities still line up.
- **ERROR** — that PDF raised an exception. The rest of the batch still runs.
- **RATE_LIMITED** — Gemini returned HTTP 429 or RESOURCE_EXHAUSTED after the retries. The next run tries this PDF again. It is not a completed result.

Comparison is deterministic. It does not ask an LLM whether the JSON matches the PDF.

## How to use cursor_fix_prompt.txt

Open `qa_results/cursor_fix_prompt.txt` and paste it into Cursor. It groups FAIL and ERROR results from every completed batch by format and shared cause. After the extractor is updated, run the same command again. Completed PDFs stay skipped until you pass `--force`.

The QA script itself does not modify production handlers.

## How to add a PDF format

Most layouts are read from the printed header (Product Name, Opening, Purchase, Sales, Closing, and Qty/Value sub-headers). A new wording is added in `qa/ground_truth.py`:

1. Add the title phrase in `_detect_format`.
2. Add the header wording in `_family` so the column maps onto `opening_qty`, `receipts_qty`, `sales_qty`, `closing_qty`, or the matching value field.
3. If returns, free, or other receipts change the stock equation, the adjusted formula is used only when those columns are actually printed.

Do not point the ground-truth path at `extract_sales_statement` or at the format parser that produced the JSON under test.
