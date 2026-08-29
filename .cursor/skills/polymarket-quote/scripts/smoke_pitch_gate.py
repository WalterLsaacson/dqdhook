#!/usr/bin/env python3
"""Smoke: AND buy (DOM in_play ∧ AF, no 射门 gate), AF-only reversal flatten."""

from __future__ import annotations

import os
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import patch

_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import af_observe as af_mod  # noqa: E402
import pitch_gate as pg  # noqa: E402
import quote_lib as lib  # noqa: E402
from dqd_stream_observe import set_active_observer  # noqa: E402


def _dom(pop: str, center: str, marks: list[str] | None = None) -> dict[str, Any]:
    return {
        "pop_box": pop,
        "pop_class": "pop-box home",
        "center_box": center,
        "marks": marks or [],
        "root_class": "football-animate",
    }


class _FakeReader:
    def __init__(
        self, frames: list[dict[str, Any] | None], *, baseline: dict[str, Any] | None
    ) -> None:
        self.queue = [baseline, *frames]
        self.i = 0
        self.closed = False

    def open(self) -> tuple[bool, str | None]:
        return True, None

    def read(self) -> tuple[dict[str, Any] | None, str | None]:
        dom = self.queue[min(self.i, len(self.queue) - 1)]
        self.i += 1
        return (dom, None) if dom is not None else (None, "no_animation_root")

    def close(self) -> None:
        self.closed = True


class _FakeObserver:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.rows: list[dict[str, Any]] = []

    def _resolve_surface(self, match_id: str) -> dict[str, Any]:
        return {
            "match_id": match_id,
            "page_url": f"https://tracker.example/{match_id}",
            "surface": "animation",
            "nami_id": "999",
        }

    def _write_rows(self, rows: list[dict[str, Any]]) -> None:
        self.rows.extend(rows)


class _FakeAf:
    def __init__(self, script: list[dict[str, Any]]) -> None:
        self.script = script
        self.i = 0
        self.calls = 0

    def sample_once(self, ev, *, event_key, sample_i, elapsed_s):  # noqa: ANN001
        self.calls += 1
        row = dict(self.script[min(self.i, len(self.script) - 1)])
        self.i += 1
        row.update(
            {
                "sample_i": sample_i,
                "elapsed_s": elapsed_s,
                "event_key": event_key,
                "match_id": ev.get("match_id"),
            }
        )
        return row


def _wait_done(coord: pg.PitchGateCoordinator, *, n: int, timeout: float = 4.0) -> list[dict]:
    deadline = time.time() + timeout
    got: list[dict] = []
    while time.time() < deadline:
        got.extend(coord.drain_done())
        if len(got) >= n:
            return got
        time.sleep(0.02)
    return got


def _open_factory(frames: list[dict[str, Any] | None], *, baseline: dict[str, Any] | None):
    readers: list[_FakeReader] = []

    def _make(_self, _session, _observer):
        r = _FakeReader(frames, baseline=baseline)
        readers.append(r)
        r.open()
        return r, None, _observer._resolve_surface("m")

    return _make, readers


def main() -> int:
    os.environ["QUOTE_DQD_STREAM_OBSERVE"] = "1"
    os.environ["QUOTE_GATE_SOURCE"] = "dom"
    assert pg.GATE_FIRST_DELAY_S == 0.0, pg.GATE_FIRST_DELAY_S
    assert pg.GATE_TIMEOUT_S == 120.0, pg.GATE_TIMEOUT_S
    assert pg._grade_is_a({"level": "A"})
    assert pg._grade_is_a({"level": "B", "uncapped_level": "A"})
    assert not pg._grade_is_a({"level": "B"})

    old = (pg.GATE_FIRST_DELAY_S, pg.GATE_INTERVAL_S, pg.GATE_TIMEOUT_S)
    pg.GATE_FIRST_DELAY_S = 0.04
    pg.GATE_INTERVAL_S = 0.04
    pg.GATE_TIMEOUT_S = 0.28
    orig_open = pg.PitchGateCoordinator._open_dom_reader
    orig_odds = pg.PitchGateCoordinator._sample_odds

    ev = {
        "type": "score_change",
        "match_id": "m1",
        "home": "H",
        "away": "A",
        "home_score": 1,
        "away_score": 0,
        "ts": "2026-08-23T12:00:00+08:00",
        "polymarket": {"event_id": "e1"},
    }

    try:
        # AND buy: celebration skips AF; first in_play tick polls and buys.
        frames = [
            _dom("H 进球", "45:01 1 : 0", ["ball"]),
            _dom("H 进攻", "45:06 1 : 0", ["attack-move"]),
            _dom("H 控球", "45:11 1 : 0", ["possession-rect"]),
        ]
        factory, readers = _open_factory(frames, baseline=_dom("H 进球", "44:56 1 : 0"))
        pg.PitchGateCoordinator._open_dom_reader = factory  # type: ignore[assignment]
        pg.reset_coordinator_for_tests()
        af = _FakeAf(
            [
                {"ok": True, "score_match": False, "af_score": "0-0"},
                {"ok": True, "score_match": True, "af_score": "1-0"},
            ]
        )
        af_mod.set_active_observer(af)  # type: ignore[arg-type]
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "data" / "pm-quote").mkdir(parents=True)
            obs = _FakeObserver(root)
            set_active_observer(obs)  # type: ignore[arg-type]
            coord = pg.get_coordinator(root)
            assert coord.start_gate(ev, event_key="k1")
            done = _wait_done(coord, n=2)
            statuses = [d["status"] for d in done]
            assert "in_play" in statuses and "aligned_buy" in statuses, done
            assert sum(1 for d in done if d["status"] == "in_play") == 1, done
            assert len(obs.rows) == 3, [r.get("sample_i") for r in obs.rows]
            assert af.calls == 2, af.calls
            assert (obs.rows[0].get("af") or {}).get("skipped") == "before_in_play"
            buy = next(d for d in done if d["status"] == "in_play")
            assert buy["sample_i"] == 2, buy
            n_rows = len(obs.rows)
            time.sleep(0.15)
            assert len(obs.rows) == n_rows, "DOM must stop after buy"
            assert not any(
                (r.get("af") or {}).get("skipped") == "after_buy" for r in obs.rows
            )
            assert readers[-1].closed
            assert not any(r.get("frame_path") for r in obs.rows)
            trail = next(d for d in done if d["status"] == "aligned_buy")
            assert trail.get("reason") == "stop_after_buy", trail

        # PM 0-1 vs Nami/DQD 1-0 (sides swapped) still buys.
        factory, _ = _open_factory(
            [
                _dom("H 进球", "45:01 1 : 0", ["ball"]),
                _dom("H 进攻", "45:06 1 : 0", ["attack-move"]),
            ],
            baseline=_dom("H 进球", "44:56 1 : 0"),
        )
        pg.PitchGateCoordinator._open_dom_reader = factory  # type: ignore[assignment]
        pg.reset_coordinator_for_tests()
        af_mod.set_active_observer(
            _FakeAf([{"ok": True, "score_match": True, "af_score": "0-1"}])  # type: ignore[arg-type]
        )
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "data" / "pm-quote").mkdir(parents=True)
            set_active_observer(_FakeObserver(root))  # type: ignore[arg-type]
            coord = pg.get_coordinator(root)
            swap_ev = {
                **ev,
                "match_id": "m_swap",
                "home": "Paris Saint-Germain FC",
                "away": "Stade Rennais FC 1901",
                "home_score": 0,
                "away_score": 1,
                "sides_swapped": True,
                "dqd_home": "Stade Rennais FC",
                "dqd_away": "Paris Saint Germain",
            }
            assert pg.PitchGateCoordinator._expected_for_dom(swap_ev) == (1, 0)
            assert coord.start_gate(swap_ev, event_key="k_swap")
            done = _wait_done(coord, n=2)
            assert "in_play" in [d["status"] for d in done], done

        # DOM in_play but AF never matches → no buy.
        factory, _ = _open_factory(
            [_dom("H 进攻", "45:06 1 : 0", ["attack-move", "ball"])] * 8,
            baseline=_dom("H 进球", "44:56 1 : 0"),
        )
        pg.PitchGateCoordinator._open_dom_reader = factory  # type: ignore[assignment]
        pg.reset_coordinator_for_tests()
        af_mod.set_active_observer(
            _FakeAf([{"ok": True, "score_match": False, "af_score": "0-0"}])  # type: ignore[arg-type]
        )
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "data" / "pm-quote").mkdir(parents=True)
            set_active_observer(_FakeObserver(root))  # type: ignore[arg-type]
            coord = pg.get_coordinator(root)
            assert coord.start_gate({**ev, "match_id": "m2"}, event_key="k2")
            done = _wait_done(coord, n=1)
            assert [d["status"] for d in done] == ["timeout"], done
            assert done[0].get("buy_emitted") is False
            assert "no_aligned_buy" in str(done[0].get("reason") or ""), done[0]

        # AF error fail-closed.
        factory, _ = _open_factory(
            [_dom("H 进攻", "45:06 1 : 0", ["attack-move", "ball"])] * 8,
            baseline=_dom("H 进球", "44:56 1 : 0"),
        )
        pg.PitchGateCoordinator._open_dom_reader = factory  # type: ignore[assignment]
        pg.reset_coordinator_for_tests()
        af_mod.set_active_observer(
            _FakeAf([{"ok": False, "score_match": None, "error": "rate_limit"}])  # type: ignore[arg-type]
        )
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "data" / "pm-quote").mkdir(parents=True)
            set_active_observer(_FakeObserver(root))  # type: ignore[arg-type]
            coord = pg.get_coordinator(root)
            assert coord.start_gate({**ev, "match_id": "m3"}, event_key="k3")
            done = _wait_done(coord, n=1)
            assert done[0]["status"] == "timeout", done
            assert done[0].get("buy_emitted") is False
            assert "no_aligned_buy" in str(done[0].get("reason") or ""), done[0]

        # in_play + AF, no 射门 overlay → still buy (射门 is not a gate).
        factory, _ = _open_factory(
            [_dom("H 进攻", "45:06 1 : 0", ["attack-move"])] * 8,
            baseline=_dom("H 进球", "44:56 1 : 0"),
        )
        pg.PitchGateCoordinator._open_dom_reader = factory  # type: ignore[assignment]
        pg.reset_coordinator_for_tests()
        af_noshot = _FakeAf([{"ok": True, "score_match": True, "af_score": "1-0"}])
        af_mod.set_active_observer(af_noshot)  # type: ignore[arg-type]
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "data" / "pm-quote").mkdir(parents=True)
            obs_shot = _FakeObserver(root)
            set_active_observer(obs_shot)  # type: ignore[arg-type]
            coord = pg.get_coordinator(root)
            assert coord.start_gate({**ev, "match_id": "m3s"}, event_key="k3s")
            done = _wait_done(coord, n=2)
            assert "in_play" in [d["status"] for d in done], done
            assert af_noshot.calls >= 1, af_noshot.calls
            assert (obs_shot.rows[0].get("af") or {}).get("score_match") is True

        # Unclear 射门 (pop) skips AF; in_play 进攻 same tick polls AF → buy.
        factory, _ = _open_factory(
            [
                _dom("H 射门", "45:01 1 : 0"),
                _dom("H 进攻", "45:06 1 : 0", ["attack-move"]),
            ],
            baseline=_dom("H 进球", "44:56 1 : 0"),
        )
        pg.PitchGateCoordinator._open_dom_reader = factory  # type: ignore[assignment]
        pg.reset_coordinator_for_tests()
        af_unclear = _FakeAf([{"ok": True, "score_match": True, "af_score": "1-0"}])
        af_mod.set_active_observer(af_unclear)  # type: ignore[arg-type]
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "data" / "pm-quote").mkdir(parents=True)
            obs_u = _FakeObserver(root)
            set_active_observer(obs_u)  # type: ignore[arg-type]
            coord = pg.get_coordinator(root)
            assert coord.start_gate({**ev, "match_id": "m3u"}, event_key="k3u")
            done = _wait_done(coord, n=2)
            assert "in_play" in [d["status"] for d in done], done
            assert (obs_u.rows[0].get("af") or {}).get("skipped") == "before_in_play"
            buy = next(d for d in done if d["status"] == "in_play")
            assert buy["sample_i"] == 1, buy
            assert af_unclear.calls == 1, af_unclear.calls

        # First in_play AF miss, next in_play match → buy; keep polling until AND.
        factory, _ = _open_factory(
            [
                _dom("H 进攻", "45:06 1 : 0", ["attack-move"]),
                _dom("H 进攻", "45:11 1 : 0", ["attack-move"]),
            ],
            baseline=_dom("H 进球", "44:56 1 : 0"),
        )
        pg.PitchGateCoordinator._open_dom_reader = factory  # type: ignore[assignment]
        pg.reset_coordinator_for_tests()
        af_late = _FakeAf(
            [
                {"ok": True, "score_match": False, "af_score": "0-0"},
                {"ok": True, "score_match": True, "af_score": "1-0"},
            ]
        )
        af_mod.set_active_observer(af_late)  # type: ignore[arg-type]
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "data" / "pm-quote").mkdir(parents=True)
            set_active_observer(_FakeObserver(root))  # type: ignore[arg-type]
            coord = pg.get_coordinator(root)
            assert coord.start_gate({**ev, "match_id": "m3late"}, event_key="k3late")
            done = _wait_done(coord, n=2)
            assert "in_play" in [d["status"] for d in done], done
            buy = next(d for d in done if d["status"] == "in_play")
            assert buy["sample_i"] == 1, buy
            assert af_late.calls == 2, af_late.calls

        # VAR still vetoes even after later in_play + AF.
        factory, _ = _open_factory(
            [
                _dom("H 射门", "45:01 1 : 0"),
                _dom("VAR 回看中", "45:06 1 : 0"),
                _dom("H 进攻", "45:11 1 : 0", ["attack-move", "ball"]),
            ],
            baseline=_dom("H 进球", "44:56 1 : 0"),
        )
        pg.PitchGateCoordinator._open_dom_reader = factory  # type: ignore[assignment]
        pg.reset_coordinator_for_tests()
        af_var = _FakeAf([{"ok": True, "score_match": True, "af_score": "1-0"}])
        af_mod.set_active_observer(af_var)  # type: ignore[arg-type]
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "data" / "pm-quote").mkdir(parents=True)
            set_active_observer(_FakeObserver(root))  # type: ignore[arg-type]
            coord = pg.get_coordinator(root)
            assert coord.start_gate({**ev, "match_id": "m3v"}, event_key="k3v")
            done = _wait_done(coord, n=1)
            assert done[0]["status"] == "var_veto", done
            assert done[0].get("buy_emitted") is False
            assert af_var.calls == 0, af_var.calls

        # Reversal observe: DOM board matches but AF is rate-limited → hold.
        # Stale/celebration center-box must not flatten without AF.
        rev_frames = [
            _dom("H 控球", "70:01 1 : 0", ["possession-rect"]),
            _dom("H 控球", "70:06 1 : 0", ["possession-rect"]),
            _dom("H 控球", "70:11 1 : 0", ["possession-rect"]),
        ]
        factory, readers_rev_stop = _open_factory(
            rev_frames, baseline=_dom("H 控球", "69:56 1 : 0", ["possession-rect"])
        )
        pg.PitchGateCoordinator._open_dom_reader = factory  # type: ignore[assignment]
        pg.reset_coordinator_for_tests()
        af_rev_stop = _FakeAf(
            [{"ok": False, "score_match": None, "error": "rate_limit"}]
        )
        af_mod.set_active_observer(af_rev_stop)  # type: ignore[arg-type]
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "data" / "pm-quote").mkdir(parents=True)
            obs_rev = _FakeObserver(root)
            set_active_observer(obs_rev)  # type: ignore[arg-type]
            coord = pg.get_coordinator(root)
            rev = {
                **ev,
                "match_id": "m4",
                "home_score": 1,
                "away_score": 0,
                "is_reversal": True,
                "observe_only": True,
            }
            assert coord.start_gate(rev, event_key="k4", observe_only=True)
            done = _wait_done(coord, n=1)
            assert all(d["status"] != "flatten_or" for d in done), done
            assert done[-1]["status"] == "observe_complete", done
            assert "no_af_confirm" in str(done[-1].get("reason") or ""), done[-1]
            assert af_rev_stop.calls >= 1, af_rev_stop.calls
            assert readers_rev_stop and readers_rev_stop[-1].closed

        # Reversal: celebration overlay on post-reverse board, AF still down → hold.
        factory, _ = _open_factory(
            [_dom("H 进球", "70:06 1 : 0")],
            baseline=_dom("H 进球", "69:56 1 : 0"),
        )
        pg.PitchGateCoordinator._open_dom_reader = factory  # type: ignore[assignment]
        pg.reset_coordinator_for_tests()
        af_cel = _FakeAf([{"ok": False, "score_match": None, "error": "rate_limit"}])
        af_mod.set_active_observer(af_cel)  # type: ignore[arg-type]
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "data" / "pm-quote").mkdir(parents=True)
            set_active_observer(_FakeObserver(root))  # type: ignore[arg-type]
            coord = pg.get_coordinator(root)
            rev = {
                **ev,
                "match_id": "m4c",
                "home_score": 1,
                "away_score": 0,
                "is_reversal": True,
                "observe_only": True,
            }
            assert coord.start_gate(rev, event_key="k4c", observe_only=True)
            done = _wait_done(coord, n=1)
            assert all(d["status"] != "flatten_or" for d in done), done
            assert done[-1]["status"] == "observe_complete", done
            assert af_cel.calls >= 1, af_cel.calls

        # Reversal: tracker will not open, AF score_match → flatten_or.
        def _fail_open(_self, _session, _observer):
            return None, "playwright_browser_missing", {}

        pg.PitchGateCoordinator._open_dom_reader = _fail_open  # type: ignore[assignment]
        pg.reset_coordinator_for_tests()
        af_dom_miss = _FakeAf([{"ok": True, "score_match": True, "af_score": "1-0"}])
        af_mod.set_active_observer(af_dom_miss)  # type: ignore[arg-type]
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "data" / "pm-quote").mkdir(parents=True)
            set_active_observer(_FakeObserver(root))  # type: ignore[arg-type]
            coord = pg.get_coordinator(root)
            rev = {
                **ev,
                "match_id": "m4d",
                "home_score": 1,
                "away_score": 0,
                "is_reversal": True,
                "observe_only": True,
            }
            assert coord.start_gate(rev, event_key="k4d", observe_only=True)
            done = _wait_done(coord, n=2)
            assert "flatten_or" in [d["status"] for d in done], done
            flat = next(d for d in done if d["status"] == "flatten_or")
            assert "af" in str(flat.get("reason")), flat
            complete = next(d for d in done if d["status"] == "observe_complete")
            assert complete.get("reason") == "flatten_or_stop_trail", complete
            assert complete.get("frames") == 1, complete
            assert af_dom_miss.calls == 1, af_dom_miss.calls
            af_n = af_dom_miss.calls
            time.sleep(0.2)
            assert af_dom_miss.calls == af_n, af_dom_miss.calls

        # Odds HTTP must not stall the AND buy.
        def _slow_odds(self, session, *, sample_i, elapsed_s):  # noqa: ANN001
            time.sleep(0.45)
            return {"level": "C", "reason": "slow"}

        pg.PitchGateCoordinator._sample_odds = _slow_odds  # type: ignore[assignment]
        factory, _ = _open_factory(
            [
                _dom("H 进球", "45:01 1 : 0", ["ball"]),
                _dom("H 进攻", "45:06 1 : 0", ["attack-move"]),
            ],
            baseline=_dom("H 进球", "44:56 1 : 0"),
        )
        pg.PitchGateCoordinator._open_dom_reader = factory  # type: ignore[assignment]
        pg.reset_coordinator_for_tests()
        af_mod.set_active_observer(
            _FakeAf([{"ok": True, "score_match": True, "af_score": "1-0"}])  # type: ignore[arg-type]
        )
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "data" / "pm-quote").mkdir(parents=True)
            set_active_observer(_FakeObserver(root))  # type: ignore[arg-type]
            coord = pg.get_coordinator(root)
            t0 = time.monotonic()
            assert coord.start_gate({**ev, "match_id": "m1o"}, event_key="k1o")
            done = _wait_done(coord, n=1)
            wall = time.monotonic() - t0
            assert "in_play" in [d["status"] for d in done], done
            assert wall < 0.4, wall
        pg.PitchGateCoordinator._sample_odds = orig_odds  # type: ignore[assignment]

        def _odds_a(_self, _session, *, sample_i, elapsed_s):  # noqa: ANN001, ARG001
            return {
                "level": "A",
                "reason": "oddsapiio_score_matches_and_bet365_open",
            }

        # Grade A + AF, never in_play, no 射门 → no buy (A does not skip DOM).
        pg.PitchGateCoordinator._sample_odds = _odds_a  # type: ignore[assignment]
        factory, _ = _open_factory(
            [_dom("H 进球", "45:01 1 : 0")] * 8,
            baseline=_dom("H 进球", "44:56 1 : 0"),
        )
        pg.PitchGateCoordinator._open_dom_reader = factory  # type: ignore[assignment]
        pg.reset_coordinator_for_tests()
        af_early = _FakeAf([{"ok": True, "score_match": True, "af_score": "1-0"}])
        af_mod.set_active_observer(af_early)  # type: ignore[arg-type]
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "data" / "pm-quote").mkdir(parents=True)
            set_active_observer(_FakeObserver(root))  # type: ignore[arg-type]
            coord = pg.get_coordinator(root)
            assert coord.start_gate({**ev, "match_id": "m_a_early"}, event_key="k_a_early")
            done = _wait_done(coord, n=1)
            assert done[0]["status"] == "timeout", done
            assert done[0].get("buy_emitted") is False
            assert "never_in_play" in str(done[0].get("reason") or ""), done[0]
            assert af_early.calls == 0, af_early.calls

        # Grade A + in_play 进攻 + AF → buy (射门 is not a gate; A still does not skip DOM).
        pg.PitchGateCoordinator._sample_odds = _odds_a  # type: ignore[assignment]
        factory, _ = _open_factory(
            [_dom("H 进攻", "45:06 1 : 0", ["attack-move"])] * 8,
            baseline=_dom("H 进球", "44:56 1 : 0"),
        )
        pg.PitchGateCoordinator._open_dom_reader = factory  # type: ignore[assignment]
        pg.reset_coordinator_for_tests()
        af_noshot_a = _FakeAf([{"ok": True, "score_match": True, "af_score": "1-0"}])
        af_mod.set_active_observer(af_noshot_a)  # type: ignore[arg-type]
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "data" / "pm-quote").mkdir(parents=True)
            set_active_observer(_FakeObserver(root))  # type: ignore[arg-type]
            coord = pg.get_coordinator(root)
            assert coord.start_gate({**ev, "match_id": "m_a_shot"}, event_key="k_a_shot")
            done = _wait_done(coord, n=2)
            assert "in_play" in [d["status"] for d in done], done
            assert af_noshot_a.calls >= 1, af_noshot_a.calls

        # AF fixture hole + in_play + Grade A → wait AF, A does not stand in.
        pg.PitchGateCoordinator._sample_odds = _odds_a  # type: ignore[assignment]
        factory, _ = _open_factory(
            [_dom("H 进攻", "45:06 1 : 0", ["attack-move", "ball"])] * 8,
            baseline=_dom("H 进球", "44:56 1 : 0"),
        )
        pg.PitchGateCoordinator._open_dom_reader = factory  # type: ignore[assignment]
        pg.reset_coordinator_for_tests()
        af_hole = _FakeAf(
            [
                {
                    "ok": False,
                    "score_match": None,
                    "error": "af_fixture_unresolved_ttl",
                }
            ]
        )
        af_mod.set_active_observer(af_hole)  # type: ignore[arg-type]
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "data" / "pm-quote").mkdir(parents=True)
            set_active_observer(_FakeObserver(root))  # type: ignore[arg-type]
            coord = pg.get_coordinator(root)
            assert coord.start_gate({**ev, "match_id": "m_a_af"}, event_key="k_a_af")
            done = _wait_done(coord, n=1)
            assert done[0]["status"] == "timeout", done
            assert done[0].get("buy_emitted") is False
            assert "no_aligned_buy" in str(done[0].get("reason") or ""), done[0]
            assert af_hole.calls >= 1, af_hole.calls

        # Grade A does not override a hard AF score mismatch.
        pg.PitchGateCoordinator._sample_odds = _odds_a  # type: ignore[assignment]
        factory, _ = _open_factory(
            [_dom("H 进攻", "45:06 1 : 0", ["attack-move", "ball"])] * 8,
            baseline=_dom("H 进球", "44:56 1 : 0"),
        )
        pg.PitchGateCoordinator._open_dom_reader = factory  # type: ignore[assignment]
        pg.reset_coordinator_for_tests()
        af_mod.set_active_observer(
            _FakeAf([{"ok": True, "score_match": False, "af_score": "0-0"}])  # type: ignore[arg-type]
        )
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "data" / "pm-quote").mkdir(parents=True)
            set_active_observer(_FakeObserver(root))  # type: ignore[arg-type]
            coord = pg.get_coordinator(root)
            assert coord.start_gate({**ev, "match_id": "m_a_mis"}, event_key="k_a_mis")
            done = _wait_done(coord, n=1)
            assert done[0]["status"] == "timeout", done
            assert done[0].get("buy_emitted") is False
            assert "no_aligned_buy" in str(done[0].get("reason") or ""), done[0]
        pg.PitchGateCoordinator._sample_odds = orig_odds  # type: ignore[assignment]

        # Reversal: AF score_match vs post-reverse, DOM still on old score → flatten_or.
        factory, _ = _open_factory(
            [_dom("H 进攻", "70:06 2 : 0", ["attack-move"])] * 4,
            baseline=_dom("H 进攻", "69:56 2 : 0", ["attack-move"]),
        )
        pg.PitchGateCoordinator._open_dom_reader = factory  # type: ignore[assignment]
        pg.reset_coordinator_for_tests()
        af_mod.set_active_observer(
            _FakeAf([{"ok": True, "score_match": True, "af_score": "1-0"}])  # type: ignore[arg-type]
        )
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "data" / "pm-quote").mkdir(parents=True)
            set_active_observer(_FakeObserver(root))  # type: ignore[arg-type]
            coord = pg.get_coordinator(root)
            rev = {
                **ev,
                "match_id": "m4b",
                "home_score": 1,
                "away_score": 0,
                "is_reversal": True,
                "observe_only": True,
            }
            assert coord.start_gate(rev, event_key="k4b", observe_only=True)
            done = _wait_done(coord, n=2)
            statuses = [d["status"] for d in done]
            assert "flatten_or" in statuses, done
            flat = next(d for d in done if d["status"] == "flatten_or")
            assert "af" in str(flat.get("reason")), flat

        # Reversal: neither AF nor DOM match → hold (no flatten_or).
        factory, _ = _open_factory(
            [_dom("H 进攻", "70:06 2 : 0", ["attack-move"])] * 8,
            baseline=_dom("H 进攻", "69:56 2 : 0", ["attack-move"]),
        )
        pg.PitchGateCoordinator._open_dom_reader = factory  # type: ignore[assignment]
        pg.reset_coordinator_for_tests()
        af_mod.set_active_observer(
            _FakeAf([{"ok": True, "score_match": False, "af_score": "2-0"}])  # type: ignore[arg-type]
        )
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "data" / "pm-quote").mkdir(parents=True)
            set_active_observer(_FakeObserver(root))  # type: ignore[arg-type]
            coord = pg.get_coordinator(root)
            rev = {
                **ev,
                "match_id": "m5",
                "home_score": 1,
                "away_score": 0,
                "is_reversal": True,
                "observe_only": True,
            }
            assert coord.start_gate(rev, event_key="k5", observe_only=True)
            done = _wait_done(coord, n=1)
            assert all(d["status"] != "flatten_or" for d in done), done
            assert done[-1]["status"] == "observe_complete", done

        # Cancel mid-session.
        factory, _ = _open_factory(
            [_dom("H 进球", "45:01 1 : 0")] * 20,
            baseline=_dom("H 进球", "44:56 1 : 0"),
        )
        pg.PitchGateCoordinator._open_dom_reader = factory  # type: ignore[assignment]
        pg.reset_coordinator_for_tests()
        af_mod.set_active_observer(
            _FakeAf([{"ok": False, "score_match": None}])  # type: ignore[arg-type]
        )
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "data" / "pm-quote").mkdir(parents=True)
            coord = pg.get_coordinator(root)
            obs6 = _FakeObserver(root)
            set_active_observer(obs6)  # type: ignore[arg-type]
            af6 = _FakeAf([{"ok": False, "score_match": None}])
            af_mod.set_active_observer(af6)  # type: ignore[arg-type]
            assert coord.start_gate({**ev, "match_id": "m6"}, event_key="k6")
            time.sleep(0.02)
            assert coord.cancel_match("m6") >= 1
            done = _wait_done(coord, n=1)
            assert done[0]["status"] == "canceled", done
            rows_after_cancel = len(obs6.rows)
            af_after_cancel = af6.calls
            assert coord.start_gate({**ev, "match_id": "m6"}, event_key="k6") is False
            time.sleep(0.12)
            assert coord.drain_done() == []
            assert len(obs6.rows) == rows_after_cancel, obs6.rows
            assert af6.calls == af_after_cancel, af6.calls

        # invert keeps ts suffix.
        inv = pg.invert_score_change_key(
            "score_change|m1|0-0->1-0|2026-08-23T12:00:00+08:00"
        )
        assert inv == "score_change|m1|1-0->0-0|2026-08-23T12:00:00+08:00", inv
        ek = lib.event_key(
            {
                "type": "score_change",
                "match_id": "m1",
                "prev": {"home": 0, "away": 0},
                "curr": {"home": 1, "away": 0},
                "ts": "2026-08-23T12:00:00+08:00",
            }
        )
        assert ek.endswith("|2026-08-23T12:00:00+08:00"), ek
        assert "0-0->1-0" in ek

        # Reverse ts must block the undone goal (any earlier ts) but not a re-award.
        pg.reset_coordinator_for_tests()
        with tempfile.TemporaryDirectory() as td:
            coord_b = pg.get_coordinator(Path(td))
            rev_key = "score_change|54483562|1-1->0-1|2026-08-23T21:51:01+08:00"
            old_goal = "score_change|54483562|0-1->1-1|2026-08-23T21:50:47+08:00"
            new_goal = "score_change|54483562|0-1->1-1|2026-08-23T21:55:00+08:00"
            coord_b.block_inverted_goal(rev_key)
            assert coord_b.has_consumed_event(old_goal), "undone 1-1 must not reopen AF/DOM"
            assert not coord_b.has_consumed_event(new_goal), "re-awarded 1-1 must still gate"
            assert (
                coord_b.start_gate(
                    {**ev, "match_id": "54483562"}, event_key=old_goal
                )
                is False
            )

        # Same-tick 0-1→1-1 then 1-1→0-1: never open AF/DOM for the undone goal.
        factory, readers_rev = _open_factory(
            [_dom("H 进球", "45:01 1 : 1")] * 20,
            baseline=_dom("H 进球", "44:56 1 : 1"),
        )
        pg.PitchGateCoordinator._open_dom_reader = factory  # type: ignore[assignment]
        pg.reset_coordinator_for_tests()
        af_rev = _FakeAf([{"ok": True, "score_match": False, "af_score": "0-1"}] * 20)
        af_mod.set_active_observer(af_rev)  # type: ignore[arg-type]
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "data" / "pm-quote").mkdir(parents=True)
            set_active_observer(_FakeObserver(root))  # type: ignore[arg-type]
            t0 = datetime.now(timezone(timedelta(hours=8)))
            ts_goal = t0.isoformat(timespec="seconds")
            ts_rev = (t0 + timedelta(seconds=14)).isoformat(timespec="seconds")
            goal_ev = {
                **ev,
                "match_id": "54483562",
                "home_score": 1,
                "away_score": 1,
                "prev": {"home": 0, "away": 1},
                "curr": {"home": 1, "away": 1},
                "ts": ts_goal,
                "polymarket": {"event_id": "e1", "slug": "x"},
            }
            rev_ev = {
                **goal_ev,
                "home_score": 0,
                "away_score": 1,
                "prev": {"home": 1, "away": 1},
                "curr": {"home": 0, "away": 1},
                "ts": ts_rev,
                "is_reversal": True,
            }
            with patch.object(lib, "load_bridge_quote_events", return_value=([], 0)):
                with patch.object(lib, "persist_bundle", return_value=None):
                    bundles = lib.process_bridge_events(
                        root, events_override=[goal_ev, rev_ev]
                    )
            time.sleep(0.12)
            assert af_rev.calls == 0, af_rev.calls
            assert readers_rev == [], readers_rev
            modes = [b.get("mode") for b in bundles]
            assert "dqd_reversal_pitch_gate_canceled" in modes, modes

        # Running 1-1 gate + reverse while quote tick is stuck in rest reconcile:
        # cancel must happen at tick start (not after the sleep).
        class _SlowEx:
            def __init__(self) -> None:
                self.ledger = self
                self.reconcile_slept = False

            def open_for_match(self, _mid):  # noqa: ANN001
                return []

            def retry_pending_flattens(self):
                return []

            def reconcile_rest_orders(self):
                self.reconcile_slept = True
                time.sleep(0.25)
                return []

            def cancel_rest_orders_for_match(self, *_a, **_k):
                return []

            def clear_rest_block(self, *_a, **_k):
                return None

            def maybe_flatten_for_event(self, *_a, **_k):
                return []

        factory, _ = _open_factory(
            [_dom("H 进球", "45:01 1 : 1")] * 40,
            baseline=_dom("H 进球", "44:56 1 : 1"),
        )
        pg.PitchGateCoordinator._open_dom_reader = factory  # type: ignore[assignment]
        pg.reset_coordinator_for_tests()
        af_slow = _FakeAf([{"ok": True, "score_match": False, "af_score": "0-1"}] * 40)
        af_mod.set_active_observer(af_slow)  # type: ignore[arg-type]
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "data" / "pm-quote").mkdir(parents=True)
            set_active_observer(_FakeObserver(root))  # type: ignore[arg-type]
            coord = pg.get_coordinator(root)
            t0 = datetime.now(timezone(timedelta(hours=8)))
            ts_goal = t0.isoformat(timespec="seconds")
            ts_rev = (t0 + timedelta(seconds=2)).isoformat(timespec="seconds")
            goal_ev = {
                **ev,
                "match_id": "54473848",
                "home_score": 1,
                "away_score": 1,
                "prev": {"home": 0, "away": 1},
                "curr": {"home": 1, "away": 1},
                "ts": ts_goal,
                "polymarket": {"event_id": "e1", "slug": "x"},
            }
            rev_ev = {
                **goal_ev,
                "home_score": 0,
                "away_score": 1,
                "prev": {"home": 1, "away": 1},
                "curr": {"home": 0, "away": 1},
                "ts": ts_rev,
                "is_reversal": True,
            }
            goal_key = lib.event_key(goal_ev)
            assert coord.start_gate(goal_ev, event_key=goal_key)
            time.sleep(0.05)
            n_before = af_slow.calls
            ex = _SlowEx()
            pg.GATE_TIMEOUT_S = 2.0
            with patch.object(lib, "load_bridge_quote_events", return_value=([], 0)):
                with patch.object(lib, "persist_bundle", return_value=None):
                    lib.process_bridge_events(
                        root, events_override=[rev_ev], trade_executor=ex
                    )
            assert ex.reconcile_slept
            time.sleep(0.12)
            assert af_slow.calls <= n_before + 1, af_slow.calls
            lib.apply_dqd_reversal_cancel(root, rev_ev)
            time.sleep(0.12)
            n_hook = af_slow.calls
            time.sleep(0.12)
            assert af_slow.calls == n_hook, af_slow.calls

        # Opening re-award after the same transition reversed → skip (no DOM).
        # First opening at ~35' (Delfin) and non-opening re-award (Maranhão) do not skip.
        pg.reset_coordinator_for_tests()
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "data" / "pm-quote").mkdir(parents=True)
            coord = pg.get_coordinator(root)
            open_goal = {
                "type": "score_change",
                "match_id": "m_open_rev",
                "home": "H",
                "away": "A",
                "home_score": 1,
                "away_score": 0,
                "prev": {"home": 0, "away": 0},
                "curr": {"home": 1, "away": 0},
                "official_clock": "36'",
                "ts": "2026-08-24T12:00:00+08:00",
                "polymarket": {"event_id": "e1"},
            }
            coord.note_reversal(
                {
                    **open_goal,
                    "home_score": 0,
                    "prev": {"home": 1, "away": 0},
                    "curr": {"home": 0, "away": 0},
                    "ts": "2026-08-24T12:01:00+08:00",
                    "is_reversal": True,
                }
            )
            reaward = {**open_goal, "ts": "2026-08-24T12:02:00+08:00"}
            assert coord.start_gate(reaward, event_key=lib.event_key(reaward)) is False
            skipped = coord.drain_done()
            assert skipped and skipped[0]["status"] == "reversal_risk_skip", skipped

            delfin = {**open_goal, "match_id": "m_delfin"}
            coord.start_gate(delfin, event_key=lib.event_key(delfin))
            delfin_done = coord.drain_done()
            assert all(d["status"] != "reversal_risk_skip" for d in delfin_done), delfin_done

            late = {
                **open_goal,
                "match_id": "m_late",
                "official_clock": "90'+2'",
                "status": "Playing 90'",
            }
            assert coord.start_gate(late, event_key=lib.event_key(late)) is False
            late_done = coord.drain_done()
            assert late_done and late_done[0]["status"] == "reversal_risk_skip", late_done

            mar = {
                **open_goal,
                "match_id": "m_mar",
                "home_score": 3,
                "prev": {"home": 2, "away": 0},
                "curr": {"home": 3, "away": 0},
                "official_clock": "25'",
            }
            coord.note_reversal(
                {
                    **mar,
                    "home_score": 2,
                    "prev": {"home": 3, "away": 0},
                    "curr": {"home": 2, "away": 0},
                }
            )
            coord.start_gate(mar, event_key=lib.event_key(mar))
            mar_done = coord.drain_done()
            assert all(d["status"] != "reversal_risk_skip" for d in mar_done), mar_done

        print("ok: pitch_gate AND only / Odds A observe-only / stop AF+DOM after buy / AF-only flatten")
        return 0
    finally:
        pg.PitchGateCoordinator._open_dom_reader = orig_open  # type: ignore[assignment]
        (
            pg.GATE_FIRST_DELAY_S,
            pg.GATE_INTERVAL_S,
            pg.GATE_TIMEOUT_S,
        ) = old
        pg.PitchGateCoordinator._sample_odds = orig_odds  # type: ignore[assignment]
        af_mod.set_active_observer(None)
        set_active_observer(None)
        pg.reset_coordinator_for_tests()


if __name__ == "__main__":
    raise SystemExit(main())
