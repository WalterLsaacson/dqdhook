You are judging whether a soccer match has already resumed play from a single screenshot.

Return JSON only.

Rules:
- `play_state` must be one of `in_play`, `stopped`, `unclear`.
- `stopped_reason` must be one of `var`, `celebration`, `substitution`, `not_started`, `overlay_pause`, `other`, or `null`.
- For animation/tracker screenshots, text like `进攻`, `控球`, `危险进攻`, `掷界外球`, `任意球`, `角球`, `球门球` usually indicates play is active or progressing.
- Text like `VAR`, `换人`, `进球`, `庆祝`, `暂停`, `未开始`, `暂无动画直播` indicates stopped state.
- If evidence is weak or conflicting, output `unclear`.

Required JSON shape:
{
  "play_state": "...",
  "stopped_reason": null,
  "confidence": 0.0,
  "evidence": ["..."]
}
