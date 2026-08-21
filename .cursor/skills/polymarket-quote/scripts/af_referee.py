#!/usr/bin/env python3
"""AF referee gate: confirm Dongqiudi goal-ups via apifootball-bridge skill lib.

Fixture **mapping** is owned by apifootball-bridge ``sync``/``watch``
(``data/apifootball/fixture_cache.json``). The referee is **cache-only**: it
never resolves DQD→AF fixtures on the quote hot path. Cache miss / unresolved
→ skip the goal immediately (no timeout spin).

Score confirmation polls ``fetch_events_for_match_id(..., cache_only=True)`` on
a tiered schedule: **3s → every 1s until 60s → every 2s until 90s** (override
with ``--af-poll`` for a fixed interval). That cadence is the contract: the
referee does **not** insert a multi-second shared throttle between schedule
ticks (optional ``QUOTE_AF_MIN_INTERVAL_S`` is per-worker only). Confirmations
run on a thread pool so watch stays responsive; on confirm,
``af_confirmed_scores.json`` persists asynchronously without an extra AF fetch.
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
DEFAULT_TIMEOUT_S = 90.0
# Enough parallel confirms so new goals are not queued behind 90s jobs.
DEFAULT_WORKERS = 8

# Confirm schedule (production default):
#   first look at 3s → every 1s until 60s → every 2s until timeout (90s).
DEFAULT_FIRST_DELAY_S = 3.0
DEFAULT_PERIOD_S = 1.0  # early period (alias for early_period_s)
DEFAULT_LATE_AFTER_S = 60.0
DEFAULT_LATE_PERIOD_S = 2.0
# Per-worker AFClient spacing only (default off). A shared 6.5s throttle used
# to destroy the 2s schedule when several confirms ran at once — do not restore
# that as the default. Set QUOTE_AF_MIN_INTERVAL_S if you need free-plan pacing.
DEFAULT_AF_MIN_INTERVAL_S = 0.0
# Soft coalesce: reuse last good poll for same DQD id within this window.
_POLL_COALESCE_S = 1.0
# match_finished older than this → skip (restart replay / late DQD).
DEFAULT_FT_MAX_AGE_S = 15 * 60.0
# Env override for FT freshness.
_FT_MAX_AGE_ENV = "QUOTE_FT_MAX_AGE_S"
_AF_MIN_INTERVAL_ENV = "QUOTE_AF_MIN_INTERVAL_S"

_AF_SCRIPTS = Path(__file__).resolve().parents[2] / "apifootball-bridge" / "scripts"
_af_sp = str(_AF_SCRIPTS)
if _af_sp not in sys.path:
    sys.path.insert(0, _af_sp)

import af_bridge_lib as aflib  # noqa: E402

_BRIDGE_SCRIPTS = Path(__file__).resolve().parents[2] / "match-bridge" / "scripts"
_br_sp = str(_BRIDGE_SCRIPTS)
if _br_sp not in sys.path:
    sys.path.insert(0, _br_sp)

import bridge_lib as bridge  # noqa: E402

# Process-local confirmed scores (disk is async / best-effort).
_MEMORY_SCORES: dict[str, tuple[int, int]] = {}
_MEMORY_LOCK = threading.Lock()
_DISK_EXEC = ThreadPoolExecutor(max_workers=1, thread_name_prefix="af-ref-disk")
_POLL_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_POLL_CACHE_LOCK = threading.Lock()


def iso_now() -> str:
    return datetime.now(TZ_CN).isoformat(timespec="seconds")


def orient_af_goals_to_event(
    goals_home: Any,
    goals_away: Any,
    *,
    af_home: str,
    af_away: str,
    event_home: str,
    event_away: str,
) -> tuple[Any, Any]:
    """Map AF fixture-frame goals onto event (usually Polymarket) home/away."""
    if goals_home is None or goals_away is None:
        return goals_home, goals_away
    if not af_home or not event_home:
        return goals_home, goals_away
    return bridge.orient_scores(
        af_home,
        af_away,
        goals_home,
        goals_away,
        event_home,
        event_away,
    )

def af_min_interval_s() -> float:
    import os

    raw = os.getenv(_AF_MIN_INTERVAL_ENV)
    if raw is None or str(raw).strip() == "":
        return float(DEFAULT_AF_MIN_INTERVAL_S)
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return float(DEFAULT_AF_MIN_INTERVAL_S)


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


def ft_max_age_s(override: float | None = None) -> float:
    if override is not None:
        return max(0.0, float(override))
    import os

    raw = os.getenv(_FT_MAX_AGE_ENV)
    if raw is None or str(raw).strip() == "":
        return float(DEFAULT_FT_MAX_AGE_S)
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return float(DEFAULT_FT_MAX_AGE_S)


def event_age_seconds(ev: dict[str, Any], *, now: datetime | None = None) -> float | None:
    """Age of event ``ts`` in seconds (CN wall clock); None if unparsable."""
    ts = ev.get("ts")
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts))
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ_CN)
    n = now or datetime.now(TZ_CN)
    return max(0.0, (n - dt.astimezone(TZ_CN)).total_seconds())


def ft_event_is_stale(
    ev: dict[str, Any],
    *,
    max_age_s: float | None = None,
    now: datetime | None = None,
) -> tuple[bool, float | None]:
    """True when match_finished is older than max_age (0 = disable age check)."""
    age = event_age_seconds(ev, now=now)
    limit = ft_max_age_s(max_age_s)
    if limit <= 0 or age is None:
        return False, age
    return age > limit + 1e-9, age


def af_ft_score_matches(
    af: tuple[int, int],
    target: tuple[int, int],
) -> bool:
    """FT confirm requires exact regulation score agreement (no 'AF ahead' shortcut)."""
    return int(af[0]) == int(target[0]) and int(af[1]) == int(target[1])


def call_af_bridge_regulation_score(
    match_id: str,
    *,
    af: aflib.AFClient,
    cache: dict[str, Any],
    cache_only: bool = True,
    http_timeout: float | None = None,
) -> dict[str, Any]:
    """In-process AF fixture fulltime (regulation) score for FT gate."""
    return aflib.fetch_regulation_score_for_match_id(
        af,
        str(match_id),
        cache=cache,
        cache_only=cache_only,
        http_timeout=http_timeout,
    )


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
    blob = _af_error_blob(payload)
    return "rate" in blob or ("limit" in blob and "request" in blob)


def _af_error_blob(payload: dict[str, Any] | str | None) -> str:
    """Flatten AF / urllib error shapes into one lowercase string for matching."""
    if payload is None:
        return ""
    if isinstance(payload, str):
        return payload.lower()
    if not isinstance(payload, dict):
        return str(payload).lower()
    parts: list[str] = []
    for key in ("exception", "error", "errors", "message"):
        v = payload.get(key)
        if v is None or v == "" or v == {}:
            continue
        if isinstance(v, dict):
            inner = v.get("exception") or v.get("error") or v.get("message")
            parts.append(str(inner if inner is not None else v))
        else:
            parts.append(str(v))
    status = payload.get("http_status")
    if status is not None:
        parts.append(f"http_{status}")
    return " ".join(parts).lower() if parts else ""


def _is_transient_af_error(payload: dict[str, Any] | str | None) -> bool:
    """SSL / connect / read timeouts — retry same schedule slot (like 429)."""
    if payload is None:
        return False
    if isinstance(payload, dict):
        try:
            if int(payload.get("http_status") or 0) in {408, 425, 502, 503, 504}:
                return True
        except (TypeError, ValueError):
            pass
    blob = _af_error_blob(payload)
    if not blob:
        return False
    # Do not treat our own confirm-timeout label as a network blip.
    if blob.strip() in {"af_confirm_timeout", "timeout"}:
        return False
    needles = (
        "ssl",
        "unexpected_eof",
        "eof occurred",
        "timed out",
        "timeouterror",
        "read timeout",
        "connect timeout",
        "temporarily unavailable",
        "connection reset",
        "connection aborted",
        "connection refused",
        "broken pipe",
        "urlopen error",
        "remote disconnected",
        "network is unreachable",
        "name or service not known",
        "temporary failure",
    )
    return any(n in blob for n in needles)


def call_af_bridge_events(
    match_id: str,
    *,
    af: aflib.AFClient,
    cache: dict[str, Any],
    persist_burst: bool = False,
    persist_cache: bool = False,
    cache_only: bool = True,
    http_timeout: float | None = None,
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
        http_timeout=http_timeout,
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
    period_s: float = DEFAULT_PERIOD_S,
    late_after_s: float = DEFAULT_LATE_AFTER_S,
    late_period_s: float = DEFAULT_LATE_PERIOD_S,
) -> list[float]:
    """Absolute seconds (from goal) at which to call AF events.

    Default: ``first_delay`` (3s), every ``period_s`` (1s) while ``t < late_after``,
    then ``late_after`` and every ``late_period_s`` (2s) until ``timeout_s``
    inclusive. No polls after timeout.
    """
    timeout = max(0.0, float(timeout_s))
    first = max(0.0, float(first_delay_s))
    early = max(0.05, float(period_s))
    late_after = max(0.0, float(late_after_s))
    late = max(0.05, float(late_period_s))

    checks: list[float] = []
    t = first
    while t < late_after - 1e-9 and t <= timeout + 1e-9:
        checks.append(round(t, 3))
        t += early
    t = late_after
    while t <= timeout + 1e-9:
        if t + 1e-9 >= first:
            checks.append(round(t, 3))
        t += late
    # De-dupe / sort if late_after lands on an early tick.
    out: list[float] = []
    for c in checks:
        if not out or abs(out[-1] - c) > 1e-9:
            out.append(c)
    return out


def advance_schedule_index(
    check_at: list[float], check_i: int, elapsed: float
) -> int:
    """Collapse missed ticks to the latest overdue slot.

    If a prior HTTP call ran long, skip intermediate checkpoints so the next
    wait targets the following *future* cadence point instead of stampeding
    every skipped GET.
    """
    n = len(check_at)
    if check_i >= n:
        return check_i
    while check_i + 1 < n and check_at[check_i + 1] <= elapsed + 1e-9:
        check_i += 1
    return check_i


def schedule_label(
    *,
    first_delay_s: float = DEFAULT_FIRST_DELAY_S,
    period_s: float = DEFAULT_PERIOD_S,
    late_after_s: float = DEFAULT_LATE_AFTER_S,
    late_period_s: float = DEFAULT_LATE_PERIOD_S,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> str:
    return (
        f"{first_delay_s:g}s→every {period_s:g}s→{late_after_s:g}s"
        f"→every {late_period_s:g}s→{timeout_s:g}s"
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
        period_s: float = DEFAULT_PERIOD_S,
        late_after_s: float = DEFAULT_LATE_AFTER_S,
        late_period_s: float = DEFAULT_LATE_PERIOD_S,
    ) -> None:
        self.root = Path(root)
        self.poll_s = max(0.05, float(poll_s))
        self.timeout_s = max(0.05, float(timeout_s))
        self.poll_schedule = bool(poll_schedule)
        self.first_delay_s = max(0.0, float(first_delay_s))
        self.period_s = max(0.05, float(period_s))
        self.late_after_s = max(0.0, float(late_after_s))
        self.late_period_s = max(0.05, float(late_period_s))
        self.env_path = env_path
        self._events_fn = events_fn
        self._af_key: str | None = None
        self._tls = threading.local()
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
                period_s=self.period_s,
                late_after_s=self.late_after_s,
                late_period_s=self.late_period_s,
                timeout_s=self.timeout_s,
            )
        return f"fixed {self.poll_s}s"

    def _client(self) -> aflib.AFClient:
        # Per-worker client so optional QUOTE_AF_MIN_INTERVAL_S does not serialize
        # unrelated confirms onto one global 2s+ queue (that broke the schedule).
        af = getattr(self._tls, "af", None)
        if af is None:
            if self._af_key is None:
                self._af_key = aflib.load_af_key(self.env_path)
            af = aflib.AFClient(self._af_key, min_interval_s=af_min_interval_s())
            self._tls.af = af
        return af

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

    def cancel_key(self, event_key: str, reason: str = "cancelled") -> bool:
        """Abort a pending confirm (DQD reversal / superseded goal)."""
        with self._lock:
            fut = self._pending.pop(event_key, None)
            meta = self._meta.pop(event_key, None)
        if meta is None and fut is None:
            return False
        if meta is not None:
            holder = meta.get("abort_reason_holder")
            if isinstance(holder, dict):
                holder["reason"] = str(reason or "cancelled")
            abort = meta.get("abort")
            if isinstance(abort, threading.Event):
                abort.set()
        if fut is not None and not fut.done():
            fut.cancel()
        print(
            f"af-referee → cancelled key={event_key} reason={reason}",
            flush=True,
        )
        return True

    def cancel_match(self, match_id: str, reason: str = "cancelled") -> int:
        """Abort all pending confirms for a Dongqiudi match id."""
        mid = str(match_id or "")
        if not mid:
            return 0
        with self._lock:
            keys = [
                k
                for k, m in self._meta.items()
                if str(m.get("match_id") or "") == mid
            ]
        n = 0
        for k in keys:
            if self.cancel_key(k, reason=reason):
                n += 1
        return n

    def poll_once(
        self,
        match_id: str,
        *,
        persist_burst: bool = False,
        http_timeout: float | None = None,
        allow_coalesce: bool = True,
    ) -> dict[str, Any]:
        mid = str(match_id)
        if (
            allow_coalesce
            and not persist_burst
            and self._events_fn is None
        ):
            with _POLL_CACHE_LOCK:
                hit = _POLL_CACHE.get(mid)
            if hit is not None:
                at, payload = hit
                if (time.monotonic() - at) <= _POLL_COALESCE_S and isinstance(
                    payload, dict
                ):
                    return dict(payload)

        if self._events_fn is not None:
            try:
                return self._events_fn(mid, persist_burst=persist_burst)
            except TypeError:
                return self._events_fn(mid)
        out = call_af_bridge_events(
            mid,
            af=self._client(),
            cache=self._fixture_cache(),
            persist_burst=persist_burst,
            persist_cache=False,
            cache_only=True,
            http_timeout=http_timeout,
        )
        if allow_coalesce and not persist_burst and out.get("ok"):
            with _POLL_CACHE_LOCK:
                _POLL_CACHE[mid] = (time.monotonic(), dict(out))
        return out

    def poll_regulation_once(
        self,
        match_id: str,
        *,
        http_timeout: float | None = None,
    ) -> dict[str, Any]:
        """One AF ``/fixtures?id=`` poll for regulation (fulltime) score."""
        mid = str(match_id)
        if self._events_fn is not None:
            # Tests inject regulation payloads via events_fn (same hook).
            try:
                return self._events_fn(mid, persist_burst=False, kind="ft")
            except TypeError:
                return self._events_fn(mid)
        return call_af_bridge_regulation_score(
            mid,
            af=self._client(),
            cache=self._fixture_cache(),
            cache_only=True,
            http_timeout=http_timeout,
        )

    def await_score(
        self,
        match_id: str,
        target: tuple[int, int],
        *,
        baseline: tuple[int, int] | None = None,
        poll_s: float | None = None,
        timeout_s: float | None = None,
        wait_cache: bool = False,
        event_home: str = "",
        event_away: str = "",
        abort_event: threading.Event | None = None,
        abort_reason_holder: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Block until AF confirms target (or AF already covers it) / timeout.

        Prefer calling via ``submit`` + ``drain_done`` so watch is not blocked.
        On confirm: return immediately (memory score); disk persistence is async.

        Fixture mapping is cache-only (filled by AF sync/watch). If the DQD id
        is not in ``fixture_cache.json`` entries:
        - ``wait_cache=False`` (gate): skip immediately — no resolve, no timeout spin.
        - ``wait_cache=True`` (postcheck): keep polling until timeout so late
          sync/watch mappings can still confirm after a buy.

        Default poll schedule: 3s → every 1s until 60s → every 2s until timeout
        (override with ``poll_schedule=False`` + ``poll_s`` for fixed interval).

        ``event_home`` / ``event_away``: consumer-facing sides (PM labels on the
        bridge event). AF goals are remapped from fixture home/away into this
        frame before compare/apply.
        """
        poll = self.poll_s if poll_s is None else max(0.05, float(poll_s))
        timeout = self.timeout_s if timeout_s is None else max(0.05, float(timeout_s))
        th, ta = int(target[0]), int(target[1])
        # Prefer persisted AF truth as baseline when present.
        stored = get_confirmed_score(self.root, str(match_id))
        base = stored if stored is not None else baseline
        abort = abort_event if abort_event is not None else threading.Event()
        reason_holder = abort_reason_holder if abort_reason_holder is not None else {}

        mid = str(match_id)

        def _aborted(polls: int, elapsed_ms: int, last_goals: dict[str, Any]) -> dict[str, Any]:
            return {
                "ok": False,
                "confirmed": False,
                "match_id": mid,
                "target": {"home": th, "away": ta},
                "goals": last_goals,
                "baseline": {"home": base[0], "away": base[1]} if base else None,
                "polls": polls,
                "elapsed_ms": elapsed_ms,
                "timeout_s": timeout,
                "schedule": self._schedule_desc(),
                "error": "aborted",
                "reason": reason_holder.get("reason") or "cancelled",
                "via": "apifootball-bridge",
                "cache_only": True,
            }
        # Fresh disk cache from AF board sync/watch.
        self._reload_fixture_cache()
        if abort.is_set():
            return _aborted(0, 0, {"home": None, "away": None})
        if (
            not wait_cache
            and self._events_fn is None
            and self.cached_af_fixture_id(mid) is None
        ):
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
                "schedule": schedule_label(
                    first_delay_s=self.first_delay_s,
                    period_s=self.period_s,
                    late_after_s=self.late_after_s,
                    late_period_s=self.late_period_s,
                    timeout_s=timeout,
                )
                if self.poll_schedule
                else f"fixed {poll}s",
                "error": err,
                "via": "apifootball-bridge",
                "cache_only": True,
            }

        if self.poll_schedule:
            check_at = confirm_check_times(
                timeout,
                first_delay_s=self.first_delay_s,
                period_s=self.period_s,
                late_after_s=self.late_after_s,
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
        retry_hits = 0
        check_i = 0

        while check_i < len(check_at):
            if abort.is_set():
                return _aborted(
                    polls,
                    int((time.monotonic() - t0) * 1000),
                    last_goals,
                )
            elapsed = time.monotonic() - t0
            if elapsed >= timeout:
                break
            check_i = advance_schedule_index(check_at, check_i, elapsed)
            if check_i >= len(check_at):
                break
            target_at = check_at[check_i]
            # Sleep until this check's wall time (from confirm start).
            while True:
                if abort.is_set():
                    return _aborted(
                        polls,
                        int((time.monotonic() - t0) * 1000),
                        last_goals,
                    )
                elapsed = time.monotonic() - t0
                if elapsed >= timeout:
                    break
                remain = target_at - elapsed
                if remain <= 0:
                    break
                time.sleep(min(remain, 0.25))
            if abort.is_set():
                return _aborted(
                    polls,
                    int((time.monotonic() - t0) * 1000),
                    last_goals,
                )
            if (time.monotonic() - t0) >= timeout and (time.monotonic() - t0) < target_at:
                break

            polls += 1
            check_i += 1
            # Hard deadline: never start a request that cannot finish in-window.
            remain_budget = timeout - (time.monotonic() - t0)
            if remain_budget <= 0.05:
                break
            # Pick up late sync mappings between polls.
            self._reload_fixture_cache()
            try:
                last = self.poll_once(
                    mid,
                    persist_burst=False,
                    http_timeout=min(8.0, max(0.5, remain_budget)),
                )
            except Exception as e:  # noqa: BLE001
                last_error = str(e)
                last = {"ok": False, "error": str(e), "goals": last_goals}

            # Discard late responses that arrived after the confirm deadline.
            if (time.monotonic() - t0) >= timeout:
                last_error = last_error or "af_confirm_timeout"
                break

            elapsed_ms = int((time.monotonic() - t0) * 1000)

            miss = str(last.get("error") or "")
            if miss in _CACHE_MISS_ERRORS:
                last_error = miss
                last_goals = {
                    "home": (last.get("goals") or {}).get("home"),
                    "away": (last.get("goals") or {}).get("away"),
                }
                if not wait_cache:
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
                # postcheck: keep polling until timeout for late fixture mapping
                continue

            if _is_rate_limited(last):
                retry_hits += 1
                last_error = last.get("error") or last.get("errors") or "rate_limited"
                backoff = min(8.0, 0.5 * (2 ** min(retry_hits, 4)))
                # Retry same slot after backoff (do not burn next schedule slot).
                check_i -= 1
                if (time.monotonic() - t0) >= timeout:
                    break
                time.sleep(backoff)
                continue

            if _is_transient_af_error(last):
                # SSL / connect blips: short retry on same slot so the 90s window
                # still gets usable AF reads instead of advancing past them.
                err_blob = _af_error_blob(last) or "transient_af_error"
                last_error = err_blob
                check_i -= 1
                if (time.monotonic() - t0) >= timeout:
                    break
                time.sleep(0.25)
                continue

            retry_hits = 0  # reset after a clean (non-transient) attempt

            if last.get("ok"):
                goals = last.get("goals") or {}
                ent = aflib.cached_fixture_entry(self._fixture_cache(), mid) or {}
                gh_o, ga_o = orient_af_goals_to_event(
                    goals.get("home"),
                    goals.get("away"),
                    af_home=str(ent.get("af_home") or ""),
                    af_away=str(ent.get("af_away") or ""),
                    event_home=event_home,
                    event_away=event_away,
                )
                last_goals = {"home": gh_o, "away": ga_o}
                try:
                    gh, ga = gh_o, ga_o
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

        final_err: Any = last_error or "af_confirm_timeout"
        out_fail: dict[str, Any] = {
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
            "error": final_err,
            "via": "apifootball-bridge",
            "cache_only": True,
        }
        if _is_transient_af_error(final_err):
            out_fail["error"] = "af_confirm_timeout"
            out_fail["last_error"] = final_err
            out_fail["transient_network"] = True
        return out_fail

    def await_ft_score(
        self,
        match_id: str,
        target: tuple[int, int],
        *,
        poll_s: float | None = None,
        timeout_s: float | None = None,
        wait_cache: bool = True,
        event_home: str = "",
        event_away: str = "",
        abort_event: threading.Event | None = None,
        abort_reason_holder: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Block until AF regulation fulltime equals DQD FT target (exact).

        Uses ``score.fulltime`` only (ET/penalties ignored — Polymarket rule).
        When AF fixture is finished with a different regulation score → immediate
        mismatch (no trade). When not finished yet → keep polling until timeout.
        Remaps AF fixture-frame goals into ``event_home``/``event_away`` before compare.
        """
        poll = self.poll_s if poll_s is None else max(0.05, float(poll_s))
        timeout = self.timeout_s if timeout_s is None else max(0.05, float(timeout_s))
        th, ta = int(target[0]), int(target[1])
        mid = str(match_id)
        abort = abort_event if abort_event is not None else threading.Event()
        reason_holder = abort_reason_holder if abort_reason_holder is not None else {}

        def _aborted(polls: int, elapsed_ms: int) -> dict[str, Any]:
            return {
                "ok": False,
                "confirmed": False,
                "kind": "ft",
                "match_id": mid,
                "target": {"home": th, "away": ta},
                "goals": {"home": None, "away": None},
                "finished": False,
                "status_short": None,
                "polls": polls,
                "elapsed_ms": elapsed_ms,
                "timeout_s": timeout,
                "schedule": self._schedule_desc(),
                "error": "aborted",
                "reason": reason_holder.get("reason") or "cancelled",
                "via": "apifootball-bridge",
                "score_source": "score.fulltime",
                "cache_only": True,
            }

        self._reload_fixture_cache()
        if abort.is_set():
            return _aborted(0, 0)
        if (
            not wait_cache
            and self._events_fn is None
            and self.cached_af_fixture_id(mid) is None
        ):
            err = aflib.fixture_miss_error(self._fixture_cache(), mid)
            return {
                "ok": False,
                "confirmed": False,
                "kind": "ft",
                "match_id": mid,
                "target": {"home": th, "away": ta},
                "goals": {"home": None, "away": None},
                "finished": False,
                "status_short": None,
                "af_fixture_id": None,
                "polls": 0,
                "elapsed_ms": 0,
                "timeout_s": timeout,
                "schedule": self._schedule_desc(),
                "error": err,
                "via": "apifootball-bridge",
                "score_source": "score.fulltime",
                "cache_only": True,
            }

        if self.poll_schedule:
            check_at = confirm_check_times(
                timeout,
                first_delay_s=self.first_delay_s,
                period_s=self.period_s,
                late_after_s=self.late_after_s,
                late_period_s=self.late_period_s,
            )
        else:
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
        last_finished = False
        last_ready = False
        last_status: str | None = None
        retry_hits = 0
        check_i = 0

        while check_i < len(check_at):
            if abort.is_set():
                return _aborted(polls, int((time.monotonic() - t0) * 1000))
            elapsed = time.monotonic() - t0
            if elapsed >= timeout:
                break
            check_i = advance_schedule_index(check_at, check_i, elapsed)
            if check_i >= len(check_at):
                break
            target_at = check_at[check_i]
            while True:
                if abort.is_set():
                    return _aborted(polls, int((time.monotonic() - t0) * 1000))
                elapsed = time.monotonic() - t0
                if elapsed >= timeout:
                    break
                remain = target_at - elapsed
                if remain <= 0:
                    break
                time.sleep(min(remain, 0.25))
            if abort.is_set():
                return _aborted(polls, int((time.monotonic() - t0) * 1000))
            if (time.monotonic() - t0) >= timeout and (time.monotonic() - t0) < target_at:
                break

            polls += 1
            check_i += 1
            remain_budget = timeout - (time.monotonic() - t0)
            if remain_budget <= 0.05:
                break
            self._reload_fixture_cache()
            try:
                last = self.poll_regulation_once(
                    mid,
                    http_timeout=min(8.0, max(0.5, remain_budget)),
                )
            except Exception as e:  # noqa: BLE001
                last_error = str(e)
                last = {"ok": False, "error": str(e), "goals": last_goals}

            if (time.monotonic() - t0) >= timeout:
                last_error = last_error or "af_confirm_timeout"
                break

            elapsed_ms = int((time.monotonic() - t0) * 1000)
            miss = str(last.get("error") or "")
            if miss in _CACHE_MISS_ERRORS:
                last_error = miss
                if not wait_cache:
                    return {
                        "ok": False,
                        "confirmed": False,
                        "kind": "ft",
                        "match_id": mid,
                        "target": {"home": th, "away": ta},
                        "goals": last.get("goals") or last_goals,
                        "finished": bool(last.get("finished")),
                        "regulation_ready": bool(last.get("regulation_ready")),
                        "status_short": last.get("status_short"),
                        "af_fixture_id": None,
                        "polls": polls,
                        "elapsed_ms": elapsed_ms,
                        "timeout_s": timeout,
                        "schedule": self._schedule_desc(),
                        "error": miss,
                        "via": "apifootball-bridge",
                        "score_source": "score.fulltime",
                        "cache_only": True,
                    }
                continue

            if _is_rate_limited(last):
                retry_hits += 1
                last_error = last.get("error") or last.get("errors") or "rate_limited"
                backoff = min(8.0, 0.5 * (2 ** min(retry_hits, 4)))
                check_i -= 1
                if (time.monotonic() - t0) >= timeout:
                    break
                time.sleep(backoff)
                continue

            if _is_transient_af_error(last):
                last_error = _af_error_blob(last) or "transient_af_error"
                check_i -= 1
                if (time.monotonic() - t0) >= timeout:
                    break
                time.sleep(0.25)
                continue

            retry_hits = 0
            if last.get("ok"):
                goals = last.get("goals") or {}
                ent = aflib.cached_fixture_entry(self._fixture_cache(), mid) or {}
                gh_o, ga_o = orient_af_goals_to_event(
                    goals.get("home"),
                    goals.get("away"),
                    af_home=str(ent.get("af_home") or ""),
                    af_away=str(ent.get("af_away") or ""),
                    event_home=event_home,
                    event_away=event_away,
                )
                last_goals = {"home": gh_o, "away": ga_o}
                last_finished = bool(last.get("finished"))
                # Prefer explicit flag; fall back for test doubles that only set finished.
                last_ready = bool(
                    last.get("regulation_ready")
                    if "regulation_ready" in last
                    else last_finished
                )
                last_status = (
                    str(last.get("status_short") or "") or None
                )
                try:
                    gh, ga = gh_o, ga_o
                    if gh is not None and ga is not None and last_ready:
                        if af_ft_score_matches((int(gh), int(ga)), (th, ta)):
                            fid = last.get("af_fixture_id")
                            truth = (int(gh), int(ga))
                            set_confirmed_score_async(
                                self.root,
                                mid,
                                truth[0],
                                truth[1],
                                af_fixture_id=int(fid) if fid is not None else None,
                                source="af_fixture_fulltime",
                            )
                            return {
                                "ok": True,
                                "confirmed": True,
                                "kind": "ft",
                                "match_id": mid,
                                "target": {"home": th, "away": ta},
                                "goals": {"home": truth[0], "away": truth[1]},
                                "finished": last_finished,
                                "regulation_ready": True,
                                "status_short": last_status,
                                "af_fixture_id": fid,
                                "polls": polls,
                                "elapsed_ms": elapsed_ms,
                                "timeout_s": timeout,
                                "schedule": self._schedule_desc(),
                                "via": "apifootball-bridge",
                                "score_source": "score.fulltime",
                                "cache_only": True,
                            }
                        # Regulation decided but ≠ DQD → do not trade.
                        return {
                            "ok": False,
                            "confirmed": False,
                            "kind": "ft",
                            "match_id": mid,
                            "target": {"home": th, "away": ta},
                            "goals": {"home": int(gh), "away": int(ga)},
                            "finished": last_finished,
                            "regulation_ready": True,
                            "status_short": last_status,
                            "af_fixture_id": last.get("af_fixture_id"),
                            "polls": polls,
                            "elapsed_ms": elapsed_ms,
                            "timeout_s": timeout,
                            "schedule": self._schedule_desc(),
                            "error": (
                                f"af_ft_score_mismatch="
                                f"{int(gh)}-{int(ga)}!={th}-{ta}"
                            ),
                            "via": "apifootball-bridge",
                            "score_source": "score.fulltime",
                            "cache_only": True,
                        }
                except (TypeError, ValueError):
                    pass
            else:
                last_error = last.get("error") or last.get("errors") or last_error

            if (time.monotonic() - t0) >= timeout:
                break

        final_err: Any = last_error or "af_confirm_timeout"
        out_fail: dict[str, Any] = {
            "ok": False,
            "confirmed": False,
            "kind": "ft",
            "match_id": mid,
            "target": {"home": th, "away": ta},
            "goals": last_goals,
            "finished": last_finished,
            "regulation_ready": last_ready,
            "status_short": last_status,
            "af_fixture_id": last.get("af_fixture_id"),
            "polls": polls,
            "elapsed_ms": int((time.monotonic() - t0) * 1000),
            "timeout_s": timeout,
            "schedule": self._schedule_desc(),
            "error": final_err,
            "via": "apifootball-bridge",
            "score_source": "score.fulltime",
            "cache_only": True,
        }
        if _is_transient_af_error(final_err):
            out_fail["error"] = "af_confirm_timeout"
            out_fail["last_error"] = final_err
            out_fail["transient_network"] = True
        return out_fail

    def submit(
        self,
        event_key: str,
        ev: dict[str, Any],
        target: tuple[int, int],
        *,
        wait_cache: bool = False,
        kind: str = "goal",
    ) -> bool:
        """Enqueue non-blocking confirmation. Returns False if already pending.

        ``kind="ft"``: poll AF regulation fulltime (score.fulltime) for exact
        agreement with DQD FT — Polymarket ignores ET/penalties.
        ``wait_cache=True``: keep polling on fixture-cache miss until timeout.
        """
        mid = str(ev.get("match_id") or "")
        if not mid:
            return False
        job_kind = "ft" if str(kind or "").strip().lower() == "ft" else "goal"
        abort = threading.Event()
        reason_holder: dict[str, str] = {"reason": ""}
        with self._lock:
            if event_key in self._pending:
                return False
            stored = get_confirmed_score(self.root, mid)
            baseline = stored if stored is not None else baseline_score_from_event(ev)
            ev_home = str(ev.get("home") or "")
            ev_away = str(ev.get("away") or "")
            if job_kind == "ft":
                fut = self._exec.submit(
                    self.await_ft_score,
                    mid,
                    target,
                    wait_cache=bool(wait_cache),
                    event_home=ev_home,
                    event_away=ev_away,
                    abort_event=abort,
                    abort_reason_holder=reason_holder,
                )
            else:
                fut = self._exec.submit(
                    self.await_score,
                    mid,
                    target,
                    baseline=baseline,
                    wait_cache=bool(wait_cache),
                    event_home=ev_home,
                    event_away=ev_away,
                    abort_event=abort,
                    abort_reason_holder=reason_holder,
                )
            self._pending[event_key] = fut
            self._meta[event_key] = {
                "ev": dict(ev),
                "target": (int(target[0]), int(target[1])),
                "match_id": mid,
                "submitted_at": iso_now(),
                "wait_cache": bool(wait_cache),
                "kind": job_kind,
                "abort": abort,
                "abort_reason_holder": reason_holder,
            }
        print(
            f"af-referee → queued {mid} target={target[0]}-{target[1]} "
            f"key={event_key} kind={job_kind} (async · cache-only · "
            f"{self._schedule_desc()} · timeout {self.timeout_s}s"
            f"{' · wait_cache' if wait_cache else ''}"
            f"{' · regulation=fulltime' if job_kind == 'ft' else ''})",
            flush=True,
        )
        return True

    def submit_ft(
        self,
        event_key: str,
        ev: dict[str, Any],
        target: tuple[int, int],
        *,
        wait_cache: bool = True,
    ) -> bool:
        """FT gate helper — always uses regulation fulltime confirm."""
        return self.submit(
            event_key, ev, target, wait_cache=wait_cache, kind="ft"
        )

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
                    "kind": meta.get("kind") or "goal",
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
                    "kind": meta.get("kind") or "goal",
                    "gate": gate,
                }
            )
        return out
