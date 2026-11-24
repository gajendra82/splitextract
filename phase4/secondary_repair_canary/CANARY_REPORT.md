# Secondary OCR repair canary report

**Decision:** `CANARY PASS — READY FOR EXPANDED CONTROLLED ROLLOUT`

## Preflight

```json
{
  "unset_enabled": false,
  "unset_noop": true,
  "false_enabled": false,
  "value_'0'": true,
  "value_'no'": true,
  "value_'TRUE'": true,
  "value_'on'": true,
  "pipeline_off_unchanged": true,
  "pipeline_off_meta_disabled": true,
  "pass": true
}
```

## Document list

- `0000701078_2026_08_ZL_03_362_05092026080346.jpeg` — role `himalaya_expiry_breakage` — OFF 16/18 → ON 18/18
- `0000701151_2026_08_ZL_14_361_03092026025650.jpg` — role `total_footer` — OFF 17/19 → ON 19/19
- `0000700275_2026_08_ZL_18_374_04092026171615.pdf` — role `liv52_closing` — OFF 20/21 → ON 21/21
- `0000700084_2026_08_ZL_06_8236_03092026155136.pdf` — role `ordinary_compatible` — OFF 15/15 → ON 15/15
- `0000700209_2026_08_ZL_18_374_04092026060313.pdf` — role `negative_blank_unresolved` — OFF 14/16 → ON 14/16

## Stop reasons

- (none)

## Per-document findings

### `0000701078_2026_08_ZL_03_362_05092026080346.jpeg` (himalaya_expiry_breakage)

- Latency: OFF 31.3 ms, ON 21051.1 ms
- Repair types ON: `['himalaya_row_anchor_ocr']`
- Pipeline helper text matched entrypoint: `True`
- Field deltas (OFF → ON):
  - `aact_close`: `89` → `69` (correct False → True; expected `69`)
  - `talekt_exp`: `0` → `12` (correct False → True; expected `12`)
- Watch fields:
  - `aact_close`: OFF `{'field_id': 'aact_close', 'expected_raw': '69', 'predicted_raw': '89', 'correct': False, 'error_type': 'WRONG_VALUE', 'silent_wrong': True, 'match_mode': 'column_aware_table'}` / ON `{'field_id': 'aact_close', 'expected_raw': '69', 'predicted_raw': '69', 'correct': True, 'error_type': None, 'silent_wrong': False, 'match_mode': 'column_aware_table'}`
  - `talekt_exp`: OFF `{'field_id': 'talekt_exp', 'expected_raw': '12', 'predicted_raw': '0', 'correct': False, 'error_type': 'WRONG_VALUE', 'silent_wrong': True, 'match_mode': 'column_aware_table'}` / ON `{'field_id': 'talekt_exp', 'expected_raw': '12', 'predicted_raw': '12', 'correct': True, 'error_type': None, 'silent_wrong': False, 'match_mode': 'column_aware_table'}`

### `0000701151_2026_08_ZL_14_361_03092026025650.jpg` (total_footer)

- Latency: OFF 13.0 ms, ON 4375.6 ms
- Repair types ON: `['total_footer_ocr']`
- Pipeline helper text matched entrypoint: `True`
- Field deltas (OFF → ON):
  - `tot_cl`: `None` → `775274` (correct False → True; expected `775274`)
  - `tot_op`: `None` → `834578` (correct False → True; expected `834578`)
- Watch fields:
  - `tot_op`: OFF `{'field_id': 'tot_op', 'expected_raw': '834578', 'predicted_raw': None, 'correct': False, 'error_type': 'MISSING', 'silent_wrong': False, 'match_mode': None}` / ON `{'field_id': 'tot_op', 'expected_raw': '834578', 'predicted_raw': '834578', 'correct': True, 'error_type': None, 'silent_wrong': False, 'match_mode': 'column_aware_table'}`
  - `tot_cl`: OFF `{'field_id': 'tot_cl', 'expected_raw': '775274', 'predicted_raw': None, 'correct': False, 'error_type': 'MISSING', 'silent_wrong': False, 'match_mode': None}` / ON `{'field_id': 'tot_cl', 'expected_raw': '775274', 'predicted_raw': '775274', 'correct': True, 'error_type': None, 'silent_wrong': False, 'match_mode': 'column_aware_table'}`
  - `aact_cl`: OFF `{'field_id': 'aact_cl', 'expected_raw': '54', 'predicted_raw': '54', 'correct': True, 'error_type': None, 'silent_wrong': False, 'match_mode': 'column_aware_table'}` / ON `{'field_id': 'aact_cl', 'expected_raw': '54', 'predicted_raw': '54', 'correct': True, 'error_type': None, 'silent_wrong': False, 'match_mode': 'column_aware_table'}`

### `0000700275_2026_08_ZL_18_374_04092026171615.pdf` (liv52_closing)

- Latency: OFF 19.3 ms, ON 7498.2 ms
- Repair types ON: `['liv_printed_closing_ocr']`
- Pipeline helper text matched entrypoint: `True`
- Field deltas (OFF → ON):
  - `liv100_cl`: `162` → `37` (correct False → True; expected `37`)
- Watch fields:
  - `liv100_cl`: OFF `{'field_id': 'liv100_cl', 'expected_raw': '37', 'predicted_raw': '162', 'correct': False, 'error_type': 'WRONG_VALUE', 'silent_wrong': True, 'match_mode': 'column_aware_table'}` / ON `{'field_id': 'liv100_cl', 'expected_raw': '37', 'predicted_raw': '37', 'correct': True, 'error_type': None, 'silent_wrong': False, 'match_mode': 'column_aware_table'}`

### `0000700084_2026_08_ZL_06_8236_03092026155136.pdf` (ordinary_compatible)

- Latency: OFF 22.9 ms, ON 7889.3 ms
- Repair types ON: `[]`
- Pipeline helper text matched entrypoint: `True`
- Field deltas: none

### `0000700209_2026_08_ZL_18_374_04092026060313.pdf` (negative_blank_unresolved)

- Latency: OFF 18.5 ms, ON 6571.8 ms
- Repair types ON: `[]`
- Pipeline helper text matched entrypoint: `True`
- Field deltas: none
- Watch fields:
  - `aact_op`: OFF `{'field_id': 'aact_op', 'expected_raw': '69', 'predicted_raw': None, 'correct': False, 'error_type': 'MISSING', 'silent_wrong': False, 'match_mode': None}` / ON `{'field_id': 'aact_op', 'expected_raw': '69', 'predicted_raw': None, 'correct': False, 'error_type': 'MISSING', 'silent_wrong': False, 'match_mode': None}`
  - `aact_cl`: OFF `{'field_id': 'aact_cl', 'expected_raw': '69', 'predicted_raw': None, 'correct': False, 'error_type': 'MISSING', 'silent_wrong': False, 'match_mode': None}` / ON `{'field_id': 'aact_cl', 'expected_raw': '69', 'predicted_raw': None, 'correct': False, 'error_type': 'MISSING', 'silent_wrong': False, 'match_mode': None}`

## Claims

- This canary does **not** claim 100% accuracy.
- This canary does **not** score the 385-image `prerana` batch.
- Flag left **disabled** after the run (environment restored).

Artifacts: `C:/Users/amnsa/OneDrive/Desktop/zydus v1/splitextract/phase4/secondary_repair_canary`
