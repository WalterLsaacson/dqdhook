#!/usr/bin/env python3
"""Offline smoke for nami_observe: MQTT framing, payload harvest, subscription TTL."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import nami_observe as no  # noqa: E402

# Real 10101 frame captured off wss://trackermq.namitiyu.com (score 0-0, ball 0.09,0.53).
LIVE_FRAME_HEX = (
    "08f54e122e080110b2c793021a250a07302d302d302d30120e08bf101a09302e30392c302e35331a08"
    "0100f5c2a0d406013002"
)


def test_mqtt_framing() -> None:
    pkt = no.connect_packet("mqttjs_12345678")
    assert pkt[0] == 0x10, pkt[:1]
    assert pkt[2:8] == b"\x00\x04MQTT", pkt[:10]
    assert pkt[8] == 4, "protocol level 3.1.1"
    # remaining length must describe the rest of the packet exactly
    assert pkt[1] == len(pkt) - 2, (pkt[1], len(pkt))

    sub = no.subscribe_packet("live/m1/4473527", 1000)
    assert sub[0] == 0x82, sub[:1]
    assert sub[1] == len(sub) - 2
    assert b"live/m1/4473527" in sub
    assert sub[-1] == 0, "qos 0"

    unsub = no.unsubscribe_packet("live/m1/4473527", 1001)
    assert unsub[0] == 0xA2, unsub[:1]
    assert unsub[1] == len(unsub) - 2
    assert b"live/m1/4473527" in unsub

    # Remaining-length must use multi-byte varints past 127.
    long_topic = "live/m1/" + "9" * 200
    long_sub = no.subscribe_packet(long_topic, 7)
    assert long_sub[1] & 0x80, "expected continuation bit for >127B packet"


def test_parse_publish() -> None:
    topic = "live/m1/4473527"
    payload = b"\x01\x02\x03"
    body = len(topic).to_bytes(2, "big") + topic.encode() + payload
    packet = bytes([0x30]) + no._remaining_length(len(body)) + body
    got = no.parse_publish(packet)
    assert got == (topic, payload), got

    assert no.parse_publish(b"") is None
    assert no.parse_publish(b"\x20\x02\x00\x00") is None, "CONNACK is not a PUBLISH"
    assert no.parse_publish(bytes([0x30, 0x7F])) is None, "truncated must not raise"


def test_harvest_live_frame() -> None:
    got = no.harvest(bytes.fromhex(LIVE_FRAME_HEX))
    assert got.get("score_raw") == "0-0-0-0", got
    assert got.get("ball_xy") == "0.09,0.53", got
    assert got.get("msg_type") == 10101, got


def test_harvest_is_total() -> None:
    """Undocumented schema: garbage in must never raise, only yield less."""
    for blob in (
        b"",
        b"\xff" * 32,
        bytes.fromhex(LIVE_FRAME_HEX)[:17],
        bytes.fromhex(LIVE_FRAME_HEX) + b"\x80\x80\x80",
        bytes(range(256)),
    ):
        out = no.harvest(blob)
        assert isinstance(out, dict), blob[:8]


def test_subscription_ttl() -> None:
    obs = no.NamiObserver(Path(tempfile.mkdtemp()))
    obs.observe_match("111", {"match_id": "m1"}, ttl_s=60)
    obs.observe_match("222", {"match_id": "m2"}, ttl_s=60)
    assert obs.stats()["wanted"] == 2, obs.stats()

    # Re-registering must extend, never shorten.
    obs.observe_match("111", {"match_id": "m1"}, ttl_s=1)
    with obs._lock:
        assert obs._wanted["111"]["until"] > time.monotonic() + 30

    obs.release_match("222")
    with obs._lock:
        assert obs._wanted["222"]["until"] <= time.monotonic() + no.LINGER_S + 1

    obs.release_match("does-not-exist")
    obs.observe_match("", {}, ttl_s=10)
    assert obs.stats()["wanted"] == 2, obs.stats()


def test_record_writes_row() -> None:
    root = Path(tempfile.mkdtemp())
    obs = no.NamiObserver(root)
    obs.observe_match("4473527", {"match_id": "54350954", "home": "H", "away": "A"}, ttl_s=60)
    obs._record("live/m1/4473527", bytes.fromhex(LIVE_FRAME_HEX))

    rows = [
        json.loads(line)
        for line in no.observe_path(root).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(rows) == 1, rows
    row = rows[0]
    assert row["nami_id"] == "4473527", row
    assert row["match_id"] == "54350954", row
    assert row["score_raw"] == "0-0-0-0", row
    assert row["ball_xy"] == "0.09,0.53", row
    assert row["payload_hex"] == LIVE_FRAME_HEX, row

    # Unknown topic still records rather than dropping the sample.
    obs._record("live/m1/999/nft/zh", b"\x08\x01")
    rows = [
        json.loads(line)
        for line in no.observe_path(root).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(rows) == 2 and rows[1]["match_id"] is None, rows[1]


def test_env_gate() -> None:
    prev = os.environ.get("QUOTE_NAMI_OBSERVE")
    try:
        os.environ.pop("QUOTE_NAMI_OBSERVE", None)
        assert no.try_create_observer(Path(tempfile.mkdtemp())) is None
        os.environ["QUOTE_NAMI_OBSERVE"] = "0"
        assert no.try_create_observer(Path(tempfile.mkdtemp())) is None
        os.environ["QUOTE_NAMI_OBSERVE"] = "1"
        assert no.try_create_observer(Path(tempfile.mkdtemp())) is not None
    finally:
        if prev is None:
            os.environ.pop("QUOTE_NAMI_OBSERVE", None)
        else:
            os.environ["QUOTE_NAMI_OBSERVE"] = prev


def main() -> int:
    test_mqtt_framing()
    test_parse_publish()
    test_harvest_live_frame()
    test_harvest_is_total()
    test_subscription_ttl()
    test_record_writes_row()
    test_env_gate()
    print("smoke_nami_observe OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
