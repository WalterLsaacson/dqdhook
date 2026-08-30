#!/usr/bin/env python3
"""Smokes for pm-locked-scan (no network)."""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import locked_scan_lib as lib  # noqa: E402

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


def test_window_and_finished() -> None:
    print("test_window_and_finished")
    now = datetime(2026, 8, 30, 4, 0, tzinfo=timezone.utc)
    start, end = lib.past_kickoff_window(48, now=now)
    check("window length", (end - start) == timedelta(hours=48))
    ko = (now - timedelta(hours=20)).strftime("%Y-%m-%dT%H:%M:%SZ")
    check("in window", lib.in_past_window(ko, (start, end)))
    future = (now + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    check("future out", not lib.in_past_window(future, (start, end)))

    check(
        "closed skipped",
        not lib.is_finished_unsettled({"closed": True, "ended": True}, ko, now=now),
    )
    check(
        "live skipped",
        not lib.is_finished_unsettled({"closed": False, "live": True}, ko, now=now),
    )
    check(
        "ended open",
        lib.is_finished_unsettled({"closed": False, "ended": True, "live": False}, ko, now=now),
    )
    old = (now - timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
    check(
        "kickoff+100m",
        lib.is_finished_unsettled({"closed": False, "live": False}, old, now=now),
    )
    recent = (now - timedelta(minutes=20)).strftime("%Y-%m-%dT%H:%M:%SZ")
    check(
        "still in 90 not ended",
        not lib.is_finished_unsettled({"closed": False, "live": False}, recent, now=now),
    )
    check(
        "100m is not a gamma finished signal",
        not lib.has_finished_signal({"closed": False, "live": False}),
    )
    check(
        "ended is a finished signal",
        lib.has_finished_signal({"ended": True, "live": False, "closed": False}),
    )


def _aet_fixture() -> dict:
    return {
        "fixture": {
            "id": 1,
            "date": "2026-08-29T16:00:00+00:00",
            "status": {"short": "AET", "long": "After Extra Time"},
        },
        "teams": {
            "home": {"name": "Atlético Malveira"},
            "away": {"name": "Mafra"},
        },
        "goals": {"home": 2, "away": 3},
        "score": {
            "halftime": {"home": 1, "away": 1},
            "fulltime": {"home": 2, "away": 2},
            "extratime": {"home": 2, "away": 3},
            "penalty": {"home": None, "away": None},
        },
    }


def test_af_regulation_ignores_et() -> None:
    print("test_af_regulation_ignores_et")
    ko = datetime(2026, 8, 29, 16, 0, tzinfo=timezone.utc)
    hit = lib.pair_pm_to_af(
        "AC Malveira",
        "CD Mafra",
        ko,
        [_aet_fixture()],
    )
    check("paired", hit is not None)
    assert hit
    check("ft 2-2 not 2-3", hit["home"] == 2 and hit["away"] == 2)
    check("ht 1-1", hit["home_half"] == 1 and hit["away_half"] == 1)
    check("source af", hit["source"] == "apifootball")

    ns = dict(_aet_fixture())
    ns["fixture"] = dict(ns["fixture"])
    ns["fixture"]["status"] = {"short": "NS"}
    ns["score"] = {
        "halftime": {"home": None, "away": None},
        "fulltime": {"home": None, "away": None},
        "extratime": {"home": None, "away": None},
        "penalty": {"home": None, "away": None},
    }
    miss = lib.pair_pm_to_af("AC Malveira", "CD Mafra", ko, [ns])
    check("NS skipped", miss is None)

    # Real API-Football AET encoding: fulltime copies the final, extratime is ET only.
    copied = dict(_aet_fixture())
    copied["score"] = {
        "halftime": {"home": 1, "away": 1},
        "fulltime": {"home": 2, "away": 3},
        "extratime": {"home": 0, "away": 1},
        "penalty": {"home": None, "away": None},
    }
    copied["goals"] = {"home": 2, "away": 3}
    hit2 = lib.pair_pm_to_af("AC Malveira", "CD Mafra", ko, [copied])
    check("copied-final subtracts ET", hit2 is not None and hit2["home"] == 2 and hit2["away"] == 2)

    ns_better = {
        "fixture": {
            "id": 99,
            "date": "2026-08-29T16:00:00+00:00",
            "status": {"short": "NS", "long": "Not Started"},
        },
        "teams": {
            "home": {"name": "AC Malveira"},
            "away": {"name": "CD Mafra"},
        },
        "goals": {"home": None, "away": None},
        "score": {
            "halftime": {"home": None, "away": None},
            "fulltime": {"home": None, "away": None},
            "extratime": {"home": None, "away": None},
            "penalty": {"home": None, "away": None},
        },
    }
    hit3 = lib.pair_pm_to_af("AC Malveira", "CD Mafra", ko, [ns_better, _aet_fixture()])
    check(
        "NS name-winner does not hide FT fixture",
        hit3 is not None and hit3["home"] == 2 and hit3["away"] == 2 and hit3["af_fixture_id"] == 1,
    )


def test_settle_2h_1_5_over_lose() -> None:
    print("test_settle_2h_1_5_over_lose")
    more = {
        "markets": [
            {
                "id": "m1",
                "conditionId": "c1",
                "question": "AC Malveira vs. CD Mafra: CD Mafra 2nd Half O/U 1.5",
                "groupItemTitle": "CD Mafra 2nd Half O/U 1.5",
                "outcomes": ["Over", "Under"],
                "clobTokenIds": ["tok_over", "tok_under"],
                "sportsMarketType": "totals",
            },
            {
                "id": "m2",
                "conditionId": "c2",
                "question": "AC Malveira vs. CD Mafra: CD Mafra 2nd Half O/U 0.5",
                "groupItemTitle": "CD Mafra 2nd Half O/U 0.5",
                "outcomes": ["Over", "Under"],
                "clobTokenIds": ["tok_o05", "tok_u05"],
                "sportsMarketType": "totals",
            },
        ]
    }
    main = {
        "markets": [
            {
                "id": "d",
                "conditionId": "cd",
                "question": "Will AC Malveira vs. CD Mafra end in a draw?",
                "outcomes": ["Yes", "No"],
                "clobTokenIds": ["draw_yes", "draw_no"],
            }
        ]
    }
    rows = lib.settle_event_tokens(
        home="AC Malveira",
        away="CD Mafra",
        home_score=2,
        away_score=2,
        home_half=1,
        away_half=1,
        main=main,
        more=more,
        exact=None,
    )
    by_tok = {r["token_id"]: r for r in rows}
    check("1.5 over lose", by_tok["tok_over"]["settlement"] == "LOSE")
    check("1.5 under win", by_tok["tok_under"]["settlement"] == "WIN")
    check("0.5 over win", by_tok["tok_o05"]["settlement"] == "WIN")
    check("draw yes win", by_tok["draw_yes"]["settlement"] == "WIN")


def test_asks_filter() -> None:
    print("test_asks_filter")
    tokens = [
        {
            "settlement": "WIN",
            "token_id": "a",
            "family": "totals",
            "outcome": "Over",
            "question": "O/U 2.5",
            "market_key": "match_total_2.5_over",
        },
        {
            "settlement": "LOSE",
            "token_id": "b",
            "family": "totals",
            "outcome": "Over",
            "question": "2H 1.5",
            "market_key": "away_2h_total_1.5_over",
        },
        {
            "settlement": "WIN",
            "token_id": "c",
            "family": "totals",
            "outcome": "Over",
            "question": "no book",
            "market_key": "x",
        },
    ]
    books = {
        "a": {
            "best_ask": 0.998,
            "asks_top": [
                {"price": "0.998", "size": "35"},
                {"price": "0.999", "size": "33"},
            ],
        },
        "b": {
            "best_ask": 0.983,
            "asks_top": [{"price": "0.983", "size": "300"}],
        },
        "c": {"best_ask": None, "asks_top": []},
    }
    all_hits = lib.win_tokens_with_asks(tokens, books, max_ask=1.0)
    check("win with asks", len(all_hits) == 1 and all_hits[0]["token_id"] == "a")
    trade = lib.win_tokens_with_asks(tokens, books, max_ask=0.995)
    check("0.995 empty", trade == [])
    mixed = {
        "a": {
            "best_ask": 0.999,
            "asks_top": [
                {"price": "0.990", "size": "10"},
                {"price": "0.999", "size": "33"},
            ],
        }
    }
    capped = lib.win_tokens_with_asks(tokens[:1], mixed, max_ask=0.995)
    check(
        "best_ask is min filtered level",
        capped and capped[0]["best_ask"] == 0.99,
    )


def test_any_other_and_no_half() -> None:
    print("test_any_other_and_no_half")
    exact = {
        "markets": [
            {
                "question": "Exact Score: AC Malveira 2 - 2 CD Mafra?",
                "outcomes": ["Yes", "No"],
                "clobTokenIds": ["e22y", "e22n"],
            },
            {
                "question": "Exact Score: Any Other Score?",
                "outcomes": ["Yes", "No"],
                "clobTokenIds": ["oth_y", "oth_n"],
            },
        ]
    }
    rows = lib.settle_event_tokens(
        home="AC Malveira",
        away="CD Mafra",
        home_score=2,
        away_score=2,
        home_half=1,
        away_half=1,
        main=None,
        more=None,
        exact=exact,
    )
    by_tok = {r["token_id"]: r for r in rows}
    check("2-2 yes win", by_tok["e22y"]["settlement"] == "WIN")
    check("any other no win", by_tok["oth_n"]["settlement"] == "WIN")
    check("any other yes lose", by_tok["oth_y"]["settlement"] == "LOSE")

    miss = lib.resolve_regulation_score(
        {"home": "AC Malveira", "away": "CD Mafra", "start_play": "2026-08-29T16:00:00+00:00"},
        af_fixtures=[],
        dqd_matches=[],
    )
    check("no score", miss.get("error") == "no_regulation_score")


def _dqd_ft(
    *,
    home_score: int = 2,
    away_score: int = 2,
    minute: str = "90",
    league: str = "EPL",
    period: str = "FT",
) -> dict:
    return {
        "id": "dqd-1",
        "home": "AC Malveira",
        "away": "CD Mafra",
        "period": period,
        "status_raw": "Played",
        "home_score": home_score,
        "away_score": away_score,
        "home_half": 1,
        "away_half": 1,
        "minute": minute,
        "league": league,
        "kickoff_beijing": "2026-08-30 00:00",
        "start_play": "2026-08-29 16:00:00+00",
    }


def test_dqd_skips_cup_and_et() -> None:
    print("test_dqd_skips_cup_and_et")
    ko = datetime(2026, 8, 29, 16, 0, tzinfo=timezone.utc)
    league_ft = _dqd_ft(minute="90", home_score=2, away_score=2, league="EPL")
    hit = lib.pair_pm_to_dqd(
        "AC Malveira", "CD Mafra", ko, [league_ft], league="EPL", league_id="epl"
    )
    check("league FT ok", hit is not None and hit["home"] == 2 and hit["away"] == 2)
    check("league et_risk false when minute<=90", hit is not None and hit["et_risk"] is False)

    empty_clock = dict(league_ft)
    empty_clock["minute"] = ""
    hit_empty = lib.pair_pm_to_dqd(
        "AC Malveira", "CD Mafra", ko, [empty_clock], league="EPL", league_id="epl"
    )
    check("empty minute et_risk", hit_empty is not None and hit_empty["et_risk"] is True)

    aet_as_ft = _dqd_ft(minute="120", home_score=2, away_score=3, league="EPL")
    miss_et = lib.pair_pm_to_dqd(
        "AC Malveira", "CD Mafra", ko, [aet_as_ft], league="EPL", league_id="epl"
    )
    check("minute>90 skipped", miss_et is None)

    cup_row = _dqd_ft(minute="90", home_score=2, away_score=3, league="Portuguese Cup")
    miss_cup = lib.pair_pm_to_dqd(
        "AC Malveira", "CD Mafra", ko, [cup_row], league="Portuguese Cup", league_id="ptc"
    )
    check("cup skipped even at minute 90", miss_cup is None)
    check("ptc is cup", lib.looks_like_cup(league_id="ptc"))
    check("epl is not cup", not lib.looks_like_cup(league_id="epl", league="EPL"))

    aet_no_ht = dict(_aet_fixture())
    aet_no_ht["score"] = {
        "halftime": {"home": None, "away": None},
        "fulltime": {"home": 2, "away": 3},
        "extratime": {"home": 0, "away": 1},
        "penalty": {"home": None, "away": None},
    }
    blocked = lib.resolve_regulation_score(
        {
            "home": "AC Malveira",
            "away": "CD Mafra",
            "start_play": "2026-08-29T16:00:00+00:00",
            "league": "EPL",
            "league_id": "epl",
        },
        af_fixtures=[aet_no_ht],
        dqd_matches=[_dqd_ft(minute="90", home_score=2, away_score=3)],
    )
    check("AF AET without 90' blocks DQD", blocked.get("error") == "af_live_or_et_no_regulation")


def test_unknown_league() -> None:
    print("test_unknown_league")
    try:
        lib.filter_soccer_catalog([{"id": "ptc"}], ["ptc1"])
        check("unknown league raises", False)
    except Exception as e:
        check("unknown league raises", "Unknown or unavailable" in str(e))
    kept = lib.filter_soccer_catalog([{"id": "ptc"}, {"id": "epl"}], ["ptc"])
    check("known league kept", [c["id"] for c in kept] == ["ptc"])

    now = datetime(2026, 8, 30, 4, 0, tzinfo=timezone.utc)
    snap = {
        "matches": [
            {
                "league_id": "ptc",
                "closed": False,
                "ended": True,
                "start_play": (now - timedelta(hours=5)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "home": "A",
                "away": "B",
            },
            {
                "league_id": "epl",
                "closed": False,
                "ended": True,
                "start_play": (now - timedelta(hours=5)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "home": "C",
                "away": "D",
            },
        ]
    }
    only_ptc = lib.matches_from_snapshot(snap, hours=48, now=now, leagues=["ptc"])
    check("snapshot honors --league", len(only_ptc) == 1 and only_ptc[0]["league_id"] == "ptc")


def main() -> int:
    test_window_and_finished()
    test_af_regulation_ignores_et()
    test_settle_2h_1_5_over_lose()
    test_asks_filter()
    test_any_other_and_no_half()
    test_dqd_skips_cup_and_et()
    test_unknown_league()
    print(f"smoke_locked_scan: {PASS} ok, {FAIL} fail")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
