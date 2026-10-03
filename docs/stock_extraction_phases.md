# Stock statement extraction — recommended design + Cursor prompts

Architecture report: `docs/stock_pipeline_architecture_report.md`.

## Status

| Prompt | Status |
|---|---|
| Prompt 0 — project rules | done |
| Prompt 1 — Phase 0 shadow classifier | done |
| Prompt 1b — offline tests + joinable shadow logs | done |
| Prompt 1c — veto enable conditions | checklist (fixtures + shadow_report) |
| **Prompt 2 — Phase 1 identity veto** | **implemented; flag OFF until 1c** |
| Prompt 3–6 | later |

---

## Prompt 1c. Conditions before enabling STOCK_IDENTITY_VETO

Do **not** set `STOCK_IDENTITY_VETO=true` until:

1. ≥ 5 real fixtures in `tests/fixtures/stock/` with hand-checked `expected.json`, including silent_error files from `shadow_report.py`
2. Extra Gemini calls fit Vertex quota
3. Enable **images first**: `STOCK_IDENTITY_VETO_TYPES=image`, then add `scanned_pdf` later
4. Offline suite failing test **ids** still match `tests/baselines/pre_phase1_failures.json`

```bash
journalctl -u splitextract --since "3 days ago" | python scripts/shadow_report.py -
```

---

## Prompt 2. Phase 1 identity veto (implemented, default OFF)

Flag: `STOCK_IDENTITY_VETO` (default **false**). Types: `STOCK_IDENTITY_VETO_TYPES` (default `image,scanned_pdf`).

See `services/stock_row_classifier.py` (`veto_decision`, `veto_active_for`), wired into
`evaluate_extraction_quality`, `maybe_apply_gemini_fallback`, `stock_image_vision`,
`lock_stock_vision_result`, and force-`fail_count=0` branches.

Do **not** flip the flag on in `.env` from this prompt. Start with
`STOCK_IDENTITY_VETO_TYPES=image` when enabling.
