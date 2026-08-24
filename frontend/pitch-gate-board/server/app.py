#!/usr/bin/env python3
"""Pitch Gate Board: DOM∧AF buy and AF∨DOM flatten trails (no screenshots)."""

from __future__ import annotations

import json
import mimetypes
import sys
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

MODULE_DIR = Path(__file__).resolve().parents[1]
FRONTEND_DIR = MODULE_DIR.parent
ROOT = FRONTEND_DIR.parent
PUBLIC = MODULE_DIR / "public"
SRC = MODULE_DIR / "src"

HOST = "127.0.0.1"
PORT = 8791
MODULE_ID = "pitch-gate-board"

DATA = ROOT / "data" / "pm-quote"
BRIDGE_EVENTS_PATH = ROOT / "data" / "bridge" / "events.jsonl"
OBSERVE_PATH = DATA / "dqd_stream_observe.jsonl"
AF_OBSERVE_PATH = DATA / "af_observe.jsonl"
BOOK_OBSERVE_PATH = DATA / "book_context_observe.jsonl"
JUDGE_PATH = DATA / "pitch_state_judge.jsonl"
QUOTES_PATH = DATA / "quotes.jsonl"

# Cap how much history we scan for the board.
_MAX_GOALS = 5000
_MAX_OBSERVE_LINES = 250000
_MAX_AF_OBSERVE_LINES = 120000
_MAX_BOOK_OBSERVE_LINES = 120000
_MAX_JUDGE_LINES = 50000
_MAX_BRIDGE_LINES = 20000
_MAX_QUOTES_LINES = 20000

_QUOTE_CANCEL_MODES = {
    "pitch_gate_canceled",
    "dqd_reversal_pitch_gate_canceled",
}

_GOALS_LOCK = threading.Lock()
# (data_stamp, limit, payload)
_GOALS_CACHE: tuple[tuple[int, ...], int, dict[str, Any]] | None = None


def _observe_stamp() -> tuple[int, ...]:
    stamp: list[int] = []
    for path in (
        OBSERVE_PATH,
        AF_OBSERVE_PATH,
        BOOK_OBSERVE_PATH,
        JUDGE_PATH,
        QUOTES_PATH,
        BRIDGE_EVENTS_PATH,
    ):
        try:
            st = path.stat()
            stamp.extend((int(st.st_mtime_ns), int(st.st_size)))
        except OSError:
            stamp.extend((0, 0))
    return tuple(stamp)


def json_response(handler: BaseHTTPRequestHandler, code: int, payload: Any) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(body)


def _safe_under(root: Path, candidate: Path) -> Path | None:
    try:
        resolved = candidate.resolve()
        root_r = root.resolve()
    except OSError:
        return None
    try:
        resolved.relative_to(root_r)
    except ValueError:
        return None
    if not resolved.is_file():
        return None
    return resolved


def serve_file(handler: BaseHTTPRequestHandler, path: Path) -> None:
    ctype = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
    if path.suffix == ".js":
        ctype = "text/javascript; charset=utf-8"
    elif path.suffix == ".css":
        ctype = "text/css; charset=utf-8"
    elif path.suffix == ".html":
        ctype = "text/html; charset=utf-8"
    elif path.suffix.lower() in {".jpg", ".jpeg"}:
        ctype = "image/jpeg"
    elif path.suffix.lower() == ".png":
        ctype = "image/png"
    data = path.read_bytes()
    handler.send_response(200)
    handler.send_header("Content-Type", ctype)
    handler.send_header("Content-Length", str(len(data)))
    handler.send_header("Cache-Control", "no-cache")
    handler.end_headers()
    handler.wfile.write(data)


def _read_jsonl_tail(path: Path, *, max_lines: int) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return []
    lines = [ln for ln in raw.splitlines() if ln.strip()]
    if max_lines > 0 and len(lines) > max_lines:
        lines = lines[-max_lines:]
    out: list[dict[str, Any]] = []
    for ln in lines:
        try:
            row = json.loads(ln)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            out.append(row)
    return out


def _norm_path(raw: Any) -> str:
    text = str(raw or "").strip()
    if not text:
        return ""
    try:
        return str(Path(text).resolve())
    except OSError:
        return text


def _index_judges(rows: list[dict[str, Any]]) -> tuple[dict[tuple[str, str, Any], dict], dict[str, dict]]:
    """Latest judge per (match_id, event_key, sample_i) and by absolute frame path."""
    by_key: dict[tuple[str, str, Any], dict[str, Any]] = {}
    by_path: dict[str, dict[str, Any]] = {}
    for row in rows:
        mid = str(row.get("match_id") or "")
        ek = str(row.get("event_key") or "")
        per = row.get("per_frame")
        frames: list[dict[str, Any]]
        if isinstance(per, list) and per:
            frames = [f for f in per if isinstance(f, dict)]
        else:
            frames = [
                {
                    "sample_i": row.get("sample_i"),
                    "elapsed_s": row.get("elapsed_s"),
                    "path": row.get("path") or row.get("frame_path"),
                    "play_state": row.get("play_state"),
                    "confidence": row.get("confidence"),
                    "stopped_reason": row.get("stopped_reason"),
                    "evidence": row.get("evidence"),
                    "frame_type": row.get("frame_type"),
                }
            ]
        for fr in frames:
            sample_i = fr.get("sample_i")
            path = _norm_path(fr.get("path") or fr.get("frame_path"))
            verdict = {
                "play_state": str(fr.get("play_state") or row.get("play_state") or "unclear"),
                "confidence": fr.get("confidence", row.get("confidence")),
                "stopped_reason": fr.get("stopped_reason", row.get("stopped_reason")),
                "evidence": fr.get("evidence") or row.get("evidence") or [],
                "frame_type": fr.get("frame_type") or row.get("frame_type"),
                "decision_source": row.get("decision_source"),
                "judged_at": row.get("judged_at"),
                "latency_ms": row.get("latency_ms"),
            }
            if mid and ek and sample_i is not None:
                by_key[(mid, ek, sample_i)] = verdict
            if path:
                by_path[path] = verdict
    return by_key, by_path


def _lookup_judge(
    *,
    mid: str,
    ek: str,
    sample_i: Any,
    frame_path: str,
    by_key: dict[tuple[str, str, Any], dict],
    by_path: dict[str, dict],
) -> dict[str, Any] | None:
    if frame_path:
        hit = by_path.get(_norm_path(frame_path))
        if hit:
            return hit
    if mid and ek and sample_i is not None:
        return by_key.get((mid, ek, sample_i))
    return None


def _frame_af(frame: dict[str, Any]) -> dict[str, Any]:
    af = frame.get("af")
    return af if isinstance(af, dict) else {}


def _frame_var(frame: dict[str, Any]) -> bool:
    judge = frame.get("judge")
    if not isinstance(judge, dict):
        return False
    reason = str(judge.get("stopped_reason") or "").strip().lower()
    if reason == "var":
        return True
    for item in judge.get("evidence") or []:
        text = str(item or "")
        if "VAR" in text or text.strip().lower() == "var":
            return True
    return False


def _frame_dom_in_play(frame: dict[str, Any]) -> bool:
    judge = frame.get("judge")
    return isinstance(judge, dict) and str(judge.get("play_state") or "") == "in_play"


def _frame_aligned_buy(frame: dict[str, Any]) -> bool:
    """Same-tick DOM in_play ∧ AF score_match (the live buy condition)."""
    return _frame_dom_in_play(frame) and _frame_af(frame).get("score_match") is True


def _frame_or_flatten(frame: dict[str, Any]) -> bool:
    """Reversal flatten: AF score_match ∨ DOM board score (not in_play)."""
    if _frame_af(frame).get("score_match") is True:
        return True
    return frame.get("board_score_match") is True


def _goal_verdict(frames: list[dict[str, Any]], *, quote_mode: str | None = None) -> str:
    """Aggregate badge for a buy-side goal (not a reversal-observe row)."""
    mode = str(quote_mode or "")
    if mode == "pitch_gate_confirmed":
        return "aligned_buy"
    if mode == "pitch_gate_var_veto":
        return "var_veto"
    if mode == "pitch_gate_buy_revoked":
        return "reversed"
    judges = [
        f.get("judge")
        for f in frames
        if isinstance(f.get("judge"), dict)
    ]
    states = [str(j.get("play_state") or "") for j in judges]
    if any(_frame_aligned_buy(f) for f in frames):
        return "aligned_buy"
    if any(_frame_var(f) for f in frames):
        return "var_veto"
    if any(_frame_dom_in_play(f) for f in frames):
        return "wait_af"
    if frames and all(f.get("ok") is False for f in frames):
        return "capture_failed"
    if not states:
        return "pending_judge"
    if any(s == "stopped" for s in states) and all(
        s in ("stopped", "unclear", "") for s in states
    ):
        return "stopped"
    if states and all(s in ("unclear", "") for s in states):
        return "waiting_in_play"
    return "mixed"


def _score_pair(obj: Any) -> tuple[int | None, int | None]:
    if not isinstance(obj, dict):
        return None, None
    try:
        h = obj.get("home")
        a = obj.get("away")
        return (int(h) if h is not None and h != "" else None,
                int(a) if a is not None and a != "" else None)
    except (TypeError, ValueError):
        return None, None


def _parse_score_transition(
    event_key: str,
) -> tuple[tuple[int, int] | None, tuple[int, int] | None]:
    """Parse ``score_change|<mid>|<ph>-<pa>-><ch>-<ca>`` → (prev, curr)."""
    parts = str(event_key or "").split("|")
    if len(parts) < 3:
        return None, None
    trans = next((p for p in parts if "->" in p), "")
    if "->" not in trans:
        return None, None
    left, right = trans.split("->", 1)

    def _one(raw: str) -> tuple[int, int] | None:
        raw = raw.strip()
        if "-" not in raw:
            return None
        hs, as_ = raw.split("-", 1)
        try:
            return int(hs), int(as_)
        except (TypeError, ValueError):
            return None

    return _one(left), _one(right)


def _invert_score_change_key(event_key: str) -> str | None:
    """Swap ``from->to``; keep match id and optional ``dqd_ts``."""
    parts = str(event_key or "").split("|")
    trans_i = next((i for i, p in enumerate(parts) if "->" in p), None)
    if trans_i is None:
        return None
    left, right = parts[trans_i].split("->", 1)
    if not left.strip() or not right.strip():
        return None
    parts = list(parts)
    parts[trans_i] = f"{right}->{left}"
    return "|".join(parts)


def _event_key_stem(event_key: str) -> str:
    """``score_change|{mid}|{from}->{to}|{ts}`` → drop the timestamp."""
    parts = str(event_key or "").split("|")
    trans_i = next((i for i, p in enumerate(parts) if "->" in p), None)
    if trans_i is None:
        return str(event_key or "")
    if parts[0] == "score_change" and len(parts) > 1:
        return f"score_change|{parts[1]}|{parts[trans_i]}"
    return "|".join(parts[: trans_i + 1])


def _invert_event_key_stem(event_key: str) -> str | None:
    inv = _invert_score_change_key(event_key)
    if not inv:
        return None
    return _event_key_stem(inv)


_DT_MIN = datetime.min.replace(tzinfo=timezone.utc)


def _parse_iso_ts(raw: Any) -> datetime | None:
    text = str(raw or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _event_happened_at(*candidates: Any) -> datetime | None:
    """First parseable ISO timestamp from event_key tails, dqd_ts, quoted_at, etc."""
    for raw in candidates:
        if raw is None or raw == "":
            continue
        text = str(raw)
        if "|" in text:
            tail = text.rsplit("|", 1)[-1].strip()
            parsed = _parse_iso_ts(tail)
            if parsed:
                return parsed
        parsed = _parse_iso_ts(text)
        if parsed:
            return parsed
    return None


def _ts_on_or_after(later: datetime | None, earlier: datetime | None) -> bool:
    """True only when both sides parse and later ≥ earlier. Missing ts → False."""
    if later is None or earlier is None:
        return False
    return later >= earlier


def _reversal_undoes_goal(
    rev: dict[str, Any],
    *,
    goal_prev: tuple[int, int] | None,
    goal_curr: tuple[int, int] | None,
) -> bool:
    """True when reverse is the mirror of this goal (e.g. 1-0→2-0 undone by 2-0→1-0)."""
    if goal_prev is None or goal_curr is None:
        return False
    rp = _score_pair(rev.get("prev"))
    rc = _score_pair(rev.get("curr"))
    return rp == goal_curr and rc == goal_prev


def _pair_goal_reversals(
    groups: dict[str, dict[str, Any]],
    rev_by_match: dict[str, list[dict[str, Any]]],
) -> dict[str, dict[str, Any]]:
    """Each DQD reverse undoes at most the latest matching goal *before* it.

    Same-score repeats (0-0→0-1 later in the match) must not inherit an earlier
    0-1→0-0 reverse just because the score pair mirrors.
    """
    goals_by_match: dict[str, list[tuple[datetime, str]]] = {}
    for ek, g in groups.items():
        if (
            g.get("kind") == "reversal_observe"
            or bool(g.get("is_reversal"))
            or bool(g.get("observe_only"))
        ):
            continue
        mid = str(g.get("match_id") or "")
        ts = _event_happened_at(ek, g.get("dqd_ts"))
        if not mid or ts is None:
            continue
        goals_by_match.setdefault(mid, []).append((ts, ek))

    paired: dict[str, dict[str, Any]] = {}
    for mid, goals in goals_by_match.items():
        goals.sort(key=lambda item: item[0])
        used: set[str] = set()
        for rev in rev_by_match.get(mid) or []:
            rev_ts = _event_happened_at(rev.get("event_key"), rev.get("ts"))
            if rev_ts is None:
                continue
            best_ek: str | None = None
            for gts, ek in goals:
                if ek in used or gts > rev_ts:
                    continue
                goal_prev, goal_curr = _parse_score_transition(ek)
                if not _reversal_undoes_goal(
                    rev, goal_prev=goal_prev, goal_curr=goal_curr
                ):
                    continue
                best_ek = ek
            if not best_ek:
                continue
            used.add(best_ek)
            paired[best_ek] = {
                "source": "dqd_reversal",
                "ts": rev.get("ts"),
                "prev": rev.get("prev"),
                "curr": rev.get("curr"),
                "home_score": rev.get("home_score"),
                "away_score": rev.get("away_score"),
                "event_key": rev.get("event_key"),
            }
    return paired


def _load_reversal_index() -> tuple[
    list[dict[str, Any]],
    dict[str, list[dict[str, Any]]],
    dict[str, dict[str, Any]],
    dict[str, dict[str, Any]],
    dict[str, dict[str, Any]],
    dict[str, dict[str, Any]],
]:
    """Bridge reversals + quote rows (cancel / confirm / flatten / hold).

    Returns:
      recent_reversals (newest first),
      reversals_by_match_id (all, chronological),
      cancel_by_event_key,
      cancel_by_stem (timestamp-stripped, plus invert),
      quote_by_event_key,
      quote_by_stem
    """
    recent: list[dict[str, Any]] = []
    by_match: dict[str, list[dict[str, Any]]] = {}
    for row in _read_jsonl_tail(BRIDGE_EVENTS_PATH, max_lines=_MAX_BRIDGE_LINES):
        if str(row.get("type") or "") != "score_change":
            continue
        if not bool(row.get("is_reversal")):
            continue
        mid = str(row.get("match_id") or "").strip()
        if not mid:
            continue
        prev = row.get("prev") if isinstance(row.get("prev"), dict) else {}
        curr = row.get("curr") if isinstance(row.get("curr"), dict) else {}
        item = {
            "type": "score_change",
            "is_reversal": True,
            "ts": row.get("ts"),
            "match_id": mid,
            "home": row.get("home") or "",
            "away": row.get("away") or "",
            "league": row.get("league") or "",
            "prev": prev,
            "curr": curr,
            "home_score": row.get("home_score"),
            "away_score": row.get("away_score"),
            "event_key": (
                f"score_change|{mid}|"
                f"{prev.get('home')}-{prev.get('away')}->"
                f"{curr.get('home')}-{curr.get('away')}|"
                f"{row.get('ts') or ''}"
            ),
        }
        recent.append(item)
        by_match.setdefault(mid, []).append(item)
    recent.reverse()  # newest first for UI toasts

    cancel_by_key: dict[str, dict[str, Any]] = {}
    cancel_by_stem: dict[str, dict[str, Any]] = {}
    quote_by_key: dict[str, dict[str, Any]] = {}
    quote_by_stem: dict[str, dict[str, Any]] = {}
    for row in _read_jsonl_tail(QUOTES_PATH, max_lines=_MAX_QUOTES_LINES):
        mode = str(row.get("mode") or "")
        key = str(row.get("event_key") or "").strip()
        mid = str(row.get("match_id") or "").strip()
        if not key:
            continue
        pg = row.get("pitch_gate") if isinstance(row.get("pitch_gate"), dict) else {}
        item = {
            "mode": mode,
            "ts": row.get("quoted_at") or row.get("ts"),
            "match_id": mid,
            "event_key": key,
            "home": row.get("home") or "",
            "away": row.get("away") or "",
            "home_score": row.get("home_score"),
            "away_score": row.get("away_score"),
            "pitch_gate": pg,
            "flatten_count": row.get("flatten_count"),
            "reason": pg.get("reason") if pg else mode,
        }
        stem = _event_key_stem(key)
        prev = quote_by_key.get(key)
        keep_flatten = (
            prev
            and prev.get("mode") == "pitch_gate_flatten_or"
            and mode != "pitch_gate_flatten_or"
        )
        if not keep_flatten:
            quote_by_key[key] = item
            if stem:
                quote_by_stem[stem] = item
        is_cancel = mode in _QUOTE_CANCEL_MODES or "pitch_gate_canceled" in mode
        if is_cancel:
            cancel_by_key[key] = item
            if stem:
                cancel_by_stem[stem] = item
            inv = _invert_event_key_stem(key)
            if inv and inv not in cancel_by_stem:
                cancel_by_stem[inv] = item
    return recent, by_match, cancel_by_key, cancel_by_stem, quote_by_key, quote_by_stem


def _lookup_quote(
    event_key: str,
    by_key: dict[str, dict[str, Any]],
    by_stem: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    if event_key in by_key:
        return by_key[event_key]
    stem = _event_key_stem(event_key)
    if stem and stem in by_stem:
        return by_stem[stem]
    return None


def _lookup_cancel(
    event_key: str,
    by_key: dict[str, dict[str, Any]],
    by_stem: dict[str, dict[str, Any]],
    *,
    goal_ts: datetime | None = None,
) -> dict[str, Any] | None:
    """Cancel for this goal session only — not an earlier same-stem reverse."""
    if event_key in by_key:
        return by_key[event_key]
    hits: list[dict[str, Any]] = []
    stem = _event_key_stem(event_key)
    if stem and stem in by_stem:
        hits.append(by_stem[stem])
    inv = _invert_event_key_stem(event_key)
    if inv and inv in by_stem:
        hit = by_stem[inv]
        if hit not in hits:
            hits.append(hit)
    for hit in hits:
        hit_key = str(hit.get("event_key") or "")
        if hit_key == event_key:
            return hit
        cancel_ts = _event_happened_at(hit_key, hit.get("ts"))
        if _ts_on_or_after(cancel_ts, goal_ts):
            return hit
    return None


def _find_linked_event_key(
    event_key: str,
    *,
    match_id: str,
    groups: dict[str, dict[str, Any]],
    prefer_observe: bool,
) -> str | None:
    inv = _invert_event_key_stem(event_key)
    if not inv:
        return None
    self_ts = _event_happened_at(
        event_key, (groups.get(event_key) or {}).get("dqd_ts")
    )
    candidates: list[str] = []
    for ek, g in groups.items():
        if ek == event_key:
            continue
        if str(g.get("match_id") or "") != match_id:
            continue
        if _event_key_stem(ek) != inv:
            continue
        other_ts = _event_happened_at(ek, g.get("dqd_ts"))
        if self_ts is not None and other_ts is not None:
            if prefer_observe:
                if other_ts < self_ts:
                    continue
            elif other_ts > self_ts:
                continue
        candidates.append(ek)
    if not candidates:
        return None

    def _ts(ek: str) -> datetime:
        return _event_happened_at(ek, groups[ek].get("dqd_ts")) or _DT_MIN

    if prefer_observe:
        obs = [ek for ek in candidates if groups[ek].get("kind") == "reversal_observe"]
        pool = obs or candidates
        return min(pool, key=_ts)
    goals = [ek for ek in candidates if groups[ek].get("kind") != "reversal_observe"]
    pool = goals or candidates
    return max(pool, key=_ts)


def _index_odds_grades() -> dict[tuple[str, Any], dict[str, Any]]:
    """Latest Odds grade per (event_key, sample_i) from the sidecar jsonl."""
    by_tick: dict[tuple[str, Any], dict[str, Any]] = {}
    for row in _read_jsonl_tail(BOOK_OBSERVE_PATH, max_lines=_MAX_BOOK_OBSERVE_LINES):
        ek = str(row.get("event_key") or "").strip()
        sample_i = row.get("sample_i")
        grade = row.get("odds_grade") if isinstance(row.get("odds_grade"), dict) else None
        if not ek or sample_i is None or not grade:
            continue
        by_tick[(ek, sample_i)] = grade
    return by_tick


def _build_goals_payload_uncached(*, limit: int = _MAX_GOALS) -> dict[str, Any]:
    observe = _read_jsonl_tail(OBSERVE_PATH, max_lines=_MAX_OBSERVE_LINES)
    judges = _read_jsonl_tail(JUDGE_PATH, max_lines=_MAX_JUDGE_LINES)
    by_key, by_path = _index_judges(judges)
    (
        recent_reversals,
        rev_by_match,
        cancel_by_key,
        cancel_by_stem,
        quote_by_key,
        quote_by_stem,
    ) = _load_reversal_index()
    odds_by_tick = _index_odds_grades()

    groups: dict[str, dict[str, Any]] = {}
    order: list[str] = []

    for row in observe:
        ek = str(row.get("event_key") or "").strip()
        mid = str(row.get("match_id") or "").strip()
        if not ek or not mid:
            continue
        if ek not in groups:
            groups[ek] = {
                "event_key": ek,
                "match_id": mid,
                "home": row.get("home") or "",
                "away": row.get("away") or "",
                "home_score": row.get("home_score"),
                "away_score": row.get("away_score"),
                "score": row.get("score"),
                "dqd_ts": row.get("dqd_ts") or row.get("sampled_at"),
                "gate": bool(row.get("gate")),
                "frames": [],
                "kind": None,
                "observe_only": False,
                "is_reversal": False,
            }
            order.append(ek)
        g = groups[ek]
        if row.get("gate"):
            g["gate"] = True
        if row.get("is_reversal") or row.get("observe_only"):
            g["is_reversal"] = True
            g["observe_only"] = True
            g["kind"] = "reversal_observe"
        # Prefer latest score/teams from later samples.
        for k in ("home", "away", "home_score", "away_score", "score", "dqd_ts"):
            if row.get(k) is not None and row.get(k) != "":
                g[k] = row.get(k)

        sample_i = row.get("sample_i")
        frame_path = str(row.get("frame_path") or "") or None
        # DOM mode judges inline (no screenshot, no OCR sidecar to join against).
        judge = row.get("judge") if isinstance(row.get("judge"), dict) else None
        if judge is None:
            judge = _lookup_judge(
                mid=mid,
                ek=ek,
                sample_i=sample_i,
                frame_path=frame_path or "",
                by_key=by_key,
                by_path=by_path,
            )
        dom = row.get("dom_state") if isinstance(row.get("dom_state"), dict) else None
        frame = {
            "sample_i": sample_i,
            "elapsed_s": row.get("elapsed_s"),
            "sampled_at": row.get("sampled_at") or row.get("observed_at"),
            "ok": row.get("ok"),
            "error": row.get("error"),
            "gate": bool(row.get("gate")),
            "surface": row.get("surface"),
            "frame_kind": row.get("frame_kind"),
            "capture_method": row.get("capture_method"),
            "page_url": row.get("page_url"),
            "frame_path": None,
            "thumb_url": None,
            "judge": judge,
            "dom_pop_box": (dom or {}).get("pop_box"),
            "dom_pop_class": (dom or {}).get("pop_class"),
            "dom_center_box": (dom or {}).get("center_box"),
            "dom_marks": (dom or {}).get("marks"),
            "af": row.get("af") if isinstance(row.get("af"), dict) else None,
            "odds_grade": row.get("odds_grade")
            if isinstance(row.get("odds_grade"), dict)
            else None,
            "board_score_match": row.get("board_score_match"),
        }

        # Upsert by sample_i so re-reads / retries replace.
        frames: list[dict[str, Any]] = g["frames"]
        replaced = False
        if sample_i is not None:
            for i, prev in enumerate(frames):
                if prev.get("sample_i") == sample_i:
                    frames[i] = frame
                    replaced = True
                    break
        if not replaced:
            frames.append(frame)

    af_rows = _read_jsonl_tail(AF_OBSERVE_PATH, max_lines=_MAX_AF_OBSERVE_LINES)
    af_by_key: dict[str, list[dict[str, Any]]] = {}
    for row in af_rows:
        ek = str(row.get("event_key") or "").strip()
        mid = str(row.get("match_id") or "").strip()
        if not ek or not mid:
            continue
        if ek not in groups:
            groups[ek] = {
                "event_key": ek,
                "match_id": mid,
                "home": row.get("home") or "",
                "away": row.get("away") or "",
                "home_score": row.get("home_score"),
                "away_score": row.get("away_score"),
                "score": row.get("dqd_score"),
                "dqd_ts": row.get("dqd_ts") or row.get("observed_at"),
                "gate": bool(row.get("gate")),
                "frames": [],
                "kind": None,
                "observe_only": False,
                "is_reversal": False,
            }
            order.append(ek)
        g = groups[ek]
        if row.get("is_reversal") or row.get("observe_only"):
            g["is_reversal"] = True
            g["observe_only"] = True
            g["kind"] = "reversal_observe"
        for k in ("home", "away", "home_score", "away_score", "dqd_ts"):
            if row.get(k) is not None and row.get(k) != "":
                g[k] = row.get(k)
        if row.get("dqd_score"):
            g["score"] = row.get("dqd_score")
        af_by_key.setdefault(ek, []).append(
            {
                "sample_i": row.get("sample_i"),
                "elapsed_s": row.get("elapsed_s"),
                "observed_at": row.get("observed_at"),
                "ok": row.get("ok"),
                "error": row.get("error"),
                "af_fixture_id": row.get("af_fixture_id"),
                "af_home": row.get("af_home"),
                "af_away": row.get("af_away"),
                "af_home_score": row.get("af_home_score"),
                "af_away_score": row.get("af_away_score"),
                "af_score": row.get("af_score"),
                "dqd_score": row.get("dqd_score"),
                "score_match": row.get("score_match"),
            }
        )

    af_by_tick: dict[tuple[str, Any], dict[str, Any]] = {}
    for ek, rows in af_by_key.items():
        for row in rows:
            sample_i = row.get("sample_i")
            if sample_i is None:
                continue
            af_by_tick[(ek, sample_i)] = {
                "ok": row.get("ok"),
                "score_match": row.get("score_match"),
                "af_score": row.get("af_score"),
                "error": row.get("error"),
            }

    paired_revs = _pair_goal_reversals(groups, rev_by_match)
    paired_rev_keys = {
        str(info.get("event_key") or "")
        for info in paired_revs.values()
        if info.get("event_key")
    }

    goals: list[dict[str, Any]] = []
    for ek in reversed(order):
        g = groups[ek]
        frames = list(g["frames"])
        frames.sort(
            key=lambda f: (
                float(f.get("elapsed_s") or 0),
                int(f.get("sample_i") or 0),
            )
        )
        for f in frames:
            sample_i = f.get("sample_i")
            if not isinstance(f.get("af"), dict):
                sidecar = af_by_tick.get((ek, sample_i))
                if sidecar:
                    f["af"] = sidecar
            if not isinstance(f.get("odds_grade"), dict) or not f["odds_grade"].get("level"):
                grade = odds_by_tick.get((ek, sample_i))
                if grade:
                    f["odds_grade"] = {
                        "level": grade.get("level"),
                        "reason": grade.get("reason"),
                    }
            f["aligned"] = _frame_aligned_buy(f)
            f["or_flatten"] = _frame_or_flatten(f)
        g["frames"] = frames
        g["frame_count"] = len(frames)
        g["ok_count"] = sum(1 for f in frames if f.get("ok") is True)
        af_frames = list(af_by_key.get(ek) or [])
        af_frames.sort(
            key=lambda f: (
                float(f.get("elapsed_s") or 0),
                int(f.get("sample_i") or 0),
            )
        )
        g["af_frames"] = af_frames
        g["af_frame_count"] = len(af_frames)
        g["af_match_count"] = sum(1 for f in af_frames if f.get("score_match") is True)
        g["af_first_match_elapsed_s"] = next(
            (
                f.get("elapsed_s")
                for f in af_frames
                if f.get("score_match") is True
            ),
            None,
        )
        quote = _lookup_quote(ek, quote_by_key, quote_by_stem)
        quote_mode = str((quote or {}).get("mode") or "")
        verdict = _goal_verdict(frames, quote_mode=quote_mode)
        in_play_at = None
        aligned_at = None
        flatten_at = None
        for f in frames:
            if in_play_at is None and _frame_dom_in_play(f):
                in_play_at = f.get("elapsed_s")
            if aligned_at is None and f.get("aligned"):
                aligned_at = f.get("elapsed_s")
            if flatten_at is None and f.get("or_flatten"):
                flatten_at = f.get("elapsed_s")
        g["in_play_elapsed_s"] = in_play_at
        g["aligned_elapsed_s"] = aligned_at
        g["flatten_elapsed_s"] = flatten_at
        g["odds_grade"] = next(
            (
                f.get("odds_grade")
                for f in reversed(frames)
                if isinstance(f.get("odds_grade"), dict) and f["odds_grade"].get("level")
            ),
            None,
        )
        for i, f in enumerate(frames):
            f["dom_seq"] = i + 1

        mid = str(g.get("match_id") or "")
        ek = str(g.get("event_key") or "")
        goal_prev, goal_curr = _parse_score_transition(ek)
        if goal_prev is not None:
            g["score_from"] = f"{goal_prev[0]}-{goal_prev[1]}"
        if goal_curr is not None:
            g["score_to"] = f"{goal_curr[0]}-{goal_curr[1]}"
        is_rev_obs = (
            g.get("kind") == "reversal_observe"
            or bool(g.get("is_reversal"))
            or bool(g.get("observe_only"))
        )
        linked = _find_linked_event_key(
            ek,
            match_id=mid,
            groups=groups,
            prefer_observe=not is_rev_obs,
        )
        if linked:
            g["linked_event_key"] = linked

        if is_rev_obs:
            g["kind"] = "reversal_observe"
            g["reversed"] = False
            g["reversal"] = None
            if quote_mode == "pitch_gate_flatten_or" or any(f.get("or_flatten") for f in frames):
                g["verdict"] = "flatten_or"
            elif quote_mode in {
                "reversal_observe_complete",
                "pitch_gate_timeout",
            } or str((quote or {}).get("reason") or "").startswith("no_or_confirm"):
                g["verdict"] = "hold"
            else:
                g["verdict"] = "reversal_observe"
            g["quote_mode"] = quote_mode or None
            goals.append(g)
            if len(goals) >= max(1, limit):
                break
            continue

        goal_ts = _event_happened_at(ek, g.get("dqd_ts"))
        cancel = _lookup_cancel(
            ek, cancel_by_key, cancel_by_stem, goal_ts=goal_ts
        )
        reversed_flag = False
        reversal_info: dict[str, Any] | None = None
        # Pair each reverse to the latest matching goal before it — an earlier
        # 0-1→0-0 must not paint a later 0-0→0-1 (Gwangju 20:23 vs 19:51).
        if ek in paired_revs:
            reversed_flag = True
            reversal_info = paired_revs[ek]
        elif cancel:
            cancel_key = str(cancel.get("event_key") or "")
            cancel_is_other_rev = (
                cancel_key
                and cancel_key != ek
                and cancel_key in paired_rev_keys
            )
            if not cancel_is_other_rev:
                reversed_flag = True
                reversal_info = {
                    "source": "pitch_gate_cancel",
                    "ts": cancel.get("ts"),
                    "reason": cancel.get("reason") or cancel.get("mode"),
                    "mode": cancel.get("mode"),
                }
        g["reversed"] = reversed_flag
        g["reversal"] = reversal_info
        g["quote_mode"] = quote_mode or None
        had_buy = verdict == "aligned_buy" or aligned_at is not None
        g["had_aligned_buy"] = had_buy
        if reversed_flag:
            verdict = "reversed_after_buy" if had_buy else "reversed"
        g["verdict"] = verdict
        goals.append(g)
        if len(goals) >= max(1, limit):
            break

    aligned_n = sum(1 for g in goals if g.get("verdict") == "aligned_buy")
    wait_af_n = sum(1 for g in goals if g.get("verdict") == "wait_af")
    rev_n = sum(1 for g in goals if g.get("reversed"))
    rev_obs_n = sum(1 for g in goals if g.get("kind") == "reversal_observe")
    flatten_n = sum(1 for g in goals if g.get("verdict") == "flatten_or")
    hold_n = sum(1 for g in goals if g.get("verdict") == "hold")
    gate_n = sum(1 for g in goals if g.get("gate"))
    return {
        "updated_at": None,
        "observe_path": str(OBSERVE_PATH),
        "af_observe_path": str(AF_OBSERVE_PATH),
        "book_observe_path": str(BOOK_OBSERVE_PATH),
        "judge_path": str(JUDGE_PATH),
        "goal_count": len(goals),
        "gate_goal_count": gate_n,
        "aligned_buy_count": aligned_n,
        "in_play_count": aligned_n,
        "wait_af_count": wait_af_n,
        "reversed_count": rev_n,
        "reversal_observe_count": rev_obs_n,
        "flatten_count": flatten_n,
        "hold_count": hold_n,
        "observe_rows": len(observe),
        "af_observe_rows": len(af_rows),
        "judge_rows": len(judges),
        "goals": goals,
        "recent_reversals": recent_reversals[:40],
    }


def build_goals_payload(*, limit: int = _MAX_GOALS) -> dict[str, Any]:
    """Join observe trails. Serialized + mtime cache so hub/UI polls share one scan."""
    limit = max(1, min(int(limit), _MAX_GOALS))
    global _GOALS_CACHE
    with _GOALS_LOCK:
        stamp = _observe_stamp()
        cached = _GOALS_CACHE
        if cached is not None and cached[0] == stamp and cached[1] >= limit:
            return cached[2]
        snap = _build_goals_payload_uncached(limit=limit)
        _GOALS_CACHE = (_observe_stamp(), limit, snap)
        return snap


def _status_from_snap(snap: dict[str, Any] | None) -> dict[str, Any]:
    goals = list((snap or {}).get("goals") or [])
    latest = goals[0].get("dqd_ts") if goals else None
    revs = list((snap or {}).get("recent_reversals") or [])
    return {
        "module": MODULE_ID,
        "running": True,
        "viewer": True,
        "observe_path": str(OBSERVE_PATH),
        "af_observe_path": str(AF_OBSERVE_PATH),
        "book_observe_path": str(BOOK_OBSERVE_PATH),
        "judge_path": str(JUDGE_PATH),
        "observe_exists": OBSERVE_PATH.is_file(),
        "af_observe_exists": AF_OBSERVE_PATH.is_file(),
        "book_observe_exists": BOOK_OBSERVE_PATH.is_file(),
        "judge_exists": JUDGE_PATH.is_file(),
        "goal_count": (snap or {}).get("goal_count") or 0,
        "gate_goal_count": (snap or {}).get("gate_goal_count") or 0,
        "aligned_buy_count": (snap or {}).get("aligned_buy_count") or 0,
        "in_play_count": (snap or {}).get("aligned_buy_count")
        or (snap or {}).get("in_play_count")
        or 0,
        "wait_af_count": (snap or {}).get("wait_af_count") or 0,
        "reversed_count": (snap or {}).get("reversed_count") or 0,
        "flatten_count": (snap or {}).get("flatten_count") or 0,
        "hold_count": (snap or {}).get("hold_count") or 0,
        "latest_dqd_ts": latest,
        "latest_reversal_ts": revs[0].get("ts") if revs else None,
    }


def status_payload() -> dict[str, Any]:
    """Cheap for hub polls — reuse the last goals snapshot; do not rescan jsonl."""
    snap = None
    with _GOALS_LOCK:
        if _GOALS_CACHE is not None:
            snap = _GOALS_CACHE[2]
    return _status_from_snap(snap)


class Handler(BaseHTTPRequestHandler):
    server_version = "PitchGateBoard/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204)
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)

        if path in ("/", "/index.html"):
            file_path = PUBLIC / "index.html"
            if file_path.is_file():
                serve_file(self, file_path)
            else:
                json_response(self, 404, {"error": "missing_index"})
            return

        if path.startswith("/src/"):
            rel = path[len("/src/") :]
            file_path = _safe_under(SRC, SRC / rel)
            if file_path:
                serve_file(self, file_path)
            else:
                json_response(self, 404, {"error": "not_found"})
            return

        if path == "/api/health":
            json_response(self, 200, {"ok": True, "module": MODULE_ID})
            return

        if path == "/api/module":
            mod = MODULE_DIR / "module.json"
            if mod.is_file():
                try:
                    json_response(self, 200, json.loads(mod.read_text(encoding="utf-8")))
                    return
                except json.JSONDecodeError:
                    pass
            json_response(self, 200, {"id": MODULE_ID})
            return

        if path == "/api/status":
            json_response(self, 200, status_payload())
            return

        if path == "/api/goals":
            try:
                limit = int((qs.get("limit") or [str(_MAX_GOALS)])[0])
            except (TypeError, ValueError):
                limit = _MAX_GOALS
            limit = max(1, min(limit, _MAX_GOALS))
            json_response(self, 200, build_goals_payload(limit=limit))
            return

        json_response(self, 404, {"error": "not_found", "path": path})


def main() -> int:
    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    httpd.daemon_threads = True
    print(f"Pitch Gate Board → http://{HOST}:{PORT}/", flush=True)
    print(f"  observe → {OBSERVE_PATH}", flush=True)
    print(f"  af observe → {AF_OBSERVE_PATH}", flush=True)
    print(f"  odds observe → {BOOK_OBSERVE_PATH}", flush=True)
    print(f"  judge   → {JUDGE_PATH}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down…", flush=True)
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
