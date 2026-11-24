# Phase 2 A/B Benchmark — GT-20 SOURCE_VERIFIED

Created: 2026-11-20T11:16:07.076120+00:00

## Availability
- Documents requested: 20
- Documents scored: 20
- Missing sources: none
- OCR mode: stored_vl_ocr_identical_inputs

## Measured accuracy (SOURCE_VERIFIED fields only)
- Field accuracy OFF: 0.5180412371134021
- Field accuracy ON: 0.5180412371134021
- Field accuracy delta (ON-OFF): 0.0
- Quantity accuracy OFF: 0.4652777777777778
- Quantity accuracy ON: 0.4652777777777778
- Quantity accuracy delta (ON-OFF): 0.0
- Missing values OFF: 128
- Missing values ON: 128

## Stock equations
- Docs with fewer failing rows ON: 0
- Docs with more failing rows ON: 0

## Healthy row integrity
- Docs where a Phase2-OFF healthy row changed under ON: 0

## Gemini usage
- Calls: 32
- Successful calls: 32
- Failed calls: 0
- Prompt tokens: 94910
- Output tokens: 84907
- Estimated cost USD: 0.043454
- Pricing note: estimate using flash-lite ballpark $0.1/1M in + $0.4/1M out
- Phase2 recovered docs: 1
- Phase2 not_needed docs: 6
- Phase2 unresolved docs: 13

## Investigation notes (this run)
- Prior unresolved cause: Gemini put correct printed qtys under `dynamic` (`Rcpt Qty`/`OpStk`/`Clstk`) while the prompt forced canonical fields null; merge ignored those dynamic headers. See `REJECT_ROOT_CAUSE.md`.
- Fixes: `Rcpt` header matcher; promote dynamic→null fixed on merge; stock prompt fills canonical qtys for listed failing rows only. Healthy-row allowlist + provenance kept. Validation acceptance not weakened.
- `0000700084...` is now Phase-1 `not_needed` (receipts mapped correctly at parse). Remaining unresolved docs mostly still get incomplete Gemini fixed fields (null opening/receipts/closing) without usable dynamic qty headers — treated as genuine incomplete model output, not acceptance softening.
- OCR mode: identical stored VL text OFF/ON.

## Claims
- No SOURCE_VERIFIED accuracy improvement is claimed (measured deltas are 0.0).
- Phase 2 left disabled by default after the run (`PHASE2_RECOVERY_ENABLED=false`).


## Per-document summary

| document_id | field_acc_off | field_acc_on | qty_acc_off | qty_acc_on | gemini_calls_on | healthy_unchanged | phase2_status |
|---|---:|---:|---:|---:|---:|---|---|
| 0000700084_2026_08_ZL_06_8236_03092026155136.pdf | 0.5333333333333333 | 0.5333333333333333 | 0.5714285714285714 | 0.5714285714285714 | 0 | True | not_needed |
| 0000700124_2026_08_ZL_06_521_02092026204703.pdf | 0.08333333333333333 | 0.08333333333333333 | 0.0 | 0.0 | 2 | True | unresolved |
| 0000700209_2026_08_ZL_18_374_04092026060313.pdf | 0.6875 | 0.6875 | 0.6923076923076923 | 0.6923076923076923 | 2 | True | unresolved |
| 0000700275_2026_08_ZL_18_374_04092026171615.pdf | 0.6190476190476191 | 0.6190476190476191 | 0.5294117647058824 | 0.5294117647058824 | 2 | True | unresolved |
| 0000700308_2026_08_ZL_18_8175_03092026072320.jpg | 0.4444444444444444 | 0.4444444444444444 | 0.0 | 0.0 | 2 | True | unresolved |
| 0000700317_2026_08_ZL_18_374_02092026171443.pdf | 0.35 | 0.35 | 0.21428571428571427 | 0.21428571428571427 | 2 | True | unresolved |
| 0000700346_2026_08_ZL_18_8189_05092026025159.pdf | 0.16666666666666666 | 0.16666666666666666 | 0.0 | 0.0 | 2 | True | unresolved |
| 0000700358_2026_08_ZL_18_8173_05092026185826.pdf | 0.5714285714285714 | 0.5714285714285714 | 0.5333333333333333 | 0.5333333333333333 | 2 | True | unresolved |
| 0000700364_2026_08_ZL_18_8173_05092026190156.pdf | 0.9 | 0.9 | 0.875 | 0.875 | 0 | True | not_needed |
| 0000700406_2026_08_ZL_10_384_05092026140711.jpg | 0.11428571428571428 | 0.11428571428571428 | 0.0 | 0.0 | 2 | True | unresolved |
| 0000700434_2026_08_ZL_10_386_05092026034503.jpg | 0.7391304347826086 | 0.7391304347826086 | 0.6842105263157895 | 0.6842105263157895 | 2 | True | unresolved |
| 0000700835_2026_08_ZL_25_447_03092026105819.jpeg | 0.6896551724137931 | 0.6896551724137931 | 0.625 | 0.625 | 0 | True | not_needed |
| 0000701012_2026_08_ZL_11_423_03092026183736.jpg | 0.7142857142857143 | 0.7142857142857143 | 1.0 | 1.0 | 2 | True | unresolved |
| 0000701078_2026_08_ZL_03_362_05092026080346.jpeg | 0.5555555555555556 | 0.5555555555555556 | 0.4666666666666667 | 0.4666666666666667 | 2 | True | unresolved |
| 0000701151_2026_08_ZL_14_361_03092026025650.jpg | 0.8947368421052632 | 0.8947368421052632 | 0.8666666666666667 | 0.8666666666666667 | 0 | True | not_needed |
| 0000701275_2026_08_ZL_03_360_02092026152716.jpg | 0.16666666666666666 | 0.16666666666666666 | 0.0 | 0.0 | 2 | True | unresolved |
| 0000701851_2026_08_ZL_20_302_03092026134940.jpg | 0.9090909090909091 | 0.9090909090909091 | 0.9444444444444444 | 0.9444444444444444 | 0 | True | not_needed |
| 0000701864_2026_08_ZL_20_5015_03092026091037.jpg | 0.85 | 0.85 | 0.875 | 0.875 | 0 | True | not_needed |
| 0000701904_2026_08_ZL_20_8227_05092026154848.jpg | 0.15 | 0.15 | 0.0 | 0.0 | 2 | True | unresolved |
| 0000704337_2026_08_ZL_24_259_03092026042213.pdf | 0.16666666666666666 | 0.16666666666666666 | 0.0 | 0.0 | 2 | True | recovered |
