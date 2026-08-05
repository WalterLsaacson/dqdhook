#!/usr/bin/env python3
"""Smoke tests for apifootball-bridge (fake AF client, no network)."""

from __future__ import annotations

import json
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import af_bridge_lib as lib  # noqa: E402

TZ_CN = timezone(timedelta(hours=8))
PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  OK  {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}" + (f": {detail}" if detail else ""))


class FakeAF:
    """Drop-in AFClient stand-in for smoke tests."""

    def __init__(self, fixtures_by_date: dict[str, list[dict[str, Any]]], events_by_id: dict[int, list] | None = None):
        self.fixtures_by_date = fixtures_by_date
        self.events_by_id = events_by_id or {}
        self.calls: list[tuple[str, dict[str, Any] | None]] = []
        self.min_interval_s = 0.0

    def get(self, path: str, params: dict[str, Any] | None = None, timeout: float = 20.0) -> dict[str, Any]:
        params = params or {}
        self.calls.append((path, dict(params)))
        if path == "/fixtures" and "date" in params:
            rows = self.fixtures_by_date.get(str(params["date"]), [])
            return {"ok": True, "http_status": 200, "latency_ms": 1, "errors": {}, "response": rows, "results": len(rows), "raw": {"response": rows}}
        if path == "/fixtures" and "id" in params:
            fid = int(params["id"])
            for rows in self.fixtures_by_date.values():
                for fx in rows:
                    if int((fx.get("fixture") or {}).get("id") or 0) == fid:
                        return {
                            "ok": True,
                            "http_status": 200,
                            "latency_ms": 1,
                            "errors": {},
                            "response": [fx],
                            "results": 1,
                            "raw": {"response": [fx]},
                        }
            return {"ok": True, "http_status": 200, "latency_ms": 1, "errors": {}, "response": [], "results": 0, "raw": {"response": []}}
        if path == "/fixtures/events":
            fid = int(params.get("fixture") or 0)
            ev = self.events_by_id.get(fid, [])
            return {"ok": True, "http_status": 200, "latency_ms": 1, "errors": {}, "response": ev, "results": len(ev), "raw": {"response": ev}}
        if path == "/status":
            return {"ok": True, "http_status": 200, "latency_ms": 1, "errors": {}, "response": {"requests": {"current": 1, "limit_day": 100}}, "results": 1, "raw": {}}
        return {"ok": False, "http_status": 404, "latency_ms": 1, "errors": {"path": path}, "response": [], "results": 0, "raw": {}}


def _make_fx(fid: int, home: str, away: str, kickoff_cn: datetime, goals: tuple[int, int] = (0, 0)) -> dict[str, Any]:
    utc = kickoff_cn.astimezone(timezone.utc)
    return {
        "fixture": {
            "id": fid,
            "date": utc.isoformat().replace("+00:00", "Z"),
            "timestamp": int(utc.timestamp()),
            "status": {"short": "NS", "long": "Not Started"},
        },
        "league": {"name": "Premier League", "country": "England"},
        "teams": {"home": {"name": home}, "away": {"name": away}},
        "goals": {"home": goals[0], "away": goals[1]},
        "score": {
            "halftime": {"home": None, "away": None},
            "fulltime": {"home": None, "away": None},
            "extratime": {"home": None, "away": None},
            "penalty": {"home": None, "away": None},
        },
    }


def _bridge_match(dqd_id: str, home: str, away: str, kickoff_cn: datetime) -> dict[str, Any]:
    return {
        "dongqiudi": {
            "id": dqd_id,
            "home": home,
            "away": away,
            "league": "Premier League",
            "start_play": kickoff_cn.strftime("%Y-%m-%d %H:%M:%S"),
            "kickoff_beijing": kickoff_cn.strftime("%Y-%m-%d %H:%M"),
        },
        "polymarket": {"event_id": "pm1"},
    }


def test_cache_hit_miss_and_ttl() -> None:
    print("test_cache_hit_miss_and_ttl")
    ko = datetime(2026, 7, 30, 20, 0, tzinfo=TZ_CN)
    date = ko.date().isoformat()
    fx = _make_fx(1546417, "Arsenal", "Chelsea", ko, (1, 0))
    af = FakeAF({date: [fx], (ko.date() - timedelta(days=1)).isoformat(): [], (ko.date() + timedelta(days=1)).isoformat(): []})

    bridge_snap = {
        "matched_at": "2026-07-30T12:00:00+08:00",
        "matches": [_bridge_match("54528347", "Arsenal", "Chelsea", ko)],
    }
    with tempfile.TemporaryDirectory() as td:
        date_dir = Path(td) / "dates"
        cache = lib.empty_cache()
        cache = lib.sync_fixture_cache(
            af, cache=cache, bridge_snap=bridge_snap, date_cache_dir=date_dir
        )
        check("resolved on miss", "54528347" in cache["entries"] and cache["entries"]["54528347"]["af_fixture_id"] == 1546417)
        check("stats resolved", (cache.get("last_sync_stats") or {}).get("resolved") == 1)
        check(
            "date fetches recorded",
            int((cache.get("last_sync_stats") or {}).get("date_fetches") or 0) >= 1,
            str(cache.get("last_sync_stats")),
        )
        calls_after_miss = len(af.calls)

        # Cache hit: no new AF date fetches needed for this match
        cache2 = lib.sync_fixture_cache(
            af, cache=cache, bridge_snap=bridge_snap, date_cache_dir=date_dir
        )
        check("cache hit keeps id", cache2["entries"]["54528347"]["af_fixture_id"] == 1546417)
        check("cache hit no extra date calls", len([c for c in af.calls[calls_after_miss:] if c[0] == "/fixtures" and "date" in (c[1] or {})]) == 0)

        # Unresolved TTL skip
        bad_ko = datetime(2026, 7, 30, 21, 0, tzinfo=TZ_CN)
        bridge_bad = {
            "matched_at": "2026-07-30T12:01:00+08:00",
            "matches": [_bridge_match("999", "NoSuch FC", "Ghost United", bad_ko)],
        }
        af2 = FakeAF({bad_ko.date().isoformat(): [fx]})
        cache_u = lib.empty_cache()
        cache_u = lib.sync_fixture_cache(
            af2, cache=cache_u, bridge_snap=bridge_bad, date_cache_dir=date_dir
        )
        check("unresolved recorded", "999" in cache_u["unresolved"])
        calls_u = len(af2.calls)
        cache_u2 = lib.sync_fixture_cache(
            af2, cache=cache_u, bridge_snap=bridge_bad, date_cache_dir=date_dir
        )
        check("unresolved TTL skips AF", len(af2.calls) == calls_u, f"calls grew {calls_u}->{len(af2.calls)}")
        check("still unresolved", "999" in cache_u2["unresolved"])


def test_date_fixtures_disk_cache() -> None:
    """Second unresolved match on the same kickoff day must reuse date blobs."""
    print("test_date_fixtures_disk_cache")
    ko = datetime(2026, 7, 30, 20, 0, tzinfo=TZ_CN)
    d0 = ko.date()
    dates = {
        (d0 - timedelta(days=1)).isoformat(): [],
        d0.isoformat(): [
            _make_fx(1001, "Arsenal", "Chelsea", ko),
            _make_fx(1002, "Liverpool", "Everton", ko + timedelta(minutes=30)),
        ],
        (d0 + timedelta(days=1)).isoformat(): [],
    }
    af = FakeAF(dates)
    with tempfile.TemporaryDirectory() as td:
        date_dir = Path(td) / "dates"
        # First match → fetch ±1 dates once
        snap1 = {
            "matched_at": "2026-07-30T12:00:00+08:00",
            "matches": [_bridge_match("m1", "Arsenal", "Chelsea", ko)],
        }
        cache = lib.sync_fixture_cache(
            af,
            cache=lib.empty_cache(),
            bridge_snap=snap1,
            date_cache_dir=date_dir,
        )
        date_calls_1 = [c for c in af.calls if c[0] == "/fixtures" and "date" in (c[1] or {})]
        check("first resolve date calls", len(date_calls_1) == 3, str(date_calls_1))
        check("m1 mapped", cache["entries"]["m1"]["af_fixture_id"] == 1001)
        check(
            "day file written",
            (date_dir / f"{d0.isoformat()}.json").is_file(),
        )
        n_calls = len(af.calls)

        # Drop DQD→AF entry but keep date cache; new match same day → no AF date=
        cache["entries"].pop("m1", None)
        snap2 = {
            "matched_at": "2026-07-30T12:05:00+08:00",
            "matches": [_bridge_match("m2", "Liverpool", "Everton", ko + timedelta(minutes=30))],
        }
        cache2 = lib.sync_fixture_cache(
            af,
            cache=cache,
            bridge_snap=snap2,
            date_cache_dir=date_dir,
        )
        date_calls_2 = [
            c
            for c in af.calls[n_calls:]
            if c[0] == "/fixtures" and "date" in (c[1] or {})
        ]
        check("second resolve uses date cache", date_calls_2 == [], str(date_calls_2))
        check("m2 mapped from cache", cache2["entries"]["m2"]["af_fixture_id"] == 1002)
        st = cache2.get("last_sync_stats") or {}
        check("date_cache_hits > 0", int(st.get("date_cache_hits") or 0) >= 1, str(st))
        check("date_fetches == 0", int(st.get("date_fetches") or 0) == 0, str(st))

        # force_refresh bypasses disk
        n3 = len(af.calls)
        lib.sync_fixture_cache(
            af,
            cache=lib.empty_cache(),
            bridge_snap=snap2,
            date_cache_dir=date_dir,
            force_date_refresh=True,
        )
        forced = [c for c in af.calls[n3:] if c[0] == "/fixtures" and "date" in (c[1] or {})]
        check("force_refresh re-fetches dates", len(forced) == 3, str(forced))


def test_events_burst_layout() -> None:
    print("test_events_burst_layout")
    ko = datetime(2026, 7, 30, 20, 0, tzinfo=TZ_CN)
    date = ko.date().isoformat()
    fx = _make_fx(1546417, "Arsenal", "Chelsea", ko, (2, 1))
    events = [
        {"type": "Goal", "detail": "Normal Goal", "time": {"elapsed": 12}, "team": {"name": "Arsenal"}},
        {"type": "Goal", "detail": "Normal Goal", "time": {"elapsed": 55}, "team": {"name": "Chelsea"}},
        {"type": "Goal", "detail": "Normal Goal", "time": {"elapsed": 80}, "team": {"name": "Arsenal"}},
    ]
    af = FakeAF({date: [fx]}, events_by_id={1546417: events})

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        cache_path = root / "fixture_cache.json"
        bridge_path = root / "matches.json"
        bursts = root / "bursts"
        index = root / "burst_index.jsonl"
        bridge_snap = {
            "matched_at": "2026-07-30T12:00:00+08:00",
            "matches": [_bridge_match("54528347", "Arsenal", "Chelsea", ko)],
        }
        lib.write_json(bridge_path, bridge_snap)
        cache = lib.empty_cache()
        # Pre-seed cache (hit path for events)
        cache["entries"]["54528347"] = {
            "dqd_match_id": "54528347",
            "af_fixture_id": 1546417,
            "dqd_home": "Arsenal",
            "dqd_away": "Chelsea",
            "af_home": "Arsenal",
            "af_away": "Chelsea",
            "matched_at": lib.iso_now(),
            "source": "bridge+af",
        }
        lib.save_cache(cache_path, cache)

        out = lib.fetch_events_for_match_id(
            af,  # type: ignore[arg-type]
            "54528347",
            cache=cache,
            cache_path=cache_path,
            bridge_path=bridge_path,
            bursts_dir=bursts,
            burst_index=index,
        )
        check("events ok", out.get("ok") is True)
        check("af_fixture_id", out.get("af_fixture_id") == 1546417)
        check("events count", len(out.get("events") or []) == 3)
        check("goals home from events", (out.get("goals") or {}).get("home") == 2)
        check("goals away from events", (out.get("goals") or {}).get("away") == 1)
        check(
            "cache hit → only one AF call (events)",
            len(af.calls) == 1 and af.calls[0][0] == "/fixtures/events",
            str(af.calls),
        )
        burst_dir = Path(out.get("burst_dir") or "")
        check("burst_dir exists", burst_dir.is_dir(), str(burst_dir))
        for name in ("meta.json", "af_events.json", "result.json"):
            check(f"artifact {name}", (burst_dir / name).is_file())
        check("no af_fixture.json", not (burst_dir / "af_fixture.json").is_file())
        meta = json.loads((burst_dir / "meta.json").read_text(encoding="utf-8"))
        check("meta source", meta.get("source") == "events_request")
        result = json.loads((burst_dir / "result.json").read_text(encoding="utf-8"))
        check("result kind", result.get("kind") == "events_request")
        check("index appended", index.is_file() and "events_request" in index.read_text(encoding="utf-8"))


def test_events_resolve_on_miss() -> None:
    print("test_events_resolve_on_miss")
    ko = datetime(2026, 7, 30, 20, 0, tzinfo=TZ_CN)
    date = ko.date().isoformat()
    fx = _make_fx(777, "Liverpool", "Everton", ko, (0, 0))
    af = FakeAF(
        {
            date: [fx],
            (ko.date() - timedelta(days=1)).isoformat(): [],
            (ko.date() + timedelta(days=1)).isoformat(): [],
        },
        events_by_id={777: []},
    )
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        bridge_path = root / "matches.json"
        cache_path = root / "cache.json"
        lib.write_json(
            bridge_path,
            {
                "matched_at": "2026-07-30T12:00:00+08:00",
                "matches": [_bridge_match("111", "Liverpool", "Everton", ko)],
            },
        )
        cache = lib.empty_cache()
        out = lib.fetch_events_for_match_id(
            af,  # type: ignore[arg-type]
            "111",
            cache=cache,
            cache_path=cache_path,
            bridge_path=bridge_path,
            bursts_dir=root / "bursts",
            burst_index=root / "index.jsonl",
            date_cache_dir=root / "dates",
        )
        check("resolve then events", out.get("ok") is True and out.get("af_fixture_id") == 777)
        check("cache updated", cache["entries"]["111"]["af_fixture_id"] == 777)
        check("cache file written", cache_path.is_file())
        # miss path: date fixtures (±1) + events — date calls only for resolve
        event_calls = [c for c in af.calls if c[0] == "/fixtures/events"]
        check("one events call after resolve", len(event_calls) == 1)


def test_events_cache_only_no_resolve() -> None:
    print("test_events_cache_only_no_resolve")
    ko = datetime(2026, 7, 30, 20, 0, tzinfo=TZ_CN)
    date = ko.date().isoformat()
    fx = _make_fx(777, "Liverpool", "Everton", ko, (0, 0))
    af = FakeAF(
        {
            date: [fx],
            (ko.date() - timedelta(days=1)).isoformat(): [],
            (ko.date() + timedelta(days=1)).isoformat(): [],
        },
        events_by_id={777: [{"type": "Goal", "team": {"name": "Liverpool"}, "detail": "Normal Goal"}]},
    )
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        bridge_path = root / "matches.json"
        lib.write_json(
            bridge_path,
            {
                "matched_at": "2026-07-30T12:00:00+08:00",
                "matches": [_bridge_match("111", "Liverpool", "Everton", ko)],
            },
        )
        cache = lib.empty_cache()
        miss = lib.fetch_events_for_match_id(
            af,  # type: ignore[arg-type]
            "111",
            cache=cache,
            bridge_path=bridge_path,
            bursts_dir=root / "bursts",
            burst_index=root / "index.jsonl",
            persist_cache=False,
            persist_burst=False,
            cache_only=True,
        )
        check("cache_only miss ok=False", miss.get("ok") is False)
        check("error not_cached", miss.get("error") == "af_fixture_not_cached", str(miss.get("error")))
        check("no AF HTTP on miss", len(af.calls) == 0, str(af.calls))
        check("cache still empty", "111" not in (cache.get("entries") or {}))

        cache["entries"]["111"] = {
            "dqd_match_id": "111",
            "af_fixture_id": 777,
            "af_home": "Liverpool",
            "af_away": "Everton",
        }
        hit = lib.fetch_events_for_match_id(
            af,  # type: ignore[arg-type]
            "111",
            cache=cache,
            bridge_path=bridge_path,
            bursts_dir=root / "bursts",
            burst_index=root / "index.jsonl",
            persist_cache=False,
            persist_burst=False,
            cache_only=True,
        )
        check("cache_only hit ok", hit.get("ok") is True)
        check("only events call", len(af.calls) == 1 and af.calls[0][0] == "/fixtures/events", str(af.calls))
        check("goals from events", (hit.get("goals") or {}).get("home") == 1)


def test_load_cache_preserves_sync_meta() -> None:
    print("test_load_cache_preserves_sync_meta")
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "c.json"
        c = lib.empty_cache()
        c["entries"]["1"] = {"af_fixture_id": 9}
        c["last_sync_at"] = "2026-07-30T12:00:00+08:00"
        c["last_sync_stats"] = {"resolved": 1}
        c["last_bridge_matched_at"] = "2026-07-30T11:00:00+08:00"
        lib.save_cache(p, c)
        loaded = lib.load_cache(p)
        check("last_sync_at kept", loaded.get("last_sync_at") == "2026-07-30T12:00:00+08:00")
        check("last_sync_stats kept", (loaded.get("last_sync_stats") or {}).get("resolved") == 1)
        check("last_bridge kept", loaded.get("last_bridge_matched_at") == "2026-07-30T11:00:00+08:00")


def test_regulation_score_ignores_et_pen() -> None:
    print("test_regulation_score_ignores_et_pen")
    fx = _make_fx(99, "Home", "Away", datetime(2026, 8, 1, 18, 30, tzinfo=TZ_CN), goals=(3, 2))
    fx["fixture"]["status"] = {"short": "AET", "long": "After Extra Time"}
    fx["score"]["fulltime"] = {"home": 2, "away": 2}
    fx["score"]["extratime"] = {"home": 3, "away": 2}
    fx["score"]["penalty"] = {"home": 5, "away": 4}
    fx["goals"] = {"home": 3, "away": 2}
    reg = lib.regulation_score_from_fixture(fx)
    check("finished on AET", reg["finished"] is True)
    check("ready on AET", reg["regulation_ready"] is True)
    check("uses fulltime not ET", reg["goals"] == {"home": 2, "away": 2})

    # Knockout still in ET: regulation fulltime must already unlock confirm.
    fx_et = dict(fx)
    fx_et["fixture"] = {"id": 99, "status": {"short": "ET", "long": "Extra Time"}}
    fx_et["score"] = {
        "fulltime": {"home": 1, "away": 1},
        "extratime": {"home": 1, "away": 1},
        "penalty": {"home": None, "away": None},
        "halftime": {"home": 0, "away": 0},
    }
    reg_et = lib.regulation_score_from_fixture(fx_et)
    check("not finished during ET", reg_et["finished"] is False)
    check("regulation ready during ET", reg_et["regulation_ready"] is True)
    check("ET uses fulltime", reg_et["goals"] == {"home": 1, "away": 1})

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        kick = datetime(2026, 8, 1, 18, 30, tzinfo=TZ_CN)
        fx2 = _make_fx(1510397, "Cheongju", "Suwon Bluewings", kick, goals=(2, 2))
        fx2["fixture"]["status"] = {"short": "FT", "long": "Match Finished"}
        fx2["score"]["fulltime"] = {"home": 2, "away": 2}
        date_key = kick.astimezone(timezone.utc).strftime("%Y-%m-%d")
        af = FakeAF({date_key: [fx2]})
        cache = lib.empty_cache()
        cache["entries"]["54364565"] = {
            "dqd_match_id": "54364565",
            "af_fixture_id": 1510397,
            "af_home": "Cheongju",
            "af_away": "Suwon Bluewings",
        }
        out = lib.fetch_regulation_score_for_match_id(
            af, "54364565", cache=cache, cache_only=True
        )
        check("fetch ok", out.get("ok") is True)
        check("regulation 2-2", out.get("goals") == {"home": 2, "away": 2})
        check("finished", out.get("finished") is True)
        check("regulation_ready", out.get("regulation_ready") is True)
        check("fixtures id call", any(c[0] == "/fixtures" and "id" in (c[1] or {}) for c in af.calls))


def main() -> int:
    test_cache_hit_miss_and_ttl()
    test_date_fixtures_disk_cache()
    test_events_burst_layout()
    test_events_resolve_on_miss()
    test_events_cache_only_no_resolve()
    test_load_cache_preserves_sync_meta()
    test_regulation_score_ignores_et_pen()
    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
