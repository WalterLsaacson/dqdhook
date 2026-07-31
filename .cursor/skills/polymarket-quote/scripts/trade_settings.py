"""Trade settings: env auth (simple_str pattern) + fill depth caps."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


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
    min_buy_price: float  # buy_win only: skip (but record) when best_ask < this
    min_order_shares: float
    enabled: bool

    @property
    def live(self) -> bool:
        """True if either signal channel posts real CLOB orders."""
        return bool(self.live_goals or self.live_ft)


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
    take_depth: str = "top",
    max_levels: int = 5,
    max_usdc: float = 5.0,
    max_shares: float = 25.0,
    max_slippage: float = 0.03,
    allow_extreme_prices: bool = False,
    min_buy_price: float = 0.8,
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

    depth = (take_depth or "top").strip().lower()
    if depth not in ("top", "walk"):
        raise ValueError(f"take_depth must be 'top' or 'walk', got {take_depth!r}")

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
        max_usdc=float(max_usdc),
        max_shares=float(max_shares),
        max_slippage=float(max_slippage),
        allow_extreme_prices=bool(allow_extreme_prices),
        min_buy_price=max(0.0, float(min_buy_price)),
        # Polymarket market buys are USDC-notional; do not impose a 5-share floor.
        min_order_shares=float(os.getenv("MIN_ORDER_SHARES", "0")),
        enabled=bool(enabled),
    )
