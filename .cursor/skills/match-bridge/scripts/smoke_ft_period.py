#!/usr/bin/env python3
"""Smoke checks for period=FT full-time gating and pending 5s poll."""

from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import bridge_lib as bl  # noqa: E402


def _assert(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


def _row(
    *,
    mid: str,
    status_raw: str,
    period: str,
    home_score: int = 2,
    away_score: int = 3,
    minute: str = "90",
    injury_time: int = 0,
) -> dict:
    dqd = {
        "id": mid,
        "home": "Home FC",
        "away": "Away FC",
        "status_raw": status_raw,
        "status": status_raw,
        "period": period,
        "home_score": home_score,
        "away_score": away_score,
        "minute": minute,
        "injury_time": injury_time,
        "official_clock": f"{minute}'+{injury_time}'" if injury_time else f"{minute}'",
    }
    return {
        "dongqiudi": dqd,
        "polymarket": {
            "home": "Home FC",
            "away": "Away FC",
            "league": "TEST",
            "event_id": "1",
            "slug": "test",
            "url": "",
            "condition_ids": [],
            "market_refs": [],
        },
        "kickoff_beijing": "2026-07-26 05:30",
    }


def main() -> int:
    # Helpers
    stoppage = {
        "status_raw": "Playing",
        "period": "2H",
        "minute": "90",
        "injury_time": 6,
    }
    _assert(bl.period_bucket(stoppage) == "2H", "2H period")
    _assert(not bl.is_full_time(stoppage), "stoppage not FT")
    _assert(not bl.is_pending_ft_poll(stoppage), "Playing stoppage not pending")

    played_early = {"status_raw": "Played", "period": "2H"}
    _assert(bl.is_pending_ft_poll(played_early), "Played+2H is pending")
    _assert(not bl.is_full_time(played_early), "Played+2H not full time")

    _assert(bl.is_full_time({"period": "FT"}), "FT is full time")
    _assert(bl.is_full_time({"period": "ft"}), "ft lowercased")
    _assert(bl.PENDING_FT_POLL_SEC == 5, "pending poll 5s")

    # Playing + 2H + injury → no FT event
    prev_status: dict[str, str] = {}
    prev_period: dict[str, str] = {}
    r1 = _row(mid="m1", status_raw="Playing", period="2H", injury_time=6)
    # seed
    ev0 = bl.detect_match_finished([r1], prev_status, prev_period)
    _assert(ev0 == [], f"seed must not emit, got {ev0}")
    # still stoppage
    r1b = _row(mid="m1", status_raw="Playing", period="2H", injury_time=6)
    ev1 = bl.detect_match_finished([r1b], prev_status, prev_period)
    _assert(ev1 == [], f"2H stoppage must not FT, got {ev1}")
    _assert(r1b.get("finished") is False, "finished flag false on 2H")

    # Playing + FT → emit (low-latency transition)
    r2 = _row(mid="m1", status_raw="Playing", period="FT", home_score=2, away_score=3)
    ev2 = bl.detect_match_finished([r2], prev_status, prev_period)
    _assert(len(ev2) == 1 and ev2[0]["type"] == "match_finished", f"Playing+FT emit: {ev2}")
    _assert(ev2[0].get("period") == "FT", "event period FT")
    _assert(r2.get("finished") is True, "finished true on FT")

    # Played + FT from 2H
    prev_status2: dict[str, str] = {"m2": "playing"}
    prev_period2: dict[str, str] = {"m2": "2H"}
    r3 = _row(mid="m2", status_raw="Played", period="FT", home_score=3, away_score=3)
    ev3 = bl.detect_match_finished([r3], prev_status2, prev_period2)
    _assert(len(ev3) == 1, f"Played+FT emit: {ev3}")

    # Played + 1H/2H → no emit; pending true
    prev_status3: dict[str, str] = {"m3": "playing"}
    prev_period3: dict[str, str] = {"m3": "2H"}
    r4 = _row(mid="m3", status_raw="Played", period="2H")
    ev4 = bl.detect_match_finished([r4], prev_status3, prev_period3)
    _assert(ev4 == [], f"Played+2H must not emit, got {ev4}")
    _assert(bl.is_pending_ft_poll(r4["dongqiudi"]), "pending for 5s poll")
    _assert(bl.has_pending_ft_poll([r4["dongqiudi"]]), "has_pending_ft_poll")

    # Upgrade: status tracked, period file missing, curr already FT → still emit
    prev_status4: dict[str, str] = {"m4": "playing"}
    prev_period4: dict[str, str] = {}
    r5 = _row(mid="m4", status_raw="Played", period="FT", home_score=1, away_score=0)
    ev5 = bl.detect_match_finished([r5], prev_status4, prev_period4)
    _assert(len(ev5) == 1, f"upgrade bootstrap must emit FT, got {ev5}")
    _assert(prev_period4.get("m4") == "FT", "period baseline updated after emit")

    # True first sighting already FT → seed only, no emit
    prev_status5: dict[str, str] = {}
    prev_period5: dict[str, str] = {}
    r6 = _row(mid="m5", status_raw="Played", period="FT")
    ev6 = bl.detect_match_finished([r6], prev_status5, prev_period5)
    _assert(ev6 == [], f"cold already-FT must seed only, got {ev6}")

    # Extra time score swings: update prev, do not emit to quote
    prev_scores: dict = {"et1": {"home": 0, "away": 1}}
    r_et = {
        "dongqiudi": {
            "id": "et1",
            "home": "Home FC",
            "away": "Away FC",
            "home_score": 0,
            "away_score": 3,
            "status_raw": "Playing",
            "status": "Playing 103'",
            "period": "2H",
            "minute": "103",
            "injury_time": 0,
            "official_clock": "103'",
        },
        "polymarket": {"league": "COL", "home": "Home FC", "away": "Away FC"},
        "kickoff_beijing": "2026-07-31 00:30",
    }
    et_ev = bl.detect_score_changes([r_et], prev_scores)
    _assert(et_ev == [], f"ET must not emit score_change, got {et_ev}")
    _assert(prev_scores["et1"] == {"home": 0, "away": 3}, "ET still updates prev")

    # Stoppage still emits
    prev_st: dict = {"st1": {"home": 1, "away": 1}}
    r_st = {
        "dongqiudi": {
            "id": "st1",
            "home": "Home FC",
            "away": "Away FC",
            "home_score": 2,
            "away_score": 1,
            "status_raw": "Playing",
            "status": "Playing 90'",
            "period": "2H",
            "minute": "90",
            "injury_time": 5,
            "official_clock": "90'+5'",
        },
        "polymarket": {"league": "EPL", "home": "Home FC", "away": "Away FC"},
        "kickoff_beijing": "2026-07-31 00:30",
    }
    st_ev = bl.detect_score_changes([r_st], prev_st)
    _assert(len(st_ev) == 1 and st_ev[0].get("is_goal") is True, f"stoppage emit: {st_ev}")

    print("smoke_ft_period: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
