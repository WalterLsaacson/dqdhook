# Polymarket Quote — Reference

## Pipeline

1. Trigger: `score_change` / `match_finished` from match-bridge (or CLI synthetic FT).
2. Join bridged row → `event_id` / `slug` / `market_refs`.
3. Gamma catalog: prefer `data/pm-quote/market_cache/{match_id}.json` (warmed after bridge match from `matches.json`). Complete only when `related_complete` and `main_event` are set (partial sibling warms are retried). Miss → fetch main + More Markets / Exact Score once and write cache. Cache holds **token definitions**, not CLOB prices. Dropped after FT quote consumption.
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
| `livescore_observe.jsonl` | **Not pruned** (research) |
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
| default | **goals=live**, **ft=live** (pitch-gate buys live; FT live, ungated) |
| `--live` | Both signal channels post `create_and_post_market_order` (**FAK**) |
| `--goals-mode` / `--ft-mode` | Independent `dry\|live` for `score_change` vs `match_finished` (modes override `--live` per channel) |

Env (System Main): `QUOTE_LIVE`, `QUOTE_GOALS_MODE`, `QUOTE_FT_MODE` (`dry`/`live`). Defaults when unset: goals **live**, ft **live**.

Flatten uses **`lot.live`**, not the global session flag — mixed dry/live sessions never CLOB-sell a dry-run open lot.

**Take depth**

| `--take-depth` | Buy WIN |
|---|---|
| `top` | Only best ask size / price; FAK |
| `walk` (default) | Accumulate `asks_top` until max_levels / max_usdc / max_shares / slippage; FAK |

`sell_lose` is **disabled at source**: settled `LOSE` tokens are dropped before CLOB `/books` (only `WIN` / non-LOSE legs are quoted; only `buy_win` is traded).

Price guard: skip when best ≤0.01 or >0.992 unless `--allow-extreme-prices`.  
`buy_win` floor: skip (still append `trades.jsonl` with `skip_reason=buy_price_below_min=…`) when `best_ask < --min-buy-price` (default **0.6**; **0** = off). Env: `QUOTE_MIN_BUY_PRICE`. **Pitch-gate confirmed buys skip this floor.**

**Size policy (`.env`)**: hard caps `QUOTE_MAX_USDC` / `QUOTE_MAX_SHARES` (default 1/25). `QUOTE_SIZE_TIERS=0.98:1` means **ask ≥ 0.98 → $1**, else **$1** (hard-capped); **shares scale with that usdc**. Concurrent open cost capped by `QUOTE_MAX_OPEN_USDC` (default **1000**). Floor `QUOTE_SIZE_FLOOR_USDC` (default 1) and the $1 marketable bump are **skipped when `trade_context.pitch_gate=true`**. Fee/`min_net` and `QUOTE_MAX_USDC` still apply.  
Idempotency: `event_key|token_id|trade` — successful live posts are skipped on restart.  
SDK: `py-clob-client-v2` (see `requirements-trade.txt`). Env: `PRIVATE_KEY`, `FUNDER`, `SIGNATURE_TYPE`, `CHAIN_ID`, `CLOB_HOST`.

Modules: `trade_settings.py`, `clob_trader.py`, `fill_planner.py`, `trade_executor.py`, `score_reversal.py`, `pitch_gate.py`.

**Pitch-gate buy (single confirm + post-buy protection)**

1. DQD goal-up + Polymarket paired → `PitchGateCoordinator.start_gate` (no immediate `_quote_one`).
2. Capture frames every **5s** for up to **150s**, with the **first frame at +5s** after the goal: **≥5** frames under normal timing; early `in_play` does **not** stop capture.
3. Each successful JPEG → pitch-state `judge_inputs`. Need **board OCR score match** + **`GATE_CONFIRM_FRAMES`** (currently **1**) consecutive `in_play` frames → one `_quote_one` with `trade_context.pitch_gate=True` (**buy once**). Otherwise keep capturing (no buy). Delayed reversals are covered by the protection window below, not by extra confirm frames.
4. Any frame with **VAR** (`stopped_reason=var`) during the session → **permanent no-buy** for that goal (`mode=pitch_gate_var_veto`); keep capturing until timeout.
5. After a buy, **keep capturing/judging** until the 150s timeout (board/debug); do not buy again. Session ends with `complete`.
6. Never `in_play` before timeout → `mode=pitch_gate_timeout`, mark seen, no buy.
7. DQD reversal → `cancel_match(match_id)` → cancels open sessions **and revokes any undrained `in_play` buy rows** (`buy_revoked` / `pitch_gate_buy_revoked`). Quote tick drains gate results **after** event handling so same-tick reversals win the race. (If the buy already executed on a prior tick, cancel cannot unwind it.)
8. Requires `QUOTE_DQD_STREAM_OBSERVE=1` and `QUOTE_PITCH_STATE=1`; else `pitch_gate_unavailable`.
9. FT path remains immediate quote (default live), no screenshot gate.
10. **Rest fallback** (opt-in): when **`QUOTE_REST_ENABLED=1`**, if pitch-gate WIN has no FAK fill (no ask / not misprice), post a limit bid @ **0.99** (`GTD`, expire **`QUOTE_REST_EXPIRE_S`** default **3600s**). Size is at least **5 shares** (CLOB limit floor, ≈ **$4.95** @ 0.99). Default **`QUOTE_REST_ENABLED=0`** (no limit posts). Logged as `rest_dry_run` / `rest_posted`; DQD reversal still cancels these rests.

**Score reversal / disallowed goal**

- Bridge emits `score_change` with `is_reversal=true` when either side’s score drops.
- Goal-ups wait for pitch-gate; FT quotes immediately (default live). Events older than `QUOTE_FT_MAX_AGE_S` (default **900**) are skipped; FT once-per-`match_id` via `cursor.processed_ft_match_ids`.
- DQD reversal cancels rest orders and open pitch-gate sessions, and revokes undrained buys.
- **Post-buy protection window**: lots carry `pitch_gate` + `opened_at`. A DQD reversal that undoes the entry score flattens gate lots opened within **`QUOTE_GATE_PROTECT_S`** (default **300s**, `0` disables) as `gate_protect_reversal`. Lots outside the window, and non-gate (FT) lots, stay deferred to the FT `ft_reversal_vs_entry` path — by then the token is near zero, so that exit frees budget rather than recovering value. Top-ups keep the first `opened_at` so the window never extends. The window is bounded because late DQD score drops are more often data noise than real VAR calls, and flattening on noise sells into a bad book for nothing.
- Live flatten FAK-sells floored shares with entry×80% floor; dry lots never CLOB-sell.
- Rebuild closes zombie opens when known FT already undoes entry (`stale_ft_reversal`).
- Dry-run logs `flatten_dry_run`. Open lots: `data/pm-quote/open_positions.json`.

**Post-goal price samples (research)**

After a `score_change` that successfully `buy_win`s (dry_run or posted), watch writes sample 0 from that quote and background-requotes **only those tokens** at +10s…+50s (6 total) into `data/pm-quote/post_goal_samples.jsonl`. Follow-up jobs run **in parallel** (wall-clock `elapsed_s`). Each follow-up re-reads the match score, recomputes settlement/lock, and sets `reversal_seen` only when a later `score_change` has `prev == score_at_t0` and a lower total. No `maybe_trade` on follow-ups.

**DQD stream / pitch-state (gate + research)**

Pitch-gate drives captures every **5s** for up to **150s** after a paired goal (first frame @ **+5s**; ≥5 frames; continues after first `in_play` buy). Rows still land in `data/pm-quote/dqd_stream_observe.jsonl` / `dqd_stream_frames/` with `gate=true`. Pitch-state judges write `data/pm-quote/pitch_state_judge.jsonl` (and JPEG sidecars). Missing stream/pitch env → gate unavailable (no buy for that goal).

**Animation source (纳米数据):** Dongqiudi embeds `iframe.md-anim-iframe` pointing at `https://tracker.namitiyu.com/zh/football?profile=…&id=…`. Map DQD → tracker via:

```text
GET /magicball/v1/match/app/detail?id=<dqd_match_id>&app=dqd&lang=zh-cn
→ living[] / matchLiving[] where live_type=animation → url
```

`profile` is the DQD partner slot (observed `yADdIyHoruqHP`); namitiyu `id` ≠ DQD `match_id` and must come from that URL. Prefer opening the tracker URL directly for OCR (avoids copyright `video` on the match page). Details: [`dongqiudi-match/reference.md`](../dongqiudi-match/reference.md)#animation-live-纳米数据--namitiyu.

Smoke: `python3 .cursor/skills/polymarket-quote/scripts/smoke_pitch_gate.py`.

**Pitch Gate board (System Main)**

`frontend/pitch-gate-board` on **:8791** (launched by `run_main`) reads `dqd_stream_observe.jsonl` + `pitch_state_judge.jsonl`, groups by goal `event_key`, and shows each frame thumbnail with `play_state` / confidence / evidence. Read-only viewer (no Start/Stop).

**Live Score API observe (trial, observe-only)**

When `.env` has `LIVESCORE_API_KEY` + `LIVESCORE_API_SECRET`, DQD-reversal may resolve the fixture via `scores/live.json`, then pull **raw** `matches/events.json` (GOAL/score) and `commentary/events.json` (VAR if package allows; errors kept raw) into `data/pm-quote/livescore_observe.jsonl`. DQD→LSA id map cached in `livescore_match_map.json`. Trial is not expected to expose `KICK_OFF`. Does **not** gate buys or flatten.

**System Main** (`python3 frontend/run_main.py`) spawns `pm_quote watch` with default **goals=live / ft=live** (+ repo `.env`). Pitch-gate: first frame @**+5s**, then every 5s until **150s** → first `in_play` → one buy (keep capturing). Logs: `data/pm-quote/watch.log`.

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
