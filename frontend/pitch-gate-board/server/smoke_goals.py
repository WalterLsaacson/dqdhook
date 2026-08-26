#!/usr/bin/env python3
"""Smoke: pitch-gate board verdicts follow DOM∧AF∧射门 buy / AF∨DOM flatten."""

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
                "shot_seen": True,
                "shot_this_frame": True,
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
                "dom_state": {
                    "pop_box": "进攻",
                    "center_box": "10:05 1 : 0",
                    "marks": ["ball"],
                },
                "shot_seen": True,
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
                "pitch_gate": {"status": "observe_complete", "reason": "flatten_or_stop_trail"},
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
    assert goal["shot_seen"] is True

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
                "shot_seen": True,
                "shot_this_frame": True,
            }
        ],
    )
    _write_jsonl(quotes, [])
    _write_jsonl(bridge, [])
    snap2 = board.build_goals_payload(limit=50)
    wait = next(g for g in snap2["goals"] if g["event_key"] == wait_key)
    assert wait["verdict"] == "wait_af", wait["verdict"]

    wait_shot_key = "score_change|m2b|0-0->1-0|t2b"
    _write_jsonl(
        observe,
        [
            {
                "event_key": wait_shot_key,
                "match_id": "m2b",
                "home": "C",
                "away": "D",
                "home_score": 1,
                "away_score": 0,
                "sample_i": 0,
                "elapsed_s": 5,
                "ok": True,
                "gate": True,
                "judge": {"play_state": "in_play"},
                "af": {"ok": None, "score_match": None, "skipped": "before_shot"},
                "dom_state": {"pop_box": "进攻", "center_box": "1 : 0"},
                "shot_seen": False,
                "shot_this_frame": False,
            }
        ],
    )
    snap_shot = board.build_goals_payload(limit=50)
    wait_shot = next(g for g in snap_shot["goals"] if g["event_key"] == wait_shot_key)
    assert wait_shot["verdict"] == "wait_shot", wait_shot["verdict"]
    assert wait_shot["shot_seen"] is False
    assert not any(f.get("aligned") for f in wait_shot["frames"])

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
                "marks": ["ball"],
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
                "dom_state": {"pop_box": "进攻", "center_box": "1 : 0", "marks": ["net"]},
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

    skip_key = "score_change|m5|0-0->1-0|t5"
    _write_jsonl(
        quotes,
        [
            {
                "event_key": skip_key,
                "match_id": "m5",
                "mode": "pitch_gate_reversal_risk_skip",
                "trigger": "score_change",
            }
        ],
    )
    _write_jsonl(
        observe,
        [
            {
                "event_key": skip_key,
                "match_id": "m5",
                "home": "G",
                "away": "H",
                "home_score": 1,
                "away_score": 0,
                "sample_i": 0,
                "elapsed_s": 0,
                "ok": True,
                "gate": True,
                "judge": {"play_state": "stopped", "confidence": 0.2},
            }
        ],
    )
    snap5 = board.build_goals_payload(limit=50)
    skipped = next(g for g in snap5["goals"] if g["event_key"] == skip_key)
    assert skipped["verdict"] == "reversal_risk_skip", skipped["verdict"]

    # Late 射门 must not back-date aligned @ to an earlier AF-green frame.
    late_shot_key = "score_change|m6|2-1->2-2|2026-08-25T23:10:05+08:00"
    _write_jsonl(
        observe,
        [
            {
                "event_key": late_shot_key,
                "match_id": "m6",
                "home": "Tobol",
                "away": "Kaysar",
                "home_score": 2,
                "away_score": 2,
                "sample_i": 6,
                "elapsed_s": 30.005,
                "ok": True,
                "gate": True,
                "judge": {"play_state": "in_play"},
                "af": {"ok": True, "score_match": True, "af_score": "2-2"},
                "dom_state": {
                    "pop_box": "卡伊萨尔 危险进攻",
                    "marks": ["dangerous-attack"],
                },
                "shot_seen": False,
                "shot_this_frame": False,
            },
            {
                "event_key": late_shot_key,
                "match_id": "m6",
                "home": "Tobol",
                "away": "Kaysar",
                "home_score": 2,
                "away_score": 2,
                "sample_i": 10,
                "elapsed_s": 50.005,
                "ok": True,
                "gate": True,
                "judge": {"play_state": "unclear"},
                "af": {"ok": True, "score_match": True, "af_score": "2-2"},
                "dom_state": {
                    "pop_box": "卡伊萨尔 48'射正",
                    "marks": ["ball", "net"],
                },
                "shot_seen": True,
                "shot_this_frame": True,
            },
            {
                "event_key": late_shot_key,
                "match_id": "m6",
                "home": "Tobol",
                "away": "Kaysar",
                "home_score": 2,
                "away_score": 2,
                "sample_i": 11,
                "elapsed_s": 55.004,
                "ok": True,
                "gate": True,
                "judge": {"play_state": "in_play"},
                "af": {"ok": True, "score_match": True, "af_score": "2-2"},
                "dom_state": {"pop_box": "杜保尔 进攻", "marks": ["attack"]},
                "shot_seen": True,
                "shot_this_frame": False,
            },
        ],
    )
    _write_jsonl(quotes, [])
    snap6 = board.build_goals_payload(limit=50)
    late = next(g for g in snap6["goals"] if g["event_key"] == late_shot_key)
    assert late["verdict"] == "aligned_buy", late["verdict"]
    assert abs(float(late["aligned_elapsed_s"]) - 55.004) < 1e-6, late.get(
        "aligned_elapsed_s"
    )
    assert late["frames"][0].get("aligned") is False
    assert late["frames"][1].get("aligned") is False
    assert late["frames"][2].get("aligned") is True

    # 进攻 + ball mark with runtime shot_seen=False is 无射门, not 等AF.
    no_shot_key = "score_change|m7|0-0->1-0|t7"
    _write_jsonl(
        observe,
        [
            {
                "event_key": no_shot_key,
                "match_id": "m7",
                "home": "Tobol",
                "away": "Kaysar",
                "home_score": 2,
                "away_score": 1,
                "sample_i": 0,
                "elapsed_s": 5,
                "ok": True,
                "gate": True,
                "judge": {"play_state": "in_play"},
                "af": {"ok": None, "score_match": None, "skipped": "before_shot"},
                "dom_state": {"pop_box": "危险进攻", "marks": ["ball"]},
                "shot_seen": False,
                "shot_this_frame": False,
            }
        ],
    )
    _write_jsonl(quotes, [])
    snap7 = board.build_goals_payload(limit=50)
    no_shot = next(g for g in snap7["goals"] if g["event_key"] == no_shot_key)
    assert no_shot["verdict"] == "wait_shot", no_shot["verdict"]
    assert no_shot["shot_seen"] is False
    assert no_shot["frames"][0].get("aligned") is False
    assert no_shot["frames"][0].get("shot_this_frame") is False
    assert no_shot["frames"][0].get("shot_latched") is False

    # Last in_play after a real 射门 latch (AF not green) is 等AF.
    mixed_key = "score_change|m8|0-0->1-0|t8"
    _write_jsonl(
        observe,
        [
            {
                "event_key": mixed_key,
                "match_id": "m8",
                "home": "A",
                "away": "B",
                "home_score": 1,
                "away_score": 0,
                "sample_i": 0,
                "elapsed_s": 5,
                "ok": True,
                "gate": True,
                "judge": {"play_state": "in_play"},
                "af": {"ok": None, "score_match": None, "skipped": "before_shot"},
                "dom_state": {"pop_box": "进攻"},
                "shot_seen": False,
                "shot_this_frame": False,
            },
            {
                "event_key": mixed_key,
                "match_id": "m8",
                "home": "A",
                "away": "B",
                "home_score": 1,
                "away_score": 0,
                "sample_i": 1,
                "elapsed_s": 50,
                "ok": True,
                "gate": True,
                "judge": {"play_state": "in_play"},
                "af": {"ok": True, "score_match": False, "af_score": "0-0"},
                "dom_state": {"pop_box": "射正", "marks": ["ball", "net"]},
                "shot_seen": True,
                "shot_this_frame": True,
            },
        ],
    )
    snap8 = board.build_goals_payload(limit=50)
    mixed = next(g for g in snap8["goals"] if g["event_key"] == mixed_key)
    assert mixed["verdict"] == "wait_af", mixed["verdict"]
    assert mixed["shot_seen"] is True
    assert mixed["frames"][0].get("aligned") is False
    assert mixed["frames"][1].get("aligned") is False

    print("ok: pitch-gate board verdicts match DOM∧AF∧射门 / AF∨DOM")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
