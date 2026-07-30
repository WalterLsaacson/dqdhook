#!/usr/bin/env python3
"""Smoke: in-process bridge event queue + async persist + live books_once."""

from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path
from typing import Any
from unittest.mock import patch

_SCRIPTS = Path(__file__).resolve().parent
_BRIDGE = _SCRIPTS.parents[1] / "match-bridge" / "scripts"
for p in (_SCRIPTS, _BRIDGE):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import af_referee as ref  # noqa: E402
import bridge_lib as bl  # noqa: E402
import quote_lib as lib  # noqa: E402

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


def test_bridge_event_queue_before_disk() -> None:
    print("test_bridge_event_queue_before_disk")
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "data" / "bridge").mkdir(parents=True)
        (root / "data" / "polymarket").mkdir(parents=True)
        (root / "data" / "snapshot.json").write_text(
            json.dumps({"tab": "full", "matches": []}), encoding="utf-8"
        )
        (root / "data" / "polymarket" / "snapshot.json").write_text(
            json.dumps({"matches": []}), encoding="utf-8"
        )

        rt = bl.BridgeRuntime(root, async_persist=True)
        paired = [
            {
                "score": 1.0,
                "finished": False,
                "dongqiudi": {
                    "id": "m1",
                    "home": "Home FC",
                    "away": "Away FC",
                    "home_score": 1,
                    "away_score": 0,
                    "status": "Playing 10'",
                    "status_raw": "Playing",
                    "official_clock": "10'",
                },
                "polymarket": {
                    "event_id": "e1",
                    "slug": "home-vs-away",
                    "url": "https://x",
                    "home": "Home FC",
                    "away": "Away FC",
                    "league": "Test",
                    "condition_ids": [],
                    "market_refs": [],
                },
            }
        ]

        # Seed baseline then bump score via patched match_fixtures.
        with patch.object(bl, "match_fixtures", return_value=paired):
            rt.rematch()
        check("seed: no queue events", rt.drain_event_queue() == [])

        paired[0]["dongqiudi"]["home_score"] = 2
        with patch.object(bl, "match_fixtures", return_value=paired):
            payload2 = rt.rematch()
        evs = payload2.get("events") or []
        check("goal event emitted", len(evs) >= 1, str(evs))
        qevs = rt.drain_event_queue()
        check("memory queue has event", len(qevs) >= 1, str(qevs))
        check(
            "queue type score_change",
            any(e.get("type") == "score_change" for e in qevs),
        )
        deadline = time.time() + 2.0
        path = root / "data" / "bridge" / "events.jsonl"
        while time.time() < deadline and not (
            path.is_file() and path.stat().st_size > 0
        ):
            time.sleep(0.05)
        check("async events.jsonl written", path.is_file() and path.stat().st_size > 0)
        rt.stop()


def test_af_confirm_no_sync_second_fetch() -> None:
    print("test_af_confirm_no_sync_second_fetch")
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        root = Path(td)
        calls = {"n": 0}

        def fake_events(match_id: str, persist_burst: bool = False) -> dict[str, Any]:
            calls["n"] += 1
            calls["last_persist"] = persist_burst
            return {
                "ok": True,
                "af_fixture_id": 1,
                "goals": {"home": 1, "away": 0},
                "events": [],
            }

        referee = ref.AfReferee(
            root, poll_s=0.05, timeout_s=2.0, events_fn=fake_events
        )
        out = referee.await_score("m1", (1, 0), baseline=(0, 0))
        check("confirmed", out.get("confirmed") is True)
        check("persist async flag", out.get("persist") == "async")
        # Hot path should call events_fn once (poll); burst fetch is async.
        check("hot path single poll", calls["n"] == 1, str(calls))
        check(
            "memory score",
            ref.get_confirmed_score(root, "m1") == (1, 0),
        )
        # Allow async side effects (may add another poll).
        time.sleep(0.3)


def test_live_books_once() -> None:
    print("test_live_books_once")
    tokens = [
        {
            "token_id": "t1",
            "family": "totals",
            "settlement": "yes",
            "outcome": "Over",
            "market_key": "tot",
        },
        {
            "token_id": "t2",
            "family": "exact_score",
            "settlement": "yes",
            "outcome": "1-0",
            "market_key": "ex",
        },
    ]
    books_calls: list[list[str]] = []

    def fake_books(ids, proxy=None):
        books_calls.append(list(ids))
        return {
            tid: {
                "best_bid": 0.1,
                "best_ask": 0.9,
                "best_bid_size": 10,
                "best_ask_size": 10,
                "book_missing": False,
            }
            for tid in ids
        }

    with patch.object(lib, "fetch_books", side_effect=fake_books):
        with patch.object(lib, "collect_target_tokens", return_value=(tokens, {"mode": "live"})):
            with patch.object(
                lib,
                "join_ft_context",
                return_value={
                    "home": "H",
                    "away": "A",
                    "home_score": 1,
                    "away_score": 0,
                    "dongqiudi": {"id": "m1"},
                    "polymarket": {"event_id": "e1", "slug": "h-vs-a"},
                    "event": {"type": "score_change", "match_id": "m1"},
                },
            ):
                with tempfile.TemporaryDirectory() as td:
                    root = Path(td)
                    (root / "data" / "pm-quote").mkdir(parents=True)
                    bundle = lib.quote_bridge_event(
                        root,
                        {
                            "type": "score_change",
                            "match_id": "m1",
                            "home": "H",
                            "away": "A",
                            "home_score": 1,
                            "away_score": 0,
                            "prev": {"home": 0, "away": 0},
                            "curr": {"home": 1, "away": 0},
                            "polymarket": {"event_id": "e1", "slug": "h-vs-a"},
                        },
                        persist=False,
                        trade_executor=None,
                    )
    check("books_once flag", (bundle.get("discovery") or {}).get("books_once") is True)
    check("single fetch_books call", len(books_calls) == 1, str(books_calls))
    check(
        "both token ids in one call",
        set(books_calls[0]) == {"t1", "t2"},
        str(books_calls),
    )
    check("latency_ms books", "books" in (bundle.get("latency_ms") or {}))


def main() -> int:
    test_bridge_event_queue_before_disk()
    test_af_confirm_no_sync_second_fetch()
    test_live_books_once()
    print(f"\nsmoke_latency_path: {PASS} ok, {FAIL} fail")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
