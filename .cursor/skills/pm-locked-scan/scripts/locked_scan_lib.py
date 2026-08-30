#!/usr/bin/env python3
"""Scan finished-but-unsettled Polymarket soccer for locked-WIN asks.

Independent of polymarket-quote watch / trading. Reuses Gamma list helpers,
quote settlement + CLOB books, match-bridge name matching, and local
API-Football / Dongqiudi score files.
"""

from __future__ import annotations

import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

_SCRIPTS = Path(__file__).resolve().parent
_SKILLS = _SCRIPTS.parent.parent
_QUOTE = _SKILLS / "polymarket-quote" / "scripts"
_PM = _SKILLS / "polymarket-soccer" / "scripts"
_BRIDGE = _SKILLS / "match-bridge" / "scripts"
_AF = _SKILLS / "apifootball-bridge" / "scripts"
for _p in (_QUOTE, _PM, _BRIDGE, _AF):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import af_bridge_lib as af_lib  # noqa: E402
import bridge_lib as bl  # noqa: E402
import pm_lib as pm  # noqa: E402
import quote_lib as ql  # noqa: E402
from league_aliases import LEAGUE_ALIASES  # noqa: E402

TZ_CN = timezone(timedelta(hours=8))
REPO_ROOT = Path(__file__).resolve().parents[4]
DEFAULT_HOURS = 48
DEFAULT_MAX_SKEW_MIN = 90
DEFAULT_MIN_SIDE = 0.75
# Regulation is decided ~90'+stoppage; extra time still `live` on Gamma when honest.
MIN_FINISHED_AFTER_KICKOFF = timedelta(minutes=100)
DEFAULT_MAX_PER_LEAGUE = 120
DEFAULT_BOOK_TOP_N = 12
GAMMA_FINISHED_PERIODS = frozenset({"FT", "AET", "PEN", "AWD"})
# AF statuses where 90' is not safely on the board, or ET/penalties have started.
AF_BLOCKS_DQD_SHORT = frozenset(
    {
        "1H",
        "HT",
        "2H",
        "ET",
        "BT",
        "P",
        "PEN",
        "AET",
        "LIVE",
        "INT",
        "SUSP",
        "BREAK",
        "1ET",
        "2ET",
    }
)
_CUP_NAME_RE = re.compile(
    r"cup|copa|coppa|pokal|ta[cç]a|trophy|shield|knockout|play[- ]?off|"
    r"super\s*cup|world cup|champions league|europa league|conference league|"
    r"libertadores|sudamericana|\u676f",
    re.IGNORECASE,
)
_KNOCKOUT_LEAGUE_IDS = frozenset(
    {"ucl", "uel", "uecl", "fifwc", "fifaw", "fifa", "cafcl", "afccl"}
)


def repo_root() -> Path:
    return REPO_ROOT


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def now_cn_iso() -> str:
    return datetime.now(TZ_CN).isoformat(timespec="seconds")


def parse_int(v: Any) -> int | None:
    if v is None or v == "":
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def parse_kickoff(raw: str | None) -> datetime | None:
    """Parse a Gamma / snapshot kickoff string to aware UTC datetime."""
    return pm._parse_kickoff(raw)


def past_kickoff_window(hours: int, *, now: datetime | None = None) -> tuple[datetime, datetime]:
    """UTC window: kickoffs in the last ``hours`` (inclusive of now)."""
    n = now or now_utc()
    h = max(1, int(hours))
    return n - timedelta(hours=h), n


def in_past_window(start_raw: str | None, window: tuple[datetime, datetime]) -> bool:
    dt = parse_kickoff(start_raw)
    if dt is None:
        return False
    start, end = window
    return start <= dt <= end


def is_unsettled_open(event: dict[str, Any] | None) -> bool:
    if not isinstance(event, dict):
        return False
    if event.get("closed") is True:
        return False
    if event.get("live") is True:
        return False
    return True


def has_finished_signal(event: dict[str, Any] | None) -> bool:
    """Gamma says regulation (or AET/pen) has ended. Does not use wall clock."""
    if not isinstance(event, dict):
        return False
    if event.get("ended") is True:
        return True
    period = str(event.get("period") or "").strip().upper()
    return period in GAMMA_FINISHED_PERIODS


def elapsed_past_regulation(start_raw: str | None, *, now: datetime | None = None) -> bool:
    dt = parse_kickoff(start_raw)
    if dt is None:
        return False
    n = now or now_utc()
    return n >= dt + MIN_FINISHED_AFTER_KICKOFF


def is_finished_unsettled(event: dict[str, Any], start_raw: str | None, *, now: datetime | None = None) -> bool:
    """Listing candidate: markets still open and a finish hint is present.

    Wall clock (kickoff+100m) is only a *candidate* signal so stale Gamma
    ``ended=false`` after FT is not dropped. Confirmation is a regulation
    score from AF/DQD, not this helper.
    """
    if not is_unsettled_open(event):
        return False
    if has_finished_signal(event):
        return True
    return elapsed_past_regulation(start_raw, now=now)


def filter_soccer_catalog(catalog: list[dict[str, Any]], leagues: list[str] | None) -> list[dict[str, Any]]:
    """Return catalog rows for ``leagues``. Raises if a code is unknown."""
    if not leagues:
        return list(catalog)
    wanted = {x.strip().lower() for x in leagues if x.strip()}
    filtered = [c for c in catalog if c["id"] in wanted]
    missing = wanted - {c["id"] for c in filtered}
    if missing:
        raise pm.FetchError(f"Unknown or unavailable league(s): {', '.join(sorted(missing))}")
    return filtered


def _cup_codes() -> frozenset[str]:
    out: set[str] = set(_KNOCKOUT_LEAGUE_IDS)
    for key, val in LEAGUE_ALIASES.items():
        if _CUP_NAME_RE.search(str(key) or ""):
            out.add(str(val).strip().lower())
            out.add(str(key).strip().lower())
    return frozenset(x for x in out if x)


_CUP_CODES = _cup_codes()


def looks_like_cup(*, league_id: str = "", league: str = "", dqd_league: str = "") -> bool:
    """True for cups / knockout competitions where AET can leak into a FT score."""
    lid = str(league_id or "").strip().lower()
    if lid and lid in _CUP_CODES:
        return True
    for name in (league, dqd_league):
        if name and _CUP_NAME_RE.search(str(name)):
            return True
    return False


def _gamma_meta(event: dict[str, Any]) -> dict[str, Any]:
    return {
        "ended": event.get("ended"),
        "live": event.get("live"),
        "closed": bool(event.get("closed")),
        "score": event.get("score"),
        "period": event.get("period"),
        "elapsed": event.get("elapsed"),
    }


def list_unsettled_matches(
    *,
    hours: int = DEFAULT_HOURS,
    leagues: list[str] | None = None,
    max_per_league: int = DEFAULT_MAX_PER_LEAGUE,
    proxy: str | None | object = ...,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Fetch open Gamma soccer events whose kickoff is in the past ``hours`` and look finished."""
    proxy_url = pm.configure_proxy(None if proxy is ... else (None if proxy is None else str(proxy)))
    proxy_arg: str = proxy_url if proxy_url else "none"
    catalog = filter_soccer_catalog(pm.soccer_league_catalog(proxy=proxy_arg), leagues)
    window = past_kickoff_window(hours, now=now)
    n = now or now_utc()

    def _one(league: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
        try:
            events = pm.fetch_events_for_series(
                league["series_id"],
                include_closed=False,
                max_events=max_per_league,
                proxy=proxy_arg,
            )
        except Exception as e:  # noqa: BLE001
            return [], {"league_id": league.get("id"), "league": league.get("name"), "error": str(e)}
        rows: list[dict[str, Any]] = []
        for ev in events:
            start_raw = pm.extract_game_start(ev)
            if not in_past_window(start_raw, window):
                continue
            if not is_finished_unsettled(ev, start_raw, now=n):
                continue
            mapped = pm.map_event(ev, league)
            if not mapped:
                continue
            mapped["gamma"] = _gamma_meta(ev)
            rows.append(mapped)
        return rows, None

    matches: list[dict[str, Any]] = []
    league_errors: list[dict[str, Any]] = []
    workers = min(12, max(4, len(catalog) or 1))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = [pool.submit(_one, league) for league in catalog]
        for fut in as_completed(futs):
            rows, err = fut.result()
            if err:
                league_errors.append(err)
            matches.extend(rows)
    matches.sort(key=lambda r: r.get("kickoff_beijing") or "")
    return {
        "fetched_at": now_cn_iso(),
        "hours": hours,
        "window_utc": {
            "start": window[0].strftime("%Y-%m-%dT%H:%M:%SZ"),
            "end": window[1].strftime("%Y-%m-%dT%H:%M:%SZ"),
        },
        "count": len(matches),
        "matches": matches,
        "league_errors": league_errors,
        "proxy": proxy_url or "direct",
    }


def matches_from_snapshot(
    snapshot: dict[str, Any],
    *,
    hours: int = DEFAULT_HOURS,
    now: datetime | None = None,
    leagues: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Filter an existing PM snapshot (no Gamma list). Still need hydrate for ended/live."""
    window = past_kickoff_window(hours, now=now)
    n = now or now_utc()
    wanted = {x.strip().lower() for x in (leagues or []) if str(x).strip()} or None
    out: list[dict[str, Any]] = []
    for row in snapshot.get("matches") or []:
        if not isinstance(row, dict):
            continue
        if row.get("closed") is True:
            continue
        if wanted is not None:
            lid = str(row.get("league_id") or "").strip().lower()
            if lid not in wanted:
                continue
        start_raw = row.get("start_play") or row.get("kickoff_utc")
        if not start_raw and row.get("kickoff_beijing"):
            try:
                start_raw = (
                    datetime.strptime(str(row["kickoff_beijing"]), "%Y-%m-%d %H:%M")
                    .replace(tzinfo=TZ_CN)
                    .astimezone(timezone.utc)
                    .strftime("%Y-%m-%dT%H:%M:%SZ")
                )
            except ValueError:
                start_raw = None
        if not in_past_window(start_raw, window):
            continue
        gamma = row.get("gamma") if isinstance(row.get("gamma"), dict) else {}
        synthetic = {
            "closed": row.get("closed"),
            "live": row.get("live") if row.get("live") is not None else gamma.get("live"),
            "ended": row.get("ended") if row.get("ended") is not None else gamma.get("ended"),
            "period": row.get("period") or gamma.get("period"),
        }
        if not is_finished_unsettled(synthetic, start_raw, now=n):
            continue
        out.append(row)
    return out


def hydrate_match(
    row: dict[str, Any],
    *,
    proxy: str | None | object = ...,
) -> dict[str, Any]:
    """Attach main + More Markets + Exact Score Gamma events."""
    slug = str(row.get("slug") or "")
    home = str(row.get("home") or "")
    away = str(row.get("away") or "")
    main = ql.fetch_gamma_event(event_id=str(row.get("id") or "") or None, slug=slug or None, proxy=proxy)
    related = (
        ql.discover_related_events(slug=slug, home=home, away=away, proxy=proxy)
        if slug
        else {"more_markets": None, "exact_score": None}
    )
    if main:
        row = dict(row)
        row["gamma"] = _gamma_meta(main)
        if not row.get("home") or not row.get("away"):
            parsed = pm.parse_matchup(str(main.get("title") or ""))
            if parsed:
                row["home"], row["away"] = parsed
    return {
        "match": row,
        "main": main,
        "more_markets": related.get("more_markets"),
        "exact_score": related.get("exact_score"),
    }


def _enrich_event_markets(ev: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not ev:
        return []
    return [ql.enrich_market(m) for m in (ev.get("markets") or []) if isinstance(m, dict)]


def _any_other_tokens(markets: list[dict[str, Any]], *, home: int, away: int) -> list[dict[str, Any]]:
    listed = False
    other_m = None
    for m in markets:
        q = (m.get("question") or "").lower()
        if "any other" in q:
            other_m = m
            continue
        printed = None
        mw = ql.EXACT_WIN_RE.search(m.get("question") or "")
        md = ql.EXACT_DRAW_RE.search(m.get("question") or "")
        if mw:
            printed = f"{mw.group(2)}-{mw.group(3)}"
        elif md:
            printed = f"{md.group(1)}-{md.group(2)}"
        else:
            ms = ql.SCORE_PAIR_RE.search(m.get("question") or "")
            if ms:
                printed = f"{ms.group(1)}-{ms.group(2)}"
        if printed == f"{home}-{away}":
            listed = True
    if other_m is None:
        return []
    yes_wins = not listed
    outcomes = other_m.get("outcomes") or ["Yes", "No"]
    tokens = other_m.get("clob_token_ids") or []
    rows: list[dict[str, Any]] = []
    for i, tok in enumerate(tokens[:2]):
        lab = str(outcomes[i]) if i < len(outcomes) else str(i)
        is_yes = lab.lower() == "yes"
        rows.append(
            {
                "family": "exact_score",
                "market_key": f"exact_other_{'yes' if is_yes else 'no'}",
                "outcome": lab,
                "settlement": "WIN" if (is_yes and yes_wins) or ((not is_yes) and (not yes_wins)) else "LOSE",
                "token_id": tok,
                "question": other_m.get("question") or "",
            }
        )
    return rows


def settle_event_tokens(
    *,
    home: str,
    away: str,
    home_score: int,
    away_score: int,
    home_half: int,
    away_half: int,
    main: dict[str, Any] | None,
    more: dict[str, Any] | None,
    exact: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """Settle moneyline from main, props from More Markets, exact from Exact Score."""
    main_m = _enrich_event_markets(main)
    more_m = _enrich_event_markets(more)
    exact_m = _enrich_event_markets(exact)
    rows: list[dict[str, Any]] = []
    rows += ql.moneyline_tokens(
        main_m, home=home, away=away, home_score=home_score, away_score=away_score
    )
    rows += ql.spread_tokens(
        more_m, home=home, away=away, home_score=home_score, away_score=away_score
    )
    rows += ql.totals_tokens(
        more_m,
        home=home,
        away=away,
        home_score=home_score,
        away_score=away_score,
        home_half=home_half,
        away_half=away_half,
        mode="ft",
    )
    rows += ql.btts_tokens(
        more_m,
        home_score=home_score,
        away_score=away_score,
        home_half=home_half,
        away_half=away_half,
        mode="ft",
    )
    rows += ql.exact_score_tokens(
        exact_m,
        home=home,
        away=away,
        home_score=home_score,
        away_score=away_score,
        mode="ft",
    )
    rows += _any_other_tokens(exact_m, home=home_score, away=away_score)
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for r in rows:
        tid = str(r.get("token_id") or "")
        if not tid or tid in seen:
            continue
        seen.add(tid)
        out.append(r)
    return out


def _fx_kickoff(fx: dict[str, Any]) -> datetime | None:
    raw = ((fx.get("fixture") or {}).get("date") or "").strip()
    if not raw:
        return None
    s = raw.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _fx_teams(fx: dict[str, Any]) -> tuple[str, str]:
    teams = fx.get("teams") or {}
    home = str(((teams.get("home") or {}).get("name")) or "")
    away = str(((teams.get("away") or {}).get("name")) or "")
    return home, away


def _name_pair_score(
    pm_home: str,
    pm_away: str,
    kickoff: datetime | None,
    fx: dict[str, Any],
    *,
    min_side: float,
    max_skew_min: int,
) -> tuple[float, bool, float | None] | None:
    af_home, af_away = _fx_teams(fx)
    if not af_home or not af_away:
        return None
    skew = None
    fko = _fx_kickoff(fx)
    if kickoff is not None and fko is not None:
        skew = abs((kickoff - fko).total_seconds()) / 60.0
        if skew > max_skew_min:
            return None
    elif kickoff is not None and fko is None:
        return None
    sh = bl.team_similarity(pm_home, af_home)
    sa = bl.team_similarity(pm_away, af_away)
    sh2 = bl.team_similarity(pm_home, af_away)
    sa2 = bl.team_similarity(pm_away, af_home)
    direct = (sh + sa) / 2 if sh >= min_side and sa >= min_side else 0.0
    swap = (sh2 + sa2) / 2 if sh2 >= min_side and sa2 >= min_side else 0.0
    score = max(direct, swap)
    if score <= 0:
        return None
    if skew is not None:
        score *= 1.0 - min(skew, max_skew_min) / (max_skew_min * 2)
    return score, swap > direct, skew


def pair_pm_to_af(
    pm_home: str,
    pm_away: str,
    kickoff: datetime | None,
    fixtures: list[dict[str, Any]],
    *,
    min_side: float = DEFAULT_MIN_SIDE,
    max_skew_min: int = DEFAULT_MAX_SKEW_MIN,
) -> dict[str, Any] | None:
    """Best *regulation-ready* AF fixture. Rank only after 90'+HT extract succeeds."""
    best: dict[str, Any] | None = None
    best_score = 0.0
    for fx in fixtures:
        scored = regulation_from_af_fixture(fx)
        if not scored:
            continue
        got = _name_pair_score(
            pm_home, pm_away, kickoff, fx, min_side=min_side, max_skew_min=max_skew_min
        )
        if not got:
            continue
        score, swapped, _skew = got
        if score > best_score:
            best_score = score
            best = {
                "fixture": fx,
                "scored": scored,
                "swapped": swapped,
                "pair_score": score,
                "af_home": _fx_teams(fx)[0],
                "af_away": _fx_teams(fx)[1],
            }
    if not best:
        return None
    scored = best["scored"]
    fh, fa = scored["home"], scored["away"]
    hh, ah = scored["home_half"], scored["away_half"]
    if best["swapped"]:
        fh, fa = fa, fh
        hh, ah = ah, hh
    return {
        "source": "apifootball",
        "af_fixture_id": ((best["fixture"].get("fixture") or {}).get("id")),
        "af_status": scored.get("status_short"),
        "pair_score": round(best_score, 4),
        "sides_swapped": bool(best["swapped"]),
        "home": fh,
        "away": fa,
        "home_half": hh,
        "away_half": ah,
        "af_home": best["af_home"],
        "af_away": best["af_away"],
    }


def af_blocks_dqd_fallback(
    pm_home: str,
    pm_away: str,
    kickoff: datetime | None,
    fixtures: list[dict[str, Any]],
    *,
    min_side: float = DEFAULT_MIN_SIDE,
    max_skew_min: int = DEFAULT_MAX_SKEW_MIN,
) -> bool:
    """True when AF found this matchup live or in ET/pen without a usable 90' score."""
    have_ready = False
    block = False
    for fx in fixtures:
        got = _name_pair_score(
            pm_home, pm_away, kickoff, fx, min_side=min_side, max_skew_min=max_skew_min
        )
        if not got:
            continue
        if regulation_from_af_fixture(fx):
            have_ready = True
            break
        short = str(
            (((fx.get("fixture") or {}).get("status") or {}).get("short")) or ""
        ).upper()
        if short in AF_BLOCKS_DQD_SHORT:
            block = True
    return (not have_ready) and block


def regulation_from_af_fixture(fx: dict[str, Any]) -> dict[str, Any] | None:
    """90'+stoppage FT + HT. Extra time is not the 90-minute score.

    API-Football is inconsistent on AET: sometimes ``fulltime`` is 90' and
    ``extratime`` is cumulative (2-2 / 2-3); sometimes ``fulltime`` copies the
    final (2-3) and ``extratime`` is the ET increment (0-1). Subtract only in
    the second case (fulltime == live goals, extra != fulltime).
    """
    st = af_lib.live_fixture_status_from_fixture(fx)
    short = str(st.get("status_short") or "").upper()
    if short not in af_lib.REGULATION_DECIDED_SHORT:
        return None
    score = st.get("score") or {}
    ft = score.get("fulltime") or {}
    et = score.get("extratime") or {}
    ht = score.get("halftime") or {}
    goals = st.get("goals") or {}
    fh, fa = parse_int(ft.get("home")), parse_int(ft.get("away"))
    hh, ah = parse_int(ht.get("home")), parse_int(ht.get("away"))
    if fh is None or fa is None or hh is None or ah is None:
        return None
    if short in {"AET", "ET", "PEN", "P", "BT"}:
        eh, ea = parse_int(et.get("home")), parse_int(et.get("away"))
        gh, ga = parse_int(goals.get("home")), parse_int(goals.get("away"))
        if (
            eh is not None
            and ea is not None
            and gh is not None
            and ga is not None
            and (fh, fa) == (gh, ga)
            and (eh, ea) != (fh, fa)
        ):
            rh, ra = fh - eh, fa - ea
            if rh >= 0 and ra >= 0 and hh <= rh and ah <= ra:
                fh, fa = rh, ra
    if hh > fh or ah > fa:
        return None
    return {
        "home": fh,
        "away": fa,
        "home_half": hh,
        "away_half": ah,
        "status_short": short,
    }


def _clock_minute(m: dict[str, Any] | None) -> int | None:
    """Regulation minute from DQD clock fields. ``90'+6'`` → 90, not 96."""
    if not m:
        return None

    def _from_clock_token(raw: Any) -> int | None:
        if raw is None or raw == "":
            return None
        s = str(raw).strip()
        if "+" in s:
            s = s.split("+", 1)[0]
        s = s.strip().rstrip("'′")
        try:
            return int(float(s))
        except (TypeError, ValueError):
            digits = "".join(ch for ch in s if ch.isdigit())
            if not digits:
                return None
            try:
                return int(digits)
            except ValueError:
                return None

    for key in ("minute", "minute_str", "official_clock"):
        parsed = _from_clock_token(m.get(key))
        if parsed is not None:
            return parsed
    return None


def _dqd_et_excluded(d: dict[str, Any]) -> bool:
    """True when this DQD row does not look like extra time / penalties."""
    period = str(d.get("period") or "").strip().upper()
    if period in {"ET", "AET", "PEN", "PENS", "P", "1ET", "2ET", "ET1", "ET2"}:
        return False
    if period != "FT":
        return False
    blob = " ".join(
        str(d.get(k) or "")
        for k in ("status", "status_raw", "official_clock", "minute_str")
    ).lower()
    if any(tok in blob for tok in ("aet", "extra time", "extratime", "加时", "点球")):
        return False
    minute = _clock_minute(d)
    if minute is not None and minute > 90:
        return False
    return True


def pair_pm_to_dqd(
    pm_home: str,
    pm_away: str,
    kickoff: datetime | None,
    dqd_matches: list[dict[str, Any]],
    *,
    min_side: float = DEFAULT_MIN_SIDE,
    max_skew_min: int = DEFAULT_MAX_SKEW_MIN,
    league: str = "",
    league_id: str = "",
) -> dict[str, Any] | None:
    """FT Dongqiudi row with half scores, oriented onto Polymarket sides.

    Cups / knockout ties are skipped: after AET, DQD often keeps ``period=FT``
    with the 120' score. League games require a clock that is not past 90'.
    """
    if looks_like_cup(league_id=league_id, league=league):
        return None
    pm_row = {
        "home": pm_home,
        "away": pm_away,
        "kickoff_beijing": (
            kickoff.astimezone(TZ_CN).strftime("%Y-%m-%d %H:%M") if kickoff else ""
        ),
        "start_play": kickoff.strftime("%Y-%m-%d %H:%M:%S+00") if kickoff else "",
    }
    best_row = None
    best_s = 0.0
    for d in dqd_matches:
        if not bl.is_full_time(d):
            continue
        if not _dqd_et_excluded(d):
            continue
        if looks_like_cup(dqd_league=str(d.get("league") or "")):
            continue
        s = bl.score_pair(
            d,
            pm_row,
            max_skew_min=max_skew_min,
            min_side=min_side,
            league_floor=0.0,
        )
        if s > best_s:
            best_s = s
            best_row = d
    if not best_row or best_s < 0.70:
        return None
    hs = parse_int(best_row.get("home_score"))
    aw = parse_int(best_row.get("away_score"))
    hh = parse_int(best_row.get("home_half"))
    ah = parse_int(best_row.get("away_half"))
    if hs is None or aw is None or hh is None or ah is None:
        return None
    if hh > hs or ah > aw:
        return None
    hs2, aw2 = bl.orient_scores(
        str(best_row.get("home") or ""),
        str(best_row.get("away") or ""),
        hs,
        aw,
        pm_home,
        pm_away,
    )
    hh2, ah2 = bl.orient_scores(
        str(best_row.get("home") or ""),
        str(best_row.get("away") or ""),
        hh,
        ah,
        pm_home,
        pm_away,
    )
    minute = _clock_minute(best_row)
    return {
        "source": "dongqiudi",
        "dqd_id": best_row.get("id"),
        "pair_score": round(best_s, 4),
        "sides_swapped": bl.sides_are_swapped(
            str(best_row.get("home") or ""),
            str(best_row.get("away") or ""),
            pm_home,
            pm_away,
        ),
        "home": parse_int(hs2),
        "away": parse_int(aw2),
        "home_half": parse_int(hh2),
        "away_half": parse_int(ah2),
        "et_risk": minute is None,
    }


def calendar_dates_for_kickoff(kickoff: datetime | None, *, extra_days: int = 1) -> list[str]:
    if kickoff is None:
        return []
    utc_d = kickoff.astimezone(timezone.utc).date()
    cn_d = kickoff.astimezone(TZ_CN).date()
    dates: list[str] = []
    for base in (utc_d, cn_d):
        for delta in range(-extra_days, extra_days + 1):
            d = (base + timedelta(days=delta)).isoformat()
            if d not in dates:
                dates.append(d)
    return dates


def load_af_fixtures_for_dates(
    dates: list[str],
    *,
    refresh: bool = False,
    af: af_lib.AFClient | None = None,
) -> list[dict[str, Any]]:
    """Read ``data/apifootball/date_fixtures``; optionally force-refresh via AF."""
    if refresh and af is not None:
        rows, _ok = af_lib.fetch_fixtures_for_dates(
            af, dates, force_refresh=True
        )
        return rows
    out: list[dict[str, Any]] = []
    seen: set[int] = set()
    for date in dates:
        cached = af_lib.load_date_fixtures(date)
        if not cached:
            continue
        for fx in cached:
            fid = int(((fx.get("fixture") or {}).get("id") or 0) or 0)
            if fid and fid in seen:
                continue
            if fid:
                seen.add(fid)
            out.append(fx)
    return out


def load_dqd_matches(path: Path | None = None) -> list[dict[str, Any]]:
    p = path or (repo_root() / "data" / "snapshot.json")
    raw = ql.load_json(p, None)
    if not isinstance(raw, dict):
        return []
    rows = raw.get("matches") or []
    return [m for m in rows if isinstance(m, dict)]


def resolve_regulation_score(
    match: dict[str, Any],
    *,
    af_fixtures: list[dict[str, Any]],
    dqd_matches: list[dict[str, Any]],
) -> dict[str, Any]:
    """Regulation FT + HT on Polymarket sides. Never uses Gamma's score field."""
    home = str(match.get("home") or "")
    away = str(match.get("away") or "")
    kickoff = parse_kickoff(match.get("start_play"))
    af_hit = pair_pm_to_af(home, away, kickoff, af_fixtures)
    if af_hit and af_hit.get("home") is not None and af_hit.get("home_half") is not None:
        return af_hit
    if af_blocks_dqd_fallback(home, away, kickoff, af_fixtures):
        return {
            "source": None,
            "error": "af_live_or_et_no_regulation",
            "home": None,
            "away": None,
            "home_half": None,
            "away_half": None,
        }
    dqd_hit = pair_pm_to_dqd(
        home,
        away,
        kickoff,
        dqd_matches,
        league=str(match.get("league") or ""),
        league_id=str(match.get("league_id") or ""),
    )
    if dqd_hit and dqd_hit.get("home") is not None and dqd_hit.get("home_half") is not None:
        return dqd_hit
    return {
        "source": None,
        "error": "no_regulation_score",
        "home": None,
        "away": None,
        "home_half": None,
        "away_half": None,
    }


def _ask_levels(book: dict[str, Any]) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    for a in book.get("asks_top") or []:
        try:
            out.append((float(a["price"]), float(a["size"])))
        except (TypeError, ValueError, KeyError):
            continue
    return out


def win_tokens_with_asks(
    tokens: list[dict[str, Any]],
    books: dict[str, dict[str, Any]],
    *,
    max_ask: float = 1.0,
) -> list[dict[str, Any]]:
    hits: list[dict[str, Any]] = []
    for row in tokens:
        if str(row.get("settlement") or "") != "WIN":
            continue
        tid = str(row.get("token_id") or "")
        book = books.get(tid) or {}
        levels = [(p, s) for p, s in _ask_levels(book) if p <= max_ask + 1e-12]
        if not levels:
            continue
        size_all = sum(s for _, s in levels)
        size_tradeable = sum(s for p, s in levels if p <= 0.995)
        best_ask = min(p for p, _ in levels)
        hits.append(
            {
                "family": row.get("family"),
                "market_key": row.get("market_key"),
                "outcome": row.get("outcome"),
                "question": row.get("question"),
                "token_id": tid,
                "best_ask": best_ask,
                "asks": [{"price": p, "size": s} for p, s in levels],
                "tick_size": str(book.get("tick_size") or "") or "0.001",
                "neg_risk": False,
                "ask_shares": round(size_all, 4),
                "tradeable_shares": round(size_tradeable, 4),
            }
        )
    hits.sort(key=lambda r: (r.get("best_ask") is None, r.get("best_ask") or 9, r.get("question") or ""))
    return hits


def scan_hydrated(
    hydrated: dict[str, Any],
    *,
    score: dict[str, Any],
    proxy: str | None | object = ...,
    max_ask: float = 1.0,
    book_top_n: int = DEFAULT_BOOK_TOP_N,
) -> dict[str, Any]:
    match = hydrated.get("match") or {}
    hs, aw = parse_int(score.get("home")), parse_int(score.get("away"))
    hh, ah = parse_int(score.get("home_half")), parse_int(score.get("away_half"))
    if hs is None or aw is None or hh is None or ah is None:
        return {
            "ok": False,
            "error": score.get("error") or "no_regulation_score",
            "match": {
                "id": match.get("id"),
                "slug": match.get("slug"),
                "title": match.get("title"),
                "home": match.get("home"),
                "away": match.get("away"),
                "kickoff_beijing": match.get("kickoff_beijing"),
                "url": match.get("url"),
                "league": match.get("league"),
            },
            "score": score,
            "hits": [],
        }
    tokens = settle_event_tokens(
        home=str(match.get("home") or ""),
        away=str(match.get("away") or ""),
        home_score=hs,
        away_score=aw,
        home_half=hh,
        away_half=ah,
        main=hydrated.get("main"),
        more=hydrated.get("more_markets"),
        exact=hydrated.get("exact_score"),
    )
    wins = [t for t in tokens if t.get("settlement") == "WIN"]
    tids = [str(t.get("token_id") or "") for t in wins if t.get("token_id")]
    books = ql.fetch_books(
        tids, proxy=proxy, timeout=30.0, top_n=book_top_n, sequential_fallback=True
    )
    hits = win_tokens_with_asks(tokens, books, max_ask=max_ask)
    return {
        "ok": True,
        "match": {
            "id": match.get("id"),
            "slug": match.get("slug"),
            "title": match.get("title"),
            "home": match.get("home"),
            "away": match.get("away"),
            "kickoff_beijing": match.get("kickoff_beijing"),
            "url": match.get("url"),
            "league": match.get("league"),
            "league_id": match.get("league_id"),
        },
        "score": {
            "home": hs,
            "away": aw,
            "home_half": hh,
            "away_half": ah,
            "second_half_home": hs - hh,
            "second_half_away": aw - ah,
            "source": score.get("source"),
            "sides_swapped": score.get("sides_swapped"),
            "af_status": score.get("af_status"),
            "et_risk": bool(score.get("et_risk")),
        },
        "win_tokens": len(wins),
        "hit_count": len(hits),
        "hits": hits,
    }


def run_scan(
    *,
    hours: int = DEFAULT_HOURS,
    leagues: list[str] | None = None,
    max_per_league: int = DEFAULT_MAX_PER_LEAGUE,
    limit: int | None = None,
    max_ask: float = 1.0,
    refresh_af: bool = False,
    from_snapshot: bool = False,
    snapshot_path: Path | None = None,
    dqd_path: Path | None = None,
    proxy: str | None | object = ...,
    progress: Any = None,
) -> dict[str, Any]:
    """List → score → settle → CLOB. Does not trade."""
    proxy_url = pm.configure_proxy(None if proxy is ... else (None if proxy is None else str(proxy)))
    proxy_arg: Any = proxy_url if proxy_url else "none"

    if leagues and from_snapshot:
        catalog = pm.soccer_league_catalog(proxy=proxy_arg)
        filter_soccer_catalog(catalog, leagues)

    if from_snapshot:
        snap = ql.load_json(snapshot_path or (repo_root() / "data" / "polymarket" / "snapshot.json"), {})
        listed = matches_from_snapshot(
            snap if isinstance(snap, dict) else {}, hours=hours, leagues=leagues
        )
        list_meta = {"source": "snapshot", "count": len(listed)}
    else:
        listed_payload = list_unsettled_matches(
            hours=hours,
            leagues=leagues,
            max_per_league=max_per_league,
            proxy=proxy_arg,
        )
        listed = listed_payload.get("matches") or []
        list_meta = {
            "source": "gamma",
            "count": listed_payload.get("count"),
            "window_utc": listed_payload.get("window_utc"),
            "proxy": listed_payload.get("proxy"),
            "league_errors": listed_payload.get("league_errors") or [],
        }

    if limit is not None:
        listed = listed[: max(0, int(limit))]

    dates: list[str] = []
    for row in listed:
        kickoff = parse_kickoff(row.get("start_play"))
        for d in calendar_dates_for_kickoff(kickoff):
            if d not in dates:
                dates.append(d)
    af_client = None
    if refresh_af:
        try:
            af_client = af_lib.AFClient(af_lib.load_af_key(), min_interval_s=af_lib.FREE_PLAN_MIN_INTERVAL_S)
        except Exception as e:  # noqa: BLE001
            if progress:
                print(f"AF key unavailable ({e}); using date cache only", file=progress)
    af_fixtures = load_af_fixtures_for_dates(dates, refresh=refresh_af, af=af_client)
    dqd_matches = load_dqd_matches(dqd_path)

    skipped: list[dict[str, Any]] = []
    results: list[dict[str, Any]] = []
    scored_no_asks: list[dict[str, Any]] = []
    for i, row in enumerate(listed):
        if progress:
            print(
                f"[{i + 1}/{len(listed)}] {row.get('kickoff_beijing')} {row.get('home')} vs {row.get('away')}",
                file=progress,
            )
        try:
            hydrated = hydrate_match(row, proxy=proxy_arg)
        except Exception as e:  # noqa: BLE001
            skipped.append({"id": row.get("id"), "slug": row.get("slug"), "error": f"hydrate:{e}"})
            continue
        ev = hydrated.get("main") or {}
        if ev:
            if ev.get("closed") is True:
                skipped.append({"id": row.get("id"), "slug": row.get("slug"), "error": "settled"})
                continue
            if ev.get("live") is True:
                skipped.append({"id": row.get("id"), "slug": row.get("slug"), "error": "still_live"})
                continue
        score = resolve_regulation_score(
            hydrated.get("match") or row,
            af_fixtures=af_fixtures,
            dqd_matches=dqd_matches,
        )
        if score.get("home") is None:
            skipped.append(
                {
                    "id": row.get("id"),
                    "slug": row.get("slug"),
                    "title": row.get("title"),
                    "error": score.get("error") or "no_regulation_score",
                }
            )
            continue
        scanned = scan_hydrated(
            hydrated, score=score, proxy=proxy_arg, max_ask=max_ask
        )
        if not scanned.get("ok"):
            skipped.append(
                {
                    "id": row.get("id"),
                    "slug": row.get("slug"),
                    "error": scanned.get("error"),
                }
            )
            continue
        if scanned.get("hits"):
            results.append(scanned)
        else:
            scored_no_asks.append(
                {
                    "match": scanned.get("match"),
                    "score": scanned.get("score"),
                    "win_tokens": scanned.get("win_tokens"),
                }
            )

    return {
        "scanned_at": now_cn_iso(),
        "hours": hours,
        "max_ask": max_ask,
        "list": list_meta,
        "listed": len(listed),
        "scored": len(results) + len(scored_no_asks),
        "skipped": skipped,
        "scored_no_asks": scored_no_asks,
        "match_hits": len(results),
        "token_hits": sum(int(r.get("hit_count") or 0) for r in results),
        "results": results,
    }
