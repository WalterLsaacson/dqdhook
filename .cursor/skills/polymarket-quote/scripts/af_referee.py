#!/usr/bin/env python3
"""AF referee gate: confirm Dongqiudi goal-ups via apifootball-bridge skill lib.

Fixture **mapping** is owned by apifootball-bridge ``sync``/``watch``
(``data/apifootball/fixture_cache.json``). The referee is **cache-only**: it
never resolves DQD→AF fixtures on the quote hot path. Cache miss / unresolved
→ skip the goal immediately (no timeout spin).

Score confirmation polls ``fetch_events_for_match_id(..., cache_only=True)`` on
a tiered schedule: **5s → every 2s until 60s → every 5s until 90s** (override
with ``--af-poll`` for a fixed interval). Confirmations run on a thread pool so
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
DEFAULT_TIMEOUT_S = 90.0
DEFAULT_WORKERS = 4

# Confirm schedule (production default):
#   first look at 5s → every 2s until 60s → every 5s until timeout (90s).
DEFAULT_FIRST_DELAY_S = 5.0
DEFAULT_PERIOD_S = 2.0  # early period (alias for early_period_s)
DEFAULT_LATE_AFTER_S = 60.0
DEFAULT_LATE_PERIOD_S = 5.0
# Shared AFClient spacing across referee workers (avoid stampede / 429).
DEFAULT_AF_MIN_INTERVAL_S = 0.35
# Soft coalesce: reuse last good poll for same DQD id within this window.
_POLL_COALESCE_S = 1.0

_AF_SCRIPTS = Path(__file__).resolve().parents[2] / "apifootball-bridge" / "scripts"
_af_sp = str(_AF_SCRIPTS)
if _af_sp not in sys.path:
    sys.path.insert(0, _af_sp)

import af_bridge_lib as aflib  # noqa: E402

# Process-local confirmed scores (disk is async / best-effort).
_MEMORY_SCORES: dict[str, tuple[int, int]] = {}
_MEMORY_LOCK = threading.Lock()
_DISK_EXEC = ThreadPoolExecutor(max_workers=1, thread_name_prefix="af-ref-disk")
_POLL_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_POLL_CACHE_LOCK = threading.Lock()


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

    Default: ``first_delay`` (5s), every ``period_s`` (2s) while ``t < late_after``,
    then ``late_after`` and every ``late_period_s`` (5s) until ``timeout_s``
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
        self.timeout_s = max(1.0, float(timeout_s))
        self.poll_schedule = bool(poll_schedule)
        self.first_delay_s = max(0.0, float(first_delay_s))
        self.period_s = max(0.05, float(period_s))
        self.late_after_s = max(0.0, float(late_after_s))
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
                period_s=self.period_s,
                late_after_s=self.late_after_s,
                late_period_s=self.late_period_s,
                timeout_s=self.timeout_s,
            )
        return f"fixed {self.poll_s}s"

    def _client(self) -> aflib.AFClient:
        if self._af is None:
            key = aflib.load_af_key(self.env_path)
            # Shared spacing across workers; 429 still backs off in await_score.
            self._af = aflib.AFClient(key, min_interval_s=DEFAULT_AF_MIN_INTERVAL_S)
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
        wait_cache: bool = False,
    ) -> dict[str, Any]:
        """Block until AF confirms target (or AF already covers it) / timeout.

        Prefer calling via ``submit`` + ``drain_done`` so watch is not blocked.
        On confirm: return immediately (memory score); disk/burst async.

        Fixture mapping is cache-only (filled by AF sync/watch). If the DQD id
        is not in ``fixture_cache.json`` entries:
        - ``wait_cache=False`` (gate): skip immediately — no resolve, no timeout spin.
        - ``wait_cache=True`` (postcheck): keep polling until timeout so late
          sync/watch mappings can still confirm after a buy.

        Default poll schedule: 5s → every 2s until 60s → every 5s until timeout
        (override with ``poll_schedule=False`` + ``poll_s`` for fixed interval).
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

    def submit(
        self,
        event_key: str,
        ev: dict[str, Any],
        target: tuple[int, int],
        *,
        wait_cache: bool = False,
    ) -> bool:
        """Enqueue non-blocking confirmation. Returns False if already pending.

        ``wait_cache=True`` (postcheck): keep polling on fixture-cache miss until
        timeout so late AF sync mappings can still confirm after a buy.
        """
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
                wait_cache=bool(wait_cache),
            )
            self._pending[event_key] = fut
            self._meta[event_key] = {
                "ev": dict(ev),
                "target": (int(target[0]), int(target[1])),
                "match_id": mid,
                "submitted_at": iso_now(),
                "wait_cache": bool(wait_cache),
            }
        print(
            f"af-referee → queued {mid} target={target[0]}-{target[1]} "
            f"key={event_key} (async · cache-only · {self._schedule_desc()} · "
            f"timeout {self.timeout_s}s"
            f"{' · wait_cache' if wait_cache else ''})",
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
