#!/usr/bin/env python3
"""Pitch Gate Board: read-only view of goal screenshots + pitch-state judgments."""

from __future__ import annotations

import json
import mimetypes
import sys
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
JUDGE_PATH = DATA / "pitch_state_judge.jsonl"
QUOTES_PATH = DATA / "quotes.jsonl"
FRAMES_ROOT = DATA / "dqd_stream_frames"

# Cap how much history we scan for the board.
_MAX_OBSERVE_LINES = 8000
_MAX_JUDGE_LINES = 8000
_MAX_BRIDGE_LINES = 4000
_MAX_QUOTES_LINES = 4000


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


def _rel_frame_url(abs_path: str) -> str | None:
    if not abs_path:
        return None
    try:
        p = Path(abs_path).resolve()
        root = FRAMES_ROOT.resolve()
        rel = p.relative_to(root)
    except (OSError, ValueError):
        return None
    return f"/api/frame?rel={rel.as_posix()}"


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


def _goal_verdict(frames: list[dict[str, Any]]) -> str:
    states = [
        str((f.get("judge") or {}).get("play_state") or "")
        for f in frames
        if isinstance(f.get("judge"), dict)
    ]
    if any(s == "in_play" for s in states):
        return "in_play"
    if frames and all(f.get("ok") is False for f in frames):
        return "capture_failed"
    if states and all(s in ("stopped", "unclear", "") for s in states):
        return "waiting"
    if not states:
        return "pending_judge"
    return "mixed"


def _ts_key(raw: Any) -> str:
    return str(raw or "")


def _load_reversal_index() -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    """Bridge reversals + quote gate-cancel rows.

    Returns:
      recent_reversals (newest first),
      latest_reversal_by_match_id,
      gate_cancel_by_event_key
    """
    recent: list[dict[str, Any]] = []
    by_match: dict[str, dict[str, Any]] = {}
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
                f"{curr.get('home')}-{curr.get('away')}"
            ),
        }
        recent.append(item)
        # Later lines overwrite → keep newest per match.
        by_match[mid] = item
    recent.reverse()  # newest first for UI toasts

    cancel_by_key: dict[str, dict[str, Any]] = {}
    for row in _read_jsonl_tail(QUOTES_PATH, max_lines=_MAX_QUOTES_LINES):
        mode = str(row.get("mode") or "")
        if mode not in {
            "pitch_gate_canceled",
            "dqd_reversal_pitch_gate_canceled",
        } and "pitch_gate_canceled" not in mode:
            continue
        key = str(row.get("event_key") or "").strip()
        mid = str(row.get("match_id") or "").strip()
        item = {
            "mode": mode,
            "ts": row.get("quoted_at") or row.get("ts"),
            "match_id": mid,
            "event_key": key,
            "home": row.get("home") or "",
            "away": row.get("away") or "",
            "home_score": row.get("home_score"),
            "away_score": row.get("away_score"),
            "pitch_gate": row.get("pitch_gate"),
            "reason": (row.get("pitch_gate") or {}).get("reason")
            if isinstance(row.get("pitch_gate"), dict)
            else mode,
        }
        if key:
            cancel_by_key[key] = item
    return recent, by_match, cancel_by_key


def build_goals_payload(*, limit: int = 80) -> dict[str, Any]:
    observe = _read_jsonl_tail(OBSERVE_PATH, max_lines=_MAX_OBSERVE_LINES)
    judges = _read_jsonl_tail(JUDGE_PATH, max_lines=_MAX_JUDGE_LINES)
    by_key, by_path = _index_judges(judges)
    recent_reversals, rev_by_match, cancel_by_key = _load_reversal_index()

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
            }
            order.append(ek)
        g = groups[ek]
        if row.get("gate"):
            g["gate"] = True
        # Prefer latest score/teams from later samples.
        for k in ("home", "away", "home_score", "away_score", "score", "dqd_ts"):
            if row.get(k) is not None and row.get(k) != "":
                g[k] = row.get(k)

        sample_i = row.get("sample_i")
        frame_path = str(row.get("frame_path") or "") or None
        judge = _lookup_judge(
            mid=mid,
            ek=ek,
            sample_i=sample_i,
            frame_path=frame_path or "",
            by_key=by_key,
            by_path=by_path,
        )
        frame = {
            "sample_i": sample_i,
            "elapsed_s": row.get("elapsed_s"),
            "sampled_at": row.get("sampled_at"),
            "ok": row.get("ok"),
            "error": row.get("error"),
            "gate": bool(row.get("gate")),
            "surface": row.get("surface"),
            "frame_kind": row.get("frame_kind"),
            "capture_method": row.get("capture_method"),
            "page_url": row.get("page_url"),
            "frame_path": frame_path,
            "thumb_url": _rel_frame_url(frame_path) if frame_path else None,
            "judge": judge,
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
        g["frames"] = frames
        g["frame_count"] = len(frames)
        g["ok_count"] = sum(1 for f in frames if f.get("ok") is True)
        verdict = _goal_verdict(frames)
        in_play_at = None
        for f in frames:
            j = f.get("judge") or {}
            if j.get("play_state") == "in_play":
                in_play_at = f.get("elapsed_s")
                break
        g["in_play_elapsed_s"] = in_play_at

        mid = str(g.get("match_id") or "")
        cancel = cancel_by_key.get(ek)
        rev = rev_by_match.get(mid)
        reversed_flag = False
        reversal_info: dict[str, Any] | None = None
        if cancel:
            reversed_flag = True
            reversal_info = {
                "source": "pitch_gate_cancel",
                "ts": cancel.get("ts"),
                "reason": cancel.get("reason") or cancel.get("mode"),
                "mode": cancel.get("mode"),
            }
        if rev:
            # Reversal after this goal (or any later correction on the match).
            goal_ts = _ts_key(g.get("dqd_ts"))
            rev_ts = _ts_key(rev.get("ts"))
            if not goal_ts or rev_ts >= goal_ts:
                reversed_flag = True
                reversal_info = {
                    "source": "dqd_reversal",
                    "ts": rev.get("ts"),
                    "prev": rev.get("prev"),
                    "curr": rev.get("curr"),
                    "home_score": rev.get("home_score"),
                    "away_score": rev.get("away_score"),
                    "event_key": rev.get("event_key"),
                }
        g["reversed"] = reversed_flag
        g["reversal"] = reversal_info
        if reversed_flag:
            verdict = "reversed"
        g["verdict"] = verdict
        goals.append(g)
        if len(goals) >= max(1, limit):
            break

    in_play_n = sum(1 for g in goals if g.get("verdict") == "in_play")
    rev_n = sum(1 for g in goals if g.get("reversed"))
    gate_n = sum(1 for g in goals if g.get("gate"))
    return {
        "updated_at": None,
        "observe_path": str(OBSERVE_PATH),
        "judge_path": str(JUDGE_PATH),
        "frames_root": str(FRAMES_ROOT),
        "goal_count": len(goals),
        "gate_goal_count": gate_n,
        "in_play_count": in_play_n,
        "reversed_count": rev_n,
        "observe_rows": len(observe),
        "judge_rows": len(judges),
        "goals": goals,
        "recent_reversals": recent_reversals[:40],
    }


def status_payload() -> dict[str, Any]:
    snap = build_goals_payload(limit=200)
    latest = None
    if snap["goals"]:
        latest = snap["goals"][0].get("dqd_ts")
    return {
        "module": MODULE_ID,
        "running": True,
        "viewer": True,
        "observe_path": str(OBSERVE_PATH),
        "judge_path": str(JUDGE_PATH),
        "frames_root": str(FRAMES_ROOT),
        "observe_exists": OBSERVE_PATH.is_file(),
        "judge_exists": JUDGE_PATH.is_file(),
        "goal_count": snap["goal_count"],
        "gate_goal_count": snap["gate_goal_count"],
        "in_play_count": snap["in_play_count"],
        "reversed_count": snap.get("reversed_count") or 0,
        "latest_dqd_ts": latest,
        "latest_reversal_ts": (snap.get("recent_reversals") or [{}])[0].get("ts")
        if snap.get("recent_reversals")
        else None,
    }


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
                limit = int((qs.get("limit") or ["80"])[0])
            except (TypeError, ValueError):
                limit = 80
            limit = max(1, min(limit, 300))
            json_response(self, 200, build_goals_payload(limit=limit))
            return

        if path == "/api/frame":
            rel = unquote((qs.get("rel") or [""])[0]).lstrip("/")
            if not rel:
                json_response(self, 400, {"error": "missing_rel"})
                return
            file_path = _safe_under(FRAMES_ROOT, FRAMES_ROOT / rel)
            if not file_path:
                json_response(self, 404, {"error": "frame_not_found"})
                return
            serve_file(self, file_path)
            return

        json_response(self, 404, {"error": "not_found", "path": path})


def main() -> int:
    FRAMES_ROOT.mkdir(parents=True, exist_ok=True)
    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Pitch Gate Board → http://{HOST}:{PORT}/", flush=True)
    print(f"  observe → {OBSERVE_PATH}", flush=True)
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
