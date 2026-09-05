# Polymarket Quote — Reference

## Pipeline

1. Trigger: `score_change` / `match_finished` from match-bridge (or CLI synthetic FT).
2. Join bridged row → `event_id` / `slug` / `market_refs`.
3. Gamma catalog: prefer `data/pm-quote/market_cache/{match_id}.json` (warmed after bridge match from `matches.json`). Complete only when `related_complete` and `main_event` are set (partial sibling warms are retried). Miss → fetch main + More Markets / Exact Score once and write cache. Cache holds **token definitions**, not CLOB prices. Dropped after FT quote consumption.
4. Settle each token from `home_score` / `away_score` → `WIN` | `LOSE` | `PENDING`. Half / team totals remap DQD `home_half` onto Polymarket home/away when `sides_swapped`.
5. CLOB: urllib `POST /books` (batch ≤50) with `GET /book` fallback (same proxy as Gamma; no subprocess curl).
6. **Live phases**: (A) totals + BTTS books → misprice → `maybe_trade`; (B) exact score (+ remainder) books → trade. One merged bundle for analytics/UI.
7. Persist bundle + opportunities.

## Watch wake

`pm_quote watch` polls `data/bridge/events.jsonl` mtime/size (~50ms). `--interval` (default **0.25**) is the **max idle** between ticks; new events wake early. Each tick starts/cancels pitch-gate and **enqueues** quote/rest/flatten onto the CLOB worker; `retry_pending_flattens` / rest reconcile run on that worker's idle sweep. `cursor.json` is atomically rewritten only when processed keys, processed FT ids, or the byte offset changes. System Main: `QUOTE_INTERVAL` default `0.25`.

Background warmer (`market_cache.py`) syncs open paired fixtures every ~5s.

## Retention (rolling 24h)

`data_prune.py` (started with `pm_quote watch`; also `pm_quote prune`):

| Target | Rule |
|---|---|
| `market_cache/*.json` | Keep open paired matches plus finished matches not yet present in `cursor.processed_ft_match_ids`; also scan `events.jsonl` so an unconsumed FT remains protected after it disappears from `matches.json`. Drop consumed FT / orphan. **Skip** if matches file missing / bad / empty. |
| `quotes.jsonl` / `opportunities.jsonl` / `trades.jsonl` | Keep rows with `quoted_at`/`sampled_at`/`dqd_ts` ≥ now − retain_hours |
| `book_context_observe.jsonl` / `dqd_stream_observe.jsonl` / `af_observe.jsonl` / `prematch_odds.jsonl` | Same rolling cutoff |
| `book_context_raw/*.json` | Drop files with mtime &lt; cutoff |
| `livescore_observe.jsonl` | **Not pruned** (research) |
| `data/bridge/events.jsonl` / `data/events.jsonl` | Keep if `ts` ≥ cutoff (pure time; `processed_keys` is capped and must not pin history) |

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

Only rows with **`net_edge ≥ min_net`** (default **0.00475** USDC/share ≈ ask≤**0.995**) go to `opportunities.jsonl`.  
Quotes still record all tokens; dust / fee-insufficient edges stay out of oppo.  
CLI: `--fee-rate`, `--min-net`.

## In-process trading (low latency)

After `flag_misprice` returns true inside `quote_tokens`, `TradeExecutor` runs **in the same process** on the **CLOB worker thread** (watch tick only enqueues the event payload; it does not wait for `/books` or rest posts).

**Gate prewarm** (`QUOTE_GATE_PREWARM`, default on): on pitch-gate `start_gate` (buy path), a background thread warms Gamma catalog + refreshes `POST /books` every `QUOTE_GATE_PREWARM_INTERVAL_S` (default 3s). On BUY, `quote_bridge_event` prefers that snapshot when age ≤ `QUOTE_GATE_PREWARM_MAX_AGE_S` (default 4s); otherwise fetches. Cuts ~0.5–1s books RTT after BUY — does **not** shorten DOM/AF/shot wait.

| Mode | Behavior |
|---|---|
| default | **goals=live**, **ft=live** (pitch-gate buys live; FT live after AF regulation confirm) |
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

Price guard: skip when best ≤0.01 or >0.995 unless `--allow-extreme-prices`. **Exception:** FT locked `WIN` with ask ≤0.01 still **FAK**s (`QUOTE_FT_DUST_FAK`, default on) at soccer tick **0.01**, sized by **`QUOTE_FT_DUST_USDC` (default $100)** — independent of `QUOTE_FT_MAX_USDC`, still clipped by leftover `QUOTE_MAX_OPEN_USDC`. Do not treat a 0.001 ghost wall as walkable size; unmatched USDC is not spent and no open lot is registered. Goals / pitch-gate still skip ≤0.01. `QUOTE_FT_DUST_USDC=0` disables this path.  
`buy_win` floor: skip (still append `trades.jsonl` with `skip_reason=buy_price_below_min=…`) when `best_ask < --min-buy-price` (default **0.6**; **0** = off). Env: `QUOTE_MIN_BUY_PRICE`. **Pitch-gate and FT buys skip this floor.**

**Size policy (`.env`)**: per-channel hard caps `QUOTE_GOAL_MAX_USDC` (score_change / pitch-gate) and `QUOTE_FT_MAX_USDC` (match_finished). Shared fallback is `QUOTE_MAX_USDC` / `QUOTE_MAX_SHARES` (default 1/25). `QUOTE_GOAL_SIZE_TIERS` / `QUOTE_FT_SIZE_TIERS` are `ask:usdc` (e.g. `0.98:50`); if a channel tier env is unset, goals inherit `QUOTE_SIZE_TIERS` and FT uses `0.98:<QUOTE_FT_MAX_USDC>` so the FT amount is not clipped by the goal tier. Shares: `QUOTE_GOAL_MAX_SHARES` / `QUOTE_FT_MAX_SHARES` (FT defaults to `max(shared, ft_usdc/0.25)` so USDC binds). Concurrent open cost capped by `QUOTE_MAX_OPEN_USDC` (default **1000**). Floor `QUOTE_SIZE_FLOOR_USDC` (default 1) and the $1 marketable bump are **skipped when `trade_context.pitch_gate=true`**. Fee/`min_net` still apply. **Locked sweep:** `QUOTE_LOCKED_SWEEP` (default on) + **`QUOTE_LOCKED_SWEEP_USDC` (default $1000)**. On pitch-gate `score_change`, if a token is already live WIN at event `prev` (this goal voided), FAK remaining asks with price ≤0.995 (`take_depth=locked_sweep`). Does **not** use `QUOTE_GOAL_MAX_USDC`; clipped by the sweep cap and leftover `QUOTE_MAX_OPEN_USDC`. `QUOTE_LOCKED_SWEEP=0` or `QUOTE_LOCKED_SWEEP_USDC=0` disables. Not FT. Not T+10. Not `reversal_cushion` (that subtracts a goal from either side). **T+10:** `QUOTE_T10_USDC` (unset/`0` off) sizes FAK and a stacked 0.99 GTC per locked WIN token after `QUOTE_T10_DELAY_S` (default 600). **Flatten:** after a confirmed reverse, skip lots that are still live WIN at the post-reverse score (1-1→1-0 does not sell `home_total_0.5_over`).  
Idempotency: `event_key|token_id|trade` — successful live posts are skipped on restart.  
SDK: `py-clob-client-v2` (see `requirements-trade.txt`). Env: `PRIVATE_KEY`, `FUNDER`, `SIGNATURE_TYPE`, `CHAIN_ID`, `CLOB_HOST`.

Modules: `trade_settings.py`, `clob_trader.py`, `fill_planner.py`, `trade_executor.py`, `score_reversal.py`, `pitch_gate.py`, `t10_scan.py`.

**Pitch-gate buy (AND only; stop AF+DOM after buy)**

1. DQD goal-up + Polymarket paired → `PitchGateCoordinator.start_gate` (no immediate `_quote_one`).
2. Sample DOM every **5s** for up to **120s**, first tick at **+0s**. AF starts on this tick's DOM `in_play` (same frame); celebration / unclear does not poll AF. Then AF+DOM on later `in_play` ticks until buy / timeout / cancel. Odds HTTP is kicked in the background so it cannot stall the AND buy; the loop only joins that thread when AND cannot fire this tick.
3. Buy only when **this tick** DOM `in_play` ∧ AF `ok && score_match` (or a latch from an earlier `in_play` poll of this goal). 射门 is not a buy gate. Then one `_quote_one` with `trade_context.pitch_gate=True`. Then **stop AF and DOM**. AF rate-limit / hard score mismatch fails closed. Odds Grade A is **observe-only** (does not skip `in_play`, does not stand in for unresolved AF). Buy-side AF∨DOM is **rejected** (`design-af-dom-or-gate.md`).
4. Any tick with **VAR before buy** → **permanent no-buy** (`mode=pitch_gate_var_veto`), even if a shot or Grade A was seen. After buy there is no DOM trail, so post-buy VAR is not sampled.
5. Never aligned before timeout → `mode=pitch_gate_timeout`, mark seen, no buy.
6. DQD reversal → in-process bridge **hooks emit** to `cancel_match` immediately; rest cancel is queued onto the CLOB worker (`rest_cancel` priority beats idle housekeep). The worker revokes **submitted `event_key`s** for that match, not the whole `match_id` (a later non-opening re-award with a new ts can still quote). **Opening** 0-0→1-0 / 0-0→0-1 skips `start_gate` when that transition was already reversed on the match, or when the DQD clock is ≥90' (`pitch_gate_reversal_risk_skip`). Ordinary ~35' opening goals stay full size. Quote tick also **pre-pass** reversals **before** `start_gate`. `block_inverted_goal` keys on the goal stem + reverse ts: older/same-ts 0-1→1-1 is blocked; a re-awarded non-opening goal with a newer ts is not. Drain gate results **after** event handling so same-tick reversals revoke queued buys. If **open lots exist**, start a 5s AF confirm trail (DOM still sampled for the board, never `_quote_one`); on first AF `ok && score_match` → flatten and **stop the trail**.
7. Requires `QUOTE_DQD_STREAM_OBSERVE=1`; else `pitch_gate_unavailable`. No screenshots / OCR / JPEG.
8. FT quotes only after AF ``regulation_ready`` (DQD ``period=FT`` is a hint). Hourly leftover sweep also requires AF regulation (``--require-af``).
9. **Rest fallback**: when **`QUOTE_REST_ENABLED=1`**, if pitch-gate WIN has no FAK fill, post a limit bid snapped onto the **CLOB-legal tick** (`GTC` by default — stays until DQD reversal, FT, or manual cancel), including one-sided bid books with no ask. Soccer CLOB minimum is **0.01** even when metadata says `0.001` — rest **clamps tick to 0.01** and **0.995 → 0.99**. Set **`QUOTE_REST_EXPIRE_S>0`** to use `GTD` instead. Size is **`QUOTE_REST_USDC` (default $5)** so the bid clears the CLOB 5-share floor. Pitch-gate rest is **not** clipped by `QUOTE_MAX_OPEN_USDC`.
10. **T+10 rescan**: each paired goal-up schedules one job (`data/pm-quote/t10_pending.json`) for **`QUOTE_T10_DELAY_S`** (default 600s), independent of whether pitch-gate bought. At fire, poll AF **live** goals (`QUOTE_T10_AF_TIMEOUT_S`, default 90s) **only while AF status is still in play**. If AF is FT/ET/PEN / `regulation_ready`, or a `match_finished` AF confirm is already pending, skip (`t10_skip_af_finished` / `t10_skip_ft_pending`) and let the FT path quote `score.fulltime`. Dongqiudi `prev_scores` is skeleton only (sides / halves, re-oriented onto Polymarket). Cache miss / timeout → skip, no DQD fallback. Then the same fee-aware `buy_win` path (skip `min_buy_price`; **no** locked sweep; **no** flatten on this quote). **`QUOTE_T10_USDC`** sizes **both** FAK and a 0.99 GTC rest (rest posts even when `QUOTE_REST_ENABLED=0`). Rest is a **second** T10-sized bid per **locked WIN token** (totals Over / BTTS / exact No already dead), not leftover after FAK, and is **not** clipped by `QUOTE_MAX_OPEN_USDC`. Unset / `0` / `QUOTE_T10=0` disables. Skip if AF has already confirmed FT; cancel pending jobs after AF regulation confirm (not on DQD `match_finished` alone). Reversal does **not** cancel the queue. Jobs more than **`QUOTE_T10_MAX_LATE_S`** (default 900s) late are dropped.

**Score reversal / disallowed goal**

- Bridge emits `score_change` with `is_reversal=true` when either side’s score drops.
- Goal-ups wait for pitch-gate; FT quotes after AF regulation confirm. Events older than `QUOTE_FT_MAX_AGE_S` (default **900**) are skipped; FT once-per-`match_id` via `cursor.processed_ft_match_ids`.
- DQD reversal cancels rest and open pitch-gate sessions, and revokes undrained buys. **Open lots** restart the 5s AF+DOM+Odds trail against the **post-reverse** score. Flatten on first AF **`ok && score_match`** only **for lots that are no longer WIN at that score**, then **stop the 5s trail immediately** (same as buy: no DOM after the trigger tick). A 1-1→1-0 reverse keeps a home O/U 0.5 that was already locked at 1-0. DOM `.center-box` (celebration / VAR / stale clock) is observed, **not** a flatten trigger. AF error / mismatch alone does not flatten. Tracker open failure still polls AF. 120s AF never confirms → **hold**. No lots → cancel only.
- Raw DQD reverse without AF confirm still uses `QUOTE_GATE_PROTECT_S` (default **300s**, `0` disables that unconfirmed path). **Confirmed** `flatten_or` (AF) sells pitch-gate lots at any age. FT lots stay on `ft_reversal_vs_entry`.
- Live flatten FAK-sells floored shares with entry×80% floor; dry lots never CLOB-sell.
- Rebuild closes zombie opens when known FT already undoes entry (`stale_ft_reversal`).
- Dry-run logs `flatten_dry_run`. Open lots: `data/pm-quote/open_positions.json`.

**Odds / Bet365 (observe-only; HTTP must not stall AND)**

`book_context_observe.sample_gate_tick` is kicked off on the same DOM clock in a background thread so HTTP cannot stall AND buy or AF flatten. Writes `data/pm-quote/book_context_observe.jsonl` (Grade A/B/C). **Grade A does not emit a buy** and does not stand in for AF. Requires `ODDS_API_IO_KEY`.

**Prematch books:** when a paired `matches.json` row first sits inside **`QUOTE_PREMATCH_LEAD_S` (default 1800s / 30 min) before kickoff**, take **one** snapshot of all Bet365 + 1xbet markets into `data/pm-quote/prematch_odds.jsonl`. Not a repeating poll. Restart will not recapture a match that already has an `ok` row. Disable with `QUOTE_PREMATCH_ODDS=0`. Same `/odds/multi` coalesce as the gate.

**DQD stream / DOM gate**

Pitch-gate drives DOM reads every **5s** for up to **120s** after a paired goal (first tick @ **+0s**; buy / flatten / cancel / timeout stop the session). Aligned buy **stops AF and DOM**. Reverse confirm **stops AF and DOM** on the flatten tick. Rows land in `data/pm-quote/dqd_stream_observe.jsonl` with `gate=true` and `frame_path=null`. Missing stream env → gate unavailable.

**AF score observe:** `af_observe.sample_once` on the **same +0s / 5s** clock, but only on DOM `in_play` ticks, until aligned buy (or for the full 120s if never bought). Writes `data/pm-quote/af_observe.jsonl`. Enabled by default when `apifootball_key` is set (`QUOTE_AF_OBSERVE=0` to disable). Never buys by itself. Fixture ids are cache-only from `apifootball-bridge`.

**Animation source (纳米 tracker URL only):** one shared Chromium; in-play paired fixtures are pre-opened from `animation_live` (`https://tracker.namitiyu.com/zh/football?profile=…&id=<nami_id>`). A goal **evaluates** `.pop-box` / `.center-box` on that tab (same match reuses it). Cap `QUOTE_DOM_POOL_MAX` (default 24). Warming is on by default (`QUOTE_DOM_WARM`, interval `QUOTE_DOM_WARM_INTERVAL_S` default 10s, open timeout `QUOTE_DOM_WARM_OPEN_TIMEOUT_S` default 3s). While an open waits for the animation root, pending DOM reads are drained so a goal sample is not stuck behind warm. No MQTT ball-xy, no `page.screenshot`. Fixtures with no `animation_live` fall back to the DQD page iframe. Missing animation → timeout, no buy.

Smoke: `python3 .cursor/skills/polymarket-quote/scripts/smoke_pitch_gate.py`, `.../smoke_locked_sweep.py`, `.../smoke_t10_scan.py`, `.../smoke_half_settle.py`, `.../smoke_pitch_gate_dom.py`, `.../smoke_dom_page_pool.py`, `.../smoke_af_observe.py`, `.../smoke_book_context_observe.py`, `.../smoke_prematch_odds.py`, `python3 .cursor/skills/dongqiudi-match/scripts/smoke_dqd_live.py`.

**Gate source: DOM only**

`gate_source()` is always `dom`. `judge_dom()` applies `IN_PLAY_TOKENS` / `STOPPED_TOKEN_MAP` to overlay text. VAR veto and score matching are unchanged. `DomPagePool` keeps **one Chromium** and **one tab per in-play match**. Frozen page → `stale_page`. Clock must not be parsed as a scoreline.

**Pitch Gate board (System Main)**

`frontend/pitch-gate-board` on **:8791** reads `dqd_stream_observe.jsonl` + `af_observe.jsonl` (+ Odds grade on frames). Groups by goal `event_key` (including `dqd_ts`). Shows DOM `play_state` text, AF trail, Odds Grade chip. No screenshots, no ball-xy pitch.

**Live Score API observe (trial, observe-only)**

When `.env` has `LIVESCORE_API_KEY` + `LIVESCORE_API_SECRET`, DQD-reversal may resolve the fixture via `scores/live.json`, then pull **raw** `matches/events.json` (GOAL/score) and `commentary/events.json` (VAR if package allows; errors kept raw) into `data/pm-quote/livescore_observe.jsonl`. DQD→LSA id map cached in `livescore_match_map.json`. Trial is not expected to expose `KICK_OFF`. Does **not** gate buys or flatten.

**System Main** (`python3 frontend/run_main.py`) spawns `pm_quote watch` with default **goals=live / ft=live** (+ repo `.env`). Pitch-gate: first tick @**+0s**, then every 5s until **120s** → same-tick DOM∧AF → one buy; buy stops AF+DOM. Logs: `data/pm-quote/watch.log`.

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
