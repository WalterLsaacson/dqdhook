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
10. **Rest fallback**: when **`QUOTE_REST_ENABLED=1`**, if pitch-gate WIN has no FAK fill (no ask / not misprice), post a limit bid @ **0.99** (`GTD`, expire **`QUOTE_REST_EXPIRE_S`** default **3600s**). Size follows **`QUOTE_MAX_USDC`** (default **$1**). Logged as `rest_dry_run` / `rest_posted`; DQD reversal still cancels these rests.

**Score reversal / disallowed goal**

- Bridge emits `score_change` with `is_reversal=true` when either side’s score drops.
- Goal-ups wait for pitch-gate; FT quotes immediately (default live). Events older than `QUOTE_FT_MAX_AGE_S` (default **900**) are skipped; FT once-per-`match_id` via `cursor.processed_ft_match_ids`.
- DQD reversal cancels rest orders and open pitch-gate sessions, and revokes undrained buys. If that reverse undoes a goal that already reached **in_play**, it then starts an **observe-only** gate on the reversal `event_key` (same DOM +5s/5s/150s and AF 90s cadence; never `_quote_one`). Pitch Gate board shows a separate 「回撤观察」 card so AF/DOM can be compared against the post-reversal DQD score. Reversals that never hit in_play are not observed.
- **Post-buy protection window**: lots carry `pitch_gate` + `opened_at`. A DQD reversal that undoes the entry score flattens gate lots opened within **`QUOTE_GATE_PROTECT_S`** (default **300s**, `0` disables) as `gate_protect_reversal`. Lots outside the window, and non-gate (FT) lots, stay deferred to the FT `ft_reversal_vs_entry` path — by then the token is near zero, so that exit frees budget rather than recovering value. Top-ups keep the first `opened_at` so the window never extends. The window is bounded because late DQD score drops are more often data noise than real VAR calls, and flattening on noise sells into a bad book for nothing.
- Live flatten FAK-sells floored shares with entry×80% floor; dry lots never CLOB-sell.
- Rebuild closes zombie opens when known FT already undoes entry (`stale_ft_reversal`).
- Dry-run logs `flatten_dry_run`. Open lots: `data/pm-quote/open_positions.json`.

**Post-goal price samples (research)**

After a `score_change` that successfully `buy_win`s (dry_run or posted), watch writes sample 0 from that quote and background-requotes **only those tokens** at +10s…+50s (6 total) into `data/pm-quote/post_goal_samples.jsonl`. Follow-up jobs run **in parallel** (wall-clock `elapsed_s`). Each follow-up re-reads the match score, recomputes settlement/lock, and sets `reversal_seen` only when a later `score_change` has `prev == score_at_t0` and a lower total. No `maybe_trade` on follow-ups.

**DQD stream / pitch-state (gate + research)**

Pitch-gate drives captures every **5s** for up to **150s** after a paired goal (first frame @ **+5s**; ≥5 frames; continues after first `in_play` buy). Rows still land in `data/pm-quote/dqd_stream_observe.jsonl` / `dqd_stream_frames/` with `gate=true`. Pitch-state judges write `data/pm-quote/pitch_state_judge.jsonl` (and JPEG sidecars). Missing stream/pitch env → gate unavailable (no buy for that goal).

**AF score observe (research only):** on the same goal (or reversal) `t0`, `af_observe.py` polls API-Football events on the **same +5s / 5s clock** but only for **90s**, writing `data/pm-quote/af_observe.jsonl`. Reversal rows carry `is_reversal` / `observe_only`. Enabled by default when `apifootball_key` is set (`QUOTE_AF_OBSERVE=0` to disable). Never buys. Fixture ids are cache-only from `apifootball-bridge` (`MAIN_AF_WATCH` via af-bridge-board **:8792**).

**Animation source (纳米数据):** the gate screenshots the nami tracker directly rather than the DQD match page. `match_list` already returns `animation_live` (`https://tracker.namitiyu.com/zh/football?profile=…&id=<nami_id>`) on every row, so `map_match` keeps it and `dqd_live.discover_live_surface` reads it out of `data/snapshot.json` (memoized on mtime) and returns `surface="animation"` with the tracker as `page_url` — no extra request, and no dependency on the DQD page DOM. Rows carry `nami_id`.

Fixtures with no `animation_live` fall back to the previous behaviour (magicball probes, then the DQD page). Playwright screenshots `.football-animate` on the tracker, still falling back to `iframe.md-anim-iframe` / `video` for the DQD page. Measured ~4.4s per capture vs ~5.0s via the DQD page.

Two things this does **not** fix: nami serves 暂无动画 for fixtures it does not animate (the gate then just times out with no buy, which is the safe outcome), and `profile` is DQD's partner slot — it is re-read from `animation_live` on every tick rather than hardcoded.

Smoke: `python3 .cursor/skills/polymarket-quote/scripts/smoke_pitch_gate.py`, `.../smoke_nami_observe.py`, `.../smoke_af_observe.py`, `python3 .cursor/skills/dongqiudi-match/scripts/smoke_dqd_live.py`.

**Nami live feed observe (default on, ``QUOTE_NAMI_OBSERVE=0`` to disable)**

The tracker SPA streams live state over MQTT-on-WebSocket (`wss://trackermq.namitiyu.com/mqtt`, anonymous, topics `live/m1/<nami_id>` and `live/m1/<nami_id>/nft/zh`); payloads are undocumented protobuf. `nami_observe.py` subscribes while a match is under gate (plus 60s linger) and keeps the latest ball in memory. jsonl is written only when pitch-gate takes a DOM sample (`sample_i`, `elapsed_s`, `play_state`, classified xy). MQTT itself does not append.

Each DOM-sample row keeps `score_raw` (`"1-0-1-0"` = full/half), `ball_xy` plus classified `zone` (`center` / `box_l` / `box_r` / `third_*` / `mid`), `restart_center`, `in_box`, and `mqtt_age_s`. MQTT types without coordinates (nft commentary, 10102, …) keep the last 10101 `ball_xy` instead of wiping it. Pitch-gate board numbers those points (no full-course trail). **Still never buys or flattens.**

Ball motion during celebration/VAR is why coordinates stay observe-only: neither motion nor stillness maps cleanly onto play state. The numbered samples are for reviewing dual AF+DOM confirms that later reversed.

`harvest()` scans the wire format for score/xy shapes rather than pinning field numbers. Observed ~1.7 msgs/s across 5 live matches; `score_raw` agreed with the DQD score on every sample so far. There is also a REST side (`https://tracker-api.namitiyu.com/api/football/{static_detail,variable_detail,progress}?id=<nami_id>`, protobuf, requires `Origin: https://tracker.namitiyu.com`) that is not wired up.

**Gate source: DOM (default) vs OCR (legacy)**

`QUOTE_GATE_SOURCE` selects what the gate judges. Default `dom`; `ocr` restores the screenshot + PaddleOCR path, which stays in the tree but is off by default.

The animation renders its play state as text and CSS classes, which is exactly what OCR was recovering from the pixels: `.pop-box` → `"皮尔利斯 危险进攻"` (with `pop-box home|away`), `.center-box` → `"78:57 1 : 0"`, plus marker classes `possession-rect` / `attack-move` / `dangerous-attack-move`. `animation_rules.judge_dom()` applies the same `IN_PLAY_TOKENS` / `STOPPED_TOKEN_MAP` tables to that text, so VAR veto and score matching behave identically — only the input is exact instead of inferred.

`DomReader` keeps **one** page open per goal instead of launching Chromium per sample: opening costs ~1.7-2.4s (overlapped with the +5s first-frame delay) and each subsequent read is 2-8ms, against ~4.4s page load plus ~3.8s OCR before. A live end-to-end gate bought at t+5.01s with zero screenshots written. The reader finds `.football-animate` in the top frame (nami tracker) or inside `iframe.md-anim-iframe` (DQD match page), and gives up after `QUOTE_DOM_OPEN_TIMEOUT_S` (default 15s) with `dom_reader: no_animation_frame` → `unavailable`, never a buy.

Two failure modes are specific to this source and are guarded:

- **Frozen page.** A stalled tab keeps rendering its last state forever. `judge_dom` requires the `.center-box` clock to have advanced since the previous read, and the reader takes a clock baseline at open so even sample 0 is covered. No advance → `unclear` with `stopped_reason=stale_page`.
- **Clock read as score.** `"45:00"` must not become 45-0 — the same confusion that trips OCR. `parse_dom_center` splits on the board's spacing convention (score is `1 : 0`, clock is `78:57`) and falls back to consuming a leading clock before looking for a trailing score.

Ball coordinates stay observe-only (see Nami section above) — the DOM gate does not use them to infer restart-of-play.

Why the switch: a 24-frame sample across 3 live matches agreed 88%, and all 3 disagreements were OCR failing to read the scoreboard on a frame the DOM read exactly (the reverse never happened). Because the gate needs `in_play` **and** a matching board score, those misses did not open the gate wrongly — they delayed entry to a later 5s sample.

In `ocr` mode each frame still records its DOM readout beside the OCR verdict in `data/pm-quote/dom_vs_ocr.jsonl` for comparison.

**Pitch Gate board (System Main)**

`frontend/pitch-gate-board` on **:8791** (launched by `run_main`) reads `dqd_stream_observe.jsonl` + `af_observe.jsonl` + `nami_observe.jsonl` + `pitch_state_judge.jsonl`, groups by goal `event_key`, and shows each DOM frame with `play_state`, the AF score trail, and numbered Nami ball positions at those DOM samples. Read-only viewer (no Start/Stop).

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
