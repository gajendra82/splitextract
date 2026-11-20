"""tests package — install offline Gemini guard on import (package-wide)."""

from __future__ import annotations

try:
    from tests.gemini_offline import bootstrap_gemini_offline_guard

    bootstrap_gemini_offline_guard()
except Exception:
    # Never block collection if the guard fails to import.
    pass
