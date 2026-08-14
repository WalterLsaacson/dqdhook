#!/usr/bin/env python3
"""Smoke: flatten sell sizing + balance-gate error parse."""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from trade_executor import (  # noqa: E402
    FLATTEN_MAX_LOSS_FRAC,
    FLATTEN_MIN_PRICE,
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

    # Entry−10% floor (no 0.01 panic dump).
    assert FLATTEN_MAX_LOSS_FRAC == Decimal("0.10")
    lot = {"shares": "20.618557", "usdc": "20.0", "tick_size": "0.01"}
    assert abs(lot_entry_price(lot) - Decimal("0.97")) < Decimal("0.001")
    # 0.97 * 0.90 = 0.873 → tick floor 0.87
    assert flatten_min_sell_price(lot) == Decimal("0.87")
    assert flatten_min_sell_price({"ask": "0.50"}) == Decimal("0.45")
    assert flatten_min_sell_price({}) == FLATTEN_MIN_PRICE

    # Delayed fill: plan shares understate live bal → cheaper VWAP → lower floor.
    delayed = {"shares": 20.618557, "usdc": 20.0, "fill_status": "pending_fill"}
    assert reconcile_lot_inventory(delayed, Decimal("25.0"))
    assert delayed["shares"] == 25.0
    assert delayed["usdc"] == 20.0
    assert abs(lot_entry_price(delayed) - Decimal("0.8")) < Decimal("0.001")
    assert flatten_min_sell_price(delayed) == Decimal("0.72")  # 0.8*0.90=0.72

    # Residual after partial sell: scale usdc so VWAP (and floor) hold.
    residual = {"shares": 20.0, "usdc": 19.4}  # entry 0.97
    assert reconcile_lot_inventory(residual, Decimal("10.0"))
    assert residual["shares"] == 10.0
    assert abs(float(residual["usdc"]) - 9.7) < 1e-9
    assert abs(lot_entry_price(residual) - Decimal("0.97")) < Decimal("0.001")
    assert flatten_min_sell_price(residual) == Decimal("0.87")

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
        ex = TradeExecutor(root, settings, af_mode="gate")
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

    print("ok: flatten sell haircut + balance-gate parse + entry-10% floor + buy-block clear")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
