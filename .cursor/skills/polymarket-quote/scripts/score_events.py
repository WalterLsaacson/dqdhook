"""Dongqiudi score-change helpers (goal-up / reversal / stale age)."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any

TZ_CN = timezone(timedelta(hours=8))
DEFAULT_FT_MAX_AGE_S = 900.0
_FT_MAX_AGE_ENV = "QUOTE_FT_MAX_AGE_S"


def event_is_goal_up(ev: dict[str, Any]) -> bool:
    if str(ev.get("type") or "") != "score_change":
        return False
    if ev.get("is_reversal"):
        return False
    if ev.get("is_goal") is True:
        return True
    prev = ev.get("prev") or {}
    curr = ev.get("curr") or {}
    try:
        ph = int(prev.get("home"))
        pa = int(prev.get("away"))
        ch = int(curr.get("home", ev.get("home_score")))
        ca = int(curr.get("away", ev.get("away_score")))
    except (TypeError, ValueError):
        return False
    return ch >= ph and ca >= pa and (ch > ph or ca > pa)


def event_is_reversal(ev: dict[str, Any]) -> bool:
    if str(ev.get("type") or "") != "score_change":
        return False
    if ev.get("is_reversal"):
        return True
    prev = ev.get("prev") or {}
    curr = ev.get("curr") or {}
    try:
        ph = int(prev.get("home"))
        pa = int(prev.get("away"))
        ch = int(curr.get("home", ev.get("home_score")))
        ca = int(curr.get("away", ev.get("away_score")))
    except (TypeError, ValueError):
        return False
    return ch < ph or ca < pa


def target_score_from_event(ev: dict[str, Any]) -> tuple[int, int] | None:
    curr = ev.get("curr") or {}
    try:
        h = curr.get("home", ev.get("home_score"))
        a = curr.get("away", ev.get("away_score"))
        if h is None or a is None:
            return None
        return int(h), int(a)
    except (TypeError, ValueError):
        return None


def ft_max_age_s(override: float | None = None) -> float:
    if override is not None:
        return max(0.0, float(override))
    raw = os.getenv(_FT_MAX_AGE_ENV)
    if raw is None or str(raw).strip() == "":
        return float(DEFAULT_FT_MAX_AGE_S)
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return float(DEFAULT_FT_MAX_AGE_S)


def event_age_seconds(ev: dict[str, Any], *, now: datetime | None = None) -> float | None:
    ts = ev.get("ts")
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts))
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ_CN)
    n = now or datetime.now(TZ_CN)
    return max(0.0, (n - dt.astimezone(TZ_CN)).total_seconds())


def ft_event_is_stale(
    ev: dict[str, Any],
    *,
    max_age_s: float | None = None,
    now: datetime | None = None,
) -> tuple[bool, float | None]:
    """True when event ts is older than max_age (0 = disable age check)."""
    age = event_age_seconds(ev, now=now)
    limit = ft_max_age_s(max_age_s)
    if limit <= 0 or age is None:
        return False, age
    return age > limit + 1e-9, age
