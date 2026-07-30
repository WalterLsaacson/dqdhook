#!/usr/bin/env python3
"""API-Football bridge library: fixture cache, matcher, events burst writer."""

from __future__ import annotations

import hashlib
import json
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

_SCRIPTS = Path(__file__).resolve().parent
_BRIDGE = _SCRIPTS.parent.parent / "match-bridge" / "scripts"
_bp = str(_BRIDGE)
if _bp not in sys.path:
    sys.path.insert(0, _bp)

import bridge_lib as bridge  # noqa: E402


def _af_errors_nonempty(errors: Any) -> bool:
    """API-Sports often returns HTTP 200 with a non-empty errors object."""
    if not errors:
        return False
    if isinstance(errors, dict):
        return any(v not in (None, "", [], {}) for v in errors.values())
    if isinstance(errors, list):
        return len(errors) > 0
    return True

TZ_CN = timezone(timedelta(hours=8))
AF_BASE = "https://v3.football.api-sports.io"
REPO_ROOT = Path(__file__).resolve().parents[4]
DEFAULT_CACHE_PATH = REPO_ROOT / "data" / "apifootball" / "fixture_cache.json"
DEFAULT_BRIDGE_MATCHES = REPO_ROOT / "data" / "bridge" / "matches.json"
DEFAULT_DQD_SNAPSHOT = REPO_ROOT / "data" / "snapshot.json"
DEFAULT_BURSTS_DIR = REPO_ROOT / "data" / "dqd-probe" / "af-latency" / "bursts"
DEFAULT_BURST_INDEX = REPO_ROOT / "data" / "dqd-probe" / "af-latency" / "burst_index.jsonl"

UNRESOLVED_TTL_H = 6.0
ENTRY_PRUNE_H = 24.0
DEFAULT_MIN_NAME = 0.75
DEFAULT_MAX_SKEW_MIN = 120
FREE_PLAN_MIN_INTERVAL_S = 6.5


def now_cn() -> datetime:
    return datetime.now(TZ_CN)


def iso_now() -> str:
    return now_cn().isoformat(timespec="seconds")


def parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts))
    except (TypeError, ValueError):
        return None


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def read_json(path: Path, default: Any = None) -> Any:
    if not path.is_file():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def append_jsonl(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def load_af_key(env_path: Path | None = None) -> str:
    path = env_path or (REPO_ROOT / ".env")
    text = path.read_text(encoding="utf-8")
    m = re.search(r"(?i)^\s*apifootball_key\s*=\s*(.+)$", text, re.M)
    if not m:
        raise RuntimeError(f"apifootball_key not found in {path}")
    return m.group(1).strip().strip('"').strip("'")


class AFClient:
    """Thread-safe API-Football client with optional min interval."""

    def __init__(self, key: str, *, min_interval_s: float = 0.0):
        self.key = key
        self.min_interval_s = max(0.0, float(min_interval_s))
        self._lock = threading.Lock()
        self._last_at = 0.0

    def get(self, path: str, params: dict[str, Any] | None = None, timeout: float = 20.0) -> dict[str, Any]:
        qs = urllib.parse.urlencode(params or {})
        url = f"{AF_BASE}{path}" + (f"?{qs}" if qs else "")
        req = urllib.request.Request(
            url,
            headers={"x-apisports-key": self.key, "Accept": "application/json"},
        )
        with self._lock:
            gap = self.min_interval_s - (time.monotonic() - self._last_at)
            if gap > 0:
                time.sleep(gap)
            t0 = time.perf_counter()
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    body = json.loads(resp.read().decode("utf-8"))
                    status = getattr(resp, "status", 200)
            except urllib.error.HTTPError as e:
                self._last_at = time.monotonic()
                err_body = e.read().decode("utf-8", errors="replace")[:500]
                try:
                    parsed = json.loads(err_body)
                except json.JSONDecodeError:
                    parsed = {"raw": err_body}
                return {
                    "ok": False,
                    "http_status": e.code,
                    "latency_ms": int((time.perf_counter() - t0) * 1000),
                    "request_url": url,
                    "errors": parsed.get("errors") or parsed,
                    "response": [],
                    "results": 0,
                    "raw": parsed if isinstance(parsed, dict) else {"raw": parsed},
                }
            except Exception as e:
                self._last_at = time.monotonic()
                return {
                    "ok": False,
                    "http_status": None,
                    "latency_ms": int((time.perf_counter() - t0) * 1000),
                    "request_url": url,
                    "errors": {"exception": str(e)},
                    "response": [],
                    "results": 0,
                    "raw": {"errors": {"exception": str(e)}},
                }
            self._last_at = time.monotonic()
        errors = body.get("errors") or {}
        ok = not _af_errors_nonempty(errors)
        return {
            "ok": ok,
            "http_status": status,
            "latency_ms": int((time.perf_counter() - t0) * 1000),
            "request_url": url,
            "errors": errors,
            "response": body.get("response") or [],
            "results": body.get("results"),
            "raw": body,
        }


def empty_cache() -> dict[str, Any]:
    return {"updated_at": None, "entries": {}, "unresolved": {}}


def load_cache(path: Path) -> dict[str, Any]:
    raw = read_json(path, None)
    if not isinstance(raw, dict):
        return empty_cache()
    entries = raw.get("entries") if isinstance(raw.get("entries"), dict) else {}
    unresolved = raw.get("unresolved") if isinstance(raw.get("unresolved"), dict) else {}
    out = empty_cache()
    out["updated_at"] = raw.get("updated_at")
    out["entries"] = {str(k): v for k, v in entries.items() if isinstance(v, dict)}
    out["unresolved"] = {str(k): v for k, v in unresolved.items() if isinstance(v, dict)}
    for key in ("last_sync_at", "last_sync_stats", "last_bridge_matched_at"):
        if key in raw:
            out[key] = raw[key]
    return out


def save_cache(path: Path, cache: dict[str, Any]) -> None:
    cache = dict(cache)
    cache["updated_at"] = iso_now()
    write_json(path, cache)


def load_bridge_snapshot(path: Path = DEFAULT_BRIDGE_MATCHES) -> dict[str, Any]:
    raw = read_json(path, {}) or {}
    if not isinstance(raw, dict):
        return {"matched_at": None, "matches": []}
    return {
        "matched_at": raw.get("matched_at"),
        "matches": list(raw.get("matches") or []),
        "count": raw.get("count"),
        "path": str(path),
        "mtime": path.stat().st_mtime if path.is_file() else None,
    }


def bridge_fingerprint(snap: dict[str, Any]) -> str:
    blob = json.dumps(
        {
            "matched_at": snap.get("matched_at"),
            "count": snap.get("count"),
            "ids": [
                str((m.get("dongqiudi") or {}).get("id") or "")
                for m in (snap.get("matches") or [])
                if isinstance(m, dict)
            ],
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()


def dqd_row_from_bridge(match_row: dict[str, Any]) -> dict[str, Any]:
    dqd = dict(match_row.get("dongqiudi") or {})
    # Ensure kickoff fields usable by bridge._kickoff_dt
    return dqd


def find_bridge_row(matches: list[dict[str, Any]], dqd_id: str) -> dict[str, Any] | None:
    for m in matches:
        if str((m.get("dongqiudi") or {}).get("id") or "") == str(dqd_id):
            return m
    return None


def find_dqd_snapshot_row(dqd_id: str, snapshot_path: Path = DEFAULT_DQD_SNAPSHOT) -> dict[str, Any] | None:
    snap = read_json(snapshot_path, {}) or {}
    for m in snap.get("matches") or []:
        if str(m.get("id") or "") == str(dqd_id):
            return m
    return None


def _af_kickoff_cn(fx: dict[str, Any]) -> datetime | None:
    ts = (fx.get("fixture") or {}).get("timestamp")
    if ts is None:
        return None
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).astimezone(TZ_CN)
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def score_af_candidate(
    dqd: dict[str, Any],
    fx: dict[str, Any],
    *,
    min_name: float = DEFAULT_MIN_NAME,
    max_skew_min: int = DEFAULT_MAX_SKEW_MIN,
) -> tuple[float, float, int | None] | None:
    home = ((fx.get("teams") or {}).get("home") or {}).get("name") or ""
    away = ((fx.get("teams") or {}).get("away") or {}).get("name") or ""
    hs = bridge.team_similarity(dqd.get("home") or "", home)
    aws = bridge.team_similarity(dqd.get("away") or "", away)
    sh = bridge.team_similarity(dqd.get("home") or "", away)
    sa = bridge.team_similarity(dqd.get("away") or "", home)
    name = max((hs + aws) / 2.0, (sh + sa) / 2.0)
    if name < min_name:
        return None
    d_ko = bridge._kickoff_dt(dqd)
    a_ko = _af_kickoff_cn(fx)
    if d_ko is None or a_ko is None:
        return None
    skew = int(abs((d_ko - a_ko).total_seconds()) // 60)
    if skew > max_skew_min:
        return None
    return (name - skew / 1000.0, name, skew)


def fixture_dates_for_dqd(dqd: dict[str, Any]) -> list[str]:
    ko = bridge._kickoff_dt(dqd)
    if ko is None:
        today = now_cn().date()
        days = [today + timedelta(days=d) for d in (-1, 0, 1)]
    else:
        d0 = ko.astimezone(TZ_CN).date()
        days = [d0 + timedelta(days=d) for d in (-1, 0, 1)]
    return [d.isoformat() for d in days]


def fetch_fixtures_for_dates(af: AFClient, dates: list[str]) -> tuple[list[dict[str, Any]], bool]:
    """Fetch fixtures for dates. Returns (fixtures, any_ok).

    If every date call fails (network / rate limit), any_ok is False so callers
    should not mark matches as unresolved.
    """
    out: list[dict[str, Any]] = []
    seen: set[int] = set()
    any_ok = False
    for date in dates:
        payload = af.get("/fixtures", {"date": date})
        if not payload.get("ok"):
            continue
        any_ok = True
        for fx in payload.get("response") or []:
            if not isinstance(fx, dict):
                continue
            fid = int((fx.get("fixture") or {}).get("id") or 0)
            if not fid or fid in seen:
                continue
            seen.add(fid)
            out.append(fx)
    return out, any_ok


def resolve_af_fixture(
    af: AFClient,
    dqd: dict[str, Any],
    *,
    fixtures: list[dict[str, Any]] | None = None,
    min_name: float = DEFAULT_MIN_NAME,
    max_skew_min: int = DEFAULT_MAX_SKEW_MIN,
) -> dict[str, Any] | None:
    if fixtures is not None:
        pool = fixtures
    else:
        pool, _ok = fetch_fixtures_for_dates(af, fixture_dates_for_dqd(dqd))
    best: tuple[float, float, int, dict[str, Any]] | None = None
    for fx in pool:
        scored = score_af_candidate(dqd, fx, min_name=min_name, max_skew_min=max_skew_min)
        if scored is None:
            continue
        rank, name, skew = scored
        if best is None or rank > best[0]:
            best = (rank, name, skew if skew is not None else 0, fx)
    if not best:
        return None
    _rank, name, skew, fx = best
    home = ((fx.get("teams") or {}).get("home") or {}).get("name") or ""
    away = ((fx.get("teams") or {}).get("away") or {}).get("name") or ""
    fid = int(fx["fixture"]["id"])
    ko = bridge._kickoff_dt(dqd)
    return {
        "dqd_match_id": str(dqd.get("id") or ""),
        "af_fixture_id": fid,
        "dqd_home": dqd.get("home") or "",
        "dqd_away": dqd.get("away") or "",
        "af_home": home,
        "af_away": away,
        "af_league": ((fx.get("league") or {}).get("name") or ""),
        "af_country": ((fx.get("league") or {}).get("country") or ""),
        "kickoff_beijing": ko.strftime("%Y-%m-%d %H:%M") if ko else "",
        "name_score": round(float(name), 3),
        "skew_min": int(skew),
        "matched_at": iso_now(),
        "source": "bridge+af",
        "af_kickoff_utc": (fx.get("fixture") or {}).get("date"),
        "af_status": (fx.get("fixture") or {}).get("status"),
        "af_goals": fx.get("goals"),
    }


def unresolved_fresh(entry: dict[str, Any], *, ttl_h: float = UNRESOLVED_TTL_H, now: datetime | None = None) -> bool:
    tried = parse_iso(str(entry.get("tried_at") or ""))
    if tried is None:
        return False
    now = now or now_cn()
    if tried.tzinfo is None:
        tried = tried.replace(tzinfo=TZ_CN)
    return (now - tried).total_seconds() < ttl_h * 3600


def sync_fixture_cache(
    af: AFClient,
    *,
    cache: dict[str, Any],
    bridge_snap: dict[str, Any],
    min_name: float = DEFAULT_MIN_NAME,
    max_skew_min: int = DEFAULT_MAX_SKEW_MIN,
    unresolved_ttl_h: float = UNRESOLVED_TTL_H,
    prune_h: float = ENTRY_PRUNE_H,
) -> dict[str, Any]:
    """Cache-first sync of bridge matches onto AF fixtures. Mutates and returns cache."""
    now = now_cn()
    entries: dict[str, Any] = dict(cache.get("entries") or {})
    unresolved: dict[str, Any] = dict(cache.get("unresolved") or {})
    matches = [m for m in (bridge_snap.get("matches") or []) if isinstance(m, dict)]
    bridge_matched_at = bridge_snap.get("matched_at")
    seen_ids: set[str] = set()

    need_resolve: list[dict[str, Any]] = []
    stats = {
        "bridge_count": 0,
        "cache_hits": 0,
        "resolved": 0,
        "unresolved_new": 0,
        "skipped_ttl": 0,
        "af_fetch_failed": 0,
        "pruned": 0,
    }

    for row in matches:
        dqd = dqd_row_from_bridge(row)
        mid = str(dqd.get("id") or "")
        if not mid:
            continue
        seen_ids.add(mid)
        if mid in entries and entries[mid].get("af_fixture_id"):
            ent = dict(entries[mid])
            ent["last_bridge_matched_at"] = bridge_matched_at or iso_now()
            ent["dqd_home"] = dqd.get("home") or ent.get("dqd_home")
            ent["dqd_away"] = dqd.get("away") or ent.get("dqd_away")
            ko = bridge._kickoff_dt(dqd)
            if ko:
                ent["kickoff_beijing"] = ko.strftime("%Y-%m-%d %H:%M")
            entries[mid] = ent
            unresolved.pop(mid, None)
            stats["cache_hits"] += 1
            continue
        u = unresolved.get(mid)
        if isinstance(u, dict) and unresolved_fresh(u, ttl_h=unresolved_ttl_h, now=now):
            stats["skipped_ttl"] += 1
            continue
        need_resolve.append(dqd)

    stats["bridge_count"] = len(seen_ids)

    fixtures_pool: list[dict[str, Any]] | None = None
    fetch_ok = True
    if need_resolve:
        dates: set[str] = set()
        for dqd in need_resolve:
            dates.update(fixture_dates_for_dqd(dqd))
        fixtures_pool, fetch_ok = fetch_fixtures_for_dates(af, sorted(dates))
        if not fetch_ok:
            stats["af_fetch_failed"] = len(need_resolve)
            # Do not burn unresolved TTL on transport / rate-limit failures
            need_resolve = []

    for dqd in need_resolve:
        mid = str(dqd.get("id") or "")
        hit = resolve_af_fixture(
            af,
            dqd,
            fixtures=fixtures_pool,
            min_name=min_name,
            max_skew_min=max_skew_min,
        )
        if hit:
            hit["last_bridge_matched_at"] = bridge_matched_at or iso_now()
            entries[mid] = hit
            unresolved.pop(mid, None)
            stats["resolved"] += 1
        else:
            unresolved[mid] = {
                "reason": "no_af_fixture",
                "tried_at": iso_now(),
                "dqd_home": dqd.get("home") or "",
                "dqd_away": dqd.get("away") or "",
                "dqd_league": dqd.get("league") or "",
            }
            stats["unresolved_new"] += 1

    # Prune entries not seen in bridge for > prune_h
    for mid in list(entries.keys()):
        if mid in seen_ids:
            continue
        ent = entries[mid]
        last = parse_iso(str(ent.get("last_bridge_matched_at") or ent.get("matched_at") or ""))
        if last is None:
            continue
        if last.tzinfo is None:
            last = last.replace(tzinfo=TZ_CN)
        if (now - last).total_seconds() > prune_h * 3600:
            entries.pop(mid, None)
            stats["pruned"] += 1

    cache["entries"] = entries
    cache["unresolved"] = unresolved
    cache["last_sync_at"] = iso_now()
    cache["last_sync_stats"] = stats
    cache["last_bridge_matched_at"] = bridge_matched_at
    return cache


def ensure_fixture_for_match_id(
    af: AFClient,
    dqd_id: str,
    *,
    cache: dict[str, Any],
    bridge_path: Path = DEFAULT_BRIDGE_MATCHES,
    snapshot_path: Path = DEFAULT_DQD_SNAPSHOT,
    min_name: float = DEFAULT_MIN_NAME,
    max_skew_min: int = DEFAULT_MAX_SKEW_MIN,
    unresolved_ttl_h: float = UNRESOLVED_TTL_H,
    force_resolve: bool = False,
) -> dict[str, Any] | None:
    """Return cache entry for dqd_id, resolving via bridge/snapshot if needed."""
    mid = str(dqd_id)
    ent = (cache.get("entries") or {}).get(mid)
    if isinstance(ent, dict) and ent.get("af_fixture_id"):
        return ent

    u = (cache.get("unresolved") or {}).get(mid)
    if (
        not force_resolve
        and isinstance(u, dict)
        and unresolved_fresh(u, ttl_h=unresolved_ttl_h)
    ):
        return None

    bridge_snap = load_bridge_snapshot(bridge_path)
    row = find_bridge_row(bridge_snap.get("matches") or [], mid)
    dqd: dict[str, Any] | None = dqd_row_from_bridge(row) if row else None
    if not dqd or not dqd.get("id"):
        dqd = find_dqd_snapshot_row(mid, snapshot_path)
    if not dqd:
        return None

    hit = resolve_af_fixture(af, dqd, min_name=min_name, max_skew_min=max_skew_min)
    if not hit:
        unresolved = dict(cache.get("unresolved") or {})
        unresolved[mid] = {
            "reason": "no_af_fixture",
            "tried_at": iso_now(),
            "dqd_home": dqd.get("home") or "",
            "dqd_away": dqd.get("away") or "",
        }
        cache["unresolved"] = unresolved
        return None

    hit["last_bridge_matched_at"] = bridge_snap.get("matched_at") or iso_now()
    entries = dict(cache.get("entries") or {})
    entries[mid] = hit
    cache["entries"] = entries
    unresolved = dict(cache.get("unresolved") or {})
    unresolved.pop(mid, None)
    cache["unresolved"] = unresolved
    return hit


def goals_from_events(
    events: list[Any],
    *,
    af_home: str = "",
    af_away: str = "",
) -> dict[str, int | None]:
    """Count standing Goal events attributed to AF home/away by team name.

    Own Goal: API-Football ``team`` is the benefiting side — same attribution.
    Falls back to a looser name threshold when the strict match fails.
    """
    home_n = 0
    away_n = 0
    attributed = False
    loose_home = 0
    loose_away = 0

    def _side(team: str, strict: float) -> str | None:
        if not team:
            return None
        if af_home and bridge.team_similarity(team, af_home) >= strict:
            return "home"
        if af_away and bridge.team_similarity(team, af_away) >= strict:
            return "away"
        return None

    for ev in events:
        if not isinstance(ev, dict):
            continue
        if str(ev.get("type") or "") != "Goal":
            continue
        detail = str(ev.get("detail") or "")
        if "Disallowed" in detail or "Cancelled" in detail or "Missed Penalty" in detail:
            continue
        team = str((ev.get("team") or {}).get("name") or "")
        side = _side(team, 0.75)
        if side == "home":
            home_n += 1
            attributed = True
        elif side == "away":
            away_n += 1
            attributed = True
        else:
            side_l = _side(team, 0.55)
            if side_l == "home":
                loose_home += 1
            elif side_l == "away":
                loose_away += 1

    if not events:
        return {"home": 0, "away": 0}
    if attributed:
        return {"home": home_n, "away": away_n}
    if loose_home or loose_away:
        return {"home": loose_home, "away": loose_away}
    if not (af_home or af_away):
        return {"home": None, "away": None}
    # Had Goal rows but could not attribute — inconclusive rather than 0-0.
    goal_rows = [
        e
        for e in events
        if isinstance(e, dict)
        and str(e.get("type") or "") == "Goal"
        and "Disallowed" not in str(e.get("detail") or "")
        and "Cancelled" not in str(e.get("detail") or "")
        and "Missed Penalty" not in str(e.get("detail") or "")
    ]
    if goal_rows:
        return {"home": None, "away": None}
    return {"home": 0, "away": 0}


def write_events_burst(
    *,
    dqd_match_id: str,
    af_fixture_id: int,
    entry: dict[str, Any] | None,
    events_payload: dict[str, Any],
    bursts_dir: Path = DEFAULT_BURSTS_DIR,
    burst_index: Path = DEFAULT_BURST_INDEX,
) -> dict[str, Any]:
    burst_id = now_cn().strftime("%Y%m%dT%H%M%S%f")[:-3]  # ms precision
    burst_dir = bursts_dir / f"{dqd_match_id}_{burst_id}"
    burst_dir.mkdir(parents=True, exist_ok=True)

    events_list = events_payload.get("response") if events_payload.get("ok") else []
    if not isinstance(events_list, list):
        events_list = []

    goals = goals_from_events(
        events_list,
        af_home=str((entry or {}).get("af_home") or ""),
        af_away=str((entry or {}).get("af_away") or ""),
    )

    meta = {
        "source": "events_request",
        "kind": "events_request",
        "dqd_match_id": str(dqd_match_id),
        "af_fixture_id": int(af_fixture_id),
        "home": (entry or {}).get("dqd_home") or (entry or {}).get("af_home"),
        "away": (entry or {}).get("dqd_away") or (entry or {}).get("af_away"),
        "started_at": iso_now(),
        "cache_entry": entry,
    }
    write_json(burst_dir / "meta.json", meta)
    write_json(
        burst_dir / "af_events.json",
        events_payload.get("raw")
        if events_payload.get("ok")
        else {
            "ok": False,
            "errors": events_payload.get("errors"),
            "http_status": events_payload.get("http_status"),
        },
    )

    ok = bool(events_payload.get("ok"))
    result = {
        "kind": "events_request",
        "source": "events_request",
        "ok": ok,
        "dqd_match_id": str(dqd_match_id),
        "af_fixture_id": int(af_fixture_id),
        "fetched_at": iso_now(),
        "events_count": len(events_list),
        "goals": goals,
        "errors": events_payload.get("errors") if not ok else {},
        "http_status": events_payload.get("http_status"),
        "burst_dir": str(burst_dir),
        "finished_at": iso_now(),
    }
    write_json(burst_dir / "result.json", result)
    append_jsonl(burst_index, result)
    return {
        **result,
        "events": events_list,
        "burst_dir": str(burst_dir),
    }


def fetch_events_for_match_id(
    af: AFClient,
    dqd_match_id: str,
    *,
    cache: dict[str, Any],
    cache_path: Path = DEFAULT_CACHE_PATH,
    bridge_path: Path = DEFAULT_BRIDGE_MATCHES,
    snapshot_path: Path = DEFAULT_DQD_SNAPSHOT,
    bursts_dir: Path = DEFAULT_BURSTS_DIR,
    burst_index: Path = DEFAULT_BURST_INDEX,
    persist_cache: bool = True,
    force_resolve: bool = False,
    persist_burst: bool = True,
) -> dict[str, Any]:
    """Cache lookup → one AF /fixtures/events call. Resolves fixture only on cache miss.

    Set persist_burst=False for high-frequency referee polls (burst written on confirm).
    """
    entry = ensure_fixture_for_match_id(
        af,
        dqd_match_id,
        cache=cache,
        bridge_path=bridge_path,
        snapshot_path=snapshot_path,
        force_resolve=force_resolve,
    )
    if persist_cache:
        save_cache(cache_path, cache)

    if not entry or not entry.get("af_fixture_id"):
        u = (cache.get("unresolved") or {}).get(str(dqd_match_id))
        err = "af_fixture_unresolved"
        if isinstance(u, dict) and unresolved_fresh(u):
            err = "af_fixture_unresolved_ttl"
        return {
            "ok": False,
            "dqd_match_id": str(dqd_match_id),
            "af_fixture_id": None,
            "fetched_at": iso_now(),
            "goals": {"home": None, "away": None},
            "events": [],
            "error": err,
            "burst_dir": None,
        }

    fid = int(entry["af_fixture_id"])
    events_payload = af.get("/fixtures/events", {"fixture": fid})
    events_list = events_payload.get("response") if events_payload.get("ok") else []
    if not isinstance(events_list, list):
        events_list = []
    goals = goals_from_events(
        events_list,
        af_home=str(entry.get("af_home") or ""),
        af_away=str(entry.get("af_away") or ""),
    )
    if persist_burst:
        out = write_events_burst(
            dqd_match_id=str(dqd_match_id),
            af_fixture_id=fid,
            entry=entry,
            events_payload=events_payload,
            bursts_dir=bursts_dir,
            burst_index=burst_index,
        )
    else:
        out = {
            "kind": "events_request",
            "source": "events_request",
            "ok": bool(events_payload.get("ok")),
            "dqd_match_id": str(dqd_match_id),
            "af_fixture_id": fid,
            "fetched_at": iso_now(),
            "events_count": len(events_list),
            "goals": goals,
            "errors": events_payload.get("errors") if not events_payload.get("ok") else {},
            "http_status": events_payload.get("http_status"),
            "burst_dir": None,
            "events": events_list,
        }
    out["ok"] = bool(events_payload.get("ok"))
    out["goals"] = goals
    out["events"] = events_list
    out["cache_entry"] = entry
    if not out["ok"]:
        out["error"] = events_payload.get("errors") or "af_events_failed"
    return out
