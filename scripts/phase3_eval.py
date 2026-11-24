"""Phase 3 dataset inventory, shadow evaluation, and report.

From the splitextract directory:

    python scripts/phase3_eval.py all --limit 100

The dataset directory is read only. Results are written under phase3_eval/.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from phase3.inventory import inventory_dataset, load_prior_status
from phase3.metrics import aggregate, recommend
from phase3.report import render_report
from phase3.runner import run_batch
from phase3.selection import select_batch


DEFAULT_DATASET = Path(r"C:\Users\amnsa\Downloads\ZL_2026_August")


def _write(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")


def _inventory(dataset: Path, output: Path) -> dict:
    prior = load_prior_status(
        [
            ROOT / "qa_results" / "progress.json",
            ROOT / "qa_results_images" / "progress.json",
        ]
    )
    summary = inventory_dataset(dataset, prior_status=prior)
    _write(output / "inventory.json", summary)
    return summary


def _select(summary: dict, output: Path, limit: int) -> list:
    chosen = select_batch(summary.get("files") or [], limit=limit)
    _write(
        output / "selection.json",
        {"limit": limit, "count": len(chosen), "files": chosen},
    )
    return chosen


def main(argv=None) -> int:
    try:
        from dotenv import load_dotenv

        load_dotenv(ROOT / ".env")
    except Exception:
        pass
    parser = argparse.ArgumentParser(description="Phase 3 shadow evaluation")
    parser.add_argument("command", choices=("inventory", "select", "run", "report", "all"))
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--output", type=Path, default=ROOT / "phase3_eval")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)
    output = args.output
    output.mkdir(parents=True, exist_ok=True)

    summary = None
    if args.command in {"inventory", "select", "all"}:
        print(f"Inventory {args.dataset}", flush=True)
        summary = _inventory(args.dataset, output)
        print(f"Files {summary['total_files']}", flush=True)
    elif (output / "inventory.json").is_file():
        summary = json.loads((output / "inventory.json").read_text(encoding="utf-8"))

    chosen = None
    if args.command in {"select", "all"}:
        chosen = _select(summary or {"files": []}, output, args.limit)
        print(f"Selected {len(chosen)}", flush=True)
    elif args.command in {"run", "report"} and (output / "selection.json").is_file():
        chosen = json.loads((output / "selection.json").read_text(encoding="utf-8")).get("files") or []

    records = []
    if args.command in {"run", "all"}:
        print(f"Shadow batch {len(chosen or [])}", flush=True)
        records = run_batch(
            chosen or [],
            dataset_dir=args.dataset,
            output_dir=output,
            ground_directories=[
                ROOT / "qa_results" / "ground_truth",
                ROOT / "qa_results_images" / "ground_truth",
            ],
            force=args.force,
        )
        metrics = aggregate(records)
        decision = recommend(metrics)
        _write(output / "metrics.json", {"metrics": metrics, "recommendation": decision})
        print(decision["recommendation"], flush=True)
    elif (output / "records").is_dir():
        records = [
            json.loads(path.read_text(encoding="utf-8"))
            for path in sorted((output / "records").glob("*.json"))
        ]

    if args.command in {"report", "all"}:
        if summary is None:
            raise SystemExit("inventory.json is missing")
        text = render_report(summary, records, batch_size=len(chosen or records))
        report_path = output / "PHASE3_REPORT.md"
        report_path.write_text(text, encoding="utf-8")
        print(report_path, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
