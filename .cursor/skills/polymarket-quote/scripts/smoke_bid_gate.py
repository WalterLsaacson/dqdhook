#!/usr/bin/env python3
"""Smoke: market bid gate poll / abort / trade hard-skip."""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path
from tempfile import TemporaryDirectory

_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from bid_gate import BidGate, pick_win_candidates  # noqa: E402
from trade_executor import TradeExecutor  # noqa: E402
from trade_settings import TradeSettings  # noqa: E402
import quote_lib as lib  # noqa: E402


def _books_factory(sequence: list[float]):
    """Return fetch_books_fn that walks bid sequence per call."""
    state = {"i": 0}

    def _fetch(token_ids, proxy=None):  # noqa: ANN001
        i = min(state["i"], len(sequence) - 1)
        bid = sequence[i]
        state["i"] += 1
        out = {}
        for tid in token_ids:
            out[str(tid)] = {
                "token_id": str(tid),
                "book_missing": False,
                "best_bid": bid,
                "best_ask": 0.98,
                "best_bid_size": 10.0,
                "best_ask_size": 10.0,
            }
        return out

    return _fetch, state


def _fake_discover(monkey_rows: list[dict]):
    def _join(_root, work_ev):  # noqa: ANN001
        return {
            "home": work_ev.get("home") or "H",
            "away": work_ev.get("away") or "A",
            "home_score": work_ev.get("home_score", 1),
            "away_score": work_ev.get("away_score", 0),
            "polymarket": {"event_id": "1", "slug": "x"},
            "dongqiudi": {"id": work_ev.get("match_id")},
            "event": work_ev,
        }

    def _collect(ctx, **_kw):  # noqa: ANN001
        return list(monkey_rows), {"mode": "live"}

    return _join, _collect


def main() -> int:
    rows = [
        {
            "token_id": "tok1",
            "settlement": "WIN",
            "market_key": "match_total_0.5_over",
            "family": "totals",
        }
    ]
    join_fn, collect_fn = _fake_discover(rows)
    orig_join = lib.join_ft_context
    orig_collect = lib.collect_target_tokens
    lib.join_ft_context = join_fn  # type: ignore[assignment]
    lib.collect_target_tokens = collect_fn  # type: ignore[assignment]
    try:
        # pick_win_candidates prefers misprice
        book_map = {
            "tok1": {
                "book_missing": False,
                "best_bid": 0.87,
                "best_ask": 0.98,
            }
        }
        cands = pick_win_candidates(
            rows, book_map, eps=0.005, fee_rate=0.05, min_net=0.0076
        )
        assert len(cands) == 1

        with TemporaryDirectory() as td:
            root = Path(td)

            # 1) t0 bid high → immediate pass
            fetch, _st = _books_factory([0.95])
            g = BidGate(
                root,
                min_bid=0.9,
                poll_s=0.05,
                timeout_s=1.0,
                fetch_books_fn=fetch,
            )
            out = g.await_bid(
                {"match_id": "m1", "home_score": 1, "away_score": 0, "type": "score_change"},
                abort=threading.Event(),
                abort_reason_holder={},
            )
            assert out.get("confirmed") is True, out
            assert out.get("polls") == 1
            assert float(out.get("pass_bid")) >= 0.9

            # 2) rises on later poll
            fetch2, st2 = _books_factory([0.87, 0.88, 0.91])
            g2 = BidGate(
                root,
                min_bid=0.9,
                poll_s=0.05,
                timeout_s=2.0,
                fetch_books_fn=fetch2,
            )
            out2 = g2.await_bid(
                {"match_id": "m2", "home_score": 1, "away_score": 0, "type": "score_change"},
                abort=threading.Event(),
                abort_reason_holder={},
            )
            assert out2.get("confirmed") is True, out2
            assert int(out2.get("polls") or 0) >= 3
            assert float(out2.get("pass_bid")) >= 0.9
            assert st2["i"] >= 3

            # 3) timeout
            fetch3, _ = _books_factory([0.88, 0.88, 0.88, 0.88])
            g3 = BidGate(
                root,
                min_bid=0.9,
                poll_s=0.05,
                timeout_s=0.2,
                fetch_books_fn=fetch3,
            )
            out3 = g3.await_bid(
                {"match_id": "m3", "home_score": 1, "away_score": 0, "type": "score_change"},
                abort=threading.Event(),
                abort_reason_holder={},
            )
            assert out3.get("confirmed") is False, out3
            assert out3.get("error") == "market_bid_timeout"

            # 4) cancel mid-poll
            fetch4, _ = _books_factory([0.80] * 50)
            g4 = BidGate(
                root,
                min_bid=0.9,
                poll_s=0.2,
                timeout_s=5.0,
                fetch_books_fn=fetch4,
            )
            assert g4.submit(
                "ek|m4",
                {"match_id": "m4", "home_score": 1, "away_score": 0, "type": "score_change"},
                af_gate={"confirmed": True},
            )
            time.sleep(0.05)
            assert g4.cancel_match("m4", reason="dqd_reversal") >= 1
            # Wait for worker to finish
            deadline = time.monotonic() + 2.0
            jobs: list = []
            while time.monotonic() < deadline:
                jobs = g4.drain_done()
                if jobs:
                    break
                time.sleep(0.05)
            # Cancelled futures may not appear in drain (popped early) — OK either way.
            if jobs:
                assert jobs[0]["gate"].get("confirmed") is False
                assert jobs[0]["gate"].get("error") in ("aborted", "market_bid_timeout")

            # 5) maybe_trade hard skip
            (root / "data" / "pm-quote").mkdir(parents=True)
            settings = TradeSettings(
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
                min_market_bid=0.9,
                min_order_shares=0.0,
                enabled=True,
                size_tiers=((0.98, 2.0),),
                max_open_usdc=1000.0,
                size_floor_usdc=1.0,
            )
            ex = TradeExecutor(root, settings, af_mode="gate")
            row = ex.maybe_trade(
                {
                    "misprice": True,
                    "trade": "buy_win",
                    "token_id": "tok_x",
                    "market_key": "match_total_0.5_over",
                    "settlement": "WIN",
                    "best_ask": 0.98,
                    "best_bid": 0.85,
                    "asks_top": [{"price": "0.98", "size": "20"}],
                    "bids_top": [{"price": "0.85", "size": "20"}],
                    "tick_size": "0.01",
                },
                event_key="score_change|m|0-0→1-0",
                match_meta={"match_id": "m", "event_type": "score_change"},
                event_type="score_change",
            )
            assert row is not None
            assert row.get("status") == "skipped"
            assert "market_bid_below_min" in str(row.get("skip_reason") or "")

    finally:
        lib.join_ft_context = orig_join  # type: ignore[assignment]
        lib.collect_target_tokens = orig_collect  # type: ignore[assignment]

    print("ok: bid gate pass/poll/timeout/cancel + trade hard-skip")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
