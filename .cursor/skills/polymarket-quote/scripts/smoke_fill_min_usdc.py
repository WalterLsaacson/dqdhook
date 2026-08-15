#!/usr/bin/env python3
"""Smoke: thin asks still post ≥$1 FAK so resting size is eaten."""

from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from fill_planner import plan_buy_win  # noqa: E402


def _q(asks: list[dict], *, ask: float | None = None) -> dict:
    best = ask if ask is not None else float(asks[0]["price"])
    return {
        "trade": "buy_win",
        "best_ask": best,
        "best_ask_size": float(asks[0]["size"]),
        "asks_top": asks,
    }


def main() -> int:
    # Thin top only (0.99@$0.99 ≈ $0.98): still order $1 FAK to eat it.
    thin = plan_buy_win(
        _q([{"price": 0.99, "size": 0.99}]),
        take_depth="walk",
        max_levels=5,
        max_usdc=3.0,
        max_shares=25.0,
        max_slippage=0.03,
    )
    assert thin.skip_reason is None, thin
    assert abs(thin.usdc - 1.0) < 1e-9, thin
    assert thin.levels and thin.levels[0]["size"] == 0.99

    # Top size=1 @ 0.99 ($0.99) + next level also ok; order amount ≥ $1.
    walked = plan_buy_win(
        _q(
            [
                {"price": 0.99, "size": 1.0},
                {"price": 1.0, "size": 10.0},
            ]
        ),
        take_depth="walk",
        max_levels=1,
        max_usdc=3.0,
        max_shares=25.0,
        max_slippage=0.03,
    )
    assert walked.skip_reason is None, walked
    assert walked.usdc + 1e-12 >= 1.0, walked

    # Deep top level still plans a full $1 budget.
    deep = plan_buy_win(
        _q([{"price": 0.99, "size": 300.0}]),
        take_depth="walk",
        max_usdc=1.0,
        max_shares=25.0,
    )
    assert deep.skip_reason is None and abs(deep.usdc - 1.0) < 1e-9, deep

    print("ok: thin book still posts $1 FAK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
