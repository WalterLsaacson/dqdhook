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
| `quotes.jsonl` / `opportunities.jsonl` / `trades.jsonl` / `post_goal_samples.jsonl` | Keep rows with `quoted_at`/`sampled_at` ≥ now − retain_hours |
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
| Sell LOSE into bid `p` | `p` | `gross − fee(p)` |

Only rows with **`net_edge ≥ min_net`** (default **0.02** USDC/share) go to `opportunities.jsonl`.  
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

| `--take-depth` | Buy WIN | Sell LOSE |
|---|---|---|
| `top` (default) | Only best ask size / price; FAK | Only best bid; needs position |
| `walk` | Accumulate `asks_top` until max_levels / max_usdc / max_shares / slippage; FAK | Walk `bids_top` downward; FAK |

Price guard: skip when best ≤0.01 or ≥0.99 unless `--allow-extreme-prices`.  
`buy_win` floor: skip (still append `trades.jsonl` with `skip_reason=buy_price_below_min=…`) when `best_ask < --min-buy-price` (default **0** = off). Env: `QUOTE_MIN_BUY_PRICE`.

**Size policy (`.env`)**: hard caps `QUOTE_MAX_USDC` / `QUOTE_MAX_SHARES` (default 20/25). Per-ask tier `QUOTE_SIZE_TIERS=0.93:20,0.95:15,…` picks an effective usdc; **shares scale with that usdc** (`eff_shares = min(max_shares * eff/hard, eff/ask)`). Concurrent open cost capped by `QUOTE_MAX_OPEN_USDC` (default 45). Floor `QUOTE_SIZE_FLOOR_USDC` (default 1). High asks (0.97+) still buy — just smaller.  
Idempotency: `event_key|token_id|trade` — successful live posts are skipped on restart.  
SDK: `py-clob-client-v2` (see `requirements-trade.txt`). Env: `PRIVATE_KEY`, `FUNDER`, `SIGNATURE_TYPE`, `CHAIN_ID`, `CLOB_HOST`.

Modules: `trade_settings.py`, `clob_trader.py`, `fill_planner.py`, `trade_executor.py`, `score_reversal.py`.

**Score reversal / disallowed goal**

- Bridge emits `score_change` with `is_reversal=true` when either side’s score drops.
- With AF referee on (**gate default**), goal-ups **wait for AF confirm** before trading (no preconfirm CLOB quote). AF timeout / miss → ignore the goal (no flatten). **Reversals flatten immediately** if a lot exists; same match blocks new buys until exit clears. Aggressive `--af-postcheck-trade`: buy on DQD score immediately; AF confirm marks lots `af_status=confirmed` (hold); AF timeout flattens `af_pending` lots (`reason=af_confirm_timeout`). Event identity is semantic (`mid|prev→curr`, no wall-clock `ts`).
- Open lots carry `af_status` (`pending`|`confirmed`|`none`), optional `af_deadline`, and `fill_status` (`open`|`pending_fill` for delayed CLOB accepts).
- **One event, two phases**: (1) FAK-flatten affected open `buy_win` lots whose **entry_score** is strictly higher than post-reversal / FT score; (2) **quote once** on the corrected `curr` score (may open newly locked markets). Unaffected lots stay open.
- Live flatten sells floored shares (**2 decimal** maker precision) with a **99% haircut**, `min_price=0.2`. Before sell: refresh conditional allowance + cancel open orders for that token (frees `sum_of_matched_orders` locks). On CLOB `not enough balance` → cancel again, size by **gate free only while live bal still matches the gate bag** (else size from live bal alone), dump at `min_price=0.01`. If still locked, **keep `pending_flatten` for the next tick** (no inline sleep on the watch path). Dust close only when live balance itself is `< 0.01`.
- CLOB `status=delayed` (+ `success`/`orderID`) counts as an accepted fill: register the open lot (brief balance poll) so a later reversal can flatten.
- Rebuild closes zombie opens when known FT already undoes entry (`stale_ft_reversal`).
- Dry-run logs `flatten_dry_run`. Default size cap remains **`max_usdc=5`**.
- Open lots: `data/pm-quote/open_positions.json`.

**Post-goal price samples (research)**

After a `score_change` that successfully `buy_win`s (dry_run or posted), watch writes sample 0 from that quote and background-requotes **only those tokens** at +10s…+50s (6 total) into `data/pm-quote/post_goal_samples.jsonl`. Follow-up jobs run **in parallel** (wall-clock `elapsed_s`). Each follow-up re-reads the match score, recomputes settlement/lock, and sets `reversal_seen` only when a later `score_change` has `prev == score_at_t0` and a lower total. No `maybe_trade` on follow-ups.

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
