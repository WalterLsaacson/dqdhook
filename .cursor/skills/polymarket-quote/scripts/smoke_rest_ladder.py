#!/usr/bin/env python3
"""Smoke: pitch-gate rest uses $5 so 0.99 bids clear the 5-share CLOB floor."""

from __future__ import annotations

import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import rest_ladder as rl  # noqa: E402


def main() -> int:
    os.environ.pop("QUOTE_REST_USDC", None)
    assert abs(rl.rest_target_usdc() - 5.0) < 1e-9
    os.environ["QUOTE_REST_USDC"] = "7"
    assert abs(rl.rest_target_usdc() - 7.0) < 1e-9
    os.environ.pop("QUOTE_REST_USDC", None)

    # $1 @ 0.99 with a 5-share floor must not emit an undersized bid.
    tiny = rl.allocate_rest_ladder(
        1.0,
        prices=(0.99,),
        tick_size="0.01",
        floor_usdc=1.0,
        best_bid=0.99,
        best_ask=None,
        min_shares=5.0,
    )
    assert tiny == [], tiny

    levels = rl.allocate_rest_ladder(
        rl.rest_target_usdc(),
        prices=(0.99,),
        tick_size="0.01",
        floor_usdc=1.0,
        best_bid=0.99,
        best_ask=None,
        min_shares=5.0,
    )
    assert len(levels) == 1, levels
    assert abs(float(levels[0]["price"]) - 0.99) < 1e-9
    assert float(levels[0]["shares"]) + 1e-12 >= 5.0, levels[0]
    os.environ.pop("QUOTE_REST_EXPIRE_S", None)
    assert abs(rl.rest_expire_s() - 0.0) < 1e-9
    os.environ["QUOTE_REST_EXPIRE_S"] = "3600"
    assert abs(rl.rest_expire_s() - 3600.0) < 1e-9
    os.environ.pop("QUOTE_REST_EXPIRE_S", None)

    print("ok: rest ladder $5 / 5-share floor / GTC default")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
