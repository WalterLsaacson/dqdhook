#!/usr/bin/env python3
"""Observe-only API-Football score sampling beside pitch-gate DOM reads.

Pitch-gate calls ``sample_once`` on the same +0s / 5s / 120s clock as DOM.
The independent session thread is unused.

Never buys, never flattens, never runs OCR. Enabled when
``QUOTE_AF_OBSERVE=1`` (default) and ``apifootball_key`` is present in ``.env``.
Fixture mapping is cache-only (owned by apifootball-bridge sync/watch).
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import quote_lib as lib

logger = logging.getLogger("pm_quote.af_observe")

_AF_SCRIPTS = Path(__file__).resolve().parents[2] / "apifootball-bridge" / "scripts"
if str(_AF_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_AF_SCRIPTS))
import af_bridge_lib as aflib  # noqa: E402

# Same first delay / interval / timeout as pitch-gate DOM.
AF_FIRST_DELAY_S = 0.0
AF_INTERVAL_S = 5.0
AF_TIMEOUT_S = 120.0

_active: "AfScoreObserver | None" = None
_active_lock = threading.Lock()


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def observe_path(root: Path) -> Path:
    return lib.data_dir(root) / "af_observe.jsonl"


def set_active_observer(observer: "AfScoreObserver | None") -> None:
    global _active
    with _active_lock:
        _active = observer


def get_active_observer() -> "AfScoreObserver | None":
    with _active_lock:
        return _active


def try_create_observer(root: Path) -> "AfScoreObserver | None":
    if not _env_bool("QUOTE_AF_OBSERVE", True):
        return None
    env_path = Path(root) / ".env"
    try:
        aflib.load_af_key(env_path if env_path.is_file() else None)
    except Exception as e:  # noqa: BLE001
        print(f"af observe skipped (no apifootball_key: {e})", flush=True)
        return None
    return AfScoreObserver(root, env_path=env_path if env_path.is_file() else None)


@dataclass
class _AfSession:
    match_id: str
    event_key: str
    ev: dict[str, Any]
    t0_mono: float
    cancel: threading.Event = field(default_factory=threading.Event)
    thread: threading.Thread | None = None


class AfScoreObserver:
    """Per-goal AF score trail; drained only for research / board display."""

    def __init__(self, root: Path, *, env_path: Path | None = None) -> None:
        self.root = Path(root)
        self.env_path = env_path
        self._lock = threading.Lock()
        self._by_event: dict[str, _AfSession] = {}
        self._by_match: dict[str, set[str]] = {}
        self._af_key: str | None = None
        self._tls = threading.local()
        self._cache: dict[str, Any] | None = None
        self._cache_mtime: float | None = None
        self._started = False

    def start(self) -> None:
        self._started = True
        set_active_observer(self)
        print(
            f"af observe → {observe_path(self.root)} "
            f"(same tick as DOM · ≤{AF_TIMEOUT_S:.0f}s · score only · no trade)",
            flush=True,
        )

    def stop(self) -> None:
        self._started = False
        with self._lock:
            sessions = list(self._by_event.values())
            self._by_event.clear()
            self._by_match.clear()
        for s in sessions:
            s.cancel.set()
        set_active_observer(None)

    def start_session(self, ev: dict[str, Any], *, event_key: str) -> bool:
        if not self._started:
            return False
        mid = str(ev.get("match_id") or "").strip()
        key = str(event_key or "").strip()
        if not mid or not key:
            return False
        session = _AfSession(
            match_id=mid,
            event_key=key,
            ev=dict(ev),
            t0_mono=time.monotonic(),
        )
        with self._lock:
            prior = list(self._by_match.get(mid) or ())
            for pk in prior:
                old = self._by_event.get(pk)
                if old is not None:
                    old.cancel.set()
            self._by_event[key] = session
            self._by_match.setdefault(mid, set()).add(key)
        thread = threading.Thread(
            target=self._run_session,
            args=(session,),
            name=f"af-observe-{mid}",
            daemon=True,
        )
        session.thread = thread
        thread.start()
        return True

    def cancel_match(self, match_id: str, *, reason: str = "canceled") -> int:
        mid = str(match_id or "").strip()
        if not mid:
            return 0
        with self._lock:
            keys = list(self._by_match.get(mid) or ())
        n = 0
        for key in keys:
            with self._lock:
                s = self._by_event.get(key)
            if s is not None and not s.cancel.is_set():
                s.cancel.set()
                n += 1
        if n:
            print(
                f"af-observe → CANCEL match_id={mid} sessions={n} reason={reason}",
                flush=True,
            )
        return n

    def _client(self) -> aflib.AFClient:
        af = getattr(self._tls, "af", None)
        if af is None:
            if self._af_key is None:
                self._af_key = aflib.load_af_key(self.env_path)
            # Free-plan friendly spacing; observe is research so 6.5s is OK,
            # but we still *schedule* every 5s — coalesce/reuse when throttled.
            af = aflib.AFClient(self._af_key, min_interval_s=af_min_interval_s())
            self._tls.af = af
        return af

    def _fixture_cache(self) -> dict[str, Any]:
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

    def _poll(self, match_id: str) -> dict[str, Any]:
        return aflib.fetch_events_for_match_id(
            self._client(),
            match_id,
            cache=self._fixture_cache(),
            persist_burst=False,
            persist_cache=False,
            cache_only=True,
            http_timeout=12.0,
        )

    def sample_once(
        self,
        ev: dict[str, Any],
        *,
        event_key: str,
        sample_i: int,
        elapsed_s: float,
    ) -> dict[str, Any]:
        """One AF poll on the pitch-gate clock. Writes jsonl; never trades."""
        from af_referee import orient_af_goals_to_event

        mid = str(ev.get("match_id") or "").strip()
        dqd_h = ev.get("home_score")
        dqd_a = ev.get("away_score")
        row: dict[str, Any] = {
            "observed_at": lib.now_cn_iso(),
            "match_id": mid,
            "event_key": str(event_key or ""),
            "dqd_ts": str(ev.get("ts") or ""),
            "home": str(ev.get("home") or ""),
            "away": str(ev.get("away") or ""),
            "home_score": dqd_h,
            "away_score": dqd_a,
            "dqd_score": (
                f"{dqd_h}-{dqd_a}" if dqd_h is not None and dqd_a is not None else None
            ),
            "sample_i": int(sample_i),
            "elapsed_s": round(float(elapsed_s), 3),
            "source": "af",
            "gate": True,
            "observe_only": bool(ev.get("is_reversal")) or bool(ev.get("observe_only")),
            "is_reversal": bool(ev.get("is_reversal")),
            "ok": False,
            "error": None,
            "af_fixture_id": None,
            "af_home": None,
            "af_away": None,
            "af_home_score": None,
            "af_away_score": None,
            "af_score": None,
            "score_match": None,
        }
        try:
            out = self._poll(mid)
        except Exception as e:  # noqa: BLE001
            row["error"] = str(e).splitlines()[0][:160]
            out = {}
        entry = (out or {}).get("cache_entry") or {}
        goals = (out or {}).get("goals") or {}
        af_h_name = str(entry.get("af_home") or "")
        af_a_name = str(entry.get("af_away") or "")
        gh, ga = orient_af_goals_to_event(
            goals.get("home"),
            goals.get("away"),
            af_home=af_h_name,
            af_away=af_a_name,
            event_home=str(ev.get("home") or ""),
            event_away=str(ev.get("away") or ""),
        )
        row["af_fixture_id"] = (out or {}).get("af_fixture_id")
        row["af_home"] = af_h_name or None
        row["af_away"] = af_a_name or None
        row["ok"] = bool((out or {}).get("ok")) and gh is not None and ga is not None
        if not (out or {}).get("ok"):
            row["error"] = str((out or {}).get("error") or "af_poll_failed")[:160]
        if gh is not None and ga is not None:
            try:
                ih, ia = int(gh), int(ga)
                row["af_home_score"] = ih
                row["af_away_score"] = ia
                row["af_score"] = f"{ih}-{ia}"
                if dqd_h is not None and dqd_a is not None:
                    row["score_match"] = ih == int(dqd_h) and ia == int(dqd_a)
            except (TypeError, ValueError):
                row["ok"] = False
                row["error"] = "af_score_parse_failed"
        try:
            lib.append_jsonl(observe_path(self.root), [row])
        except Exception:  # noqa: BLE001
            logger.exception("af observe write failed")
        return row

    def _run_session(self, session: _AfSession) -> None:
        from af_referee import orient_af_goals_to_event

        sample_i = 0
        captured = 0
        try:
            first_t = session.t0_mono + max(0.0, float(AF_FIRST_DELAY_S))
            while not session.cancel.is_set():
                now = time.monotonic()
                if now - session.t0_mono > AF_TIMEOUT_S + 1e-9:
                    break
                if now >= first_t:
                    break
                time.sleep(min(0.2, max(0.0, first_t - now)))

            while not session.cancel.is_set():
                elapsed = time.monotonic() - session.t0_mono
                if elapsed > AF_TIMEOUT_S + 1e-9:
                    break

                dqd_h = session.ev.get("home_score")
                dqd_a = session.ev.get("away_score")
                row: dict[str, Any] = {
                    "observed_at": lib.now_cn_iso(),
                    "match_id": session.match_id,
                    "event_key": session.event_key,
                    "dqd_ts": str(session.ev.get("ts") or ""),
                    "home": str(session.ev.get("home") or ""),
                    "away": str(session.ev.get("away") or ""),
                    "home_score": dqd_h,
                    "away_score": dqd_a,
                    "dqd_score": (
                        f"{dqd_h}-{dqd_a}"
                        if dqd_h is not None and dqd_a is not None
                        else None
                    ),
                    "sample_i": sample_i,
                    "elapsed_s": round(elapsed, 3),
                    "source": "af",
                    "gate": True,
                    "observe_only": bool(session.ev.get("is_reversal"))
                    or bool(session.ev.get("observe_only")),
                    "is_reversal": bool(session.ev.get("is_reversal")),
                    "ok": False,
                    "error": None,
                    "af_fixture_id": None,
                    "af_home": None,
                    "af_away": None,
                    "af_home_score": None,
                    "af_away_score": None,
                    "af_score": None,
                    "score_match": None,
                }
                try:
                    out = self._poll(session.match_id)
                except Exception as e:  # noqa: BLE001
                    row["error"] = str(e).splitlines()[0][:160]
                    out = {}
                entry = (out or {}).get("cache_entry") or {}
                goals = (out or {}).get("goals") or {}
                af_h_name = str(entry.get("af_home") or "")
                af_a_name = str(entry.get("af_away") or "")
                gh, ga = orient_af_goals_to_event(
                    goals.get("home"),
                    goals.get("away"),
                    af_home=af_h_name,
                    af_away=af_a_name,
                    event_home=str(session.ev.get("home") or ""),
                    event_away=str(session.ev.get("away") or ""),
                )
                row["af_fixture_id"] = (out or {}).get("af_fixture_id")
                row["af_home"] = af_h_name or None
                row["af_away"] = af_a_name or None
                row["ok"] = bool((out or {}).get("ok")) and gh is not None and ga is not None
                if not (out or {}).get("ok"):
                    row["error"] = str((out or {}).get("error") or "af_poll_failed")[:160]
                if gh is not None and ga is not None:
                    try:
                        ih, ia = int(gh), int(ga)
                        row["af_home_score"] = ih
                        row["af_away_score"] = ia
                        row["af_score"] = f"{ih}-{ia}"
                        if dqd_h is not None and dqd_a is not None:
                            row["score_match"] = ih == int(dqd_h) and ia == int(dqd_a)
                    except (TypeError, ValueError):
                        row["ok"] = False
                        row["error"] = "af_score_parse_failed"

                try:
                    lib.append_jsonl(observe_path(self.root), [row])
                except Exception:  # noqa: BLE001
                    logger.exception("af observe write failed")
                captured += 1
                sample_i += 1

                next_t = (
                    session.t0_mono
                    + max(0.0, float(AF_FIRST_DELAY_S))
                    + sample_i * AF_INTERVAL_S
                )
                if next_t - session.t0_mono > AF_TIMEOUT_S + 1e-9:
                    break
                while not session.cancel.is_set():
                    now = time.monotonic()
                    if now >= next_t:
                        break
                    if now - session.t0_mono > AF_TIMEOUT_S:
                        break
                    time.sleep(min(0.2, max(0.0, next_t - now)))

            print(
                f"af-observe → DONE match_id={session.match_id} key={session.event_key} "
                f"samples={captured} "
                f"{'canceled' if session.cancel.is_set() else 'timeout'}",
                flush=True,
            )
        except Exception:  # noqa: BLE001
            logger.exception(
                "af observe session failed match=%s key=%s",
                session.match_id,
                session.event_key,
            )
        finally:
            with self._lock:
                self._by_event.pop(session.event_key, None)
                keys = self._by_match.get(session.match_id)
                if keys is not None:
                    keys.discard(session.event_key)
                    if not keys:
                        self._by_match.pop(session.match_id, None)


def af_min_interval_s() -> float:
    """Optional AF HTTP spacing; default 0 so the 5s schedule is not serialized."""
    raw = os.getenv("QUOTE_AF_MIN_INTERVAL_S")
    if raw is None or str(raw).strip() == "":
        return 0.0
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return 0.0
