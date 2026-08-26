---
name: polymarket-quote
description: >-
  After match-bridge emits match_finished / score_change, discovers Polymarket
  moneyline (and optional More Markets / Exact Score) tokens, settles them from
  the score, quotes CLOB best_bid/best_ask (with depth), and optionally places
  in-process CLOB market orders on fee-aware misprices. Use when the user wants
  post-FT mispricing scans, order-book snapshots, settlement-aware quotes, or
  low-latency dry-run / live trading from the quote skill.
---

# Polymarket Post-FT Quote

Consumes **match-bridge** 进球/终场事件，按比分解读盘口，对 CLOB token 询价；判定 `misprice` 后可在**同一进程内**下单（不经 `opportunities.jsonl` 二次消费）。

**当前策略（同帧 DOM∧AF∧射门）**：DQD `score_change` 进球且已配对 → 进球后 **+0s** 起每 **5s 先采 DOM**。**见过射门且本拍 `in_play` 才打 AF**，之后同拍 AF+DOM 直到买入/超时。买入需 **DOM `in_play` 且 AF `ok && score_match` 且本球从 t0 起见过射门**（pop「射门」或 marks `ball`/`net`；不把射门当 `in_play`）。买入后 **立刻停 AF**（省额度），**DOM 继续抓到 120s** 再停。没射门或未 `in_play` 不打 AF（观察行 `af.skipped=before_shot` / `before_in_play`）。AF 限流/失败本拍否决（fail-closed），继续采。`in_play` 但还没射门 → `WAIT_SHOT`（此时不拉 AF）。买入前 DOM 出现 **VAR** → **该球永久不下单**（即使见过射门）。开场球若**同一过渡刚被回撤过**，或时钟 **≥90′**，`start_gate` 直接 skip；约 35′ 的普通开场球仍走完整门控。限价 rest 需 `QUOTE_REST_ENABLED=1`。终场立刻询价。AF∨DOM **或门买入否决、不实现**（见 `design-af-dom-or-gate.md`）。回撤确认轨从 t0 每 5s AF∨DOM（不要求射门、不推迟 AF）；**认分 flatten 后立刻停轨**（不像买入那样再 DOM 拖到 120s）。

> 询价、挂 rest、flatten、rest 对账在 **CLOB worker 线程**；watch tick 只 `start_gate` / 取消门控 / 把事件载荷入队。别场的 `/books` 和 GTC 不再堵住新球开 DOM。**`start_gate` 后并行预热** Gamma catalog + 周期 `POST /books`（`QUOTE_GATE_PREWARM`，默认开）；BUY 询价优先吃新鲜预热盘口，省掉热路径上约 0.5–1s 的 books RTT（**不缩短** DOM/AF/射门等待）。

> 动画已改比分、随后 DQD 才回撤的**延迟回撤**在买入时刻无法预知；出口靠懂球帝回撤后再开 5s AF∨DOM 确认。**认分 flatten 后立刻停 AF+DOM**（买入后则仍抓 DOM 到原超时，便于事后看 VAR/庆祝，不再打 AF）。

- 懂球帝 **回撤**：立刻取消未完成进球门控，并按 **event_key** 撤销已入队的询价（不按 `match_id` 永久拉黑）。rest 取消入队优先级高于 idle flatten/rest 对账，对账不挡 `rest_cancel`。**进程内 bridge 入队回撤时就会 `cancel_match`**。询价 tick **先扫回撤再 `start_gate`**。回撤 ts 挡住该进球 stem（更早或相同 ts）。非开场的重判进球（更晚 ts）仍可开；**开场球**（0-0→1-0 / 0-0→0-1）若同一过渡刚被撤过，或时钟 ≥90′，不再开闸（`pitch_gate_reversal_risk_skip`）。询价 tick **先处理事件再 drain**。若该场 **已有仓**，再开 5s AF+DOM（期望=回撤后比分）；某一拍 AF 或 DOM **比分条**对齐（不要求 `in_play`）→ flatten（**不受** `QUOTE_GATE_PROTECT_S` 窗限制）并 **立刻停 5s 轨**。懂球帝回撤本身不立刻平仓；窗只约束未确认的 DQD 路径。两边都不认直到 120s → **持仓**。未买入的回撤只取消门控。
- 事件超过 **`QUOTE_FT_MAX_AGE_S`（默认 900s）** → 跳过（防重启重放）
- 同 `match_id` 已处理过终场 → 跳过
- 门控路径需 `QUOTE_DQD_STREAM_OBSERVE=1`（缺则 `pitch_gate_unavailable`，该球不下单）
- **判定源是动画 DOM，不截图 / 不跑 OCR**：共用一台 Chromium，进行中已配对场预开 tracker 页；同场后续进球复用标签。每次采样读 `.pop-box` 与 `.center-box`。无 JPEG、无 `QUOTE_GATE_REF_SCREENSHOT`。标签上限 `QUOTE_DOM_POOL_MAX`（默认 24）；预热 `QUOTE_DOM_WARM`（默认开）/`QUOTE_DOM_WARM_INTERVAL_S`（默认 10s）/`QUOTE_DOM_WARM_OPEN_TIMEOUT_S`（默认 3s）。开页等待动画时会穿插处理其它场的 DOM 读。
- **防僵死**：判定要求 `.center-box` 时钟相对上一次读数有推进；时钟没走 → `unclear`（`stale_page`），不下单。
- **页面是纳米 tracker**：`animation_live` URL 打开 `tracker.namitiyu.com` 读 DOM，不是懂球帝比赛页。不做 MQTT 球位观察。
- Pitch-gate 限价 rest：需 **`QUOTE_REST_ENABLED=1`** → 目标 @**0.995**，但 **不信** Gamma/book 的 `0.001` 元数据；CLOB 足球最小 tick 是 **0.01**，rest 一律按 0.01 **向下收到 0.99**。**`QUOTE_REST_USDC`（默认 $5）** / **`GTC` 一直挂着**（回撤、终场、手取消才撤；`QUOTE_REST_EXPIRE_S>0` 才改回 GTD）。门控 rest **不受** `QUOTE_MAX_OPEN_USDC` 限制。没有卖盘（一边倒买盘）也挂，等砸盘。FAK / misprice 上限 **ask≤0.995**（fee 后 `min_net≈0.00475`）。
- Odds/Bet365：跟 DOM 同一拍后台写入 `book_context_observe.jsonl`（Grade A/B/C 看板旁路），**不挡**买入/flatten，**不改下单 size**。已配对场在距开球 **30 分钟**时 **采一次** Bet365+1xbet 全盘口，写入 `data/pm-quote/prematch_odds.jsonl`。
- **主客对调**：懂球帝/纳米主场与 Polymarket 相反时，事件带 `sides_swapped`；门控用动画主场比分条，半场大小球用 PM 方向的 `home_half`/`away_half`，避免把雷恩的半场算到巴黎头上。

## Quick start

**主入口（推荐）** — System Main 拉起 boards + `pm_quote watch`（默认 goals+ft live）：

```bash
python3 frontend/run_main.py
python3 frontend/run_main.py --take-depth walk --max-usdc 5
python3 frontend/run_main.py --goals-mode dry --ft-mode dry   # 全 dry
python3 frontend/run_main.py --no-trade                       # 仅询价
```

Hub：http://127.0.0.1:8790/ · quote 日志：`data/pm-quote/watch.log` · 成交尝试：`data/pm-quote/trades.jsonl`

Skill 单跑（调试用）：

```bash
python3 .cursor/skills/polymarket-quote/scripts/pm_quote.py once --from-bridge --json
python3 .cursor/skills/polymarket-quote/scripts/pm_quote.py watch --goals-mode live --ft-mode dry
python3 .cursor/skills/polymarket-quote/scripts/pm_quote.py once --from-bridge --no-trade
```

Trading deps (once):

```bash
pip install -r .cursor/skills/polymarket-quote/requirements-trade.txt
```

Env (same names as simple_str): `PRIVATE_KEY`, `FUNDER`, `SIGNATURE_TYPE`, `CHAIN_ID`, `CLOB_HOST`. Load from repo `.env` or `--trade-env-file`. Never commit keys.

## Agent workflow

1. Prefer System Main (`frontend/run_main.py`): boards (UI) + `pm_quote watch` owns **in-process** match-bridge (memory `event_queue` → quote). `MAIN_BRIDGE_INPROC=0` falls back to bridge-board file wake.
2. Bridge events in `data/bridge/events.jsonl`:
   - `score_change` goal-up (paired) → **start pitch-gate** (DOM @+0s every 5s; AF from first `in_play`); quote on same-tick DOM `in_play` ∧ AF `score_match` ∧ latched 射门; stop AF after buy, keep DOM until 120s
   - `score_change` reversal → first cancel/block the undone goal, then cancel rest + pitch-gate; if lots are open, 5s AF∨DOM trail → flatten on first score_match vs post-reverse score **then stop the trail**
   - `match_finished` → immediate quote (default live; stale / once-per-match skip)
3. Join `data/bridge/matches.json` for full `market_refs` / `event_id`.
4. **Latency path**: wake on events (poll ~50ms; `--interval` default **0.25s**). Market warmer fills `data/pm-quote/market_cache/{match_id}.json`. Live quote: CLOB worker thread (not the watch tick) runs one `/books` POST; totals/BTTS before exact.
5. On misprice after pitch-gate, executor plans fills → `trades.jsonl` (`dry_run` or live `posted`). Pitch-gate and FT buys skip `min_buy_price`; pitch-gate also skips size/$1 floors (fee/`min_net` + `QUOTE_MAX_USDC` remain).
6. **No post-goal CLOB re-quotes.** One `quote_bridge_event` per aligned buy.

## Trading flags

| Flag | Default | Meaning |
|---|---|---|
| (default) | goals **live** / ft **live** | Pitch-gate live buys; FT live (ungated) |
| `--live` | off | Both channels live |
| `--goals-mode` / `--ft-mode` | live / live | Per-channel override |
| `--no-trade` | off | Quote only (no executor) |
| `--take-depth top\|walk` | `walk` | Walk book vs best level only |
| `--max-levels` | 5 | Walk depth cap |
| `--max-usdc` / `--max-shares` | 1 / 25 | Hard caps; **`.env` `QUOTE_MAX_*` wins** |
| `--max-slippage` | 0.03 | Walk adverse price cap |
| `--min-buy-price` | 0.6 | leftover non-gate path only; **skipped** for pitch-gate and FT |
| `--allow-extreme-prices` | off | Allow ≤0.01 / >0.995 |
| `sell_lose` | off | Disabled — only `buy_win` |

## Cooperation

| Channel | Path | Purpose |
|---|---|---|
| Trigger | `data/bridge/events.jsonl` | `match_finished` / `score_change` |
| Join | `data/bridge/matches.json` | Polymarket handles + warmer input |
| Market cache | `data/pm-quote/market_cache/{match_id}.json` | Pre-warmed Gamma catalog |
| Quotes | `data/pm-quote/quotes.jsonl` | Full bundles |
| Latest | `data/pm-quote/latest.json` | Last bundle |
| Opportunities | `data/pm-quote/opportunities.jsonl` | `misprice=true` rows |
| Trades | `data/pm-quote/trades.jsonl` | Dry / live attempts |
| DQD stream observe | `data/pm-quote/dqd_stream_observe.jsonl` | Pitch-gate DOM samples (no JPEG) |
| Pitch Gate board | http://127.0.0.1:8791/ | Per-goal DOM + AF + Odds grade (System Main) |
| AF observe | `data/pm-quote/af_observe.jsonl` | AF score on the same 5s/120s gate clock |
| Odds observe | `data/pm-quote/book_context_observe.jsonl` | Bet365 grade A/B/C, observe only |
| Prematch odds | `data/pm-quote/prematch_odds.jsonl` | One shot at T-30m: Bet365+1xbet all markets |
| AF Bridge board | http://127.0.0.1:8792/ | DQD→AF fixture cache / events |
| Live Score observe | `data/pm-quote/livescore_observe.jsonl` | Optional LSA research |
| Open lots | `data/pm-quote/open_positions.json` | buy_win lots |
| Cursor | `data/pm-quote/cursor.json` | Processed keys / FT ids / offset |

## Related skills

- [`match-bridge`](../match-bridge/SKILL.md) — FT / score_change trigger
- [`apifootball-bridge`](../apifootball-bridge/SKILL.md) — DQD→AF fixture cache (AF observe)
- [`pitch-state`](../pitch-state/SKILL.md) — keyword tables reused by DOM `judge_dom` (quote no longer screenshots / OCR)
- [`polymarket-soccer`](../polymarket-soccer/SKILL.md) — Gamma helpers / fixture list
- [`trade-analytics`](../trade-analytics/SKILL.md) — historical trades / PnL

## Details

See [reference.md](reference.md).
