# PaddleOCR-VL 1.6 — 50-Document OCR Benchmark

## 1. Executive summary

PaddleOCR-VL 1.6 was run as an **opt-in, isolated** OCR evidence producer on the **first 50 documents** of the exact Phase 3 representative set (`phase3_eval/selection.json`). The full 100-doc run was intentionally stopped early per request; this is not a new random sample.
Production OCR priority was not changed. Tesseract, classic PaddleOCR, Gemini, Phase 2, and parsers were not run.
No OCR accuracy percentage is claimed; no manually verified OCR ground-truth set was used for scoring.

- Documents: **50**
- Pages seen: **93**
- Native-text pages skipped: **53**
- OCR-required pages: **40**
- Successful OCR pages: **38**
- Failed OCR pages: **2**
- Timeouts: **0**

## 2. Environment

- OS: Windows 11
- Python: 3.12.6
- Isolated venv: C:\Users\amnsa\OneDrive\Desktop\zydus v1\splitextract\.venvs\paddleocr_vl

## 3. PaddleOCR-VL version

- PaddleOCR package: 3.7.0
- PaddleX: 3.7.2
- Pipeline: `v1.6` → `PaddleOCR-VL-1.6`

## 4. Model identifier

- Engine: `paddleocr-vl-1.6`
- Pipeline name: `PaddleOCR-VL-1.6`
- VL recognition model: `PaddleOCR-VL-1.6-0.9B`
- Layout model: `PP-DocLayoutV3`

## 5. Hardware

- CPU: AMD64 Family 25 Model 68 Stepping 1, AuthenticAMD
- RAM: 15.31 GiB
- GPU: NVIDIA GeForce RTX 3050 6GB Laptop GPU
- VRAM: 6.0
- CUDA compiled (Paddle): True
- Device used: gpu:0

## 6. Dataset

- Dataset root: `C:\Users\amnsa\Downloads\ZL_2026_August`
- Manifest: `C:\Users\amnsa\OneDrive\Desktop\zydus v1\splitextract\phase3_eval\selection.json` (Phase 3 `selection.json`, not a new sample)

## 7. Number of documents

50

## 8. Number of pages

93

## 9. Native-text pages skipped

53

## 10. OCR-required pages

40

## 11. Successful pages

38

## 12. Failed pages

2

## 13. Timeout pages

0

## 14. Average latency

116131.64428157895 ms (successful OCR pages; excludes model init)

## 15. Median latency

108633.70295000004 ms

## 16. Cold-start latency

5870.848799999976 ms (first OCR page after model load)

## 17. Warm latency

119111.66578108109 ms (mean of subsequent successful OCR pages)

- Model init latency: 12079.44099999986 ms
- Total benchmark wall time: 1200183.8808000002 ms

## 18. Average text length

5050.9473684210525

## 19. Average word count

103.86842105263158

## 20. Numeric token statistics

- Pages with numeric tokens: 38
- Total numeric tokens: 8235
- Average per success page: 216.71052631578948
- Median per success page: 139.5

Tokens are reported exactly as produced (no automatic correction).

## 21. Confidence statistics

- Pages with mean confidence: 0
- Mean: None
- Median: None
- Note: PaddleOCR-VL block confidence is often unavailable; do not invent.

## 22. Bounding-box statistics

- Valid: 191
- Invalid: 0
- Total: 191

## 23. Table / layout evidence

- Pages with layout regions: 38
- Pages with tables: 37
- Table/layout evidence available: True

## 24. Known difficult-document results

No known difficult IDs from the Phase 3 set were present or completed yet.
## 25. Failure analysis

Document status counts: `{'success': 47, 'skipped_native_text': 1, 'error': 2}`

Per-document error detail is in `phase4/paddle_vl_benchmark/per_document/`.

## 26. Resource usage

- Snapshot at summary time: `{'cpu_percent': 8.3, 'ram_used_gb': 9.84, 'ram_total_gb': 15.31, 'gpu': 'NVIDIA GeForce RTX 3050 6GB Laptop GPU', 'vram_used_gb': 7.095, 'vram_total_gb': 6.0}`

## 27. Limitations

- Isolated `.venvs/paddleocr_vl` required; production Python has classic Paddle/torch DLL issues.
- CUDNN host/runtime mismatch warning observed (Paddle built with 9.9, host 9.5).
- VL page latency is high on RTX 3050 6GB; unsuitable as a silent production swap.
- Block-level confidence is frequently unavailable from the VL pipeline.
- No full OCR character/word ground truth for the 100-doc set → no fake accuracy %.
- Digital PDFs and non-visual office files are skipped via native-text / non-visual rules.

## 28. Recommendation

Keep PaddleOCR-VL 1.6 **opt-in / benchmark-only**. Do not change production OCR priority.
Use this report as one engine's evidence profile for a later controlled multi-engine bakeoff on the same Phase 3 manifest.
