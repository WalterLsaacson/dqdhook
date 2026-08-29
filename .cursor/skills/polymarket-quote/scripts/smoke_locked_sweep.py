#!/usr/bin/env python3
"""Smoke: win-if-goal-void sweep (prev score still WIN → FAK remaining asks)."""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from fill_planner import LOCKED_SWEEP_DEPTH, plan_locked_sweep  # noqa: E402
from quote_lib import (  # noqa: E402
    prev_scores_if_goal_void,
    reversal_cushion_locked,
    token_is_win_at_score,
)
from trade_executor import clip_locked_sweep_usdc  # noqa: E402


def _over(*, side: str = "home", period: str = "ft", line: float = 0.5) -> dict:
    return {
        "family": "totals",
        "settlement": "WIN",
        "outcome": "Over",
        "market_key": f"{side}_total_{line}_over",
        "line": line,
        "total_side": side,
        "total_period": period,
    }


def _btts_yes(*, period: str = "ft") -> dict:
    return {
        "family": "btts",
        "settlement": "WIN",
        "outcome": "Yes",
        "market_key": "btts_yes",
        "btts_period": period,
    }


def _exact_no(scoreline: str) -> dict:
    sh, sa = (int(x) for x in scoreline.split("-", 1))
    return {
        "family": "exact_score",
        "settlement": "WIN",
        "outcome": "No",
        "market_key": f"exact_{scoreline}_no",
        "scoreline": scoreline,
        "exact_home": sh,
        "exact_away": sa,
    }


def main() -> int:
    # 1-0 → 1-1 home O/U 0.5: prev 1-0 still WIN (Malveira). Not reversal_cushion.
    home05 = _over(side="home", line=0.5)
    assert token_is_win_at_score(home05, home_score=1, away_score=0)
    assert not token_is_win_at_score(home05, home_score=0, away_score=0)
    assert not reversal_cushion_locked(
        {**home05, "goals": 1},
        home_score=1,
        away_score=1,
    )

    # Opening goal locks the over — do not sweep.
    assert not token_is_win_at_score(home05, home_score=0, away_score=0)

    match05 = _over(side="match", line=0.5)
    assert token_is_win_at_score(match05, home_score=1, away_score=0)
    assert not token_is_win_at_score(match05, home_score=0, away_score=0)
    # 1-0 → 2-0: already over before this goal.
    assert token_is_win_at_score(match05, home_score=1, away_score=0)

    away05 = _over(side="away", line=0.5)
    assert not token_is_win_at_score(away05, home_score=1, away_score=0)
    assert token_is_win_at_score(away05, home_score=1, away_score=1)

    # This goal created the lock (home 1.5 at 1-0 → 2-0).
    home15 = _over(side="home", line=1.5)
    assert not token_is_win_at_score(home15, home_score=1, away_score=0)
    assert token_is_win_at_score(home15, home_score=2, away_score=0)

    # BTTS Yes: 1-0 → 1-1 this goal created both-scored.
    assert not token_is_win_at_score(_btts_yes(), home_score=1, away_score=0)
    assert token_is_win_at_score(_btts_yes(), home_score=1, away_score=1)
    # 1-1 → 2-1 already both scored.
    assert token_is_win_at_score(_btts_yes(), home_score=1, away_score=1)

    # Exact No 0-0 already dead at 1-0; No 1-0 only dies on the 1-1 equalizer.
    assert token_is_win_at_score(_exact_no("0-0"), home_score=1, away_score=0)
    assert not token_is_win_at_score(_exact_no("1-0"), home_score=1, away_score=0)
    assert token_is_win_at_score(_exact_no("1-0"), home_score=1, away_score=1)

    prev = prev_scores_if_goal_void(
        {"prev": {"home": 1, "away": 0}, "period": "1H"},
        {"dongqiudi": {"period": "1H"}, "home_half": 1, "away_half": 1},
    )
    assert prev == {"home": 1, "away": 0, "home_half": 1, "away_half": 0}

    prev_2h = prev_scores_if_goal_void(
        {"prev": {"home": 1, "away": 0}},
        {"dongqiudi": {"period": "2H"}, "home_half": 1, "away_half": 0},
    )
    assert prev_2h is not None
    assert prev_2h["home"] == 1 and prev_2h["away"] == 0
    assert prev_2h["home_half"] == 1 and prev_2h["away_half"] == 0

    # Malveira-style remaining asks: 185.08@0.98 + 5@0.99, ignore $50.
    plan = plan_locked_sweep(
        {
            "best_ask": 0.98,
            "best_ask_size": 185.08,
            "asks_top": [
                {"price": 0.98, "size": 185.08},
                {"price": 0.99, "size": 5},
            ],
        },
        max_usdc=898.0,
    )
    want = 185.08 * 0.98 + 5.0 * 0.99
    assert plan.skip_reason is None, plan
    assert plan.take_depth == LOCKED_SWEEP_DEPTH
    assert abs(plan.usdc - want) < 1e-6
    assert abs(plan.worst_price - 0.99) < 1e-12
    assert plan.levels_used == 2
    assert abs(plan.shares - 190.08) < 1e-6

    # Do not walk through 0.996 (above FAK zone).
    capped = plan_locked_sweep(
        {
            "best_ask": 0.98,
            "best_ask_size": 10,
            "asks_top": [
                {"price": 0.98, "size": 10},
                {"price": 0.996, "size": 1000},
            ],
        },
        max_usdc=1000.0,
    )
    assert abs(capped.usdc - 9.8) < 1e-9
    assert abs(capped.worst_price - 0.98) < 1e-12

    # Open-budget clip: leftover 40 vs $1000 cap; 0 cap disables.
    assert abs(clip_locked_sweep_usdc(sweep_cap=0.0, remaining_open=40.0) - 0.0) < 1e-9
    assert abs(clip_locked_sweep_usdc(sweep_cap=200.0, remaining_open=898.0) - 200.0) < 1e-9
    assert abs(clip_locked_sweep_usdc(sweep_cap=1000.0, remaining_open=898.0) - 898.0) < 1e-9
    assert abs(clip_locked_sweep_usdc(sweep_cap=1000.0, remaining_open=None) - 1000.0) < 1e-9
    assert abs(clip_locked_sweep_usdc(sweep_cap=1000.0, remaining_open=50000.0) - 1000.0) < 1e-9

    # $50 goal cap would have stopped at ~51 shares; sweep takes the book.
    from fill_planner import plan_buy_win

    fifty = plan_buy_win(
        {
            "best_ask": 0.98,
            "best_ask_size": 185.08,
            "asks_top": [
                {"price": 0.98, "size": 185.08},
                {"price": 0.99, "size": 5},
            ],
        },
        take_depth="walk",
        max_levels=5,
        max_usdc=50.0,
        max_shares=150.0,
        max_slippage=0.03,
        min_order_usdc=0.0,
    )
    assert fifty.usdc <= 50.0 + 1e-9
    assert plan.usdc > fifty.usdc + 100.0

    from quote_lib import quote_tokens

    quoted = quote_tokens(
        [
            {
                **home05,
                "token_id": "tok-home-05",
                "goals": 2,
                "settlement": "WIN",
            }
        ],
        books={
            "tok-home-05": {
                "best_ask": 0.98,
                "best_ask_size": 185.08,
                "asks_top": [{"price": 0.98, "size": 185.08}],
            }
        },
        match_meta={
            "event_type": "score_change",
            "prev_home": 1,
            "prev_away": 0,
            "prev_home_half": 1,
            "prev_away_half": 0,
            "trade_context": {"pitch_gate": True},
        },
    )
    assert quoted and quoted[0]["win_if_goal_void"] is True

    opening = quote_tokens(
        [
            {
                **home05,
                "token_id": "tok-open",
                "goals": 1,
                "settlement": "WIN",
            }
        ],
        books={"tok-open": {"best_ask": 0.98, "asks_top": [{"price": 0.98, "size": 10}]}},
        match_meta={
            "event_type": "score_change",
            "prev_home": 0,
            "prev_away": 0,
            "trade_context": {"pitch_gate": True},
        },
    )
    assert opening and opening[0]["win_if_goal_void"] is False

    import tempfile
    from dataclasses import replace
    from trade_executor import TradeExecutor
    from trade_settings import TradeSettings

    settings = TradeSettings(
        private_key="",
        funder=None,
        signature_type=2,
        chain_id=137,
        clob_host="https://clob.polymarket.com",
        data_api_url="https://data-api.polymarket.com",
        live_goals=True,
        live_ft=True,
        take_depth="walk",
        max_levels=5,
        max_usdc=50.0,
        max_shares=150.0,
        max_slippage=0.03,
        allow_extreme_prices=False,
        min_buy_price=0.0,
        min_order_shares=0.0,
        enabled=True,
        size_tiers=((0.98, 50.0),),
        max_open_usdc=1000.0,
        size_floor_usdc=1.0,
        goal_max_usdc=50.0,
    )
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "data" / "pm-quote").mkdir(parents=True)
        ex = TradeExecutor(root, settings)
        q = {**quoted[0], "trade": "buy_win", "settlement": "WIN"}
        meta = {"event_type": "score_change", "trade_context": {"pitch_gate": True}}
        assert ex._locked_sweep_eligible(
            q, trade="buy_win", match_meta=meta, event_type="score_change"
        )
        q_open = {**opening[0], "trade": "buy_win", "settlement": "WIN"}
        assert not ex._locked_sweep_eligible(
            q_open, trade="buy_win", match_meta=meta, event_type="score_change"
        )
        assert not ex._locked_sweep_eligible(
            q, trade="buy_win", match_meta=None, event_type="score_change"
        )
        ex.settings = replace(ex.settings, locked_sweep=False)
        assert not ex._locked_sweep_eligible(
            q, trade="buy_win", match_meta=meta, event_type="score_change"
        )
        ex.settings = replace(ex.settings, locked_sweep=True, locked_sweep_usdc=0.0)
        assert not ex._locked_sweep_eligible(
            q, trade="buy_win", match_meta=meta, event_type="score_change"
        )
        ex.settings = replace(ex.settings, locked_sweep=True, locked_sweep_usdc=1000.0)

        from quote_lib import lot_still_win_at_score
        from score_reversal import iso_now

        home_lot = {
            "market_key": "home_total_0.5_over",
            "family": "totals",
            "token_id": "tok-home-05",
            "pitch_gate": True,
            "entry_score": [1, 1],
            "shares": 190.08,
            "usdc": 186.0,
            "opened_at": iso_now(),
        }
        away_lot = {
            "market_key": "away_total_0.5_over",
            "family": "totals",
            "token_id": "tok-away-05",
            "pitch_gate": True,
            "entry_score": [1, 1],
            "shares": 50.0,
            "usdc": 49.0,
            "opened_at": iso_now(),
        }
        assert lot_still_win_at_score(home_lot, home_score=1, away_score=0)
        assert not lot_still_win_at_score(away_lot, home_score=1, away_score=0)

        ex.ledger.register_buy(
            match_id="m-rev",
            token_id="tok-home-05",
            market_key="home_total_0.5_over",
            family="totals",
            shares=190.08,
            usdc=186.0,
            home_score=1,
            away_score=1,
            live=False,
            event_key="score_change|m-rev|1-1",
            pitch_gate=True,
        )
        ex.ledger.register_buy(
            match_id="m-rev",
            token_id="tok-away-05",
            market_key="away_total_0.5_over",
            family="totals",
            shares=50.0,
            usdc=49.0,
            home_score=1,
            away_score=1,
            live=False,
            event_key="score_change|m-rev|1-1",
            pitch_gate=True,
        )
        rev = {
            "type": "score_change",
            "is_reversal": True,
            "match_id": "m-rev",
            "prev": {"home": 1, "away": 1},
            "curr": {"home": 1, "away": 0},
            "home_score": 1,
            "away_score": 0,
        }
        rows = ex.maybe_flatten_for_event(rev, require_protect_window=False)
        flattened_tids = {str(r.get("token_id") or "") for r in rows}
        assert "tok-home-05" not in flattened_tids, rows
        assert "tok-away-05" in flattened_tids, rows
        open_tids = {str(r.get("token_id") or "") for r in ex.ledger.open_for_match("m-rev")}
        assert "tok-home-05" in open_tids
        assert "tok-away-05" not in open_tids

    print("ok: locked sweep (win if goal void)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
