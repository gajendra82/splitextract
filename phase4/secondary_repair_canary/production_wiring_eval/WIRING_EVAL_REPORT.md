# Production wiring GT eval (DI production entrypoint)

**Decision:** `WIRING VERIFIED — READY FOR 385-DOCUMENT EVALUATION`

Primary VL stubbed; secondary Tesseract real; ON path uses real `extract_sales_statement_core` via DI OCR override.

```json
{
  "fields": 331,
  "off_correct": 142,
  "on_correct": 147,
  "verified_corrections": 5,
  "regressions": 0,
  "incorrect_on": 0
}
```

## Stop reasons

- (none)

## Corrections

### `0000700275_2026_08_ZL_18_374_04092026171615.pdf`
- `liv100_cl`: `162` → `37` (expected `37`)

### `0000701078_2026_08_ZL_03_362_05092026080346.jpeg`
- `aact_close`: `89` → `69` (expected `69`)
- `talekt_exp`: `0` → `12` (expected `12`)

### `0000701151_2026_08_ZL_14_361_03092026025650.jpg`
- `tot_op`: `None` → `834578` (expected `834578`)
- `tot_cl`: `None` → `775274` (expected `775274`)
