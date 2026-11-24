# PaddleOCR-VL silent-wrong gate (GT-20)

- Documents: **20**
- Verified fields: **388**

## Before gate (raw OCR acceptance)
- Field accuracy: **99.2%**
- Silent-wrong rate: **0.0%** (0/388)
- Error types: `{'MISSING': 3}`

## After production gate (prefer unresolved over silent wrong)
- Field accuracy: **99.2%** (correctness unchanged)
- Silent-wrong rate (verified denom): **0.0%** (0/388)
- Accepted-field silent-wrong rate: **0.0%** (0/385)
- Explicit unresolved rate: **0.0%**
- Gated former silent-wrong: **0**
- Remaining silent-wrong: **0**

This does not claim 100% extraction accuracy. It measures how many incorrect accepted values the geometry/cell gate would refuse.
