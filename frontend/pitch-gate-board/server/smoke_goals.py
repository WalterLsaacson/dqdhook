#!/usr/bin/env python3
"""Smoke: pitch-gate board verdicts follow DOM∧AF buy / AF∨DOM flatten."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import app as board  # noqa: E402


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
        encoding="utf-8",
    )


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="pg-board-"))
    observe = tmp / "dqd_stream_observe.jsonl"
    af = tmp / "af_observe.jsonl"
    book = tmp / "book_context_observe.jsonl"
    quotes = tmp / "quotes.jsonl"
    bridge = tmp / "events.jsonl"
    judge = tmp / "pitch_state_judge.jsonl"
    for p in (observe, af, book, quotes, bridge, judge):
        p.write_text("", encoding="utf-8")

    board.OBSERVE_PATH = observe
    board.AF_OBSERVE_PATH = af
    board.BOOK_OBSERVE_PATH = book
    board.QUOTES_PATH = quotes
    board.BRIDGE_EVENTS_PATH = bridge
    board.JUDGE_PATH = judge

    goal_key = "score_change|m1|0-0->1-0|2026-08-23T10:00:00+08:00"
    rev_key = "score_change|m1|1-0->0-0|2026-08-23T10:02:00+08:00"

    _write_jsonl(
        observe,
        [
            {
                "event_key": goal_key,
                "match_id": "m1",
                "home": "A",
                "away": "B",
                "home_score": 1,
                "away_score": 0,
                "sample_i": 0,
                "elapsed_s": 5,
                "ok": True,
                "gate": True,
                "judge": {"play_state": "in_play", "confidence": 0.9},
                "af": {"ok": True, "score_match": False, "af_score": "0-0"},
                "dom_state": {"pop_box": "进攻", "center_box": "10:00 1 : 0"},
            },
            {
                "event_key": goal_key,
                "match_id": "m1",
                "home": "A",
                "away": "B",
                "home_score": 1,
                "away_score": 0,
                "sample_i": 1,
                "elapsed_s": 10,
                "ok": True,
                "gate": True,
                "judge": {"play_state": "in_play", "confidence": 0.9},
                "af": {"ok": True, "score_match": True, "af_score": "1-0"},
                "dom_state": {"pop_box": "进攻", "center_box": "10:05 1 : 0"},
            },
            {
                "event_key": rev_key,
                "match_id": "m1",
                "home": "A",
                "away": "B",
                "home_score": 0,
                "away_score": 0,
                "sample_i": 0,
                "elapsed_s": 5,
                "ok": True,
                "gate": True,
                "observe_only": True,
                "is_reversal": True,
                "board_score_match": True,
                "judge": {"play_state": "stopped", "stopped_reason": "celebration"},
                "af": {"ok": True, "score_match": False, "af_score": "1-0"},
                "dom_state": {"pop_box": "进球", "center_box": "10:20 0 : 0"},
            },
        ],
    )
    _write_jsonl(
        quotes,
        [
            {
                "mode": "pitch_gate_confirmed",
                "event_key": goal_key,
                "match_id": "m1",
                "quoted_at": "2026-08-23T10:00:10+08:00",
                "pitch_gate": {"status": "in_play"},
            },
            {
                "mode": "dqd_reversal_pitch_gate_canceled",
                "event_key": rev_key,
                "match_id": "m1",
                "quoted_at": "2026-08-23T10:02:00+08:00",
                "pitch_gate": {"status": "cancel_requested"},
            },
            {
                "mode": "pitch_gate_flatten_or",
                "event_key": rev_key,
                "match_id": "m1",
                "quoted_at": "2026-08-23T10:02:06+08:00",
                "flatten_count": 1,
                "pitch_gate": {"status": "flatten_or", "reason": "reversal_dom_score_match"},
            },
            {
                "mode": "reversal_observe_complete",
                "event_key": rev_key,
                "match_id": "m1",
                "quoted_at": "2026-08-23T10:02:07+08:00",
                "pitch_gate": {"status": "observe_complete", "reason": "flatten_or"},
            },
        ],
    )
    _write_jsonl(
        bridge,
        [
            {
                "type": "score_change",
                "is_reversal": True,
                "match_id": "m1",
                "home": "A",
                "away": "B",
                "ts": "2026-08-23T10:02:00+08:00",
                "prev": {"home": 1, "away": 0},
                "curr": {"home": 0, "away": 0},
                "home_score": 0,
                "away_score": 0,
            }
        ],
    )
    _write_jsonl(
        book,
        [
            {
                "event_key": goal_key,
                "sample_i": 1,
                "odds_grade": {"level": "B", "reason": "bet365_clean_identity_soft"},
            }
        ],
    )

    snap = board.build_goals_payload(limit=50)
    by_key = {g["event_key"]: g for g in snap["goals"]}
    goal = by_key[goal_key]
    rev = by_key[rev_key]

    assert goal["verdict"] == "reversed_after_buy", goal["verdict"]
    assert goal["had_aligned_buy"] is True
    assert goal["linked_event_key"] == rev_key, goal.get("linked_event_key")
    assert goal["odds_grade"]["level"] == "B", goal.get("odds_grade")
    assert any(f.get("aligned") for f in goal["frames"])
    assert not all(f.get("aligned") for f in goal["frames"])

    assert rev["verdict"] == "flatten_or", rev["verdict"]
    assert rev["kind"] == "reversal_observe"
    assert rev["linked_event_key"] == goal_key
    assert any(f.get("or_flatten") for f in rev["frames"])

    wait_key = "score_change|m2|0-0->1-0|t2"
    _write_jsonl(
        observe,
        [
            {
                "event_key": wait_key,
                "match_id": "m2",
                "home": "C",
                "away": "D",
                "home_score": 1,
                "away_score": 0,
                "sample_i": 0,
                "elapsed_s": 5,
                "ok": True,
                "gate": True,
                "judge": {"play_state": "in_play"},
                "af": {"ok": True, "score_match": False, "af_score": "0-0"},
                "dom_state": {"pop_box": "进攻", "center_box": "1 : 0"},
            }
        ],
    )
    _write_jsonl(quotes, [])
    _write_jsonl(bridge, [])
    snap2 = board.build_goals_payload(limit=50)
    wait = next(g for g in snap2["goals"] if g["event_key"] == wait_key)
    assert wait["verdict"] == "wait_af", wait["verdict"]

    print("ok: pitch-gate board verdicts match DOM∧AF / AF∨DOM")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
