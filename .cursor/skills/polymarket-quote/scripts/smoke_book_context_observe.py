#!/usr/bin/env python3
"""Smoke: Odds-API.io Bet365 gate + 1xbet observe, grading, upgrades, reversal."""

from __future__ import annotations

import json
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from book_context_observe import (  # noqa: E402
    BookContextObserver,
    DEFAULT_ODDS_API_IO_BOOKS,
    DEFAULT_SOURCES,
    cap_grade_awaiting_af,
    evaluate_reversal_sample,
    grade_oddsapiio_sample,
    inspect_bet365_impossible_markets,
    load_source_keys,
    observe_path,
    parse_oddsapiio_books,
    resolve_team_match,
    raw_dir,
    redact_url,
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


def _rows(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _wait(path: Path, count: int, timeout: float = 2.0) -> list[dict]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        rows = _rows(path)
        if len(rows) >= count:
            return rows
        time.sleep(0.01)
    return _rows(path)


def main() -> int:
    assert DEFAULT_SOURCES == ("oddsapiio",)
    assert DEFAULT_ODDS_API_IO_BOOKS == ("Bet365", "1xbet")
    assert load_source_keys(env={})["active_sources"] == []
    assert try_create_observer(Path("/tmp"), env={}) is None
    cfg = load_source_keys(
        env={
            "ODDS_API_IO_KEY": "io",
            "ODDSPAPI_KEY": "ignored",
            "THE_ODDS_API_KEY": "ignored",
            "BOOK_OBSERVE_SOURCES": "oddspapi,theoddsapi",
        }
    )
    assert cfg["active_sources"] == ["oddsapiio"]
    assert cfg["oddsapiio_books"] == ("Bet365", "1xbet")

    # Same-team catalogs may contain settled historical fixtures. Kickoff and
    # status must select the current event, and reversed provider sides are explicit.
    mapping_rows = [
        {
            "id": "old",
            "home": "Home",
            "away": "Away",
            "date": "2026-08-12T23:00:00Z",
            "status": "settled",
        },
        {
            "id": "live",
            "home": "Home",
            "away": "Away",
            "date": "2026-08-13T23:00:00Z",
            "status": "live",
        },
    ]
    mapped = resolve_team_match(
        mapping_rows,
        home="Home",
        away="Away",
        kickoff_at="2026-08-14 07:00",
        require_nonterminal=True,
    )
    assert mapped["id"] == "live" and mapped["time_delta_s"] == 0.0
    swapped_map = resolve_team_match(
        [{"id": "sw", "home": "Away", "away": "Home", "status": "live"}],
        home="Home",
        away="Away",
        require_nonterminal=True,
    )
    assert swapped_map["ok"] and swapped_map["swapped"] is True

    clean = [
        {"name": "ML", "odds": [{"home": "1.5", "draw": "3", "away": "6"}]},
        {"name": "Correct Score", "odds": [{"label": "1-0", "odds": "5"}, {"label": "2-0", "odds": "7"}]},
        {"name": "Totals", "odds": [{"hdp": 1.5, "over": "1.8", "under": "2"}]},
    ]
    clean_changed = [
        clean[0],
        clean[1],
        {"name": "Totals", "odds": [{"hdp": 1.5, "over": "1.7", "under": "2.1"}]},
    ]
    impossible = [
        {"name": "ML", "odds": [{"home": "1.5", "draw": "3", "away": "6"}]},
        {"name": "Correct Score", "odds": [{"label": "0-0", "odds": "9"}]},
    ]
    b = grade_oddsapiio_sample(_source((0, 0), clean), home_score=1, away_score=0)
    assert b["level"] == "B" and b["target_usdc"] == grade_target_usdc("B")
    a = grade_oddsapiio_sample(_source((1, 0), clean), home_score=1, away_score=0)
    assert a["level"] == "A" and a["target_usdc"] == grade_target_usdc("A")
    a_blocked = grade_oddsapiio_sample(_source((1, 0), impossible), home_score=1, away_score=0)
    assert a_blocked["level"] == "C" and a_blocked["reason"] == "bet365_has_impossible_markets"
    latency_raw = {"status": "live", "bookmakers": {"Bet365 (no latency)": clean}}
    latency_books = parse_oddsapiio_books(
        latency_raw, wanted_books=("Bet365",), home="Home", away="Away"
    )
    assert latency_books[0]["book"] == "Bet365" and latency_books[0]["status"] == "open"
    latency_src = _source((1, 0), clean)
    latency_src["books"] = latency_books
    latency_src["raw"] = latency_raw
    assert grade_oddsapiio_sample(latency_src, home_score=1, away_score=0)["level"] == "A"
    spread_only = [{"name": "Spread", "odds": [{"hdp": 0.5, "home": "2.000", "away": "1.800"}]}]
    spread_raw = {"status": "live", "bookmakers": {"Bet365 (no latency)": spread_only}}
    spread_src = {
        "ok": True,
        "identity_verified": True,
        "orientation": "same",
        "score": {"home": 1, "away": 0},
        "books": parse_oddsapiio_books(
            spread_raw, wanted_books=("Bet365",), home="Home", away="Away"
        ),
        "raw": spread_raw,
    }
    spread_grade = grade_oddsapiio_sample(spread_src, home_score=1, away_score=0)
    assert spread_src["books"][0]["status"] == "open"
    assert spread_grade["level"] == "C"
    assert spread_grade["reason"] == "bet365_no_score_sensitive_markets"
    extra_raw = {
        "status": "live",
        "bookmakers": {
            "Bet365 (no latency)": spread_only,
            "Pinnacle": [
                {"name": "ML", "odds": [{"home": "1.5", "draw": "3", "away": "6"}]},
                {"name": "Correct Score", "odds": [{"label": "1-0", "odds": "5"}]},
                {"name": "Totals", "odds": [{"hdp": 1.5, "over": "1.8", "under": "2"}]},
            ],
        },
    }
    extra_books = parse_oddsapiio_books(
        extra_raw, wanted_books=DEFAULT_ODDS_API_IO_BOOKS, home="Home", away="Away"
    )
    by_book = {b["book"]: b for b in extra_books}
    assert list(by_book) == ["Bet365", "1xbet"]
    assert by_book["Bet365"]["status"] == "open"
    assert "observe_only" not in by_book["Bet365"]
    assert by_book["1xbet"]["status"] == "missing" and by_book["1xbet"]["observe_only"] is True
    extra_src = {
        "ok": True,
        "identity_verified": True,
        "orientation": "same",
        "score": {"home": 1, "away": 0},
        "books": extra_books,
        "raw": extra_raw,
    }
    extra_grade = grade_oddsapiio_sample(extra_src, home_score=1, away_score=0)
    assert extra_grade["level"] == "C"
    assert extra_grade["reason"] == "bet365_no_score_sensitive_markets"
    assert extra_grade["observe_books"][0]["book"] == "1xbet"
    assert extra_grade["observe_books"][0]["core_clean"] is False
    assert extra_grade["observe_books"][0]["reason"] == "1xbet_missing"

    xbet_dirty = {
        "status": "live",
        "bookmakers": {
            "Bet365": clean,
            "1xbet (no latency)": impossible,
        },
    }
    xbet_books = parse_oddsapiio_books(
        xbet_dirty, wanted_books=DEFAULT_ODDS_API_IO_BOOKS, home="Home", away="Away"
    )
    xbet_by = {b["book"]: b for b in xbet_books}
    assert xbet_by["1xbet"]["status"] == "open" and xbet_by["1xbet"]["observe_only"] is True
    xbet_src = {
        "ok": True,
        "identity_verified": True,
        "orientation": "same",
        "score": {"home": 1, "away": 0},
        "books": xbet_books,
        "raw": xbet_dirty,
    }
    xbet_grade = grade_oddsapiio_sample(xbet_src, home_score=1, away_score=0)
    assert xbet_grade["level"] == "A"
    assert xbet_grade["observe_books"][0]["core_clean"] is False
    assert xbet_grade["observe_books"][0]["reason"] == "1xbet_has_impossible_markets"
    xbet_clean_raw = {
        "status": "live",
        "bookmakers": {"Bet365": clean, "1xbet": clean},
    }
    xbet_clean_src = {
        "ok": True,
        "identity_verified": True,
        "orientation": "same",
        "score": {"home": 1, "away": 0},
        "books": parse_oddsapiio_books(
            xbet_clean_raw, wanted_books=DEFAULT_ODDS_API_IO_BOOKS, home="Home", away="Away"
        ),
        "raw": xbet_clean_raw,
    }
    xbet_ok = grade_oddsapiio_sample(xbet_clean_src, home_score=1, away_score=0)
    assert xbet_ok["level"] == "A"
    assert xbet_ok["observe_books"][0]["core_clean"] is True
    assert xbet_ok["observe_books"][0]["reason"] == "1xbet_core_clean"
    unverified = _source((1, 0), clean)
    unverified["identity_verified"] = False
    assert grade_oddsapiio_sample(unverified, home_score=1, away_score=0)["level"] == "C"
    c_bad = grade_oddsapiio_sample(_source((0, 0), impossible), home_score=1, away_score=0)
    assert c_bad["level"] == "C" and c_bad["impossible_offers"]
    c_ml = grade_oddsapiio_sample(
        _source((0, 0), [clean[0]]), home_score=1, away_score=0
    )
    assert c_ml["level"] == "C" and c_ml["reason"] == "bet365_no_score_sensitive_markets"
    btts_bad = inspect_bet365_impossible_markets(
        _odds([{"name": "Both Teams To Score", "odds": [{"yes": "1.2", "no": "4"}]}]),
        home_score=1,
        away_score=1,
    )
    assert btts_bad["impossible_offers"][0]["offer"] == "no"
    assert btts_bad["gate_impossible_offers"] == []

    # Alt / BTTS / clean-sheet dirt must not veto core-clean CS + main Totals.
    dirty_side = [
        {"name": "ML", "odds": [{"home": "1.5", "draw": "3", "away": "6"}]},
        {"name": "Correct Score", "odds": [{"label": "1-1", "odds": "5"}, {"label": "2-1", "odds": "7"}]},
        {"name": "Totals", "odds": [{"hdp": 2.5, "over": "1.8", "under": "2"}]},
        {"name": "Both Teams To Score", "odds": [{"yes": "1.2", "no": "4"}]},
        {"name": "Alternative Goal Line", "odds": [{"hdp": 0.5, "over": "1.1", "under": "8"}]},
        {"name": "Clean Sheet Home", "odds": [{"yes": "2", "no": "1.5"}]},
    ]
    dirty_inspect = inspect_bet365_impossible_markets(
        _odds(dirty_side), home_score=1, away_score=1
    )
    assert dirty_inspect["impossible_offers"]
    assert not dirty_inspect["gate_impossible_offers"]
    dirty_b = grade_oddsapiio_sample(_source((0, 0), dirty_side), home_score=1, away_score=1)
    assert dirty_b["level"] == "B" and dirty_b["reason"] == "bet365_open_no_impossible_markets"
    dirty_a = grade_oddsapiio_sample(_source((1, 1), dirty_side), home_score=1, away_score=1)
    assert dirty_a["level"] == "A" and dirty_a["ignored_impossible_offers"]

    totals_under_dead = [
        {"name": "ML", "odds": [{"home": "1.5", "draw": "3", "away": "6"}]},
        {"name": "Totals", "odds": [{"hdp": 0.5, "over": "1.1", "under": "8"}]},
    ]
    totals_dead = grade_oddsapiio_sample(
        _source((1, 0), totals_under_dead), home_score=1, away_score=0
    )
    assert totals_dead["level"] == "C"
    assert totals_dead["reason"] == "bet365_has_impossible_markets"
    assert totals_dead["gate_impossible_offers"]

    soft_src = _source((1, 0), clean)
    soft_src["identity_verified"] = False
    soft_src["identity_soft_ok"] = True
    soft_grade = grade_oddsapiio_sample(soft_src, home_score=1, away_score=0)
    assert soft_grade["level"] == "B"
    assert soft_grade["reason"] == "bet365_clean_identity_soft"
    assert soft_grade["target_usdc"] == grade_target_usdc("B")
    assert soft_grade["score_match"] is True

    hard_a = grade_oddsapiio_sample(_source((1, 0), clean), home_score=1, away_score=0)
    assert hard_a["level"] == "A" and hard_a["identity_verified"] is True
    capped = cap_grade_awaiting_af(hard_a, af_confirmed=False)
    assert capped["level"] == "B" and capped["uncapped_level"] == "A"
    assert capped["af_hard_confirm"] is False and "|awaiting_af" in capped["reason"]
    assert cap_grade_awaiting_af(hard_a, af_confirmed=True)["level"] == "A"
    assert hard_a["identity_soft_ok"] is False
    score_reversal = evaluate_reversal_sample(
        _source((0, 0), clean),
        pre_reversal_score={"home": 1, "away": 0},
    )
    assert score_reversal["confirmed"] is True
    assert score_reversal["reason"] == "odds_score_reverted"
    book_reversal = evaluate_reversal_sample(
        _source((1, 0), impossible),
        pre_reversal_score={"home": 1, "away": 0},
    )
    assert book_reversal["confirmed"] is True
    assert book_reversal["reason"] == "bet365_impossible_markets_returned"
    assert book_reversal["returned_impossible_offers"]
    no_reversal = evaluate_reversal_sample(
        _source((1, 0), clean),
        pre_reversal_score={"home": 1, "away": 0},
    )
    assert no_reversal["confirmed"] is False
    swapped_reversal_src = _source(
        (1, 0),
        [
            {"name": "Correct Score", "odds": [{"label": "1-0", "odds": "5"}]},
            {"name": "Totals", "odds": [{"hdp": 1.5, "over": "1.8", "under": "2"}]},
        ],
    )
    swapped_reversal_src["orientation"] = "swapped"
    swapped_book_reversal = evaluate_reversal_sample(
        swapped_reversal_src,
        pre_reversal_score={"home": 1, "away": 0},
    )
    assert swapped_book_reversal["confirmed"] is True
    assert swapped_book_reversal["reason"] == "bet365_impossible_markets_returned"
    assert swapped_book_reversal["provider_pre_reversal_score"] == {
        "home": 0,
        "away": 1,
    }

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        path = observe_path(root)
        sequence = [
            _source((0, 0), impossible),  # C
            _source((0, 0), clean),       # B
            _source((1, 0), clean),       # A
            _source((1, 0), clean_changed),  # changed data, remains A
        ]
        calls = 0

        def fetch(_mid: str, _home: str, _away: str) -> dict:
            nonlocal calls
            row = sequence[min(calls, len(sequence) - 1)]
            calls += 1
            return row

        obs = BookContextObserver(
            root,
            source_cfg={
                "active_sources": ["oddspapi", "oddsapiio", "theoddsapi"],
                "keys": {"oddsapiio": "io"},
                "oddsapiio_books": ("Bet365",),
            },
            poll_interval_s=0.03,
            poll_timeout_s=0.09,
            fetch_oddsapiio=fetch,
        )
        assert obs.active_sources == ("oddsapiio",)
        assert obs.oddsapiio_books == ("Bet365",)
        obs.start()
        obs.on_af_confirmed(
            match_id="m1",
            event_key="score_change|m1|0-0->1-0",
            ev={"type": "score_change", "match_id": "m1", "home": "Home", "away": "Away", "home_score": 1, "away_score": 0},
            af_gate={"confirmed": True, "goals": {"home": 1, "away": 0}},
        )
        rows = _wait(path, 4)
        assert len(rows) == 4, len(rows)
        assert [r["poll"]["offset_s"] for r in rows] == [0.0, 0.03, 0.06, 0.09]
        assert [r["odds_grade"]["level"] for r in rows] == ["C", "B", "A", "A"]
        assert all(r["odds_grade"]["observe_books"][0]["book"] == "1xbet" for r in rows)
        assert all(r["odds_grade"]["observe_books"][0]["observe_only"] is True for r in rows)
        assert rows[1]["data_changed"] is True and rows[1]["upgrade_emitted"] is True
        assert rows[2]["upgrade_emitted"] is True
        assert rows[3]["data_changed"] is True and rows[3]["upgrade_emitted"] is False
        stored_source = rows[3]["sources"]["oddsapiio"]
        assert "raw" not in stored_source
        assert all("raw" not in req for req in stored_source["requests"])
        upgrades = obs.drain_upgrades()
        assert [u["odds_grade"]["level"] for u in upgrades] == ["B", "A"]
        obs.acknowledge_upgrade(upgrades[-1], success=False)
        with obs._lock:
            obs._upgrades[-1]["retry_after_mono"] = 0.0
        retried = obs.drain_upgrades()
        assert len(retried) == 1 and retried[0]["odds_grade"]["level"] == "A"
        assert retried[0]["retry_count"] == 1
        obs.acknowledge_upgrade(retried[0], success=True)
        assert obs.drain_upgrades() == []
        obs.stop()

        # DQD starts Odds immediately; a raw-A sample sizes B until AF confirms.
        par_path = observe_path(root)
        before_par = len(_rows(par_path))
        par_calls = {"n": 0}

        def fetch_a(_mid: str, _home: str, _away: str) -> dict:
            par_calls["n"] += 1
            return _source((1, 0), clean)

        par_obs = BookContextObserver(
            root,
            source_cfg={"active_sources": ["oddsapiio"], "keys": {"oddsapiio": "io"}},
            poll_interval_s=0.05,
            poll_timeout_s=0.05,
            fetch_oddsapiio=fetch_a,
        )
        par_obs.start()
        par_obs.on_dqd_goal_up(
            match_id="m_par",
            event_key="score_change|m_par|0-0->1-0",
            ev={
                "type": "score_change",
                "match_id": "m_par",
                "home": "Home",
                "away": "Away",
                "home_score": 1,
                "away_score": 0,
            },
        )
        deadline = time.monotonic() + 2.0
        pre_af: list[dict] = []
        while time.monotonic() < deadline:
            pre_af = [r for r in _rows(par_path)[before_par:] if r["match_id"] == "m_par"]
            if pre_af:
                break
            time.sleep(0.01)
        assert pre_af, pre_af
        assert pre_af[0]["phase"] == "dqd_goal"
        assert all(r["odds_grade"]["level"] == "B" for r in pre_af), [
            r["odds_grade"] for r in pre_af
        ]
        assert all(r["odds_grade"].get("uncapped_level") == "A" for r in pre_af)
        assert all(r["odds_grade"].get("af_hard_confirm") is False for r in pre_af)
        ups = par_obs.drain_upgrades()
        assert [u["odds_grade"]["level"] for u in ups] == ["B"], ups
        par_obs.on_af_confirmed(
            match_id="m_par",
            event_key="score_change|m_par|0-0->1-0",
            ev={
                "type": "score_change",
                "match_id": "m_par",
                "home": "Home",
                "away": "Away",
                "home_score": 1,
                "away_score": 0,
            },
            af_gate={"confirmed": True, "goals": {"home": 1, "away": 0}},
        )
        deadline = time.monotonic() + 2.0
        post_af: list[dict] = []
        while time.monotonic() < deadline:
            post_af = [r for r in _rows(par_path)[before_par:] if r["match_id"] == "m_par"]
            if any(r["odds_grade"]["level"] == "A" for r in post_af):
                break
            time.sleep(0.01)
        assert any(r["odds_grade"]["level"] == "A" for r in post_af), [
            r["odds_grade"]["level"] for r in post_af
        ]
        a_ups = par_obs.drain_upgrades()
        assert any(u["odds_grade"]["level"] == "A" for u in a_ups), a_ups
        assert all(u["odds_grade"].get("af_hard_confirm") is True for u in a_ups if u["odds_grade"]["level"] == "A")
        par_obs.stop()

        # Production schedule is 0 plus thirty delayed samples: 0/3/.../90.
        sched = BookContextObserver(
            root,
            source_cfg={"active_sources": ["oddsapiio"], "keys": {"oddsapiio": "io"}},
            fetch_oddsapiio=fetch,
        )
        assert sched._poll_offsets() == [float(x) for x in range(3, 91, 3)]
        sched.stop()

        # A DQD reversal cancels goal polls, then runs six Odds arbitration
        # samples.  DQD alone cannot emit a confirmed flatten decision.
        rev_obs = BookContextObserver(
            root,
            source_cfg={"active_sources": ["oddsapiio"], "keys": {"oddsapiio": "io"}},
            poll_interval_s=0.05,
            poll_timeout_s=0.2,
            reversal_poll_interval_s=0.03,
            reversal_poll_count=6,
            fetch_oddsapiio=lambda *_: _source((1, 0), clean),
        )
        before = len(_rows(path))
        rev_obs.start()
        rev_obs.on_af_confirmed(
            match_id="m2",
            event_key="score_change|m2|0-0->1-0",
            ev={"type": "score_change", "match_id": "m2", "home": "Home", "away": "Away", "home_score": 1, "away_score": 0},
            af_gate={"confirmed": True},
        )
        rev_obs.on_dqd_reversal(
            match_id="m2",
            event_key="score_change|m2|1-0->0-0",
            ev={"home_score": 0, "away_score": 0, "prev": {"home": 1, "away": 0}},
        )
        time.sleep(0.25)
        after = _rows(path)[before:]
        reversal_rows = [r for r in after if r["phase"] == "dqd_reversal"]
        assert len(reversal_rows) == 6, reversal_rows
        assert [r["poll"]["offset_s"] for r in reversal_rows] == [
            0.03, 0.06, 0.09, 0.12, 0.15, 0.18
        ]
        assert all(r["poll"]["interval_s"] == 0.03 for r in reversal_rows)
        assert all(r["poll"]["count"] == 6 for r in reversal_rows)
        assert all(r["poll"]["timeout_s"] == 0.18 for r in reversal_rows)
        assert all(not r["reversal_decision"]["confirmed"] for r in reversal_rows)
        assert rev_obs.drain_reversal_confirms() == []
        assert rev_obs.drain_upgrades() == []
        rev_obs.stop()

        # First corroborating Odds score ends the window and emits exactly one
        # decision; later timers are canceled.
        confirm_obs = BookContextObserver(
            root,
            source_cfg={"active_sources": ["oddsapiio"], "keys": {"oddsapiio": "io"}},
            reversal_poll_interval_s=0.02,
            reversal_poll_count=6,
            fetch_oddsapiio=lambda *_: _source((0, 0), clean),
        )
        confirm_obs.start()
        confirm_obs.on_af_confirmed(
            match_id="m3",
            event_key="score_change|m3|0-0->1-0",
            ev={"match_id": "m3", "home": "Home", "away": "Away", "home_score": 1, "away_score": 0},
            af_gate={"confirmed": True},
        )
        confirm_obs.on_dqd_reversal(
            match_id="m3",
            event_key="score_change|m3|1-0->0-0",
            ev={"match_id": "m3", "home_score": 0, "away_score": 0, "prev": {"home": 1, "away": 0}},
        )
        time.sleep(0.08)
        decisions = confirm_obs.drain_reversal_confirms()
        assert len(decisions) == 1, decisions
        assert decisions[0]["decision"]["reason"] == "odds_score_reverted"
        assert decisions[0]["poll_offset_s"] == 0.02
        confirm_obs.stop()

        # HTTP helper always persists the full body and redacts the key.
        import book_context_observe as bco

        old_http = bco._http_get_json
        bco._http_get_json = lambda *_args, **_kw: (200, _odds(clean), {}, None)
        try:
            raw_obs = BookContextObserver(
                root,
                source_cfg={"active_sources": ["oddsapiio"], "keys": {"oddsapiio": "io"}},
                fetch_oddsapiio=fetch,
            )
            raw_obs._begin_snap_ctx(phase="test", observe_group_id="g", match_id="raw", event_key="e")
            _status, body, _headers, err, meta = raw_obs._record_http(
                source="oddsapiio",
                kind="odds",
                url="https://api.odds-api.io/v3/odds?apiKey=SECRET&eventId=1",
                inline_raw=True,
            )
            assert err is None and body and meta["raw_path"]
            assert "SECRET" not in meta["url"] and "REDACTED" in redact_url(meta["url"])
            assert list(raw_dir(root).glob("*.json"))
            raw_obs._begin_snap_ctx(phase="same", observe_group_id="g1", match_id="same", event_key="e1")
            first_unique = raw_obs._record_http(
                source="oddsapiio", kind="odds", url="https://example/odds?apiKey=x", inline_raw=False
            )
            raw_obs._begin_snap_ctx(phase="same", observe_group_id="g2", match_id="same", event_key="e2")
            second_unique = raw_obs._record_http(
                source="oddsapiio", kind="odds", url="https://example/odds?apiKey=x", inline_raw=False
            )
            assert first_unique[4]["raw_path"] != second_unique[4]["raw_path"]
            raw_obs.stop()
        finally:
            bco._http_get_json = old_http

        # Events catalog is shared across matches; misses are cached per catalog
        # generation, and raw HTTP bodies remain available via raw_path only.
        http_calls = {"catalog": 0, "odds": 0, "event": 0}

        def catalog_http(url: str, **_kw: object) -> tuple:
            if "/events?" in url and "sport=football" in url:
                http_calls["catalog"] += 1
                return 200, [{"id": "e1", "homeTeam": "Home", "awayTeam": "Away"}], {}, None
            if "/odds/multi" in url or "/odds?" in url:
                http_calls["odds"] += 1
                # multi returns a list of per-event odds payloads
                if "/odds/multi" in url:
                    return 200, [{"id": "e1", **_odds(clean)}], {}, None
                return 200, _odds(clean), {}, None
            http_calls["event"] += 1
            return 200, {"id": "e1", "status": "live", "scores": {"home": 1, "away": 0}}, {}, None

        bco._http_get_json = catalog_http
        try:
            cache_obs = BookContextObserver(
                root,
                source_cfg={"active_sources": ["oddsapiio"], "keys": {"oddsapiio": "io"}},
            )
            cache_obs._begin_snap_ctx(phase="test", observe_group_id="g1", match_id="cat1", event_key="e1")
            first = cache_obs._default_fetch_oddsapiio("cat1", "Home", "Away")
            cache_obs._begin_snap_ctx(phase="test", observe_group_id="g2", match_id="cat2", event_key="e2")
            second = cache_obs._default_fetch_oddsapiio("cat2", "Home", "Away")
            assert first["ok"] and second["ok"] and http_calls["catalog"] == 1, http_calls
            assert second["requests"][0]["cache_hit"] is True
            # The event response above has a score but no team identity.  A
            # catalog/cache match alone must never authorize A/B for this poll.
            assert first["identity_verified"] is False
            assert grade_oddsapiio_sample(first, home_score=1, away_score=0)["level"] == "C"

            cache_obs._begin_snap_ctx(phase="test", observe_group_id="g3", match_id="missing", event_key="e3")
            miss1 = cache_obs._default_fetch_oddsapiio("missing", "Unknown", "Nobody")
            miss2 = cache_obs._default_fetch_oddsapiio("missing", "Unknown", "Nobody")
            assert miss1["error"] == "not_mapped"
            assert miss2.get("mapping_cache") == "negative"
            assert http_calls["catalog"] == 1, http_calls
            cache_obs.stop()
        finally:
            bco._http_get_json = old_http

        # Soft identity: cached mapping + fuzzy teams (terminal status fails hard).
        def soft_http(url: str, **_kw: object) -> tuple:
            event = {
                "id": "e-soft",
                "homeTeam": "Home",
                "awayTeam": "Away",
                "status": "settled",
                "scores": {"home": 1, "away": 0},
            }
            if "/odds/multi" in url:
                return 200, [{"id": "e-soft", **_odds(clean)}], {}, None
            if "/odds?" in url:
                return 200, _odds(clean), {}, None
            if "/events/" in url:
                return 200, event, {}, None
            return 200, [event], {}, None

        bco._http_get_json = soft_http
        try:
            soft_obs = BookContextObserver(
                root,
                source_cfg={"active_sources": ["oddsapiio"], "keys": {"oddsapiio": "io"}},
            )
            soft_obs._update_cache_entry(
                "soft1",
                {
                    "oddsapiio_event_id": "e-soft",
                    "home": "Home",
                    "away": "Away",
                },
            )
            soft_obs._begin_snap_ctx(
                phase="test", observe_group_id="gs", match_id="soft1", event_key="es"
            )
            soft_fetch = soft_obs._default_fetch_oddsapiio("soft1", "Home", "Away")
            assert soft_fetch["ok"] is True
            assert soft_fetch["identity_verified"] is False
            assert soft_fetch["identity_soft_ok"] is True
            assert soft_fetch["identity_soft_reason"] == "cached_event_team_fuzzy"
            soft_g = grade_oddsapiio_sample(soft_fetch, home_score=1, away_score=0)
            assert soft_g["level"] == "B" and soft_g["reason"] == "bet365_clean_identity_soft"
            assert soft_obs._cache_entry("soft1").get("oddsapiio_event_id") == "e-soft"

            def event_fail_http(url: str, **_kw: object) -> tuple:
                if "/odds/multi" in url or "/odds?" in url:
                    payload = _odds(clean)
                    if "/odds/multi" in url:
                        return 200, [{"id": "e-soft", **payload}], {}, None
                    return 200, payload, {}, None
                return None, None, {}, "timeout"

            bco._http_get_json = event_fail_http
            soft_obs._begin_snap_ctx(
                phase="test", observe_group_id="gf", match_id="soft1", event_key="ef"
            )
            fail_fetch = soft_obs._default_fetch_oddsapiio("soft1", "Home", "Away")
            assert fail_fetch["ok"] is True
            assert fail_fetch["identity_soft_ok"] is True
            assert fail_fetch["identity_soft_reason"] == "event_fetch_failed_cached_mapping"
            assert grade_oddsapiio_sample(fail_fetch, home_score=1, away_score=0)["level"] == "B"

            def wrong_http(url: str, **_kw: object) -> tuple:
                event = {
                    "id": "e-soft",
                    "homeTeam": "Other FC",
                    "awayTeam": "Nobody United",
                    "status": "live",
                    "scores": {"home": 1, "away": 0},
                }
                if "/odds/multi" in url:
                    return 200, [{"id": "e-soft", **_odds(clean)}], {}, None
                if "/odds?" in url:
                    return 200, _odds(clean), {}, None
                return 200, event, {}, None

            bco._http_get_json = wrong_http
            soft_obs._begin_snap_ctx(
                phase="test", observe_group_id="gw", match_id="soft1", event_key="ew"
            )
            wrong_fetch = soft_obs._default_fetch_oddsapiio("soft1", "Home", "Away")
            assert wrong_fetch.get("identity_verified") is False
            assert wrong_fetch.get("identity_soft_ok") is not True
            assert not soft_obs._cache_entry("soft1").get("oddsapiio_event_id")
            soft_obs.stop()
        finally:
            bco._http_get_json = old_http

        # End-to-end provider-side reversal: preserve the raw score but grade
        # against the score normalized back into the DQD home/away frame.
        swapped_clean = [
            {"name": "ML", "odds": [{"home": "6", "draw": "3", "away": "1.5"}]},
            {"name": "Correct Score", "odds": [{"label": "0-1", "odds": "5"}, {"label": "0-2", "odds": "7"}]},
            {"name": "Totals", "odds": [{"hdp": 1.5, "over": "1.8", "under": "2"}]},
        ]

        def swapped_http(url: str, **_kw: object) -> tuple:
            event = {
                "id": "sw",
                "homeTeam": "Away",
                "awayTeam": "Home",
                "date": "2026-08-13T23:00:00Z",
                "status": "live",
                "scores": {"home": 0, "away": 1},
            }
            if "/events?" in url:
                return 200, [event], {}, None
            if "/odds/multi" in url:
                return 200, [{"id": "sw", **_odds(swapped_clean)}], {}, None
            if "/odds?" in url:
                return 200, _odds(swapped_clean), {}, None
            return 200, event, {}, None

        bco._http_get_json = swapped_http
        try:
            swapped_obs = BookContextObserver(
                root,
                source_cfg={"active_sources": ["oddsapiio"], "keys": {"oddsapiio": "io"}},
            )
            swapped_obs._begin_snap_ctx(
                phase="test", observe_group_id="swg", match_id="swm", event_key="swe"
            )
            swapped_source = swapped_obs._default_fetch_oddsapiio("swm", "Home", "Away")
            assert swapped_source["identity_verified"] is True
            assert swapped_source["orientation"] == "swapped"
            assert swapped_source["provider_score_raw"] == {"home": 0, "away": 1}
            assert swapped_source["score"] == {"home": 1, "away": 0}
            swapped_grade = grade_oddsapiio_sample(
                swapped_source, home_score=1, away_score=0
            )
            assert swapped_grade["level"] == "A", swapped_grade
            swapped_obs.stop()
        finally:
            bco._http_get_json = old_http

        # Concurrent odds pulls for different event ids coalesce into one /odds/multi.
        multi_calls = {"odds_multi": 0, "odds_single": 0, "event": 0}

        def multi_http(url: str, **_kw: object) -> tuple:
            if "/events?" in url:
                return (
                    200,
                    [
                        {"id": "m1", "homeTeam": "Home", "awayTeam": "Away"},
                        {"id": "m2", "homeTeam": "Alpha", "awayTeam": "Beta"},
                    ],
                    {},
                    None,
                )
            if "/odds/multi" in url:
                multi_calls["odds_multi"] += 1
                return (
                    200,
                    [
                        {"id": "m1", **_odds(clean)},
                        {"id": "m2", **_odds(clean)},
                    ],
                    {},
                    None,
                )
            if "/odds?" in url:
                multi_calls["odds_single"] += 1
                return 200, _odds(clean), {}, None
            multi_calls["event"] += 1
            eid = "m1" if "/events/m1" in url else "m2"
            return 200, {"id": eid, "status": "live", "scores": {"home": 1, "away": 0},
                         "homeTeam": "Home" if eid == "m1" else "Alpha",
                         "awayTeam": "Away" if eid == "m1" else "Beta"}, {}, None

        bco._http_get_json = multi_http
        try:
            multi_obs = BookContextObserver(
                root,
                source_cfg={"active_sources": ["oddsapiio"], "keys": {"oddsapiio": "io"}},
            )
            multi_obs._odds_multi_window_s = 0.02
            from concurrent.futures import ThreadPoolExecutor as _TPE

            def _one(mid: str, home: str, away: str) -> dict:
                multi_obs._begin_snap_ctx(
                    phase="test", observe_group_id=mid, match_id=mid, event_key=mid
                )
                return multi_obs._default_fetch_oddsapiio(mid, home, away)

            with _TPE(max_workers=2) as pool:
                f1 = pool.submit(_one, "mm1", "Home", "Away")
                f2 = pool.submit(_one, "mm2", "Alpha", "Beta")
                r1, r2 = f1.result(timeout=5), f2.result(timeout=5)
            assert r1["ok"] and r2["ok"], (r1, r2)
            assert multi_calls["odds_multi"] == 1, multi_calls
            assert multi_calls["odds_single"] == 0, multi_calls
            assert any(req.get("multi") for req in r1.get("requests") or [])
            multi_obs.stop()

            # Flush exceptions must complete the in-flight batch (not only queued).
            boom_obs = BookContextObserver(
                root,
                source_cfg={"active_sources": ["oddsapiio"], "keys": {"oddsapiio": "io"}},
                workers=1,
            )
            boom_obs._odds_multi_window_s = 0.01
            real_exec = boom_obs._execute_odds_multi_batch

            def boom_exec(batch: list) -> None:
                raise RuntimeError("multi_boom")

            boom_obs._execute_odds_multi_batch = boom_exec  # type: ignore[method-assign]
            boom_obs._begin_snap_ctx(
                phase="test", observe_group_id="b1", match_id="b1", event_key="b1"
            )
            try:
                out = boom_obs._oddsapiio_odds_coalesced(
                    event_id="boom1",
                    books_param="Bet365",
                    snapshot_ctx={"match_id": "b1"},
                )
                assert False, f"expected exception, got {out}"
            except RuntimeError as e:
                assert "multi_boom" in str(e)
            boom_obs._execute_odds_multi_batch = real_exec  # type: ignore[method-assign]
            boom_obs.stop()
        finally:
            bco._http_get_json = old_http

        # A 429 arms one shared backoff; subsequent samples are recorded as
        # skipped and do not perform another HTTP request.
        rate_calls = {"n": 0}

        def rate_http(*_args: object, **_kw: object) -> tuple:
            rate_calls["n"] += 1
            return 429, {"message": "limited"}, {"retry-after": "30"}, "http_429"

        bco._http_get_json = rate_http
        try:
            reset_now = datetime.fromisoformat("2026-08-13T19:09:50+00:00").timestamp()
            assert bco.rate_limit_backoff_s(
                {"x-ratelimit-reset": "2026-08-13T19:45:57Z"}, now_epoch=reset_now
            ) == 2167.0
            assert bco.rate_limit_backoff_s(
                {"x-ratelimit-reset": str(reset_now - 10)}, now_epoch=reset_now
            ) == 60.0
            rate_obs = BookContextObserver(
                root,
                source_cfg={"active_sources": ["oddsapiio"], "keys": {"oddsapiio": "io"}},
            )
            rate_obs._begin_snap_ctx(phase="test", observe_group_id="rg", match_id="rate", event_key="re")
            first_http = rate_obs._record_http(
                source="oddsapiio", kind="odds", url="https://example/odds?apiKey=x", inline_raw=False
            )
            second_http = rate_obs._record_http(
                source="oddsapiio", kind="event", url="https://example/event?apiKey=x", inline_raw=False
            )
            assert first_http[0] == 429 and first_http[4].get("rate_limited_until")
            assert second_http[3] == "rate_limited" and second_http[4].get("skipped") is True
            assert rate_calls["n"] == 1
            rate_obs.stop()
        finally:
            bco._http_get_json = old_http

    print("ok: Odds-API.io Bet365 gate + 1xbet observe polling/grading/upgrades/reversal/raw")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
