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

**进球 AF 门控（默认）**：`score_change` 进球后 **先**用 apifootball-bridge 异步确认（与 `events` CLI 同路径）。Poll：**每 2s → 90s 截止**（首看 2s；全 worker 共享 `QUOTE_AF_MIN_INTERVAL_S` 默认 1s；HTTP timeout 受剩余 deadline 约束）。缺 fixture 映射 → **立即跳过**（gate 不 `wait_cache`）。AF 确认 → 才询价/下单；AF 超时/映射失败 → **忽略该球、不下单**；懂球帝回撤只启动 Odds 仲裁，不直接 flatten。

**终场 AF 门控**：`match_finished` **同样先等 AF**，但读的是 fixture **`score.fulltime`（正赛 90'+补时）**——**加时 / 点球不作数**（与 Polymarket soccer 结算一致）。DQD 终场比分须与 AF 正赛比分**完全一致**才下单；不一致（如 VAR 改判）或超时 → **不下单**。另：事件超过 **`QUOTE_FT_MAX_AGE_S`（默认 900s）** 或同 `match_id` 已处理过终场 → 跳过（防重启重放）。

- AF 确认目标比分（或已覆盖该次上涨）→ `_quote_one` 下单
- 超时（默认 **90s**）→ `mode=af_unconfirmed`，**不 flatten**
- 确认 poll **不**额外写 burst；成功后只异步保存 confirmed score
- 429 / SSL 等瞬态错误退避重试（不烧 poll 槽）
- 懂球帝 **回撤** → 取消 pending AF，随后在 5/10/15/20/25/30s 拉 Odds；仅当硬身份验证后的 Odds 比分也下降，或 Bet365 重新出现按回撤前比分不可能的 Correct Score / 主 Totals 盘口时 flatten；30s 无佐证则持仓不动
- 终场确认失败 → `mode=af_ft_unconfirmed` / `ft_stale`（无 flatten）

激进模式（先买后 AF）用 `--af-postcheck-trade` / `QUOTE_AF_POSTCHECK_TRADE=1`；`--no-af-referee` 完全不管 AF。默认 **dry-run**；`--live` / `--goals-mode` / `--ft-mode` 控制真下单。

人工/其它 skill 仍可：

```bash
python3 .cursor/skills/apifootball-bridge/scripts/af_bridge.py events --match-id <DQD_ID> --json
```

## Quick start

**主入口（推荐）** — System Main 会拉起全部 boards（含 AF 验证板 :8791）、启动 apifootball-bridge watch，并跑 `pm_quote watch`（默认 AF referee + dry-run 交易）：

```bash
python3 frontend/run_main.py
python3 frontend/run_main.py --take-depth walk --max-usdc 5
python3 frontend/run_main.py --live --max-usdc 2          # 进球+终场真下单
python3 frontend/run_main.py --goals-mode dry --ft-mode live --max-usdc 1   # 进球模拟 / 终场真买
python3 frontend/run_main.py --goals-mode live --ft-mode dry --max-usdc 1   # 反过来
python3 frontend/run_main.py --no-trade                   # 仅询价
```

Hub：http://127.0.0.1:8790/ · quote 日志：`data/pm-quote/watch.log` · 成交尝试：`data/pm-quote/trades.jsonl`

也可用环境变量：`QUOTE_LIVE`、`QUOTE_GOALS_MODE`、`QUOTE_FT_MODE`、`QUOTE_TAKE_DEPTH`、`QUOTE_MAX_USDC`、`QUOTE_TRADE=0` 等（见 System Main）。

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

1. Prefer System Main (`frontend/run_main.py`): boots boards (UI) + AF watch + `pm_quote watch`, which owns **in-process** match-bridge (memory `event_queue` → AF referee → quote/trade). File writes are async. `MAIN_BRIDGE_INPROC=0` falls back to bridge-board file wake. Do not start boards as skill hosts separately.
2. Prefer bridge events in `data/bridge/events.jsonl`:
    - `score_change` — mid-match after a goal; Odds/Bet365 starts immediately (B live); **A waits for AF confirm**.
   - `match_finished` — AF **regulation** fulltime gate (`score.fulltime`; ET/pen ignored), then moneyline + props + exact; stale / once-per-match skip.
3. Join `data/bridge/matches.json` for full `market_refs` / `event_id`.
4. **AF referee (gate default)**: on goal-up, **async** poll AF events (**every 2s from 2s until timeout**, default 90s) **in parallel with Odds/Bet365**. Concurrent confirms share one AF client paced by `QUOTE_AF_MIN_INTERVAL_S` (default 1s). `cache_only`; gate uses `wait_cache=False` (miss → skip). Bet365 core-clean can live-buy **B ($10)** before AF; **A ($20)** only after AF confirms the same score. AF timeout/miss → no A upgrade (existing B lots stay, no flatten). DQD score reversals cancel pending AF jobs and open the 5s × 6 Odds rollback arbitration window; DQD alone never flattens. Aggressive `--af-postcheck-trade`: buy on DQD immediately, flatten on AF timeout. Live quoting uses **one** CLOB `/books` POST then totals/BTTS before exact.
5. **Latency path**: watch wakes on `events.jsonl` mtime/size (poll ~50ms; `--interval` / `QUOTE_INTERVAL` default **0.25s** max idle). After bridge match, a warmer fills `data/pm-quote/market_cache/{match_id}.json` (Gamma catalog only — not live prices). Live quote settles from cache, then CLOB books via urllib; **totals/BTTS trade first**, then exact. Finished-match cache survives the AF FT wait and drops after the FT quote is consumed.
6. Read quote output from stdout `--json` or `data/pm-quote/latest.json`.
7. Treat `opportunities[]` as fee-aware edges (`net_edge ≥ 0.0076` default ≈ ask≤0.992).
8. On misprice, executor plans fills (`--take-depth top|walk`) and writes `trades.jsonl`; `--live` or per-signal `--goals-mode`/`--ft-mode` posts market **FAK** via `py-clob-client-v2`. **A/B remainder** (after FAK, or when WIN has no ask) rests **GTD** bids at **0.99 then 0.98** toward `$10`/`$20`. C never rests. DQD reversal / FT / a token that is no longer WIN **cancels those bids immediately** (does not wait for Odds flatten).
9. **CLOB `delayed`**: treat as accepted fill — register open lot immediately (poll balance briefly) so later FT flatten can fire.
10. **Post-goal samples (data only)**: when `score_change` produces a successful `buy_win` (dry or live), write that quote as sample 0 and background-requote the same tokens at +10s…+50s (6 total) into `post_goal_samples.jsonl` — no extra `maybe_trade`. Jobs run in **parallel**; follow-ups re-read score and recompute settlement.
11. **No post-confirm DQD/AF research polling**: AF is queried only for the required second confirmation. After confirmation, the old DQD overview + AF fixture/list sampling is disabled; `goal_context_observe.jsonl` is historical data only.
12. **Live Score API observe (trial, data only)**: when `LIVESCORE_API_KEY` + `LIVESCORE_API_SECRET` are set, same phases also pull LSA live resolve + raw `matches/events` + raw `commentary/events` into `livescore_observe.jsonl` (trial: score/VAR focus, not `KICK_OFF`). Does **not** gate buys or flatten.
13. **Odds third confirmation + graded sizing**: with `ODDS_API_IO_KEY`, a DQD goal-up starts Odds-API.io `/events/{id}` score plus Bet365+1xbet `/odds/multi` polling at `0/3/…/90s` **immediately** (does not wait for AF). Concurrent pulls coalesce, ≤10 events = 1 request. 1xbet is persisted on the same observe/raw rows (`observe_only`, same CS/Totals core-clean fields) and **never** grades A/B; missing 1xbet does not veto. **Core-clean Bet365** means the book is open, at least one of **Correct Score** or the main **`Totals`** market is inspectable, and those gate markets have no already-impossible offers. Alternative Goal Line / team totals / BTTS / clean sheet / corners are logged but do **not** veto B/A. C is record-only (dry-run into `trades.jsonl`, no CLOB, no open lot, no USDC notional). B live-buys toward `$10` whenever the core book is clean (score match not required) **even before AF**; **soft identity** (cached mapping + fuzzy teams, or odds multi ok with a brief event GET failure) is enough for B. A live-buys toward `$20` only when AF has confirmed **and** the core book is clean **and** the provider score exactly matches AF/DQD **and** hard identity is verified (teams + kickoff ±12h + non-terminal + orientation). A raw-A Odds sample is capped to B until that AF confirm. Soft identity never sizes `$10`. A matching score with missing/stale Bet365 (e.g. only Spread, or `Bet365 (no latency)` with no CS/Totals) stays C. Hard mismatch that is clearly the wrong fixture clears the cached id; a soft-ok poll does not. A/B upgrades buy only the per-token difference toward `$10`/`$20` (C does not count toward already_usdc); live ledger sizing uses actual matched `makingAmount`/`takingAmount`, and failed/partial upgrades retry while the same goal generation remains live. B upgrades skip Exact Score; A still quotes totals/BTTS first, then exact, and skips exact rows whose book `tick_size` is `0.001`. Live CLOB posts run off the executor lock with default `QUOTE_TRADE_WORKERS=4`. Grades never downgrade/sell, and same-grade data changes are logged without creating a second upgrade edge. The football events catalog is shared for 60s with per-generation mapping misses; 429 activates shared `Retry-After`/ISO-or-epoch reset backoff. DQD reversal cancels remaining upgrades, then polls Odds at 5/10/15/20/25/30s; a hard-verified Odds score rollback or reappearing Bet365 gate-impossible offer emits one flatten decision and cancels the remaining timers. Every raw response gets a collision-resistant filename and stays in `book_context_raw/`; parsed grade, reason, fingerprint/change, compact request metadata and upgrade/reversal decision are persisted in `book_context_observe.jsonl`. Other aggregators are not fetched.

## Trading flags

| Flag | Default | Meaning |
|---|---|---|
| (default) | both dry | Plan + log only |
| `--live` | off | Both `score_change` and `match_finished` post real orders |
| `--goals-mode dry\|live` | dry | Per-signal mode for进球 (`score_change`); overrides `--live` for that channel |
| `--ft-mode dry\|live` | dry | Per-signal mode for终场 (`match_finished`); overrides `--live` for that channel |
| `--no-trade` | off | Quote only |
| `--no-af-referee` | off | Skip AF entirely (DQD-only buys; reversals still require Odds arbitration) |
| `--af-postcheck-trade` | off | Aggressive: buy on DQD goal, flatten on AF timeout |
| `--af-gate-before-trade` | off | Explicit gate (same as default) |
| `--af-poll` | (off) | If set, fixed poll interval; otherwise **2s → every 2s → timeout** |
| `--af-timeout` | 90 | AF confirm deadline (gate: ignore goal; postcheck: flatten pending) |
| `--take-depth top\|walk` | `walk` | Walk book vs best level only |
| `--max-levels` | 5 | Walk depth cap |
| `--max-usdc` / `--max-shares` | 1 / 25 | Hard caps; **`.env` `QUOTE_MAX_*` wins**; per-ask tiers scale both together |
| `QUOTE_SIZE_TIERS` | `0.98:1` | ask≥threshold → usdc; below → `QUOTE_MAX_USDC` ($1) |
| `QUOTE_MAX_OPEN_USDC` | 1000 | Sum of open lot `usdc` budget (effectively open) |
| `QUOTE_TRADE_WORKERS` | 4 | Parallel live FAK posts per quote phase (CLOB HTTP is off the executor lock) |
| Rest GTD (A/B) | 0.99 + 0.98 | After FAK / empty asks, remainder toward `$10`/`$20`; `QUOTE_REST_EXPIRE_S` default 3600 (1h; 0=GTC). Cancel on DQD reversal immediately |
| `QUOTE_SIZE_FLOOR_USDC` | 1 | Skip buy if effective usdc below floor |
| `--max-slippage` | 0.03 | Walk adverse price cap |
| `--min-buy-price` | 0.6 | `buy_win` only when `best_ask ≥` this; `0` = off; below → skip + still write `trades.jsonl` |
| `--allow-extreme-prices` | off | Allow ≤0.01 / >0.992 |
| `sell_lose` | off | Disabled at source — `LOSE` tokens skipped before `/books`; only `buy_win` |

Mixed example: `--goals-mode dry --ft-mode live` simulates goal fills while posting FT fills. Flatten always uses the lot’s own `live` flag so a dry goal lot is never live-sold.

## Cooperation

| Channel | Path | Purpose |
|---|---|---|
| Trigger | `data/bridge/events.jsonl` | `match_finished` / `score_change` (watch wake) |
| Join | `data/bridge/matches.json` | Full Polymarket handles + warmer input |
| Market cache | `data/pm-quote/market_cache/{match_id}.json` | Pre-warmed Gamma main / more / exact |
| Quotes | `data/pm-quote/quotes.jsonl` | Full bundles (append; rolling prune) |
| Latest | `data/pm-quote/latest.json` | Last bundle |
| Opportunities | `data/pm-quote/opportunities.jsonl` | `misprice=true` rows |
| Trades | `data/pm-quote/trades.jsonl` | Dry/live attempts + flatten |
| AF confirmed scores | `data/pm-quote/af_confirmed_scores.json` | Last AF-confirmed score per DQD match_id |
| Post-goal samples | `data/pm-quote/post_goal_samples.jsonl` | After buy_win on score_change: books at 0/10/20/30/40/50s (no trade) |
| Historical goal-context data | `data/pm-quote/goal_context_observe.jsonl` | No longer written; retained only for prior false-goal analysis |
| Live Score observe | `data/pm-quote/livescore_observe.jsonl` | Same phases: LSA events + commentary raw (trial; needs API key/secret) |
| Odds confirmation | `data/pm-quote/book_context_observe.jsonl` | DQD goal-up at 0/3/…/90s (B live; A after AF): Odds-API.io score + Bet365 gate + 1xbet observe-only, A/B/C reason, change fingerprint and upgrade action |
| Book-context raw | `data/pm-quote/book_context_raw/*.json` | Full HTTP bodies per request (quota-precious; not auto-pruned) |
| Open lots | `data/pm-quote/open_positions.json` | buy_win lots awaiting flatten |
| Cursor | `data/pm-quote/cursor.json` | Processed event keys / FT ids / byte offset; atomically written only on change |

## Coverage

| Family | Source | Notes |
|---|---|---|
| Moneyline ×6 | Main event | Always attempted |
| Spreads / Totals / BTTS | `{slug}-more-markets` | Skipped if not listed |
| Exact score | Exact Score sibling event | Skipped if not listed |

## Retention / prune

Rolling **24h** window (not calendar midnight): `watch` prunes on start and every ~10m.

- Drop `market_cache/{match_id}.json` after successful FT consumption or when orphaned; retain unconsumed finished matches during the AF FT gate, including restart cases where the FT event remains in `events.jsonl` but the match has disappeared from `matches.json` (skip prune if matches snapshot empty/missing)
- Truncate `quotes` / `opportunities` / `trades` / `post_goal_samples` / `bridge/events` by timestamp; **unprocessed** bridge events are always kept
- **Not pruned** (kept indefinitely): `book_context_observe.jsonl`, `goal_context_observe.jsonl`, `livescore_observe.jsonl`, `book_context_raw/`
- Append/prune use `{path}.lock` so rewrite cannot race bridge/quote writers

```bash
python3 .cursor/skills/polymarket-quote/scripts/pm_quote.py prune --retain-hours 24
```

## Related skills

- [`match-bridge`](../match-bridge/SKILL.md) — FT / score_change trigger
- [`apifootball-bridge`](../apifootball-bridge/SKILL.md) — AF fixture cache + events for goal referee
- [`polymarket-soccer`](../polymarket-soccer/SKILL.md) — Gamma proxy helpers / fixture list
- [`trade-analytics`](../trade-analytics/SKILL.md) — historical trades / overnight PnL from `trades.jsonl`

## Details

See [reference.md](reference.md).
