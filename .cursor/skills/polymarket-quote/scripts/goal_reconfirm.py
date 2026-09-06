"""Post-goal DOM+AF reconfirm: live $1 after the first pitch-gate dry-run.

After a pitch-gate knife that posted (dry-run counts) and still has remaining
edge — WIN tokens that would *not* stay WIN if this goal is voided — wait
``QUOTE_RECONFIRM_DELAY_S`` (default 60s), then:

1. Sample DOM 6 frames × 3s; **every** frame must be ``play_state==in_play``.
2. One AF live poll vs this goal's score (``ok && score_match``).
3. Pass → live ``QUOTE_RECONFIRM_USDC`` (default $1) ``buy_win``.
4. Fail → wait another 60s, up to 3 attempts.
5. ``match_finished`` / DQD reversal block the match; a newer goal sets a ts cutoff
   so a delayed CLOB-worker result cannot schedule the old knife. Cancel also
   drops already-passed jobs. Orientation (``sides_swapped`` / dqd names) is
   carried on ``reconfirm_ev`` through the worker.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

DEFAULT_RECONFIRM_DELAY_S = 60.0
DEFAULT_RECONFIRM_USDC = 1.0
DEFAULT_RECONFIRM_MAX_LATE_S = 900.0
DEFAULT_MAX_ATTEMPTS = 3
DOM_FRAMES = 6
DOM_INTERVAL_S = 3.0
POSTED_STATUSES = frozenset({"posted", "dry_run"})

_sample_dom_fn: Callable[..., str] | None = None
_poll_af_fn: Callable[..., dict[str, Any]] | None = None
_sleep_fn: Callable[[float], None] = time.sleep
_sync_for_tests = False


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return float(default)
    try:
        return float(raw)
    except (TypeError, ValueError):
        return float(default)


def reconfirm_delay_s() -> float:
    return max(0.0, _env_float("QUOTE_RECONFIRM_DELAY_S", DEFAULT_RECONFIRM_DELAY_S))


def reconfirm_usdc() -> float:
    raw = os.getenv("QUOTE_RECONFIRM_USDC")
    if raw is None or str(raw).strip() == "":
        return float(DEFAULT_RECONFIRM_USDC)
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return float(DEFAULT_RECONFIRM_USDC)


def reconfirm_max_late_s() -> float:
    return max(0.0, _env_float("QUOTE_RECONFIRM_MAX_LATE_S", DEFAULT_RECONFIRM_MAX_LATE_S))


def reconfirm_enabled() -> bool:
    return reconfirm_usdc() > 1e-12


def reconfirm_event_key(source_event_key: str) -> str:
    src = str(source_event_key or "").strip()
    if src.startswith("reconfirm|"):
        return src
    return f"reconfirm|{src}"


def set_hooks_for_tests(
    *,
    sample_dom_fn: Callable[..., str] | None = None,
    poll_af_fn: Callable[..., dict[str, Any]] | None = None,
    sleep_fn: Callable[[float], None] | None = None,
    sync: bool = True,
) -> None:
    global _sample_dom_fn, _poll_af_fn, _sleep_fn, _sync_for_tests
    _sample_dom_fn = sample_dom_fn
    _poll_af_fn = poll_af_fn
    _sleep_fn = sleep_fn if sleep_fn is not None else time.sleep
    _sync_for_tests = bool(sync)


def reset_hooks_for_tests() -> None:
    set_hooks_for_tests(sample_dom_fn=None, poll_af_fn=None, sleep_fn=time.sleep, sync=False)


def _pending_path(root: Path) -> Path:
    return Path(root) / "data" / "pm-quote" / "reconfirm_pending.json"


_SLIM_KEYS = (
    "type",
    "ts",
    "match_id",
    "home",
    "away",
    "home_score",
    "away_score",
    "home_half",
    "away_half",
    "league",
    "kickoff_beijing",
    "official_clock",
    "sides_swapped",
    "dqd_home",
    "dqd_away",
    "polymarket",
    "prev",
    "curr",
    "is_goal",
)


def slim_event(ev: dict[str, Any] | None) -> dict[str, Any]:
    """Keep orientation fields (sides_swapped / dqd names) across the CLOB worker."""
    out: dict[str, Any] = {}
    if not isinstance(ev, dict):
        return out
    for k in _SLIM_KEYS:
        if k in ev:
            out[k] = ev[k]
    return out


# Back-compat alias for in-module callers.
_slim_ev = slim_event


def remaining_edge_quotes(quotes: list[Any] | None) -> list[dict[str, Any]]:
    """WIN tokens that would flip if this goal is voided (not locked-if-void)."""
    out: list[dict[str, Any]] = []
    for q in quotes or []:
        if not isinstance(q, dict):
            continue
        if str(q.get("settlement") or "").upper() != "WIN":
            continue
        if q.get("win_if_goal_void") is True:
            continue
        out.append(q)
    return out


def _attempt_status(quote: dict[str, Any]) -> str:
    att = quote.get("trade_attempt") if isinstance(quote.get("trade_attempt"), dict) else {}
    return str(att.get("status") or "")


def _attempt_trade(quote: dict[str, Any]) -> str:
    att = quote.get("trade_attempt") if isinstance(quote.get("trade_attempt"), dict) else {}
    return str(att.get("trade") or quote.get("trade") or "")


def posted_buy_win(quotes: list[Any] | None) -> bool:
    for q in quotes or []:
        if not isinstance(q, dict):
            continue
        if _attempt_trade(q) != "buy_win":
            continue
        if _attempt_status(q) in POSTED_STATUSES:
            return True
    return False


def bundle_should_schedule(bundle: dict[str, Any] | None) -> bool:
    if not isinstance(bundle, dict):
        return False
    if str(bundle.get("mode") or "") in {"t10_scan", "reconfirm"}:
        return False
    tc = bundle.get("trade_context") if isinstance(bundle.get("trade_context"), dict) else {}
    if tc.get("reconfirm") or tc.get("t10"):
        return False
    pg = bundle.get("pitch_gate") if isinstance(bundle.get("pitch_gate"), dict) else None
    if not pg and str(bundle.get("mode") or "") != "pitch_gate_confirmed":
        return False
    quotes = bundle.get("quotes") if isinstance(bundle.get("quotes"), list) else []
    if not posted_buy_win(quotes):
        return False
    return bool(remaining_edge_quotes(quotes))


def ev_from_bundle(bundle: dict[str, Any]) -> dict[str, Any]:
    attached = bundle.get("reconfirm_ev") if isinstance(bundle, dict) else None
    if isinstance(attached, dict) and attached.get("match_id"):
        return slim_event(attached)
    ev: dict[str, Any] = {
        "type": "score_change",
        "match_id": bundle.get("match_id"),
        "home": bundle.get("home"),
        "away": bundle.get("away"),
        "home_score": bundle.get("home_score"),
        "away_score": bundle.get("away_score"),
        "prev": bundle.get("prev_score"),
        "curr": {"home": bundle.get("home_score"), "away": bundle.get("away_score")},
        "is_goal": True,
        "polymarket": bundle.get("polymarket") or {},
    }
    for k in ("ts", "sides_swapped", "dqd_home", "dqd_away", "home_half", "away_half"):
        if k in bundle:
            ev[k] = bundle[k]
    return ev


def resolve_schedule_ev(
    bundle: dict[str, Any] | None,
    ev: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if isinstance(ev, dict) and (ev.get("match_id") or ev.get("ts")):
        return slim_event(ev) or dict(ev)
    if isinstance(bundle, dict):
        return ev_from_bundle(bundle)
    return {}


def build_reconfirm_work_event(job: dict[str, Any]) -> dict[str, Any] | None:
    ev = job.get("ev") if isinstance(job.get("ev"), dict) else {}
    mid = str(job.get("match_id") or ev.get("match_id") or "").strip()
    src = str(job.get("source_event_key") or "").strip()
    if not mid or not src:
        return None
    work = dict(ev)
    work["type"] = "score_change"
    work["match_id"] = mid
    rec_key = str(job.get("reconfirm_event_key") or reconfirm_event_key(src))
    work["_trade_event_key"] = rec_key
    work["_trade_context"] = {
        "pitch_gate": True,
        "reconfirm": True,
        "base_event_key": rec_key,
        "source_event_key": src,
        "attempt": int(job.get("attempt") or 1),
    }
    return work


_BLOCK_REASONS = frozenset({"match_finished", "dqd_reversal", "ft", "ft_hard_stop"})
_MAX_DEAD_SRC = 2000


def _ts_lt(left: str, right: str) -> bool:
    """True when left is strictly earlier than right (ISO event ts)."""
    a = str(left or "").strip()
    b = str(right or "").strip()
    if not a or not b:
        return False
    try:
        from score_reversal import parse_iso

        da, db = parse_iso(a), parse_iso(b)
    except Exception:  # noqa: BLE001
        da = db = None
    if da is not None and db is not None:
        return da < db
    return a < b


def _play_state_from_judge(judged: dict[str, Any] | None) -> str:
    if not isinstance(judged, dict):
        return ""
    return str(judged.get("play_state") or "")


def sample_dom_play_state(
    root: Path,
    ev: dict[str, Any],
    *,
    sample_i: int,
    prev_clock: str | None,
    reader_holder: dict[str, Any],
) -> tuple[str, str | None]:
    """Return (play_state, new_prev_clock). Test hook bypasses Chromium."""
    if _sample_dom_fn is not None:
        state = str(_sample_dom_fn(sample_i=sample_i, prev_clock=prev_clock, ev=ev) or "")
        return state, prev_clock
    from dqd_stream_observe import get_active_observer
    from pitch_gate import PitchGateCoordinator, get_coordinator

    observer = get_active_observer()
    if observer is None:
        return "", prev_clock
    coord = get_coordinator(root)
    reader = reader_holder.get("reader")
    info = reader_holder.get("info") or {}
    if reader is None:
        session = SimpleNamespace(
            match_id=str(ev.get("match_id") or ""),
            ev=ev,
            event_key="",
            observe_only=False,
            require_score=False,
        )
        reader, _err, info = coord._open_dom_reader(session, observer)  # noqa: SLF001
        if reader is None:
            return "", prev_clock
        reader_holder["reader"] = reader
        reader_holder["info"] = info
        prev_clock = PitchGateCoordinator._baseline_clock(reader) or prev_clock
    session = SimpleNamespace(
        match_id=str(ev.get("match_id") or ""),
        ev=ev,
        event_key=str(ev.get("_trade_event_key") or ""),
        observe_only=False,
        require_score=False,
    )
    _row, judged = coord._sample_dom(  # noqa: SLF001
        session,
        reader,
        info,
        sample_i=sample_i,
        elapsed_s=float(sample_i) * DOM_INTERVAL_S,
        prev_clock=prev_clock,
    )
    clock = str((judged or {}).get("dom_clock") or "") or prev_clock
    return _play_state_from_judge(judged), clock


def poll_af_score_match(root: Path, ev: dict[str, Any]) -> dict[str, Any]:
    """One AF live poll vs this goal. Test hook bypasses HTTP."""
    if _poll_af_fn is not None:
        row = _poll_af_fn(ev=ev) or {}
        return dict(row)
    mid = str(ev.get("match_id") or "").strip()
    if not mid:
        return {"ok": False, "score_match": False, "error": "no_match_id"}
    try:
        from af_observe import get_active_observer as get_af

        obs = get_af()
        if obs is not None:
            row = obs.sample_once(ev, event_key=str(ev.get("_trade_event_key") or ""), sample_i=0, elapsed_s=0.0)
            return {
                "ok": bool(row.get("ok")),
                "score_match": bool(row.get("score_match")),
                "af_score": row.get("af_score"),
                "error": row.get("error"),
            }
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "score_match": False, "error": str(e)[:160]}
    try:
        from af_referee import get_ft_referee, orient_af_goals_to_event
        from score_events import target_score_from_event

        referee = get_ft_referee(root)
        last = referee.poll_t10_once(mid)
        target = target_score_from_event(ev)
        goals = last.get("goals") if isinstance(last.get("goals"), dict) else {}
        ent = last.get("cache_entry") if isinstance(last.get("cache_entry"), dict) else {}
        gh, ga = orient_af_goals_to_event(
            goals.get("home"),
            goals.get("away"),
            af_home=str((ent or {}).get("af_home") or ""),
            af_away=str((ent or {}).get("af_away") or ""),
            event_home=str(ev.get("home") or ""),
            event_away=str(ev.get("away") or ""),
        )
        ok = bool(last.get("ok")) and gh is not None and ga is not None
        score_match = False
        if ok and target is not None:
            score_match = int(gh) == int(target[0]) and int(ga) == int(target[1])
        return {
            "ok": ok,
            "score_match": score_match,
            "af_score": f"{gh}-{ga}" if gh is not None and ga is not None else None,
            "error": last.get("error"),
        }
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "score_match": False, "error": str(e)[:160]}


def run_confirm_attempt(
    root: Path,
    ev: dict[str, Any],
    *,
    cancel: threading.Event | None = None,
    frames: int = DOM_FRAMES,
    interval_s: float = DOM_INTERVAL_S,
) -> dict[str, Any]:
    """DOM 6×3s all in_play, then one AF live poll. Does not block on cancel."""
    abort = cancel if cancel is not None else threading.Event()
    sleep = _sleep_fn
    reader_holder: dict[str, Any] = {}
    prev_clock: str | None = None
    play_states: list[str] = []
    try:
        for i in range(int(frames)):
            if abort.is_set():
                return {"ok": False, "reason": "canceled", "play_states": play_states}
            state, prev_clock = sample_dom_play_state(
                root, ev, sample_i=i, prev_clock=prev_clock, reader_holder=reader_holder
            )
            play_states.append(state)
            if state != "in_play":
                return {
                    "ok": False,
                    "reason": f"dom_not_in_play frame={i} play_state={state or 'empty'}",
                    "play_states": play_states,
                    "failed_frame": i,
                }
            if i + 1 < int(frames) and interval_s > 0:
                # Sleep in slices so cancel_match can abort mid-wait.
                deadline = time.monotonic() + float(interval_s)
                while time.monotonic() < deadline:
                    if abort.is_set():
                        return {"ok": False, "reason": "canceled", "play_states": play_states}
                    remain = deadline - time.monotonic()
                    sleep(min(0.25, max(0.0, remain)) if sleep is time.sleep else remain)
                    if sleep is not time.sleep:
                        break
        if abort.is_set():
            return {"ok": False, "reason": "canceled", "play_states": play_states}
        af = poll_af_score_match(root, ev)
        if abort.is_set():
            return {"ok": False, "reason": "canceled", "play_states": play_states, "af": af}
        if af.get("ok") and af.get("score_match"):
            return {"ok": True, "reason": "confirmed", "play_states": play_states, "af": af}
        return {
            "ok": False,
            "reason": "af_mismatch" if af.get("ok") else (str(af.get("error") or "af_fail")),
            "play_states": play_states,
            "af": af,
        }
    finally:
        reader = reader_holder.get("reader")
        if reader is not None:
            try:
                reader.close()
            except Exception:  # noqa: BLE001
                pass


class ReconfirmScheduler:
    """Persist pending reconfirm jobs; confirm threads are in-process only."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self._lock = threading.Lock()
        self._jobs: dict[str, dict[str, Any]] = {}
        self._inflight: dict[str, threading.Event] = {}
        self._inflight_mid: dict[str, str] = {}
        self._done: list[dict[str, Any]] = []
        self._blocked: set[str] = set()
        self._cutoff_ts: dict[str, str] = {}
        self._dead_src: set[str] = set()
        self._load()

    def _load(self) -> None:
        try:
            import quote_lib as lib

            raw = lib.load_json(_pending_path(self.root), {}) or {}
        except Exception:  # noqa: BLE001
            raw = {}
        if isinstance(raw, list):
            jobs = raw
            raw = {}
        elif isinstance(raw, dict):
            jobs = raw.get("jobs") if isinstance(raw.get("jobs"), list) else []
        else:
            jobs = []
            raw = {}
        out: dict[str, dict[str, Any]] = {}
        for row in jobs:
            if not isinstance(row, dict):
                continue
            src = str(row.get("source_event_key") or "").strip()
            if src:
                out[src] = row
        self._jobs = out
        blocked = raw.get("blocked_matches") if isinstance(raw.get("blocked_matches"), list) else []
        self._blocked = {str(x).strip() for x in blocked if str(x).strip()}
        cutoff = raw.get("cutoff_ts") if isinstance(raw.get("cutoff_ts"), dict) else {}
        self._cutoff_ts = {
            str(k).strip(): str(v)
            for k, v in cutoff.items()
            if str(k).strip() and str(v).strip()
        }
        dead = raw.get("dead_src") if isinstance(raw.get("dead_src"), list) else []
        self._dead_src = {str(x).strip() for x in dead if str(x).strip()}

    def _save_locked(self) -> None:
        try:
            import quote_lib as lib

            dead = list(self._dead_src)
            if len(dead) > _MAX_DEAD_SRC:
                dead = dead[-_MAX_DEAD_SRC:]
                self._dead_src = set(dead)
            payload = {
                "updated_at": lib.now_cn_iso(),
                "jobs": list(self._jobs.values()),
                "blocked_matches": sorted(self._blocked),
                "cutoff_ts": dict(self._cutoff_ts),
                "dead_src": dead,
            }
            lib.write_json(_pending_path(self.root), payload)
        except Exception:  # noqa: BLE001
            pass

    def _job_event_ts(self, ev: dict[str, Any] | None, extra_ts: str = "") -> str:
        if extra_ts:
            return str(extra_ts)
        if isinstance(ev, dict):
            return str(ev.get("ts") or "")
        return ""

    def _blocked_locked(
        self,
        mid: str,
        src: str,
        ev: dict[str, Any] | None = None,
        *,
        event_ts: str = "",
    ) -> bool:
        if mid and mid in self._blocked:
            return True
        if src and src in self._dead_src:
            return True
        cutoff = str(self._cutoff_ts.get(mid) or "")
        if cutoff:
            ts = self._job_event_ts(ev, event_ts)
            if not ts or _ts_lt(ts, cutoff):
                return True
        return False

    def accept_job(self, job: dict[str, Any] | None) -> bool:
        if not isinstance(job, dict):
            return False
        src = str(job.get("source_event_key") or "")
        mid = str(job.get("match_id") or "")
        ev = job.get("ev") if isinstance(job.get("ev"), dict) else {}
        extra_ts = str(job.get("event_ts") or "")
        with self._lock:
            return not self._blocked_locked(mid, src, ev, event_ts=extra_ts)

    def schedule(
        self,
        ev: dict[str, Any],
        *,
        event_key: str,
        now: float | None = None,
        attempt: int = 1,
        delay_s: float | None = None,
    ) -> bool:
        if not reconfirm_enabled():
            return False
        src = str(event_key or "").strip()
        mid = str(ev.get("match_id") or "").strip()
        if not src or not mid:
            return False
        if src.startswith("reconfirm|") or src.startswith("t10|"):
            return False
        wait = reconfirm_delay_s() if delay_s is None else max(0.0, float(delay_s))
        with self._lock:
            if src in self._jobs or src in self._inflight:
                return False
            if self._blocked_locked(mid, src, ev):
                return False
            due = float(now if now is not None else time.time()) + wait
            slim = _slim_ev(ev)
            self._jobs[src] = {
                "source_event_key": src,
                "reconfirm_event_key": reconfirm_event_key(src),
                "match_id": mid,
                "due_ts": due,
                "attempt": int(attempt),
                "event_ts": str(slim.get("ts") or ev.get("ts") or ""),
                "ev": slim,
            }
            self._save_locked()
        print(
            f"reconfirm → SCHEDULE match_id={mid} key={src} "
            f"attempt={int(attempt)} delay={wait:g}s",
            flush=True,
        )
        return True

    def maybe_schedule_from_bundle(
        self,
        bundle: dict[str, Any],
        *,
        ev: dict[str, Any] | None = None,
    ) -> bool:
        if not bundle_should_schedule(bundle):
            return False
        key = str(bundle.get("event_key") or "").strip()
        work = resolve_schedule_ev(bundle, ev)
        return self.schedule(work, event_key=key)

    def cancel_match(
        self,
        match_id: str,
        *,
        reason: str = "canceled",
        event_ts: str | None = None,
    ) -> int:
        mid = str(match_id or "").strip()
        if not mid:
            return 0
        n = 0
        why = str(reason or "canceled")
        ts = str(event_ts or "").strip()
        with self._lock:
            if why in _BLOCK_REASONS:
                self._blocked.add(mid)
            if ts:
                prev = str(self._cutoff_ts.get(mid) or "")
                if not prev or _ts_lt(prev, ts) or prev == ts:
                    self._cutoff_ts[mid] = ts
            drop = [k for k, j in self._jobs.items() if str(j.get("match_id") or "") == mid]
            for k in drop:
                self._jobs.pop(k, None)
                self._dead_src.add(k)
            n += len(drop)
            for src, cancel in list(self._inflight.items()):
                if str(self._inflight_mid.get(src) or "") == mid:
                    cancel.set()
                    self._dead_src.add(src)
                    n += 1
            kept: list[dict[str, Any]] = []
            for row in self._done:
                j = row.get("job") if isinstance(row.get("job"), dict) else {}
                row_mid = str(row.get("match_id") or j.get("match_id") or "")
                if row_mid == mid:
                    src = str(row.get("source_event_key") or j.get("source_event_key") or "")
                    if src:
                        self._dead_src.add(src)
                    continue
                kept.append(row)
            self._done = kept
            self._save_locked()
        if n or why in _BLOCK_REASONS or ts:
            print(
                f"reconfirm → CANCELED match_id={mid} n={n} reason={why}",
                flush=True,
            )
        return n

    def pop_due(self, *, now: float | None = None) -> list[dict[str, Any]]:
        ts = float(now if now is not None else time.time())
        late = reconfirm_max_late_s()
        due_rows: list[dict[str, Any]] = []
        stale_keys: list[str] = []
        with self._lock:
            for src, job in list(self._jobs.items()):
                mid = str(job.get("match_id") or "")
                ev = job.get("ev") if isinstance(job.get("ev"), dict) else {}
                extra_ts = str(job.get("event_ts") or "")
                if self._blocked_locked(mid, src, ev, event_ts=extra_ts):
                    stale_keys.append(src)
                    continue
                try:
                    due = float(job.get("due_ts") or 0)
                except (TypeError, ValueError):
                    stale_keys.append(src)
                    continue
                if due > ts:
                    continue
                if late > 0 and ts - due > late:
                    stale_keys.append(src)
                    continue
                due_rows.append(dict(job))
                self._jobs.pop(src, None)
            for src in stale_keys:
                self._jobs.pop(src, None)
            if due_rows or stale_keys:
                self._save_locked()
        if stale_keys:
            print(
                f"reconfirm → SKIP stale jobs n={len(stale_keys)} "
                f"(later than {late:g}s after due)",
                flush=True,
            )
        return due_rows

    def drain_done(self) -> list[dict[str, Any]]:
        with self._lock:
            out = list(self._done)
            self._done.clear()
            return out

    def _push_done(self, row: dict[str, Any]) -> None:
        with self._lock:
            self._done.append(row)

    def kick_due(self, *, now: float | None = None) -> int:
        due = self.pop_due(now=now)
        n = 0
        for job in due:
            src = str(job.get("source_event_key") or "")
            mid = str(job.get("match_id") or "")
            if not src:
                continue
            cancel = threading.Event()
            with self._lock:
                ev = job.get("ev") if isinstance(job.get("ev"), dict) else {}
                extra_ts = str(job.get("event_ts") or "")
                if self._blocked_locked(mid, src, ev, event_ts=extra_ts):
                    self._dead_src.add(src)
                    continue
                self._inflight[src] = cancel
                self._inflight_mid[src] = mid
            if _sync_for_tests:
                self._run_job(job, cancel)
            else:
                threading.Thread(
                    target=self._run_job,
                    args=(job, cancel),
                    name=f"reconfirm-{mid}",
                    daemon=True,
                ).start()
            n += 1
        return n

    def _run_job(self, job: dict[str, Any], cancel: threading.Event) -> None:
        src = str(job.get("source_event_key") or "")
        mid = str(job.get("match_id") or "")
        attempt = int(job.get("attempt") or 1)
        ev = job.get("ev") if isinstance(job.get("ev"), dict) else {}
        try:
            result = run_confirm_attempt(self.root, ev, cancel=cancel)
        except Exception as e:  # noqa: BLE001
            result = {"ok": False, "reason": str(e)[:160]}
        extra_ts = str(job.get("event_ts") or "")
        with self._lock:
            self._inflight.pop(src, None)
            self._inflight_mid.pop(src, None)
            suppressed = (
                cancel.is_set()
                or self._blocked_locked(mid, src, ev, event_ts=extra_ts)
                or str(result.get("reason") or "") == "canceled"
            )
            if suppressed:
                self._dead_src.add(src)
                self._done.append(
                    {
                        "status": "canceled",
                        "job": job,
                        "result": result,
                        "match_id": mid,
                        "source_event_key": src,
                        "attempt": attempt,
                    }
                )
                self._save_locked()
                return
            if result.get("ok"):
                self._done.append(
                    {
                        "status": "pass",
                        "job": job,
                        "result": result,
                        "match_id": mid,
                        "source_event_key": src,
                        "attempt": attempt,
                    }
                )
                return
        if attempt < DEFAULT_MAX_ATTEMPTS:
            print(
                f"reconfirm → RETRY match_id={mid} key={src} "
                f"attempt={attempt} reason={result.get('reason')}",
                flush=True,
            )
            # schedule() re-checks block / cutoff / dead_src.
            retried = self.schedule(ev, event_key=src, attempt=attempt + 1)
            self._push_done(
                {
                    "status": "retry" if retried else "canceled",
                    "job": job,
                    "result": result,
                    "match_id": mid,
                    "source_event_key": src,
                    "attempt": attempt,
                }
            )
            return
        print(
            f"reconfirm → DROP match_id={mid} key={src} "
            f"attempt={attempt} reason={result.get('reason')}",
            flush=True,
        )
        self._push_done(
            {
                "status": "drop",
                "job": job,
                "result": result,
                "match_id": mid,
                "source_event_key": src,
                "attempt": attempt,
            }
        )


_active: ReconfirmScheduler | None = None
_active_lock = threading.Lock()


def get_scheduler(root: Path) -> ReconfirmScheduler:
    global _active
    rt = Path(root)
    with _active_lock:
        if _active is None or _active.root != rt:
            _active = ReconfirmScheduler(rt)
        return _active


def reset_scheduler_for_tests(root: Path | None = None) -> None:
    global _active
    with _active_lock:
        _active = None
    reset_hooks_for_tests()
    if root is not None:
        pending = _pending_path(Path(root))
        try:
            pending.unlink()
        except FileNotFoundError:
            pass
