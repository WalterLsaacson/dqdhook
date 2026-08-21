"""Pitch-screenshot gate: first frame @+5s, then every 5s until 2.5min; buy once on in_play."""

from __future__ import annotations

import logging
import os
import queue
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
# Pitch-gate buys require board OCR == expected DQD score on every in_play.
GATE_REQUIRE_SCORE = True
# Consecutive in_play(+score) frames required before the one-shot buy.
# 1 = buy on the first confirmed frame; delayed reversals are handled after the
# buy by the post-buy protection window (QUOTE_GATE_PROTECT_S) instead.
GATE_CONFIRM_FRAMES = 1
# Backward-compat alias used by older smokes/docs.
GATE_FRAME_COUNT = GATE_MIN_FRAMES

OnInPlay = Callable[[dict[str, Any]], None]
# result payload: {status, event_key, match_id, ev, judge?, reason?, elapsed_s?}


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class _RefShotJob:
    """Background screenshot work — never feeds OCR or the buy path."""

    op: str  # open | shot | close
    event_key: str
    page_url: str = ""
    match_id: str = ""
    sample_i: int = 0
    elapsed_s: float = 0.0
    dqd_ts: str = ""
    home: str = ""
    away: str = ""
    home_score: Any = None
    away_score: Any = None
    surface: Any = None
    nami_id: Any = None
    root: Path | None = None


def _judge_shows_var(judge: dict[str, Any] | None) -> bool:
    """True when pitch-state marked this frame as a VAR review overlay."""
    if not isinstance(judge, dict):
        return False
    reason = str(judge.get("stopped_reason") or "").strip().lower()
    if reason == "var":
        return True
    # Defensive: evidence sometimes carries the token without stopped_reason.
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
    """`dom` reads the animation's own text; `ocr` is the legacy screenshot path."""
    raw = str(os.getenv("QUOTE_GATE_SOURCE") or "").strip().lower()
    return "ocr" if raw == "ocr" else "dom"


def ref_screenshot_enabled() -> bool:
    """DOM mode: async store-only tracker screenshots for side-by-side对照."""
    if gate_source() != "dom":
        return False
    return _env_bool("QUOTE_GATE_REF_SCREENSHOT", True)


def gate_ready() -> tuple[bool, str]:
    """Stream observe is always needed; OCR mode additionally needs pitch-state."""
    if not _env_bool("QUOTE_DQD_STREAM_OBSERVE", False):
        return False, "QUOTE_DQD_STREAM_OBSERVE=0"
    if gate_source() == "ocr" and not _env_bool("QUOTE_PITCH_STATE", False):
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
    # Always on for gate: board OCR must match expected score for in_play.
    require_score: bool = True
    confirm_streak: int = 0
    # Any VAR frame during this goal's capture → never buy for this session.
    var_seen: bool = False
    # Nami tracker id, only used to tag the observe-only feed recording.
    nami_id: str = ""


class PitchGateCoordinator:
    """Per-goal capture/judge sessions; results drained on the quote tick."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self._lock = threading.Lock()
        self._by_event: dict[str, _GateSession] = {}
        self._by_match: dict[str, set[str]] = {}
        self._done: list[dict[str, Any]] = []
        # Async store-only screenshots (DOM mode对照); never touch OCR / buys.
        self._ref_q: queue.Queue[_RefShotJob | None] = queue.Queue()
        self._ref_stop = threading.Event()
        self._ref_thread: threading.Thread | None = None
        self._ref_readers: dict[str, Any] = {}

    def _ensure_ref_worker(self) -> None:
        if self._ref_thread is not None and self._ref_thread.is_alive():
            return
        self._ref_stop.clear()
        self._ref_thread = threading.Thread(
            target=self._ref_worker_loop,
            name="pitch-gate-ref-shot",
            daemon=True,
        )
        self._ref_thread.start()

    def _enqueue_ref(self, job: _RefShotJob) -> None:
        if not ref_screenshot_enabled():
            return
        self._ensure_ref_worker()
        self._ref_q.put(job)

    def _ref_worker_loop(self) -> None:
        while not self._ref_stop.is_set():
            try:
                job = self._ref_q.get(timeout=0.5)
            except queue.Empty:
                continue
            if job is None:
                break
            try:
                self._ref_handle(job)
            except Exception:  # noqa: BLE001
                logger.exception(
                    "ref screenshot failed op=%s key=%s", job.op, job.event_key
                )
        for reader in list(self._ref_readers.values()):
            try:
                reader.close()
            except Exception:  # noqa: BLE001
                pass
        self._ref_readers.clear()

    def _ref_handle(self, job: _RefShotJob) -> None:
        from dqd_stream_observe import (
            DomReader,
            _safe_part,
            frames_dir,
            get_active_observer,
        )
        import quote_lib as lib

        key = job.event_key
        if job.op == "open":
            old = self._ref_readers.pop(key, None)
            if old is not None:
                old.close()
            if not job.page_url:
                return
            reader = DomReader(job.page_url)
            ok, err = reader.open()
            if not ok:
                reader.close()
                logger.debug("ref DomReader open failed key=%s err=%s", key, err)
                return
            self._ref_readers[key] = reader
            return

        if job.op == "close":
            reader = self._ref_readers.pop(key, None)
            if reader is not None:
                reader.close()
            return

        if job.op != "shot":
            return

        reader = self._ref_readers.get(key)
        if reader is None and job.page_url:
            reader = DomReader(job.page_url)
            ok, err = reader.open()
            if ok:
                self._ref_readers[key] = reader
            else:
                reader.close()
                logger.debug("ref shot open-on-demand failed key=%s err=%s", key, err)
                return
        if reader is None:
            return

        root = Path(job.root or self.root)
        frame_dir = (
            frames_dir(root) / _safe_part(job.match_id) / _safe_part(job.event_key)
        )
        frame_path = frame_dir / (
            f"{int(job.sample_i):02d}_{int(round(job.elapsed_s)):02d}s_ref.jpg"
        )
        ok, err = reader.screenshot(frame_path)
        observer = get_active_observer()
        if observer is None:
            return
        row = {
            "observed_at": lib.now_cn_iso(),
            "match_id": job.match_id,
            "event_key": job.event_key,
            "dqd_ts": job.dqd_ts,
            "home": job.home,
            "away": job.away,
            "home_score": job.home_score,
            "away_score": job.away_score,
            "sample_i": job.sample_i,
            "elapsed_s": job.elapsed_s,
            "surface": job.surface,
            "page_url": job.page_url or None,
            "nami_id": job.nami_id,
            "capture_method": "dom_ref_shot",
            "frame_kind": "animation",
            "frame_path": str(frame_path) if ok else None,
            "ok": bool(ok),
            "error": None if ok else (err or "ref_screenshot_failed"),
            "screenshot_only": True,
            "no_ocr": True,
            "gate": True,
        }
        try:
            observer._write_rows([row])
        except Exception:  # noqa: BLE001
            logger.exception("ref screenshot observe write failed")

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
            # Still sample AF scores for research when DOM gate cannot run.
            self._af_observe_start(ev, event_key=key)
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
            self._af_observe_start(ev, event_key=key)
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
            # Also drop any undrained buy for those prior goals.
            self._revoke_pending_buys_locked(
                match_id=mid, reason="superseded_by_new_goal"
            )
            self._by_event[key] = session
            self._by_match.setdefault(mid, set()).add(key)
        # AF + DOM share this t0 (+5s / 5s); AF stops at 90s.
        self._af_observe_start(ev, event_key=key)
        self._nami_observe_start(session)

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
            f"(in_play+score×{GATE_CONFIRM_FRAMES}; VAR→no buy; "
            f"keep capturing until timeout)",
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
                sess = self._by_event.get(key)
                if sess is not None and not sess.finished:
                    sess.cancel_reason = reason
                    sess.cancel.set()
                    n += 1
            # Drop queued buys that have not been drained yet (same-tick race).
            revoked = self._revoke_pending_buys_locked(
                match_id=mid, reason=reason or "dqd_reversal"
            )
        try:
            from af_observe import get_active_observer as get_af

            af = get_af()
            if af is not None:
                af.cancel_match(mid, reason=reason or "dqd_reversal")
        except Exception:  # noqa: BLE001
            logger.debug("af observe cancel skipped", exc_info=True)
        if n or revoked:
            print(
                f"pitch-gate → CANCEL match_id={mid} sessions={n} "
                f"revoked_buys={revoked} reason={reason}",
                flush=True,
            )
        return n + revoked

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
                # Buy never executed — allow finish status to reflect cancel, not complete.
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
    ) -> bool:
        """Queue a one-shot buy signal without ending the capture session."""
        with self._lock:
            if (
                session.finished
                or session.buy_emitted
                or session.cancel.is_set()
                or session.var_seen
            ):
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
                f"· confirmed×{max(1, int(GATE_CONFIRM_FRAMES))} "
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
                    "var_seen": bool(session.var_seen),
                    "nami_id": session.nami_id or None,
                }
            )
        self._nami_observe_stop(session)

    def _af_observe_start(self, ev: dict[str, Any], *, event_key: str) -> None:
        """Kick AF score sampling for this goal. Observe-only, never fatal."""
        try:
            from af_observe import get_active_observer as get_af

            observer = get_af()
            if observer is None:
                return
            observer.start_session(ev, event_key=event_key)
        except Exception:  # noqa: BLE001
            logger.debug("af observe start skipped", exc_info=True)

    def _nami_observe_start(self, session: _GateSession) -> None:
        """Tap the nami live feed for this goal. Observe-only, never fatal."""
        try:
            from nami_observe import get_active_observer as get_nami

            observer = get_nami()
            if observer is None:
                return
            import dqd_live  # type: ignore
            import dqd_lib  # type: ignore

            url = dqd_live.animation_url_from_snapshot(session.match_id, self.root)
            nami_id = dqd_lib.nami_id_from_url(url) or ""
            if not nami_id:
                return
            session.nami_id = nami_id
            observer.observe_match(
                nami_id,
                {
                    "match_id": session.match_id,
                    "event_key": session.event_key,
                    "home": session.ev.get("home"),
                    "away": session.ev.get("away"),
                    "dqd_score": (
                        f"{session.ev.get('home_score')}-{session.ev.get('away_score')}"
                    ),
                },
                ttl_s=GATE_TIMEOUT_S,
            )
        except Exception:  # noqa: BLE001
            logger.debug("nami observe start skipped", exc_info=True)

    def _nami_observe_stop(self, session: _GateSession) -> None:
        if not session.nami_id:
            return
        try:
            from nami_observe import get_active_observer as get_nami

            observer = get_nami()
            if observer is not None:
                observer.release_match(session.nami_id)
        except Exception:  # noqa: BLE001
            logger.debug("nami observe stop skipped", exc_info=True)

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

        dom, err = reader.read()
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
            "ok": dom is not None,
            "error": err,
            "dom_state": dom,
            "gate": True,
        }
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
        return row, judged

    def _run_session(self, session: _GateSession, observer: Any) -> None:
        captured = 0
        sample_i = 0
        source = gate_source()
        reader: Any = None
        surface_info: dict[str, Any] = {}
        prev_clock: str | None = None
        try:
            # Load the page inside the first-frame delay so the wait is not
            # spent twice: the browser is ready by the time sampling starts.
            if source == "dom" and not session.cancel.is_set():
                reader, open_err, surface_info = self._open_dom_reader(session, observer)
                if reader is None:
                    self._finish_session(
                        session,
                        status="unavailable",
                        reason=f"dom_reader: {open_err}",
                        elapsed_s=round(time.monotonic() - session.t0_mono, 3),
                        frames=0,
                    )
                    print(
                        f"pitch-gate → DOM_UNAVAILABLE match_id={session.match_id} "
                        f"key={session.event_key} reason={open_err} (no buy)",
                        flush=True,
                    )
                    return
                prev_clock = self._baseline_clock(reader)
                if ref_screenshot_enabled():
                    self._enqueue_ref(
                        _RefShotJob(
                            op="open",
                            event_key=session.event_key,
                            page_url=str((surface_info or {}).get("page_url") or ""),
                            match_id=session.match_id,
                            root=self.root,
                        )
                    )

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

                if source == "dom":
                    row, judged = self._sample_dom(
                        session,
                        reader,
                        surface_info,
                        sample_i=sample_i,
                        elapsed_s=round(elapsed, 3),
                        prev_clock=prev_clock,
                    )
                    clock = str((judged or {}).get("dom_clock") or "")
                    if clock:
                        prev_clock = clock
                    has_reading = row.get("ok") is True
                else:
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
                    judged = None
                    has_reading = bool(row.get("ok") is True and row.get("frame_path"))

                try:
                    observer._write_rows([row])
                except Exception:  # noqa: BLE001
                    logger.exception("pitch-gate observe write failed")
                captured += 1

                if has_reading:
                    if source == "ocr":
                        row["require_score"] = bool(
                            session.require_score and GATE_REQUIRE_SCORE
                        )
                        judged = _judge_frame_sync(row)
                        _write_dom_vs_ocr(self.root, session, row, judged)
                    play_state = str((judged or {}).get("play_state") or "")
                    if _judge_shows_var(judged):
                        if not session.var_seen:
                            session.var_seen = True
                            print(
                                f"pitch-gate → VAR_VETO match_id={session.match_id} "
                                f"key={session.event_key} sample={sample_i} "
                                f"(no buy for this goal; keep capturing)",
                                flush=True,
                            )
                        if session.confirm_streak:
                            print(
                                f"pitch-gate → CONFIRM_RESET "
                                f"match_id={session.match_id} key={session.event_key} "
                                f"was={session.confirm_streak} play_state={play_state} "
                                f"reason=var",
                                flush=True,
                            )
                        session.confirm_streak = 0
                    elif play_state == "in_play":
                        if session.var_seen:
                            # Later in_play after VAR still must not buy.
                            session.confirm_streak = 0
                        else:
                            session.confirm_streak += 1
                            need = max(1, int(GATE_CONFIRM_FRAMES))
                            if session.confirm_streak < need:
                                print(
                                    f"pitch-gate → CONFIRM {session.confirm_streak}/{need} "
                                    f"match_id={session.match_id} key={session.event_key} "
                                    f"sample={sample_i} score="
                                    f"{session.ev.get('home_score')}-"
                                    f"{session.ev.get('away_score')}",
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
                                )
                                # Keep capturing until timeout; do not return.
                    else:
                        if session.confirm_streak:
                            print(
                                f"pitch-gate → CONFIRM_RESET "
                                f"match_id={session.match_id} key={session.event_key} "
                                f"was={session.confirm_streak} play_state={play_state}",
                                flush=True,
                            )
                        session.confirm_streak = 0

                if source == "dom" and ref_screenshot_enabled():
                    self._enqueue_ref(
                        _RefShotJob(
                            op="shot",
                            event_key=session.event_key,
                            page_url=str((surface_info or {}).get("page_url") or ""),
                            match_id=session.match_id,
                            sample_i=sample_i,
                            elapsed_s=round(elapsed, 3),
                            dqd_ts=str(session.ev.get("ts") or ""),
                            home=str(session.ev.get("home") or ""),
                            away=str(session.ev.get("away") or ""),
                            home_score=session.ev.get("home_score"),
                            away_score=session.ev.get("away_score"),
                            surface=(surface_info or {}).get("surface"),
                            nami_id=(surface_info or {}).get("nami_id"),
                            root=self.root,
                        )
                    )

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
        finally:
            if source == "dom" and ref_screenshot_enabled():
                self._enqueue_ref(
                    _RefShotJob(op="close", event_key=session.event_key, root=self.root)
                )
            if reader is not None:
                reader.close()


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


def dom_vs_ocr_path(root: Path) -> Path:
    import quote_lib as lib

    return lib.data_dir(root) / "dom_vs_ocr.jsonl"


def _write_dom_vs_ocr(
    root: Path,
    session: _GateSession,
    row: dict[str, Any],
    judged: dict[str, Any] | None,
) -> None:
    """Pair the DOM readout with the OCR verdict for the same frame.

    Research trail only: it answers whether reading the overlay text off the
    page could replace OCR, which cannot be settled without both sides of the
    same frame side by side.
    """
    try:
        import quote_lib as lib

        dom = row.get("dom_state")
        if not isinstance(dom, dict):
            dom = {}
        judged = judged if isinstance(judged, dict) else {}
        lib.append_jsonl(
            dom_vs_ocr_path(root),
            [
                {
                    "observed_at": lib.now_cn_iso(),
                    "match_id": session.match_id,
                    "event_key": session.event_key,
                    "nami_id": session.nami_id or None,
                    "sample_i": row.get("sample_i"),
                    "elapsed_s": row.get("elapsed_s"),
                    "expected_score": (
                        f"{session.ev.get('home_score')}-{session.ev.get('away_score')}"
                    ),
                    "page_url": row.get("page_url"),
                    "frame_kind": row.get("frame_kind"),
                    "dom_pop_box": dom.get("pop_box"),
                    "dom_pop_class": dom.get("pop_class"),
                    "dom_center_box": dom.get("center_box"),
                    "dom_marks": dom.get("marks"),
                    "ocr_play_state": judged.get("play_state"),
                    "ocr_stopped_reason": judged.get("stopped_reason"),
                    "ocr_confidence": judged.get("confidence"),
                    "ocr_score": judged.get("score") or judged.get("board_score"),
                }
            ],
        )
    except Exception:  # noqa: BLE001
        logger.debug("dom_vs_ocr write skipped", exc_info=True)


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
                "require_score": bool(row.get("require_score")),
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
        if _coordinator is not None:
            try:
                _coordinator._ref_stop.set()
                _coordinator._ref_q.put(None)
            except Exception:  # noqa: BLE001
                pass
        _coordinator = None
