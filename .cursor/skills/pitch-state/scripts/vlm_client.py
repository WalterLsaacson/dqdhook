#!/usr/bin/env python3
"""OpenAI-compatible VLM client for pitch-state judgment."""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from typing import Any
from urllib import request


def vlm_enabled() -> bool:
    raw = os.getenv("PITCH_STATE_VLM")
    if raw is None or str(raw).strip() == "":
        return True
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _api_key() -> str:
    return str(os.getenv("PITCH_STATE_VLM_API_KEY") or os.getenv("OPENAI_API_KEY") or "").strip()


def _base_url() -> str:
    return str(os.getenv("PITCH_STATE_VLM_BASE_URL") or "https://api.openai.com/v1").rstrip("/")


def _model() -> str:
    return str(os.getenv("PITCH_STATE_VLM_MODEL") or "gpt-4.1-mini").strip()


def _read_prompt(path: Path) -> str:
    return path.read_text(encoding="utf-8").strip()


def _image_part(path: Path) -> dict[str, Any]:
    mime = "image/jpeg"
    suffix = path.suffix.lower()
    if suffix == ".png":
        mime = "image/png"
    payload = base64.b64encode(path.read_bytes()).decode("ascii")
    return {
        "type": "image_url",
        "image_url": {"url": f"data:{mime};base64,{payload}"},
    }


def judge_with_vlm(
    *,
    frames: list[dict[str, Any]],
    frame_type: str,
    prompt_path: Path,
) -> dict[str, Any]:
    if not vlm_enabled():
        return {"ok": False, "error": "vlm_disabled"}
    key = _api_key()
    if not key:
        return {"ok": False, "error": "missing_api_key"}

    prompt = _read_prompt(prompt_path)
    text_context = {
        "frame_type": frame_type,
        "frames": [
            {
                "sample_i": frame.get("sample_i"),
                "elapsed_s": frame.get("elapsed_s"),
                "surface": frame.get("surface"),
                "frame_kind": frame.get("frame_kind"),
            }
            for frame in frames
        ],
    }
    content: list[dict[str, Any]] = [
        {"type": "text", "text": prompt},
        {"type": "text", "text": json.dumps(text_context, ensure_ascii=False)},
    ]
    for frame in frames:
        path = Path(str(frame.get("path") or ""))
        if path.is_file():
            content.append({"type": "text", "text": f"frame sample_i={frame.get('sample_i')} elapsed_s={frame.get('elapsed_s')}"})
            content.append(_image_part(path))

    body = {
        "model": _model(),
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": "You are a strict JSON-only soccer play-state judge."},
            {"role": "user", "content": content},
        ],
    }
    req = request.Request(
        url=f"{_base_url()}/chat/completions",
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
        data=json.dumps(body).encode("utf-8"),
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=90) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}

    try:
        content_text = payload["choices"][0]["message"]["content"]
        if isinstance(content_text, list):
            content_text = "".join(str(part.get("text") or "") for part in content_text if isinstance(part, dict))
        result = json.loads(str(content_text))
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"invalid_vlm_json: {e}"}
    return {"ok": True, "result": result, "model": _model()}
