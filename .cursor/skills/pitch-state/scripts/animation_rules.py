#!/usr/bin/env python3
"""Rule-based play-state judgment for animation screenshots."""

from __future__ import annotations

import os
import re
from typing import Any


IN_PLAY_TOKENS = (
    "进攻",
    "控球",
    "危险进攻",
    "掷界外球",
    "任意球",
    "角球",
    "球门球",
)
STOPPED_TOKEN_MAP = {
    "VAR": "var",
    "换人": "substitution",
    "进球": "celebration",
    "庆祝": "celebration",
    "暂停": "overlay_pause",
    "未开始": "not_started",
    "暂无动画直播": "not_started",
    # Avoid matching the substring inside 「伤停补时」.
    "伤停补时": None,  # ignore / not a pause token
    "伤停": "overlay_pause",
}

# Hard stops that often precede a disallowed goal — enable score veto after these.
SCORE_GATE_STOP_REASONS = frozenset({"var", "celebration"})

# Board score like "0:0" / "0 : 0" / "2-0". Avoid bare clock "27:23".
_SCORE_RE = re.compile(r"(?<!\d)(\d{1,2})\s*[:：\-]\s*(\d{1,2})(?!\d)")
_CLOCK_HINT_RE = re.compile(r"\(\s*\+\s*\d+\s*\)")
_FULLWIDTH_DIGITS = str.maketrans("０１２３４５６７８９", "0123456789")
# Football FT scores rarely exceed this per side.
_MAX_SIDE_GOALS = 15
_MIN_SCORE_CONF = 0.85


def _threshold() -> float:
    raw = os.getenv("PITCH_STATE_OCR_MIN_CONF", "0.75")
    try:
        return max(0.0, min(1.0, float(raw)))
    except ValueError:
        return 0.75


def _as_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return None


def _box_xy(box: Any) -> tuple[float, float] | None:
    if box is None:
        return None
    try:
        # PaddleOCR returns numpy.ndarray; normalize to nested lists first.
        if hasattr(box, "tolist"):
            try:
                box = box.tolist()
            except Exception:  # noqa: BLE001
                pass
        pts = list(box)
        if not pts:
            return None
        xs: list[float] = []
        ys: list[float] = []
        for p in pts:
            if hasattr(p, "tolist"):
                try:
                    p = p.tolist()
                except Exception:  # noqa: BLE001
                    pass
            if isinstance(p, (list, tuple)) and len(p) >= 2:
                xs.append(float(p[0]))
                ys.append(float(p[1]))
        if len(xs) < 2:
            flat: list[float] = []
            for p in pts:
                try:
                    flat.append(float(p))
                except (TypeError, ValueError):
                    return None
            if len(flat) >= 4 and len(flat) % 2 == 0:
                xs = flat[0::2]
                ys = flat[1::2]
            else:
                return None
        return (sum(xs) / len(xs), sum(ys) / len(ys))
    except (TypeError, ValueError):
        return None


def _box_width_frac(box: Any, max_x: float) -> float:
    """Horizontal span of a box as a fraction of frame width proxy."""
    if box is None or max_x <= 0:
        return 0.0
    try:
        if hasattr(box, "tolist"):
            try:
                box = box.tolist()
            except Exception:  # noqa: BLE001
                pass
        xs: list[float] = []
        for p in list(box):
            if hasattr(p, "tolist"):
                try:
                    p = p.tolist()
                except Exception:  # noqa: BLE001
                    pass
            if isinstance(p, (list, tuple)) and len(p) >= 2:
                xs.append(float(p[0]))
        if len(xs) < 2:
            flat = [float(v) for v in list(box)]
            if len(flat) >= 4 and len(flat) % 2 == 0:
                xs = flat[0::2]
        if len(xs) < 2:
            return 0.0
        return max(0.0, (max(xs) - min(xs)) / max_x)
    except (TypeError, ValueError):
        return 0.0


def _normalize_score_text(text: str) -> str:
    t = (text or "").translate(_FULLWIDTH_DIGITS)
    t = t.replace("：", ":").replace("－", "-").replace("—", "-")
    return t.strip()


def _is_likely_clock(home: int, away: int, left_s: str, right_s: str, raw: str) -> bool:
    text = str(raw or "")
    if _CLOCK_HINT_RE.search(text):
        return True
    # Match-clock style: MM:SS with seconds always 2 digits and < 60.
    if len(right_s) == 2 and away <= 59 and (len(left_s) == 2 or home >= 10):
        # e.g. 27:23 / 47:02 / 93:51 — not a football board score.
        return True
    # Late-game clock without (+N): 90:00–99:59 style.
    if len(left_s) == 2 and len(right_s) == 2 and 45 <= home <= 99 and away <= 59:
        return True
    return False


def _line_is_score_clean(text: str, match_span: tuple[int, int]) -> bool:
    """Reject jersey glue / Chinese noise around the matched score."""
    t = _normalize_score_text(text)
    if not t:
        return False
    # Strip the matched score; leftover should be tiny punctuation/space only.
    a, b = match_span
    rest = (t[:a] + t[b:]).strip(" \t|/·•.,;，。")
    if rest:
        # Allow a trailing period; reject letters / CJK / extra digits.
        if any(ch.isdigit() for ch in rest):
            return False
        if re.search(r"[A-Za-z\u4e00-\u9fff]", rest):
            return False
    return True


def _plausible_football_score(
    home: int,
    away: int,
    *,
    expected_home: int | None = None,
    expected_away: int | None = None,
) -> bool:
    if home < 0 or away < 0 or home > _MAX_SIDE_GOALS or away > _MAX_SIDE_GOALS:
        return False
    if expected_home is None or expected_away is None:
        return True
    # Post-goal / VAR window: board is expected or a reverse (one side drops).
    # Reject wild OCR (e.g. 10-1 when expecting 0-1 / 3-2).
    if home > expected_home + 1 or away > expected_away + 1:
        return False
    if abs(home - expected_home) + abs(away - expected_away) > 4:
        return False
    return True


def extract_board_score(
    lines: list[dict[str, Any]],
    *,
    expected_home: Any = None,
    expected_away: Any = None,
) -> dict[str, Any] | None:
    """Pick the animation board score (bottom-center), ignoring clock/jerseys."""
    exp_h = _as_int(expected_home)
    exp_a = _as_int(expected_away)
    candidates: list[dict[str, Any]] = []
    xs_all: list[float] = []
    ys_all: list[float] = []
    for line in lines:
        xy = _box_xy(line.get("box"))
        if xy:
            xs_all.append(xy[0])
            ys_all.append(xy[1])
    if not xs_all or not ys_all:
        # No geometry — only accept ultra-clean compact scores.
        min_x = max_x = min_y = max_y = 0.0
    else:
        min_x, max_x = min(xs_all), max(xs_all)
        min_y, max_y = min(ys_all), max(ys_all)
    span_x = max(max_x - min_x, 0.0)
    span_y = max(max_y - min_y, 0.0)
    # When all text boxes sit in one cluster, min-max ROI is meaningless —
    # skip position gates and rank on text cleanliness + expected match.
    geometry_ok = span_x >= 40.0 or span_y >= 25.0

    for line in lines:
        text = _normalize_score_text(str(line.get("text") or ""))
        if not text:
            continue
        conf = float(line.get("score") or 0.0)
        if conf + 1e-12 < _MIN_SCORE_CONF:
            continue
        xy = _box_xy(line.get("box"))
        if geometry_ok and xy:
            x_frac = (xy[0] - min_x) / max(span_x, 1.0)
            y_frac = (xy[1] - min_y) / max(span_y, 1.0)
        else:
            x_frac, y_frac = 0.5, 1.0
        # Prefer lower-half scoreboard; reject wing jerseys only on wide frames.
        if geometry_ok:
            if y_frac + 1e-9 < 0.45:
                continue
            # Full-width animation frames have jerseys on both sides (span large).
            if span_x >= 300.0 and (x_frac + 1e-9 < 0.18 or x_frac - 1e-9 > 0.82):
                continue
            width_frac = _box_width_frac(line.get("box"), max(max_x, 1.0))
            # Tiny single-glyph boxes are almost always jersey/badge digits.
            if width_frac > 0 and width_frac < 0.015:
                continue

        for m in _SCORE_RE.finditer(text):
            left_s, right_s = m.group(1), m.group(2)
            home = int(left_s)
            away = int(right_s)
            raw = m.group(0)
            if _is_likely_clock(home, away, left_s, right_s, text):
                continue
            if not _line_is_score_clean(text, m.span()):
                continue
            if not _plausible_football_score(
                home, away, expected_home=exp_h, expected_away=exp_a
            ):
                continue
            pos_score = (1.0 - abs(x_frac - 0.5)) + max(0.0, y_frac - 0.45)
            # Prefer compact "3:2" lines over wide noisy banners.
            compact = 1.0 if re.fullmatch(r"\d{1,2}\s*[:：\-]\s*\d{1,2}", text) else 0.0
            expected_hit = (
                1.0
                if exp_h is not None
                and exp_a is not None
                and home == exp_h
                and away == exp_a
                else 0.0
            )
            candidates.append(
                {
                    "home": home,
                    "away": away,
                    "text": f"{home}-{away}",
                    "raw": raw,
                    "confidence": conf,
                    "y_frac": y_frac,
                    "x_frac": x_frac,
                    "_rank": (expected_hit, pos_score, compact, conf),
                }
            )
    if not candidates:
        return None
    candidates.sort(key=lambda c: c["_rank"], reverse=True)
    best = dict(candidates[0])
    # Ambiguous: top two disagree and neither is the expected score → abstain.
    if len(candidates) >= 2:
        a, b = candidates[0], candidates[1]
        same = a["home"] == b["home"] and a["away"] == b["away"]
        if not same:
            a_exp = (
                exp_h is not None
                and exp_a is not None
                and a["home"] == exp_h
                and a["away"] == exp_a
            )
            b_exp = (
                exp_h is not None
                and exp_a is not None
                and b["home"] == exp_h
                and b["away"] == exp_a
            )
            # Close geometry ranks without an expected hit → unsafe.
            if not a_exp and not b_exp and abs(a["_rank"][1] - b["_rank"][1]) < 0.08:
                return None
            if b_exp and not a_exp:
                best = dict(b)
    best.pop("_rank", None)
    return best


def judge_animation(
    lines: list[dict[str, Any]],
    *,
    expected_home: Any = None,
    expected_away: Any = None,
    require_score: bool = False,
) -> dict[str, Any]:
    """Judge play state from OCR tokens; optional post-VAR score veto.

    Default: keyword-only (进攻/控球 vs VAR…).
    When ``require_score`` (pitch-gate saw VAR/celebration earlier): board OCR
    must match expected DQD score before ``in_play`` — blocks animation-ahead
    reversals that DQD has not emitted yet.
    """
    texts = [
        str(line.get("text") or "").strip()
        for line in lines
        if str(line.get("text") or "").strip()
    ]
    full = "\n".join(texts)
    evidence: list[str] = []

    exp_h = _as_int(expected_home)
    exp_a = _as_int(expected_away)
    board = (
        extract_board_score(lines, expected_home=exp_h, expected_away=exp_a)
        if require_score
        else None
    )
    score_payload: dict[str, Any] = {
        "ocr_score": board.get("text") if board else None,
        "ocr_home_score": board.get("home") if board else None,
        "ocr_away_score": board.get("away") if board else None,
        "expected_home_score": exp_h,
        "expected_away_score": exp_a,
        "score_match": None,
        "require_score": bool(require_score),
    }

    # 「伤停补时」 is a stoppage-time banner, not an injury pause.
    scan_text = full.replace("伤停补时", "")
    stop_hits: list[tuple[str, str]] = []
    for token, reason in STOPPED_TOKEN_MAP.items():
        if reason is None or not token:
            continue
        if token.lower() == "var":
            if "VAR" in scan_text or "var" in scan_text.lower():
                stop_hits.append(("VAR", reason))
            continue
        if token in scan_text:
            stop_hits.append((token, reason))
    if stop_hits:
        token, reason = stop_hits[0]
        evidence.append(f"命中暂停关键词: {token}")
        confidence = 0.92 if token in {"VAR", "换人", "未开始", "暂无动画直播"} else 0.84
        return {
            "play_state": "stopped",
            "stopped_reason": reason,
            "confidence": confidence,
            "evidence": evidence,
            **score_payload,
        }

    # Post-VAR / celebration: veto in_play unless board score still matches.
    if require_score and exp_h is not None and exp_a is not None:
        if board is None:
            score_payload["score_match"] = False
            evidence.append(f"VAR后需比分确认（未识别，期望 {exp_h}-{exp_a}）")
            return {
                "play_state": "unclear",
                "stopped_reason": None,
                "confidence": 0.25 if texts else 0.0,
                "evidence": evidence,
                **score_payload,
            }
        matched = int(board["home"]) == exp_h and int(board["away"]) == exp_a
        score_payload["score_match"] = matched
        if matched:
            evidence.append(f"VAR后比分一致: OCR {board['text']} = 期望 {exp_h}-{exp_a}")
        else:
            evidence.append(
                f"VAR后比分不一致: OCR {board['text']} ≠ 期望 {exp_h}-{exp_a}"
            )
            return {
                "play_state": "unclear",
                "stopped_reason": None,
                "confidence": 0.55,
                "evidence": evidence,
                **score_payload,
            }

    play_hits = [token for token in IN_PLAY_TOKENS if token in full]
    if play_hits:
        evidence.append(f"命中进行中关键词: {play_hits[0]}")
        confidence = 0.86 if play_hits[0] in {"进攻", "控球", "危险进攻"} else 0.8
        if confidence >= _threshold():
            return {
                "play_state": "in_play",
                "stopped_reason": None,
                "confidence": confidence,
                "evidence": evidence,
                **score_payload,
            }

    return {
        "play_state": "unclear",
        "stopped_reason": None,
        "confidence": 0.3 if texts else 0.0,
        "evidence": evidence
        or (["OCR 未命中强规则"] if texts else ["OCR 无有效文本"]),
        **score_payload,
    }
