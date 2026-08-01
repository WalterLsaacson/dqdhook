#!/usr/bin/env python3
"""Smoke: regulation / stoppage / extra-time clock phase + emit filter."""

from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import dqd_lib as lib  # noqa: E402


def _m(**kw):
    base = {
        "id": "1",
        "cmp_type": "soccer",
        "home": "A",
        "away": "B",
        "home_score": 0,
        "away_score": 0,
        "status_raw": "Playing",
        "status": "Playing 50'",
        "minute": "50",
        "injury_time": 0,
        "period": "2H",
        "league": "Test",
        "league_id": "1",
    }
    base.update(kw)
    return base


def main() -> int:
    assert lib.parse_match_minute({"minute": "90", "minute_str": "90'+6'"}) == 90
    assert lib.parse_match_minute({"minute": "", "minute_str": "90'+6'"}) == 90
    assert lib.parse_match_minute({"status": "Playing 92'"}) == 92
    assert lib.parse_match_minute({"minute": "103"}) == 103

    assert lib.clock_phase(_m(minute="57", injury_time=0)) == "regulation"
    assert lib.clock_phase(_m(minute="90", injury_time=6, status="Playing 90'")) == "stoppage"
    assert lib.clock_phase(_m(minute="45", injury_time=3, period="1H")) == "stoppage"
    # Composite minute_str must not become 906 → false ET when injury_time missing
    assert (
        lib.clock_phase(
            _m(
                minute="",
                minute_str="90'+6'",
                injury_time=0,
                status="Playing 90'",
                status_raw="Playing",
            )
        )
        == "regulation"
    )
    assert lib.clock_phase(_m(minute="92", injury_time=0, status="Playing 92'")) == "extra_time"
    assert lib.clock_phase(_m(minute="120", injury_time=0, status="Playing 120'")) == "extra_time"
    assert lib.clock_phase(_m(period="ET", minute="95")) == "extra_time"
    assert lib.clock_phase(_m(status_raw="Played", period="FT", minute="120")) == "regulation"
    assert lib.is_extra_time_clock(_m(minute="103", injury_time=0))

    prev: dict = {"1": {"home": 0, "away": 1}}
    # Regulation goal emits downstream
    regs = lib.detect_score_changes(
        [_m(home_score=1, away_score=1, minute="70", status="Playing 70'")],
        dict(prev),
        tab="full",
    )
    assert len(regs) == 1 and regs[0]["emit_downstream"] is True
    assert regs[0]["clock_phase"] == "regulation"

    # Extra-time flicker recorded but not for downstream
    prev2: dict = {"1": {"home": 0, "away": 1}}
    ets = lib.detect_score_changes(
        [
            _m(
                home_score=0,
                away_score=3,
                minute="103",
                status="Playing 103'",
                status_raw="Playing",
                injury_time=0,
            )
        ],
        prev2,
        tab="full",
    )
    assert len(ets) == 1
    assert ets[0]["extra_time"] is True
    assert ets[0]["emit_downstream"] is False
    assert lib.events_for_downstream(ets) == []
    assert prev2["1"] == {"home": 0, "away": 3}

    # Stoppage still emits
    prev3: dict = {"1": {"home": 1, "away": 1}}
    st = lib.detect_score_changes(
        [
            _m(
                home_score=2,
                away_score=1,
                minute="90",
                injury_time=4,
                status="Playing 90'",
                status_raw="Playing",
            )
        ],
        prev3,
        tab="hot",
    )
    assert len(st) == 1 and st[0]["clock_phase"] == "stoppage"
    assert st[0]["emit_downstream"] is True

    print("ok: clock_phase regulation/stoppage/extra_time emit filter")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
