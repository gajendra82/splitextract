"""Build primary OCR cache for controlled batch (killable subprocesses)."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HELPER = Path(__file__).resolve().parent / "_primary_ocr_one.py"
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".pdf", ".tif", ".tiff", ".webp", ".bmp"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--image-root", type=Path, required=True)
    ap.add_argument("--out-dir", type=Path, required=True)
    ap.add_argument("--timeout-sec", type=int, default=400)
    ap.add_argument("--resume", action="store_true")
    args = ap.parse_args()

    root = args.image_root.expanduser().resolve()
    files = sorted(
        p for p in root.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_EXTS
    )
    cache_dir = args.out_dir
    cache_dir.mkdir(parents=True, exist_ok=True)
    log_path = cache_dir / "build_log.jsonl"

    env = os.environ.copy()
    env["OCR_ENGINE"] = os.getenv("OCR_ENGINE", "paddleocr-vl")
    env["OCR_SECONDARY_REPAIR"] = "false"
    env["PYTHONUNBUFFERED"] = "1"

    done = 0
    errors = 0
    for idx, path in enumerate(files, start=1):
        out = cache_dir / f"{path.stem}.json"
        if args.resume and out.is_file():
            done += 1
            print(f"[CACHE] {idx}/{len(files)} skip {path.name}", flush=True)
            continue
        tmp = out.with_suffix(".partial.json")
        t0 = time.perf_counter()
        try:
            proc = subprocess.run(
                [
                    sys.executable,
                    "-u",
                    str(HELPER),
                    "--path",
                    str(path),
                    "--out",
                    str(tmp),
                ],
                cwd=str(ROOT),
                env=env,
                capture_output=True,
                text=True,
                timeout=args.timeout_sec,
            )
            if proc.returncode != 0 or not tmp.is_file():
                raise RuntimeError(
                    f"rc={proc.returncode} err={(proc.stderr or proc.stdout or '')[-500:]}"
                )
            payload = json.loads(tmp.read_text(encoding="utf-8"))
            out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            try:
                tmp.unlink()
            except OSError:
                pass
            ms = (time.perf_counter() - t0) * 1000
            done += 1
            row = {
                "document_id": path.name,
                "status": "ok",
                "ms": ms,
                "text_len": payload.get("text_len"),
                "engine": payload.get("engine"),
            }
            print(
                f"[CACHE] {idx}/{len(files)} ok {path.name[:40]} "
                f"ms={ms:.0f} len={payload.get('text_len')}",
                flush=True,
            )
        except subprocess.TimeoutExpired:
            errors += 1
            row = {
                "document_id": path.name,
                "status": "timeout",
                "timeout_sec": args.timeout_sec,
            }
            print(f"[CACHE] {idx}/{len(files)} TIMEOUT {path.name}", flush=True)
            # Kill leftover children from this helper if any.
            try:
                subprocess.run(
                    ["taskkill", "/F", "/IM", "python.exe", "/T"],
                    capture_output=True,
                    check=False,
                )
            except Exception:
                pass
        except Exception as exc:
            errors += 1
            row = {"document_id": path.name, "status": "error", "error": str(exc)[:400]}
            print(f"[CACHE] {idx}/{len(files)} ERROR {path.name}: {exc}", flush=True)
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")

    print(json.dumps({"done": done, "errors": errors, "total": len(files)}), flush=True)
    return 0 if errors == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
