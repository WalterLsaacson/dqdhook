#!/usr/bin/env python3
"""Smoke: Nami MQTT overlay synthesis, clock tick/stale, offline hub (no network)."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import nami_mqtt as nm  # noqa: E402
from nami_mqtt import (  # noqa: E402
    MqttReader,
    NamiMqttHub,
    decode_push,
    encode_push_mlive,
    format_clock,
    overlay_pop,
    reset_hub_for_tests,
)
from dom_page_pool import DomPagePool, MemoryDomBackend  # noqa: E402
from dqd_stream_observe import DqdStreamObserver  # noqa: E402

_PITCH_SCRIPTS = Path(__file__).resolve().parents[2] / "pitch-state" / "scripts"
if str(_PITCH_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_PITCH_SCRIPTS))
import animation_rules as rules  # noqa: E402


class _Clock:
    def __init__(self, *, mono: float = 1000.0, wall: float = 1_700_000_120.0) -> None:
        self.mono = float(mono)
        self.wall = float(wall)

    def time_fn(self) -> float:
        return self.mono

    def wall_fn(self) -> float:
        return self.wall


def _hub(*, clock: _Clock | None = None, stale_s: float = 8.0) -> NamiMqttHub:
    clk = clock or _Clock()
    return NamiMqttHub(
        fetch_fn=lambda _nid: {},
        time_fn=clk.time_fn,
        wall_fn=clk.wall_fn,
        stale_s=stale_s,
        offline=True,
    )


def check_protobuf_roundtrip() -> None:
    raw = encode_push_mlive(vc_code=2111, ss="0-1-0-0", st=2)
    decoded = decode_push(raw)
    assert decoded["code"] == 10101, decoded
    item = decoded["mlive"][-1]
    assert item["ss"] == "0-1-0-0", item
    assert item["st"] == 2, item
    assert item["vc"]["code"] == 2111, item


def check_overlay_pop() -> None:
    cases = (
        (1112, "Home", "Away", "Home 进攻"),
        (2112, "Home", "Away", "Away 进攻"),
        (2111, "Home", "Away", "Away 危险进攻"),
        (114, "Home", "Away", "进球"),
        (145, "Home", "Away", "VAR"),
        (2145, "Home", "Away", "Away VAR"),
        (121, "Home", "Away", "射门"),
        (133, "Home", "Away", "点球不进"),
        (1134, "Home", "Away", "Home 掷界外球"),
        (125, "Home", "Away", "暂停"),
    )
    for code, home, away, want in cases:
        got = overlay_pop(code, home=home, away=away)
        assert got == want, (code, got, want)
        pop = {"pop_box": got, "center_box": "10:00 1 : 0", "marks": []}
        judge = rules.judge_dom(
            pop,
            expected_home=1,
            expected_away=0,
        )
        if any(tok in got for tok in ("危险进攻", "进攻", "掷界外球")) and "射门" not in got:
            assert judge["play_state"] == "in_play", (got, judge)
        elif "进球" in got:
            assert judge["play_state"] == "stopped", (got, judge)
            assert judge["stopped_reason"] == "celebration", (got, judge)
        elif "VAR" in got:
            assert judge["play_state"] == "stopped", (got, judge)
            assert judge["stopped_reason"] == "var", (got, judge)
        elif "点球不进" in got or "暂停" in got:
            assert judge["play_state"] == "stopped", (got, judge)
        elif "射门" in got:
            assert judge["play_state"] != "in_play", (got, judge)


def check_clock_tick_and_stale() -> None:
    clock = _Clock()
    hub = _hub(clock=clock, stale_s=8.0)
    hub.start()
    uptime = int(clock.wall - 125)
    hub.inject_variable(
        "4597886",
        status_id=2,
        ticking=True,
        home=0,
        away=1,
        uptime=uptime,
        vc_code=2112,
        ss="0-1-0-0",
    )
    hub.inject_mlive("4597886", vc_code=2112, ss="0-1-0-0", st=2)
    snap, err = hub.snapshot("4597886", home="Anorthosis", away="APOEL", refresh_http=False)
    assert err is None and snap is not None, (snap, err)
    assert snap["pop_box"] == "APOEL 进攻", snap
    assert snap["center_box"] == format_clock(125, 0, 1), snap
    first_clock = snap["center_box"]

    clock.wall += 5
    clock.mono += 5
    snap2, err2 = hub.snapshot("4597886", home="Anorthosis", away="APOEL", refresh_http=False)
    assert err2 is None and snap2 is not None
    assert snap2["center_box"] == format_clock(130, 0, 1), snap2
    assert snap2["center_box"] != first_clock

    clock.mono += 9
    clock.wall += 9
    frozen, err3 = hub.snapshot("4597886", home="Anorthosis", away="APOEL", refresh_http=False)
    assert err3 is None and frozen is not None
    assert frozen["center_box"] == snap2["center_box"], frozen
    prev = rules.parse_dom_center(snap2["center_box"]).get("clock")
    judge = rules.judge_dom(
        frozen,
        expected_home=0,
        expected_away=1,
        prev_clock=prev,
    )
    assert judge["play_state"] == "unclear", judge
    assert judge["stopped_reason"] == "stale_page", judge


def check_mqtt_reader() -> None:
    hub = _hub()
    reset_hub_for_tests(hub)
    try:
        hub.start()
        hub.inject_variable("99", status_id=2, ticking=False, home=1, away=0, vc_code=1112, ss="1-0-0-0")
        hub.inject_mlive("99", vc_code=1112, ss="1-0-0-0", st=2)
        reader = MqttReader("99", match_id="dqd-1", home="Home", away="Away", hub=hub)
        ok, err = reader.open()
        assert ok and err is None, (ok, err)
        assert reader.source == "mqtt"
        dom, rerr = reader.read()
        assert rerr is None and dom is not None
        assert dom["pop_box"] == "Home 进攻", dom
        assert "1 : 0" in str(dom["center_box"]), dom
        reader.close()
        closed, cerr = reader.read()
        assert closed is None and cerr == "not_open"
    finally:
        reset_hub_for_tests()


def check_truncated_protobuf() -> None:
    assert nm.decode_fields(b"") == []
    assert nm.decode_fields(b"\x12\xff\xff\xff\xff\x0f") == []


def check_vc_zero_clears_overlay() -> None:
    hub = _hub()
    hub.start()
    hub.inject_mlive("99", vc_code=2112, ss="0-1-0-0", st=2)
    snap, _err = hub.snapshot("99", home="H", away="A")
    assert snap and snap["pop_box"] == "A 进攻", snap

    class _Msg:
        topic = "live/m1/99"
        payload = encode_push_mlive(vc_code=0, ss="0-1-0-0", st=2)

    hub._on_message(None, None, _Msg())
    cleared, _err2 = hub.snapshot("99", home="H", away="A")
    assert cleared is not None
    assert cleared["pop_box"] == "", cleared
    assert overlay_pop(None, home="H", away="A") == ""


def check_second_half_clock() -> None:
    clock = _Clock()
    hub = _hub(clock=clock)
    hub.start()
    hub.inject_variable(
        "2h",
        status_id=4,
        ticking=True,
        home=1,
        away=0,
        uptime=int(clock.wall - 10),
        ss="1-0-0-0",
        vc_code=1112,
    )
    snap, err = hub.snapshot("2h")
    assert err is None and snap is not None
    assert snap["center_box"] == format_clock(45 * 60 + 10, 1, 0), snap

    clock.mono += 5
    clock.wall += 5
    snap2, _err = hub.snapshot("2h")
    assert snap2 is not None
    assert snap2["center_box"] == format_clock(45 * 60 + 15, 1, 0), snap2


def check_http_second_clock() -> None:
    clock = _Clock()
    hub = _hub(clock=clock)
    hub.start()
    hub.inject_variable(
        "http-clk",
        status_id=4,
        ticking=True,
        home=2,
        away=1,
        second=2700,
        ss="2-1-0-0",
        vc_code=1112,
    )
    snap, err = hub.snapshot("http-clk")
    assert err is None and snap is not None
    assert snap["center_box"] == format_clock(2700, 2, 1), snap
    clock.mono += 8
    clock.wall += 8
    snap2, _err = hub.snapshot("http-clk")
    assert snap2 is not None
    assert snap2["center_box"] == format_clock(2708, 2, 1), snap2


def check_snapshot_skips_http() -> None:
    hits: list[str] = []

    def fetch(nid: str) -> dict:
        hits.append(nid)
        return {
            "statusId": 2,
            "timer": {"ticking": 1, "second": 100, "uptime": 1_700_000_000},
            "homeScores": {"score": 0},
            "awayScores": {"score": 0},
        }

    hub = NamiMqttHub(fetch_fn=fetch, offline=True)
    hub.start()
    hub.inject_mlive("77", vc_code=1112, ss="0-0-0-0", st=2)
    reader = MqttReader("77", home="H", away="A", hub=hub)
    assert reader.open()[0]
    after_open = len(hits)
    assert after_open == 1, hits
    reader.read()
    reader.read()
    assert len(hits) == after_open, hits
    hub.snapshot("77", refresh_http=True)
    assert len(hits) == after_open + 1, hits
    reader.close()


def check_connect_fail_retry() -> None:
    class Hub(NamiMqttHub):
        def __init__(self) -> None:
            super().__init__(offline=False, fetch_fn=lambda _n: {})
            self.creates = 0

        def _connect_mqtt(self) -> tuple[bool, str | None]:
            self.creates += 1
            if self.creates == 1:
                self._client = object()
                return False, "mqtt_connect_timeout"
            self._connected = True
            self._client = object()
            return True, None

    hub = Hub()
    ok, err = hub.start()
    assert ok is False and err == "mqtt_connect_timeout", (ok, err)
    assert hub._client is None, "failed start must drop the client"
    ok2, err2 = hub.start()
    assert ok2 is True and err2 is None, (ok2, err2)
    assert hub.creates == 2, hub.creates


def check_reconnect_reuses_client() -> None:
    class Hub(NamiMqttHub):
        def __init__(self) -> None:
            super().__init__(offline=False, fetch_fn=lambda _n: {})
            self.creates = 0

        def _connect_mqtt(self) -> tuple[bool, str | None]:
            self.creates += 1
            self._client = object()
            self._connected = True
            return True, None

        def _wait_connected(self, timeout_s: float) -> tuple[bool, str | None]:
            del timeout_s
            self._connected = True
            return True, None

    hub = Hub()
    assert hub.start()[0]
    hub._connected = False
    assert hub.start()[0]
    assert hub.creates == 1, hub.creates


def check_subscribe_requires_connection() -> None:
    hub = NamiMqttHub(offline=False, fetch_fn=lambda _n: {})
    ok, err = hub.subscribe("1")
    assert ok is False
    assert "mqtt" in str(err or "")


def check_observer_skips_chromium() -> None:
    old = os.environ.get("QUOTE_GATE_SOURCE")
    os.environ["QUOTE_GATE_SOURCE"] = "mqtt"

    class BoomPool(DomPagePool):
        def start(self) -> None:
            raise AssertionError("chromium must not start when gate-source=mqtt")

        def shutdown(self) -> None:
            return None

    hub = _hub()
    reset_hub_for_tests(hub)
    try:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            obs = DqdStreamObserver(
                root,
                discover_fn=lambda mid, *, root=None, timeout=None: {
                    "match_id": mid,
                    "nami_id": "4597886",
                    "page_url": "https://tracker.namitiyu.com/zh/football?id=4597886",
                    "surface": "animation",
                },
                page_pool=BoomPool(backend=MemoryDomBackend()),
            )
            obs.start()
            try:
                hub.inject_mlive("4597886", vc_code=2111, ss="0-0-0-0", st=2)
                reader, err, info = obs.acquire_dom_reader(
                    "m1",
                    "https://tracker.namitiyu.com/zh/football?id=4597886",
                    {"nami_id": "4597886", "home": "H", "away": "A"},
                )
                assert err is None and reader is not None, (err, info)
                assert reader.source == "mqtt"
                dom, rerr = reader.read()
                assert rerr is None and dom is not None
                assert "危险进攻" in str(dom.get("pop_box") or ""), dom
                reader.close()
                obs.release_match("m1", reason="done")
                stats = obs.sync_playing_pages()
                assert "warmed" in stats and "closed" in stats
            finally:
                obs.stop()
    finally:
        reset_hub_for_tests()
        if old is None:
            os.environ.pop("QUOTE_GATE_SOURCE", None)
        else:
            os.environ["QUOTE_GATE_SOURCE"] = old


def main() -> int:
    check_protobuf_roundtrip()
    check_truncated_protobuf()
    check_overlay_pop()
    check_vc_zero_clears_overlay()
    check_clock_tick_and_stale()
    check_second_half_clock()
    check_http_second_clock()
    check_mqtt_reader()
    check_snapshot_skips_http()
    check_connect_fail_retry()
    check_reconnect_reuses_client()
    check_subscribe_requires_connection()
    check_observer_skips_chromium()
    print("ok: nami mqtt overlay + clock + offline hub (no Chromium)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
