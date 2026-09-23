"""
Compare scan-OCR methods on local POD PDFs (manual / CI optional).

Usage (from splitextract/):
  set PADDLE_OCR_ENABLED=false
  python scripts/compare_scan_ocr.py path\\to\\pod.pdf

  set PADDLE_OCR_ENABLED=true
  python scripts/compare_scan_ocr.py path\\to\\pod.pdf

Prints per-page char counts and confidence for Paddle (if enabled) vs Tesseract.
Does not call Gemini — OCR-layer A/B only. Keep Laravel extraction path unchanged;
use /test-extract for full pipeline comparison.
"""

from __future__ import annotations

import argparse
import os
import sys
import time


def main() -> int:
    parser = argparse.ArgumentParser(description="Compare PaddleOCR vs Tesseract on a PDF")
    parser.add_argument("pdf", help="Path to a POD/invoice PDF")
    parser.add_argument("--max-pages", type=int, default=3)
    args = parser.parse_args()

    pdf_path = os.path.abspath(args.pdf)
    if not os.path.isfile(pdf_path):
        print(f"File not found: {pdf_path}", file=sys.stderr)
        return 1

    # Ensure project root on path
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if root not in sys.path:
        sys.path.insert(0, root)

    import fitz

    from services.paddle_ocr import (
        extract_text_with_paddleocr,
        is_paddle_ocr_enabled,
        reset_paddle_ocr_engine_for_tests,
    )

    # Import app helpers after path setup
    import app as app_mod

    reset_paddle_ocr_engine_for_tests()
    print(f"PDF: {pdf_path}")
    print(f"PADDLE_OCR_ENABLED={is_paddle_ocr_enabled()}")
    print("-" * 60)

    doc = fitz.open(pdf_path)
    try:
        n = min(doc.page_count, max(1, args.max_pages))
        for i in range(n):
            page = doc[i]
            print(f"\nPage {i + 1}/{doc.page_count}")

            if is_paddle_ocr_enabled():
                t0 = time.time()
                p_text, p_conf = extract_text_with_paddleocr(page, page_num=i)
                print(
                    f"  PaddleOCR: chars={len((p_text or '').strip())} "
                    f"conf={p_conf:.1f}% time={time.time() - t0:.1f}s"
                )
            else:
                print("  PaddleOCR: skipped (flag OFF)")

            t0 = time.time()
            t_text, t_conf = app_mod.extract_text_with_tesseract(page, page_num=i)
            print(
                f"  Tesseract: chars={len((t_text or '').strip())} "
                f"conf={t_conf:.1f}% time={time.time() - t0:.1f}s"
            )

            t0 = time.time()
            s_text, s_conf, s_method = app_mod.extract_scan_ocr_text(
                page, page_num=i, relaxed=False
            )
            print(
                f"  Hybrid:    method={s_method} chars={len((s_text or '').strip())} "
                f"conf={s_conf:.1f}% time={time.time() - t0:.1f}s"
            )
    finally:
        doc.close()

    print("\nDone. For full Laravel JSON compare, call POST /test-extract twice "
          "(PADDLE_OCR_ENABLED=false vs true) on the same POD file.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
