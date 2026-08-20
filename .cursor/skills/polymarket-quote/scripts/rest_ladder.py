"""GTC/GTD rest-buy ladder after FAK (A/B remaining toward target)."""

from __future__ import annotations

import os
from decimal import ROUND_DOWN, Decimal
from typing import Any

# Willing-to-pay bids after FAK: 0.99 first, then 0.98.
DEFAULT_REST_PRICES = (0.99, 0.98)
# Align with quote_lib.DEFAULT_MAX_BUY_ASK — ask at or below this is FAK, not rest.
FAK_ZONE_MAX_ASK = 0.992
# When the book bid already sits here, rest only at 0.99 (skip 0.98).
REST_CONCENTRATE_BID = 0.99
# GTD safety net; 0 → GTC. Reversal/FT still cancel immediately.
DEFAULT_REST_EXPIRE_S = 3600.0
MIN_REST_USDC = 1.0
# CLOB limit buys reject below this share size on soccer markets.
MIN_REST_SHARES = 5.0
_SHARE_Q = Decimal("0.01")
# CLOB limit orders reject tick_size below 0.01 on many soccer tokens even
# when the book metadata (or exact-score rows) reports 0.001.
REST_LIMIT_MIN_TICK = 0.01
# Master switch for GTD/GTC rest bids (pitch-gate fallback + cushion). Off until stable.
DEFAULT_REST_ENABLED = False


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def rest_enabled() -> bool:
    """``QUOTE_REST_ENABLED`` — default off; set 1 to post limit rest bids."""
    return _env_bool("QUOTE_REST_ENABLED", DEFAULT_REST_ENABLED)


def min_rest_usdc(price: float, *, min_shares: float = MIN_REST_SHARES) -> float:
    """USDC needed for ``min_shares`` at ``price`` (CLOB limit floor)."""
    try:
        px = float(price)
    except (TypeError, ValueError):
        return float(MIN_REST_USDC)
    if px <= 0:
        return float(MIN_REST_USDC)
    return round(max(float(min_shares), 0.0) * px, 6)


def rest_limit_tick_size(tick_size: str | float | None) -> str:
    """Tick string for GTD/GTC limit posts — never below REST_LIMIT_MIN_TICK."""
    try:
        tick = float(tick_size or REST_LIMIT_MIN_TICK)
    except (TypeError, ValueError):
        tick = REST_LIMIT_MIN_TICK
    tick = max(REST_LIMIT_MIN_TICK, tick)
    if abs(tick - round(tick, 2)) < 1e-9:
        return f"{tick:.2f}"
    return str(tick)


def rest_expire_s() -> float:
    raw = os.getenv("QUOTE_REST_EXPIRE_S")
    if raw is None or str(raw).strip() == "":
        return float(DEFAULT_REST_EXPIRE_S)
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return float(DEFAULT_REST_EXPIRE_S)


def ask_in_fak_zone(best_ask: Any) -> bool:
    """True when a tradeable ask exists — remainder should FAK, not rest."""
    if best_ask is None:
        return False
    try:
        return float(best_ask) <= FAK_ZONE_MAX_ASK + 1e-12
    except (TypeError, ValueError):
        return False


def select_rest_prices(
    *,
    best_bid: Any = None,
    best_ask: Any = None,
    prices: tuple[float, ...] | None = None,
) -> tuple[float, ...]:
    """Pick rest ladder prices; empty tuple means do not rest (FAK zone)."""
    if prices is not None:
        return prices
    if ask_in_fak_zone(best_ask):
        return ()
    try:
        bid = float(best_bid) if best_bid is not None else None
    except (TypeError, ValueError):
        bid = None
    if bid is not None and bid + 1e-12 >= REST_CONCENTRATE_BID:
        return (REST_CONCENTRATE_BID,)
    return rest_prices()


def rest_prices() -> tuple[float, ...]:
    raw = os.getenv("QUOTE_REST_PRICES")
    if raw is None or str(raw).strip() == "":
        return DEFAULT_REST_PRICES
    out: list[float] = []
    for part in str(raw).split(","):
        part = part.strip()
        if not part:
            continue
        try:
            px = float(part)
        except (TypeError, ValueError):
            continue
        if 0.01 <= px < 1.0:
            out.append(px)
    return tuple(out) if out else DEFAULT_REST_PRICES


def snap_rest_price(price: float, tick_size: str | float | None) -> float | None:
    """Tick-snap a rest bid; drop at/above par or unsnappable ticks."""
    try:
        px = float(price)
        tick = float(tick_size or 0.01)
    except (TypeError, ValueError):
        return None
    if tick <= 0 or px <= 0:
        return None
    snapped = round(round(px / tick) * tick, 6)
    if snapped <= 0 or snapped >= 1.0 - 1e-12:
        return None
    return snapped


def shares_for_usdc(usdc: float, price: float) -> float:
    if price <= 0 or usdc <= 0:
        return 0.0
    sh = (Decimal(str(usdc)) / Decimal(str(price))).quantize(
        _SHARE_Q, rounding=ROUND_DOWN
    )
    return float(sh)


def allocate_rest_ladder(
    remaining_usdc: float,
    *,
    prices: tuple[float, ...] | None = None,
    tick_size: str | float | None = "0.01",
    floor_usdc: float = MIN_REST_USDC,
    best_bid: Any = None,
    best_ask: Any = None,
    min_shares: float = MIN_REST_SHARES,
) -> list[dict[str, Any]]:
    """Split remaining USDC across rest prices (0.99 only when bid≥0.99, else 0.99+0.98)."""
    tick_size = rest_limit_tick_size(tick_size)
    need = max(0.0, float(remaining_usdc))
    floor = max(0.0, float(floor_usdc))
    share_floor = max(0.0, float(min_shares))
    if need + 1e-12 < floor:
        return []
    chosen = select_rest_prices(best_bid=best_bid, best_ask=best_ask, prices=prices)
    if not chosen:
        return []
    snapped: list[float] = []
    seen: set[float] = set()
    for raw in chosen:
        px = snap_rest_price(raw, tick_size)
        if px is None or px in seen:
            continue
        seen.add(px)
        snapped.append(px)
    if not snapped:
        return []
    # Raise budget so at least one level clears CLOB min shares (e.g. 5 @ 0.99).
    level_floor = floor
    if share_floor > 0:
        need = max(need, min_rest_usdc(min(snapped), min_shares=share_floor))
        level_floor = max(
            floor, min_rest_usdc(min(snapped), min_shares=share_floor)
        )
    # Only split when each side can still clear the share floor; otherwise
    # concentrate on the first price (avoids 2× min-share overspend).
    if len(snapped) == 1 or need + 1e-12 < 2.0 * level_floor:
        chunks = [need]
        snapped = snapped[:1]
    else:
        half = round(need / 2.0, 6)
        if half + 1e-12 < level_floor:
            chunks = [need]
            snapped = snapped[:1]
        else:
            chunks = [need - half, half]
            # First price (0.99) gets the larger leftover.
            if chunks[0] + 1e-12 < level_floor:
                chunks = [need]
                snapped = snapped[:1]
    levels: list[dict[str, Any]] = []
    share_tick = float(_SHARE_Q)
    for px, usdc in zip(snapped, chunks):
        if usdc + 1e-12 < floor:
            continue
        shares = shares_for_usdc(usdc, px)
        cost = round(shares * px, 6)
        need_floor = min(floor, usdc)
        # ROUND_DOWN at 0.99 can leave $1 → 0.9999; bump one share tick so the
        # notional still clears the floor (slight overspend ≤ one tick).
        if shares > 0 and cost + 1e-12 < need_floor:
            bumped = round(shares + share_tick, 6)
            bumped_cost = round(bumped * px, 6)
            if (
                bumped_cost + 1e-12 >= need_floor
                and bumped_cost <= usdc + share_tick * px + 1e-9
            ):
                shares, cost = bumped, bumped_cost
        if share_floor > 0 and shares + 1e-12 < share_floor:
            shares = float(share_floor)
            cost = round(shares * px, 6)
        if shares <= 0 or cost + 1e-12 < need_floor:
            continue
        levels.append(
            {
                "price": px,
                "shares": round(shares, 6),
                "usdc": cost,
            }
        )
    return levels
