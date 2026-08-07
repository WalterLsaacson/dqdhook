#!/usr/bin/env python3
"""Smoke: binary ask usdc (>=0.98→1, else 2) + open cap."""

from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from size_policy import (  # noqa: E402
    DEFAULT_SIZE_TIERS,
    compute_buy_size_caps,
    parse_size_tiers,
    scale_max_shares,
    tier_max_usdc,
)

TIERS = parse_size_tiers("0.98:1")


def main() -> int:
    assert TIERS == [(0.98, 1.0)]
    assert list(DEFAULT_SIZE_TIERS) == [(0.98, 1.0)]

    assert tier_max_usdc(0.97, TIERS, fallback=2) == 2
    assert tier_max_usdc(0.979, TIERS, fallback=2) == 2
    assert tier_max_usdc(0.98, TIERS, fallback=2) == 1
    assert tier_max_usdc(0.99, TIERS, fallback=2) == 1
    assert tier_max_usdc(0.995, TIERS, fallback=2) == 1
    # hard cap still wins
    assert tier_max_usdc(0.97, TIERS, fallback=1.5) == 1.5
    assert tier_max_usdc(0.99, TIERS, fallback=0.8) == 0.8

    sh2 = scale_max_shares(max_shares=25, max_usdc_cap=2, eff_usdc=2, ask=0.97)
    assert abs(sh2 - min(25.0, 2 / 0.97)) < 1e-6

    caps_lo = compute_buy_size_caps(
        0.97,
        max_usdc=2,
        max_shares=25,
        tiers=TIERS,
        open_usdc=0,
        max_open_usdc=1000,
        floor_usdc=1,
    )
    assert caps_lo.skip_reason is None
    assert abs(caps_lo.max_usdc - 2) < 1e-9
    assert abs(caps_lo.max_shares - sh2) < 1e-6

    sh1 = scale_max_shares(max_shares=25, max_usdc_cap=2, eff_usdc=1, ask=0.98)
    caps_hi = compute_buy_size_caps(
        0.98,
        max_usdc=2,
        max_shares=25,
        tiers=TIERS,
        open_usdc=0,
        max_open_usdc=1000,
        floor_usdc=1,
    )
    assert caps_hi.skip_reason is None
    assert abs(caps_hi.max_usdc - 1) < 1e-9
    assert abs(caps_hi.max_shares - sh1) < 1e-6

    # open budget still clamps when remaining is small
    capped = compute_buy_size_caps(
        0.97,
        max_usdc=2,
        max_shares=25,
        tiers=TIERS,
        open_usdc=999,
        max_open_usdc=1000,
        floor_usdc=1,
    )
    assert abs(capped.max_usdc - 1) < 1e-9

    skip = compute_buy_size_caps(
        0.96,
        max_usdc=2,
        max_shares=25,
        tiers=TIERS,
        open_usdc=1000,
        max_open_usdc=1000,
        floor_usdc=1,
    )
    assert skip.skip_reason == "size_policy_open_budget"
    assert skip.max_usdc == 0 and skip.max_shares == 0

    print("ok: binary size (>=0.98→1 else 2) + open cap")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
