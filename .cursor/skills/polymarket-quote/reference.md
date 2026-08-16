# Polymarket Quote — Reference

## Pipeline

1. Trigger: `score_change` / `match_finished` from match-bridge (or CLI synthetic FT).
2. Join bridged row → `event_id` / `slug` / `market_refs`.
3. Gamma catalog: prefer `data/pm-quote/market_cache/{match_id}.json` (warmed after bridge match from `matches.json`). Complete only when `related_complete` and `main_event` are set (partial sibling warms are retried). Miss → fetch main + More Markets / Exact Score once and write cache. Cache holds **token definitions**, not CLOB prices. It survives the AF FT wait and is dropped after FT quote consumption.
4. Settle each token from `home_score` / `away_score` → `WIN` | `LOSE` | `PENDING`.
5. CLOB: urllib `POST /books` (batch ≤50) with `GET /book` fallback (same proxy as Gamma; no subprocess curl).
6. **Live phases**: (A) totals + BTTS books → misprice → `maybe_trade`; (B) exact score (+ remainder) books → trade. One merged bundle for analytics/UI.
7. Persist bundle + opportunities.

## Watch wake

`pm_quote watch` polls `data/bridge/events.jsonl` mtime/size (~50ms). `--interval` (default **0.25**) is the **max idle** between ticks; new events wake early. Each tick still runs `retry_pending_flattens` and drains unprocessed cursor keys. `cursor.json` is atomically rewritten only when processed keys, processed FT ids, or the byte offset changes. System Main: `QUOTE_INTERVAL` default `0.25`.

Background warmer (`market_cache.py`) syncs open paired fixtures every ~5s.

## Retention (rolling 24h)

`data_prune.py` (started with `pm_quote watch`; also `pm_quote prune`):

| Target | Rule |
|---|---|
| `market_cache/*.json` | Keep open paired matches plus finished matches not yet present in `cursor.processed_ft_match_ids`; also scan `events.jsonl` so an unconsumed FT remains protected after it disappears from `matches.json`. Drop consumed FT / orphan. **Skip** if matches file missing / bad / empty. |
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
| `top` | Only best ask size / price; FAK |
| `walk` (default) | Accumulate `asks_top` until max_levels / max_usdc / max_shares / slippage; FAK |

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

**Post-confirm DQD/AF sampling (disabled)**

The old post-confirm DQD overview and AF live/list research snapshots are no longer started or triggered. AF polling required to make the second confirmation remains unchanged. Existing `goal_context_observe.jsonl` files are historical and are not deleted.

**Live Score API observe (trial, observe-only)**

When `.env` has `LIVESCORE_API_KEY` + `LIVESCORE_API_SECRET`, the same AF-confirm / +15s / +45s / DQD-reversal phases also resolve the fixture via `scores/live.json`, then pull **raw** `matches/events.json` (GOAL/score) and `commentary/events.json` (VAR if package allows; errors kept raw) into `data/pm-quote/livescore_observe.jsonl`. DQD→LSA id map cached in `livescore_match_map.json`. Trial is not expected to expose `KICK_OFF`. Does **not** gate buys or flatten.

**Odds third confirmation (graded trading + research log)**

With `ODDS_API_IO_KEY`, every AF-confirmed goal starts 31 samples at `0/3/…/90s`. Each sample fetches only Odds-API.io: `/odds/multi` for Bet365 (gate) plus Unibet (observe-only) and `/events/{id}` score/clock in parallel (concurrent odds pulls coalesce, up to 10 events per request). Unibet is written into `book_context_observe.jsonl` / `book_context_raw/` with `observe_only=true` and the same CS/Totals core-clean fields (`observe_books`); it does not affect A/B/C. Missing Unibet does not veto. OddsPapi and The Odds API remain disabled (legacy parsers may remain but are unreachable from the active source config).

Grades are cumulative per Polymarket token/opportunity for **A/B live sizing**: C is **record-only** (dry-run into `trades.jsonl` with no USDC notional, no CLOB post, no open lot). **Core-clean Bet365** = open, at least one of Correct Score or the main `Totals` market is inspectable, and those two gate markets have no already-impossible offers. Alternative Goal Line / team totals / BTTS / clean sheet / corners are stored on the observe row (`impossible_offers` / `ignored_impossible_offers`) but do not veto B/A. B=`$3` when the core book is clean (score match not required); soft identity (`identity_soft_ok`: cached mapping + fuzzy teams, or odds multi ok with a brief `/events/{id}` failure) is enough for B. A=`$10` when the core book is clean **and** the Odds-API.io provider score exactly equals AF/DQD **and** hard `identity_verified` (teams, kickoff within ±12h, non-terminal status, home/away direction). Soft identity never reaches A. A matching provider score with Bet365 missing, only Spread/ML and no CS/Totals, or a gate-market impossible offer stays C. Book labels such as `Bet365 (no latency)` are treated as Bet365. Hard identity still uses that poll's `/events/{id}` body. Provider-reversed fixtures are normalized to the DQD score frame, while impossible-market checks remain in the provider frame. Missing identity with no soft path forces C; a clear wrong-fixture mismatch (teams do not fuzzy-match) clears the cached id for remapping. Soft-ok polls keep the cache. Examples of gate-impossible offers include a Correct Score below either observed team score, or a main Totals Under line below goals already scored. BTTS No / Clean Sheet Yes after the opponent scored remain research-only dirt.

Because C never registers exposure, B sizes the full `$3` and a direct A sizes the full `$10`; B→A adds at most `$7`. Existing open-budget, share, price and depth guards still apply. Live matched buys are accounted from CLOB `makingAmount` (USDC) and `takingAmount` (shares), not the requested FAK amount. Parallel live posts reserve `plan.usdc` before the CLOB HTTP so `QUOTE_MAX_OPEN_USDC` cannot be overshot (`QUOTE_TRADE_WORKERS` default 4). B upgrades omit Exact Score; A still trades totals/BTTS first, then exact, and skips exact rows whose book `tick_size` is `0.001`. If an A/B quote errors or only partially fills, the same upgrade is retried every 1–5s while its observer generation is current; success, DQD reversal or a replacement goal generation stops it. A strict grade increase creates the upgrade edge; same-grade market/score changes remain research rows.

Every actual HTTP body is written under `data/pm-quote/book_context_raw/` with `apiKey` redacted and a nanosecond/group/sequence filename so concurrent samples cannot overwrite one another. The football events catalog is shared for 60s, mapping misses are cached for that catalog generation, and a 429 pauses all Odds-API.io calls until numeric/date `Retry-After` or ISO/epoch/relative rate-limit reset (60s fallback). `book_context_observe.jsonl` stores compact request metadata + `raw_path`, source parsing, poll offset, grade/target/reason, impossible offers, fingerprint, `data_changed`, and `upgrade_emitted`; it does not duplicate full raw bodies. `trades.jsonl` stores grade context plus target/already/remaining sizing and the actual matched plan. Raw dumps are not auto-pruned.

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
