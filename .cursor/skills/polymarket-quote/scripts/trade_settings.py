"""Trade settings: env auth (simple_str pattern) + fill depth caps."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from size_policy import (
    DEFAULT_MAX_USDC,
    DEFAULT_SIZE_TIERS,
    format_size_tiers,
    parse_size_tiers,
)


def _env_optional_float(name: str) -> float | None:
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return None
    return float(raw)


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _channel_size_tiers(
    env_name: str,
    *,
    inherit: str | None,
    hard: float,
) -> tuple[tuple[float, float], ...]:
    """Parse ``ask:usdc`` tiers. Empty channel env uses inherit, else ``0.98:hard``."""
    raw = os.getenv(env_name)
    if raw is not None and str(raw).strip():
        return tuple(parse_size_tiers(raw))
    if inherit is not None and str(inherit).strip():
        return tuple(parse_size_tiers(inherit))
    return ((0.98, float(hard)),)


def parse_trade_mode(mode: str | None, *, default_live: bool) -> bool:
    """Map dry|live (or None) to bool; None keeps ``default_live``."""
    if mode is None or str(mode).strip() == "":
        return bool(default_live)
    m = str(mode).strip().lower()
    if m not in ("dry", "live"):
        raise ValueError(f"trade mode must be 'dry' or 'live', got {mode!r}")
    return m == "live"


def resolve_live_modes(
    *,
    live: bool = False,
    goals_mode: str | None = None,
    ft_mode: str | None = None,
) -> tuple[bool, bool]:
    """Return (live_goals, live_ft). Per-channel modes override ``--live``."""
    base = bool(live)
    return (
        parse_trade_mode(goals_mode, default_live=base),
        parse_trade_mode(ft_mode, default_live=base),
    )


@dataclass(frozen=True)
class TradeSettings:
    """CLOB auth + in-process execution knobs."""

    private_key: str
    funder: str | None
    signature_type: int
    chain_id: int
    clob_host: str
    data_api_url: str

    live_goals: bool  # score_change
    live_ft: bool  # match_finished
    take_depth: str  # "top" | "walk"
    max_levels: int
    max_usdc: float
    max_shares: float
    max_slippage: float
    allow_extreme_prices: bool
    min_buy_price: float  # buy_win only: skip (but record) when best_ask < this; 0 = off
    min_order_shares: float
    enabled: bool
    # Price-tiered buy sizing (from .env); hard caps remain max_usdc/max_shares.
    size_tiers: tuple[tuple[float, float], ...]
    max_open_usdc: float
    size_floor_usdc: float
    # Per-channel buy caps. None → fall back to max_usdc / max_shares / size_tiers.
    goal_max_usdc: float | None = None
    ft_max_usdc: float | None = None
    goal_max_shares: float | None = None
    ft_max_shares: float | None = None
    goal_size_tiers: tuple[tuple[float, float], ...] | None = None
    ft_size_tiers: tuple[tuple[float, float], ...] | None = None
    # FT locked WIN with ask ≤0.01: try FAK anyway (ghost 0.001 books).
    ft_dust_fak: bool = True
    ft_dust_usdc: float = 100.0

    @property
    def live(self) -> bool:
        """True if either signal channel posts real CLOB orders."""
        return bool(self.live_goals or self.live_ft)

    def caps_for_buy(
        self,
        *,
        event_type: str = "",
        pitch_gate: bool = False,
    ) -> tuple[float, float, tuple[tuple[float, float], ...]]:
        """Return (max_usdc, max_shares, size_tiers) for this buy channel."""
        typ = (event_type or "").strip()
        ft = typ == "match_finished" and not pitch_gate
        if ft:
            usdc = float(
                self.ft_max_usdc if self.ft_max_usdc is not None else self.max_usdc
            )
            shares = float(
                self.ft_max_shares if self.ft_max_shares is not None else self.max_shares
            )
            tiers = (
                self.ft_size_tiers
                if self.ft_size_tiers is not None
                else self.size_tiers
            )
            return usdc, shares, tiers
        usdc = float(
            self.goal_max_usdc if self.goal_max_usdc is not None else self.max_usdc
        )
        shares = float(
            self.goal_max_shares if self.goal_max_shares is not None else self.max_shares
        )
        tiers = (
            self.goal_size_tiers
            if self.goal_size_tiers is not None
            else self.size_tiers
        )
        return usdc, shares, tiers


def _repo_root_from(here: Path) -> Path:
    # .../.cursor/skills/polymarket-quote/scripts/trade_settings.py → repo root
    return here.resolve().parents[4]


def load_trade_settings(
    *,
    live: bool = False,
    live_goals: bool | None = None,
    live_ft: bool | None = None,
    goals_mode: str | None = None,
    ft_mode: str | None = None,
    take_depth: str = "walk",
    max_levels: int = 5,
    max_usdc: float | None = None,
    max_shares: float | None = None,
    max_slippage: float = 0.03,
    allow_extreme_prices: bool = False,
    min_buy_price: float = 0.6,
    enabled: bool = True,
    env_file: str | Path | None = None,
    require_key: bool = False,
) -> TradeSettings:
    """Load PRIVATE_KEY / FUNDER / … from repo .env or --trade-env-file."""
    here = Path(__file__).resolve()
    root = _repo_root_from(here)
    path = Path(env_file) if env_file else root / ".env"
    if path.is_file():
        load_dotenv(path, override=False)

    if live_goals is not None or live_ft is not None:
        g = bool(live_goals) if live_goals is not None else bool(live)
        f = bool(live_ft) if live_ft is not None else bool(live)
    else:
        g, f = resolve_live_modes(live=live, goals_mode=goals_mode, ft_mode=ft_mode)

    private_key = os.getenv("PRIVATE_KEY", "").strip()
    if require_key and not private_key:
        raise ValueError(
            "PRIVATE_KEY not set; copy .env with PRIVATE_KEY/FUNDER "
            "(same names as simple_str) or pass --trade-env-file"
        )

    depth = (take_depth or "walk").strip().lower()
    if depth not in ("top", "walk"):
        raise ValueError(f"take_depth must be 'top' or 'walk', got {take_depth!r}")

    # Shared hard caps: .env QUOTE_MAX_* wins when set; else CLI/hub arg; else defaults.
    if os.getenv("QUOTE_MAX_USDC"):
        hard_usdc = float(os.getenv("QUOTE_MAX_USDC") or DEFAULT_MAX_USDC)
    elif max_usdc is not None:
        hard_usdc = float(max_usdc)
    else:
        hard_usdc = float(DEFAULT_MAX_USDC)
    if os.getenv("QUOTE_MAX_SHARES"):
        hard_shares = float(os.getenv("QUOTE_MAX_SHARES") or 25)
    elif max_shares is not None:
        hard_shares = float(max_shares)
    else:
        hard_shares = 25.0

    goal_usdc = _env_optional_float("QUOTE_GOAL_MAX_USDC")
    ft_usdc = _env_optional_float("QUOTE_FT_MAX_USDC")
    if goal_usdc is None:
        goal_usdc = hard_usdc
    if ft_usdc is None:
        ft_usdc = hard_usdc
    goal_shares = _env_optional_float("QUOTE_GOAL_MAX_SHARES")
    ft_shares = _env_optional_float("QUOTE_FT_MAX_SHARES")
    if goal_shares is None:
        goal_shares = hard_shares
    if ft_shares is None:
        # Keep USDC as the binding cap when FT size is larger than the shared share ceiling.
        ft_shares = max(hard_shares, ft_usdc / 0.25) if ft_usdc > 0 else hard_shares
    goal_tiers = _channel_size_tiers(
        "QUOTE_GOAL_SIZE_TIERS",
        inherit=os.getenv("QUOTE_SIZE_TIERS"),
        hard=goal_usdc,
    )
    ft_tiers = _channel_size_tiers(
        "QUOTE_FT_SIZE_TIERS",
        inherit=None,
        hard=ft_usdc,
    )
    max_open = float(os.getenv("QUOTE_MAX_OPEN_USDC", "1000") or 1000)
    floor_usdc = float(os.getenv("QUOTE_SIZE_FLOOR_USDC", "1") or 1)
    dust_usdc = _env_optional_float("QUOTE_FT_DUST_USDC")

    funder = os.getenv("FUNDER", "").strip() or None
    return TradeSettings(
        private_key=private_key,
        funder=funder,
        signature_type=int(os.getenv("SIGNATURE_TYPE", "2")),
        chain_id=int(os.getenv("CHAIN_ID", "137")),
        clob_host=os.getenv("CLOB_HOST", "https://clob.polymarket.com").rstrip("/"),
        data_api_url=os.getenv("DATA_API_URL", "https://data-api.polymarket.com").rstrip("/"),
        live_goals=g,
        live_ft=f,
        take_depth=depth,
        max_levels=max(1, int(max_levels)),
        max_usdc=goal_usdc,
        max_shares=goal_shares,
        max_slippage=float(max_slippage),
        allow_extreme_prices=bool(allow_extreme_prices),
        min_buy_price=max(0.0, float(min_buy_price)),
        # Polymarket market buys are USDC-notional; do not impose a 5-share floor.
        min_order_shares=float(os.getenv("MIN_ORDER_SHARES", "0")),
        enabled=bool(enabled),
        size_tiers=tuple(goal_tiers) if goal_tiers else tuple(DEFAULT_SIZE_TIERS),
        max_open_usdc=max(0.0, max_open),
        size_floor_usdc=max(0.0, floor_usdc),
        goal_max_usdc=goal_usdc,
        ft_max_usdc=ft_usdc,
        goal_max_shares=goal_shares,
        ft_max_shares=ft_shares,
        goal_size_tiers=tuple(goal_tiers) if goal_tiers else tuple(DEFAULT_SIZE_TIERS),
        ft_size_tiers=tuple(ft_tiers) if ft_tiers else ((0.98, ft_usdc),),
        ft_dust_fak=_env_bool("QUOTE_FT_DUST_FAK", True),
        ft_dust_usdc=(
            float(dust_usdc) if dust_usdc is not None else 100.0
        ),
    )


def size_tiers_label(settings: TradeSettings) -> str:
    return format_size_tiers(settings.size_tiers)


def size_channels_label(settings: TradeSettings) -> str:
    g_u, _, g_t = settings.caps_for_buy(event_type="score_change")
    f_u, _, f_t = settings.caps_for_buy(event_type="match_finished")
    dust = float(getattr(settings, "ft_dust_usdc", 100.0) or 0)
    return (
        f"goals_usdc={g_u:g} tiers={format_size_tiers(g_t)} "
        f"ft_usdc={f_u:g} tiers={format_size_tiers(f_t)} "
        f"ft_dust_usdc={dust:g}"
    )
