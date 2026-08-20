#!/usr/bin/env python3
"""Schema helpers for pitch-state outputs."""

from __future__ import annotations

from typing import Any

PLAY_STATES = {"in_play", "stopped", "unclear"}
STOPPED_REASONS = {
    "var",
    "celebration",
    "substitution",
    "not_started",
    "overlay_pause",
    "other",
    None,
    "null",
}
FRAME_TYPES = {"animation", "real_video", "mixed", "unknown"}
DECISION_SOURCES = {"ocr_rule", "vlm", "hybrid"}
SEQUENCE_VERDICTS = {"resumed_at_s", "still_stopped", "unclear", None, "null"}


def normalize_stopped_reason(value: Any) -> str | None:
    if value in (None, "", "null"):
        return None
    text = str(value).strip().lower()
    if text in {"var", "celebration", "substitution", "not_started", "overlay_pause", "other"}:
        return text
    return "other"


def _clamp_conf(value: Any) -> float:
    try:
        num = float(value)
    except (TypeError, ValueError):
        num = 0.0
    return max(0.0, min(1.0, num))


def validate_result(payload: dict[str, Any]) -> dict[str, Any]:
    out = dict(payload)
    if str(out.get("play_state")) not in PLAY_STATES:
        out["play_state"] = "unclear"
    out["stopped_reason"] = normalize_stopped_reason(out.get("stopped_reason"))
    out["confidence"] = _clamp_conf(out.get("confidence"))
    if str(out.get("frame_type")) not in FRAME_TYPES:
        out["frame_type"] = "unknown"
    if str(out.get("decision_source")) not in DECISION_SOURCES:
        out["decision_source"] = "hybrid"
    if out.get("sequence_verdict") not in SEQUENCE_VERDICTS:
        out["sequence_verdict"] = None
    if not isinstance(out.get("evidence"), list):
        out["evidence"] = []
    if not isinstance(out.get("per_frame"), list):
        out["per_frame"] = []
    return out
