#!/usr/bin/env python3
"""Smoke: Odds-API.io/Bet365 polling, grading, upgrades, reversal cancellation."""

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
    assert DEFAULT_ODDS_API_IO_BOOKS == ("Bet365",)
    assert load_source_keys(env={})["active_sources"] == []
    assert try_create_observer(Path("/tmp"), env={}) is None
    cfg = load_source_keys(
        env={
            "ODDS_API_IO_KEY": "io",
            "ODDSPAPI_KEY": "ignored",
            "THE_ODDS_API_KEY": "ignored",
            "BOOK_OBSERVE_SOURCES": "oddspapi,theoddsapi",
            "BOOK_ODDS_API_IO_BOOKS": "Bet365,DraftKings",
        }
    )
    assert cfg["active_sources"] == ["oddsapiio"]
    assert cfg["oddsapiio_books"] == ("Bet365",)

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
    assert b["level"] == "B" and b["target_usdc"] == 2.0
    a = grade_oddsapiio_sample(_source((1, 0), impossible), home_score=1, away_score=0)
    assert a["level"] == "A" and a["target_usdc"] == 3.0
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
                "oddsapiio_books": ("Bet365", "DraftKings"),
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

        # Production schedule is 0 plus twelve delayed samples: 0/5/.../60.
        sched = BookContextObserver(
            root,
            source_cfg={"active_sources": ["oddsapiio"], "keys": {"oddsapiio": "io"}},
            fetch_oddsapiio=fetch,
        )
        assert sched._poll_offsets() == [float(x) for x in range(5, 61, 5)]
        sched.stop()

        # A reversal cancels all remaining polls and suppresses queued upgrades.
        rev_obs = BookContextObserver(
            root,
            source_cfg={"active_sources": ["oddsapiio"], "keys": {"oddsapiio": "io"}},
            poll_interval_s=0.05,
            poll_timeout_s=0.2,
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
        assert any(r["phase"] == "dqd_reversal" for r in after)
        assert not any(r.get("poll", {}).get("offset_s", 0) not in (0, None) for r in after)
        assert rev_obs.drain_upgrades() == []
        rev_obs.stop()

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
            if "/odds?" in url:
                http_calls["odds"] += 1
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

        # End-to-end provider-side reversal: preserve the raw score but grade
        # against the score normalized back into the DQD home/away frame.
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
            if "/odds?" in url:
                return 200, _odds(clean), {}, None
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
            assert grade_oddsapiio_sample(
                swapped_source, home_score=1, away_score=0
            )["level"] == "A"
            swapped_obs.stop()
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

    print("ok: Odds-API.io Bet365 polling/grading/upgrades/reversal/raw")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
