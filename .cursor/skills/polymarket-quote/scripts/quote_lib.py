#!/usr/bin/env python3
"""Post-FT Polymarket quoting: discover markets, settle from score, CLOB books."""

from __future__ import annotations

import fcntl
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

TZ_CN = timezone(timedelta(hours=8))
CLOB_HOST = "clob.polymarket.com"
CLOB_BASE = f"https://{CLOB_HOST}"
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# Import Gamma helpers from polymarket-soccer skill.
_PM_SCRIPTS = Path(__file__).resolve().parents[2] / "polymarket-soccer" / "scripts"
if str(_PM_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_PM_SCRIPTS))
import pm_lib as pm  # noqa: E402

WIN_RE = re.compile(r"will\s+(.+?)\s+win\s+on\b", re.I)
DRAW_RE = re.compile(r"end\s+in\s+a\s+draw", re.I)
SPREAD_RE = re.compile(r"spread:\s*(.+?)\s*\(([+-]?\d+(?:\.\d+)?)\)", re.I)
TOTAL_RE = re.compile(r"o/?u\s*(\d+(?:\.\d+)?)", re.I)
HALF_1_RE = re.compile(r"(?:1st|first)\s*half", re.I)
HALF_2_RE = re.compile(r"(?:2nd|second)\s*half", re.I)
EXACT_WIN_RE = re.compile(r"will\s+(.+?)\s+win\s+(\d+)\s*[-–]\s*(\d+)\s*\?", re.I)
EXACT_DRAW_RE = re.compile(r"draw\s+(\d+)\s*[-–]\s*(\d+)", re.I)
SCORE_PAIR_RE = re.compile(r"\b(\d+)\s*[-–]\s*(\d+)\b")

DEFAULT_EPS = 0.005
# Polymarket sports taker feeRate (2026): fee/share = feeRate * p * (1-p)
SPORTS_TAKER_FEE_RATE = 0.05
# Min net edge per share after fee before writing opportunities.jsonl
DEFAULT_MIN_NET = 0.02
TOP_N = 5


class QuoteError(RuntimeError):
    pass


def taker_fee_per_share(price: float, fee_rate: float = SPORTS_TAKER_FEE_RATE) -> float:
    """USDC fee per share for a taker fill at `price` (0..1)."""
    p = max(0.0, min(1.0, float(price)))
    return float(fee_rate) * p * (1.0 - p)


def repo_root_from(scripts_file: Path) -> Path:
    # scripts -> polymarket-quote -> skills -> .cursor -> repo
    return scripts_file.resolve().parents[4]


def data_dir(root: Path) -> Path:
    return root / "data" / "pm-quote"


def bridge_dir(root: Path) -> Path:
    return root / "data" / "bridge"


def now_cn_iso() -> str:
    return datetime.now(TZ_CN).isoformat(timespec="seconds")


def load_json(path: Path, default: Any = None) -> Any:
    if not path.is_file():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def append_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    """Append JSONL under exclusive flock (pairs with data_prune.prune_jsonl)."""
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = Path(str(path) + ".lock")
    with lock_path.open("a+", encoding="utf-8") as lf:
        fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
        try:
            with path.open("a", encoding="utf-8") as f:
                for row in rows:
                    f.write(json.dumps(row, ensure_ascii=False) + "\n")
        finally:
            fcntl.flock(lf.fileno(), fcntl.LOCK_UN)


def _parse_list_field(raw: Any) -> list[Any]:
    if raw is None:
        return []
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str):
        s = raw.strip()
        if not s:
            return []
        try:
            val = json.loads(s)
            return val if isinstance(val, list) else [val]
        except json.JSONDecodeError:
            return [s]
    return []


def enrich_market(m: dict[str, Any]) -> dict[str, Any]:
    outcomes = [str(x) for x in _parse_list_field(m.get("outcomes"))]
    tokens = [str(t) for t in _parse_list_field(m.get("clobTokenIds") or m.get("clob_token_ids")) if t]
    return {
        "market_id": str(m.get("id") or m.get("conditionId") or "").strip(),
        "condition_id": str(m.get("conditionId") or m.get("condition_id") or "").strip(),
        "question": str(m.get("question") or m.get("groupItemTitle") or ""),
        "group_item_title": str(m.get("groupItemTitle") or m.get("group_item_title") or ""),
        "slug": str(m.get("slug") or ""),
        "sports_market_type": str(m.get("sportsMarketType") or m.get("sports_market_type") or ""),
        "outcomes": outcomes,
        "clob_token_ids": tokens,
        "neg_risk": bool(m.get("negRisk") if m.get("negRisk") is not None else m.get("neg_risk")),
        "active": bool(m.get("active", True)),
        "closed": bool(m.get("closed", False)),
    }


def fetch_gamma_event(
    *,
    event_id: str | None = None,
    slug: str | None = None,
    proxy: str | None | object = ...,
) -> dict[str, Any] | None:
    if event_id:
        data = pm.fetch_json(f"/events/{event_id}", proxy=proxy)
        if isinstance(data, list) and data:
            return data[0] if isinstance(data[0], dict) else None
        return data if isinstance(data, dict) else None
    if slug:
        data = pm.fetch_json("/events", {"slug": slug}, proxy=proxy)
        if isinstance(data, list) and data:
            return data[0] if isinstance(data[0], dict) else None
        if isinstance(data, dict):
            return data
    return None


def discover_related_events(
    *,
    slug: str,
    home: str,
    away: str,
    proxy: str | None | object = ...,
) -> dict[str, dict[str, Any] | None]:
    """Fetch sibling More Markets / Exact Score events when present."""
    out: dict[str, dict[str, Any] | None] = {"more_markets": None, "exact_score": None}
    candidates = [
        ("more_markets", f"{slug}-more-markets"),
        ("exact_score", f"{slug}-exact-score"),
    ]
    # Title-based slugs vary; also try common patterns.
    home_slug = re.sub(r"[^a-z0-9]+", "-", home.lower()).strip("-")
    away_slug = re.sub(r"[^a-z0-9]+", "-", away.lower()).strip("-")
    if home_slug and away_slug:
        candidates.append(("exact_score", f"{home_slug}-vs-{away_slug}-exact-score"))

    seen: set[str] = set()
    for key, s in candidates:
        if not s or s in seen:
            continue
        seen.add(s)
        try:
            ev = fetch_gamma_event(slug=s, proxy=proxy)
        except pm.FetchError:
            ev = None
        if ev and isinstance(ev, dict) and (ev.get("markets") or ev.get("id")):
            # Keep first hit per key.
            if out.get(key) is None:
                out[key] = ev
    return out


# --- Settlement -------------------------------------------------------------


def winner_from_score(home_score: Any, away_score: Any) -> str | None:
    try:
        h = int(home_score)
        a = int(away_score)
    except (TypeError, ValueError):
        return None
    if h > a:
        return "home"
    if h < a:
        return "away"
    return "draw"


def _norm_name(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


def classify_moneyline_role(
    market: dict[str, Any],
    home: str,
    away: str,
) -> str | None:
    q = market.get("question") or ""
    title = market.get("group_item_title") or ""
    blob = f"{q} {title}"
    if DRAW_RE.search(blob) or _norm_name(title) == "draw" or "draw" in _norm_name(title):
        return "draw"
    m = WIN_RE.search(q)
    team = (m.group(1) if m else title).strip()
    nt = _norm_name(team)
    nh, na = _norm_name(home), _norm_name(away)
    if nh and (nt == nh or nh in nt or nt in nh):
        return "home"
    if na and (nt == na or na in nt or nt in na):
        return "away"
    # Fallback: question contains team names
    if nh and nh in _norm_name(q) and "win" in q.lower():
        return "home"
    if na and na in _norm_name(q) and "win" in q.lower():
        return "away"
    return None


def settle_yes_no(yes_wins: bool) -> tuple[str, str]:
    """Return (yes_settlement, no_settlement)."""
    if yes_wins:
        return "WIN", "LOSE"
    return "LOSE", "WIN"


def moneyline_tokens(
    markets: list[dict[str, Any]],
    *,
    home: str,
    away: str,
    home_score: Any,
    away_score: Any,
) -> list[dict[str, Any]]:
    winner = winner_from_score(home_score, away_score)
    rows: list[dict[str, Any]] = []
    for m in markets:
        role = classify_moneyline_role(m, home, away)
        if role not in ("home", "away", "draw"):
            continue
        outcomes = m.get("outcomes") or []
        tokens = m.get("clob_token_ids") or []
        if len(tokens) < 2:
            continue
        # Align by outcomes when possible
        yes_i, no_i = 0, 1
        for i, o in enumerate(outcomes):
            if str(o).lower() == "yes":
                yes_i = i
            elif str(o).lower() == "no":
                no_i = i
        if winner is None:
            yes_s = no_s = "PENDING"
        else:
            yes_wins = role == winner
            yes_s, no_s = settle_yes_no(yes_wins)
        for outcome, idx, settlement, suffix in (
            ("Yes", yes_i, yes_s, "yes"),
            ("No", no_i, no_s, "no"),
        ):
            if idx >= len(tokens):
                continue
            rows.append(
                {
                    "family": "moneyline",
                    "market_key": f"{role}_{suffix}",
                    "role": role,
                    "outcome": outcome,
                    "settlement": settlement,
                    "token_id": tokens[idx],
                    "market_id": m.get("market_id") or "",
                    "condition_id": m.get("condition_id") or "",
                    "question": m.get("question") or "",
                    "sports_market_type": m.get("sports_market_type") or "moneyline",
                }
            )
    # Stable order
    order = ["home_yes", "home_no", "draw_yes", "draw_no", "away_yes", "away_no"]
    rows.sort(key=lambda r: order.index(r["market_key"]) if r["market_key"] in order else 99)
    return rows


def _parse_spread(market: dict[str, Any]) -> tuple[str, float] | None:
    q = market.get("question") or ""
    m = SPREAD_RE.search(q)
    if m:
        return m.group(1).strip(), float(m.group(2))
    smt = (market.get("sports_market_type") or "").lower()
    if smt != "spreads":
        return None
    # line sometimes in group title
    m2 = re.search(r"\(([+-]?\d+(?:\.\d+)?)\)", q)
    team = (market.get("group_item_title") or "").strip() or q
    if m2:
        return team, float(m2.group(1))
    return None


def spread_tokens(
    markets: list[dict[str, Any]],
    *,
    home: str,
    away: str,
    home_score: Any,
    away_score: Any,
) -> list[dict[str, Any]]:
    try:
        h, a = int(home_score), int(away_score)
    except (TypeError, ValueError):
        return []
    margin = h - a
    rows: list[dict[str, Any]] = []
    for m in markets:
        smt = (m.get("sports_market_type") or "").lower()
        parsed = _parse_spread(m)
        if smt not in ("spreads", "spread") and not parsed:
            if "spread:" not in (m.get("question") or "").lower():
                continue
        if not parsed:
            continue
        fav_team, line = parsed
        # Handicap applies to favorite team named in "Spread: Team (line)"
        # If line is -1.5 on home, home covers when margin > 1.5
        fav_is_home = _norm_name(fav_team) == _norm_name(home) or _norm_name(home) in _norm_name(
            fav_team
        )
        fav_margin = margin if fav_is_home else -margin
        fav_covers = fav_margin + line > 0
        outcomes = m.get("outcomes") or []
        tokens = m.get("clob_token_ids") or []
        if len(tokens) < 2 or len(outcomes) < 2:
            # assume [favorite, underdog] or Yes/No
            outcomes = outcomes or [fav_team, away if fav_is_home else home]
        for i, token in enumerate(tokens[:2]):
            label = str(outcomes[i]) if i < len(outcomes) else f"outcome_{i}"
            nl = _norm_name(label)
            is_fav = nl == _norm_name(fav_team) or _norm_name(fav_team) in nl or label.lower() == "yes"
            if label.lower() == "no":
                is_fav = False
            settlement = "WIN" if (is_fav and fav_covers) or ((not is_fav) and (not fav_covers)) else "LOSE"
            rows.append(
                {
                    "family": "spreads",
                    "market_key": f"spread_{_norm_name(fav_team)}_{line}_{i}",
                    "role": "spread",
                    "outcome": label,
                    "settlement": settlement,
                    "token_id": token,
                    "market_id": m.get("market_id") or "",
                    "condition_id": m.get("condition_id") or "",
                    "question": m.get("question") or "",
                    "sports_market_type": "spreads",
                    "line": line,
                    "favorite": fav_team,
                }
            )
    return rows


def _parse_int_score(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _parse_total_market(
    question: str,
    home: str,
    away: str,
) -> dict[str, Any] | None:
    """Classify O/U markets: match vs team, and ft / 1h / 2h.

    Examples:
      "A vs. B: O/U 2.5" → match ft
      "A vs. B: 1st Half O/U 0.5" → match 1h
      "A vs. B: CS Huancayo O/U 2.5" → away ft
      "A vs. B: Spain 1st Half O/U 0.5" → home 1h
    """
    q = question or ""
    mline = TOTAL_RE.search(q)
    if not mline:
        return None
    line = float(mline.group(1))
    period = "ft"
    if HALF_1_RE.search(q):
        period = "1h"
    elif HALF_2_RE.search(q):
        period = "2h"

    suffix = q.split(":")[-1].strip() if ":" in q else q
    team_blob = TOTAL_RE.sub("", suffix)
    team_blob = HALF_1_RE.sub("", team_blob)
    team_blob = HALF_2_RE.sub("", team_blob)
    team_blob = team_blob.strip(" :-")

    side = "match"
    if team_blob:
        nt = _norm_name(team_blob)
        nh, na = _norm_name(home), _norm_name(away)
        if nh and (nt == nh or nh in nt or nt in nh):
            side = "home"
        elif na and (nt == na or na in nt or nt in na):
            side = "away"
        else:
            # Named team we cannot map — do not fall back to match total.
            return None

    return {"line": line, "period": period, "side": side}


def _goals_for_total(
    *,
    side: str,
    period: str,
    home_score: Any,
    away_score: Any,
    home_half: Any = None,
    away_half: Any = None,
) -> int | None:
    """Return the goal count that settles this total, or None if unknown."""
    h = _parse_int_score(home_score)
    a = _parse_int_score(away_score)
    if h is None or a is None:
        return None
    hh = _parse_int_score(home_half)
    ah = _parse_int_score(away_half)

    if period == "ft":
        if side == "match":
            return h + a
        if side == "home":
            return h
        if side == "away":
            return a
        return None

    if period == "1h":
        if hh is None or ah is None:
            return None
        if side == "match":
            return hh + ah
        if side == "home":
            return hh
        if side == "away":
            return ah
        return None

    if period == "2h":
        if hh is None or ah is None:
            return None
        h2, a2 = h - hh, a - ah
        if h2 < 0 or a2 < 0:
            return None
        if side == "match":
            return h2 + a2
        if side == "home":
            return h2
        if side == "away":
            return a2
        return None

    return None


def totals_tokens(
    markets: list[dict[str, Any]],
    *,
    home: str,
    away: str,
    home_score: Any,
    away_score: Any,
    home_half: Any = None,
    away_half: Any = None,
    mode: str = "ft",
) -> list[dict[str, Any]]:
    """Settle match / team / half O/U tokens using the correct goal bucket."""
    rows: list[dict[str, Any]] = []
    for m in markets:
        smt = (m.get("sports_market_type") or "").lower()
        q = m.get("question") or ""
        parsed = _parse_total_market(q, home, away)
        if smt not in ("totals", "total", "team_totals", "team_total") and not parsed:
            continue
        if not parsed:
            continue
        line = float(parsed["line"])
        side = str(parsed["side"])
        period = str(parsed["period"])
        goals = _goals_for_total(
            side=side,
            period=period,
            home_score=home_score,
            away_score=away_score,
            home_half=home_half,
            away_half=away_half,
        )
        if goals is None:
            continue
        # Live: only emit when Over is already locked (goals can only rise).
        if mode == "live" and not (goals > line):
            continue
        over_wins = goals > line
        outcomes = m.get("outcomes") or ["Over", "Under"]
        tokens = m.get("clob_token_ids") or []
        side_key = "match" if side == "match" else side
        period_key = "" if period == "ft" else f"_{period}"
        for i, token in enumerate(tokens[:2]):
            label = str(outcomes[i]) if i < len(outcomes) else f"outcome_{i}"
            is_over = "over" in label.lower()
            settlement = (
                "WIN" if (is_over and over_wins) or ((not is_over) and (not over_wins)) else "LOSE"
            )
            rows.append(
                {
                    "family": "totals",
                    "market_key": f"{side_key}{period_key}_total_{line}_{'over' if is_over else 'under'}",
                    "role": "totals",
                    "outcome": label,
                    "settlement": settlement,
                    "locked": True,
                    "token_id": token,
                    "market_id": m.get("market_id") or "",
                    "condition_id": m.get("condition_id") or "",
                    "question": q,
                    "sports_market_type": "totals",
                    "line": line,
                    "goals": goals,
                    "total_side": side,
                    "total_period": period,
                }
            )
    return rows


def _parse_btts_period(question: str) -> str:
    """Return ft | 1h | 2h for BTTS markets."""
    q = question or ""
    if HALF_2_RE.search(q) or re.search(r"in\s+second\s+half", q, re.I):
        return "2h"
    if HALF_1_RE.search(q) or re.search(r"in\s+(?:the\s+)?(?:1st|first)\s+half", q, re.I):
        return "1h"
    return "ft"


def _btts_both_scored(
    *,
    period: str,
    home_score: Any,
    away_score: Any,
    home_half: Any = None,
    away_half: Any = None,
) -> bool | None:
    """True/False if decidable; None if half scores required but missing."""
    h = _parse_int_score(home_score)
    a = _parse_int_score(away_score)
    if h is None or a is None:
        return None
    if period == "ft":
        return h > 0 and a > 0

    hh = _parse_int_score(home_half)
    ah = _parse_int_score(away_half)
    if hh is None or ah is None:
        return None

    if period == "1h":
        return hh > 0 and ah > 0

    if period == "2h":
        h2, a2 = h - hh, a - ah
        if h2 < 0 or a2 < 0:
            return None
        return h2 > 0 and a2 > 0

    return None


def btts_tokens(
    markets: list[dict[str, Any]],
    *,
    home_score: Any,
    away_score: Any,
    home_half: Any = None,
    away_half: Any = None,
    mode: str = "ft",
) -> list[dict[str, Any]]:
    """Settle BTTS Yes/No for full match, 1st half, or 2nd half.

    Half markets require `home_half` / `away_half`. Without them we skip — never
    fall back to full-time BTTS (that caused false sell_lose on 2H No).
    Live: only emit once both sides have scored in that period (Yes locked).
    """
    rows: list[dict[str, Any]] = []
    for m in markets:
        smt = (m.get("sports_market_type") or "").lower()
        q_raw = m.get("question") or ""
        q = q_raw.lower()
        if smt not in ("both_teams_to_score", "btts") and "both teams to score" not in q:
            continue
        period = _parse_btts_period(q_raw)
        both = _btts_both_scored(
            period=period,
            home_score=home_score,
            away_score=away_score,
            home_half=home_half,
            away_half=away_half,
        )
        if both is None:
            continue
        # Live: only lock after both have scored in-period (No is then dead).
        if mode == "live" and not both:
            continue
        outcomes = m.get("outcomes") or ["Yes", "No"]
        tokens = m.get("clob_token_ids") or []
        period_key = "" if period == "ft" else f"_{period}"
        for i, token in enumerate(tokens[:2]):
            label = str(outcomes[i]) if i < len(outcomes) else f"outcome_{i}"
            is_yes = label.lower() == "yes"
            settlement = "WIN" if (is_yes and both) or ((not is_yes) and (not both)) else "LOSE"
            rows.append(
                {
                    "family": "btts",
                    "market_key": f"btts{period_key}_{'yes' if is_yes else 'no'}",
                    "role": "btts",
                    "outcome": label,
                    "settlement": settlement,
                    "locked": True,
                    "token_id": token,
                    "market_id": m.get("market_id") or "",
                    "condition_id": m.get("condition_id") or "",
                    "question": q_raw,
                    "sports_market_type": "both_teams_to_score",
                    "btts_period": period,
                    "btts_both": both,
                }
            )
    return rows


def exact_score_tokens(
    markets: list[dict[str, Any]],
    *,
    home: str,
    away: str,
    home_score: Any,
    away_score: Any,
    mode: str = "ft",
) -> list[dict[str, Any]]:
    """Settle exact-score Yes/No tokens.

    FT: only the final H-A Yes is WIN.
    Live (soccer goals only rise): any scoreline with home<curr or away<curr is
    already dead → Yes=LOSE / No=WIN (e.g. after 1-0, 0-0 No is the locked buy).
    Still-possible scorelines are skipped in live mode (not locked yet).
    """
    try:
        h, a = int(home_score), int(away_score)
    except (TypeError, ValueError):
        return []
    target = f"{h}-{a}"
    rows: list[dict[str, Any]] = []
    for m in markets:
        q = m.get("question") or ""
        score = None
        mw = EXACT_WIN_RE.search(q)
        md = EXACT_DRAW_RE.search(q)
        if mw:
            score = f"{mw.group(2)}-{mw.group(3)}"
        elif md:
            score = f"{md.group(1)}-{md.group(2)}"
        else:
            ms = SCORE_PAIR_RE.search(q)
            if ms:
                score = f"{ms.group(1)}-{ms.group(2)}"
        if score is None:
            continue
        try:
            sh, sa = (int(x) for x in score.split("-", 1))
        except ValueError:
            continue

        if mode == "live":
            # Impossible once either side has already exceeded the printed scoreline.
            if sh < h or sa < a:
                yes_wins = False
                locked = True
            else:
                continue  # still reachable — wait for FT or further goals
        else:
            yes_wins = score == target
            locked = True

        outcomes = m.get("outcomes") or ["Yes", "No"]
        tokens = m.get("clob_token_ids") or []
        yes_i, no_i = 0, 1
        for i, o in enumerate(outcomes):
            if str(o).lower() == "yes":
                yes_i = i
            elif str(o).lower() == "no":
                no_i = i
        for outcome, idx, settlement in (
            ("Yes", yes_i, "WIN" if yes_wins else "LOSE"),
            ("No", no_i, "LOSE" if yes_wins else "WIN"),
        ):
            if idx >= len(tokens):
                continue
            rows.append(
                {
                    "family": "exact_score",
                    "market_key": f"exact_{score}_{outcome.lower()}",
                    "role": "exact_score",
                    "outcome": outcome,
                    "settlement": settlement,
                    "locked": locked,
                    "token_id": tokens[idx],
                    "market_id": m.get("market_id") or "",
                    "condition_id": m.get("condition_id") or "",
                    "question": q,
                    "sports_market_type": "exact_score",
                    "scoreline": score,
                    "is_correct_score": score == target,
                }
            )
    return rows


# --- CLOB -------------------------------------------------------------------


def _http_clob(
    url: str,
    proxy: str | None,
    timeout: float,
    *,
    method: str = "GET",
    body: str | None = None,
) -> str:
    """GET/POST CLOB JSON via urllib (SOCKS via pm.configure_proxy socket patch)."""
    headers = {
        "User-Agent": UA,
        "Accept": "application/json",
    }
    data: bytes | None = None
    if method.upper() == "POST":
        headers["Content-Type"] = "application/json"
        data = (body or "").encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method=method.upper())
    scheme = (urlparse(proxy).scheme or "").lower() if proxy else ""
    try:
        if proxy and scheme in ("http", "https"):
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({"http": proxy, "https": proxy})
            )
            with opener.open(req, timeout=timeout) as resp:
                return resp.read().decode("utf-8")
        if proxy and scheme.startswith("socks"):
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read().decode("utf-8")
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        body_txt = e.read().decode("utf-8", errors="replace")[:300]
        raise QuoteError(f"CLOB HTTP {e.code}: {body_txt}") from e
    except urllib.error.URLError as e:
        raise QuoteError(f"CLOB network error: {e.reason}") from e


def fetch_books(
    token_ids: list[str],
    *,
    proxy: str | None | object = ...,
    timeout: float = 25.0,
    top_n: int = TOP_N,
) -> dict[str, dict[str, Any]]:
    """Return map token_id -> normalized book summary."""
    ids = [str(t) for t in token_ids if t]
    if not ids:
        return {}
    if proxy is ...:
        proxy_url = pm.configure_proxy(None)
    else:
        proxy_url = pm.configure_proxy(None if proxy is None else str(proxy))

    books: dict[str, dict[str, Any]] = {}
    for i in range(0, len(ids), 50):
        chunk = ids[i : i + 50]
        payload = json.dumps([{"token_id": t} for t in chunk])
        url = f"{CLOB_BASE}/books"
        try:
            raw = _http_clob(url, proxy_url, timeout, method="POST", body=payload)
            data = json.loads(raw)
            if not isinstance(data, list):
                raise QuoteError("POST /books did not return a list")
            for book in data:
                if isinstance(book, dict):
                    tid = str(book.get("asset_id") or "")
                    if tid:
                        books[tid] = normalize_book(book, top_n=top_n)
        except (QuoteError, json.JSONDecodeError, pm.FetchError):
            for tid in chunk:
                if tid in books:
                    continue
                try:
                    one = _http_clob(
                        f"{CLOB_BASE}/book?token_id={urllib.parse.quote(tid)}",
                        proxy_url,
                        timeout,
                    )
                    book = json.loads(one)
                    if isinstance(book, dict):
                        books[tid] = normalize_book(book, top_n=top_n)
                except Exception:  # noqa: BLE001
                    books[tid] = {
                        "token_id": tid,
                        "book_missing": True,
                        "best_bid": None,
                        "best_ask": None,
                        "error": "book_fetch_failed",
                    }
    for tid in ids:
        if tid not in books:
            books[tid] = {
                "token_id": tid,
                "book_missing": True,
                "best_bid": None,
                "best_ask": None,
            }
    return books


def normalize_book(book: dict[str, Any], *, top_n: int = TOP_N) -> dict[str, Any]:
    bids = list(book.get("bids") or [])
    asks = list(book.get("asks") or [])

    def _px(level: dict[str, Any]) -> float:
        try:
            return float(level.get("price"))
        except (TypeError, ValueError):
            return 0.0

    bids_sorted = sorted((b for b in bids if isinstance(b, dict)), key=_px, reverse=True)
    asks_sorted = sorted((a for a in asks if isinstance(a, dict)), key=_px)
    best_bid = best_ask = None
    best_bid_size = best_ask_size = None
    if bids_sorted:
        best_bid = _px(bids_sorted[0])
        try:
            best_bid_size = float(bids_sorted[0].get("size"))
        except (TypeError, ValueError):
            best_bid_size = None
    if asks_sorted:
        best_ask = _px(asks_sorted[0])
        try:
            best_ask_size = float(asks_sorted[0].get("size"))
        except (TypeError, ValueError):
            best_ask_size = None
    spread = None
    midpoint = None
    if best_bid is not None and best_ask is not None:
        spread = round(best_ask - best_bid, 6)
        midpoint = round((best_ask + best_bid) / 2, 6)
    ltp = book.get("last_trade_price")
    try:
        ltp_f = float(ltp) if ltp is not None and ltp != "" else None
    except (TypeError, ValueError):
        ltp_f = None
    return {
        "token_id": str(book.get("asset_id") or ""),
        "market": book.get("market") or "",
        "book_ts": str(book.get("timestamp") or ""),
        "hash": book.get("hash") or "",
        "best_bid": best_bid,
        "best_bid_size": best_bid_size,
        "best_ask": best_ask,
        "best_ask_size": best_ask_size,
        "spread": spread,
        "midpoint": midpoint,
        "last_trade_price": ltp_f,
        "bids_top": [
            {"price": str(b.get("price")), "size": str(b.get("size"))} for b in bids_sorted[:top_n]
        ],
        "asks_top": [
            {"price": str(a.get("price")), "size": str(a.get("size"))} for a in asks_sorted[:top_n]
        ],
        "tick_size": str(book.get("tick_size") or ""),
        "min_order_size": str(book.get("min_order_size") or ""),
        "neg_risk": bool(book.get("neg_risk")),
        "book_missing": False,
    }


def flag_misprice(
    settlement: str,
    book: dict[str, Any],
    *,
    eps: float = DEFAULT_EPS,
    fee_rate: float = SPORTS_TAKER_FEE_RATE,
    min_net: float = DEFAULT_MIN_NET,
) -> tuple[bool, str, dict[str, Any]]:
    """Return (is_opp, reason, economics).

    Only flag opportunities whose net edge after sports taker fee covers
    `min_net` (default 0.02 USDC/share). Economics always filled when priced.
    """
    meta: dict[str, Any] = {
        "fee_rate": fee_rate,
        "min_net": min_net,
        "gross_edge": None,
        "fee": None,
        "net_edge": None,
        "trade": None,
        "price": None,
    }
    if book.get("book_missing"):
        return False, "book_missing", meta
    bid = book.get("best_bid")
    ask = book.get("best_ask")

    if settlement == "WIN" and ask is not None:
        p = float(ask)
        if p >= 1.0 - 1e-12:
            return False, "ask_at_par", meta
        gross = 1.0 - p
        fee = taker_fee_per_share(p, fee_rate)
        net = gross - fee
        meta.update(
            {
                "trade": "buy_win",
                "price": p,
                "gross_edge": round(gross, 6),
                "fee": round(fee, 6),
                "net_edge": round(net, 6),
            }
        )
        if net + 1e-12 < float(min_net):
            return False, f"WIN ask={p} net={net:.4f} < min_net={min_net} (fee={fee:.4f})", meta
        if gross < float(eps):
            return False, f"WIN ask={p} gross<{eps}", meta
        return True, f"WIN buy ask={p} net={net:.4f} after fee={fee:.4f}", meta

    if settlement == "LOSE" and bid is not None:
        p = float(bid)
        if p <= 1e-12:
            return False, "bid_zero", meta
        # Sell the losing token into the bid; settles to 0 → keep the proceeds.
        gross = p
        fee = taker_fee_per_share(p, fee_rate)
        net = gross - fee
        meta.update(
            {
                "trade": "sell_lose",
                "price": p,
                "gross_edge": round(gross, 6),
                "fee": round(fee, 6),
                "net_edge": round(net, 6),
            }
        )
        if net + 1e-12 < float(min_net):
            return False, f"LOSE bid={p} net={net:.4f} < min_net={min_net} (fee={fee:.4f})", meta
        if gross < float(eps):
            return False, f"LOSE bid={p} gross<{eps}", meta
        return True, f"LOSE sell bid={p} net={net:.4f} after fee={fee:.4f}", meta

    return False, "", meta


# --- Bridge join + pipeline -------------------------------------------------


def event_key(ev: dict[str, Any]) -> str:
    typ = ev.get("type") or ""
    mid = ev.get("match_id") or ""
    if typ == "score_change":
        prev = ev.get("prev") or {}
        curr = ev.get("curr") or {}
        return (
            f"score_change|{mid}|"
            f"{prev.get('home')}-{prev.get('away')}->"
            f"{curr.get('home')}-{curr.get('away')}|{ev.get('ts')}"
        )
    return f"{typ}|{mid}|{ev.get('ts')}"


def load_bridge_matches(root: Path) -> list[dict[str, Any]]:
    snap = load_json(bridge_dir(root) / "matches.json", {}) or {}
    return list(snap.get("matches") or [])


def load_bridge_quote_events(root: Path) -> list[dict[str, Any]]:
    """Load score_change + match_finished events for quoting."""
    path = bridge_dir(root) / "events.jsonl"
    rows: list[dict[str, Any]] = []
    if not path.is_file():
        return rows
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(ev, dict) and ev.get("type") in ("match_finished", "score_change"):
            rows.append(ev)
    return rows


def load_bridge_ft_events(root: Path) -> list[dict[str, Any]]:
    """Backward-compatible alias."""
    return [e for e in load_bridge_quote_events(root) if e.get("type") == "match_finished"]


def join_ft_context(root: Path, ev: dict[str, Any]) -> dict[str, Any]:
    """Merge FT event with full matched row from matches.json."""
    mid = str(ev.get("match_id") or "")
    eid = str((ev.get("polymarket") or {}).get("event_id") or "")
    row = None
    for r in load_bridge_matches(root):
        dqd = r.get("dongqiudi") or {}
        pm = r.get("polymarket") or {}
        if mid and str(dqd.get("id") or "") == mid:
            row = r
            break
        if eid and str(pm.get("event_id") or "") == eid:
            row = r
            break
    pm = dict((row or {}).get("polymarket") or {})
    # Overlay slim event polymarket
    pm.update({k: v for k, v in (ev.get("polymarket") or {}).items() if v})
    dqd = dict((row or {}).get("dongqiudi") or {})
    if ev.get("home_score") is not None:
        dqd["home_score"] = ev.get("home_score")
    if ev.get("away_score") is not None:
        dqd["away_score"] = ev.get("away_score")
    return {
        "event": ev,
        "match_row": row,
        "dongqiudi": dqd,
        "polymarket": pm,
        "home_score": ev.get("home_score", dqd.get("home_score")),
        "away_score": ev.get("away_score", dqd.get("away_score")),
        "home": ev.get("home") or pm.get("home") or dqd.get("home") or "",
        "away": ev.get("away") or pm.get("away") or dqd.get("away") or "",
    }


def markets_from_refs(refs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Normalize bridge market_refs (may lack outcomes) into enrich_market shape."""
    out: list[dict[str, Any]] = []
    for r in refs or []:
        tokens = [str(t) for t in (r.get("clob_token_ids") or []) if t]
        out.append(
            {
                "market_id": str(r.get("market_id") or ""),
                "condition_id": str(r.get("condition_id") or ""),
                "question": str(r.get("question") or ""),
                "group_item_title": "",
                "slug": str(r.get("slug") or ""),
                "sports_market_type": str(r.get("sports_market_type") or ""),
                "outcomes": list(r.get("outcomes") or ["Yes", "No"]),
                "clob_token_ids": tokens,
                "neg_risk": bool(r.get("neg_risk", True)),
            }
        )
    return out


def collect_target_tokens(
    ctx: dict[str, Any],
    *,
    proxy: str | None | object = ...,
    include_props: bool = True,
    include_exact: bool = True,
    mode: str = "ft",
    market_cache: Any | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Build settled token rows + discovery meta.

    mode=ft: full moneyline + props + exact (final settlement).
    mode=live: only outcomes already locked by current score (no moneyline/spreads).

    When ``market_cache`` is set, prefer warmed Gamma catalog (main / more / exact)
    so the hot path skips Gamma HTTP; misses fetch once and write back.
    """
    pm_h = ctx["polymarket"]
    home, away = ctx["home"], ctx["away"]
    hs, aws = ctx["home_score"], ctx["away_score"]
    match_id = str(
        (ctx.get("event") or {}).get("match_id")
        or (ctx.get("dongqiudi") or {}).get("id")
        or ""
    )
    meta: dict[str, Any] = {
        "mode": mode,
        "main_event": None,
        "more_markets": None,
        "exact_score": None,
        "skipped": [],
        "catalog_cache": "none",
    }

    cached: dict[str, Any] | None = None
    if market_cache is not None and match_id:
        try:
            hit = market_cache.get(match_id)
            if isinstance(hit, dict):
                cached = hit
        except Exception:  # noqa: BLE001
            cached = None

    main_from_cache = bool(cached and isinstance(cached.get("main_event"), dict))
    related_from_cache = bool(cached and cached.get("related_complete"))
    if main_from_cache and related_from_cache:
        meta["catalog_cache"] = "hit"
    elif cached and (main_from_cache or related_from_cache or cached.get("more_markets") or cached.get("exact_score")):
        meta["catalog_cache"] = "partial"

    main_markets: list[dict[str, Any]] = []
    ev: dict[str, Any] | None = None
    if main_from_cache:
        ev = cached["main_event"]  # type: ignore[index]
    else:
        try:
            ev = fetch_gamma_event(
                event_id=str(pm_h.get("event_id") or "") or None,
                slug=str(pm_h.get("slug") or "") or None,
                proxy=proxy,
            )
            if meta["catalog_cache"] not in ("hit", "partial"):
                meta["catalog_cache"] = "miss"
        except pm.FetchError as e:
            ev = None
            meta["main_fetch_error"] = str(e)
            if meta["catalog_cache"] not in ("hit", "partial"):
                meta["catalog_cache"] = "miss"
    if ev:
        meta["main_event"] = {
            "id": str(ev.get("id") or ""),
            "slug": ev.get("slug") or "",
            "title": ev.get("title") or "",
        }
        main_markets = [enrich_market(m) for m in (ev.get("markets") or []) if isinstance(m, dict)]
    if not main_markets:
        main_markets = markets_from_refs(list(pm_h.get("market_refs") or []))

    tokens: list[dict[str, Any]] = []
    if mode == "ft":
        tokens.extend(
            moneyline_tokens(main_markets, home=home, away=away, home_score=hs, away_score=aws)
        )
        if len([t for t in tokens if t["family"] == "moneyline"]) < 6:
            meta["skipped"].append({"family": "moneyline", "reason": "incomplete_moneyline_markets"})
    else:
        meta["skipped"].append({"family": "moneyline", "reason": "live_not_locked_until_ft"})
        meta["skipped"].append({"family": "spreads", "reason": "live_not_locked"})

    related: dict[str, Any] = {"more_markets": None, "exact_score": None}
    related_complete = False
    if related_from_cache:
        related = {
            "more_markets": cached.get("more_markets"),  # type: ignore[union-attr]
            "exact_score": cached.get("exact_score"),  # type: ignore[union-attr]
        }
        related_complete = True
    elif include_props or include_exact:
        slug = str(pm_h.get("slug") or "")
        if slug:
            try:
                related = discover_related_events(slug=slug, home=home, away=away, proxy=proxy)
                related_complete = True
                if meta["catalog_cache"] == "hit":
                    meta["catalog_cache"] = "partial"
                elif meta["catalog_cache"] not in ("miss", "partial"):
                    meta["catalog_cache"] = "miss"
            except pm.FetchError as e:
                meta["related_fetch_error"] = str(e)
                # Keep any partial siblings from an incomplete warm; do not mark complete.
                if cached:
                    related = {
                        "more_markets": cached.get("more_markets"),
                        "exact_score": cached.get("exact_score"),
                    }
                related_complete = False
        else:
            related_complete = True

    # Persist when we newly completed related discovery and/or filled main.
    if market_cache is not None and match_id and (ev or related_complete):
        already = bool(
            cached
            and cached.get("related_complete")
            and isinstance(cached.get("main_event"), dict)
        )
        improved = (related_complete and not (cached and cached.get("related_complete"))) or (
            isinstance(ev, dict) and not (cached and isinstance(cached.get("main_event"), dict))
        )
        if improved or (related_complete and isinstance(ev, dict) and not already):
            try:
                market_cache.put(
                    match_id,
                    {
                        "event_id": str(pm_h.get("event_id") or ""),
                        "slug": str(pm_h.get("slug") or ""),
                        "home": home,
                        "away": away,
                        "main_event": ev
                        if isinstance(ev, dict)
                        else (cached or {}).get("main_event"),
                        "more_markets": related.get("more_markets"),
                        "exact_score": related.get("exact_score"),
                        "related_complete": bool(
                            related_complete
                            and isinstance(
                                ev if isinstance(ev, dict) else (cached or {}).get("main_event"),
                                dict,
                            )
                        ),
                    },
                )
            except Exception:  # noqa: BLE001
                pass

    if include_props:
        more = related.get("more_markets")
        if more:
            meta["more_markets"] = {
                "id": str(more.get("id") or ""),
                "slug": more.get("slug") or "",
                "title": more.get("title") or "",
            }
            prop_markets = [
                enrich_market(m) for m in (more.get("markets") or []) if isinstance(m, dict)
            ]
            if mode == "ft":
                tokens.extend(
                    spread_tokens(prop_markets, home=home, away=away, home_score=hs, away_score=aws)
                )
            dqd = ctx.get("dongqiudi") or {}
            tokens.extend(
                totals_tokens(
                    prop_markets,
                    home=home,
                    away=away,
                    home_score=hs,
                    away_score=aws,
                    home_half=dqd.get("home_half"),
                    away_half=dqd.get("away_half"),
                    mode=mode,
                )
            )
            tokens.extend(
                btts_tokens(
                    prop_markets,
                    home_score=hs,
                    away_score=aws,
                    home_half=dqd.get("home_half"),
                    away_half=dqd.get("away_half"),
                    mode=mode,
                )
            )
        else:
            meta["skipped"].append({"family": "props", "reason": "not_listed"})

    if include_exact:
        ex = related.get("exact_score")
        if ex:
            meta["exact_score"] = {
                "id": str(ex.get("id") or ""),
                "slug": ex.get("slug") or "",
                "title": ex.get("title") or "",
            }
            exact_markets = [
                enrich_market(m) for m in (ex.get("markets") or []) if isinstance(m, dict)
            ]
            tokens.extend(
                exact_score_tokens(
                    exact_markets,
                    home=home,
                    away=away,
                    home_score=hs,
                    away_score=aws,
                    mode=mode,
                )
            )
        else:
            meta["skipped"].append({"family": "exact_score", "reason": "not_listed"})

    return tokens, meta


def quote_tokens(
    token_rows: list[dict[str, Any]],
    *,
    proxy: str | None | object = ...,
    eps: float = DEFAULT_EPS,
    fee_rate: float = SPORTS_TAKER_FEE_RATE,
    min_net: float = DEFAULT_MIN_NET,
    trade_executor: Any | None = None,
    event_key_str: str = "",
    match_meta: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    ids = [r["token_id"] for r in token_rows if r.get("token_id")]
    books = fetch_books(ids, proxy=proxy)
    out: list[dict[str, Any]] = []
    for row in token_rows:
        tid = row["token_id"]
        book = books.get(tid) or {"book_missing": True, "best_bid": None, "best_ask": None}
        mis, reason, econ = flag_misprice(
            str(row.get("settlement") or ""),
            book,
            eps=eps,
            fee_rate=fee_rate,
            min_net=min_net,
        )
        item = {
            **row,
            "best_bid": book.get("best_bid"),
            "best_bid_size": book.get("best_bid_size"),
            "best_ask": book.get("best_ask"),
            "best_ask_size": book.get("best_ask_size"),
            "spread": book.get("spread"),
            "midpoint": book.get("midpoint"),
            "last_trade_price": book.get("last_trade_price"),
            "bids_top": book.get("bids_top") or [],
            "asks_top": book.get("asks_top") or [],
            "tick_size": book.get("tick_size") or "",
            "neg_risk": book.get("neg_risk"),
            "book_ts": book.get("book_ts") or "",
            "book_missing": bool(book.get("book_missing")),
            "misprice": mis,
            "misprice_reason": reason,
            "gross_edge": econ.get("gross_edge"),
            "fee": econ.get("fee"),
            "net_edge": econ.get("net_edge"),
            "trade": econ.get("trade"),
        }
        # Lowest latency: trade in-process right after misprice (do not read JSONL).
        if mis and trade_executor is not None:
            try:
                trade_row = trade_executor.maybe_trade(
                    item,
                    event_key=event_key_str,
                    match_meta=match_meta,
                    event_type=str((match_meta or {}).get("event_type") or ""),
                )
                if trade_row is not None:
                    item["trade_attempt"] = {
                        "status": trade_row.get("status"),
                        "success": trade_row.get("success"),
                        "skip_reason": trade_row.get("skip_reason"),
                        "plan": trade_row.get("plan"),
                        "live": trade_row.get("live"),
                    }
            except Exception as e:  # noqa: BLE001
                item["trade_attempt"] = {"status": "error", "skip_reason": str(e)}
        out.append(item)
    return out


def build_bundle(
    ctx: dict[str, Any],
    quotes: list[dict[str, Any]],
    discovery: dict[str, Any],
) -> dict[str, Any]:
    ev = ctx.get("event") or {}
    mode = (discovery or {}).get("mode") or (
        "live" if ev.get("type") == "score_change" else "ft"
    )
    winner = winner_from_score(ctx["home_score"], ctx["away_score"]) if mode == "ft" else None
    opps = [q for q in quotes if q.get("misprice")]
    return {
        "quoted_at": now_cn_iso(),
        "source": "polymarket-quote",
        "trigger": ev.get("type") or "manual",
        "mode": mode,
        "match_id": ev.get("match_id") or (ctx.get("dongqiudi") or {}).get("id") or "",
        "event_key": event_key(ev),
        "home": ctx["home"],
        "away": ctx["away"],
        "home_score": ctx["home_score"],
        "away_score": ctx["away_score"],
        "prev_score": ev.get("prev"),
        "winner": winner,
        "polymarket": {
            "event_id": (ctx.get("polymarket") or {}).get("event_id") or "",
            "slug": (ctx.get("polymarket") or {}).get("slug") or "",
            "url": (ctx.get("polymarket") or {}).get("url") or "",
        },
        "discovery": discovery,
        "count": len(quotes),
        "opportunity_count": len(opps),
        "quotes": quotes,
        "opportunities": [
            {
                "market_key": q.get("market_key"),
                "family": q.get("family"),
                "outcome": q.get("outcome"),
                "settlement": q.get("settlement"),
                "token_id": q.get("token_id"),
                "trade": q.get("trade"),
                "best_bid": q.get("best_bid"),
                "best_ask": q.get("best_ask"),
                "gross_edge": q.get("gross_edge"),
                "fee": q.get("fee"),
                "net_edge": q.get("net_edge"),
                "misprice_reason": q.get("misprice_reason"),
                "question": q.get("question"),
            }
            for q in opps
        ],
    }


def persist_bundle(root: Path, bundle: dict[str, Any]) -> None:
    ddir = data_dir(root)
    write_json(ddir / "latest.json", bundle)
    append_jsonl(ddir / "quotes.jsonl", [bundle])
    if bundle.get("opportunities"):
        append_jsonl(
            ddir / "opportunities.jsonl",
            [
                {
                    "quoted_at": bundle.get("quoted_at"),
                    "match_id": bundle.get("match_id"),
                    "home": bundle.get("home"),
                    "away": bundle.get("away"),
                    "home_score": bundle.get("home_score"),
                    "away_score": bundle.get("away_score"),
                    **opp,
                }
                for opp in bundle["opportunities"]
            ],
        )


def load_cursor(root: Path) -> dict[str, Any]:
    return load_json(data_dir(root) / "cursor.json", {"processed_keys": []}) or {
        "processed_keys": []
    }


def save_cursor(root: Path, cursor: dict[str, Any]) -> None:
    write_json(data_dir(root) / "cursor.json", cursor)


def quote_bridge_event(
    root: Path,
    ev: dict[str, Any],
    *,
    proxy: str | None | object = ...,
    include_props: bool = True,
    include_exact: bool = True,
    eps: float = DEFAULT_EPS,
    fee_rate: float = SPORTS_TAKER_FEE_RATE,
    min_net: float = DEFAULT_MIN_NET,
    persist: bool = True,
    trade_executor: Any | None = None,
    market_cache: Any | None = None,
) -> dict[str, Any]:
    ctx = join_ft_context(root, ev)
    if ctx["home_score"] is None or ctx["away_score"] is None:
        raise QuoteError("missing home_score/away_score on event")
    if not (ctx.get("polymarket") or {}).get("event_id") and not (ctx.get("polymarket") or {}).get(
        "slug"
    ):
        raise QuoteError("missing polymarket event_id/slug after join")
    mode = "live" if ev.get("type") == "score_change" else "ft"
    tokens, discovery = collect_target_tokens(
        ctx,
        proxy=proxy,
        include_props=include_props,
        include_exact=include_exact,
        mode=mode,
        market_cache=market_cache,
    )
    ek = event_key(ev)
    match_meta = {
        "match_id": ev.get("match_id") or (ctx.get("dongqiudi") or {}).get("id") or "",
        "home": ctx.get("home") or "",
        "away": ctx.get("away") or "",
        "home_score": ctx.get("home_score"),
        "away_score": ctx.get("away_score"),
        "event_type": str(ev.get("type") or ""),
    }
    quote_kw = dict(
        proxy=proxy,
        eps=eps,
        fee_rate=fee_rate,
        min_net=min_net,
        trade_executor=trade_executor,
        event_key_str=ek,
        match_meta=match_meta,
    )
    # Live: books+trade totals/BTTS first, then exact (and any remainder).
    if mode == "live":
        phase_a = [t for t in tokens if t.get("family") in ("totals", "btts")]
        phase_a_ids = {id(t) for t in phase_a}
        phase_b = [t for t in tokens if id(t) not in phase_a_ids]
        quotes: list[dict[str, Any]] = []
        if phase_a:
            quotes.extend(quote_tokens(phase_a, **quote_kw))
        if phase_b:
            quotes.extend(quote_tokens(phase_b, **quote_kw))
        discovery["quote_phases"] = ["totals_btts", "exact_rest"]
    else:
        quotes = quote_tokens(tokens, **quote_kw)
    bundle = build_bundle(ctx, quotes, discovery)
    if persist:
        persist_bundle(root, bundle)
    return bundle


def quote_finished_event(
    root: Path,
    ev: dict[str, Any],
    *,
    proxy: str | None | object = ...,
    include_props: bool = True,
    include_exact: bool = True,
    eps: float = DEFAULT_EPS,
    fee_rate: float = SPORTS_TAKER_FEE_RATE,
    min_net: float = DEFAULT_MIN_NET,
    persist: bool = True,
    trade_executor: Any | None = None,
    market_cache: Any | None = None,
) -> dict[str, Any]:
    """Alias for FT / manual quoting."""
    if not ev.get("type"):
        ev = dict(ev)
        ev["type"] = "match_finished"
    return quote_bridge_event(
        root,
        ev,
        proxy=proxy,
        include_props=include_props,
        include_exact=include_exact,
        eps=eps,
        fee_rate=fee_rate,
        min_net=min_net,
        persist=persist,
        trade_executor=trade_executor,
        market_cache=market_cache,
    )


def process_bridge_events(
    root: Path,
    *,
    proxy: str | None | object = ...,
    include_props: bool = True,
    include_exact: bool = True,
    eps: float = DEFAULT_EPS,
    fee_rate: float = SPORTS_TAKER_FEE_RATE,
    min_net: float = DEFAULT_MIN_NET,
    force: bool = False,
    trade_executor: Any | None = None,
    market_cache: Any | None = None,
) -> list[dict[str, Any]]:
    cursor = load_cursor(root)
    seen = set(cursor.get("processed_keys") or [])
    bundles: list[dict[str, Any]] = []

    # Retry any live flatten that failed / partial-filled on a prior tick.
    if trade_executor is not None:
        try:
            retried = list(trade_executor.retry_pending_flattens() or [])
            if retried:
                bundles.append(
                    {
                        "quoted_at": now_cn_iso(),
                        "trigger": "flatten_retry",
                        "flatten_attempts": retried,
                        "flatten_count": len(retried),
                    }
                )
        except Exception as e:  # noqa: BLE001
            print(f"ALERT flatten retry sweep failed: {e}", flush=True)

    for ev in load_bridge_quote_events(root):
        key = event_key(ev)
        if not force and key in seen:
            continue
        # Score disallow / correction: flatten open buy_win lots BEFORE quoting.
        flatten_rows: list[dict[str, Any]] = []
        if trade_executor is not None:
            try:
                flatten_rows = list(trade_executor.maybe_flatten_for_event(ev) or [])
            except Exception as e:  # noqa: BLE001
                flatten_rows = [
                    {
                        "quoted_at": now_cn_iso(),
                        "status": "flatten_error",
                        "error": str(e),
                        "match_id": ev.get("match_id"),
                        "event_key": key,
                    }
                ]
        try:
            bundle = quote_bridge_event(
                root,
                ev,
                proxy=proxy,
                include_props=include_props,
                include_exact=include_exact,
                eps=eps,
                fee_rate=fee_rate,
                min_net=min_net,
                persist=True,
                trade_executor=trade_executor,
                market_cache=market_cache,
            )
            if flatten_rows:
                bundle["flatten_attempts"] = flatten_rows
                bundle["flatten_count"] = len(flatten_rows)
            bundles.append(bundle)
            # Data-only: after score_change buy_win, schedule 10s×6 book samples.
            try:
                from post_goal_sampler import get_active_sampler

                sampler = get_active_sampler()
                if sampler is not None:
                    sampler.enqueue_from_bundle(
                        bundle,
                        eps=eps,
                        fee_rate=fee_rate,
                        min_net=min_net,
                        proxy=proxy,
                    )
            except Exception as e:  # noqa: BLE001
                print(f"post-goal sampler enqueue failed: {e}", flush=True)
            seen.add(key)
            if market_cache is not None and ev.get("type") == "match_finished":
                mid = str(ev.get("match_id") or bundle.get("match_id") or "")
                if mid:
                    try:
                        market_cache.drop(mid)
                    except Exception:  # noqa: BLE001
                        pass
        except Exception as e:  # noqa: BLE001
            err_row: dict[str, Any] = {
                "quoted_at": now_cn_iso(),
                "error": str(e),
                "event_key": key,
                "match_id": ev.get("match_id"),
                "trigger": ev.get("type"),
            }
            if flatten_rows:
                err_row["flatten_attempts"] = flatten_rows
            bundles.append(err_row)
            # Do not mark processed / drop cache on failure — retry next tick.
            print(
                f"ALERT quote failed (will retry) key={key}: {e}",
                flush=True,
            )
    cursor["processed_keys"] = sorted(seen)[-1000:]
    cursor["updated_at"] = now_cn_iso()
    save_cursor(root, cursor)
    return bundles


def find_match_row(
    root: Path,
    *,
    match_id: str | None = None,
    event_id: str | None = None,
) -> dict[str, Any] | None:
    for r in load_bridge_matches(root):
        dqd = r.get("dongqiudi") or {}
        pm = r.get("polymarket") or {}
        if match_id and str(dqd.get("id") or "") == str(match_id):
            return r
        if event_id and str(pm.get("event_id") or "") == str(event_id):
            return r
    return None


def synthetic_ft_from_row(
    row: dict[str, Any],
    *,
    home_score: int,
    away_score: int,
) -> dict[str, Any]:
    dqd = row.get("dongqiudi") or {}
    pm = row.get("polymarket") or {}
    return {
        "type": "match_finished",
        "ts": now_cn_iso(),
        "match_id": str(dqd.get("id") or ""),
        "prev_status": "playing",
        "status": "played",
        "status_display": "Played",
        "league": pm.get("league") or dqd.get("league") or "",
        "home": pm.get("home") or dqd.get("home") or "",
        "away": pm.get("away") or dqd.get("away") or "",
        "home_score": home_score,
        "away_score": away_score,
        "kickoff_beijing": row.get("kickoff_beijing") or "",
        "official_clock": "FT",
        "polymarket": {
            "event_id": pm.get("event_id") or "",
            "slug": pm.get("slug") or "",
            "url": pm.get("url") or "",
            "condition_ids": list(pm.get("condition_ids") or []),
            "market_refs": list(pm.get("market_refs") or []),
        },
    }


BRIDGE_BOARD_URL = "http://127.0.0.1:8789"
_OWNED_BRIDGE: Any = None


def _http_json(method: str, url: str, *, timeout: float = 2.0) -> dict[str, Any] | None:
    import urllib.request

    req = urllib.request.Request(
        url,
        method=method,
        data=b"{}" if method == "POST" else None,
        headers={"Content-Type": "application/json", "User-Agent": UA},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
        return json.loads(raw) if raw else {}
    except Exception:  # noqa: BLE001
        return None


def ensure_upstream_bridge(
    root: Path,
    *,
    prefer_board: bool = True,
) -> dict[str, Any]:
    """Start match-bridge (which starts dongqiudi-match + polymarket-soccer).

    Prefer Bridge Board on :8789 if it is already up; otherwise start an
    in-process BridgeRuntime owned by this quote process.
    """
    global _OWNED_BRIDGE

    if prefer_board:
        st = _http_json("GET", f"{BRIDGE_BOARD_URL}/api/status", timeout=1.5)
        if st is not None:
            if st.get("running"):
                return {
                    "ok": True,
                    "mode": "bridge_board",
                    "already": True,
                    "url": BRIDGE_BOARD_URL,
                    "dqd_ticks": st.get("dqd_ticks"),
                    "pm_ticks": st.get("pm_ticks"),
                }
            started = _http_json("POST", f"{BRIDGE_BOARD_URL}/api/bridge/start", timeout=120)
            if started and started.get("ok"):
                return {
                    "ok": True,
                    "mode": "bridge_board",
                    "already": bool(started.get("already")),
                    "url": BRIDGE_BOARD_URL,
                    "dqd_ticks": started.get("dqd_ticks"),
                    "pm_ticks": started.get("pm_ticks"),
                }

    bridge_scripts = root / ".cursor" / "skills" / "match-bridge" / "scripts"
    if str(bridge_scripts) not in sys.path:
        sys.path.insert(0, str(bridge_scripts))
    import bridge_lib as bridge  # type: ignore

    if _OWNED_BRIDGE is None:
        _OWNED_BRIDGE = bridge.BridgeRuntime(root)
    result = _OWNED_BRIDGE.start()
    return {
        "ok": True,
        "mode": "in_process",
        "already": bool(result.get("already")),
        "dqd_interval": _OWNED_BRIDGE.dqd_interval,
        "dqd_idle_interval": _OWNED_BRIDGE.dqd_idle_interval,
        "pm_interval": _OWNED_BRIDGE.pm_interval,
        "owned": True,
    }


def stop_owned_bridge() -> None:
    """Stop in-process bridge started by ensure_upstream_bridge (if any)."""
    global _OWNED_BRIDGE
    if _OWNED_BRIDGE is None:
        return
    try:
        _OWNED_BRIDGE.stop()
    except Exception:  # noqa: BLE001
        pass
    _OWNED_BRIDGE = None
