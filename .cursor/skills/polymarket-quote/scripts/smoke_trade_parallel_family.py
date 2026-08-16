#!/usr/bin/env python3
"""Smoke: parallel live posts + B/A family tilt (no exact on B; skip 0.001 exact)."""

from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
import types
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

if "eth_account" not in sys.modules:
    eth_account = types.ModuleType("eth_account")
    eth_account.Account = object  # type: ignore[attr-defined]
    sys.modules["eth_account"] = eth_account
if "dotenv" not in sys.modules:
    dotenv = types.ModuleType("dotenv")
    dotenv.load_dotenv = lambda *_args, **_kwargs: False  # type: ignore[attr-defined]
    sys.modules["dotenv"] = dotenv

from quote_lib import (  # noqa: E402
    TRADE_POOL_WORKERS,
    odds_grade_include_exact,
    odds_grade_skip_fine_tick_exact,
    quote_tokens,
)
from trade_executor import TradeExecutor  # noqa: E402
from trade_settings import TradeSettings  # noqa: E402


def _settings(*, max_open_usdc: float = 1000.0) -> TradeSettings:
    return TradeSettings(
        private_key="",
        funder=None,
        signature_type=2,
        chain_id=137,
        clob_host="https://clob.polymarket.com",
        data_api_url="https://data-api.polymarket.com",
        live_goals=True,
        live_ft=False,
        take_depth="top",
        max_levels=5,
        max_usdc=1.0,
        max_shares=25.0,
        max_slippage=0.03,
        allow_extreme_prices=False,
        min_buy_price=0.6,
        min_order_shares=0.0,
        enabled=True,
        size_tiers=((0.98, 1.0),),
        max_open_usdc=max_open_usdc,
        size_floor_usdc=1.0,
    )


def _quote(token: str) -> dict:
    return {
        "misprice": True,
        "trade": "buy_win",
        "token_id": token,
        "market_key": f"totals:{token}",
        "family": "totals",
        "settlement": "WIN",
        "best_ask": 0.9,
        "best_ask_size": 100.0,
        "asks_top": [{"price": 0.9, "size": 100.0}],
        "best_bid": 0.89,
        "net_edge": 0.09,
        "gross_edge": 0.1,
        "fee": 0.01,
        "tick_size": "0.01",
    }


def _meta(base: str, level: str, target: float, match_id: str = "m1") -> dict:
    return {
        "match_id": match_id,
        "home": "Home",
        "away": "Away",
        "home_score": 1,
        "away_score": 0,
        "event_type": "score_change",
        "trade_context": {
            "odds_grade": level,
            "target_usdc": target,
            "base_event_key": base,
            "position_semantics": "cumulative_target_per_token",
        },
    }


class _SlowTrader:
    def __init__(self, delay_s: float = 0.2) -> None:
        self.delay_s = delay_s
        self.ready = True
        self.calls = 0
        self._lock = threading.Lock()
        self.started: list[float] = []

    def post_market_buy(self, *_args: object, **_kwargs: object) -> dict:
        with self._lock:
            self.calls += 1
            self.started.append(time.monotonic())
        time.sleep(self.delay_s)
        return {
            "status": "matched",
            "success": True,
            "makingAmount": "3.0",
            "takingAmount": "3.333333",
        }

    @staticmethod
    def is_order_success(_response: dict) -> bool:
        return True


class _RecordingExecutor:
    def __init__(self) -> None:
        self.tokens: list[str] = []

    def maybe_trade(self, quote: dict, **_kw: object) -> dict:
        tid = str(quote.get("token_id") or "")
        self.tokens.append(tid)
        return {"status": "dry_run", "success": True, "skip_reason": None, "plan": None, "live": False}


def main() -> int:
    if not os.getenv("QUOTE_TRADE_WORKERS"):
        assert TRADE_POOL_WORKERS == 4
    assert odds_grade_include_exact("B") is False
    assert odds_grade_include_exact("A") is True
    assert odds_grade_include_exact("C") is True
    assert odds_grade_skip_fine_tick_exact("A") is True
    assert odds_grade_skip_fine_tick_exact("B") is False

    rec = _RecordingExecutor()
    books = {
        "exact-001": {
            "best_bid": 0.89,
            "best_ask": 0.9,
            "best_ask_size": 100.0,
            "tick_size": "0.001",
        },
        "totals-ok": {
            "best_bid": 0.89,
            "best_ask": 0.9,
            "best_ask_size": 100.0,
            "tick_size": "0.01",
        },
    }
    token_rows = [
        {
            "token_id": "exact-001",
            "family": "exact_score",
            "settlement": "WIN",
            "market_key": "exact:1-0",
        },
        {
            "token_id": "totals-ok",
            "family": "totals",
            "settlement": "WIN",
            "market_key": "totals:over:1.5",
        },
    ]
    priced = quote_tokens(
        token_rows,
        books=books,
        trade_executor=rec,
        skip_fine_tick_exact=True,
        trade_workers=1,
    )
    by_tid = {r["token_id"]: r for r in priced}
    assert by_tid["exact-001"]["trade_attempt"]["skip_reason"] == "exact_tick_0_001"
    assert rec.tokens == ["totals-ok"]

    base = "score_change|m1|0-0->1-0"
    with tempfile.TemporaryDirectory() as td:
        trader = _SlowTrader(delay_s=0.2)
        ex = TradeExecutor(
            Path(td),
            _settings(max_open_usdc=1000.0),
            trader=trader,  # type: ignore[arg-type]
            af_mode="gate",
        )

        def _buy(token: str) -> dict:
            row = ex.maybe_trade(
                _quote(token),
                event_key=f"{base}|odds_grade_B|{token}",
                match_meta=_meta(base, "B", 3.0),
            )
            assert row is not None
            return row

        t0 = time.monotonic()
        with ThreadPoolExecutor(max_workers=2) as pool:
            rows = list(pool.map(_buy, ["token-a", "token-b"]))
        elapsed = time.monotonic() - t0
        assert all(r.get("status") == "posted" and r.get("live") is True for r in rows)
        assert trader.calls == 2
        open_usdc = sum(float(lot.get("usdc") or 0) for lot in ex.ledger.all_open())
        assert round(open_usdc, 6) == 6.0
        # Serialized lock-through-HTTP would take ~0.4s; overlapping posts ~0.2s.
        assert elapsed < 0.35, elapsed
        overlap = abs(trader.started[0] - trader.started[1]) < 0.15
        assert overlap, trader.started

    with tempfile.TemporaryDirectory() as td:
        trader = _SlowTrader(delay_s=0.15)
        ex = TradeExecutor(
            Path(td),
            _settings(max_open_usdc=3.0),
            trader=trader,  # type: ignore[arg-type]
            af_mode="gate",
        )

        def _buy2(token: str) -> dict:
            row = ex.maybe_trade(
                _quote(token),
                event_key=f"{base}|odds_grade_B|{token}",
                match_meta=_meta(base, "B", 3.0),
            )
            assert row is not None
            return row

        with ThreadPoolExecutor(max_workers=2) as pool:
            rows = list(pool.map(_buy2, ["token-c", "token-d"]))
        posted = [r for r in rows if r.get("status") == "posted"]
        skipped = [r for r in rows if r.get("status") == "skipped"]
        assert len(posted) == 1 and len(skipped) == 1
        assert skipped[0]["skip_reason"]
        open_usdc = sum(float(lot.get("usdc") or 0) for lot in ex.ledger.all_open())
        assert round(open_usdc, 6) <= 3.0 + 1e-9
        assert trader.calls == 1
        time.sleep(0.05)

    with tempfile.TemporaryDirectory() as td:
        started = threading.Event()

        class _GateTrader(_SlowTrader):
            def post_market_buy(self, *_args: object, **_kwargs: object) -> dict:
                started.set()
                return super().post_market_buy(*_args, **_kwargs)

        trader = _GateTrader(delay_s=0.2)
        ex = TradeExecutor(
            Path(td),
            _settings(max_open_usdc=1000.0),
            trader=trader,  # type: ignore[arg-type]
            af_mode="gate",
        )
        with ex._lock:
            ex._buy_blocked_matches.add("m1")
            ex._pending["in-flight"] = {
                "usdc": 3.0,
                "token_id": "token-x",
                "match_id": "m1",
                "event_key": base,
            }
            ex._maybe_clear_buy_block("m1")
            assert "m1" in ex._buy_blocked_matches
            ex._pending.clear()
            ex._maybe_clear_buy_block("m1")
            assert "m1" not in ex._buy_blocked_matches

        def _late() -> dict:
            row = ex.maybe_trade(
                _quote("token-late"),
                event_key=f"{base}|odds_grade_B|token-late",
                match_meta=_meta(base, "B", 3.0),
            )
            assert row is not None
            return row

        t = threading.Thread(target=_late)
        t.start()
        assert started.wait(2.0)
        with ex._lock:
            ex._buy_blocked_matches.add("m1")
        t.join(3.0)
        assert not t.is_alive()
        lots = ex.ledger.open_for_match("m1")
        assert len(lots) == 1
        assert lots[0].get("pending_flatten") is True
        assert "buy_blocked_after_post" in str(lots[0].get("pending_reason") or "")
        time.sleep(0.05)

    print("ok: parallel posts + family tilt")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
