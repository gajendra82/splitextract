"""Tests for geometry hybrid v3 blue-mark helpers."""

from __future__ import annotations

import numpy as np

from services.stock_geometry_hybrid_v3 import (
    _normalize_numeric,
    remove_blue_preserve_ink,
    select_orig_vs_clean,
)


def test_v3_normalize_no_invention():
    assert _normalize_numeric("p3") == (None, False, True)
    assert _normalize_numeric("60") == (60.0, False, False)
    assert _normalize_numeric("53") == (53.0, False, False)
    assert _normalize_numeric("") == (None, True, False)


def test_v3_blue_removal_preserves_dark_ink():
    # White bg, black digit stroke, bright cyan UI mark on background
    img = np.full((20, 40, 3), 255, dtype=np.uint8)
    img[8:16, 10:14] = (20, 20, 20)  # dark ink
    img[5:15, 30:36] = (255, 220, 60)  # bright BGR cyan/blue (high luminance)
    cleaned, n, stats = remove_blue_preserve_ink(img)
    assert n > 0
    # dark ink remains dark
    assert int(cleaned[10, 12].mean()) < 80
    # bright blue area becomes white-ish
    assert int(cleaned[10, 33].mean()) > 200


def test_v3_blue_on_ink_forced_black():
    img = np.full((20, 40, 3), 255, dtype=np.uint8)
    # dark bluish pixels simulating blue overlay on ink
    img[8:16, 10:18] = (120, 40, 20)  # B high, dark-ish
    cleaned, n, stats = remove_blue_preserve_ink(img)
    assert stats["dark"] >= 0
    # after preserve-ink, those pixels should be black or white, not blue
    b, g, r = cleaned[10, 12]
    assert not (int(b) > int(r) + 40 and int(b) > int(g) + 20)


def test_v3_select_orig_vs_clean_arjuna_patterns():
    # purchase: clean damaged 6→3
    v, reason = select_orig_vs_clean(60.0, 30.0)
    assert v == 60.0
    assert "prefer_orig" in reason
    # closing: blue overlay closed 5→6
    v, reason = select_orig_vs_clean(63.0, 53.0)
    assert v == 53.0
    assert "prefer_clean" in reason
    # agree
    v, reason = select_orig_vs_clean(26.0, 26.0)
    assert v == 26.0
    assert reason == "agree"
