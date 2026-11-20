"""Run resumable batch QA for PDFs under test_pdfs (or --pdf-dir).

From the splitextract directory:

    python scripts/batch_qa.py --batch-size 100 --workers 1

Completed PDFs are skipped. RATE_LIMITED PDFs are retried. The script calls
the existing extract_sales_statement function and writes qa_results/.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from qa.runner import main

if __name__ == "__main__":
    raise SystemExit(main())
