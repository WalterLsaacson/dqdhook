"""Pitch-gate: same-tick DOM∧AF buy, then stop; reversal AF∨DOM flatten.

Cadence: first sample @+5s, then every 5s until 120s (or buy / flatten / cancel).
No screenshots. No nami ball-xy. Odds observe rides the same clock.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger("pm_quote.pitch_gate")

GATE_INTERVAL_S = 5.0
GATE_FIRST_DELAY_S = 5.0
GATE_TIMEOUT_S = 120.0
GATE_REQUIRE_SCORE = True
GATE_CONFIRM_FRAMES = 1
# Unused for capture length (buy/flatten stop the session). Kept for older smokes.
GATE_MIN_FRAMES = 1
GATE_FRAME_COUNT = GATE_MIN_FRAMES


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _judge_shows_var(judge: dict[str, Any] | None) -> bool:
    """True when pitch-state marked this frame as a VAR review overlay."""
    if not isinstance(judge, dict):
        return False
    reason = str(judge.get("stopped_reason") or "").strip().lower()
    if reason == "var":
        return True
    for item in judge.get("evidence") or []:
        text = str(item or "")
        if "VAR" in text or text.strip().lower() == "var":
            return True
    return False


def _animation_rules() -> Any:
    """Keyword tables live with pitch-state; DOM mode reuses them without OCR."""
    import sys

    scripts = Path(__file__).resolve().parents[2] / "pitch-state" / "scripts"
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    import animation_rules  # type: ignore

    return animation_rules


def gate_source() -> str:
    """Live gate is DOM-only. ``ocr`` is rejected."""
    return "dom"


def gate_ready() -> tuple[bool, str]:
    if not _env_bool("QUOTE_DQD_STREAM_OBSERVE", False):
        return False, "QUOTE_DQD_STREAM_OBSERVE=0"
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
    require_score: bool = True
    confirm_streak: int = 0
    var_seen: bool = False
    observe_only: bool = False


class PitchGateCoordinator:
    """Per-goal capture/judge sessions; results drained on the quote tick."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self._lock = threading.Lock()
        self._by_event: dict[str, _GateSession] = {}
        self._by_match: dict[str, set[str]] = {}
        self._done: list[dict[str, Any]] = []
        self._in_play_keys: set[str] = set()
        self._bought_matches: set[str] = set()
        # Terminal event_keys (buy / timeout / cancel / supersede). Blocks
        # file-lag replays from opening a second AF/DOM trail after reverse.
        self._closed_keys: set[str] = set()
        self._closed_by_match: dict[str, set[str]] = {}

    def pending_event_keys(self) -> set[str]:
        with self._lock:
            return set(self._by_event.keys())

    def drain_done(self) -> list[dict[str, Any]]:
        with self._lock:
            out = list(self._done)
            self._done.clear()
            return out

    def has_bought_match(self, match_id: str) -> bool:
        mid = str(match_id or "").strip()
        if not mid:
            return False
        with self._lock:
            return mid in self._bought_matches

    def has_consumed_event(self, event_key: str) -> bool:
        key = str(event_key or "").strip()
        if not key:
            return False
        with self._lock:
            return key in self._closed_keys

    def consumed_event_keys(self, match_id: str) -> set[str]:
        mid = str(match_id or "").strip()
        if not mid:
            return set()
        with self._lock:
            return set(self._closed_by_match.get(mid) or ())

    def _mark_closed_locked(self, match_id: str, event_key: str) -> None:
        """Caller must hold ``_lock``."""
        mid = str(match_id or "").strip()
        key = str(event_key or "").strip()
        if not key:
            return
        self._closed_keys.add(key)
        if mid:
            self._closed_by_match.setdefault(mid, set()).add(key)

    def start_gate(
        self,
        ev: dict[str, Any],
        *,
        event_key: str,
        observe_only: bool = False,
    ) -> bool:
        """Start (or replace) a gate session for this goal / reversal event_key."""
        from dqd_stream_observe import get_active_observer

        mid = str(ev.get("match_id") or "").strip()
        key = str(event_key or "").strip()
        if not mid or not key:
            return False
        with self._lock:
            if key in self._closed_keys:
                return False
            existing = self._by_event.get(key)
            if existing is not None and not existing.finished:
                return True
        observer = get_active_observer()
        if observer is None:
            self._push_done(
                {
                    "status": "unavailable",
                    "event_key": key,
                    "match_id": mid,
                    "ev": dict(ev),
                    "reason": "dqd_stream_observer_missing",
                    "observe_only": bool(observe_only),
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
                    "observe_only": bool(observe_only),
                }
            )
            return False

        session = _GateSession(
            match_id=mid,
            event_key=key,
            ev=dict(ev),
            t0_mono=time.monotonic(),
            observe_only=bool(observe_only),
        )
        with self._lock:
            if key in self._closed_keys:
                return False
            existing = self._by_event.get(key)
            if existing is not None and not existing.finished:
                return True
            prior_keys = list(self._by_match.get(mid) or ())
            for pk in prior_keys:
                if pk == key:
                    continue
                old = self._by_event.get(pk)
                if old is not None and not old.finished and not old.cancel.is_set():
                    old.cancel_reason = "superseded_by_new_goal"
                    old.cancel.set()
                    self._mark_closed_locked(mid, pk)
            self._revoke_pending_buys_locked(
                match_id=mid, reason="superseded_by_new_goal"
            )
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
        kind = "OBSERVE START" if session.observe_only else "START"
        extra = (
            "reversal trail; flatten on AF∨DOM score_match"
            if session.observe_only
            else (
                f"DOM in_play ∧ AF score_match; VAR→no buy; "
                f"stop on aligned_buy / {GATE_TIMEOUT_S:g}s"
            )
        )
        print(
            f"pitch-gate → {kind} match_id={mid} key={key} "
            f"first_delay={GATE_FIRST_DELAY_S:g}s interval={GATE_INTERVAL_S:g}s "
            f"timeout={GATE_TIMEOUT_S:g}s ({extra})",
            flush=True,
        )
        return True

    def cancel_match(self, match_id: str, *, reason: str = "dqd_reversal") -> int:
        mid = str(match_id or "").strip()
        if not mid:
            return 0
        n = 0
        revoked = 0
        with self._lock:
            keys = list(self._by_match.get(mid) or ())
            for key in keys:
                self._mark_closed_locked(mid, key)
                sess = self._by_event.get(key)
                if sess is not None and not sess.finished:
                    sess.cancel_reason = reason
                    sess.cancel.set()
                    n += 1
            revoked = self._revoke_pending_buys_locked(
                match_id=mid, reason=reason or "dqd_reversal"
            )
        if n or revoked:
            print(
                f"pitch-gate → CANCEL match_id={mid} sessions={n} "
                f"revoked_buys={revoked} reason={reason}",
                flush=True,
            )
        return n + revoked

    def should_observe_reversal(
        self, reversal_event_key: str, *, match_id: str = ""
    ) -> bool:
        """True when this match already emitted a buy (lots may still be open)."""
        mid = str(match_id or "").strip()
        if mid:
            return self.has_bought_match(mid)
        inv = invert_score_change_key(reversal_event_key)
        with self._lock:
            if inv and inv in self._in_play_keys:
                return True
        return False

    def _revoke_pending_buys_locked(
        self,
        *,
        match_id: str | None = None,
        event_key: str | None = None,
        reason: str = "canceled",
    ) -> int:
        """Invalidate undrained ``in_play`` buy rows. Caller must hold ``_lock``."""
        mid = str(match_id or "").strip() or None
        ek = str(event_key or "").strip() or None
        if mid is None and ek is None:
            return 0
        kept: list[dict[str, Any]] = []
        revoked = 0
        for row in self._done:
            if str(row.get("status") or "") != "in_play":
                kept.append(row)
                continue
            row_mid = str(row.get("match_id") or "")
            row_ek = str(row.get("event_key") or "")
            if mid is not None and row_mid != mid:
                kept.append(row)
                continue
            if ek is not None and row_ek != ek:
                kept.append(row)
                continue
            revoked += 1
            sess = self._by_event.get(row_ek)
            if sess is not None:
                sess.buy_emitted = False
            kept.append(
                {
                    "status": "buy_revoked",
                    "event_key": row_ek,
                    "match_id": row_mid,
                    "ev": row.get("ev") if isinstance(row.get("ev"), dict) else {},
                    "judge": row.get("judge"),
                    "reason": reason or "buy_revoked_on_cancel",
                    "elapsed_s": row.get("elapsed_s"),
                    "sample_i": row.get("sample_i"),
                    "buy_emitted": False,
                    "revoked": True,
                }
            )
        if revoked:
            self._done = kept
        return revoked

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
        af_row: dict[str, Any] | None = None,
    ) -> bool:
        """Queue a one-shot buy. Caller must then finish the session."""
        with self._lock:
            if (
                session.finished
                or session.buy_emitted
                or session.cancel.is_set()
                or session.var_seen
                or session.observe_only
            ):
                return False
            session.buy_emitted = True
            self._in_play_keys.add(session.event_key)
            self._bought_matches.add(session.match_id)
            self._done.append(
                {
                    "status": "in_play",
                    "event_key": session.event_key,
                    "match_id": session.match_id,
                    "ev": dict(session.ev),
                    "judge": judge,
                    "af": af_row,
                    "reason": "dom_in_play_and_af_score_match",
                    "elapsed_s": elapsed_s,
                    "sample_i": sample_i,
                }
            )
            print(
                f"pitch-gate → IN_PLAY (aligned buy) match_id={session.match_id} "
                f"key={session.event_key} sample={sample_i} elapsed={elapsed_s:.1f}s "
                f"· stop capture",
                flush=True,
            )
        return True

    def _emit_flatten_or(
        self,
        session: _GateSession,
        *,
        judge: dict[str, Any] | None,
        elapsed_s: float,
        sample_i: int,
        af_row: dict[str, Any] | None,
        source: str,
    ) -> bool:
        with self._lock:
            if session.finished or session.cancel.is_set():
                return False
            self._done.append(
                {
                    "status": "flatten_or",
                    "event_key": session.event_key,
                    "match_id": session.match_id,
                    "ev": dict(session.ev),
                    "judge": judge,
                    "af": af_row,
                    "reason": f"reversal_{source}_score_match",
                    "elapsed_s": elapsed_s,
                    "sample_i": sample_i,
                    "observe_only": True,
                }
            )
            print(
                f"pitch-gate → FLATTEN_OR match_id={session.match_id} "
                f"key={session.event_key} via={source} sample={sample_i} "
                f"elapsed={elapsed_s:.1f}s",
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
            self._mark_closed_locked(session.match_id, session.event_key)
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
                    "var_seen": bool(session.var_seen),
                    "observe_only": bool(session.observe_only),
                }
            )

    def _sample_af(
        self,
        session: _GateSession,
        *,
        sample_i: int,
        elapsed_s: float,
    ) -> dict[str, Any] | None:
        try:
            from af_observe import get_active_observer as get_af

            observer = get_af()
            if observer is None:
                return None
            return observer.sample_once(
                session.ev,
                event_key=session.event_key,
                sample_i=sample_i,
                elapsed_s=elapsed_s,
            )
        except Exception:  # noqa: BLE001
            logger.debug("af gate sample skipped", exc_info=True)
            return None

    def _sample_odds(
        self,
        session: _GateSession,
        *,
        sample_i: int,
        elapsed_s: float,
    ) -> dict[str, Any] | None:
        try:
            from book_context_observe import get_active_observer as get_book

            observer = get_book()
            if observer is None:
                return None
            return observer.sample_gate_tick(
                session.ev,
                event_key=session.event_key,
                sample_i=sample_i,
                elapsed_s=elapsed_s,
                observe_only=bool(session.observe_only),
            )
        except Exception:  # noqa: BLE001
            logger.debug("odds gate sample skipped", exc_info=True)
            return None

    def _kick_odds(
        self,
        session: _GateSession,
        *,
        sample_i: int,
        elapsed_s: float,
        holder: dict[str, Any],
    ) -> threading.Thread:
        """Odds is observe-only: never block AND buy / OR flatten."""

        def _run() -> None:
            grade = self._sample_odds(
                session, sample_i=sample_i, elapsed_s=elapsed_s
            )
            if grade:
                holder["grade"] = grade

        thread = threading.Thread(
            target=_run,
            name=f"odds-gate-{session.match_id}",
            daemon=True,
        )
        thread.start()
        return thread

    def _open_dom_reader(self, session: _GateSession, observer: Any) -> Any:
        """Resolve the tracker URL once and keep that page open for the session."""
        from dqd_stream_observe import DomReader

        info = observer._resolve_surface(session.match_id)
        page_url = str((info or {}).get("page_url") or "")
        if not page_url:
            return None, "no_page_url", info
        reader = DomReader(page_url)
        ok, err = reader.open()
        if not ok:
            reader.close()
            return None, err or "dom_open_failed", info
        return reader, None, info

    @staticmethod
    def _baseline_clock(reader: Any) -> str | None:
        """Clock at page-open, so even the first sample can detect a frozen page."""
        try:
            dom, _err = reader.read()
        except Exception:  # noqa: BLE001
            return None
        if not isinstance(dom, dict):
            return None
        return _animation_rules().parse_dom_center(dom.get("center_box")).get("clock")

    def _sample_dom(
        self,
        session: _GateSession,
        reader: Any,
        info: dict[str, Any],
        *,
        sample_i: int,
        elapsed_s: float,
        prev_clock: str | None,
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        import quote_lib as lib

        rules = _animation_rules()
        row: dict[str, Any] = {
            "observed_at": lib.now_cn_iso(),
            "match_id": session.match_id,
            "event_key": session.event_key,
            "dqd_ts": str(session.ev.get("ts") or ""),
            "home": str(session.ev.get("home") or ""),
            "away": str(session.ev.get("away") or ""),
            "home_score": session.ev.get("home_score"),
            "away_score": session.ev.get("away_score"),
            "sample_i": sample_i,
            "elapsed_s": elapsed_s,
            "surface": (info or {}).get("surface"),
            "page_url": (info or {}).get("page_url"),
            "stream_url": None,
            "nami_id": (info or {}).get("nami_id"),
            "capture_method": "dom",
            "frame_kind": "dom",
            "frame_path": None,
            "ok": False,
            "error": "no_dom_reader",
            "dom_state": None,
            "gate": True,
            "observe_only": bool(session.observe_only),
            "is_reversal": bool(session.ev.get("is_reversal")),
        }
        if reader is None:
            return row, None
        dom, err = reader.read()
        row["ok"] = dom is not None
        row["error"] = err
        row["dom_state"] = dom
        if dom is None:
            return row, None
        judged = rules.judge_dom(
            dom,
            expected_home=session.ev.get("home_score"),
            expected_away=session.ev.get("away_score"),
            require_score=bool(session.require_score and GATE_REQUIRE_SCORE),
            prev_clock=prev_clock,
        )
        row["judge"] = judged
        row["board_score_match"] = bool(
            rules.board_score_match(
                dom,
                expected_home=session.ev.get("home_score"),
                expected_away=session.ev.get("away_score"),
            )
        )
        return row, judged

    def _run_session(self, session: _GateSession, observer: Any) -> None:
        captured = 0
        sample_i = 0
        reader: Any = None
        surface_info: dict[str, Any] = {}
        prev_clock: str | None = None
        flatten_emitted = False
        try:
            if not session.cancel.is_set():
                reader, open_err, surface_info = self._open_dom_reader(session, observer)
                if session.cancel.is_set():
                    if reader is not None:
                        try:
                            reader.close()
                        except Exception:  # noqa: BLE001
                            pass
                        reader = None
                elif reader is None:
                    print(
                        f"pitch-gate → DOM_UNAVAILABLE match_id={session.match_id} "
                        f"key={session.event_key} reason={open_err} "
                        f"(continue AF; no DOM buy)",
                        flush=True,
                    )
                else:
                    prev_clock = self._baseline_clock(reader)

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
                if elapsed > GATE_TIMEOUT_S + 1e-9:
                    break

                row, judged = self._sample_dom(
                    session,
                    reader,
                    surface_info,
                    sample_i=sample_i,
                    elapsed_s=round(elapsed, 3),
                    prev_clock=prev_clock,
                )
                if session.cancel.is_set():
                    break
                clock = str((judged or {}).get("dom_clock") or "")
                if clock:
                    prev_clock = clock
                has_reading = row.get("ok") is True
                board_match = bool(row.get("board_score_match"))
                if not board_match and isinstance(row.get("dom_state"), dict):
                    board_match = bool(
                        _animation_rules().board_score_match(
                            row.get("dom_state"),
                            expected_home=session.ev.get("home_score"),
                            expected_away=session.ev.get("away_score"),
                        )
                    )

                odds_holder: dict[str, Any] = {}
                self._kick_odds(
                    session,
                    sample_i=sample_i,
                    elapsed_s=round(elapsed, 3),
                    holder=odds_holder,
                )

                af_row = self._sample_af(
                    session, sample_i=sample_i, elapsed_s=round(elapsed, 3)
                )
                if session.cancel.is_set():
                    break
                if af_row:
                    row["af"] = {
                        "ok": af_row.get("ok"),
                        "score_match": af_row.get("score_match"),
                        "af_score": af_row.get("af_score"),
                        "error": af_row.get("error"),
                    }
                if odds_holder.get("grade"):
                    grade = odds_holder["grade"]
                    row["odds_grade"] = {
                        "level": grade.get("level"),
                        "reason": grade.get("reason"),
                    }

                try:
                    observer._write_rows([row])
                except Exception:  # noqa: BLE001
                    logger.exception("pitch-gate observe write failed")
                captured += 1

                af_ok = bool(af_row and af_row.get("ok") and af_row.get("score_match") is True)
                play_state = str((judged or {}).get("play_state") or "")

                if session.observe_only:
                    if af_ok or board_match:
                        self._emit_flatten_or(
                            session,
                            judge=judged,
                            elapsed_s=round(time.monotonic() - session.t0_mono, 3),
                            sample_i=sample_i,
                            af_row=af_row,
                            source="af" if af_ok else "dom",
                        )
                        flatten_emitted = True
                        break
                elif has_reading:
                    if _judge_shows_var(judged):
                        if not session.var_seen:
                            session.var_seen = True
                            print(
                                f"pitch-gate → VAR_VETO match_id={session.match_id} "
                                f"key={session.event_key} sample={sample_i} "
                                f"(no buy for this goal)",
                                flush=True,
                            )
                        session.confirm_streak = 0
                    elif play_state == "in_play":
                        if session.var_seen:
                            session.confirm_streak = 0
                        elif not af_ok:
                            print(
                                f"pitch-gate → WAIT_AF match_id={session.match_id} "
                                f"key={session.event_key} sample={sample_i} "
                                f"af_ok={bool(af_row and af_row.get('ok'))} "
                                f"score_match={(af_row or {}).get('score_match')} "
                                f"err={(af_row or {}).get('error')}",
                                flush=True,
                            )
                            session.confirm_streak = 0
                        else:
                            session.confirm_streak += 1
                            need = max(1, int(GATE_CONFIRM_FRAMES))
                            if session.confirm_streak < need:
                                print(
                                    f"pitch-gate → CONFIRM {session.confirm_streak}/{need} "
                                    f"match_id={session.match_id} key={session.event_key} "
                                    f"sample={sample_i}",
                                    flush=True,
                                )
                            elif session.cancel.is_set():
                                break
                            else:
                                self._emit_buy_once(
                                    session,
                                    judge=judged,
                                    elapsed_s=round(
                                        time.monotonic() - session.t0_mono, 3
                                    ),
                                    sample_i=sample_i,
                                    af_row=af_row,
                                )
                                break
                    else:
                        session.confirm_streak = 0

                sample_i += 1
                next_t = (
                    session.t0_mono
                    + max(0.0, float(GATE_FIRST_DELAY_S))
                    + sample_i * GATE_INTERVAL_S
                )
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

            if flatten_emitted:
                self._finish_session(
                    session,
                    status="observe_complete",
                    reason="flatten_or",
                    elapsed_s=elapsed_end,
                    frames=captured,
                )
                return

            if session.observe_only:
                self._finish_session(
                    session,
                    status="observe_complete",
                    reason=f"no_or_confirm_in_{captured}_frames",
                    elapsed_s=elapsed_end,
                    frames=captured,
                )
                print(
                    f"pitch-gate → OBSERVE_COMPLETE match_id={session.match_id} "
                    f"key={session.event_key} frames={captured} "
                    f"elapsed={elapsed_end:.1f}s (hold; no flatten)",
                    flush=True,
                )
                return

            if session.buy_emitted:
                self._finish_session(
                    session,
                    status="aligned_buy",
                    reason="stop_after_aligned_buy",
                    elapsed_s=elapsed_end,
                    frames=captured,
                )
                print(
                    f"pitch-gate → ALIGNED_BUY match_id={session.match_id} "
                    f"key={session.event_key} frames={captured} "
                    f"elapsed={elapsed_end:.1f}s (capture stopped)",
                    flush=True,
                )
            elif session.var_seen:
                self._finish_session(
                    session,
                    status="var_veto",
                    reason="var_during_capture",
                    elapsed_s=elapsed_end,
                    frames=captured,
                )
                print(
                    f"pitch-gate → VAR_VETO_DONE match_id={session.match_id} "
                    f"key={session.event_key} frames={captured} "
                    f"elapsed={elapsed_end:.1f}s (no buy)",
                    flush=True,
                )
            else:
                self._finish_session(
                    session,
                    status="timeout",
                    reason=(
                        f"no_aligned_buy_in_{captured}_frames"
                        if captured
                        else "pitch_gate_timeout"
                    ),
                    elapsed_s=elapsed_end,
                    frames=captured,
                )
                print(
                    f"pitch-gate → NO_ALIGNED_BUY match_id={session.match_id} "
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
        finally:
            if reader is not None:
                reader.close()


_coordinator: PitchGateCoordinator | None = None
_coord_lock = threading.Lock()


def invert_score_change_key(event_key: str) -> str | None:
    """Swap the ``from->to`` segment; keep match id and optional ``dqd_ts``."""
    parts = str(event_key or "").split("|")
    trans_i = next((i for i, p in enumerate(parts) if "->" in p), None)
    if trans_i is None:
        return None
    left, right = parts[trans_i].split("->", 1)
    if not left.strip() or not right.strip():
        return None
    parts[trans_i] = f"{right}->{left}"
    return "|".join(parts)


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
