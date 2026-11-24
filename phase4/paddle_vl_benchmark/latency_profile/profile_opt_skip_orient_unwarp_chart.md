# PaddleOCR-VL latency profile `opt_skip_orient_unwarp_chart`

- Primary: `paddleocr-vl-1.6` / `PaddleOCR-VL-1.6`
- Backend: `subprocess` max_side=`2048`
- GPU idle: `{'util_pct': 0.0, 'mem_used_mib': 0.0, 'mem_total_mib': 6144.0, 'raw': '0, 0, 6144'}`

| doc | label | VL ms | Tess ms | ratio | VL chars | Tess chars |
|---|---|---:|---:|---:|---:|---:|
| `0000720756_2026_08_ZA_24_291_07092026162` | cold_first | 126396 | 524 | 241.43 | 5750 | 128 |
| `0000721924_2026_08_ZA_12_350_03092026163` | warm | 150343 | 2094 | 71.8 | 6316 | 2226 |
| `0000736984_2026_08_ZA_22_594_05092026033` | warm | 163862 | 2150 | 76.2 | 7978 | 2205 |
| `0000729726_2026_08_ZA_12_4345_0309202614` | warm | 121507 | 4895 | 24.82 | 4318 | 1103 |
| `0000718964_2026_08_ZA_24_255_07092026081` | warm | 133202 | 10110 | 13.18 | 7071 | 1146 |
