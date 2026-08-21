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
from animation_rules import extract_board_score  # noqa: E402


def _make_jpg(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\xff\xd8\xff\xd9")


def _test_extract_board_score() -> None:
    # Drita-like: clock + board + wing jersey digits.
    lines = [
        {"text": "VAR", "score": 0.99, "box": [[427, 83], [480, 83], [480, 104], [427, 104]]},
        {
            "text": "93:51 (+4)",
            "score": 0.99,
            "box": [[412, 340], [500, 340], [500, 358], [412, 358]],
        },
        {
            "text": "3:2",
            "score": 0.995,
            "box": [[411, 362], [470, 362], [470, 383], [411, 383]],
        },
        {"text": "0", "score": 0.99, "box": [[71, 374], [85, 374], [85, 387], [71, 387]]},
        {"text": "3", "score": 0.99, "box": [[111, 374], [125, 374], [125, 387], [111, 387]]},
        {"text": "4", "score": 0.99, "box": [[867, 375], [880, 375], [880, 385], [867, 385]]},
    ]
    board = extract_board_score(lines, expected_home=3, expected_away=2)
    assert board and board["home"] == 3 and board["away"] == 2, board

    # Reversal board 2:2 still preferred over jerseys/clock.
    lines[2] = {
        "text": "2:2",
        "score": 0.99,
        "box": [[409, 362], [470, 362], [470, 383], [409, 383]],
    }
    board2 = extract_board_score(lines, expected_home=3, expected_away=2)
    assert board2 and board2["text"] == "2-2", board2

    # Chinese noise / top overlay must not become the board score.
    junk = [
        {
            "text": "2-2",
            "score": 0.99,
            "box": [[391, 118], [480, 118], [480, 175], [391, 175]],
        },
        {
            "text": "管2：21",
            "score": 0.77,
            "box": [[378, 356], [470, 356], [470, 378], [378, 378]],
        },
        {"text": "3", "score": 0.99, "box": [[109, 374], [125, 374], [125, 387], [109, 387]]},
    ]
    assert extract_board_score(junk, expected_home=3, expected_away=2) is None

    # Classic false positive: jersey-glue style 10:1 when expecting 0-1.
    glue = [
        {
            "text": "10:1",
            "score": 0.99,
            "box": [[400, 360], [470, 360], [470, 384], [400, 384]],
        },
        {
            "text": "0:1",
            "score": 0.98,
            "box": [[410, 362], [460, 362], [460, 382], [410, 382]],
        },
    ]
    # With expected 0-1, prefer the plausible 0-1 (10-1 rejected by bounds).
    g = extract_board_score(glue, expected_home=0, expected_away=1)
    assert g and g["text"] == "0-1", g
    # Without a clean alternate, wild 10-1 alone is rejected when expected is 0-1.
    assert (
        extract_board_score(
            [
                {
                    "text": "10:1",
                    "score": 0.99,
                    "box": [[400, 360], [470, 360], [470, 384], [400, 384]],
                }
            ],
            expected_home=0,
            expected_away=1,
        )
        is None
    )


def main() -> int:
    _test_extract_board_score()
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

            # Wrong board digits must not block in_play when 控球 is present
            # (default path: no require_score).
            ignore_score = pipeline.judge_inputs(
                image=img1,
                frame_meta={"home_score": 1, "away_score": 0},
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
            assert ignore_score["play_state"] == "in_play", ignore_score

            # After VAR, mismatched board score must block in_play (Drita-style).
            post_var = pipeline.judge_inputs(
                image=img1,
                frame_meta={
                    "home_score": 3,
                    "away_score": 2,
                    "require_score": True,
                },
                ocr_engine=FakeOcrEngine(
                    {
                        "00_00s.jpg": [
                            {"text": "进攻", "score": 0.99, "box": [[100, 100], [120, 100], [120, 120], [100, 120]]},
                            {
                                "text": "2:2",
                                "score": 0.98,
                                "box": [[200, 360], [260, 360], [260, 384], [200, 384]],
                            },
                        ]
                    }
                ),
                append_output=False,
                write_sidecars=False,
            )
            assert post_var["play_state"] == "unclear", post_var
            assert post_var.get("score_match") is False, post_var

            post_var_ok = pipeline.judge_inputs(
                image=img1,
                frame_meta={
                    "home_score": 3,
                    "away_score": 2,
                    "require_score": True,
                },
                ocr_engine=FakeOcrEngine(
                    {
                        "00_00s.jpg": [
                            {"text": "控球", "score": 0.99, "box": [[100, 100], [120, 100], [120, 120], [100, 120]]},
                            {
                                "text": "3:2",
                                "score": 0.98,
                                "box": [[200, 360], [260, 360], [260, 384], [200, 384]],
                            },
                        ]
                    }
                ),
                append_output=False,
                write_sidecars=False,
            )
            assert post_var_ok["play_state"] == "in_play", post_var_ok
            assert post_var_ok.get("score_match") is True, post_var_ok

            # Digits alone (no play/stop tokens) stay unclear.
            score_only = pipeline.judge_inputs(
                image=img1,
                frame_meta={"home_score": 1, "away_score": 0},
                ocr_engine=FakeOcrEngine(
                    {
                        "00_00s.jpg": [
                            {
                                "text": "1:0",
                                "score": 0.9,
                                "box": [[200, 360], [260, 360], [260, 384], [200, 384]],
                            }
                        ]
                    }
                ),
                append_output=False,
                write_sidecars=False,
            )
            assert score_only["play_state"] == "unclear", score_only

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
