#!/usr/bin/env python3
"""PaddleOCR wrapper for animation screenshots."""

from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger("pitch_state.ocr")

_shared: "PaddleOcrEngine | None" = None
_shared_lock = threading.Lock()
_infer_lock = threading.Lock()
_load_lock = threading.Lock()


def ocr_enabled() -> bool:
    raw = os.getenv("PITCH_STATE_OCR")
    if raw is None or str(raw).strip() == "":
        return True
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def get_shared_ocr_engine() -> "PaddleOcrEngine":
    global _shared
    with _shared_lock:
        if _shared is None:
            _shared = PaddleOcrEngine()
        return _shared


def reset_shared_ocr_engine() -> None:
    """Test helper: drop the process-wide engine so the next call recreates it."""
    global _shared
    with _shared_lock:
        _shared = None


def warmup_ocr() -> dict[str, Any]:
    """Load PaddleOCR models once at process start."""
    started = time.time()
    engine = get_shared_ocr_engine()
    ok = bool(engine.available)
    out = {
        "ok": ok,
        "error": None if ok else (engine.error or "paddleocr_unavailable"),
        "latency_ms": int((time.time() - started) * 1000),
    }
    if ok:
        logger.info("pitch-state OCR warmed in %sms", out["latency_ms"])
    else:
        logger.warning("pitch-state OCR warmup failed: %s", out["error"])
    return out


class PaddleOcrEngine:
    def __init__(self) -> None:
        self._ocr = None
        self._load_error: str | None = None

    def _ensure(self) -> None:
        if self._ocr is not None or self._load_error is not None:
            return
        with _load_lock:
            if self._ocr is not None or self._load_error is not None:
                return
            # Skip slow hoster connectivity probe on every process start.
            os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")
            try:
                from paddleocr import PaddleOCR
            except Exception as e:  # noqa: BLE001
                self._load_error = str(e)
                return
            try:
                # PaddleOCR 3.x prefers use_textline_orientation; keep a 2.x fallback.
                try:
                    self._ocr = PaddleOCR(use_textline_orientation=True, lang="ch")
                except TypeError:
                    self._ocr = PaddleOCR(use_angle_cls=True, lang="ch")
            except Exception as e:  # noqa: BLE001
                self._load_error = str(e)

    @property
    def available(self) -> bool:
        self._ensure()
        return self._ocr is not None

    @property
    def error(self) -> str | None:
        self._ensure()
        return self._load_error

    @staticmethod
    def _lines_from_legacy(raw: Any) -> list[dict[str, Any]]:
        lines: list[dict[str, Any]] = []
        for page in raw or []:
            for item in page or []:
                if not isinstance(item, (list, tuple)) or len(item) < 2:
                    continue
                box, rec = item[0], item[1]
                text = ""
                score = 0.0
                if isinstance(rec, (list, tuple)) and len(rec) >= 2:
                    text = str(rec[0] or "").strip()
                    try:
                        score = float(rec[1])
                    except (TypeError, ValueError):
                        score = 0.0
                if text:
                    lines.append({"text": text, "score": score, "box": box})
        return lines

    @staticmethod
    def _lines_from_predict(raw: Any) -> list[dict[str, Any]]:
        lines: list[dict[str, Any]] = []
        items = raw if isinstance(raw, list) else [raw]
        for item in items:
            if not isinstance(item, dict):
                continue
            texts = item.get("rec_texts") or item.get("texts") or []
            scores = item.get("rec_scores") or item.get("scores") or []
            boxes = item.get("dt_polys") or item.get("rec_polys") or item.get("boxes") or []
            for idx, text in enumerate(texts):
                cleaned = str(text or "").strip()
                if not cleaned:
                    continue
                score = 0.0
                if idx < len(scores):
                    try:
                        score = float(scores[idx])
                    except (TypeError, ValueError):
                        score = 0.0
                box = boxes[idx] if idx < len(boxes) else None
                lines.append({"text": cleaned, "score": score, "box": box})
        return lines

    def extract(self, path: Path) -> dict[str, Any]:
        self._ensure()
        if self._ocr is None:
            return {"ok": False, "error": self._load_error or "paddleocr_unavailable", "lines": []}
        # PaddleOCR is not safe for concurrent predict calls.
        with _infer_lock:
            try:
                if hasattr(self._ocr, "predict"):
                    raw = self._ocr.predict(str(path))
                    lines = self._lines_from_predict(raw)
                else:
                    raw = self._ocr.ocr(str(path), cls=True)
                    lines = self._lines_from_legacy(raw)
            except TypeError:
                try:
                    raw = self._ocr.ocr(str(path))
                    lines = self._lines_from_legacy(raw)
                except Exception as e:  # noqa: BLE001
                    return {"ok": False, "error": str(e), "lines": []}
            except Exception as e:  # noqa: BLE001
                return {"ok": False, "error": str(e), "lines": []}
        return {"ok": True, "error": None, "lines": lines}
