"""Nami football MQTT + HTTP snapshot → the same overlay dict as Chromium DOM.

``QUOTE_GATE_SOURCE=mqtt`` makes pitch-gate read this instead of a tracker tab.
The overlay is synthesized so ``judge_dom()`` keyword tables stay unchanged.
"""

from __future__ import annotations

import logging
import os
import threading
import time
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import urlparse

logger = logging.getLogger("pm_quote.nami_mqtt")

MQTT_HOST = "trackermq.namitiyu.com"
MQTT_PATH = "/mqtt"
MQTT_PORT = 443
MQTT_ORIGIN = "https://tracker.namitiyu.com"
API_ROOT = "https://tracker-api.namitiyu.com"
PUSH_MLIVE = 10101
PUSH_STATS = 10102
STALE_S_DEFAULT = 8.0
HTTP_TIMEOUT_S_DEFAULT = 2.0
HTTP_INTERVAL_S_DEFAULT = 15.0
CONNECT_WAIT_S = 5.0
SECOND_HALF_BASE_S = 45 * 60

# Remainder of vc.code (code % 1000). Thousands digit 1=home / 2=away.
_VC_LABEL: dict[int, str] = {
    111: "危险进攻",
    141: "危险进攻",
    112: "进攻",
    142: "进攻",
    113: "控球",
    143: "控球",
    115: "角球",
    151: "角球",
    152: "角球",
    118: "球门球",
    144: "球门球",
    134: "掷界外球",
    157: "掷界外球",
    138: "任意球",
    153: "任意球",
    154: "任意球",
    155: "任意球",
    156: "任意球",
    114: "进球",
    123: "换人",
    140: "换人",
    121: "射门",
    122: "射门",
    133: "点球不进",
    103: "点球不进",
    145: "VAR",
    146: "VAR",
    147: "VAR",
    148: "VAR",
    149: "暂停",
    116: "黄牌",
    117: "红牌",
    135: "暂停",
    137: "越位",
    124: "比赛开始",
    125: "暂停",
    126: "比赛开始",
    127: "暂停",
}

_SHOT_MARKS = {
    111: ["dangerous-attack-move"],
    141: ["dangerous-attack-move"],
    112: ["attack-move"],
    142: ["attack-move"],
    121: ["ball", "net"],
    122: ["ball"],
}


def _mqtt_rc(rc: Any) -> int:
    """CONNACK/disconnect reason as int (paho v1 int or v2 ReasonCode)."""
    if rc is None:
        return 0
    if isinstance(rc, int):
        return rc
    val = getattr(rc, "value", None)
    if isinstance(val, int):
        return val
    try:
        return int(rc)
    except (TypeError, ValueError):
        text = str(rc).strip().lower()
        return 0 if text in {"0", "success"} else 1


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def nami_id_from_url(url: Any) -> str:
    text = str(url or "").strip()
    if not text:
        return ""
    try:
        qs = urlparse(text)
        from urllib.parse import parse_qs

        return str((parse_qs(qs.query).get("id") or [""])[0] or "").strip()
    except ValueError:
        return ""


def topic_for(nami_id: str) -> str:
    return f"live/m1/{str(nami_id).strip()}"


def read_varint(buf: bytes, i: int) -> tuple[int, int]:
    n = 0
    shift = 0
    while i < len(buf):
        x = buf[i]
        i += 1
        n |= (x & 0x7F) << shift
        if x < 0x80:
            return n, i
        shift += 7
        if shift > 70:
            raise ValueError("varint overflow")
    raise ValueError("eof")


def decode_fields(buf: bytes) -> list[tuple[int, str, object]]:
    i = 0
    out: list[tuple[int, str, object]] = []
    while i < len(buf):
        try:
            key, i = read_varint(buf, i)
        except ValueError:
            break
        fn, wt = key >> 3, key & 7
        if wt == 0:
            try:
                v, i = read_varint(buf, i)
            except ValueError:
                break
            out.append((fn, "varint", v))
        elif wt == 1:
            if i + 8 > len(buf):
                break
            i += 8
        elif wt == 2:
            try:
                ln, i = read_varint(buf, i)
            except ValueError:
                break
            if ln < 0 or i + ln > len(buf):
                break
            data = buf[i : i + ln]
            i += ln
            out.append((fn, "bytes", data))
        elif wt == 5:
            if i + 4 > len(buf):
                break
            i += 4
        else:
            break
    return out


def encode_varint(n: int) -> bytes:
    out = bytearray()
    n = int(n)
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def encode_key(fn: int, wt: int) -> bytes:
    return encode_varint((int(fn) << 3) | int(wt))


def encode_varint_field(fn: int, n: int) -> bytes:
    return encode_key(fn, 0) + encode_varint(n)


def encode_bytes_field(fn: int, data: bytes) -> bytes:
    raw = bytes(data)
    return encode_key(fn, 2) + encode_varint(len(raw)) + raw


def encode_str_field(fn: int, text: str) -> bytes:
    return encode_bytes_field(fn, text.encode("utf-8"))


def encode_push_mlive(*, vc_code: int, ss: str, st: int = 2) -> bytes:
    """Test helper: PushMsg code=10101 wrapping one MLiveItem."""
    vc = encode_varint_field(1, int(vc_code))
    item = (
        encode_str_field(1, str(ss or "0-0-0-0"))
        + encode_bytes_field(2, vc)
        + encode_varint_field(6, int(st))
    )
    mlive = encode_bytes_field(3, item)
    return encode_varint_field(1, PUSH_MLIVE) + encode_bytes_field(2, mlive)


def decode_push(payload: bytes) -> dict[str, Any]:
    code = None
    data = b""
    for fn, typ, v in decode_fields(payload):
        if fn == 1 and typ == "varint":
            code = int(v)
        if fn == 2 and typ == "bytes":
            data = v  # type: ignore[assignment]
    out: dict[str, Any] = {"code": code, "data": data}
    if code == PUSH_MLIVE:
        out["mlive"] = decode_mlive(data)
    return out


def decode_mlive(buf: bytes) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for fn, typ, v in decode_fields(buf):
        if fn == 3 and typ == "bytes":
            items.append(decode_mlive_item(v))  # type: ignore[arg-type]
    return items


def decode_mlive_item(buf: bytes) -> dict[str, Any]:
    ml: dict[str, Any] = {}
    for fn, typ, v in decode_fields(buf):
        if fn == 1 and typ == "bytes":
            ml["ss"] = v.decode("utf-8", "replace")  # type: ignore[union-attr]
        elif fn == 2 and typ == "bytes":
            vc: dict[str, Any] = {}
            for f2, t2, v2 in decode_fields(v):  # type: ignore[arg-type]
                if f2 == 1:
                    vc["code"] = int(v2)
                elif f2 == 2 and t2 == "bytes":
                    vc["extra"] = v2.decode("utf-8", "replace")  # type: ignore[union-attr]
                elif f2 == 3 and t2 == "bytes":
                    vc["position"] = v2.decode("utf-8", "replace")  # type: ignore[union-attr]
                elif f2 == 4:
                    vc["playerId"] = v2
            ml["vc"] = vc
        elif fn == 6:
            ml["st"] = int(v)
    return ml


def unwrap_api(raw: bytes) -> tuple[int, bytes]:
    code = 0
    data = b""
    for fn, typ, v in decode_fields(raw):
        if fn == 1 and typ == "varint":
            code = int(v)
        if fn == 2 and typ == "bytes":
            data = v  # type: ignore[assignment]
    return code, data


def decode_variable_detail(raw: bytes) -> dict[str, Any]:
    _code, data = unwrap_api(raw)
    obj: dict[str, Any] = {}
    for fn, typ, v in decode_fields(data):
        if fn == 1:
            obj["id"] = int(v)
        elif fn == 2:
            obj["statusId"] = int(v)
        elif fn == 3 and typ == "bytes":
            obj["homeScores"] = _score_map(v)  # type: ignore[arg-type]
        elif fn == 4 and typ == "bytes":
            obj["awayScores"] = _score_map(v)  # type: ignore[arg-type]
        elif fn == 5 and typ == "bytes":
            obj["timer"] = _timer_map(v)  # type: ignore[arg-type]
        elif fn == 8 and typ == "bytes":
            obj["mLiveItem"] = decode_mlive_item(v)  # type: ignore[arg-type]
    return obj


def _score_map(buf: bytes) -> dict[str, int]:
    names = {1: "score", 2: "halfScore", 3: "redCard", 4: "yellowCard", 5: "corner"}
    out: dict[str, int] = {}
    for fn, typ, v in decode_fields(buf):
        if fn in names:
            out[names[fn]] = int(v)
    return out


def _timer_map(buf: bytes) -> dict[str, int]:
    names = {1: "ticking", 2: "countdown", 3: "uptime", 4: "second", 5: "addTime"}
    out: dict[str, int] = {}
    for fn, typ, v in decode_fields(buf):
        if fn in names:
            out[names[fn]] = int(v)
    return out


def parse_ss(ss: str) -> tuple[int, int]:
    parts = str(ss or "").replace(":", "-").split("-")
    nums: list[int] = []
    for p in parts:
        p = p.strip()
        if p.isdigit() or (p.startswith("-") and p[1:].isdigit()):
            nums.append(int(p))
        else:
            nums.append(0)
    while len(nums) < 2:
        nums.append(0)
    return nums[0], nums[1]


def vc_kind(code: int | None) -> int:
    if code is None:
        return 0
    return int(code) % 1000


def vc_side(code: int | None) -> str:
    if not code:
        return ""
    thou = (int(code) // 1000) % 10
    if thou == 1:
        return "home"
    if thou == 2:
        return "away"
    return ""


def overlay_pop(
    vc_code: int | None,
    *,
    home: str = "",
    away: str = "",
) -> str:
    kind = vc_kind(vc_code)
    label = _VC_LABEL.get(kind, "")
    if not label:
        return ""
    side = vc_side(vc_code)
    team = home if side == "home" else away if side == "away" else ""
    if label == "VAR":
        return f"{team} VAR".strip()
    if team:
        return f"{team} {label}"
    return label


def overlay_marks(vc_code: int | None) -> list[str]:
    return list(_SHOT_MARKS.get(vc_kind(vc_code), []))


def format_clock(match_s: int, home: int, away: int) -> str:
    sec = max(0, int(match_s))
    mm, ss = divmod(sec, 60)
    return f"{mm:02d}:{ss:02d} {int(home)} : {int(away)}"


def fetch_variable_detail(nami_id: str, *, timeout_s: float | None = None) -> dict[str, Any]:
    timeout_s = float(
        timeout_s
        if timeout_s is not None
        else _env_float("QUOTE_MQTT_HTTP_TIMEOUT_S", HTTP_TIMEOUT_S_DEFAULT)
    )
    url = f"{API_ROOT}/api/football/variable_detail?id={nami_id}"
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0",
            "Origin": "https://tracker.namitiyu.com",
            "Referer": "https://tracker.namitiyu.com/",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        raw = resp.read()
    return decode_variable_detail(raw)


@dataclass
class _MatchSlot:
    nami_id: str
    vc_code: int | None = None
    ss: str = "0-0-0-0"
    st: int | None = None
    status_id: int | None = None
    home_score: int = 0
    away_score: int = 0
    ticking: bool = False
    uptime: int | None = None
    clock_base_sec: int = 0
    clock_base_mono: float = 0.0
    last_mlive_mono: float = 0.0
    last_clock_sec: int = 0
    last_clock_text: str = ""
    last_http_mono: float = 0.0
    subscribed: bool = False


class NamiMqttHub:
    """Process-wide MQTT client + per-match overlay cache."""

    def __init__(
        self,
        *,
        fetch_fn: Callable[[str], dict[str, Any]] | None = None,
        time_fn: Callable[[], float] | None = None,
        wall_fn: Callable[[], float] | None = None,
        stale_s: float | None = None,
        offline: bool = False,
    ) -> None:
        timeout_s = _env_float("QUOTE_MQTT_HTTP_TIMEOUT_S", HTTP_TIMEOUT_S_DEFAULT)
        self._http_timeout_s = float(timeout_s)
        self._http_interval_s = _env_float("QUOTE_MQTT_HTTP_INTERVAL_S", HTTP_INTERVAL_S_DEFAULT)
        self._fetch_fn = fetch_fn or (
            lambda nid: fetch_variable_detail(nid, timeout_s=self._http_timeout_s)
        )
        self._mono = time_fn or time.monotonic
        self._wall = wall_fn or time.time
        self._offline = bool(offline)
        self._stale_s = float(
            stale_s if stale_s is not None else _env_float("QUOTE_MQTT_STALE_S", STALE_S_DEFAULT)
        )
        self._lock = threading.Lock()
        self._slots: dict[str, _MatchSlot] = {}
        self._client: Any = None
        self._connected = False
        self._started = False
        self._start_err: str | None = None
        self._connect_in_progress = False
        self._life = threading.Lock()

    @property
    def connected(self) -> bool:
        return bool(self._connected)

    @property
    def start_error(self) -> str | None:
        return self._start_err

    def start(self) -> tuple[bool, str | None]:
        with self._life:
            if self._offline:
                self._connected = True
                self._started = True
                self._start_err = None
                return True, None
            if self._connected:
                self._started = True
                self._start_err = None
                return True, None
            existing = self._client is not None or self._connect_in_progress
            if not existing:
                self._connect_in_progress = True
        if existing:
            return self._wait_connected(CONNECT_WAIT_S)
        try:
            ok, err = self._connect_mqtt()
        finally:
            with self._life:
                self._connect_in_progress = False
        if not ok:
            self._abandon_client()
            with self._life:
                self._started = False
                self._connected = False
                prev = self._start_err
                self._start_err = err or "mqtt_connect_failed"
            if prev != self._start_err:
                print(f"nami-mqtt → connect failed: {self._start_err}", flush=True)
            return False, self._start_err
        with self._life:
            self._started = True
            self._connected = True
            self._start_err = None
        print("nami-mqtt → connected wss://trackermq.namitiyu.com/mqtt", flush=True)
        return True, None

    def _wait_connected(self, timeout_s: float) -> tuple[bool, str | None]:
        deadline = self._mono() + max(0.2, float(timeout_s))
        while self._mono() < deadline:
            if self._connected:
                with self._life:
                    self._started = True
                    self._start_err = None
                return True, None
            if self._client is None and not self._connect_in_progress:
                return False, self._start_err or "mqtt_unavailable"
            time.sleep(0.05)
        return False, self._start_err or "mqtt_reconnect_timeout"

    def _abandon_client(self) -> None:
        client = self._client
        self._client = None
        self._connected = False
        if client is None:
            return
        try:
            client.loop_stop()
        except Exception:  # noqa: BLE001
            pass
        try:
            client.disconnect()
        except Exception:  # noqa: BLE001
            pass

    def _connect_mqtt(self) -> tuple[bool, str | None]:
        try:
            import paho.mqtt.client as mqtt
        except Exception:  # noqa: BLE001
            return False, "paho_mqtt_not_installed"
        try:
            kwargs: dict[str, Any] = {"transport": "websockets"}
            ver = getattr(mqtt, "CallbackAPIVersion", None)
            if ver is not None:
                client = mqtt.Client(
                    callback_api_version=ver.VERSION2,
                    client_id=f"dqdhook-nami-{os.getpid()}",
                    **kwargs,
                )
            else:
                client = mqtt.Client(client_id=f"dqdhook-nami-{os.getpid()}", **kwargs)
            if hasattr(client, "connect_timeout"):
                try:
                    client.connect_timeout = CONNECT_WAIT_S
                except Exception:  # noqa: BLE001
                    pass
            tls = getattr(client, "tls_set", None)
            if callable(tls):
                tls()
            ws_opts = getattr(client, "ws_set_options", None)
            if callable(ws_opts):
                ws_opts(
                    path=MQTT_PATH,
                    headers={
                        "Origin": MQTT_ORIGIN,
                        "User-Agent": "Mozilla/5.0",
                    },
                )
            client.on_connect = self._on_connect
            client.on_message = self._on_message
            client.on_disconnect = self._on_disconnect
            client.connect(MQTT_HOST, MQTT_PORT, 60)
            client.loop_start()
            self._client = client
            ok, _err = self._wait_connected(CONNECT_WAIT_S)
            if ok:
                return True, None
            return False, "mqtt_connect_timeout"
        except Exception as e:  # noqa: BLE001
            return False, str(e).splitlines()[0][:200] or "mqtt_connect_failed"

    def _on_connect(self, client: Any, _u: Any, _f: Any, rc: Any, _p: Any = None) -> None:
        if self._client is not None and client is not self._client:
            return
        code = _mqtt_rc(rc)
        self._connected = code == 0
        if not self._connected:
            self._start_err = f"mqtt_connack_{code}"
            return
        self._start_err = None
        with self._lock:
            ids = [s.nami_id for s in self._slots.values() if s.subscribed]
        for nid in ids:
            try:
                client.subscribe(topic_for(nid))
            except Exception:  # noqa: BLE001
                logger.debug("nami mqtt resubscribe failed nami=%s", nid, exc_info=True)

    def _on_disconnect(self, client: Any, _u: Any, *args: Any) -> None:
        if self._client is not None and client is not self._client:
            return
        self._connected = False

    def _on_message(self, client: Any, _u: Any, msg: Any) -> None:
        if self._client is not None and client is not self._client:
            return
        topic = str(getattr(msg, "topic", "") or "")
        payload = bytes(getattr(msg, "payload", b"") or b"")
        nami_id = topic.rsplit("/", 1)[-1].strip() if topic.startswith("live/m1/") else ""
        if not nami_id:
            return
        try:
            decoded = decode_push(payload)
        except Exception:  # noqa: BLE001
            logger.debug("nami mqtt decode failed nami=%s", nami_id, exc_info=True)
            return
        if decoded.get("code") != PUSH_MLIVE:
            return
        items = decoded.get("mlive") or []
        if not items:
            return
        self._apply_mlive(nami_id, items[-1])

    def _apply_mlive(self, nami_id: str, item: dict[str, Any]) -> None:
        vc = item.get("vc") if isinstance(item.get("vc"), dict) else None
        with self._lock:
            slot = self._slots.setdefault(str(nami_id), _MatchSlot(nami_id=str(nami_id)))
            if vc is not None:
                raw = int(vc.get("code") or 0)
                slot.vc_code = raw if raw else None
            if item.get("ss"):
                slot.ss = str(item.get("ss") or slot.ss)
                h, a = parse_ss(slot.ss)
                slot.home_score, slot.away_score = h, a
            if item.get("st") is not None:
                slot.st = int(item["st"])
            slot.last_mlive_mono = self._mono()

    def inject_mlive(
        self,
        nami_id: str,
        *,
        vc_code: int,
        ss: str = "0-0-0-0",
        st: int = 2,
    ) -> None:
        """Test helper — apply a 10101 payload without the broker."""
        raw = encode_push_mlive(vc_code=vc_code, ss=ss, st=st)
        decoded = decode_push(raw)
        item = (decoded.get("mlive") or [{}])[-1]
        self._apply_mlive(str(nami_id), item if isinstance(item, dict) else {})
        with self._lock:
            slot = self._slots.setdefault(str(nami_id), _MatchSlot(nami_id=str(nami_id)))
            slot.subscribed = True

    def inject_variable(
        self,
        nami_id: str,
        *,
        status_id: int = 2,
        ticking: bool = True,
        home: int = 0,
        away: int = 0,
        uptime: float | None = None,
        vc_code: int | None = None,
        ss: str | None = None,
        second: int | None = None,
    ) -> None:
        with self._lock:
            slot = self._slots.setdefault(str(nami_id), _MatchSlot(nami_id=str(nami_id)))
            slot.status_id = int(status_id)
            slot.ticking = bool(ticking)
            slot.home_score = int(home)
            slot.away_score = int(away)
            if uptime is not None:
                slot.uptime = int(uptime)
            if vc_code is not None:
                slot.vc_code = int(vc_code) or None
            if ss is not None:
                slot.ss = ss
                h, a = parse_ss(ss)
                slot.home_score, slot.away_score = h, a
            if second is not None:
                slot.clock_base_sec = int(second)
                slot.clock_base_mono = self._mono()
                slot.last_clock_sec = int(second)

    def subscribe(self, nami_id: str) -> tuple[bool, str | None]:
        nid = str(nami_id or "").strip()
        if not nid:
            return False, "no_nami_id"
        if not self._offline and not self._connected:
            return False, self._start_err or "mqtt_unavailable"
        with self._lock:
            slot = self._slots.setdefault(nid, _MatchSlot(nami_id=nid))
            already = bool(slot.subscribed)
            slot.subscribed = True
        client = self._client
        if client is not None:
            try:
                client.subscribe(topic_for(nid))
            except Exception as e:  # noqa: BLE001
                return False, str(e).splitlines()[0][:160]
        if not already:
            print(f"nami-mqtt → SUB nami_id={nid}", flush=True)
        try:
            self.refresh_variable(nid)
        except Exception:  # noqa: BLE001
            logger.debug("nami variable_detail failed nami=%s", nid, exc_info=True)
        return True, None

    def unsubscribe(self, nami_id: str) -> None:
        nid = str(nami_id or "").strip()
        if not nid:
            return
        with self._lock:
            slot = self._slots.get(nid)
            if slot is not None:
                slot.subscribed = False
        client = self._client
        if client is not None:
            try:
                client.unsubscribe(topic_for(nid))
            except Exception:  # noqa: BLE001
                pass
        print(f"nami-mqtt → UNSUB nami_id={nid}", flush=True)

    def sync(self, want: set[str]) -> dict[str, int]:
        want_ids = {str(x).strip() for x in want if str(x).strip()}
        with self._lock:
            have = {nid for nid, s in self._slots.items() if s.subscribed}
        added = 0
        closed = 0
        for nid in sorted(want_ids - have):
            ok, _err = self.subscribe(nid)
            if ok:
                added += 1
        for nid in sorted(have - want_ids):
            self.unsubscribe(nid)
            closed += 1
        self.refresh_subscribed()
        return {"sub": added, "unsub": closed, "kept": len(want_ids & have)}

    def refresh_subscribed(self) -> None:
        """HTTP status/clock correction off the gate tick (warm loop)."""
        now = self._mono()
        with self._lock:
            ids = [
                nid
                for nid, slot in self._slots.items()
                if slot.subscribed
                and (
                    slot.last_http_mono <= 0
                    or (now - slot.last_http_mono) >= self._http_interval_s
                )
            ]
        for nid in ids:
            try:
                self.refresh_variable(nid)
            except Exception:  # noqa: BLE001
                logger.debug("nami variable_detail failed nami=%s", nid, exc_info=True)

    def refresh_variable(self, nami_id: str) -> None:
        nid = str(nami_id or "").strip()
        if not nid:
            return
        data = self._fetch_fn(nid)
        if not isinstance(data, dict) or not data:
            return
        home_s = data.get("homeScores") if isinstance(data.get("homeScores"), dict) else {}
        away_s = data.get("awayScores") if isinstance(data.get("awayScores"), dict) else {}
        timer = data.get("timer") if isinstance(data.get("timer"), dict) else {}
        mlive = data.get("mLiveItem") if isinstance(data.get("mLiveItem"), dict) else {}
        with self._lock:
            slot = self._slots.setdefault(nid, _MatchSlot(nami_id=nid))
            if data.get("statusId") is not None:
                slot.status_id = int(data["statusId"])
            if "score" in home_s:
                slot.home_score = int(home_s.get("score") or 0)
            if "score" in away_s:
                slot.away_score = int(away_s.get("score") or 0)
            slot.ticking = bool(int(timer.get("ticking") or 0))
            if timer.get("uptime"):
                slot.uptime = int(timer["uptime"])
            if timer.get("second") is not None:
                slot.clock_base_sec = int(timer["second"])
                slot.clock_base_mono = self._mono()
                slot.last_clock_sec = int(timer["second"])
            slot.last_http_mono = self._mono()
            vc = mlive.get("vc") if isinstance(mlive.get("vc"), dict) else {}
            if vc.get("code") and slot.vc_code is None:
                slot.vc_code = int(vc["code"])
            if mlive.get("ss"):
                slot.ss = str(mlive["ss"])
            if mlive.get("st") is not None:
                slot.st = int(mlive["st"])

    def snapshot(
        self,
        nami_id: str,
        *,
        home: str = "",
        away: str = "",
        refresh_http: bool = False,
    ) -> tuple[dict[str, Any] | None, str | None]:
        nid = str(nami_id or "").strip()
        if not nid:
            return None, "no_nami_id"
        if refresh_http:
            try:
                self.refresh_variable(nid)
            except Exception as e:  # noqa: BLE001
                logger.debug("nami snapshot http failed nami=%s: %s", nid, e)
        with self._lock:
            slot = self._slots.get(nid)
            if slot is None:
                return None, "not_open"
            now = self._mono()
            stale = False
            playing = slot.status_id in {2, 4} or slot.st in {2, 4} or slot.ticking
            if playing and slot.last_mlive_mono > 0:
                stale = (now - slot.last_mlive_mono) > self._stale_s
            elif playing and slot.last_mlive_mono <= 0:
                stale = False
            match_s = self._match_seconds(slot, now=now, stale=stale)
            if not stale:
                slot.last_clock_sec = match_s
            h, a = slot.home_score, slot.away_score
            if slot.ss:
                sh, sa = parse_ss(slot.ss)
                if sh or sa or not (h or a):
                    h, a = sh, sa
            clock = format_clock(match_s, h, a)
            if stale:
                clock = slot.last_clock_text or clock
            else:
                slot.last_clock_text = clock
            pop = overlay_pop(slot.vc_code, home=home, away=away)
            marks = overlay_marks(slot.vc_code)
            vc_code = slot.vc_code
        return (
            {
                "pop_box": pop,
                "center_box": clock,
                "marks": marks,
                "vc_code": vc_code,
                "nami_id": nid,
            },
            None,
        )

    def _match_seconds(self, slot: _MatchSlot, *, now: float, stale: bool) -> int:
        if stale:
            return int(slot.last_clock_sec)
        if slot.ticking and slot.clock_base_mono > 0:
            return int(slot.clock_base_sec) + max(0, int(now - slot.clock_base_mono))
        if slot.ticking and slot.uptime and slot.uptime > 1_000_000_000:
            match_s = max(0, int(self._wall() - slot.uptime))
            if (slot.status_id == 4 or slot.st == 4) and match_s < SECOND_HALF_BASE_S:
                match_s += SECOND_HALF_BASE_S
            return match_s
        return int(slot.last_clock_sec)

    def shutdown(self) -> None:
        with self._life:
            self._started = False
            self._start_err = None
            self._connect_in_progress = False
        self._abandon_client()


class MqttReader:
    """DomReader-shaped handle over :class:`NamiMqttHub`."""

    source = "mqtt"

    def __init__(
        self,
        nami_id: str,
        *,
        match_id: str = "",
        home: str = "",
        away: str = "",
        hub: NamiMqttHub | None = None,
    ) -> None:
        self.nami_id = str(nami_id or "").strip()
        self.match_id = str(match_id or "")
        self.home = str(home or "")
        self.away = str(away or "")
        self.hub = hub if hub is not None else get_hub()
        self.reused = False
        self._open = False

    def open(self) -> tuple[bool, str | None]:
        if not self.nami_id:
            return False, "no_nami_id"
        ok, err = self.hub.start()
        if not ok:
            return False, err or "mqtt_unavailable"
        with self.hub._lock:
            existed = self.nami_id in self.hub._slots and (
                self.hub._slots[self.nami_id].vc_code is not None
                or self.hub._slots[self.nami_id].subscribed
            )
        self.reused = bool(existed)
        sub_ok, sub_err = self.hub.subscribe(self.nami_id)
        if not sub_ok:
            return False, sub_err or "mqtt_subscribe_failed"
        self._open = True
        return True, None

    def read(self) -> tuple[dict[str, Any] | None, str | None]:
        if not self._open:
            return None, "not_open"
        return self.hub.snapshot(self.nami_id, home=self.home, away=self.away, refresh_http=False)

    def close(self) -> None:
        self._open = False


_hub: NamiMqttHub | None = None
_hub_lock = threading.Lock()


def get_hub() -> NamiMqttHub:
    global _hub
    with _hub_lock:
        if _hub is None:
            _hub = NamiMqttHub()
        return _hub


def reset_hub_for_tests(hub: NamiMqttHub | None = None) -> NamiMqttHub:
    global _hub
    with _hub_lock:
        old = _hub
        if old is not None:
            try:
                old.shutdown()
            except Exception:  # noqa: BLE001
                pass
        _hub = hub if hub is not None else NamiMqttHub(offline=True, fetch_fn=lambda _n: {})
        return _hub
