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

# Board score like "0:0" / "0 : 0" / "2-0". Avoid bare clock "27:23".
_SCORE_RE = re.compile(r"(?<!\d)(\d{1,2})\s*[:：\-]\s*(\d{1,2})(?!\d)")
_CLOCK_HINT_RE = re.compile(r"\(\s*\+\s*\d+\s*\)")


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
        pts = list(box)
        if not pts:
            return None
        xs: list[float] = []
        ys: list[float] = []
        for p in pts:
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


def _is_likely_clock(home: int, away: int, raw: str) -> bool:
    text = str(raw or "")
    if _CLOCK_HINT_RE.search(text):
        return True
    # Match-clock style: MM:SS with seconds always 2 digits and < 60.
    m = _SCORE_RE.search(text.replace("：", ":"))
    if not m:
        return False
    left_s, right_s = m.group(1), m.group(2)
    if len(right_s) == 2 and away <= 59 and (len(left_s) == 2 or home >= 10):
        # e.g. 27:23 / 47:02 — not a football board score.
        return True
    return False


def extract_board_score(lines: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Pick the animation board score (bottom-center), ignoring match clock."""
    candidates: list[dict[str, Any]] = []
    max_x = 1.0
    max_y = 1.0
    for line in lines:
        xy = _box_xy(line.get("box"))
        if xy:
            max_x = max(max_x, xy[0])
            max_y = max(max_y, xy[1])

    for line in lines:
        text = str(line.get("text") or "").strip()
        if not text:
            continue
        for m in _SCORE_RE.finditer(text):
            home = int(m.group(1))
            away = int(m.group(2))
            raw = m.group(0)
            if _is_likely_clock(home, away, text):
                continue
            if home > 20 or away > 20:
                continue
            xy = _box_xy(line.get("box"))
            x_frac = (xy[0] / max_x) if xy else 0.5
            y_frac = (xy[1] / max_y) if xy else 0.5
            # Prefer bottom-center board score over stray digits.
            pos_score = (1.0 - abs(x_frac - 0.5)) + max(0.0, y_frac - 0.45)
            conf = float(line.get("score") or 0.0)
            candidates.append(
                {
                    "home": home,
                    "away": away,
                    "text": f"{home}-{away}",
                    "raw": raw,
                    "confidence": conf,
                    "y_frac": y_frac,
                    "x_frac": x_frac,
                    "_rank": (pos_score, conf),
                }
            )
    if not candidates:
        return None
    candidates.sort(key=lambda c: c["_rank"], reverse=True)
    best = dict(candidates[0])
    best.pop("_rank", None)
    return best


def judge_animation(
    lines: list[dict[str, Any]],
    *,
    expected_home: Any = None,
    expected_away: Any = None,
) -> dict[str, Any]:
    texts = [
        str(line.get("text") or "").strip()
        for line in lines
        if str(line.get("text") or "").strip()
    ]
    full = "\n".join(texts)
    evidence: list[str] = []

    exp_h = _as_int(expected_home)
    exp_a = _as_int(expected_away)
    require_score = exp_h is not None and exp_a is not None
    board = extract_board_score(lines)
    score_payload: dict[str, Any] = {
        "ocr_score": board.get("text") if board else None,
        "ocr_home_score": board.get("home") if board else None,
        "ocr_away_score": board.get("away") if board else None,
        "expected_home_score": exp_h,
        "expected_away_score": exp_a,
        "score_match": None,
    }

    if require_score:
        if board is None:
            score_payload["score_match"] = False
            evidence.append(f"比分未识别（期望 {exp_h}-{exp_a}）")
            return {
                "play_state": "unclear",
                "stopped_reason": None,
                "confidence": 0.2 if texts else 0.0,
                "evidence": evidence,
                **score_payload,
            }
        matched = int(board["home"]) == exp_h and int(board["away"]) == exp_a
        score_payload["score_match"] = matched
        if matched:
            evidence.append(f"比分一致: OCR {board['text']} = 期望 {exp_h}-{exp_a}")
        else:
            evidence.append(
                f"比分不一致: OCR {board['text']} ≠ 期望 {exp_h}-{exp_a}"
            )
            return {
                "play_state": "unclear",
                "stopped_reason": None,
                "confidence": 0.9,
                "evidence": evidence,
                **score_payload,
            }
    elif board is not None:
        evidence.append(f"OCR 比分: {board['text']}")

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
