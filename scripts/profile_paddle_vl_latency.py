"""Profile PaddleOCR-VL 1.6 latency on representative images.

Measures cold vs warm phases; optional Tesseract comparison on the same bytes.
Does NOT run the 385-doc batch. Aborts if primary is not PaddleOCR-VL.

Usage:
  python -u scripts/profile_paddle_vl_latency.py \\
    --image-root \"C:\\Users\\amnsa\\Downloads\\image dataset stock\" \\
    --compare-tesseract
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

OUT_DIR = ROOT / "phase4" / "paddle_vl_benchmark" / "latency_profile"


def _gpu_snapshot() -> Dict[str, Any]:
    try:
        out = subprocess.check_output(
            [
                "nvidia-smi",
                "--query-gpu=utilization.gpu,memory.used,memory.total",
                "--format=csv,noheader,nounits",
            ],
            text=True,
            timeout=10,
        ).strip()
        parts = [p.strip() for p in out.split(",")]
        return {
            "util_pct": float(parts[0]) if parts else None,
            "mem_used_mib": float(parts[1]) if len(parts) > 1 else None,
            "mem_total_mib": float(parts[2]) if len(parts) > 2 else None,
            "raw": out,
        }
    except Exception as exc:
        return {"error": str(exc)}


def _assert_vl_primary() -> Dict[str, Any]:
    os.environ["OCR_ENGINE"] = "paddleocr-vl"
    os.environ["OCR_FALLBACK_ENGINE"] = "off"
    from services.ocr.orchestrator import OCROrchestrator, ocr_settings
    from services.ocr.paddle_vl_engine import ENGINE_NAME, MODEL_NAME

    settings = ocr_settings()
    primary = (settings.primary_engine or "").strip().lower()
    if primary not in {"paddleocr-vl", "paddleocr-vl-1.6", "paddle_vl"}:
        raise SystemExit(f"ABORT: primary={primary!r} not paddleocr-vl")
    orch = OCROrchestrator()
    eng = orch.engine(primary)
    if eng is None or not eng.available():
        raise SystemExit("ABORT: PaddleOCR-VL unavailable")
    name = getattr(eng, "name", None) or ENGINE_NAME
    if "paddleocr-vl" not in str(name).lower():
        raise SystemExit(f"ABORT: engine name={name!r}")
    return {
        "settings_primary": primary,
        "engine_name": name,
        "model": MODEL_NAME,
        "backend": getattr(eng, "_backend", None),
        "device_env": os.getenv("PADDLE_VL_DEVICE") or "",
        "max_side": os.getenv("PADDLE_VL_MAX_SIDE", "2048"),
        "timeout_s": os.getenv("PADDLE_VL_TIMEOUT_S", "300"),
        "orchestrator": orch,
        "engine": eng,
    }


def _select_images(image_root: Path, names: Optional[List[str]]) -> List[Path]:
    from PIL import Image

    if names:
        paths = [image_root / n for n in names]
        missing = [str(p) for p in paths if not p.is_file()]
        if missing:
            raise SystemExit(f"Missing images: {missing}")
        return paths

    rows = []
    for p in sorted(image_root.iterdir()):
        if p.suffix.lower() not in {
            ".jpg",
            ".jpeg",
            ".png",
            ".webp",
            ".tif",
            ".tiff",
            ".bmp",
        }:
            continue
        try:
            with Image.open(p) as im:
                w, h = im.size
            rows.append((w * h, p))
        except Exception:
            continue
    if len(rows) < 5:
        raise SystemExit(f"Need >=5 images under {image_root}, found {len(rows)}")
    rows.sort(key=lambda x: x[0])
    idxs = [0, len(rows) // 4, len(rows) // 2, (3 * len(rows)) // 4]
    big = [r for r in rows if r[0] >= 10_000_000]
    idxs.append(rows.index(big[0]) if big else len(rows) - 1)
    picked = []
    seen = set()
    for i in idxs:
        p = rows[i][1]
        if p.name in seen:
            continue
        seen.add(p.name)
        picked.append(p)
    return picked[:5]


def _table_stats(text: str) -> Dict[str, Any]:
    rows = len(re.findall(r"<tr\b", text, flags=re.I))
    cells = len(re.findall(r"<td\b", text, flags=re.I))
    return {
        "chars": len(text or ""),
        "words": len((text or "").split()),
        "has_table": "<table" in (text or "").lower(),
        "tr_count": rows,
        "td_count": cells,
    }


def _profile_one_vl(
    *,
    path: Path,
    orch,
    eng,
    label: str,
    ensure_worker_ready: bool,
) -> Dict[str, Any]:
    from services.ocr.paddle_vl_engine import (
        _downscale_image_bytes,
        _ensure_serve_proc,
        _kill_serve_proc,
        _serve_lock,
    )

    raw = path.read_bytes()
    from PIL import Image
    import io

    with Image.open(io.BytesIO(raw)) as im:
        orig_w, orig_h = im.size

    row: Dict[str, Any] = {
        "label": label,
        "document_id": path.name,
        "bytes": len(raw),
        "orig_width": orig_w,
        "orig_height": orig_h,
        "megapixels": round((orig_w * orig_h) / 1e6, 3),
        "gpu_before": _gpu_snapshot(),
        "errors": [],
        "worker_restarts": 0,
    }

    # Preprocess only (host side).
    t0 = time.perf_counter()
    prepared, prep_meta = _downscale_image_bytes(raw)
    row["preprocess_ms"] = (time.perf_counter() - t0) * 1000.0
    row["prep_meta"] = prep_meta

    worker_start_ms = None
    if ensure_worker_ready:
        python = eng._resolve_python()
        if not python:
            raise RuntimeError("PADDLE_VL_PYTHON unresolved")
        t1 = time.perf_counter()
        with _serve_lock():
            _kill_serve_proc()
            _ensure_serve_proc(
                python, device=eng._device, timeout_s=float(eng._timeout_s)
            )
        worker_start_ms = (time.perf_counter() - t1) * 1000.0
        # Ready marker is before heavy model load; model loads on first predict.
        row["worker_ready_ms"] = worker_start_ms
        row["note"] = (
            "worker_ready is serve process + ready JSON; "
            "model weights load on first inference inside worker"
        )

    gpu_mid = _gpu_snapshot()
    row["gpu_pre_infer"] = gpu_mid

    t2 = time.perf_counter()
    try:
        bundle = orch.recognize(raw, page=1)
    except Exception as exc:
        row["errors"].append(str(exc))
        row["e2e_ms"] = (time.perf_counter() - t2) * 1000.0
        row["status"] = "error"
        row["gpu_after"] = _gpu_snapshot()
        return row
    e2e_ms = (time.perf_counter() - t2) * 1000.0

    prim = bundle.primary
    text = (prim.text if prim else "") or ""
    meta = dict(getattr(prim, "metadata", None) or {})
    engine = (prim.engine if prim else "") or ""
    if "paddleocr-vl" not in engine.lower():
        raise SystemExit(
            f"ABORT: recognize returned engine={engine!r}, not paddleocr-vl"
        )
    if getattr(bundle, "unverified_fallback", False):
        raise SystemExit("ABORT: unverified_fallback used during profiling")

    # Inference ≈ e2e from orchestrator call (includes IPC + model + parse).
    # Host preprocess already measured separately; orchestrator also re-preps.
    row.update(
        {
            "status": meta.get("status"),
            "primary_engine": engine,
            "model": meta.get("model") or meta.get("model_name"),
            "model_version": meta.get("model_version"),
            "backend": meta.get("backend") or getattr(eng, "_backend", None),
            "resolved_device": meta.get("resolved_device")
            or meta.get("device")
            or getattr(eng, "_resolved_device", None),
            "e2e_ms": e2e_ms,
            "meta_latency_ms": meta.get("latency_ms"),
            "init_latency_ms": meta.get("init_latency_ms"),
            "image_prep": meta.get("image_prep"),
            "error": meta.get("error"),
            "output": _table_stats(text),
            "text_preview": text[:240],
            "gpu_after": _gpu_snapshot(),
            "engines_used": list(getattr(bundle, "engines_used", []) or []),
        }
    )
    # Approximate: inference_ms = e2e (recognize already includes prep inside).
    row["inference_plus_ipc_ms"] = e2e_ms
    row["postprocess_note"] = (
        "parsing bundled inside worker evidence + host _result_from_evidence; "
        "not separately exposed — included in e2e_ms / meta_latency_ms"
    )
    return row


def _profile_tesseract(path: Path) -> Dict[str, Any]:
    """Raw Tesseract only — bypass PrimaryOCRPytesseractShim (which routes to VL)."""
    import io

    import pytesseract
    from PIL import Image

    cmd = os.getenv("TESSERACT_CMD", "").strip()
    if cmd:
        pytesseract.pytesseract.tesseract_cmd = cmd
    elif os.name == "nt":
        win_cmd = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
        if os.path.exists(win_cmd):
            pytesseract.pytesseract.tesseract_cmd = win_cmd

    raw = path.read_bytes()
    image = Image.open(io.BytesIO(raw)).convert("RGB")
    t0 = time.perf_counter()
    text = pytesseract.image_to_string(image, config="--psm 6") or ""
    ms = (time.perf_counter() - t0) * 1000.0
    return {
        "document_id": path.name,
        "primary_engine": "tesseract-raw",
        "status": "success",
        "e2e_ms": ms,
        "output": _table_stats(text),
        "text_preview": text[:240],
        "note": "bypassed PrimaryOCRPytesseractShim; not production DI path",
    }


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image-root", type=Path, required=True)
    parser.add_argument(
        "--images",
        nargs="*",
        default=None,
        help="Optional explicit filenames under image-root",
    )
    parser.add_argument("--compare-tesseract", action="store_true")
    parser.add_argument(
        "--tag",
        default=None,
        help="Output tag (default: UTC timestamp)",
    )
    parser.add_argument(
        "--skip-cold-kill",
        action="store_true",
        help="Do not kill serve worker before first image (warm-only)",
    )
    args = parser.parse_args(argv)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    tag = args.tag or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_path = OUT_DIR / f"profile_{tag}.json"

    ctx = _assert_vl_primary()
    paths = _select_images(args.image_root, args.images)
    print(
        f"[PROFILE] primary={ctx['settings_primary']} "
        f"engine={ctx['engine_name']} model={ctx['model']} "
        f"backend={ctx['backend']} max_side={ctx['max_side']}",
        flush=True,
    )
    for p in paths:
        print(f"[PROFILE] image {p.name}", flush=True)

    from services.ocr.paddle_vl_engine import _kill_serve_proc

    report: Dict[str, Any] = {
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "tag": tag,
        "assert": {
            k: ctx[k]
            for k in (
                "settings_primary",
                "engine_name",
                "model",
                "backend",
                "device_env",
                "max_side",
                "timeout_s",
            )
        },
        "gpu_idle": _gpu_snapshot(),
        "images": [str(p) for p in paths],
        "vl_runs": [],
        "tesseract_runs": [],
    }

    orch = ctx["orchestrator"]
    eng = ctx["engine"]

    # Cold: kill worker, first image pays ready+first-load.
    if not args.skip_cold_kill:
        _kill_serve_proc()
        # Drop orchestrator engine cache so backend re-resolves cleanly.
        try:
            eng._pipeline = None
        except Exception:
            pass

    for idx, path in enumerate(paths):
        label = "cold_first" if idx == 0 and not args.skip_cold_kill else "warm"
        ensure = idx == 0 and not args.skip_cold_kill
        print(f"[PROFILE] VL {label} {path.name} ...", flush=True)
        row = _profile_one_vl(
            path=path,
            orch=orch,
            eng=eng,
            label=label,
            ensure_worker_ready=ensure,
        )
        report["vl_runs"].append(row)
        print(
            f"[PROFILE]   engine={row.get('primary_engine')} "
            f"status={row.get('status')} e2e_ms={row.get('e2e_ms'):.0f} "
            f"chars={((row.get('output') or {}).get('chars'))} "
            f"prep_ms={row.get('preprocess_ms'):.1f}",
            flush=True,
        )

    if args.compare_tesseract:
        for path in paths:
            print(f"[PROFILE] Tesseract {path.name} ...", flush=True)
            trow = _profile_tesseract(path)
            report["tesseract_runs"].append(trow)
            print(
                f"[PROFILE]   tess e2e_ms={trow.get('e2e_ms'):.0f} "
                f"chars={((trow.get('output') or {}).get('chars'))}",
                flush=True,
            )

    # Summary ratios
    summary = []
    tess_by = {r["document_id"]: r for r in report["tesseract_runs"]}
    for r in report["vl_runs"]:
        tid = r["document_id"]
        entry = {
            "document_id": tid,
            "label": r.get("label"),
            "vl_e2e_ms": r.get("e2e_ms"),
            "vl_chars": (r.get("output") or {}).get("chars"),
            "tess_e2e_ms": (tess_by.get(tid) or {}).get("e2e_ms"),
            "tess_chars": ((tess_by.get(tid) or {}).get("output") or {}).get(
                "chars"
            ),
        }
        if entry["vl_e2e_ms"] and entry["tess_e2e_ms"]:
            entry["vl_over_tess"] = round(
                float(entry["vl_e2e_ms"]) / float(entry["tess_e2e_ms"]), 2
            )
        summary.append(entry)
    report["summary"] = summary

    out_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    md = OUT_DIR / f"profile_{tag}.md"
    lines = [
        f"# PaddleOCR-VL latency profile `{tag}`",
        "",
        f"- Primary: `{ctx['engine_name']}` / `{ctx['model']}`",
        f"- Backend: `{ctx['backend']}` max_side=`{ctx['max_side']}`",
        f"- GPU idle: `{report['gpu_idle']}`",
        "",
        "| doc | label | VL ms | Tess ms | ratio | VL chars | Tess chars |",
        "|---|---|---:|---:|---:|---:|---:|",
    ]
    for s in summary:
        lines.append(
            f"| `{s['document_id'][:40]}` | {s['label']} | "
            f"{s.get('vl_e2e_ms') and round(s['vl_e2e_ms'])} | "
            f"{s.get('tess_e2e_ms') and round(s['tess_e2e_ms'])} | "
            f"{s.get('vl_over_tess', '')} | {s.get('vl_chars')} | "
            f"{s.get('tess_chars')} |"
        )
    md.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"[PROFILE] wrote {out_path}", flush=True)
    print(f"[PROFILE] wrote {md}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
