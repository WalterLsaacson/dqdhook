#!/usr/bin/env python3
"""AF referee gate: confirm Dongqiudi goal-ups via apifootball-bridge skill lib.

Fixture **mapping** is owned by apifootball-bridge ``sync``/``watch``
(``data/apifootball/fixture_cache.json``). The referee is **cache-only**: it
never resolves DQD→AF fixtures on the quote hot path. Cache miss / unresolved
→ skip the goal immediately (no 120s spin).

Score confirmation polls ``fetch_events_for_match_id(..., cache_only=True)`` on
a tiered schedule: **5s → every 2s until 60s → every 5s** (override with
``--af-poll`` for a fixed interval). Confirmations run on a thread pool so
watch stays responsive; on confirm, burst + ``af_confirmed_scores.json``
persist asynchronously.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

TZ_CN = timezone(timedelta(hours=8))

# Legacy fixed-interval default (tests / --af-poll override).
DEFAULT_POLL_S = 0.5
DEFAULT_TIMEOUT_S = 120.0
DEFAULT_WORKERS = 4

# Tiered confirm schedule (production default): first look at 5s, then every 2s
# through 60s, then every 5s until timeout. Cuts AF events quota vs 0.5s polling
# while keeping ~1s mean wait after AF score is ready.
DEFAULT_FIRST_DELAY_S = 5.0
DEFAULT_MID_PERIOD_S = 2.0
DEFAULT_MID_UNTIL_S = 60.0
DEFAULT_LATE_PERIOD_S = 5.0

_AF_SCRIPTS = Path(__file__).resolve().parents[2] / "apifootball-bridge" / "scripts"
_af_sp = str(_AF_SCRIPTS)
if _af_sp not in sys.path:
    sys.path.insert(0, _af_sp)

import af_bridge_lib as aflib  # noqa: E402

# Process-local confirmed scores (disk is async / best-effort).
_MEMORY_SCORES: dict[str, tuple[int, int]] = {}
_MEMORY_LOCK = threading.Lock()
_DISK_EXEC = ThreadPoolExecutor(max_workers=1, thread_name_prefix="af-ref-disk")


def iso_now() -> str:
    return datetime.now(TZ_CN).isoformat(timespec="seconds")


def confirmed_scores_path(root: Path) -> Path:
    return root / "data" / "pm-quote" / "af_confirmed_scores.json"


def load_confirmed_scores(root: Path) -> dict[str, Any]:
    path = confirmed_scores_path(root)
    if not path.is_file():
        return {"updated_at": None, "scores": {}}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"updated_at": None, "scores": {}}
    if not isinstance(raw, dict):
        return {"updated_at": None, "scores": {}}
    scores = raw.get("scores") if isinstance(raw.get("scores"), dict) else {}
    return {
        "updated_at": raw.get("updated_at"),
        "scores": {str(k): v for k, v in scores.items() if isinstance(v, dict)},
    }


def save_confirmed_scores(root: Path, store: dict[str, Any]) -> None:
    path = confirmed_scores_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    out = dict(store)
    out["updated_at"] = iso_now()
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def get_confirmed_score(root: Path, match_id: str) -> tuple[int, int] | None:
    mid = str(match_id)
    with _MEMORY_LOCK:
        mem = _MEMORY_SCORES.get(mid)
    if mem is not None:
        return mem
    store = load_confirmed_scores(root)
    row = (store.get("scores") or {}).get(mid)
    if not isinstance(row, dict):
        return None
    try:
        h, a = row.get("home"), row.get("away")
        if h is None or a is None:
            return None
        out = (int(h), int(a))
        with _MEMORY_LOCK:
            _MEMORY_SCORES[mid] = out
        return out
    except (TypeError, ValueError):
        return None


def set_confirmed_score(
    root: Path,
    match_id: str,
    home: int,
    away: int,
    *,
    af_fixture_id: int | None = None,
    source: str = "af_bridge_events",
    burst_dir: str | None = None,
    persist: bool = True,
) -> None:
    mid = str(match_id)
    with _MEMORY_LOCK:
        _MEMORY_SCORES[mid] = (int(home), int(away))
    if not persist:
        return
    store = load_confirmed_scores(root)
    scores = dict(store.get("scores") or {})
    scores[mid] = {
        "home": int(home),
        "away": int(away),
        "af_fixture_id": af_fixture_id,
        "source": source,
        "burst_dir": burst_dir,
        "confirmed_at": iso_now(),
    }
    store["scores"] = scores
    save_confirmed_scores(root, store)


def set_confirmed_score_async(
    root: Path,
    match_id: str,
    home: int,
    away: int,
    *,
    af_fixture_id: int | None = None,
    source: str = "af_bridge_events",
    burst_dir: str | None = None,
) -> None:
    """Update memory immediately; disk write on background thread."""
    mid = str(match_id)
    with _MEMORY_LOCK:
        _MEMORY_SCORES[mid] = (int(home), int(away))
    _DISK_EXEC.submit(
        set_confirmed_score,
        root,
        mid,
        int(home),
        int(away),
        af_fixture_id=af_fixture_id,
        source=source,
        burst_dir=burst_dir,
        persist=True,
    )


def event_is_goal_up(ev: dict[str, Any]) -> bool:
    if str(ev.get("type") or "") != "score_change":
        return False
    if ev.get("is_reversal"):
        return False
    if ev.get("is_goal") is True:
        return True
    prev = ev.get("prev") or {}
    curr = ev.get("curr") or {}
    try:
        ph = int(prev.get("home"))
        pa = int(prev.get("away"))
        ch = int(curr.get("home", ev.get("home_score")))
        ca = int(curr.get("away", ev.get("away_score")))
    except (TypeError, ValueError):
        return False
    return ch >= ph and ca >= pa and (ch > ph or ca > pa)


def event_is_reversal(ev: dict[str, Any]) -> bool:
    if str(ev.get("type") or "") != "score_change":
        return False
    if ev.get("is_reversal"):
        return True
    prev = ev.get("prev") or {}
    curr = ev.get("curr") or {}
    try:
        ph = int(prev.get("home"))
        pa = int(prev.get("away"))
        ch = int(curr.get("home", ev.get("home_score")))
        ca = int(curr.get("away", ev.get("away_score")))
    except (TypeError, ValueError):
        return False
    return ch < ph or ca < pa


def target_score_from_event(ev: dict[str, Any]) -> tuple[int, int] | None:
    curr = ev.get("curr") or {}
    try:
        h = curr.get("home", ev.get("home_score"))
        a = curr.get("away", ev.get("away_score"))
        if h is None or a is None:
            return None
        return int(h), int(a)
    except (TypeError, ValueError):
        return None


def baseline_score_from_event(ev: dict[str, Any]) -> tuple[int, int] | None:
    prev = ev.get("prev") or {}
    try:
        if prev.get("home") is None or prev.get("away") is None:
            return None
        return int(prev["home"]), int(prev["away"])
    except (TypeError, ValueError, KeyError):
        return None


def apply_af_score_to_event(
    ev: dict[str, Any],
    *,
    home: int,
    away: int,
) -> dict[str, Any]:
    out = dict(ev)
    out["home_score"] = int(home)
    out["away_score"] = int(away)
    out["curr"] = {"home": int(home), "away": int(away)}
    out["score_source"] = "api_football"
    return out


def af_score_satisfies(
    af: tuple[int, int],
    target: tuple[int, int],
    *,
    baseline: tuple[int, int] | None = None,
) -> tuple[bool, tuple[int, int]]:
    """Whether AF score confirms the DQD goal-up.

    Exact match always wins. If AF is already ahead of the DQD target (e.g. AF
    2-0 while DQD just reported 1-0), accept and use AF as truth — as long as AF
    did not drop below the pre-goal baseline.
    """
    ah, aa = int(af[0]), int(af[1])
    th, ta = int(target[0]), int(target[1])
    if ah == th and aa == ta:
        return True, (ah, aa)
    if ah >= th and aa >= ta:
        if baseline is None:
            return True, (ah, aa)
        bh, ba = int(baseline[0]), int(baseline[1])
        if ah >= bh and aa >= ba:
            return True, (ah, aa)
    return False, (ah, aa)


def _is_rate_limited(payload: dict[str, Any]) -> bool:
    if payload.get("http_status") == 429:
        return True
    blob = str(payload.get("error") or payload.get("errors") or "").lower()
    return "rate" in blob or "limit" in blob and "request" in blob


def call_af_bridge_events(
    match_id: str,
    *,
    af: aflib.AFClient,
    cache: dict[str, Any],
    persist_burst: bool = False,
    persist_cache: bool = False,
    cache_only: bool = True,
) -> dict[str, Any]:
    """In-process apifootball-bridge events path (same as CLI ``events``).

    Default ``cache_only=True``: fixture mapping comes only from sync/watch cache.
    """
    return aflib.fetch_events_for_match_id(
        af,
        str(match_id),
        cache=cache,
        persist_cache=persist_cache,
        persist_burst=persist_burst,
        cache_only=cache_only,
    )


_CACHE_MISS_ERRORS = frozenset(
    {
        "af_fixture_not_cached",
        "af_fixture_unresolved",
        "af_fixture_unresolved_ttl",
    }
)


def confirm_check_times(
    timeout_s: float,
    *,
    first_delay_s: float = DEFAULT_FIRST_DELAY_S,
    mid_period_s: float = DEFAULT_MID_PERIOD_S,
    mid_until_s: float = DEFAULT_MID_UNTIL_S,
    late_period_s: float = DEFAULT_LATE_PERIOD_S,
) -> list[float]:
    """Absolute seconds (from goal) at which to call AF events.

    Schedule: ``first_delay``, then every ``mid_period`` until ``mid_until``,
    then every ``late_period`` until ``timeout_s``.
    """
    timeout = max(0.0, float(timeout_s))
    first = max(0.0, float(first_delay_s))
    mid_p = max(0.05, float(mid_period_s))
    mid_until = max(first, float(mid_until_s))
    late_p = max(0.05, float(late_period_s))

    checks: list[float] = []
    if first <= timeout:
        checks.append(round(first, 3))
    t = first + mid_p
    while t <= mid_until + 1e-9 and t <= timeout + 1e-9:
        checks.append(round(t, 3))
        t += mid_p
    # Ensure a check at mid_until when it falls on the grid boundary.
    if mid_until <= timeout and (not checks or checks[-1] < mid_until - 1e-9):
        if mid_until >= first:
            checks.append(round(mid_until, 3))
    t = (checks[-1] + late_p) if checks else late_p
    while t <= timeout + 1e-9:
        checks.append(round(t, 3))
        t += late_p
    # De-dupe / sort
    out = sorted(set(checks))
    return [c for c in out if c <= timeout + 1e-9]


def schedule_label(
    *,
    first_delay_s: float = DEFAULT_FIRST_DELAY_S,
    mid_period_s: float = DEFAULT_MID_PERIOD_S,
    mid_until_s: float = DEFAULT_MID_UNTIL_S,
    late_period_s: float = DEFAULT_LATE_PERIOD_S,
) -> str:
    return (
        f"{first_delay_s:g}s→every {mid_period_s:g}s to {mid_until_s:g}s→"
        f"every {late_period_s:g}s"
    )


class AfReferee:
    """Async AF confirmations via apifootball-bridge lib + thread pool."""

    def __init__(
        self,
        root: Path,
        *,
        poll_s: float = DEFAULT_POLL_S,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        env_path: Path | None = None,
        max_workers: int = DEFAULT_WORKERS,
        events_fn: Callable[..., dict[str, Any]] | None = None,
        poll_schedule: bool = True,
        first_delay_s: float = DEFAULT_FIRST_DELAY_S,
        mid_period_s: float = DEFAULT_MID_PERIOD_S,
        mid_until_s: float = DEFAULT_MID_UNTIL_S,
        late_period_s: float = DEFAULT_LATE_PERIOD_S,
    ) -> None:
        self.root = Path(root)
        self.poll_s = max(0.05, float(poll_s))
        self.timeout_s = max(1.0, float(timeout_s))
        self.poll_schedule = bool(poll_schedule)
        self.first_delay_s = max(0.0, float(first_delay_s))
        self.mid_period_s = max(0.05, float(mid_period_s))
        self.mid_until_s = max(0.0, float(mid_until_s))
        self.late_period_s = max(0.05, float(late_period_s))
        self.env_path = env_path
        self._events_fn = events_fn
        self._af: aflib.AFClient | None = None
        self._cache: dict[str, Any] | None = None
        self._cache_mtime: float | None = None
        self._exec = ThreadPoolExecutor(
            max_workers=max(1, int(max_workers)),
            thread_name_prefix="af-ref",
        )
        self._lock = threading.Lock()
        self._pending: dict[str, Future] = {}
        self._meta: dict[str, dict[str, Any]] = {}

    def _schedule_desc(self) -> str:
        if self.poll_schedule:
            return schedule_label(
                first_delay_s=self.first_delay_s,
                mid_period_s=self.mid_period_s,
                mid_until_s=self.mid_until_s,
                late_period_s=self.late_period_s,
            )
        return f"fixed {self.poll_s}s"

    def _client(self) -> aflib.AFClient:
        if self._af is None:
            key = aflib.load_af_key(self.env_path)
            # No Free-plan 6.5s spacing — referee needs ~500ms polls; backoff on 429.
            self._af = aflib.AFClient(key, min_interval_s=0.0)
        return self._af

    def _reload_fixture_cache(self) -> dict[str, Any]:
        """Re-read fixture_cache.json when AF watch/sync updates it on disk."""
        path = aflib.DEFAULT_CACHE_PATH
        mtime: float | None = None
        try:
            mtime = path.stat().st_mtime if path.is_file() else None
        except OSError:
            mtime = None
        if self._cache is None or mtime != self._cache_mtime:
            self._cache = aflib.load_cache(path)
            self._cache_mtime = mtime
        return self._cache

    def _fixture_cache(self) -> dict[str, Any]:
        return self._reload_fixture_cache()

    def cached_af_fixture_id(self, match_id: str) -> int | None:
        ent = aflib.cached_fixture_entry(self._fixture_cache(), str(match_id))
        if not ent:
            return None
        try:
            return int(ent["af_fixture_id"])
        except (TypeError, ValueError, KeyError):
            return None

    def pending_event_keys(self) -> set[str]:
        with self._lock:
            return set(self._pending.keys())

    def poll_once(self, match_id: str, *, persist_burst: bool = False) -> dict[str, Any]:
        if self._events_fn is not None:
            try:
                return self._events_fn(str(match_id), persist_burst=persist_burst)
            except TypeError:
                return self._events_fn(str(match_id))
        out = call_af_bridge_events(
            str(match_id),
            af=self._client(),
            cache=self._fixture_cache(),
            persist_burst=persist_burst,
            persist_cache=False,
            cache_only=True,
        )
        return out

    def _persist_confirm_side_effects(
        self, match_id: str, last: dict[str, Any], truth: tuple[int, int]
    ) -> None:
        """Background burst artifact + fixture cache (off hot path).

        Score disk write is handled by ``set_confirmed_score_async``. Burst may
        trigger one AF HTTP here — never on the confirm return path.
        """
        mid = str(match_id)
        burst = str(last.get("burst_dir") or "") or None
        if burst is None:
            try:
                out = self.poll_once(mid, persist_burst=True)
                burst = str(out.get("burst_dir") or "") or None
                if burst:
                    # Refresh disk row with burst_dir once available.
                    fid = last.get("af_fixture_id") or out.get("af_fixture_id")
                    set_confirmed_score(
                        self.root,
                        mid,
                        truth[0],
                        truth[1],
                        af_fixture_id=int(fid) if fid is not None else None,
                        burst_dir=burst,
                        persist=True,
                    )
            except Exception:  # noqa: BLE001
                pass
        # Referee is cache-only; do not overwrite sync/watch fixture_cache.json.

    def await_score(
        self,
        match_id: str,
        target: tuple[int, int],
        *,
        baseline: tuple[int, int] | None = None,
        poll_s: float | None = None,
        timeout_s: float | None = None,
    ) -> dict[str, Any]:
        """Block until AF confirms target (or AF already covers it) / timeout.

        Prefer calling via ``submit`` + ``drain_done`` so watch is not blocked.
        On confirm: return immediately (memory score); disk/burst async.

        Fixture mapping is cache-only (filled by AF sync/watch). If the DQD id
        is not in ``fixture_cache.json`` entries, skip immediately — no resolve,
        no 120s spin.

        Default poll schedule: first check at 5s, every 2s until 60s, then every
        5s (override with ``poll_schedule=False`` + ``poll_s`` for fixed interval).
        """
        poll = self.poll_s if poll_s is None else max(0.05, float(poll_s))
        timeout = self.timeout_s if timeout_s is None else max(1.0, float(timeout_s))
        th, ta = int(target[0]), int(target[1])
        # Prefer persisted AF truth as baseline when present.
        stored = get_confirmed_score(self.root, str(match_id))
        base = stored if stored is not None else baseline

        mid = str(match_id)
        # Fresh disk cache from AF board sync/watch.
        self._reload_fixture_cache()
        if self._events_fn is None and self.cached_af_fixture_id(mid) is None:
            err = aflib.fixture_miss_error(self._fixture_cache(), mid)
            print(
                f"af-referee → skip {mid} target={th}-{ta} err={err} (cache-only)",
                flush=True,
            )
            return {
                "ok": False,
                "confirmed": False,
                "match_id": mid,
                "target": {"home": th, "away": ta},
                "goals": {"home": None, "away": None},
                "baseline": {"home": base[0], "away": base[1]} if base else None,
                "af_fixture_id": None,
                "burst_dir": None,
                "polls": 0,
                "elapsed_ms": 0,
                "poll_s": poll,
                "timeout_s": timeout,
                "schedule": self._schedule_desc(),
                "error": err,
                "via": "apifootball-bridge",
                "cache_only": True,
            }

        if self.poll_schedule:
            check_at = confirm_check_times(
                timeout,
                first_delay_s=self.first_delay_s,
                mid_period_s=self.mid_period_s,
                mid_until_s=self.mid_until_s,
                late_period_s=self.late_period_s,
            )
        else:
            # Fixed interval from t=0 (tests / --af-poll override).
            check_at = []
            t = 0.0
            while t <= timeout + 1e-9:
                check_at.append(round(t, 3))
                t += poll
            if not check_at:
                check_at = [0.0]

        t0 = time.monotonic()
        polls = 0
        last: dict[str, Any] = {}
        last_goals: dict[str, int | None] = {"home": None, "away": None}
        last_error: Any = None
        rate_hits = 0
        check_i = 0

        while check_i < len(check_at):
            target_at = check_at[check_i]
            # Sleep until this check's wall time (from confirm start).
            while True:
                elapsed = time.monotonic() - t0
                if elapsed >= timeout:
                    break
                remain = target_at - elapsed
                if remain <= 0:
                    break
                time.sleep(min(remain, 0.25))
            if (time.monotonic() - t0) >= timeout and (time.monotonic() - t0) < target_at:
                break

            polls += 1
            check_i += 1
            # Pick up late sync mappings between polls.
            self._reload_fixture_cache()
            try:
                last = self.poll_once(mid, persist_burst=False)
            except Exception as e:  # noqa: BLE001
                last_error = str(e)
                last = {"ok": False, "error": str(e), "goals": last_goals}

            elapsed_ms = int((time.monotonic() - t0) * 1000)

            miss = str(last.get("error") or "")
            if miss in _CACHE_MISS_ERRORS:
                return {
                    "ok": False,
                    "confirmed": False,
                    "match_id": mid,
                    "target": {"home": th, "away": ta},
                    "goals": last_goals,
                    "baseline": {"home": base[0], "away": base[1]} if base else None,
                    "af_fixture_id": None,
                    "burst_dir": None,
                    "polls": polls,
                    "elapsed_ms": elapsed_ms,
                    "poll_s": poll,
                    "timeout_s": timeout,
                    "schedule": self._schedule_desc(),
                    "error": miss,
                    "via": "apifootball-bridge",
                    "cache_only": True,
                }

            if _is_rate_limited(last):
                rate_hits += 1
                last_error = last.get("error") or last.get("errors") or "rate_limited"
                backoff = min(8.0, 0.5 * (2 ** min(rate_hits, 4)))
                # Retry same slot after backoff (do not burn next schedule slot).
                check_i -= 1
                if (time.monotonic() - t0) >= timeout:
                    break
                time.sleep(backoff)
                continue

            if last.get("ok"):
                goals = last.get("goals") or {}
                last_goals = {"home": goals.get("home"), "away": goals.get("away")}
                try:
                    gh, ga = goals.get("home"), goals.get("away")
                    if gh is not None and ga is not None:
                        ok, truth = af_score_satisfies(
                            (int(gh), int(ga)), (th, ta), baseline=base
                        )
                        if ok:
                            fid = last.get("af_fixture_id")
                            # Hot path: memory only — no second AF fetch, no sync disk.
                            set_confirmed_score_async(
                                self.root,
                                mid,
                                truth[0],
                                truth[1],
                                af_fixture_id=int(fid) if fid is not None else None,
                                burst_dir=str(last.get("burst_dir") or "") or None,
                            )
                            _DISK_EXEC.submit(
                                self._persist_confirm_side_effects,
                                mid,
                                dict(last),
                                truth,
                            )
                            return {
                                "ok": True,
                                "confirmed": True,
                                "match_id": mid,
                                "target": {"home": th, "away": ta},
                                "goals": {"home": truth[0], "away": truth[1]},
                                "baseline": (
                                    {"home": base[0], "away": base[1]} if base else None
                                ),
                                "af_fixture_id": fid,
                                "burst_dir": last.get("burst_dir"),
                                "polls": polls,
                                "elapsed_ms": elapsed_ms,
                                "poll_s": poll,
                                "timeout_s": timeout,
                                "schedule": self._schedule_desc(),
                                "via": "apifootball-bridge",
                                "persist": "async",
                                "cache_only": True,
                            }
                except (TypeError, ValueError):
                    pass
            else:
                last_error = last.get("error") or last.get("errors") or last_error

            if (time.monotonic() - t0) >= timeout:
                break

        return {
            "ok": False,
            "confirmed": False,
            "match_id": mid,
            "target": {"home": th, "away": ta},
            "goals": last_goals,
            "baseline": {"home": base[0], "away": base[1]} if base else None,
            "af_fixture_id": last.get("af_fixture_id"),
            "burst_dir": last.get("burst_dir"),
            "polls": polls,
            "elapsed_ms": int((time.monotonic() - t0) * 1000),
            "poll_s": poll,
            "timeout_s": timeout,
            "schedule": self._schedule_desc(),
            "error": last_error or "af_confirm_timeout",
            "via": "apifootball-bridge",
            "cache_only": True,
        }

    def submit(
        self,
        event_key: str,
        ev: dict[str, Any],
        target: tuple[int, int],
    ) -> bool:
        """Enqueue non-blocking confirmation. Returns False if already pending."""
        mid = str(ev.get("match_id") or "")
        if not mid:
            return False
        with self._lock:
            if event_key in self._pending:
                return False
            stored = get_confirmed_score(self.root, mid)
            baseline = stored if stored is not None else baseline_score_from_event(ev)
            fut = self._exec.submit(
                self.await_score,
                mid,
                target,
                baseline=baseline,
            )
            self._pending[event_key] = fut
            self._meta[event_key] = {
                "ev": dict(ev),
                "target": (int(target[0]), int(target[1])),
                "match_id": mid,
                "submitted_at": iso_now(),
            }
        print(
            f"af-referee → queued {mid} target={target[0]}-{target[1]} "
            f"key={event_key} (async · cache-only · {self._schedule_desc()} · "
            f"timeout {self.timeout_s}s)",
            flush=True,
        )
        return True

    def drain_done(self) -> list[dict[str, Any]]:
        """Return completed confirm jobs (confirmed or timed out)."""
        with self._lock:
            done_keys = [k for k, fut in self._pending.items() if fut.done()]
        out: list[dict[str, Any]] = []
        for key in done_keys:
            with self._lock:
                fut = self._pending.pop(key, None)
                meta = self._meta.pop(key, None)
            if fut is None or meta is None:
                continue
            try:
                gate = fut.result()
            except Exception as e:  # noqa: BLE001
                gate = {
                    "ok": False,
                    "confirmed": False,
                    "error": str(e),
                    "match_id": meta.get("match_id"),
                    "target": {
                        "home": meta["target"][0],
                        "away": meta["target"][1],
                    },
                }
            out.append(
                {
                    "event_key": key,
                    "ev": meta["ev"],
                    "target": meta["target"],
                    "match_id": meta["match_id"],
                    "gate": gate,
                }
            )
        return out
