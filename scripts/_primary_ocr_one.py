"""One-shot primary OCR for controlled batch (subprocess-killable)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--path", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    path: Path = args.path
    file_bytes = path.read_bytes()
    from services.ocr.orchestrator import OCROrchestrator

    orch = OCROrchestrator()
    bundle = orch.recognize(file_bytes, page=1)
    primary = getattr(bundle, "primary", None)
    text = (primary.text if primary else "") or ""
    engine = (primary.engine if primary else "") or ""
    payload = {
        "document_id": path.name,
        "engine": engine,
        "text": text,
        "text_len": len(text),
        "status": (getattr(primary, "metadata", None) or {}).get("status"),
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload), encoding="utf-8")
    print(json.dumps({"ok": True, "engine": engine, "text_len": len(text)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
