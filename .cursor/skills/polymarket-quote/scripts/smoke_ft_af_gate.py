#!/usr/bin/env python3
"""Smoke: match_finished AF regulation gate + stale / once-per-match skips."""

from __future__ import annotations

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

import af_referee as ref  # noqa: E402
import quote_lib as lib  # noqa: E402

TZ_CN = timezone(timedelta(hours=8))
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


def _ft_ev(
    *,
    match_id: str = "m_ft",
    home_score: int = 2,
    away_score: int = 3,
    ts: str | None = None,
) -> dict[str, Any]:
    return {
        "type": "match_finished",
        "ts": ts or datetime.now(TZ_CN).isoformat(timespec="seconds"),
        "match_id": match_id,
        "home": "Home FC",
        "away": "Away FC",
        "home_score": home_score,
        "away_score": away_score,
        "status": "played",
        "official_clock": "FT",
        "polymarket": {"event_id": "1", "slug": "x", "url": "", "condition_ids": [], "market_refs": []},
    }


def test_ft_stale_and_helpers() -> None:
    print("test_ft_stale_and_helpers")
    now = datetime(2026, 8, 2, 1, 5, tzinfo=TZ_CN)
    old = _ft_ev(ts="2026-08-01T20:30:32+08:00")
    stale, age = ref.ft_event_is_stale(old, max_age_s=15 * 60, now=now)
    check("hours-old FT is stale", stale is True and age is not None and age > 900)
    fresh = _ft_ev(ts="2026-08-02T01:00:00+08:00")
    stale2, _ = ref.ft_event_is_stale(fresh, max_age_s=15 * 60, now=now)
    check("recent FT not stale", stale2 is False)
    check("exact FT match", ref.af_ft_score_matches((2, 2), (2, 2)))
    check("FT mismatch", not ref.af_ft_score_matches((2, 2), (2, 3)))


def test_ft_mismatch_and_confirm() -> None:
    print("test_ft_mismatch_and_confirm")
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "data" / "bridge").mkdir(parents=True)
        (root / "data" / "pm-quote").mkdir(parents=True)
        (root / "data" / "bridge" / "matches.json").write_text('{"matches":[]}', encoding="utf-8")
        (root / "data" / "bridge" / "events.jsonl").write_text("", encoding="utf-8")

        # AF regulation 2-2 while DQD FT says 2-3 → mismatch, no quote.
        def events_fn_mismatch(mid: str, **kwargs: Any) -> dict[str, Any]:
            return {
                "ok": True,
                "goals": {"home": 2, "away": 2},
                "finished": True,
                "status_short": "FT",
                "af_fixture_id": 1,
                "score_source": "score.fulltime",
            }

        referee = ref.AfReferee(
            root,
            poll_s=0.01,
            timeout_s=1.0,
            events_fn=events_fn_mismatch,
            poll_schedule=False,
        )
        with patch.object(lib, "quote_bridge_event") as q:
            q.side_effect = AssertionError("should not quote on FT mismatch")
            bundles = lib.process_bridge_events(
                root,
                events_override=[_ft_ev(home_score=2, away_score=3)],
                af_referee=referee,
                af_mode="gate",
                trade_executor=None,
            )
            # wait for async confirm
            for _ in range(50):
                more = lib.process_bridge_events(
                    root,
                    events_override=[],
                    af_referee=referee,
                    af_mode="gate",
                    trade_executor=None,
                )
                bundles.extend(more)
                if any(b.get("mode") == "af_ft_unconfirmed" for b in bundles if isinstance(b, dict)):
                    break
                time.sleep(0.02)
        modes = [b.get("mode") for b in bundles if isinstance(b, dict)]
        check("mismatch → af_ft_unconfirmed", "af_ft_unconfirmed" in modes, str(modes))
        check("no quote on mismatch", q.call_count == 0)

        # Second FT for same match_id should be skipped via processed_ft_match_ids.
        bundles2 = lib.process_bridge_events(
            root,
            events_override=[_ft_ev(home_score=2, away_score=2)],
            af_referee=referee,
            af_mode="gate",
            trade_executor=None,
        )
        check("once-per-match skips re-FT", bundles2 == [], str(bundles2))
        cur = lib.load_cursor(root)
        check(
            "processed_ft_match_ids set",
            "m_ft" in (cur.get("processed_ft_match_ids") or []),
            str(cur.get("processed_ft_match_ids")),
        )


def test_ft_confirm_quotes_regulation() -> None:
    print("test_ft_confirm_quotes_regulation")
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "data" / "bridge").mkdir(parents=True)
        (root / "data" / "pm-quote").mkdir(parents=True)
        (root / "data" / "bridge" / "matches.json").write_text('{"matches":[]}', encoding="utf-8")
        (root / "data" / "bridge" / "events.jsonl").write_text("", encoding="utf-8")

        def events_fn_ok(mid: str, **kwargs: Any) -> dict[str, Any]:
            return {
                "ok": True,
                "goals": {"home": 1, "away": 0},
                "finished": True,
                "status_short": "FT",
                "af_fixture_id": 9,
                "score_source": "score.fulltime",
            }

        referee = ref.AfReferee(
            root,
            poll_s=0.01,
            timeout_s=1.0,
            events_fn=events_fn_ok,
            poll_schedule=False,
        )
        quoted: list[dict[str, Any]] = []

        def fake_quote(root_arg: Path, ev: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
            quoted.append(dict(ev))
            return {
                "quoted_at": lib.now_cn_iso(),
                "trigger": "match_finished",
                "match_id": ev.get("match_id"),
                "home_score": ev.get("home_score"),
                "away_score": ev.get("away_score"),
                "count": 0,
                "opportunity_count": 0,
            }

        with patch.object(lib, "quote_bridge_event", side_effect=fake_quote):
            lib.process_bridge_events(
                root,
                events_override=[_ft_ev(match_id="m_ok", home_score=1, away_score=0)],
                af_referee=referee,
                af_mode="postcheck",  # FT still gates even if goals postcheck
                trade_executor=None,
            )
            for _ in range(50):
                lib.process_bridge_events(
                    root,
                    events_override=[],
                    af_referee=referee,
                    af_mode="postcheck",
                    trade_executor=None,
                )
                if quoted:
                    break
                time.sleep(0.02)
        check("quoted after AF FT confirm", len(quoted) == 1, str(quoted))
        if quoted:
            check("uses AF regulation score", quoted[0].get("home_score") == 1 and quoted[0].get("away_score") == 0)
            check("score_source api_football", quoted[0].get("score_source") == "api_football")


def test_stale_skip_in_pipeline() -> None:
    print("test_stale_skip_in_pipeline")
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "data" / "bridge").mkdir(parents=True)
        (root / "data" / "pm-quote").mkdir(parents=True)
        (root / "data" / "bridge" / "matches.json").write_text('{"matches":[]}', encoding="utf-8")
        (root / "data" / "bridge" / "events.jsonl").write_text("", encoding="utf-8")
        old_ts = (datetime.now(TZ_CN) - timedelta(hours=4)).isoformat(timespec="seconds")
        bundles = lib.process_bridge_events(
            root,
            events_override=[_ft_ev(match_id="m_old", ts=old_ts)],
            af_referee=None,
            af_mode="off",
            trade_executor=None,
        )
        # Without referee, stale still applies before quote.
        # Re-run with a stub referee path: stale is checked before af submit.
        referee = ref.AfReferee(
            root, poll_s=0.01, timeout_s=0.2, events_fn=lambda mid, **k: {"ok": False}, poll_schedule=False
        )
        bundles = lib.process_bridge_events(
            root,
            events_override=[_ft_ev(match_id="m_old2", home_score=0, away_score=0, ts=old_ts)],
            af_referee=referee,
            af_mode="gate",
            trade_executor=None,
        )
        check(
            "pipeline marks ft_stale",
            any(b.get("mode") == "ft_stale" for b in bundles if isinstance(b, dict)),
            str([b.get("mode") for b in bundles]),
        )


def _goal_ev(
    *,
    match_id: str = "m_goal",
    prev: tuple[int, int] = (0, 0),
    curr: tuple[int, int] = (1, 0),
    ts: str | None = None,
) -> dict[str, Any]:
    return {
        "type": "score_change",
        "ts": ts or datetime.now(TZ_CN).isoformat(timespec="seconds"),
        "match_id": match_id,
        "home": "Home FC",
        "away": "Away FC",
        "prev": {"home": prev[0], "away": prev[1]},
        "curr": {"home": curr[0], "away": curr[1]},
        "home_score": curr[0],
        "away_score": curr[1],
        "is_goal": True,
        "polymarket": {"event_id": "1", "slug": "x", "url": "", "condition_ids": [], "market_refs": []},
    }


def test_goal_stale_skips_af() -> None:
    print("test_goal_stale_skips_af")
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "data" / "bridge").mkdir(parents=True)
        (root / "data" / "pm-quote").mkdir(parents=True)
        (root / "data" / "bridge" / "matches.json").write_text('{"matches":[]}', encoding="utf-8")
        (root / "data" / "bridge" / "events.jsonl").write_text("", encoding="utf-8")
        old_ts = (datetime.now(TZ_CN) - timedelta(hours=4)).isoformat(timespec="seconds")
        submitted: list[str] = []

        class _StubRef:
            def pending_event_keys(self) -> set[str]:
                return set()

            def drain_done(self) -> list:
                return []

            def submit(self, event_key: str, ev: dict, target: tuple, **kwargs: Any) -> bool:
                submitted.append(event_key)
                return True

        bundles = lib.process_bridge_events(
            root,
            events_override=[_goal_ev(match_id="m_old_g", ts=old_ts)],
            af_referee=_StubRef(),  # type: ignore[arg-type]
            af_mode="gate",
            trade_executor=None,
        )
        check("no AF submit for stale goal", submitted == [], str(submitted))
        check(
            "pipeline marks af_goal_stale",
            any(b.get("mode") == "af_goal_stale" for b in bundles if isinstance(b, dict)),
            str([b.get("mode") for b in bundles]),
        )


def test_events_offset_clamps_on_shrink() -> None:
    """data_prune shrink must not rewind cursor and replay retained events."""
    print("test_events_offset_clamps_on_shrink")
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        bridge = root / "data" / "bridge"
        bridge.mkdir(parents=True)
        path = bridge / "events.jsonl"
        line1 = (
            '{"type":"score_change","ts":"2026-08-02T10:00:00+08:00",'
            '"match_id":"1","prev":{"home":0,"away":0},"curr":{"home":1,"away":0},'
            '"home_score":1,"away_score":0,"is_goal":true}\n'
        )
        line2 = (
            '{"type":"score_change","ts":"2026-08-02T11:00:00+08:00",'
            '"match_id":"2","prev":{"home":0,"away":0},"curr":{"home":1,"away":0},'
            '"home_score":1,"away_score":0,"is_goal":true}\n'
        )
        path.write_text(line1 + line2, encoding="utf-8")
        full = path.stat().st_size
        rows, off = lib.load_bridge_quote_events(root, byte_offset=0)
        check("initial read both events", len(rows) == 2 and off == full, f"{len(rows)} off={off}")

        # Simulate prune: drop first line, file shrinks below prior EOF offset.
        path.write_text(line2, encoding="utf-8")
        new_size = path.stat().st_size
        check("file shrank", new_size < full and new_size > 0)
        rows2, off2 = lib.load_bridge_quote_events(root, byte_offset=full)
        check(
            "shrink clamps to EOF with no replay",
            rows2 == [] and off2 == new_size,
            f"rows={len(rows2)} off={off2} size={new_size}",
        )
        # New append after prune still visible from clamped offset.
        line3 = (
            '{"type":"match_finished","ts":"2026-08-02T12:00:00+08:00",'
            '"match_id":"3","home_score":1,"away_score":0}\n'
        )
        with path.open("a", encoding="utf-8") as f:
            f.write(line3)
        rows3, off3 = lib.load_bridge_quote_events(root, byte_offset=off2)
        check(
            "append after clamp is readable",
            len(rows3) == 1 and rows3[0].get("match_id") == "3" and off3 == path.stat().st_size,
            str(rows3),
        )


def main() -> int:
    test_ft_stale_and_helpers()
    test_ft_mismatch_and_confirm()
    test_ft_confirm_quotes_regulation()
    test_stale_skip_in_pipeline()
    test_goal_stale_skips_af()
    test_events_offset_clamps_on_shrink()
    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
