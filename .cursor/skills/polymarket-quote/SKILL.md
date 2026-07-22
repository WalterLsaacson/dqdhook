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

默认 **dry-run**（只写 `trades.jsonl`）；加 `--live` 才真正 post。

## Quick start

**主入口（推荐）** — System Main 会拉起 boards + `pm_quote watch`，并把交易参数一并传入（默认 dry-run，写 `trades.jsonl`）：

```bash
python3 frontend/run_main.py
python3 frontend/run_main.py --take-depth walk --max-usdc 5
python3 frontend/run_main.py --live --max-usdc 2          # 真下单
python3 frontend/run_main.py --no-trade                   # 仅询价
```

Hub：http://127.0.0.1:8790/ · quote 日志：`data/pm-quote/watch.log` · 成交尝试：`data/pm-quote/trades.jsonl`

也可用环境变量：`QUOTE_LIVE`、`QUOTE_TAKE_DEPTH`、`QUOTE_MAX_USDC`、`QUOTE_TRADE=0` 等（见 System Main）。

Skill 单跑（调试用）：

```bash
# Process new bridge FT events (cursor-aware); dry-run trade plans on misprice
python3 .cursor/skills/polymarket-quote/scripts/pm_quote.py once --from-bridge --json

# Walk deeper book levels when planning fills
python3 .cursor/skills/polymarket-quote/scripts/pm_quote.py once --from-bridge \
  --take-depth walk --max-usdc 10 --max-levels 5

# Quote only (no trade executor)
python3 .cursor/skills/polymarket-quote/scripts/pm_quote.py once --from-bridge --no-trade
```

Trading deps (once):

```bash
pip install -r .cursor/skills/polymarket-quote/requirements-trade.txt
```

Env (same names as simple_str): `PRIVATE_KEY`, `FUNDER`, `SIGNATURE_TYPE`, `CHAIN_ID`, `CLOB_HOST`. Load from repo `.env` or `--trade-env-file`. Never commit keys.

## Agent workflow

1. Prefer `watch` (or `frontend/run_stack.py`) so upstream skills are running.
2. Prefer bridge events in `data/bridge/events.jsonl`:
   - `score_change` — mid-match after a goal; quote **locked** outcomes only.
   - `match_finished` — full moneyline + props + exact settlement.
3. Join `data/bridge/matches.json` for full `market_refs` / `event_id`.
4. Read quote output from stdout `--json` or `data/pm-quote/latest.json`.
5. Treat `opportunities[]` as fee-aware edges (`net_edge ≥ 0.02` default).
6. On misprice, executor plans fills (`--take-depth top|walk`) and writes `trades.jsonl`; `--live` posts FOK/FAK via `py-clob-client-v2`.

## Trading flags

| Flag | Default | Meaning |
|---|---|---|
| (default) | dry-run | Plan + log only |
| `--live` | off | Real `create_and_post_market_order` |
| `--no-trade` | off | Quote only |
| `--take-depth top\|walk` | `top` | Best level vs walk book |
| `--max-levels` | 5 | Walk depth cap |
| `--max-usdc` / `--max-shares` | 5 / 25 | Size caps |
| `--max-slippage` | 0.03 | Walk adverse price cap |
| `--allow-extreme-prices` | off | Allow ≤0.01 / ≥0.99 |

## Cooperation

| Channel | Path | Purpose |
|---|---|---|
| Trigger | `data/bridge/events.jsonl` | `match_finished` / `score_change` |
| Join | `data/bridge/matches.json` | Full Polymarket handles |
| Quotes | `data/pm-quote/quotes.jsonl` | Full bundles (append) |
| Latest | `data/pm-quote/latest.json` | Last bundle |
| Opportunities | `data/pm-quote/opportunities.jsonl` | `misprice=true` rows |
| Trades | `data/pm-quote/trades.jsonl` | Dry/live attempts (plan + response) |
| Cursor | `data/pm-quote/cursor.json` | Processed event keys |

## Coverage

| Family | Source | Notes |
|---|---|---|
| Moneyline ×6 | Main event | Always attempted |
| Spreads / Totals / BTTS | `{slug}-more-markets` | Skipped if not listed |
| Exact score | Exact Score sibling event | Skipped if not listed |

## Related skills

- [`match-bridge`](../match-bridge/SKILL.md) — FT / score_change trigger
- [`polymarket-soccer`](../polymarket-soccer/SKILL.md) — Gamma proxy helpers / fixture list

## Details

See [reference.md](reference.md).
