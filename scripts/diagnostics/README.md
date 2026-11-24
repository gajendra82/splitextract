# Diagnostics probes

One-off OCR / secondary-repair investigation scripts moved out of the repository root.

Run from the repository root so `services` imports resolve:

```bash
python scripts/diagnostics/_verify_secondary_gt20.py
```

These are not part of the production API path.
