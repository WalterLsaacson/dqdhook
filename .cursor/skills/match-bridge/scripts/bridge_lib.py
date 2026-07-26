#!/usr/bin/env python3
"""Match Dongqiudi fixtures to Polymarket events and emit market handles."""

from __future__ import annotations

import fcntl
import json
import re
import sys
import threading
import time
import traceback
import unicodedata
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from league_aliases import LEAGUE_ALIASES
from team_aliases import TEAM_ALIASES

TZ_CN = timezone(timedelta(hours=8))

DEFAULT_MIN_SCORE = 0.70
DEFAULT_MIN_SIDE = 0.75
DEFAULT_MAX_SKEW_MIN = 90
DEFAULT_PM_STALE_HOURS = 6
DEFAULT_LEAGUE_FLOOR = 0.40
# After status=Played but period not yet FT, poll faster (default live stays 15s).
PENDING_FT_POLL_SEC = 5

# Noise words stripped before fuzzy compare.
_TEAM_STOP = frozenset(
    {
        "fc",
        "cf",
        "sc",
        "ac",
        "afc",
        "sfc",
        "fk",
        "bk",
        "if",
        "ik",
        "sk",
        "kk",
        "cd",
        "ca",
        "rc",
        "ud",
        "sd",
        "as",
        "ss",
        "sv",
        "vfl",
        "vfb",
        "tsg",
        "club",
        "de",
        "la",
        "el",
        "the",
        "united",
        "city",
        "town",
        "hotspur",
        "association",
        "sports",
    }
)

# Canonical alias tables — extend in team_aliases.py / league_aliases.py
_TEAM_ALIASES = TEAM_ALIASES
_LEAGUE_ALIASES = LEAGUE_ALIASES
_KNOWN_LEAGUE_CODES = frozenset(_LEAGUE_ALIASES.values())

_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)
_WS_RE = re.compile(r"\s+")


def repo_root_from(scripts_file: Path) -> Path:
    # scripts -> match-bridge -> skills -> .cursor -> repo
    return scripts_file.resolve().parents[4]


def normalize_team(name: str) -> str:
    """Normalize team names for fuzzy compare.

    Keeps standalone digit tokens (e.g. ``2028``, ``04``). Never strips digits
    from inside a token (``shenzhen2028``, ``u23`` stay intact).
    """
    s = unicodedata.normalize("NFKD", str(name or ""))
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = s.lower().strip()
    if s in _TEAM_ALIASES:
        s = _TEAM_ALIASES[s]
    # Also try alias on original Chinese without NFKD destroying CJK
    raw = str(name or "").strip()
    if raw in _TEAM_ALIASES:
        s = _TEAM_ALIASES[raw]
    s = _PUNCT_RE.sub(" ", s)
    s = _WS_RE.sub(" ", s).strip()
    if s in _TEAM_ALIASES:
        s = _TEAM_ALIASES[s]
    tokens = [t for t in s.split(" ") if t and t not in _TEAM_STOP]
    out = " ".join(tokens)
    return _TEAM_ALIASES.get(out, out)


def team_similarity(a: str, b: str) -> float:
    na, nb = normalize_team(a), normalize_team(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    if na in nb or nb in na:
        return 0.92
    ta, tb = set(na.split()), set(nb.split())
    jacc = len(ta & tb) / len(ta | tb) if ta and tb else 0.0
    seq = SequenceMatcher(None, na, nb).ratio()
    return max(jacc, seq)


def normalize_league(name: str = "", league_id: str = "") -> str:
    """Map DQD/PM league labels to a comparable string (prefer alias codes)."""
    for raw in (league_id, name):
        key = str(raw or "").strip()
        if not key:
            continue
        low = key.lower()
        if low in _LEAGUE_ALIASES:
            return _LEAGUE_ALIASES[low]
        if key in _LEAGUE_ALIASES:
            return _LEAGUE_ALIASES[key]
        # light cleanup then alias again
        cleaned = _WS_RE.sub(" ", _PUNCT_RE.sub(" ", low)).strip()
        if cleaned in _LEAGUE_ALIASES:
            return _LEAGUE_ALIASES[cleaned]
    # Fallback: cleaned display name (or id) for fuzzy compare
    for raw in (name, league_id):
        key = str(raw or "").strip()
        if key:
            return _WS_RE.sub(" ", _PUNCT_RE.sub(" ", key.lower())).strip()
    return ""


def league_similarity(dqd: dict[str, Any], pm: dict[str, Any]) -> float | None:
    """Return league similarity, or None when either side lacks league fields."""
    a = normalize_league(str(dqd.get("league") or ""), str(dqd.get("league_id") or ""))
    b = normalize_league(str(pm.get("league") or ""), str(pm.get("league_id") or ""))
    if not a or not b:
        return None
    if a == b:
        return 1.0
    # Known canonical codes must match exactly (中甲 vs 中乙 etc.).
    if a in _KNOWN_LEAGUE_CODES and b in _KNOWN_LEAGUE_CODES:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def _parse_hhmm(value: str) -> int | None:
    try:
        hh, mm = str(value).strip().split(":")[:2]
        return int(hh) * 60 + int(mm)
    except (ValueError, AttributeError):
        return None


def _kickoff_dt(row: dict[str, Any]) -> datetime | None:
    """Absolute Beijing kickoff: match_timestamp → kickoff_beijing → local_date+time."""
    ts = row.get("match_timestamp")
    if ts is not None and str(ts).strip() != "":
        try:
            epoch = float(ts)
            if epoch > 1e12:  # ms
                epoch /= 1000.0
            return datetime.fromtimestamp(epoch, tz=timezone.utc).astimezone(TZ_CN)
        except (TypeError, ValueError, OSError, OverflowError):
            pass

    kb = str(row.get("kickoff_beijing") or "").strip()
    if kb:
        for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"):
            try:
                return datetime.strptime(kb, fmt).replace(tzinfo=TZ_CN)
            except ValueError:
                continue

    da = str(row.get("local_date") or "").strip()
    tm = str(row.get("time") or "").strip()
    if da and tm and _parse_hhmm(tm) is not None:
        try:
            return datetime.strptime(f"{da} {tm}", "%Y-%m-%d %H:%M").replace(tzinfo=TZ_CN)
        except ValueError:
            return None
    return None


def kickoff_minutes_apart(a: dict[str, Any], b: dict[str, Any]) -> int | None:
    """Absolute minute distance between kickoffs (prefer epoch / kickoff_beijing)."""
    dta = _kickoff_dt(a)
    dtb = _kickoff_dt(b)
    if dta is None or dtb is None:
        return None
    return int(abs((dta - dtb).total_seconds()) // 60)


def filter_fresh_pm_matches(
    pm_matches: list[dict[str, Any]],
    *,
    stale_hours: float = DEFAULT_PM_STALE_HOURS,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Drop PM events whose kickoff is more than ``stale_hours`` in the past."""
    if stale_hours is None or stale_hours < 0:
        return list(pm_matches)
    now_cn = now or datetime.now(TZ_CN)
    cutoff = now_cn - timedelta(hours=float(stale_hours))
    out: list[dict[str, Any]] = []
    for p in pm_matches:
        dt = _kickoff_dt(p)
        if dt is None or dt >= cutoff:
            out.append(p)
    return out


def _side_pair_ok(home_s: float, away_s: float, min_side: float) -> bool:
    return home_s >= min_side and away_s >= min_side


def score_pair(
    dqd: dict[str, Any],
    pm: dict[str, Any],
    *,
    max_skew_min: int = DEFAULT_MAX_SKEW_MIN,
    min_side: float = DEFAULT_MIN_SIDE,
    league_floor: float = DEFAULT_LEAGUE_FLOOR,
) -> float:
    skew = kickoff_minutes_apart(dqd, pm)
    if skew is None or skew > max_skew_min:
        return 0.0

    league_s = league_similarity(dqd, pm)
    if league_s is not None and league_s < league_floor:
        return 0.0

    home_s = team_similarity(dqd.get("home") or "", pm.get("home") or "")
    away_s = team_similarity(dqd.get("away") or "", pm.get("away") or "")
    direct = (home_s + away_s) / 2 if _side_pair_ok(home_s, away_s, min_side) else 0.0

    swap_h = team_similarity(dqd.get("home") or "", pm.get("away") or "")
    swap_a = team_similarity(dqd.get("away") or "", pm.get("home") or "")
    swap = (swap_h + swap_a) / 2 if _side_pair_ok(swap_h, swap_a, min_side) else 0.0

    best = max(direct, swap)
    if best <= 0.0:
        return 0.0
    # Soft time bonus
    time_factor = 1.0 - min(skew, max_skew_min) / (max_skew_min * 2)
    return best * (0.85 + 0.15 * time_factor)


def polymarket_handle(pm: dict[str, Any]) -> dict[str, Any]:
    """Stable handles for external market/odds consumers."""
    slug = pm.get("slug") or ""
    return {
        "event_id": str(pm.get("id") or ""),
        "slug": slug,
        "url": pm.get("url") or (f"https://polymarket.com/event/{slug}" if slug else ""),
        "gamma_event_url": (
            f"https://gamma-api.polymarket.com/events/{pm.get('id')}" if pm.get("id") else ""
        ),
        "series_id": str(pm.get("series_id") or ""),
        "title": pm.get("title") or "",
        "league_id": pm.get("league_id") or "",
        "league": pm.get("league") or "",
        "home": pm.get("home") or "",
        "away": pm.get("away") or "",
        "kickoff_beijing": pm.get("kickoff_beijing") or "",
        "condition_ids": list(pm.get("condition_ids") or []),
        "market_refs": list(pm.get("market_refs") or []),
    }


def match_fixtures(
    dqd_matches: list[dict[str, Any]],
    pm_matches: list[dict[str, Any]],
    *,
    min_score: float = DEFAULT_MIN_SCORE,
    max_skew_min: int = DEFAULT_MAX_SKEW_MIN,
    min_side: float = DEFAULT_MIN_SIDE,
    league_floor: float = DEFAULT_LEAGUE_FLOOR,
    pm_stale_hours: float = DEFAULT_PM_STALE_HOURS,
) -> list[dict[str, Any]]:
    """Greedy 1:1 matching by similarity score."""
    pm_fresh = filter_fresh_pm_matches(pm_matches, stale_hours=pm_stale_hours)
    candidates: list[tuple[float, int, int]] = []
    for i, d in enumerate(dqd_matches):
        for j, p in enumerate(pm_fresh):
            s = score_pair(
                d,
                p,
                max_skew_min=max_skew_min,
                min_side=min_side,
                league_floor=league_floor,
            )
            if s >= min_score:
                candidates.append((s, i, j))
    candidates.sort(reverse=True, key=lambda x: x[0])

    used_d: set[int] = set()
    used_p: set[int] = set()
    out: list[dict[str, Any]] = []
    for score, i, j in candidates:
        if i in used_d or j in used_p:
            continue
        used_d.add(i)
        used_p.add(j)
        d = dict(dqd_matches[i])
        p = pm_fresh[j]
        # Refresh clocks at match time (wall clock drifts between DQD polls).
        try:
            import dqd_lib as dqd  # type: ignore

            d.update(dqd.progress_fields(d))
        except Exception:  # noqa: BLE001
            pass
        out.append(
            {
                "match_score": round(score, 4),
                "kickoff_beijing": p.get("kickoff_beijing")
                or f"{d.get('local_date') or ''} {d.get('time') or ''}".strip(),
                "dongqiudi": {
                    "id": str(d.get("id") or ""),
                    "league": d.get("league") or "",
                    "league_id": str(d.get("league_id") or ""),
                    "league_color": d.get("league_color") or "",
                    "home": d.get("home") or "",
                    "away": d.get("away") or "",
                    "home_logo": d.get("home_logo") or "",
                    "away_logo": d.get("away_logo") or "",
                    "local_date": d.get("local_date") or "",
                    "time": d.get("time") or "",
                    "start_play": d.get("start_play") or "",
                    "match_timestamp": d.get("match_timestamp"),
                    "status": d.get("status") or "",
                    "status_raw": d.get("status_raw") or "",
                    "home_score": d.get("home_score"),
                    "away_score": d.get("away_score"),
                    "home_half": d.get("home_half") or "",
                    "away_half": d.get("away_half") or "",
                    "minute": d.get("minute") or "",
                    "minute_str": d.get("minute_str") or "",
                    "injury_time": d.get("injury_time") or 0,
                    "period": d.get("period") or "",
                    "official_clock": d.get("official_clock") or "",
                    "wall_clock": d.get("wall_clock") or "",
                    "wall_elapsed_sec": d.get("wall_elapsed_sec"),
                },
                "polymarket": polymarket_handle(p),
            }
        )
    out.sort(key=lambda m: (m.get("kickoff_beijing") or "9999", -m.get("match_score", 0)))
    return out


def load_json(path: Path, default: Any = None) -> Any:
    if not path.is_file():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def append_events(path: Path, events: list[dict[str, Any]]) -> None:
    """Append events under exclusive flock so quote prune cannot race replace."""
    if not events:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = Path(str(path) + ".lock")
    with lock_path.open("a+", encoding="utf-8") as lf:
        fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
        try:
            with path.open("a", encoding="utf-8") as f:
                for ev in events:
                    f.write(json.dumps(ev, ensure_ascii=False) + "\n")
        finally:
            fcntl.flock(lf.fileno(), fcntl.LOCK_UN)


def status_bucket(dqd: dict[str, Any] | None) -> str:
    """Normalize Dongqiudi status into fixture|playing|played|""."""
    if not dqd:
        return ""
    raw = str(dqd.get("status_raw") or "").lower().strip()
    disp = str(dqd.get("status") or "").lower().strip()
    if not raw:
        if disp.startswith("playing") or "进行中" in disp:
            raw = "playing"
        elif disp.startswith("played") or disp in ("ft", "完场", "finished"):
            raw = "played"
        elif disp.startswith("fixture") or disp in ("未开赛",):
            raw = "fixture"
    if raw in ("playing", "played", "fixture"):
        return raw
    if "play" in raw and "ed" in raw:
        return "played"
    return raw or ""


def period_bucket(dqd: dict[str, Any] | None) -> str:
    """Normalize Dongqiudi period into 1H|2H|FT|'' (uppercase)."""
    if not dqd:
        return ""
    return str(dqd.get("period") or "").strip().upper()


def is_full_time(dqd: dict[str, Any] | None) -> bool:
    """True when Dongqiudi marks the match period as full time."""
    return period_bucket(dqd) == "FT"


def is_pending_ft_poll(dqd: dict[str, Any] | None) -> bool:
    """Played status but period not FT yet — accelerate DQD polling."""
    return status_bucket(dqd) == "played" and not is_full_time(dqd)


def has_pending_ft_poll(matches: list[dict[str, Any]] | None) -> bool:
    return any(is_pending_ft_poll(m) for m in (matches or []))


def _pm_event_handle(pm: dict[str, Any]) -> dict[str, Any]:
    return {
        "event_id": pm.get("event_id") or "",
        "slug": pm.get("slug") or "",
        "url": pm.get("url") or "",
        "condition_ids": list(pm.get("condition_ids") or []),
        "market_refs": list(pm.get("market_refs") or []),
    }


def detect_score_changes(
    paired: list[dict[str, Any]],
    prev_scores: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Emit score_change on goals and on score reversals (disallowed / corrections).

    First sighting only seeds prev_scores. Goals: is_goal=True.
    Score drop on either side: is_reversal=True (polymarket-quote flattens).
    """
    events: list[dict[str, Any]] = []
    ts = datetime.now(TZ_CN).isoformat(timespec="seconds")
    for row in paired:
        dqd = row.get("dongqiudi") or {}
        pm = row.get("polymarket") or {}
        mid = str(dqd.get("id") or "")
        if not mid:
            continue
        hs, aws = dqd.get("home_score"), dqd.get("away_score")
        if hs is None or aws is None:
            continue
        try:
            hs_i, aws_i = int(hs), int(aws)
        except (TypeError, ValueError):
            continue

        prev = prev_scores.get(mid)
        if prev is None:
            prev_scores[mid] = {"home": hs_i, "away": aws_i}
            continue

        try:
            ph, pa = int(prev.get("home")), int(prev.get("away"))
        except (TypeError, ValueError):
            prev_scores[mid] = {"home": hs_i, "away": aws_i}
            continue

        if ph == hs_i and pa == aws_i:
            continue

        # Goal: scores only rise.
        if hs_i >= ph and aws_i >= pa and (hs_i > ph or aws_i > pa):
            events.append(
                {
                    "type": "score_change",
                    "ts": ts,
                    "match_id": mid,
                    "league": pm.get("league") or dqd.get("league") or "",
                    "home": pm.get("home") or dqd.get("home") or "",
                    "away": pm.get("away") or dqd.get("away") or "",
                    "prev": {"home": ph, "away": pa},
                    "curr": {"home": hs_i, "away": aws_i},
                    "home_score": hs_i,
                    "away_score": aws_i,
                    "side": (
                        "both"
                        if hs_i > ph and aws_i > pa
                        else ("home" if hs_i > ph else "away")
                    ),
                    "is_goal": True,
                    "is_reversal": False,
                    "status": dqd.get("status") or "",
                    "status_raw": dqd.get("status_raw") or "",
                    "official_clock": dqd.get("official_clock") or "",
                    "kickoff_beijing": row.get("kickoff_beijing") or "",
                    "polymarket": _pm_event_handle(pm),
                }
            )
        # Disallowed / correction: either side's score drops.
        elif hs_i < ph or aws_i < pa:
            side = (
                "both"
                if hs_i < ph and aws_i < pa
                else ("home" if hs_i < ph else "away")
            )
            events.append(
                {
                    "type": "score_change",
                    "ts": ts,
                    "match_id": mid,
                    "league": pm.get("league") or dqd.get("league") or "",
                    "home": pm.get("home") or dqd.get("home") or "",
                    "away": pm.get("away") or dqd.get("away") or "",
                    "prev": {"home": ph, "away": pa},
                    "curr": {"home": hs_i, "away": aws_i},
                    "home_score": hs_i,
                    "away_score": aws_i,
                    "side": side,
                    "is_goal": False,
                    "is_reversal": True,
                    "status": dqd.get("status") or "",
                    "status_raw": dqd.get("status_raw") or "",
                    "official_clock": dqd.get("official_clock") or "",
                    "kickoff_beijing": row.get("kickoff_beijing") or "",
                    "polymarket": _pm_event_handle(pm),
                }
            )
        prev_scores[mid] = {"home": hs_i, "away": aws_i}
    return events


def detect_match_finished(
    paired: list[dict[str, Any]],
    prev_status: dict[str, str],
    prev_period: dict[str, str],
) -> list[dict[str, Any]]:
    """Emit match_finished when DQD period transitions into FT.

    Dongqiudi keeps period=1H/2H through stoppage; full time is period=FT
    (status may still be Playing briefly). First sighting only seeds baselines.
    """
    events: list[dict[str, Any]] = []
    ts = datetime.now(TZ_CN).isoformat(timespec="seconds")
    for row in paired:
        dqd = row.get("dongqiudi") or {}
        pm = row.get("polymarket") or {}
        mid = str(dqd.get("id") or "")
        if not mid:
            continue
        curr_status = status_bucket(dqd)
        curr_period = period_bucket(dqd)
        finished = is_full_time(dqd)
        dqd["is_finished"] = finished
        row["finished"] = finished
        row["dongqiudi"] = dqd

        if mid not in prev_period:
            # Upgrade / cold bootstrap: status was already tracked, period file is new.
            # Do not seed current FT as the baseline (that would swallow the edge).
            if mid in prev_status:
                prev_period[mid] = ""
            else:
                prev_period[mid] = curr_period
                prev_status[mid] = curr_status
                continue

        prev_p = str(prev_period.get(mid) or "")
        prev_s = str(prev_status.get(mid) or "")
        if prev_p != "FT" and curr_period == "FT":
            events.append(
                {
                    "type": "match_finished",
                    "ts": ts,
                    "match_id": mid,
                    "prev_status": prev_s,
                    "prev_period": prev_p,
                    "status": curr_status or "played",
                    "period": curr_period,
                    "status_display": dqd.get("status") or "Played",
                    "league": pm.get("league") or dqd.get("league") or "",
                    "home": pm.get("home") or dqd.get("home") or "",
                    "away": pm.get("away") or dqd.get("away") or "",
                    "home_score": dqd.get("home_score"),
                    "away_score": dqd.get("away_score"),
                    "kickoff_beijing": row.get("kickoff_beijing") or "",
                    "official_clock": dqd.get("official_clock") or "FT",
                    "polymarket": _pm_event_handle(pm),
                }
            )
            row["finished_at"] = ts
        elif finished and row.get("finished_at") is None:
            # Already FT before we started watching — no toast, keep flag.
            pass

        prev_period[mid] = curr_period
        prev_status[mid] = curr_status
    return events


class BridgeRuntime:
    """Runs DQD watch + PM list at defaults and rematches into data/bridge/."""

    def __init__(
        self,
        root: Path,
        *,
        dqd_tab: str = "full",
        dqd_interval: int = 15,
        dqd_idle_interval: int = 60,
        pm_interval: int = 600,
        pm_within_hours: int = 48,
        min_score: float = DEFAULT_MIN_SCORE,
        max_skew_min: int = DEFAULT_MAX_SKEW_MIN,
        min_side: float = DEFAULT_MIN_SIDE,
        pm_stale_hours: float = DEFAULT_PM_STALE_HOURS,
    ) -> None:
        self.root = root
        # full tab overlaps Polymarket's multi-league 48h window better than hot-only
        self.dqd_tab = dqd_tab
        self.dqd_interval = max(10, dqd_interval)
        self.dqd_idle_interval = max(max(30, self.dqd_interval), dqd_idle_interval)
        self.pm_interval = max(120, pm_interval)
        self.pm_within_hours = pm_within_hours
        self.min_score = min_score
        self.max_skew_min = max_skew_min
        self.min_side = min_side
        self.pm_stale_hours = pm_stale_hours

        self.dqd_scripts = root / ".cursor" / "skills" / "dongqiudi-match" / "scripts"
        self.pm_scripts = root / ".cursor" / "skills" / "polymarket-soccer" / "scripts"
        self.dqd_data = root / "data"
        self.pm_data = root / "data" / "polymarket"
        self.bridge_data = root / "data" / "bridge"

        self.lock = threading.RLock()
        self.running = False
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self.last_error: str | None = None
        self.dqd_ticks = 0
        self.pm_ticks = 0
        self.match_ticks = 0
        self.started_at: str | None = None
        self.last_result: dict[str, Any] | None = None
        self.last_events: list[dict[str, Any]] = []

        for p in (self.dqd_scripts, self.pm_scripts):
            if str(p) not in sys.path:
                sys.path.insert(0, str(p))

    def status(self) -> dict[str, Any]:
        with self.lock:
            return {
                "module": "match-bridge",
                "running": self.running,
                "started_at": self.started_at,
                "dqd_tab": self.dqd_tab,
                "dqd_interval": self.dqd_interval,
                "dqd_idle_interval": self.dqd_idle_interval,
                "pm_interval": self.pm_interval,
                "pm_within_hours": self.pm_within_hours,
                "min_score": self.min_score,
                "max_skew_min": self.max_skew_min,
                "min_side": self.min_side,
                "pm_stale_hours": self.pm_stale_hours,
                "dqd_ticks": self.dqd_ticks,
                "pm_ticks": self.pm_ticks,
                "match_ticks": self.match_ticks,
                "last_error": self.last_error,
                "last_events": list(self.last_events),
                "last_result": {
                    "matched_at": (self.last_result or {}).get("matched_at"),
                    "count": (self.last_result or {}).get("count"),
                    "finished_count": (self.last_result or {}).get("finished_count"),
                    "dqd_count": (self.last_result or {}).get("dqd_count"),
                    "pm_count": (self.last_result or {}).get("pm_count"),
                    "events": (self.last_result or {}).get("events") or [],
                }
                if self.last_result
                else None,
            }

    def refresh_dqd_once(self) -> dict[str, Any]:
        from dqd_match import data_dir, run_watch_once  # type: ignore

        ddir = data_dir(str(self.dqd_data))
        result = run_watch_once(self.dqd_tab, "en", ddir, quiet=False)
        with self.lock:
            self.dqd_ticks += 1
        return result

    def refresh_pm_once(self) -> dict[str, Any]:
        import pm_lib as pm  # type: ignore
        from pm_soccer import data_dir, write_json  # type: ignore

        payload = pm.load_matches(within_hours=self.pm_within_hours)
        write_json(data_dir(str(self.pm_data)) / "snapshot.json", payload)
        with self.lock:
            self.pm_ticks += 1
        return payload

    def rematch(self) -> dict[str, Any]:
        dqd_snap = load_json(self.dqd_data / "snapshot.json", {}) or {}
        pm_snap = load_json(self.pm_data / "snapshot.json", {}) or {}
        dqd_matches = list(dqd_snap.get("matches") or [])
        pm_matches = list(pm_snap.get("matches") or [])
        paired = match_fixtures(
            dqd_matches,
            pm_matches,
            min_score=self.min_score,
            max_skew_min=self.max_skew_min,
            min_side=self.min_side,
            pm_stale_hours=self.pm_stale_hours,
        )

        prev_path = self.bridge_data / "prev_status.json"
        prev_period_path = self.bridge_data / "prev_period.json"
        prev_scores_path = self.bridge_data / "prev_scores.json"
        events_path = self.bridge_data / "events.jsonl"
        prev_status = load_json(prev_path, {}) or {}
        if not isinstance(prev_status, dict):
            prev_status = {}
        prev_status = {str(k): str(v) for k, v in prev_status.items()}

        prev_period = load_json(prev_period_path, {}) or {}
        if not isinstance(prev_period, dict):
            prev_period = {}
        prev_period = {str(k): str(v) for k, v in prev_period.items()}

        prev_scores = load_json(prev_scores_path, {}) or {}
        if not isinstance(prev_scores, dict):
            prev_scores = {}
        prev_scores = {str(k): v for k, v in prev_scores.items() if isinstance(v, dict)}

        score_events = detect_score_changes(paired, prev_scores)
        ft_events = detect_match_finished(paired, prev_status, prev_period)
        events = score_events + ft_events
        write_json(prev_path, prev_status)
        write_json(prev_period_path, prev_period)
        write_json(prev_scores_path, prev_scores)
        append_events(events_path, events)

        finished_n = sum(1 for r in paired if r.get("finished"))
        payload = {
            "matched_at": datetime.now(TZ_CN).isoformat(timespec="seconds"),
            "source": "match-bridge",
            "dqd_tab": dqd_snap.get("tab") or self.dqd_tab,
            "dqd_count": len(dqd_matches),
            "pm_count": len(pm_matches),
            "count": len(paired),
            "finished_count": finished_n,
            "min_score": self.min_score,
            "max_skew_min": self.max_skew_min,
            "min_side": self.min_side,
            "pm_stale_hours": self.pm_stale_hours,
            "events": events,
            "matches": paired,
        }
        write_json(self.bridge_data / "matches.json", payload)
        write_json(self.bridge_data / "latest.json", payload)
        with self.lock:
            self.last_result = payload
            self.match_ticks += 1
            self.last_error = None
            if events:
                self.last_events = list(events)
        return payload

    def run_once(self, *, refresh: bool = True) -> dict[str, Any]:
        if refresh:
            try:
                self.refresh_dqd_once()
            except Exception as e:  # noqa: BLE001
                with self.lock:
                    self.last_error = f"dqd: {e}"
                traceback.print_exc()
            try:
                self.refresh_pm_once()
            except Exception as e:  # noqa: BLE001
                with self.lock:
                    self.last_error = f"pm: {e}"
                traceback.print_exc()
        return self.rematch()

    def start(self) -> dict[str, Any]:
        with self.lock:
            if self.running:
                return {"ok": True, "already": True, **self.status()}
            self.running = True
            self._stop.clear()
            self.started_at = datetime.now(TZ_CN).isoformat(timespec="seconds")
            self.last_error = None

        # Loops fetch immediately; do not block start() on a slow PM pull.
        t_dqd = threading.Thread(target=self._dqd_loop, name="bridge-dqd", daemon=True)
        t_pm = threading.Thread(target=self._pm_loop, name="bridge-pm", daemon=True)
        self._threads = [t_dqd, t_pm]
        t_dqd.start()
        t_pm.start()
        return {"ok": True, "already": False, **self.status()}

    def stop(self) -> dict[str, Any]:
        with self.lock:
            self._stop.set()
            self.running = False
            threads = list(self._threads)
        for t in threads:
            t.join(timeout=2)
        with self.lock:
            self._threads = []
            return {"ok": True, **self.status()}

    def _dqd_loop(self) -> None:
        while not self._stop.is_set():
            sleep_s = self.dqd_idle_interval
            try:
                result = self.refresh_dqd_once()
                self.rematch()
                dqd_snap = load_json(self.dqd_data / "snapshot.json", {}) or {}
                dqd_matches = list(dqd_snap.get("matches") or [])
                if has_pending_ft_poll(dqd_matches):
                    # Played but period not FT yet — catch period→FT sooner.
                    sleep_s = PENDING_FT_POLL_SEC
                elif result.get("has_live"):
                    sleep_s = self.dqd_interval
                else:
                    sleep_s = self.dqd_idle_interval
            except Exception as e:  # noqa: BLE001
                with self.lock:
                    self.last_error = f"dqd: {e}"
                traceback.print_exc()
                sleep_s = 15
            self._stop.wait(sleep_s)
        with self.lock:
            self.running = self.running and any(t.is_alive() for t in self._threads)

    def _pm_loop(self) -> None:
        # First tick runs immediately (seed may also refresh; rematch is cheap).
        while not self._stop.is_set():
            try:
                self.refresh_pm_once()
                self.rematch()
            except Exception as e:  # noqa: BLE001
                with self.lock:
                    self.last_error = f"pm: {e}"
                traceback.print_exc()
                self._stop.wait(60)
                continue
            if self._stop.wait(self.pm_interval):
                break
