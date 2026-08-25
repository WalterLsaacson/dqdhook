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

Only rows with **`net_edge ≥ min_net`** (default **0.00475** USDC/share ≈ ask≤**0.995**) go to `opportunities.jsonl`.  
Quotes still record all tokens; dust / fee-insufficient edges stay out of oppo.  
CLI: `--fee-rate`, `--min-net`.

## In-process trading (low latency)

After `flag_misprice` returns true inside `quote_tokens`, `TradeExecutor` runs **in the same process** on the **CLOB worker thread** (watch tick only enqueues the event payload; it does not wait for `/books` or rest posts).

**Gate prewarm** (`QUOTE_GATE_PREWARM`, default on): on pitch-gate `start_gate` (buy path), a background thread warms Gamma catalog + refreshes `POST /books` every `QUOTE_GATE_PREWARM_INTERVAL_S` (default 3s). On BUY, `quote_bridge_event` prefers that snapshot when age ≤ `QUOTE_GATE_PREWARM_MAX_AGE_S` (default 4s); otherwise fetches. Cuts ~0.5–1s books RTT after BUY — does **not** shorten DOM/AF/shot wait.

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

Price guard: skip when best ≤0.01 or >0.995 unless `--allow-extreme-prices`.  
`buy_win` floor: skip (still append `trades.jsonl` with `skip_reason=buy_price_below_min=…`) when `best_ask < --min-buy-price` (default **0.6**; **0** = off). Env: `QUOTE_MIN_BUY_PRICE`. **Pitch-gate and FT buys skip this floor.**

**Size policy (`.env`)**: hard caps `QUOTE_MAX_USDC` / `QUOTE_MAX_SHARES` (default 1/25). `QUOTE_SIZE_TIERS=0.98:1` means **ask ≥ 0.98 → $1**, else **$1** (hard-capped); **shares scale with that usdc**. Concurrent open cost capped by `QUOTE_MAX_OPEN_USDC` (default **1000**). Floor `QUOTE_SIZE_FLOOR_USDC` (default 1) and the $1 marketable bump are **skipped when `trade_context.pitch_gate=true`**. Fee/`min_net` and `QUOTE_MAX_USDC` still apply.  
Idempotency: `event_key|token_id|trade` — successful live posts are skipped on restart.  
SDK: `py-clob-client-v2` (see `requirements-trade.txt`). Env: `PRIVATE_KEY`, `FUNDER`, `SIGNATURE_TYPE`, `CHAIN_ID`, `CLOB_HOST`.

Modules: `trade_settings.py`, `clob_trader.py`, `fill_planner.py`, `trade_executor.py`, `score_reversal.py`, `pitch_gate.py`.

**Pitch-gate buy (DOM first; same-tick in_play ∧ AF ∧ shot; stop AF after buy)**

1. DQD goal-up + Polymarket paired → `PitchGateCoordinator.start_gate` (no immediate `_quote_one`).
2. Sample DOM every **5s** for up to **120s**, first tick at **+0s**. AF starts only when this goal has latched a 射门 **and** the current tick is DOM `in_play`; then later ticks sample AF+DOM until buy / timeout / cancel. Odds grade on every DOM tick (including before AF arms).
3. Buy when **this tick** has DOM `play_state==in_play` **and** AF `ok && score_match` **and** this goal latched a 射门 since t0 (pop contains `射门`, or DOM marks `ball`/`net`). 射门 is **not** an `in_play` token. Then one `_quote_one` with `trade_context.pitch_gate=True`. Then **stop AF** (quota) and **keep DOM** until the original 120s timeout (`aligned_buy` when the trail ends). AF rate-limit / error fails closed for that tick. `in_play` without a shot logs `WAIT_SHOT` and **does not call AF**. Buy-side AF∨DOM is **rejected** (`design-af-dom-or-gate.md`).
4. Any tick with **VAR before buy** → **permanent no-buy** (`mode=pitch_gate_var_veto`), even if a shot was seen. VAR after buy is logged on the DOM trail only.
5. Never aligned before timeout → `mode=pitch_gate_timeout`, mark seen, no buy.
6. DQD reversal → in-process bridge **hooks emit** to `cancel_match` immediately; rest cancel is queued onto the CLOB worker (`rest_cancel` priority beats idle housekeep). The worker revokes **submitted `event_key`s** for that match, not the whole `match_id` (a later non-opening re-award with a new ts can still quote). **Opening** 0-0→1-0 / 0-0→0-1 skips `start_gate` when that transition was already reversed on the match, or when the DQD clock is ≥90' (`pitch_gate_reversal_risk_skip`). Ordinary ~35' opening goals stay full size. Quote tick also **pre-pass** reversals **before** `start_gate`. `block_inverted_goal` keys on the goal stem + reverse ts: older/same-ts 0-1→1-1 is blocked; a re-awarded non-opening goal with a newer ts is not. Drain gate results **after** event handling so same-tick reversals revoke queued buys. If **open lots exist**, start a 5s AF∨DOM confirm trail (never `_quote_one`); on first score match → flatten and **stop the trail** (no DOM drag to 120s).
7. Requires `QUOTE_DQD_STREAM_OBSERVE=1`; else `pitch_gate_unavailable`. No screenshots / OCR / JPEG.
8. FT path remains immediate quote (default live).
9. **Rest fallback**: when **`QUOTE_REST_ENABLED=1`**, if pitch-gate WIN has no FAK fill, post a limit bid at **min(0.995, book tick grid)** (`GTC` by default — stays until DQD reversal, FT, or manual cancel), including one-sided bid books with no ask. On **0.01** soccer ticks that is **0.99**; on published **0.001** ticks it stays **0.995**. Do not invent `tick=0.001` on a 0.01 book (CLOB rejects it). Set **`QUOTE_REST_EXPIRE_S>0`** to use `GTD` instead. Size is **`QUOTE_REST_USDC` (default $5)** so the bid clears the CLOB 5-share floor. Pitch-gate rest is **not** clipped by `QUOTE_MAX_OPEN_USDC`.

**Score reversal / disallowed goal**

- Bridge emits `score_change` with `is_reversal=true` when either side’s score drops.
- Goal-ups wait for pitch-gate; FT quotes immediately (default live). Events older than `QUOTE_FT_MAX_AGE_S` (default **900**) are skipped; FT once-per-`match_id` via `cursor.processed_ft_match_ids`.
- DQD reversal cancels rest and open pitch-gate sessions, and revokes undrained buys. **Open lots** restart the 5s AF+DOM+Odds trail against the **post-reverse** score. Flatten on first AF **or** DOM **board score** (`.center-box`, not `in_play`; celebration/VAR/stale clock still count), then **stop the 5s trail immediately** (buy path keeps DOM to 120s after buy; reverse does not). AF error alone does not flatten. Tracker open failure still polls AF. 120s neither confirms → **hold**. No lots → cancel only.
- Raw DQD reverse without AF/DOM confirm still uses `QUOTE_GATE_PROTECT_S` (default **300s**, `0` disables that unconfirmed path). **Confirmed** `flatten_or` sells pitch-gate lots at any age. FT lots stay on `ft_reversal_vs_entry`.
- Live flatten FAK-sells floored shares with entry×80% floor; dry lots never CLOB-sell.
- Rebuild closes zombie opens when known FT already undoes entry (`stale_ft_reversal`).
- Dry-run logs `flatten_dry_run`. Open lots: `data/pm-quote/open_positions.json`.

**Odds / Bet365 (observe only)**

`book_context_observe.sample_gate_tick` is kicked off on the same DOM clock in a background thread so HTTP cannot stall AND buy or OR flatten. Writes `data/pm-quote/book_context_observe.jsonl` (Grade A/B/C). **Does not** change `QUOTE_MAX_USDC` or place orders. No 3s timers. Requires `ODDS_API_IO_KEY`.

**Prematch books:** when a paired `matches.json` row first sits inside **`QUOTE_PREMATCH_LEAD_S` (default 1800s / 30 min) before kickoff**, take **one** snapshot of all Bet365 + 1xbet markets into `data/pm-quote/prematch_odds.jsonl`. Not a repeating poll. Restart will not recapture a match that already has an `ok` row. Disable with `QUOTE_PREMATCH_ODDS=0`. Same `/odds/multi` coalesce as the gate.

**DQD stream / DOM gate**

Pitch-gate drives DOM reads every **5s** for up to **120s** after a paired goal (first tick @ **+0s**; flatten / cancel / timeout stop the session). Aligned buy **stops AF** but **does not** stop DOM. Reverse confirm **stops AF and DOM** on the flatten tick. Rows land in `data/pm-quote/dqd_stream_observe.jsonl` with `gate=true` and `frame_path=null`. Missing stream env → gate unavailable.

**AF score observe:** `af_observe.sample_once` on the **same +0s / 5s** clock until aligned buy (or for the full 120s if never bought). Writes `data/pm-quote/af_observe.jsonl`. Enabled by default when `apifootball_key` is set (`QUOTE_AF_OBSERVE=0` to disable). Never buys by itself. Fixture ids are cache-only from `apifootball-bridge`.

**Animation source (纳米 tracker URL only):** one shared Chromium; in-play paired fixtures are pre-opened from `animation_live` (`https://tracker.namitiyu.com/zh/football?profile=…&id=<nami_id>`). A goal **evaluates** `.pop-box` / `.center-box` on that tab (same match reuses it). Cap `QUOTE_DOM_POOL_MAX` (default 24). Warming is on by default (`QUOTE_DOM_WARM`, interval `QUOTE_DOM_WARM_INTERVAL_S` default 10s, open timeout `QUOTE_DOM_WARM_OPEN_TIMEOUT_S` default 3s). While an open waits for the animation root, pending DOM reads are drained so a goal sample is not stuck behind warm. No MQTT ball-xy, no `page.screenshot`. Fixtures with no `animation_live` fall back to the DQD page iframe. Missing animation → timeout, no buy.

Smoke: `python3 .cursor/skills/polymarket-quote/scripts/smoke_pitch_gate.py`, `.../smoke_pitch_gate_dom.py`, `.../smoke_dom_page_pool.py`, `.../smoke_af_observe.py`, `.../smoke_book_context_observe.py`, `.../smoke_prematch_odds.py`, `python3 .cursor/skills/dongqiudi-match/scripts/smoke_dqd_live.py`.

**Gate source: DOM only**

`gate_source()` is always `dom`. `judge_dom()` applies `IN_PLAY_TOKENS` / `STOPPED_TOKEN_MAP` to overlay text. VAR veto and score matching are unchanged. `DomPagePool` keeps **one Chromium** and **one tab per in-play match**. Frozen page → `stale_page`. Clock must not be parsed as a scoreline.

**Pitch Gate board (System Main)**

`frontend/pitch-gate-board` on **:8791** reads `dqd_stream_observe.jsonl` + `af_observe.jsonl` (+ Odds grade on frames). Groups by goal `event_key` (including `dqd_ts`). Shows DOM `play_state` text, AF trail, Odds Grade chip. No screenshots, no ball-xy pitch.

**Live Score API observe (trial, observe-only)**

When `.env` has `LIVESCORE_API_KEY` + `LIVESCORE_API_SECRET`, DQD-reversal may resolve the fixture via `scores/live.json`, then pull **raw** `matches/events.json` (GOAL/score) and `commentary/events.json` (VAR if package allows; errors kept raw) into `data/pm-quote/livescore_observe.jsonl`. DQD→LSA id map cached in `livescore_match_map.json`. Trial is not expected to expose `KICK_OFF`. Does **not** gate buys or flatten.

**System Main** (`python3 frontend/run_main.py`) spawns `pm_quote watch` with default **goals=live / ft=live** (+ repo `.env`). Pitch-gate: first tick @**+0s**, then every 5s until **120s** → same-tick DOM∧AF → one buy; stop AF, keep DOM to timeout. Logs: `data/pm-quote/watch.log`.

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
