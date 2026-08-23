#!/usr/bin/env python3
"""Smoke: pitch-gate (≥5 frames until timeout, buy-once, cancel / multi-match)."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import dqd_stream_observe as obs_mod  # noqa: E402
import pitch_gate as pg  # noqa: E402
from dqd_stream_observe import set_active_observer  # noqa: E402
from score_reversal import TZ_CN, iso_now, lot_in_protect_window  # noqa: E402
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
            "dom_state": {
                "pop_box": "Home 控球",
                "pop_class": "pop-box home",
                "center_box": "12:34 1 : 0",
                "marks": ["possession-rect"],
            },
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
    # This file covers the legacy screenshot+OCR path; DOM mode has its own smoke.
    os.environ["QUOTE_GATE_SOURCE"] = "ocr"

    old_interval = pg.GATE_INTERVAL_S
    old_timeout = pg.GATE_TIMEOUT_S
    old_min = pg.GATE_MIN_FRAMES
    old_first = pg.GATE_FIRST_DELAY_S
    # ~7 frames after first delay: 0.05 + 0..0.30 @ 0.05s.
    pg.GATE_FIRST_DELAY_S = 0.05
    pg.GATE_INTERVAL_S = 0.05
    pg.GATE_TIMEOUT_S = 0.37
    pg.GATE_MIN_FRAMES = 5
    pg.GATE_FRAME_COUNT = 5

    import pitch_gate as pg_mod

    orig_judge = pg_mod._judge_frame_sync

    try:
        # --- in_play fires once; keep capturing past min until timeout → complete ---
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
            assert fake.frames >= pg.GATE_MIN_FRAMES, fake.frames
            assert fake.frames > pg.GATE_MIN_FRAMES, (
                f"expected more than min frames until timeout, got {fake.frames}"
            )
            # Buy only once even though later frames also in_play.
            assert sum(1 for d in done if d["status"] == "in_play") == 1
            assert judges["n"] == fake.frames

            # Research trail: DOM readout and OCR verdict land on one row.
            cmp_rows = [
                json.loads(line)
                for line in pg.dom_vs_ocr_path(root)
                .read_text(encoding="utf-8")
                .splitlines()
                if line.strip()
            ]
            assert len(cmp_rows) == fake.frames, (len(cmp_rows), fake.frames)
            first = cmp_rows[0]
            assert first["dom_pop_box"] == "Home 控球", first
            assert first["dom_pop_class"] == "pop-box home", first
            assert first["dom_center_box"] == "12:34 1 : 0", first
            assert first["dom_marks"] == ["possession-rect"], first
            assert first["expected_score"] == "1-0", first
            assert first["match_id"] == "m1" and first["event_key"] == "k1", first
            assert {r["ocr_play_state"] for r in cmp_rows} <= {"in_play", "stopped"}, cmp_rows

            # --- timeout: never in_play until wall clock expires ---
            judges["n"] = 0
            frames_before = fake.frames

            def always_stopped(**_kwargs: Any) -> dict[str, Any]:
                judges["n"] += 1
                return {"play_state": "stopped"}

            pg_mod._judge_frame_sync = lambda row: always_stopped()  # type: ignore[assignment]
            assert coord.start_gate({**ev, "match_id": "m2"}, event_key="k2")
            done2 = _wait_done(coord, n=1, timeout=3.0)
            assert len(done2) == 1 and done2[0]["status"] == "timeout", done2
            assert done2[0].get("buy_emitted") is False
            frames_m2 = fake.frames - frames_before
            assert frames_m2 >= pg.GATE_MIN_FRAMES, frames_m2
            assert judges["n"] == frames_m2

            # --- cancel mid-session ---
            assert coord.start_gate({**ev, "match_id": "m3"}, event_key="k3")
            time.sleep(0.02)
            assert coord.cancel_match("m3") >= 1
            done3 = _wait_done(coord, n=1, timeout=2.0)
            assert len(done3) == 1 and done3[0]["status"] == "canceled", done3

            # --- queued in_play buy revoked by cancel (same-tick race fix) ---
            pg.reset_coordinator_for_tests()
            set_active_observer(fake)  # type: ignore[arg-type]
            coord = pg.get_coordinator(root)
            with coord._lock:
                coord._done.append(
                    {
                        "status": "in_play",
                        "event_key": "k_queued",
                        "match_id": "m_queued",
                        "ev": {**ev, "match_id": "m_queued"},
                        "reason": "play_state_in_play",
                        "elapsed_s": 10.0,
                        "sample_i": 2,
                    }
                )
                # Sibling match must not be touched.
                coord._done.append(
                    {
                        "status": "in_play",
                        "event_key": "k_other",
                        "match_id": "m_other",
                        "ev": {**ev, "match_id": "m_other"},
                        "reason": "play_state_in_play",
                        "elapsed_s": 10.0,
                        "sample_i": 2,
                    }
                )
            assert coord.cancel_match("m_queued", reason="dqd_reversal") >= 1
            drained = coord.drain_done()
            by_k = {str(d["event_key"]): d for d in drained}
            assert by_k["k_queued"]["status"] == "buy_revoked", by_k
            assert by_k["k_queued"].get("buy_emitted") is False
            assert by_k["k_other"]["status"] == "in_play", by_k
            assert sum(1 for d in drained if d["status"] == "in_play") == 1

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

            # --- score required + board already reversed → no buy ---
            def always_wrong_score(row: dict[str, Any]) -> dict[str, Any] | None:
                # Gate always sets require_score; mismatch must not buy.
                if bool(row.get("require_score")):
                    return {
                        "play_state": "unclear",
                        "score_match": False,
                        "ocr_score": "0-0",
                        "confidence": 0.55,
                    }
                return {"play_state": "in_play", "confidence": 0.9}

            pg_mod._judge_frame_sync = always_wrong_score  # type: ignore[assignment]
            assert coord.start_gate(
                {
                    **ev,
                    "match_id": "m_var",
                    "home_score": 1,
                    "away_score": 0,
                },
                event_key="k_var",
            )
            done_var = _wait_done(coord, n=1, timeout=3.0)
            assert done_var and done_var[0]["status"] == "timeout", done_var
            assert done_var[0].get("buy_emitted") is False
            assert sum(1 for d in done_var if d["status"] == "in_play") == 0

            # --- single in_play is enough (GATE_CONFIRM_FRAMES=1) ---
            assert pg.GATE_CONFIRM_FRAMES == 1, pg.GATE_CONFIRM_FRAMES

            def first_frame_in_play(row: dict[str, Any]) -> dict[str, Any] | None:
                if int(row.get("sample_i") or 0) == 0:
                    return {"play_state": "in_play", "confidence": 0.9}
                return {"play_state": "stopped", "stopped_reason": "celebration", "confidence": 0.9}

            pg_mod._judge_frame_sync = first_frame_in_play  # type: ignore[assignment]
            assert coord.start_gate(
                {**ev, "match_id": "m_streak", "home_score": 1, "away_score": 0},
                event_key="k_streak",
            )
            done_st = _wait_done(coord, n=2, timeout=3.0)
            st_by_status = [d["status"] for d in done_st]
            assert "in_play" in st_by_status, done_st
            buy_st = next(d for d in done_st if d["status"] == "in_play")
            assert buy_st.get("sample_i") == 0, buy_st

            # --- VAR during capture → permanent no-buy even if later in_play ---
            def var_then_in_play(row: dict[str, Any]) -> dict[str, Any] | None:
                if int(row.get("sample_i") or 0) == 0:
                    return {
                        "play_state": "stopped",
                        "stopped_reason": "var",
                        "confidence": 0.95,
                        "evidence": ["VAR"],
                    }
                return {"play_state": "in_play", "confidence": 0.9}

            pg_mod._judge_frame_sync = var_then_in_play  # type: ignore[assignment]
            assert coord.start_gate(
                {**ev, "match_id": "m_varveto", "home_score": 1, "away_score": 0},
                event_key="k_varveto",
            )
            done_vv = _wait_done(coord, n=1, timeout=3.0)
            assert done_vv and done_vv[0]["status"] == "var_veto", done_vv
            assert done_vv[0].get("buy_emitted") is False
            assert done_vv[0].get("var_seen") is True
            assert done_vv[0].get("reason") == "var_during_capture"
            assert sum(1 for d in done_vv if d["status"] == "in_play") == 0

            # --- VAR on a later frame still vetoes when no buy fired yet ---
            def unclear_then_var(row: dict[str, Any]) -> dict[str, Any] | None:
                if int(row.get("sample_i") or 0) == 0:
                    return {"play_state": "unclear", "confidence": 0.3}
                return {
                    "play_state": "stopped",
                    "stopped_reason": "var",
                    "confidence": 0.95,
                }

            pg_mod._judge_frame_sync = unclear_then_var  # type: ignore[assignment]
            assert coord.start_gate(
                {**ev, "match_id": "m_var2", "home_score": 2, "away_score": 0},
                event_key="k_var2",
            )
            done_v2 = _wait_done(coord, n=1, timeout=3.0)
            assert done_v2 and done_v2[0]["status"] == "var_veto", done_v2
            assert done_v2[0].get("buy_emitted") is False
            assert done_v2[0].get("var_seen") is True

            # --- observe_only: in_play frames never queue a buy ---
            def always_in_play(_row: dict[str, Any]) -> dict[str, Any] | None:
                return {"play_state": "in_play", "confidence": 0.9}

            pg_mod._judge_frame_sync = always_in_play  # type: ignore[assignment]
            assert coord.start_gate(
                {
                    **ev,
                    "match_id": "m_obs",
                    "home_score": 0,
                    "away_score": 0,
                    "is_reversal": True,
                },
                event_key="k_obs",
                observe_only=True,
            )
            done_obs = _wait_done(coord, n=1, timeout=3.0)
            assert done_obs and done_obs[0]["status"] == "observe_complete", done_obs
            assert done_obs[0].get("observe_only") is True
            assert done_obs[0].get("buy_emitted") is False
            assert sum(1 for d in done_obs if d["status"] == "in_play") == 0
            tagged = [
                r
                for batch in fake.writes
                for r in batch
                if r.get("event_key") == "k_obs"
            ]
            assert tagged, fake.writes
            assert all(r.get("observe_only") for r in tagged), tagged[0]
            assert tagged[0].get("is_reversal") is True

            # --- cancel a goal then start observe_only on the reversal key ---
            def always_stopped_obs(_row: dict[str, Any]) -> dict[str, Any] | None:
                return {"play_state": "stopped", "confidence": 0.5}

            pg_mod._judge_frame_sync = always_stopped_obs  # type: ignore[assignment]
            assert coord.start_gate(
                {**ev, "match_id": "m_then"}, event_key="k_goal"
            )
            time.sleep(0.02)
            assert coord.cancel_match("m_then", reason="dqd_reversal") >= 1
            done_then = _wait_done(coord, n=1, timeout=2.0)
            assert any(d.get("status") == "canceled" for d in done_then), done_then
            assert coord.start_gate(
                {
                    **ev,
                    "match_id": "m_then",
                    "home_score": 0,
                    "away_score": 0,
                    "is_reversal": True,
                },
                event_key="k_rev",
                observe_only=True,
            )
            done_rev = _wait_done(coord, n=1, timeout=3.0)
            assert done_rev[-1]["status"] == "observe_complete", done_rev
            assert done_rev[-1]["event_key"] == "k_rev"
            assert done_rev[-1].get("observe_only") is True

            # --- quote tick: reversal without in_play does not start observe ---
            import quote_lib as lib

            pg.reset_coordinator_for_tests()
            set_active_observer(fake)  # type: ignore[arg-type]
            coord = pg.get_coordinator(root)
            pg_mod._judge_frame_sync = always_stopped_obs  # type: ignore[assignment]
            goal_key = "score_change|m_qrev|0-0->1-0"
            assert coord.start_gate({**ev, "match_id": "m_qrev"}, event_key=goal_key)
            time.sleep(0.02)
            rev_ev = {
                "type": "score_change",
                "is_reversal": True,
                "match_id": "m_qrev",
                "home": "H",
                "away": "A",
                "home_score": 0,
                "away_score": 0,
                "prev": {"home": 1, "away": 0},
                "curr": {"home": 0, "away": 0},
                "ts": datetime.now(TZ_CN).isoformat(timespec="seconds"),
            }
            bundles = lib.process_bridge_events(
                root,
                events_override=[rev_ev],
                include_props=False,
                include_exact=False,
            )
            modes = [str(b.get("mode") or "") for b in bundles if isinstance(b, dict)]
            assert "dqd_reversal_pitch_gate_canceled" in modes, bundles
            rev_bundle = next(
                b
                for b in bundles
                if isinstance(b, dict)
                and b.get("mode") == "dqd_reversal_pitch_gate_canceled"
            )
            assert (rev_bundle.get("pitch_gate") or {}).get("observe_started") is False
            rev_key = lib.event_key(rev_ev)
            assert rev_key not in coord.pending_event_keys(), coord.pending_event_keys()
            done_q = _wait_done(coord, n=1, timeout=3.0)
            assert any(d.get("status") == "canceled" for d in done_q), done_q
            assert sum(1 for d in done_q if d.get("observe_only")) == 0, done_q

            # --- quote tick: in_play then reverse → observe trail, no buy ---
            pg.reset_coordinator_for_tests()
            set_active_observer(fake)  # type: ignore[arg-type]
            coord = pg.get_coordinator(root)
            pg_mod._judge_frame_sync = always_in_play  # type: ignore[assignment]
            goal_ip = "score_change|m_qrev_ip|0-0->1-0"
            assert coord.start_gate(
                {**ev, "match_id": "m_qrev_ip"}, event_key=goal_ip
            )
            done_ip = _wait_done(coord, n=1, timeout=3.0)
            assert any(d.get("status") == "in_play" for d in done_ip), done_ip
            rev_ip = {
                **rev_ev,
                "match_id": "m_qrev_ip",
            }
            bundles_ip = lib.process_bridge_events(
                root,
                events_override=[rev_ip],
                include_props=False,
                include_exact=False,
            )
            rev_b = next(
                b
                for b in bundles_ip
                if isinstance(b, dict)
                and b.get("mode") == "dqd_reversal_pitch_gate_canceled"
            )
            assert (rev_b.get("pitch_gate") or {}).get("observe_started") is True, rev_b
            assert all(
                "pitch_gate_confirmed" not in str(b.get("mode") or "")
                for b in bundles_ip
                if isinstance(b, dict)
            ), bundles_ip
            done_ip2 = _wait_done(coord, n=1, timeout=3.0)
            more_ip = _wait_done(coord, n=1, timeout=3.0)
            all_ip = done_ip2 + more_ip
            assert any(
                d.get("status") == "observe_complete" and d.get("observe_only")
                for d in all_ip
            ), all_ip
            assert sum(1 for d in all_ip if d.get("status") == "in_play") == 0
    finally:
        pg_mod._judge_frame_sync = orig_judge  # type: ignore[assignment]
        set_active_observer(None)
        pg.reset_coordinator_for_tests()
        pg.GATE_INTERVAL_S = old_interval
        pg.GATE_TIMEOUT_S = old_timeout
        pg.GATE_MIN_FRAMES = old_min
        pg.GATE_FIRST_DELAY_S = old_first
        pg.GATE_FRAME_COUNT = old_min

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
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        root = Path(td)
        (root / "data" / "pm-quote").mkdir(parents=True)
        settings = TradeSettings(
            private_key="",
            funder=None,
            signature_type=2,
            chain_id=137,
            clob_host="https://clob.polymarket.com",
            data_api_url="https://data-api.polymarket.com",
            live_goals=False,
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

        # Pitch-gate: no ask / not misprice → dry rest @0.99 GTD ~1h (opt-in).
        # Size follows QUOTE_MAX_USDC ($1); do not lift to CLOB min_order_size.
        os.environ["QUOTE_REST_ENABLED"] = "1"
        quote = {
            "token_id": "tok_pitch_rest",
            "market_key": "match_total_0.5_over",
            "settlement": "WIN",
            "locked": True,
            "trade": "buy_win",
            "misprice": False,
            "best_bid": 0.999,
            "best_ask": None,
            "tick_size": "0.01",
            "min_order_size": "5",
            "asks_top": [],
            "bids_top": [{"price": 0.999, "size": 10}],
        }
        meta = {
            "match_id": "m_pitch_rest",
            "event_type": "score_change",
            "event_key": "score_change|m_pitch_rest|0-0->1-0",
            "trade_context": {
                "pitch_gate": True,
                "base_event_key": "score_change|m_pitch_rest|0-0->1-0",
            },
        }
        row = ex.maybe_trade(
            quote,
            event_key=meta["event_key"],
            match_meta=meta,
            event_type="score_change",
        )
        assert row is not None, row
        assert row.get("status") == "rest_dry_run", row
        plan = row.get("plan") or {}
        levels = plan.get("levels") or row.get("rest_orders") or []
        orders = row.get("rest_orders") or levels
        if not orders and isinstance(plan, dict):
            orders = plan.get("rest_orders") or plan.get("levels") or []
        assert orders, row
        assert abs(float(orders[0].get("price") or 0) - 0.99) < 1e-9, orders
        assert 0.99 <= float(orders[0].get("usdc") or 0) <= 1.02, orders
        assert float(orders[0].get("shares") or 0) + 1e-9 >= 1.0, orders
        assert float(orders[0].get("shares") or 0) < 2.0, orders
        assert str(orders[0].get("order_type") or "") == "GTD", orders
        exp = int(orders[0].get("expiration") or 0)
        assert exp > time.time() + 3000, (exp, time.time())

        cheap = dict(quote)
        cheap["token_id"] = "tok_pitch_rest_1"
        cheap.pop("min_order_size", None)
        meta_cheap = {
            **meta,
            "match_id": "m_pitch_rest_1",
            "event_key": "score_change|m_pitch_rest_1|0-0->1-0",
            "trade_context": {
                "pitch_gate": True,
                "base_event_key": "score_change|m_pitch_rest_1|0-0->1-0",
            },
        }
        row1 = ex.maybe_trade(
            cheap,
            event_key=meta_cheap["event_key"],
            match_meta=meta_cheap,
            event_type="score_change",
        )
        assert row1 is not None and row1.get("status") == "rest_dry_run", row1
        orders1 = row1.get("rest_orders") or (row1.get("plan") or {}).get("levels") or []
        assert orders1, row1
        assert abs(float(orders1[0].get("price") or 0) - 0.99) < 1e-9, orders1
        assert 0.99 <= float(orders1[0].get("usdc") or 0) <= 1.02, orders1
        assert float(orders1[0].get("shares") or 0) + 1e-9 >= 1.0, orders1
        assert float(orders1[0].get("shares") or 0) < 2.0, orders1

        # --- post-buy protection window: DQD reversal flattens gate lots ---
        os.environ.pop("QUOTE_REST_ENABLED", None)
        os.environ["QUOTE_GATE_PROTECT_S"] = "90"

        def _open_lot(mid: str, tid: str, *, gate: bool, opened_at: str) -> None:
            ex.ledger.register_buy(
                match_id=mid,
                token_id=tid,
                market_key="match_total_0.5_over",
                shares=5.0,
                usdc=4.95,
                home_score=1,
                away_score=0,
                live=False,
                event_key=f"score_change|{mid}|0-0->1-0",
                home="H",
                away="A",
                pitch_gate=gate,
                opened_at=opened_at,
            )

        fresh = iso_now()
        stale = (datetime.now(TZ_CN) - timedelta(seconds=600)).isoformat(
            timespec="seconds"
        )
        _open_lot("m_prot", "tok_prot", gate=True, opened_at=fresh)
        _open_lot("m_prot_old", "tok_prot_old", gate=True, opened_at=stale)
        _open_lot("m_prot_ft", "tok_prot_ft", gate=False, opened_at=fresh)

        lots = {r["match_id"]: r for r in ex.ledger.all_open()}
        assert lot_in_protect_window(lots["m_prot"], window_s=90), lots["m_prot"]
        assert not lot_in_protect_window(lots["m_prot_old"], window_s=90)
        assert not lot_in_protect_window(lots["m_prot_ft"], window_s=90)

        def _reversal(mid: str) -> dict[str, Any]:
            return {
                "type": "score_change",
                "match_id": mid,
                "is_reversal": True,
                "home_score": 0,
                "away_score": 0,
                "prev": {"home": 1, "away": 0},
                "curr": {"home": 0, "away": 0},
                "ts": iso_now(),
            }

        # Fresh gate lot → flatten now (dry lot never touches CLOB).
        rows = ex.maybe_flatten_for_event(_reversal("m_prot"))
        assert rows and rows[0].get("status") == "flatten_dry_run", rows
        assert "gate_protect_reversal" in str(rows[0].get("skip_reason") or ""), rows
        assert not ex.ledger.open_for_match("m_prot")

        # Outside the window, and non-gate lots → still deferred to FT.
        assert ex.maybe_flatten_for_event(_reversal("m_prot_old")) == []
        assert ex.ledger.open_for_match("m_prot_old")
        assert ex.maybe_flatten_for_event(_reversal("m_prot_ft")) == []
        assert ex.ledger.open_for_match("m_prot_ft")

        # Window disabled → no reversal flatten at all.
        os.environ["QUOTE_GATE_PROTECT_S"] = "0"
        _open_lot("m_prot_off", "tok_prot_off", gate=True, opened_at=iso_now())
        assert ex.maybe_flatten_for_event(_reversal("m_prot_off")) == []
        assert ex.ledger.open_for_match("m_prot_off")

        # FT reversal still flattens regardless of window/gate flag.
        os.environ.pop("QUOTE_GATE_PROTECT_S", None)
        ft_rows = ex.maybe_flatten_for_event(
            {
                "type": "match_finished",
                "match_id": "m_prot_ft",
                "home_score": 0,
                "away_score": 0,
                "ts": iso_now(),
            }
        )
        assert ft_rows and ft_rows[0].get("status") == "flatten_dry_run", ft_rows
        assert not ex.ledger.open_for_match("m_prot_ft")

    assert obs_mod.get_active_observer() is None

    print(
        "ok: pitch_gate 1-frame buy + var_veto + buy_revoke + gate_protect_flatten "
        "+ reversal observe_only"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
