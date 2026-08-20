---
name: pitch-state
description: Judge whether play has resumed from Dongqiudi screenshots. Classify animation vs real video, use PaddleOCR plus rules for animation, and fall back to an OpenAI-compatible vision model for real video. Use when working with dqd_stream_frames, dqd_stream_observe.jsonl, 开球判断, VAR/庆祝暂停, animation tracker screenshots, or live broadcast frames.
disable-model-invocation: true
---

# Pitch State

根据懂球帝截图判断“场上是否已经恢复开球”。默认只做观察与落盘，不直接门控交易。

每张截图独立判定；进程启动时预热 OCR，进球后按帧即时出结论。

## Quick start

```bash
# 单张图
python3 .cursor/skills/pitch-state/scripts/judge.py \
  --image data/pm-quote/dqd_stream_frames/.../00_00s.jpg --json

# 多张图（仍按张独立判定，不融合序列）
python3 .cursor/skills/pitch-state/scripts/judge.py \
  --images a.jpg b.jpg c.jpg --elapsed 0,10,20 --json

# 从 observe JSONL 按 event_key 取帧（同样按张独立判定）
python3 .cursor/skills/pitch-state/scripts/judge.py \
  --observe-jsonl data/pm-quote/dqd_stream_observe.jsonl \
  --match-id 54329163 \
  --event-key score_change_54329163_0-0-_1-0 \
  --json
```

输出会返回统一 JSON，并可选追加到 `data/pm-quote/pitch_state_judge.jsonl`。
每个截图旁写同名 `.json`：

```text
data/pm-quote/dqd_stream_frames/<match_id>/<event_key>/
  00_00s.jpg
  00_00s.json
  01_20s.jpg
  01_20s.json
```

## Agent workflow

1. 优先按**单帧**判定；多图输入也是逐张独立结论，不做序列融合。
2. 先判定 `frame_type`：
   - `animation`：懂球帝动画球场 / tracker
   - `real_video`：真实转播画面
   - `unknown` / `mixed`
3. 动画图只走本地 OCR + 规则，**不再升级 VLM**。门控只看「进攻/控球/VAR」等关键词；**不做比分 OCR**。
4. 真实视频图仍可走 OpenAI 兼容视觉模型。
5. 保守输出：不确定时给 `unclear`，不要硬判 `in_play`。

## Labels

- `play_state`
  - `in_play`：比赛已恢复进行，或明显处于正常比赛推进/定位球执行状态
  - `stopped`：比赛仍暂停，或处于 VAR / 换人 / 进球庆祝 / 未开始等状态
  - `unclear`：信息不足，不能可靠判断

- `frame_type`
  - `animation`：懂球帝动画/球场 tracker
  - `real_video`：真实比赛视频帧
  - `mixed`：一个输入里两类都有（仅元数据提示）
  - `unknown`：无法可靠分类

## Inputs

支持三类输入：

- `--image <path>`
- `--images <path...>` + 可选 `--elapsed 0,10,20`
- `--observe-jsonl ... --match-id ... --event-key ...`

`observe-jsonl` 默认读取 `dqd_stream_observe.jsonl` 的 `frame_path`、`sample_i`、`elapsed_s`、`surface`、`stream_url`、`page_url`、`frame_kind`。

## Environment

```bash
# animation 本地 OCR
PITCH_STATE_OCR=1
PITCH_STATE_OCR_MIN_CONF=0.75

# VLM
PITCH_STATE_VLM=1
OPENAI_API_KEY=...
PITCH_STATE_VLM_BASE_URL=https://...
PITCH_STATE_VLM_MODEL=gpt-4.1-mini

# watch 挂钩：observe 启动时预热 OCR；每抓到一张成功截图立刻判定
QUOTE_PITCH_STATE=1

# 输出
PITCH_STATE_APPEND_JSONL=1
PITCH_STATE_OUTPUT_PATH=data/pm-quote/pitch_state_judge.jsonl
```

## Outputs

统一 JSON 关键字段：

- `input_type`
- `frame_type`
- `decision_source`
- `play_state`
- `stopped_reason`
- `confidence`
- `evidence`
- `per_frame`

## Notes

- 这个 skill 默认**不接交易逻辑**。
- `掷界外球` / `任意球` / `角球` 这类定位球，若没有明确暂停遮罩，按比赛已恢复/正在进行处理。
- 对动画图，若 OCR 与规则没有高置信结果，返回 `unclear`（不升级 VLM）。

## Files

- Main doc: [reference.md](reference.md)
- Scripts: [scripts/judge.py](scripts/judge.py)
