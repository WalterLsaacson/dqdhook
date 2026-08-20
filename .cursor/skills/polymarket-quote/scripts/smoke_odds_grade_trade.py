#!/usr/bin/env python3
"""Smoke: C=record-only (no USDC); B/A cumulative live targets $10/$20."""

from __future__ import annotations

import sys
import tempfile
import time
import types
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

# Dry-run smoke does not initialize a wallet; keep the optional live dependency
# from preventing import in minimal test environments.
if "eth_account" not in sys.modules:
    eth_account = types.ModuleType("eth_account")
    eth_account.Account = object  # type: ignore[attr-defined]
    sys.modules["eth_account"] = eth_account
if "dotenv" not in sys.modules:
    dotenv = types.ModuleType("dotenv")
    dotenv.load_dotenv = lambda *_args, **_kwargs: False  # type: ignore[attr-defined]
    sys.modules["dotenv"] = dotenv

from fill_planner import FillPlan  # noqa: E402
from grade_sizing import grade_target_usdc  # noqa: E402
from trade_executor import TradeExecutor, actual_matched_buy_plan  # noqa: E402
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


def _quote(token: str) -> dict:
    return {
        "misprice": True,
        "trade": "buy_win",
        "token_id": token,
        "market_key": "totals:over:1.5",
        "family": "totals",
        "settlement": "WIN",
        "best_ask": 0.9,
        "best_ask_size": 100.0,
        "asks_top": [{"price": 0.9, "size": 100.0}],
        "best_bid": 0.89,
        "net_edge": 0.09,
        "gross_edge": 0.1,
        "fee": 0.01,
    }


def _meta(base: str, level: str, target: float) -> dict:
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
            "position_semantics": "cumulative_target_per_token",
        },
    }


def _buy(ex: TradeExecutor, token: str, base: str, level: str, target: float) -> dict:
    key = base if level == "C" else f"{base}|odds_grade_{level}"
    row = ex.maybe_trade(_quote(token), event_key=key, match_meta=_meta(base, level, target))
    assert row is not None
    return row


def main() -> int:
    base = "score_change|m1|0-0->1-0"
    b_target = grade_target_usdc("B")
    a_target = grade_target_usdc("A")
    with tempfile.TemporaryDirectory() as td:
        ex = TradeExecutor(Path(td), _settings(), af_mode="gate")
        c = _buy(ex, "token-cba", base, "C", 0.0)
        assert c["status"] == "dry_run"
        assert c.get("live") is False
        assert c.get("plan") is None
        assert ex.ledger.open_for_match("m1") == []
        b = _buy(ex, "token-cba", base, "B", b_target)
        a = _buy(ex, "token-cba", base, "A", a_target)
        assert b["plan"]["usdc"] == b_target
        assert a["plan"]["usdc"] == max(0.0, a_target - b_target)
        assert b["size_policy"]["already_usdc"] == 0.0
        assert a["size_policy"]["already_usdc"] == b_target
        assert a["size_policy"]["remaining_target_usdc"] == max(0.0, a_target - b_target)
        lot = ex.ledger.open_for_match("m1")[0]
        assert round(float(lot["usdc"]), 6) == a_target
        dup = _buy(ex, "token-cba", base, "A", a_target)
        assert dup["status"] == "skipped" and dup["skip_reason"] == "already_done"
        time.sleep(0.05)

    with tempfile.TemporaryDirectory() as td:
        ex = TradeExecutor(Path(td), _settings(), af_mode="gate")
        _buy(ex, "token-ca", base, "C", 0.0)
        direct_a = _buy(ex, "token-ca", base, "A", a_target)
        # C left no lot — A sizes the full target.
        assert direct_a["plan"]["usdc"] == a_target
        assert direct_a["size_policy"]["remaining_target_usdc"] == a_target
        assert round(float(ex.ledger.open_for_match("m1")[0]["usdc"]), 6) == a_target
        time.sleep(0.05)

    with tempfile.TemporaryDirectory() as td:
        ex = TradeExecutor(Path(td), _settings(), af_mode="gate")
        # Existing target exposure skips even if this grade idempotency key is new.
        ex.ledger.register_buy(
            match_id="m1",
            token_id="token-full",
            market_key="totals",
            shares=a_target,
            usdc=a_target,
            home_score=1,
            away_score=0,
            live=False,
            event_key=base,
        )
        reached = _buy(ex, "token-full", base, "A", a_target)
        assert reached["status"] == "skipped"
        assert reached["skip_reason"] == "odds_grade_target_reached"
        time.sleep(0.05)

    partial_plan = FillPlan(
        trade="buy_win",
        side="BUY",
        take_depth="top",
        order_type="FAK",
        shares=1.0,
        usdc=1.0,
        worst_price=0.9,
        levels_used=1,
        levels=[],
        skip_reason=None,
    )
    actual = actual_matched_buy_plan(
        partial_plan,
        {"status": "matched", "takingAmount": "0.444444", "makingAmount": "0.4"},
    )
    assert actual.usdc == 0.4 and actual.shares == 0.444444

    class _PartialTrader:
        def __init__(self) -> None:
            self.calls = 0
            self.ready = True

        def post_market_buy(self, *_args: object, **_kwargs: object) -> dict:
            self.calls += 1
            making = "0.4" if self.calls == 1 else "9.6"
            taking = "0.444444" if self.calls == 1 else "10.666667"
            return {
                "status": "matched",
                "success": True,
                "makingAmount": making,
                "takingAmount": taking,
            }

        @staticmethod
        def is_order_success(_response: dict) -> bool:
            return True

    with tempfile.TemporaryDirectory() as td:
        trader = _PartialTrader()
        ex = TradeExecutor(
            Path(td),
            _settings(live_goals=True),
            trader=trader,  # type: ignore[arg-type]
            af_mode="gate",
        )
        # C stays dry even when goals are live — no CLOB, no open lot.
        c_live_stack = _buy(ex, "token-partial", base, "C", 0.0)
        assert c_live_stack["status"] == "dry_run"
        assert c_live_stack.get("live") is False
        assert c_live_stack.get("plan") is None
        assert trader.calls == 0
        assert ex.ledger.open_for_match("m1") == []

        first_partial = _buy(ex, "token-partial", base, "B", b_target)
        assert first_partial.get("live") is True
        assert first_partial["plan"]["usdc"] == 0.4
        assert round(float(ex.ledger.open_for_match("m1")[0]["usdc"]), 6) == 0.4
        retry_partial = _buy(ex, "token-partial", base, "B", b_target)
        assert retry_partial["size_policy"]["remaining_target_usdc"] == round(b_target - 0.4, 6)
        assert retry_partial["size_policy"]["max_usdc"] == round(b_target - 0.4, 6)
        assert retry_partial["plan"]["usdc"] == 9.6
        assert trader.calls == 2
        assert round(float(ex.ledger.open_for_match("m1")[0]["usdc"]), 6) == b_target
        a_partial = _buy(ex, "token-partial", base, "A", a_target)
        assert a_partial["size_policy"]["already_usdc"] == b_target
        assert a_partial["size_policy"]["remaining_target_usdc"] == max(0.0, a_target - b_target)
        assert a_partial["plan"]["usdc"] == 9.6
        assert trader.calls == 3
        assert round(float(ex.ledger.open_for_match("m1")[0]["usdc"]), 6) == round(0.4 + 9.6 + 9.6, 6)
        time.sleep(0.05)

    print("ok: odds grade C=record-only; B/A live sizing follows grade_sizing")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
