#!/usr/bin/env python3
"""Smoke: goal-context observe group link, delayed phases, error isolation."""

from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from goal_context_observe import (  # noqa: E402
    GoalContextObserver,
    compact_overview_events,
    get_active_observer,
    make_observe_group_id,
    observe_path,
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
    # Compact overview flattener
    compact = compact_overview_events(
        {
            "data": {
                "match_status": "Playing",
                "events": {
                    "90+3": {
                        "teamAEvents": [
                            {
                                "code": "VAR",
                                "reason": "进球无效",
                                "person": "利诺",
                                "score": "1-1",
                            }
                        ]
                    }
                },
            }
        }
    )
    assert compact["match_status"] == "Playing"
    assert len(compact["events"]) == 1
    assert compact["events"][0]["code"] == "VAR"
    assert compact["events"][0]["reason"] == "进球无效"

    gid = make_observe_group_id("m1", 1, 0, "m1|0-0→1-0")
    assert gid == "m1|1-0|m1|0-0→1-0"

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "data" / "pm-quote").mkdir(parents=True)
        path = observe_path(root)

        calls = {"overview": 0, "af": 0, "list": 0}

        def fetch_overview(mid: str) -> dict:
            calls["overview"] += 1
            return {
                "ok": True,
                "match_status": "Playing",
                "events": [
                    {
                        "minute": "12",
                        "side": "home",
                        "code": "G",
                        "reason": "进球",
                        "person": "A",
                        "score": "1-0",
                    }
                ],
            }

        def fetch_af(mid: str) -> dict:
            calls["af"] += 1
            return {
                "ok": True,
                "status_short": "2H",
                "status_long": "Second Half",
                "elapsed": 67,
                "extra": None,
                "goals": {"home": 1, "away": 0},
                "score": {
                    "halftime": {"home": 0, "away": 0},
                    "fulltime": {"home": None, "away": None},
                    "extratime": {"home": None, "away": None},
                    "penalty": {"home": None, "away": None},
                },
                "af_fixture_id": 999,
            }

        def fetch_list(mid: str) -> dict:
            calls["list"] += 1
            return {
                "team_A_event": "G",
                "team_B_event": None,
                "period": "2H",
                "minute": "67",
                "status": "Playing 67'",
            }

        obs = GoalContextObserver(
            root,
            delay_15_s=0.05,
            delay_45_s=0.1,
            fetch_overview=fetch_overview,
            fetch_af=fetch_af,
            fetch_list=fetch_list,
        )
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
        assert r0["overview"]["events"][0]["code"] == "G"
        assert r0["af_fixture"]["status_short"] == "2H"
        assert r0["list_events"]["team_A_event"] == "G"
        assert "error" not in r0

        rows = _wait_rows(path, 3, timeout_s=2.0)
        phases = {r["phase"] for r in rows}
        assert "post_confirm_15s" in phases, phases
        assert "post_confirm_45s" in phases, phases
        assert all(r["observe_group_id"] == group for r in rows)

        # Reversal reuses same group
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
        rows = _wait_rows(path, 4, timeout_s=2.0)
        rev_rows = [r for r in rows if r["phase"] == "dqd_reversal"]
        assert len(rev_rows) >= 1
        assert rev_rows[0]["observe_group_id"] == group
        assert rev_rows[0].get("unlinked_reversal") is not True
        assert rev_rows[0]["dqd_prev"] == {"home": 1, "away": 0}

        obs.stop()
        assert get_active_observer() is None

        # Error isolation: fetch failures still write a row, no throw
        path2 = observe_path(root)

        def boom_overview(mid: str) -> dict:
            return {"error": "overview_down"}

        def boom_af(mid: str) -> dict:
            raise RuntimeError("af_boom")

        def boom_list(mid: str) -> dict:
            return {"error": "list_miss"}

        obs2 = GoalContextObserver(
            root,
            delay_15_s=60.0,
            delay_45_s=60.0,
            fetch_overview=boom_overview,
            fetch_af=boom_af,
            fetch_list=boom_list,
        )
        obs2.start()
        before = len(_read_rows(path2))
        # Should not raise into caller
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
        assert "overview" in err_row["error"]
        assert "af_fixture" in err_row["error"]
        # Unlinked reversal
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

    print("ok: goal_context_observe group/phases/error isolation")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
