#!/usr/bin/env python3
"""Smoke: price-tiered buy size (usdc + shares scaled together)."""

from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from size_policy import (  # noqa: E402
    compute_buy_size_caps,
    parse_size_tiers,
    scale_max_shares,
    tier_max_usdc,
)

TIERS = parse_size_tiers("0.93:20,0.95:15,0.96:10,0.97:7,0.98:4,0.99:2,1.01:1")


def main() -> int:
    assert tier_max_usdc(0.92, TIERS, fallback=20) == 20
    assert tier_max_usdc(0.93, TIERS, fallback=20) == 20
    assert tier_max_usdc(0.94, TIERS, fallback=20) == 15
    assert tier_max_usdc(0.965, TIERS, fallback=20) == 7
    assert tier_max_usdc(0.97, TIERS, fallback=20) == 7
    assert tier_max_usdc(0.975, TIERS, fallback=20) == 4
    assert tier_max_usdc(0.99, TIERS, fallback=20) == 2
    assert tier_max_usdc(0.995, TIERS, fallback=20) == 1  # still buys

    # usdc + shares move together
    sh = scale_max_shares(max_shares=25, max_usdc_cap=20, eff_usdc=7, ask=0.97)
    assert abs(sh - min(25 * 7 / 20, 7 / 0.97)) < 1e-6
    assert sh < 25 and sh > 0

    caps = compute_buy_size_caps(
        0.97,
        max_usdc=20,
        max_shares=25,
        tiers=TIERS,
        open_usdc=0,
        max_open_usdc=45,
        floor_usdc=1,
    )
    assert caps.skip_reason is None
    assert abs(caps.max_usdc - 7) < 1e-9
    assert abs(caps.max_shares - sh) < 1e-6

    # open budget clamps both
    capped = compute_buy_size_caps(
        0.93,
        max_usdc=20,
        max_shares=25,
        tiers=TIERS,
        open_usdc=40,
        max_open_usdc=45,
        floor_usdc=1,
    )
    assert abs(capped.max_usdc - 5) < 1e-9
    assert capped.max_shares <= 5 / 0.93 + 1e-6

    # exhausted open budget
    skip = compute_buy_size_caps(
        0.96,
        max_usdc=20,
        max_shares=25,
        tiers=TIERS,
        open_usdc=45,
        max_open_usdc=45,
        floor_usdc=1,
    )
    assert skip.skip_reason == "size_policy_open_budget"
    assert skip.max_usdc == 0 and skip.max_shares == 0

    print("ok: size_policy tiers + paired usdc/shares + open cap")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
