#!/usr/bin/env python3
"""Smoke: dqd_live discovery cache + payload parsing."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import dqd_live  # noqa: E402


def main() -> int:
    calls: list[str] = []

    def fake_fetch(path: str, params: dict[str, object], timeout: float) -> dict[str, object]:
        calls.append(path)
        mid = str(params.get("match_id") or "")
        if mid == "video":
            return {
                "data": {
                    "playInfo": {"m3u8": "https://cdn.example.com/live/video.m3u8"},
                    "live_status": "video",
                }
            }
        if mid == "anim":
            return {"data": {"animation": True, "room": {"status": "open"}}}
        return {"data": {}}

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        video = dqd_live.discover_live_surface("video", root=root, fetch_json_fn=fake_fetch)
        assert video["stream_url"].endswith(".m3u8"), video
        assert video["surface"] == "video", video

        anim = dqd_live.discover_live_surface("anim", root=root, fetch_json_fn=fake_fetch)
        assert anim["stream_url"] is None, anim
        assert anim["surface"] == "animation", anim

        miss = dqd_live.discover_live_surface("none", root=root, fetch_json_fn=fake_fetch)
        assert miss["surface"] == "page_only", miss
        assert miss["page_url"] == "https://www.dongqiudi.com/match/none", miss

        before = len(calls)
        cached = dqd_live.discover_live_surface("video", root=root, fetch_json_fn=fake_fetch)
        assert cached["stream_url"] == video["stream_url"], cached
        assert len(calls) == before, calls

    print("ok: dqd_live discover video/animation/page-only + cache")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
