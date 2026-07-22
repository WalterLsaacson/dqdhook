#!/usr/bin/env python3
"""CLI for Polymarket soccer match lists (Gamma API).

Examples:
  python3 pm_soccer.py list --json
  python3 pm_soccer.py list --league epl --json
  python3 pm_soccer.py list --league epl,ucl,mls --json
  python3 pm_soccer.py leagues --json
  python3 pm_soccer.py list --no-proxy --json
  python3 pm_soccer.py list --proxy socks5h://127.0.0.1:1082 --json
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import pm_lib as lib  # noqa: E402


def repo_root() -> Path:
    # scripts -> polymarket-soccer -> skills -> .cursor -> repo
    return Path(__file__).resolve().parents[4]


def data_dir(explicit: str | None = None) -> Path:
    root = Path(explicit) if explicit else repo_root() / "data" / "polymarket"
    root.mkdir(parents=True, exist_ok=True)
    return root


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def parse_leagues_arg(raw: str | None) -> list[str] | None:
    if not raw or raw.strip().lower() in ("all", "*"):
        return None
    return [p.strip().lower() for p in raw.split(",") if p.strip()]


def resolve_cli_proxy(args: argparse.Namespace) -> str | None | object:
    """Return proxy arg for lib: ellipsis=default, 'none'=direct, or URL string."""
    if getattr(args, "no_proxy", False):
        return "none"
    explicit = getattr(args, "proxy", None)
    if explicit is not None:
        return explicit
    return ...


def cmd_leagues(args: argparse.Namespace) -> int:
    proxy = resolve_cli_proxy(args)
    try:
        catalog = lib.soccer_league_catalog(proxy=proxy)
        proxy_url = lib.resolve_proxy(
            None if proxy is ... else (None if proxy == "none" else str(proxy))
        )
    except lib.FetchError as e:
        print(f"fetch failed: {e}", file=sys.stderr)
        return 1
    payload = {
        "fetched_at": datetime.now(lib.TZ_CN).isoformat(timespec="seconds"),
        "source": "polymarket-gamma",
        "proxy": proxy_url or "direct",
        "count": len(catalog),
        "leagues": catalog,
    }
    write_json(data_dir(args.data_dir) / "leagues.json", payload)
    json.dump(payload, sys.stdout, ensure_ascii=False, indent=2)
    print()
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    leagues = parse_leagues_arg(args.league)
    proxy = resolve_cli_proxy(args)
    try:
        payload = lib.load_matches(
            leagues,
            include_closed=args.include_closed,
            max_per_league=args.max,
            within_hours=args.within_hours,
            proxy=proxy,
        )
    except lib.FetchError as e:
        print(f"fetch failed: {e}", file=sys.stderr)
        return 1

    ddir = data_dir(args.data_dir)
    write_json(ddir / "snapshot.json", payload)
    json.dump(payload, sys.stdout, ensure_ascii=False, indent=2)
    print()
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Polymarket soccer games via Gamma API")
    p.add_argument(
        "--data-dir",
        default=None,
        help="State directory (default: <repo>/data/polymarket)",
    )
    p.add_argument(
        "--proxy",
        default=None,
        help=(
            "Outbound proxy URL (default: http://127.0.0.1:1082). "
            "Also: PM_PROXY / ALL_PROXY. Bare host:port → socks5h://"
        ),
    )
    p.add_argument(
        "--no-proxy",
        action="store_true",
        help="Disable proxy (direct HTTPS)",
    )
    sub = p.add_subparsers(dest="command", required=True)

    list_p = sub.add_parser("list", help="Fetch soccer matchup list as JSON")
    list_p.add_argument(
        "--league",
        default="all",
        help="League code(s), comma-separated (default: all soccer allowlist)",
    )
    list_p.add_argument(
        "--max",
        type=int,
        default=100,
        help="Max events to fetch per league (default: 100)",
    )
    list_p.add_argument(
        "--include-closed",
        action="store_true",
        help="Include closed events",
    )
    list_p.add_argument(
        "--within-hours",
        type=int,
        default=lib.DEFAULT_WITHIN_HOURS,
        help=(
            "Only fixtures with gameStartTime in the next N hours "
            f"(default: {lib.DEFAULT_WITHIN_HOURS}). Use 0 for no time window."
        ),
    )
    list_p.add_argument("--json", action="store_true", default=True, help="Emit JSON (default)")
    list_p.set_defaults(func=cmd_list)

    leagues_p = sub.add_parser("leagues", help="List available soccer league codes")
    leagues_p.add_argument("--json", action="store_true", default=True)
    leagues_p.set_defaults(func=cmd_leagues)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
