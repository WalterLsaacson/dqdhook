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

**当前策略（过渡）**：已拆除 AF 确认与 Odds/Bet365 三阳门控。`score_change` / `match_finished` 在 DQD 事件到达后 **立即 dry 询价**（可写 `trades.jsonl`）。**真下单（live CLOB）暂停**，等下一轮「DQD 进球 + 截图确认」两阳门控接上后再开。

- 懂球帝 **回撤**：取消相关 rest 挂单，**不自动 flatten**（下一轮再定）
- 事件超过 **`QUOTE_FT_MAX_AGE_S`（默认 900s）** → 跳过（防重启重放）
- 同 `match_id` 已处理过终场 → 跳过

截图观察（**不门控买卖**，为下一轮两阳准备）：

- `QUOTE_DQD_STREAM_OBSERVE=1` → 进球后截帧
- `QUOTE_PITCH_STATE=1` → 对截帧做 pitch-state 判读

## Quick start

**主入口（推荐）** — System Main 拉起 boards + `pm_quote watch`（强制 dry）：

```bash
python3 frontend/run_main.py
python3 frontend/run_main.py --take-depth walk --max-usdc 5
python3 frontend/run_main.py --no-trade                   # 仅询价（不开 executor）
```

Hub：http://127.0.0.1:8790/ · quote 日志：`data/pm-quote/watch.log` · 成交尝试：`data/pm-quote/trades.jsonl`

`--live` / `--goals-mode live` / `--ft-mode live` / `QUOTE_LIVE` **本轮无效**（强制 dry）。

Skill 单跑（调试用）：

```bash
python3 .cursor/skills/polymarket-quote/scripts/pm_quote.py once --from-bridge --json
python3 .cursor/skills/polymarket-quote/scripts/pm_quote.py once --from-bridge \
  --take-depth walk --max-usdc 10 --max-levels 5
python3 .cursor/skills/polymarket-quote/scripts/pm_quote.py once --from-bridge --no-trade
```

Trading deps (once):

```bash
pip install -r .cursor/skills/polymarket-quote/requirements-trade.txt
```

Env (same names as simple_str): `PRIVATE_KEY`, `FUNDER`, `SIGNATURE_TYPE`, `CHAIN_ID`, `CLOB_HOST`. Load from repo `.env` or `--trade-env-file`. Never commit keys.

## Agent workflow

1. Prefer System Main (`frontend/run_main.py`): boards (UI) + `pm_quote watch` owns **in-process** match-bridge (memory `event_queue` → dry quote). `MAIN_BRIDGE_INPROC=0` falls back to bridge-board file wake.
2. Bridge events in `data/bridge/events.jsonl`:
   - `score_change` goal-up → dry quote (+ optional DQD stream / pitch-state observe)
   - `score_change` reversal → cancel rest; no auto-flatten
   - `match_finished` → dry quote (stale / once-per-match skip)
3. Join `data/bridge/matches.json` for full `market_refs` / `event_id`.
4. **Latency path**: wake on events (poll ~50ms; `--interval` default **0.25s**). Market warmer fills `data/pm-quote/market_cache/{match_id}.json`. Live quote: one CLOB `/books` POST; totals/BTTS before exact.
5. On misprice, executor plans fills and writes `trades.jsonl` as **dry_run** only this round.
6. **Post-goal samples**: successful `buy_win` dry rows still schedule 0/10…/50s book samples (data only).
7. **DQD stream / pitch-state**: observe-only; do **not** gate buys yet.

## Trading flags

| Flag | Default | Meaning |
|---|---|---|
| (default) | both dry | Plan + log only; live forced off |
| `--live` / `--goals-mode` / `--ft-mode` | ignored | Live paused pending screenshot gate |
| `--no-trade` | off | Quote only (no executor) |
| `--take-depth top\|walk` | `walk` | Walk book vs best level only |
| `--max-levels` | 5 | Walk depth cap |
| `--max-usdc` / `--max-shares` | 1 / 25 | Hard caps; **`.env` `QUOTE_MAX_*` wins** |
| `--max-slippage` | 0.03 | Walk adverse price cap |
| `--min-buy-price` | 0.6 | `buy_win` only when `best_ask ≥` this; `0` = off |
| `--allow-extreme-prices` | off | Allow ≤0.01 / >0.992 |
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
| Trades | `data/pm-quote/trades.jsonl` | Dry attempts (+ historical live) |
| Post-goal samples | `data/pm-quote/post_goal_samples.jsonl` | Books at 0/10/…/50s after buy_win |
| DQD stream observe | `data/pm-quote/dqd_stream_observe.jsonl` | Goal screenshots metadata |
| DQD stream frames | `data/pm-quote/dqd_stream_frames/` | JPEG frames |
| Pitch-state judge | `data/pm-quote/pitch_state_judge.jsonl` | Per-frame resume-play verdict |
| Live Score observe | `data/pm-quote/livescore_observe.jsonl` | Optional LSA research |
| Open lots | `data/pm-quote/open_positions.json` | buy_win lots |
| Cursor | `data/pm-quote/cursor.json` | Processed keys / FT ids / offset |

## Related skills

- [`match-bridge`](../match-bridge/SKILL.md) — FT / score_change trigger
- [`pitch-state`](../pitch-state/SKILL.md) — screenshot resume-play judge (next-round gate)
- [`polymarket-soccer`](../polymarket-soccer/SKILL.md) — Gamma helpers / fixture list
- [`trade-analytics`](../trade-analytics/SKILL.md) — historical trades / PnL

## Details

See [reference.md](reference.md).
