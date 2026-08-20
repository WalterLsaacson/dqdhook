"""Observe-only Live Score API snapshots (match events + commentary).

Trial-oriented: does **not** rely on ``KICK_OFF``. Pulls:
  - ``scores/live.json`` → resolve DQD match → LSA match_id (cached)
  - ``matches/events.json`` → GOAL / score raw
  - ``commentary/events.json`` → VAR_* if package allows (else keep HTTP error raw)

Phase: ``dqd_reversal`` only (AF confirm / post-confirm timers removed).

Does **not** gate buys or flatten. Enabled only when
``LIVESCORE_API_KEY`` + ``LIVESCORE_API_SECRET`` are set.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Callable, Dict

import quote_lib as lib

logger = logging.getLogger("pm_quote.livescore_observe")

LSA_BASE = "https://livescore-api.com/api-client"
# Docs/live rows: live scores under /matches/live.json; events under /scores/events.json.
LIVESCORES_PATH = "/matches/live.json"
EVENTS_PATH = "/scores/events.json"
# Fallback if an older docs path is needed.
EVENTS_ALT_PATH = "/matches/events.json"
COMMENTARY_PATH = "/commentary/events.json"

ENV_KEY = "LIVESCORE_API_KEY"
ENV_SECRET = "LIVESCORE_API_SECRET"

DEFAULT_WORKERS = 4
DEFAULT_HTTP_TIMEOUT_S = 12.0
DEFAULT_MIN_SIDE_SIM = 0.72

PHASE_DQD_REVERSAL = "dqd_reversal"

_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)
_WS_RE = re.compile(r"\s+")

FetchLiveFn = Callable[[], Dict[str, Any]]
FetchEventsFn = Callable[[str], Dict[str, Any]]
FetchCommentaryFn = Callable[[str], Dict[str, Any]]

_active: "LiveScoreObserver | None" = None
_active_lock = threading.Lock()


def set_active_observer(observer: "LiveScoreObserver | None") -> None:
    global _active
    with _active_lock:
        _active = observer


def get_active_observer() -> "LiveScoreObserver | None":
    with _active_lock:
        return _active


def observe_path(root: Path) -> Path:
    return lib.data_dir(root) / "livescore_observe.jsonl"


def match_map_path(root: Path) -> Path:
    return lib.data_dir(root) / "livescore_match_map.json"


def make_observe_group_id(match_id: str, home: Any, away: Any, event_key: str) -> str:
    return f"{match_id}|{home}-{away}|{event_key}"


def load_credentials(*, env: dict[str, str] | None = None) -> tuple[str, str] | None:
    src = env if env is not None else os.environ
    key = str(src.get(ENV_KEY) or "").strip()
    secret = str(src.get(ENV_SECRET) or "").strip()
    if key and secret:
        return key, secret
    return None


def try_create_observer(
    root: Path,
    **kwargs: Any,
) -> "LiveScoreObserver | None":
    creds = load_credentials()
    if creds is None:
        return None
    return LiveScoreObserver(
        root,
        api_key=creds[0],
        api_secret=creds[1],
        **kwargs,
    )


def normalize_team(name: str) -> str:
    s = unicodedata.normalize("NFKD", str(name or ""))
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = s.lower().strip()
    s = _PUNCT_RE.sub(" ", s)
    s = _WS_RE.sub(" ", s).strip()
    return s


def team_similarity(a: str, b: str) -> float:
    na, nb = normalize_team(a), normalize_team(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    if na in nb or nb in na:
        return 0.92
    ta, tb = set(na.split()), set(nb.split())
    jacc = len(ta & tb) / len(ta | tb) if ta and tb else 0.0
    seq = SequenceMatcher(None, na, nb).ratio()
    return max(jacc, seq)


def _team_name_from_side(side: Any) -> str:
    if isinstance(side, dict):
        return str(side.get("name") or side.get("team") or "")
    return str(side or "")


def extract_live_matches(payload: Any) -> list[dict[str, Any]]:
    """Flatten LSA livescores payload into a list of match dicts."""
    if not isinstance(payload, dict):
        return []
    data = payload.get("data")
    if isinstance(data, list):
        return [m for m in data if isinstance(m, dict)]
    if not isinstance(data, dict):
        return []
    for key in ("match", "matches", "fixtures", "scores"):
        raw = data.get(key)
        if isinstance(raw, list):
            return [m for m in raw if isinstance(m, dict)]
        if isinstance(raw, dict):
            # Single match object
            if raw.get("id") is not None or raw.get("home") is not None:
                return [raw]
    # Some responses nest under data["data"]
    nested = data.get("data")
    if isinstance(nested, list):
        return [m for m in nested if isinstance(m, dict)]
    return []


def resolve_lsa_match(
    live_payload: Any,
    *,
    home: str,
    away: str,
    min_side: float = DEFAULT_MIN_SIDE_SIM,
) -> dict[str, Any]:
    """Pick best LSA live match for home/away names.

    Returns ``{ok, lsa_match_id, live_row, home_sim, away_sim, candidates}``
    or ``{ok: False, error, ...}``.
    """
    matches = extract_live_matches(live_payload)
    if not matches:
        return {
            "ok": False,
            "error": "no_live_matches",
            "lsa_match_id": None,
            "live_row": None,
            "candidates": 0,
        }
    best: dict[str, Any] | None = None
    best_score = -1.0
    for row in matches:
        h = _team_name_from_side(row.get("home"))
        a = _team_name_from_side(row.get("away"))
        hs = team_similarity(home, h)
        aws = team_similarity(away, a)
        # Also try swapped sides (some feeds flip)
        hs_sw = team_similarity(home, a)
        aws_sw = team_similarity(away, h)
        if min(hs_sw, aws_sw) > min(hs, aws):
            hs, aws = hs_sw, aws_sw
            swapped = True
        else:
            swapped = False
        if hs < min_side or aws < min_side:
            continue
        score = (hs + aws) / 2.0
        if score > best_score:
            best_score = score
            mid = row.get("id") or row.get("match_id")
            best = {
                "ok": True,
                "lsa_match_id": str(mid) if mid is not None else None,
                "live_row": row,
                "home_sim": round(hs, 4),
                "away_sim": round(aws, 4),
                "swapped_sides": swapped,
                "candidates": len(matches),
            }
    if best is None or not best.get("lsa_match_id"):
        return {
            "ok": False,
            "error": "no_team_match",
            "lsa_match_id": None,
            "live_row": None,
            "candidates": len(matches),
        }
    return best


@dataclass
class _GroupState:
    observe_group_id: str
    match_id: str
    event_key: str
    home: str
    away: str
    dqd_score: dict[str, Any]
    gen: int = 0
    timers: list[threading.Timer] = field(default_factory=list)


class LiveScoreObserver:
    """Background LSA snapshots for DQD reversals (observe-only)."""

    def __init__(
        self,
        root: Path,
        *,
        api_key: str,
        api_secret: str,
        workers: int = DEFAULT_WORKERS,
        http_timeout_s: float = DEFAULT_HTTP_TIMEOUT_S,
        min_side_sim: float = DEFAULT_MIN_SIDE_SIM,
        fetch_live: FetchLiveFn | None = None,
        fetch_events: FetchEventsFn | None = None,
        fetch_commentary: FetchCommentaryFn | None = None,
    ) -> None:
        self.root = Path(root)
        self.api_key = str(api_key or "").strip()
        self.api_secret = str(api_secret or "").strip()
        self.http_timeout_s = max(0.5, float(http_timeout_s))
        self.min_side_sim = float(min_side_sim)
        self._fetch_live = fetch_live
        self._fetch_events = fetch_events
        self._fetch_commentary = fetch_commentary
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._by_match: dict[str, _GroupState] = {}
        self._id_map: dict[str, str] = {}
        self._live_cache: dict[str, dict[str, Any]] = {}
        self._pool = ThreadPoolExecutor(
            max_workers=max(1, int(workers)),
            thread_name_prefix="lsa-obs",
        )
        self._load_match_map()

    def start(self) -> None:
        self._stop.clear()
        set_active_observer(self)
        logger.info(
            "livescore observe on → %s",
            observe_path(self.root),
        )

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            for st in self._by_match.values():
                for t in st.timers:
                    t.cancel()
                st.timers.clear()
        try:
            self._pool.shutdown(wait=False, cancel_futures=True)
        except TypeError:
            self._pool.shutdown(wait=False)
        if get_active_observer() is self:
            set_active_observer(None)

    def on_dqd_reversal(
        self,
        root: Path | None = None,
        *,
        match_id: str,
        event_key: str = "",
        ev: dict[str, Any] | None = None,
    ) -> str | None:
        if self._stop.is_set():
            return None
        mid = str(match_id or "").strip()
        if not mid:
            return None
        ev = ev if isinstance(ev, dict) else {}
        with self._lock:
            linked = self._by_match.get(mid)
            if linked is not None:
                group_id = linked.observe_group_id
                home_name = linked.home or str(ev.get("home") or "")
                away_name = linked.away or str(ev.get("away") or "")
                key = linked.event_key or str(event_key or "")
                unlinked = False
                gen = linked.gen
            else:
                home = ev.get("home_score")
                away = ev.get("away_score")
                key = str(event_key or "")
                group_id = make_observe_group_id(mid, home, away, key or "reversal")
                home_name = str(ev.get("home") or "")
                away_name = str(ev.get("away") or "")
                unlinked = True
                gen = 0
        prev = ev.get("prev") if isinstance(ev.get("prev"), dict) else None
        dqd_score = {
            "home": ev.get(
                "home_score",
                (ev.get("curr") or {}).get("home")
                if isinstance(ev.get("curr"), dict)
                else None,
            ),
            "away": ev.get(
                "away_score",
                (ev.get("curr") or {}).get("away")
                if isinstance(ev.get("curr"), dict)
                else None,
            ),
        }
        dqd_prev = None
        if prev is not None:
            dqd_prev = {"home": prev.get("home"), "away": prev.get("away")}
        self._pool.submit(
            self._safe_snapshot,
            PHASE_DQD_REVERSAL,
            group_id,
            mid,
            key,
            home_name,
            away_name,
            dqd_score,
            dqd_prev,
            unlinked,
            gen,
        )
        return group_id

    def _load_match_map(self) -> None:
        path = match_map_path(self.root)
        try:
            raw = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        except (OSError, json.JSONDecodeError):
            raw = {}
        if isinstance(raw, dict):
            with self._lock:
                for k, v in raw.items():
                    if k and v is not None:
                        self._id_map[str(k)] = str(v)

    def _persist_match_map(self) -> None:
        path = match_map_path(self.root)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with self._lock:
                payload = dict(self._id_map)
            path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        except OSError as e:
            logger.warning("livescore match map write failed: %s", e)

    def _lsa_get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        q = {
            "key": self.api_key,
            "secret": self.api_secret,
            **{k: v for k, v in params.items() if v is not None},
        }
        url = f"{LSA_BASE}{path}?{urllib.parse.urlencode(q)}"
        req = urllib.request.Request(
            url,
            headers={"Accept": "application/json", "User-Agent": "dongqiudihook-lsa-observe/1"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.http_timeout_s) as resp:
                body_bytes = resp.read()
                status = int(getattr(resp, "status", 200) or 200)
                try:
                    raw = json.loads(body_bytes.decode("utf-8"))
                except json.JSONDecodeError:
                    return {
                        "http_status": status,
                        "raw": {"_non_json": body_bytes.decode("utf-8", errors="replace")[:8000]},
                        "error": "non_json_body",
                    }
                return {"http_status": status, "raw": raw}
        except urllib.error.HTTPError as e:
            body: Any
            try:
                body = json.loads(e.read().decode("utf-8"))
            except Exception:  # noqa: BLE001
                body = {"_http_error": str(e)}
            return {"http_status": int(e.code), "raw": body, "error": f"http_{e.code}"}
        except Exception as e:  # noqa: BLE001
            return {"http_status": None, "raw": None, "error": str(e)}

    def _default_fetch_live(self) -> dict[str, Any]:
        return self._lsa_get(LIVESCORES_PATH, {})

    def _default_fetch_events(self, lsa_match_id: str) -> dict[str, Any]:
        primary = self._lsa_get(EVENTS_PATH, {"id": lsa_match_id})
        if primary.get("error") in ("http_401", "http_403") or (
            isinstance(primary.get("raw"), dict)
            and primary["raw"].get("success") is False
        ):
            alt = self._lsa_get(EVENTS_ALT_PATH, {"id": lsa_match_id})
            # Prefer alt when it succeeds; otherwise keep primary (full raw).
            if not alt.get("error") and isinstance(alt.get("raw"), dict):
                if alt["raw"].get("success") is not False:
                    return alt
            # Keep both attempts under primary for debugging.
            primary = {
                **primary,
                "alt_attempt": alt,
            }
        return primary

    def _default_fetch_commentary(self, lsa_match_id: str) -> dict[str, Any]:
        return self._lsa_get(COMMENTARY_PATH, {"match_id": lsa_match_id})

    def _resolve_for_match(
        self, match_id: str, home: str, away: str
    ) -> dict[str, Any]:
        with self._lock:
            cached_id = self._id_map.get(match_id)
            cached_live = self._live_cache.get(match_id)
        if cached_id:
            return {
                "ok": True,
                "lsa_match_id": cached_id,
                "live_row": cached_live,
                "from_cache": True,
            }

        live_resp = (self._fetch_live or self._default_fetch_live)()
        if live_resp.get("error") and live_resp.get("raw") is None:
            return {
                "ok": False,
                "error": live_resp.get("error") or "live_fetch_failed",
                "lsa_live_fetch": live_resp,
                "lsa_match_id": None,
                "live_row": None,
            }
        resolved = resolve_lsa_match(
            live_resp.get("raw"),
            home=home,
            away=away,
            min_side=self.min_side_sim,
        )
        if not resolved.get("ok"):
            return {
                **resolved,
                "lsa_live_fetch": {
                    "http_status": live_resp.get("http_status"),
                    # Keep full raw for coverage debugging (trial volume is low).
                    "raw": live_resp.get("raw"),
                    "error": live_resp.get("error"),
                },
            }
        lsa_id = str(resolved["lsa_match_id"])
        live_row = resolved.get("live_row")
        with self._lock:
            self._id_map[match_id] = lsa_id
            if isinstance(live_row, dict):
                self._live_cache[match_id] = live_row
        self._persist_match_map()
        return {
            **resolved,
            "from_cache": False,
            "lsa_live_fetch": {
                "http_status": live_resp.get("http_status"),
                "error": live_resp.get("error"),
            },
        }

    def _safe_snapshot(
        self,
        phase: str,
        observe_group_id: str,
        match_id: str,
        event_key: str,
        home: str,
        away: str,
        dqd_score: dict[str, Any],
        dqd_prev: dict[str, Any] | None,
        unlinked_reversal: bool,
        gen: int,
    ) -> None:
        try:
            self._write_snapshot(
                phase=phase,
                observe_group_id=observe_group_id,
                match_id=match_id,
                event_key=event_key,
                home=home,
                away=away,
                dqd_score=dqd_score,
                dqd_prev=dqd_prev,
                unlinked_reversal=unlinked_reversal,
                gen=gen,
            )
        except Exception as e:  # noqa: BLE001
            logger.exception("livescore snapshot failed phase=%s match=%s", phase, match_id)
            try:
                lib.append_jsonl_async(
                    observe_path(self.root),
                    [
                        {
                            "sampled_at": lib.now_cn_iso(),
                            "quoted_at": lib.now_cn_iso(),
                            "phase": phase,
                            "observe_group_id": observe_group_id,
                            "match_id": match_id,
                            "event_key": event_key,
                            "home": home,
                            "away": away,
                            "dqd_score": dqd_score,
                            "dqd_prev": dqd_prev,
                            "unlinked_reversal": bool(unlinked_reversal),
                            "error": {"fatal": str(e)},
                        }
                    ],
                )
            except Exception:  # noqa: BLE001
                pass

    def _write_snapshot(
        self,
        *,
        phase: str,
        observe_group_id: str,
        match_id: str,
        event_key: str,
        home: str,
        away: str,
        dqd_score: dict[str, Any],
        dqd_prev: dict[str, Any] | None,
        unlinked_reversal: bool,
        gen: int,
    ) -> None:
        if self._stop.is_set():
            return
        errors: dict[str, Any] = {}
        resolve = self._resolve_for_match(match_id, home, away)
        lsa_match_id = resolve.get("lsa_match_id")
        lsa_live: dict[str, Any] | None = None
        if isinstance(resolve.get("live_row"), dict):
            lsa_live = {"raw": resolve["live_row"]}
        elif isinstance(resolve.get("lsa_live_fetch"), dict):
            # Resolve failed — still attach live fetch raw when present
            fetch = resolve["lsa_live_fetch"]
            if fetch.get("raw") is not None:
                lsa_live = {
                    "http_status": fetch.get("http_status"),
                    "raw": fetch.get("raw"),
                    "error": fetch.get("error"),
                }
        if not resolve.get("ok"):
            errors["resolve"] = resolve.get("error") or "resolve_failed"

        lsa_events: dict[str, Any] | None = None
        lsa_commentary: dict[str, Any] | None = None
        if lsa_match_id:
            try:
                lsa_events = (self._fetch_events or self._default_fetch_events)(
                    str(lsa_match_id)
                )
                if isinstance(lsa_events, dict) and lsa_events.get("error"):
                    errors["lsa_events"] = lsa_events.get("error")
            except Exception as e:  # noqa: BLE001
                errors["lsa_events"] = str(e)
                lsa_events = {"http_status": None, "raw": None, "error": str(e)}
            try:
                lsa_commentary = (
                    self._fetch_commentary or self._default_fetch_commentary
                )(str(lsa_match_id))
                if isinstance(lsa_commentary, dict) and lsa_commentary.get("error"):
                    errors["lsa_commentary"] = lsa_commentary.get("error")
            except Exception as e:  # noqa: BLE001
                errors["lsa_commentary"] = str(e)
                lsa_commentary = {"http_status": None, "raw": None, "error": str(e)}
        else:
            lsa_events = None
            lsa_commentary = None

        ts = lib.now_cn_iso()
        row: dict[str, Any] = {
            "sampled_at": ts,
            "quoted_at": ts,
            "phase": phase,
            "observe_group_id": observe_group_id,
            "match_id": match_id,
            "event_key": event_key,
            "home": home,
            "away": away,
            "dqd_score": dqd_score,
            "lsa_match_id": lsa_match_id,
            "lsa_events": lsa_events,
            "lsa_commentary": lsa_commentary,
            "lsa_live": lsa_live,
            "resolve": {
                "ok": bool(resolve.get("ok")),
                "from_cache": bool(resolve.get("from_cache")),
                "home_sim": resolve.get("home_sim"),
                "away_sim": resolve.get("away_sim"),
                "swapped_sides": resolve.get("swapped_sides"),
                "candidates": resolve.get("candidates"),
                "error": resolve.get("error"),
            },
        }
        if dqd_prev is not None:
            row["dqd_prev"] = dqd_prev
        if unlinked_reversal:
            row["unlinked_reversal"] = True
        if errors:
            row["error"] = errors
        lib.append_jsonl_async(observe_path(self.root), [row])
