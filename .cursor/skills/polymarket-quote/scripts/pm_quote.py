#!/usr/bin/env python3
"""CLI for polymarket-quote: post-FT CLOB quoting from bridge match_finished.

Examples:
  python3 pm_quote.py once --from-bridge --json
  python3 pm_quote.py once --match-id 54363289 --home-score 2 --away-score 1 --json
  python3 pm_quote.py once --event-id 674336 --home-score 1 --away-score 0 --json
  python3 pm_quote.py watch --interval 2
  python3 pm_quote.py watch --take-depth walk --max-usdc 10   # dry-run trade plans
  python3 pm_quote.py watch --live --take-depth top --max-usdc 2
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import quote_lib as lib  # noqa: E402
from trade_executor import TradeExecutor  # noqa: E402
from trade_settings import load_trade_settings  # noqa: E402


def root() -> Path:
    return lib.repo_root_from(Path(__file__))


def build_executor(args: argparse.Namespace, rt: Path) -> TradeExecutor | None:
    """Build TradeExecutor when trading is enabled (default on; --no-trade disables)."""
    if getattr(args, "no_trade", False):
        return None
    live = bool(getattr(args, "live", False))
    settings = load_trade_settings(
        live=live,
        take_depth=str(getattr(args, "take_depth", "top") or "top"),
        max_levels=int(getattr(args, "max_levels", 5)),
        max_usdc=float(getattr(args, "max_usdc", 5.0)),
        max_shares=float(getattr(args, "max_shares", 25.0)),
        max_slippage=float(getattr(args, "max_slippage", 0.03)),
        allow_extreme_prices=bool(getattr(args, "allow_extreme_prices", False)),
        enabled=True,
        env_file=getattr(args, "trade_env_file", None),
        require_key=live,
    )
    executor = TradeExecutor(rt, settings)
    # Plan: initialize ClobClient once at watch/start when trading is on (reuse).
    # Also needed in dry-run so sell_lose can query position.
    if settings.private_key:
        try:
            executor.ensure_trader()
        except Exception as e:  # noqa: BLE001
            if live:
                raise
            print(
                f"trade → CLOB init failed (sell position checks disabled): {e}",
                file=sys.stderr,
                flush=True,
            )
    if settings.live:
        print(
            f"trade → LIVE take_depth={settings.take_depth} "
            f"max_usdc={settings.max_usdc} max_shares={settings.max_shares}",
            file=sys.stderr,
            flush=True,
        )
    else:
        print(
            f"trade → dry-run take_depth={settings.take_depth} "
            f"max_usdc={settings.max_usdc} (pass --live to post)",
            file=sys.stderr,
            flush=True,
        )
    return executor


def cmd_once(args: argparse.Namespace) -> int:
    rt = root()
    proxy = None if args.no_proxy else (args.proxy if args.proxy else ...)
    include_props = not args.moneyline_only
    include_exact = not args.moneyline_only and not args.no_exact
    try:
        executor = build_executor(args, rt)
    except Exception as e:  # noqa: BLE001
        print(f"trade setup failed: {e}", file=sys.stderr)
        return 1

    try:
        if args.from_bridge:
            bundles = lib.process_bridge_events(
                rt,
                proxy=proxy,
                include_props=include_props,
                include_exact=include_exact,
                eps=float(args.eps),
                fee_rate=float(args.fee_rate),
                min_net=float(args.min_net),
                force=bool(args.force),
                trade_executor=executor,
            )
            payload = {
                "quoted_at": lib.now_cn_iso(),
                "source": "polymarket-quote",
                "mode": "from_bridge",
                "count": len(bundles),
                "bundles": bundles,
            }
        else:
            row = lib.find_match_row(
                rt, match_id=args.match_id, event_id=args.event_id
            )
            if not row:
                print(
                    "match not found in data/bridge/matches.json; "
                    "pass --match-id or --event-id of a bridged fixture",
                    file=sys.stderr,
                )
                return 1
            if args.home_score is None or args.away_score is None:
                dqd = row.get("dongqiudi") or {}
                hs, aws = dqd.get("home_score"), dqd.get("away_score")
                if hs is None or aws is None:
                    print(
                        "need --home-score and --away-score (fixture not finished in bridge)",
                        file=sys.stderr,
                    )
                    return 1
            else:
                hs, aws = int(args.home_score), int(args.away_score)
            ev = lib.synthetic_ft_from_row(row, home_score=int(hs), away_score=int(aws))
            payload = lib.quote_finished_event(
                rt,
                ev,
                proxy=proxy,
                include_props=include_props,
                include_exact=include_exact,
                eps=float(args.eps),
                fee_rate=float(args.fee_rate),
                min_net=float(args.min_net),
                persist=not args.no_persist,
                trade_executor=executor,
            )
    except Exception as e:  # noqa: BLE001
        print(f"quote failed: {e}", file=sys.stderr)
        return 1

    if args.json:
        json.dump(payload, sys.stdout, ensure_ascii=False, indent=2)
        print()
    else:
        if isinstance(payload, dict) and "bundles" in payload:
            print(
                f"processed {payload.get('count')} FT events → data/pm-quote/",
                flush=True,
            )
        else:
            print(
                f"quoted {payload.get('home')} {payload.get('home_score')}-"
                f"{payload.get('away_score')} {payload.get('away')} "
                f"tokens={payload.get('count')} opps={payload.get('opportunity_count')}",
                flush=True,
            )
    return 0


def cmd_watch(args: argparse.Namespace) -> int:
    rt = root()
    proxy = None if args.no_proxy else (args.proxy if args.proxy else ...)
    include_props = not args.moneyline_only
    include_exact = not args.moneyline_only and not args.no_exact
    interval = max(1, int(args.interval))

    if not args.no_upstream:
        print(
            "upstream → starting match-bridge "
            "(dongqiudi-match + polymarket-soccer)…",
            file=sys.stderr,
            flush=True,
        )
        try:
            up = lib.ensure_upstream_bridge(rt)
            mode = up.get("mode")
            already = "already running" if up.get("already") else "started"
            if mode == "bridge_board":
                print(
                    f"upstream → bridge-board {already} @ {up.get('url')} "
                    f"(DQD ticks={up.get('dqd_ticks')} · PM ticks={up.get('pm_ticks')})",
                    file=sys.stderr,
                    flush=True,
                )
            else:
                print(
                    f"upstream → match-bridge {already} in-process "
                    f"(DQD {up.get('dqd_interval')}/{up.get('dqd_idle_interval')}s · "
                    f"PM {up.get('pm_interval')}s)",
                    file=sys.stderr,
                    flush=True,
                )
        except Exception as e:  # noqa: BLE001
            print(f"upstream → failed to start bridge: {e}", file=sys.stderr, flush=True)
            return 1

    try:
        executor = build_executor(args, rt)
    except Exception as e:  # noqa: BLE001
        print(f"trade setup failed: {e}", file=sys.stderr)
        return 1

    print(
        f"polymarket-quote watch (interval={interval}s) → {lib.data_dir(rt)}",
        file=sys.stderr,
        flush=True,
    )
    try:
        while True:
            bundles = lib.process_bridge_events(
                rt,
                proxy=proxy,
                include_props=include_props,
                include_exact=include_exact,
                eps=float(args.eps),
                fee_rate=float(args.fee_rate),
                min_net=float(args.min_net),
                force=False,
                trade_executor=executor,
            )
            for b in bundles:
                if b.get("error"):
                    print(
                        f"error match_id={b.get('match_id')} "
                        f"trigger={b.get('trigger')}: {b.get('error')}",
                        flush=True,
                    )
                else:
                    prev = b.get("prev_score") or {}
                    prev_s = (
                        f"{prev.get('home')}-{prev.get('away')}→"
                        if prev.get("home") is not None
                        else ""
                    )
                    trades = sum(
                        1
                        for q in (b.get("quotes") or [])
                        if (q.get("trade_attempt") or {}).get("status")
                        in ("dry_run", "posted")
                    )
                    print(
                        f"[{b.get('quoted_at')}] {b.get('trigger')}/{b.get('mode')} "
                        f"{b.get('home')} {prev_s}{b.get('home_score')}-{b.get('away_score')} "
                        f"{b.get('away')} quotes={b.get('count')} "
                        f"opps={b.get('opportunity_count')} trades={trades}",
                        flush=True,
                    )
                    if args.json:
                        json.dump(b, sys.stdout, ensure_ascii=False)
                        print(flush=True)
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\nbye", file=sys.stderr)
        if not args.no_upstream:
            lib.stop_owned_bridge()
        return 0


def _add_common_flags(sp: argparse.ArgumentParser) -> None:
    sp.add_argument("--proxy", default=None, help="Proxy URL (default PM_PROXY / 127.0.0.1:1082)")
    sp.add_argument("--no-proxy", action="store_true", help="Disable proxy")
    sp.add_argument("--eps", type=float, default=0.005, help="Min gross edge vs 0/1 before fee check")
    sp.add_argument(
        "--fee-rate",
        type=float,
        default=lib.SPORTS_TAKER_FEE_RATE,
        help=f"Sports taker feeRate (default {lib.SPORTS_TAKER_FEE_RATE})",
    )
    sp.add_argument(
        "--min-net",
        type=float,
        default=lib.DEFAULT_MIN_NET,
        help=f"Min net edge/share after fee to write opportunities (default {lib.DEFAULT_MIN_NET})",
    )
    sp.add_argument(
        "--moneyline-only",
        action="store_true",
        help="Skip More Markets / Exact Score discovery",
    )
    sp.add_argument("--no-exact", action="store_true", help="Skip Exact Score only")
    # --- in-process trading (after misprice; default dry-run) ---
    sp.add_argument(
        "--no-trade",
        action="store_true",
        help="Disable in-process trading (quote only)",
    )
    sp.add_argument(
        "--live",
        action="store_true",
        help="Post real CLOB market orders (default is dry-run)",
    )
    sp.add_argument(
        "--take-depth",
        choices=("top", "walk"),
        default="top",
        help="top=best level only; walk=deeper into asks_top/bids_top (default top)",
    )
    sp.add_argument(
        "--max-levels",
        type=int,
        default=5,
        help="Walk max book levels (default 5, matches TOP_N)",
    )
    sp.add_argument("--max-usdc", type=float, default=5.0, help="Max USDC per buy (default 5)")
    sp.add_argument(
        "--max-shares",
        type=float,
        default=25.0,
        help="Max shares per order (default 25)",
    )
    sp.add_argument(
        "--max-slippage",
        type=float,
        default=0.03,
        help="Walk: max adverse price move from best (default 0.03)",
    )
    sp.add_argument(
        "--allow-extreme-prices",
        action="store_true",
        help="Allow orders when best price <=0.01 or >=0.99 (default blocked)",
    )
    sp.add_argument(
        "--trade-env-file",
        default=None,
        help="Env file with PRIVATE_KEY/FUNDER/… (default repo .env)",
    )


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Post-FT Polymarket CLOB quoting")
    sub = p.add_subparsers(dest="command", required=True)

    once = sub.add_parser("once", help="Quote once (bridge backlog or single match)")
    _add_common_flags(once)
    once.add_argument("--from-bridge", action="store_true", help="Process new match_finished events")
    once.add_argument("--force", action="store_true", help="Re-process already-seen FT keys")
    once.add_argument("--match-id", default=None, help="Dongqiudi match id")
    once.add_argument("--event-id", default=None, help="Polymarket event id")
    once.add_argument("--home-score", type=int, default=None)
    once.add_argument("--away-score", type=int, default=None)
    once.add_argument("--no-persist", action="store_true")
    once.add_argument("--json", action="store_true")
    once.set_defaults(func=cmd_once)

    watch = sub.add_parser(
        "watch",
        help="Poll bridge events and quote; autostarts match-bridge (+ DQD + PM)",
    )
    _add_common_flags(watch)
    watch.add_argument("--interval", type=int, default=2, help="Poll seconds (default 2)")
    watch.add_argument("--json", action="store_true", help="Emit each bundle as JSON line")
    watch.add_argument(
        "--no-upstream",
        action="store_true",
        help="Do not autostart match-bridge / dongqiudi-match / polymarket-soccer",
    )
    watch.set_defaults(func=cmd_watch)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
