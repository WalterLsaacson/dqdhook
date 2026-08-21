"""Score-reversal helpers + open buy_win ledger for emergency FAK flatten."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

logger = logging.getLogger("pm_quote.reversal")

# Ignore sub-dust share drift when aligning ledger to chain balance.
_INVENTORY_EPS = Decimal("0.000001")

TZ_CN = timezone(timedelta(hours=8))

FILL_STATUS_OPEN = "open"
FILL_STATUS_PENDING = "pending_fill"

_FLATTEN_ORDER_FIELDS = (
    "flatten_order_id",
    "flatten_order_status",
    "flatten_order_submitted_at",
    "flatten_order_balance_before",
    "flatten_order_shares",
    "flatten_order_event_key",
    "flatten_order_checks",
    "flatten_order_last_checked_at",
)


def _clear_flatten_order_fields(row: dict[str, Any]) -> None:
    for key in _FLATTEN_ORDER_FIELDS:
        row.pop(key, None)


def score_pair(home: Any, away: Any) -> tuple[int, int] | None:
    try:
        if home is None or away is None:
            return None
        return int(home), int(away)
    except (TypeError, ValueError):
        return None


def is_score_decrease(
    prev: tuple[int, int] | None,
    curr: tuple[int, int] | None,
) -> bool:
    if prev is None or curr is None:
        return False
    return curr[0] < prev[0] or curr[1] < prev[1]


def reconcile_lot_inventory(lot: dict[str, Any], live_shares: Any) -> bool:
    """Align lot ``shares``/``usdc`` with live conditional balance for VWAP.

    - live > ledger shares (better/understated fill): keep ``usdc``, raise shares.
    - live < ledger shares (partial exit / overstated plan): scale ``usdc`` so
      VWAP is preserved on the residual.
    Mutates ``lot`` in place. Returns True when fields changed.
    """
    try:
        live = Decimal(str(live_shares))
    except Exception:  # noqa: BLE001
        return False
    if live <= 0:
        return False
    try:
        ledger_sh = Decimal(str(lot.get("shares") or 0))
    except Exception:  # noqa: BLE001
        ledger_sh = Decimal("0")
    try:
        usdc = Decimal(str(lot.get("usdc") or 0))
    except Exception:  # noqa: BLE001
        usdc = Decimal("0")

    if ledger_sh <= 0:
        lot["shares"] = float(live)
        return True
    if abs(live - ledger_sh) <= _INVENTORY_EPS:
        return False
    if live < ledger_sh:
        if usdc > 0:
            lot["usdc"] = float(usdc * (live / ledger_sh))
        lot["shares"] = float(live)
        return True
    # live > ledger: more shares than planned → cheaper true VWAP.
    lot["shares"] = float(live)
    return True


def event_signals_reversal(ev: dict[str, Any]) -> bool:
    """True if bridge marked reversal or prev→curr score dropped."""
    if ev.get("is_reversal"):
        return True
    if (ev.get("type") or "") != "score_change":
        return False
    prev = ev.get("prev") or {}
    curr = ev.get("curr") or {}
    p = score_pair(prev.get("home"), prev.get("away"))
    c = score_pair(
        curr.get("home", ev.get("home_score")),
        curr.get("away", ev.get("away_score")),
    )
    return is_score_decrease(p, c)


def ft_reversal_vs_entry(
    *,
    entry: tuple[int, int] | None,
    ft: tuple[int, int] | None,
) -> bool:
    """FT/curr score lower than entry on either side → entry depended on a later-disallowed goal."""
    return is_score_decrease(entry, ft)


def entry_tuple(lot: dict[str, Any]) -> tuple[int, int] | None:
    entry = lot.get("entry_score")
    if isinstance(entry, (list, tuple)) and len(entry) >= 2:
        return score_pair(entry[0], entry[1])
    return None


def lot_depends_on_disallowed_goal(
    lot: dict[str, Any],
    *,
    after_score: tuple[int, int] | None,
) -> bool:
    """True if this buy was opened at a score that is strictly 'higher' than after_score.

    Example: entry 1-2, after 1-1 → away goal blown → Over 2.5 lot depends on it.
    entry 1-0 Exact 0-0 No, after 1-1 → not a decrease vs entry → keep.
    """
    return is_score_decrease(entry_tuple(lot), after_score)


def lot_protect_age_s(lot: dict[str, Any], *, now: datetime | None = None) -> float | None:
    """Seconds since this lot was opened, or None when ``opened_at`` is unusable."""
    opened = parse_iso(str(lot.get("opened_at") or "") or None)
    if opened is None:
        return None
    ref = now or datetime.now(TZ_CN)
    return (ref - opened).total_seconds()


def lot_in_protect_window(
    lot: dict[str, Any],
    *,
    window_s: float,
    now: datetime | None = None,
) -> bool:
    """True when a pitch-gate lot is still inside its post-buy protection window.

    Only gate buys are protected: FT buys are not exposed to delayed DQD
    reversals, and a missing/unparseable ``opened_at`` must not open an
    unbounded flatten window.
    """
    if window_s <= 0:
        return False
    if not lot.get("pitch_gate"):
        return False
    age = lot_protect_age_s(lot, now=now)
    if age is None:
        return False
    return -1.0 <= age <= float(window_s)


def iso_now() -> str:
    return datetime.now(TZ_CN).isoformat(timespec="seconds")


def parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    raw = str(ts).strip()
    if not raw:
        return None
    try:
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ_CN)
    return dt


class OpenPositionLedger:
    """Persist open buy_win lots per match for reversal flatten."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._rows: list[dict[str, Any]] = []
        self._load()

    def _load(self) -> None:
        if not self.path.is_file():
            self._rows = []
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            self._rows = []
            return
        if isinstance(raw, list):
            self._rows = [r for r in raw if isinstance(r, dict)]
        elif isinstance(raw, dict) and isinstance(raw.get("positions"), list):
            self._rows = [r for r in raw["positions"] if isinstance(r, dict)]
        else:
            self._rows = []

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Keep only recent closed + all open to bound size
        open_rows = [r for r in self._rows if r.get("status") == "open"]
        closed = [r for r in self._rows if r.get("status") != "open"][-200:]
        payload = {"positions": open_rows + closed}
        self.path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def register_buy(
        self,
        *,
        match_id: str,
        token_id: str,
        market_key: str,
        shares: float,
        usdc: float,
        home_score: Any,
        away_score: Any,
        live: bool,
        event_key: str,
        home: str = "",
        away: str = "",
        family: str = "",
        tick_size: str = "0.01",
        neg_risk: bool | None = None,
        fill_status: str = FILL_STATUS_OPEN,
        pitch_gate: bool = False,
        opened_at: str | None = None,
    ) -> None:
        if not match_id or not token_id or shares <= 0:
            return
        sc = score_pair(home_score, away_score)
        fill = str(fill_status or FILL_STATUS_OPEN)
        if fill not in (FILL_STATUS_OPEN, FILL_STATUS_PENDING):
            fill = FILL_STATUS_OPEN
        # Aggregate with prior open lot for same match+token (avoid under-selling).
        existing = None
        kept: list[dict[str, Any]] = []
        for r in self._rows:
            if (
                r.get("status") == "open"
                and str(r.get("match_id")) == str(match_id)
                and str(r.get("token_id")) == str(token_id)
            ):
                existing = r
            else:
                kept.append(r)
        self._rows = kept
        if existing is not None:
            shares = float(existing.get("shares") or 0) + float(shares)
            usdc = float(existing.get("usdc") or 0) + float(usdc)
            if existing.get("pending_flatten"):
                # Keep exit intent across top-ups.
                pass
            if existing.get("fill_status") == FILL_STATUS_OPEN:
                fill = FILL_STATUS_OPEN
            event_key = str(existing.get("event_key") or event_key)
            if existing.get("entry_score"):
                sc = score_pair(
                    (existing.get("entry_score") or [None, None])[0],
                    (existing.get("entry_score") or [None, None])[1],
                ) or sc
            pitch_gate = bool(pitch_gate or existing.get("pitch_gate"))
            # Top-ups must not extend the protection window past the first buy.
            opened_at = str(existing.get("opened_at") or "") or opened_at
        row = {
            "status": "open",
            "match_id": str(match_id),
            "token_id": str(token_id),
            "market_key": market_key or str(existing.get("market_key") if existing else ""),
            "family": family or str(existing.get("family") if existing else ""),
            "shares": float(shares),
            "usdc": float(usdc),
            "entry_score": list(sc) if sc else None,
            "home": home or str(existing.get("home") if existing else ""),
            "away": away or str(existing.get("away") if existing else ""),
            "live": bool(live if existing is None else (live or existing.get("live"))),
            "event_key": event_key,
            "tick_size": tick_size or "0.01",
            "neg_risk": neg_risk if neg_risk is not None else (existing.get("neg_risk") if existing else None),
            "fill_status": fill,
            "pitch_gate": bool(pitch_gate),
            "opened_at": str(opened_at or iso_now()),
        }
        if existing is not None and existing.get("pending_flatten"):
            row["pending_flatten"] = True
            row["pending_reason"] = existing.get("pending_reason")
        if existing is not None and existing.get("rest_orders"):
            row["rest_orders"] = list(existing.get("rest_orders") or [])
        self._rows.append(row)
        self._save()
        logger.info(
            "ledger open match=%s token=%s… shares=%.4f entry=%s fill=%s",
            match_id,
            token_id[:12],
            shares,
            sc,
            fill,
        )

    def mark_fill_open(
        self,
        token_id: str,
        match_id: str,
        *,
        live_shares: Any = None,
    ) -> int:
        """Promote pending_fill → open after balance appears.

        When ``live_shares`` is set, also reconcile ``shares``/``usdc`` so
        flatten floors use true VWAP (not the pre-trade plan size).
        """
        n = 0
        tid, mid = str(token_id), str(match_id)
        for r in self._rows:
            if (
                r.get("status") == "open"
                and str(r.get("token_id")) == tid
                and str(r.get("match_id")) == mid
            ):
                changed = False
                if r.get("fill_status") == FILL_STATUS_PENDING:
                    r["fill_status"] = FILL_STATUS_OPEN
                    changed = True
                if live_shares is not None and reconcile_lot_inventory(r, live_shares):
                    changed = True
                if changed:
                    n += 1
        if n:
            self._save()
        return n

    def reconcile_inventory(
        self,
        token_id: str,
        match_id: str,
        live_shares: Any,
    ) -> int:
        """Align open lot inventory to chain balance; promote pending_fill if needed."""
        return self.mark_fill_open(token_id, match_id, live_shares=live_shares)

    def open_for_match(self, match_id: str) -> list[dict[str, Any]]:
        mid = str(match_id)
        return [
            r
            for r in self._rows
            if r.get("status") == "open" and str(r.get("match_id")) == mid
        ]

    def mark_closed(self, token_id: str, match_id: str, *, reason: str) -> None:
        mid = str(match_id)
        tid = str(token_id)
        changed = False
        for r in self._rows:
            if (
                r.get("status") == "open"
                and str(r.get("match_id")) == mid
                and str(r.get("token_id")) == tid
            ):
                r["status"] = "closed"
                r["close_reason"] = reason
                r.pop("pending_flatten", None)
                r.pop("pending_reason", None)
                r.pop("flatten_attempts", None)
                _clear_flatten_order_fields(r)
                changed = True
        if changed:
            self._save()

    def mark_pending_flatten(
        self,
        token_id: str,
        match_id: str,
        *,
        reason: str,
        increment_attempt: bool = True,
        order: dict[str, Any] | None = None,
    ) -> None:
        """Keep lot open but flag for retry on next watch tick."""
        mid = str(match_id)
        tid = str(token_id)
        changed = False
        for r in self._rows:
            if (
                r.get("status") == "open"
                and str(r.get("match_id")) == mid
                and str(r.get("token_id")) == tid
            ):
                r["pending_flatten"] = True
                r["pending_reason"] = str(reason or "")[:500]
                if increment_attempt:
                    r["flatten_attempts"] = int(r.get("flatten_attempts") or 0) + 1
                if order is not None:
                    _clear_flatten_order_fields(r)
                    for key in _FLATTEN_ORDER_FIELDS:
                        if order.get(key) is not None:
                            r[key] = order[key]
                changed = True
        if changed:
            self._save()

    def update_pending_flatten_order(
        self,
        token_id: str,
        match_id: str,
        **fields: Any,
    ) -> None:
        """Persist status/check metadata without counting another sell attempt."""
        mid = str(match_id)
        tid = str(token_id)
        changed = False
        allowed = set(_FLATTEN_ORDER_FIELDS)
        for row in self._rows:
            if (
                row.get("status") == "open"
                and str(row.get("match_id")) == mid
                and str(row.get("token_id")) == tid
            ):
                for key, value in fields.items():
                    if key in allowed and value is not None:
                        row[key] = value
                        changed = True
        if changed:
            self._save()

    def clear_pending_flatten_order(
        self,
        token_id: str,
        match_id: str,
        *,
        reason: str | None = None,
    ) -> None:
        """Forget a terminal/expired order while keeping the lot retryable."""
        mid = str(match_id)
        tid = str(token_id)
        changed = False
        for row in self._rows:
            if (
                row.get("status") == "open"
                and str(row.get("match_id")) == mid
                and str(row.get("token_id")) == tid
            ):
                _clear_flatten_order_fields(row)
                if reason is not None:
                    row["pending_reason"] = str(reason)[:500]
                changed = True
        if changed:
            self._save()

    def pending_flatten_lots(self) -> list[dict[str, Any]]:
        return [
            r
            for r in self._rows
            if r.get("status") == "open" and r.get("pending_flatten")
        ]

    def purge_dry_run_opens(self, *, reason: str = "pre_live_purge") -> int:
        """Close simulated (live=False) open lots before enabling live trading."""
        return self.purge_dry_run_opens_for_signals(
            {"score_change", "match_finished"},
            reason=reason,
            purge_unknown=True,
        )

    def purge_dry_run_opens_for_signals(
        self,
        signals: set[str],
        *,
        reason: str = "pre_live_purge",
        purge_unknown: bool = False,
    ) -> int:
        """Close dry open lots whose event_key signal is in ``signals``.

        When both score_change and match_finished are live, pass both and
        ``purge_unknown=True`` to match the old full purge.
        """
        if not signals:
            return 0
        n = 0
        for r in self._rows:
            if r.get("status") != "open" or r.get("live"):
                continue
            ek = str(r.get("event_key") or "")
            sig = ek.split("|", 1)[0] if ek else ""
            if sig in signals or (purge_unknown and not sig):
                r["status"] = "closed"
                r["close_reason"] = reason
                r.pop("pending_flatten", None)
                n += 1
        if n:
            self._save()
        return n

    def all_open(self) -> list[dict[str, Any]]:
        return [r for r in self._rows if r.get("status") == "open"]

    def rest_reserved_usdc(
        self,
        *,
        token_id: str = "",
        match_id: str = "",
        base_event_key: str = "",
    ) -> float:
        """USDC still working on live GTC/GTD bids (not yet matched)."""
        tid = str(token_id or "")
        mid = str(match_id or "")
        base = str(base_event_key or "")
        total = 0.0
        for row in self._rows:
            if row.get("status") != "open":
                continue
            if tid and str(row.get("token_id") or "") != tid:
                continue
            if mid and str(row.get("match_id") or "") != mid:
                continue
            if base and str(row.get("event_key") or "") != base:
                continue
            for order in row.get("rest_orders") or []:
                if not isinstance(order, dict):
                    continue
                if not rest_order_is_live(order):
                    continue
                total += rest_order_working_usdc(order)
        return total

    def live_rest_orders(
        self,
        *,
        match_id: str = "",
        token_id: str = "",
        keep_token_ids: set[str] | None = None,
    ) -> list[tuple[dict[str, Any], dict[str, Any]]]:
        """(lot, rest_order) pairs still working on the book."""
        mid = str(match_id or "")
        tid = str(token_id or "")
        keep = keep_token_ids
        out: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for row in self._rows:
            if row.get("status") != "open":
                continue
            if mid and str(row.get("match_id") or "") != mid:
                continue
            row_tid = str(row.get("token_id") or "")
            if tid and row_tid != tid:
                continue
            if keep is not None and row_tid in keep:
                continue
            for order in row.get("rest_orders") or []:
                if isinstance(order, dict) and rest_order_is_live(order):
                    out.append((row, order))
        return out

    def add_rest_order(
        self,
        *,
        match_id: str,
        token_id: str,
        order: dict[str, Any],
        market_key: str = "",
        family: str = "",
        event_key: str = "",
        home: str = "",
        away: str = "",
        home_score: Any = None,
        away_score: Any = None,
        live: bool = True,
        tick_size: str = "0.01",
        neg_risk: bool | None = None,
    ) -> None:
        """Attach a live rest bid to the match+token lot (creates a 0-share lot)."""
        mid, tid = str(match_id), str(token_id)
        if not mid or not tid or not isinstance(order, dict):
            return
        existing = None
        for row in self._rows:
            if (
                row.get("status") == "open"
                and str(row.get("match_id")) == mid
                and str(row.get("token_id")) == tid
            ):
                existing = row
                break
        if existing is None:
            sc = score_pair(home_score, away_score)
            existing = {
                "status": "open",
                "match_id": mid,
                "token_id": tid,
                "market_key": market_key,
                "family": family,
                "shares": 0.0,
                "usdc": 0.0,
                "entry_score": list(sc) if sc else None,
                "home": home,
                "away": away,
                "live": bool(live),
                "event_key": event_key,
                "tick_size": tick_size or "0.01",
                "neg_risk": neg_risk,
                "fill_status": FILL_STATUS_OPEN,
                "rest_orders": [],
            }
            self._rows.append(existing)
        orders = list(existing.get("rest_orders") or [])
        orders.append(dict(order))
        existing["rest_orders"] = orders
        self._save()

    def update_rest_order(
        self,
        *,
        match_id: str,
        token_id: str,
        order_id: str,
        **fields: Any,
    ) -> dict[str, Any] | None:
        mid, tid, oid = str(match_id), str(token_id), str(order_id or "")
        if not oid:
            return None
        for row in self._rows:
            if (
                row.get("status") != "open"
                or str(row.get("match_id")) != mid
                or str(row.get("token_id")) != tid
            ):
                continue
            for order in row.get("rest_orders") or []:
                if not isinstance(order, dict):
                    continue
                if str(order.get("order_id") or "") != oid:
                    continue
                order.update(fields)
                self._save()
                return dict(order)
        return None

    def close_empty_rest_lot(self, token_id: str, match_id: str, *, reason: str) -> bool:
        """Close a lot that has no inventory and no live rest bids."""
        mid, tid = str(match_id), str(token_id)
        for row in self._rows:
            if (
                row.get("status") == "open"
                and str(row.get("match_id")) == mid
                and str(row.get("token_id")) == tid
            ):
                if float(row.get("shares") or 0) > 1e-9:
                    return False
                if any(
                    rest_order_is_live(o)
                    for o in (row.get("rest_orders") or [])
                    if isinstance(o, dict)
                ):
                    return False
                row["status"] = "closed"
                row["close_reason"] = reason
                self._save()
                return True
        return False


def rest_order_is_live(order: dict[str, Any]) -> bool:
    status = str(order.get("status") or "").upper()
    if status in ("CANCELED", "CANCELLED", "MATCHED", "FILLED", "EXPIRED"):
        return False
    working = rest_order_working_usdc(order)
    return working > 1e-9


def rest_order_working_usdc(order: dict[str, Any]) -> float:
    try:
        usdc = float(order.get("usdc") or 0)
        filled = float(order.get("filled_usdc") or 0)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, usdc - filled)
