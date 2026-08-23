#!/usr/bin/env python3
"""Smoke: grade_oddsapiio_sample only (no 3s timers, no live sizing)."""

from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from book_context_observe import (  # noqa: E402
    DEFAULT_ODDS_API_IO_BOOKS,
    DEFAULT_SOURCES,
    cap_grade_awaiting_af,
    grade_oddsapiio_sample,
    inspect_bet365_impossible_markets,
    load_source_keys,
    parse_oddsapiio_books,
    try_create_observer,
)
from grade_sizing import grade_target_usdc  # noqa: E402


def _odds(markets: list[dict]) -> dict:
    return {"status": "live", "bookmakers": {"Bet365": markets}}


def _source(score: tuple[int, int] | None, markets: list[dict], *, ok: bool = True) -> dict:
    raw = _odds(markets)
    out = {
        "ok": ok,
        "identity_verified": True,
        "orientation": "same",
        "books": parse_oddsapiio_books(
            raw, wanted_books=("Bet365",), home="Home", away="Away"
        ),
        "raw": raw,
        "requests": [{"kind": "odds", "raw": raw, "raw_path": "fake.json"}],
    }
    if score is not None:
        out["score"] = {"home": score[0], "away": score[1]}
    return out


def main() -> int:
    assert DEFAULT_SOURCES == ("oddsapiio",)
    assert DEFAULT_ODDS_API_IO_BOOKS == ("Bet365", "1xbet")
    assert load_source_keys(env={})["active_sources"] == []
    assert try_create_observer(Path("/tmp"), env={}) is None

    clean = [
        {"name": "ML", "odds": [{"home": "1.5", "draw": "3", "away": "6"}]},
        {
            "name": "Correct Score",
            "odds": [{"label": "1-0", "odds": "5"}, {"label": "2-0", "odds": "7"}],
        },
        {"name": "Totals", "odds": [{"hdp": 1.5, "over": "1.8", "under": "2"}]},
    ]
    impossible = [
        {"name": "ML", "odds": [{"home": "1.5", "draw": "3", "away": "6"}]},
        {"name": "Correct Score", "odds": [{"label": "0-0", "odds": "9"}]},
    ]
    b = grade_oddsapiio_sample(_source((0, 0), clean), home_score=1, away_score=0)
    assert b["level"] == "B" and b["target_usdc"] == grade_target_usdc("B")
    a = grade_oddsapiio_sample(_source((1, 0), clean), home_score=1, away_score=0)
    assert a["level"] == "A" and a["target_usdc"] == grade_target_usdc("A")
    a_blocked = grade_oddsapiio_sample(
        _source((1, 0), impossible), home_score=1, away_score=0
    )
    assert a_blocked["level"] == "C" and a_blocked["reason"] == "bet365_has_impossible_markets"

    unverified = _source((1, 0), clean)
    unverified["identity_verified"] = False
    assert grade_oddsapiio_sample(unverified, home_score=1, away_score=0)["level"] == "C"

    btts_bad = inspect_bet365_impossible_markets(
        _odds([{"name": "Both Teams To Score", "odds": [{"yes": "1.2", "no": "4"}]}]),
        home_score=1,
        away_score=1,
    )
    assert btts_bad["impossible_offers"][0]["offer"] == "no"
    assert btts_bad["gate_impossible_offers"] == []

    hard_a = grade_oddsapiio_sample(_source((1, 0), clean), home_score=1, away_score=0)
    capped = cap_grade_awaiting_af(hard_a, af_confirmed=False)
    assert capped["level"] == "B" and capped["uncapped_level"] == "A"
    assert cap_grade_awaiting_af(hard_a, af_confirmed=True)["level"] == "A"

    # Gate-clock sample writes jsonl without arming 3s timers.
    from book_context_observe import BookContextObserver

    calls = {"n": 0}

    def fake_fetch(_mid: str, _home: str, _away: str) -> dict:
        calls["n"] += 1
        return _source((1, 0), clean)

    import tempfile
    import time

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        root = Path(td)
        obs = BookContextObserver(
            root,
            source_cfg={
                "active_sources": ("oddsapiio",),
                "keys": {"oddsapiio": "x"},
            },
            fetch_oddsapiio=fake_fetch,
        )
        obs.start()
        ev = {
            "match_id": "m1",
            "home": "Home",
            "away": "Away",
            "home_score": 1,
            "away_score": 0,
        }
        grade = obs.sample_gate_tick(
            ev, event_key="k1", sample_i=0, elapsed_s=5.0
        )
        obs.stop()
        time.sleep(0.05)
        assert grade and grade["level"] == "A", grade
        assert calls["n"] == 1, calls
        # No upgrade queue from observe-only ticks.
        assert obs.drain_upgrades() == []

    print("ok: grade_oddsapiio_sample + gate-clock sample")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
