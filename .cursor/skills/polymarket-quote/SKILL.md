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

**当前策略（两阳门控）**：DQD `score_change` 进球且已配对 → 进球后 **+5s** 起每 **5s** 抓一帧，直到 **2.5 分钟超时**（或回撤取消）；需 **进攻/控球等 + 底部比分 OCR=期望**，且 **连续 2 帧** `in_play` 才 **一刀** `_quote_one`（之后继续抓帧不再下单）。比分未更新 / 不一致 → 不买。截图过程中出现 **VAR** → **该球永久不下单**（继续抓帧，结束 `pitch_gate_var_veto`）。限价 rest 默认关。终场立刻询价。

> 动画已改比分、随后 DQD 才回撤的延迟回撤，比分门控拦不住；连续 2 帧只能挡住部分快回撤（约一个采样间隔内）。VAR 若出现在已下单之后则拦不住该刀。

- 懂球帝 **回撤**：取消相关 rest 挂单 + **取消该场 pitch-gate**，并 **撤销尚未 drain 的 `in_play` 买信号**（`buy_revoked`）；**不自动 flatten**。询价 tick **先处理事件再 drain 门控**，避免同 tick 回撤输掉竞态。
- 事件超过 **`QUOTE_FT_MAX_AGE_S`（默认 900s）** → 跳过（防重启重放）
- 同 `match_id` 已处理过终场 → 跳过
- 门控路径需 `QUOTE_DQD_STREAM_OBSERVE=1` 且 `QUOTE_PITCH_STATE=1`（缺则 `pitch_gate_unavailable`，该球不下单）
- Pitch-gate 限价 rest：需 **`QUOTE_REST_ENABLED=1`** → @**0.99** / **≥5 shares** / **`QUOTE_REST_EXPIRE_S`（默认 3600）**

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
   - `score_change` goal-up (paired) → **start pitch-gate** (first frame @+5s, then every 5s until 150s); quote on first `in_play`, keep capturing
   - `score_change` reversal → cancel rest + cancel pitch-gate; no auto-flatten
   - `match_finished` → immediate quote (default live; stale / once-per-match skip)
3. Join `data/bridge/matches.json` for full `market_refs` / `event_id`.
4. **Latency path**: wake on events (poll ~50ms; `--interval` default **0.25s**). Market warmer fills `data/pm-quote/market_cache/{match_id}.json`. Live quote: one CLOB `/books` POST; totals/BTTS before exact.
5. On misprice after pitch-gate, executor plans fills → `trades.jsonl` (`dry_run` or live `posted`). Pitch-gate buys skip `min_buy_price` and size/$1 floors (fee/`min_net` + `QUOTE_MAX_USDC` remain).
6. **Post-goal samples**: successful `buy_win` rows still schedule 0/10…/50s book samples (data only).

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
| `--min-buy-price` | 0.6 | `buy_win` when `best_ask ≥` this; **skipped** for pitch-gate |
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
| Trades | `data/pm-quote/trades.jsonl` | Dry / live attempts |
| Post-goal samples | `data/pm-quote/post_goal_samples.jsonl` | Books at 0/10/…/50s after buy_win |
| DQD stream observe | `data/pm-quote/dqd_stream_observe.jsonl` | Pitch-gate (+ research) frame meta |
| DQD stream frames | `data/pm-quote/dqd_stream_frames/` | JPEG frames |
| Pitch-state judge | `data/pm-quote/pitch_state_judge.jsonl` | Per-frame resume-play verdict |
| Pitch Gate board | http://127.0.0.1:8791/ | Per-goal frames + judgments (System Main) |
| Live Score observe | `data/pm-quote/livescore_observe.jsonl` | Optional LSA research |
| Open lots | `data/pm-quote/open_positions.json` | buy_win lots |
| Cursor | `data/pm-quote/cursor.json` | Processed keys / FT ids / offset |

## Related skills

- [`match-bridge`](../match-bridge/SKILL.md) — FT / score_change trigger
- [`pitch-state`](../pitch-state/SKILL.md) — screenshot resume-play judge (gate)
- [`polymarket-soccer`](../polymarket-soccer/SKILL.md) — Gamma helpers / fixture list
- [`trade-analytics`](../trade-analytics/SKILL.md) — historical trades / PnL

## Details

See [reference.md](reference.md).
