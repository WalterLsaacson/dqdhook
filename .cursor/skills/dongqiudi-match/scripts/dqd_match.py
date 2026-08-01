#!/usr/bin/env python3
"""CLI for Dongqiudi soccer tabs + score-change events.

Examples:
  python3 dqd_match.py list --tab hot --json
  python3 dqd_match.py watch --tab hot --once --json
  python3 dqd_match.py watch --tab hot --interval 15
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

# Allow running as a script without installing a package.
_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import dqd_lib as lib  # noqa: E402

SENTINEL = "DQD_SCORE_CHANGE"


def repo_root() -> Path:
    # scripts -> dongqiudi-match -> skills -> .cursor -> repo root
    return Path(__file__).resolve().parents[4]


def data_dir(explicit: str | None = None) -> Path:
    root = Path(explicit) if explicit else repo_root() / "data"
    root.mkdir(parents=True, exist_ok=True)
    return root


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def append_events(path: Path, events: list[dict[str, Any]]) -> None:
    if not events:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        for ev in events:
            f.write(json.dumps(ev, ensure_ascii=False) + "\n")


def emit_sentinels(events: list[dict[str, Any]]) -> None:
    for ev in events:
        print(f"{SENTINEL} {json.dumps(ev, ensure_ascii=False)}", flush=True)


def cmd_list(args: argparse.Namespace) -> int:
    days = int(getattr(args, "days", 2) or 2)
    try:
        matches = lib.safe_load_matches(language=args.language, days=days)
    except lib.FetchError as e:
        print(f"fetch failed: {e}", file=sys.stderr)
        return 1

    if args.tab == "all":
        payload = {
            "fetched_at": lib.build_snapshot(
                "full", language=args.language, days=days, matches=matches
            )["fetched_at"],
            "language": args.language,
            "today": lib.today_cn(),
            "days": days,
            "dates": lib.day_window(None, days),
            "tabs": {
                name: lib.build_snapshot(
                    name, language=args.language, days=days, matches=matches
                )
                for name in ("full", "hot", "beidan", "jingcai")
            },
        }
    else:
        payload = lib.build_snapshot(
            args.tab, language=args.language, days=days, matches=matches
        )

    ddir = data_dir(args.data_dir)
    write_json(ddir / "snapshot.json", payload)
    json.dump(payload, sys.stdout, ensure_ascii=False, indent=2)
    print()
    return 0


def run_watch_once(
    tab: str,
    language: str,
    ddir: Path,
    *,
    quiet: bool = False,
    days: int = 3,
) -> dict[str, Any]:
    matches = lib.safe_load_matches(language=language, days=days)
    snapshot = lib.build_snapshot(tab, language=language, days=days, matches=matches)
    selected = snapshot["matches"]

    prev_path = ddir / "prev_scores.json"
    events_path = ddir / "events.jsonl"
    prev_scores = read_json(prev_path, {})
    if not isinstance(prev_scores, dict):
        prev_scores = {}

    # Only diff within the watched tab set.
    all_events = lib.detect_score_changes(selected, prev_scores, tab=tab)
    emit_events = lib.events_for_downstream(all_events)

    # Also seed baseline for other known ids so restarts don't explode.
    for m in selected:
        mid = str(m.get("id") or "")
        if mid and mid not in prev_scores:
            prev_scores[mid] = {"home": m.get("home_score"), "away": m.get("away_score")}

    write_json(prev_path, prev_scores)
    write_json(ddir / "snapshot.json", snapshot)
    # Record every score swing (incl. extra-time noise); only emit clean ones.
    append_events(events_path, all_events)

    result = {
        "fetched_at": snapshot["fetched_at"],
        "tab": tab,
        "count": snapshot["count"],
        "has_live": snapshot["has_live"],
        "changes": len(emit_events),
        "recorded_changes": len(all_events),
        "suppressed_extra_time": len(all_events) - len(emit_events),
        "events": emit_events,
        "matches": selected if not quiet else None,
    }
    if quiet:
        result.pop("matches", None)
    return result


def emit_watch_output(result: dict[str, Any], *, as_json: bool) -> None:
    """JSON mode is pure JSON; text mode prints sentinels for loop skill."""
    events = result.get("events") or []
    if as_json:
        json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
        print(flush=True)
        return
    emit_sentinels(events)
    print(
        f"[{result.get('fetched_at')}] tab={result.get('tab')} "
        f"count={result.get('count')} changes={result.get('changes')} "
        f"has_live={result.get('has_live')}",
        flush=True,
    )


def cmd_watch(args: argparse.Namespace) -> int:
    ddir = data_dir(args.data_dir)
    # Align with official /match cadence: ~10–15s when live, 30–60s when idle.
    interval = max(10, int(args.interval))
    idle_interval = max(max(30, interval), int(args.idle_interval))

    days = int(getattr(args, "days", 2) or 2)

    def tick() -> dict[str, Any]:
        return run_watch_once(
            args.tab, args.language, ddir, quiet=args.quiet, days=days
        )

    try:
        if args.once:
            result = tick()
            emit_watch_output(result, as_json=args.json)
            return 0

        while True:
            result = tick()
            emit_watch_output(result, as_json=args.json)
            sleep_s = interval if result.get("has_live") else idle_interval
            time.sleep(sleep_s)
    except lib.FetchError as e:
        print(f"fetch failed: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("stopped", file=sys.stderr)
        return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Dongqiudi soccer match tabs + score watch")
    p.add_argument(
        "--data-dir",
        default=None,
        help="State directory (default: <repo>/data)",
    )
    p.add_argument(
        "--language",
        default="en",
        help="API language (default: en for English team/league names)",
    )
    p.add_argument(
        "--days",
        type=int,
        default=3,
        help="Beijing calendar days starting today (default 3 ≈ PM 48h window)",
    )

    sub = p.add_subparsers(dest="command", required=True)

    list_p = sub.add_parser("list", help="Fetch tab snapshot as JSON")
    list_p.add_argument(
        "--tab",
        choices=["full", "hot", "beidan", "jingcai", "all"],
        default="full",
    )
    list_p.add_argument("--json", action="store_true", default=True, help="Emit JSON (default)")
    list_p.set_defaults(func=cmd_list)

    watch_p = sub.add_parser("watch", help="Poll and emit score_change events")
    watch_p.add_argument("--tab", choices=["full", "hot", "beidan", "jingcai"], default="full")
    watch_p.add_argument("--once", action="store_true", help="Single poll then exit")
    watch_p.add_argument(
        "--interval",
        type=int,
        default=15,
        help="Seconds when live matches exist (min 10, default 15)",
    )
    watch_p.add_argument(
        "--idle-interval",
        type=int,
        default=60,
        help="Seconds when no live matches (min 30, default 60)",
    )
    watch_p.add_argument("--json", action="store_true", help="Print JSON summary each tick")
    watch_p.add_argument("--quiet", action="store_true", help="Omit matches array from JSON summary")
    watch_p.set_defaults(func=cmd_watch)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
