#!/usr/bin/env python3
"""Classify screenshot type: animation vs real video."""

from __future__ import annotations

from pathlib import Path
from typing import Any


def classify_frame(meta: dict[str, Any]) -> tuple[str, list[str]]:
    evidence: list[str] = []
    frame_kind = str(meta.get("frame_kind") or "").strip().lower()
    if frame_kind == "animation":
        return "animation", ["frame_kind=animation"]
    if frame_kind == "video":
        return "real_video", ["frame_kind=video"]

    surface = str(meta.get("surface") or "").strip().lower()
    stream_url = str(meta.get("stream_url") or "").strip().lower()
    page_url = str(meta.get("page_url") or "").strip().lower()
    raw_hint = meta.get("raw_hint")

    if stream_url.endswith((".m3u8", ".flv")) or "/m3u8" in stream_url or "/flv" in stream_url:
        return "real_video", ["stream_url looks like live video"]
    if surface == "animation":
        return "animation", ["surface=animation"]
    if surface == "video":
        return "real_video", ["surface=video"]
    if "tracker.namitiyu.com" in page_url:
        return "animation", ["tracker page url"]

    hint_text = str(raw_hint or "")
    if "tracker.namitiyu.com" in hint_text or "md-anim-iframe" in hint_text:
        evidence.append("raw_hint mentions animation tracker")
    if "video" in hint_text or "m3u8" in hint_text or "flv" in hint_text:
        evidence.append("raw_hint mentions video stream")

    path = Path(str(meta.get("path") or ""))
    name = path.name.lower()
    if any(tok in name for tok in ("anim", "tracker")):
        evidence.append("filename hints animation")

    if evidence:
        if any("video" in item or "stream" in item for item in evidence):
            return "real_video", evidence
        return "animation", evidence
    return "unknown", []


def classify_sequence(frames: list[dict[str, Any]]) -> str:
    kinds = {classify_frame(frame)[0] for frame in frames if frame}
    kinds.discard("unknown")
    if not kinds:
        return "unknown"
    if len(kinds) == 1:
        return next(iter(kinds))
    return "mixed"
