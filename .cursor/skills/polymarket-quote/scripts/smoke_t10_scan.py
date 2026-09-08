#!/usr/bin/env python3
"""Smoke: goal +10min rescan scheduler, caps, rest without QUOTE_REST_ENABLED."""

from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import af_referee as ref  # noqa: E402
import quote_lib as lib  # noqa: E402
import t10_scan as t10  # noqa: E402
from trade_executor import TradeExecutor, _trade_context_t10  # noqa: E402
from trade_settings import TradeSettings  # noqa: E402


def _goal_ev(**kw: object) -> dict:
    ev = {
        "type": "score_change",
        "ts": "2026-08-30T02:00:00+08:00",
        "match_id": "m1",
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


def _settings(*, t10_usdc: float = 15.0) -> TradeSettings:
    return TradeSettings(
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
        t10_usdc=t10_usdc,
    )


def main() -> int:
    t10.reset_scheduler_for_tests()
    saved = {k: os.environ.get(k) for k in (
        "QUOTE_T10",
        "QUOTE_T10_USDC",
        "QUOTE_T10_DELAY_S",
        "QUOTE_T10_MAX_LATE_S",
        "QUOTE_T10_AF_TIMEOUT_S",
        "QUOTE_REST_ENABLED",
        "QUOTE_REST_USDC",
    )}
    try:
        rc = _run()
        if rc != 0:
            return rc
        test_t10_af_live_gate()
        test_t10_reversal_aborts_inflight()
        return 0
    finally:
        t10.reset_scheduler_for_tests()
        ref.reset_ft_referee_for_tests()
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _run() -> int:
    t10.reset_scheduler_for_tests()
    os.environ["QUOTE_T10"] = "1"
    os.environ["QUOTE_T10_USDC"] = "15"
    os.environ["QUOTE_T10_DELAY_S"] = "0"
    os.environ["QUOTE_T10_MAX_LATE_S"] = "900"
    os.environ.pop("QUOTE_REST_ENABLED", None)
    os.environ.pop("QUOTE_REST_USDC", None)

    assert t10.t10_enabled()
    assert abs(t10.t10_usdc() - 15.0) < 1e-9
    src = "score_change|m1|0-0->1-0|2026-08-30T02:00:00+08:00"
    assert t10.t10_event_key(src) == f"t10|{src}"

    s = _settings(t10_usdc=15.0)
    u, sh, tiers = s.caps_for_buy(event_type="score_change", pitch_gate=True, t10=True)
    assert abs(u - 15.0) < 1e-9, (u, sh, tiers)
    assert abs(sh - 1500.0) < 1e-9, sh
    g_u, _, _ = s.caps_for_buy(event_type="score_change", pitch_gate=True)
    assert abs(g_u - 50.0) < 1e-9, g_u

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        root = Path(td)
        (root / "data" / "bridge").mkdir(parents=True)
        (root / "data" / "pm-quote").mkdir(parents=True)
        import quote_lib as lib

        lib.write_json(
            root / "data" / "bridge" / "prev_scores.json",
            {"m1": {"home": 2, "away": 1}},
        )
        sched = t10.get_scheduler(root)
        ev = _goal_ev()
        assert sched.schedule(ev, event_key=src)
        assert sched.schedule(ev, event_key=src) is False
        due = sched.pop_due()
        assert len(due) == 1, due
        work = t10.build_t10_work_event(root, due[0])
        assert work is not None
        # Trigger was 1-0; live prev_scores 2-1 must not become the traded score.
        assert work["home_score"] == 1 and work["away_score"] == 0
        assert work.get("dqd_live_home") == 2 and work.get("dqd_live_away") == 1
        assert work.get("prev") == {"home": 0, "away": 0}
        tc = work["_trade_context"]
        assert tc.get("t10") is True
        assert tc.get("pitch_gate") is True
        assert work["_trade_event_key"] == f"t10|{src}"

        n = sched.cancel_match("m1")
        assert n == 0
        assert sched.schedule(ev, event_key=src)
        assert sched.cancel_match("m1") == 1
        assert sched.pop_due() == []

        # Reversal cancels only the undone goal's T+10 (stem; ts ≤ reverse).
        t10.reset_scheduler_for_tests()
        os.environ["QUOTE_T10_DELAY_S"] = "0"
        sched_r = t10.get_scheduler(root)
        standing_src = "score_change|m_rev|0-0->0-1|2026-09-08T02:38:00+08:00"
        undone_src = "score_change|m_rev|0-1->0-2|2026-09-08T02:56:44+08:00"
        later_src = "score_change|m_rev|0-1->0-2|2026-09-08T03:07:00+08:00"
        ev_stand = _goal_ev(
            match_id="m_rev",
            home_score=0,
            away_score=1,
            prev={"home": 0, "away": 0},
            curr={"home": 0, "away": 1},
            ts="2026-09-08T02:38:00+08:00",
        )
        ev_02 = _goal_ev(
            match_id="m_rev",
            home_score=0,
            away_score=2,
            prev={"home": 0, "away": 1},
            curr={"home": 0, "away": 2},
            ts="2026-09-08T02:56:44+08:00",
        )
        ev_later = _goal_ev(
            match_id="m_rev",
            home_score=0,
            away_score=2,
            prev={"home": 0, "away": 1},
            curr={"home": 0, "away": 2},
            ts="2026-09-08T03:07:00+08:00",
        )
        assert sched_r.schedule(ev_stand, event_key=standing_src)
        assert sched_r.schedule(ev_02, event_key=undone_src)
        assert sched_r.schedule(ev_later, event_key=later_src)
        rev_key = "score_change|m_rev|0-2->0-1|2026-09-08T02:57:09+08:00"
        cancelled = sched_r.cancel_undone_goal(rev_key)
        assert t10.t10_event_key(undone_src) in cancelled, cancelled
        assert t10.t10_event_key(standing_src) not in cancelled
        assert t10.t10_event_key(later_src) not in cancelled
        due_r = {str(j.get("source_event_key")) for j in sched_r.pop_due()}
        assert standing_src in due_r, due_r
        assert later_src in due_r, due_r
        assert undone_src not in due_r

        t10.reset_scheduler_for_tests()
        sched_a = t10.get_scheduler(root)
        assert sched_a.schedule(ev_02, event_key=undone_src)
        rev_ev = _goal_ev(
            match_id="m_rev",
            home_score=0,
            away_score=1,
            prev={"home": 0, "away": 2},
            curr={"home": 0, "away": 1},
            ts="2026-09-08T02:57:09+08:00",
            is_reversal=True,
            is_goal=False,
        )
        import pitch_gate as pg

        pg.reset_coordinator_for_tests()
        dropped = t10.abort_t10_for_reversal(root, rev_key, match_id="m_rev")
        assert t10.t10_event_key(undone_src) in dropped, dropped
        assert sched_a.pop_due() == []
        lib.apply_dqd_reversal_cancel(root, rev_ev)
        pg.reset_coordinator_for_tests()

        # Already pop_due'd: reversal still blocks submit via stem list.
        t10.reset_scheduler_for_tests()
        lib.write_json(
            root / "data" / "pm-quote" / "t10_pending.json",
            {"jobs": [], "blocked_until": {}},
        )
        sched_b = t10.get_scheduler(root)
        assert sched_b.schedule(ev_02, event_key=undone_src)
        popped = sched_b.pop_due()
        assert len(popped) == 1, popped
        assert sched_b.is_source_blocked(undone_src) is False
        t10.abort_t10_for_reversal(root, rev_key, match_id="m_rev")
        assert sched_b.is_source_blocked(undone_src)
        assert sched_b.is_source_blocked(standing_src) is False
        assert sched_b.is_source_blocked(later_src) is False
        t10.reset_scheduler_for_tests()
        sched_c = t10.get_scheduler(root)
        assert sched_c.is_source_blocked(undone_src)
        assert sched_c.is_source_blocked(later_src) is False

        # Re-queue a popped job after reverse → drain must skip, not submit.
        job = dict(popped[0])
        job["due_ts"] = time.time()
        with sched_c._lock:
            sched_c._jobs[undone_src] = job
            sched_c._save_locked()
        last_block = lib.process_bridge_events(
            root,
            events_override=[],
            af_mode="off",
            trade_executor=None,
        )
        assert any(
            isinstance(b, dict) and str(b.get("mode") or "") == "t10_skip_reversal"
            for b in last_block
        ), last_block

        # DQD prev_scores stay in venue order; event labels are Polymarket.
        # Guoan 1-0 Lanzhou on DQD → Lanzhou 0-1 Guoan on PM (Lanzhou tonight).
        lib.write_json(
            root / "data" / "bridge" / "prev_scores.json",
            {"m_flip": {"home": 1, "away": 0}},
        )
        t10.reset_scheduler_for_tests()
        os.environ["QUOTE_T10_DELAY_S"] = "0"
        os.environ["QUOTE_T10_MAX_LATE_S"] = "900"
        flip_src = "score_change|m_flip|0-0->0-1|2026-09-01T20:00:40+08:00"
        flip_ev = _goal_ev(
            match_id="m_flip",
            home="Lanzhou Longyuan Athletic",
            away="Beijing Guoan",
            home_score=0,
            away_score=1,
            prev={"home": 0, "away": 0},
            curr={"home": 0, "away": 1},
            sides_swapped=True,
            dqd_home="Beijing Guoan",
            dqd_away="Lanzhou Longyuan Athletic",
        )
        assert t10.orient_dqd_score_to_pm(1, 0, flip_ev) == (0, 1)
        assert t10.orient_dqd_score_to_pm(1, 0, _goal_ev()) == (1, 0)
        sched_flip = t10.get_scheduler(root)
        assert sched_flip.schedule(flip_ev, event_key=flip_src)
        flip_due = sched_flip.pop_due()
        assert len(flip_due) == 1, flip_due
        flip_work = t10.build_t10_work_event(root, flip_due[0])
        assert flip_work is not None
        assert flip_work["home_score"] == 0 and flip_work["away_score"] == 1, flip_work
        assert flip_work["home"] == "Lanzhou Longyuan Athletic"
        assert flip_work["away"] == "Beijing Guoan"

        # Too late after due → drop, do not fire.
        t10.reset_scheduler_for_tests()
        os.environ["QUOTE_T10_DELAY_S"] = "1"
        os.environ["QUOTE_T10_MAX_LATE_S"] = "5"
        sched2 = t10.get_scheduler(root)
        past = time.time() - 30
        assert sched2.schedule(ev, event_key=src + "|late", now=past - 1)
        # due_ts = past-1+1 = past; now - due ~ 30 > 5
        stale = sched2.pop_due()
        assert stale == [], stale

    t10.reset_scheduler_for_tests()
    os.environ["QUOTE_T10_DELAY_S"] = "0"
    os.environ.pop("QUOTE_T10_USDC", None)
    assert not t10.t10_enabled()
    os.environ["QUOTE_T10_USDC"] = "15"

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        root = Path(td)
        (root / "data" / "pm-quote").mkdir(parents=True)
        ex = TradeExecutor(root, _settings(t10_usdc=15.0))
        meta = {
            "match_id": "m1",
            "home": "H",
            "away": "A",
            "home_score": 2,
            "away_score": 1,
            "event_type": "score_change",
            "trade_context": {
                "pitch_gate": True,
                "t10": True,
                "base_event_key": "t10|k1",
            },
        }
        assert _trade_context_t10(meta)
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
        q_no = {
            "trade": "buy_win",
            "settlement": "WIN",
            "token_id": "tok_t10",
            "match_id": "m1",
            "market_key": "match_total_0.5_over",
            "family": "totals",
            "best_bid": 0.99,
            "best_bid_size": 3000,
            "best_ask": None,
            "asks_top": [],
            "tick_size": "0.01",
            "min_order_size": "5",
            "misprice": False,
        }
        posted = ex.maybe_trade(
            q_no, event_key="t10|k1", match_meta=meta, event_type="score_change"
        )
        assert posted and posted.get("status") == "rest_dry_run", posted
        plan = posted.get("plan") or {}
        levels = plan.get("levels") or []
        assert levels and abs(float(levels[0]["price"]) - 0.99) < 1e-9, plan
        # Rest notional is QUOTE_T10_USDC, not QUOTE_REST_USDC ($5).
        assert float(levels[0]["usdc"]) + 1e-9 >= 14.0, levels[0]

    print("ok: t10 scan schedule / score overlay / caps / rest without REST_ENABLED")
    return 0


def _t10_root(td: str) -> Path:
    root = Path(td)
    (root / "data" / "bridge").mkdir(parents=True)
    (root / "data" / "pm-quote").mkdir(parents=True)
    (root / "data" / "bridge" / "matches.json").write_text(
        '{"matches":[]}', encoding="utf-8"
    )
    (root / "data" / "bridge" / "events.jsonl").write_text("", encoding="utf-8")
    return root


def _pump_t10(
    root: Path,
    referee: ref.AfReferee,
    quoted: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    last: list[dict[str, Any]] = []
    for _ in range(50):
        last = lib.process_bridge_events(
            root,
            events_override=[],
            af_referee=referee,
            af_mode="gate",
            trade_executor=None,
        )
        if quoted or any(
            isinstance(b, dict)
            and str(b.get("mode") or "")
            in {
                "t10_af_unconfirmed",
                "t10_skip_af_finished",
                "t10_skip_ft_pending",
                "t10_skip_score_mismatch",
                "t10_skip_reversal",
            }
            for b in last
        ):
            break
        time.sleep(0.02)
    return last


def test_t10_af_live_gate() -> None:
    """T+10 quotes AF live only when it matches the triggering goal score."""
    t10.reset_scheduler_for_tests()
    ref.reset_ft_referee_for_tests()
    os.environ["QUOTE_T10"] = "1"
    os.environ["QUOTE_T10_USDC"] = "15"
    os.environ["QUOTE_T10_DELAY_S"] = "0"
    os.environ["QUOTE_T10_MAX_LATE_S"] = "900"
    os.environ["QUOTE_T10_AF_TIMEOUT_S"] = "1"

    src = "score_change|m_t10_af|0-0->1-2|2026-09-02T01:00:00+08:00"
    ev = _goal_ev(
        match_id="m_t10_af",
        home_score=1,
        away_score=2,
        prev={"home": 0, "away": 1},
        curr={"home": 1, "away": 2},
    )

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        root = _t10_root(td)
        lib.write_json(
            root / "data" / "bridge" / "prev_scores.json",
            {"m_t10_af": {"home": 3, "away": 2}},
        )
        sched = t10.get_scheduler(root)
        assert sched.schedule(ev, event_key=src)

        def events_fn_live(mid: str, **kwargs: Any) -> dict[str, Any]:
            return {
                "ok": True,
                "goals": {"home": 1, "away": 2},
                "finished": False,
                "status_short": "2H",
                "af_fixture_id": 42,
            }

        referee = ref.AfReferee(
            root,
            poll_s=0.01,
            timeout_s=1.0,
            events_fn=events_fn_live,
            poll_schedule=False,
        )
        quoted: list[dict[str, Any]] = []

        def fake_quote(root_arg: Path, ev_arg: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
            quoted.append(dict(ev_arg))
            return {
                "quoted_at": lib.now_cn_iso(),
                "trigger": "score_change",
                "match_id": ev_arg.get("match_id"),
                "home_score": ev_arg.get("home_score"),
                "away_score": ev_arg.get("away_score"),
                "score_source": ev_arg.get("score_source"),
                "count": 0,
                "opportunity_count": 0,
            }

        with patch.object(lib, "quote_bridge_event", side_effect=fake_quote):
            _pump_t10(root, referee, quoted)
        assert len(quoted) == 1, quoted
        assert quoted[0].get("home_score") == 1 and quoted[0].get("away_score") == 2, quoted[0]
        assert quoted[0].get("score_source") == "api_football", quoted[0]

    t10.reset_scheduler_for_tests()
    ref.reset_ft_referee_for_tests()

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        root = _t10_root(td)
        lib.write_json(
            root / "data" / "bridge" / "prev_scores.json",
            {"m_t10_af": {"home": 1, "away": 2}},
        )
        sched = t10.get_scheduler(root)
        assert sched.schedule(ev, event_key=src + "|miss")

        def events_fn_miss(mid: str, **kwargs: Any) -> dict[str, Any]:
            return {
                "ok": False,
                "error": "af_fixture_not_cached",
                "goals": {"home": None, "away": None},
            }

        referee = ref.AfReferee(
            root,
            poll_s=0.01,
            timeout_s=1.0,
            events_fn=events_fn_miss,
            poll_schedule=False,
        )
        quoted = []

        def fake_quote_miss(
            root_arg: Path, ev_arg: dict[str, Any], **kwargs: Any
        ) -> dict[str, Any]:
            quoted.append(dict(ev_arg))
            return {"quoted_at": lib.now_cn_iso(), "count": 0, "opportunity_count": 0}

        with patch.object(lib, "quote_bridge_event", side_effect=fake_quote_miss):
            last = _pump_t10(root, referee, quoted)
        assert quoted == [], quoted
        assert any(
            isinstance(b, dict) and str(b.get("mode") or "") == "t10_af_unconfirmed"
            for b in last
        ), last

    t10.reset_scheduler_for_tests()
    ref.reset_ft_referee_for_tests()

    check_block = ref.t10_live_blocked_by_af_status
    assert check_block({"status_short": "2H", "finished": False}) is False
    assert check_block({"status_short": "FT", "finished": True}) is True
    assert check_block({"status_short": "ET"}) is True
    assert check_block({"regulation_ready": True, "status_short": "2H"}) is True

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        root = _t10_root(td)
        lib.write_json(
            root / "data" / "bridge" / "prev_scores.json",
            {"m_t10_af": {"home": 1, "away": 2}},
        )
        sched = t10.get_scheduler(root)
        assert sched.schedule(ev, event_key=src + "|ft")

        def events_fn_ft(mid: str, **kwargs: Any) -> dict[str, Any]:
            return {
                "ok": True,
                "goals": {"home": 1, "away": 2},
                "finished": True,
                "regulation_ready": True,
                "status_short": "FT",
                "af_fixture_id": 7,
            }

        referee = ref.AfReferee(
            root,
            poll_s=0.01,
            timeout_s=1.0,
            events_fn=events_fn_ft,
            poll_schedule=False,
        )
        quoted = []

        def fake_quote_ft(
            root_arg: Path, ev_arg: dict[str, Any], **kwargs: Any
        ) -> dict[str, Any]:
            quoted.append(dict(ev_arg))
            return {"quoted_at": lib.now_cn_iso(), "count": 0, "opportunity_count": 0}

        with patch.object(lib, "quote_bridge_event", side_effect=fake_quote_ft):
            last = _pump_t10(root, referee, quoted)
        assert quoted == [], quoted
        assert any(
            isinstance(b, dict) and str(b.get("mode") or "") == "t10_skip_af_finished"
            for b in last
        ), last

    t10.reset_scheduler_for_tests()
    ref.reset_ft_referee_for_tests()

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        root = _t10_root(td)
        lib.write_json(
            root / "data" / "bridge" / "prev_scores.json",
            {"m_t10_pend": {"home": 1, "away": 2}},
        )
        pend_ev = _goal_ev(
            match_id="m_t10_pend",
            home_score=1,
            away_score=2,
            prev={"home": 0, "away": 1},
            curr={"home": 1, "away": 2},
        )
        sched = t10.get_scheduler(root)
        assert sched.schedule(pend_ev, event_key=src + "|pend")

        def events_fn_2h(mid: str, **kwargs: Any) -> dict[str, Any]:
            return {
                "ok": True,
                "goals": {"home": 1, "away": 2},
                "finished": False,
                "regulation_ready": False,
                "status_short": "2H",
                "af_fixture_id": 8,
            }

        referee = ref.AfReferee(
            root,
            poll_s=0.01,
            timeout_s=2.0,
            events_fn=events_fn_2h,
            poll_schedule=False,
        )
        from datetime import datetime, timedelta, timezone

        TZ_CN = timezone(timedelta(hours=8))
        ft_ev = {
            "type": "match_finished",
            "ts": datetime.now(TZ_CN).isoformat(timespec="seconds"),
            "match_id": "m_t10_pend",
            "home": "Home",
            "away": "Away",
            "home_score": 1,
            "away_score": 2,
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
        quoted = []

        def fake_quote_pend(
            root_arg: Path, ev_arg: dict[str, Any], **kwargs: Any
        ) -> dict[str, Any]:
            quoted.append(dict(ev_arg))
            return {"quoted_at": lib.now_cn_iso(), "count": 0, "opportunity_count": 0}

        with patch.object(lib, "quote_bridge_event", side_effect=fake_quote_pend):
            last = lib.process_bridge_events(
                root,
                events_override=[ft_ev],
                af_referee=referee,
                af_mode="gate",
                trade_executor=None,
            )
            if not any(
                isinstance(b, dict)
                and str(b.get("mode") or "") == "t10_skip_ft_pending"
                for b in last
            ):
                last = _pump_t10(root, referee, quoted)
        assert quoted == [], quoted
        assert any(
            isinstance(b, dict) and str(b.get("mode") or "") == "t10_skip_ft_pending"
            for b in (last or [])
        ), last

    t10.reset_scheduler_for_tests()
    ref.reset_ft_referee_for_tests()

    # Later goal (Arsenal 0-1 → 1-1 while T+10 was scheduled on 0-1) must not buy.
    mismatch_src = "score_change|m_t10_mm|0-0->0-1|2026-09-06T23:33:12+08:00"
    mismatch_ev = _goal_ev(
        match_id="m_t10_mm",
        home_score=0,
        away_score=1,
        prev={"home": 0, "away": 0},
        curr={"home": 0, "away": 1},
    )
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        root = _t10_root(td)
        lib.write_json(
            root / "data" / "bridge" / "prev_scores.json",
            {"m_t10_mm": {"home": 1, "away": 1}},
        )
        sched = t10.get_scheduler(root)
        assert sched.schedule(mismatch_ev, event_key=mismatch_src)

        def events_fn_later(mid: str, **kwargs: Any) -> dict[str, Any]:
            return {
                "ok": True,
                "goals": {"home": 1, "away": 1},
                "finished": False,
                "status_short": "1H",
                "af_fixture_id": 9,
            }

        referee = ref.AfReferee(
            root,
            poll_s=0.01,
            timeout_s=1.0,
            events_fn=events_fn_later,
            poll_schedule=False,
        )
        quoted = []

        def fake_quote_mm(
            root_arg: Path, ev_arg: dict[str, Any], **kwargs: Any
        ) -> dict[str, Any]:
            quoted.append(dict(ev_arg))
            return {"quoted_at": lib.now_cn_iso(), "count": 0, "opportunity_count": 0}

        with patch.object(lib, "quote_bridge_event", side_effect=fake_quote_mm):
            last = _pump_t10(root, referee, quoted)
        assert quoted == [], quoted
        assert any(
            isinstance(b, dict) and str(b.get("mode") or "") == "t10_skip_score_mismatch"
            for b in last
        ), last

    print("ok: t10 AF live score (match trigger) / skip unconfirmed / skip AF FT / skip later goal")


def test_t10_reversal_aborts_inflight() -> None:
    """DQD reverse aborts an in-flight T+10 AF live poll for the undone goal."""
    t10.reset_scheduler_for_tests()
    ref.reset_ft_referee_for_tests()
    import pitch_gate as pg

    pg.reset_coordinator_for_tests()
    os.environ["QUOTE_T10"] = "1"
    os.environ["QUOTE_T10_USDC"] = "15"
    os.environ["QUOTE_T10_DELAY_S"] = "0"
    os.environ["QUOTE_T10_MAX_LATE_S"] = "900"
    os.environ["QUOTE_T10_AF_TIMEOUT_S"] = "2"

    src = "score_change|m_t10_rev|0-1->0-2|2026-09-08T02:56:44+08:00"
    ev = _goal_ev(
        match_id="m_t10_rev",
        home_score=0,
        away_score=2,
        prev={"home": 0, "away": 1},
        curr={"home": 0, "away": 2},
        ts="2026-09-08T02:56:44+08:00",
    )
    rev_ev = _goal_ev(
        match_id="m_t10_rev",
        home_score=0,
        away_score=1,
        prev={"home": 0, "away": 2},
        curr={"home": 0, "away": 1},
        ts="2026-09-08T02:57:09+08:00",
        is_reversal=True,
        is_goal=False,
    )

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        root = _t10_root(td)
        pg.get_coordinator(root)
        sched = t10.get_scheduler(root)
        assert sched.schedule(ev, event_key=src)

        def events_fn_behind(mid: str, **kwargs: Any) -> dict[str, Any]:
            return {
                "ok": True,
                "goals": {"home": 0, "away": 1},
                "finished": False,
                "status_short": "2H",
                "af_fixture_id": 42,
            }

        referee = ref.AfReferee(
            root,
            poll_s=0.02,
            timeout_s=2.0,
            events_fn=events_fn_behind,
            poll_schedule=False,
        )
        quoted: list[dict[str, Any]] = []

        def fake_quote(
            root_arg: Path, ev_arg: dict[str, Any], **kwargs: Any
        ) -> dict[str, Any]:
            quoted.append(dict(ev_arg))
            return {
                "quoted_at": lib.now_cn_iso(),
                "trigger": "score_change",
                "count": 0,
                "opportunity_count": 0,
            }

        with patch.object(lib, "quote_bridge_event", side_effect=fake_quote):
            lib.process_bridge_events(
                root,
                events_override=[],
                af_referee=referee,
                af_mode="gate",
                trade_executor=None,
            )
            pending = referee.pending_event_keys()
            assert any(str(k).startswith("t10|") for k in pending), pending
            last = lib.process_bridge_events(
                root,
                events_override=[rev_ev],
                af_referee=referee,
                af_mode="gate",
                trade_executor=None,
            )
            modes = [
                str(b.get("mode"))
                for b in last
                if isinstance(b, dict) and b.get("mode")
            ]
        assert quoted == [], quoted
        assert "t10_skip_reversal" in modes, (modes, last)
        assert not any(
            str(k).startswith("t10|") for k in referee.pending_event_keys()
        )
        assert sched.pop_due() == []

    t10.reset_scheduler_for_tests()
    ref.reset_ft_referee_for_tests()
    pg.reset_coordinator_for_tests()
    print("ok: t10 reversal cancels undone goal queue + in-flight AF")


if __name__ == "__main__":
    raise SystemExit(main())
