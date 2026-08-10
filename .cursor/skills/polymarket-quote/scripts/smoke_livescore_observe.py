#!/usr/bin/env python3
"""Smoke: Live Score API observe resolve, phases, raw retention, no-key skip."""

from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from livescore_observe import (  # noqa: E402
    LiveScoreObserver,
    extract_live_matches,
    get_active_observer,
    load_credentials,
    make_observe_group_id,
    observe_path,
    resolve_lsa_match,
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
    assert load_credentials(env={}) is None
    assert load_credentials(env={"LIVESCORE_API_KEY": "k"}) is None
    assert load_credentials(
        env={"LIVESCORE_API_KEY": "k", "LIVESCORE_API_SECRET": "s"}
    ) == ("k", "s")

    live_payload = {
        "success": True,
        "data": {
            "match": [
                {
                    "id": 555,
                    "home": {"name": "Home FC"},
                    "away": {"name": "Away United"},
                    "scores": {"score": "1 - 0"},
                    "status": "IN PLAY",
                },
                {
                    "id": 556,
                    "home": {"name": "Other"},
                    "away": {"name": "Side"},
                    "scores": {"score": "0 - 0"},
                },
            ]
        },
    }
    assert len(extract_live_matches(live_payload)) == 2
    resolved = resolve_lsa_match(live_payload, home="Home FC", away="Away United")
    assert resolved["ok"] is True
    assert resolved["lsa_match_id"] == "555"

    miss = resolve_lsa_match(live_payload, home="Nope", away="Missing")
    assert miss["ok"] is False
    assert miss["error"] == "no_team_match"

    gid = make_observe_group_id("m1", 1, 0, "m1|0-0→1-0")
    assert gid == "m1|1-0|m1|0-0→1-0"

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "data" / "pm-quote").mkdir(parents=True)
        assert try_create_observer(root) is None

        path = observe_path(root)
        events_raw = {
            "success": True,
            "data": {
                "match": {"id": "555", "scores": {"score": "1 - 0"}},
                "event": [
                    {
                        "id": 1,
                        "event": "GOAL",
                        "time": 12,
                        "is_home": True,
                        "label": "Goal",
                    }
                ],
            },
        }
        commentary_raw = {
            "success": False,
            "data": {"error": "commentary package required"},
        }
        calls = {"live": 0, "events": 0, "commentary": 0}

        def fetch_live() -> dict:
            calls["live"] += 1
            return {"http_status": 200, "raw": live_payload}

        def fetch_events(lsa_id: str) -> dict:
            calls["events"] += 1
            assert str(lsa_id) == "555"
            return {"http_status": 200, "raw": events_raw}

        def fetch_commentary(lsa_id: str) -> dict:
            calls["commentary"] += 1
            assert str(lsa_id) == "555"
            return {
                "http_status": 403,
                "raw": commentary_raw,
                "error": "http_403",
            }

        obs = LiveScoreObserver(
            root,
            api_key="k",
            api_secret="s",
            delay_15_s=0.05,
            delay_45_s=0.1,
            fetch_live=fetch_live,
            fetch_events=fetch_events,
            fetch_commentary=fetch_commentary,
        )
        obs.start()
        assert get_active_observer() is obs

        ev = {
            "match_id": "m1",
            "home": "Home FC",
            "away": "Away United",
            "home_score": 1,
            "away_score": 0,
        }
        gate = {"confirmed": True, "goals": {"home": 1, "away": 0}}
        group = obs.on_af_confirmed(
            root,
            match_id="m1",
            event_key="m1|0-0→1-0",
            ev=ev,
            af_gate=gate,
        )
        assert group == gid

        rows = _wait_rows(path, 1)
        assert len(rows) >= 1, f"expected immediate row, got {len(rows)}"
        r0 = rows[0]
        assert r0["phase"] == "af_confirmed"
        assert r0["observe_group_id"] == group
        assert r0["lsa_match_id"] == "555"
        assert r0["lsa_events"]["raw"] == events_raw
        assert r0["lsa_commentary"]["raw"] == commentary_raw
        assert r0["lsa_commentary"]["http_status"] == 403
        assert r0["lsa_live"]["raw"]["id"] == 555
        assert "sampled_at" in r0

        rows = _wait_rows(path, 3, timeout_s=2.0)
        phases = {r["phase"] for r in rows}
        assert "post_confirm_15s" in phases, phases
        assert "post_confirm_45s" in phases, phases
        assert all(r["observe_group_id"] == group for r in rows)
        # After cache, delayed snaps should not re-hit live list every time,
        # but events+commentary still fire per phase.
        assert calls["events"] >= 3
        assert calls["commentary"] >= 3

        rev_ev = {
            "match_id": "m1",
            "home": "Home FC",
            "away": "Away United",
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
        rows = _wait_rows(path, 4, timeout_s=2.0)
        rev_rows = [r for r in rows if r["phase"] == "dqd_reversal"]
        assert len(rev_rows) >= 1
        assert rev_rows[0]["observe_group_id"] == group
        assert rev_rows[0].get("unlinked_reversal") is not True
        assert rev_rows[0]["dqd_prev"] == {"home": 1, "away": 0}
        # Full raw still present on reverse
        assert rev_rows[0]["lsa_events"]["raw"]["data"]["event"][0]["event"] == "GOAL"

        map_path = root / "data" / "pm-quote" / "livescore_match_map.json"
        assert map_path.is_file()
        assert json.loads(map_path.read_text(encoding="utf-8")).get("m1") == "555"

        obs.stop()
        assert get_active_observer() is None

        # Resolve failure still writes a row with live raw + error
        def fetch_live_empty() -> dict:
            return {"http_status": 200, "raw": {"success": True, "data": {"match": []}}}

        obs2 = LiveScoreObserver(
            root,
            api_key="k",
            api_secret="s",
            delay_15_s=60.0,
            delay_45_s=60.0,
            fetch_live=fetch_live_empty,
            fetch_events=fetch_events,
            fetch_commentary=fetch_commentary,
        )
        obs2.start()
        before = len(_read_rows(path))
        obs2.on_af_confirmed(
            root,
            match_id="m_miss",
            event_key="x",
            ev={"home": "Z", "away": "Y", "home_score": 1, "away_score": 0},
            af_gate={"goals": {"home": 1, "away": 0}},
        )
        rows2 = _wait_rows(path, before + 1, timeout_s=2.0)
        miss_row = [r for r in rows2 if r.get("match_id") == "m_miss"][-1]
        assert miss_row["lsa_match_id"] is None
        assert "resolve" in miss_row.get("error", {})
        assert miss_row["lsa_events"] is None
        assert miss_row["lsa_live"] is not None
        obs2.stop()

    print("ok: livescore_observe resolve/phases/raw/no-key")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
