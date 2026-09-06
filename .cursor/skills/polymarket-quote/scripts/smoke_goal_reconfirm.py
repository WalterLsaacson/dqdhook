#!/usr/bin/env python3
"""Smoke: goal reconfirm scheduler, DOM 6×3s all in_play, AF, live $1."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import experiment_flags as ef  # noqa: E402
import goal_reconfirm as rec  # noqa: E402
import quote_lib as lib  # noqa: E402
from trade_executor import TradeExecutor, _trade_context_reconfirm  # noqa: E402
from trade_settings import TradeSettings  # noqa: E402


def _goal_ev(**kw: object) -> dict[str, Any]:
    ev: dict[str, Any] = {
        "type": "score_change",
        "ts": "2026-09-06T15:00:00+08:00",
        "match_id": "m_rec",
        "home": "Home",
        "away": "Away",
        "home_score": 1,
        "away_score": 0,
        "is_goal": True,
        "prev": {"home": 0, "away": 0},
        "curr": {"home": 1, "away": 0},
        "polymarket": {"event_id": "e1", "slug": "home-vs-away"},
    }
    ev.update(kw)
    return ev


def _settings(**kw: object) -> TradeSettings:
    base: dict[str, Any] = dict(
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
        max_usdc=50.0,
        max_shares=150.0,
        max_slippage=0.03,
        allow_extreme_prices=False,
        min_buy_price=0.6,
        min_order_shares=0.0,
        enabled=True,
        size_tiers=((0.98, 50.0),),
        max_open_usdc=100000.0,
        size_floor_usdc=1.0,
        goal_max_usdc=50.0,
        ft_max_usdc=300.0,
        reconfirm_usdc=1.0,
    )
    base.update(kw)
    return TradeSettings(**base)  # type: ignore[arg-type]


def _posted_bundle(*, void_locked: bool = False, posted: bool = True) -> dict[str, Any]:
    quotes = [
        {
            "settlement": "WIN",
            "win_if_goal_void": False,
            "trade": "buy_win",
            "trade_attempt": {
                "trade": "buy_win",
                "status": "dry_run" if posted else "skipped",
                "success": posted,
            },
        },
        {
            "settlement": "WIN",
            "win_if_goal_void": True,
            "trade": "buy_win",
            "trade_attempt": {"trade": "buy_win", "status": "skipped"},
        },
    ]
    if void_locked:
        quotes[0]["win_if_goal_void"] = True
    return {
        "mode": "pitch_gate_confirmed",
        "pitch_gate": {"status": "in_play"},
        "event_key": "score_change|m_rec|0-0->1-0|2026-09-06T15:00:00+08:00",
        "match_id": "m_rec",
        "home": "Home",
        "away": "Away",
        "home_score": 1,
        "away_score": 0,
        "quotes": quotes,
        "polymarket": {"event_id": "e1", "slug": "home-vs-away"},
    }


def main() -> int:
    rec.reset_scheduler_for_tests()
    saved = {
        k: os.environ.get(k)
        for k in ("QUOTE_RECONFIRM_DELAY_S", "QUOTE_RECONFIRM_USDC")
    }
    try:
        _run()
        print("ok: goal reconfirm schedule / DOM 6-frame / AF / live $1 / cancel / worker-ev")
        return 0
    finally:
        rec.reset_scheduler_for_tests()
        ef.restore_hard_stops()
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _run() -> None:
    os.environ["QUOTE_RECONFIRM_DELAY_S"] = "0"
    os.environ["QUOTE_RECONFIRM_USDC"] = "1"
    assert rec.reconfirm_enabled()
    assert abs(rec.reconfirm_usdc() - 1.0) < 1e-9
    src = "score_change|m_rec|0-0->1-0|2026-09-06T15:00:00+08:00"
    assert rec.reconfirm_event_key(src) == f"reconfirm|{src}"

    s = _settings()
    u, sh, _tiers = s.caps_for_buy(
        event_type="score_change", pitch_gate=True, reconfirm=True
    )
    assert abs(u - 1.0) < 1e-9, u
    assert abs(sh - 100.0) < 1e-9, sh
    g_u, _, _ = s.caps_for_buy(event_type="score_change", pitch_gate=True)
    assert abs(g_u - 50.0) < 1e-9, g_u

    posted = _posted_bundle()
    assert rec.posted_buy_win(posted["quotes"])
    assert rec.remaining_edge_quotes(posted["quotes"])
    assert rec.bundle_should_schedule(posted)
    locked_only = _posted_bundle(void_locked=True)
    assert not rec.bundle_should_schedule(locked_only)
    not_posted = _posted_bundle(posted=False)
    assert not rec.bundle_should_schedule(not_posted)

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        root = Path(td)
        (root / "data" / "pm-quote").mkdir(parents=True)
        (root / "data" / "bridge").mkdir(parents=True)
        (root / "data" / "bridge" / "matches.json").write_text(
            '{"matches":[]}', encoding="utf-8"
        )
        (root / "data" / "bridge" / "events.jsonl").write_text("", encoding="utf-8")
        sched = rec.get_scheduler(root)
        ev = _goal_ev()
        assert sched.maybe_schedule_from_bundle(posted, ev=ev)
        assert sched.maybe_schedule_from_bundle(posted, ev=ev) is False
        due = sched.pop_due()
        assert len(due) == 1, due
        work = rec.build_reconfirm_work_event(due[0])
        assert work is not None
        tc = work["_trade_context"]
        assert tc.get("reconfirm") is True
        assert tc.get("pitch_gate") is True
        assert work["_trade_event_key"] == f"reconfirm|{src}"

        rec.reset_scheduler_for_tests(root)
        sched = rec.get_scheduler(root)
        assert sched.schedule(ev, event_key=src)
        assert sched.cancel_match("m_rec", reason="test") == 1
        assert sched.pop_due() == []
        assert sched.schedule(ev, event_key=src) is False

        swapped = _goal_ev(
            sides_swapped=True,
            dqd_home="Away",
            dqd_away="Home",
        )
        slim = rec.slim_event(swapped)
        assert slim.get("sides_swapped") is True
        assert slim.get("dqd_home") == "Away"
        posted_swapped = _posted_bundle()
        posted_swapped["reconfirm_ev"] = slim
        rec.reset_scheduler_for_tests(root)
        sched = rec.get_scheduler(root)
        assert sched.maybe_schedule_from_bundle(posted_swapped) is True
        due_swapped = sched.pop_due()
        assert len(due_swapped) == 1, due_swapped
        assert due_swapped[0]["ev"].get("sides_swapped") is True
        assert due_swapped[0]["ev"].get("dqd_home") == "Away"
        assert due_swapped[0]["ev"].get("dqd_away") == "Home"

        # 6 frames all in_play + AF match → pass
        rec.reset_scheduler_for_tests()
        states = ["in_play"] * 6

        def _all_in_play(**_kw: Any) -> str:
            i = int(_kw.get("sample_i") or 0)
            return states[i]

        rec.set_hooks_for_tests(
            sample_dom_fn=_all_in_play,
            poll_af_fn=lambda **_k: {"ok": True, "score_match": True},
            sleep_fn=lambda _s: None,
            sync=True,
        )
        out = rec.run_confirm_attempt(root, ev)
        assert out.get("ok") is True, out
        assert out.get("play_states") == states

        # Frame 3 celebration → fail this attempt
        def _fail_frame3(**kw: Any) -> str:
            i = int(kw.get("sample_i") or 0)
            return "celebration" if i == 2 else "in_play"

        rec.set_hooks_for_tests(
            sample_dom_fn=_fail_frame3,
            poll_af_fn=lambda **_k: {"ok": True, "score_match": True},
            sleep_fn=lambda _s: None,
            sync=True,
        )
        out = rec.run_confirm_attempt(root, ev)
        assert out.get("ok") is False, out
        assert out.get("failed_frame") == 2, out

        # Scheduler: fail ×3 then drop
        rec.reset_scheduler_for_tests(root)
        rec.set_hooks_for_tests(
            sample_dom_fn=_fail_frame3,
            poll_af_fn=lambda **_k: {"ok": True, "score_match": True},
            sleep_fn=lambda _s: None,
            sync=True,
        )
        os.environ["QUOTE_RECONFIRM_DELAY_S"] = "0"
        sched = rec.get_scheduler(root)
        assert sched.schedule(ev, event_key=src + "|drop", delay_s=0)
        statuses: list[str] = []
        for _ in range(5):
            sched.kick_due()
            done = sched.drain_done()
            statuses.extend(str(x.get("status") or "") for x in done)
            if "drop" in statuses:
                break
        assert "retry" in statuses, statuses
        assert "drop" in statuses, statuses
        assert sched.pop_due() == []

        # Pass on first attempt → quote context live $1
        rec.reset_scheduler_for_tests(root)
        rec.set_hooks_for_tests(
            sample_dom_fn=_all_in_play,
            poll_af_fn=lambda **_k: {"ok": True, "score_match": True},
            sleep_fn=lambda _s: None,
            sync=True,
        )
        sched = rec.get_scheduler(root)
        assert sched.schedule(ev, event_key=src + "|pass", delay_s=0)
        sched.kick_due()
        done = sched.drain_done()
        assert done and done[0].get("status") == "pass", done

        ex = TradeExecutor(root, _settings(reconfirm_usdc=1.0))
        meta = {
            "match_id": "m_rec",
            "home": "H",
            "away": "A",
            "home_score": 1,
            "away_score": 0,
            "event_type": "score_change",
            "trade_context": {
                "pitch_gate": True,
                "reconfirm": True,
                "base_event_key": "reconfirm|k1",
            },
        }
        assert _trade_context_reconfirm(meta)
        assert ex._live_for_signal("score_change", meta) is True
        assert ex._live_for_signal("score_change") is False
        assert not ex._locked_sweep_eligible(
            {
                "settlement": "WIN",
                "locked": True,
                "win_if_goal_void": True,
            },
            trade="buy_win",
            match_meta=meta,
            event_type="score_change",
        )
        q = {
            "trade": "buy_win",
            "settlement": "WIN",
            "misprice": True,
            "token_id": "tok_rec",
            "match_id": "m_rec",
            "market_key": "match_total_0.5_over",
            "family": "totals",
            "best_ask": 0.92,
        }
        assert ex._buy_target_usdc(q, meta) == (0.0, "")
        assert (
            ex._rest_remaining_buy(
                q, event_key="reconfirm|k1", match_meta=meta, event_type="score_change"
            )
            is None
        )

        # FT hard-stop still cancels pending reconfirm
        rec.reset_scheduler_for_tests(root)
        rec.set_hooks_for_tests(sync=True, sleep_fn=lambda _s: None)
        sched = rec.get_scheduler(root)
        assert sched.schedule(ev, event_key=src + "|ft", delay_s=60)
        from datetime import datetime, timedelta, timezone

        TZ_CN = timezone(timedelta(hours=8))
        ft_ev = {
            "type": "match_finished",
            "ts": datetime.now(TZ_CN).isoformat(timespec="seconds"),
            "match_id": "m_rec",
            "home": "Home",
            "away": "Away",
            "home_score": 1,
            "away_score": 0,
            "status": "played",
            "official_clock": "FT",
            "polymarket": {
                "event_id": "e1",
                "slug": "home-vs-away",
                "url": "",
                "condition_ids": [],
                "market_refs": [],
            },
        }
        bundles = lib.process_bridge_events(
            root,
            events_override=[ft_ev],
            af_mode="off",
            trade_executor=None,
        )
        assert any(
            isinstance(b, dict) and str(b.get("mode") or "") == "ft_hard_stop"
            for b in bundles
        ), bundles
        assert rec.get_scheduler(root).pop_due() == []

        # New goal cancels pending job
        rec.reset_scheduler_for_tests(root)
        sched = rec.get_scheduler(root)
        assert sched.schedule(ev, event_key=src + "|old", delay_s=60)
        goal2 = _goal_ev(
            ts="2026-09-06T15:05:00+08:00",
            home_score=2,
            away_score=0,
            prev={"home": 1, "away": 0},
            curr={"home": 2, "away": 0},
        )
        lib.process_bridge_events(
            root,
            events_override=[goal2],
            af_mode="off",
            trade_executor=None,
        )
        assert rec.get_scheduler(root).pop_due() == []

        # Stale worker bundle after a newer goal must not schedule.
        rec.reset_scheduler_for_tests(root)
        sched = rec.get_scheduler(root)
        old_ev = _goal_ev(ts="2026-09-06T15:00:00+08:00")
        new_ev = _goal_ev(
            ts="2026-09-06T15:05:00+08:00",
            home_score=2,
            away_score=0,
            prev={"home": 1, "away": 0},
            curr={"home": 2, "away": 0},
        )
        sched.cancel_match("m_rec", reason="new_goal", event_ts=new_ev["ts"])
        stale_bundle = _posted_bundle()
        stale_bundle["reconfirm_ev"] = rec.slim_event(old_ev)
        assert sched.maybe_schedule_from_bundle(stale_bundle) is False
        fresh_bundle = _posted_bundle()
        fresh_bundle["event_key"] = (
            "score_change|m_rec|1-0->2-0|2026-09-06T15:05:00+08:00"
        )
        fresh_bundle["home_score"] = 2
        fresh_bundle["reconfirm_ev"] = rec.slim_event(new_ev)
        assert sched.maybe_schedule_from_bundle(fresh_bundle) is True
        assert sched.pop_due()

        # Cancel drops an already-passed _done item; drain must not quote it.
        rec.reset_scheduler_for_tests(root)
        rec.set_hooks_for_tests(
            sample_dom_fn=_all_in_play,
            poll_af_fn=lambda **_k: {"ok": True, "score_match": True},
            sleep_fn=lambda _s: None,
            sync=True,
        )
        sched = rec.get_scheduler(root)
        pass_key = src + "|done-drop"
        assert sched.schedule(ev, event_key=pass_key, delay_s=0)
        sched.kick_due()
        assert any(x.get("status") == "pass" for x in list(sched._done))
        sched.cancel_match("m_rec", reason="new_goal", event_ts="2026-09-06T15:06:00+08:00")
        assert sched.drain_done() == []
        job = {
            "source_event_key": pass_key,
            "reconfirm_event_key": rec.reconfirm_event_key(pass_key),
            "match_id": "m_rec",
            "event_ts": ev["ts"],
            "ev": rec.slim_event(ev),
        }
        assert sched.accept_job(job) is False

        # Retry after cancel must not resurrect the same source key.
        rec.reset_scheduler_for_tests(root)
        rec.set_hooks_for_tests(
            sample_dom_fn=_fail_frame3,
            poll_af_fn=lambda **_k: {"ok": True, "score_match": True},
            sleep_fn=lambda _s: None,
            sync=True,
        )
        sched = rec.get_scheduler(root)
        retry_key = src + "|no-resurrect"
        assert sched.schedule(ev, event_key=retry_key, delay_s=0)
        sched.cancel_match("m_rec", reason="dqd_reversal", event_ts=ev["ts"])
        assert sched.schedule(ev, event_key=retry_key, delay_s=0) is False
        assert sched.kick_due() == 0
        assert sched.pop_due() == []

        # FT block survives scheduler reload from disk.
        rec.reset_scheduler_for_tests(root)
        sched = rec.get_scheduler(root)
        persist_key = src + "|persist-ft"
        assert sched.schedule(ev, event_key=persist_key, delay_s=60)
        sched.cancel_match("m_rec", reason="match_finished", event_ts=ev["ts"])
        rec.reset_scheduler_for_tests()  # keep pending.json
        sched = rec.get_scheduler(root)
        assert sched.schedule(ev, event_key=src + "|after-restart", delay_s=0) is False

        # CLOB worker stamps reconfirm_ev so drain can schedule without the original ev.
        rec.reset_scheduler_for_tests(root)
        import quote_worker as qw

        qw.reset_quote_worker_for_tests()
        try:
            worker = qw.start_quote_worker(root, trade_executor=None, market_cache=None)
            worker.idle_housekeep_s = 99.0
            worker_key = src + "|worker-ev"

            def _quote(_root, _ev, **_kw):  # noqa: ANN001
                b = _posted_bundle()
                b["event_key"] = worker_key
                b["match_id"] = "m_rec"
                return b

            extra = {
                "mode": "pitch_gate_confirmed",
                "pitch_gate": {"status": "in_play"},
                "reconfirm_ev": rec.slim_event(swapped),
            }
            with patch.object(lib, "quote_bridge_event", side_effect=_quote):
                worker.submit_quote(swapped, event_key=worker_key, extra=extra)
                worker.wait_idle(timeout=2.0)
            results = worker.drain_results()
            assert results and results[0].bundles, results
            stamped = results[0].bundles[0]
            assert stamped.get("reconfirm_ev", {}).get("sides_swapped") is True
            sched = rec.get_scheduler(root)
            assert sched.maybe_schedule_from_bundle(stamped) is True
            due_w = sched.pop_due()
            assert due_w and due_w[0]["ev"].get("sides_swapped") is True
            assert due_w[0]["ev"].get("dqd_home") == "Away"
        finally:
            qw.reset_quote_worker_for_tests()


if __name__ == "__main__":
    raise SystemExit(main())

