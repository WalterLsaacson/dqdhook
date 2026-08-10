#!/usr/bin/env python3
"""Smoke tests for AF referee gate (fake events_fn, no network)."""

from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path
from typing import Any

_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import af_referee as ref  # noqa: E402

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  OK  {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}" + (f": {detail}" if detail else ""))


def test_classifiers() -> None:
    print("test_classifiers")
    goal = {
        "type": "score_change",
        "is_goal": True,
        "is_reversal": False,
        "prev": {"home": 0, "away": 0},
        "curr": {"home": 1, "away": 0},
        "home_score": 1,
        "away_score": 0,
        "match_id": "m1",
    }
    rev = {
        "type": "score_change",
        "is_goal": False,
        "is_reversal": True,
        "prev": {"home": 1, "away": 0},
        "curr": {"home": 0, "away": 0},
        "home_score": 0,
        "away_score": 0,
    }
    check("goal_up", ref.event_is_goal_up(goal))
    check("not reversal on goal", not ref.event_is_reversal(goal))
    check("reversal", ref.event_is_reversal(rev))
    check("not goal_up on reversal", not ref.event_is_goal_up(rev))
    check("target", ref.target_score_from_event(goal) == (1, 0))
    check("baseline", ref.baseline_score_from_event(goal) == (0, 0))


def test_af_score_satisfies() -> None:
    print("test_af_score_satisfies")
    ok, truth = ref.af_score_satisfies((1, 0), (1, 0), baseline=(0, 0))
    check("exact", ok and truth == (1, 0))
    ok, truth = ref.af_score_satisfies((2, 0), (1, 0), baseline=(0, 0))
    check("af ahead accepted", ok and truth == (2, 0), str(truth))
    ok, _ = ref.af_score_satisfies((0, 0), (1, 0), baseline=(0, 0))
    check("behind rejected", not ok)


def test_await_via_bridge_fn() -> None:
    print("test_await_via_bridge_fn")
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        root = Path(td)
        seq: list[dict[str, Any]] = [
            {"ok": True, "af_fixture_id": 99, "goals": {"home": 0, "away": 0}, "events": []},
            {
                "ok": True,
                "af_fixture_id": 99,
                "goals": {"home": 1, "away": 0},
                "events": [{"type": "Goal"}],
                "burst_dir": None,
            },
            # persist confirm burst fetch
            {
                "ok": True,
                "af_fixture_id": 99,
                "goals": {"home": 1, "away": 0},
                "events": [{"type": "Goal"}],
                "burst_dir": str(root / "bursts" / "m1_confirm"),
            },
        ]

        def events_fn(_mid: str, persist_burst: bool = False) -> dict[str, Any]:
            if not seq:
                return {"ok": True, "goals": {"home": 0, "away": 0}}
            return seq.pop(0)

        referee = ref.AfReferee(root, poll_s=0.01, timeout_s=2.0, events_fn=events_fn, poll_schedule=False)
        out = referee.await_score("m1", (1, 0), baseline=(0, 0))
        check("confirmed", out.get("confirmed") is True, str(out))
        check("via bridge", out.get("via") == "apifootball-bridge")
        check("polls>=2", int(out.get("polls") or 0) >= 2, str(out.get("polls")))
        check("persist async", out.get("persist") == "async")
        stored = ref.get_confirmed_score(root, "m1")
        check("store 1-0", stored == (1, 0), str(stored))
        # Burst may land asynchronously after confirm return.
        time.sleep(0.2)


def test_await_af_ahead() -> None:
    print("test_await_af_ahead")
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        root = Path(td)

        def events_fn(_mid: str, persist_burst: bool = False) -> dict[str, Any]:
            row = {
                "ok": True,
                "af_fixture_id": 1,
                "goals": {"home": 2, "away": 0},
                "events": [],
            }
            if persist_burst:
                row["burst_dir"] = str(root / "b")
            return row

        referee = ref.AfReferee(root, poll_s=0.01, timeout_s=1.0, events_fn=events_fn, poll_schedule=False)
        out = referee.await_score("m3", (1, 0), baseline=(0, 0))
        check("ahead confirmed", out.get("confirmed") is True, str(out))
        check("truth 2-0", out.get("goals") == {"home": 2, "away": 0}, str(out.get("goals")))


def test_async_submit_drain() -> None:
    print("test_async_submit_drain")
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        root = Path(td)
        calls = {"n": 0}

        def events_fn(_mid: str, persist_burst: bool = False) -> dict[str, Any]:
            calls["n"] += 1
            row = {
                "ok": True,
                "af_fixture_id": 5,
                "goals": {"home": 1, "away": 0},
                "events": [],
            }
            if persist_burst:
                row["burst_dir"] = str(root / "x")
            return row

        referee = ref.AfReferee(root, poll_s=0.01, timeout_s=2.0, events_fn=events_fn, poll_schedule=False)
        ev = {
            "type": "score_change",
            "match_id": "m9",
            "is_goal": True,
            "prev": {"home": 0, "away": 0},
            "curr": {"home": 1, "away": 0},
            "home_score": 1,
            "away_score": 0,
        }
        ok = referee.submit("ek1", ev, (1, 0))
        check("submitted", ok)
        check("pending", "ek1" in referee.pending_event_keys())
        done = []
        for _ in range(50):
            done = referee.drain_done()
            if done:
                break
            time.sleep(0.02)
        check("drained", len(done) == 1, str(done))
        check("drained confirmed", done[0]["gate"].get("confirmed") is True)
        check("pending clear", len(referee.pending_event_keys()) == 0)


def test_await_timeout() -> None:
    print("test_await_timeout")
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)

        def events_fn(_mid: str, persist_burst: bool = False) -> dict[str, Any]:
            return {
                "ok": True,
                "af_fixture_id": 7,
                "goals": {"home": 0, "away": 0},
                "events": [],
            }

        referee = ref.AfReferee(root, poll_s=0.01, timeout_s=0.05, events_fn=events_fn, poll_schedule=False)
        out = referee.await_score("m2", (1, 0), baseline=(0, 0))
        check("not confirmed", out.get("confirmed") is False)
        check(
            "timeout error",
            "timeout" in str(out.get("error") or "").lower()
            or out.get("error") == "af_confirm_timeout",
            str(out.get("error")),
        )
        check("no store", ref.get_confirmed_score(root, "m2") is None)


def test_cache_miss_no_spin() -> None:
    print("test_cache_miss_no_spin")
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        calls = {"n": 0}

        def events_fn(_mid: str, persist_burst: bool = False) -> dict[str, Any]:
            calls["n"] += 1
            return {
                "ok": False,
                "error": "af_fixture_unresolved_ttl",
                "goals": {"home": None, "away": None},
            }

        referee = ref.AfReferee(root, poll_s=0.5, timeout_s=30.0, events_fn=events_fn, poll_schedule=False)
        t0 = time.monotonic()
        out = referee.await_score("m_miss", (1, 0), baseline=(0, 0))
        elapsed = time.monotonic() - t0
        check("not confirmed", out.get("confirmed") is False)
        check("error ttl", out.get("error") == "af_fixture_unresolved_ttl", str(out.get("error")))
        check("single poll", int(out.get("polls") or 0) == 1, str(out.get("polls")))
        check("one events call", calls["n"] == 1, str(calls))
        check("no 30s spin", elapsed < 2.0, f"{elapsed:.2f}s")


def test_cache_miss_wait_until_timeout() -> None:
    print("test_cache_miss_wait_until_timeout")
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        calls = {"n": 0}

        def events_fn(_mid: str, persist_burst: bool = False) -> dict[str, Any]:
            calls["n"] += 1
            return {
                "ok": False,
                "error": "af_fixture_unresolved_ttl",
                "goals": {"home": None, "away": None},
            }

        referee = ref.AfReferee(
            root, poll_s=0.05, timeout_s=0.2, events_fn=events_fn, poll_schedule=False
        )
        t0 = time.monotonic()
        out = referee.await_score(
            "m_wait", (1, 0), baseline=(0, 0), wait_cache=True
        )
        elapsed = time.monotonic() - t0
        check("not confirmed", out.get("confirmed") is False)
        check("error still miss", "af_fixture" in str(out.get("error") or ""))
        check("multiple polls", int(out.get("polls") or 0) >= 2, str(out.get("polls")))
        check("waited near timeout", elapsed >= 0.15, f"{elapsed:.2f}s")
        check("many events calls", calls["n"] >= 2, str(calls))


def test_confirm_check_times() -> None:
    print("test_confirm_check_times")
    checks = ref.confirm_check_times(90.0)
    check("starts at 3", checks[0] == 3.0, str(checks[:3]))
    check("no immediate 0", 0.0 not in checks)
    check("has 4", 4.0 in checks)
    check("has 59", 59.0 in checks)
    check("has 60", 60.0 in checks)
    check("has 62", 62.0 in checks)
    check("has 90", 90.0 in checks)
    check("no 61 on late grid", 61.0 not in checks)
    # Early phase: 1s spacing before 60
    early = [c for c in checks if c < 60]
    check(
        "early spacing ~1s",
        all(abs(early[i + 1] - early[i] - 1.0) < 0.01 for i in range(len(early) - 1)),
        str(early[:5]),
    )
    # Late phase: 2s spacing from 60
    late = [c for c in checks if c >= 60]
    check(
        "late spacing ~2s",
        late == [60.0, 62.0, 64.0, 66.0, 68.0, 70.0, 72.0, 74.0, 76.0, 78.0, 80.0, 82.0, 84.0, 86.0, 88.0, 90.0],
        str(late),
    )
    # 3..59 @1s → 57 ticks; 60..90 @2s → 16 ticks → 73
    check("count 73", len(checks) == 73, str(len(checks)))
    check("last is timeout", checks[-1] == 90.0, str(checks[-1]))
    short = ref.confirm_check_times(2.5, first_delay_s=0.0, period_s=1.0, late_after_s=10.0)
    check("short ends ≤timeout", short[-1] <= 2.5 + 1e-9, str(short))
    check("short no past timeout", all(c <= 2.5 + 1e-9 for c in short), str(short))
    label = ref.schedule_label()
    check("label mentions 3s→1s→60s→2s→90s", "3s→every 1s→60s→every 2s→90s" == label, label)
    check(
        "default AF min interval off (schedule-first)",
        abs(ref.af_min_interval_s() - 0.0) < 1e-9,
        str(ref.af_min_interval_s()),
    )
    # Missed ticks collapse to latest overdue, then resume future cadence.
    checks = [5.0, 7.0, 9.0, 11.0, 13.0]
    check("advance at t=5 stays 0", ref.advance_schedule_index(checks, 0, 5.0) == 0)
    check("advance at t=8 → slot 7", ref.advance_schedule_index(checks, 0, 8.0) == 1)
    check("advance at t=12 → slot 11", ref.advance_schedule_index(checks, 0, 12.0) == 3)
    check("advance from mid index", ref.advance_schedule_index(checks, 2, 12.0) == 3)


def test_schedule_cadence_wall_times() -> None:
    """Polls land on the tiered checkpoints (not a shared min-interval queue)."""
    print("test_schedule_cadence_wall_times")
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        root = Path(td)
        hits: list[float] = []
        t_start = {"v": 0.0}

        def events_fn(_mid: str, persist_burst: bool = False, **_k: Any) -> dict[str, Any]:
            hits.append(time.monotonic() - t_start["v"])
            return {
                "ok": True,
                "goals": {"home": 0, "away": 0},
                "af_fixture_id": 1,
            }

        referee = ref.AfReferee(
            root,
            timeout_s=0.45,
            events_fn=events_fn,
            poll_schedule=True,
            first_delay_s=0.05,
            period_s=0.08,
            late_after_s=0.25,
            late_period_s=0.1,
            max_workers=2,
        )
        expect = ref.confirm_check_times(
            0.45,
            first_delay_s=0.05,
            period_s=0.08,
            late_after_s=0.25,
            late_period_s=0.1,
        )
        t_start["v"] = time.monotonic()
        out = referee.await_score("m1", (1, 0), baseline=(0, 0), wait_cache=True)
        check("timed out (score never matches)", not out.get("confirmed"), str(out))
        # Ignore any overrun past the inclusive timeout tick (scheduler may
        # finish one in-flight GET slightly after deadline).
        in_window = [h for h in hits if h <= 0.45 + 0.05]
        check(
            "polled near every checkpoint",
            len(in_window) >= max(1, len(expect) - 1),
            f"hits={in_window} expect={expect} raw={hits}",
        )
        for h in in_window:
            nearest = min(abs(h - e) for e in expect)
            check(
                f"hit {h:.3f}s near a tick",
                nearest < 0.08,
                f"nearest={nearest:.3f} expect={expect}",
            )
            if nearest >= 0.08:
                break
        gaps = [in_window[i + 1] - in_window[i] for i in range(len(in_window) - 1)]
        check("no multi-second throttle gap", all(g < 0.35 for g in gaps), str(gaps))
        check(
            "polls matches schedule scale",
            int(out.get("polls") or 0) >= len(expect) - 1,
            str(out.get("polls")),
        )


def test_transient_network_retry() -> None:
    print("test_transient_network_retry")
    check(
        "ssl dict",
        ref._is_transient_af_error(
            {"ok": False, "exception": "<urlopen error [SSL: UNEXPECTED_EOF_WHILE_READING]>"}
        ),
    )
    check(
        "ssl nested errors (real AF shape)",
        ref._is_transient_af_error(
            {
                "ok": False,
                "http_status": None,
                "errors": {
                    "exception": "<urlopen error [SSL: UNEXPECTED_EOF_WHILE_READING] EOF occurred>"
                },
            }
        ),
    )
    check(
        "read timeout",
        ref._is_transient_af_error({"ok": False, "error": "The read operation timed out"}),
    )
    check("not cache miss", not ref._is_transient_af_error({"error": "af_fixture_unresolved"}))
    check(
        "not confirm-timeout label",
        not ref._is_transient_af_error("af_confirm_timeout"),
    )
    check("429 still rate", ref._is_rate_limited({"http_status": 429}))

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        root = Path(td)
        calls = {"n": 0}

        def events_fn(_mid: str, persist_burst: bool = False) -> dict[str, Any]:
            calls["n"] += 1
            if calls["n"] < 4:
                # Mirror af_bridge_lib HTTP failure shape.
                return {
                    "ok": False,
                    "http_status": None,
                    "errors": {
                        "exception": "<urlopen error [SSL: UNEXPECTED_EOF_WHILE_READING]>"
                    },
                    "goals": {"home": None, "away": None},
                }
            return {
                "ok": True,
                "af_fixture_id": 42,
                "goals": {"home": 1, "away": 0},
                "events": [],
            }

        referee = ref.AfReferee(
            root, poll_s=0.05, timeout_s=2.0, events_fn=events_fn, poll_schedule=False
        )
        out = referee.await_score("m_ssl", (1, 0), baseline=(0, 0))
        check("recovered after ssl", out.get("confirmed") is True, str(out))
        check("retried several times", calls["n"] >= 4, str(calls))

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        root = Path(td)

        def events_fn(_mid: str, persist_burst: bool = False) -> dict[str, Any]:
            return {
                "ok": True,
                "af_fixture_id": 7,
                "goals": {"home": 0, "away": 0},
                "events": [],
            }

        referee = ref.AfReferee(
            root, poll_s=0.01, timeout_s=0.05, events_fn=events_fn, poll_schedule=False
        )
        out = referee.await_score("m_clean_to", (1, 0), baseline=(0, 0))
        check("score-miss timeout not transient flag", out.get("transient_network") is not True)
        check(
            "score-miss error label",
            out.get("error") == "af_confirm_timeout"
            or "timeout" in str(out.get("error") or "").lower(),
            str(out.get("error")),
        )


def test_apply_score() -> None:
    print("test_apply_score")
    ev = {
        "type": "score_change",
        "home_score": 2,
        "away_score": 0,
        "curr": {"home": 2, "away": 0},
    }
    out = ref.apply_af_score_to_event(ev, home=1, away=0)
    check("rewrote", out["home_score"] == 1 and out["away_score"] == 0)
    check("source", out.get("score_source") == "api_football")
    # AF fixture Pathum home 1-3, event labeled Villa home → consumer sees 3-1
    gh, ga = ref.orient_af_goals_to_event(
        1,
        3,
        af_home="BG Pathum United",
        af_away="Aston Villa",
        event_home="Aston Villa",
        event_away="BG Pathum United",
    )
    check("AF→PM orient", (gh, ga) == (3, 1), f"got {gh}-{ga}")
    same = ref.orient_af_goals_to_event(
        2,
        1,
        af_home="Aston Villa",
        af_away="BG Pathum United",
        event_home="Aston Villa",
        event_away="BG Pathum United",
    )
    check("same orientation passthrough", same == (2, 1), str(same))


def main() -> int:
    test_classifiers()
    test_af_score_satisfies()
    test_await_via_bridge_fn()
    test_await_af_ahead()
    test_async_submit_drain()
    test_await_timeout()
    test_cache_miss_no_spin()
    test_cache_miss_wait_until_timeout()
    test_confirm_check_times()
    test_schedule_cadence_wall_times()
    test_transient_network_retry()
    test_apply_score()
    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
