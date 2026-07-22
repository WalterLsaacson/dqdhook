# Polymarket Quote — Reference

## Pipeline

1. Trigger: `match_finished` from match-bridge (or CLI synthetic FT).
2. Join bridged row → `event_id` / `slug` / `market_refs`.
3. Gamma: refresh main event markets (with `outcomes`); optionally fetch More Markets / Exact Score siblings.
4. Settle each token from `home_score` / `away_score` → `WIN` | `LOSE` | `PENDING`.
5. CLOB: `POST /books` (batch ≤50) with `GET /book` fallback.
6. Persist bundle + opportunities.

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
| `--live` | Same plan, then `create_and_post_market_order` (FOK top buy / FAK walk buy; FAK sells) |

**Take depth**

| `--take-depth` | Buy WIN | Sell LOSE |
|---|---|---|
| `top` (default) | Only best ask size / price; FOK | Only best bid; needs position |
| `walk` | Accumulate `asks_top` until max_levels / max_usdc / max_shares / slippage; FAK | Walk `bids_top` downward; FAK |

Price guard: skip when best ≤0.01 or ≥0.99 unless `--allow-extreme-prices`.  
Idempotency: `event_key|token_id|trade` — successful live posts are skipped on restart.  
SDK: `py-clob-client-v2` (see `requirements-trade.txt`). Env: `PRIVATE_KEY`, `FUNDER`, `SIGNATURE_TYPE`, `CHAIN_ID`, `CLOB_HOST`.

Modules: `trade_settings.py`, `clob_trader.py`, `fill_planner.py`, `trade_executor.py`.

**System Main** (`python3 frontend/run_main.py`) spawns `pm_quote watch` with these flags automatically (default dry-run + repo `.env`). Same flags on the hub CLI / `QUOTE_*` env. Logs: `data/pm-quote/watch.log`.

## CLOB endpoints

| Method | URL |
|---|---|
| POST | `https://clob.polymarket.com/books` body `[{"token_id":"..."}]` |
| GET | `https://clob.polymarket.com/book?token_id=` |

Normalized fields per token: `best_bid`, `best_bid_size`, `best_ask`, `best_ask_size`, `spread`, `midpoint`, `last_trade_price`, `bids_top`, `asks_top`, `tick_size`, `neg_risk`, `book_ts`.

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
