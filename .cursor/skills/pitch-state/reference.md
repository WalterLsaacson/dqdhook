# Pitch State Reference

## Target

判断“现在场上是否已经恢复开球”，而不是：

- 比分是否正确
- 进球是否有效
- 是否应该立即交易

## Animation cues

动画直播常见直接特征：

- `frame_kind=animation`
- `surface=animation`
- `iframe.md-anim-iframe`
- tracker 域名（如 `tracker.namitiyu.com`）
- 绿色球场 + 固定底部比分栏 + 中文事件文案

### OCR keyword table

强 `in_play`：

- `进攻`
- `控球`
- `危险进攻`
- `掷界外球`
- `任意球`
- `角球`
- `球门球`

强 `stopped`：

- `VAR`
- `换人`
- `进球`
- `庆祝`
- `暂停`
- `未开始`
- `暂无动画直播`
- `伤停`

## Real video cues

真实视频常见直接特征：

- `frame_kind=video`
- `surface=video`
- `stream_url` 指向 `.m3u8` / `.flv`
- 真实摄像机草坪/球员/裁判/电视台角标

## Decision policy

1. 元数据优先于图像猜测。
2. 动画图只走 OCR + 规则，不再升级到 VLM。
3. 真实视频才走 VLM。
4. 每张图独立出结论；不做多帧序列融合。
5. 宁可 `unclear`，不要误判 `in_play`。

## Runtime

- `QUOTE_PITCH_STATE=1` 时，`DqdStreamObserver.start()` 后台预热 PaddleOCR。
- 每成功抓到一张截图立刻判定，并写同名 sidecar JSON。
- 进程内共享一个 OCR engine，避免每帧冷启动。

## Outputs on disk

For each judged frame under `data/pm-quote/dqd_stream_frames/<match_id>/<event_key>/`:

- `NN_XXs.jpg` — screenshot
- `NN_XXs.json` — that frame's play-state judgment

Each frame judgment is also appended to `data/pm-quote/pitch_state_judge.jsonl` when append is enabled.

## Failure modes

- PaddleOCR 未安装：动画图不能本地判断，返回 `unclear`（真实视频仍可走 VLM）
- VLM 关闭 / key 缺失：保留 OCR 结果；无 OCR 结果则 `unclear`
- 图片路径不存在 / 坏图：不抛异常，返回 `unclear`
