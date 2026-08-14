#!/usr/bin/env python3
"""Smoke: book-context observe group link, delayed phases, summary, error isolation."""

from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from book_context_observe import (  # noqa: E402
    BookContextObserver,
    DEFAULT_THE_ODDS_SPORT_KEYS,
    get_active_observer,
    load_source_keys,
    make_observe_group_id,
    observe_path,
    parse_oddsapiio_event_meta,
    persist_raw_blob,
    raw_dir,
    redact_url,
    soccer_sport_keys_from_sports_payload,
    summarize_sources,
    try_create_observer,
)


def _read_rows(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    out: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        out.append(json.loads(line))
    return out


def _wait_rows(path: Path, n: int, timeout_s: float = 3.0) -> list[dict]:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        rows = _read_rows(path)
        if len(rows) >= n:
            return rows
        time.sleep(0.02)
    return _read_rows(path)


def main() -> int:
    gid = make_observe_group_id("m1", 1, 0, "m1|0-0→1-0")
    assert gid == "m1|1-0|m1|0-0→1-0"

    assert "soccer_concacaf_leagues_cup" in DEFAULT_THE_ODDS_SPORT_KEYS
    assert "soccer_usa_mls" in DEFAULT_THE_ODDS_SPORT_KEYS
    assert soccer_sport_keys_from_sports_payload(
        [
            {"key": "soccer_epl", "active": True},
            {"key": "basketball_nba", "active": True},
            {"key": "soccer_usa_mls", "active": False},
            {"key": "soccer_concacaf_leagues_cup", "active": True},
        ]
    ) == ["soccer_epl", "soccer_concacaf_leagues_cup"]

    cfg = load_source_keys(env={})
    assert cfg["active_sources"] == []
    assert cfg["theoddsapi_discover"] is True
    assert try_create_observer(Path("/tmp"), env={}) is None

    cfg_disc = load_source_keys(
        env={"THE_ODDS_API_KEY": "k3", "BOOK_THE_ODDS_DISCOVER_SPORTS": "0"}
    )
    assert cfg_disc["theoddsapi_discover"] is False
    assert cfg_disc["active_sources"] == ["theoddsapi"]
    assert "us" in cfg_disc["theoddsapi_regions"]

    cfg_books = load_source_keys(
        env={"ODDS_API_IO_KEY": "k2", "BOOK_ODDS_API_IO_BOOKS": "Bet365,DraftKings"}
    )
    assert cfg_books["oddsapiio_books"] == ("Bet365", "DraftKings")
    assert load_source_keys(env={"ODDS_API_IO_KEY": "k2"})["oddsapiio_books"] == (
        "Bet365",
        "DraftKings",
    )

    meta_sc = parse_oddsapiio_event_meta(
        {
            "id": 99,
            "status": "live",
            "scores": {"home": 0, "away": 3, "periods": {"p1": {"home": 0, "away": 1}}},
            "clock": {"minute": 88, "running": True, "statusDetail": "2nd half"},
        }
    )
    assert meta_sc["score"] == {"home": 0, "away": 3}
    assert meta_sc["clock"]["minute"] == 88
    assert meta_sc["event_status"] == "live"
    assert parse_oddsapiio_event_meta(None) == {}
    assert parse_oddsapiio_event_meta({"status": "pending"})["event_status"] == "pending"

    cfg2 = load_source_keys(
        env={
            "ODDSPAPI_KEY": "k1",
            "ODDS_API_IO_KEY": "k2",
            "THE_ODDS_API_KEY": "k3",
            "BOOK_OBSERVE_SOURCES": "oddspapi,oddsapiio",
        }
    )
    assert cfg2["active_sources"] == ["oddspapi", "oddsapiio"]

    summary = summarize_sources(
        {
            "oddspapi": {
                "ok": True,
                "books": [{"book": "pinnacle", "status": "suspended"}],
            },
            "oddsapiio": {
                "ok": True,
                "books": [{"book": "Bet365", "status": "missing"}],
            },
            "theoddsapi": {
                "ok": False,
                "error": "quota",
                "books": [{"book": "any", "status": "error"}],
            },
        }
    )
    assert summary["any_suspended"] is True
    assert summary["any_missing"] is True
    assert summary["quorum_suspended"] is True
    assert set(summary["ok_sources"]) == {"oddspapi", "oddsapiio"}

    assert "REDACTED" in redact_url("https://api.example/v4/odds?apiKey=SECRET&x=1")
    assert "SECRET" not in redact_url("https://api.example/v4/odds?apiKey=SECRET&x=1")

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "data" / "pm-quote").mkdir(parents=True)
        path = observe_path(root)
        calls = {"oddspapi": 0, "oddsapiio": 0, "theoddsapi": 0}
        obs_holder: dict[str, BookContextObserver] = {}

        def _persist_fake(source: str, kind: str, body: Any) -> tuple[str, dict]:
            obs = obs_holder["obs"]
            ctx = dict(obs._snap_ctx)
            seq = obs._next_raw_seq()
            raw_path = persist_raw_blob(
                root,
                source=source,
                kind=kind,
                match_id=str(ctx.get("match_id") or "m1"),
                phase=str(ctx.get("phase") or "af_confirmed"),
                observe_group_id=str(ctx.get("observe_group_id") or ""),
                seq=seq,
                record={
                    "url": "https://example.test/odds?apiKey=REDACTED",
                    "http_status": 200,
                    "headers": {},
                    "error": None,
                    "body": body,
                },
            )
            return raw_path, body

        def fetch_oddspapi(mid: str, home: str, away: str) -> dict:
            calls["oddspapi"] += 1
            assert mid == "m1"
            body = {
                "bookmakers": [
                    {"name": "pinnacle", "suspended": True},
                    {"name": "singbet", "markets": [{"key": "h2h", "outcomes": []}]},
                ]
            }
            raw_path, raw = _persist_fake("oddspapi", "odds", body)
            return {
                "ok": True,
                "fixture_id": "op-1",
                "books": [
                    {"book": "pinnacle", "status": "suspended", "suspended": True},
                    {"book": "singbet", "status": "open", "ml": {"home": 1.9}},
                ],
                "raw": raw,
                "raw_path": raw_path,
                "requests": [
                    {
                        "kind": "odds",
                        "raw_path": raw_path,
                        "raw": raw,
                        "http_status": 200,
                    }
                ],
            }

        def fetch_oddsapiio(mid: str, home: str, away: str) -> dict:
            calls["oddsapiio"] += 1
            body = {"bookmakers": []}
            raw_path, raw = _persist_fake("oddsapiio", "odds", body)
            return {
                "ok": True,
                "event_id": "io-1",
                "books": [{"book": "Bet365", "status": "missing"}],
                "score": {"home": 1, "away": 0},
                "clock": {"minute": 55, "running": True},
                "event_status": "live",
                "raw": raw,
                "raw_path": raw_path,
                "requests": [{"kind": "odds", "raw_path": raw_path, "raw": raw}],
            }

        def fetch_theoddsapi(mid: str, home: str, away: str) -> dict:
            calls["theoddsapi"] += 1
            body = {"bookmakers": [{"key": "betfair", "markets": []}]}
            raw_path, raw = _persist_fake("theoddsapi", "odds", body)
            return {
                "ok": True,
                "event_id": "toa-1",
                "books": [{"book": "betfair", "status": "open"}],
                "raw": raw,
                "raw_path": raw_path,
                "requests": [{"kind": "odds", "raw_path": raw_path, "raw": raw}],
            }

        obs = BookContextObserver(
            root,
            source_cfg={
                "active_sources": ["oddspapi", "oddsapiio", "theoddsapi"],
                "keys": {
                    "oddspapi": "k1",
                    "oddsapiio": "k2",
                    "theoddsapi": "k3",
                },
            },
            delay_5_s=0.05,
            delay_15_s=0.08,
            delay_45_s=0.12,
            fetch_oddspapi=fetch_oddspapi,
            fetch_oddsapiio=fetch_oddsapiio,
            fetch_theoddsapi=fetch_theoddsapi,
        )
        obs_holder["obs"] = obs
        obs.start()
        assert get_active_observer() is obs

        ev = {
            "match_id": "m1",
            "home": "Home FC",
            "away": "Away FC",
            "home_score": 1,
            "away_score": 0,
        }
        gate = {
            "confirmed": True,
            "goals": {"home": 1, "away": 0},
            "home_score": 1,
            "away_score": 0,
        }
        group = obs.on_af_confirmed(
            root,
            match_id="m1",
            event_key="m1|0-0→1-0",
            ev=ev,
            af_gate=gate,
        )
        assert group == "m1|1-0|m1|0-0→1-0"

        rows = _wait_rows(path, 1)
        assert len(rows) >= 1, f"expected immediate row, got {len(rows)}"
        r0 = rows[0]
        assert r0["phase"] == "af_confirmed"
        assert r0["observe_group_id"] == group
        assert r0["sources"]["oddspapi"]["books"][0]["status"] == "suspended"
        assert r0["summary"]["any_suspended"] is True
        assert r0["sources"]["oddsapiio"]["score"] == {"home": 1, "away": 0}
        assert r0["sources"]["oddsapiio"]["clock"]["minute"] == 55
        assert "error" not in r0
        assert r0["sources"]["oddspapi"].get("raw_path")
        assert r0["sources"]["oddspapi"].get("raw") is not None
        raw_files = list(raw_dir(root).glob("*.json"))
        assert len(raw_files) >= 3, f"expected raw dumps, got {len(raw_files)}"
        sample = json.loads(raw_files[0].read_text(encoding="utf-8"))
        assert "body" in sample
        assert sample.get("source")

        rows = _wait_rows(path, 4, timeout_s=2.0)
        phases = {r["phase"] for r in rows}
        assert "post_confirm_5s" in phases, phases
        assert "post_confirm_15s" in phases, phases
        assert "post_confirm_45s" in phases, phases
        assert all(r["observe_group_id"] == group for r in rows)

        rev_ev = {
            "match_id": "m1",
            "home": "Home FC",
            "away": "Away FC",
            "home_score": 0,
            "away_score": 0,
            "prev": {"home": 1, "away": 0},
            "curr": {"home": 0, "away": 0},
            "is_reversal": True,
        }
        g2 = obs.on_dqd_reversal(
            root, match_id="m1", event_key="m1|1-0→0-0", ev=rev_ev
        )
        assert g2 == group
        rows = _wait_rows(path, 5, timeout_s=2.0)
        rev_rows = [r for r in rows if r["phase"] == "dqd_reversal"]
        assert len(rev_rows) >= 1
        assert rev_rows[0]["observe_group_id"] == group
        assert rev_rows[0].get("unlinked_reversal") is not True
        assert rev_rows[0]["dqd_prev"] == {"home": 1, "away": 0}
        assert calls["oddspapi"] >= 4

        obs.stop()
        assert get_active_observer() is None

        # Live _record_http path (patched transport) must dump body to disk
        import book_context_observe as bco

        def fake_http(url: str, *, timeout_s: float = 12.0, headers=None):
            return (
                200,
                {"bookmakers": [{"name": "pinnacle", "suspended": True}]},
                {"x-requests-remaining": "42", "content-type": "application/json"},
                None,
            )

        prev_http = bco._http_get_json
        bco._http_get_json = fake_http  # type: ignore[assignment]
        try:
            obs_http = BookContextObserver(
                root,
                source_cfg={
                    "active_sources": ["oddspapi"],
                    "keys": {"oddspapi": "k1", "oddsapiio": "", "theoddsapi": ""},
                },
                delay_5_s=60.0,
                delay_15_s=60.0,
                delay_45_s=60.0,
            )
            obs_http._begin_snap_ctx(
                phase="af_confirmed",
                observe_group_id="g-raw",
                match_id="m_raw",
                event_key="ek",
            )
            before_n = len(list(raw_dir(root).glob("*.json")))
            _st, body, _h, err, meta = obs_http._record_http(
                source="oddspapi",
                kind="odds",
                url="https://api.oddspapi.io/v4/odds?apiKey=SECRET123&fixtureId=1",
                inline_raw=True,
            )
            assert err is None
            assert body and meta["raw_path"]
            assert "SECRET123" not in meta["url"]
            assert meta["raw"]["bookmakers"][0]["suspended"] is True
            after_files = list(raw_dir(root).glob("*.json"))
            assert len(after_files) == before_n + 1
            dumped = json.loads(
                (root / "data" / "pm-quote" / meta["raw_path"]).read_text(encoding="utf-8")
            )
            assert dumped["body"]["bookmakers"][0]["suspended"] is True
            assert "SECRET123" not in dumped["url"]
            obs_http.stop()
        finally:
            bco._http_get_json = prev_http

        path2 = observe_path(root)

        def boom_oddspapi(mid: str, home: str, away: str) -> dict:
            return {"ok": False, "error": "oddspapi_down"}

        def boom_oddsapiio(mid: str, home: str, away: str) -> dict:
            raise RuntimeError("oddsapiio_boom")

        def boom_theodds(mid: str, home: str, away: str) -> dict:
            return {"ok": False, "error": "theodds_quota"}

        obs2 = BookContextObserver(
            root,
            source_cfg={
                "active_sources": ["oddspapi", "oddsapiio", "theoddsapi"],
                "keys": {
                    "oddspapi": "k1",
                    "oddsapiio": "k2",
                    "theoddsapi": "k3",
                },
            },
            delay_5_s=60.0,
            delay_15_s=60.0,
            delay_45_s=60.0,
            fetch_oddspapi=boom_oddspapi,
            fetch_oddsapiio=boom_oddsapiio,
            fetch_theoddsapi=boom_theodds,
        )
        obs2.start()
        before = len(_read_rows(path2))
        obs2.on_af_confirmed(
            root,
            match_id="m2",
            event_key="m2|0-0→1-0",
            ev={"home_score": 1, "away_score": 0, "home": "H", "away": "A"},
            af_gate={"goals": {"home": 1, "away": 0}},
        )
        rows2 = _wait_rows(path2, before + 1, timeout_s=2.0)
        assert len(rows2) >= before + 1
        err_row = rows2[-1]
        assert err_row["phase"] == "af_confirmed"
        assert "error" in err_row
        assert "oddspapi" in err_row["error"]
        assert "oddsapiio" in err_row["error"]
        assert "theoddsapi" in err_row["error"]

        g_un = obs2.on_dqd_reversal(
            root,
            match_id="m_orphan",
            event_key="x",
            ev={
                "home_score": 0,
                "away_score": 0,
                "prev": {"home": 1, "away": 0},
                "home": "H",
                "away": "A",
            },
        )
        assert g_un is not None
        rows3 = _wait_rows(path2, before + 2, timeout_s=2.0)
        orphan = [r for r in rows3 if r.get("match_id") == "m_orphan"]
        assert orphan and orphan[0].get("unlinked_reversal") is True
        obs2.stop()

    print("ok: book_context_observe group/phases/summary/raw/error isolation")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
