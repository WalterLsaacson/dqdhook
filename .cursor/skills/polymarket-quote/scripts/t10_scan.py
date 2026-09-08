"""Post-goal +10min book rescan.

After a paired DQD goal-up, wait ``QUOTE_T10_DELAY_S`` (default 600s) and poll
API-Football **live** goals. Quote only when that live tally **exactly matches
the triggering score-change** (the goal that scheduled this job). A later goal
or other disagreement skips; AF still behind the trigger keeps polling.
A DQD reversal cancels the **undone** goal's pending / in-flight T+10 (stem
match, ``ts`` ≤ reverse ``ts``) and records that stem so a job already
``pop_due``'d cannot submit. Earlier standing goals on the same match keep
theirs. Dongqiudi ``prev_scores`` is only a skeleton (sides / halves); no T+10
order without an AF score that matches the trigger.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import Any

DEFAULT_T10_DELAY_S = 600.0
DEFAULT_T10_MAX_LATE_S = 900.0
DEFAULT_T10_ENABLED = True
# AF live poll budget at fire (already waited delay_s). Cache miss skips immediately.
DEFAULT_T10_AF_TIMEOUT_S = 90.0


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return bool(default)
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return float(default)
    try:
        return float(raw)
    except (TypeError, ValueError):
        return float(default)


def t10_enabled() -> bool:
    """Off when ``QUOTE_T10=0`` or ``QUOTE_T10_USDC`` is unset/0."""
    if not _env_bool("QUOTE_T10", DEFAULT_T10_ENABLED):
        return False
    return t10_usdc() > 1e-12


def t10_usdc() -> float:
    """FAK and rest notional for the T+10 scan. Unset → 0 (strategy off)."""
    raw = os.getenv("QUOTE_T10_USDC")
    if raw is None or str(raw).strip() == "":
        return 0.0
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return 0.0


def t10_delay_s() -> float:
    return max(0.0, _env_float("QUOTE_T10_DELAY_S", DEFAULT_T10_DELAY_S))


def t10_max_late_s() -> float:
    return max(0.0, _env_float("QUOTE_T10_MAX_LATE_S", DEFAULT_T10_MAX_LATE_S))


def t10_af_timeout_s() -> float:
    return max(0.05, _env_float("QUOTE_T10_AF_TIMEOUT_S", DEFAULT_T10_AF_TIMEOUT_S))


def t10_event_key(source_event_key: str) -> str:
    src = str(source_event_key or "").strip()
    if src.startswith("t10|"):
        return src
    return f"t10|{src}"


def t10_source_key(event_key: str) -> str:
    """Strip the ``t10|`` prefix so stem/ts helpers see the goal key."""
    src = str(event_key or "").strip()
    if src.startswith("t10|"):
        return src[4:]
    return src


def source_matches_undone_goal(source_event_key: str, reversal_event_key: str) -> bool:
    """True when ``source`` is the inverted goal of this reverse (any ts ≤ reverse)."""
    from pitch_gate import event_key_stem, event_key_ts, invert_score_change_key

    src = t10_source_key(source_event_key)
    inv = invert_score_change_key(reversal_event_key)
    if not src or not inv:
        return False
    if event_key_stem(src) != event_key_stem(inv):
        return False
    until = event_key_ts(reversal_event_key) or event_key_ts(inv)
    ts = event_key_ts(src)
    if until and ts and ts > until:
        return False
    return True


def _pending_path(root: Path) -> Path:
    return Path(root) / "data" / "pm-quote" / "t10_pending.json"


def _slim_ev(ev: dict[str, Any]) -> dict[str, Any]:
    keep = (
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
    out: dict[str, Any] = {}
    for k in keep:
        if k in ev:
            out[k] = ev[k]
    return out


def _score_pair(row: Any) -> tuple[int, int] | None:
    if not isinstance(row, dict):
        return None
    try:
        h = row.get("home", row.get("home_score"))
        a = row.get("away", row.get("away_score"))
        if h is None or a is None:
            return None
        return int(h), int(a)
    except (TypeError, ValueError):
        return None


def orient_dqd_score_to_pm(
    hs: int,
    aws: int,
    ev: dict[str, Any] | None,
    row: dict[str, Any] | None = None,
) -> tuple[int, int]:
    """Map DQD-frame ``prev_scores`` / snapshot onto event (Polymarket) sides.

    Bridge keeps ``prev_scores`` in Dongqiudi home/away so change detection
    matches the snapshot. Score-change events already re-orient. T+10 overlay
    must do the same or team totals / exact score buy the wrong side.
    """
    ev = ev if isinstance(ev, dict) else {}
    dqd = row.get("dongqiudi") if isinstance((row or {}).get("dongqiudi"), dict) else {}
    pm = row.get("polymarket") if isinstance((row or {}).get("polymarket"), dict) else {}
    src_h = str(ev.get("dqd_home") or (dqd or {}).get("home") or "")
    src_a = str(ev.get("dqd_away") or (dqd or {}).get("away") or "")
    dst_h = str(ev.get("home") or (pm or {}).get("home") or "")
    dst_a = str(ev.get("away") or (pm or {}).get("away") or "")
    if src_h and dst_h:
        try:
            import quote_lib as lib

            oh, oa = lib._bridge_lib().orient_scores(src_h, src_a, hs, aws, dst_h, dst_a)
            return int(oh), int(oa)
        except (TypeError, ValueError, Exception):  # noqa: BLE001
            pass
    if ev.get("sides_swapped") is True:
        return aws, hs
    return hs, aws


def current_score_for_match(
    root: Path,
    match_id: str,
    fallback_ev: dict[str, Any] | None = None,
) -> tuple[int, int] | None:
    """Live DQD score in Polymarket home/away (metadata only).

    ``prev_scores`` and the DQD snapshot are venue/DQD order; re-orient before
    attaching as ``dqd_live_*`` on the T+10 work event. Never used as the
    traded score — that stays the triggering goal.
    """
    mid = str(match_id or "").strip()
    row: dict[str, Any] | None = None
    if mid:
        try:
            import quote_lib as lib

            row = lib.find_match_row(root, match_id=mid)
        except Exception:  # noqa: BLE001
            row = None

    dqd_pair: tuple[int, int] | None = None
    if mid:
        try:
            import quote_lib as lib

            owned = lib.get_owned_bridge()
            ps = getattr(owned, "_prev_scores", None) if owned is not None else None
            if isinstance(ps, dict):
                dqd_pair = _score_pair(ps.get(mid))
        except Exception:  # noqa: BLE001
            pass
        if dqd_pair is None:
            try:
                import quote_lib as lib

                file_ps = lib.load_json(lib.bridge_dir(root) / "prev_scores.json", {}) or {}
                if isinstance(file_ps, dict):
                    dqd_pair = _score_pair(file_ps.get(mid))
            except Exception:  # noqa: BLE001
                pass
        if dqd_pair is None and isinstance(row, dict):
            dqd_pair = _score_pair((row or {}).get("dongqiudi") or {})
    if dqd_pair is not None:
        return orient_dqd_score_to_pm(dqd_pair[0], dqd_pair[1], fallback_ev, row)
    if fallback_ev:
        try:
            from score_events import target_score_from_event

            got = target_score_from_event(fallback_ev)
            if got is not None:
                return got
        except Exception:  # noqa: BLE001
            pass
        got = _score_pair(fallback_ev)
        if got is not None:
            return got
    return None


def match_is_played(root: Path, match_id: str) -> bool:
    mid = str(match_id or "").strip()
    if not mid:
        return False
    try:
        import quote_lib as lib

        row = lib.find_match_row(root, match_id=mid)
    except Exception:  # noqa: BLE001
        return False
    if not isinstance(row, dict):
        return False
    dqd = row.get("dongqiudi") or {}
    st = str(dqd.get("status") or "").lower()
    disp = str(dqd.get("status_display") or "").lower()
    return st in {"played", "finished"} or disp in {"played", "ft", "full time"}


def trigger_score_from_job(job: dict[str, Any]) -> tuple[int, int] | None:
    """Score **after** the goal that scheduled this T+10 — not the live overlay."""
    ev = job.get("ev") if isinstance(job.get("ev"), dict) else {}
    try:
        from score_events import target_score_from_event

        got = target_score_from_event(ev)
        if got is not None:
            return got
    except Exception:  # noqa: BLE001
        pass
    return _score_pair(ev)


def build_t10_work_event(
    root: Path,
    job: dict[str, Any],
) -> dict[str, Any] | None:
    ev = job.get("ev") if isinstance(job.get("ev"), dict) else {}
    mid = str(job.get("match_id") or ev.get("match_id") or "").strip()
    src = str(job.get("source_event_key") or "").strip()
    if not mid or not src:
        return None
    score = trigger_score_from_job(job)
    if score is None:
        return None
    hs, aws = score
    work = dict(ev)
    work["type"] = "score_change"
    work["match_id"] = mid
    work["home_score"] = hs
    work["away_score"] = aws
    work["curr"] = {"home": hs, "away": aws}
    work.pop("is_reversal", None)
    live = current_score_for_match(root, mid, ev)
    if live is not None:
        work["dqd_live_home"] = live[0]
        work["dqd_live_away"] = live[1]
    try:
        import quote_lib as lib

        row = lib.find_match_row(root, match_id=mid)
    except Exception:  # noqa: BLE001
        row = None
    if isinstance(row, dict):
        dqd = row.get("dongqiudi") if isinstance(row.get("dongqiudi"), dict) else {}
        pm = row.get("polymarket") if isinstance(row.get("polymarket"), dict) else {}
        try:
            fields = lib._bridge_lib().pm_side_fields(dqd or {}, pm or {})
        except Exception:  # noqa: BLE001
            fields = {}
        if isinstance(fields, dict):
            if "sides_swapped" in fields:
                work["sides_swapped"] = fields.get("sides_swapped")
            if fields.get("dqd_home"):
                work["dqd_home"] = fields.get("dqd_home")
            if fields.get("dqd_away"):
                work["dqd_away"] = fields.get("dqd_away")
            hh, ah = fields.get("home_half"), fields.get("away_half")
            if hh not in (None, "") and ah not in (None, ""):
                work["home_half"] = hh
                work["away_half"] = ah
    t10_key = str(job.get("t10_event_key") or t10_event_key(src))
    work["_trade_event_key"] = t10_key
    work["_trade_context"] = {
        "pitch_gate": True,
        "t10": True,
        "base_event_key": t10_key,
        "source_event_key": src,
    }
    return work


class T10Scheduler:
    """Persist pending T+10 jobs across watch ticks / restarts."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self._lock = threading.Lock()
        self._jobs: dict[str, dict[str, Any]] = {}
        self._blocked_until: dict[str, str] = {}
        self._load()

    def _load(self) -> None:
        try:
            import quote_lib as lib

            raw = lib.load_json(_pending_path(self.root), {}) or {}
        except Exception:  # noqa: BLE001
            raw = {}
        jobs = raw.get("jobs") if isinstance(raw, dict) else raw
        if not isinstance(jobs, list):
            jobs = []
        out: dict[str, dict[str, Any]] = {}
        for row in jobs:
            if not isinstance(row, dict):
                continue
            src = str(row.get("source_event_key") or "").strip()
            if src:
                out[src] = row
        self._jobs = out
        blocked: dict[str, str] = {}
        raw_block = raw.get("blocked_until") if isinstance(raw, dict) else None
        if isinstance(raw_block, dict):
            for stem, until in raw_block.items():
                s = str(stem or "").strip()
                u = str(until or "").strip()
                if s and u:
                    blocked[s] = u
        self._blocked_until = blocked

    def _save_locked(self) -> None:
        try:
            import quote_lib as lib

            payload = {
                "updated_at": lib.now_cn_iso(),
                "jobs": list(self._jobs.values()),
                "blocked_until": dict(self._blocked_until),
            }
            lib.write_json(_pending_path(self.root), payload)
        except Exception:  # noqa: BLE001
            pass

    def schedule(
        self,
        ev: dict[str, Any],
        *,
        event_key: str,
        now: float | None = None,
    ) -> bool:
        if not t10_enabled():
            return False
        src = str(event_key or "").strip()
        mid = str(ev.get("match_id") or "").strip()
        if not src or not mid:
            return False
        with self._lock:
            if src in self._jobs:
                return False
            due = float(now if now is not None else time.time()) + t10_delay_s()
            self._jobs[src] = {
                "source_event_key": src,
                "t10_event_key": t10_event_key(src),
                "match_id": mid,
                "due_ts": due,
                "ev": _slim_ev(ev),
            }
            self._save_locked()
        return True

    def cancel_match(self, match_id: str) -> int:
        mid = str(match_id or "").strip()
        if not mid:
            return 0
        with self._lock:
            drop = [k for k, j in self._jobs.items() if str(j.get("match_id") or "") == mid]
            for k in drop:
                self._jobs.pop(k, None)
            if drop:
                self._save_locked()
            return len(drop)

    def _block_undone_goal_locked(self, reversal_event_key: str) -> bool:
        """Record inverted stem → reverse ts. Caller holds ``_lock``."""
        from pitch_gate import event_key_stem, event_key_ts, invert_score_change_key

        inv = invert_score_change_key(reversal_event_key)
        if not inv:
            return False
        stem = event_key_stem(inv)
        ts = event_key_ts(reversal_event_key) or event_key_ts(inv)
        if not stem or not ts:
            return False
        prev = str(self._blocked_until.get(stem) or "")
        if ts >= prev:
            self._blocked_until[stem] = ts
            return True
        return False

    def block_undone_goal(self, reversal_event_key: str) -> bool:
        """Remember this reverse so an already-popped T+10 still skips submit."""
        rev = str(reversal_event_key or "").strip()
        if not rev:
            return False
        with self._lock:
            changed = self._block_undone_goal_locked(rev)
            if changed:
                self._save_locked()
            return changed

    def is_source_blocked(self, source_event_key: str) -> bool:
        """True when this goal stem is blocked and ``ts`` ≤ the reverse ts."""
        from pitch_gate import event_key_stem, event_key_ts

        src = t10_source_key(source_event_key)
        stem = event_key_stem(src)
        if not stem:
            return False
        ts = event_key_ts(src)
        with self._lock:
            until = str(self._blocked_until.get(stem) or "")
            if not until:
                return False
            if not ts:
                return True
            return ts <= until

    def cancel_undone_goal(self, reversal_event_key: str) -> list[str]:
        """Drop pending T+10 for the inverted goal and block the stem.

        Matches the goal stem (``score_change|mid|from->to``), any ``ts`` ≤ the
        reverse. A later re-award of the same transition keeps its own job.
        The stem block covers a job already ``pop_due``'d on the watch thread.
        Returns cancelled ``t10_event_key`` values.
        """
        rev = str(reversal_event_key or "").strip()
        if not rev:
            return []
        cancelled: list[str] = []
        with self._lock:
            blocked = self._block_undone_goal_locked(rev)
            drop = [src for src in list(self._jobs) if source_matches_undone_goal(src, rev)]
            for src in drop:
                job = self._jobs.pop(src, None)
                if job is None:
                    continue
                cancelled.append(str(job.get("t10_event_key") or t10_event_key(src)))
            if drop or blocked:
                self._save_locked()
        return cancelled

    def pending_keys_for_match(self, match_id: str) -> list[str]:
        mid = str(match_id or "").strip()
        if not mid:
            return []
        with self._lock:
            return [
                str(j.get("t10_event_key") or t10_event_key(k))
                for k, j in self._jobs.items()
                if str(j.get("match_id") or "") == mid
            ]

    def pop_due(self, *, now: float | None = None) -> list[dict[str, Any]]:
        ts = float(now if now is not None else time.time())
        late = t10_max_late_s()
        due_rows: list[dict[str, Any]] = []
        stale_keys: list[str] = []
        with self._lock:
            for src, job in list(self._jobs.items()):
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
                f"t10 → SKIP stale jobs n={len(stale_keys)} "
                f"(later than {late:g}s after due)",
                flush=True,
            )
        return due_rows


_active: T10Scheduler | None = None
_active_lock = threading.Lock()


def get_scheduler(root: Path) -> T10Scheduler:
    global _active
    rt = Path(root)
    with _active_lock:
        if _active is None or _active.root != rt:
            _active = T10Scheduler(rt)
        return _active


def abort_t10_for_reversal(
    root: Path,
    reversal_event_key: str,
    *,
    match_id: str = "",
    referee: Any | None = None,
    worker: Any | None = None,
) -> list[str]:
    """Cancel queued T+10 for the undone goal and abort in-flight AF live polls.

    Always records the inverted stem (even if the job already left the queue)
    so ``_drain_t10`` can skip a just-popped due row.
    """
    rev = str(reversal_event_key or "").strip()
    cancelled: list[str] = []
    if not rev:
        return cancelled
    cancelled.extend(get_scheduler(root).cancel_undone_goal(rev))

    if referee is None:
        try:
            import af_referee as _af

            referee = _af.active_ft_referee()
        except Exception:  # noqa: BLE001
            referee = None
    if worker is None:
        try:
            from quote_worker import get_quote_worker

            worker = get_quote_worker()
        except Exception:  # noqa: BLE001
            worker = None

    extra: list[str] = []
    if referee is not None:
        try:
            pending = list(referee.pending_event_keys() or [])
        except Exception:  # noqa: BLE001
            pending = []
        for key in pending:
            k = str(key)
            if source_matches_undone_goal(k, rev):
                extra.append(k)
        for k in extra:
            try:
                referee.cancel_key(k, reason="dqd_reversal")
            except Exception:  # noqa: BLE001
                pass
            if k not in cancelled:
                cancelled.append(k)

    if worker is not None:
        for k in cancelled:
            try:
                worker.revoke_event(k)
            except Exception:  # noqa: BLE001
                pass

    if cancelled:
        mid = str(match_id or "").strip()
        print(
            f"t10 → CANCEL reversal n={len(cancelled)} match_id={mid} "
            f"keys={','.join(cancelled)}",
            flush=True,
        )
    return cancelled


def reset_scheduler_for_tests() -> None:
    global _active
    with _active_lock:
        _active = None
