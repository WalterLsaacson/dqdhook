"""Trade settings: env auth (simple_str pattern) + fill depth caps."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True)
class TradeSettings:
    """CLOB auth + in-process execution knobs."""

    private_key: str
    funder: str | None
    signature_type: int
    chain_id: int
    clob_host: str
    data_api_url: str

    live: bool
    take_depth: str  # "top" | "walk"
    max_levels: int
    max_usdc: float
    max_shares: float
    max_slippage: float
    allow_extreme_prices: bool
    min_order_shares: float
    enabled: bool


def _repo_root_from(here: Path) -> Path:
    # .../.cursor/skills/polymarket-quote/scripts/trade_settings.py → repo root
    return here.resolve().parents[4]


def load_trade_settings(
    *,
    live: bool = False,
    take_depth: str = "top",
    max_levels: int = 5,
    max_usdc: float = 5.0,
    max_shares: float = 25.0,
    max_slippage: float = 0.03,
    allow_extreme_prices: bool = False,
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
        live=bool(live),
        take_depth=depth,
        max_levels=max(1, int(max_levels)),
        max_usdc=float(max_usdc),
        max_shares=float(max_shares),
        max_slippage=float(max_slippage),
        allow_extreme_prices=bool(allow_extreme_prices),
        # Polymarket market buys are USDC-notional; do not impose a 5-share floor.
        min_order_shares=float(os.getenv("MIN_ORDER_SHARES", "0")),
        enabled=bool(enabled),
    )
