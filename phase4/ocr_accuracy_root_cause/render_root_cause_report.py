"""Render ROOT_CAUSE_REPORT.md from investigation_aggregate.json (read-only)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
AGG = HERE / "investigation_aggregate.json"
OUT = HERE / "ROOT_CAUSE_REPORT.md"


def pct(x) -> str:
    return "n/a" if x is None else f"{100 * x:.1f}%"


def esc(x) -> str:
    s = "" if x is None else str(x)
    return s.replace("|", "\\|").replace("\n", " ")


def main() -> int:
    p = json.loads(AGG.read_text(encoding="utf-8"))
    m = p["stage_metrics"]
    cats = p["error_category_counts"]
    lines: list[str] = []
    A = lines.append

    A("# GT-20 OCR Accuracy Root-Cause Investigation")
    A("")
    A(f"Created: {datetime.now(timezone.utc).isoformat()}")
    A("")
    A("## Scope and method")
    A("")
    A("- **Dataset:** SOURCE_VERIFIED GT-20 (`phase4/ground_truth_50/seed_index_20.json`)")
    A("- **OCR input:** stored PaddleOCR-VL pages (`phase4/paddle_vl_benchmark/per_document`)")
    A("- **Extraction score:** Phase2-OFF arm from `phase4/phase2_ab_gt20` (`vl_html_table_parse`)")
    A("- **Constraint:** read-only; GT/production/benchmark inputs unchanged; no stock-equation guessing; no LLM quantities")
    A("- **Artifacts:** `investigation_aggregate.json`, `incorrect_quantity_traces.json`, `investigate_gt20_qty_errors.py`")
    A("")
    A("## Headline finding (confirmed)")
    A("")
    A(
        "Published **field accuracy 51.8%** / **quantity accuracy 46.5%** measure "
        "**extraction JSON scoring**, not raw OCR digit quality."
    )
    A("")
    A("| Evaluation | Quantity accuracy | Source |")
    A("|---|---:|---|")
    A(
        "| Direct OCR/HTML `match_field` (stored VL) | **99.3%** | "
        "`phase4/PADDLE_VL_GROUND_TRUTH_20_REPORT.md` |"
    )
    A(
        "| Extraction `score_extraction` Phase2 OFF | **46.5%** (134/288) | "
        "`phase4/phase2_ab_gt20/COMPARISON_REPORT.md` |"
    )
    A(
        "| Expected qty token present in VL text | **98.3%** (283/288) | this investigation |"
    )
    A("")
    A(
        "Structurally valid VL HTML tables are **not** proof of numeric extraction "
        "accuracy: many tables parse, yet column mapping / product matching drop or zero the GT cells."
    )
    A("")
    A("## Independent stage metrics (quantity fields only)")
    A("")
    A(
        "Denominators are SOURCE_VERIFIED `field_type=quantity` from the Phase2-OFF bench "
        "(n=288 unless noted). Metrics are **not** combined."
    )
    A("")
    A("| Metric | Correct | Total | Accuracy |")
    A("|---|---:|---:|---:|")
    for key, label in [
        ("raw_ocr_numeric_accuracy", "Raw OCR numeric (expected token in VL text)"),
        (
            "table_cell_extraction_accuracy",
            "Table cell extraction (HTML cell @ product row + header)",
        ),
        ("row_alignment_accuracy", "Row alignment (GT product found in HTML cells)"),
        (
            "header_mapping_accuracy",
            "Header mapping (scorer OR production matcher)",
        ),
        ("final_quantity_accuracy", "Final quantity accuracy (score_extraction)"),
        (
            "exact_product_row_accuracy",
            "Exact product-row accuracy (all qtys correct per GT product)",
        ),
    ]:
        v = m[key]
        A(f"| {label} | {v['correct']} | {v['total']} | {pct(v['accuracy'])} |")
    A("")
    A("Notes:")
    for key in m:
        A(f"- {m[key]['note']}")
    A(
        "- Bounding boxes: **0 verified** in GT (`bbox` null); coordinates unavailable — marked n/a per field."
    )
    A("")
    A("## Error classification (154 incorrect quantities)")
    A("")
    A("| Cat | Name | Count | Confirmed | Share of incorrect |")
    A("|---|---|---:|---:|---:|")
    for k in "ABCDEFGH":
        if k not in cats:
            continue
        v = cats[k]
        share = 100.0 * v["count"] / 154.0
        A(
            f"| {k} | {v['name']} | {v['count']} | {v['confirmed']} | {share:.1f}% |"
        )
    A("")
    A("### Confirmed vs suspected")
    A("")
    A(
        "- **Confirmed:** stage evidence shows the failure locus "
        "(e.g. extract has correct `opening_qty` but scorer returns UNMAPPED for `OpStk`)."
    )
    A(
        "- **Suspected / undetermined:** token-presence heuristics only, or conflicting "
        "signals — labeled `confirmed=false` / category H."
    )
    A("")
    A("### Mechanism breakdown inside top categories")
    A("")
    A("| Cat | Mechanism | Count | Confidence |")
    A("|---|---|---:|---|")
    A("| D | Product visible; qty columns unmapped / empty extract | 38 | confirmed |")
    A(
        "| D | HTML cell correct; mapped line_item field wrong (Free vs Qty / shift) | 24 | confirmed |"
    )
    A(
        "| D | OCR garbage headers (`0g/0tv`); only product_name matched | 8 | confirmed |"
    )
    A(
        "| D | Expected digit in OCR; extract=0 (shift suspected) | 3 | suspected |"
    )
    A(
        "| E | HTML product row exists; missing from extract line_items | 37 | confirmed |"
    )
    A(
        "| E | Ambiguous pack / first stem match (e.g. RENALKA 100 vs 200) | 29 | confirmed |"
    )
    A(
        "| G | Scorer `_canonical_field` misses `OpStk` / `Bal.` synonyms | 9 | confirmed |"
    )
    A("| A | Expected digit absent / disagreed in VL text | 3 | suspected |")
    A("| H | Insufficient evidence | 3 | undetermined |")
    A("")
    A("## Pipeline stage map (where accuracy is lost)")
    A("")
    A("```")
    A("Source page")
    A("  -> stored PaddleOCR-VL text/HTML     ~98% expected digits present")
    A("  -> HTML row for GT product           ~97% rows found")
    A("  -> cell under GT header              ~75% cells match GT")
    A("  -> column_matcher + parse_html_table  <-- major loss (D)")
    A("  -> product match into line_items      <-- major loss (E)")
    A("  -> score_extraction / _canonical_field <-- false negatives (G)")
    A("  -> published quantity accuracy        46.5%")
    A("```")
    A("")
    A("Relevant production / bench functions:")
    A("- OCR store: `phase4/paddle_vl_benchmark/per_document/*.json`")
    A("- HTML extract: `services/vl_html_table.py::parse_vl_html_table`")
    A("- Table parse: `services/html_table/html_table_parser.py::parse_html_table`")
    A("- Headers: `services/html_table/column_matcher.py::match_headers`")
    A("- Cell grid: `services/ground_truth/html_table.py::parse_html_tables`")
    A(
        "- Score: `scripts/benchmark_phase2_ab_gt20.py::score_extraction`, `_canonical_field`, `_find_line_item`"
    )
    A("")
    A("## Representative confirmed cases")
    A("")
    A("### G — Scorer false negative (`OpStk`)")
    A("")
    A("- Doc `0000700084...pdf` p1, product `ACTARIL SOAP`, field `OpStk`")
    A(
        "- GT `64.0` | OCR/HTML cell `64.0` | extract `opening_qty=64` | scored predicted=null `UNMAPPED_HEADER`"
    )
    A(
        "- Cause: bench `_canonical_field` lacks `op.?stk`; production `match_headers` maps it"
    )
    A("- Source: `scripts/benchmark_phase2_ab_gt20.py::_canonical_field`")
    A("")
    A("### D — Garbage OCR headers → empty extract")
    A("")
    A("- Doc `0000700124...pdf` p1, product `AACTARIL SOAP`, field `Op Qty`")
    A("- VL headers become `0g\\n0tv` / `Sales\\n0tv` / `Closure\\n0tv`")
    A("- `match_headers` keeps only `product_name` → **0 line_items**")
    A(
        "- GT qty tokens (20/144/40/124) exist on body rows; product OCR also mangled (`ANTIBIOL SOP`)"
    )
    A(
        "- Sources: stored VL text; `column_matcher.match_headers`; `html_table_parser.resolve_column_mapping`"
    )
    A("")
    A("### D — Free/Qty column collision")
    A("")
    A("- Docs such as `0000700209`, `0000700275`: multi-header Purchase/Sales Free columns")
    A(
        "- Warnings: `Purchase` also matches `receive_qty`; first Free-like column can win"
    )
    A(
        "- HTML cell under GT header often correct; line_item canonical field becomes `0`"
    )
    A("- Source: `column_matcher.match_headers` first-match policy")
    A("")
    A("### E — Pack ambiguity")
    A("")
    A("- Doc `0000700209...pdf`, `RENALKA SYRUP` Op/Cl GT=12")
    A("- HTML has 100ML row (all 0) and 200ML row (12/12)")
    A("- `_find_line_item` returns first stem match → predicted 0")
    A(
        "- Source: `benchmark_phase2_ab_gt20.py::_find_line_item` (+ extract lacking pack disambiguation)"
    )
    A("")
    A("### E — Product in HTML, absent from extract")
    A("")
    A(
        "- Common on docs with partial mapping / dropped rows / name decoration "
        "(`$^{*}$`, truncated names)"
    )
    A("- Cell grid still tags `product_key`; sales extract line_items omit the row")
    A("")
    A("## Top five root causes by impact")
    A("")
    A(
        "| Rank | Cause | Impact (incorrect qtys) | Confidence | Smallest targeted fix (DO NOT IMPLEMENT YET) |"
    )
    A("|---:|---|---:|---|---|")
    A(
        "| 1 | **D — Qty header mapping failures** (unmapped/garbage headers → empty or zeroed extract) "
        "| **73** | confirmed (69) | Strengthen header repair for OCR-mangled qty headers; prefer Qty "
        "over Free when both match; fail soft to positional stock columns when fuzzy qty set empty |"
    )
    A(
        "| 2 | **E — Product matching / pack disambiguation** | **66** | confirmed (66) | Match "
        "product+pack (or GT packing); require unique best match; avoid first-stem wins |"
    )
    A(
        "| 3 | **Cell→field binding after multi-row headers** (subset of D: correct HTML cell, wrong "
        "canonical) | **24** (of the 73) | confirmed | Align extract parser with `parse_html_tables` "
        "span-expansion / Free-vs-Qty repairs already used in GT eval |"
    )
    A(
        "| 4 | **G — Bench scorer header synonyms** (`OpStk`, `Bal.`) | **9** | confirmed | Add "
        "`op.?stk` / bare `Bal.` to `_canonical_field` (eval-only; does not change production) |"
    )
    A(
        "| 5 | **A — True OCR digit / residual misses** | **3** | mostly suspected | Low priority "
        "vs 1–4; only after mapping/matching fixed |"
    )
    A("")
    A(
        "If ranks 1–4 were fixed without improving OCR, quantity accuracy could rise from **46.5%** "
        "toward the **~75% table-cell** / **~98% OCR-token** ceilings measured here — exact uplift "
        "depends on overlap between D and E on the same fields."
    )
    A("")
    A("## Field accuracy context (51.8%)")
    A("")
    A("- Verified fields: **388**; correct: **201** → 51.8%")
    A(
        "- Quantity incorrect: **154**; other field misses: **33** (headers/text/unsupported)"
    )
    A("- Fixing the quantity path dominates the headline field score")
    A("")
    A("## Per-incorrect-quantity catalog (154)")
    A("")
    A(
        "Full structured traces (raw OCR flags, extract snapshots, column maps, reasons): "
        "`incorrect_quantity_traces.json`."
    )
    A("")
    A(
        "| Doc | Pg | Product | Field | GT | OCR token? | HTML cell | Extracted | Cat | Conf | Bench err | BBox | Source |"
    )
    A("|---|---:|---|---|---|---|---|---|---|---|---|---|---|")

    for t in p["all_incorrect_quantity_traces"]:
        doc = t["document_id"]
        if len(doc) > 28:
            doc = doc[:20] + "…" + doc[-6:]
        prod = esc(t["product_name"])
        if len(prod) > 28:
            prod = prod[:28] + "…"
        src = esc((t.get("source_file_function") or "").split(" ")[0][:48])
        bbox = "n/a" if t.get("bbox") in (None, "", []) else esc(t.get("bbox"))
        A(
            "| {doc} | {pg} | {prod} | {fld} | {gt} | {ocr} | {cell} | {ext} | {cat} | {conf} | {err} | {bbox} | {src} |".format(
                doc=doc,
                pg=t.get("page"),
                prod=prod,
                fld=esc(t.get("field_name")),
                gt=esc(t.get("ground_truth_value")),
                ocr="Y" if t.get("raw_ocr_has_expected_token") else "N",
                cell=esc(t.get("parsed_table_cell_value")),
                ext=esc(t.get("final_extracted_value")),
                cat=t.get("failure_category"),
                conf="Y" if t.get("confirmed") else "N",
                err=esc(t.get("bench_error_type")),
                bbox=bbox,
                src=src,
            )
        )

    A("")
    A("### Per-field reason index")
    A("")
    A(
        "For every incorrect quantity, `incorrect_quantity_traces.json` includes: "
        "`document_id`, `page`, `product_name`, `field_name`, `ground_truth_value`, "
        "`raw_ocr_has_expected_token`, `parsed_table_cell_value`, `final_extracted_value`, "
        "`extract_line_item`, `bbox`, `failure_category`, `confirmed`, `reason`, "
        "`source_file_function`, `columns_matched`, `columns_unmatched`."
    )
    A("")
    A("## What was ruled out")
    A("")
    A(
        "- **Primary cause is not raw VL digit OCR** for this published score "
        "(OCR-token presence 98.3%; direct OCR eval 99.3%)."
    )
    A("- **Phase 2 Gemini recovery** was OFF in the scored arm — not implicated.")
    A("- **Stock-equation post-correction** was not used in this investigation.")
    A("")
    A("## Recommended next steps (investigation only — no implementation)")
    A("")
    A(
        "1. Eval-only: extend `_canonical_field` for `OpStk`/`Bal.` to remove G false negatives."
    )
    A(
        "2. Production: Qty-vs-Free disambiguation + mangled-header repair in "
        "`column_matcher` / `resolve_column_mapping`."
    )
    A(
        "3. Production: product+pack matching in HTML parse and in bench `_find_line_item`."
    )
    A(
        "4. Re-score GT-20 extraction after (1)–(3); only then invest in OCR digit work "
        "for the residual A/H cases."
    )
    A("")

    OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote {OUT} lines={len(lines)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
