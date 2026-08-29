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

**当前会动 CLOB 的策略（只买 `buy_win`）：**

| 策略 | 触发 | 门控 | 金额 | Rest |
|---|---|---|---|---|
| Pitch-gate | 已配对进球，同帧 DOM `in_play` ∧ AF `score_match` | 是 | `QUOTE_GOAL_MAX_USDC` | 仅 `QUOTE_REST_ENABLED=1` |
| Locked sweep | 门控买时 token 在上一分已是 live WIN | 同一刀 | `QUOTE_LOCKED_SWEEP_USDC` | 同门控 |
| T+10 | 进球后默认 600s，按当时比分 | 否 | `QUOTE_T10_USDC`（FAK 与 0.99 GTC **各**一份） | **始终挂**，每已锁 WIN token 一笔 |
| 终场 | `match_finished` | 否 | `QUOTE_FT_MAX_USDC` | 不挂 |
| 终场灰尘盘 | 终场锁定 WIN、ask≤0.01 | 否 | `QUOTE_FT_DUST_USDC` | 不挂 |

**Pitch-gate 细节**：DQD `score_change` 进球且已配对 → 进球后 **+0s** 起每 **5s 先采 DOM**。**本拍 DOM `in_play` 才同帧打 AF**（庆祝/`unclear` 不打）。买入只有一条：**同帧 DOM `in_play` ∧ AF `ok && score_match`**（或本球更早一次 `in_play` 已认分的锁存；本拍硬不一致会清掉）。**射门不再卡买入**。Odds Grade A **只观察、不下单**。VAR 买入前仍永久否决。买入后立刻停 AF，DOM 抓到 120s。回撤确认轨仍是 5s AF+DOM 观察，**仅 AF `score_match` 才 flatten**（DOM 比分条不单独卖出）。AF∨DOM **或门买入否决、不实现**（见 `design-af-dom-or-gate.md`）。终场立刻询价。

> 询价、挂 rest、flatten、rest 对账在 **CLOB worker 线程**；watch tick 只 `start_gate` / 取消门控 / 把事件载荷入队。别场的 `/books` 和 GTC 不再堵住新球开 DOM。**`start_gate` 后并行预热** Gamma catalog + 周期 `POST /books`（`QUOTE_GATE_PREWARM`，默认开）；BUY 询价优先吃新鲜预热盘口，省掉热路径上约 0.5–1s 的 books RTT（**不缩短** DOM/AF 等待）。

> 动画已改比分、随后 DQD 才回撤的**延迟回撤**在买入时刻无法预知；出口靠懂球帝回撤后再开 5s 轨，**等 AF 认回撤后比分**。**AF 认分 flatten 后立刻停 AF+DOM**（买入后则仍抓 DOM 到原超时，便于事后看 VAR/庆祝，不再打 AF）。

- 懂球帝 **回撤**：立刻取消未完成进球门控，并按 **event_key** 撤销已入队的询价（不按 `match_id` 永久拉黑）。rest 取消入队优先级高于 idle flatten/rest 对账，对账不挡 `rest_cancel`。**进程内 bridge 入队回撤时就会 `cancel_match`**。询价 tick **先扫回撤再 `start_gate`**。回撤 ts 挡住该进球 stem（更早或相同 ts）。非开场的重判进球（更晚 ts）仍可开；**开场球**（0-0→1-0 / 0-0→0-1）若同一过渡刚被撤过，或时钟 ≥90′，不再开闸（`pitch_gate_reversal_risk_skip`）。询价 tick **先处理事件再 drain**。若该场 **已有仓**，再开 5s AF+DOM（期望=回撤后比分）；某一拍 **AF `ok && score_match`** → flatten **仅不再 WIN 的仓**（**不受** `QUOTE_GATE_PROTECT_S` 窗限制）并 **立刻停 5s 轨**。DOM 中心比分（含僵死页）**不单独 flatten**。懂球帝回撤本身不立刻平仓；窗只约束未确认的 DQD 路径。AF 不认直到 120s → **持仓**。未买入的回撤只取消门控。
- 事件超过 **`QUOTE_FT_MAX_AGE_S`（默认 900s）** → 跳过（防重启重放）
- 同 `match_id` 已处理过终场 → 跳过
- 门控路径需 `QUOTE_DQD_STREAM_OBSERVE=1`（缺则 `pitch_gate_unavailable`，该球不下单）
- **判定源是动画 DOM，不截图 / 不跑 OCR**：共用一台 Chromium，进行中已配对场预开 tracker 页；同场后续进球复用标签。每次采样读 `.pop-box` 与 `.center-box`。无 JPEG、无 `QUOTE_GATE_REF_SCREENSHOT`。标签上限 `QUOTE_DOM_POOL_MAX`（默认 24）；预热 `QUOTE_DOM_WARM`（默认开）/`QUOTE_DOM_WARM_INTERVAL_S`（默认 10s）/`QUOTE_DOM_WARM_OPEN_TIMEOUT_S`（默认 3s）。开页等待动画时会穿插处理其它场的 DOM 读。
- **防僵死**：判定要求 `.center-box` 时钟相对上一次读数有推进；时钟没走 → `unclear`（`stale_page`），不下单。
- **页面是纳米 tracker**：`animation_live` URL 打开 `tracker.namitiyu.com` 读 DOM，不是懂球帝比赛页。不做 MQTT 球位观察。
- Pitch-gate 限价 rest：需 **`QUOTE_REST_ENABLED=1`** → 目标 @**0.995**，但 **不信** Gamma/book 的 `0.001` 元数据；CLOB 足球最小 tick 是 **0.01**，rest 一律按 0.01 **向下收到 0.99**。**`QUOTE_REST_USDC`（默认 $5）** / **`GTC` 一直挂着**（回撤、终场、手取消才撤；`QUOTE_REST_EXPIRE_S>0` 才改回 GTD）。门控 rest **不受** `QUOTE_MAX_OPEN_USDC` 限制。没有卖盘（一边倒买盘）也挂，等砸盘。FAK / misprice 上限 **ask≤0.995**（fee 后 `min_net≈0.00475`）。**进球作废仍 WIN：** 比分变化后若该 token 在 **上一分**（`prev`）已经是 live WIN（当下这球不算也锁死），pitch-gate **FAK 吃光** ask≤0.995 的剩余卖盘，**不走** `QUOTE_GOAL_MAX_USDC` $50。金额顶 **`QUOTE_LOCKED_SWEEP_USDC`（默认 $1000）**，仍受 `QUOTE_MAX_OPEN_USDC` 剩余额度。开关 **`QUOTE_LOCKED_SWEEP`**（默认开；`0` 或金额 `0` 关闭）。回撤后若仓位在新比分仍是 WIN（如 1-1→1-0 的主队 0.5 大球），**不平仓**。终场已锁定 WIN 且 ask≤0.01：**仍 FAK**（`QUOTE_FT_DUST_FAK` 默认开），金额 **`QUOTE_FT_DUST_USDC`（默认 $100）**，独立于 `QUOTE_FT_MAX_USDC`，仍受 `QUOTE_MAX_OPEN_USDC` 剩余额度限制；max_price=0.01，不按 0.001 墙走单。
- Odds/Bet365：跟 DOM 同一拍后台写入 `book_context_observe.jsonl`（Grade A/B/C）。**Grade A 不下单**，AND 路径不因 Odds HTTP 阻塞。已配对场在距开球 **30 分钟**时 **采一次** Bet365+1xbet 全盘口，写入 `data/pm-quote/prematch_odds.jsonl`。
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
  - `score_change` goal-up (paired) → **start pitch-gate** (DOM @+0s every 5s; AF on each `in_play` tick; quote on AND; stop AF only after AND buy, keep DOM until 120s) and **schedule T+10** rescan
  - `score_change` reversal → first cancel/block the undone goal, then cancel rest + pitch-gate; if lots are open, 5s AF trail → flatten on first AF `score_match` vs post-reverse score **only lots that are no longer WIN** (e.g. 1-1→1-0 keeps home O/U 0.5), then stop the trail. T+10 is **not** canceled (uses score at fire time).
  - `match_finished` → cancel pending T+10 + rest; immediate quote (default live; stale / once-per-match skip)
3. Join `data/bridge/matches.json` for full `market_refs` / `event_id`.
4. **Latency path**: wake on events (poll ~50ms; `--interval` default **0.25s**). Market warmer fills `data/pm-quote/market_cache/{match_id}.json`. Live quote: CLOB worker thread (not the watch tick) runs one `/books` POST; totals/BTTS before exact.
5. On misprice after pitch-gate, executor plans fills → `trades.jsonl` (`dry_run` or live `posted`). Pitch-gate and FT buys skip `min_buy_price`; pitch-gate also skips size/$1 floors (fee/`min_net` + per-channel `QUOTE_GOAL_MAX_USDC` / `QUOTE_FT_MAX_USDC` remain). Tokens that are WIN **even at the previous score** (`win_if_goal_void`) FAK remaining asks ≤0.995 up to `QUOTE_LOCKED_SWEEP_USDC` (default $1000) instead of the $50 goal cap.
6. **Pitch-gate: one `quote_bridge_event` per aligned buy.** Separately, each paired goal schedules a **T+10** rescan (`QUOTE_T10_USDC`; delay `QUOTE_T10_DELAY_S` default 600s) from the score at fire time. FAK uses the same fee/`min_net`/0.995 path; rest @0.99 uses the same USDC var (does not need `QUOTE_REST_ENABLED`). Locked sweep does **not** apply. Skip / cancel if the match is already FT.

## Trading flags

| Flag | Default | Meaning |
|---|---|---|
| (default) | goals **live** / ft **live** | Pitch-gate live buys; FT live (ungated) |
| `--live` | off | Both channels live |
| `--goals-mode` / `--ft-mode` | live / live | Per-channel override |
| `--no-trade` | off | Quote only (no executor) |
| `--take-depth top\|walk` | `walk` | Walk book vs best level only |
| `--max-levels` | 5 | Walk depth cap |
| `--max-usdc` / `--max-shares` | 1 / 25 | Shared fallback; **`.env` `QUOTE_GOAL_*` / `QUOTE_FT_*` win** |
| `--max-slippage` | 0.03 | Walk adverse price cap |
| `--min-buy-price` | 0.6 | leftover non-gate path only; **skipped** for pitch-gate and FT |
| `--allow-extreme-prices` | off | Allow ≤0.01 / >0.995 (goals still skip ≤0.01 unless this flag). FT locked WIN ≤0.01 uses `QUOTE_FT_DUST_FAK` (default on) sized by `QUOTE_FT_DUST_USDC` (default $100) |
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
| Odds observe | `data/pm-quote/book_context_observe.jsonl` | Bet365 grade A/B/C (observe-only; does not buy) |
| Prematch odds | `data/pm-quote/prematch_odds.jsonl` | One shot at T-30m: Bet365+1xbet all markets |
| AF Bridge board | http://127.0.0.1:8792/ | DQD→AF fixture cache / events |
| Live Score observe | `data/pm-quote/livescore_observe.jsonl` | Optional LSA research |
| Open lots | `data/pm-quote/open_positions.json` | buy_win lots |
| T+10 pending | `data/pm-quote/t10_pending.json` | Goal +10min rescan jobs |
| Cursor | `data/pm-quote/cursor.json` | Processed keys / FT ids / offset |

## Related skills

- [`match-bridge`](../match-bridge/SKILL.md) — FT / score_change trigger
- [`apifootball-bridge`](../apifootball-bridge/SKILL.md) — DQD→AF fixture cache (AF observe)
- [`pitch-state`](../pitch-state/SKILL.md) — keyword tables reused by DOM `judge_dom` (quote no longer screenshots / OCR)
- [`polymarket-soccer`](../polymarket-soccer/SKILL.md) — Gamma helpers / fixture list
- [`trade-analytics`](../trade-analytics/SKILL.md) — historical trades / PnL

## Details

See [reference.md](reference.md).
