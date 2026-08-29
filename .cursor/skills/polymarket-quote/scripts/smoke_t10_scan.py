#!/usr/bin/env python3
"""Smoke: goal +10min rescan scheduler, caps, rest without QUOTE_REST_ENABLED."""

from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import t10_scan as t10  # noqa: E402
from trade_executor import TradeExecutor, _trade_context_t10  # noqa: E402
from trade_settings import TradeSettings  # noqa: E402


def _goal_ev(**kw: object) -> dict:
    ev = {
        "type": "score_change",
        "ts": "2026-08-30T02:00:00+08:00",
        "match_id": "m1",
        "home": "Home",
        "away": "Away",
        "home_score": 1,
        "away_score": 0,
        "is_goal": True,
        "prev": {"home": 0, "away": 0},
        "curr": {"home": 1, "away": 0},
        "polymarket": {"event_id": "e1", "slug": "home-vs-away"},
    }
    ev.update(kw)
    return ev


def _settings(*, t10_usdc: float = 15.0) -> TradeSettings:
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
        max_usdc=50.0,
        max_shares=150.0,
        max_slippage=0.03,
        allow_extreme_prices=False,
        min_buy_price=0.6,
        min_order_shares=0.0,
        enabled=True,
        size_tiers=((0.98, 50.0),),
        max_open_usdc=100000.0,
        size_floor_usdc=1.0,
        goal_max_usdc=50.0,
        ft_max_usdc=300.0,
        t10_usdc=t10_usdc,
    )


def main() -> int:
    t10.reset_scheduler_for_tests()
    saved = {k: os.environ.get(k) for k in (
        "QUOTE_T10",
        "QUOTE_T10_USDC",
        "QUOTE_T10_DELAY_S",
        "QUOTE_T10_MAX_LATE_S",
        "QUOTE_REST_ENABLED",
        "QUOTE_REST_USDC",
    )}
    try:
        return _run()
    finally:
        t10.reset_scheduler_for_tests()
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _run() -> int:
    t10.reset_scheduler_for_tests()
    os.environ["QUOTE_T10"] = "1"
    os.environ["QUOTE_T10_USDC"] = "15"
    os.environ["QUOTE_T10_DELAY_S"] = "0"
    os.environ["QUOTE_T10_MAX_LATE_S"] = "900"
    os.environ.pop("QUOTE_REST_ENABLED", None)
    os.environ.pop("QUOTE_REST_USDC", None)

    assert t10.t10_enabled()
    assert abs(t10.t10_usdc() - 15.0) < 1e-9
    src = "score_change|m1|0-0->1-0|2026-08-30T02:00:00+08:00"
    assert t10.t10_event_key(src) == f"t10|{src}"

    s = _settings(t10_usdc=15.0)
    u, sh, tiers = s.caps_for_buy(event_type="score_change", pitch_gate=True, t10=True)
    assert abs(u - 15.0) < 1e-9, (u, sh, tiers)
    assert abs(sh - 1500.0) < 1e-9, sh
    g_u, _, _ = s.caps_for_buy(event_type="score_change", pitch_gate=True)
    assert abs(g_u - 50.0) < 1e-9, g_u

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        root = Path(td)
        (root / "data" / "bridge").mkdir(parents=True)
        (root / "data" / "pm-quote").mkdir(parents=True)
        import quote_lib as lib

        lib.write_json(
            root / "data" / "bridge" / "prev_scores.json",
            {"m1": {"home": 2, "away": 1}},
        )
        sched = t10.get_scheduler(root)
        ev = _goal_ev()
        assert sched.schedule(ev, event_key=src)
        assert sched.schedule(ev, event_key=src) is False
        due = sched.pop_due()
        assert len(due) == 1, due
        work = t10.build_t10_work_event(root, due[0])
        assert work is not None
        assert work["home_score"] == 2 and work["away_score"] == 1
        assert "prev" not in work
        tc = work["_trade_context"]
        assert tc.get("t10") is True
        assert tc.get("pitch_gate") is True
        assert work["_trade_event_key"] == f"t10|{src}"

        n = sched.cancel_match("m1")
        assert n == 0
        assert sched.schedule(ev, event_key=src)
        assert sched.cancel_match("m1") == 1
        assert sched.pop_due() == []

        # Too late after due → drop, do not fire.
        t10.reset_scheduler_for_tests()
        os.environ["QUOTE_T10_DELAY_S"] = "1"
        os.environ["QUOTE_T10_MAX_LATE_S"] = "5"
        sched2 = t10.get_scheduler(root)
        past = time.time() - 30
        assert sched2.schedule(ev, event_key=src + "|late", now=past - 1)
        # due_ts = past-1+1 = past; now - due ~ 30 > 5
        stale = sched2.pop_due()
        assert stale == [], stale

    t10.reset_scheduler_for_tests()
    os.environ["QUOTE_T10_DELAY_S"] = "0"
    os.environ.pop("QUOTE_T10_USDC", None)
    assert not t10.t10_enabled()
    os.environ["QUOTE_T10_USDC"] = "15"

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        root = Path(td)
        (root / "data" / "pm-quote").mkdir(parents=True)
        ex = TradeExecutor(root, _settings(t10_usdc=15.0))
        meta = {
            "match_id": "m1",
            "home": "H",
            "away": "A",
            "home_score": 2,
            "away_score": 1,
            "event_type": "score_change",
            "trade_context": {
                "pitch_gate": True,
                "t10": True,
                "base_event_key": "t10|k1",
            },
        }
        assert _trade_context_t10(meta)
        assert not ex._locked_sweep_eligible(
            {
                "settlement": "WIN",
                "locked": True,
                "win_if_goal_void": True,
            },
            trade="buy_win",
            match_meta=meta,
            event_type="score_change",
        )
        q_no = {
            "trade": "buy_win",
            "settlement": "WIN",
            "token_id": "tok_t10",
            "match_id": "m1",
            "market_key": "match_total_0.5_over",
            "family": "totals",
            "best_bid": 0.99,
            "best_bid_size": 3000,
            "best_ask": None,
            "asks_top": [],
            "tick_size": "0.01",
            "min_order_size": "5",
            "misprice": False,
        }
        posted = ex.maybe_trade(
            q_no, event_key="t10|k1", match_meta=meta, event_type="score_change"
        )
        assert posted and posted.get("status") == "rest_dry_run", posted
        plan = posted.get("plan") or {}
        levels = plan.get("levels") or []
        assert levels and abs(float(levels[0]["price"]) - 0.99) < 1e-9, plan
        # Rest notional is QUOTE_T10_USDC, not QUOTE_REST_USDC ($5).
        assert float(levels[0]["usdc"]) + 1e-9 >= 14.0, levels[0]

    print("ok: t10 scan schedule / score overlay / caps / rest without REST_ENABLED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
