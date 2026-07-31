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


def test_confirm_check_times() -> None:
    print("test_confirm_check_times")
    checks = ref.confirm_check_times(120.0)
    check("starts at 5", checks[0] == 5.0, str(checks[:3]))
    check("has 7", 7.0 in checks)
    check("has 60", 60.0 in checks)
    check("has 65", 65.0 in checks)
    check("has 120", 120.0 in checks)
    # no 0.5s dense junk in middle
    between = [c for c in checks if 20 < c < 40]
    check("mid spacing ~2s", all(
        abs(between[i+1] - between[i] - 2.0) < 0.01 for i in range(len(between)-1)
    ), str(between[:5]))
    late = [c for c in checks if c > 60]
    check("late spacing ~5s", all(
        abs(late[i+1] - late[i] - 5.0) < 0.01 for i in range(len(late)-1)
    ), str(late[:4]))
    check("count ~40", 35 <= len(checks) <= 45, str(len(checks)))


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


def main() -> int:
    test_classifiers()
    test_af_score_satisfies()
    test_await_via_bridge_fn()
    test_await_af_ahead()
    test_async_submit_drain()
    test_await_timeout()
    test_cache_miss_no_spin()
    test_confirm_check_times()
    test_apply_score()
    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
