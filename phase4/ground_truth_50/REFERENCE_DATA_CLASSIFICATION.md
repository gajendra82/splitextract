# Existing Reference Data Classification

| Artifact | Classification | May use as GT? |
| --- | --- | --- |
| `qa_results/ground_truth` from embedded PDF text | SOURCE_VERIFIED | Yes for native-text pages |
| `qa_results/ground_truth` OCR-fallback pages (`source=ocr`) | MODEL_DERIVED | No |
| `qa_results_images/ground_truth` (Tesseract notes) | MODEL_DERIVED | No |
| `phase3_eval/records` extractor outputs | PARSER_DERIVED | No |
| Phase 3 PASS/FAIL / stock identity | PARSER_DERIVED | No |
| Tesseract / classic Paddle / Gemini / VL outputs | MODEL_DERIVED | No |
| Human/agent visual inspection of original file | SOURCE_VERIFIED | Yes |

Only `SOURCE_VERIFIED` fields are scored in `evaluate_paddle_vl_ground_truth.py`.
