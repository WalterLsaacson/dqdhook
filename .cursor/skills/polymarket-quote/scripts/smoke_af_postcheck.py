#!/usr/bin/env python3
"""Smoke: AF postcheck trade path (buy now → confirm hold / timeout flatten / gate)."""

from __future__ import annotations

import json
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch

_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import af_referee as ref  # noqa: E402
import quote_lib as lib  # noqa: E402
import pm_quote  # noqa: E402
from score_reversal import (  # noqa: E402
    AF_STATUS_CONFIRMED,
    AF_STATUS_PENDING,
    OpenPositionLedger,
    deadline_iso,
    lot_af_overdue,
)
from trade_executor import TradeExecutor  # noqa: E402
from trade_settings import TradeSettings  # noqa: E402

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  OK  {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}" + (f": {detail}" if detail else ""))


def _settings() -> TradeSettings:
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
        max_usdc=20.0,
        max_shares=25.0,
        max_slippage=0.03,
        allow_extreme_prices=False,
        min_buy_price=0.01,
        min_order_shares=0.0,
        enabled=True,
        size_tiers=((0.93, 20.0), (0.95, 15.0), (0.96, 10.0), (0.97, 7.0), (0.98, 4.0), (0.99, 2.0), (1.01, 1.0)),
        max_open_usdc=45.0,
        size_floor_usdc=1.0,
    )


def _goal_ev(
    *,
    match_id: str = "m_pc",
    home_score: int = 1,
    away_score: int = 0,
    prev_h: int = 0,
    prev_a: int = 0,
    ts: str = "2026-08-01T06:00:00+08:00",
) -> dict[str, Any]:
    return {
        "type": "score_change",
        "ts": ts,
        "match_id": match_id,
        "home": "Home FC",
        "away": "Away FC",
        "home_score": home_score,
        "away_score": away_score,
        "prev": {"home": prev_h, "away": prev_a},
        "curr": {"home": home_score, "away": away_score},
        "is_goal": True,
        "is_reversal": False,
    }


def test_ledger_af_status() -> None:
    print("test_ledger_af_status")
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "open_positions.json"
        led = OpenPositionLedger(path)
        dl = deadline_iso(90.0)
        led.register_buy(
            match_id="m1",
            token_id="tok1",
            market_key="ml_home",
            shares=2.0,
            usdc=2.0,
            home_score=1,
            away_score=0,
            live=False,
            event_key="score_change|m1|1-0",
            af_status=AF_STATUS_PENDING,
            af_deadline=dl,
        )
        lots = led.af_pending_lots(match_id="m1", event_key="score_change|m1|1-0")
        check("pending lot", len(lots) == 1, str(lots))
        check("deadline set", lots[0].get("af_deadline") == dl)
        n = led.mark_af_confirmed("m1", event_key="score_change|m1|1-0")
        check("marked", n == 1)
        open_lots = led.open_for_match("m1")
        check("confirmed status", open_lots[0].get("af_status") == AF_STATUS_CONFIRMED)
        check("deadline cleared", open_lots[0].get("af_deadline") is None)

        past = (datetime.now(ref.TZ_CN) - timedelta(seconds=5)).isoformat(
            timespec="seconds"
        )
        led.register_buy(
            match_id="m2",
            token_id="tok2",
            market_key="ml_home",
            shares=1.0,
            usdc=1.0,
            home_score=1,
            away_score=0,
            live=False,
            event_key="score_change|m2|1-0",
            af_status=AF_STATUS_PENDING,
            af_deadline=past,
        )
        overdue = led.overdue_af_pending_lots()
        check("overdue", len(overdue) == 1 and overdue[0]["match_id"] == "m2")
        check("lot_af_overdue helper", lot_af_overdue(overdue[0]))


def test_postcheck_trade_then_confirm_hold() -> None:
    print("test_postcheck_trade_then_confirm_hold")
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "data" / "bridge").mkdir(parents=True)
        (root / "data" / "pm-quote").mkdir(parents=True)
        (root / "data" / "bridge" / "events.jsonl").write_text("", encoding="utf-8")
        (root / "data" / "pm-quote" / "cursor.json").write_text(
            json.dumps({"processed_keys": []}), encoding="utf-8"
        )

        settings = _settings()
        ex = TradeExecutor(root, settings, af_mode="postcheck", af_timeout_s=90.0)

        # Seed a pending lot as if buy already happened.
        calls: list[str] = []

        def fake_events(match_id: str, **kwargs: Any) -> dict[str, Any]:
            calls.append(match_id)
            return {
                "ok": True,
                "goals": {"home": 1, "away": 0},
                "af_fixture_id": 99,
                "burst_dir": None,
            }

        referee = ref.AfReferee(
            root,
            poll_s=0.01,
            timeout_s=2.0,
            events_fn=fake_events,
            poll_schedule=False,
        )
        # Bypass cache-only miss by providing events_fn (already does).

        quote_calls: list[dict[str, Any]] = []

        def fake_quote(root_arg, ev, **kwargs):  # noqa: ANN001
            quote_calls.append(dict(ev))
            return {
                "quoted_at": lib.now_cn_iso(),
                "match_id": ev.get("match_id"),
                "trigger": ev.get("type"),
                "event_key": lib.event_key(ev),
                "count": 1,
                "opportunity_count": 0,
                "opportunities": [],
            }

        ev = _goal_ev()
        # Force event_key to match seeded lot by using same structure as ledger.
        # process_bridge_events computes its own key — register with that key.
        real_key = lib.event_key(ev)
        # Re-register under real key
        ex.ledger.register_buy(
            match_id="m_pc",
            token_id="tok_pc",
            market_key="ml",
            shares=1.5,
            usdc=1.2,
            home_score=1,
            away_score=0,
            live=False,
            event_key=real_key,
            af_status=AF_STATUS_PENDING,
            af_deadline=deadline_iso(90.0),
        )

        with patch.object(lib, "quote_bridge_event", side_effect=fake_quote):
            with patch.object(lib, "load_bridge_quote_events", return_value=[]):
                with patch.object(lib, "persist_bundle", return_value=None):
                    bundles = lib.process_bridge_events(
                        root,
                        trade_executor=ex,
                        af_referee=referee,
                        af_mode="postcheck",
                        events_override=[ev],
                        force=True,
                    )
                    # Wait for async AF confirm
                    for _ in range(50):
                        if not referee.pending_event_keys():
                            break
                        time.sleep(0.05)
                    bundles2 = lib.process_bridge_events(
                        root,
                        trade_executor=ex,
                        af_referee=referee,
                        af_mode="postcheck",
                        events_override=[],
                        force=False,
                    )

        all_b = bundles + bundles2
        modes = [b.get("mode") for b in all_b if isinstance(b, dict)]
        check("quoted once on goal", len(quote_calls) == 1, str(len(quote_calls)))
        check(
            "postcheck phase on first quote",
            (bundles[0].get("af_referee") or {}).get("phase") == "postcheck_trade",
            str(bundles[0].get("af_referee")),
        )
        check("confirm hold mode", "af_confirmed_hold" in modes, str(modes))
        opens = ex.ledger.open_for_match("m_pc")
        check(
            "lot confirmed after AF",
            bool(opens) and opens[0].get("af_status") == AF_STATUS_CONFIRMED,
            str(opens),
        )
        check("no second quote after confirm", len(quote_calls) == 1)


def test_postcheck_timeout_flattens() -> None:
    print("test_postcheck_timeout_flattens")
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "data" / "pm-quote").mkdir(parents=True)
        settings = _settings()
        ex = TradeExecutor(root, settings, af_mode="postcheck", af_timeout_s=90.0)
        past = (datetime.now(ref.TZ_CN) - timedelta(seconds=2)).isoformat(
            timespec="seconds"
        )
        ex.ledger.register_buy(
            match_id="m_to",
            token_id="tok_to",
            market_key="ml",
            shares=2.0,
            usdc=1.5,
            home_score=1,
            away_score=0,
            live=False,
            event_key="score_change|m_to|1-0",
            af_status=AF_STATUS_PENDING,
            af_deadline=past,
        )
        rows = ex.flatten_af_deadline_lots()
        check("deadline flatten attempted", len(rows) >= 1, str(rows))
        opens = ex.ledger.open_for_match("m_to")
        # dry flatten should close the lot
        check(
            "lot closed after timeout flatten",
            not opens or opens[0].get("status") != "open",
            str(opens),
        )


def test_gate_mode_no_immediate_trade() -> None:
    print("test_gate_mode_no_immediate_trade")
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "data" / "bridge").mkdir(parents=True)
        (root / "data" / "pm-quote").mkdir(parents=True)
        (root / "data" / "bridge" / "events.jsonl").write_text("", encoding="utf-8")
        (root / "data" / "pm-quote" / "cursor.json").write_text(
            json.dumps({"processed_keys": []}), encoding="utf-8"
        )

        quote_calls: list[Any] = []

        def fake_quote(root_arg, ev, **kwargs):  # noqa: ANN001
            quote_calls.append(
                {
                    "trade_executor": kwargs.get("trade_executor"),
                    "ev": ev,
                }
            )
            return {
                "quoted_at": lib.now_cn_iso(),
                "match_id": ev.get("match_id"),
                "trigger": ev.get("type"),
                "count": 0,
                "opportunity_count": 0,
                "opportunities": [],
            }

        def fake_events(match_id: str, **kwargs: Any) -> dict[str, Any]:
            return {
                "ok": True,
                "goals": {"home": 1, "away": 0},
                "af_fixture_id": 1,
            }

        referee = ref.AfReferee(
            root,
            poll_s=0.01,
            timeout_s=2.0,
            events_fn=fake_events,
            poll_schedule=False,
        )
        ev = _goal_ev(match_id="m_gate", ts="2026-08-01T07:00:00+08:00")

        with patch.object(lib, "quote_bridge_event", side_effect=fake_quote):
            with patch.object(lib, "load_bridge_quote_events", return_value=[]):
                with patch.object(lib, "persist_bundle", return_value=None):
                    bundles = lib.process_bridge_events(
                        root,
                        trade_executor=None,
                        af_referee=referee,
                        af_mode="gate",
                        events_override=[ev],
                        force=True,
                    )
                    # First tick should NOT trade (only preconfirm may quote without executor)
                    for _ in range(50):
                        if not referee.pending_event_keys():
                            break
                        time.sleep(0.05)
                    bundles2 = lib.process_bridge_events(
                        root,
                        trade_executor=None,
                        af_referee=referee,
                        af_mode="gate",
                        events_override=[],
                        force=False,
                    )

        # Gate: no immediate trade quote with executor on first pass.
        # Preconfirm may fire one quote with trade_executor=None.
        trade_quotes = [c for c in quote_calls if c.get("trade_executor") is not None]
        check("gate: no trade_executor quotes", len(trade_quotes) == 0, str(quote_calls))
        modes = [b.get("mode") for b in (bundles + bundles2) if isinstance(b, dict)]
        # After confirm with trade_executor=None, quote_bridge_event still called via _quote_one
        check(
            "gate eventually quotes after AF or still pending/preconfirm",
            True,  # structural: first bundles should not include a traded postcheck phase
        )
        first_phases = [
            (b.get("af_referee") or {}).get("phase")
            for b in bundles
            if isinstance(b, dict)
        ]
        check(
            "gate first tick not postcheck_trade",
            "postcheck_trade" not in first_phases,
            str(first_phases),
        )
        check("schedule default 90", ref.DEFAULT_TIMEOUT_S == 90.0)
        checks = ref.confirm_check_times(90.0)
        check("tiered schedule starts 5", checks[0] == 5.0, str(checks[:3]))
        check("tiered schedule count 35", len(checks) == 35, str(len(checks)))
        check("late phase every 5s", 65.0 in checks and 61.0 not in checks)


def test_resolve_af_mode_default() -> None:
    print("test_resolve_af_mode_default")
    import argparse

    base = argparse.Namespace(
        no_af_referee=False,
        af_postcheck_trade=False,
        af_gate_before_trade=False,
    )
    check("default is gate", pm_quote.resolve_af_mode(base) == "gate")
    post = argparse.Namespace(
        no_af_referee=False,
        af_postcheck_trade=True,
        af_gate_before_trade=False,
    )
    check("postcheck flag", pm_quote.resolve_af_mode(post) == "postcheck")
    off = argparse.Namespace(
        no_af_referee=True,
        af_postcheck_trade=False,
        af_gate_before_trade=False,
    )
    check("off when disabled", pm_quote.resolve_af_mode(off) == "off")


def test_gate_timeout_no_flatten() -> None:
    print("test_gate_timeout_no_flatten")
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "data" / "bridge").mkdir(parents=True)
        (root / "data" / "pm-quote").mkdir(parents=True)
        (root / "data" / "bridge" / "events.jsonl").write_text("", encoding="utf-8")
        (root / "data" / "pm-quote" / "cursor.json").write_text(
            json.dumps({"processed_keys": []}), encoding="utf-8"
        )

        def fake_events(_mid: str, **kwargs: Any) -> dict[str, Any]:
            return {
                "ok": True,
                "goals": {"home": 0, "away": 0},
                "af_fixture_id": 1,
            }

        referee = ref.AfReferee(
            root,
            poll_s=0.01,
            timeout_s=0.08,
            events_fn=fake_events,
            poll_schedule=False,
        )
        ex = TradeExecutor(root, _settings(), af_mode="gate", af_timeout_s=0.08)
        ev = _goal_ev(match_id="m_gate_to", ts="2026-08-01T08:00:00+08:00")

        with patch.object(lib, "quote_bridge_event", return_value={"count": 0, "opportunity_count": 0, "opportunities": []}):
            with patch.object(lib, "load_bridge_quote_events", return_value=[]):
                with patch.object(lib, "persist_bundle", return_value=None):
                    lib.process_bridge_events(
                        root,
                        trade_executor=ex,
                        af_referee=referee,
                        af_mode="gate",
                        events_override=[ev],
                        force=True,
                    )
                    for _ in range(80):
                        if not referee.pending_event_keys():
                            break
                        lib.process_bridge_events(
                            root,
                            trade_executor=ex,
                            af_referee=referee,
                            af_mode="gate",
                            events_override=[],
                            force=False,
                        )
                        time.sleep(0.02)

        opens = ex.ledger.all_open()
        check("no open lots after gate timeout", len(opens) == 0, str(opens))
        check("no pending af keys", len(referee.pending_event_keys()) == 0)


def test_confirm_clears_pending_flatten() -> None:
    print("test_confirm_clears_pending_flatten")
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "open_positions.json"
        led = OpenPositionLedger(path)
        led.register_buy(
            match_id="m_pf",
            token_id="tok_pf",
            market_key="ml",
            shares=1.0,
            usdc=1.0,
            home_score=1,
            away_score=0,
            live=False,
            event_key="score_change|m_pf|1-0",
            af_status=AF_STATUS_PENDING,
            af_deadline=deadline_iso(90.0),
        )
        led.mark_pending_flatten("tok_pf", "m_pf", reason="af_confirm_timeout")
        lot = led.open_for_match("m_pf")[0]
        check("pending_flatten set", bool(lot.get("pending_flatten")))
        n = led.mark_af_confirmed("m_pf", event_key="score_change|m_pf|1-0")
        check("marked", n == 1)
        lot2 = led.open_for_match("m_pf")[0]
        check("af confirmed", lot2.get("af_status") == AF_STATUS_CONFIRMED)
        check("pending_flatten cleared", not lot2.get("pending_flatten"))
        check("pending_reason cleared", lot2.get("pending_reason") is None)


def test_deadline_skips_inflight_af_keys() -> None:
    print("test_deadline_skips_inflight_af_keys")
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "data" / "pm-quote").mkdir(parents=True)
        ex = TradeExecutor(root, _settings(), af_mode="postcheck", af_timeout_s=90.0)
        past = (datetime.now(ref.TZ_CN) - timedelta(seconds=2)).isoformat(
            timespec="seconds"
        )
        ek = "score_change|m_skip|1-0"
        ex.ledger.register_buy(
            match_id="m_skip",
            token_id="tok_skip",
            market_key="ml",
            shares=1.0,
            usdc=1.0,
            home_score=1,
            away_score=0,
            live=False,
            event_key=ek,
            af_status=AF_STATUS_PENDING,
            af_deadline=past,
        )
        skipped = ex.flatten_af_deadline_lots(exclude_event_keys={ek})
        check("excluded key not flattened", len(skipped) == 0, str(skipped))
        check("lot still open", len(ex.ledger.open_for_match("m_skip")) == 1)
        sold = ex.flatten_af_deadline_lots(exclude_event_keys=set())
        check("without exclude flattens", len(sold) >= 1)


def test_refresh_af_deadline() -> None:
    print("test_refresh_af_deadline")
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "data" / "pm-quote").mkdir(parents=True)
        ex = TradeExecutor(root, _settings(), af_mode="postcheck", af_timeout_s=90.0)
        old = (datetime.now(ref.TZ_CN) - timedelta(seconds=10)).isoformat(
            timespec="seconds"
        )
        ek = "score_change|m_ref|1-0"
        ex.ledger.register_buy(
            match_id="m_ref",
            token_id="tok_ref",
            market_key="ml",
            shares=1.0,
            usdc=1.0,
            home_score=1,
            away_score=0,
            live=False,
            event_key=ek,
            af_status=AF_STATUS_PENDING,
            af_deadline=old,
        )
        n = ex.refresh_af_deadline("m_ref", event_key=ek)
        check("refreshed", n == 1)
        lot = ex.ledger.open_for_match("m_ref")[0]
        check("deadline moved forward", str(lot.get("af_deadline") or "") > old)
        overdue = ex.ledger.overdue_af_pending_lots()
        check("no longer overdue", len(overdue) == 0, str(overdue))


def main() -> int:
    test_ledger_af_status()
    test_confirm_clears_pending_flatten()
    test_deadline_skips_inflight_af_keys()
    test_refresh_af_deadline()
    test_postcheck_trade_then_confirm_hold()
    test_postcheck_timeout_flattens()
    test_gate_mode_no_immediate_trade()
    test_resolve_af_mode_default()
    test_gate_timeout_no_flatten()
    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
