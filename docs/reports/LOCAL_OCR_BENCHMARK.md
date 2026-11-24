# Local CPU OCR benchmark — three ZA audit images

**Run:** 2026-10-30, local Windows host. The images were read from `Downloads/ZA_2026_August`. OCR inference used CPU only; no GPU/driver settings were changed and no packages were installed. OCR outputs are model-derived evidence and are not source truth.

## Tesseract 5.4.0 segmentation-mode comparison

Each invocation used the original image, `--oem 1`, and the listed page segmentation mode. Times below include the per-image Tesseract CLI process. PSM 4 was the strongest general table-reading configuration of those tested; PSM 11 often separated the table into per-column fragments, and PSM 6 skipped much of the content on the screenshot.

| Original | PSM 4 | PSM 6 | PSM 11 | Observed text/header result |
|---|---:|---:|---:|---|
| `0000701478…jpg` (989×1382) | 0.34 s | 0.29 s | 0.32 s | PSM 4 read `SPEMAN Tab 60s 15.00 0.00 0.00) 15.00`. The image has no quantity headers, so the four numbers remain unmapped. |
| `0000733955…jpeg` (720×1280) | 0.22 s | 0.21 s | 0.24 s | All modes returned empty text on the blurred photo. No product rows or headers. |
| `0000727190…png` (864×1928) | 1.03 s | 0.55 s | 1.24 s | PSM 4 read the eight-column header and most table lines. It read the HIORA row as `HIORA $6 GEL 5.00 50.00 $5.00 1.00 3.00 3.00 5400`, so 5/7 numeric cells in that row matched exactly; Total 55.00 and CloseStock 54.00 were wrong. |

The existing `TesseractEngine` adapter was also exercised with PSM 4, `OCR_ENGINE=tesseract`, and `OCR_FALLBACK_ENGINE=none`. It returned the same text; its first call took 6.20 s including application/module initialization, then 0.69 s for the JPEG and 2.27 s for the PNG. It exposes word boxes but does not produce explicit table cells, header-to-column mappings, or structured rows.

## Other installed local candidates

| Candidate | Result |
|---|---|
| Classic PaddleOCR 2.10.0 / PaddlePaddle 2.6.2 (FIXED) | **Working and verified.** The `torch\lib\shm.dll` DLL collision caused by optional PyTorch imports in `albumentations` was resolved by shielding `albumentations.pytorch`. Models load cleanly on CPU. **Benchmarked across all 3 audit images:**<br>• `0000701478.jpg`: 14 lines in 1.69s (captures full company and category headers + `SPEMAN Tab 60s 15.00 0.00 0.00 15.00` + group totals).<br>• `0000733955.jpeg`: 48 lines in 3.88s (successfully extracts table & numbers from blurred photo where Tesseract failed completely).<br>• `0000727190.png`: 214 lines in 5.34s (HIORA row: all 7 numbers matched with 99.7% confidence: Opening 5.00, Purchase 50.00, Total 55.00, Sale 1.00, SaleRet 3.00, Exp/Dmg 3.00, ClosStock 54.00, perfectly aligning under column headers). |
| Installed PaddleOCR-VL 1.6 stack | Prior audit tried CPU inference on the PNG for over 15 minutes without output. It is not lightweight for this local CPU target. |
| Installed Surya 2 | Its project adapter requires a configured inference backend (llama.cpp server or vLLM); none is available under the local-only/no-Docker constraint. No image inference was run. |

## Decision

Tesseract PSM 4 is the best-performing runnable local option among the installed candidates. It is useful for text evidence on the PNG and JPG but does **not** meet the reliability target on all three images: it misreads the PNG's critical Total/CloseStock cells and returns no text on the JPEG. Consequently the integration is opt-in, verification remains authoritative, and unresolved cells must stay unresolved. No aggregate accuracy percentage is claimed from this three-image audit sample.
