# Expanded secondary OCR repair canary

**Decision:** `EXPANDED CANARY PASS`

Baseline: `C:/Users/amnsa/OneDrive/Desktop/zydus v1/splitextract/phase4/secondary_repair_canary/CANARY_REPORT.md`

## Preflight

```json
{
  "unset_enabled": false,
  "false_enabled": false,
  "noop_when_off": true,
  "baseline_report_exists": true,
  "pass": true
}
```

## Scope

- Source-verified GT documents evaluated: **20**
- Not the 385-image batch; not a general production accuracy claim.
- Flag default remains OFF; environment restored OFF after the run.

## Totals

```json
{
  "fields_off_correct": 380,
  "fields_on_correct": 385,
  "fields_total": 388,
  "verified_corrections": 5,
  "incorrect_repairs": 0,
  "regressions": 0,
  "unchanged_fields": 383,
  "unresolved_missing_on": 3,
  "docs_with_repairs": 3,
  "secondary_ocr_ms_sum_on": 57562.7,
  "secondary_ocr_ms_sum_off": 156.80000000000004,
  "docs_evaluated": 20,
  "flag_restored_off": true
}
```

## Stop reasons

- (none)

## Per-document

### `0000700084_2026_08_ZL_06_8236_03092026155136.pdf` (ordinary)

- Fields: OFF 15/15 → ON 15/15
- Secondary OCR latency: OFF 10.0 ms, ON 4340.9 ms (DI helper ON 3944.0 ms, separate)
- Repair types ON: `[]`
- Deltas: none

### `0000700124_2026_08_ZL_06_521_02092026204703.pdf` (negative_unresolved)

- Fields: OFF 11/12 → ON 11/12
- Secondary OCR latency: OFF 11.0 ms, ON 3730.4 ms (DI helper ON 3800.6 ms, separate)
- Repair types ON: `[]`
- Deltas: none

### `0000700209_2026_08_ZL_18_374_04092026060313.pdf` (negative_unresolved)

- Fields: OFF 14/16 → ON 14/16
- Secondary OCR latency: OFF 11.4 ms, ON 3503.6 ms (DI helper ON 3517.5 ms, separate)
- Repair types ON: `[]`
- Deltas: none

### `0000700275_2026_08_ZL_18_374_04092026171615.pdf` (repair_liv)

- Fields: OFF 20/21 → ON 21/21
- Secondary OCR latency: OFF 7.8 ms, ON 3923.7 ms (DI helper ON 3890.0 ms, separate)
- Repair types ON: `['liv_printed_closing_ocr']`
- Deltas:
  - `liv100_cl`: `162` → `37` (correct False→True; expected `37`)

### `0000700308_2026_08_ZL_18_8175_03092026072320.jpg` (compatible_ssa_like)

- Fields: OFF 9/9 → ON 9/9
- Secondary OCR latency: OFF 2.3 ms, ON 6656.3 ms (DI helper ON 6580.3 ms, separate)
- Repair types ON: `[]`
- Deltas: none

### `0000700317_2026_08_ZL_18_374_02092026171443.pdf` (compatible_ssa_like)

- Fields: OFF 20/20 → ON 20/20
- Secondary OCR latency: OFF 4.2 ms, ON 704.5 ms (DI helper ON 690.0 ms, separate)
- Repair types ON: `[]`
- Deltas: none

### `0000700346_2026_08_ZL_18_8189_05092026025159.pdf` (ordinary)

- Fields: OFF 18/18 → ON 18/18
- Secondary OCR latency: OFF 10.6 ms, ON 3186.4 ms (DI helper ON 3339.4 ms, separate)
- Repair types ON: `[]`
- Deltas: none

### `0000700358_2026_08_ZL_18_8173_05092026185826.pdf` (compatible_ssa_like)

- Fields: OFF 21/21 → ON 21/21
- Secondary OCR latency: OFF 3.5 ms, ON 1170.5 ms (DI helper ON 1165.0 ms, separate)
- Repair types ON: `[]`
- Deltas: none

### `0000700364_2026_08_ZL_18_8173_05092026190156.pdf` (compatible_himalaya_qty)

- Fields: OFF 20/20 → ON 20/20
- Secondary OCR latency: OFF 4.7 ms, ON 1521.8 ms (DI helper ON 1454.2 ms, separate)
- Repair types ON: `[]`
- Deltas: none

### `0000700406_2026_08_ZL_10_384_05092026140711.jpg` (ordinary)

- Fields: OFF 35/35 → ON 35/35
- Secondary OCR latency: OFF 4.2 ms, ON 1339.4 ms (DI helper ON 1322.3 ms, separate)
- Repair types ON: `[]`
- Deltas: none

### `0000700434_2026_08_ZL_10_386_05092026034503.jpg` (compatible_ssa_like)

- Fields: OFF 23/23 → ON 23/23
- Secondary OCR latency: OFF 29.1 ms, ON 2405.2 ms (DI helper ON 2311.9 ms, separate)
- Repair types ON: `[]`
- Deltas: none

### `0000700835_2026_08_ZL_25_447_03092026105819.jpeg` (compatible_himalaya_qty)

- Fields: OFF 29/29 → ON 29/29
- Secondary OCR latency: OFF 3.8 ms, ON 943.5 ms (DI helper ON 979.4 ms, separate)
- Repair types ON: `[]`
- Deltas: none

### `0000701012_2026_08_ZL_11_423_03092026183736.jpg` (ordinary)

- Fields: OFF 14/14 → ON 14/14
- Secondary OCR latency: OFF 5.7 ms, ON 2128.0 ms (DI helper ON 2243.3 ms, separate)
- Repair types ON: `[]`
- Deltas: none

### `0000701078_2026_08_ZL_03_362_05092026080346.jpeg` (repair_himalaya)

- Fields: OFF 16/18 → ON 18/18
- Secondary OCR latency: OFF 11.4 ms, ON 10163.7 ms (DI helper ON 9991.0 ms, separate)
- Repair types ON: `['himalaya_row_anchor_ocr']`
- Deltas:
  - `aact_close`: `89` → `69` (correct False→True; expected `69`)
  - `talekt_exp`: `0` → `12` (correct False→True; expected `12`)

### `0000701151_2026_08_ZL_14_361_03092026025650.jpg` (repair_total_footer)

- Fields: OFF 17/19 → ON 19/19
- Secondary OCR latency: OFF 6.6 ms, ON 2171.4 ms (DI helper ON 2272.9 ms, separate)
- Repair types ON: `['total_footer_ocr']`
- Deltas:
  - `tot_cl`: `None` → `775274` (correct False→True; expected `775274`)
  - `tot_op`: `None` → `834578` (correct False→True; expected `834578`)

### `0000701275_2026_08_ZL_03_360_02092026152716.jpg` (ordinary)

- Fields: OFF 24/24 → ON 24/24
- Secondary OCR latency: OFF 5.3 ms, ON 1788.3 ms (DI helper ON 1830.4 ms, separate)
- Repair types ON: `[]`
- Deltas: none

### `0000701851_2026_08_ZL_20_302_03092026134940.jpg` (compatible_himalaya_qty)

- Fields: OFF 22/22 → ON 22/22
- Secondary OCR latency: OFF 5.4 ms, ON 1508.8 ms (DI helper ON 1524.3 ms, separate)
- Repair types ON: `[]`
- Deltas: none

### `0000701864_2026_08_ZL_20_5015_03092026091037.jpg` (compatible_himalaya_qty)

- Fields: OFF 20/20 → ON 20/20
- Secondary OCR latency: OFF 3.0 ms, ON 1429.7 ms (DI helper ON 1379.7 ms, separate)
- Repair types ON: `[]`
- Deltas: none

### `0000701904_2026_08_ZL_20_8227_05092026154848.jpg` (ordinary)

- Fields: OFF 20/20 → ON 20/20
- Secondary OCR latency: OFF 10.4 ms, ON 3426.0 ms (DI helper ON 3317.5 ms, separate)
- Repair types ON: `[]`
- Deltas: none

### `0000704337_2026_08_ZL_24_259_03092026042213.pdf` (ordinary)

- Fields: OFF 12/12 → ON 12/12
- Secondary OCR latency: OFF 6.4 ms, ON 1520.6 ms (DI helper ON 1486.2 ms, separate)
- Repair types ON: `[]`
- Deltas: none

## Commands

```text
python scripts/run_secondary_repair_expanded_canary.py
```

Artifacts: `C:/Users/amnsa/OneDrive/Desktop/zydus v1/splitextract/phase4/secondary_repair_canary/expanded`
