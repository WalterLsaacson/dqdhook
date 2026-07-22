#!/usr/bin/env python3
"""Match Dongqiudi fixtures to Polymarket events and emit market handles."""

from __future__ import annotations

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

from team_aliases import TEAM_ALIASES

TZ_CN = timezone(timedelta(hours=8))

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

# Canonical alias table — extend in team_aliases.py
_TEAM_ALIASES = TEAM_ALIASES

_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)
_WS_RE = re.compile(r"\s+")


def repo_root_from(scripts_file: Path) -> Path:
    # scripts -> match-bridge -> skills -> .cursor -> repo
    return scripts_file.resolve().parents[4]


def normalize_team(name: str) -> str:
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
    tokens = [t for t in s.split(" ") if t and t not in _TEAM_STOP and not t.isdigit()]
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


def _parse_hhmm(value: str) -> int | None:
    try:
        hh, mm = str(value).strip().split(":")[:2]
        return int(hh) * 60 + int(mm)
    except (ValueError, AttributeError):
        return None


def kickoff_minutes_apart(a: dict[str, Any], b: dict[str, Any]) -> int | None:
    """Absolute minute distance using local_date + time (Beijing)."""
    da = str(a.get("local_date") or "")
    db = str(b.get("local_date") or "")
    ta = _parse_hhmm(str(a.get("time") or ""))
    tb = _parse_hhmm(str(b.get("time") or ""))
    if not da or not db or ta is None or tb is None:
        return None
    try:
        dta = datetime.strptime(f"{da} {a.get('time')}", "%Y-%m-%d %H:%M").replace(tzinfo=TZ_CN)
        dtb = datetime.strptime(f"{db} {b.get('time')}", "%Y-%m-%d %H:%M").replace(tzinfo=TZ_CN)
    except ValueError:
        return abs(ta - tb) if da == db else None
    return int(abs((dta - dtb).total_seconds()) // 60)


def score_pair(dqd: dict[str, Any], pm: dict[str, Any], *, max_skew_min: int = 45) -> float:
    skew = kickoff_minutes_apart(dqd, pm)
    if skew is None or skew > max_skew_min:
        return 0.0
    home_s = team_similarity(dqd.get("home") or "", pm.get("home") or "")
    away_s = team_similarity(dqd.get("away") or "", pm.get("away") or "")
    direct = (home_s + away_s) / 2
    swap = (
        team_similarity(dqd.get("home") or "", pm.get("away") or "")
        + team_similarity(dqd.get("away") or "", pm.get("home") or "")
    ) / 2
    best = max(direct, swap)
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
    min_score: float = 0.62,
    max_skew_min: int = 45,
) -> list[dict[str, Any]]:
    """Greedy 1:1 matching by similarity score."""
    candidates: list[tuple[float, int, int]] = []
    for i, d in enumerate(dqd_matches):
        for j, p in enumerate(pm_matches):
            s = score_pair(d, p, max_skew_min=max_skew_min)
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
        p = pm_matches[j]
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
    if not events:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        for ev in events:
            f.write(json.dumps(ev, ensure_ascii=False) + "\n")


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
    """Emit score_change when a matched live fixture's score ticks (goal).

    First sighting only seeds prev_scores. Used by polymarket-quote for
    mid-match locked-outcome scans (e.g. 1-0 kills 0-0 exact score).
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

        # Soccer scores only rise; ignore non-monotonic glitches for emission.
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
) -> list[dict[str, Any]]:
    """Emit match_finished when DQD status transitions into played.

    First sighting only seeds prev_status (no event). Dongqiudi skill exposes
    status/status_raw; bridge owns the终局 trigger.
    """
    events: list[dict[str, Any]] = []
    ts = datetime.now(TZ_CN).isoformat(timespec="seconds")
    for row in paired:
        dqd = row.get("dongqiudi") or {}
        pm = row.get("polymarket") or {}
        mid = str(dqd.get("id") or "")
        if not mid:
            continue
        curr = status_bucket(dqd)
        finished = curr == "played"
        dqd["is_finished"] = finished
        row["finished"] = finished
        row["dongqiudi"] = dqd

        prev = prev_status.get(mid)
        if prev is None:
            prev_status[mid] = curr
            continue

        if prev != "played" and curr == "played":
            events.append(
                {
                    "type": "match_finished",
                    "ts": ts,
                    "match_id": mid,
                    "prev_status": prev,
                    "status": curr,
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
            # Already finished before we started watching — no toast, keep flag.
            pass

        prev_status[mid] = curr
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
        min_score: float = 0.62,
    ) -> None:
        self.root = root
        # full tab overlaps Polymarket's multi-league 48h window better than hot-only
        self.dqd_tab = dqd_tab
        self.dqd_interval = max(10, dqd_interval)
        self.dqd_idle_interval = max(max(30, self.dqd_interval), dqd_idle_interval)
        self.pm_interval = max(120, pm_interval)
        self.pm_within_hours = pm_within_hours
        self.min_score = min_score

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
        paired = match_fixtures(dqd_matches, pm_matches, min_score=self.min_score)

        prev_path = self.bridge_data / "prev_status.json"
        prev_scores_path = self.bridge_data / "prev_scores.json"
        events_path = self.bridge_data / "events.jsonl"
        prev_status = load_json(prev_path, {}) or {}
        if not isinstance(prev_status, dict):
            prev_status = {}
        prev_status = {str(k): str(v) for k, v in prev_status.items()}

        prev_scores = load_json(prev_scores_path, {}) or {}
        if not isinstance(prev_scores, dict):
            prev_scores = {}
        prev_scores = {str(k): v for k, v in prev_scores.items() if isinstance(v, dict)}

        score_events = detect_score_changes(paired, prev_scores)
        ft_events = detect_match_finished(paired, prev_status)
        events = score_events + ft_events
        write_json(prev_path, prev_status)
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
                sleep_s = self.dqd_interval if result.get("has_live") else self.dqd_idle_interval
                self.rematch()
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
