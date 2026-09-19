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

    assert abs(rl.clip_rest_to_balance(100.0, 40.0) - 40.0) < 1e-9
    assert abs(rl.clip_rest_to_balance(100.0, 150.0) - 100.0) < 1e-9
    assert abs(rl.clip_rest_to_balance(100.0, None) - 100.0) < 1e-9
    assert abs(rl.clip_rest_to_balance(100.0, 0.0) - 0.0) < 1e-9
    assert abs(rl.rest_place_usdc_for_wallet(100.0, available=40.0) - 40.0) < 1e-9
    assert (
        abs(
            rl.rest_place_usdc_for_wallet(
                100.0, available=10.0, working=40.0, replace=True
            )
            - 50.0
        )
        < 1e-9
    )

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
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
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

            # Dead 0.001 ask: do not rest @0.99 (Lincoln 1H).
            q_dead = dict(q_no)
            q_dead["token_id"] = "tok_dead"
            q_dead["best_bid"] = None
            q_dead["best_ask"] = 0.001
            q_dead["asks_top"] = [{"price": "0.001", "size": "50"}]
            dead = ex.maybe_trade(
                q_dead, event_key=ek, match_meta=meta, event_type="score_change"
            )
            assert dead is None or dead.get("status") == "skipped", dead

            # Ask 0.20 < gate floor 0.30: skip FAK and do not rest @0.99.
            q_cheap = dict(q_no)
            q_cheap["token_id"] = "tok_cheap20"
            q_cheap["best_ask"] = 0.20
            q_cheap["best_ask_size"] = 50.0
            q_cheap["asks_top"] = [{"price": "0.20", "size": "50"}]
            q_cheap["misprice"] = True
            cheap = ex.maybe_trade(
                q_cheap, event_key=ek, match_meta=meta, event_type="score_change"
            )
            assert cheap is not None and cheap.get("status") == "skipped", cheap
            assert "buy_price_below_min" in str(cheap.get("skip_reason") or ""), cheap
            assert cheap.get("rest") is None, cheap
            assert ex._skip_rest_after_prepare(cheap)

            q_floor = dict(q_cheap)
            q_floor["token_id"] = "tok_floor30"
            q_floor["best_ask"] = 0.30
            q_floor["asks_top"] = [{"price": "0.30", "size": "50"}]
            q_floor["misprice"] = False
            at_floor = ex.maybe_trade(
                q_floor, event_key=ek, match_meta=meta, event_type="score_change"
            )
            assert at_floor and at_floor.get("status") == "rest_dry_run", at_floor

            orig_avail = ex._available_rest_usdc
            os.environ["QUOTE_REST_USDC"] = "20"
            try:
                ex._available_rest_usdc = lambda *, live=False: 8.0
                q_wallet = dict(q_no)
                q_wallet["token_id"] = "tok_wallet8"
                clipped = ex.maybe_trade(
                    q_wallet, event_key=ek, match_meta=meta, event_type="score_change"
                )
                assert clipped and clipped.get("status") == "rest_dry_run", clipped
                wlvls = (clipped.get("plan") or {}).get("levels") or []
                assert wlvls and abs(float(wlvls[0]["usdc"]) - 8.0) < 0.05, wlvls
                ex._available_rest_usdc = lambda *, live=False: 50.0
                q_full = dict(q_no)
                q_full["token_id"] = "tok_wallet50"
                full = ex.maybe_trade(
                    q_full, event_key=ek, match_meta=meta, event_type="score_change"
                )
                assert full and full.get("status") == "rest_dry_run", full
                flvls = (full.get("plan") or {}).get("levels") or []
                assert flvls and abs(float(flvls[0]["usdc"]) - 20.0) < 0.05, flvls
                ex._available_rest_usdc = lambda *, live=False: 2.0
                q_broke = dict(q_no)
                q_broke["token_id"] = "tok_wallet2"
                broke = ex.maybe_trade(
                    q_broke, event_key=ek, match_meta=meta, event_type="score_change"
                )
                assert broke is None, broke
            finally:
                ex._available_rest_usdc = orig_avail
                os.environ.pop("QUOTE_REST_USDC", None)

            os.environ["QUOTE_T10"] = "1"
            os.environ["QUOTE_T10_USDC"] = "15"
            t10_meta = {
                "match_id": "m1",
                "home": "H",
                "away": "A",
                "home_score": 1,
                "away_score": 2,
                "trade_context": {"pitch_gate": True, "t10": True},
            }
            q_dead_t10 = dict(q_dead)
            q_dead_t10["token_id"] = "tok_dead_t10"
            q_dead_t10["misprice"] = True
            dead_t10 = ex.maybe_trade(
                q_dead_t10,
                event_key="t10|" + ek,
                match_meta=t10_meta,
                event_type="score_change",
            )
            assert dead_t10 is None or str(dead_t10.get("skip_reason") or "").startswith(
                "extreme_price"
            ), dead_t10
            os.environ.pop("QUOTE_T10_USDC", None)
            os.environ.pop("QUOTE_T10", None)
            del ex
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

    print(
        "ok: rest ladder $5 / 0.01→0.99 / metadata 0.001 clamped to 0.01 / "
        "GTC default / gate ask<0.30 skip / rest clips to wallet USDC"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
