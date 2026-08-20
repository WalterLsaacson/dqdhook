#!/usr/bin/env python3
"""Smoke: flatten sell sizing + balance-gate error parse."""

from __future__ import annotations

import sys
import json
import types
from decimal import Decimal
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

# This state-machine smoke supplies its own fake trader and never initializes a
# wallet.  Keep the optional live dependency from blocking minimal CI hosts.
if "eth_account" not in sys.modules:
    eth_account = types.ModuleType("eth_account")
    eth_account.Account = object  # type: ignore[attr-defined]
    sys.modules["eth_account"] = eth_account
if "dotenv" not in sys.modules:
    dotenv = types.ModuleType("dotenv")
    dotenv.load_dotenv = lambda *_args, **_kwargs: False  # type: ignore[attr-defined]
    sys.modules["dotenv"] = dotenv

from trade_executor import (  # noqa: E402
    FLATTEN_MAX_LOSS_FRAC,
    FLATTEN_MIN_PRICE,
    flatten_cancel_ack,
    flatten_min_sell_price,
    flatten_reason_append,
    flatten_sell_shares,
    flatten_sell_shares_available,
    floor_shares,
    gate_free_cap,
    gate_has_locked_inventory,
    is_not_enough_balance_error,
    is_terminal_flatten_error,
    lot_entry_price,
    parse_balance_gate_error,
)
from score_reversal import reconcile_lot_inventory  # noqa: E402


def main() -> int:
    assert floor_shares(Decimal("3.333332")) == Decimal("3.33")
    assert flatten_cancel_ack({"canceled": ["o1"]}, "o1")
    assert flatten_cancel_ack({"status": "cancelled"}, "o1")
    assert not flatten_cancel_ack(
        {"canceled": [], "not_canceled": {"o1": "live"}}, "o1"
    )
    assert flatten_sell_shares(Decimal("3.333332")) == Decimal("3.29")
    assert flatten_sell_shares(Decimal("1.0")) == Decimal("0.99")
    assert flatten_sell_shares(Decimal("0.015")) == Decimal("0.01")

    assert is_terminal_flatten_error("invalid maker amount")
    assert is_terminal_flatten_error(
        "PolyApiException[status_code=400, error_message={'error': 'invalid token id'}]"
    )
    assert not is_terminal_flatten_error("Request exception!")
    # Append should not explode on repeated delayed-fill tags.
    r = "ft_reversal_vs_entry ft=0-1"
    for _ in range(50):
        r = flatten_reason_append(r, "awaiting_delayed_fill")
    assert r.count("awaiting_delayed_fill") == 1
    assert len(r) < 200

    err = (
        "PolyApiException[status_code=400, error_message={'error': "
        "'not enough balance / allowance: the balance is not enough -> "
        "balance: 3333332, sum of matched orders: 3330000, "
        "order amount (inc. fees): 3330000'}]"
    )
    assert is_not_enough_balance_error(err)
    gate = parse_balance_gate_error(err)
    assert gate is not None
    assert gate["balance"] == Decimal("3.333332")
    assert gate["matched"] == Decimal("3.33")
    assert abs(gate["free"] - Decimal("0.003332")) < Decimal("0.000001")
    assert gate_has_locked_inventory(gate)
    assert gate_free_cap(gate, Decimal("3.333332")) == gate["free"]
    # Live bal moved → ignore stale free.
    assert gate_free_cap(gate, Decimal("0.05")) is None

    # Bodø first reject: free 0.0358 is still sellable (cap to 0.03).
    bodo = parse_balance_gate_error(
        "not enough balance / allowance: the balance is not enough -> "
        "balance: 3225800, sum of matched orders: 3190000, "
        "order amount (inc. fees): 3190000"
    )
    assert bodo is not None
    assert abs(bodo["free"] - Decimal("0.0358")) < Decimal("0.000001")
    assert not gate_has_locked_inventory(bodo)
    assert flatten_sell_shares_available(bodo["balance"], free=bodo["free"]) == Decimal(
        "0.03"
    )
    assert gate_free_cap(bodo, bodo["balance"]) == bodo["free"]
    # Later tick: free dust while matched still holds bag → keep pending.
    locked = parse_balance_gate_error(
        "not enough balance / allowance: the balance is not enough -> "
        "balance: 35800, sum of matched orders: 3190000, "
        "order amount (inc. fees): 30000"
    )
    assert locked is not None
    assert gate_has_locked_inventory(locked)
    assert (
        flatten_sell_shares_available(Decimal("0.0358"), free=Decimal("0.0058"))
        == Decimal("0")
    )

    # Entry−20% floor (no 0.01 panic dump).
    assert FLATTEN_MAX_LOSS_FRAC == Decimal("0.20")
    lot = {"shares": "20.618557", "usdc": "20.0", "tick_size": "0.01"}
    assert abs(lot_entry_price(lot) - Decimal("0.97")) < Decimal("0.001")
    # 0.97 * 0.80 = 0.776 → tick floor 0.77
    assert flatten_min_sell_price(lot) == Decimal("0.77")
    assert flatten_min_sell_price({"ask": "0.50"}) == Decimal("0.40")
    assert flatten_min_sell_price({}) == FLATTEN_MIN_PRICE

    # Delayed fill: plan shares understate live bal → cheaper VWAP → lower floor.
    delayed = {"shares": 20.618557, "usdc": 20.0, "fill_status": "pending_fill"}
    assert reconcile_lot_inventory(delayed, Decimal("25.0"))
    assert delayed["shares"] == 25.0
    assert delayed["usdc"] == 20.0
    assert abs(lot_entry_price(delayed) - Decimal("0.8")) < Decimal("0.001")
    assert flatten_min_sell_price(delayed) == Decimal("0.64")  # 0.8*0.80=0.64

    # Residual after partial sell: scale usdc so VWAP (and floor) hold.
    residual = {"shares": 20.0, "usdc": 19.4}  # entry 0.97
    assert reconcile_lot_inventory(residual, Decimal("10.0"))
    assert residual["shares"] == 10.0
    assert abs(float(residual["usdc"]) - 9.7) < 1e-9
    assert abs(lot_entry_price(residual) - Decimal("0.97")) < Decimal("0.001")
    assert flatten_min_sell_price(residual) == Decimal("0.77")

    # Dust/close must lift buy_blocked_pending_flatten for the match.
    import tempfile
    from trade_settings import TradeSettings
    from trade_executor import TradeExecutor

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
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
            max_usdc=2.0,
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
        ex = TradeExecutor(root, settings)
        mid = "m_block"
        ex.ledger.register_buy(
            match_id=mid,
            token_id="tok_block",
            market_key="match_total_0.5_over",
            shares=1.02,
            usdc=1.0,
            home_score=1,
            away_score=0,
            live=True,
            event_key="score_change|m_block|0-0->1-0",
        )
        ex._buy_blocked_matches.add(mid)
        ex.ledger.mark_closed("tok_block", mid, reason="score_reversal|dust_bal=0.0004")
        ex._maybe_clear_buy_block(mid)
        assert mid not in ex._buy_blocked_matches
        # Still-open lot must keep the block.
        ex.ledger.register_buy(
            match_id=mid,
            token_id="tok_block2",
            market_key="match_total_1.5_over",
            shares=1.0,
            usdc=1.0,
            home_score=1,
            away_score=0,
            live=True,
            event_key="score_change|m_block|0-0->1-0|b",
        )
        ex._buy_blocked_matches.add(mid)
        ex._maybe_clear_buy_block(mid)
        assert mid in ex._buy_blocked_matches

        # A raw DQD score drop is only provisional this round: no auto-flatten
        # until the screenshot gate lands (Odds-confirmed flatten removed).
        rev_mid = "m_odds_gate"
        rev_tid = "tok_odds_gate"
        ex.ledger.register_buy(
            match_id=rev_mid,
            token_id=rev_tid,
            market_key="match_total_0.5_over",
            shares=1.02,
            usdc=1.0,
            home_score=1,
            away_score=0,
            live=False,
            event_key="score_change|m_odds_gate|0-0->1-0",
        )
        reversal_ev = {
            "type": "score_change",
            "match_id": rev_mid,
            "is_reversal": True,
            "prev": {"home": 1, "away": 0},
            "curr": {"home": 0, "away": 0},
            "home_score": 0,
            "away_score": 0,
        }
        assert ex.maybe_flatten_for_event(reversal_ev) == []
        assert len(ex.ledger.open_for_match(rev_mid)) == 1

    # Accepted DELAYED sell: retry ticks reconcile the exact order instead of
    # asset-wide cancel/repost.  Once balance reaches zero, write one settlement
    # row and close the lot without posting another sell.
    class FakeDelayedTrader:
        ready = True

        def __init__(self) -> None:
            self.balance = Decimal("5")
            self.sell_calls = 0
            self.asset_cancel_calls = 0
            self.order_cancel_calls = 0
            self.order_get_calls = 0
            self.order_status = "delayed"

        def refresh_conditional_allowance(self, _token_id: str) -> None:
            return None

        def cancel_orders_for_asset(self, _token_id: str) -> None:
            self.asset_cancel_calls += 1

        def get_conditional_balance(self, _token_id: str) -> Decimal:
            return self.balance

        def post_market_sell(self, *_args, **_kwargs) -> dict:
            self.sell_calls += 1
            return {
                "success": True,
                "status": "delayed",
                "orderID": f"o{self.sell_calls}",
            }

        @staticmethod
        def is_order_success(response: dict) -> bool:
            return bool(response.get("success"))

        def get_order(self, order_id: str) -> dict:
            assert order_id.startswith("o")
            self.order_get_calls += 1
            return {"id": order_id, "status": self.order_status}

        def cancel_order(self, order_id: str) -> dict:
            assert order_id.startswith("o")
            self.order_cancel_calls += 1
            return {"canceled": True}

    with tempfile.TemporaryDirectory() as td:
        import quote_lib

        root = Path(td)
        (root / "data" / "pm-quote").mkdir(parents=True)
        fake = FakeDelayedTrader()
        settings = TradeSettings(
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
            max_usdc=2.0,
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
        ex = TradeExecutor(root, settings, trader=fake)
        mid = "m_delayed_sell"
        tid = "tok_delayed_sell"
        ex.ledger.register_buy(
            match_id=mid,
            token_id=tid,
            market_key="match_total_0.5_over",
            shares=5.0,
            usdc=4.85,
            home_score=1,
            away_score=0,
            live=True,
            event_key="score_change|m_delayed_sell|0-0->1-0",
        )
        lot = ex.ledger.open_for_match(mid)[0]

        # Make persistence deterministic for exact row-count assertions.
        old_append = quote_lib.append_jsonl_async
        quote_lib.append_jsonl_async = quote_lib.append_jsonl
        try:
            first = ex._flatten_lot(
                lot,
                event_key="flatten|m_delayed_sell|confirmed",
                reason="odds_score_reverted",
                match_ev={"match_id": mid},
            )
            assert first["status"] == "flatten_posted"
            assert fake.sell_calls == 1
            assert fake.asset_cancel_calls == 1
            pending = ex.ledger.pending_flatten_lots()
            assert len(pending) == 1
            assert pending[0]["flatten_order_id"] == "o1"
            assert pending[0]["flatten_attempts"] == 1

            for _ in range(5):
                assert ex.retry_pending_flattens() == []
            assert fake.sell_calls == 1
            assert fake.asset_cancel_calls == 1
            assert fake.order_cancel_calls == 0
            # The 250ms watch loop is throttled to one reconciliation query.
            assert fake.order_get_calls == 1
            assert ex.ledger.pending_flatten_lots()[0]["flatten_order_checks"] == 1

            restarted = TradeExecutor(root, settings, trader=fake)
            assert restarted.ledger.pending_flatten_lots()[0]["flatten_order_id"] == "o1"
            assert restarted.retry_pending_flattens() == []
            assert fake.sell_calls == 1
            ex = restarted

            rows = [
                json.loads(line)
                for line in ex.trades_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            assert len(rows) == 1
            assert rows[0]["status"] == "flatten_posted"

            fake.balance = Decimal("0")
            ex.ledger.update_pending_flatten_order(
                tid,
                mid,
                flatten_order_last_checked_at="1970-01-01T00:00:00+00:00",
            )
            settled = ex.retry_pending_flattens()
            assert len(settled) == 1
            assert settled[0]["status"] == "flatten_settled"
            assert fake.sell_calls == 1
            assert fake.asset_cancel_calls == 1
            assert fake.order_cancel_calls == 0
            assert ex.ledger.open_for_match(mid) == []

            rows = [
                json.loads(line)
                for line in ex.trades_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            assert [row["status"] for row in rows] == [
                "flatten_posted",
                "flatten_settled",
            ]
            assert ex.retry_pending_flattens() == []
            assert fake.sell_calls == 1

            # A proven terminal failure may submit exactly one replacement for
            # the still-live residual; that replacement then enters the same
            # wait/reconcile state instead of looping on subsequent ticks.
            fake.balance = Decimal("5")
            fake.order_status = "failed"
            mid2 = "m_failed_sell"
            tid2 = "tok_failed_sell"
            ex.ledger.register_buy(
                match_id=mid2,
                token_id=tid2,
                market_key="match_total_0.5_over",
                shares=5.0,
                usdc=4.85,
                home_score=1,
                away_score=0,
                live=True,
                event_key="score_change|m_failed_sell|0-0->1-0",
            )
            failed_lot = ex.ledger.open_for_match(mid2)[0]
            ex._flatten_lot(
                failed_lot,
                event_key="flatten|m_failed_sell|confirmed",
                reason="odds_score_reverted",
                match_ev={"match_id": mid2},
            )
            assert fake.sell_calls == 2
            replacement = ex.retry_pending_flattens()
            assert len(replacement) == 1
            assert replacement[0]["status"] == "flatten_posted"
            assert fake.sell_calls == 3
            assert fake.asset_cancel_calls == 3
            assert fake.order_cancel_calls == 0
            pending2 = ex.ledger.pending_flatten_lots()
            assert len(pending2) == 1
            assert pending2[0]["flatten_order_id"] == "o3"
            fake.order_status = "delayed"
            assert ex.retry_pending_flattens() == []
            assert fake.sell_calls == 3
        finally:
            quote_lib.append_jsonl_async = old_append

    print(
        "ok: flatten sell haircut + balance-gate parse + entry-20% floor + "
        "buy-block clear + delayed-order reconciliation"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
