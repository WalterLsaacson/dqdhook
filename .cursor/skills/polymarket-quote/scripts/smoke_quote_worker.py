#!/usr/bin/env python3
"""Smoke: CLOB quote/rest run on a worker; watch tick still start_gates immediately."""

from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path
from typing import Any
from unittest.mock import patch

_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import pitch_gate as pg  # noqa: E402
import quote_lib as lib  # noqa: E402
import quote_worker as qw  # noqa: E402


def _goal(match_id: str, ts: str) -> dict[str, Any]:
    return {
        "type": "score_change",
        "ts": ts,
        "match_id": match_id,
        "home": "Home",
        "away": "Away",
        "home_score": 1,
        "away_score": 0,
        "prev": {"home": 0, "away": 0},
        "curr": {"home": 1, "away": 0},
        "is_goal": True,
        "polymarket": {"event_id": "e1", "slug": "x"},
    }


def main() -> int:
    qw.reset_quote_worker_for_tests()
    pg.reset_coordinator_for_tests()
    try:
        return _run()
    finally:
        qw.reset_quote_worker_for_tests()
        pg.reset_coordinator_for_tests()


def _run() -> int:

    quoted: list[str] = []
    started: list[float] = []

    def _slow_quote(root, ev, **_kw):  # noqa: ANN001
        quoted.append(str(ev.get("match_id")))
        time.sleep(0.35)
        return {
            "quoted_at": lib.now_cn_iso(),
            "trigger": ev.get("type"),
            "mode": "pitch_gate_confirmed",
            "match_id": ev.get("match_id"),
            "home": ev.get("home"),
            "away": ev.get("away"),
            "home_score": ev.get("home_score"),
            "away_score": ev.get("away_score"),
            "count": 1,
            "opportunity_count": 0,
            "quotes": [],
        }

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "data" / "pm-quote").mkdir(parents=True)
        worker = qw.start_quote_worker(root, trade_executor=None, market_cache=None)
        worker.idle_housekeep_s = 99.0
        ev = _goal("m_slow", "2026-08-24T12:00:00+08:00")
        key = lib.event_key(ev)
        with patch.object(lib, "quote_bridge_event", side_effect=_slow_quote):
            with patch.object(lib, "load_bridge_quote_events", return_value=([], 0)):
                t0 = time.monotonic()
                # Simulate pitch-gate drain submitting a buy while CLOB is slow.
                worker.submit_quote(ev, event_key=key, extra={"mode": "pitch_gate_confirmed"})
                # Next tick must return immediately (start_gate path).
                bundles = lib.process_bridge_events(
                    root,
                    events_override=[_goal("m_next", "2026-08-24T12:00:01+08:00")],
                )
                started.append(time.monotonic() - t0)
        if started[0] >= 0.25:
            print(f"FAIL tick blocked on CLOB: {started[0]:.3f}s")
            qw.reset_quote_worker_for_tests()
            return 1
        worker.wait_idle(timeout=2.0)
        results = worker.drain_results()
        if not quoted:
            print("FAIL worker never quoted")
            qw.reset_quote_worker_for_tests()
            return 1
        if not results:
            print("FAIL no worker results")
            qw.reset_quote_worker_for_tests()
            return 1

        # Reverse must skip a pending quote.
        ev2 = _goal("m_rev", "2026-08-24T12:01:00+08:00")
        key2 = lib.event_key(ev2)

        def _never(_root, _ev, **_kw):  # noqa: ANN001
            raise AssertionError("revoked quote must not call CLOB")

        with patch.object(lib, "quote_bridge_event", side_effect=_never):
            worker.revoke_event(key2)
            worker.submit_quote(ev2, event_key=key2)
            worker.wait_idle(timeout=2.0)
        skipped = [r for r in worker.drain_results() if r.skipped]
        if not skipped:
            print("FAIL revoked quote was not skipped")
            qw.reset_quote_worker_for_tests()
            return 1

        qw.reset_quote_worker_for_tests()
        worker = qw.start_quote_worker(root, trade_executor=None, market_cache=None)
        worker.idle_housekeep_s = 99.0
        old_ev = _goal("m_same", "2026-08-24T12:00:00+08:00")
        new_ev = _goal("m_same", "2026-08-24T12:02:00+08:00")
        old_key = lib.event_key(old_ev)
        new_key = lib.event_key(new_ev)
        quoted_keys: list[str] = []

        blocker = _goal("m_block", "2026-08-24T12:00:00+08:00")
        block_key = lib.event_key(blocker)

        def _track(_root, ev, **_kw):  # noqa: ANN001
            quoted_keys.append(lib.event_key(ev))
            if str(ev.get("match_id")) == "m_block":
                time.sleep(0.25)
            return {
                "quoted_at": lib.now_cn_iso(),
                "trigger": ev.get("type"),
                "mode": "pitch_gate_confirmed",
                "match_id": ev.get("match_id"),
                "count": 1,
                "opportunity_count": 0,
                "quotes": [],
            }

        with patch.object(lib, "quote_bridge_event", side_effect=_track):
            worker.submit_quote(blocker, event_key=block_key)
            deadline = time.time() + 1.0
            while not worker.is_in_flight(block_key) and time.time() < deadline:
                time.sleep(0.01)
            worker.submit_quote(old_ev, event_key=old_key)
            worker.submit_rest_cancel("m_same", reason="dqd_reversal")
            worker.submit_quote(new_ev, event_key=new_key)
            worker.wait_idle(timeout=2.0)
        if new_key not in quoted_keys:
            print(f"FAIL re-award was not quoted: {quoted_keys}")
            qw.reset_quote_worker_for_tests()
            return 1
        if old_key in quoted_keys:
            print(f"FAIL reversed old key still quoted: {quoted_keys}")
            qw.reset_quote_worker_for_tests()
            return 1

        order: list[str] = []

        class _SlowEx:
            def retry_pending_flattens(self) -> list[Any]:
                order.append("flatten_retry_start")
                time.sleep(0.35)
                order.append("flatten_retry_end")
                return []

            def reconcile_rest_orders(self) -> list[Any]:
                order.append("reconcile")
                return []

            def cancel_rest_orders_for_match(self, _mid: str, *, reason: str) -> None:
                order.append("rest_cancel")

        qw.reset_quote_worker_for_tests()
        worker = qw.start_quote_worker(root, trade_executor=_SlowEx(), market_cache=None)
        worker.idle_housekeep_s = 99.0
        worker._schedule_housekeep()
        deadline = time.time() + 2.0
        while "flatten_retry_start" not in order and time.time() < deadline:
            time.sleep(0.01)
        if "flatten_retry_start" not in order:
            print("FAIL housekeep never started")
            qw.reset_quote_worker_for_tests()
            return 1
        worker.submit_rest_cancel("m_hk", reason="dqd_reversal")
        worker.wait_idle(timeout=3.0)
        if "rest_cancel" not in order or "reconcile" not in order:
            print(f"FAIL housekeep/rest_cancel order incomplete: {order}")
            qw.reset_quote_worker_for_tests()
            return 1
        if order.index("rest_cancel") > order.index("reconcile"):
            print(f"FAIL housekeep blocked rest_cancel: {order}")
            qw.reset_quote_worker_for_tests()
            return 1

        qw.reset_quote_worker_for_tests()
        modes = [b.get("mode") for b in bundles]
        print(
            f"ok: clob worker (tick {started[0]:.3f}s, quoted={quoted}, "
            f"next_tick_modes={modes}, skipped_revoked={len(skipped)}, "
            f"reaward={quoted_keys}, housekeep_order={order})"
        )
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
