"""Execute fills right after misprice flag (in-process, no JSONL consume)."""

from __future__ import annotations

import json
import logging
from decimal import Decimal
from pathlib import Path
from typing import Any

import quote_lib as lib
from clob_trader import ClobTrader
from fill_planner import FillPlan, plan_fill
from trade_settings import TradeSettings

logger = logging.getLogger("pm_quote.trade")


def trade_idempotency_key(event_key: str, token_id: str, trade: str) -> str:
    return f"{event_key}|{token_id}|{trade}"


class TradeExecutor:
    """Plan → optional post → trades.jsonl; memory + file idempotency."""

    def __init__(
        self,
        root: Path,
        settings: TradeSettings,
        *,
        trader: ClobTrader | None = None,
    ) -> None:
        self.root = Path(root)
        self.settings = settings
        self.trader = trader
        self._done: set[str] = set()
        self._load_recent_successes()

    @property
    def trades_path(self) -> Path:
        return lib.data_dir(self.root) / "trades.jsonl"

    def _load_recent_successes(self, limit: int = 500) -> None:
        path = self.trades_path
        if not path.is_file():
            return
        try:
            lines = path.read_text(encoding="utf-8").splitlines()[-limit:]
        except OSError:
            return
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except Exception:
                continue
            if not isinstance(row, dict):
                continue
            # Only live successful posts block future attempts across restarts.
            if (
                row.get("status") == "posted"
                and row.get("success")
                and row.get("idempotency_key")
            ):
                self._done.add(str(row["idempotency_key"]))

    def ensure_trader(self) -> ClobTrader | None:
        """Initialize once and reuse (plan: watch 启动时 initialize)."""
        if not self.settings.private_key:
            return self.trader if self.trader and self.trader.ready else None
        if self.trader is None:
            self.trader = ClobTrader(self.settings)
        if not self.trader.ready:
            self.trader.initialize()
        return self.trader

    def _extreme_price_blocked(self, price: float | None) -> str | None:
        if self.settings.allow_extreme_prices or price is None:
            return None
        if price <= 0.01 + 1e-12:
            return f"extreme_price={price} (<=0.01)"
        if price >= 0.99 - 1e-12:
            return f"extreme_price={price} (>=0.99)"
        return None

    def maybe_trade(
        self,
        quote: dict[str, Any],
        *,
        event_key: str = "",
        match_meta: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """If quote is a misprice opportunity, plan and optionally post."""
        if not self.settings.enabled:
            return None
        if not quote.get("misprice"):
            return None

        trade = str(quote.get("trade") or "")
        token_id = str(quote.get("token_id") or "")
        if not trade or not token_id:
            return None

        key = trade_idempotency_key(event_key or "", token_id, trade)
        if key in self._done:
            logger.debug("skip duplicate %s", key)
            return {
                "idempotency_key": key,
                "status": "skipped",
                "skip_reason": "already_done",
            }

        # Price guard on the reference book price (best ask/bid)
        ref_price = quote.get("best_ask") if trade == "buy_win" else quote.get("best_bid")
        try:
            ref_f = float(ref_price) if ref_price is not None else None
        except (TypeError, ValueError):
            ref_f = None
        extreme = self._extreme_price_blocked(ref_f)
        if extreme:
            row = self._record(
                quote,
                event_key=event_key,
                match_meta=match_meta,
                plan=None,
                status="skipped",
                skip_reason=extreme,
                response=None,
                success=False,
                idempotency_key=key,
            )
            return row

        available: float | None = None
        if trade == "sell_lose":
            available = self._position_shares(token_id)
            if available is None:
                row = self._record(
                    quote,
                    event_key=event_key,
                    match_meta=match_meta,
                    plan=None,
                    status="skipped",
                    skip_reason="no_position_query",
                    response=None,
                    success=False,
                    idempotency_key=key,
                )
                return row
            if available <= 0:
                row = self._record(
                    quote,
                    event_key=event_key,
                    match_meta=match_meta,
                    plan=None,
                    status="skipped",
                    skip_reason="no_position",
                    response=None,
                    success=False,
                    idempotency_key=key,
                )
                return row

        plan = plan_fill(
            quote,
            take_depth=self.settings.take_depth,
            max_levels=self.settings.max_levels,
            max_usdc=self.settings.max_usdc,
            max_shares=self.settings.max_shares,
            max_slippage=self.settings.max_slippage,
            min_order_shares=self.settings.min_order_shares,
            available_shares=available,
        )

        if plan.skip_reason:
            row = self._record(
                quote,
                event_key=event_key,
                match_meta=match_meta,
                plan=plan,
                status="skipped",
                skip_reason=plan.skip_reason,
                response=None,
                success=False,
                idempotency_key=key,
            )
            return row

        if not self.settings.live:
            row = self._record(
                quote,
                event_key=event_key,
                match_meta=match_meta,
                plan=plan,
                status="dry_run",
                skip_reason=None,
                response=None,
                success=True,
                idempotency_key=key,
            )
            self._done.add(key)
            logger.info(
                "dry-run %s %s shares=%.4f usdc=%.4f worst=%.4f depth=%s",
                trade,
                token_id[:12],
                plan.shares,
                plan.usdc,
                plan.worst_price,
                plan.take_depth,
            )
            return row

        # Live post
        try:
            trader = self.ensure_trader()
            assert trader is not None
            tick = str(quote.get("tick_size") or "0.01") or "0.01"
            neg = quote.get("neg_risk")
            neg_risk = bool(neg) if neg is not None else None
            if plan.side == "BUY":
                response = trader.post_market_buy(
                    token_id,
                    Decimal(str(plan.usdc)),
                    tick,
                    Decimal(str(plan.worst_price)),
                    order_type=plan.order_type,
                    neg_risk=neg_risk,
                )
            else:
                response = trader.post_market_sell(
                    token_id,
                    Decimal(str(plan.shares)),
                    tick,
                    min_price=Decimal(str(plan.worst_price)),
                    order_type=plan.order_type,
                    neg_risk=neg_risk,
                )
            ok = trader.is_order_success(response)
            row = self._record(
                quote,
                event_key=event_key,
                match_meta=match_meta,
                plan=plan,
                status="posted",
                skip_reason=None,
                response=response,
                success=ok,
                idempotency_key=key,
            )
            if ok:
                self._done.add(key)
            return row
        except Exception as e:  # noqa: BLE001
            logger.exception("live order failed: %s", e)
            row = self._record(
                quote,
                event_key=event_key,
                match_meta=match_meta,
                plan=plan,
                status="error",
                skip_reason=str(e),
                response=None,
                success=False,
                idempotency_key=key,
            )
            return row

    def _position_shares(self, token_id: str) -> float | None:
        """Available conditional balance.

        Plan: sell_lose must check position; skip if insufficient.
        Returns None when the balance API cannot be queried (caller should skip).
        """
        try:
            trader = self.ensure_trader()
            if trader is None:
                return None
            bal = trader.get_conditional_balance(token_id)
            return float(bal)
        except Exception as e:  # noqa: BLE001
            logger.warning("position query failed token=%s: %s", token_id[:12], e)
            return None

    def _record(
        self,
        quote: dict[str, Any],
        *,
        event_key: str,
        match_meta: dict[str, Any] | None,
        plan: FillPlan | None,
        status: str,
        skip_reason: str | None,
        response: dict | None,
        success: bool,
        idempotency_key: str,
    ) -> dict[str, Any]:
        meta = match_meta or {}
        row: dict[str, Any] = {
            "quoted_at": lib.now_cn_iso(),
            "status": status,
            "success": success,
            "live": self.settings.live,
            "idempotency_key": idempotency_key,
            "event_key": event_key,
            "match_id": meta.get("match_id") or quote.get("match_id") or "",
            "home": meta.get("home") or "",
            "away": meta.get("away") or "",
            "home_score": meta.get("home_score"),
            "away_score": meta.get("away_score"),
            "market_key": quote.get("market_key"),
            "family": quote.get("family"),
            "outcome": quote.get("outcome"),
            "settlement": quote.get("settlement"),
            "token_id": quote.get("token_id"),
            "trade": quote.get("trade"),
            "net_edge": quote.get("net_edge"),
            "gross_edge": quote.get("gross_edge"),
            "fee": quote.get("fee"),
            "best_bid": quote.get("best_bid"),
            "best_ask": quote.get("best_ask"),
            "take_depth": self.settings.take_depth,
            "plan": plan.to_dict() if plan else None,
            "skip_reason": skip_reason,
            "response": response,
        }
        lib.append_jsonl(self.trades_path, [row])
        return row
