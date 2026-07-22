"""Thin py-clob-client-v2 wrapper (mirrors simple_str ClobExecutionClient)."""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any

from eth_account import Account

from trade_settings import TradeSettings

logger = logging.getLogger("pm_quote.clob")

MIN_ORDER_SIZE = Decimal("5")


class ClobTrader:
    """Auth once, reuse for market FOK/FAK orders."""

    def __init__(self, settings: TradeSettings) -> None:
        self._settings = settings
        self._client: Any = None
        self._wallet_address: str = ""

    def initialize(self) -> None:
        from py_clob_client_v2 import ClobClient

        if not self._settings.private_key:
            raise RuntimeError("PRIVATE_KEY required for ClobTrader.initialize()")

        kwargs: dict[str, Any] = {
            "host": self._settings.clob_host,
            "key": self._settings.private_key,
            "chain_id": self._settings.chain_id,
            "signature_type": self._settings.signature_type,
        }
        if self._settings.funder:
            kwargs["funder"] = self._settings.funder

        client = ClobClient(**kwargs)
        try:
            creds = client.derive_api_key()
            logger.debug("CLOB API key derived")
        except Exception:
            creds = client.create_api_key()
            logger.info("CLOB API key created (first registration)")
        client.set_api_creds(creds)
        self._client = client
        self._wallet_address = self._resolve_wallet_address()
        logger.info("CLOB ready wallet=%s…", self._wallet_address[:10])

    def _resolve_wallet_address(self) -> str:
        if self._settings.funder:
            return self._settings.funder
        get_addr = getattr(self.client, "get_address", None)
        if callable(get_addr):
            return str(get_addr())
        return Account.from_key(self._settings.private_key).address

    @property
    def wallet_address(self) -> str:
        return self._wallet_address

    @property
    def client(self) -> Any:
        if self._client is None:
            raise RuntimeError("CLOB client not initialized; call initialize()")
        return self._client

    @property
    def ready(self) -> bool:
        return self._client is not None

    def get_conditional_balance(self, token_id: str) -> Decimal:
        from py_clob_client_v2.clob_types import AssetType, BalanceAllowanceParams

        result = self.client.get_balance_allowance(
            BalanceAllowanceParams(asset_type=AssetType.CONDITIONAL, token_id=token_id)
        )
        return Decimal(str(result.get("balance", "0"))) / Decimal("1000000")

    @staticmethod
    def is_order_success(result: dict | None) -> bool:
        if not result:
            return False
        status = str(result.get("status", "")).upper()
        if status in ("MATCHED", "FILLED", "LIVE", "OPEN", "SUCCESS"):
            return True
        if result.get("success") is True:
            return True
        if result.get("orderID") or result.get("id"):
            err = result.get("errorMsg") or result.get("error")
            return not err
        return False

    def post_market_buy(
        self,
        token_id: str,
        amount_usdc: Decimal,
        tick_size: str,
        max_price: Decimal,
        *,
        order_type: str = "FOK",
        neg_risk: bool | None = None,
    ) -> dict:
        from py_clob_client_v2 import MarketOrderArgs, OrderType, Side
        from py_clob_client_v2.clob_types import PartialCreateOrderOptions

        if max_price is None or max_price <= 0:
            raise ValueError("BUY requires max_price")
        ot = OrderType.FOK if str(order_type).upper() == "FOK" else OrderType.FAK
        options = PartialCreateOrderOptions(tick_size=tick_size)
        if neg_risk is not None:
            options = PartialCreateOrderOptions(tick_size=tick_size, neg_risk=bool(neg_risk))
        args = MarketOrderArgs(
            token_id=token_id,
            amount=float(amount_usdc),
            side=Side.BUY,
            order_type=ot,
            price=float(max_price),
        )
        result = self.client.create_and_post_market_order(
            args, options=options, order_type=ot
        )
        out = result if isinstance(result, dict) else {"result": result}
        logger.info(
            "market BUY %s token=%s usdc=%s max_price=%s ok=%s",
            ot.name,
            token_id[:12],
            amount_usdc,
            max_price,
            self.is_order_success(out),
        )
        return out

    def post_market_sell(
        self,
        token_id: str,
        shares: Decimal,
        tick_size: str,
        *,
        min_price: Decimal | None = None,
        order_type: str = "FAK",
        neg_risk: bool | None = None,
    ) -> dict:
        from py_clob_client_v2 import MarketOrderArgs, OrderType, Side
        from py_clob_client_v2.clob_types import PartialCreateOrderOptions

        ot = OrderType.FOK if str(order_type).upper() == "FOK" else OrderType.FAK
        options = PartialCreateOrderOptions(tick_size=tick_size)
        if neg_risk is not None:
            options = PartialCreateOrderOptions(tick_size=tick_size, neg_risk=bool(neg_risk))
        kwargs: dict[str, Any] = {
            "token_id": token_id,
            "amount": float(shares),
            "side": Side.SELL,
            "order_type": ot,
        }
        if min_price is not None and min_price > 0:
            kwargs["price"] = float(min_price)
        args = MarketOrderArgs(**kwargs)
        result = self.client.create_and_post_market_order(
            args, options=options, order_type=ot
        )
        out = result if isinstance(result, dict) else {"result": result}
        logger.info(
            "market SELL %s token=%s shares=%s min_price=%s ok=%s",
            ot.name,
            token_id[:12],
            shares,
            min_price,
            self.is_order_success(out),
        )
        return out
