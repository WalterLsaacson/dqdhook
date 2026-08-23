"""Shared grade sizing helpers for live test/prod profiles."""

from __future__ import annotations

import os

DEFAULT_GRADE_TARGET_USDC = {"C": 0.0, "B": 10.0, "A": 20.0}
TEST_GRADE_TARGET_USDC = {"C": 0.0, "B": 1.0, "A": 2.0}


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def test_sizing_enabled() -> bool:
    return _env_bool("QUOTE_TEST_SIZING", False)


def grade_target_usdc(level: str) -> float:
    key = str(level or "").strip().upper()
    table = TEST_GRADE_TARGET_USDC if test_sizing_enabled() else DEFAULT_GRADE_TARGET_USDC
    return float(table.get(key, 0.0))


def cushion_rest_usdc() -> float:
    return grade_target_usdc("B")
