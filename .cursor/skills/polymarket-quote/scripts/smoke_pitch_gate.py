#!/usr/bin/env python3
"""Smoke: pitch-gate sessions (5 frames, buy-once, timeout / cancel / multi-match)."""

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

import dqd_stream_observe as obs_mod  # noqa: E402
import pitch_gate as pg  # noqa: E402
from dqd_stream_observe import set_active_observer  # noqa: E402
from trade_executor import TradeExecutor, _trade_context_pitch_gate  # noqa: E402
from trade_settings import TradeSettings  # noqa: E402


class _FakeObserver:
    """Minimal stand-in for DqdStreamObserver capture/write."""

    def __init__(self) -> None:
        self.frames = 0
        self.writes: list[list[dict[str, Any]]] = []

    def _capture_row(self, job: Any, *, sample_i: int, elapsed_s: float) -> dict[str, Any]:
        self.frames += 1
        path = f"/tmp/fake_{job.match_id}_{sample_i}.jpg"
        return {
            "ok": True,
            "frame_path": path,
            "match_id": job.match_id,
            "event_key": job.event_key,
            "sample_i": sample_i,
            "elapsed_s": elapsed_s,
            "surface": "fake",
            "stream_url": None,
            "page_url": "https://example.com",
            "frame_kind": "page",
        }

    def _write_rows(self, rows: list[dict[str, Any]]) -> None:
        self.writes.append(list(rows))


def _wait_done(coord: pg.PitchGateCoordinator, *, n: int, timeout: float = 3.0) -> list[dict]:
    deadline = time.time() + timeout
    got: list[dict] = []
    while time.time() < deadline:
        got.extend(coord.drain_done())
        if len(got) >= n:
            return got
        time.sleep(0.02)
    return got


def main() -> int:
    os.environ["QUOTE_DQD_STREAM_OBSERVE"] = "1"
    os.environ["QUOTE_PITCH_STATE"] = "1"

    old_interval = pg.GATE_INTERVAL_S
    old_timeout = pg.GATE_TIMEOUT_S
    old_frames = pg.GATE_FRAME_COUNT
    pg.GATE_INTERVAL_S = 0.05
    pg.GATE_TIMEOUT_S = 5.0
    pg.GATE_FRAME_COUNT = 5

    import pitch_gate as pg_mod

    orig_judge = pg_mod._judge_frame_sync

    try:
        # --- in_play fires once; still captures all 5 frames → complete ---
        pg.reset_coordinator_for_tests()
        fake = _FakeObserver()
        set_active_observer(fake)  # type: ignore[arg-type]
        judges = {"n": 0}

        def judge_in_play(**_kwargs: Any) -> dict[str, Any]:
            judges["n"] += 1
            if judges["n"] >= 2:
                return {"play_state": "in_play", "confidence": 0.9}
            return {"play_state": "stopped", "confidence": 0.5}

        pg_mod._judge_frame_sync = lambda row: judge_in_play()  # type: ignore[assignment]

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            coord = pg.get_coordinator(root)
            ev = {
                "type": "score_change",
                "match_id": "m1",
                "home": "H",
                "away": "A",
                "home_score": 1,
                "away_score": 0,
                "ts": "2026-08-20T12:00:00+08:00",
                "polymarket": {"event_id": "e1"},
            }
            assert coord.start_gate(ev, event_key="k1")
            done = _wait_done(coord, n=2, timeout=3.0)
            statuses = [d["status"] for d in done]
            assert "in_play" in statuses, done
            assert "complete" in statuses, done
            assert fake.frames == 5, fake.frames
            # Buy only once even though later frames also in_play.
            assert sum(1 for d in done if d["status"] == "in_play") == 1
            assert judges["n"] == 5

            # --- timeout: never in_play across all frames ---
            judges["n"] = 0

            def always_stopped(**_kwargs: Any) -> dict[str, Any]:
                judges["n"] += 1
                return {"play_state": "stopped"}

            pg_mod._judge_frame_sync = lambda row: always_stopped()  # type: ignore[assignment]
            assert coord.start_gate({**ev, "match_id": "m2"}, event_key="k2")
            done2 = _wait_done(coord, n=1, timeout=3.0)
            assert len(done2) == 1 and done2[0]["status"] == "timeout", done2
            assert done2[0].get("buy_emitted") is False
            assert judges["n"] == 5

            # --- cancel mid-session ---
            assert coord.start_gate({**ev, "match_id": "m3"}, event_key="k3")
            time.sleep(0.02)
            assert coord.cancel_match("m3") >= 1
            done3 = _wait_done(coord, n=1, timeout=2.0)
            assert len(done3) == 1 and done3[0]["status"] == "canceled", done3

            # --- multi-match concurrent: cancel one does not stop the other ---
            def stopped_or_play(row: dict[str, Any]) -> dict[str, Any] | None:
                mid = str(row.get("match_id") or "")
                if mid == "mb" and int(row.get("sample_i") or 0) >= 1:
                    return {"play_state": "in_play", "confidence": 0.9}
                return {"play_state": "stopped", "confidence": 0.5}

            pg_mod._judge_frame_sync = stopped_or_play  # type: ignore[assignment]
            assert coord.start_gate({**ev, "match_id": "ma"}, event_key="ka")
            assert coord.start_gate({**ev, "match_id": "mb"}, event_key="kb")
            time.sleep(0.03)
            assert "ka" in coord.pending_event_keys()
            coord.cancel_match("ma")
            done_m = _wait_done(coord, n=3, timeout=4.0)
            by_key: dict[str, list[str]] = {}
            for d in done_m:
                by_key.setdefault(str(d["event_key"]), []).append(str(d["status"]))
            assert by_key.get("ka") == ["canceled"], by_key
            assert "in_play" in by_key.get("kb", []), by_key
            assert "complete" in by_key.get("kb", []), by_key
    finally:
        pg_mod._judge_frame_sync = orig_judge  # type: ignore[assignment]
        set_active_observer(None)
        pg.reset_coordinator_for_tests()
        pg.GATE_INTERVAL_S = old_interval
        pg.GATE_TIMEOUT_S = old_timeout
        pg.GATE_FRAME_COUNT = old_frames

    # --- unavailable when env off ---
    os.environ["QUOTE_PITCH_STATE"] = "0"
    pg.reset_coordinator_for_tests()
    set_active_observer(_FakeObserver())  # type: ignore[arg-type]
    try:
        with tempfile.TemporaryDirectory() as td:
            coord = pg.get_coordinator(Path(td))
            ok = coord.start_gate(
                {
                    "match_id": "mx",
                    "polymarket": {"event_id": "e"},
                },
                event_key="kx",
            )
            assert ok is False
            done_u = _wait_done(coord, n=1, timeout=1.0)
            assert done_u and done_u[0]["status"] == "unavailable"
    finally:
        set_active_observer(None)
        pg.reset_coordinator_for_tests()
        os.environ["QUOTE_PITCH_STATE"] = "1"

    # --- trade_context pitch_gate skips min price ---
    assert _trade_context_pitch_gate({"trade_context": {"pitch_gate": True}})
    assert not _trade_context_pitch_gate({})
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "data" / "pm-quote").mkdir(parents=True)
        settings = TradeSettings(
            private_key="",
            funder=None,
            signature_type=2,
            chain_id=137,
            clob_host="https://clob.polymarket.com",
            data_api_url="https://data-api.polymarket.com",
            live_goals=True,
            live_ft=False,
            take_depth="top",
            max_levels=5,
            max_usdc=1.0,
            max_shares=25.0,
            max_slippage=0.03,
            allow_extreme_prices=False,
            min_buy_price=0.6,
            min_order_shares=0.0,
            enabled=True,
            size_tiers=((0.98, 1.0),),
            max_open_usdc=1000.0,
            size_floor_usdc=1.0,
        )
        ex = TradeExecutor(root, settings)
        assert ex._min_buy_price_blocked(0.05) is not None
        assert (
            ex._min_buy_price_blocked(
                0.05, match_meta={"trade_context": {"pitch_gate": True}}
            )
            is None
        )

    assert obs_mod.get_active_observer() is None

    print("ok: pitch_gate 5-frame continue + buy-once + size relax")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
