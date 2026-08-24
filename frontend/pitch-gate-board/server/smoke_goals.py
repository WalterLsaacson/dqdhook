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

    # Gwangju: earlier 0-1→0-0 must not paint a later bought 0-0→0-1.
    early_goal = "score_change|m3|0-0->0-1|2026-08-23T19:49:00+08:00"
    early_rev = "score_change|m3|0-1->0-0|2026-08-23T19:51:55+08:00"
    later_goal = "score_change|m3|0-0->0-1|2026-08-23T20:23:00+08:00"

    def _aligned_row(ek: str, home_score: int, away_score: int) -> dict:
        return {
            "event_key": ek,
            "match_id": "m3",
            "home": "Gwangju FC",
            "away": "Incheon United FC",
            "home_score": home_score,
            "away_score": away_score,
            "sample_i": 0,
            "elapsed_s": 5.8,
            "ok": True,
            "gate": True,
            "judge": {"play_state": "in_play", "confidence": 0.9},
            "af": {
                "ok": True,
                "score_match": True,
                "af_score": f"{home_score}-{away_score}",
            },
            "dom_state": {
                "pop_box": "进攻",
                "center_box": f"1 : 0 {home_score} : {away_score}",
            },
        }

    _write_jsonl(observe, [_aligned_row(early_goal, 0, 1), _aligned_row(later_goal, 0, 1)])
    _write_jsonl(
        quotes,
        [
            {
                "mode": "pitch_gate_confirmed",
                "event_key": early_goal,
                "match_id": "m3",
                "quoted_at": "2026-08-23T19:49:10+08:00",
                "pitch_gate": {"status": "in_play"},
            },
            {
                "mode": "dqd_reversal_pitch_gate_canceled",
                "event_key": early_rev,
                "match_id": "m3",
                "quoted_at": "2026-08-23T19:51:55+08:00",
                "pitch_gate": {"status": "cancel_requested"},
            },
            {
                "mode": "pitch_gate_confirmed",
                "event_key": later_goal,
                "match_id": "m3",
                "quoted_at": "2026-08-23T20:25:45+08:00",
                "pitch_gate": {"status": "in_play"},
            },
        ],
    )
    _write_jsonl(
        bridge,
        [
            {
                "type": "score_change",
                "is_reversal": True,
                "match_id": "m3",
                "home": "Gwangju FC",
                "away": "Incheon United FC",
                "ts": "2026-08-23T19:51:55+08:00",
                "prev": {"home": 0, "away": 1},
                "curr": {"home": 0, "away": 0},
                "home_score": 0,
                "away_score": 0,
            }
        ],
    )
    snap3 = board.build_goals_payload(limit=50)
    by_m3 = {g["event_key"]: g for g in snap3["goals"]}
    early = by_m3[early_goal]
    later = by_m3[later_goal]
    assert early["verdict"] == "reversed_after_buy", early["verdict"]
    assert later["verdict"] == "aligned_buy", later["verdict"]
    assert later.get("reversed") is False
    assert later.get("linked_event_key") != early_rev
    assert (later.get("reversal") or {}).get("ts") != "2026-08-23T19:51:55+08:00"

    var_after_key = "score_change|m4|0-0->1-0|t4"
    _write_jsonl(
        observe,
        [
            {
                "event_key": var_after_key,
                "match_id": "m4",
                "home": "E",
                "away": "F",
                "home_score": 1,
                "away_score": 0,
                "sample_i": 0,
                "elapsed_s": 5,
                "ok": True,
                "gate": True,
                "judge": {"play_state": "in_play", "confidence": 0.9},
                "af": {"ok": True, "score_match": True, "af_score": "1-0"},
            },
            {
                "event_key": var_after_key,
                "match_id": "m4",
                "home": "E",
                "away": "F",
                "home_score": 1,
                "away_score": 0,
                "sample_i": 1,
                "elapsed_s": 10,
                "ok": True,
                "gate": True,
                "judge": {
                    "play_state": "stopped",
                    "stopped_reason": "var",
                    "confidence": 0.9,
                },
                "af": {
                    "ok": None,
                    "score_match": None,
                    "skipped": "after_buy",
                    "error": "af_stopped_after_buy",
                },
            },
        ],
    )
    snap4 = board.build_goals_payload(limit=50)
    var_after = next(g for g in snap4["goals"] if g["event_key"] == var_after_key)
    assert var_after["verdict"] == "aligned_buy", var_after["verdict"]

    print("ok: pitch-gate board verdicts match DOM∧AF / AF∨DOM")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
