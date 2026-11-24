# Day 5 — Known Limitations

## Hardware / OCR

- RTX 3050 Laptop **6 GB** VRAM. One PaddleOCR-VL page uses nearly all VRAM (~5.9 GB observed on Day 4). Safe VL concurrency on this host is **1**.
- Full scanned/image extraction is dominated by VL OCR (hundreds of seconds). Digital Excel/Word/TXT/HTML/PDF-native paths are sub-second to ~2 s after warmup.
- Default sales extraction deadline is **600 s**. Day 5 scanned 1-page (276 s), PNG (109 s), and 2-page scanned (343 s) finished inside the deadline with Phase 2 Gemini disabled.

## Extraction quality

- Arbitrary document extraction is **not** 100% accurate. Safe `UNRESOLVED` is preferred over fabricated `VERIFIED`.
- Scanned PDF `0000700124` with DI override: **0 rows**, `insufficient_source_evidence` — unreadable headers remain unresolved; no invented stock quantities.
- Multi-page scanned PDF `0000700037` (2 pages, 0 native text): OCR ran sequentially; **0 rows**, `insufficient_source_evidence`. Page order probe (mocked) confirms pages 1→2→3; live mapping did not produce stock rows for this fixture.
- ClosStock PNG with DI override: 25 rows, `identity_profile_unknown`, verified false. Day 2 production ClosStock path without this audit’s secondary-repair flag previously reached different unresolved reasons; do not treat either run as verified quantities without grounding.
- Digital HTML and digital PDF medivision fixtures remain `UNRESOLVED` / `stock_identity_failure` when printed-zero closings fail identity — same Day 2 policy (do not coerce zeros).

## Gemini / recovery

- Day 5 end-to-end runs used `PHASE2_RECOVERY_ENABLED=false` to avoid live Vertex cost and 429 noise. Live Gemini latency was **not** substituted with mocks. Failure paths (429/503 type, retry bounds, cancellation) were tested separately.
- Enabling Phase 2 on this machine can add Gemini latency and provider 503/429 behavior under load.

## Storage

- PostgreSQL JSONB was proven only on a **disposable** Postgres 16 container in Day 3. This repository has **no** production application database integration. Do not claim production JSONB persistence.

## Concurrency

- Digital batch safe concurrency ≈ **2** (`MAX_CONCURRENT_EXTRACTIONS` default).
- API `MAX_CONCURRENT_REQUESTS` default remains **1**.
- Raising VL concurrency on 6 GB VRAM is unsafe.

## Secondary repair / DI override

- Production default `OCR_SECONDARY_REPAIR` is OFF. DI OCR override into core is active when secondary repair is enabled. Day 5 scanned/image audits explicitly enabled it to exercise the override path. Digital audits left it OFF.
