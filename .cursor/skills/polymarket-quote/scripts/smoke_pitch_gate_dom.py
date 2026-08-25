#!/usr/bin/env python3
"""Smoke: DOM-mode pitch-gate (no screenshot, no OCR) + judge_dom rules."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))
_PITCH_STATE = _SCRIPTS.parents[1] / "pitch-state" / "scripts"
if str(_PITCH_STATE) not in sys.path:
    sys.path.insert(0, str(_PITCH_STATE))

import af_observe as af_mod  # noqa: E402
import animation_rules as rules  # noqa: E402
import pitch_gate as pg  # noqa: E402
from dqd_stream_observe import observe_path, set_active_observer  # noqa: E402


def _dom(pop: str, center: str, marks: list[str] | None = None) -> dict[str, Any]:
    return {
        "pop_box": pop,
        "pop_class": "pop-box home",
        "center_box": center,
        "marks": marks or [],
        "root_class": "football-animate",
    }


class _FakeReader:
    """Stands in for the persistent tracker page.

    The gate reads once at open to baseline the clock, then once per sample, so
    the fake serves ``baseline`` first and walks ``frames`` after that.
    """

    def __init__(
        self, frames: list[dict[str, Any] | None], *, baseline: dict[str, Any] | None
    ) -> None:
        self.queue = [baseline, *frames]
        self.i = 0
        self.opened = False
        self.closed = False

    def open(self) -> tuple[bool, str | None]:
        self.opened = True
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
        import quote_lib as lib

        lib.append_jsonl(observe_path(self.root), rows)


def check_parse_center() -> None:
    cases = {
        # raw: (clock, home, away)
        "78:57 1 : 0": ("78:57", 1, 0),
        "34:03  1 : 0": ("34:03", 1, 0),
        "9:05 10 : 2": ("9:05", 10, 2),
        # Clock still syncing renders single-digit seconds.
        "0:0 0 : 0": ("0:0", 0, 0),
        # Unspaced board still parses via the clock-first fallback.
        "35:12 1:0": ("35:12", 1, 0),
        # Score with no clock at all.
        "1 : 0": (None, 1, 0),
    }
    for raw, (clock, home, away) in cases.items():
        got = rules.parse_dom_center(raw)
        assert (got["clock"], got["home"], got["away"]) == (clock, home, away), (raw, got)
        assert got["text"] == f"{home}-{away}", (raw, got)

    # A bare clock must never become a scoreline (the classic OCR failure).
    for raw in ("45:00", "9:05", "90:00"):
        got = rules.parse_dom_center(raw)
        assert got["text"] is None, (raw, got)
        assert got["clock"] is not None, (raw, got)

    for raw in ("", "暂无动画", None):
        got = rules.parse_dom_center(raw)
        assert got["text"] is None and got["clock"] is None, (raw, got)


def check_judge_dom() -> None:
    exp = {"expected_home": 1, "expected_away": 0, "require_score": True}

    j = rules.judge_dom(_dom("阿森纳 进攻", "35:12 1 : 0"), **exp)
    assert j["play_state"] == "in_play" and j["score_match"] is True, j
    assert j["source"] == "dom" and j["dom_clock"] == "35:12", j

    # VAR anywhere in the overlay vetoes, before the score is even considered.
    j = rules.judge_dom(_dom("VAR 回看中", "35:12 1 : 0"), **exp)
    assert j["play_state"] == "stopped" and j["stopped_reason"] == "var", j

    # Board still behind the DQD score → not in_play.
    j = rules.judge_dom(_dom("阿森纳 进攻", "35:12 0 : 0"), **exp)
    assert j["play_state"] == "unclear" and j["score_match"] is False, j

    # Celebration overlay is a stop, not a restart.
    j = rules.judge_dom(_dom("阿森纳 进球", "35:12 1 : 0"), **exp)
    assert j["play_state"] == "stopped" and j["stopped_reason"] == "celebration", j

    # A frozen page repeats its last state; the clock exposes it.
    j = rules.judge_dom(_dom("阿森纳 进攻", "35:12 1 : 0"), prev_clock="35:12", **exp)
    assert j["play_state"] == "unclear" and j["stopped_reason"] == "stale_page", j
    j = rules.judge_dom(_dom("阿森纳 进攻", "35:13 1 : 0"), prev_clock="35:12", **exp)
    assert j["play_state"] == "in_play", j

    # Celebration / VAR / stale clock: play_state is not in_play, but the board
    # score is still usable for reversal flatten.
    assert rules.board_score_match(
        _dom("阿森纳 进球", "35:12 1 : 0"), expected_home=1, expected_away=0
    ) is True
    assert rules.board_score_match(
        _dom("VAR 回看中", "35:12 1 : 0"), expected_home=1, expected_away=0
    ) is True
    stale = _dom("阿森纳 进攻", "35:12 1 : 0")
    assert rules.judge_dom(stale, prev_clock="35:12", **exp)["score_match"] is None
    assert rules.board_score_match(stale, expected_home=1, expected_away=0) is True
    assert rules.board_score_match(
        _dom("阿森纳 进攻", "35:12 0 : 0"), expected_home=1, expected_away=0
    ) is False

    # Empty / missing DOM must never open the gate.
    for bad in (None, {}, _dom("", "")):
        j = rules.judge_dom(bad, **exp)
        assert j["play_state"] == "unclear", (bad, j)

    # 「伤停补时」 is a banner, not a pause.
    j = rules.judge_dom(_dom("阿森纳 进攻 伤停补时", "45:30 1 : 0"), **exp)
    assert j["play_state"] == "in_play", j

    # Without require_score the keyword alone decides.
    j = rules.judge_dom(_dom("阿森纳 控球", "35:12 9 : 9"), expected_home=1, expected_away=0)
    assert j["play_state"] == "in_play", j

    # 射门 is a latch overlay, not an in_play token.
    j = rules.judge_dom(_dom("阿森纳 射门", "35:12 1 : 0"), **exp)
    assert j["play_state"] != "in_play", j
    assert pg._dom_shows_shot(_dom("阿森纳 射门", "35:12 1 : 0"))
    assert pg._dom_shows_shot(_dom("阿森纳 进攻", "35:12 1 : 0", ["ball"]))
    assert pg._dom_shows_shot(_dom("阿森纳 进攻", "35:12 1 : 0", ["net"]))
    assert not pg._dom_shows_shot(_dom("阿森纳 进攻", "35:12 1 : 0", ["attack-move"]))


def check_gate_source_env() -> None:
    old = os.environ.get("QUOTE_GATE_SOURCE")
    try:
        for raw in ("", "dom", "OCR", "bogus"):
            os.environ["QUOTE_GATE_SOURCE"] = raw
            assert pg.gate_source() == "dom", (raw, pg.gate_source())
        os.environ["QUOTE_PITCH_STATE"] = "0"
        os.environ["QUOTE_DQD_STREAM_OBSERVE"] = "1"
        assert pg.gate_ready()[0] is True, pg.gate_ready()
        os.environ["QUOTE_DQD_STREAM_OBSERVE"] = "0"
        assert pg.gate_ready() == (False, "QUOTE_DQD_STREAM_OBSERVE=0"), pg.gate_ready()
        os.environ["QUOTE_DQD_STREAM_OBSERVE"] = "1"
    finally:
        os.environ["QUOTE_PITCH_STATE"] = "1"
        if old is None:
            os.environ.pop("QUOTE_GATE_SOURCE", None)
        else:
            os.environ["QUOTE_GATE_SOURCE"] = old


def _wait_done(coord: pg.PitchGateCoordinator, *, n: int, timeout: float = 5.0) -> list[dict]:
    deadline = time.time() + timeout
    got: list[dict] = []
    while time.time() < deadline:
        got.extend(coord.drain_done())
        if len(got) >= n:
            return got
        time.sleep(0.02)
    return got


def check_dom_session() -> None:
    """End-to-end: DOM readings drive the buy, and no screenshot is produced."""
    readers: list[_FakeReader] = []

    def fake_reader_factory(
        frames: list[dict[str, Any] | None], *, baseline: dict[str, Any] | None
    ):
        def _make(_self, _session, _observer):
            r = _FakeReader(frames, baseline=baseline)
            readers.append(r)
            r.open()
            return r, None, _observer._resolve_surface("m")

        return _make

    orig_open = pg.PitchGateCoordinator._open_dom_reader
    try:
        # Frame 0 stopped (celebration, 射门 mark), frame 1 in_play → one buy.
        # AF is skipped on the celebration frame and starts on first in_play.
        frames = [
            _dom("H 进球", "45:01 1 : 0", ["ball"]),
            _dom("H 进攻", "45:06 1 : 0", ["attack-move"]),
            _dom("H 控球", "45:11 1 : 0", ["possession-rect"]),
        ]
        pg.PitchGateCoordinator._open_dom_reader = fake_reader_factory(  # type: ignore[assignment]
            frames, baseline=_dom("H 进球", "44:56 1 : 0")
        )
        pg.reset_coordinator_for_tests()

        class _AfOk:
            def __init__(self) -> None:
                self.calls = 0

            def sample_once(self, ev, **_kw):  # noqa: ANN001
                self.calls += 1
                return {
                    "ok": True,
                    "score_match": True,
                    "af_score": "1-0",
                    "error": None,
                }

        af = _AfOk()
        af_mod.set_active_observer(af)  # type: ignore[arg-type]
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "data" / "pm-quote").mkdir(parents=True)
            obs = _FakeObserver(root)
            set_active_observer(obs)  # type: ignore[arg-type]
            coord = pg.get_coordinator(root)
            ev = {
                "type": "score_change",
                "match_id": "m1",
                "home": "H",
                "away": "A",
                "home_score": 1,
                "away_score": 0,
                "ts": "2026-08-21T20:00:00+08:00",
                "polymarket": {"event_id": "e1"},
            }
            assert coord.start_gate(ev, event_key="k1")
            done = _wait_done(coord, n=2)
            statuses = [d["status"] for d in done]
            assert "in_play" in statuses and "aligned_buy" in statuses, done
            assert sum(1 for d in done if d["status"] == "in_play") == 1, done
            assert af.calls == 1, af.calls
            assert (obs.rows[0].get("af") or {}).get("skipped") == "before_in_play"
            assert obs.rows[0].get("shot_seen") is True
            assert len(obs.rows) > 2, [r.get("sample_i") for r in obs.rows]
            assert any(
                (r.get("af") or {}).get("skipped") == "after_buy" for r in obs.rows
            )

            buy = next(d for d in done if d["status"] == "in_play")
            assert buy["judge"]["source"] == "dom", buy
            assert buy["judge"]["play_state"] == "in_play", buy
            # Frame 0 was the celebration, so the buy comes off frame 1.
            assert buy["sample_i"] == 1, buy

            rows = [
                json.loads(line)
                for line in observe_path(root).read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            assert rows, "DOM mode still writes an observe trail"
            assert all(r["frame_path"] is None for r in rows), "no screenshots in DOM mode"
            assert all(r["capture_method"] == "dom" for r in rows), rows[0]
            assert all(isinstance(r["dom_state"], dict) for r in rows), rows[0]
            assert rows[0]["judge"]["play_state"] == "stopped", rows[0]
            frames_root = root / "data" / "pm-quote" / "dqd_stream_frames"
            assert not frames_root.exists() or not any(frames_root.rglob("*.jpg"))
            assert readers and readers[-1].closed, "reader must be closed"

        # A page frozen from the start must never buy: the clock baseline taken
        # at open already matches, so even sample 0 is rejected.
        stuck = [_dom("H 进攻", "45:06 1 : 0")]
        pg.PitchGateCoordinator._open_dom_reader = fake_reader_factory(  # type: ignore[assignment]
            stuck, baseline=_dom("H 进攻", "45:06 1 : 0")
        )
        pg.reset_coordinator_for_tests()
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "data" / "pm-quote").mkdir(parents=True)
            set_active_observer(_FakeObserver(root))  # type: ignore[arg-type]
            coord = pg.get_coordinator(root)
            assert coord.start_gate({**ev, "match_id": "m2"}, event_key="k2")
            done = _wait_done(coord, n=1)
            assert [d["status"] for d in done] == ["timeout"], done
            assert done[0].get("buy_emitted") is False, done[0]
            assert "never_in_play" in str(done[0].get("reason") or ""), done[0]

        # Reader that cannot open: never arm AF, never buy.
        def _fail_open(_self, _session, _observer):
            return None, "playwright_browser_missing", {}

        pg.PitchGateCoordinator._open_dom_reader = _fail_open  # type: ignore[assignment]
        pg.reset_coordinator_for_tests()
        af_miss = _AfOk()
        af_mod.set_active_observer(af_miss)  # type: ignore[arg-type]
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "data" / "pm-quote").mkdir(parents=True)
            set_active_observer(_FakeObserver(root))  # type: ignore[arg-type]
            coord = pg.get_coordinator(root)
            assert coord.start_gate({**ev, "match_id": "m3"}, event_key="k3")
            done = _wait_done(coord, n=1)
            assert [d["status"] for d in done] == ["timeout"], done
            assert done[0].get("buy_emitted") is False, done[0]
            assert af_miss.calls == 0, af_miss.calls
            assert "never_in_play" in str(done[0].get("reason") or ""), done[0]
    finally:
        pg.PitchGateCoordinator._open_dom_reader = orig_open  # type: ignore[assignment]
        set_active_observer(None)
        pg.reset_coordinator_for_tests()


def main() -> int:
    os.environ["QUOTE_DQD_STREAM_OBSERVE"] = "1"
    os.environ["QUOTE_GATE_SOURCE"] = "dom"

    check_parse_center()
    check_judge_dom()
    check_gate_source_env()

    old = (pg.GATE_FIRST_DELAY_S, pg.GATE_INTERVAL_S, pg.GATE_TIMEOUT_S)
    pg.GATE_FIRST_DELAY_S = 0.05
    pg.GATE_INTERVAL_S = 0.05
    pg.GATE_TIMEOUT_S = 0.3
    try:
        check_dom_session()
    finally:
        (
            pg.GATE_FIRST_DELAY_S,
            pg.GATE_INTERVAL_S,
            pg.GATE_TIMEOUT_S,
        ) = old
        af_mod.set_active_observer(None)

    print("ok: pitch_gate DOM mode (no screenshot/OCR) + judge_dom rules")
    return 0



if __name__ == "__main__":
    raise SystemExit(main())
