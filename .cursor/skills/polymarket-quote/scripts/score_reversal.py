"""Score-reversal helpers + open buy_win ledger for emergency FAK flatten."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger("pm_quote.reversal")


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
    ) -> None:
        if not match_id or not token_id or shares <= 0:
            return
        sc = score_pair(home_score, away_score)
        row = {
            "status": "open",
            "match_id": str(match_id),
            "token_id": str(token_id),
            "market_key": market_key,
            "family": family,
            "shares": float(shares),
            "usdc": float(usdc),
            "entry_score": list(sc) if sc else None,
            "home": home,
            "away": away,
            "live": bool(live),
            "event_key": event_key,
            "tick_size": tick_size or "0.01",
            "neg_risk": neg_risk,
        }
        # Replace prior open lot for same match+token (idempotent re-quote)
        self._rows = [
            r
            for r in self._rows
            if not (
                r.get("status") == "open"
                and str(r.get("match_id")) == str(match_id)
                and str(r.get("token_id")) == str(token_id)
            )
        ]
        self._rows.append(row)
        self._save()
        logger.info(
            "ledger open match=%s token=%s… shares=%.4f entry=%s",
            match_id,
            token_id[:12],
            shares,
            sc,
        )

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
                changed = True
        if changed:
            self._save()

    def mark_pending_flatten(
        self,
        token_id: str,
        match_id: str,
        *,
        reason: str,
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
                r["pending_reason"] = reason
                r["flatten_attempts"] = int(r.get("flatten_attempts") or 0) + 1
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
        n = 0
        for r in self._rows:
            if r.get("status") == "open" and not r.get("live"):
                r["status"] = "closed"
                r["close_reason"] = reason
                r.pop("pending_flatten", None)
                n += 1
        if n:
            self._save()
        return n

    def all_open(self) -> list[dict[str, Any]]:
        return [r for r in self._rows if r.get("status") == "open"]
