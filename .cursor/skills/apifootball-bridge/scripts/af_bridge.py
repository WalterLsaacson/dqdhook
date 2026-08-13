#!/usr/bin/env python3
"""CLI: sync | watch | events | list | status for API-Football bridge."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import af_bridge_lib as lib  # noqa: E402


def _emit(payload: Any, *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        if isinstance(payload, dict):
            print(json.dumps(payload, ensure_ascii=False))
        else:
            print(payload)


def _make_af(args: argparse.Namespace) -> lib.AFClient:
    key = lib.load_af_key(Path(args.env) if args.env else None)
    if getattr(args, "no_free_plan", False):
        interval = 0.0
    else:
        interval = float(getattr(args, "af_interval", lib.FREE_PLAN_MIN_INTERVAL_S))
    return lib.AFClient(key, min_interval_s=interval)


def cmd_sync(args: argparse.Namespace) -> int:
    cache_path = Path(args.cache)
    bridge_path = Path(args.bridge)
    cache = lib.load_cache(cache_path)
    bridge_snap = lib.load_bridge_snapshot(bridge_path)
    af = _make_af(args)
    cache = lib.sync_fixture_cache(
        af,
        cache=cache,
        bridge_snap=bridge_snap,
        min_name=float(args.min_name),
        max_skew_min=int(args.max_skew_min),
        unresolved_ttl_h=float(args.unresolved_ttl_h),
        prune_h=float(args.prune_h),
        date_cache_dir=Path(args.date_fixtures_dir),
        force_date_refresh=bool(args.refresh_dates),
    )
    lib.save_cache(cache_path, cache)
    out = {
        "ok": True,
        "cache_path": str(cache_path),
        "bridge_matched_at": bridge_snap.get("matched_at"),
        "bridge_count": len(bridge_snap.get("matches") or []),
        "entries": len(cache.get("entries") or {}),
        "unresolved": len(cache.get("unresolved") or {}),
        "stats": cache.get("last_sync_stats"),
        "updated_at": cache.get("updated_at"),
    }
    _emit(out, as_json=args.json)
    return 0


def cmd_watch(args: argparse.Namespace) -> int:
    cache_path = Path(args.cache)
    bridge_path = Path(args.bridge)
    interval = float(args.interval)
    af = _make_af(args)
    last_fp: str | None = None
    force_every = max(1, int(args.force_every))
    tick = 0

    if not args.json:
        print(
            f"apifootball-bridge watch: bridge={bridge_path} cache={cache_path} "
            f"interval={interval}s free_plan_interval={af.min_interval_s}s",
            file=sys.stderr,
        )

    while True:
        tick += 1
        bridge_snap = lib.load_bridge_snapshot(bridge_path)
        fp = lib.bridge_fingerprint(bridge_snap)
        should = fp != last_fp or (tick % force_every == 0) or last_fp is None
        if should:
            cache = lib.load_cache(cache_path)
            cache = lib.sync_fixture_cache(
                af,
                cache=cache,
                bridge_snap=bridge_snap,
                min_name=float(args.min_name),
                max_skew_min=int(args.max_skew_min),
                unresolved_ttl_h=float(args.unresolved_ttl_h),
                prune_h=float(args.prune_h),
                date_cache_dir=Path(args.date_fixtures_dir),
                force_date_refresh=bool(args.refresh_dates),
            )
            lib.save_cache(cache_path, cache)
            last_fp = fp
            out = {
                "ok": True,
                "event": "sync",
                "tick": tick,
                "bridge_fp": fp,
                "stats": cache.get("last_sync_stats"),
                "entries": len(cache.get("entries") or {}),
                "unresolved": len(cache.get("unresolved") or {}),
                "updated_at": cache.get("updated_at"),
            }
            _emit(out, as_json=args.json)
            if args.once:
                return 0
        time.sleep(interval)


def cmd_events(args: argparse.Namespace) -> int:
    cache_path = Path(args.cache)
    cache = lib.load_cache(cache_path)
    af = _make_af(args)
    out = lib.fetch_events_for_match_id(
        af,
        str(args.match_id),
        cache=cache,
        cache_path=cache_path,
        bridge_path=Path(args.bridge),
        snapshot_path=Path(args.snapshot),
        bursts_dir=Path(args.bursts_dir),
        burst_index=Path(args.burst_index),
        persist_cache=True,
        force_resolve=bool(getattr(args, "force_resolve", False)),
    )
    _emit(out, as_json=args.json)
    return 0 if out.get("ok") else 1


def cmd_list(args: argparse.Namespace) -> int:
    cache = lib.load_cache(Path(args.cache))
    entries = cache.get("entries") or {}
    unresolved = cache.get("unresolved") or {}
    if args.unresolved_only:
        payload: Any = {"unresolved": unresolved, "count": len(unresolved)}
    else:
        payload = {
            "updated_at": cache.get("updated_at"),
            "last_sync_at": cache.get("last_sync_at"),
            "entries": entries,
            "unresolved": unresolved,
            "entry_count": len(entries),
            "unresolved_count": len(unresolved),
        }
    _emit(payload, as_json=args.json)
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    cache_path = Path(args.cache)
    cache = lib.load_cache(cache_path)
    bridge_snap = lib.load_bridge_snapshot(Path(args.bridge))
    bridge_ids = {
        str((m.get("dongqiudi") or {}).get("id") or "")
        for m in (bridge_snap.get("matches") or [])
        if isinstance(m, dict)
    }
    bridge_ids.discard("")
    entries = cache.get("entries") or {}
    unresolved = cache.get("unresolved") or {}
    bridge_mapped = sum(
        1 for mid in bridge_ids if mid in entries and (entries[mid] or {}).get("af_fixture_id")
    )
    bridge_unresolved = sum(1 for mid in bridge_ids if mid in unresolved)
    bridge_count = len(bridge_ids)
    out: dict[str, Any] = {
        "ok": True,
        "cache_path": str(cache_path),
        "cache_exists": cache_path.is_file(),
        "updated_at": cache.get("updated_at"),
        "last_sync_at": cache.get("last_sync_at"),
        "last_sync_stats": cache.get("last_sync_stats"),
        "entry_count": len(entries),
        "unresolved_count": len(unresolved),
        "bridge_matched_at": bridge_snap.get("matched_at"),
        "bridge_count": bridge_count,
        "bridge_mapped": bridge_mapped,
        "bridge_unresolved": bridge_unresolved,
        "bridge_mapped_rate": round(bridge_mapped / bridge_count, 4) if bridge_count else None,
    }
    # AF /status is optional and burns Free-plan quota — only when explicitly requested.
    if getattr(args, "af_status", False):
        try:
            af = _make_af(args)
            st = af.get("/status")
            if st.get("ok"):
                resp = st.get("response") or {}
                out["af_quota"] = resp if resp else st.get("raw")
            else:
                out["af_quota"] = {"ok": False, "errors": st.get("errors"), "http_status": st.get("http_status")}
        except Exception as e:
            out["af_quota"] = {"ok": False, "error": str(e)}
    _emit(out, as_json=args.json)
    return 0


def _add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--json", action="store_true", help="Pretty JSON stdout")
    p.add_argument("--env", default=None, help="Path to .env (default: repo .env)")
    p.add_argument(
        "--cache",
        default=str(lib.DEFAULT_CACHE_PATH),
        help="fixture_cache.json path",
    )
    p.add_argument(
        "--bridge",
        default=str(lib.DEFAULT_BRIDGE_MATCHES),
        help="bridge matches.json path",
    )
    p.add_argument(
        "--free-plan",
        dest="free_plan",
        action="store_true",
        default=True,
        help="Space AF calls (default on)",
    )
    p.add_argument(
        "--no-free-plan",
        dest="no_free_plan",
        action="store_true",
        help="Disable Free-plan spacing",
    )
    p.add_argument(
        "--af-interval",
        type=float,
        default=lib.FREE_PLAN_MIN_INTERVAL_S,
        help=f"Min seconds between AF calls (default {lib.FREE_PLAN_MIN_INTERVAL_S})",
    )
    p.add_argument("--min-name", type=float, default=lib.DEFAULT_MIN_NAME)
    p.add_argument("--max-skew-min", type=int, default=lib.DEFAULT_MAX_SKEW_MIN)
    p.add_argument("--unresolved-ttl-h", type=float, default=lib.UNRESOLVED_TTL_H)
    p.add_argument("--prune-h", type=float, default=lib.ENTRY_PRUNE_H)
    p.add_argument(
        "--date-fixtures-dir",
        default=str(lib.DEFAULT_DATE_FIXTURES_DIR),
        help="Per-day AF /fixtures?date= cache directory",
    )
    p.add_argument(
        "--refresh-dates",
        action="store_true",
        help="Ignore per-day date fixtures cache and re-fetch from AF",
    )


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="API-Football bridge (DQD match_id ↔ AF fixture)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_sync = sub.add_parser("sync", help="One bridge→AF mapping pass")
    _add_common(p_sync)
    p_sync.add_argument("--once", action="store_true", default=True, help="Alias (sync is always once)")
    p_sync.set_defaults(func=cmd_sync)

    p_watch = sub.add_parser("watch", help="Loop sync every N seconds")
    _add_common(p_watch)
    p_watch.add_argument("--interval", type=float, default=15.0, help="Poll interval seconds")
    p_watch.add_argument("--foreground", action="store_true", help="Stay in foreground (default)")
    p_watch.add_argument("--once", action="store_true", help="Single watch tick then exit")
    p_watch.add_argument(
        "--force-every",
        type=int,
        default=40,
        help="Force sync every N ticks even if bridge fingerprint unchanged",
    )
    p_watch.set_defaults(func=cmd_watch)

    p_ev = sub.add_parser("events", help="Fetch AF events for a Dongqiudi match id")
    _add_common(p_ev)
    p_ev.add_argument("--match-id", required=True, help="Dongqiudi match id")
    p_ev.add_argument(
        "--snapshot",
        default=str(lib.DEFAULT_DQD_SNAPSHOT),
        help="DQD snapshot.json fallback",
    )
    p_ev.add_argument("--bursts-dir", default=str(lib.DEFAULT_BURSTS_DIR))
    p_ev.add_argument("--burst-index", default=str(lib.DEFAULT_BURST_INDEX))
    p_ev.add_argument(
        "--force-resolve",
        action="store_true",
        help="Ignore unresolved TTL and re-query AF for fixture mapping",
    )
    p_ev.set_defaults(func=cmd_events)

    p_list = sub.add_parser("list", help="Dump fixture cache")
    _add_common(p_list)
    p_list.add_argument("--unresolved-only", action="store_true")
    p_list.set_defaults(func=cmd_list)

    p_st = sub.add_parser("status", help="Local cache + bridge status (no AF by default)")
    _add_common(p_st)
    p_st.add_argument(
        "--af-status",
        action="store_true",
        help="Also call AF /status (uses Free-plan quota)",
    )
    p_st.set_defaults(func=cmd_status)

    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
