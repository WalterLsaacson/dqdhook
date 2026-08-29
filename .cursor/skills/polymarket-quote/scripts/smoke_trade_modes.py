#!/usr/bin/env python3
"""Smoke: resolve goals/ft dry|live modes and per-signal live lookup."""

from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from fill_planner import FillPlan  # noqa: E402
from trade_executor import (  # noqa: E402
    TradeExecutor,
    apply_ft_dust_fak_plan,
    clip_ft_dust_usdc,
    signal_from_event_key,
)
from trade_settings import resolve_live_modes, TradeSettings  # noqa: E402


def _settings(*, goals: bool, ft: bool) -> TradeSettings:
    return TradeSettings(
        private_key="",
        funder=None,
        signature_type=2,
        chain_id=137,
        clob_host="https://clob.polymarket.com",
        data_api_url="https://data-api.polymarket.com",
        live_goals=goals,
        live_ft=ft,
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
    assert resolve_live_modes(live=False) == (False, False)
    assert resolve_live_modes(live=True) == (True, True)
    assert resolve_live_modes(live=True, goals_mode="dry", ft_mode="live") == (
        False,
        True,
    )
    assert resolve_live_modes(live=False, goals_mode="live", ft_mode="dry") == (
        True,
        False,
    )
    assert resolve_live_modes(live=True, goals_mode="dry") == (False, True)
    assert signal_from_event_key("score_change|1|…") == "score_change"
    assert signal_from_event_key("match_finished|1|t") == "match_finished"

    # Executor live-for-signal (no data dir side effects beyond tmp)
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "data" / "pm-quote").mkdir(parents=True)
        # patch data_dir via symlink-ish: TradeExecutor uses quote_lib.data_dir
        # which is root/data/pm-quote when root is repo-like. Use empty root +
        # monkey by writing under expected path.
        ex = TradeExecutor(root, _settings(goals=False, ft=True))
        assert ex._live_for_signal("score_change") is False
        assert ex._live_for_signal("match_finished") is True
        assert ex.settings.live is True

        ex2 = TradeExecutor(root, _settings(goals=True, ft=False))
        assert ex2._live_for_signal("score_change") is True
        assert ex2._live_for_signal("match_finished") is False

    # CLI flag resolution mirrors pm_quote (goals_mode / ft_mode / live)
    from trade_settings import load_trade_settings

    s = load_trade_settings(
        live=True,
        goals_mode="dry",
        ft_mode="live",
        require_key=False,
        env_file="/dev/null",
    )
    assert s.live_goals is False and s.live_ft is True and s.live is True
    assert s.min_buy_price == 0.6

    s2 = load_trade_settings(
        live=False,
        goals_mode="live",
        ft_mode="dry",
        require_key=False,
        env_file="/dev/null",
    )
    assert s2.live_goals is True and s2.live_ft is False

    s3 = load_trade_settings(
        live=False,
        min_buy_price=0.9,
        require_key=False,
        env_file="/dev/null",
    )
    assert s3.min_buy_price == 0.9

    # argparse shape on pm_quote
    import pm_quote

    p = pm_quote.build_parser()
    ns = p.parse_args(
        ["watch", "--goals-mode", "dry", "--ft-mode", "live", "--no-upstream"]
    )
    assert ns.goals_mode == "dry" and ns.ft_mode == "live" and not ns.live
    assert ns.min_buy_price == 0.6
    ns2 = p.parse_args(["watch", "--live", "--no-upstream"])
    assert ns2.live and ns2.goals_mode is None and ns2.ft_mode is None
    ns3 = p.parse_args(["watch", "--min-buy-price", "0.75", "--no-upstream"])
    assert ns3.min_buy_price == 0.75

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "data" / "pm-quote").mkdir(parents=True)
        ex = TradeExecutor(root, _settings(goals=False, ft=True))
        from dataclasses import replace

        ex.settings = replace(ex.settings, min_buy_price=0.6)
        # FT skips the ask floor (same as pitch-gate).
        assert (
            ex._min_buy_price_blocked(0.05, event_key="match_finished|m1|1-0")
            is None
        )
        assert (
            ex._min_buy_price_blocked(0.59, event_type="match_finished") is None
        )
        # leftover non-FT / non-gate path still honors the floor
        assert ex._min_buy_price_blocked(0.59) is not None
        assert ex._min_buy_price_blocked(0.6) is None
        assert "buy_price_below_min" in (ex._min_buy_price_blocked(0.5) or "")

        win = {"settlement": "WIN", "locked": True, "trade": "buy_win"}
        assert (
            ex._extreme_price_blocked(
                0.001,
                quote=win,
                trade="buy_win",
                event_type="match_finished",
            )
            is None
        )
        assert (
            ex._extreme_price_blocked(
                0.01,
                quote=win,
                trade="buy_win",
                event_type="match_finished",
            )
            is None
        )
        # Goals / pitch-gate still skip dust asks.
        assert ex._extreme_price_blocked(
            0.001, quote=win, trade="buy_win", event_type="score_change"
        )
        assert ex._extreme_price_blocked(
            0.001,
            quote=win,
            trade="buy_win",
            event_type="match_finished",
            match_meta={"trade_context": {"pitch_gate": True}},
        )
        assert ex._extreme_price_blocked(0.001, quote=win, trade="buy_win")
        assert ex._extreme_price_blocked(
            0.996, quote=win, trade="buy_win", event_type="match_finished"
        )
        dust = apply_ft_dust_fak_plan(
            FillPlan(
                trade="buy_win",
                side="BUY",
                take_depth="walk",
                order_type="FAK",
                shares=2000.0,
                usdc=2.0,
                worst_price=0.001,
                levels_used=1,
                levels=[{"price": 0.001, "size": 136000}],
            ),
            max_usdc=300.0,
        )
        assert dust.take_depth == "dust_fak"
        assert abs(dust.usdc - 300.0) < 1e-9
        assert abs(dust.worst_price - 0.01) < 1e-12
        assert dust.shares > 2000.0
        assert abs(clip_ft_dust_usdc(dust_cap=100.0, remaining_open=100000) - 100.0) < 1e-9
        assert abs(clip_ft_dust_usdc(dust_cap=100.0, remaining_open=40.0) - 40.0) < 1e-9
        assert abs(clip_ft_dust_usdc(dust_cap=100.0, remaining_open=None) - 100.0) < 1e-9
        ex.settings = replace(ex.settings, ft_dust_usdc=0.0)
        assert ex._extreme_price_blocked(
            0.001, quote=win, trade="buy_win", event_type="match_finished"
        )
        ex.settings = replace(ex.settings, ft_dust_usdc=100.0)
        hundred = apply_ft_dust_fak_plan(dust, max_usdc=100.0)
        assert abs(hundred.usdc - 100.0) < 1e-9

    s_sz = TradeSettings(
        private_key="",
        funder=None,
        signature_type=2,
        chain_id=137,
        clob_host="https://clob.polymarket.com",
        data_api_url="https://data-api.polymarket.com",
        live_goals=True,
        live_ft=True,
        take_depth="top",
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
        ft_max_usdc=300.0,
        goal_max_shares=150.0,
        ft_max_shares=2000.0,
        goal_size_tiers=((0.98, 50.0),),
        ft_size_tiers=((0.98, 300.0),),
    )
    g_u, g_sh, g_t = s_sz.caps_for_buy(event_type="score_change", pitch_gate=True)
    f_u, f_sh, f_t = s_sz.caps_for_buy(event_type="match_finished")
    assert g_u == 50.0 and g_t == ((0.98, 50.0),), (g_u, g_t)
    assert f_u == 300.0 and f_t == ((0.98, 300.0),), (f_u, f_t)
    assert f_sh == 2000.0 and g_sh == 150.0
    assert abs(s_sz.ft_dust_usdc - 100.0) < 1e-9

    print("ok: split goals/ft trade modes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
