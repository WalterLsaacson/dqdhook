#!/usr/bin/env python3
"""Smoke: AF observe samples on the same clock as DOM (+5s / 5s / 120s)."""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[4]
SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import af_observe as ao  # noqa: E402


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "data" / "pm-quote").mkdir(parents=True)

        polls: list[float] = []
        lock = threading.Lock()

        def fake_poll(self, match_id: str):  # noqa: ANN001
            with lock:
                polls.append(time.monotonic())
            return {
                "ok": True,
                "af_fixture_id": 99,
                "goals": {"home": 1, "away": 0},
                "cache_entry": {
                    "af_home": "Home FC",
                    "af_away": "Away United",
                },
            }

        # Speed up the schedule for the smoke (still same shape).
        with (
            patch.object(ao, "AF_FIRST_DELAY_S", 0.15),
            patch.object(ao, "AF_INTERVAL_S", 0.1),
            patch.object(ao, "AF_TIMEOUT_S", 0.45),
            patch.object(ao.AfScoreObserver, "_poll", fake_poll),
            patch.dict("os.environ", {"QUOTE_AF_OBSERVE": "1"}, clear=False),
        ):
            # Bypass key load in try_create — construct directly.
            obs = ao.AfScoreObserver(root)
            obs.start()
            once = obs.sample_once(
                {
                    "match_id": "m0",
                    "home": "Home FC",
                    "away": "Away United",
                    "home_score": 1,
                    "away_score": 0,
                    "ts": "2026-08-21T11:00:00+08:00",
                },
                event_key="m0|1-0",
                sample_i=0,
                elapsed_s=5.0,
            )
            assert once.get("ok") is True and once.get("score_match") is True, once
            assert once.get("elapsed_s") == 5.0
            t0 = time.monotonic()
            assert obs.start_session(
                {
                    "match_id": "m1",
                    "home": "Home FC",
                    "away": "Away United",
                    "home_score": 1,
                    "away_score": 0,
                    "ts": "2026-08-21T12:00:00+08:00",
                },
                event_key="m1|1-0",
            )
            deadline = time.monotonic() + 3.0
            path = ao.observe_path(root)
            while time.monotonic() < deadline:
                if path.is_file():
                    n = sum(
                        1
                        for ln in path.read_text(encoding="utf-8").splitlines()
                        if '"event_key": "m1|1-0"' in ln or '"event_key":"m1|1-0"' in ln
                    )
                    if n >= 3:
                        break
                time.sleep(0.05)

            rows = [
                json.loads(ln)
                for ln in path.read_text(encoding="utf-8").splitlines()
                if ln.strip()
            ]
            sess_rows = [r for r in rows if r.get("event_key") == "m1|1-0"]
            assert len(sess_rows) >= 3, sess_rows
            assert all(r.get("source") == "af" for r in sess_rows)
            assert all(r.get("af_score") == "1-0" for r in sess_rows)
            assert all(r.get("score_match") is True for r in sess_rows)
            # First sample near first_delay; last within timeout.
            assert abs(sess_rows[0]["elapsed_s"] - 0.15) < 0.12, sess_rows[0]
            assert sess_rows[-1]["elapsed_s"] <= 0.45 + 0.12, sess_rows[-1]
            assert polls, "expected AF polls"
            # Spaced roughly by interval after first.
            if len(polls) >= 3:
                gaps = [b - a for a, b in zip(polls[1:], polls[2:])]
                assert all(g >= 0.05 for g in gaps), gaps
            assert all(r.get("is_reversal") is not True for r in sess_rows)

            assert obs.start_session(
                {
                    "match_id": "m2",
                    "home": "Home FC",
                    "away": "Away United",
                    "home_score": 0,
                    "away_score": 0,
                    "is_reversal": True,
                    "observe_only": True,
                    "ts": "2026-08-21T12:01:00+08:00",
                },
                event_key="m2|1-0->0-0",
            )
            deadline2 = time.monotonic() + 3.0
            while time.monotonic() < deadline2:
                all_rows = [
                    json.loads(ln)
                    for ln in path.read_text(encoding="utf-8").splitlines()
                    if ln.strip()
                ]
                if any(r.get("event_key") == "m2|1-0->0-0" for r in all_rows):
                    break
                time.sleep(0.05)
            obs.stop()
            time.sleep(0.05)
            all_rows = [
                json.loads(ln)
                for ln in path.read_text(encoding="utf-8").splitlines()
                if ln.strip()
            ]
            rev_rows = [r for r in all_rows if r.get("event_key") == "m2|1-0->0-0"]
            assert rev_rows, all_rows[-3:]
            assert rev_rows[0].get("is_reversal") is True
            assert rev_rows[0].get("observe_only") is True

            print(
                f"ok af_observe samples={len(sess_rows)} "
                f"elapsed={[round(r['elapsed_s'], 3) for r in sess_rows]} "
                f"wall={time.monotonic() - t0:.2f}s reversal_rows={len(rev_rows)}",
                flush=True,
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
