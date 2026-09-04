#!/usr/bin/env python3
"""Smoke: post-FT leftover WIN sweep ranking + trade context (no network)."""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import postft_sweep as ps  # noqa: E402
from fill_planner import plan_locked_sweep  # noqa: E402
from trade_executor import (  # noqa: E402
    TradeExecutor,
    _trade_context_postft,
)
from trade_settings import TradeSettings  # noqa: E402


def _settings(**kw: object) -> TradeSettings:
    base = dict(
        private_key="",
        funder=None,
        signature_type=3,
        chain_id=137,
        clob_host="https://clob.polymarket.com",
        data_api_url="https://data-api.polymarket.com",
        live_goals=False,
        live_ft=True,
        take_depth="walk",
        max_levels=5,
        max_usdc=30.0,
        max_shares=150.0,
        max_slippage=0.03,
        allow_extreme_prices=False,
        min_buy_price=0.6,
        min_order_shares=0.0,
        enabled=True,
        size_tiers=((0.98, 50.0),),
        max_open_usdc=100000.0,
        size_floor_usdc=1.0,
        goal_max_usdc=30.0,
        ft_max_usdc=1000.0,
        postft_sweep=True,
        postft_sweep_usdc=1000.0,
    )
    base.update(kw)
    return TradeSettings(**base)  # type: ignore[arg-type]


def main() -> int:
    payload = {
        "results": [
            {
                "match": {"id": "m-hi", "home": "A", "away": "B"},
                "score": {"home": 2, "away": 0, "source": "apifootball"},
                "hits": [
                    {
                        "token_id": "tok_hi",
                        "best_ask": 0.994,
                        "tradeable_shares": 10,
                        "asks": [{"price": 0.994, "size": 10}, {"price": 0.999, "size": 30}],
                        "question": "O/U 1.5 Over",
                    }
                ],
            },
            {
                "match": {"id": "m-lo", "home": "C", "away": "D"},
                "score": {"home": 2, "away": 0, "source": "apifootball"},
                "hits": [
                    {
                        "token_id": "tok_lo",
                        "best_ask": 0.99,
                        "tradeable_shares": 5,
                        "asks": [{"price": 0.99, "size": 5}],
                        "question": "1H Under 0.5",
                    }
                ],
            },
            {
                "match": {"id": "m-wall", "home": "E", "away": "F"},
                "score": {"home": 1, "away": 0, "source": "apifootball"},
                "hits": [
                    {
                        "token_id": "tok_wall",
                        "best_ask": 0.999,
                        "tradeable_shares": 0,
                        "asks": [{"price": 0.999, "size": 33}],
                        "question": "O/U 0.5 Over",
                    }
                ],
            },
            {
                "match": {"id": "m-dqd", "home": "G", "away": "H"},
                "score": {"home": 3, "away": 0, "source": "dongqiudi"},
                "hits": [
                    {
                        "token_id": "tok_dqd",
                        "best_ask": 0.95,
                        "tradeable_shares": 20,
                        "asks": [{"price": 0.95, "size": 20}],
                        "question": "O/U 0.5 Over",
                    }
                ],
            },
        ]
    }
    hits = ps.collect_tradeable_hits(payload, max_ask=0.995)
    assert [h["token_id"] for h in hits] == ["tok_lo", "tok_hi"], hits
    assert not any(h["token_id"] == "tok_wall" for h in hits)
    assert not any(h["token_id"] == "tok_dqd" for h in hits)

    q = ps.hit_to_quote(hits[0])
    assert q["neg_risk"] is False
    assert q["trade"] == "buy_win"
    plan = plan_locked_sweep(q, max_usdc=1000.0)
    assert plan.skip_reason is None
    assert abs(plan.worst_price - 0.99) < 1e-12
    assert plan.levels_used == 1

    hi = ps.hit_to_quote(hits[1])
    plan_hi = plan_locked_sweep(hi, max_usdc=1000.0)
    assert plan_hi.skip_reason is None
    assert abs(plan_hi.worst_price - 0.994) < 1e-12

    meta = ps.match_meta_for_hit(hits[0], event_key="postft|x|tok_lo")
    assert _trade_context_postft(meta)

    import tempfile

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "data" / "pm-quote").mkdir(parents=True)
        ex = TradeExecutor(root, _settings())
        assert ex._live_for_signal("postft") is True
        assert ex._min_buy_price_blocked(0.99, match_meta=meta) is None
        assert ex._locked_sweep_eligible(
            q, trade="buy_win", match_meta=meta, event_type="postft"
        )
        dry = _settings(live_ft=False, live_goals=True)
        ex_dry = TradeExecutor(root, dry)
        assert ex_dry._live_for_signal("postft") is False
        off = _settings(postft_sweep=False)
        ex_off = TradeExecutor(root, off)
        assert not ex_off._locked_sweep_eligible(
            q, trade="buy_win", match_meta=meta, event_type="postft"
        )

    key = ps.sweep_event_key("20260830T050000Z", "abc")
    assert key.startswith("postft|")
    cmd = ps.scan_cli_cmd(hours=24, max_ask=0.995, out_path=Path("/tmp/latest.json"))
    assert cmd[0] == sys.executable
    assert "--hours" in cmd and "24" in cmd
    assert "--max-ask" in cmd and "0.995" in cmd
    assert "--json" in cmd
    assert "--require-af" in cmd
    print("smoke_postft_sweep: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
