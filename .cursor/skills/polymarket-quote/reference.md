# Polymarket Quote — Reference

## Pipeline

1. Trigger: `score_change` / `match_finished` from match-bridge (or CLI synthetic FT).
2. Join bridged row → `event_id` / `slug` / `market_refs`.
3. Gamma catalog: prefer `data/pm-quote/market_cache/{match_id}.json` (warmed after bridge match from `matches.json`). Complete only when `related_complete` and `main_event` are set (partial sibling warms are retried). Miss → fetch main + More Markets / Exact Score once and write cache. Cache holds **token definitions**, not CLOB prices. Dropped on FT.
4. Settle each token from `home_score` / `away_score` → `WIN` | `LOSE` | `PENDING`.
5. CLOB: urllib `POST /books` (batch ≤50) with `GET /book` fallback (same proxy as Gamma; no subprocess curl).
6. **Live phases**: (A) totals + BTTS books → misprice → `maybe_trade`; (B) exact score (+ remainder) books → trade. One merged bundle for analytics/UI.
7. Persist bundle + opportunities.

## Watch wake

`pm_quote watch` polls `data/bridge/events.jsonl` mtime/size (~50ms). `--interval` (default **0.25**) is the **max idle** between ticks; new events wake early. Each tick still runs `retry_pending_flattens` and drains unprocessed cursor keys. System Main: `QUOTE_INTERVAL` default `0.25`.

Background warmer (`market_cache.py`) syncs open paired fixtures every ~5s.

## Retention (rolling 24h)

`data_prune.py` (started with `pm_quote watch`; also `pm_quote prune`):

| Target | Rule |
|---|---|
| `market_cache/*.json` | Keep only **open** paired matches still in `matches.json`; drop finished / orphan. **Skip** if matches file missing / bad / empty. |
| `quotes.jsonl` / `opportunities.jsonl` / `trades.jsonl` / `post_goal_samples.jsonl` | Keep rows with `quoted_at`/`sampled_at`/`dqd_ts` ≥ now − retain_hours |
| `goal_context_observe.jsonl` / `livescore_observe.jsonl` / `book_context_observe.jsonl` / `book_context_raw/` | **Not pruned** (retained for multi-day book/false-goal analysis) |
| `data/bridge/events.jsonl` | Keep if `ts` ≥ cutoff **or** event key not yet in `cursor.processed_keys` |

Default `retain_hours=24` is a **rolling** cutoff (`datetime.now − 24h`), not “delete at local midnight”. Append + prune share `{file}.lock` (`fcntl`) so rewrite cannot drop concurrent appends. Unparseable timestamps are kept. Interval in watch: ~600s after an immediate first pass.

## Moneyline six tokens

| market_key | Meaning when home wins |
|---|---|
| `home_yes` / `home_no` | WIN / LOSE |
| `draw_yes` / `draw_no` | LOSE / WIN |
| `away_yes` / `away_no` | LOSE / WIN |

Outcomes are aligned via Gamma `outcomes[]` ↔ `clobTokenIds[]` (Yes/No).

## Totals settlement

Questions like `A vs. B: O/U 2.5` are **match** totals (`home+away`).  
`A vs. B: CS Huancayo O/U 2.5` is a **team** total (that side's goals only).  
`1st Half` / `2nd Half` use half scores when present (`home_half` / `away_half`); 2H = FT − 1H.

Live mode only emits a total when Over is already locked (`goals > line`); otherwise the market is still open and is skipped.

## BTTS settlement

- Full match: both FT scores &gt; 0.
- First half: both half scores &gt; 0.
- Second half: both `(FT − HT)` scores &gt; 0.
- Half markets **require** half scores; otherwise skip (never fall back to FT BTTS).
- Live: only emit once both sides have scored in that period.

## Misprice rules

Sports taker fee (2026): `fee = feeRate × p × (1 − p)` with default `feeRate = 0.05`.

| Trade | Gross | Net |
|---|---|---|
| Buy WIN at ask `p` | `1 − p` | `gross − fee(p)` |
| Sell LOSE into bid `p` | — | **disabled** (not traded) |

Only rows with **`net_edge ≥ min_net`** (default **0.0076** USDC/share ≈ ask≤**0.992**) go to `opportunities.jsonl`.  
Quotes still record all tokens; dust / fee-insufficient edges stay out of oppo.  
CLI: `--fee-rate`, `--min-net`.

## In-process trading (low latency)

After `flag_misprice` returns true inside `quote_tokens`, `TradeExecutor` runs **in the same process** (does not read `opportunities.jsonl`).

| Mode | Behavior |
|---|---|
| dry-run (default) | Plan fill → append `data/pm-quote/trades.jsonl`; no chain order |
| `--live` | Both signal channels post `create_and_post_market_order` (**FAK**) |
| `--goals-mode` / `--ft-mode` | Independent `dry\|live` for `score_change` vs `match_finished` (modes override `--live` per channel) |

Env (System Main): `QUOTE_LIVE`, `QUOTE_GOALS_MODE`, `QUOTE_FT_MODE` (`dry`/`live`).

Flatten uses **`lot.live`**, not the global session flag — mixed dry/live sessions never CLOB-sell a dry-run open lot.

**Take depth**

| `--take-depth` | Buy WIN |
|---|---|
| `top` (default) | Only best ask size / price; FAK |
| `walk` | Accumulate `asks_top` until max_levels / max_usdc / max_shares / slippage; FAK |

`sell_lose` is **disabled at source**: settled `LOSE` tokens are dropped before CLOB `/books` (only `WIN` / non-LOSE legs are quoted; only `buy_win` is traded).

Price guard: skip when best ≤0.01 or >0.992 unless `--allow-extreme-prices`.  
`buy_win` floor: skip (still append `trades.jsonl` with `skip_reason=buy_price_below_min=…`) when `best_ask < --min-buy-price` (default **0.6**; **0** = off). Env: `QUOTE_MIN_BUY_PRICE`.

**Size policy (`.env`)**: hard caps `QUOTE_MAX_USDC` / `QUOTE_MAX_SHARES` (default 1/25). `QUOTE_SIZE_TIERS=0.98:1` means **ask ≥ 0.98 → $1**, else **$1** (hard-capped); **shares scale with that usdc**. Concurrent open cost capped by `QUOTE_MAX_OPEN_USDC` (default **1000**). Floor `QUOTE_SIZE_FLOOR_USDC` (default 1).  
Idempotency: `event_key|token_id|trade` — successful live posts are skipped on restart.  
SDK: `py-clob-client-v2` (see `requirements-trade.txt`). Env: `PRIVATE_KEY`, `FUNDER`, `SIGNATURE_TYPE`, `CHAIN_ID`, `CLOB_HOST`.

Modules: `trade_settings.py`, `clob_trader.py`, `fill_planner.py`, `trade_executor.py`, `score_reversal.py`.

**Score reversal / disallowed goal**

- Bridge emits `score_change` with `is_reversal=true` when either side’s score drops.
- With AF referee on (**gate default**), goal-ups **wait for AF confirm** before trading (no preconfirm CLOB quote). AF default cadence is **3s first look, then every 1s until 60s, then every 2s until 90s timeout**; `--af-poll` still overrides this with a fixed interval. Confirm → quote/trade. DQD reversal **cancels** pending AF jobs and flattens open lots. AF timeout / miss → ignore the goal (no flatten). Aggressive `--af-postcheck-trade`: buy on DQD score immediately; AF confirm marks lots `af_status=confirmed` (hold); AF timeout flattens `af_pending` lots (`reason=af_confirm_timeout`). Event identity is semantic (`mid|prev→curr`, no wall-clock `ts`).
- **`match_finished` always AF-gates** (even under postcheck): poll AF fixture **`score.fulltime`** (regulation / 90'+stoppage only — **ET and penalties ignored**, matching Polymarket). Trade only when AF regulation equals DQD FT exactly; mismatch / timeout → `af_ft_unconfirmed` (no buy). Skip if event age > `QUOTE_FT_MAX_AGE_S` (default **900**) or `match_id` already in `cursor.processed_ft_match_ids`.
- Open lots carry `af_status` (`pending`|`confirmed`|`none`), optional `af_deadline`, and `fill_status` (`open`|`pending_fill` for delayed CLOB accepts).
- **One event, two phases**: (1) FAK-flatten affected open `buy_win` lots whose **entry_score** is strictly higher than post-reversal / FT score; (2) **quote once** on the corrected `curr` score (may open newly locked markets). Unaffected lots stay open.
- Live flatten sells floored shares (**2 decimal** maker precision) with a **99% haircut**. Before pricing the FAK floor, ledger ``shares``/``usdc`` are reconciled to the live conditional balance (raise shares on better fills; scale ``usdc`` on residuals so VWAP holds). FAK `min_price` = **entry × (1 − 10%)** (tick-floored; unknown entry falls back to `0.5`) — no panic dump at `0.01` after false goals; unfilled residual stays `pending_flatten` for later retries. Before sell: refresh conditional allowance + cancel open orders for that token (frees `sum_of_matched_orders` locks). On CLOB `not enough balance` → cancel again, size by **gate free only while live bal still matches the gate bag** (else size from live bal alone), same entry−10% floor. If still locked, **keep `pending_flatten` for the next tick** (no inline sleep on the watch path). Dust close only when live balance itself is `< 0.01`.
- CLOB `status=delayed` (+ `success`/`orderID`) counts as an accepted fill: register the open lot (brief balance poll) so a later reversal can flatten.
- Rebuild closes zombie opens when known FT already undoes entry (`stale_ft_reversal`).
- Dry-run logs `flatten_dry_run`. Default size cap remains **`max_usdc=5`**.
- Open lots: `data/pm-quote/open_positions.json`.

**Post-goal price samples (research)**

After a `score_change` that successfully `buy_win`s (dry_run or posted), watch writes sample 0 from that quote and background-requotes **only those tokens** at +10s…+50s (6 total) into `data/pm-quote/post_goal_samples.jsonl`. Follow-up jobs run **in parallel** (wall-clock `elapsed_s`). Each follow-up re-reads the match score, recomputes settlement/lock, and sets `reversal_seen` only when a later `score_change` has `prev == score_at_t0` and a lower total. No `maybe_trade` on follow-ups.

**Goal-context observe (research, observe-only)**

After AF goal confirm (gate or postcheck), watch snapshots DQD overview (`/api/data/overview/match/{id}`), AF live `/fixtures?id=` status+goals+score, and list-side `team_A_event`/`team_B_event` 旁证 into `data/pm-quote/goal_context_observe.jsonl` at phases `af_confirmed`, `post_confirm_15s`, `post_confirm_45s`. DQD reversal writes `dqd_reversal` with the same `observe_group_id` when the match had a confirm group (else `unlinked_reversal=true`). Does **not** gate buys or flatten.

**Live Score API observe (trial, observe-only)**

When `.env` has `LIVESCORE_API_KEY` + `LIVESCORE_API_SECRET`, the same AF-confirm / +15s / +45s / DQD-reversal phases also resolve the fixture via `scores/live.json`, then pull **raw** `matches/events.json` (GOAL/score) and `commentary/events.json` (VAR if package allows; errors kept raw) into `data/pm-quote/livescore_observe.jsonl`. DQD→LSA id map cached in `livescore_match_map.json`. Trial is not expected to expose `KICK_OFF`. Does **not** gate buys or flatten.

**Book-context observe (research, observe-only)**

When any of `ODDSPAPI_KEY` / `ODDS_API_IO_KEY` / `THE_ODDS_API_KEY` is set (optional `BOOK_OBSERVE_SOURCES`), the same AF-confirm / DQD-reversal hooks also snapshot moneyline availability (`open` / `suspended` / `missing`) from OddsPapi (default books `pinnacle,singbet`), Odds-API.io (default `Bet365,DraftKings`; override `BOOK_ODDS_API_IO_BOOKS`), and The Odds API (`h2h`, default `regions=us,eu`) into `data/pm-quote/book_context_observe.jsonl` at phases `af_confirmed`, `post_confirm_5s`, `post_confirm_15s`, `post_confirm_45s`, `dqd_reversal`. Odds-API.io odds pulls run **in parallel** with `GET /events/{id}` so each snapshot also stores provider `score` / `clock` (failures land in `score_error`, odds still recorded). The Odds sport-key walk prefers an expanded soccer whitelist (Leagues Cup / MLS / Liga MX / Libertadores / …); on miss it optionally discovers remaining active `soccer_*` keys via `GET /sports` (`BOOK_THE_ODDS_DISCOVER_SPORTS`, default on). **Every HTTP response body** is also written under `data/pm-quote/book_context_raw/` (URLs redact `apiKey`; observe rows keep `raw`/`raw_path`/`requests`). Fixture id map cached in `book_fixture_cache.json`. Event-driven only (quota-safe). Raw dumps are **not** auto-pruned. Does **not** gate buys or flatten.

**System Main** (`python3 frontend/run_main.py`) spawns `pm_quote watch` with these flags automatically (default dry-run + repo `.env`). Same flags on the hub CLI / `QUOTE_*` env. Logs: `data/pm-quote/watch.log`.

## CLOB endpoints

| Method | URL |
|---|---|
| POST | `https://clob.polymarket.com/books` body `[{"token_id":"..."}]` |
| GET | `https://clob.polymarket.com/book?token_id=` |

Fetched via `_http_clob` / `urllib` (HTTP proxy handler or SOCKS socket patch from `pm_lib.configure_proxy`).  
Normalized fields per token: `best_bid`, `best_bid_size`, `best_ask`, `best_ask_size`, `spread`, `midpoint`, `last_trade_price`, `bids_top`, `asks_top`, `tick_size`, `neg_risk`, `book_ts`.

## Market cache file

```json
{
  "match_id": "…",
  "event_id": "…",
  "slug": "…",
  "main_event": { "…Gamma event…" },
  "more_markets": { "…or null…" },
  "exact_score": { "…or null…" },
  "warmed_at": "…+08:00"
}
```

## Prop settlement (when listed)

| Family | Rule |
|---|---|
| Spreads | Parse `Spread: Team (±line)`; favorite covers if margin + line &gt; 0 |
| Totals | Match / team / half O/U; goals from the correct side & period (not always FT total) |
| BTTS | FT / 1H / 2H both-scored; half markets need HT scores |
| Exact score | Question scoreline equals `H-A` → that Yes is WIN |

## Bundle shape (abbrev)

```json
{
  "quoted_at": "…+08:00",
  "match_id": "…",
  "home": "FC Anyang",
  "away": "Gwangju FC",
  "home_score": 2,
  "away_score": 1,
  "winner": "home",
  "count": 6,
  "opportunity_count": 1,
  "quotes": [{ "market_key": "home_yes", "settlement": "WIN", "best_ask": 0.95, "misprice": true }],
  "opportunities": [{ "market_key": "home_yes", "misprice_reason": "WIN token best_ask=0.95 < 1.0" }],
  "discovery": { "skipped": [] }
}
```
