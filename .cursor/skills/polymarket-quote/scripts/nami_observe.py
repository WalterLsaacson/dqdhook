"""Observe-only nami (纳米数据) live feed for matches under pitch-gate.

The animation DQD embeds is served by nami, whose tracker page streams the live
state over MQTT-on-WebSocket. That stream carries the score and the ball's
normalized pitch position directly, i.e. the same facts the OCR gate infers from
a screenshot. This module only records it so the two can be compared offline; it
never feeds the trading path.

Enable with ``QUOTE_NAMI_OBSERVE=1``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import re
import threading
import time
from pathlib import Path
from typing import Any

import quote_lib as lib

logger = logging.getLogger("pm_quote.nami_observe")

WS_URL = "wss://trackermq.namitiyu.com/mqtt"
WS_ORIGIN = "https://tracker.namitiyu.com"

# Keep observing a little past the gate window so late reversals are captured.
LINGER_S = 60.0
RECONNECT_MIN_S = 2.0
RECONNECT_MAX_S = 60.0

_SCORE_RE = re.compile(r"^\d{1,3}(?:-\d{1,3}){2,3}$")
_XY_RE = re.compile(r"^-?\d+(?:\.\d+)?,-?\d+(?:\.\d+)?$")

_active: "NamiObserver | None" = None


def observe_path(root: Path) -> Path:
    return lib.data_dir(root) / "nami_observe.jsonl"


def set_active_observer(observer: "NamiObserver | None") -> None:
    global _active
    _active = observer


def get_active_observer() -> "NamiObserver | None":
    return _active


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


# --- minimal MQTT 3.1.1 packet building / parsing -------------------------


def _remaining_length(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n % 128
        n //= 128
        if n:
            b |= 0x80
        out.append(b)
        if not n:
            return bytes(out)


def connect_packet(client_id: str) -> bytes:
    body = bytearray()
    body += (4).to_bytes(2, "big") + b"MQTT"
    body += bytes([4])      # protocol level 3.1.1
    body += bytes([0xC2])   # clean session + (empty) username/password
    body += (60).to_bytes(2, "big")
    body += len(client_id).to_bytes(2, "big") + client_id.encode()
    body += (0).to_bytes(2, "big")
    body += (0).to_bytes(2, "big")
    return bytes([0x10]) + _remaining_length(len(body)) + bytes(body)


def subscribe_packet(topic: str, packet_id: int) -> bytes:
    body = (
        packet_id.to_bytes(2, "big")
        + len(topic).to_bytes(2, "big")
        + topic.encode()
        + bytes([0])
    )
    return bytes([0x82]) + _remaining_length(len(body)) + bytes(body)


def unsubscribe_packet(topic: str, packet_id: int) -> bytes:
    body = packet_id.to_bytes(2, "big") + len(topic).to_bytes(2, "big") + topic.encode()
    return bytes([0xA2]) + _remaining_length(len(body)) + bytes(body)


def _read_varint(buf: bytes, i: int) -> tuple[int, int]:
    shift = val = 0
    while i < len(buf):
        b = buf[i]
        i += 1
        val |= (b & 0x7F) << shift
        if not b & 0x80:
            return val, i
        shift += 7
        if shift > 63:
            break
    raise ValueError("truncated varint")


def parse_publish(data: bytes) -> tuple[str, bytes] | None:
    """Return ``(topic, payload)`` for an MQTT PUBLISH packet, else None."""
    if len(data) < 2 or data[0] >> 4 != 3:
        return None
    try:
        remaining, i = _read_varint(data, 1)
    except ValueError:
        return None
    end = i + remaining
    if remaining < 2 or end > len(data):
        return None
    topic_len = int.from_bytes(data[i : i + 2], "big")
    i += 2
    if topic_len <= 0 or i + topic_len > end:
        return None
    topic = data[i : i + topic_len].decode("utf-8", "replace")
    return topic, data[i + topic_len : end]


# --- protobuf field harvesting -------------------------------------------


def harvest(payload: bytes, depth: int = 0) -> dict[str, Any]:
    """Pull score / ball position out of an undocumented protobuf payload.

    Nami publishes no schema, so rather than pin field numbers this scans the
    wire format for the two value shapes that matter (``"1-0-1-0"`` scores and
    ``"0.62,0.77"`` coordinates) and keeps the leading varint as a message type.
    """
    found: dict[str, Any] = {}
    if depth > 6:
        return found
    i = 0
    first_varint: int | None = None
    while i < len(payload):
        try:
            key, i = _read_varint(payload, i)
        except ValueError:
            break
        field, wire = key >> 3, key & 7
        if wire == 0:
            try:
                val, i = _read_varint(payload, i)
            except ValueError:
                break
            if depth == 0 and field == 1 and first_varint is None:
                first_varint = val
        elif wire == 1:
            i += 8
        elif wire == 5:
            i += 4
        elif wire == 2:
            try:
                ln, i = _read_varint(payload, i)
            except ValueError:
                break
            if ln < 0 or i + ln > len(payload):
                break
            sub, i = payload[i : i + ln], i + ln
            text = None
            try:
                text = sub.decode("utf-8")
            except UnicodeDecodeError:
                text = None
            if text and _SCORE_RE.match(text):
                found.setdefault("score_raw", text)
            elif text and _XY_RE.match(text):
                found.setdefault("ball_xy", text)
            elif sub:
                for k, v in harvest(sub, depth + 1).items():
                    found.setdefault(k, v)
        else:
            break
    if depth == 0 and first_varint is not None:
        found["msg_type"] = first_varint
    return found


class NamiObserver:
    """Subscribes to nami live topics for the matches currently under gate."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self._lock = threading.Lock()
        # nami_id -> {"meta": {...}, "until": monotonic deadline}
        self._wanted: dict[str, dict[str, Any]] = {}
        self._subscribed: set[str] = set()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._packet_id = 1
        self._rows = 0

    # -- lifecycle --------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run_forever, name="nami-observe", daemon=True
        )
        self._thread.start()
        set_active_observer(self)
        print(
            f"nami observe → {observe_path(self.root)} "
            f"(MQTT {WS_URL} · observe-only)",
            flush=True,
        )

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        if get_active_observer() is self:
            set_active_observer(None)

    # -- match registration ----------------------------------------------

    def observe_match(self, nami_id: str, meta: dict[str, Any], *, ttl_s: float) -> None:
        nid = str(nami_id or "").strip()
        if not nid or self._stop.is_set():
            return
        deadline = time.monotonic() + max(1.0, float(ttl_s))
        with self._lock:
            cur = self._wanted.get(nid)
            if cur is None:
                self._wanted[nid] = {"meta": dict(meta), "until": deadline}
            else:
                cur["meta"] = dict(meta)
                cur["until"] = max(float(cur["until"]), deadline)

    def release_match(self, nami_id: str) -> None:
        """Let the subscription expire after LINGER_S instead of dropping now."""
        nid = str(nami_id or "").strip()
        if not nid:
            return
        with self._lock:
            cur = self._wanted.get(nid)
            if cur is not None:
                cur["until"] = min(
                    float(cur["until"]), time.monotonic() + LINGER_S
                )

    def stats(self) -> dict[str, Any]:
        with self._lock:
            return {
                "wanted": len(self._wanted),
                "subscribed": len(self._subscribed),
                "rows": self._rows,
            }

    # -- internals --------------------------------------------------------

    def _topics(self, nami_id: str) -> list[str]:
        return [f"live/m1/{nami_id}", f"live/m1/{nami_id}/nft/zh"]

    def _next_packet_id(self) -> int:
        self._packet_id = (self._packet_id % 65000) + 1
        return self._packet_id

    def _run_forever(self) -> None:
        try:
            asyncio.run(self._main())
        except Exception:  # noqa: BLE001
            logger.exception("nami observe loop died")

    async def _main(self) -> None:
        backoff = RECONNECT_MIN_S
        while not self._stop.is_set():
            try:
                await self._session()
                backoff = RECONNECT_MIN_S
            except Exception as e:  # noqa: BLE001
                logger.warning("nami observe session ended: %s", e)
                backoff = min(backoff * 2, RECONNECT_MAX_S)
            for _ in range(int(backoff * 4)):
                if self._stop.is_set():
                    return
                await asyncio.sleep(0.25)

    async def _session(self) -> None:
        try:
            import websockets
        except ImportError:
            logger.warning("nami observe needs the websockets package; staying idle")
            await asyncio.sleep(RECONNECT_MAX_S)
            return

        client_id = f"mqttjs_{random.randint(10**7, 10**8 - 1)}"
        async with websockets.connect(
            WS_URL, subprotocols=["mqtt"], origin=WS_ORIGIN, open_timeout=15
        ) as ws:
            await ws.send(connect_packet(client_id))
            ack = await asyncio.wait_for(ws.recv(), timeout=15)
            if not (isinstance(ack, (bytes, bytearray)) and len(ack) >= 4 and ack[3] == 0):
                raise RuntimeError(f"CONNACK refused: {bytes(ack)[:8].hex()}")
            self._subscribed.clear()
            while not self._stop.is_set():
                await self._sync_subscriptions(ws)
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue
                if not isinstance(msg, (bytes, bytearray)):
                    continue
                got = parse_publish(bytes(msg))
                if got is None:
                    continue
                self._record(*got)

    async def _sync_subscriptions(self, ws: Any) -> None:
        now = time.monotonic()
        with self._lock:
            for nid in [k for k, v in self._wanted.items() if v["until"] <= now]:
                self._wanted.pop(nid, None)
            wanted = set(self._wanted)
        for nid in wanted - self._subscribed:
            for topic in self._topics(nid):
                await ws.send(subscribe_packet(topic, self._next_packet_id()))
            self._subscribed.add(nid)
            logger.info("nami observe subscribed nami_id=%s", nid)
        for nid in self._subscribed - wanted:
            for topic in self._topics(nid):
                await ws.send(unsubscribe_packet(topic, self._next_packet_id()))
            self._subscribed.discard(nid)
            logger.info("nami observe unsubscribed nami_id=%s", nid)

    def _record(self, topic: str, payload: bytes) -> None:
        nid = ""
        parts = topic.split("/")
        if len(parts) >= 3:
            nid = parts[2]
        with self._lock:
            meta = dict((self._wanted.get(nid) or {}).get("meta") or {})
            self._rows += 1
        try:
            fields = harvest(payload)
        except Exception:  # noqa: BLE001
            fields = {}
        row = {
            "observed_at": lib.now_cn_iso(),
            "topic": topic,
            "nami_id": nid,
            "match_id": meta.get("match_id"),
            "event_key": meta.get("event_key"),
            "home": meta.get("home"),
            "away": meta.get("away"),
            "dqd_score": meta.get("dqd_score"),
            "msg_type": fields.get("msg_type"),
            "score_raw": fields.get("score_raw"),
            "ball_xy": fields.get("ball_xy"),
            "payload_len": len(payload),
            # Raw bytes retained so the schema can be reverse-engineered offline
            # without needing another live match.
            "payload_hex": payload[:256].hex(),
        }
        try:
            lib.append_jsonl(observe_path(self.root), [row])
        except Exception:  # noqa: BLE001
            logger.exception("nami observe write failed")


def try_create_observer(root: Path) -> NamiObserver | None:
    if not _env_bool("QUOTE_NAMI_OBSERVE", False):
        return None
    return NamiObserver(root)
