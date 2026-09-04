"""Post-goal +10min book rescan.

After a paired DQD goal-up, wait ``QUOTE_T10_DELAY_S`` (default 600s) and quote
from the **API-Football live score** at fire time. Dongqiudi ``prev_scores`` is
only a skeleton (sides / halves); no T+10 order without an AF score.
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
    """Live score in Polymarket home/away (same frame as the scheduled event).

    ``prev_scores`` and the DQD snapshot are venue/DQD order; re-orient before
    overlaying onto the PM-labeled T+10 work event.
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


def build_t10_work_event(
    root: Path,
    job: dict[str, Any],
) -> dict[str, Any] | None:
    ev = job.get("ev") if isinstance(job.get("ev"), dict) else {}
    mid = str(job.get("match_id") or ev.get("match_id") or "").strip()
    src = str(job.get("source_event_key") or "").strip()
    if not mid or not src:
        return None
    score = current_score_for_match(root, mid, ev)
    if score is None:
        score = _score_pair(ev)
    if score is None:
        # Placeholder until AF live confirm overwrites home/away_score.
        score = (0, 0)
    hs, aws = score
    work = dict(ev)
    work["type"] = "score_change"
    work["match_id"] = mid
    work["home_score"] = hs
    work["away_score"] = aws
    work["curr"] = {"home": hs, "away": aws}
    work.pop("prev", None)
    work.pop("is_reversal", None)
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

    def _save_locked(self) -> None:
        try:
            import quote_lib as lib

            payload = {
                "updated_at": lib.now_cn_iso(),
                "jobs": list(self._jobs.values()),
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


def reset_scheduler_for_tests() -> None:
    global _active
    with _active_lock:
        _active = None
