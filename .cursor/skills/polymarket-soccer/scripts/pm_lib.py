#!/usr/bin/env python3
"""Polymarket Gamma API helpers: soccer leagues, events, Beijing kickoff times."""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import threading
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable
from urllib.parse import urlparse

GAMMA_HOST = "gamma-api.polymarket.com"
GAMMA_BASE = f"https://{GAMMA_HOST}"
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# Default local proxy (Shadowrocket / MacPacket system HTTP on 1082).
# SOCKS5 also works on the same port when enabled; override with PM_PROXY.
DEFAULT_PROXY = "http://127.0.0.1:1082"

# Some Shadowrocket rules return CONNECT 503 for the gamma hostname but allow
# the Cloudflare anycast IPs. Keep a small fallback list + live DoH lookup.
_GAMMA_IP_FALLBACK = ("104.18.34.205", "172.64.153.51")
_GAMMA_IPS_CACHE: list[str] | None = None

TZ_CN = timezone(timedelta(hours=8))

_PROXY_APPLIED: str | None = None
_PROXY_LOCK = threading.RLock()
_ORIG_SOCKET = socket.socket

# Gamma /sports tag for football matchups (polymarket.com/.../sports/soccer/games).
SOCCER_TAG = "100350"

# Legacy allowlist kept for docs / explicit --league shortcuts. Catalog is dynamic via SOCCER_TAG.
SOCCER_SPORTS = frozenset(
    {
        "epl",
        "ucl",
        "uel",
        "mls",
        "lal",
        "bun",
        "fl1",
        "sea",
        "afc",
        "caf",
        "efa",
        "fifa",
        "fifaw",
        "fifwc",
        "nor",
        "swe",
        "kor",
        "bra",
        "mex",
        "nwsl",
        "wwcquefa",
    }
)

VS_RE = re.compile(
    r"^\s*(?:(?P<prefix>[^:]+):\s*)?(?P<home>.+?)\s+(?:vs\.?|v)\s+(?P<away>.+?)\s*$",
    re.IGNORECASE,
)

# Prop / derivative markets on the same fixture (keep only the main moneyline matchup).
PROP_SUFFIX_RE = re.compile(
    r"\s+-\s+(More Markets|Halftime|Second Half|Exact Score|First Team|"
    r"Spread|Total|Corners|BTTS|Both Teams|Goalscorer|Cards|Result).*$",
    re.IGNORECASE,
)

LEAGUE_NAMES = {
    "epl": "EPL",
    "ucl": "UCL",
    "uel": "UEL",
    "mls": "MLS",
    "lal": "La Liga",
    "bun": "Bundesliga",
    "fl1": "Ligue 1",
    "sea": "Serie A",
    "afc": "AFC",
    "caf": "CAF",
    "efa": "EFA",
    "fifa": "FIFA",
    "fifaw": "FIFA Women",
    "fifwc": "FIFA World Cup",
    "nor": "Eliteserien",
    "swe": "Allsvenskan",
    "kor": "K League",
    "bra": "Brasileirão",
    "mex": "Liga MX",
    "nwsl": "NWSL",
    "wwcquefa": "WWC Qualifiers (UEFA)",
}


class FetchError(RuntimeError):
    """Raised when Gamma API cannot be reached or returns invalid data."""


def resolve_proxy(explicit: str | None = None) -> str | None:
    """Return proxy URL. Default socks5h://127.0.0.1:1082. Use 'none'/'off'/'direct' to disable."""
    if explicit is not None:
        raw = explicit.strip()
    else:
        raw = (
            os.environ.get("PM_PROXY")
            or os.environ.get("ALL_PROXY")
            or os.environ.get("all_proxy")
            or DEFAULT_PROXY
        ).strip()
    if not raw or raw.lower() in ("none", "off", "direct", "0", "-"):
        return None
    if "://" not in raw:
        # bare host:port → SOCKS5 with remote DNS
        raw = f"socks5h://{raw}"
    return raw


def _apply_socks_proxy(proxy_url: str) -> None:
    """Route all sockets through SOCKS5 via PySocks."""
    global _PROXY_APPLIED
    try:
        import socks  # type: ignore
    except ImportError as e:
        raise FetchError(
            "SOCKS proxy requires PySocks. Install: pip3 install PySocks"
        ) from e

    u = urlparse(proxy_url)
    host = u.hostname or "127.0.0.1"
    port = u.port or 1082
    scheme = (u.scheme or "socks5h").lower()
    rdns = scheme in ("socks5h", "socks5a")
    if scheme not in ("socks5", "socks5h", "socks5a", "socks4"):
        raise FetchError(
            f"Unsupported SOCKS scheme '{u.scheme}'. Use socks5h://127.0.0.1:1082 "
            "or set PM_PROXY=http://127.0.0.1:1082 for HTTP proxy."
        )
    ptype = socks.SOCKS4 if scheme == "socks4" else socks.SOCKS5
    socks.set_default_proxy(ptype, host, port, rdns=rdns)
    socket.socket = socks.socksocket  # type: ignore[misc, assignment]
    _PROXY_APPLIED = proxy_url


def _apply_http_proxy(proxy_url: str) -> urllib.request.OpenerDirector:
    handler = urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url})
    opener = urllib.request.build_opener(handler)
    return opener


def configure_proxy(explicit: str | None = None) -> str | None:
    """Configure process-wide proxy. Returns the proxy URL used, or None if direct.

    Idempotent under a lock: same target skips socket reset so concurrent warmer
    + CLOB/Gamma threads do not tear down an in-flight SOCKS patch.
    """
    global _PROXY_APPLIED
    with _PROXY_LOCK:
        proxy = resolve_proxy(explicit)
        if proxy == _PROXY_APPLIED:
            if not proxy:
                return None
            scheme = (urlparse(proxy).scheme or "").lower()
            if scheme.startswith("socks"):
                if socket.socket is not _ORIG_SOCKET:
                    return proxy
            elif scheme in ("http", "https"):
                return proxy

        # Reset socket if previously patched
        socket.socket = _ORIG_SOCKET  # type: ignore[misc, assignment]
        _PROXY_APPLIED = None

        if not proxy:
            return None

        scheme = (urlparse(proxy).scheme or "").lower()
        if scheme.startswith("socks"):
            _apply_socks_proxy(proxy)
            return proxy
        if scheme in ("http", "https"):
            _PROXY_APPLIED = proxy
            return proxy
        raise FetchError(f"Unsupported proxy URL: {proxy}")


def resolve_gamma_ips(proxy: str | None = None) -> list[str]:
    """Return IPv4s for gamma-api (DoH via proxy when possible, else CF fallback)."""
    global _GAMMA_IPS_CACHE
    if _GAMMA_IPS_CACHE:
        return list(_GAMMA_IPS_CACHE)

    ips: list[str] = []
    doh = "https://1.1.1.1/dns-query?name=gamma-api.polymarket.com&type=A"
    cmd = [
        "curl",
        "-sS",
        "--max-time",
        "8",
        "-H",
        "accept: application/dns-json",
    ]
    if proxy:
        cmd.extend(["-x", proxy])
    cmd.append(doh)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if proc.returncode == 0 and proc.stdout:
            data = json.loads(proc.stdout)
            for ans in data.get("Answer") or []:
                if ans.get("type") == 1 and ans.get("data"):
                    ips.append(str(ans["data"]))
    except Exception:  # noqa: BLE001
        pass

    for ip in _GAMMA_IP_FALLBACK:
        if ip not in ips:
            ips.append(ip)
    _GAMMA_IPS_CACHE = ips
    return list(ips)


def _fetch_via_curl(
    url: str,
    proxy: str | None,
    timeout: float,
    *,
    connect_ip: str | None = None,
    force_noproxy: bool = False,
) -> str:
    cmd = [
        "curl",
        "-sS",
        "-L",
        "--max-time",
        str(int(timeout)),
        "-A",
        UA,
        "-H",
        "Accept: application/json",
        "-H",
        "Origin: https://polymarket.com",
        "-H",
        "Referer: https://polymarket.com/",
    ]
    if force_noproxy:
        cmd.extend(["--noproxy", "*"])
    elif proxy:
        cmd.extend(["-x", proxy])
    if connect_ip:
        # Bypass domain-based CONNECT/SOCKS failures (Shadowrocket 503 on hostname).
        cmd.extend(["--connect-to", f"{GAMMA_HOST}:443:{connect_ip}:443"])
        if proxy and (urlparse(proxy).scheme or "").lower() in ("socks5", "socks5h", "socks4"):
            # Prefer IP CONNECT over remote DNS for SOCKS when pinning.
            cmd.extend(["--resolve", f"{GAMMA_HOST}:443:{connect_ip}"])
    cmd.append(url)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    except FileNotFoundError as e:
        raise FetchError("curl not found for proxy fallback") from e
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()[:300]
        raise FetchError(f"curl failed ({proc.returncode}) for {url}: {err}")
    return proc.stdout


def _curl_gamma(url: str, proxy: str | None, timeout: float) -> str:
    """curl Gamma: hostname first, then Cloudflare IP pinning if needed."""
    errors: list[str] = []
    try:
        return _fetch_via_curl(url, proxy, timeout)
    except FetchError as e:
        errors.append(str(e))

    for ip in resolve_gamma_ips(proxy):
        try:
            return _fetch_via_curl(url, proxy, timeout, connect_ip=ip)
        except FetchError as e:
            errors.append(f"ip={ip}: {e}")
    raise FetchError("; ".join(errors))


def fetch_json(
    path: str,
    params: dict[str, Any] | None = None,
    timeout: float = 30.0,
    *,
    proxy: str | None | object = ...,
) -> Any:
    """
    GET JSON from Gamma API.

    proxy:
      ellipsis → use DEFAULT_PROXY / PM_PROXY (default http://127.0.0.1:1082)
      None / 'none' → direct
      str → explicit proxy URL
    """
    qs = urllib.parse.urlencode({k: v for k, v in (params or {}).items() if v is not None}, doseq=True)
    url = f"{GAMMA_BASE}{path}"
    if qs:
        url = f"{url}?{qs}"

    if proxy is ...:
        proxy_url = configure_proxy(None)
    else:
        proxy_url = configure_proxy(None if proxy is None else str(proxy))

    headers = {
        "User-Agent": UA,
        "Accept": "application/json",
        "Origin": "https://polymarket.com",
        "Referer": "https://polymarket.com/",
    }
    req = urllib.request.Request(url, headers=headers)
    scheme = (urlparse(proxy_url).scheme or "").lower() if proxy_url else ""
    errors: list[str] = []

    # Prefer curl: handles SOCKS + Shadowrocket hostname CONNECT 503 via IP pin.
    try:
        raw = _curl_gamma(url, proxy_url, timeout)
        return json.loads(raw)
    except FetchError as e:
        errors.append(str(e))
    except json.JSONDecodeError as e:
        raise FetchError(f"Invalid JSON from {url}") from e

    try:
        if proxy_url and scheme in ("http", "https"):
            opener = _apply_http_proxy(proxy_url)
            with opener.open(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8")
        elif proxy_url and scheme.startswith("socks"):
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8")
        else:
            # Force-disable env/system proxy leakage on macOS Python.
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8")
    except Exception as first_err:
        if isinstance(first_err, urllib.error.HTTPError):
            body = first_err.read().decode("utf-8", errors="replace")[:300]
            errors.append(f"HTTP {first_err.code}: {body}")
        elif isinstance(first_err, urllib.error.URLError):
            errors.append(str(first_err.reason))
        else:
            errors.append(str(first_err))
        raise FetchError(
            f"Network error calling {url} via proxy={proxy_url or 'direct'}: "
            f"{'; '.join(errors)}. "
            "Shadowrocket may block gamma-api hostname (CONNECT 503); "
            "skill retries via Cloudflare IP. Check proxy node / rules."
        ) from first_err

    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise FetchError(f"Invalid JSON from {url}") from e


def fetch_sports(*, proxy: str | None | object = ...) -> list[dict[str, Any]]:
    data = fetch_json("/sports", proxy=proxy)
    if not isinstance(data, list):
        raise FetchError("/sports did not return a list")
    return data


def _has_soccer_tag(tags: Any) -> bool:
    parts = [p.strip() for p in str(tags or "").replace(" ", "").split(",") if p.strip()]
    return SOCCER_TAG in parts


def soccer_league_catalog(
    sports: list[dict[str, Any]] | None = None,
    *,
    proxy: str | None | object = ...,
) -> list[dict[str, Any]]:
    """Return soccer leagues from /sports (tag 100350), matching the soccer/games page."""
    rows = sports if sports is not None else fetch_sports(proxy=proxy)
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for meta in rows:
        if not isinstance(meta, dict):
            continue
        if not _has_soccer_tag(meta.get("tags")):
            continue
        code = str(meta.get("sport") or "").strip().lower()
        series = str(meta.get("series") or "").strip()
        if not code or not series or code in seen:
            continue
        # Skip obvious non-matchup sports that sometimes share tags.
        if code.startswith("bk") or code.startswith("vb") or code.startswith("tt"):
            continue
        seen.add(code)
        out.append(
            {
                "id": code,
                "name": LEAGUE_NAMES.get(code, code.upper()),
                "sport": code,
                "series_id": series,
                "ordering": (meta.get("ordering") or "home").lower(),
                "tags": meta.get("tags") or "",
            }
        )
    out.sort(key=lambda c: (0 if c["id"] in SOCCER_SPORTS else 1, c["name"].lower(), c["id"]))
    return out


def is_matchup_title(title: str) -> bool:
    t = (title or "").strip()
    if not t or PROP_SUFFIX_RE.search(t) or " - " in t:
        return False
    return bool(VS_RE.match(t))


def parse_matchup(title: str, ordering: str = "home") -> tuple[str, str] | None:
    t = (title or "").strip()
    if not t or PROP_SUFFIX_RE.search(t) or " - " in t:
        return None
    m = VS_RE.match(t)
    if not m:
        return None
    a = m.group("home").strip()
    b = m.group("away").strip()
    if not a or not b:
        return None
    # ordering "home" means first team is home (Polymarket default).
    if ordering == "away":
        return b, a
    return a, b


def _parse_kickoff(raw: str | None) -> datetime | None:
    if not raw:
        return None
    s = str(raw).strip()
    if not s:
        return None
    # "2025-08-15 19:00:00+00" or ISO
    s2 = s.replace(" ", "T", 1) if "T" not in s else s
    if s2.endswith("+00"):
        s2 = s2[:-3] + "+00:00"
    try:
        dt = datetime.fromisoformat(s2)
    except ValueError:
        try:
            dt = datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def extract_game_start(event: dict[str, Any]) -> str | None:
    markets = event.get("markets") or []
    for m in markets:
        if not isinstance(m, dict):
            continue
        gst = m.get("gameStartTime") or m.get("game_start_time")
        if gst:
            return str(gst)
    # fallbacks
    for key in ("startTime", "eventDate", "startDate"):
        if event.get(key):
            return str(event.get(key))
    return None


def to_beijing(dt: datetime | None) -> tuple[str, str, str]:
    """Return (HH:MM, YYYY-MM-DD, YYYY-MM-DD HH:MM) in Asia/Shanghai."""
    if dt is None:
        return "", "", ""
    local = dt.astimezone(TZ_CN)
    return (
        local.strftime("%H:%M"),
        local.strftime("%Y-%m-%d"),
        local.strftime("%Y-%m-%d %H:%M"),
    )


def extract_market_refs(event: dict[str, Any]) -> list[dict[str, Any]]:
    """Slim market handles for CLOB / Gamma lookups by external consumers."""
    refs: list[dict[str, Any]] = []
    for m in event.get("markets") or []:
        if not isinstance(m, dict):
            continue
        mid = str(m.get("id") or m.get("conditionId") or m.get("condition_id") or "").strip()
        cid = str(m.get("conditionId") or m.get("condition_id") or "").strip()
        tokens = m.get("clobTokenIds") or m.get("clob_token_ids") or []
        if isinstance(tokens, str):
            try:
                tokens = json.loads(tokens)
            except json.JSONDecodeError:
                tokens = [tokens]
        if not isinstance(tokens, list):
            tokens = []
        refs.append(
            {
                "market_id": mid,
                "condition_id": cid,
                "question": (m.get("question") or m.get("groupItemTitle") or "")[:160],
                "clob_token_ids": [str(t) for t in tokens if t],
                "slug": m.get("slug") or "",
            }
        )
    return refs


def map_event(event: dict[str, Any], league: dict[str, Any]) -> dict[str, Any] | None:
    title = event.get("title") or ""
    parsed = parse_matchup(title, ordering=league.get("ordering") or "home")
    if not parsed:
        return None
    home, away = parsed
    start_raw = extract_game_start(event)
    dt = _parse_kickoff(start_raw)
    time_s, local_date, kickoff_beijing = to_beijing(dt)
    slug = event.get("slug") or ""
    market_refs = extract_market_refs(event)
    return {
        "id": str(event.get("id") or ""),
        "slug": slug,
        "league": league.get("name") or league.get("id") or "",
        "league_id": league.get("id") or "",
        "series_id": league.get("series_id") or "",
        "home": home,
        "away": away,
        "title": title,
        "start_play": start_raw or "",
        "time": time_s,
        "local_date": local_date,
        "kickoff_beijing": kickoff_beijing,
        "url": f"https://polymarket.com/event/{slug}" if slug else "",
        "active": bool(event.get("active")),
        "closed": bool(event.get("closed")),
        "market_refs": market_refs,
        "condition_ids": [r["condition_id"] for r in market_refs if r.get("condition_id")],
    }


# Default horizon for upcoming fixtures (server + local filter).
DEFAULT_WITHIN_HOURS = 48


def upcoming_window(within_hours: int | None = DEFAULT_WITHIN_HOURS) -> tuple[datetime, datetime] | None:
    """UTC [now, now+hours] window. None / <=0 disables the window."""
    if within_hours is None or within_hours <= 0:
        return None
    now = datetime.now(timezone.utc)
    return now, now + timedelta(hours=within_hours)


def _iso_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def in_kickoff_window(start_raw: str | None, window: tuple[datetime, datetime] | None) -> bool:
    """True if gameStartTime falls inside window. Includes games that started up to 3h ago (live)."""
    if window is None:
        return True
    dt = _parse_kickoff(start_raw)
    if dt is None:
        return False
    start, end = window
    return (start - timedelta(hours=3)) <= dt <= end


def fetch_events_for_series(
    series_id: str,
    *,
    include_closed: bool = False,
    page_size: int = 50,
    max_events: int = 80,
    proxy: str | None | object = ...,
) -> list[dict[str, Any]]:
    """
    Fetch events for a series.

    Note: Gamma `order=start_date` and `start_date_*` / `start_time_*` are unreliable
    for soccer (invalid order / empty results). Fetch open events and filter kickoff
    locally via markets[].gameStartTime.
    """
    out: list[dict[str, Any]] = []
    offset = 0
    while len(out) < max_events:
        limit = min(page_size, max_events - len(out))
        params: dict[str, Any] = {
            "series_id": series_id,
            "limit": limit,
            "offset": offset,
        }
        if not include_closed:
            params["closed"] = "false"
            params["active"] = "true"
        data = fetch_json("/events", params, proxy=proxy)
        if not isinstance(data, list) or not data:
            break
        out.extend(data)
        if len(data) < limit:
            break
        offset += len(data)
    return out


def load_matches(
    leagues: Iterable[str] | None = None,
    *,
    include_closed: bool = False,
    max_per_league: int = 80,
    within_hours: int | None = DEFAULT_WITHIN_HOURS,
    sports: list[dict[str, Any]] | None = None,
    proxy: str | None | object = ...,
) -> dict[str, Any]:
    proxy_url = configure_proxy(None if proxy is ... else (None if proxy is None else str(proxy)))
    catalog = soccer_league_catalog(sports, proxy=proxy_url if proxy_url else "none")
    if not catalog:
        raise FetchError("No soccer leagues found in /sports (tag 100350)")

    wanted = None
    if leagues:
        wanted = {x.strip().lower() for x in leagues if x.strip()}
        catalog = [c for c in catalog if c["id"] in wanted]
        missing = wanted - {c["id"] for c in catalog}
        if missing:
            raise FetchError(f"Unknown or unavailable league(s): {', '.join(sorted(missing))}")

    window = upcoming_window(within_hours)
    proxy_arg: str = proxy_url if proxy_url else "none"
    # Smaller page budget when scanning all soccer leagues for a short window.
    per_league = min(max_per_league, 40 if within_hours and within_hours > 0 else max_per_league)

    def _league_matches(league: dict[str, Any]) -> list[dict[str, Any]]:
        events = fetch_events_for_series(
            league["series_id"],
            include_closed=include_closed,
            max_events=per_league,
            proxy=proxy_arg,
        )
        rows: list[dict[str, Any]] = []
        for ev in events:
            if not in_kickoff_window(extract_game_start(ev), window):
                continue
            mapped = map_event(ev, league)
            if mapped:
                rows.append(mapped)
        return rows

    matches: list[dict[str, Any]] = []
    workers = min(12, max(4, len(catalog)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = [pool.submit(_league_matches, league) for league in catalog]
        for fut in as_completed(futs):
            matches.extend(fut.result())

    matches.sort(key=lambda m: (m.get("kickoff_beijing") or "9999", m.get("league_id") or "", m.get("home") or ""))

    counts: dict[str, int] = defaultdict(int)
    for m in matches:
        counts[m["league_id"]] += 1
    league_summary = [
        {
            "id": c["id"],
            "series_id": c["series_id"],
            "name": c["name"],
            "count": counts.get(c["id"], 0),
        }
        for c in catalog
    ]

    window_meta = None
    if window is not None:
        window_meta = {
            "within_hours": within_hours,
            "start_utc": _iso_z(window[0]),
            "end_utc": _iso_z(window[1]),
        }

    return {
        "fetched_at": datetime.now(TZ_CN).isoformat(timespec="seconds"),
        "source": "polymarket-gamma",
        "proxy": proxy_url or "direct",
        "window": window_meta,
        "count": len(matches),
        "leagues": league_summary,
        "matches": matches,
    }


def map_events_offline(
    events: list[dict[str, Any]],
    league: dict[str, Any],
) -> list[dict[str, Any]]:
    """Map a preloaded events list (for tests / fixtures)."""
    out = []
    for ev in events:
        m = map_event(ev, league)
        if m:
            out.append(m)
    return out
