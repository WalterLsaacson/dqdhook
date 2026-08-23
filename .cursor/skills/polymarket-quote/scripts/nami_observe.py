"""Observe-only nami (纳米数据) live feed for matches under pitch-gate.

The animation DQD embeds is served by nami, whose tracker page streams the live
state over MQTT-on-WebSocket. That stream carries the score and the ball's
normalized pitch position directly, i.e. the same facts the OCR gate infers from
a screenshot. This module only records it so the two can be compared offline; it
never feeds the trading path.

Enabled by default (``QUOTE_NAMI_OBSERVE=0`` to disable). MQTT stays in
memory; jsonl is written only when pitch-gate takes a DOM sample, with
that sample's sequence number. Classifies ``ball_xy`` into pitch zones
(center / box / third) for board observation of in_play-then-reversal
cases; still never buys or flattens.
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
# Pitch is x along length (0=left goal, 1=right), y across. FIFA 105×68.
_PENALTY_X = 16.5 / 105.0
_PENALTY_Y0 = (68.0 - 40.32) / 2.0 / 68.0
_PENALTY_Y1 = 1.0 - _PENALTY_Y0
_CENTER_R = 9.15 / 105.0 + 0.015

_SCORE_RE = re.compile(r"^\d{1,3}(?:-\d{1,3}){2,3}$")
_XY_RE = re.compile(r"^-?\d+(?:\.\d+)?,-?\d+(?:\.\d+)?$")


def parse_ball_xy(raw: Any) -> tuple[float, float] | None:
    """``\"0.09,0.53\"`` → ``(x, y)`` in pitch units, else None."""
    text = str(raw or "").strip()
    if not _XY_RE.match(text):
        return None
    xs, ys = text.split(",", 1)
    try:
        x, y = float(xs), float(ys)
    except ValueError:
        return None
    if not (-0.05 <= x <= 1.05 and -0.05 <= y <= 1.05):
        return None
    return x, y


def classify_ball(xy: tuple[float, float] | None) -> dict[str, Any]:
    """Zone labels for a normalized pitch point. Observe-only."""
    if xy is None:
        return {
            "ball_x": None,
            "ball_y": None,
            "zone": "unknown",
            "restart_center": False,
            "in_box": False,
        }
    x, y = xy
    dist = ((x - 0.5) ** 2 + (y - 0.5) ** 2) ** 0.5
    restart = dist <= _CENTER_R
    in_box_l = x <= _PENALTY_X and _PENALTY_Y0 <= y <= _PENALTY_Y1
    in_box_r = x >= 1.0 - _PENALTY_X and _PENALTY_Y0 <= y <= _PENALTY_Y1
    in_box = in_box_l or in_box_r
    if restart:
        zone = "center"
    elif in_box_l:
        zone = "box_l"
    elif in_box_r:
        zone = "box_r"
    elif x < 1.0 / 3.0:
        zone = "third_l"
    elif x > 2.0 / 3.0:
        zone = "third_r"
    else:
        zone = "mid"
    return {
        "ball_x": round(x, 4),
        "ball_y": round(y, 4),
        "zone": zone,
        "restart_center": bool(restart),
        "in_box": bool(in_box),
    }

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
        # nami_id -> {"meta": {...}, "until": monotonic deadline, "t0_mono": ...}
        self._wanted: dict[str, dict[str, Any]] = {}
        self._subscribed: set[str] = set()
        # nami_id -> last classified ball from MQTT (memory only)
        self._latest: dict[str, dict[str, Any]] = {}
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
            f"(MQTT memory · jsonl on DOM sample · observe-only)",
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
        now = time.monotonic()
        with self._lock:
            cur = self._wanted.get(nid)
            if cur is None:
                self._wanted[nid] = {
                    "meta": dict(meta),
                    "until": deadline,
                    "t0_mono": now,
                }
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
                self._latest.pop(nid, None)
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
            self._latest.pop(nid, None)
            logger.info("nami observe unsubscribed nami_id=%s", nid)

    def latest_ball(self, nami_id: str) -> dict[str, Any] | None:
        """In-memory last MQTT ball for this tracker id (not jsonl)."""
        nid = str(nami_id or "").strip()
        if not nid:
            return None
        with self._lock:
            hit = self._latest.get(nid)
            return dict(hit) if hit else None

    def write_dom_sample(
        self,
        nami_id: str,
        *,
        sample_i: int,
        elapsed_s: float,
        play_state: str | None = None,
    ) -> dict[str, Any] | None:
        """Persist one row at a DOM sample. MQTT itself does not write jsonl."""
        nid = str(nami_id or "").strip()
        if not nid:
            return None
        now = time.monotonic()
        with self._lock:
            wanted = self._wanted.get(nid) or {}
            meta = dict(wanted.get("meta") or {})
            latest = dict(self._latest.get(nid) or {})
            self._rows += 1
        ball_ts = latest.get("ball_updated_mono") or latest.get("updated_mono")
        classified = {
            "ball_xy": latest.get("ball_xy"),
            "ball_x": latest.get("ball_x"),
            "ball_y": latest.get("ball_y"),
            "zone": latest.get("zone") or "unknown",
            "restart_center": bool(latest.get("restart_center")),
            "in_box": bool(latest.get("in_box")),
            "score_raw": latest.get("score_raw"),
        }
        row = {
            "observed_at": lib.now_cn_iso(),
            "nami_id": nid,
            "match_id": meta.get("match_id"),
            "event_key": meta.get("event_key"),
            "home": meta.get("home"),
            "away": meta.get("away"),
            "dqd_score": meta.get("dqd_score"),
            "sample_i": sample_i,
            "elapsed_s": round(float(elapsed_s), 3),
            "mqtt_age_s": (
                round(now - float(ball_ts), 3) if ball_ts is not None else None
            ),
            "play_state": play_state,
            **classified,
        }
        try:
            lib.append_jsonl(observe_path(self.root), [row])
        except Exception:  # noqa: BLE001
            logger.exception("nami observe write failed")
        return classified

    def _record(self, topic: str, payload: bytes) -> None:
        """Keep latest ball in memory. jsonl is written only at DOM samples.

        Most MQTT types (nft commentary, 10102, …) have no ``ball_xy``. Those
        must not wipe the last 10101 coordinate, or DOM samples become unknown.
        """
        nid = ""
        parts = topic.split("/")
        if len(parts) >= 3:
            nid = parts[2]
        if not nid:
            return
        try:
            fields = harvest(payload)
        except Exception:  # noqa: BLE001
            fields = {}
        xy_raw = str(fields.get("ball_xy") or "")
        classified = classify_ball(parse_ball_xy(xy_raw))
        now = time.monotonic()
        with self._lock:
            prev = dict(self._latest.get(nid) or {})
            if classified["ball_x"] is None and prev.get("ball_x") is not None:
                classified = {
                    "ball_x": prev.get("ball_x"),
                    "ball_y": prev.get("ball_y"),
                    "zone": prev.get("zone") or "unknown",
                    "restart_center": bool(prev.get("restart_center")),
                    "in_box": bool(prev.get("in_box")),
                }
                xy_keep = prev.get("ball_xy")
                ball_updated = prev.get("ball_updated_mono") or prev.get("updated_mono")
            else:
                xy_keep = xy_raw or None
                ball_updated = (
                    now
                    if classified["ball_x"] is not None
                    else prev.get("ball_updated_mono")
                )
            self._latest[nid] = {
                **classified,
                "ball_xy": xy_keep,
                "score_raw": fields.get("score_raw") or prev.get("score_raw"),
                "msg_type": fields.get("msg_type"),
                "updated_mono": now,
                "ball_updated_mono": ball_updated,
            }


def try_create_observer(root: Path) -> NamiObserver | None:
    if not _env_bool("QUOTE_NAMI_OBSERVE", True):
        return None
    return NamiObserver(root)
