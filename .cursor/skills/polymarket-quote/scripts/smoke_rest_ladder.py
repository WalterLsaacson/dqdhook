#!/usr/bin/env python3
"""Smoke: pitch-gate rest uses $5 so 0.995 bids clear the 5-share CLOB floor."""

from __future__ import annotations

import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import rest_ladder as rl  # noqa: E402
from trade_executor import TradeExecutor, _rest_remote_filled_shares  # noqa: E402
from trade_settings import TradeSettings  # noqa: E402


def _settings() -> TradeSettings:
    return TradeSettings(
        private_key="",
        funder=None,
        signature_type=2,
        chain_id=137,
        clob_host="https://clob.polymarket.com",
        data_api_url="https://data-api.polymarket.com",
        live_goals=False,
        live_ft=False,
        take_depth="top",
        max_levels=5,
        max_usdc=20.0,
        max_shares=25.0,
        max_slippage=0.03,
        allow_extreme_prices=False,
        min_buy_price=0.0,
        min_order_shares=0.0,
        enabled=True,
        size_tiers=((0.98, 2.0),),
        max_open_usdc=1000.0,
        size_floor_usdc=1.0,
    )


def main() -> int:
    os.environ.pop("QUOTE_REST_USDC", None)
    assert abs(rl.rest_target_usdc() - 5.0) < 1e-9
    os.environ["QUOTE_REST_USDC"] = "7"
    assert abs(rl.rest_target_usdc() - 7.0) < 1e-9
    os.environ.pop("QUOTE_REST_USDC", None)

    assert abs(rl.FAK_ZONE_MAX_ASK - 0.995) < 1e-12
    assert abs(rl.REST_CONCENTRATE_BID - 0.995) < 1e-12

    # $1 @ 0.995 with a 5-share floor must not emit an undersized bid.
    tiny = rl.allocate_rest_ladder(
        1.0,
        prices=(0.995,),
        tick_size="0.01",
        floor_usdc=1.0,
        best_bid=0.995,
        best_ask=None,
        min_shares=5.0,
    )
    assert tiny == [], tiny

    levels = rl.allocate_rest_ladder(
        rl.rest_target_usdc(),
        prices=(0.995,),
        tick_size="0.01",
        floor_usdc=1.0,
        best_bid=0.995,
        best_ask=None,
        min_shares=5.0,
    )
    assert len(levels) == 1, levels
    # 0.01 books cannot represent 0.995; floor to 0.99, post with book tick.
    assert abs(float(levels[0]["price"]) - 0.99) < 1e-9, levels[0]
    assert levels[0].get("tick_size") == "0.01", levels[0]
    assert float(levels[0]["shares"]) + 1e-12 >= 5.0, levels[0]

    # Metadata 0.001 is a lie on soccer CLOB — clamp to 0.01 and snap 0.995→0.99.
    fine = rl.allocate_rest_ladder(
        rl.rest_target_usdc(),
        prices=(0.995,),
        tick_size="0.001",
        floor_usdc=1.0,
        best_bid=0.995,
        best_ask=None,
        min_shares=5.0,
    )
    assert len(fine) == 1, fine
    assert abs(float(fine[0]["price"]) - 0.99) < 1e-9, fine[0]
    assert fine[0].get("tick_size") == "0.01", fine[0]

    resolved_coarse = rl.resolve_rest_price(0.995, "0.01")
    assert resolved_coarse is not None and abs(resolved_coarse[0] - 0.99) < 1e-9
    assert resolved_coarse[1] == "0.01"
    resolved_fine = rl.resolve_rest_price(0.995, "0.001")
    assert resolved_fine is not None and abs(resolved_fine[0] - 0.99) < 1e-9
    assert resolved_fine[1] == "0.01"
    assert rl.rest_limit_tick_size("0.001") == "0.01"

    os.environ.pop("QUOTE_REST_EXPIRE_S", None)
    assert abs(rl.rest_expire_s() - 0.0) < 1e-9
    os.environ["QUOTE_REST_EXPIRE_S"] = "3600"
    assert abs(rl.rest_expire_s() - 3600.0) < 1e-9
    os.environ.pop("QUOTE_REST_EXPIRE_S", None)

    import tempfile

    os.environ["QUOTE_REST_ENABLED"] = "1"
    try:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "data" / "pm-quote").mkdir(parents=True)
            ex = TradeExecutor(root, _settings())
            meta = {
                "match_id": "m1",
                "home": "H",
                "away": "A",
                "home_score": 1,
                "away_score": 0,
                "trade_context": {"pitch_gate": True},
            }
            ek = "score_change|m1|0-0->1-0|2026-08-24T06:39:04+08:00"
            q_no = {
                "trade": "buy_win",
                "settlement": "WIN",
                "token_id": "tok1",
                "match_id": "m1",
                "market_key": "match_total_0.5_over",
                "family": "totals",
                "best_bid": 0.99,
                "best_bid_size": 3000,
                "best_ask": None,
                "asks_top": [],
                "tick_size": "0.01",
                "min_order_size": "5",
            }
            posted_empty = ex.maybe_trade(
                q_no, event_key=ek, match_meta=meta, event_type="score_change"
            )
            assert posted_empty and posted_empty.get("status") == "rest_dry_run", posted_empty
            plan = posted_empty.get("plan") or {}
            levels_out = plan.get("levels") or []
            assert levels_out and abs(float(levels_out[0]["price"]) - 0.99) < 1e-9, plan

            q_meta_fine = dict(q_no)
            q_meta_fine["token_id"] = "tok_meta_001"
            q_meta_fine["tick_size"] = "0.001"
            posted_fine = ex.maybe_trade(
                q_meta_fine, event_key=ek, match_meta=meta, event_type="score_change"
            )
            assert posted_fine and posted_fine.get("status") == "rest_dry_run", posted_fine
            fine_plan = posted_fine.get("plan") or {}
            fine_lvls = fine_plan.get("levels") or []
            assert fine_lvls and abs(float(fine_lvls[0]["price"]) - 0.99) < 1e-9, fine_plan

            q_stub = dict(q_no)
            q_stub["token_id"] = "tok2"
            q_stub["best_ask"] = 0.999
            q_stub["best_ask_size"] = 5.0
            q_stub["asks_top"] = [{"price": "0.999", "size": "5"}]
            posted = ex.maybe_trade(
                q_stub, event_key=ek, match_meta=meta, event_type="score_change"
            )
            assert posted and posted.get("status") == "rest_dry_run", posted
    finally:
        os.environ.pop("QUOTE_REST_ENABLED", None)

    # MATCHED with no size_matched must not invent the full rest size.
    assert (
        _rest_remote_filled_shares({"status": "MATCHED"}, {"shares": 101.01, "filled_shares": 0})
        == 0
    )
    assert (
        abs(
            _rest_remote_filled_shares(
                {"status": "MATCHED", "size_matched": "4.04"},
                {"shares": 101.01, "filled_shares": 0},
            )
            - 4.04
        )
        < 1e-9
    )

    print("ok: rest ladder $5 / 0.01→0.99 / metadata 0.001 clamped to 0.01 / GTC default")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
