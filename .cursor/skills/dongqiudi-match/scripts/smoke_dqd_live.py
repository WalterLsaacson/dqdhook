#!/usr/bin/env python3
"""Smoke: dqd_live discovery cache + payload parsing."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import dqd_live  # noqa: E402


def _write_snapshot(root: Path, matches: list[dict[str, object]]) -> None:
    (root / "data").mkdir(parents=True, exist_ok=True)
    (root / "data" / "snapshot.json").write_text(
        json.dumps({"matches": matches}), encoding="utf-8"
    )


def check_animation_from_snapshot() -> None:
    """animation_live from the match list wins over any endpoint probing."""

    def boom(*_a: object, **_k: object) -> dict[str, object]:
        raise AssertionError("must not fetch when animation_live is known")

    tracker = "https://tracker.namitiyu.com/zh/football?profile=P&id=4473527"
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _write_snapshot(
            root,
            [
                {"id": "54350954", "animation_live": tracker},
                {"id": "99999999", "animation_live": None},
            ],
        )

        got = dqd_live.discover_live_surface("54350954", root=root, fetch_json_fn=boom)
        assert got["surface"] == "animation", got
        assert got["page_url"] == tracker, got
        assert got["nami_id"] == "4473527", got
        assert got["stream_url"] is None, got

        # A row without animation_live must fall back to the old probe path.
        probes: list[str] = []

        def probe(path: str, _params: object, _timeout: float) -> dict[str, object]:
            probes.append(path)
            return {"data": {}}

        miss = dqd_live.discover_live_surface("99999999", root=root, fetch_json_fn=probe)
        assert miss["surface"] == "page_only", miss
        assert miss["nami_id"] is None, miss
        assert probes, "expected fallback probing"

        # The memoized snapshot must notice a rewrite (DQD rewrites it every tick).
        _write_snapshot(
            root,
            [{"id": "54350954", "animation_live": tracker.replace("4473527", "777")}],
        )
        again = dqd_live.discover_live_surface("54350954", root=root, fetch_json_fn=boom)
        assert again["nami_id"] == "777", again

    # No snapshot at all → previous behaviour, no crash.
    with tempfile.TemporaryDirectory() as td:
        assert dqd_live.animation_url_from_snapshot("1", Path(td)) == ""
    assert dqd_live.animation_url_from_snapshot("1", None) == ""


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

    check_animation_from_snapshot()
    print("ok: dqd_live discover video/animation/page-only + cache + nami tracker")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
