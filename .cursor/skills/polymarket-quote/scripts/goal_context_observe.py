"""Observe-only goal-context snapshots (DQD overview + AF live + list 旁证).

Phases:
  - ``af_confirmed`` — when AF goal confirm succeeds (gate or postcheck)
  - ``post_confirm_15s`` / ``post_confirm_45s`` — delayed follow-ups
  - ``dqd_reversal`` — DQD score_change reversal (reuses observe_group_id)

Does **not** gate buys or flatten. Failures are recorded as ``error`` fields.
"""

from __future__ import annotations

import json
import logging
import sys
import threading
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict

import quote_lib as lib

logger = logging.getLogger("pm_quote.goal_context_observe")

_AF_SCRIPTS = Path(__file__).resolve().parents[2] / "apifootball-bridge" / "scripts"
_af_sp = str(_AF_SCRIPTS)
if _af_sp not in sys.path:
    sys.path.insert(0, _af_sp)

import af_bridge_lib as aflib  # noqa: E402

DQD_OVERVIEW_URL = "https://www.dongqiudi.com/api/data/overview/match/{match_id}"
DQD_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

DEFAULT_DELAY_15_S = 15.0
DEFAULT_DELAY_45_S = 45.0
DEFAULT_WORKERS = 4
DEFAULT_HTTP_TIMEOUT_S = 12.0

PHASE_AF_CONFIRMED = "af_confirmed"
PHASE_POST_15 = "post_confirm_15s"
PHASE_POST_45 = "post_confirm_45s"
PHASE_DQD_REVERSAL = "dqd_reversal"

FetchOverviewFn = Callable[[str], Dict[str, Any]]
FetchAfFn = Callable[[str], Dict[str, Any]]
FetchListFn = Callable[[str], Dict[str, Any]]

_active: "GoalContextObserver | None" = None
_active_lock = threading.Lock()


def set_active_observer(observer: "GoalContextObserver | None") -> None:
    global _active
    with _active_lock:
        _active = observer


def get_active_observer() -> "GoalContextObserver | None":
    with _active_lock:
        return _active


def observe_path(root: Path) -> Path:
    return lib.data_dir(root) / "goal_context_observe.jsonl"


def make_observe_group_id(match_id: str, home: Any, away: Any, event_key: str) -> str:
    return f"{match_id}|{home}-{away}|{event_key}"


def compact_overview_events(payload: Any) -> dict[str, Any]:
    """Flatten DQD overview into match_status + compact event list."""
    root = payload
    if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
        root = payload["data"]
    if not isinstance(root, dict):
        return {"match_status": None, "events": []}

    match_status = root.get("match_status") or root.get("status") or None
    events_raw = root.get("events")
    flat: list[dict[str, Any]] = []

    def _push(minute: Any, side: str, ev: Any) -> None:
        if not isinstance(ev, dict):
            return
        flat.append(
            {
                "minute": str(minute) if minute is not None else "",
                "side": side,
                "code": ev.get("code") or ev.get("type") or "",
                "reason": ev.get("reason") or "",
                "person": ev.get("person") or ev.get("player") or "",
                "score": ev.get("score") or ev.get("fs") or "",
            }
        )

    if isinstance(events_raw, dict):
        for minute, bucket in events_raw.items():
            if isinstance(bucket, list):
                for ev in bucket:
                    _push(minute, "", ev)
                continue
            if not isinstance(bucket, dict):
                continue
            for key, side in (
                ("teamAEvents", "home"),
                ("team_A_events", "home"),
                ("teamBEvents", "away"),
                ("team_B_events", "away"),
                ("events", ""),
            ):
                for ev in bucket.get(key) or []:
                    _push(minute, side, ev)
            # Some payloads nest under home/away.
            for key, side in (("home", "home"), ("away", "away"), ("A", "home"), ("B", "away")):
                for ev in bucket.get(key) or []:
                    _push(minute, side, ev)
    elif isinstance(events_raw, list):
        for ev in events_raw:
            if not isinstance(ev, dict):
                continue
            minute = ev.get("minute") or ev.get("time") or ""
            side = str(ev.get("side") or "")
            if not side and ev.get("team") in ("A", "home", "teamA"):
                side = "home"
            elif not side and ev.get("team") in ("B", "away", "teamB"):
                side = "away"
            _push(minute, side, ev)

    return {"match_status": match_status, "events": flat}


def fetch_dqd_overview(
    match_id: str,
    *,
    timeout_s: float = DEFAULT_HTTP_TIMEOUT_S,
) -> dict[str, Any]:
    """GET DQD overview; returns compact overview or ``error``."""
    mid = str(match_id or "").strip()
    if not mid:
        return {"error": "missing_match_id"}
    url = DQD_OVERVIEW_URL.format(match_id=mid)
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": DQD_UA,
            "Accept": "application/json",
            "Referer": f"https://www.dongqiudi.com/match/{mid}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=max(0.5, float(timeout_s))) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return {"error": f"http_{e.code}"}
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}
    compact = compact_overview_events(body)
    compact["ok"] = True
    return compact


def compact_list_events(row: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(row, dict):
        return {
            "team_A_event": None,
            "team_B_event": None,
            "period": None,
            "minute": None,
            "status": None,
        }
    return {
        "team_A_event": row.get("team_A_event"),
        "team_B_event": row.get("team_B_event"),
        "period": row.get("period"),
        "minute": row.get("minute"),
        "status": row.get("status") or row.get("status_raw"),
    }


def lookup_list_events_from_bridge(root: Path, match_id: str) -> dict[str, Any]:
    """Best-effort list 旁证 from bridge ``matches.json`` (DQD side)."""
    mid = str(match_id or "")
    path = lib.bridge_dir(root) / "matches.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None
    except (OSError, json.JSONDecodeError):
        return compact_list_events(None)
    rows = raw if isinstance(raw, list) else (raw.get("matches") if isinstance(raw, dict) else None)
    if not isinstance(rows, list):
        return compact_list_events(None)
    for row in rows:
        if not isinstance(row, dict):
            continue
        dqd = row.get("dongqiudi") if isinstance(row.get("dongqiudi"), dict) else row
        if str(dqd.get("id") or dqd.get("match_id") or "") == mid:
            return compact_list_events(dqd)
    return compact_list_events(None)


@dataclass
class _GroupState:
    observe_group_id: str
    match_id: str
    event_key: str
    home: str
    away: str
    dqd_score: dict[str, Any]
    af_goals: dict[str, Any] | None
    gen: int = 0
    timers: list[threading.Timer] = field(default_factory=list)


class GoalContextObserver:
    """Background snapshots for AF-confirmed goals and DQD reversals."""

    def __init__(
        self,
        root: Path,
        *,
        delay_15_s: float = DEFAULT_DELAY_15_S,
        delay_45_s: float = DEFAULT_DELAY_45_S,
        workers: int = DEFAULT_WORKERS,
        fetch_overview: FetchOverviewFn | None = None,
        fetch_af: FetchAfFn | None = None,
        fetch_list: FetchListFn | None = None,
        http_timeout_s: float = DEFAULT_HTTP_TIMEOUT_S,
    ) -> None:
        self.root = Path(root)
        self.delay_15_s = max(0.0, float(delay_15_s))
        self.delay_45_s = max(0.0, float(delay_45_s))
        self.http_timeout_s = max(0.5, float(http_timeout_s))
        self._fetch_overview = fetch_overview
        self._fetch_af = fetch_af
        self._fetch_list = fetch_list
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._by_match: dict[str, _GroupState] = {}
        self._pool = ThreadPoolExecutor(
            max_workers=max(1, int(workers)),
            thread_name_prefix="goal-ctx-obs",
        )
        self._af_key: str | None = None
        self._af_client: aflib.AFClient | None = None
        self._cache: dict[str, Any] | None = None
        self._cache_mtime: float | None = None

    def start(self) -> None:
        self._stop.clear()
        set_active_observer(self)
        logger.info(
            "goal-context observe on → %s (delays=%ss/%ss)",
            observe_path(self.root),
            self.delay_15_s,
            self.delay_45_s,
        )

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            for st in self._by_match.values():
                for t in st.timers:
                    t.cancel()
                st.timers.clear()
        try:
            self._pool.shutdown(wait=False, cancel_futures=True)
        except TypeError:
            self._pool.shutdown(wait=False)
        if get_active_observer() is self:
            set_active_observer(None)

    def on_af_confirmed(
        self,
        root: Path | None = None,
        *,
        match_id: str,
        event_key: str,
        ev: dict[str, Any] | None = None,
        af_gate: dict[str, Any] | None = None,
    ) -> str | None:
        """Immediate snapshot + schedule +15s/+45s. Fire-and-forget safe."""
        if self._stop.is_set():
            return None
        mid = str(match_id or "").strip()
        if not mid:
            return None
        ev = ev if isinstance(ev, dict) else {}
        gate = af_gate if isinstance(af_gate, dict) else {}
        home = ev.get("home_score", gate.get("home_score"))
        away = ev.get("away_score", gate.get("away_score"))
        if isinstance(gate.get("goals"), dict):
            g = gate["goals"]
            if home is None:
                home = g.get("home")
            if away is None:
                away = g.get("away")
        key = str(event_key or "")
        group_id = make_observe_group_id(mid, home, away, key)
        af_goals = None
        if isinstance(gate.get("goals"), dict):
            af_goals = {
                "home": gate["goals"].get("home"),
                "away": gate["goals"].get("away"),
            }
        elif gate.get("home_score") is not None or gate.get("away_score") is not None:
            af_goals = {
                "home": gate.get("home_score"),
                "away": gate.get("away_score"),
            }
        dqd_score = {"home": home, "away": away}
        with self._lock:
            prev = self._by_match.get(mid)
            if prev is not None:
                for t in prev.timers:
                    t.cancel()
                prev.timers.clear()
                gen = prev.gen + 1
            else:
                gen = 1
            state = _GroupState(
                observe_group_id=group_id,
                match_id=mid,
                event_key=key,
                home=str(ev.get("home") or ""),
                away=str(ev.get("away") or ""),
                dqd_score=dqd_score,
                af_goals=af_goals,
                gen=gen,
            )
            self._by_match[mid] = state
            self._arm_delayed(state)
        self._pool.submit(
            self._safe_snapshot,
            PHASE_AF_CONFIRMED,
            state.observe_group_id,
            mid,
            key,
            state.home,
            state.away,
            dict(dqd_score),
            None,
            af_goals,
            False,
            gen,
        )
        return group_id

    def on_dqd_reversal(
        self,
        root: Path | None = None,
        *,
        match_id: str,
        event_key: str = "",
        ev: dict[str, Any] | None = None,
    ) -> str | None:
        """Snapshot on DQD reversal; reuse group id when available."""
        if self._stop.is_set():
            return None
        mid = str(match_id or "").strip()
        if not mid:
            return None
        ev = ev if isinstance(ev, dict) else {}
        with self._lock:
            linked = self._by_match.get(mid)
            if linked is not None:
                group_id = linked.observe_group_id
                home_name = linked.home or str(ev.get("home") or "")
                away_name = linked.away or str(ev.get("away") or "")
                key = linked.event_key or str(event_key or "")
                af_goals = linked.af_goals
                unlinked = False
                gen = linked.gen
            else:
                home = ev.get("home_score")
                away = ev.get("away_score")
                key = str(event_key or "")
                group_id = make_observe_group_id(mid, home, away, key or "reversal")
                home_name = str(ev.get("home") or "")
                away_name = str(ev.get("away") or "")
                af_goals = None
                unlinked = True
                gen = 0
        prev = ev.get("prev") if isinstance(ev.get("prev"), dict) else None
        dqd_score = {
            "home": ev.get("home_score", (ev.get("curr") or {}).get("home") if isinstance(ev.get("curr"), dict) else None),
            "away": ev.get("away_score", (ev.get("curr") or {}).get("away") if isinstance(ev.get("curr"), dict) else None),
        }
        dqd_prev = None
        if prev is not None:
            dqd_prev = {"home": prev.get("home"), "away": prev.get("away")}
        self._pool.submit(
            self._safe_snapshot,
            PHASE_DQD_REVERSAL,
            group_id,
            mid,
            key,
            home_name,
            away_name,
            dqd_score,
            dqd_prev,
            af_goals,
            unlinked,
            gen,
        )
        return group_id

    def _arm_delayed(self, state: _GroupState) -> None:
        for delay, phase in (
            (self.delay_15_s, PHASE_POST_15),
            (self.delay_45_s, PHASE_POST_45),
        ):
            gen = state.gen

            def _fire(ph: str = phase, g: int = gen, mid: str = state.match_id) -> None:
                if self._stop.is_set():
                    return
                with self._lock:
                    cur = self._by_match.get(mid)
                    if cur is None or cur.gen != g:
                        return
                    snap_state = cur
                self._pool.submit(
                    self._safe_snapshot,
                    ph,
                    snap_state.observe_group_id,
                    snap_state.match_id,
                    snap_state.event_key,
                    snap_state.home,
                    snap_state.away,
                    dict(snap_state.dqd_score),
                    None,
                    snap_state.af_goals,
                    False,
                    g,
                )

            t = threading.Timer(delay, _fire)
            t.daemon = True
            state.timers.append(t)
            t.start()

    def _af_client_cached(self) -> aflib.AFClient:
        if self._af_client is None:
            if self._af_key is None:
                self._af_key = aflib.load_af_key()
            self._af_client = aflib.AFClient(self._af_key, min_interval_s=0.0)
        return self._af_client

    def _fixture_cache(self) -> dict[str, Any]:
        path = aflib.DEFAULT_CACHE_PATH
        try:
            mtime = path.stat().st_mtime if path.is_file() else None
        except OSError:
            mtime = None
        if self._cache is None or mtime != self._cache_mtime:
            self._cache = aflib.load_cache(path)
            self._cache_mtime = mtime
        return self._cache

    def _default_fetch_af(self, match_id: str) -> dict[str, Any]:
        try:
            out = aflib.fetch_live_fixture_status_for_match_id(
                self._af_client_cached(),
                match_id,
                cache=self._fixture_cache(),
                cache_only=True,
                http_timeout=self.http_timeout_s,
            )
        except Exception as e:  # noqa: BLE001
            return {"error": str(e)}
        if not out.get("ok"):
            return {
                "error": out.get("error") or "af_fixture_failed",
                "status_short": out.get("status_short"),
                "status_long": out.get("status_long"),
                "elapsed": out.get("elapsed"),
                "extra": out.get("extra"),
                "goals": out.get("goals"),
                "score": out.get("score"),
                "af_fixture_id": out.get("af_fixture_id"),
            }
        return {
            "status_short": out.get("status_short"),
            "status_long": out.get("status_long"),
            "elapsed": out.get("elapsed"),
            "extra": out.get("extra"),
            "goals": out.get("goals"),
            "score": out.get("score"),
            "af_fixture_id": out.get("af_fixture_id"),
            "ok": True,
        }

    def _default_fetch_list(self, match_id: str) -> dict[str, Any]:
        return lookup_list_events_from_bridge(self.root, match_id)

    def _safe_snapshot(
        self,
        phase: str,
        observe_group_id: str,
        match_id: str,
        event_key: str,
        home: str,
        away: str,
        dqd_score: dict[str, Any],
        dqd_prev: dict[str, Any] | None,
        af_goals: dict[str, Any] | None,
        unlinked_reversal: bool,
        gen: int,
    ) -> None:
        try:
            self._write_snapshot(
                phase=phase,
                observe_group_id=observe_group_id,
                match_id=match_id,
                event_key=event_key,
                home=home,
                away=away,
                dqd_score=dqd_score,
                dqd_prev=dqd_prev,
                af_goals=af_goals,
                unlinked_reversal=unlinked_reversal,
                gen=gen,
            )
        except Exception as e:  # noqa: BLE001
            logger.exception("goal-context snapshot failed phase=%s match=%s", phase, match_id)
            try:
                lib.append_jsonl_async(
                    observe_path(self.root),
                    [
                        {
                            "quoted_at": lib.now_cn_iso(),
                            "phase": phase,
                            "observe_group_id": observe_group_id,
                            "match_id": match_id,
                            "event_key": event_key,
                            "home": home,
                            "away": away,
                            "dqd_score": dqd_score,
                            "dqd_prev": dqd_prev,
                            "af_goals": af_goals,
                            "unlinked_reversal": bool(unlinked_reversal),
                            "error": {"fatal": str(e)},
                        }
                    ],
                )
            except Exception:  # noqa: BLE001
                pass

    def _write_snapshot(
        self,
        *,
        phase: str,
        observe_group_id: str,
        match_id: str,
        event_key: str,
        home: str,
        away: str,
        dqd_score: dict[str, Any],
        dqd_prev: dict[str, Any] | None,
        af_goals: dict[str, Any] | None,
        unlinked_reversal: bool,
        gen: int,
    ) -> None:
        if self._stop.is_set():
            return
        # Drop superseded delayed jobs.
        if phase in (PHASE_POST_15, PHASE_POST_45):
            with self._lock:
                cur = self._by_match.get(match_id)
                if cur is None or cur.gen != gen:
                    return

        errors: dict[str, Any] = {}
        overview: dict[str, Any] | None = None
        af_fixture: dict[str, Any] | None = None
        list_events: dict[str, Any] | None = None

        try:
            ov = (self._fetch_overview or fetch_dqd_overview)(match_id)
            if isinstance(ov, dict) and ov.get("error") and not ov.get("ok"):
                errors["overview"] = ov.get("error")
                overview = {"match_status": None, "events": []}
            else:
                overview = {
                    "match_status": (ov or {}).get("match_status"),
                    "events": (ov or {}).get("events") or [],
                }
        except Exception as e:  # noqa: BLE001
            errors["overview"] = str(e)
            overview = {"match_status": None, "events": []}

        try:
            af = (self._fetch_af or self._default_fetch_af)(match_id)
            if isinstance(af, dict) and af.get("error") and not af.get("ok"):
                errors["af_fixture"] = af.get("error")
                af_fixture = {
                    "status_short": af.get("status_short"),
                    "status_long": af.get("status_long"),
                    "elapsed": af.get("elapsed"),
                    "extra": af.get("extra"),
                    "goals": af.get("goals"),
                    "score": af.get("score"),
                    "af_fixture_id": af.get("af_fixture_id"),
                }
            else:
                af_fixture = {
                    "status_short": (af or {}).get("status_short"),
                    "status_long": (af or {}).get("status_long"),
                    "elapsed": (af or {}).get("elapsed"),
                    "extra": (af or {}).get("extra"),
                    "goals": (af or {}).get("goals"),
                    "score": (af or {}).get("score"),
                    "af_fixture_id": (af or {}).get("af_fixture_id"),
                }
        except Exception as e:  # noqa: BLE001
            errors["af_fixture"] = str(e)
            af_fixture = None

        try:
            list_events = (self._fetch_list or self._default_fetch_list)(match_id)
            if isinstance(list_events, dict) and list_events.get("error"):
                errors["list_events"] = list_events.get("error")
        except Exception as e:  # noqa: BLE001
            errors["list_events"] = str(e)
            list_events = compact_list_events(None)

        row: dict[str, Any] = {
            "quoted_at": lib.now_cn_iso(),
            "phase": phase,
            "observe_group_id": observe_group_id,
            "match_id": match_id,
            "event_key": event_key,
            "home": home,
            "away": away,
            "dqd_score": dqd_score,
            "overview": overview,
            "af_fixture": af_fixture,
            "list_events": list_events,
        }
        if dqd_prev is not None:
            row["dqd_prev"] = dqd_prev
        if af_goals is not None:
            row["af_goals"] = af_goals
        if unlinked_reversal:
            row["unlinked_reversal"] = True
        if errors:
            row["error"] = errors
        lib.append_jsonl_async(observe_path(self.root), [row])
