#!/usr/bin/env python3
"""Smoke: DQD stream observer schedules 6 samples and writes frame rows."""

from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import dqd_stream_observe as obs_mod  # noqa: E402
from dqd_stream_observe import DqdStreamObserver, observe_path  # noqa: E402


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "data" / "pm-quote").mkdir(parents=True)
        old_count = obs_mod.SAMPLE_COUNT
        old_interval = obs_mod.SAMPLE_INTERVAL_S
        obs_mod.SAMPLE_COUNT = 3
        obs_mod.SAMPLE_INTERVAL_S = 0.02

        def fake_discover(match_id: str, *, root: Path | None = None) -> dict[str, object]:
            return {
                "match_id": match_id,
                "page_url": f"https://example.com/live/{match_id}",
                "stream_url": None,
                "surface": "page_only",
                "discovered_at": "2026-01-01T00:00:00+08:00",
                "raw_hint": {"source": "fake"},
            }

        def fake_capture(_page_url: str, frame_path: Path) -> tuple[bool, str | None]:
            frame_path.parent.mkdir(parents=True, exist_ok=True)
            frame_path.write_bytes(b"jpeg")
            return True, None

        obs: DqdStreamObserver | None = None
        try:
            obs = DqdStreamObserver(
                root,
                discover_fn=fake_discover,
                capture_page_fn=fake_capture,
                capture_stream_fn=lambda *_args, **_kwargs: (False, "unused"),
            )
            obs.start()
            ok = obs.enqueue_event(
                {
                    "type": "score_change",
                    "ts": "2026-08-19T14:00:00+08:00",
                    "match_id": "m1",
                    "home": "Home",
                    "away": "Away",
                    "home_score": 1,
                    "away_score": 0,
                },
                event_key="score_change|m1|0-0->1-0",
            )
            assert ok
            deadline = time.time() + 5
            path = observe_path(root)
            rows: list[dict[str, object]] = []
            while time.time() < deadline:
                if path.is_file():
                    rows = [
                        json.loads(line)
                        for line in path.read_text(encoding="utf-8").splitlines()
                        if line.strip()
                    ]
                    if len(rows) >= 3:
                        break
                time.sleep(0.25)
            assert len(rows) == 3, rows
            assert rows[0]["elapsed_s"] == 0.0, rows[0]
            assert rows[-1]["sample_i"] == 2, rows[-1]
            assert all(row["ok"] is True for row in rows), rows
            assert all(row["frame_kind"] == "page" for row in rows), rows
            assert all(Path(str(row["frame_path"])).is_file() for row in rows), rows

            # Animation surface must prefer page capture even when a stream URL is present.
            calls = {"ffmpeg": 0, "page": 0}

            def anim_discover(match_id: str, *, root: Path | None = None) -> dict[str, object]:
                return {
                    "match_id": match_id,
                    "page_url": f"https://example.com/live/{match_id}",
                    "stream_url": "https://example.com/live.m3u8",
                    "surface": "animation",
                    "discovered_at": "2026-01-01T00:00:00+08:00",
                    "raw_hint": {"source": "fake"},
                }

            def anim_page(_page_url: str, frame_path: Path) -> tuple[bool, str | None, str]:
                calls["page"] += 1
                frame_path.parent.mkdir(parents=True, exist_ok=True)
                frame_path.write_bytes(b"jpeg")
                return True, None, "animation"

            def anim_stream(_stream_url: str, frame_path: Path) -> tuple[bool, str | None]:
                calls["ffmpeg"] += 1
                return False, "should_not_ffmpeg_animation"

            obs2 = DqdStreamObserver(
                root,
                discover_fn=anim_discover,
                capture_page_fn=anim_page,
                capture_stream_fn=anim_stream,
            )
            row = obs2._capture_row(
                obs_mod._ObserveJob(
                    match_id="m2",
                    event_key="score_change|m2|0-0->1-0",
                    dqd_ts="2026-08-19T14:00:00+08:00",
                    home="Home",
                    away="Away",
                    home_score=1,
                    away_score=0,
                    t0_mono=time.monotonic(),
                ),
                sample_i=0,
                elapsed_s=0.0,
            )
            assert calls["ffmpeg"] == 0, calls
            assert calls["page"] == 1, calls
            assert row["frame_kind"] == "animation", row
            assert row["capture_method"] == "playwright", row
        finally:
            if obs is not None:
                obs.stop()
            obs_mod.SAMPLE_COUNT = old_count
            obs_mod.SAMPLE_INTERVAL_S = old_interval

    print("ok: dqd_stream_observe writes 6 screenshot rows")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
