#!/usr/bin/env python3
"""Smoke: same-tick DOM∧AF buy, stop after buy, AF∨DOM reversal flatten."""

from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

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
        # AND buy: DOM in_play + AF score_match on same tick → stop (no extra frames).
        frames = [
            _dom("H 进球", "45:01 1 : 0"),
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
            assert len(obs.rows) == 2, [r.get("sample_i") for r in obs.rows]
            assert af.calls == 2, af.calls
            assert readers[-1].closed
            assert not any(r.get("frame_path") for r in obs.rows)

        # DOM in_play but AF never matches → no buy.
        factory, _ = _open_factory(
            [_dom("H 进攻", "45:06 1 : 0", ["attack-move"])] * 8,
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

        # AF error fail-closed.
        factory, _ = _open_factory(
            [_dom("H 进攻", "45:06 1 : 0", ["attack-move"])] * 8,
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

        # Reversal observe: DOM score_match vs post-reverse → flatten_or.
        rev_frames = [
            _dom("H 控球", "70:01 1 : 0", ["possession-rect"]),
            _dom("H 控球", "70:06 1 : 0", ["possession-rect"]),
        ]
        factory, _ = _open_factory(
            rev_frames, baseline=_dom("H 控球", "69:56 1 : 0", ["possession-rect"])
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
            rev = {
                **ev,
                "match_id": "m4",
                "home_score": 1,
                "away_score": 0,
                "is_reversal": True,
                "observe_only": True,
            }
            assert coord.start_gate(rev, event_key="k4", observe_only=True)
            done = _wait_done(coord, n=2)
            statuses = [d["status"] for d in done]
            assert "flatten_or" in statuses, done
            assert "observe_complete" in statuses, done
            flat = next(d for d in done if d["status"] == "flatten_or")
            assert "dom" in str(flat.get("reason")), flat

        # Reversal: celebration overlay still showing post-reverse board score → flatten.
        factory, _ = _open_factory(
            [_dom("H 进球", "70:06 1 : 0")],
            baseline=_dom("H 进球", "69:56 1 : 0"),
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
            rev = {
                **ev,
                "match_id": "m4c",
                "home_score": 1,
                "away_score": 0,
                "is_reversal": True,
                "observe_only": True,
            }
            assert coord.start_gate(rev, event_key="k4c", observe_only=True)
            done = _wait_done(coord, n=2)
            assert "flatten_or" in [d["status"] for d in done], done
            flat = next(d for d in done if d["status"] == "flatten_or")
            assert "dom" in str(flat.get("reason")), flat

        # Reversal: tracker will not open, AF score_match → flatten_or.
        def _fail_open(_self, _session, _observer):
            return None, "playwright_browser_missing", {}

        pg.PitchGateCoordinator._open_dom_reader = _fail_open  # type: ignore[assignment]
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

        # Odds HTTP must not stall the AND buy.
        def _slow_odds(self, session, *, sample_i, elapsed_s):  # noqa: ANN001
            time.sleep(0.45)
            return {"level": "C", "reason": "slow"}

        pg.PitchGateCoordinator._sample_odds = _slow_odds  # type: ignore[assignment]
        factory, _ = _open_factory(
            [
                _dom("H 进球", "45:01 1 : 0"),
                _dom("H 进攻", "45:06 1 : 0", ["attack-move"]),
            ],
            baseline=_dom("H 进球", "44:56 1 : 0"),
        )
        pg.PitchGateCoordinator._open_dom_reader = factory  # type: ignore[assignment]
        pg.reset_coordinator_for_tests()
        af_mod.set_active_observer(
            _FakeAf(
                [
                    {"ok": True, "score_match": False, "af_score": "0-0"},
                    {"ok": True, "score_match": True, "af_score": "1-0"},
                ]
            )  # type: ignore[arg-type]
        )
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "data" / "pm-quote").mkdir(parents=True)
            set_active_observer(_FakeObserver(root))  # type: ignore[arg-type]
            coord = pg.get_coordinator(root)
            t0 = time.monotonic()
            assert coord.start_gate({**ev, "match_id": "m1o"}, event_key="k1o")
            done = _wait_done(coord, n=2)
            wall = time.monotonic() - t0
            assert "in_play" in [d["status"] for d in done], done
            assert wall < 0.4, wall
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
            set_active_observer(_FakeObserver(root))  # type: ignore[arg-type]
            coord = pg.get_coordinator(root)
            assert coord.start_gate({**ev, "match_id": "m6"}, event_key="k6")
            time.sleep(0.02)
            assert coord.cancel_match("m6") >= 1
            done = _wait_done(coord, n=1)
            assert done[0]["status"] == "canceled", done

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

        print("ok: pitch_gate AND buy / stop / AF∨DOM flatten / no JPEG")
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
