#!/usr/bin/env python3
"""Smoke tests for pitch-state pipeline with mocked OCR/VLM."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import pipeline  # noqa: E402


def _make_jpg(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\xff\xd8\xff\xd9")


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        out = root / "data" / "pm-quote" / "pitch_state_judge.jsonl"
        frame_dir = root / "data" / "pm-quote" / "dqd_stream_frames" / "m1" / "score_change_m1"
        img1 = frame_dir / "00_00s.jpg"
        img2 = frame_dir / "01_20s.jpg"
        _make_jpg(img1)
        _make_jpg(img2)

        old_vlm = pipeline.judge_with_vlm
        vlm_calls = {"n": 0}

        class FakeOcrEngine:
            def __init__(self, lines_by_name: dict[str, list[dict[str, object]]]) -> None:
                self._lines_by_name = lines_by_name

            def extract(self, path: Path) -> dict[str, object]:
                lines = self._lines_by_name.get(path.name) or self._lines_by_name.get("*") or []
                return {"ok": True, "error": None, "lines": lines}

        def fake_vlm(**_kwargs: object) -> dict[str, object]:
            vlm_calls["n"] += 1
            return {"ok": False, "error": "should_not_call_vlm_for_animation"}

        pipeline.judge_with_vlm = fake_vlm
        try:
            play = pipeline.judge_inputs(
                image=img1,
                ocr_engine=FakeOcrEngine(
                    {"00_00s.jpg": [{"text": "进攻", "score": 0.99, "box": []}]}
                ),
                append_output=True,
                output_path=out,
                write_sidecars=True,
            )
            assert play["play_state"] == "in_play", play
            assert play["decision_source"] == "ocr_rule", play
            assert play["sequence_verdict"] is None, play
            assert vlm_calls["n"] == 0, vlm_calls
            assert img1.with_suffix(".json").is_file(), "missing per-frame sidecar"
            assert not (frame_dir / "pitch_state.json").exists(), "sequence summary should not be written"

            # Expected DQD score must match OCR board score before in_play.
            mismatch = pipeline.judge_inputs(
                image=img1,
                frame_meta={"expected_home_score": 1, "expected_away_score": 0},
                ocr_engine=FakeOcrEngine(
                    {
                        "00_00s.jpg": [
                            {"text": "控球", "score": 0.99, "box": [[100, 100], [120, 100], [120, 120], [100, 120]]},
                            {"text": "0:0", "score": 0.98, "box": [[200, 360], [260, 360], [260, 384], [200, 384]]},
                            {"text": "27:23", "score": 1.0, "box": [[180, 338], [240, 338], [240, 356], [180, 356]]},
                        ]
                    }
                ),
                append_output=False,
                write_sidecars=False,
            )
            assert mismatch["play_state"] == "unclear", mismatch
            assert mismatch.get("score_match") is False, mismatch
            assert mismatch.get("ocr_score") == "0-0", mismatch
            assert any("比分不一致" in str(e) for e in mismatch.get("evidence") or []), mismatch

            matched = pipeline.judge_inputs(
                image=img1,
                frame_meta={"home_score": 1, "away_score": 0},
                ocr_engine=FakeOcrEngine(
                    {
                        "00_00s.jpg": [
                            {"text": "控球", "score": 0.99, "box": [[100, 100], [120, 100], [120, 120], [100, 120]]},
                            {"text": "1:0", "score": 0.98, "box": [[200, 360], [260, 360], [260, 384], [200, 384]]},
                        ]
                    }
                ),
                append_output=False,
                write_sidecars=False,
            )
            assert matched["play_state"] == "in_play", matched
            assert matched.get("score_match") is True, matched
            assert matched.get("ocr_score") == "1-0", matched

            stopped = pipeline.judge_inputs(
                image=img2,
                match_id="m1",
                event_key="score_change_m1",
                ocr_engine=FakeOcrEngine(
                    {"01_20s.jpg": [{"text": "VAR", "score": 0.99, "box": []}]}
                ),
                append_output=True,
                output_path=out,
                write_sidecars=True,
            )
            assert stopped["play_state"] == "stopped", stopped
            assert stopped["sequence_verdict"] is None, stopped
            assert vlm_calls["n"] == 0, "animation path must not call VLM"
            assert img2.with_suffix(".json").is_file(), "missing second sidecar"

            # Multi-path CLI still judges each image independently (last result returned).
            multi = pipeline.judge_inputs(
                images=[img1, img2],
                elapsed=[0.0, 20.0],
                ocr_engine=FakeOcrEngine(
                    {
                        "00_00s.jpg": [{"text": "进攻", "score": 0.99, "box": []}],
                        "01_20s.jpg": [{"text": "控球", "score": 0.99, "box": []}],
                    }
                ),
                append_output=False,
                write_sidecars=False,
            )
            assert multi["play_state"] == "in_play", multi
            assert multi["sequence_verdict"] is None, multi

            missing = pipeline.judge_inputs(
                image=root / "missing.jpg",
                append_output=False,
                output_path=out,
                write_sidecars=False,
            )
            assert missing["play_state"] == "unclear", missing

            rows = [
                json.loads(line)
                for line in out.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            assert len(rows) == 2, rows
        finally:
            pipeline.judge_with_vlm = old_vlm

    print("ok: pitch-state smoke")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
