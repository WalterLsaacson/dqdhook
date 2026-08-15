"""Odds-API.io score + Bet365 gate, with Sbobet persisted observe-only.

Polls immediately and every second for sixty seconds.  Each sample is persisted,
graded C/B/A from Bet365 only, and may emit a monotonic position-target upgrade.
Concurrent odds pulls coalesce into ``GET /odds/multi`` (up to 10 events / 1 request).

Every actual HTTP response body is persisted under ``data/pm-quote/book_context_raw/``
(URLs redact ``apiKey``). Observe rows keep compact request metadata and ``raw_path``;
the large response body is not duplicated in JSONL.

Failures are isolated in ``error`` fields. DQD reversals cancel pending polls/upgrades.
"""

from __future__ import annotations

import json
import hashlib
import logging
import os
import re
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Callable, Dict

import quote_lib as lib

logger = logging.getLogger("pm_quote.book_context_observe")
TZ_CN = timezone(timedelta(hours=8))

ODDSPAPI_BASE = "https://api.oddspapi.io/v4"
ODDS_API_IO_BASE = "https://api.odds-api.io/v3"
THE_ODDS_API_BASE = "https://api.the-odds-api.com/v4"

ENV_ODDSPAPI_KEY = "ODDSPAPI_KEY"
ENV_ODDS_API_IO_KEY = "ODDS_API_IO_KEY"
ENV_THE_ODDS_API_KEY = "THE_ODDS_API_KEY"
ENV_BOOK_OBSERVE_SOURCES = "BOOK_OBSERVE_SOURCES"
ENV_BOOK_ODDSPAPI_BOOKS = "BOOK_ODDSPAPI_BOOKS"
ENV_BOOK_ODDS_API_IO_BOOKS = "BOOK_ODDS_API_IO_BOOKS"
ENV_BOOK_THE_ODDS_REGIONS = "BOOK_THE_ODDS_REGIONS"
ENV_BOOK_THE_ODDS_SPORT_KEYS = "BOOK_THE_ODDS_SPORT_KEYS"
ENV_BOOK_THE_ODDS_DISCOVER = "BOOK_THE_ODDS_DISCOVER_SPORTS"

SOURCE_ODDSPAPI = "oddspapi"
SOURCE_ODDSAPIIO = "oddsapiio"
SOURCE_THEODDSAPI = "theoddsapi"

DEFAULT_SOURCES = (SOURCE_ODDSAPIIO,)
DEFAULT_ODDSPAPI_BOOKS = ("pinnacle", "singbet")
DEFAULT_ODDS_API_IO_GATE_BOOKS = ("Bet365",)
# Solo plan: Bet365 gates A/B; Sbobet is sharp observe-only (dashboard name is "Sbobet").
DEFAULT_ODDS_API_IO_BOOKS = ("Bet365", "Sbobet")
ODDS_API_IO_MULTI_MAX = 10
ODDS_API_IO_MULTI_WINDOW_S = 0.05
# us+eu: Leagues Cup / MLS books often land in us; EU books for UEFA.
DEFAULT_THE_ODDS_REGIONS = ("us", "eu")
# Prefer keys that match our common PM slate first; discover fills the rest.
DEFAULT_THE_ODDS_SPORT_KEYS = (
    # Americas cups / leagues (frequent overnight slate)
    "soccer_concacaf_leagues_cup",
    "soccer_usa_mls",
    "soccer_mexico_ligamx",
    "soccer_conmebol_copa_libertadores",
    "soccer_conmebol_copa_sudamericana",
    "soccer_chile_campeonato",
    "soccer_brazil_campeonato",
    "soccer_brazil_serie_b",
    "soccer_argentina_primera_division",
    # UEFA
    "soccer_uefa_champs_league",
    "soccer_uefa_champs_league_qualification",
    "soccer_uefa_europa_league",
    "soccer_uefa_europa_conference_league",
    "soccer_uefa_nations_league",
    "soccer_uefa_european_championship",
    "soccer_fifa_world_cup",
    # Big-5 + cups / 2nd tiers
    "soccer_epl",
    "soccer_efl_champ",
    "soccer_england_efl_cup",
    "soccer_england_league1",
    "soccer_england_league2",
    "soccer_spain_la_liga",
    "soccer_spain_segunda_division",
    "soccer_italy_serie_a",
    "soccer_italy_serie_b",
    "soccer_germany_bundesliga",
    "soccer_germany_bundesliga2",
    "soccer_germany_dfb_pokal",
    "soccer_germany_liga3",
    "soccer_france_ligue_one",
    "soccer_france_ligue_two",
    # Other active national leagues The Odds lists
    "soccer_netherlands_eredivisie",
    "soccer_portugal_primeira_liga",
    "soccer_belgium_first_div",
    "soccer_turkey_super_league",
    "soccer_greece_super_league",
    "soccer_scotland_spl",
    "soccer_spl",
    "soccer_saudi_arabia_pro_league",
    "soccer_japan_j_league",
    "soccer_korea_kleague1",
    "soccer_china_superleague",
    "soccer_australia_aleague",
    "soccer_sweden_allsvenskan",
    "soccer_norway_eliteserien",
    "soccer_denmark_superliga",
    "soccer_austria_bundesliga",
    "soccer_switzerland_superleague",
    "soccer_poland_ekstraklasa",
    "soccer_russia_premier_league",
    "soccer_finland_veikkausliiga",
    "soccer_sweden_superettan",
)
# When whitelist misses, GET /sports once and retry remaining active soccer_* keys.
DEFAULT_THE_ODDS_DISCOVER = True
THE_ODDS_SPORTS_CACHE_TTL_S = 6 * 3600.0

# Solo ~5k req/h: 1s cadence (0/1/…/60) catches book moves faster for A/B upgrades.
DEFAULT_POLL_INTERVAL_S = 1.0
DEFAULT_POLL_TIMEOUT_S = 60.0
DEFAULT_WORKERS = 4
DEFAULT_HTTP_TIMEOUT_S = 12.0
DEFAULT_MIN_SIDE_SIM = 0.72
DEFAULT_EVENTS_CATALOG_TTL_S = 60.0
DEFAULT_RATE_LIMIT_BACKOFF_S = 60.0
DEFAULT_EVENT_TIME_TOLERANCE_S = 12 * 3600.0

PHASE_AF_CONFIRMED = "af_confirmed"
PHASE_DQD_REVERSAL = "dqd_reversal"

GRADE_TARGET_USDC = {"C": 1.0, "B": 2.0, "A": 3.0}
GRADE_RANK = {"C": 0, "B": 1, "A": 2}

_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)
_WS_RE = re.compile(r"\s+")

FetchBookFn = Callable[[str, str, str], Dict[str, Any]]

_active: "BookContextObserver | None" = None
_active_lock = threading.Lock()


def set_active_observer(observer: "BookContextObserver | None") -> None:
    global _active
    with _active_lock:
        _active = observer


def get_active_observer() -> "BookContextObserver | None":
    with _active_lock:
        return _active


def observe_path(root: Path) -> Path:
    return lib.data_dir(root) / "book_context_observe.jsonl"


def fixture_cache_path(root: Path) -> Path:
    return lib.data_dir(root) / "book_fixture_cache.json"


def raw_dir(root: Path) -> Path:
    """Per-request raw HTTP dumps (quota-precious; not auto-pruned)."""
    return lib.data_dir(root) / "book_context_raw"


_APIKEY_QUERY_RE = re.compile(r"([?&](?:apiKey|api_key|key)=)[^&]*", re.IGNORECASE)
_SAFE_NAME_RE = re.compile(r"[^\w.\-]+", re.UNICODE)


def redact_url(url: str) -> str:
    return _APIKEY_QUERY_RE.sub(r"\1REDACTED", str(url or ""))


def select_response_headers(hdrs: dict[str, str] | None) -> dict[str, str]:
    if not hdrs:
        return {}
    out: dict[str, str] = {}
    for k, v in hdrs.items():
        lk = str(k).lower()
        if (
            lk.startswith("x-")
            or lk
            in (
                "date",
                "content-type",
                "retry-after",
                "ratelimit-limit",
                "ratelimit-remaining",
                "ratelimit-reset",
            )
        ):
            out[lk] = str(v)
    return out


def rate_limit_backoff_s(
    headers: dict[str, str] | None,
    *,
    now_epoch: float | None = None,
    default_s: float = DEFAULT_RATE_LIMIT_BACKOFF_S,
) -> float:
    """Return a conservative delay from Retry-After / rate-limit reset headers."""
    hdrs = {str(k).lower(): str(v).strip() for k, v in (headers or {}).items()}
    now = time.time() if now_epoch is None else float(now_epoch)

    retry_after = hdrs.get("retry-after")
    if retry_after:
        try:
            return max(0.0, float(retry_after))
        except ValueError:
            try:
                dt = parsedate_to_datetime(retry_after)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return max(0.0, dt.timestamp() - now)
            except (TypeError, ValueError, OverflowError):
                pass

    reset = hdrs.get("x-ratelimit-reset") or hdrs.get("ratelimit-reset")
    if reset:
        try:
            reset_dt = datetime.fromisoformat(reset.replace("Z", "+00:00"))
            if reset_dt.tzinfo is None:
                reset_dt = reset_dt.replace(tzinfo=timezone.utc)
            delay = reset_dt.timestamp() - now
            return delay if delay > 0.0 else max(0.0, float(default_s))
        except (TypeError, ValueError, OverflowError):
            pass
        try:
            value = float(reset)
            if value > 10_000_000_000:  # epoch milliseconds
                value /= 1000.0
            if value >= 100_000_000:  # epoch seconds, including stale values
                delay = value - now
                return delay if delay > 0.0 else max(0.0, float(default_s))
            if value >= 0.0:  # small values are relative seconds
                return value
        except ValueError:
            pass
    return max(0.0, float(default_s))


def _safe_name(value: str, *, max_len: int = 80) -> str:
    s = _SAFE_NAME_RE.sub("_", str(value or "").strip()) or "na"
    return s[:max_len]


def persist_raw_blob(
    root: Path,
    *,
    source: str,
    kind: str,
    match_id: str,
    phase: str,
    observe_group_id: str,
    seq: int,
    record: dict[str, Any],
) -> str:
    """Write one raw request/response JSON; return path relative to ``data/pm-quote``."""
    now_ns = time.time_ns()
    ts = time.strftime("%Y%m%dT%H%M%S") + f"_{now_ns % 1_000_000_000:09d}"
    group_tag = hashlib.sha256(str(observe_group_id).encode("utf-8")).hexdigest()[:10]
    fname = (
        f"{ts}_{_safe_name(match_id)}_{_safe_name(phase, max_len=40)}_"
        f"{group_tag}_{_safe_name(source)}_{_safe_name(kind)}_{int(seq):06d}.json"
    )
    path = raw_dir(root) / fname
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "saved_at": lib.now_cn_iso(),
        "source": source,
        "kind": kind,
        "match_id": match_id,
        "phase": phase,
        "observe_group_id": observe_group_id,
        **record,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return f"book_context_raw/{fname}"


def request_meta_for_jsonl(
    *,
    kind: str,
    url: str,
    http_status: int | None,
    headers: dict[str, str],
    error: str | None,
    body: Any,
    raw_path: str,
    inline_raw: bool,
) -> dict[str, Any]:
    """Compact request record embedded in observe jsonl (full body always on ``raw_path``)."""
    meta: dict[str, Any] = {
        "kind": kind,
        "url": redact_url(url),
        "http_status": http_status,
        "headers": headers,
        "raw_path": raw_path,
    }
    if error:
        meta["error"] = error
    if inline_raw:
        meta["raw"] = body
    elif isinstance(body, list):
        meta["raw_summary"] = {"type": "list", "len": len(body)}
    elif isinstance(body, dict):
        meta["raw_summary"] = {"type": "object", "keys": sorted(str(k) for k in list(body.keys())[:40])}
    elif body is None:
        meta["raw"] = None
    else:
        meta["raw_summary"] = {"type": type(body).__name__}
    return meta


def compact_sources_for_jsonl(sources: dict[str, Any]) -> dict[str, Any]:
    """Drop duplicated response bodies while preserving parsed data and raw paths."""
    compact: dict[str, Any] = {}
    for name, payload in sources.items():
        if not isinstance(payload, dict):
            compact[name] = payload
            continue
        source_row = dict(payload)
        source_row.pop("raw", None)
        requests = source_row.get("requests")
        if isinstance(requests, list):
            compact_requests: list[Any] = []
            for request in requests:
                if isinstance(request, dict):
                    request_row = dict(request)
                    request_row.pop("raw", None)
                    compact_requests.append(request_row)
                else:
                    compact_requests.append(request)
            source_row["requests"] = compact_requests
        compact[name] = source_row
    return compact


def make_observe_group_id(match_id: str, home: Any, away: Any, event_key: str) -> str:
    return f"{match_id}|{home}-{away}|{event_key}"


def normalize_team(name: str) -> str:
    s = unicodedata.normalize("NFKD", str(name or ""))
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = s.lower().strip()
    s = _PUNCT_RE.sub(" ", s)
    s = _WS_RE.sub(" ", s).strip()
    return s


def team_sim(a: str, b: str) -> float:
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


def _split_csv(raw: str | None, default: tuple[str, ...]) -> tuple[str, ...]:
    if not raw or not str(raw).strip():
        return default
    parts = [p.strip() for p in str(raw).split(",") if p.strip()]
    return tuple(parts) if parts else default


def _env_flag(src: dict[str, str], name: str, default: bool) -> bool:
    raw = str(src.get(name) or "").strip().lower()
    if not raw:
        return default
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    return default


def soccer_sport_keys_from_sports_payload(payload: Any) -> list[str]:
    """Extract active soccer_* keys from The Odds API ``GET /sports`` body."""
    rows = _as_list(payload) if not isinstance(payload, list) else payload
    out: list[str] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        key = str(row.get("key") or "").strip()
        if not key.startswith("soccer_"):
            continue
        if row.get("active") is False:
            continue
        if key in seen:
            continue
        seen.add(key)
        out.append(key)
    return out


def load_source_keys(*, env: dict[str, str] | None = None) -> dict[str, Any]:
    """Load the sole supported confirmation source: Odds-API.io / Bet365."""
    src = env if env is not None else os.environ
    keys = {
        SOURCE_ODDSAPIIO: str(src.get(ENV_ODDS_API_IO_KEY) or "").strip(),
    }
    sources = DEFAULT_SOURCES
    active = [s for s in sources if keys.get(s)]
    return {
        "sources": sources,
        "active_sources": active,
        "keys": keys,
        "oddspapi_books": _split_csv(src.get(ENV_BOOK_ODDSPAPI_BOOKS), DEFAULT_ODDSPAPI_BOOKS),
        # Fetch Bet365 (gate) + Sbobet (observe-only). Env cannot change this pair.
        "oddsapiio_books": DEFAULT_ODDS_API_IO_BOOKS,
        "theoddsapi_regions": _split_csv(src.get(ENV_BOOK_THE_ODDS_REGIONS), DEFAULT_THE_ODDS_REGIONS),
        "theoddsapi_sport_keys": _split_csv(
            src.get(ENV_BOOK_THE_ODDS_SPORT_KEYS), DEFAULT_THE_ODDS_SPORT_KEYS
        ),
        "theoddsapi_discover": _env_flag(
            src, ENV_BOOK_THE_ODDS_DISCOVER, DEFAULT_THE_ODDS_DISCOVER
        ),
    }


def try_create_observer(
    root: Path,
    *,
    poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
    poll_timeout_s: float = DEFAULT_POLL_TIMEOUT_S,
    env: dict[str, str] | None = None,
    **kwargs: Any,
) -> "BookContextObserver | None":
    cfg = load_source_keys(env=env)
    if not cfg.get("active_sources"):
        return None
    return BookContextObserver(
        root,
        source_cfg=cfg,
        poll_interval_s=poll_interval_s,
        poll_timeout_s=poll_timeout_s,
        **kwargs,
    )


def _http_get_json(
    url: str,
    *,
    timeout_s: float,
    headers: dict[str, str] | None = None,
) -> tuple[int | None, dict[str, Any] | list[Any] | None, dict[str, str], str | None]:
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "dongqiudihook-book-observe/1",
            **(headers or {}),
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=max(0.5, float(timeout_s))) as resp:
            status = int(getattr(resp, "status", 200) or 200)
            hdrs = {k.lower(): v for k, v in resp.headers.items()}
            raw = resp.read()
            text = raw.decode("utf-8", errors="replace")
            try:
                body = json.loads(text) if text else None
            except json.JSONDecodeError:
                return status, {"_non_json": text[:500_000]}, hdrs, "non_json_body"
            return status, body, hdrs, None
    except urllib.error.HTTPError as e:
        hdrs = {k.lower(): v for k, v in e.headers.items()} if e.headers else {}
        try:
            text = e.read().decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            text = ""
        body: Any
        try:
            body = json.loads(text) if text else None
        except json.JSONDecodeError:
            body = {"_non_json": text[:500_000]} if text else None
        return int(e.code), body, hdrs, f"http_{e.code}"
    except Exception as e:  # noqa: BLE001
        return None, None, {}, str(e)


def _as_list(payload: Any) -> list[Any]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("data", "fixtures", "events", "results", "items"):
            raw = payload.get(key)
            if isinstance(raw, list):
                return raw
        if payload.get("fixtureId") or payload.get("id") or payload.get("eventId"):
            return [payload]
    return []


def _team_names_from_row(row: dict[str, Any]) -> tuple[str, str]:
    home = (
        row.get("home")
        or row.get("homeTeam")
        or row.get("home_team")
        or row.get("teamHome")
    )
    away = (
        row.get("away")
        or row.get("awayTeam")
        or row.get("away_team")
        or row.get("teamAway")
    )
    if isinstance(home, dict):
        home = home.get("name") or home.get("team") or home.get("participantName")
    if isinstance(away, dict):
        away = away.get("name") or away.get("team") or away.get("participantName")
    parts = row.get("participants")
    if (not home or not away) and isinstance(parts, list):
        for p in parts:
            if not isinstance(p, dict):
                continue
            role = str(p.get("role") or p.get("position") or p.get("type") or "").lower()
            nm = p.get("name") or p.get("participantName") or p.get("team")
            if role in ("home", "1", "h") and not home:
                home = nm
            elif role in ("away", "2", "a") and not away:
                away = nm
        if (not home or not away) and len(parts) >= 2:
            if not home:
                home = parts[0].get("name") if isinstance(parts[0], dict) else parts[0]
            if not away:
                away = parts[1].get("name") if isinstance(parts[1], dict) else parts[1]
    return str(home or ""), str(away or "")


def _row_id(row: dict[str, Any]) -> str | None:
    for key in ("fixtureId", "fixture_id", "eventId", "event_id", "id"):
        val = row.get(key)
        if val is not None and str(val).strip():
            return str(val)
    return None


def _parse_match_datetime(value: Any, *, naive_tz: timezone) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=naive_tz)
    return dt.astimezone(timezone.utc)


def _event_row_datetime(row: dict[str, Any]) -> datetime | None:
    for key in ("date", "startTime", "start_time", "kickoff", "commence_time"):
        dt = _parse_match_datetime(row.get(key), naive_tz=timezone.utc)
        if dt is not None:
            return dt
    return None


def _event_terminal(row: dict[str, Any]) -> bool:
    status = str(row.get("status") or row.get("state") or "").strip().casefold()
    return status in {
        "settled",
        "finished",
        "ended",
        "cancelled",
        "canceled",
        "postponed",
        "abandoned",
    }


def _event_league_name(row: dict[str, Any]) -> str:
    league = row.get("league")
    if isinstance(league, dict):
        return str(league.get("name") or league.get("slug") or "")
    return str(league or row.get("leagueName") or "")


def resolve_team_match(
    rows: list[dict[str, Any]],
    *,
    home: str,
    away: str,
    min_side: float = DEFAULT_MIN_SIDE_SIM,
    kickoff_at: Any = None,
    league: str = "",
    max_time_delta_s: float = DEFAULT_EVENT_TIME_TOLERANCE_S,
    require_nonterminal: bool = False,
) -> dict[str, Any]:
    best: dict[str, Any] | None = None
    best_key: tuple[float, float, float] | None = None
    kickoff = _parse_match_datetime(kickoff_at, naive_tz=TZ_CN)
    tolerance = max(0.0, float(max_time_delta_s))
    for row in rows:
        if not isinstance(row, dict):
            continue
        if require_nonterminal and _event_terminal(row):
            continue
        row_dt = _event_row_datetime(row)
        delta_s: float | None = None
        if kickoff is not None:
            if row_dt is None:
                continue
            delta_s = abs((row_dt - kickoff).total_seconds())
            if delta_s > tolerance:
                continue
        h_name, a_name = _team_names_from_row(row)
        hs = team_sim(home, h_name)
        aws = team_sim(away, a_name)
        hs_sw = team_sim(home, a_name)
        aws_sw = team_sim(away, h_name)
        swapped = False
        if min(hs_sw, aws_sw) > min(hs, aws):
            hs, aws = hs_sw, aws_sw
            swapped = True
        if hs < min_side or aws < min_side:
            continue
        score = (hs + aws) / 2.0
        row_league = _event_league_name(row)
        league_score = team_sim(league, row_league) if league and row_league else 0.0
        candidate_key = (score, league_score, -(delta_s or 0.0))
        if best_key is None or candidate_key > best_key:
            rid = _row_id(row)
            best_key = candidate_key
            best = {
                "ok": True,
                "row": row,
                "id": rid,
                "home_sim": round(hs, 4),
                "away_sim": round(aws, 4),
                "candidates": len(rows),
                "swapped": swapped,
                "event_home": h_name,
                "event_away": a_name,
                "event_date": row_dt.isoformat() if row_dt is not None else None,
                "event_league": row_league or None,
                "league_sim": round(league_score, 4),
                "time_delta_s": round(delta_s, 3) if delta_s is not None else None,
            }
    if best is None or not best.get("id"):
        return {
            "ok": False,
            "error": "not_mapped",
            "id": None,
            "row": None,
            "candidates": len(rows),
        }
    return best


_BOOK_LATENCY_RE = re.compile(
    r"[\s_\-]*\((?:no\s*)?latency\)|\s+(?:no\s+)?latency\b",
    re.IGNORECASE,
)


def _normalize_book_key(name: str) -> str:
    # Odds-API.io may label the same shop "Bet365 (no latency)".
    s = _BOOK_LATENCY_RE.sub(" ", str(name or ""))
    return normalize_team(s).replace(" ", "")


_GATE_BOOK_KEYS = frozenset(_normalize_book_key(b) for b in DEFAULT_ODDS_API_IO_GATE_BOOKS)


def _is_gate_book(name: str) -> bool:
    return _normalize_book_key(name) in _GATE_BOOK_KEYS


def _with_observe_only(entry: dict[str, Any]) -> dict[str, Any]:
    if not _is_gate_book(str(entry.get("book") or "")):
        entry["observe_only"] = True
    return entry


def _ml_from_outcomes(
    outcomes: list[Any],
    *,
    home: str,
    away: str,
) -> dict[str, Any] | None:
    if not outcomes:
        return None
    ml: dict[str, Any] = {"h": None, "d": None, "a": None}
    nh, na = normalize_team(home), normalize_team(away)
    for o in outcomes:
        if not isinstance(o, dict):
            continue
        label = str(o.get("name") or o.get("label") or o.get("outcome") or "")
        nl = normalize_team(label)
        price = o.get("price")
        if price is None:
            price = o.get("odds") or o.get("decimal") or o.get("value")
        try:
            pval = float(price) if price is not None else None
        except (TypeError, ValueError):
            pval = None
        if nl in ("draw", "x", "tie"):
            ml["d"] = pval
        elif nh and (nl == nh or team_sim(label, home) >= 0.85):
            ml["h"] = pval
        elif na and (nl == na or team_sim(label, away) >= 0.85):
            ml["a"] = pval
    if ml["h"] is None and ml["d"] is None and ml["a"] is None:
        return None
    return ml


def _find_h2h_market(markets: list[Any]) -> dict[str, Any] | None:
    for m in markets:
        if not isinstance(m, dict):
            continue
        key = str(m.get("key") or m.get("market") or m.get("name") or "").lower()
        if key in ("h2h", "1x2", "ml", "moneyline", "match winner", "match_winner"):
            return m
    for m in markets:
        if isinstance(m, dict) and m.get("outcomes"):
            return m
    return None


def parse_oddspapi_books(
    odds_payload: Any,
    *,
    wanted_books: tuple[str, ...],
    home: str,
    away: str,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    wanted = {_normalize_book_key(b): b for b in wanted_books}
    found_keys: set[str] = set()

    bookmakers: list[Any] = []
    if isinstance(odds_payload, dict):
        raw = odds_payload.get("bookmakers") or odds_payload.get("books") or odds_payload.get("data")
        if isinstance(raw, list):
            bookmakers = raw
        elif isinstance(raw, dict):
            bookmakers = [raw]
    elif isinstance(odds_payload, list):
        bookmakers = odds_payload

    for bm in bookmakers:
        if not isinstance(bm, dict):
            continue
        book_name = str(
            bm.get("bookmaker")
            or bm.get("bookmakerName")
            or bm.get("name")
            or bm.get("key")
            or bm.get("slug")
            or ""
        )
        norm = _normalize_book_key(book_name)
        if norm not in wanted:
            continue
        found_keys.add(norm)
        suspended = bm.get("suspended")
        if suspended is None:
            suspended = bm.get("isSuspended") or bm.get("betStop")
        markets = bm.get("markets") or bm.get("odds") or []
        ml = None
        if isinstance(markets, list):
            mkt = _find_h2h_market(markets)
            if mkt and isinstance(mkt.get("outcomes"), list):
                ml = _ml_from_outcomes(mkt["outcomes"], home=home, away=away)
        status = "open"
        if suspended is True:
            status = "suspended"
        elif not markets or ml is None:
            status = "missing"
        entry: dict[str, Any] = {
            "book": wanted[norm],
            "status": status,
        }
        if suspended is not None:
            entry["suspended"] = bool(suspended)
        if ml is not None:
            entry["ml"] = ml
        rows.append(entry)

    for norm, display in wanted.items():
        if norm in found_keys:
            continue
        rows.append({"book": display, "status": "missing"})

    return rows


def _bet365_markets(odds_payload: Any) -> list[dict[str, Any]]:
    """Return the complete Bet365 market list from an Odds-API.io odds body."""
    if not isinstance(odds_payload, dict):
        return []
    raw = odds_payload.get("bookmakers") or odds_payload.get("books") or odds_payload.get("data")
    if isinstance(raw, dict):
        for name, markets in raw.items():
            if _normalize_book_key(str(name)) == "bet365" and isinstance(markets, list):
                return [m for m in markets if isinstance(m, dict)]
        return []
    if isinstance(raw, list):
        for book in raw:
            if not isinstance(book, dict):
                continue
            name = str(book.get("bookmaker") or book.get("name") or book.get("key") or "")
            if _normalize_book_key(name) != "bet365":
                continue
            markets = book.get("markets") or book.get("odds") or []
            return [m for m in markets if isinstance(m, dict)] if isinstance(markets, list) else []
    return []


def _offered(row: dict[str, Any], key: str) -> bool:
    return key in row and row.get(key) not in (None, "")


def inspect_bet365_impossible_markets(
    odds_payload: Any,
    *,
    home_score: Any,
    away_score: Any,
) -> dict[str, Any]:
    """Find Bet365 offers that cannot still win at the already-observed score.

    Only full-match score-sensitive markets are considered.  A B grade also
    requires at least one such market, preventing a lone moneyline from passing
    merely because there was nothing useful to inspect.
    """
    try:
        home = int(home_score)
        away = int(away_score)
    except (TypeError, ValueError):
        return {
            "score_sensitive_markets": 0,
            "score_sensitive_market_names": [],
            "impossible_offers": [],
        }
    total = home + away
    sensitive: list[str] = []
    impossible: list[dict[str, Any]] = []

    def _line(v: Any) -> float | None:
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    for market in _bet365_markets(odds_payload):
        name = str(market.get("name") or market.get("market") or "").strip()
        key = name.casefold()
        rows = market.get("odds") or market.get("outcomes") or []
        if not isinstance(rows, list):
            continue
        kind = ""
        threshold: int | None = None
        if key == "correct score":
            kind = "correct_score"
        elif key in ("totals", "alternative goal line"):
            kind, threshold = "totals_under", total
        elif key == "team total goals home":
            kind, threshold = "team_total_home_under", home
        elif key == "team total goals away":
            kind, threshold = "team_total_away_under", away
        elif key == "both teams to score":
            kind = "btts"
        elif key == "clean sheet home":
            kind = "clean_sheet_home"
        elif key == "clean sheet away":
            kind = "clean_sheet_away"
        if not kind:
            continue
        sensitive.append(name)
        for offer in rows:
            if not isinstance(offer, dict):
                continue
            if kind == "correct_score":
                label = str(offer.get("label") or offer.get("name") or "")
                m = re.fullmatch(r"\s*(\d+)\s*[-:–—]\s*(\d+)\s*", label)
                if m and _offered(offer, "odds"):
                    h, a = int(m.group(1)), int(m.group(2))
                    if h < home or a < away:
                        impossible.append({"market": name, "offer": label, "reason": "score_below_target"})
            elif kind in ("totals_under", "team_total_home_under", "team_total_away_under"):
                hdp = _line(offer.get("hdp"))
                if hdp is not None and threshold is not None and hdp < threshold and _offered(offer, "under"):
                    impossible.append({
                        "market": name,
                        "offer": f"under {offer.get('hdp')}",
                        "reason": f"line_below_observed_{threshold}",
                    })
            elif kind == "btts" and home > 0 and away > 0 and _offered(offer, "no"):
                impossible.append({"market": name, "offer": "no", "reason": "both_teams_already_scored"})
            elif kind == "clean_sheet_home" and away > 0 and _offered(offer, "yes"):
                impossible.append({"market": name, "offer": "yes", "reason": "away_already_scored"})
            elif kind == "clean_sheet_away" and home > 0 and _offered(offer, "yes"):
                impossible.append({"market": name, "offer": "yes", "reason": "home_already_scored"})
    return {
        "score_sensitive_markets": len(sensitive),
        "score_sensitive_market_names": sensitive,
        "impossible_offers": impossible,
    }


def grade_oddsapiio_sample(
    source_payload: Any,
    *,
    home_score: Any,
    away_score: Any,
) -> dict[str, Any]:
    """Grade one Odds-API.io score + Bet365 snapshot as C/B/A.

    A requires a matching provider score *and* a clean Bet365 book (open,
    inspectable score-sensitive markets, no already-impossible offers).
    """
    source = source_payload if isinstance(source_payload, dict) else {}
    provider_score = source.get("score") if isinstance(source.get("score"), dict) else {}
    try:
        target = {"home": int(home_score), "away": int(away_score)}
    except (TypeError, ValueError):
        target = {"home": None, "away": None}
    score_match = (
        target["home"] is not None
        and provider_score.get("home") == target["home"]
        and provider_score.get("away") == target["away"]
    )
    bet365 = next(
        (
            b for b in (source.get("books") or [])
            if isinstance(b, dict) and _normalize_book_key(str(b.get("book") or "")) == "bet365"
        ),
        {},
    )
    bet365_status = str(bet365.get("status") or "missing")
    identity_verified = source.get("identity_verified") is True
    provider_target = dict(target)
    if str(source.get("orientation") or "") == "swapped":
        provider_target = {"home": target["away"], "away": target["home"]}
    inspection = inspect_bet365_impossible_markets(
        source.get("raw"),
        home_score=provider_target["home"],
        away_score=provider_target["away"],
    )
    bet365_clean = (
        bet365_status == "open"
        and inspection["score_sensitive_markets"] > 0
        and not inspection["impossible_offers"]
    )
    if not identity_verified:
        level, reason = "C", "oddsapiio_event_identity_unverified"
    elif score_match and bet365_clean:
        level, reason = "A", "oddsapiio_score_matches_and_bet365_open"
    elif bet365_clean:
        level, reason = "B", "bet365_open_no_impossible_markets"
    elif bet365_status != "open":
        level, reason = "C", f"bet365_{bet365_status}"
    elif inspection["score_sensitive_markets"] <= 0:
        level, reason = "C", "bet365_no_score_sensitive_markets"
    else:
        level, reason = "C", "bet365_has_impossible_markets"
    return {
        "level": level,
        "target_usdc": GRADE_TARGET_USDC[level],
        "reason": reason,
        "target_score": target,
        "provider_score": provider_score or None,
        "score_match": bool(score_match),
        "identity_verified": identity_verified,
        "orientation": source.get("orientation"),
        "bet365_status": bet365_status,
        **inspection,
    }


def odds_sample_fingerprint(source_payload: Any) -> str:
    source = source_payload if isinstance(source_payload, dict) else {}
    material = {
        "score": source.get("score"),
        "event_status": source.get("event_status"),
        "bet365_markets": _bet365_markets(source.get("raw")),
    }
    body = json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def parse_oddsapiio_event_meta(payload: Any) -> dict[str, Any]:
    """Extract live score / clock from Odds-API.io ``/events`` or ``/events/{id}``."""
    if not isinstance(payload, dict):
        return {}
    out: dict[str, Any] = {}
    if payload.get("id") is not None:
        out["event_id"] = payload.get("id")
    if payload.get("status") is not None:
        out["event_status"] = payload.get("status")
    event_home, event_away = _team_names_from_row(payload)
    if event_home:
        out["event_home"] = event_home
    if event_away:
        out["event_away"] = event_away
    event_dt = _event_row_datetime(payload)
    if event_dt is not None:
        out["event_date"] = event_dt.isoformat()

    scores = payload.get("scores")
    if isinstance(scores, dict):

        def _score_int(v: Any) -> int | None:
            if v is None or v == "":
                return None
            try:
                return int(v)
            except (TypeError, ValueError):
                try:
                    return int(float(v))
                except (TypeError, ValueError):
                    return None

        home_sc = _score_int(scores.get("home"))
        away_sc = _score_int(scores.get("away"))
        if home_sc is not None or away_sc is not None:
            out["score"] = {"home": home_sc, "away": away_sc}
        periods = scores.get("periods")
        if isinstance(periods, dict) and periods:
            out["periods"] = periods

    clock = payload.get("clock")
    if isinstance(clock, dict) and clock:
        clock_out: dict[str, Any] = {}
        for k in ("minute", "playedSeconds", "period", "running", "statusDetail", "injuryTime"):
            if clock.get(k) is not None:
                clock_out[k] = clock.get(k)
        if clock_out:
            out["clock"] = clock_out
    return out


def parse_oddsapiio_books(
    odds_payload: Any,
    *,
    wanted_books: tuple[str, ...],
    home: str,
    away: str,
) -> list[dict[str, Any]]:
    if odds_payload is None or odds_payload == {}:
        return [_with_observe_only({"book": b, "status": "missing"}) for b in wanted_books]

    # Odds-API.io returns bookmakers as a dict: {"Bet365": [ {name: ML, odds: [...]}, ... ]}
    # Older/docs shapes may use a list of {bookmaker, markets} objects.
    bookmakers: list[Any] = []
    if isinstance(odds_payload, dict):
        raw = odds_payload.get("bookmakers") or odds_payload.get("books") or odds_payload.get("data")
        if isinstance(raw, dict):
            for book_name, markets in raw.items():
                bookmakers.append({"bookmaker": book_name, "markets": markets})
        elif isinstance(raw, list):
            bookmakers = raw
        elif raw is None and odds_payload.get("id"):
            bookmakers = [odds_payload]
    elif isinstance(odds_payload, list):
        bookmakers = odds_payload

    if not bookmakers:
        return [_with_observe_only({"book": b, "status": "missing"}) for b in wanted_books]

    rows: list[dict[str, Any]] = []
    wanted = {_normalize_book_key(b): b for b in wanted_books}
    found: set[str] = set()
    for bm in bookmakers:
        if not isinstance(bm, dict):
            continue
        book_name = str(bm.get("bookmaker") or bm.get("name") or bm.get("key") or "")
        norm = _normalize_book_key(book_name)
        if norm not in wanted:
            continue
        found.add(norm)
        markets = bm.get("markets") or bm.get("odds") or []
        ml = None
        if isinstance(markets, list):
            mkt = _find_h2h_market(markets)
            if mkt:
                # Odds-API.io ML shape: {"name":"ML","odds":[{"home":"..","draw":"..","away":".."}]}
                odds_rows = mkt.get("odds")
                if (
                    isinstance(odds_rows, list)
                    and odds_rows
                    and isinstance(odds_rows[0], dict)
                    and (
                        "home" in odds_rows[0]
                        or "away" in odds_rows[0]
                        or "draw" in odds_rows[0]
                    )
                ):
                    o0 = odds_rows[0]

                    def _f(v: Any) -> float | None:
                        try:
                            return float(v) if v is not None and v != "" else None
                        except (TypeError, ValueError):
                            return None

                    cand = {"h": _f(o0.get("home")), "d": _f(o0.get("draw")), "a": _f(o0.get("away"))}
                    if any(x is not None for x in cand.values()):
                        ml = cand
                elif isinstance(mkt.get("outcomes"), list):
                    ml = _ml_from_outcomes(mkt["outcomes"], home=home, away=away)
        suspended = bm.get("suspended")
        status = "open"
        if suspended is True:
            status = "suspended"
        elif not markets:
            status = "missing"
        entry: dict[str, Any] = {"book": wanted[norm], "status": status}
        if suspended is not None:
            entry["suspended"] = bool(suspended)
        if ml is not None:
            entry["ml"] = ml
        # Event-level status (live/pending/settled) often sits on the odds payload root.
        if isinstance(odds_payload, dict) and odds_payload.get("status") is not None:
            entry["event_status"] = odds_payload.get("status")
        rows.append(_with_observe_only(entry))

    for norm, display in wanted.items():
        if norm in found:
            continue
        rows.append(_with_observe_only({"book": display, "status": "missing"}))
    return rows


def parse_theoddsapi_books(
    odds_payload: Any,
    *,
    home: str,
    away: str,
) -> list[dict[str, Any]]:
    if not isinstance(odds_payload, dict):
        return []
    bookmakers = odds_payload.get("bookmakers")
    if not isinstance(bookmakers, list):
        return []
    rows: list[dict[str, Any]] = []
    for bm in bookmakers:
        if not isinstance(bm, dict):
            continue
        book_key = str(bm.get("key") or bm.get("title") or "")
        markets = bm.get("markets") or []
        mkt = _find_h2h_market(markets if isinstance(markets, list) else [])
        ml = None
        if mkt and isinstance(mkt.get("outcomes"), list):
            ml = _ml_from_outcomes(mkt["outcomes"], home=home, away=away)
        status = "open" if ml is not None else "missing"
        entry: dict[str, Any] = {"book": book_key, "status": status}
        if ml is not None:
            entry["ml"] = ml
        rows.append(entry)
    return rows


def _source_suspended_signal(books: list[Any]) -> bool:
    if not books:
        return False
    saw_book = False
    all_missing = True
    any_suspended = False
    for b in books:
        if not isinstance(b, dict):
            continue
        saw_book = True
        st = b.get("status")
        if st == "open":
            all_missing = False
        elif st == "suspended":
            any_suspended = True
            all_missing = False
        elif st == "missing":
            continue
        else:
            all_missing = False
    if not saw_book:
        return False
    return any_suspended or all_missing


def summarize_sources(sources: dict[str, Any]) -> dict[str, Any]:
    ok_sources: list[str] = []
    any_suspended = False
    any_missing = False
    any_open = False
    suspended_votes = 0

    for name, payload in sources.items():
        if not isinstance(payload, dict):
            continue
        if payload.get("ok"):
            ok_sources.append(name)
        books = payload.get("books")
        if not isinstance(books, list):
            continue
        for b in books:
            if not isinstance(b, dict):
                continue
            st = b.get("status")
            if st == "open":
                any_open = True
            elif st == "suspended":
                any_suspended = True
            elif st == "missing":
                any_missing = True
        if payload.get("ok") and _source_suspended_signal(books):
            suspended_votes += 1

    quorum_suspended = len(ok_sources) >= 2 and suspended_votes >= 2
    return {
        "ok_sources": ok_sources,
        "any_suspended": any_suspended,
        "any_missing": any_missing,
        "any_open": any_open,
        "quorum_suspended": quorum_suspended,
    }


@dataclass
class _GroupState:
    observe_group_id: str
    match_id: str
    event_key: str
    home: str
    away: str
    dqd_score: dict[str, Any]
    ev: dict[str, Any] = field(default_factory=dict)
    af_gate: dict[str, Any] = field(default_factory=dict)
    gen: int = 0
    timers: list[threading.Timer] = field(default_factory=list)
    highest_grade: str = "C"
    last_fingerprint: str | None = None
    reversed: bool = False


class BookContextObserver:
    """Background bookmaker suspension snapshots for AF-confirmed goals and DQD reversals."""

    def __init__(
        self,
        root: Path,
        *,
        source_cfg: dict[str, Any] | None = None,
        poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
        poll_timeout_s: float = DEFAULT_POLL_TIMEOUT_S,
        workers: int = DEFAULT_WORKERS,
        http_timeout_s: float = DEFAULT_HTTP_TIMEOUT_S,
        min_side_sim: float = DEFAULT_MIN_SIDE_SIM,
        events_catalog_ttl_s: float = DEFAULT_EVENTS_CATALOG_TTL_S,
        fetch_oddspapi: FetchBookFn | None = None,
        fetch_oddsapiio: FetchBookFn | None = None,
        fetch_theoddsapi: FetchBookFn | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        self.root = Path(root)
        self.cfg = source_cfg if source_cfg is not None else load_source_keys(env=env)
        self.active_sources: tuple[str, ...] = tuple(
            s for s in (self.cfg.get("active_sources") or ()) if s == SOURCE_ODDSAPIIO
        )
        self.keys: dict[str, str] = dict(self.cfg.get("keys") or {})
        self.oddspapi_books: tuple[str, ...] = tuple(
            self.cfg.get("oddspapi_books") or DEFAULT_ODDSPAPI_BOOKS
        )
        self.oddsapiio_books: tuple[str, ...] = DEFAULT_ODDS_API_IO_BOOKS
        self.theoddsapi_regions: tuple[str, ...] = tuple(
            self.cfg.get("theoddsapi_regions") or DEFAULT_THE_ODDS_REGIONS
        )
        self.theoddsapi_sport_keys: tuple[str, ...] = tuple(
            self.cfg.get("theoddsapi_sport_keys") or DEFAULT_THE_ODDS_SPORT_KEYS
        )
        self.theoddsapi_discover: bool = bool(
            self.cfg.get("theoddsapi_discover", DEFAULT_THE_ODDS_DISCOVER)
        )
        self.poll_interval_s = max(0.01, float(poll_interval_s))
        self.poll_timeout_s = max(self.poll_interval_s, float(poll_timeout_s))
        self.http_timeout_s = max(0.5, float(http_timeout_s))
        self.min_side_sim = float(min_side_sim)
        self.events_catalog_ttl_s = max(0.0, float(events_catalog_ttl_s))
        self._fetch_oddspapi = fetch_oddspapi
        self._fetch_oddsapiio = fetch_oddsapiio
        self._fetch_theoddsapi = fetch_theoddsapi
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._by_match: dict[str, _GroupState] = {}
        self._fixture_cache: dict[str, Any] = {}
        self._fixture_disk_lock = threading.Lock()
        self._snap_ctx: dict[str, Any] = {}
        self._snap_local = threading.local()
        self._upgrades: list[dict[str, Any]] = []
        self._raw_seq = 0
        self._events_catalog_lock = threading.Lock()
        self._events_catalog_body: Any = None
        self._events_catalog_fetched_mono = 0.0
        self._events_catalog_fetched_at: str | None = None
        self._events_catalog_raw_path: str | None = None
        self._events_catalog_status: int | None = None
        self._events_catalog_generation = 0
        self._events_negative_cache: dict[str, int] = {}
        self._rate_limit_lock = threading.Lock()
        self._rate_limited_until_mono = 0.0
        self._rate_limited_until_iso: str | None = None
        self._odds_multi_lock = threading.Lock()
        self._odds_multi_pending: list[
            tuple[str, str, dict[str, Any], Future]
        ] = []
        self._odds_multi_flush_scheduled = False
        self._odds_multi_window_s = ODDS_API_IO_MULTI_WINDOW_S
        self._theodds_sports_keys: list[str] | None = None
        self._theodds_sports_fetched_at: float = 0.0
        self._pool = ThreadPoolExecutor(
            max_workers=max(1, int(workers)),
            thread_name_prefix="book-ctx-obs",
        )
        self._http_pool = ThreadPoolExecutor(
            max_workers=max(2, min(8, int(workers) * 2)),
            thread_name_prefix="book-ctx-http",
        )
        self._load_fixture_cache()

    def start(self) -> None:
        self._stop.clear()
        set_active_observer(self)
        observe_books = ",".join(
            b for b in self.oddsapiio_books if not _is_gate_book(b)
        ) or "none"
        logger.info(
            "odds confirmation on → %s source=Odds-API.io gate=Bet365 observe=%s poll=%ss timeout=%ss multi=≤%s/%.0fms",
            observe_path(self.root),
            observe_books,
            self.poll_interval_s,
            self.poll_timeout_s,
            ODDS_API_IO_MULTI_MAX,
            self._odds_multi_window_s * 1000,
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
        try:
            self._http_pool.shutdown(wait=False, cancel_futures=True)
        except TypeError:
            self._http_pool.shutdown(wait=False)
        if get_active_observer() is self:
            set_active_observer(None)

    def on_af_confirmed(
        self,
        root: Path | None = None,
        *,
        match_id: str,
        event_key: str,
        ev: dict[str, Any] | None = None,
        af_gate: dict[str, Any] | None = None,
    ) -> str | None:
        if self._stop.is_set():
            return None
        mid = str(match_id or "").strip()
        if not mid:
            return None
        ev = ev if isinstance(ev, dict) else {}
        gate = af_gate if isinstance(af_gate, dict) else {}
        home_sc = ev.get("home_score", gate.get("home_score"))
        away_sc = ev.get("away_score", gate.get("away_score"))
        if isinstance(gate.get("goals"), dict):
            g = gate["goals"]
            if home_sc is None:
                home_sc = g.get("home")
            if away_sc is None:
                away_sc = g.get("away")
        key = str(event_key or "")
        group_id = make_observe_group_id(mid, home_sc, away_sc, key)
        dqd_score = {"home": home_sc, "away": away_sc}
        with self._lock:
            prev = self._by_match.get(mid)
            if prev is not None:
                for t in prev.timers:
                    t.cancel()
                prev.timers.clear()
                gen = prev.gen + 1
            else:
                gen = 1
            state = _GroupState(
                observe_group_id=group_id,
                match_id=mid,
                event_key=key,
                home=str(ev.get("home") or gate.get("home") or ""),
                away=str(ev.get("away") or gate.get("away") or ""),
                dqd_score=dqd_score,
                ev=dict(ev),
                af_gate=dict(gate),
                gen=gen,
            )
            self._by_match[mid] = state
            self._arm_delayed(state)
        self._pool.submit(
            self._safe_snapshot,
            self._poll_phase(0.0),
            state.observe_group_id,
            mid,
            key,
            state.home,
            state.away,
            dict(dqd_score),
            None,
            False,
            gen,
            0.0,
        )
        return group_id

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
                linked.reversed = True
                for t in linked.timers:
                    t.cancel()
                linked.timers.clear()
                group_id = linked.observe_group_id
                home_name = linked.home or str(ev.get("home") or "")
                away_name = linked.away or str(ev.get("away") or "")
                key = linked.event_key or str(event_key or "")
                unlinked = False
                gen = linked.gen
            else:
                home_sc = ev.get("home_score")
                away_sc = ev.get("away_score")
                key = str(event_key or "")
                group_id = make_observe_group_id(mid, home_sc, away_sc, key or "reversal")
                home_name = str(ev.get("home") or "")
                away_name = str(ev.get("away") or "")
                unlinked = True
                gen = 0
        prev = ev.get("prev") if isinstance(ev.get("prev"), dict) else None
        dqd_score = {
            "home": ev.get(
                "home_score",
                (ev.get("curr") or {}).get("home") if isinstance(ev.get("curr"), dict) else None,
            ),
            "away": ev.get(
                "away_score",
                (ev.get("curr") or {}).get("away") if isinstance(ev.get("curr"), dict) else None,
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
            None,
        )
        return group_id

    def _poll_offsets(self) -> list[float]:
        count = int(self.poll_timeout_s // self.poll_interval_s)
        return [self.poll_interval_s * i for i in range(1, count + 1)]

    @staticmethod
    def _poll_phase(offset_s: float) -> str:
        if offset_s <= 0:
            return PHASE_AF_CONFIRMED
        label = int(offset_s) if float(offset_s).is_integer() else round(offset_s, 3)
        return f"odds_poll_{label}s"

    def _arm_delayed(self, state: _GroupState) -> None:
        for delay in self._poll_offsets():
            phase = self._poll_phase(delay)
            gen = state.gen

            def _fire(
                ph: str = phase,
                g: int = gen,
                mid: str = state.match_id,
                offset: float = delay,
            ) -> None:
                if self._stop.is_set():
                    return
                with self._lock:
                    cur = self._by_match.get(mid)
                    if cur is None or cur.gen != g or cur.reversed:
                        return
                    snap_state = cur
                self._pool.submit(
                    self._safe_snapshot,
                    ph,
                    snap_state.observe_group_id,
                    snap_state.match_id,
                    snap_state.event_key,
                    snap_state.home,
                    snap_state.away,
                    dict(snap_state.dqd_score),
                    None,
                    False,
                    g,
                    offset,
                )

            t = threading.Timer(delay, _fire)
            t.daemon = True
            state.timers.append(t)
            t.start()

    def _load_fixture_cache(self) -> None:
        path = fixture_cache_path(self.root)
        try:
            raw = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        except (OSError, json.JSONDecodeError):
            raw = {}
        if isinstance(raw, dict):
            self._fixture_cache = raw

    def _persist_fixture_cache(self) -> None:
        path = fixture_cache_path(self.root)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with self._fixture_disk_lock:
                with self._lock:
                    payload = dict(self._fixture_cache)
                tmp = path.with_suffix(path.suffix + ".tmp")
                tmp.write_text(
                    json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
                tmp.replace(path)
        except OSError as e:
            logger.warning("book fixture cache write failed: %s", e)

    def _cache_entry(self, match_id: str) -> dict[str, Any]:
        with self._lock:
            row = self._fixture_cache.get(match_id)
        return dict(row) if isinstance(row, dict) else {}

    def _update_cache_entry(self, match_id: str, patch: dict[str, Any]) -> None:
        changed = False
        with self._lock:
            cur = self._fixture_cache.get(match_id)
            base = dict(cur) if isinstance(cur, dict) else {}
            for key, value in patch.items():
                if base.get(key) != value:
                    base[key] = value
                    changed = True
            if changed:
                self._fixture_cache[match_id] = base
        if changed:
            self._persist_fixture_cache()

    def _clear_oddsapiio_mapping(self, match_id: str) -> None:
        keys = {
            "oddsapiio_event_id",
            "oddsapiio_swapped",
            "oddsapiio_event_date",
            "oddsapiio_event_league",
            "oddsapiio_event_home",
            "oddsapiio_event_away",
        }
        with self._lock:
            cur = self._fixture_cache.get(match_id)
            if not isinstance(cur, dict):
                return
            row = {k: v for k, v in cur.items() if k not in keys}
            self._fixture_cache[match_id] = row
        self._persist_fixture_cache()

    def _mapping_context(self, match_id: str) -> dict[str, Any]:
        with self._lock:
            state = self._by_match.get(str(match_id))
            ev = dict(state.ev) if state is not None else {}
        kickoff = (
            ev.get("kickoff_beijing")
            or ev.get("kickoff")
            or ev.get("start_time")
            or ev.get("commence_time")
        )
        return {"kickoff_at": kickoff, "league": ev.get("league")}

    def _url_with_key(self, base: str, path: str, params: dict[str, Any], api_key: str) -> str:
        q = {k: v for k, v in params.items() if v is not None}
        q["apiKey"] = api_key
        return f"{base.rstrip('/')}/{path.lstrip('/')}?{urllib.parse.urlencode(q)}"

    def _begin_snap_ctx(
        self,
        *,
        phase: str,
        observe_group_id: str,
        match_id: str,
        event_key: str,
    ) -> None:
        ctx = {
            "phase": phase,
            "observe_group_id": observe_group_id,
            "match_id": match_id,
            "event_key": event_key,
        }
        self._snap_local.ctx = ctx
        with self._lock:
            # Kept for diagnostics/tests; request code uses thread-local context.
            self._snap_ctx = dict(ctx)

    def _current_snap_ctx(self) -> dict[str, Any]:
        local = getattr(self._snap_local, "ctx", None)
        if isinstance(local, dict):
            return dict(local)
        with self._lock:
            return dict(self._snap_ctx)

    def _next_raw_seq(self) -> int:
        with self._lock:
            self._raw_seq += 1
            return int(self._raw_seq)

    def _active_rate_limit(self) -> str | None:
        with self._rate_limit_lock:
            if time.monotonic() >= self._rate_limited_until_mono:
                return None
            return self._rate_limited_until_iso

    def _note_rate_limit(self, headers: dict[str, str]) -> str:
        delay = rate_limit_backoff_s(headers)
        deadline_mono = time.monotonic() + delay
        deadline_iso = (
            datetime.now(timezone.utc) + timedelta(seconds=delay)
        ).isoformat(timespec="seconds")
        with self._rate_limit_lock:
            if deadline_mono >= self._rate_limited_until_mono:
                self._rate_limited_until_mono = deadline_mono
                self._rate_limited_until_iso = deadline_iso
            return str(self._rate_limited_until_iso or deadline_iso)

    def _record_http(
        self,
        *,
        source: str,
        kind: str,
        url: str,
        inline_raw: bool,
        snapshot_ctx: dict[str, Any] | None = None,
    ) -> tuple[int | None, Any, dict[str, str], str | None, dict[str, Any]]:
        """GET + always persist full body under ``book_context_raw/``."""
        if source == SOURCE_ODDSAPIIO:
            limited_until = self._active_rate_limit()
            if limited_until:
                return None, None, {}, "rate_limited", {
                    "kind": kind,
                    "url": redact_url(url),
                    "http_status": None,
                    "headers": {},
                    "skipped": True,
                    "rate_limited_until": limited_until,
                }
        status, body, hdrs, err = _http_get_json(url, timeout_s=self.http_timeout_s)
        headers = select_response_headers(hdrs)
        ctx = dict(snapshot_ctx) if isinstance(snapshot_ctx, dict) else self._current_snap_ctx()
        seq = self._next_raw_seq()
        raw_path = persist_raw_blob(
            self.root,
            source=source,
            kind=kind,
            match_id=str(ctx.get("match_id") or ""),
            phase=str(ctx.get("phase") or "unknown"),
            observe_group_id=str(ctx.get("observe_group_id") or ""),
            seq=seq,
            record={
                "event_key": ctx.get("event_key"),
                "url": redact_url(url),
                "http_status": status,
                "headers": headers,
                "error": err,
                "body": body,
            },
        )
        meta = request_meta_for_jsonl(
            kind=kind,
            url=url,
            http_status=status,
            headers=headers,
            error=err,
            body=body,
            raw_path=raw_path,
            inline_raw=inline_raw,
        )
        if source == SOURCE_ODDSAPIIO and status == 429:
            meta["rate_limited_until"] = self._note_rate_limit(headers)
        return status, body, hdrs, err, meta

    def _oddsapiio_events_catalog(
        self,
        *,
        url: str,
        snapshot_ctx: dict[str, Any],
    ) -> tuple[int | None, Any, dict[str, str], str | None, dict[str, Any], int]:
        """Return one process-shared football catalog; only one caller refreshes it."""
        with self._events_catalog_lock:
            now_mono = time.monotonic()
            age = now_mono - self._events_catalog_fetched_mono
            if (
                self._events_catalog_body is not None
                and age <= self.events_catalog_ttl_s
            ):
                meta: dict[str, Any] = {
                    "kind": "events",
                    "url": redact_url(url),
                    "http_status": self._events_catalog_status,
                    "headers": {},
                    "cache_hit": True,
                    "catalog_generation": self._events_catalog_generation,
                    "catalog_fetched_at": self._events_catalog_fetched_at,
                    "catalog_age_s": round(max(0.0, age), 3),
                }
                if self._events_catalog_raw_path:
                    meta["raw_path"] = self._events_catalog_raw_path
                return (
                    self._events_catalog_status,
                    self._events_catalog_body,
                    {},
                    None,
                    meta,
                    self._events_catalog_generation,
                )

            status, body, hdrs, err, meta = self._record_http(
                source=SOURCE_ODDSAPIIO,
                kind="events",
                url=url,
                inline_raw=False,
                snapshot_ctx=snapshot_ctx,
            )
            if err is None and body is not None:
                self._events_catalog_body = body
                self._events_catalog_fetched_mono = time.monotonic()
                self._events_catalog_fetched_at = lib.now_cn_iso()
                self._events_catalog_raw_path = str(meta.get("raw_path") or "") or None
                self._events_catalog_status = status
                self._events_catalog_generation += 1
                self._events_negative_cache.clear()
            meta["cache_hit"] = False
            meta["catalog_generation"] = self._events_catalog_generation
            return status, body, hdrs, err, meta, self._events_catalog_generation

    def _default_fetch_oddspapi(self, match_id: str, home: str, away: str) -> dict[str, Any]:
        t0 = time.perf_counter()
        key = self.keys.get(SOURCE_ODDSPAPI) or ""
        if not key:
            return {"ok": False, "error": "missing_key"}
        requests_meta: list[dict[str, Any]] = []
        cache = self._cache_entry(match_id)
        fixture_id = cache.get("oddspapi_fixture_id")
        mapped = bool(fixture_id)
        if not fixture_id:
            # OddsPapi requires sportId / from / to (not sport=football).
            now = datetime.now(timezone.utc)
            url = self._url_with_key(
                ODDSPAPI_BASE,
                "/fixtures",
                {
                    "sportId": 10,
                    "from": (now - timedelta(hours=18)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "to": (now + timedelta(hours=18)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                },
                key,
            )
            status, body, _hdrs, err, meta = self._record_http(
                source=SOURCE_ODDSPAPI,
                kind="fixtures",
                url=url,
                inline_raw=False,
            )
            requests_meta.append(meta)
            if err:
                return {
                    "ok": False,
                    "error": err,
                    "http_status": status,
                    "requests": requests_meta,
                }
            rows = [r for r in _as_list(body) if isinstance(r, dict)]
            resolved = resolve_team_match(rows, home=home, away=away, min_side=self.min_side_sim)
            if not resolved.get("ok"):
                return {
                    "ok": False,
                    "error": resolved.get("error") or "not_mapped",
                    "candidates": resolved.get("candidates"),
                    "requests": requests_meta,
                }
            fixture_id = resolved.get("id")
            self._update_cache_entry(
                match_id,
                {
                    "oddspapi_fixture_id": fixture_id,
                    "home": home,
                    "away": away,
                    "mapped_at": lib.now_cn_iso(),
                },
            )
            mapped = False

        books_param = ",".join(self.oddspapi_books)
        odds_url = self._url_with_key(
            ODDSPAPI_BASE,
            "/odds",
            {"fixtureId": fixture_id, "bookmakers": books_param},
            key,
        )
        status, body, _hdrs, err, meta = self._record_http(
            source=SOURCE_ODDSPAPI,
            kind="odds",
            url=odds_url,
            inline_raw=True,
        )
        requests_meta.append(meta)
        latency_ms = int((time.perf_counter() - t0) * 1000)
        if err:
            return {
                "ok": False,
                "error": err,
                "http_status": status,
                "fixture_id": fixture_id,
                "latency_ms": latency_ms,
                "requests": requests_meta,
                "raw": meta.get("raw"),
                "raw_path": meta.get("raw_path"),
            }
        books = parse_oddspapi_books(body, wanted_books=self.oddspapi_books, home=home, away=away)
        return {
            "ok": True,
            "fixture_id": fixture_id,
            "from_cache": mapped,
            "books": books,
            "latency_ms": latency_ms,
            "http_status": status,
            "requests": requests_meta,
            "raw": meta.get("raw"),
            "raw_path": meta.get("raw_path"),
        }

    def _oddsapiio_odds_coalesced(
        self,
        *,
        event_id: str,
        books_param: str,
        snapshot_ctx: dict[str, Any],
    ) -> tuple[int | None, Any, dict[str, str], str | None, dict[str, Any]]:
        """Batch concurrent odds pulls into ``GET /odds/multi`` (≤10 = 1 request).

        Callers must wait on the snapshot/worker thread — not on ``_http_pool`` —
        so the dedicated flush thread can run ``_record_http`` without deadlocking.
        """
        fut: Future = Future()
        with self._odds_multi_lock:
            self._odds_multi_pending.append(
                (str(event_id), books_param, dict(snapshot_ctx or {}), fut)
            )
            if not self._odds_multi_flush_scheduled:
                self._odds_multi_flush_scheduled = True
                threading.Thread(
                    target=self._odds_multi_flush_worker,
                    name="odds-multi-flush",
                    daemon=True,
                ).start()
        return fut.result(timeout=max(5.0, self.http_timeout_s + 5.0))

    def _odds_multi_flush_worker(self) -> None:
        in_flight: list[tuple[str, str, dict[str, Any], Future]] = []
        try:
            while not self._stop.is_set():
                time.sleep(self._odds_multi_window_s)
                with self._odds_multi_lock:
                    if not self._odds_multi_pending:
                        self._odds_multi_flush_scheduled = False
                        return
                    batch = self._odds_multi_pending[:ODDS_API_IO_MULTI_MAX]
                    del self._odds_multi_pending[:ODDS_API_IO_MULTI_MAX]
                in_flight = batch
                self._execute_odds_multi_batch(batch)
                in_flight = []
        except Exception as e:  # noqa: BLE001
            logger.exception("odds multi flush failed: %s", e)
            with self._odds_multi_lock:
                self._odds_multi_flush_scheduled = False
                stranded = list(self._odds_multi_pending)
                self._odds_multi_pending.clear()
            for _eid, _books, _ctx, fut in (*in_flight, *stranded):
                if not fut.done():
                    fut.set_exception(e)

    def _execute_odds_multi_batch(
        self,
        batch: list[tuple[str, str, dict[str, Any], Future]],
    ) -> None:
        if not batch:
            return
        key = self.keys.get(SOURCE_ODDSAPIIO) or ""
        books_param = batch[0][1]
        event_ids = list(dict.fromkeys(eid for eid, _b, _c, _f in batch))
        snapshot_ctx = next((ctx for _e, _b, ctx, _f in batch if ctx), {})
        url = self._url_with_key(
            ODDS_API_IO_BASE,
            "/odds/multi",
            {"eventIds": ",".join(event_ids), "bookmakers": books_param},
            key,
        )
        status, body, hdrs, err, meta = self._record_http(
            source=SOURCE_ODDSAPIIO,
            kind="odds_multi",
            url=url,
            inline_raw=False,
            snapshot_ctx=snapshot_ctx,
        )
        by_id: dict[str, Any] = {}
        if isinstance(body, list):
            for item in body:
                if isinstance(item, dict) and item.get("id") is not None:
                    by_id[str(item.get("id"))] = item
        elif isinstance(body, dict) and body.get("id") is not None:
            by_id[str(body.get("id"))] = body

        for event_id, _books, _ctx, fut in batch:
            if fut.done():
                continue
            if err:
                fut.set_result((status, None, hdrs, err, dict(meta)))
                continue
            item = by_id.get(str(event_id))
            if item is None:
                item_meta = dict(meta)
                item_meta["kind"] = "odds"
                item_meta["multi"] = True
                item_meta["event_id"] = str(event_id)
                item_meta["error"] = "missing_in_multi"
                fut.set_result(
                    (status, None, hdrs, "missing_in_multi", item_meta)
                )
                continue
            item_meta = dict(meta)
            item_meta["kind"] = "odds"
            item_meta["multi"] = True
            item_meta["event_id"] = str(event_id)
            item_meta["raw"] = item
            fut.set_result((status, item, hdrs, None, item_meta))

    def _default_fetch_oddsapiio(self, match_id: str, home: str, away: str) -> dict[str, Any]:
        t0 = time.perf_counter()
        snapshot_ctx = self._current_snap_ctx()
        key = self.keys.get(SOURCE_ODDSAPIIO) or ""
        if not key:
            return {"ok": False, "error": "missing_key"}
        requests_meta: list[dict[str, Any]] = []
        cache = self._cache_entry(match_id)
        mapping_ctx = self._mapping_context(match_id)
        event_id = cache.get("oddsapiio_event_id")
        from_cache = bool(event_id)
        swapped = bool(cache.get("oddsapiio_swapped"))
        if not event_id:
            url = self._url_with_key(ODDS_API_IO_BASE, "/events", {"sport": "football"}, key)
            status, body, _hdrs, err, meta, catalog_generation = (
                self._oddsapiio_events_catalog(
                    url=url,
                    snapshot_ctx=snapshot_ctx,
                )
            )
            requests_meta.append(meta)
            if err:
                out = {
                    "ok": False,
                    "error": err,
                    "http_status": status,
                    "requests": requests_meta,
                }
                if meta.get("rate_limited_until"):
                    out["rate_limited_until"] = meta["rate_limited_until"]
                return out
            with self._events_catalog_lock:
                negative_hit = (
                    self._events_negative_cache.get(match_id) == catalog_generation
                )
            if negative_hit:
                return {
                    "ok": False,
                    "error": "not_mapped",
                    "mapping_cache": "negative",
                    "catalog_generation": catalog_generation,
                    "requests": requests_meta,
                }
            rows = [r for r in _as_list(body) if isinstance(r, dict)]
            resolved = resolve_team_match(
                rows,
                home=home,
                away=away,
                min_side=self.min_side_sim,
                kickoff_at=mapping_ctx.get("kickoff_at"),
                league=str(mapping_ctx.get("league") or ""),
                require_nonterminal=True,
            )
            if not resolved.get("ok"):
                with self._events_catalog_lock:
                    self._events_negative_cache[match_id] = catalog_generation
                return {
                    "ok": False,
                    "error": resolved.get("error") or "not_mapped",
                    "candidates": resolved.get("candidates"),
                    "catalog_generation": catalog_generation,
                    "requests": requests_meta,
                }
            event_id = resolved.get("id")
            swapped = bool(resolved.get("swapped"))
            self._update_cache_entry(
                match_id,
                {
                    "oddsapiio_event_id": event_id,
                    "oddsapiio_swapped": swapped,
                    "oddsapiio_event_date": resolved.get("event_date"),
                    "oddsapiio_event_league": resolved.get("event_league"),
                    "oddsapiio_event_home": resolved.get("event_home"),
                    "oddsapiio_event_away": resolved.get("event_away"),
                    "home": home,
                    "away": away,
                    "mapped_at": lib.now_cn_iso(),
                },
            )
            from_cache = False

        books_param = ",".join(self.oddsapiio_books)
        # Fresh score/clock live on /events/{id}; markets via coalesced /odds/multi.
        # Coalesce waits on this (snapshot) thread; only the event GET uses _http_pool.
        event_url = self._url_with_key(ODDS_API_IO_BASE, f"/events/{event_id}", {}, key)

        def _pull_event() -> tuple[Any, ...]:
            return self._record_http(
                source=SOURCE_ODDSAPIIO,
                kind="event",
                url=event_url,
                inline_raw=False,
                snapshot_ctx=snapshot_ctx,
            )

        fut_event = self._http_pool.submit(_pull_event)
        try:
            status, body, _hdrs, err, meta = self._oddsapiio_odds_coalesced(
                event_id=str(event_id),
                books_param=books_param,
                snapshot_ctx=snapshot_ctx,
            )
        except Exception as e:  # noqa: BLE001
            status, body, _hdrs, err, meta = None, None, {}, str(e), {
                "kind": "odds",
                "multi": True,
                "error": str(e),
            }
        try:
            ev_status, ev_body, _ev_hdrs, ev_err, ev_meta = fut_event.result()
        except Exception as e:  # noqa: BLE001
            ev_status, ev_body, ev_err, ev_meta = None, None, str(e), {}

        requests_meta.append(meta)
        if isinstance(ev_meta, dict) and ev_meta:
            requests_meta.append(ev_meta)

        event_meta = parse_oddsapiio_event_meta(ev_body) if not ev_err else {}
        # A cached orientation is useful for decoding the payload, but it is
        # not proof that this poll still points at the intended live fixture.
        # Only the fresh /events/{id} response may authorize an A/B grade.
        identity_verified = False
        identity_error: str | None = None
        if isinstance(ev_body, dict) and (
            event_meta.get("event_home") or event_meta.get("event_away")
        ):
            identity = resolve_team_match(
                [ev_body],
                home=home,
                away=away,
                min_side=self.min_side_sim,
                kickoff_at=mapping_ctx.get("kickoff_at"),
                league=str(mapping_ctx.get("league") or ""),
                require_nonterminal=True,
            )
            if identity.get("ok") and str(identity.get("id") or "") == str(event_id):
                swapped = bool(identity.get("swapped"))
                identity_verified = True
                self._update_cache_entry(
                    match_id,
                    {
                        "oddsapiio_swapped": swapped,
                        "oddsapiio_event_date": identity.get("event_date"),
                        "oddsapiio_event_league": identity.get("event_league"),
                        "oddsapiio_event_home": identity.get("event_home"),
                        "oddsapiio_event_away": identity.get("event_away"),
                    },
                )
            else:
                identity_verified = False
                identity_error = "event_identity_mismatch"
                self._clear_oddsapiio_mapping(match_id)
        raw_provider_score = (
            dict(event_meta.get("score"))
            if isinstance(event_meta.get("score"), dict)
            else None
        )
        if raw_provider_score is not None and swapped:
            event_meta["score"] = {
                "home": raw_provider_score.get("away"),
                "away": raw_provider_score.get("home"),
            }
        latency_ms = int((time.perf_counter() - t0) * 1000)
        if err:
            out_err: dict[str, Any] = {
                "ok": False,
                "error": err,
                "http_status": status,
                "event_id": event_id,
                "latency_ms": latency_ms,
                "requests": requests_meta,
                "raw": meta.get("raw"),
                "raw_path": meta.get("raw_path"),
                "identity_verified": identity_verified,
                "orientation": "swapped" if swapped else "same",
            }
            if raw_provider_score is not None:
                out_err["provider_score_raw"] = raw_provider_score
            if event_meta.get("score") is not None:
                out_err["score"] = event_meta["score"]
            if event_meta.get("clock") is not None:
                out_err["clock"] = event_meta["clock"]
            if event_meta.get("periods") is not None:
                out_err["periods"] = event_meta["periods"]
            if event_meta.get("event_status") is not None:
                out_err["event_status"] = event_meta["event_status"]
            if ev_err:
                out_err["score_error"] = ev_err
            elif identity_error:
                out_err["score_error"] = identity_error
            elif ev_status is not None:
                out_err["score_http_status"] = ev_status
            if meta.get("rate_limited_until"):
                out_err["rate_limited_until"] = meta["rate_limited_until"]
            return out_err
        books = parse_oddsapiio_books(body, wanted_books=self.oddsapiio_books, home=home, away=away)
        if not books:
            books = [
                _with_observe_only({"book": b, "status": "missing"})
                for b in self.oddsapiio_books
            ]
        out = {
            "ok": True,
            "event_id": event_id,
            "from_cache": from_cache,
            "books": books,
            "latency_ms": latency_ms,
            "http_status": status,
            "requests": requests_meta,
            "raw": meta.get("raw"),
            "raw_path": meta.get("raw_path"),
            "identity_verified": identity_verified,
            "orientation": "swapped" if swapped else "same",
        }
        if raw_provider_score is not None:
            out["provider_score_raw"] = raw_provider_score
        if event_meta.get("score") is not None:
            out["score"] = event_meta["score"]
        if event_meta.get("clock") is not None:
            out["clock"] = event_meta["clock"]
        if event_meta.get("periods") is not None:
            out["periods"] = event_meta["periods"]
        if event_meta.get("event_status") is not None:
            out["event_status"] = event_meta["event_status"]
        elif isinstance(body, dict) and body.get("status") is not None:
            out["event_status"] = body.get("status")
        if ev_err:
            out["score_error"] = ev_err
        elif identity_error:
            out["score_error"] = identity_error
        elif event_meta.get("score") is None and not ev_err:
            # Event call ok but no scores object (pre-match / provider gap).
            out["score_error"] = "score_missing"
        return out

    def _theodds_note_credits(self, hdrs: dict[str, str], credits: dict[str, Any]) -> None:
        if hdrs.get("x-requests-remaining") is not None:
            credits["requests_remaining"] = hdrs.get("x-requests-remaining")
        if hdrs.get("x-requests-used") is not None:
            credits["requests_used"] = hdrs.get("x-requests-used")
        if hdrs.get("x-requests-last") is not None:
            credits["requests_last"] = hdrs.get("x-requests-last")

    def _theodds_search_events(
        self,
        *,
        api_key: str,
        home: str,
        away: str,
        sport_keys: list[str],
        requests_meta: list[dict[str, Any]],
        credits: dict[str, Any],
    ) -> tuple[str | None, str | None]:
        """Walk sport keys until team fuzzy match; return (sport_key, event_id)."""
        for sk in sport_keys:
            if not sk:
                continue
            ev_url = self._url_with_key(THE_ODDS_API_BASE, f"/sports/{sk}/events", {}, api_key)
            status, body, hdrs, err, meta = self._record_http(
                source=SOURCE_THEODDSAPI,
                kind=f"events:{sk}",
                url=ev_url,
                inline_raw=False,
            )
            requests_meta.append(meta)
            self._theodds_note_credits(hdrs, credits)
            if err:
                continue
            rows = [r for r in _as_list(body) if isinstance(r, dict)]
            resolved = resolve_team_match(rows, home=home, away=away, min_side=self.min_side_sim)
            if resolved.get("ok") and resolved.get("id"):
                return sk, str(resolved.get("id"))
        return None, None

    def _theodds_discovered_soccer_keys(
        self,
        *,
        api_key: str,
        requests_meta: list[dict[str, Any]],
        credits: dict[str, Any],
    ) -> list[str]:
        """Cache active soccer_* keys from GET /sports (TTL)."""
        now = time.monotonic()
        with self._lock:
            cached = self._theodds_sports_keys
            fetched_at = self._theodds_sports_fetched_at
            fresh = (
                cached is not None
                and (now - fetched_at) < THE_ODDS_SPORTS_CACHE_TTL_S
            )
            if fresh:
                return list(cached or [])

        sports_url = self._url_with_key(THE_ODDS_API_BASE, "/sports", {}, api_key)
        _status, body, hdrs, err, meta = self._record_http(
            source=SOURCE_THEODDSAPI,
            kind="sports",
            url=sports_url,
            inline_raw=False,
        )
        requests_meta.append(meta)
        self._theodds_note_credits(hdrs, credits)
        if err:
            return []
        keys = soccer_sport_keys_from_sports_payload(body)
        with self._lock:
            self._theodds_sports_keys = keys
            self._theodds_sports_fetched_at = time.monotonic()
        return list(keys)

    def _default_fetch_theoddsapi(self, match_id: str, home: str, away: str) -> dict[str, Any]:
        t0 = time.perf_counter()
        key = self.keys.get(SOURCE_THEODDSAPI) or ""
        if not key:
            return {"ok": False, "error": "missing_key"}
        requests_meta: list[dict[str, Any]] = []
        cache = self._cache_entry(match_id)
        sport_key = cache.get("theoddsapi_sport_key")
        event_id = cache.get("theoddsapi_event_id")
        from_cache = bool(sport_key and event_id)
        credits: dict[str, Any] = {}

        if not (sport_key and event_id):
            preferred = [str(sk) for sk in self.theoddsapi_sport_keys if sk]
            sport_key, event_id = self._theodds_search_events(
                api_key=key,
                home=home,
                away=away,
                sport_keys=preferred,
                requests_meta=requests_meta,
                credits=credits,
            )
            if not (sport_key and event_id) and self.theoddsapi_discover:
                discovered = self._theodds_discovered_soccer_keys(
                    api_key=key,
                    requests_meta=requests_meta,
                    credits=credits,
                )
                tried = set(preferred)
                extra = [sk for sk in discovered if sk not in tried]
                if extra:
                    sport_key, event_id = self._theodds_search_events(
                        api_key=key,
                        home=home,
                        away=away,
                        sport_keys=extra,
                        requests_meta=requests_meta,
                        credits=credits,
                    )
            if not (sport_key and event_id):
                out_miss: dict[str, Any] = {
                    "ok": False,
                    "error": "not_mapped",
                    "requests": requests_meta,
                }
                if credits:
                    out_miss["credits"] = credits
                return out_miss
            self._update_cache_entry(
                match_id,
                {
                    "theoddsapi_sport_key": sport_key,
                    "theoddsapi_event_id": event_id,
                    "home": home,
                    "away": away,
                    "mapped_at": lib.now_cn_iso(),
                },
            )
            from_cache = False

        regions = ",".join(self.theoddsapi_regions)
        odds_url = self._url_with_key(
            THE_ODDS_API_BASE,
            f"/sports/{sport_key}/events/{event_id}/odds",
            {"markets": "h2h", "regions": regions},
            key,
        )
        status, body, hdrs, err, meta = self._record_http(
            source=SOURCE_THEODDSAPI,
            kind="odds",
            url=odds_url,
            inline_raw=True,
        )
        requests_meta.append(meta)
        latency_ms = int((time.perf_counter() - t0) * 1000)
        self._theodds_note_credits(hdrs, credits)
        if err:
            out: dict[str, Any] = {
                "ok": False,
                "error": err,
                "http_status": status,
                "sport_key": sport_key,
                "event_id": event_id,
                "latency_ms": latency_ms,
                "requests": requests_meta,
                "raw": meta.get("raw"),
                "raw_path": meta.get("raw_path"),
            }
            if credits:
                out["credits"] = credits
            return out
        books = parse_theoddsapi_books(body, home=home, away=away)
        if not books:
            books = [{"book": "any", "status": "missing"}]
        out = {
            "ok": True,
            "sport_key": sport_key,
            "event_id": event_id,
            "from_cache": from_cache,
            "books": books,
            "latency_ms": latency_ms,
            "http_status": status,
            "requests": requests_meta,
            "raw": meta.get("raw"),
            "raw_path": meta.get("raw_path"),
        }
        if credits:
            out["credits"] = credits
        return out

    def _fetch_all_sources(self, match_id: str, home: str, away: str) -> dict[str, Any]:
        if SOURCE_ODDSAPIIO not in self.active_sources:
            return {}
        fn = self._fetch_oddsapiio or self._default_fetch_oddsapiio
        try:
            result = fn(match_id, home, away)
        except Exception as e:  # noqa: BLE001
            result = {"ok": False, "error": str(e)}
        return {SOURCE_ODDSAPIIO: result}

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
        poll_offset_s: float | None,
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
                poll_offset_s=poll_offset_s,
            )
        except Exception as e:  # noqa: BLE001
            logger.exception("book-context snapshot failed phase=%s match=%s", phase, match_id)
            try:
                lib.append_jsonl_async(
                    observe_path(self.root),
                    [
                        {
                            "quoted_at": lib.now_cn_iso(),
                            "phase": phase,
                            "observe_group_id": observe_group_id,
                            "match_id": match_id,
                            "event_key": event_key,
                            "home": home,
                            "away": away,
                            "dqd_score": dqd_score,
                            "dqd_prev": dqd_prev,
                            "sources": {},
                            "summary": {},
                            "poll": {
                                "offset_s": poll_offset_s,
                                "interval_s": self.poll_interval_s,
                                "timeout_s": self.poll_timeout_s,
                            },
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
        poll_offset_s: float | None,
    ) -> None:
        if self._stop.is_set():
            return
        if phase != PHASE_DQD_REVERSAL:
            with self._lock:
                cur = self._by_match.get(match_id)
                if cur is None or cur.gen != gen or cur.reversed:
                    return

        self._begin_snap_ctx(
            phase=phase,
            observe_group_id=observe_group_id,
            match_id=match_id,
            event_key=event_key,
        )
        sources = self._fetch_all_sources(match_id, home, away)
        summary = summarize_sources(sources)
        odds_source = sources.get(SOURCE_ODDSAPIIO)
        grade = grade_oddsapiio_sample(
            odds_source,
            home_score=dqd_score.get("home"),
            away_score=dqd_score.get("away"),
        )
        fingerprint = odds_sample_fingerprint(odds_source)
        data_changed = False
        upgrade_emitted = False
        previous_grade = "C"
        if phase != PHASE_DQD_REVERSAL:
            with self._lock:
                cur = self._by_match.get(match_id)
                if cur is None or cur.gen != gen or cur.reversed:
                    return
                previous_grade = cur.highest_grade
                data_changed = (
                    cur.last_fingerprint is not None and cur.last_fingerprint != fingerprint
                )
                cur.last_fingerprint = fingerprint
                level = str(grade.get("level") or "C")
                rank = GRADE_RANK.get(level, 0)
                prior_rank = GRADE_RANK.get(cur.highest_grade, 0)
                should_emit = rank > prior_rank
                if rank > prior_rank:
                    cur.highest_grade = level
                if should_emit:
                    upgrade_emitted = True
                    self._upgrades.append(
                        {
                            "quoted_at": lib.now_cn_iso(),
                            "observe_group_id": cur.observe_group_id,
                            "generation": cur.gen,
                            "match_id": cur.match_id,
                            "event_key": cur.event_key,
                            "ev": dict(cur.ev),
                            "af_gate": dict(cur.af_gate),
                            "odds_grade": dict(grade),
                            "poll_offset_s": poll_offset_s,
                            "data_changed": data_changed,
                            "fingerprint": fingerprint,
                        }
                    )
        else:
            with self._lock:
                cur = self._by_match.get(match_id)
                if cur is not None and cur.gen == gen:
                    previous_grade = cur.highest_grade
                    data_changed = (
                        cur.last_fingerprint is not None
                        and cur.last_fingerprint != fingerprint
                    )
                    cur.last_fingerprint = fingerprint
        errors: dict[str, Any] = {}
        for name, payload in sources.items():
            if isinstance(payload, dict) and payload.get("error") and not payload.get("ok"):
                errors[name] = payload.get("error")

        row: dict[str, Any] = {
            "quoted_at": lib.now_cn_iso(),
            "phase": phase,
            "observe_group_id": observe_group_id,
            "match_id": match_id,
            "event_key": event_key,
            "home": home,
            "away": away,
            "dqd_score": dqd_score,
            "sources": compact_sources_for_jsonl(sources),
            "summary": summary,
            "poll": {
                "offset_s": poll_offset_s,
                "interval_s": self.poll_interval_s,
                "timeout_s": self.poll_timeout_s,
            },
            "odds_grade": grade,
            "previous_highest_grade": previous_grade,
            "fingerprint": fingerprint,
            "data_changed": data_changed,
            "upgrade_emitted": upgrade_emitted,
        }
        if dqd_prev is not None:
            row["dqd_prev"] = dqd_prev
        if unlinked_reversal:
            row["unlinked_reversal"] = True
        if errors:
            row["error"] = errors
        lib.append_jsonl_async(observe_path(self.root), [row])

    def drain_upgrades(self) -> list[dict[str, Any]]:
        """Return only upgrades that still belong to a live, non-reversed goal."""
        with self._lock:
            queued, self._upgrades = self._upgrades, []
            out: list[dict[str, Any]] = []
            now_mono = time.monotonic()
            for item in queued:
                cur = self._by_match.get(str(item.get("match_id") or ""))
                if cur is None or cur.reversed:
                    continue
                if cur.gen != item.get("generation"):
                    continue
                if cur.observe_group_id != item.get("observe_group_id"):
                    continue
                if float(item.get("retry_after_mono") or 0.0) > now_mono:
                    self._upgrades.append(item)
                    continue
                out.append(item)
            return out

    def acknowledge_upgrade(self, item: dict[str, Any], *, success: bool) -> None:
        """Ack a quote attempt; failed attempts are retried without a new grade edge."""
        if success:
            return
        with self._lock:
            mid = str(item.get("match_id") or "")
            cur = self._by_match.get(mid)
            if cur is None or cur.reversed:
                return
            if cur.gen != item.get("generation"):
                return
            if cur.observe_group_id != item.get("observe_group_id"):
                return
            retry = dict(item)
            retry["retry_count"] = int(item.get("retry_count") or 0) + 1
            retry["retry_after_mono"] = time.monotonic() + max(
                1.0, min(5.0, self.poll_interval_s)
            )
            self._upgrades.append(retry)
