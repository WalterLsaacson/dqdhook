#!/usr/bin/env python3
"""Smoke: schedule_list IncompleteRead retries + day fallback keep fixtures."""

from __future__ import annotations

import http.client
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

_SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPTS))

import dqd_lib as lib  # noqa: E402


def _assert(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


def test_fetch_json_retries_incomplete_read() -> None:
    print("test_fetch_json_retries_incomplete_read")
    calls = {"n": 0}

    class _Resp:
        def read(self) -> bytes:
            calls["n"] += 1
            if calls["n"] < 3:
                raise http.client.IncompleteRead(b"{", 10)
            return b'{"data":{"matches":[{"id":"1"}]}}'

        def __enter__(self) -> "_Resp":
            return self

        def __exit__(self, *args: object) -> None:
            return None

    with patch("urllib.request.urlopen", return_value=_Resp()):
        payload = lib.fetch_json("/x", {}, timeout=1.0, retries=4, retry_sleep_s=0.0)
    _assert(calls["n"] == 3, f"expected 3 attempts, got {calls['n']}")
    _assert(payload["data"]["matches"][0]["id"] == "1", str(payload))


def test_load_matches_keeps_fallback_on_schedule_fail() -> None:
    print("test_load_matches_keeps_fallback_on_schedule_fail")
    today = lib.today_cn()
    window = lib.day_window(today, 3)
    future = [d for d in window if d > today]
    _assert(bool(future), "need a future day in window")
    day = future[0]
    fallback = [
        {
            "id": "keep-me",
            "local_date": day,
            "time": "20:00",
            "home": "Alpha",
            "away": "Beta",
            "league": "英超",
            "status": "Fixture",
            "status_raw": "Fixture",
            "home_score": None,
            "away_score": None,
            "tabs": ["full"],
        }
    ]

    def boom(*_a: object, **_k: object) -> list:
        raise lib.FetchError("IncompleteRead: boom")

    with (
        patch.object(lib, "_map_soccer_list", return_value=[]),
        patch.object(lib, "fetch_soccer_schedule_list", side_effect=boom),
        patch.object(lib, "apply_english_team_names", side_effect=lambda ms, **_k: ms),
    ):
        got = lib.load_matches(language="en", day=today, days=3, fallback_matches=fallback)
    ids = {str(m.get("id")) for m in got}
    _assert("keep-me" in ids, f"fallback lost: {ids}")


def test_resolve_team_en_priority_order() -> None:
    print("test_resolve_team_en_priority_order")
    fetched: list[str] = []

    def fake_fetch(tid: str) -> str:
        fetched.append(str(tid))
        return f"Name {tid}"

    with tempfile.TemporaryDirectory() as td:
        cache_path = Path(td) / "dqd_team_en.json"
        with (
            patch.object(lib, "team_en_cache_path", return_value=cache_path),
            patch.object(lib, "fetch_team_en_name", side_effect=fake_fetch),
        ):
            lib.save_team_en_cache({})
            lib.resolve_team_en_names(
                ["10", "999", "20"],
                priority_ids=["999", "20"],
                max_fetch=2,
                fetch_timeout_s=5.0,
                workers=2,
            )
    _assert(fetched[:2] == ["999", "20"], f"priority first, got {fetched}")


def main() -> int:
    test_fetch_json_retries_incomplete_read()
    test_load_matches_keeps_fallback_on_schedule_fail()
    test_resolve_team_en_priority_order()
    print("ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
