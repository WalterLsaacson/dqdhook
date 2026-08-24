#!/usr/bin/env python3
"""Smoke: one-shot T-30 prematch odds snapshot to prematch_odds.jsonl."""

from __future__ import annotations

import json
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from book_context_observe import (  # noqa: E402
    extract_all_book_markets,
    in_prematch_window,
    kickoff_dt_from_match_row,
    parse_oddsapiio_books,
    prematch_path,
    try_create_observer,
)

TZ_CN = timezone(timedelta(hours=8))


def _row(*, kickoff: datetime, status: str = "fixture", finished: bool = False) -> dict:
    return {
        "finished": finished,
        "kickoff_beijing": kickoff.astimezone(TZ_CN).strftime("%Y-%m-%d %H:%M"),
        "dongqiudi": {
            "id": "m1",
            "home": "Home FC",
            "away": "Away FC",
            "status": status,
            "match_timestamp": kickoff.timestamp(),
        },
        "polymarket": {"home": "Home FC", "away": "Away FC"},
    }


def main() -> int:
    now = datetime(2026, 8, 24, 12, 0, tzinfo=TZ_CN)
    inside = _row(kickoff=now + timedelta(minutes=20))
    outside = _row(kickoff=now + timedelta(minutes=40))
    live = _row(kickoff=now + timedelta(minutes=10), status="Playing 3'")
    done = _row(kickoff=now + timedelta(minutes=10), finished=True)
    assert in_prematch_window(inside, now=now)
    assert not in_prematch_window(outside, now=now)
    assert not in_prematch_window(live, now=now)
    assert not in_prematch_window(done, now=now)
    assert kickoff_dt_from_match_row(inside) is not None

    payload = {
        "id": 1,
        "status": "pending",
        "bookmakers": {
            "Bet365": [
                {"name": "ML", "odds": [{"home": "2.10", "draw": "3.20", "away": "3.60"}]},
                {
                    "name": "Totals",
                    "updatedAt": "2026-08-24T04:00:00Z",
                    "odds": [{"hdp": 2.5, "over": "1.90", "under": "1.90"}],
                },
                {
                    "name": "Correct Score",
                    "odds": [{"label": "1-0", "odds": "7.5"}, {"label": "0-0", "odds": "9.0"}],
                },
            ],
            "1xbet": [
                {"name": "ML", "odds": [{"home": "2.05", "draw": "3.30", "away": "3.50"}]},
                {
                    "name": "Spread",
                    "odds": [{"hdp": -0.5, "home": "1.95", "away": "1.85"}],
                },
            ],
        },
    }
    books = extract_all_book_markets(payload, home="Home FC", away="Away FC")
    assert books[0]["book"] == "Bet365" and books[0]["ml"]["h"] == 2.10
    assert books[0]["market_count"] == 3
    names = {m["name"] for m in books[0]["markets"]}
    assert names == {"ML", "Totals", "Correct Score"}
    assert books[1]["market_count"] == 2

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "data" / "pm-quote").mkdir(parents=True)
        obs = try_create_observer(
            root,
            env={"ODDS_API_IO_KEY": "test", "QUOTE_PREMATCH_ODDS": "1"},
        )
        assert obs is not None

        def _fake(match_id: str, home: str, away: str) -> dict:
            return {
                "ok": True,
                "event_id": "42",
                "event_status": "pending",
                "raw": payload,
                "raw_path": "book_context_raw/fake.json",
                "books": parse_oddsapiio_books(
                    payload, wanted_books=("Bet365", "1xbet"), home=home, away=away
                ),
                "requests": [],
            }

        obs._fetch_oddsapiio = _fake  # type: ignore[method-assign]
        rec = obs.sample_prematch(inside)
        assert rec is not None and rec["ok"] is True
        assert rec["event_status"] == "pending"
        assert rec["market_count"] == 5
        assert "m1" in obs._prematch_closed
        import quote_lib as ql

        orig_load = ql.load_bridge_matches
        ql.load_bridge_matches = lambda _root: [inside]  # type: ignore[assignment]
        try:
            obs._prematch_tick()
        finally:
            ql.load_bridge_matches = orig_load
        path = prematch_path(root)
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
        assert len(rows) == 1, "T-30 is one shot, not a repeating poll"
        assert rows[0]["phase"] == "prematch"
        assert rows[0]["books"][0]["markets"][1]["name"] == "Totals"

    print("ok: prematch one-shot at T-30")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
