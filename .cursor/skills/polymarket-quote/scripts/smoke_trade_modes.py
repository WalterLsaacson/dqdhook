#!/usr/bin/env python3
"""Smoke: resolve goals/ft dry|live modes and per-signal live lookup."""

from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from trade_executor import TradeExecutor, signal_from_event_key  # noqa: E402
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
        size_tiers=((0.93, 20.0), (0.95, 15.0), (0.96, 10.0), (0.97, 7.0), (0.98, 4.0), (0.99, 2.0), (1.01, 1.0)),
        max_open_usdc=45.0,
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
    assert s.min_buy_price == 0.0

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
    assert ns.min_buy_price == 0.0
    ns2 = p.parse_args(["watch", "--live", "--no-upstream"])
    assert ns2.live and ns2.goals_mode is None and ns2.ft_mode is None
    ns3 = p.parse_args(["watch", "--min-buy-price", "0.75", "--no-upstream"])
    assert ns3.min_buy_price == 0.75

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "data" / "pm-quote").mkdir(parents=True)
        # default floor off: any ask allowed
        ex = TradeExecutor(root, _settings(goals=False, ft=True))
        assert ex._min_buy_price_blocked(0.05) is None
        assert ex._min_buy_price_blocked(0.79) is None
        # explicit floor still works
        from dataclasses import replace

        ex.settings = replace(ex.settings, min_buy_price=0.8)
        assert ex._min_buy_price_blocked(0.79) is not None
        assert ex._min_buy_price_blocked(0.8) is None
        assert "buy_price_below_min" in (ex._min_buy_price_blocked(0.5) or "")

    print("ok: split goals/ft trade modes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
