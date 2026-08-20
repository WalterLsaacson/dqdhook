You are judging whether a soccer match has resumed play from a time-ordered screenshot sequence.

Return JSON only.

Rules:
- Frames are ordered from earliest to latest.
- Determine per-frame `play_state` and the overall sequence verdict.
- If play resumes during the sequence, use `sequence_verdict="resumed_at_s"` and set `resumed_at_elapsed_s` to the first reliable resumed timestamp.
- If the whole sequence still shows a stopped state, use `sequence_verdict="still_stopped"`.
- If evidence is too weak or contradictory, use `sequence_verdict="unclear"`.
- For animation/tracker screenshots, text like `进攻`, `控球`, `危险进攻`, `掷界外球`, `任意球`, `角球`, `球门球` usually indicates play is active or progressing.
- Text like `VAR`, `换人`, `进球`, `庆祝`, `暂停`, `未开始`, `暂无动画直播` indicates stopped state.

Required JSON shape:
{
  "play_state_now": "in_play",
  "sequence_verdict": "resumed_at_s",
  "resumed_at_elapsed_s": 30,
  "confidence": 0.0,
  "evidence": ["..."],
  "per_frame": [
    {
      "sample_i": 0,
      "elapsed_s": 0,
      "play_state": "stopped",
      "confidence": 0.0,
      "evidence": ["..."]
    }
  ]
}
