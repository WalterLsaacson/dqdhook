#!/usr/bin/env python3
"""Main pipeline for pitch-state judgment."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any

_SCRIPTS = Path(__file__).resolve().parent
_QUOTE_SCRIPTS = _SCRIPTS.parents[1] / "polymarket-quote" / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))
if str(_QUOTE_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_QUOTE_SCRIPTS))

from animation_ocr import (  # noqa: E402
    PaddleOcrEngine,
    get_shared_ocr_engine,
    ocr_enabled,
)
from animation_rules import judge_animation  # noqa: E402
from frame_type import classify_frame  # noqa: E402
from schema import validate_result  # noqa: E402
from vlm_client import judge_with_vlm, vlm_enabled  # noqa: E402
import quote_lib as lib  # noqa: E402


def repo_root() -> Path:
    return _SCRIPTS.parents[3]


def default_output_path(root: Path | None = None) -> Path:
    rt = Path(root) if root is not None else repo_root()
    return lib.data_dir(rt) / "pitch_state_judge.jsonl"


def load_observe_frames(
    observe_jsonl: Path,
    *,
    match_id: str | None = None,
    event_key: str | None = None,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not observe_jsonl.is_file():
        return rows
    for line in observe_jsonl.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if match_id and str(row.get("match_id") or "") != str(match_id):
            continue
        if event_key and str(row.get("event_key") or "") != str(event_key):
            continue
        if row.get("ok") is not True:
            continue
        path = row.get("frame_path")
        if not path:
            continue
        rows.append(
            {
                "path": path,
                "sample_i": row.get("sample_i"),
                "elapsed_s": row.get("elapsed_s"),
                "surface": row.get("surface"),
                "stream_url": row.get("stream_url"),
                "page_url": row.get("page_url"),
                "frame_kind": row.get("frame_kind"),
                "match_id": row.get("match_id"),
                "event_key": row.get("event_key"),
                "home_score": row.get("home_score"),
                "away_score": row.get("away_score"),
            }
        )
    rows.sort(key=lambda row: (float(row.get("elapsed_s") or 0.0), int(row.get("sample_i") or 0)))
    return rows


def _normalize_frames(
    *,
    image: Path | None = None,
    images: list[Path] | None = None,
    elapsed: list[float] | None = None,
    observe_jsonl: Path | None = None,
    match_id: str | None = None,
    event_key: str | None = None,
    frame_meta: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    if observe_jsonl is not None:
        return load_observe_frames(observe_jsonl, match_id=match_id, event_key=event_key)

    paths: list[Path] = []
    if image is not None:
        paths.append(image)
    if images:
        paths.extend(images)
    meta = dict(frame_meta or {})
    frames: list[dict[str, Any]] = []
    for idx, path in enumerate(paths):
        frames.append(
            {
                "path": str(path),
                "sample_i": meta.get("sample_i", idx),
                "elapsed_s": (
                    elapsed[idx]
                    if elapsed and idx < len(elapsed)
                    else meta.get("elapsed_s", float(idx))
                ),
                "surface": meta.get("surface"),
                "stream_url": meta.get("stream_url"),
                "page_url": meta.get("page_url"),
                "frame_kind": meta.get("frame_kind"),
                "match_id": meta.get("match_id") or match_id,
                "event_key": meta.get("event_key") or event_key,
                "home_score": meta.get("home_score"),
                "away_score": meta.get("away_score"),
            }
        )
    return frames


def _judge_animation_frame(
    frame: dict[str, Any],
    ocr_engine: PaddleOcrEngine,
) -> dict[str, Any]:
    path = Path(str(frame.get("path") or ""))
    if not path.is_file():
        return {
            "frame_type": "animation",
            "play_state": "unclear",
            "confidence": 0.0,
            "stopped_reason": None,
            "evidence": [f"missing image: {path}"],
        }
    if not ocr_enabled():
        return {
            "frame_type": "animation",
            "play_state": "unclear",
            "confidence": 0.0,
            "stopped_reason": None,
            "evidence": ["OCR disabled"],
        }
    ocr = ocr_engine.extract(path)
    if not ocr.get("ok"):
        return {
            "frame_type": "animation",
            "play_state": "unclear",
            "confidence": 0.0,
            "stopped_reason": None,
            "evidence": [f"OCR unavailable: {ocr.get('error') or 'unknown'}"],
        }
    judged = judge_animation(list(ocr.get("lines") or []))
    judged["frame_type"] = "animation"
    judged["ocr_lines"] = ocr.get("lines") or []
    return judged


def write_frame_sidecars(result: dict[str, Any]) -> list[str]:
    """Write per-frame JSON next to each screenshot."""
    written: list[str] = []
    per_frame = list(result.get("per_frame") or [])
    for row in per_frame:
        path = Path(str(row.get("path") or ""))
        if not path.name:
            continue
        sidecar = path.with_suffix(".json")
        payload = {
            "judged_at": result.get("judged_at"),
            "match_id": result.get("match_id"),
            "event_key": result.get("event_key"),
            "sample_i": row.get("sample_i"),
            "elapsed_s": row.get("elapsed_s"),
            "path": str(path),
            "frame_type": row.get("frame_type") or result.get("frame_type"),
            "play_state": row.get("play_state") or result.get("play_state"),
            "stopped_reason": row.get("stopped_reason", result.get("stopped_reason")),
            "confidence": row.get("confidence", result.get("confidence")),
            "evidence": row.get("evidence") or result.get("evidence") or [],
            "decision_source": result.get("decision_source"),
            "latency_ms": result.get("latency_ms"),
        }
        sidecar.parent.mkdir(parents=True, exist_ok=True)
        sidecar.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        written.append(str(sidecar))
    return written


def _judge_single_frame(
    frame: dict[str, Any],
    *,
    ocr_engine: PaddleOcrEngine,
    match_id: str | None,
    event_key: str | None,
    started: float,
) -> dict[str, Any]:
    inferred, type_evidence = classify_frame(frame)
    frame_type = inferred
    evidence = list(type_evidence)
    decision_source = "ocr_rule"
    model = None
    play_state = "unclear"
    confidence = 0.0
    stopped_reason = None

    per_row = {
        "sample_i": frame.get("sample_i"),
        "elapsed_s": frame.get("elapsed_s"),
        "path": frame.get("path"),
        "frame_type": frame_type,
    }

    if frame_type in {"animation", "unknown"}:
        judged = _judge_animation_frame(frame, ocr_engine)
        per_row.update(
            {
                "play_state": judged.get("play_state"),
                "confidence": judged.get("confidence"),
                "stopped_reason": judged.get("stopped_reason"),
                "evidence": judged.get("evidence"),
            }
        )
        if frame_type == "unknown" and judged.get("play_state") != "unclear":
            frame_type = "animation"
            per_row["frame_type"] = frame_type
        play_state = str(judged.get("play_state") or "unclear")
        confidence = float(judged.get("confidence") or 0.0)
        stopped_reason = judged.get("stopped_reason")
        evidence.extend(list(judged.get("evidence") or []))
        decision_source = "ocr_rule"
    elif frame_type in {"real_video", "mixed"} and vlm_enabled():
        prompt_path = _SCRIPTS.parent / "prompts" / "single.md"
        resp = judge_with_vlm(frames=[frame], frame_type=frame_type, prompt_path=prompt_path)
        if resp.get("ok"):
            raw = dict(resp.get("result") or {})
            model = resp.get("model")
            decision_source = "vlm"
            play_state = str(raw.get("play_state") or "unclear")
            stopped_reason = raw.get("stopped_reason")
            confidence = float(raw.get("confidence") or 0.0)
            evidence.extend(list(raw.get("evidence") or []))
            per_row.update(
                {
                    "play_state": play_state,
                    "confidence": confidence,
                    "stopped_reason": stopped_reason,
                    "evidence": raw.get("evidence") or [],
                }
            )
        else:
            evidence.append(f"VLM unavailable: {resp.get('error')}")
            per_row.update({"play_state": "unclear", "confidence": 0.0, "evidence": list(evidence)})
    else:
        evidence.append(f"no local path for frame_type={frame_type}")
        per_row.update({"play_state": "unclear", "confidence": 0.0, "evidence": list(evidence)})

    return validate_result(
        {
            "judged_at": lib.now_cn_iso(),
            "input_type": "single",
            "frame_type": frame_type,
            "decision_source": decision_source,
            "play_state": play_state,
            "stopped_reason": stopped_reason,
            "confidence": confidence,
            "evidence": list(dict.fromkeys(str(item) for item in evidence if str(item).strip())),
            "per_frame": [per_row],
            "sequence_verdict": None,
            "resumed_at_elapsed_s": None,
            "model": model,
            "ocr_engine": "paddleocr" if ocr_enabled() else None,
            "latency_ms": int((time.time() - started) * 1000),
            "error": None,
            "match_id": match_id or frame.get("match_id"),
            "event_key": event_key or frame.get("event_key"),
        }
    )


def judge_inputs(
    *,
    image: Path | None = None,
    images: list[Path] | None = None,
    elapsed: list[float] | None = None,
    observe_jsonl: Path | None = None,
    match_id: str | None = None,
    event_key: str | None = None,
    frame_meta: dict[str, Any] | None = None,
    append_output: bool = True,
    output_path: Path | None = None,
    write_sidecars: bool = True,
    ocr_engine: PaddleOcrEngine | None = None,
) -> dict[str, Any]:
    """Judge screenshot(s) one-by-one. Each frame is an independent conclusion."""
    started = time.time()
    frames = _normalize_frames(
        image=image,
        images=images,
        elapsed=elapsed,
        observe_jsonl=observe_jsonl,
        match_id=match_id,
        event_key=event_key,
        frame_meta=frame_meta,
    )
    if not frames:
        return validate_result(
            {
                "judged_at": lib.now_cn_iso(),
                "input_type": "single",
                "frame_type": "unknown",
                "decision_source": "hybrid",
                "play_state": "unclear",
                "stopped_reason": None,
                "confidence": 0.0,
                "evidence": ["no frames found for input"],
                "per_frame": [],
                "sequence_verdict": None,
                "resumed_at_elapsed_s": None,
                "model": None,
                "ocr_engine": "paddleocr" if ocr_enabled() else None,
                "latency_ms": int((time.time() - started) * 1000),
                "error": "no_frames",
                "match_id": match_id,
                "event_key": event_key,
            }
        )

    engine = ocr_engine or get_shared_ocr_engine()
    last: dict[str, Any] | None = None
    for frame in frames:
        # Each image is judged independently (no sequence fusion).
        frame_started = time.time()
        result = _judge_single_frame(
            frame,
            ocr_engine=engine,
            match_id=match_id,
            event_key=event_key,
            started=frame_started,
        )
        if write_sidecars:
            result["sidecar_paths"] = write_frame_sidecars(result)
        if append_output:
            lib.append_jsonl(output_path or default_output_path(), [result])
        last = result
    assert last is not None
    last["latency_ms"] = int((time.time() - started) * 1000)
    return last
