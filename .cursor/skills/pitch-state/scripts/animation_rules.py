#!/usr/bin/env python3
"""Rule-based play-state judgment for animation screenshots."""

from __future__ import annotations

import os
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


def _threshold() -> float:
    raw = os.getenv("PITCH_STATE_OCR_MIN_CONF", "0.75")
    try:
        return max(0.0, min(1.0, float(raw)))
    except ValueError:
        return 0.75


def judge_animation(lines: list[dict[str, Any]]) -> dict[str, Any]:
    texts = [str(line.get("text") or "").strip() for line in lines if str(line.get("text") or "").strip()]
    full = "\n".join(texts)
    evidence: list[str] = []

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
            }

    return {
        "play_state": "unclear",
        "stopped_reason": None,
        "confidence": 0.3 if texts else 0.0,
        "evidence": evidence or (["OCR 未命中强规则"] if texts else ["OCR 无有效文本"]),
    }
