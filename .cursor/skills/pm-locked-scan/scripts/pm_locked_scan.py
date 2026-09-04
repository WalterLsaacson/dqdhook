#!/usr/bin/env python3
"""CLI: finished-but-unsettled Polymarket soccer → locked-WIN asks.

Examples:
  python3 pm_locked_scan.py --json
  python3 pm_locked_scan.py --hours 48 --league ptc --json
  python3 pm_locked_scan.py --max-ask 0.995 --json
  python3 pm_locked_scan.py --from-snapshot --refresh-af --json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import locked_scan_lib as lib  # noqa: E402


def _resolve_proxy(args: argparse.Namespace) -> str | None | object:
    if getattr(args, "no_proxy", False):
        return "none"
    if getattr(args, "proxy", None):
        return args.proxy
    return ...


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Scan finished, unsettled Polymarket soccer for locked-WIN asks"
    )
    p.add_argument("--hours", type=int, default=lib.DEFAULT_HOURS, help="Lookback hours (default 48)")
    p.add_argument(
        "--league",
        default="",
        help="League code(s), comma-separated (default: all soccer)",
    )
    p.add_argument("--max-per-league", type=int, default=lib.DEFAULT_MAX_PER_LEAGUE)
    p.add_argument("--limit", type=int, default=None, help="Cap listed matches after filter")
    p.add_argument(
        "--max-ask",
        type=float,
        default=1.0,
        help="Keep asks at or below this price (default 1.0 = any sell)",
    )
    p.add_argument(
        "--tradeable",
        action="store_true",
        help="Shorthand for --max-ask 0.995 (quote bot taker cap)",
    )
    p.add_argument(
        "--from-snapshot",
        action="store_true",
        help="List from data/polymarket/snapshot.json instead of a Gamma league scan",
    )
    p.add_argument(
        "--refresh-af",
        action="store_true",
        help=(
            "Force-refresh API-Football date fixtures (writes shared "
            "data/apifootball/date_fixtures/ and uses AF quota). Default is read-only cache."
        ),
    )
    p.add_argument("--snapshot", default=None, help="Override PM snapshot path")
    p.add_argument("--dqd-snapshot", default=None, help="Override Dongqiudi snapshot path")
    p.add_argument(
        "--require-af",
        action="store_true",
        help=(
            "Settle only from API-Football regulation (score.fulltime + HT). "
            "Skip matches that would have used a Dongqiudi fallback. "
            "Used by the hourly quote sweep."
        ),
    )
    p.add_argument("--proxy", default=None)
    p.add_argument("--no-proxy", action="store_true")
    p.add_argument("--json", action="store_true")
    p.add_argument(
        "--out",
        default=None,
        help="Write JSON (default: data/pm-locked-scan/latest.json)",
    )
    p.add_argument("-q", "--quiet", action="store_true", help="No per-match progress on stderr")
    return p


def _human(payload: dict) -> None:
    print(
        f"listed={payload.get('listed')} scored={payload.get('scored')} "
        f"scored_no_asks={len(payload.get('scored_no_asks') or [])} "
        f"skipped={len(payload.get('skipped') or [])} "
        f"match_hits={payload.get('match_hits')} token_hits={payload.get('token_hits')}"
    )
    for row in payload.get("results") or []:
        sc = row.get("score") or {}
        m = row.get("match") or {}
        print(
            f"\n{m.get('kickoff_beijing')} [{m.get('league_id')}] "
            f"{m.get('home')} {sc.get('home')}-{sc.get('away')} "
            f"(HT {sc.get('home_half')}-{sc.get('away_half')}) "
            f"src={sc.get('source')} {m.get('url')}"
        )
        for h in row.get("hits") or []:
            best = h.get("best_ask")
            print(
                f"  {h.get('outcome'):6} ask={best} "
                f"≤0.995={h.get('tradeable_shares')}sh all={h.get('ask_shares')}sh  "
                f"{h.get('question')}"
            )
    errs = (payload.get("list") or {}).get("league_errors") or []
    if errs:
        print(f"\nleague_errors ({len(errs)}):")
        for e in errs:
            print(f"  {e.get('league_id') or e.get('league')}: {e.get('error')}")
    skipped = payload.get("skipped") or []
    no_asks = payload.get("scored_no_asks") or []
    if no_asks:
        print(f"\nscored, no asks ({len(no_asks)}):")
        for s in no_asks[:30]:
            m = s.get("match") or {}
            sc = s.get("score") or {}
            print(
                f"  {m.get('home')} {sc.get('home')}-{sc.get('away')} "
                f"{m.get('slug') or m.get('id')}"
            )
        if len(no_asks) > 30:
            print(f"  … {len(no_asks) - 30} more")
    if skipped:
        print(f"\nskipped ({len(skipped)}):")
        for s in skipped[:30]:
            print(f"  {s.get('slug') or s.get('id')}: {s.get('error')}")
        if len(skipped) > 30:
            print(f"  … {len(skipped) - 30} more")


def main() -> int:
    args = build_parser().parse_args()
    max_ask = 0.995 if args.tradeable else float(args.max_ask)
    leagues = [p.strip().lower() for p in (args.league or "").split(",") if p.strip()]
    out_path = Path(args.out) if args.out else (lib.repo_root() / "data" / "pm-locked-scan" / "latest.json")
    progress = None if args.quiet else sys.stderr
    try:
        payload = lib.run_scan(
            hours=int(args.hours),
            leagues=leagues or None,
            max_per_league=int(args.max_per_league),
            limit=args.limit,
            max_ask=max_ask,
            refresh_af=bool(args.refresh_af),
            from_snapshot=bool(args.from_snapshot),
            snapshot_path=Path(args.snapshot) if args.snapshot else None,
            dqd_path=Path(args.dqd_snapshot) if args.dqd_snapshot else None,
            proxy=_resolve_proxy(args),
            progress=progress,
            require_af=bool(args.require_af),
        )
    except Exception as e:  # noqa: BLE001
        print(f"scan failed: {e}", file=sys.stderr)
        return 1
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if args.json:
        json.dump(payload, sys.stdout, ensure_ascii=False, indent=2)
        print()
    else:
        _human(payload)
        print(f"\nwrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
