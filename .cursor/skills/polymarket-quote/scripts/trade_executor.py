"""Execute fills right after misprice flag (in-process, no JSONL consume)."""

from __future__ import annotations

import json
import logging
import re
import threading
from decimal import ROUND_DOWN, Decimal
from pathlib import Path
from typing import Any

import quote_lib as lib
from clob_trader import ClobTrader
from fill_planner import FillPlan, plan_fill
from score_reversal import (
    OpenPositionLedger,
    entry_tuple,
    event_signals_reversal,
    ft_reversal_vs_entry,
    lot_depends_on_disallowed_goal,
    score_pair,
)
from trade_settings import TradeSettings

logger = logging.getLogger("pm_quote.trade")

# CLOB FAK/FOK sell: maker (shares) max 2 decimals; floor to avoid invalid maker amount.
FLATTEN_SHARE_DECIMALS = 2
FLATTEN_MIN_SHARES = Decimal("0.01")
# Emergency flatten floor price (do not dump into sub-0.2 bids forever).
FLATTEN_MIN_PRICE = Decimal("0.2")
_TERMINAL_FLATTEN_ERR_RE = re.compile(
    r"invalid\s+maker\s+amount|invalid\s+amounts",
    re.IGNORECASE,
)


def trade_idempotency_key(event_key: str, token_id: str, trade: str) -> str:
    return f"{event_key}|{token_id}|{trade}"


def floor_shares(shares: Decimal | float | str, *, decimals: int = FLATTEN_SHARE_DECIMALS) -> Decimal:
    """Round shares down to ``decimals`` (CLOB sell maker precision)."""
    q = Decimal(10) ** -int(decimals)
    d = Decimal(str(shares))
    if d <= 0:
        return Decimal("0")
    return (d / q).to_integral_value(rounding=ROUND_DOWN) * q


def is_terminal_flatten_error(err: str | Exception | None) -> bool:
    """Errors that will not succeed on blind retry (stop pending_flatten loop)."""
    if err is None:
        return False
    return bool(_TERMINAL_FLATTEN_ERR_RE.search(str(err)))


def signal_from_event_key(event_key: str) -> str:
    """First segment of event_key (score_change|… / match_finished|…)."""
    ek = (event_key or "").strip()
    if not ek:
        return ""
    return ek.split("|", 1)[0]


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
        self._flatten_done: set[str] = set()
        self._lock = threading.RLock()
        self.ledger = OpenPositionLedger(lib.data_dir(self.root) / "open_positions.json")
        self._load_recent_successes()
        self._load_recent_flattens()
        live_signals: set[str] = set()
        if self.settings.live_goals:
            live_signals.add("score_change")
        if self.settings.live_ft:
            live_signals.add("match_finished")
        if live_signals:
            both = live_signals >= {"score_change", "match_finished"}
            purged = self.ledger.purge_dry_run_opens_for_signals(
                live_signals,
                reason="pre_live_purge",
                purge_unknown=both,
            )
            if purged:
                logger.warning(
                    "purged %d dry-run open lots before live trading (%s)",
                    purged,
                    ",".join(sorted(live_signals)),
                )
        self._rebuild_open_from_trades()

    def _live_for_signal(self, event_type: str) -> bool:
        """Whether this bridge signal posts real CLOB orders."""
        typ = (event_type or "").strip()
        if typ == "score_change":
            return bool(self.settings.live_goals)
        if typ == "match_finished":
            return bool(self.settings.live_ft)
        return bool(self.settings.live)

    def _resolve_event_type(
        self,
        *,
        event_type: str = "",
        event_key: str = "",
        match_meta: dict[str, Any] | None = None,
    ) -> str:
        typ = (event_type or "").strip()
        if not typ and match_meta:
            typ = str(match_meta.get("event_type") or "").strip()
        if not typ:
            typ = signal_from_event_key(event_key)
        return typ

    def _keep_buy_for_rebuild(self, row: dict[str, Any]) -> bool:
        """Keep buy_win row as open lot for the channel's current dry/live mode."""
        if not row.get("success"):
            return False
        sig = signal_from_event_key(str(row.get("event_key") or ""))
        channel_live = self._live_for_signal(sig)
        if channel_live:
            return (
                row.get("status") == "posted"
                and bool(row.get("live"))
            )
        return row.get("status") in ("dry_run", "posted")

    def _rebuild_open_from_trades(self, limit: int = 800) -> None:
        """Re-open buy_win lots from trades.jsonl that were never flattened (restart-safe)."""
        path = self.trades_path
        if not path.is_file():
            return
        try:
            lines = path.read_text(encoding="utf-8").splitlines()[-limit:]
        except OSError:
            return
        flattened: set[tuple[str, str]] = set()
        buys: list[dict[str, Any]] = []
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
            mid = str(row.get("match_id") or "")
            tid = str(row.get("token_id") or "")
            if row.get("trade") == "flatten_reversal" and row.get("success"):
                if mid and tid:
                    flattened.add((mid, tid))
                continue
            if row.get("trade") != "buy_win":
                continue
            if not self._keep_buy_for_rebuild(row):
                continue
            if "verify" in str(row.get("idempotency_key") or ""):
                continue
            buys.append(row)
        # Last buy per match+token wins
        latest: dict[tuple[str, str], dict[str, Any]] = {}
        for row in buys:
            mid = str(row.get("match_id") or "")
            tid = str(row.get("token_id") or "")
            if mid and tid:
                latest[(mid, tid)] = row
        for (mid, tid), row in latest.items():
            if (mid, tid) in flattened:
                continue
            if any(
                str(x.get("token_id")) == tid
                for x in self.ledger.open_for_match(mid)
            ):
                continue
            plan = row.get("plan") or {}
            shares = float(plan.get("shares") or 0)
            if shares <= 0:
                continue
            self.ledger.register_buy(
                match_id=mid,
                token_id=tid,
                market_key=str(row.get("market_key") or ""),
                shares=shares,
                usdc=float(plan.get("usdc") or 0),
                home_score=row.get("home_score"),
                away_score=row.get("away_score"),
                live=bool(row.get("live")),
                event_key=str(row.get("event_key") or ""),
                home=str(row.get("home") or ""),
                away=str(row.get("away") or ""),
                family=str(row.get("family") or ""),
                tick_size="0.01",
                neg_risk=None,
            )
        self._close_stale_ft_reversed_lots()

    def _close_stale_ft_reversed_lots(self) -> None:
        """Drop zombie opens whose entry score was already undone by known FT."""
        latest_ft: dict[str, tuple[int, int]] = {}
        try:
            for m in lib.load_bridge_matches(self.root):
                dqd = m.get("dongqiudi") or {}
                mid = str(dqd.get("id") or "")
                sc = score_pair(dqd.get("home_score"), dqd.get("away_score"))
                st = str(dqd.get("status") or "").lower()
                finished = bool(dqd.get("is_finished")) or st in (
                    "played",
                    "finished",
                    "ft",
                ) or "play" == st
                # Dongqiudi often uses status display "Played"
                if "played" in st or finished:
                    if mid and sc:
                        latest_ft[mid] = sc
        except Exception as e:  # noqa: BLE001
            logger.warning("stale FT scan matches.json failed: %s", e)

        # Also peek last scores from events.jsonl for finished matches not in matches.json
        try:
            for line in (lib.bridge_dir(self.root) / "events.jsonl").read_text(
                encoding="utf-8"
            ).splitlines()[-2000:]:
                if not line.strip():
                    continue
                try:
                    ev = json.loads(line)
                except Exception:
                    continue
                if (ev.get("type") or "") != "match_finished":
                    continue
                mid = str(ev.get("match_id") or "")
                sc = score_pair(ev.get("home_score"), ev.get("away_score"))
                if mid and sc:
                    latest_ft[mid] = sc
        except OSError:
            pass

        closed = 0
        for lot in list(self.ledger.all_open()):
            mid = str(lot.get("match_id") or "")
            ft = latest_ft.get(mid)
            if ft_reversal_vs_entry(entry=entry_tuple(lot), ft=ft):
                self.ledger.mark_closed(
                    str(lot.get("token_id")),
                    mid,
                    reason=f"stale_ft_reversal ft={ft[0]}-{ft[1]}",
                )
                closed += 1
        if closed:
            logger.info("closed %d stale FT-reversed open lots on rebuild", closed)


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

    def _load_recent_flattens(self, limit: int = 500) -> None:
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
            if row.get("trade") != "flatten_reversal":
                continue
            key = row.get("idempotency_key")
            if key and row.get("status") in ("flatten_dry_run", "flatten_posted") and row.get(
                "success"
            ):
                self._flatten_done.add(str(key))
            # Rebuild open ledger from successful live buys if ledger empty? skip — ledger file is source

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
        event_type: str = "",
    ) -> dict[str, Any] | None:
        """If quote is a misprice opportunity, plan and optionally post."""
        with self._lock:
            return self._maybe_trade_locked(
                quote,
                event_key=event_key,
                match_meta=match_meta,
                event_type=event_type,
            )

    def _maybe_trade_locked(
        self,
        quote: dict[str, Any],
        *,
        event_key: str = "",
        match_meta: dict[str, Any] | None = None,
        event_type: str = "",
    ) -> dict[str, Any] | None:
        """Caller must hold ``self._lock`` (serializes ledger / _done / posts)."""
        if not self.settings.enabled:
            return None
        if not quote.get("misprice"):
            return None

        trade = str(quote.get("trade") or "")
        token_id = str(quote.get("token_id") or "")
        if not trade or not token_id:
            return None

        typ = self._resolve_event_type(
            event_type=event_type,
            event_key=event_key,
            match_meta=match_meta,
        )
        channel_live = self._live_for_signal(typ)

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
                live=channel_live,
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
                    live=channel_live,
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
                    live=channel_live,
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
                live=channel_live,
            )
            return row

        if not channel_live:
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
                live=False,
            )
            self._done.add(key)
            logger.info(
                "dry-run %s %s shares=%.4f usdc=%.4f worst=%.4f depth=%s signal=%s",
                trade,
                token_id[:12],
                plan.shares,
                plan.usdc,
                plan.worst_price,
                plan.take_depth,
                typ or "?",
            )
            if trade == "buy_win":
                self._register_open_buy(
                    quote, plan=plan, event_key=event_key, match_meta=match_meta, live=False
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
            resp_status = (
                str((response or {}).get("status") or "").upper()
                if isinstance(response, dict)
                else ""
            )
            # DELAYED: CLOB accepted — one quick balance peek (no multi-second wait).
            ledger_plan = plan
            if ok and trade == "buy_win" and resp_status == "DELAYED":
                bal = trader.wait_conditional_balance(
                    token_id, min_shares=0.01, timeout_s=0.0, interval_s=0.05
                )
                if bal > 0:
                    ledger_plan = FillPlan(
                        trade=plan.trade,
                        side=plan.side,
                        take_depth=plan.take_depth,
                        order_type=plan.order_type,
                        shares=round(bal, 6),
                        usdc=plan.usdc,
                        worst_price=plan.worst_price,
                        levels_used=plan.levels_used,
                        levels=list(plan.levels),
                        skip_reason=plan.skip_reason,
                    )
                    logger.info(
                        "delayed buy confirmed token=%s… shares=%.4f (plan=%.4f)",
                        token_id[:12],
                        bal,
                        plan.shares,
                    )
                else:
                    logger.warning(
                        "delayed buy accepted but balance still 0 token=%s… "
                        "registering plan shares=%.4f for flatten safety",
                        token_id[:12],
                        plan.shares,
                    )
            row = self._record(
                quote,
                event_key=event_key,
                match_meta=match_meta,
                plan=ledger_plan,
                status="posted",
                skip_reason=(
                    f"delayed|{resp_status.lower()}" if resp_status == "DELAYED" else None
                ),
                response=response,
                success=ok,
                idempotency_key=key,
                live=True,
            )
            if ok:
                self._done.add(key)
                if trade == "buy_win":
                    # Must register even when delayed — otherwise score-reversal
                    # flatten never sees the lot (Alianza Over 4.5 bug).
                    self._register_open_buy(
                        quote,
                        plan=ledger_plan,
                        event_key=event_key,
                        match_meta=match_meta,
                        live=True,
                    )
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
                live=True,
            )
            return row

    def _register_open_buy(
        self,
        quote: dict[str, Any],
        *,
        plan: FillPlan,
        event_key: str,
        match_meta: dict[str, Any] | None,
        live: bool,
    ) -> None:
        meta = match_meta or {}
        mid = str(meta.get("match_id") or quote.get("match_id") or "")
        if not mid:
            return
        neg = quote.get("neg_risk")
        self.ledger.register_buy(
            match_id=mid,
            token_id=str(quote.get("token_id") or ""),
            market_key=str(quote.get("market_key") or ""),
            shares=float(plan.shares),
            usdc=float(plan.usdc),
            home_score=meta.get("home_score"),
            away_score=meta.get("away_score"),
            live=live,
            event_key=event_key,
            home=str(meta.get("home") or ""),
            away=str(meta.get("away") or ""),
            family=str(quote.get("family") or ""),
            tick_size=str(quote.get("tick_size") or "0.01"),
            neg_risk=bool(neg) if neg is not None else None,
        )

    def maybe_flatten_for_event(self, ev: dict[str, Any]) -> list[dict[str, Any]]:
        """Flatten only lots that depended on a disallowed goal (entry > after score)."""
        with self._lock:
            return self._maybe_flatten_for_event_locked(ev)

    def _maybe_flatten_for_event_locked(self, ev: dict[str, Any]) -> list[dict[str, Any]]:
        if not self.settings.enabled:
            return []
        mid = str(ev.get("match_id") or "")
        if not mid:
            return []

        open_lots = self.ledger.open_for_match(mid)
        if not open_lots:
            return []

        typ = ev.get("type") or ""
        after: tuple[int, int] | None = None
        reason = ""

        if typ == "score_change" and event_signals_reversal(ev):
            after = score_pair(ev.get("home_score"), ev.get("away_score"))
            prev = ev.get("prev") or {}
            reason = (
                f"score_reversal "
                f"{prev.get('home')}-{prev.get('away')}→"
                f"{ev.get('home_score')}-{ev.get('away_score')}"
            )
        elif typ == "match_finished":
            after = score_pair(ev.get("home_score"), ev.get("away_score"))
            reason = (
                f"ft_reversal_vs_entry ft={ev.get('home_score')}-{ev.get('away_score')}"
            )
        else:
            return []

        affected = [
            lot
            for lot in open_lots
            if lot_depends_on_disallowed_goal(lot, after_score=after)
        ]
        if not affected:
            return []

        logger.warning(
            "reversal flatten match=%s lots=%d/%d reason=%s",
            mid,
            len(affected),
            len(open_lots),
            reason,
        )
        out: list[dict[str, Any]] = []
        ek = f"flatten|{mid}|{ev.get('ts') or lib.now_cn_iso()}|{reason}"
        for lot in affected:
            out.append(self._flatten_lot(lot, event_key=ek, reason=reason, match_ev=ev))
        return out

    def retry_pending_flattens(self) -> list[dict[str, Any]]:
        """Retry live FAK exits that failed earlier (lot still open + pending_flatten)."""
        with self._lock:
            return self._retry_pending_flattens_locked()

    def _retry_pending_flattens_locked(self) -> list[dict[str, Any]]:
        if not self.settings.enabled:
            return []
        pending = self.ledger.pending_flatten_lots()
        if not pending:
            return []
        out: list[dict[str, Any]] = []
        for lot in pending:
            mid = str(lot.get("match_id") or "")
            tid = str(lot.get("token_id") or "")
            reason = str(lot.get("pending_reason") or "retry_pending_flatten")
            # Stop looping terminal CLOB amount errors left in the ledger.
            if is_terminal_flatten_error(reason):
                self.ledger.mark_closed(
                    tid, mid, reason="terminal_flatten_error|" + reason[:180]
                )
                alert = (
                    f"ALERT flatten_give_up match={mid} token={tid[:12]}… "
                    f"terminal prior error — stopped retry"
                )
                logger.error(alert)
                print(alert, flush=True)
                out.append(
                    {
                        "quoted_at": lib.now_cn_iso(),
                        "status": "flatten_abandoned",
                        "skip_reason": reason[:300],
                        "trade": "flatten_reversal",
                        "match_id": mid,
                        "token_id": tid,
                    }
                )
                continue
            ek = f"flatten_retry|{mid}|{lib.now_cn_iso()}|{reason}"
            logger.error(
                "ALERT flatten_retry match=%s token=%s… attempt=%s reason=%s",
                mid,
                tid[:12],
                lot.get("flatten_attempts"),
                reason,
            )
            out.append(
                self._flatten_lot(
                    lot,
                    event_key=ek,
                    reason=reason,
                    match_ev={
                        "match_id": mid,
                        "home": lot.get("home"),
                        "away": lot.get("away"),
                        "home_score": (lot.get("entry_score") or [None, None])[0]
                        if isinstance(lot.get("entry_score"), list)
                        else None,
                        "away_score": (lot.get("entry_score") or [None, None])[1]
                        if isinstance(lot.get("entry_score"), list)
                        else None,
                    },
                )
            )
        return out

    def _flatten_lot(
        self,
        lot: dict[str, Any],
        *,
        event_key: str,
        reason: str,
        match_ev: dict[str, Any],
    ) -> dict[str, Any]:
        token_id = str(lot.get("token_id") or "")
        mid = str(lot.get("match_id") or "")
        # Success-only idempotency: failures stay retryable (new retry keys / pending).
        key = f"{event_key}|{token_id}|flatten_reversal"
        if key in self._flatten_done:
            return {
                "idempotency_key": key,
                "status": "skipped",
                "skip_reason": "already_flattened",
                "trade": "flatten_reversal",
            }

        planned_shares = float(lot.get("shares") or 0)
        meta = {
            "match_id": mid,
            "home": lot.get("home") or match_ev.get("home") or "",
            "away": lot.get("away") or match_ev.get("away") or "",
            "home_score": match_ev.get("home_score"),
            "away_score": match_ev.get("away_score"),
        }
        quote_stub = {
            "market_key": lot.get("market_key"),
            "family": lot.get("family"),
            "token_id": token_id,
            "trade": "flatten_reversal",
            "settlement": "REVERSAL",
            "tick_size": lot.get("tick_size") or "0.01",
            "neg_risk": lot.get("neg_risk"),
        }

        # Dry-run lots: log intent and close ledger (never post CLOB for simulated buys).
        lot_live = bool(lot.get("live"))
        if not lot_live:
            plan = FillPlan(
                trade="flatten_reversal",
                side="SELL",
                take_depth="emergency",
                order_type="FAK",
                shares=planned_shares,
                usdc=0.0,
                worst_price=0.0,
                levels_used=0,
                levels=[],
                skip_reason=None,
            )
            row = self._record(
                quote_stub,
                event_key=event_key,
                match_meta=meta,
                plan=plan,
                status="flatten_dry_run",
                skip_reason=reason,
                response=None,
                success=True,
                idempotency_key=key,
                live=False,
            )
            self._flatten_done.add(key)
            self.ledger.mark_closed(token_id, mid, reason=reason)
            logger.info(
                "flatten dry-run match=%s token=%s… shares=%.4f (%s)",
                mid,
                token_id[:12],
                planned_shares,
                reason,
            )
            return row

        # Live FAK sell: floor shares to 2dp (CLOB maker precision), min_price=0.2.
        try:
            trader = self.ensure_trader()
            if trader is None:
                raise RuntimeError("no trader for flatten")
            bal = Decimal(str(trader.get_conditional_balance(token_id)))
            shares = floor_shares(bal)
            if bal <= 0 or shares < FLATTEN_MIN_SHARES:
                # Dust / empty: close ledger and stop retrying.
                dust_reason = (
                    f"{reason}|dust_bal={bal:.6f}|floor={shares}"
                    if bal > 0
                    else reason + "|empty"
                )
                row = self._record(
                    quote_stub,
                    event_key=event_key,
                    match_meta=meta,
                    plan=None,
                    status="flatten_skipped",
                    skip_reason=(
                        "flatten_dust" if bal > 0 else "no_position_on_flatten"
                    ),
                    response=None,
                    success=True if bal > 0 else False,
                    idempotency_key=key,
                    live=True,
                )
                self._flatten_done.add(key)
                self.ledger.mark_closed(token_id, mid, reason=dust_reason)
                if bal > 0:
                    alert = (
                        f"ALERT flatten_dust match={mid} token={token_id[:12]}… "
                        f"bal={bal} floor={shares} < {FLATTEN_MIN_SHARES} — closed"
                    )
                    logger.warning(alert)
                    print(alert, flush=True)
                return row
            tick = str(lot.get("tick_size") or "0.01") or "0.01"
            neg = lot.get("neg_risk")
            neg_risk = bool(neg) if neg is not None else None
            response = trader.post_market_sell(
                token_id,
                shares,
                tick,
                min_price=FLATTEN_MIN_PRICE,
                order_type="FAK",
                neg_risk=neg_risk,
            )
            ok = trader.is_order_success(response)
            # Confirm residual — FAK may partial-fill; dust residual closes.
            residual = Decimal("-1")
            try:
                residual = Decimal(str(trader.get_conditional_balance(token_id)))
            except Exception:  # noqa: BLE001
                residual = Decimal("-1")
            residual_floor = (
                floor_shares(residual) if residual >= 0 else Decimal("-1")
            )
            fully_flat = ok and residual >= 0 and residual_floor < FLATTEN_MIN_SHARES

            plan = FillPlan(
                trade="flatten_reversal",
                side="SELL",
                take_depth="emergency",
                order_type="FAK",
                shares=float(shares),
                usdc=0.0,
                worst_price=float(FLATTEN_MIN_PRICE),
                levels_used=0,
                levels=[],
                skip_reason=None,
            )
            row = self._record(
                quote_stub,
                event_key=event_key,
                match_meta=meta,
                plan=plan,
                status="flatten_posted",
                skip_reason=reason
                + (
                    f"|residual={float(residual):.4f}"
                    if residual > FLATTEN_MIN_SHARES
                    else ""
                ),
                response=response,
                success=fully_flat,
                idempotency_key=key,
                live=True,
            )
            if fully_flat:
                self._flatten_done.add(key)
                close_r = reason
                if residual > 0:
                    close_r += f"|dust_residual={residual}"
                self.ledger.mark_closed(token_id, mid, reason=close_r)
            else:
                alert = (
                    f"ALERT flatten_incomplete match={mid} token={token_id[:12]}… "
                    f"sold≈{shares} residual={residual} ok={ok} — will retry"
                )
                logger.error(alert)
                print(alert, flush=True)
                self.ledger.mark_pending_flatten(token_id, mid, reason=reason)
            return row
        except Exception as e:  # noqa: BLE001
            terminal = is_terminal_flatten_error(e)
            alert = (
                f"ALERT flatten_failed match={mid} token={token_id[:12]}… "
                f"err={e} — "
                + ("stopped retry (terminal)" if terminal else "will retry")
            )
            logger.exception(alert)
            print(alert, flush=True)
            if terminal:
                self._flatten_done.add(key)
                self.ledger.mark_closed(
                    token_id, mid, reason=f"{reason}|terminal|{e}"[:240]
                )
            else:
                self.ledger.mark_pending_flatten(
                    token_id, mid, reason=f"{reason}|err={e}"
                )
            return self._record(
                quote_stub,
                event_key=event_key,
                match_meta=meta,
                plan=None,
                status="flatten_error" if not terminal else "flatten_abandoned",
                skip_reason=str(e),
                response=None,
                success=False,
                idempotency_key=key,
                live=True,
            )

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
        live: bool | None = None,
    ) -> dict[str, Any]:
        meta = match_meta or {}
        row: dict[str, Any] = {
            "quoted_at": lib.now_cn_iso(),
            "status": status,
            "success": success,
            "live": bool(live) if live is not None else bool(self.settings.live),
            "idempotency_key": idempotency_key,
            "event_key": event_key,
            "match_id": meta.get("match_id") or quote.get("match_id") or "",
            "home": meta.get("home") or "",
            "away": meta.get("away") or "",
            "home_score": meta.get("home_score"),
            "away_score": meta.get("away_score"),
            "event_type": meta.get("event_type")
            or signal_from_event_key(event_key)
            or "",
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
        lib.append_jsonl_async(self.trades_path, [row])
        return row
