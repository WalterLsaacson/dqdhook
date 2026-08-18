#!/usr/bin/env python3
"""Smoke: A/B FAK then GTD rest at 0.98/0.99; reversal cancels immediately."""

from __future__ import annotations

import sys
import tempfile
import time
import types
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

from rest_ladder import (  # noqa: E402
    allocate_rest_ladder,
    ask_in_fak_zone,
    rest_expire_s,
    rest_limit_tick_size,
    select_rest_prices,
)
from score_reversal import AF_STATUS_CONFIRMED, AF_STATUS_PENDING  # noqa: E402
from trade_executor import (  # noqa: E402
    TradeExecutor,
    odds_grade_from_event_key,
    rest_fill_odds_grade,
)
from trade_settings import TradeSettings  # noqa: E402


def _settings(*, live_goals: bool = False) -> TradeSettings:
    return TradeSettings(
        private_key="",
        funder=None,
        signature_type=2,
        chain_id=137,
        clob_host="https://clob.polymarket.com",
        data_api_url="https://data-api.polymarket.com",
        live_goals=live_goals,
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
        max_open_usdc=1000.0,
        size_floor_usdc=1.0,
    )


def _meta(level: str, target: float, base: str = "score_change|m1|0-0->1-0") -> dict:
    return {
        "match_id": "m1",
        "home": "Home",
        "away": "Away",
        "home_score": 1,
        "away_score": 0,
        "event_type": "score_change",
        "trade_context": {
            "odds_grade": level,
            "target_usdc": target,
            "base_event_key": base,
        },
    }


def _win_quote(*, misprice: bool, token: str = "tok1") -> dict:
    if misprice:
        return {
            "misprice": True,
            "trade": "buy_win",
            "token_id": token,
            "market_key": "totals:over:0.5",
            "family": "totals",
            "settlement": "WIN",
            "best_ask": 0.97,
            "best_ask_size": 2.0,
            "asks_top": [{"price": 0.97, "size": 2.0}],
            "best_bid": 0.96,
            "tick_size": "0.01",
            "net_edge": 0.03,
        }
    return {
        "misprice": False,
        "trade": "buy_win",
        "token_id": token,
        "market_key": "totals:over:0.5",
        "family": "totals",
        "settlement": "WIN",
        "best_ask": None,
        "asks_top": [],
        "best_bid": 0.99,
        "best_bid_size": 2000.0,
        "tick_size": "0.01",
        "misprice_reason": "",
    }


def test_ladder() -> None:
    assert abs(rest_expire_s() - 3600.0) < 1e-9
    assert rest_limit_tick_size("0.001") == "0.01"
    assert rest_limit_tick_size("0.01") == "0.01"
    assert rest_limit_tick_size(None) == "0.01"
    assert select_rest_prices(best_ask=0.99) == ()
    assert select_rest_prices(best_bid=0.99) == (0.99,)
    assert select_rest_prices(best_bid=0.97) == (0.99, 0.98)
    levels = allocate_rest_ladder(20.0, tick_size="0.001")
    assert [round(x["price"], 2) for x in levels] == [0.99, 0.98], levels
    levels = allocate_rest_ladder(20.0, tick_size="0.01")
    assert [round(x["price"], 2) for x in levels] == [0.99, 0.98], levels
    assert len(allocate_rest_ladder(20.0, best_bid=0.99)) == 1
    assert allocate_rest_ladder(20.0, best_bid=0.99)[0]["price"] == 0.99
    assert allocate_rest_ladder(20.0, best_ask=0.99) == []
    assert ask_in_fak_zone(0.992) and not ask_in_fak_zone(0.993)
    assert abs(sum(x["usdc"] for x in levels) - 20.0) < 0.03, levels
    small = allocate_rest_ladder(1.5, tick_size="0.01")
    assert len(small) == 1 and small[0]["price"] == 0.99, small
    none = allocate_rest_ladder(0.4, tick_size="0.01")
    assert none == []
    assert odds_grade_from_event_key("score_change|m1|0-0->1-0|odds_grade_B") == "B"
    assert rest_fill_odds_grade({}, {"event_key": "score_change|m|odds_grade_B"}) == "B"
    assert rest_fill_odds_grade({}, {"odds_grade": "B"}) == "B"
    assert rest_fill_odds_grade({}, {}) == "B"
    print("ok: rest ladder 0.99/0.98")


def test_rest_only_no_ask() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "data" / "pm-quote").mkdir(parents=True)
        ex = TradeExecutor(root, _settings(live_goals=False), af_mode="gate")
        row = ex.maybe_trade(
            _win_quote(misprice=False),
            event_key="score_change|m1|0-0->1-0|odds_grade_A",
            match_meta=_meta("A", 20.0),
            event_type="score_change",
        )
        assert row is not None, row
        assert row["status"] == "rest_dry_run", row
        assert row["plan"]["order_type"] in ("GTD", "GTC")
        prices = [lvl["price"] for lvl in row["plan"]["levels"]]
        assert prices == [0.99], prices
        reserved = ex.ledger.rest_reserved_usdc(token_id="tok1", match_id="m1")
        assert reserved >= 19.0, reserved
        again = ex.maybe_trade(
            _win_quote(misprice=False),
            event_key="score_change|m1|0-0->1-0|odds_grade_A",
            match_meta=_meta("A", 20.0),
            event_type="score_change",
        )
        assert again is None, again
        time.sleep(0.15)
        print("ok: no-ask A rests once at 0.99 when bid>=0.99")


def test_c_does_not_rest() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "data" / "pm-quote").mkdir(parents=True)
        ex = TradeExecutor(root, _settings(live_goals=False), af_mode="gate")
        row = ex.maybe_trade(
            _win_quote(misprice=False),
            event_key="score_change|m1|0-0->1-0|odds_grade_C",
            match_meta=_meta("C", 0.0),
            event_type="score_change",
        )
        assert row is None, row
        time.sleep(0.05)
        print("ok: C does not rest")


def test_reversal_cancels_rest() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "data" / "pm-quote").mkdir(parents=True)
        ex = TradeExecutor(root, _settings(live_goals=True), af_mode="gate")

        class FakeTrader:
            def __init__(self) -> None:
                self.canceled: list[str] = []
                self.n = 0

            def post_limit_buy(self, *args, **kwargs):
                self.n += 1
                return {"success": True, "status": "LIVE", "orderID": f"rest{self.n}"}

            def is_order_success(self, result, *, market=True):
                return bool(result and result.get("success"))

            def cancel_order(self, order_id: str):
                self.canceled.append(str(order_id))
                return {"canceled": True}

        fake = FakeTrader()
        ex.trader = fake  # type: ignore[assignment]
        ex.ensure_trader = lambda: fake  # type: ignore[method-assign]
        row = ex.maybe_trade(
            _win_quote(misprice=False),
            event_key="score_change|m1|0-0->1-0|odds_grade_B",
            match_meta=_meta("B", 10.0),
            event_type="score_change",
        )
        assert row and row["status"] == "rest_posted", row
        n = ex.cancel_rest_orders_for_match("m1", reason="dqd_reversal")
        assert n >= 1, n
        assert fake.canceled, fake.canceled
        assert ex.ledger.rest_reserved_usdc(match_id="m1") < 1e-9
        blocked = ex.maybe_trade(
            _win_quote(misprice=False),
            event_key="score_change|m1|0-0->1-0|odds_grade_B",
            match_meta=_meta("B", 10.0),
            event_type="score_change",
        )
        assert blocked is None, blocked
        ex.clear_rest_block("m1")
        restored = ex.maybe_trade(
            _win_quote(misprice=False),
            event_key="score_change|m1|0-0->1-0|odds_grade_B",
            match_meta=_meta("B", 10.0),
            event_type="score_change",
        )
        assert restored and restored["status"] == "rest_posted", restored
        time.sleep(0.15)
        print("ok: DQD reversal cancels live rest immediately")


def test_ask_fak_cancels_rest() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "data" / "pm-quote").mkdir(parents=True)
        ex = TradeExecutor(root, _settings(live_goals=True), af_mode="gate")

        class FakeTrader:
            def __init__(self) -> None:
                self.canceled: list[str] = []
                self.market_buys = 0
                self.n = 0

            def post_limit_buy(self, *args, **kwargs):
                self.n += 1
                return {"success": True, "status": "LIVE", "orderID": f"rest{self.n}"}

            def post_market_buy(self, *args, **kwargs):
                self.market_buys += 1
                return {
                    "success": True,
                    "status": "MATCHED",
                    "takingAmount": "2.0",
                    "makingAmount": "1.94",
                }

            def is_order_success(self, result, *, market=True):
                return bool(result and result.get("success"))

            def cancel_order(self, order_id: str):
                self.canceled.append(str(order_id))
                return {"canceled": True}

        fake = FakeTrader()
        ex.trader = fake  # type: ignore[assignment]
        ex.ensure_trader = lambda: fake  # type: ignore[method-assign]
        rest_row = ex.maybe_trade(
            _win_quote(misprice=False),
            event_key="score_change|m1|0-0->1-0|odds_grade_B",
            match_meta=_meta("B", 10.0),
            event_type="score_change",
        )
        assert rest_row and rest_row["status"] == "rest_posted", rest_row
        mis = dict(_win_quote(misprice=True))
        mis["best_ask"] = 0.99
        mis["misprice"] = True
        fak_row = ex.maybe_trade(
            mis,
            event_key="score_change|m1|0-0->1-0|odds_grade_B",
            match_meta=_meta("B", 10.0),
            event_type="score_change",
        )
        assert fake.canceled == ["rest1"], fake.canceled
        assert fake.market_buys == 1, fake.market_buys
        assert fak_row and fak_row["status"] == "posted", fak_row
        leftover = ex.ledger.rest_reserved_usdc(match_id="m1")
        assert leftover >= 1.0, leftover
        assert fak_row.get("rest", {}).get("status") == "rest_posted", fak_row.get("rest")
        time.sleep(0.15)
        print("ok: misprice ask cancels rest, FAKs, then rests remainder")


def test_stale_rest_keeps_other_family() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "data" / "pm-quote").mkdir(parents=True)
        ex = TradeExecutor(root, _settings(live_goals=False), af_mode="gate")
        ex.maybe_trade(
            _win_quote(misprice=False, token="tok_totals"),
            event_key="score_change|m1|0-0->1-0|odds_grade_A",
            match_meta=_meta("A", 20.0),
            event_type="score_change",
        )
        n = ex.cancel_stale_rest("m1", keep_token_ids={"tok_totals", "tok_exact"})
        assert n == 0, n
        assert ex.ledger.rest_reserved_usdc(token_id="tok_totals", match_id="m1") >= 19.0
        n2 = ex.cancel_stale_rest("m1", keep_token_ids={"tok_exact"})
        assert n2 >= 1, n2
        assert ex.ledger.rest_reserved_usdc(token_id="tok_totals", match_id="m1") < 1e-9
        time.sleep(0.15)
        print("ok: stale rest only drops tokens outside the full WIN set")


def test_rest_fill_keeps_b_pending() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "data" / "pm-quote").mkdir(parents=True)
        ex = TradeExecutor(root, _settings(live_goals=True), af_mode="gate")

        class FakeTrader:
            def post_limit_buy(self, *args, **kwargs):
                return {"success": True, "status": "LIVE", "orderID": "restb1"}

            def is_order_success(self, result, *, market=True):
                return bool(result and result.get("success"))

            def get_order(self, order_id: str):
                return {
                    "status": "MATCHED",
                    "size_matched": 10.0,
                    "makingAmount": "9.9",
                }

            def cancel_order(self, order_id: str):
                return {"canceled": True}

        fake = FakeTrader()
        ex.trader = fake  # type: ignore[assignment]
        ex.ensure_trader = lambda: fake  # type: ignore[method-assign]
        row = ex.maybe_trade(
            _win_quote(misprice=False),
            event_key="score_change|m1|0-0->1-0|odds_grade_B",
            match_meta=_meta("B", 10.0),
            event_type="score_change",
        )
        assert row and row["status"] == "rest_posted", row
        lots = ex.ledger.open_for_match("m1")
        assert lots and lots[0].get("af_status") == AF_STATUS_PENDING, lots
        filled = ex.reconcile_rest_orders()
        assert filled, filled
        lots = ex.ledger.open_for_match("m1")
        assert lots, lots
        assert lots[0].get("af_status") == AF_STATUS_PENDING, lots[0]
        assert lots[0].get("af_status") != AF_STATUS_CONFIRMED
        assert float(lots[0].get("usdc") or 0) >= 9.0, lots[0]
        time.sleep(0.15)
        print("ok: B rest fill stays pending, not A/confirmed")


def main() -> int:
    test_ladder()
    test_rest_only_no_ask()
    test_c_does_not_rest()
    test_reversal_cancels_rest()
    test_ask_fak_cancels_rest()
    test_stale_rest_keeps_other_family()
    test_rest_fill_keeps_b_pending()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
