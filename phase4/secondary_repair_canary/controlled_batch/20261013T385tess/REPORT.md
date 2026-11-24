# Secondary OCR repair — controlled / 385-document batch

**Run ID:** `20261013T385tess`
**Decision:** `PASS`
**Interrupted:** `False`

## GT-20 reconfirm (source-verified accuracy)

- Decision: `PASS`
- Totals: `{"fields": 331, "incorrect_on": 0, "off_correct": 142, "on_correct": 147, "regressions": 0, "unresolved_missing_on": 0, "verified_corrections": 5}`
- Stop reasons: `[]`

## 385-document operational soak (no accuracy claim)

- Processed: **385**
- Remaining: **0**
- Errors: **0**
- Repair triggered (overlay): **0**
- Docs with qty deltas: **0**
- Delta fields total: **0**
- Primary OCR latency sum: **3299082 ms**
- OFF path latency sum: **2885 ms**
- ON path latency sum: **1637349 ms**

### Cohorts

```json
{
  "ordinary": 284,
  "compatible_ssa": 44,
  "negative_unresolved": 46,
  "ambiguous_poor_quality": 10,
  "compatible_himalaya_qty": 1
}
```

### Stop reasons

- (none)

## Notes

- Accuracy metrics apply **only** to GT-20 reconfirm.
- The 385-image archive has no source GT; metrics are repair triggers, deltas, errors, and latency only.
- `OCR_SECONDARY_REPAIR` restored OFF after the run.
