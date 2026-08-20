"""Pitch-screenshot gate: first frame @+5s, then every 5s until 2.5min; buy once on in_play."""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger("pm_quote.pitch_gate")

GATE_INTERVAL_S = 5.0
# First capture is delayed so celebration/VAR overlays can clear.
GATE_FIRST_DELAY_S = 5.0
# Minimum captures for the board / research trail (keep going after early in_play).
GATE_MIN_FRAMES = 5
# Hard ceiling for the whole session (not a max frame count).
GATE_TIMEOUT_S = 150.0
# Backward-compat alias used by older smokes/docs.
GATE_FRAME_COUNT = GATE_MIN_FRAMES

OnInPlay = Callable[[dict[str, Any]], None]
# result payload: {status, event_key, match_id, ev, judge?, reason?, elapsed_s?}


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def gate_ready() -> tuple[bool, str]:
    """Both stream observe and pitch-state must be enabled for the gate."""
    if not _env_bool("QUOTE_DQD_STREAM_OBSERVE", False):
        return False, "QUOTE_DQD_STREAM_OBSERVE=0"
    if not _env_bool("QUOTE_PITCH_STATE", False):
        return False, "QUOTE_PITCH_STATE=0"
    return True, ""


@dataclass
class _GateSession:
    match_id: str
    event_key: str
    ev: dict[str, Any]
    t0_mono: float
    cancel: threading.Event = field(default_factory=threading.Event)
    cancel_reason: str = ""
    thread: threading.Thread | None = None
    finished: bool = False
    buy_emitted: bool = False


class PitchGateCoordinator:
    """Per-goal capture/judge sessions; results drained on the quote tick."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self._lock = threading.Lock()
        self._by_event: dict[str, _GateSession] = {}
        self._by_match: dict[str, set[str]] = {}
        self._done: list[dict[str, Any]] = []

    def pending_event_keys(self) -> set[str]:
        with self._lock:
            return set(self._by_event.keys())

    def drain_done(self) -> list[dict[str, Any]]:
        with self._lock:
            out = list(self._done)
            self._done.clear()
            return out

    def start_gate(self, ev: dict[str, Any], *, event_key: str) -> bool:
        """Start (or replace) a gate session for this goal event_key."""
        from dqd_stream_observe import get_active_observer

        mid = str(ev.get("match_id") or "").strip()
        key = str(event_key or "").strip()
        if not mid or not key:
            return False
        observer = get_active_observer()
        if observer is None:
            self._push_done(
                {
                    "status": "unavailable",
                    "event_key": key,
                    "match_id": mid,
                    "ev": dict(ev),
                    "reason": "dqd_stream_observer_missing",
                }
            )
            return False

        ok, reason = gate_ready()
        if not ok:
            self._push_done(
                {
                    "status": "unavailable",
                    "event_key": key,
                    "match_id": mid,
                    "ev": dict(ev),
                    "reason": reason,
                }
            )
            return False

        session = _GateSession(
            match_id=mid,
            event_key=key,
            ev=dict(ev),
            t0_mono=time.monotonic(),
        )
        with self._lock:
            # New goal on same match cancels prior open gates for that match.
            prior_keys = list(self._by_match.get(mid) or ())
            for pk in prior_keys:
                old = self._by_event.get(pk)
                if old is not None and not old.finished:
                    old.cancel_reason = "superseded_by_new_goal"
                    old.cancel.set()
            self._by_event[key] = session
            self._by_match.setdefault(mid, set()).add(key)

        thread = threading.Thread(
            target=self._run_session,
            args=(session, observer),
            name=f"pitch-gate-{mid}",
            daemon=True,
        )
        session.thread = thread
        thread.start()
        print(
            f"pitch-gate → START match_id={mid} key={key} "
            f"first_delay={GATE_FIRST_DELAY_S:g}s interval={GATE_INTERVAL_S:g}s "
            f"min_frames={GATE_MIN_FRAMES} timeout={GATE_TIMEOUT_S:g}s "
            f"(buy on first in_play; keep capturing until timeout)",
            flush=True,
        )
        return True

    def cancel_match(self, match_id: str, *, reason: str = "dqd_reversal") -> int:
        mid = str(match_id or "").strip()
        if not mid:
            return 0
        n = 0
        with self._lock:
            keys = list(self._by_match.get(mid) or ())
            for key in keys:
                sess = self._by_event.get(key)
                if sess is not None and not sess.finished:
                    sess.cancel_reason = reason
                    sess.cancel.set()
                    n += 1
        if n:
            print(
                f"pitch-gate → CANCEL match_id={mid} sessions={n} reason={reason}",
                flush=True,
            )
        return n

    def _push_done(self, row: dict[str, Any]) -> None:
        with self._lock:
            self._done.append(row)

    def _emit_buy_once(
        self,
        session: _GateSession,
        *,
        judge: dict[str, Any] | None,
        elapsed_s: float,
        sample_i: int,
    ) -> bool:
        """Queue a one-shot buy signal without ending the capture session."""
        with self._lock:
            if session.finished or session.buy_emitted or session.cancel.is_set():
                return False
            session.buy_emitted = True
            self._done.append(
                {
                    "status": "in_play",
                    "event_key": session.event_key,
                    "match_id": session.match_id,
                    "ev": dict(session.ev),
                    "judge": judge,
                    "reason": "play_state_in_play",
                    "elapsed_s": elapsed_s,
                    "sample_i": sample_i,
                }
            )
        print(
            f"pitch-gate → IN_PLAY (buy once) match_id={session.match_id} "
            f"key={session.event_key} sample={sample_i} elapsed={elapsed_s:.1f}s "
            f"· continue ≥{GATE_MIN_FRAMES} frames / ≤{GATE_TIMEOUT_S:g}s",
            flush=True,
        )
        return True

    def _finish_session(
        self,
        session: _GateSession,
        *,
        status: str,
        judge: dict[str, Any] | None = None,
        reason: str = "",
        elapsed_s: float | None = None,
        frames: int | None = None,
    ) -> None:
        with self._lock:
            if session.finished:
                return
            if status == "in_play" and session.cancel.is_set():
                status = "canceled"
                reason = getattr(session, "cancel_reason", None) or reason or "canceled"
                judge = None
            session.finished = True
            self._by_event.pop(session.event_key, None)
            keys = self._by_match.get(session.match_id)
            if keys is not None:
                keys.discard(session.event_key)
                if not keys:
                    self._by_match.pop(session.match_id, None)
            self._done.append(
                {
                    "status": status,
                    "event_key": session.event_key,
                    "match_id": session.match_id,
                    "ev": dict(session.ev),
                    "judge": judge,
                    "reason": reason or status,
                    "elapsed_s": elapsed_s,
                    "frames": frames,
                    "buy_emitted": bool(session.buy_emitted),
                }
            )

    def _run_session(self, session: _GateSession, observer: Any) -> None:
        captured = 0
        sample_i = 0
        try:
            # First frame at t0 + GATE_FIRST_DELAY_S (default +5s after goal).
            first_t = session.t0_mono + max(0.0, float(GATE_FIRST_DELAY_S))
            while not session.cancel.is_set():
                now = time.monotonic()
                if now - session.t0_mono > GATE_TIMEOUT_S + 1e-9:
                    break
                if now >= first_t:
                    break
                time.sleep(min(0.2, max(0.0, first_t - now)))

            while True:
                if session.cancel.is_set():
                    break
                elapsed = time.monotonic() - session.t0_mono
                # Stop starting new captures after the hard timeout.
                if elapsed > GATE_TIMEOUT_S + 1e-9:
                    break

                job = _CaptureJob(
                    match_id=session.match_id,
                    event_key=session.event_key,
                    dqd_ts=str(session.ev.get("ts") or ""),
                    home=str(session.ev.get("home") or ""),
                    away=str(session.ev.get("away") or ""),
                    home_score=session.ev.get("home_score"),
                    away_score=session.ev.get("away_score"),
                    t0_mono=session.t0_mono,
                )
                row = observer._capture_row(
                    job, sample_i=sample_i, elapsed_s=round(elapsed, 3)
                )
                row["gate"] = True
                try:
                    observer._write_rows([row])
                except Exception:  # noqa: BLE001
                    logger.exception("pitch-gate observe write failed")
                captured += 1

                if row.get("ok") is True and row.get("frame_path"):
                    judged = _judge_frame_sync(row)
                    play_state = str((judged or {}).get("play_state") or "")
                    if play_state == "in_play":
                        if session.cancel.is_set():
                            break
                        self._emit_buy_once(
                            session,
                            judge=judged,
                            elapsed_s=round(time.monotonic() - session.t0_mono, 3),
                            sample_i=sample_i,
                        )
                        # Keep capturing until timeout; do not return.

                sample_i += 1
                next_t = (
                    session.t0_mono
                    + max(0.0, float(GATE_FIRST_DELAY_S))
                    + sample_i * GATE_INTERVAL_S
                )
                # Next slot past the timeout ceiling → done.
                if next_t - session.t0_mono > GATE_TIMEOUT_S + 1e-9:
                    break
                while not session.cancel.is_set():
                    now = time.monotonic()
                    if now >= next_t:
                        break
                    if now - session.t0_mono > GATE_TIMEOUT_S:
                        break
                    time.sleep(min(0.2, max(0.0, next_t - now)))

            elapsed_end = round(time.monotonic() - session.t0_mono, 3)
            if session.cancel.is_set():
                self._finish_session(
                    session,
                    status="canceled",
                    reason=getattr(session, "cancel_reason", None) or "canceled",
                    elapsed_s=elapsed_end,
                    frames=captured,
                )
                return

            if session.buy_emitted:
                self._finish_session(
                    session,
                    status="complete",
                    reason=f"captured_{captured}_frames",
                    elapsed_s=elapsed_end,
                    frames=captured,
                )
                print(
                    f"pitch-gate → COMPLETE match_id={session.match_id} "
                    f"key={session.event_key} frames={captured} "
                    f"elapsed={elapsed_end:.1f}s (buy already emitted)",
                    flush=True,
                )
            else:
                self._finish_session(
                    session,
                    status="timeout",
                    reason=(
                        f"no_in_play_in_{captured}_frames"
                        if captured
                        else "pitch_gate_timeout"
                    ),
                    elapsed_s=elapsed_end,
                    frames=captured,
                )
                print(
                    f"pitch-gate → NO_IN_PLAY match_id={session.match_id} "
                    f"key={session.event_key} frames={captured} "
                    f"elapsed={elapsed_end:.1f}s",
                    flush=True,
                )
        except Exception as e:  # noqa: BLE001
            logger.exception(
                "pitch-gate session failed match=%s key=%s",
                session.match_id,
                session.event_key,
            )
            self._finish_session(
                session,
                status="error",
                reason=str(e),
                elapsed_s=round(time.monotonic() - session.t0_mono, 3),
                frames=captured,
            )


@dataclass
class _CaptureJob:
    """Duck-type for DqdStreamObserver._capture_row."""

    match_id: str
    event_key: str
    dqd_ts: str
    home: str
    away: str
    home_score: Any
    away_score: Any
    t0_mono: float


def _judge_frame_sync(row: dict[str, Any]) -> dict[str, Any] | None:
    import sys

    try:
        pitch_state_scripts = (
            Path(__file__).resolve().parents[2] / "pitch-state" / "scripts"
        )
        if str(pitch_state_scripts) not in sys.path:
            sys.path.insert(0, str(pitch_state_scripts))
        from pipeline import judge_inputs  # type: ignore

        result = judge_inputs(
            image=Path(str(row["frame_path"])),
            match_id=str(row.get("match_id") or "") or None,
            event_key=str(row.get("event_key") or "") or None,
            frame_meta={
                "sample_i": row.get("sample_i"),
                "elapsed_s": row.get("elapsed_s"),
                "surface": row.get("surface"),
                "stream_url": row.get("stream_url"),
                "page_url": row.get("page_url"),
                "frame_kind": row.get("frame_kind"),
                "match_id": row.get("match_id"),
                "event_key": row.get("event_key"),
                "gate": True,
                "home_score": row.get("home_score"),
                "away_score": row.get("away_score"),
            },
            append_output=True,
            write_sidecars=True,
        )
        if isinstance(result, dict):
            return result
        return None
    except Exception:  # noqa: BLE001
        logger.exception(
            "pitch-gate judge failed match=%s sample=%s",
            row.get("match_id"),
            row.get("sample_i"),
        )
        return None


_coordinator: PitchGateCoordinator | None = None
_coord_lock = threading.Lock()


def get_coordinator(root: Path | None = None) -> PitchGateCoordinator:
    global _coordinator
    with _coord_lock:
        if _coordinator is None:
            if root is None:
                raise RuntimeError("pitch gate coordinator requires root on first use")
            _coordinator = PitchGateCoordinator(root)
        return _coordinator


def reset_coordinator_for_tests() -> None:
    global _coordinator
    with _coord_lock:
        _coordinator = None
