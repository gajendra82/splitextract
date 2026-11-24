"""Authenticated real-image smoke test for the PaddleOCR-VL cloud worker."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


def _request(url: str, token: str, path: Path, page: int, timeout: float) -> dict:
    boundary = "----OCRSmoke" + str(time.time_ns())
    payload = path.read_bytes()
    content_type = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
    }.get(path.suffix.lower())
    if content_type is None:
        raise ValueError(f"unsupported image extension: {path.suffix}")
    body = (
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{path.name}\"\r\n"
        f"Content-Type: {content_type}\r\n\r\n"
    ).encode("utf-8") + payload + f"\r\n--{boundary}--\r\n".encode("ascii")
    request = urllib.request.Request(
        url.rstrip("/") + "/v1/ocr",
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "X-OCR-Page": str(page),
        },
        method="POST",
    )
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"{path.name}: HTTP {exc.code}: {detail}") from exc
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    required = ("text", "headers", "rows", "tables", "page", "page_info", "status")
    missing = [key for key in required if key not in result]
    if missing:
        raise RuntimeError(f"{path.name}: response missing fields: {', '.join(missing)}")
    if result.get("status") != "success" or not str(result.get("text") or "").strip():
        raise RuntimeError(f"{path.name}: OCR did not return usable text: {result.get('status')}")
    if int(result["page"]) != page or int(result["page_info"].get("page", -1)) != page:
        raise RuntimeError(f"{path.name}: page evidence mismatch")
    result["client_latency_ms"] = round(elapsed_ms, 2)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("images", nargs="+", type=Path)
    parser.add_argument("--page", type=int, default=1)
    parser.add_argument("--timeout", type=float, default=None)
    parser.add_argument("--save-dir", type=Path, default=None)
    args = parser.parse_args()
    url = os.getenv("PADDLE_VL_API_URL", "").strip()
    token = os.getenv("PADDLE_VL_API_KEY", "").strip()
    if not url or not token:
        parser.error("set PADDLE_VL_API_URL and PADDLE_VL_API_KEY in the environment")
    timeout = args.timeout or float(os.getenv("PADDLE_VL_TEST_TIMEOUT_S", "600"))
    if args.page < 1 or timeout <= 0:
        parser.error("--page must be >= 1 and timeout must be positive")
    if args.save_dir:
        args.save_dir.mkdir(parents=True, exist_ok=True)
    failures = 0
    for image in args.images:
        try:
            result = _request(url, token, image, args.page, timeout)
            summary = {
                "file": image.name,
                "status": result["status"],
                "model": result.get("model"),
                "page_info": result["page_info"],
                "latency_ms": result.get("latency_ms"),
                "client_latency_ms": result["client_latency_ms"],
                "headers": result["headers"],
                "rows": result["rows"],
                "text": result["text"],
            }
            print(json.dumps(summary, ensure_ascii=False, indent=2))
            if args.save_dir:
                (args.save_dir / f"{image.name}.ocr.json").write_text(
                    json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
                )
        except Exception as exc:
            failures += 1
            print(f"ERROR {image}: {exc}", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
