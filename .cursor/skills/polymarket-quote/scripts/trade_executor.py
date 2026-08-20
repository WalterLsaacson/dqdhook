"""Execute fills right after misprice flag (in-process, no JSONL consume)."""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from datetime import datetime
from decimal import ROUND_DOWN, Decimal
from pathlib import Path
from typing import Any

import quote_lib as lib
from clob_trader import ClobTrader
from fill_planner import FillPlan, plan_fill, MIN_MARKETABLE_BUY_USDC
from rest_ladder import (
    MIN_REST_USDC,
    allocate_rest_ladder,
    ask_in_fak_zone,
    rest_expire_s,
    rest_limit_tick_size,
)
from size_policy import compute_buy_size_caps
from score_reversal import (
    FILL_STATUS_OPEN,
    FILL_STATUS_PENDING,
    OpenPositionLedger,
    entry_tuple,
    event_signals_reversal,
    ft_reversal_vs_entry,
    lot_depends_on_disallowed_goal,
    reconcile_lot_inventory,
    rest_order_is_live,
    rest_order_working_usdc,
    score_pair,
    iso_now,
    parse_iso,
)
from trade_settings import TradeSettings

logger = logging.getLogger("pm_quote.trade")

# CLOB FAK/FOK sell: maker (shares) max 2 decimals; floor to avoid invalid maker amount.
FLATTEN_SHARE_DECIMALS = 2
FLATTEN_MIN_SHARES = Decimal("0.01")
# Fallback floor only when entry price is unknown (never dump at 0.01).
FLATTEN_MIN_PRICE = Decimal("0.5")
# Max loss vs entry on emergency flatten: min_sell = entry * (1 - this).
# Thin books that cannot fill leave residual for later retry ticks.
FLATTEN_MAX_LOSS_FRAC = Decimal("0.20")
# Polymarket matched-order cache often rejects 100% sells; keep a haircut.
FLATTEN_SELL_HAIRCUT = Decimal("0.99")
# Live bal vs gate bal: within this → trust gate "free"; else size from live only.
FLATTEN_GATE_BAL_EPS = Decimal("0.02")
# Hard stop: zombie pending_flatten loops (resolved markets / never-filled buys).
FLATTEN_MAX_ATTEMPTS = 60
# Delayed buy never shows balance → abandon (don't retry forever).
FLATTEN_DELAYED_FILL_MAX_ATTEMPTS = 30
# Accepted asynchronous sell orders get one settlement window.  During this
# period retry ticks only reconcile order/balance state; they never cancel and
# repost the same shares.  A single-order cancel/retry is allowed after timeout.
FLATTEN_ORDER_SETTLE_GRACE_S = 30.0
FLATTEN_ORDER_MAX_WAIT_S = 60.0
# The watch loop ticks at 250ms; do not turn one pending order into an API poll
# storm while waiting for the exchange's balance/order views to converge.
FLATTEN_ORDER_RECHECK_INTERVAL_S = 2.0
REST_ORDER_RECHECK_INTERVAL_S = 2.0
MIN_WIN_BEST_BID = 0.85
CUSHION_REST_USDC = 10.0
CUSHION_REST_PRICES = (0.99,)
# Keep pending_reason bounded (append loops used to grow to 80KB+).
FLATTEN_REASON_MAX_LEN = 400
_TERMINAL_FLATTEN_ERR_RE = re.compile(
    r"invalid\s+maker\s+amount|invalid\s+amounts|invalid\s+token\s+id",
    re.IGNORECASE,
)
_BALANCE_GATE_RE = re.compile(
    r"balance:\s*(\d+).*?sum of matched orders:\s*(\d+).*?order amount[^:]*:\s*(\d+)",
    re.IGNORECASE | re.DOTALL,
)
_NOT_ENOUGH_BAL_RE = re.compile(
    r"not enough balance\s*/\s*allowance",
    re.IGNORECASE,
)


def best_bid_clears_min(best_bid: Any, floor: float = MIN_WIN_BEST_BID) -> bool:
    """Live buy_win requires a visible bid at or above the floor (missing bid fails)."""
    if best_bid is None or best_bid == "":
        return False
    try:
        return float(best_bid) + 1e-12 >= float(floor)
    except (TypeError, ValueError):
        return False


def quote_reversal_cushion(quote: dict[str, Any] | None) -> bool:
    raw = (quote or {}).get("reversal_cushion")
    if raw is True:
        return True
    if raw is False or raw is None:
        return False
    return str(raw).strip().lower() in {"1", "true", "yes"}


def flatten_order_id(response: Any) -> str:
    if not isinstance(response, dict):
        return ""
    direct = str(
        response.get("orderID")
        or response.get("order_id")
        or response.get("id")
        or ""
    )
    if direct:
        return direct
    for key in ("order", "result", "data"):
        nested = response.get(key)
        if isinstance(nested, dict):
            found = flatten_order_id(nested)
            if found:
                return found
    return ""


def _rest_remote_filled_shares(remote: dict[str, Any], order: dict[str, Any]) -> float:
    for key in ("size_matched", "sizeMatched", "matched_shares", "takingAmount"):
        raw = remote.get(key)
        try:
            if raw is not None and str(raw) != "":
                val = float(raw)
                if val >= 0:
                    return val
        except (TypeError, ValueError):
            continue
    orig = float(order.get("shares") or 0)
    status = str(remote.get("status") or "").upper()
    if status in ("MATCHED", "FILLED") and orig > 0:
        return orig
    return float(order.get("filled_shares") or 0)


def _rest_remote_filled_usdc(
    remote: dict[str, Any], order: dict[str, Any], filled_shares: float
) -> float:
    for key in ("makingAmount", "matched_usdc", "size_matched_usdc"):
        raw = remote.get(key)
        try:
            if raw is not None and str(raw) != "":
                val = float(raw)
                if val >= 0:
                    return val
        except (TypeError, ValueError):
            continue
    px = float(order.get("price") or 0)
    if filled_shares > 0 and px > 0:
        return round(filled_shares * px, 6)
    return float(order.get("filled_usdc") or 0)


def flatten_order_status(response: Any) -> str:
    if not isinstance(response, dict):
        return ""
    value = response.get("status") or response.get("orderStatus")
    if value is None:
        for key in ("order", "result", "data"):
            nested = response.get(key)
            if isinstance(nested, dict):
                value = flatten_order_status(nested)
                if value:
                    break
    return str(value or "").strip().upper()


def flatten_cancel_ack(response: Any, order_id: str) -> bool:
    """True only when a single-order cancel response positively acknowledges it."""
    if response is True:
        return True
    if not isinstance(response, dict):
        return False
    canceled = response.get("canceled")
    if canceled is True:
        return True
    if isinstance(canceled, (list, tuple, set)):
        return str(order_id) in {str(value) for value in canceled}
    status = str(response.get("status") or "").strip().upper()
    return status in {"CANCELED", "CANCELLED", "SUCCESS"}


def trade_idempotency_key(event_key: str, token_id: str, trade: str) -> str:
    return f"{event_key}|{token_id}|{trade}"


def actual_matched_buy_plan(plan: FillPlan, response: Any) -> FillPlan:
    """Use CLOB matched making/taking amounts instead of the requested FAK size."""
    if not isinstance(response, dict):
        return plan
    status = str(response.get("status") or "").upper()
    if status not in ("MATCHED", "FILLED", "SUCCESS"):
        return plan
    try:
        shares = float(response.get("takingAmount") or 0)
        usdc = float(response.get("makingAmount") or 0)
    except (TypeError, ValueError):
        return plan
    if shares <= 0.0 or usdc <= 0.0:
        return plan
    return FillPlan(
        trade=plan.trade,
        side=plan.side,
        take_depth=plan.take_depth,
        order_type=plan.order_type,
        shares=round(shares, 6),
        usdc=round(usdc, 6),
        worst_price=plan.worst_price,
        levels_used=plan.levels_used,
        levels=list(plan.levels),
        skip_reason=plan.skip_reason,
    )


def floor_shares(shares: Decimal | float | str, *, decimals: int = FLATTEN_SHARE_DECIMALS) -> Decimal:
    """Round shares down to ``decimals`` (CLOB sell maker precision)."""
    q = Decimal(10) ** -int(decimals)
    d = Decimal(str(shares))
    if d <= 0:
        return Decimal("0")
    return (d / q).to_integral_value(rounding=ROUND_DOWN) * q


def lot_entry_price(lot: dict[str, Any]) -> Decimal | None:
    """Best-effort buy price for an open lot (VWAP from usdc/shares, else stored fields)."""
    try:
        shares = Decimal(str(lot.get("shares") or 0))
        usdc = Decimal(str(lot.get("usdc") or 0))
    except Exception:  # noqa: BLE001
        shares = Decimal("0")
        usdc = Decimal("0")
    if shares > 0 and usdc > 0:
        return usdc / shares
    for key in ("fill_price", "entry_price", "ask", "worst_price"):
        raw = lot.get(key)
        if raw is None or raw == "":
            continue
        try:
            px = Decimal(str(raw))
        except Exception:  # noqa: BLE001
            continue
        if px > 0:
            return px
    return None


def flatten_min_sell_price(
    lot: dict[str, Any],
    *,
    tick: str = "0.01",
    max_loss_frac: Decimal = FLATTEN_MAX_LOSS_FRAC,
) -> Decimal:
    """FAK sell floor: entry × (1 − max_loss), floored to tick.

    Avoids panic dumps at 0.01 after false goals; unfilled size stays pending for retry.
    """
    try:
        tick_d = Decimal(str(tick or "0.01"))
    except Exception:  # noqa: BLE001
        tick_d = Decimal("0.01")
    if tick_d <= 0:
        tick_d = Decimal("0.01")
    entry = lot_entry_price(lot)
    if entry is None or entry <= 0:
        return max(FLATTEN_MIN_PRICE, tick_d)
    loss = max(Decimal("0"), min(Decimal("0.5"), Decimal(str(max_loss_frac))))
    raw = entry * (Decimal("1") - loss)
    # Floor to tick so min_price stays on the price grid.
    stepped = (raw / tick_d).to_integral_value(rounding=ROUND_DOWN) * tick_d
    if stepped < tick_d:
        stepped = tick_d
    # Keep below par.
    cap = Decimal("1") - tick_d
    if stepped > cap:
        stepped = cap
    return stepped


def flatten_sell_shares(bal: Decimal) -> Decimal:
    """Shares to FAK-sell: 2dp floor with 99% haircut (PM balance-gate workaround)."""
    bal = Decimal(str(bal))
    if bal <= 0:
        return Decimal("0")
    haircut = floor_shares(bal * FLATTEN_SELL_HAIRCUT)
    if haircut >= FLATTEN_MIN_SHARES:
        return haircut
    # Tiny lots: leave 0.01 dust when possible, else full floor.
    full = floor_shares(bal)
    leave = full - Decimal("0.01")
    if leave >= FLATTEN_MIN_SHARES:
        return leave
    return full


def flatten_sell_shares_available(
    bal: Decimal,
    *,
    free: Decimal | None = None,
) -> Decimal:
    """Haircut sell size, capped by free (unlocked) shares when known."""
    bal = Decimal(str(bal))
    sized = flatten_sell_shares(bal)
    if free is None:
        return sized
    free_d = Decimal(str(free))
    if free_d <= 0:
        return Decimal("0")
    capped = floor_shares(free_d)
    if capped < FLATTEN_MIN_SHARES:
        return Decimal("0")
    return min(sized, capped)


def parse_balance_gate_error(err: str | Exception | None) -> dict[str, Decimal] | None:
    """Parse CLOB 'not enough balance' micro-unit fields → Decimal shares."""
    if err is None:
        return None
    text = str(err)
    if not _NOT_ENOUGH_BAL_RE.search(text):
        return None
    m = _BALANCE_GATE_RE.search(text)
    if not m:
        return None
    scale = Decimal("1000000")
    bal = Decimal(m.group(1)) / scale
    matched = Decimal(m.group(2)) / scale
    order_amt = Decimal(m.group(3)) / scale
    return {
        "balance": bal,
        "matched": matched,
        "order_amount": order_amt,
        "free": bal - matched,
    }


def gate_has_locked_inventory(gate: dict[str, Decimal] | None) -> bool:
    """True when matched orders leave nothing tradeable (do not dust-close)."""
    if not gate:
        return False
    matched = Decimal(str(gate.get("matched") or 0))
    bal = Decimal(str(gate.get("balance") or 0))
    free = Decimal(str(gate.get("free") or 0))
    # Nothing free to sell, but matched and/or bag still look real.
    if free < FLATTEN_MIN_SHARES and matched >= FLATTEN_MIN_SHARES:
        return True
    if free < FLATTEN_MIN_SHARES and bal >= FLATTEN_MIN_SHARES:
        return True
    return False


def gate_free_cap(
    gate: dict[str, Decimal] | None,
    live_bal: Decimal,
    *,
    eps: Decimal = FLATTEN_GATE_BAL_EPS,
) -> Decimal | None:
    """Return gate free shares only when live bal still matches the gate bag.

    After cancel/settle, if live balance moved, ignore stale free and size from
    live balance alone.
    """
    if not gate:
        return None
    try:
        gbal = Decimal(str(gate.get("balance") or 0))
        free = Decimal(str(gate.get("free") or 0))
    except Exception:  # noqa: BLE001
        return None
    live = Decimal(str(live_bal))
    if abs(live - gbal) > Decimal(str(eps)):
        return None
    return free


def is_not_enough_balance_error(err: str | Exception | None) -> bool:
    if err is None:
        return False
    return bool(_NOT_ENOUGH_BAL_RE.search(str(err)))


def is_terminal_flatten_error(err: str | Exception | None) -> bool:
    """Errors that will not succeed on blind retry (stop pending_flatten loop)."""
    if err is None:
        return False
    return bool(_TERMINAL_FLATTEN_ERR_RE.search(str(err)))


def flatten_reason_append(base: str, *parts: str) -> str:
    """Join reason fragments without unbounded growth from retry appends."""
    chunks: list[str] = []
    for p in (base, *parts):
        s = str(p or "").strip()
        if not s:
            continue
        # Drop repeated awaiting_delayed_fill / balance_gate_partial spam.
        if chunks and s in ("awaiting_delayed_fill", "balance_gate_partial"):
            if chunks[-1] == s or chunks[-1].endswith("|" + s):
                continue
        chunks.append(s)
    out = "|".join(chunks)
    if len(out) <= FLATTEN_REASON_MAX_LEN:
        return out
    return out[: FLATTEN_REASON_MAX_LEN - 3] + "..."


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
        # Matches with in-flight / pending exits — block new buy_win opens.
        self._buy_blocked_matches: set[str] = set()
        self._rest_blocked_matches: set[str] = set()
        self._rest_epoch: dict[str, int] = {}
        self._in_flight: set[str] = set()
        self._pending: dict[str, dict[str, Any]] = {}
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
        ctx = row.get("trade_context") if isinstance(row.get("trade_context"), dict) else {}
        # Grade C is trades-only — never rebuild into open exposure.
        if str(ctx.get("odds_grade") or "").strip().upper() == "C":
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
        self._close_stale_finished_lots()

    def _close_stale_finished_lots(self) -> None:
        """Drop open lots for matches that already finished (free open-budget).

        Also closes FT-vs-entry reversals (false-goal leftovers). Finished winners
        must not keep blocking ``QUOTE_MAX_OPEN_USDC`` after the game ends.
        """
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

        # Peek finished scores from events.jsonl (matches drop off matches.json).
        try:
            for line in (lib.bridge_dir(self.root) / "events.jsonl").read_text(
                encoding="utf-8"
            ).splitlines()[-50000:]:
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
            if not ft:
                continue
            if ft_reversal_vs_entry(entry=entry_tuple(lot), ft=ft):
                reason = f"stale_ft_reversal ft={ft[0]}-{ft[1]}"
            else:
                reason = f"stale_ft_settled ft={ft[0]}-{ft[1]}"
            self.ledger.mark_closed(
                str(lot.get("token_id")),
                mid,
                reason=reason,
            )
            closed += 1
        if closed:
            logger.info("closed %d stale finished open lots on rebuild", closed)


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
        # Cap aligned with quote_lib.DEFAULT_MAX_BUY_ASK (ask≤0.992).
        max_ask = float(getattr(lib, "DEFAULT_MAX_BUY_ASK", 0.992))
        if price > max_ask + 1e-12:
            return f"extreme_price={price} (>{max_ask})"
        return None

    def _min_buy_price_blocked(self, price: float | None) -> str | None:
        """buy_win: require best_ask >= min_buy_price (default 0.6; 0=off)."""
        floor = float(getattr(self.settings, "min_buy_price", 0.0) or 0.0)
        if floor <= 0 or price is None:
            return None
        if price < floor - 1e-12:
            return f"buy_price_below_min={price}<{floor}"
        return None

    def maybe_trade(
        self,
        quote: dict[str, Any],
        *,
        event_key: str = "",
        match_meta: dict[str, Any] | None = None,
        event_type: str = "",
    ) -> dict[str, Any] | None:
        """FAK a misprice (rest-after-FAK disabled while Odds grades are off)."""
        if not self.settings.enabled:
            return None
        q = dict(quote)
        if str(q.get("trade") or "") != "buy_win" and str(q.get("settlement") or "") == "WIN":
            q["trade"] = "buy_win"
        mis = bool(q.get("misprice"))
        # Rest ladder was Odds A/B only; grades stripped → never rest this round.
        rest_ok = False
        if not mis and not rest_ok:
            return None

        fak_row: dict[str, Any] | None = None
        skip_rest = False
        token_id = str(q.get("token_id") or "")
        mid = str((match_meta or {}).get("match_id") or q.get("match_id") or "")
        typ = self._resolve_event_type(
            event_type=event_type, event_key=event_key, match_meta=match_meta
        )
        if (
            typ == "score_change"
            and str(q.get("trade") or "") == "buy_win"
            and rest_ok
            and not best_bid_clears_min(q.get("best_bid"))
        ):
            return self._record(
                q,
                event_key=event_key,
                match_meta=match_meta,
                plan=None,
                status="skipped",
                skip_reason="best_bid_below_min",
                response=None,
                success=False,
                idempotency_key=trade_idempotency_key(
                    event_key or "", token_id, "buy_win"
                ),
                live=self._live_for_signal(typ),
                extra={
                    "size_policy": {
                        "best_bid": q.get("best_bid"),
                        "min_win_best_bid": MIN_WIN_BEST_BID,
                    }
                },
            )
        channel_live = self._live_for_signal(typ)
        if mis and token_id and mid:
            n = self._cancel_live_rest_for_token(
                mid,
                token_id,
                reason="ask_fak",
                live=channel_live,
            )
            if n:
                print(
                    f"rest-buy → CANCELED token={token_id[:12]}… orders={n} "
                    f"reason=ask_fak (misprice ask)",
                    flush=True,
                )
        if mis:
            with self._lock:
                prepared = self._prepare_trade_locked(
                    q,
                    event_key=event_key,
                    match_meta=match_meta,
                    event_type=event_type,
                )
            if not (isinstance(prepared, dict) and prepared.get("_live_post")):
                fak_row = prepared if isinstance(prepared, dict) else None
                skip_rest = self._skip_rest_after_prepare(fak_row)
            else:
                ctx = prepared
                try:
                    trader = self.ensure_trader()
                    assert trader is not None
                    token_id = str(ctx["token_id"])
                    plan: FillPlan = ctx["plan"]
                    tick = str(ctx.get("tick") or "0.01") or "0.01"
                    neg_risk = ctx.get("neg_risk")
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
                    delayed_shares: float | None = None
                    if ok and ctx["trade"] == "buy_win" and resp_status == "DELAYED":
                        bal = trader.wait_conditional_balance(
                            token_id, min_shares=0.01, timeout_s=0.0, interval_s=0.05
                        )
                        delayed_shares = max(
                            0.0, float(bal) - float(ctx.get("known_shares_before") or 0)
                        )
                except Exception as e:  # noqa: BLE001
                    logger.exception("live order failed: %s", e)
                    with self._lock:
                        self._release_pending_locked(str(ctx["key"]))
                        fak_row = self._record(
                            ctx["quote"],
                            event_key=str(ctx.get("event_key") or ""),
                            match_meta=ctx.get("match_meta"),
                            plan=ctx.get("plan"),
                            status="error",
                            skip_reason=str(e),
                            response=None,
                            success=False,
                            idempotency_key=str(ctx["key"]),
                            live=True,
                            extra=(
                                {"size_policy": ctx["size_meta"]}
                                if ctx.get("size_meta")
                                else None
                            ),
                        )
                else:
                    with self._lock:
                        fak_row = self._commit_live_trade_locked(
                            ctx,
                            response=response,
                            ok=ok,
                            resp_status=resp_status,
                            delayed_shares=delayed_shares,
                        )

        if rest_ok and not skip_rest:
            after_fak = bool(mis)
            # Ask in FAK zone: do not *add* rest (handled in _rest_remaining_buy),
            # but still run rest so a lowered cushion cap can shrink/cancel bids.
            rest_row = self._rest_remaining_buy(
                q,
                event_key=event_key,
                match_meta=match_meta,
                event_type=event_type,
                ignore_ask_zone=after_fak,
            )
            if fak_row is None:
                return rest_row
            if isinstance(fak_row, dict) and rest_row is not None:
                fak_row = dict(fak_row)
                fak_row["rest"] = {
                    "status": rest_row.get("status"),
                    "plan": rest_row.get("plan"),
                    "success": rest_row.get("success"),
                }
        return fak_row

    def _odds_grade(self, match_meta: dict[str, Any] | None) -> str:
        # Odds A/B/C gates stripped — always empty so rest/grade paths stay inert.
        return ""

    def _buy_target_usdc(
        self,
        quote: dict[str, Any],
        match_meta: dict[str, Any] | None,
    ) -> tuple[float, str]:
        """Cushion rest target, or legacy trade_context target_usdc (rest-only)."""
        ctx = (
            (match_meta or {}).get("trade_context")
            if isinstance((match_meta or {}).get("trade_context"), dict)
            else {}
        )
        base = str(ctx.get("base_event_key") or "")
        if quote_reversal_cushion(quote):
            return float(CUSHION_REST_USDC), base
        try:
            target = max(0.0, float(ctx.get("target_usdc") or 0))
        except (TypeError, ValueError):
            target = 0.0
        return target, base

    def _skip_rest_after_prepare(self, row: dict[str, Any] | None) -> bool:
        if not isinstance(row, dict):
            return False
        reason = str(row.get("skip_reason") or "")
        # Hard skips should not attempt rest ladder adjustments.
        if reason in (
            "buy_blocked_pending_flatten",
            "in_flight",
            "sell_lose_disabled",
            "already_done",
            "best_bid_below_min",
        ):
            return True
        if str(row.get("status") or "") == "record_only":
            return True
        return False

    def _grade_remaining_usdc_locked(
        self,
        quote: dict[str, Any],
        match_meta: dict[str, Any] | None,
        *,
        include_rest: bool,
    ) -> tuple[float, float, str]:
        """Return (remaining, target, base_event_key). Caller holds lock."""
        ctx = (
            (match_meta or {}).get("trade_context")
            if isinstance((match_meta or {}).get("trade_context"), dict)
            else {}
        )
        target, base = self._buy_target_usdc(quote, match_meta)
        if not base:
            base = str(ctx.get("base_event_key") or "")
        token_id = str(quote.get("token_id") or "")
        mid = str((match_meta or {}).get("match_id") or quote.get("match_id") or "")
        already = 0.0
        if target > 1e-9 and base and token_id:
            prefix = base + "|odds_grade_"
            already = sum(
                float(lot.get("usdc") or 0)
                for lot in self.ledger.all_open()
                if str(lot.get("token_id") or "") == token_id
                and (not mid or str(lot.get("match_id") or "") == mid)
                and (
                    str(lot.get("event_key") or "") == base
                    or str(lot.get("event_key") or "").startswith(prefix)
                )
            )
            already += self._pending_already_usdc_locked(
                token_id=token_id, match_id=mid, base_event_key=base
            )
            if include_rest:
                already += self.ledger.rest_reserved_usdc(
                    token_id=token_id, match_id=mid, base_event_key=base
                )
        return max(0.0, target - already), target, base

    def _rest_remaining_buy(
        self,
        quote: dict[str, Any],
        *,
        event_key: str,
        match_meta: dict[str, Any] | None,
        event_type: str,
        ignore_ask_zone: bool = False,
    ) -> dict[str, Any] | None:
        """Post/adjust GTD bids for A/B remainder after FAK."""
        typ = self._resolve_event_type(
            event_type=event_type, event_key=event_key, match_meta=match_meta
        )
        if typ != "score_change":
            return None
        token_id = str(quote.get("token_id") or "")
        mid = str((match_meta or {}).get("match_id") or quote.get("match_id") or "")
        if not token_id or not mid:
            return None
        if not best_bid_clears_min(quote.get("best_bid")):
            return None
        cushion = quote_reversal_cushion(quote)
        rest_prices = CUSHION_REST_PRICES if cushion else None
        with self._lock:
            if self._match_buy_blocked_locked(mid) or mid in self._rest_blocked_matches:
                return None
            epoch = int(self._rest_epoch.get(mid) or 0)
            remaining, target, base = self._grade_remaining_usdc_locked(
                quote, match_meta, include_rest=False
            )
            working = self.ledger.rest_reserved_usdc(
                token_id=token_id, match_id=mid, base_event_key=base
            )
            channel_live = self._live_for_signal(typ)
            tick = rest_limit_tick_size(quote.get("tick_size") or "0.01")
            floor = max(float(self.settings.size_floor_usdc or 1), MIN_REST_USDC)
            gap = remaining
            live_pairs = list(
                self.ledger.live_rest_orders(match_id=mid, token_id=token_id)
            )
            live_oids = [
                str(order.get("order_id") or "")
                for _lot, order in live_pairs
                if str(order.get("order_id") or "")
            ]
            if gap + 1e-12 < floor and working <= 1e-9:
                return None
            # Lots already cover the (possibly lowered) target: drop leftover bids.
            if gap + 1e-12 < floor:
                cancel_ids = live_oids
                levels = []
                replace = True
            else:
                replace = working + 1e-9 > gap and working > 1e-9
                add_usdc = 0.0 if replace else max(0.0, gap - working)
                if not replace and add_usdc + 1e-12 < floor:
                    return None
                place_usdc = gap if replace else add_usdc
                ask_for_ladder = None if ignore_ask_zone else quote.get("best_ask")
                # Adding rest while the ask is FAK-able is wrong; shrinking an
                # oversized ladder (cushion cap drop) must still go through.
                if ask_in_fak_zone(ask_for_ladder) and not replace:
                    return None
                levels = allocate_rest_ladder(
                    place_usdc,
                    prices=rest_prices,
                    tick_size=tick,
                    floor_usdc=floor,
                    best_bid=quote.get("best_bid"),
                    best_ask=ask_for_ladder,
                )
                if not levels and not replace:
                    return None
                live_prices = {
                    round(float(order.get("price") or 0), 4)
                    for _lot, order in live_pairs
                    if order.get("price") is not None
                }
                desired_prices = {round(float(lvl["price"]), 4) for lvl in levels}
                ladder_changed = bool(live_pairs) and live_prices != desired_prices
                if ladder_changed:
                    cancel_ids = live_oids
                    replace = True
                    place_usdc = gap
                    levels = allocate_rest_ladder(
                        place_usdc,
                        prices=rest_prices,
                        tick_size=tick,
                        floor_usdc=floor,
                        best_bid=quote.get("best_bid"),
                        best_ask=ask_for_ladder,
                    )
                    if not levels:
                        # Still cancel the old ladder (e.g. target now below floor).
                        levels = []
                else:
                    cancel_ids = live_oids if replace else []

        if cancel_ids:
            self._cancel_rest_ids(
                mid, token_id, cancel_ids, reason="rest_resize", live=channel_live
            )

        if not levels:
            return None

        expire_s = 0.0 if cushion else rest_expire_s()
        ot = "GTC" if expire_s <= 0 else "GTD"
        exp = int(time.time()) + int(expire_s) if ot == "GTD" else 0
        posted: list[dict[str, Any]] = []
        responses: list[dict[str, Any]] = []
        if not channel_live:
            posted = [
                {
                    "order_id": f"dry|{lvl['price']}",
                    "price": lvl["price"],
                    "shares": lvl["shares"],
                    "usdc": lvl["usdc"],
                    "status": "dry_run",
                    "order_type": ot,
                    "expiration": exp,
                    "filled_usdc": 0.0,
                    "filled_shares": 0.0,
                }
                for lvl in levels
            ]
        else:
            try:
                trader = self.ensure_trader()
                if trader is None:
                    raise RuntimeError("no trader for rest buy")
                neg = quote.get("neg_risk")
                neg_risk = bool(neg) if neg is not None else None
                for lvl in levels:
                    post_fn = getattr(trader, "post_limit_buy", None)
                    if not callable(post_fn):
                        logger.warning("trader has no post_limit_buy; skip rest")
                        break
                    resp = post_fn(
                        token_id,
                        Decimal(str(lvl["shares"])),
                        Decimal(str(lvl["price"])),
                        tick,
                        order_type=ot,
                        expiration=exp,
                        neg_risk=neg_risk,
                    )
                    responses.append(resp if isinstance(resp, dict) else {})
                    oid = flatten_order_id(resp)
                    ok = trader.is_order_success(
                        resp if isinstance(resp, dict) else None, market=False
                    )
                    if not ok or not oid:
                        logger.warning(
                            "rest buy rejected token=%s… px=%s resp=%s",
                            token_id[:12],
                            lvl["price"],
                            resp,
                        )
                        continue
                    posted.append(
                        {
                            "order_id": oid,
                            "price": lvl["price"],
                            "shares": lvl["shares"],
                            "usdc": lvl["usdc"],
                            "status": "LIVE",
                            "order_type": ot,
                            "expiration": exp,
                            "filled_usdc": 0.0,
                            "filled_shares": 0.0,
                            "posted_at": iso_now(),
                        }
                    )
            except Exception as e:  # noqa: BLE001
                logger.exception("rest buy failed: %s", e)
                with self._lock:
                    return self._record(
                        quote,
                        event_key=event_key,
                        match_meta=match_meta,
                        plan=None,
                        status="rest_error",
                        skip_reason=str(e),
                        response=responses[-1] if responses else None,
                        success=False,
                        idempotency_key=f"rest|{event_key}|{token_id}",
                        live=channel_live,
                    )

        if not posted:
            return None

        grade = self._odds_grade(match_meta)
        for order in posted:
            order["odds_grade"] = grade
            order["base_event_key"] = base
            order["target_usdc"] = target
            order["event_key"] = event_key

        stale = False
        with self._lock:
            stale = (
                int(self._rest_epoch.get(mid) or 0) != epoch
                or mid in self._rest_blocked_matches
            )
        if stale:
            self._cancel_rest_ids(
                mid,
                token_id,
                [str(p.get("order_id") or "") for p in posted],
                reason="rest_epoch_stale",
                live=channel_live,
            )
            return None

        with self._lock:
            for order in posted:
                self.ledger.add_rest_order(
                    match_id=mid,
                    token_id=token_id,
                    order=order,
                    market_key=str(quote.get("market_key") or ""),
                    family=str(quote.get("family") or ""),
                    event_key=event_key,
                    home=str((match_meta or {}).get("home") or ""),
                    away=str((match_meta or {}).get("away") or ""),
                    home_score=(match_meta or {}).get("home_score"),
                    away_score=(match_meta or {}).get("away_score"),
                    live=channel_live,
                    tick_size=tick,
                    neg_risk=(
                        bool(quote.get("neg_risk"))
                        if quote.get("neg_risk") is not None
                        else None
                    ),
                    odds_grade=grade,
                )
            plan = FillPlan(
                trade="buy_win",
                side="BUY",
                take_depth="rest",
                order_type=ot,
                shares=round(sum(float(p["shares"]) for p in posted), 6),
                usdc=round(sum(float(p["usdc"]) for p in posted), 6),
                worst_price=max(float(p["price"]) for p in posted),
                levels_used=len(posted),
                levels=[
                    {"price": p["price"], "size": p["shares"], "usdc": p["usdc"]}
                    for p in posted
                ],
            )
            extra = {
                "rest_orders": posted,
                "rest_replace": replace,
                "size_policy": {
                    "odds_grade": self._odds_grade(match_meta),
                    "target_usdc": target,
                    "remaining_target_usdc": round(gap, 6),
                    "base_event_key": base,
                },
            }
            row = self._record(
                quote,
                event_key=event_key,
                match_meta=match_meta,
                plan=plan,
                status="rest_posted" if channel_live else "rest_dry_run",
                skip_reason=None,
                response=responses[0] if responses else None,
                success=True,
                idempotency_key=f"rest|{event_key}|{token_id}|{posted[0].get('price')}",
                live=channel_live,
                extra=extra,
            )
            logger.info(
                "rest %s %s match=%s usdc=%.2f prices=%s",
                "LIVE" if channel_live else "dry",
                token_id[:12],
                mid,
                plan.usdc,
                [p["price"] for p in posted],
            )
            print(
                f"rest-buy → {'posted' if channel_live else 'dry'} match_id={mid} "
                f"token={token_id[:12]}… usdc={plan.usdc:.2f} "
                f"px={','.join(str(p['price']) for p in posted)} {ot}",
                flush=True,
            )
            return row

    def _cancel_live_rest_for_token(
        self,
        match_id: str,
        token_id: str,
        *,
        reason: str,
        live: bool,
    ) -> int:
        """Cancel working rest bids for one token (e.g. before ask-zone FAK)."""
        mid = str(match_id or "")
        tid = str(token_id or "")
        if not mid or not tid:
            return 0
        with self._lock:
            pairs = list(
                self.ledger.live_rest_orders(match_id=mid, token_id=tid)
            )
        if not pairs:
            return 0
        ids = [
            str(order.get("order_id") or "")
            for _lot, order in pairs
            if str(order.get("order_id") or "")
        ]
        if ids:
            self._cancel_rest_ids(mid, tid, ids, reason=reason, live=live)
        return len(ids)

    def _cancel_rest_ids(
        self,
        match_id: str,
        token_id: str,
        order_ids: list[str],
        *,
        reason: str,
        live: bool,
    ) -> None:
        ids = [oid for oid in order_ids if oid]
        if live and ids:
            try:
                trader = self.ensure_trader()
            except Exception:  # noqa: BLE001
                trader = None
            if trader is not None:
                for oid in ids:
                    try:
                        trader.cancel_order(oid)
                    except Exception as e:  # noqa: BLE001
                        logger.warning("rest cancel failed order=%s…: %s", oid[:14], e)
        with self._lock:
            for oid in ids:
                self.ledger.update_rest_order(
                    match_id=match_id,
                    token_id=token_id,
                    order_id=oid,
                    status="CANCELED",
                    cancel_reason=reason,
                    canceled_at=iso_now(),
                )
            self.ledger.close_empty_rest_lot(
                token_id, match_id, reason=f"rest_canceled|{reason}"
            )

    def cancel_rest_orders_for_match(
        self,
        match_id: str,
        *,
        reason: str = "dqd_reversal",
    ) -> int:
        """Immediately cancel working GTC/GTD bids for a match (do not wait for Odds)."""
        mid = str(match_id or "")
        if not mid:
            return 0
        with self._lock:
            self._rest_epoch[mid] = int(self._rest_epoch.get(mid) or 0) + 1
            if reason in ("dqd_reversal", "match_finished"):
                self._rest_blocked_matches.add(mid)
            pairs = list(self.ledger.live_rest_orders(match_id=mid))
        n = 0
        seen: set[tuple[str, str]] = set()
        for lot, order in pairs:
            tid = str(lot.get("token_id") or "")
            oid = str(order.get("order_id") or "")
            key = (tid, oid)
            if not tid or key in seen:
                continue
            seen.add(key)
            self._cancel_rest_ids(
                mid,
                tid,
                [oid] if oid else [],
                reason=reason,
                live=bool(lot.get("live")),
            )
            n += 1
        if n:
            print(
                f"rest-buy → CANCELED match_id={mid} orders={n} reason={reason}",
                flush=True,
            )
        return n

    def clear_rest_block(self, match_id: str) -> None:
        """Allow rest bids again after a DQD score restore."""
        mid = str(match_id or "")
        if not mid:
            return
        with self._lock:
            self._rest_blocked_matches.discard(mid)

    def cancel_stale_rest(
        self,
        match_id: str,
        *,
        keep_token_ids: set[str],
        reason: str = "settlement_no_longer_win",
    ) -> int:
        """Cancel rest bids whose token is no longer a current WIN leg."""
        mid = str(match_id or "")
        if not mid:
            return 0
        with self._lock:
            pairs = list(
                self.ledger.live_rest_orders(match_id=mid, keep_token_ids=keep_token_ids)
            )
        n = 0
        for lot, order in pairs:
            tid = str(lot.get("token_id") or "")
            oid = str(order.get("order_id") or "")
            if not tid:
                continue
            self._cancel_rest_ids(
                mid,
                tid,
                [oid] if oid else [],
                reason=reason,
                live=bool(lot.get("live")),
            )
            n += 1
        return n

    def reconcile_rest_orders(self) -> list[dict[str, Any]]:
        """Promote partial/full rest fills into the open lot; drop finished bids."""
        with self._lock:
            pairs = list(self.ledger.live_rest_orders())
        if not pairs:
            return []
        try:
            trader = self.ensure_trader()
        except Exception:  # noqa: BLE001
            trader = None
        if trader is None:
            return []
        out: list[dict[str, Any]] = []
        now = time.time()
        for lot, order in pairs:
            if not lot.get("live"):
                continue
            oid = str(order.get("order_id") or "")
            if not oid:
                continue
            last = parse_iso(str(order.get("last_checked_at") or "") or None)
            if last is not None:
                age = now - last.timestamp()
                if age < REST_ORDER_RECHECK_INTERVAL_S:
                    continue
            mid = str(lot.get("match_id") or "")
            tid = str(lot.get("token_id") or "")
            remote = trader.get_order(oid) or {}
            status = str(remote.get("status") or order.get("status") or "").upper()
            filled_shares = _rest_remote_filled_shares(remote, order)
            filled_usdc = _rest_remote_filled_usdc(remote, order, filled_shares)
            prev_sh = float(order.get("filled_shares") or 0)
            delta_sh = max(0.0, filled_shares - prev_sh)
            delta_us = max(0.0, filled_usdc - float(order.get("filled_usdc") or 0))
            with self._lock:
                self.ledger.update_rest_order(
                    match_id=mid,
                    token_id=tid,
                    order_id=oid,
                    status=status or order.get("status"),
                    filled_shares=filled_shares,
                    filled_usdc=filled_usdc,
                    last_checked_at=iso_now(),
                )
                if delta_sh > 1e-9:
                    plan = FillPlan(
                        trade="buy_win",
                        side="BUY",
                        take_depth="rest",
                        order_type=str(order.get("order_type") or "GTD"),
                        shares=round(delta_sh, 6),
                        usdc=round(delta_us or (delta_sh * float(order.get("price") or 0)), 6),
                        worst_price=float(order.get("price") or 0),
                        levels_used=1,
                        levels=[{"price": order.get("price"), "size": delta_sh}],
                    )
                    grade = str(
                        order.get("odds_grade") or lot.get("odds_grade") or ""
                    ).strip().upper()
                    fill_event_key = str(
                        order.get("event_key") or lot.get("event_key") or ""
                    )
                    fill_ctx: dict[str, Any] = {}
                    if grade:
                        fill_ctx["odds_grade"] = grade
                    if order.get("target_usdc") is not None:
                        fill_ctx["target_usdc"] = order.get("target_usdc")
                    elif lot.get("target_usdc") is not None:
                        fill_ctx["target_usdc"] = lot.get("target_usdc")
                    base_key = str(
                        order.get("base_event_key") or lot.get("base_event_key") or ""
                    )
                    if base_key:
                        fill_ctx["base_event_key"] = base_key
                    self._register_open_buy(
                        {
                            "token_id": tid,
                            "market_key": lot.get("market_key"),
                            "family": lot.get("family"),
                            "tick_size": lot.get("tick_size") or "0.01",
                            "neg_risk": lot.get("neg_risk"),
                        },
                        plan=plan,
                        event_key=fill_event_key,
                        match_meta={
                            "match_id": mid,
                            "home": lot.get("home"),
                            "away": lot.get("away"),
                            "home_score": (lot.get("entry_score") or [None, None])[0],
                            "away_score": (lot.get("entry_score") or [None, None])[1],
                            "event_type": "score_change",
                            "trade_context": fill_ctx,
                        },
                        live=True,
                    )
                    out.append(
                        {
                            "status": "rest_filled",
                            "match_id": mid,
                            "token_id": tid,
                            "order_id": oid,
                            "shares": delta_sh,
                            "usdc": plan.usdc,
                        }
                    )
                if status in ("CANCELED", "CANCELLED", "EXPIRED", "MATCHED", "FILLED") or (
                    rest_order_working_usdc(
                        {**order, "filled_usdc": filled_usdc, "status": status}
                    )
                    <= 1e-9
                ):
                    if status not in ("MATCHED", "FILLED"):
                        self.ledger.update_rest_order(
                            match_id=mid,
                            token_id=tid,
                            order_id=oid,
                            status=status or "CANCELED",
                        )
                    self.ledger.close_empty_rest_lot(
                        tid, mid, reason="rest_complete"
                    )
        return out

    def _pending_usdc_total_locked(self) -> float:
        return sum(float(p.get("usdc") or 0) for p in self._pending.values())

    def _pending_already_usdc_locked(
        self,
        *,
        token_id: str,
        match_id: str,
        base_event_key: str,
    ) -> float:
        prefix = base_event_key + "|odds_grade_"
        total = 0.0
        for p in self._pending.values():
            if str(p.get("token_id") or "") != token_id:
                continue
            if match_id and str(p.get("match_id") or "") != match_id:
                continue
            ek = str(p.get("event_key") or "")
            if ek == base_event_key or ek.startswith(prefix):
                total += float(p.get("usdc") or 0)
        return total

    def _release_pending_locked(self, key: str) -> None:
        self._pending.pop(key, None)
        self._in_flight.discard(key)

    def _match_buy_blocked_locked(self, mid: str) -> bool:
        if not mid:
            return False
        if mid in self._buy_blocked_matches:
            return True
        return any(
            r.get("pending_flatten") for r in self.ledger.open_for_match(mid)
        )

    def _match_has_in_flight_buy_locked(self, mid: str) -> bool:
        return any(str(p.get("match_id") or "") == mid for p in self._pending.values())

    def _prepare_trade_locked(
        self,
        quote: dict[str, Any],
        *,
        event_key: str = "",
        match_meta: dict[str, Any] | None = None,
        event_type: str = "",
    ) -> dict[str, Any] | None:
        """Caller must hold ``self._lock``. Returns a trades row or a live-post ctx."""
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
        if key in self._in_flight:
            logger.debug("skip in-flight %s", key)
            return {
                "idempotency_key": key,
                "status": "skipped",
                "skip_reason": "in_flight",
            }
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

        if trade == "buy_win":
            mid_block = str(
                (match_meta or {}).get("match_id")
                or quote.get("match_id")
                or ""
            )
            if mid_block and self._match_buy_blocked_locked(mid_block):
                row = self._record(
                    quote,
                    event_key=event_key,
                    match_meta=match_meta,
                    plan=None,
                    status="skipped",
                    skip_reason="buy_blocked_pending_flatten",
                    response=None,
                    success=False,
                    idempotency_key=key,
                    live=channel_live,
                )
                return row
            below_min = self._min_buy_price_blocked(ref_f)
            if below_min:
                row = self._record(
                    quote,
                    event_key=event_key,
                    match_meta=match_meta,
                    plan=None,
                    status="skipped",
                    skip_reason=below_min,
                    response=None,
                    success=False,
                    idempotency_key=key,
                    live=channel_live,
                )
                self._done.add(key)
                logger.info(
                    "skip buy_win %s ask=%s (%s)",
                    (quote.get("market_key") or "")[:40],
                    ref_f,
                    below_min,
                )
                return row

        available: float | None = None
        if trade == "sell_lose":
            row = self._record(
                quote,
                event_key=event_key,
                match_meta=match_meta,
                plan=None,
                status="skipped",
                skip_reason="sell_lose_disabled",
                response=None,
                success=False,
                idempotency_key=key,
                live=channel_live,
            )
            self._done.add(key)
            return row

        max_usdc = float(self.settings.max_usdc)
        max_shares = float(self.settings.max_shares)
        size_meta: dict[str, Any] | None = None
        if trade == "buy_win":
            open_usdc = sum(
                float(r.get("usdc") or 0)
                for r in self.ledger.all_open()
            ) + self._pending_usdc_total_locked() + self.ledger.rest_reserved_usdc()
            caps = compute_buy_size_caps(
                ref_f,
                max_usdc=self.settings.max_usdc,
                max_shares=self.settings.max_shares,
                tiers=self.settings.size_tiers,
                open_usdc=open_usdc,
                max_open_usdc=self.settings.max_open_usdc,
                floor_usdc=self.settings.size_floor_usdc,
            )
            size_meta = caps.to_dict()
            if caps.skip_reason:
                row = self._record(
                    quote,
                    event_key=event_key,
                    match_meta=match_meta,
                    plan=None,
                    status="skipped",
                    skip_reason=caps.skip_reason,
                    response=None,
                    success=False,
                    idempotency_key=key,
                    live=channel_live,
                    extra={"size_policy": size_meta},
                )
                return row
            max_usdc = float(caps.max_usdc)
            max_shares = float(caps.max_shares)
            logger.info(
                "size_policy ask=%.3f tier=%.2f eff_usdc=%.2f eff_shares=%.2f "
                "open=%.2f remaining=%s",
                caps.ask,
                caps.tier_usdc,
                caps.max_usdc,
                caps.max_shares,
                caps.open_usdc,
                caps.remaining_open,
            )

        plan = plan_fill(
            quote,
            take_depth=self.settings.take_depth,
            max_levels=self.settings.max_levels,
            max_usdc=max_usdc,
            max_shares=max_shares,
            max_slippage=self.settings.max_slippage,
            min_order_shares=self.settings.min_order_shares,
            available_shares=available,
        )

        # Marketable BUY floor is $1: bump thin-book plans up so FAK can eat
        # resting size (e.g. 0.99@$0.99) instead of CLOB rejecting $0.98.
        if (
            trade == "buy_win"
            and plan.skip_reason is None
            and float(plan.usdc or 0) > 0
            and float(plan.usdc or 0) + 1e-12 < MIN_MARKETABLE_BUY_USDC
        ):
            plan.usdc = float(MIN_MARKETABLE_BUY_USDC)

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
                extra={"size_policy": size_meta} if size_meta else None,
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
                extra={"size_policy": size_meta} if size_meta else None,
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

        # Live post: reserve under the lock, HTTP happens in maybe_trade.
        tick = str(quote.get("tick_size") or "0.01") or "0.01"
        neg = quote.get("neg_risk")
        neg_risk = bool(neg) if neg is not None else None
        mid_live = str((match_meta or {}).get("match_id") or quote.get("match_id") or "")
        known_shares_before = sum(
            float(lot.get("shares") or 0)
            for lot in self.ledger.all_open()
            if str(lot.get("token_id") or "") == token_id
            and (not mid_live or str(lot.get("match_id") or "") == mid_live)
        )
        reserved = float(plan.usdc or 0) if trade == "buy_win" else 0.0
        self._in_flight.add(key)
        if reserved > 0:
            self._pending[key] = {
                "usdc": reserved,
                "token_id": token_id,
                "match_id": mid_live,
                "event_key": event_key,
            }
        return {
            "_live_post": True,
            "quote": quote,
            "event_key": event_key,
            "match_meta": match_meta,
            "trade": trade,
            "token_id": token_id,
            "key": key,
            "plan": plan,
            "size_meta": size_meta,
            "tick": tick,
            "neg_risk": neg_risk,
            "known_shares_before": known_shares_before,
        }

    def _commit_live_trade_locked(
        self,
        ctx: dict[str, Any],
        *,
        response: Any,
        ok: bool,
        resp_status: str,
        delayed_shares: float | None,
    ) -> dict[str, Any]:
        """Caller must hold ``self._lock``. Apply fill, ledger, release reserve."""
        quote = ctx["quote"]
        event_key = str(ctx.get("event_key") or "")
        match_meta = ctx.get("match_meta")
        trade = str(ctx.get("trade") or "")
        key = str(ctx["key"])
        plan: FillPlan = ctx["plan"]
        size_meta = ctx.get("size_meta")
        self._release_pending_locked(key)
        ledger_plan = (
            actual_matched_buy_plan(plan, response)
            if ok and trade == "buy_win"
            else plan
        )
        if ok and trade == "buy_win" and resp_status == "DELAYED":
            if delayed_shares is not None and delayed_shares > 1e-6:
                fill_fraction = min(1.0, delayed_shares / max(plan.shares, 1e-9))
                ledger_plan = FillPlan(
                    trade=plan.trade,
                    side=plan.side,
                    take_depth=plan.take_depth,
                    order_type=plan.order_type,
                    shares=round(delayed_shares, 6),
                    usdc=round(plan.usdc * fill_fraction, 6),
                    worst_price=plan.worst_price,
                    levels_used=plan.levels_used,
                    levels=list(plan.levels),
                    skip_reason=plan.skip_reason,
                )
                logger.info(
                    "delayed buy confirmed token=%s… shares=%.4f (plan=%.4f)",
                    str(ctx.get("token_id") or "")[:12],
                    delayed_shares,
                    plan.shares,
                )
            else:
                logger.warning(
                    "delayed buy accepted but balance still 0 token=%s… "
                    "registering plan shares=%.4f for flatten safety",
                    str(ctx.get("token_id") or "")[:12],
                    plan.shares,
                )
        skip_reason = (
            f"delayed|{resp_status.lower()}" if resp_status == "DELAYED" else None
        )
        extra: dict[str, Any] | None = (
            {"size_policy": size_meta} if size_meta else None
        )
        if ok:
            self._done.add(key)
            if trade == "buy_win":
                fill_st = FILL_STATUS_OPEN
                if resp_status == "DELAYED" and ledger_plan is plan:
                    fill_st = FILL_STATUS_PENDING
                self._register_open_buy(
                    quote,
                    plan=ledger_plan,
                    event_key=event_key,
                    match_meta=match_meta,
                    live=True,
                    fill_status=fill_st,
                )
                mid = str(
                    (match_meta or {}).get("match_id")
                    or quote.get("match_id")
                    or ""
                )
                # Fill landed after a reversal flatten started (CLOB HTTP was
                # off-lock). Keep the lot so exit can see it, and flag retry.
                if mid and self._match_buy_blocked_locked(mid):
                    self._buy_blocked_matches.add(mid)
                    self.ledger.mark_pending_flatten(
                        str(quote.get("token_id") or ""),
                        mid,
                        reason="buy_blocked_after_post",
                    )
                    extra = dict(extra or {})
                    extra["flatten_after_post"] = True
                    if not skip_reason:
                        skip_reason = "buy_blocked_after_post"
                    else:
                        skip_reason = flatten_reason_append(
                            skip_reason, "buy_blocked_after_post"
                        )
        row = self._record(
            quote,
            event_key=event_key,
            match_meta=match_meta,
            plan=ledger_plan,
            status="posted",
            skip_reason=skip_reason,
            response=response if isinstance(response, dict) else None,
            success=ok,
            idempotency_key=key,
            live=True,
            extra=extra,
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
        fill_status: str = FILL_STATUS_OPEN,
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
            fill_status=fill_status,
        )

    def maybe_flatten_for_event(self, ev: dict[str, Any]) -> list[dict[str, Any]]:
        """Flatten FT corrections; score_change reversals deferred (no AF this round)."""
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
            logger.info(
                "defer score_change reversal flatten match=%s score=%s-%s",
                mid,
                ev.get("home_score"),
                ev.get("away_score"),
            )
            return []
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

        # Block new buys for this match until exits clear.
        self._buy_blocked_matches.add(mid)
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
        self._maybe_clear_buy_block(mid)
        return out

    def _maybe_clear_buy_block(self, mid: str) -> None:
        """Lift buy_win block once this match has no open lots or in-flight buys."""
        mid = str(mid or "")
        if not mid:
            return
        if self.ledger.open_for_match(mid):
            return
        if any(
            str(r.get("match_id")) == mid for r in self.ledger.pending_flatten_lots()
        ):
            return
        if self._match_has_in_flight_buy_locked(mid):
            return
        self._buy_blocked_matches.discard(mid)

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
            attempts = int(lot.get("flatten_attempts") or 0)
            if lot.get("flatten_order_id"):
                reconciled = self._reconcile_pending_flatten_order(lot)
                if reconciled is not None:
                    out.append(reconciled)
                continue
            # Stop looping terminal CLOB errors left in the ledger.
            # Delayed-fill timeout is handled inside _flatten_lot after a live
            # balance check (so late credits still get sold).
            give_up = ""
            if is_terminal_flatten_error(reason):
                give_up = "terminal_flatten_error"
            elif attempts >= FLATTEN_MAX_ATTEMPTS:
                give_up = f"max_attempts={attempts}"
            if give_up:
                self.ledger.mark_closed(
                    tid, mid, reason=flatten_reason_append(give_up, reason)
                )
                self._maybe_clear_buy_block(mid)
                alert = (
                    f"ALERT flatten_give_up match={mid} token={tid[:12]}… "
                    f"{give_up} — stopped retry"
                )
                logger.error(alert)
                print(alert, flush=True)
                out.append(
                    {
                        "quoted_at": lib.now_cn_iso(),
                        "status": "flatten_abandoned",
                        "skip_reason": flatten_reason_append(give_up, reason),
                        "trade": "flatten_reversal",
                        "match_id": mid,
                        "token_id": tid,
                    }
                )
                continue
            ek = f"flatten_retry|{mid}|{lib.now_cn_iso()}|{reason[:80]}"
            logger.error(
                "ALERT flatten_retry match=%s token=%s… attempt=%s reason=%s",
                mid,
                tid[:12],
                attempts,
                reason[:200],
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

    def _reconcile_pending_flatten_order(
        self,
        lot: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Reconcile one accepted async sell without reposting it every tick."""
        mid = str(lot.get("match_id") or "")
        token_id = str(lot.get("token_id") or "")
        order_id = str(lot.get("flatten_order_id") or "")
        reason = str(lot.get("pending_reason") or "confirmed_reversal")
        if not (mid and token_id and order_id):
            return None
        last_checked = parse_iso(
            str(lot.get("flatten_order_last_checked_at") or "") or None
        )
        if last_checked is not None:
            now_for_check = (
                datetime.now(last_checked.tzinfo)
                if last_checked.tzinfo
                else datetime.now()
            )
            if (
                now_for_check - last_checked
            ).total_seconds() < FLATTEN_ORDER_RECHECK_INTERVAL_S:
                return None
        trader = self.ensure_trader()
        if trader is None:
            return None

        try:
            balance_before = Decimal(str(lot.get("flatten_order_balance_before")))
        except Exception:  # noqa: BLE001
            balance_before = Decimal(str(lot.get("shares") or 0))
        try:
            balance_now = Decimal(str(trader.get_conditional_balance(token_id)))
        except Exception:  # noqa: BLE001
            balance_now = Decimal("-1")
        order_payload = trader.get_order(order_id)
        status = flatten_order_status(order_payload) or str(
            lot.get("flatten_order_status") or "DELAYED"
        ).upper()
        checks = int(lot.get("flatten_order_checks") or 0) + 1
        self.ledger.update_pending_flatten_order(
            token_id,
            mid,
            flatten_order_status=status,
            flatten_order_checks=checks,
            flatten_order_last_checked_at=iso_now(),
        )

        submitted = parse_iso(str(lot.get("flatten_order_submitted_at") or "") or None)
        age_s = 0.0
        if submitted is not None:
            now = datetime.now(submitted.tzinfo) if submitted.tzinfo else datetime.now()
            age_s = max(0.0, (now - submitted).total_seconds())
        terminal_ok = status in {"MATCHED", "FILLED", "SUCCESS", "COMPLETED"}
        terminal_fail = status in {
            "CANCELED", "CANCELLED", "FAILED", "REJECTED", "EXPIRED"
        }
        balance_changed = balance_now >= 0 and balance_now + Decimal("0.000001") < balance_before

        if balance_now >= 0 and floor_shares(balance_now) < FLATTEN_MIN_SHARES:
            sold = max(Decimal("0"), balance_before - balance_now)
            plan = FillPlan(
                trade="flatten_reversal",
                side="SELL",
                take_depth="emergency",
                order_type="FAK",
                shares=float(sold),
                usdc=0.0,
                worst_price=float(flatten_min_sell_price(lot)),
                levels_used=0,
                levels=[],
                skip_reason=None,
            )
            row = self._record(
                {
                    "market_key": lot.get("market_key"),
                    "family": lot.get("family"),
                    "token_id": token_id,
                    "trade": "flatten_reversal",
                    "settlement": "REVERSAL",
                },
                event_key=str(lot.get("flatten_order_event_key") or "flatten_settled"),
                match_meta={
                    "match_id": mid,
                    "home": lot.get("home") or "",
                    "away": lot.get("away") or "",
                },
                plan=plan,
                status="flatten_settled",
                skip_reason=f"{reason}|order={order_id}|status={status}",
                response=order_payload,
                success=True,
                idempotency_key=f"flatten_settled|{mid}|{token_id}|{order_id}",
                live=True,
            )
            self._flatten_done.add(str(row.get("idempotency_key") or ""))
            self.ledger.mark_closed(
                token_id,
                mid,
                reason=flatten_reason_append(reason, f"order_settled={order_id}"),
            )
            self._maybe_clear_buy_block(mid)
            return row

        # Retry only a proven residual: failed order, or a balance reduction
        # that has remained after the settlement grace. A MATCHED/FILLED status
        # with an unchanged balance is explicitly *not* enough to repost—the
        # balance endpoint may simply lag and reposting could double-sell.
        retry_ready = terminal_fail or (
            balance_changed and age_s >= FLATTEN_ORDER_SETTLE_GRACE_S
        )
        timed_out = age_s >= FLATTEN_ORDER_MAX_WAIT_S
        if terminal_ok and not balance_changed:
            return None
        if not retry_ready and not timed_out:
            return None
        if not terminal_fail and not (terminal_ok and balance_changed):
            cancel_result = trader.cancel_order(order_id)
            # If the exact-order cancel cannot be acknowledged, keep waiting;
            # never clear state and send a competing order on assumption alone.
            if not flatten_cancel_ack(cancel_result, order_id):
                return None
        # Re-read after terminal/cancel to reduce the fill-vs-cancel race and
        # size any retry from the latest real residual, not the earlier sample.
        try:
            latest_balance = Decimal(str(trader.get_conditional_balance(token_id)))
            if latest_balance >= 0:
                balance_now = latest_balance
        except Exception:  # noqa: BLE001
            pass
        if balance_now >= 0 and floor_shares(balance_now) < FLATTEN_MIN_SHARES:
            # Let the normal settlement branch record and close on the next
            # throttled check; critically, do not post a zero/residual sell now.
            self.ledger.update_pending_flatten_order(
                token_id,
                mid,
                flatten_order_last_checked_at="1970-01-01T00:00:00+00:00",
            )
            return None
        next_reason = flatten_reason_append(
            reason,
            f"prior_order={order_id}",
            f"prior_status={status or 'unknown'}",
            f"age_s={age_s:.1f}",
        )
        self.ledger.clear_pending_flatten_order(
            token_id,
            mid,
            reason=next_reason,
        )
        if balance_now > 0:
            self.ledger.reconcile_inventory(token_id, mid, balance_now)
            reconcile_lot_inventory(lot, balance_now)
        return self._flatten_lot(
            lot,
            event_key=f"flatten_retry_after_order|{mid}|{iso_now()}",
            reason=next_reason,
            match_ev={
                "match_id": mid,
                "home": lot.get("home"),
                "away": lot.get("away"),
            },
        )

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
        if lot.get("flatten_order_id"):
            return {
                "quoted_at": lib.now_cn_iso(),
                "status": "flatten_waiting_order",
                "success": False,
                "trade": "flatten_reversal",
                "match_id": mid,
                "token_id": token_id,
                "order_id": lot.get("flatten_order_id"),
                "order_status": lot.get("flatten_order_status"),
                "skip_reason": "accepted_async_order_pending",
            }
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
            self._maybe_clear_buy_block(mid)
            logger.info(
                "flatten dry-run match=%s token=%s… shares=%.4f (%s)",
                mid,
                token_id[:12],
                planned_shares,
                reason,
            )
            return row

        # Live FAK sell: cancel locks, haircut size, min_price = entry×(1−20%).
        # No 0.01 panic dump — if the book cannot fill, residual stays for retry.
        try:
            trader = self.ensure_trader()
            if trader is None:
                raise RuntimeError("no trader for flatten")

            try:
                trader.refresh_conditional_allowance(token_id)
            except Exception:  # noqa: BLE001
                pass
            try:
                trader.cancel_orders_for_asset(token_id)
            except Exception:  # noqa: BLE001
                pass

            bal = Decimal(str(trader.get_conditional_balance(token_id)))
            if bal > 0:
                # Align ledger VWAP before pricing the sell floor (delayed fills
                # often still hold planned shares until balance appears).
                self.ledger.reconcile_inventory(token_id, mid, bal)
                reconcile_lot_inventory(lot, bal)
            shares = flatten_sell_shares(bal)
            if bal <= 0 or shares < FLATTEN_MIN_SHARES:
                # True empty → close. Tiny dust with no locks → close.
                # If balance still tradeable but sizing floored to 0, or we only
                # see dust while locks may remain, keep pending (Bodø case).
                if bal <= 0:
                    if lot.get("fill_status") == FILL_STATUS_PENDING:
                        attempts = int(lot.get("flatten_attempts") or 0)
                        if attempts >= FLATTEN_DELAYED_FILL_MAX_ATTEMPTS:
                            close_r = flatten_reason_append(
                                reason, "delayed_fill_never_appeared", f"attempts={attempts}"
                            )
                            row = self._record(
                                quote_stub,
                                event_key=event_key,
                                match_meta=meta,
                                plan=None,
                                status="flatten_abandoned",
                                skip_reason="delayed_fill_never_appeared",
                                response=None,
                                success=False,
                                idempotency_key=key,
                                live=True,
                            )
                            self._flatten_done.add(key)
                            self.ledger.mark_closed(token_id, mid, reason=close_r)
                            self._maybe_clear_buy_block(mid)
                            alert = (
                                f"ALERT flatten_give_up match={mid} token={token_id[:12]}… "
                                f"delayed fill never appeared after {attempts} attempts"
                            )
                            logger.error(alert)
                            print(alert, flush=True)
                            return row
                        # Delayed fill not yet visible — keep pending; don't zombie-close.
                        self.ledger.mark_pending_flatten(
                            token_id,
                            mid,
                            reason=flatten_reason_append(reason, "awaiting_delayed_fill"),
                        )
                        self._buy_blocked_matches.add(mid)
                        row = self._record(
                            quote_stub,
                            event_key=event_key,
                            match_meta=meta,
                            plan=None,
                            status="flatten_skipped",
                            skip_reason="awaiting_delayed_fill",
                            response=None,
                            success=False,
                            idempotency_key=key,
                            live=True,
                        )
                        return row
                    row = self._record(
                        quote_stub,
                        event_key=event_key,
                        match_meta=meta,
                        plan=None,
                        status="flatten_skipped",
                        skip_reason="no_position_on_flatten",
                        response=None,
                        success=False,
                        idempotency_key=key,
                        live=True,
                    )
                    self._flatten_done.add(key)
                    self.ledger.mark_closed(token_id, mid, reason=reason + "|empty")
                    self._maybe_clear_buy_block(mid)
                    return row
                if bal >= FLATTEN_MIN_SHARES:
                    # Have inventory but cannot size a legal sell this tick.
                    self.ledger.mark_pending_flatten(
                        token_id,
                        mid,
                        reason=flatten_reason_append(
                            reason, f"unsellable_bal={bal:.6f}", f"floor={shares}"
                        ),
                    )
                    alert = (
                        f"ALERT flatten_unsellable match={mid} token={token_id[:12]}… "
                        f"bal={bal} sell={shares} — keep pending"
                    )
                    logger.warning(alert)
                    print(alert, flush=True)
                    return self._record(
                        quote_stub,
                        event_key=event_key,
                        match_meta=meta,
                        plan=None,
                        status="flatten_error",
                        skip_reason=f"unsellable_bal={bal}",
                        response=None,
                        success=False,
                        idempotency_key=key,
                        live=True,
                    )
                dust_reason = f"{reason}|dust_bal={bal:.6f}|floor={shares}"
                row = self._record(
                    quote_stub,
                    event_key=event_key,
                    match_meta=meta,
                    plan=None,
                    status="flatten_skipped",
                    skip_reason="flatten_dust",
                    response=None,
                    success=True,
                    idempotency_key=key,
                    live=True,
                )
                self._flatten_done.add(key)
                self.ledger.mark_closed(token_id, mid, reason=dust_reason)
                self._maybe_clear_buy_block(mid)
                alert = (
                    f"ALERT flatten_dust match={mid} token={token_id[:12]}… "
                    f"bal={bal} sell={shares} < {FLATTEN_MIN_SHARES} — closed"
                )
                logger.warning(alert)
                print(alert, flush=True)
                return row

            tick = str(lot.get("tick_size") or "0.01") or "0.01"
            neg = lot.get("neg_risk")
            neg_risk = bool(neg) if neg is not None else None
            min_px = flatten_min_sell_price(lot, tick=tick)
            logger.info(
                "flatten sell match=%s token=%s… shares=%s min=%s entry≈%s",
                mid,
                token_id[:12],
                shares,
                min_px,
                lot_entry_price(lot),
            )
            response, shares, ok = self._flatten_post_sell(
                trader,
                token_id=token_id,
                shares=shares,
                tick=tick,
                min_price=min_px,
                neg_risk=neg_risk,
            )

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
                worst_price=float(min_px),
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
                self._maybe_clear_buy_block(mid)
            else:
                oid = flatten_order_id(response)
                pending_reason = flatten_reason_append(
                    reason,
                    f"incomplete residual={float(residual):.4f}"
                    if residual >= 0
                    else "incomplete",
                )
                if ok and oid:
                    self.ledger.mark_pending_flatten(
                        token_id,
                        mid,
                        reason=pending_reason,
                        order={
                            "flatten_order_id": oid,
                            "flatten_order_status": flatten_order_status(response) or "DELAYED",
                            "flatten_order_submitted_at": iso_now(),
                            "flatten_order_balance_before": str(bal),
                            "flatten_order_shares": str(shares),
                            "flatten_order_event_key": event_key,
                            "flatten_order_checks": 0,
                        },
                    )
                    logger.warning(
                        "flatten async pending match=%s token=%s… order=%s… "
                        "status=%s balance=%s (no repost until reconciled)",
                        mid,
                        token_id[:12],
                        oid[:14],
                        flatten_order_status(response) or "DELAYED",
                        residual,
                    )
                else:
                    alert = (
                        f"ALERT flatten_incomplete match={mid} token={token_id[:12]}… "
                        f"sold≈{shares} residual={residual} ok={ok} — will retry"
                    )
                    logger.error(alert)
                    print(alert, flush=True)
                    self.ledger.mark_pending_flatten(
                        token_id,
                        mid,
                        reason=pending_reason,
                    )
            return row
        except Exception as e:  # noqa: BLE001
            if is_not_enough_balance_error(e):
                recovered = self._flatten_retry_after_balance_gate(
                    lot,
                    event_key=event_key,
                    reason=reason,
                    match_ev=match_ev,
                    key=key,
                    quote_stub=quote_stub,
                    meta=meta,
                    err=e,
                )
                if recovered is not None:
                    return recovered
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
                    token_id,
                    mid,
                    reason=flatten_reason_append(reason, "terminal", str(e)),
                )
                self._maybe_clear_buy_block(mid)
            else:
                self.ledger.mark_pending_flatten(
                    token_id,
                    mid,
                    reason=flatten_reason_append(reason, f"err={e}"),
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

    def _flatten_post_sell(
        self,
        trader: ClobTrader,
        *,
        token_id: str,
        shares: Decimal,
        tick: str,
        min_price: Decimal,
        neg_risk: bool | None,
    ) -> tuple[dict[str, Any], Decimal, bool]:
        response = trader.post_market_sell(
            token_id,
            shares,
            tick,
            min_price=min_price,
            order_type="FAK",
            neg_risk=neg_risk,
        )
        return response, shares, trader.is_order_success(response)

    def _flatten_retry_after_balance_gate(
        self,
        lot: dict[str, Any],
        *,
        event_key: str,
        reason: str,
        match_ev: dict[str, Any],
        key: str,
        quote_stub: dict[str, Any],
        meta: dict[str, Any],
        err: Exception,
    ) -> dict[str, Any] | None:
        """Cancel + sell free/haircut size after CLOB matched-order gate rejects.

        No inline sleep (keeps watch responsive). If still locked, leave
        ``pending_flatten`` for the next tick's ``retry_pending_flattens``.
        """
        token_id = str(lot.get("token_id") or "")
        mid = str(lot.get("match_id") or "")
        trader = self.ensure_trader()
        if trader is None:
            return None
        gate = parse_balance_gate_error(err)
        logger.warning(
            "flatten balance-gate match=%s token=%s… gate=%s — cancel+resize",
            mid,
            token_id[:12],
            {k: str(v) for k, v in (gate or {}).items()},
        )
        try:
            trader.cancel_orders_for_asset(token_id)
        except Exception:  # noqa: BLE001
            pass
        try:
            trader.refresh_conditional_allowance(token_id)
        except Exception:  # noqa: BLE001
            pass

        try:
            bal = Decimal(str(trader.get_conditional_balance(token_id)))
        except Exception:  # noqa: BLE001
            bal = Decimal("0")

        if bal > 0:
            self.ledger.reconcile_inventory(token_id, mid, bal)
            reconcile_lot_inventory(lot, bal)

        # Trust gate free only while live bal still looks like the same bag.
        free = gate_free_cap(gate, bal)
        shares = flatten_sell_shares_available(bal, free=free)
        if shares < FLATTEN_MIN_SHARES and free is not None and free >= FLATTEN_MIN_SHARES:
            shares = floor_shares(free)
        if shares < FLATTEN_MIN_SHARES:
            # Locked inventory or true dust. Never dust-close while matched
            # still holds a tradeable bag — next tick retries after cancel.
            if gate_has_locked_inventory(gate) or bal >= FLATTEN_MIN_SHARES:
                lock_reason = (
                    f"{reason}|err={err}|await_unlock|matched="
                    f"{(gate or {}).get('matched')}|free={free}|bal={bal}"
                )
                self.ledger.mark_pending_flatten(token_id, mid, reason=lock_reason[:300])
                alert = (
                    f"ALERT flatten_locked match={mid} token={token_id[:12]}… "
                    f"bal={bal} free={free} matched={(gate or {}).get('matched')} "
                    f"— pending next tick"
                )
                logger.warning(alert)
                print(alert, flush=True)
                return self._record(
                    quote_stub,
                    event_key=event_key,
                    match_meta=meta,
                    plan=None,
                    status="flatten_error",
                    skip_reason=f"{err}|await_unlock",
                    response=None,
                    success=False,
                    idempotency_key=key,
                    live=True,
                )
            self.ledger.mark_pending_flatten(
                token_id,
                mid,
                reason=flatten_reason_append(
                    reason, f"err={err}", f"free<{FLATTEN_MIN_SHARES}"
                ),
            )
            return self._record(
                quote_stub,
                event_key=event_key,
                match_meta=meta,
                plan=None,
                status="flatten_error",
                skip_reason=str(err),
                response=None,
                success=False,
                idempotency_key=key,
                live=True,
            )

        tick = str(lot.get("tick_size") or "0.01") or "0.01"
        neg = lot.get("neg_risk")
        neg_risk = bool(neg) if neg is not None else None
        min_px = flatten_min_sell_price(lot, tick=tick)
        try:
            response, shares, ok = self._flatten_post_sell(
                trader,
                token_id=token_id,
                shares=shares,
                tick=tick,
                min_price=min_px,
                neg_risk=neg_risk,
            )
        except Exception as e2:  # noqa: BLE001
            self.ledger.mark_pending_flatten(
                token_id,
                mid,
                reason=f"{reason}|err={err}|retry_err={e2}"[:300],
            )
            return self._record(
                quote_stub,
                event_key=event_key,
                match_meta=meta,
                plan=None,
                status="flatten_error",
                skip_reason=f"{err}|retry={e2}",
                response=None,
                success=False,
                idempotency_key=key,
                live=True,
            )

        try:
            residual = Decimal(str(trader.get_conditional_balance(token_id)))
        except Exception:  # noqa: BLE001
            residual = Decimal("-1")
        residual_floor = floor_shares(residual) if residual >= 0 else Decimal("-1")
        fully_flat = ok and residual >= 0 and residual_floor < FLATTEN_MIN_SHARES
        plan = FillPlan(
            trade="flatten_reversal",
            side="SELL",
            take_depth="emergency",
            order_type="FAK",
            shares=float(shares),
            usdc=0.0,
            worst_price=float(min_px),
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
            skip_reason=(
                reason + f"|balance_gate_retry|residual={float(residual):.4f}"
                if residual > FLATTEN_MIN_SHARES
                else reason + "|balance_gate_retry"
            ),
            response=response,
            success=fully_flat,
            idempotency_key=key,
            live=True,
        )
        if fully_flat:
            self._flatten_done.add(key)
            self.ledger.mark_closed(
                token_id,
                mid,
                reason=flatten_reason_append(reason, "balance_gate_retry"),
            )
            self._maybe_clear_buy_block(mid)
        else:
            oid = flatten_order_id(response)
            pending_reason = flatten_reason_append(reason, "balance_gate_partial")
            if ok and oid:
                self.ledger.mark_pending_flatten(
                    token_id,
                    mid,
                    reason=pending_reason,
                    order={
                        "flatten_order_id": oid,
                        "flatten_order_status": flatten_order_status(response) or "DELAYED",
                        "flatten_order_submitted_at": iso_now(),
                        "flatten_order_balance_before": str(bal),
                        "flatten_order_shares": str(shares),
                        "flatten_order_event_key": event_key,
                        "flatten_order_checks": 0,
                    },
                )
            else:
                self.ledger.mark_pending_flatten(
                    token_id,
                    mid,
                    reason=pending_reason,
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
        live: bool | None = None,
        extra: dict[str, Any] | None = None,
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
        if isinstance(meta.get("trade_context"), dict):
            row["trade_context"] = dict(meta["trade_context"])
        if extra:
            row.update(extra)
        lib.append_jsonl_async(self.trades_path, [row])
        return row
