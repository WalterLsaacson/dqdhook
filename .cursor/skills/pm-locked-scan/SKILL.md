---
name: pm-locked-scan
description: >-
  Lists Polymarket soccer games from the last 48h that have finished but
  markets are still open, resolves regulation (90'+stoppage) full-time and
  half-time scores, and finds locked-WIN tokens that still have CLOB asks.
  Independent of polymarket-quote trading. Use when the user wants a
  post-FT unsettled-market ask sweep, leftover WIN sells, or a locked-WIN
  scan that must not count extra time.
---

# PM locked-WIN ask scan

Standalone scan. **Does not** start quote watch by itself. When System Main
(`python3 frontend/run_main.py`) is running, `pm_quote watch` runs this scan
every 1 hour (24h lookback) and FAK-walks leftover WIN asks ≤ **0.995**.
The scan itself is a **child process**; quote watch only submits short FAKs.

Regulation only: **90 minutes + stoppage**. Extra time and penalties are
excluded (same as Polymarket soccer contract text).

## Quick start

```bash
python3 .cursor/skills/pm-locked-scan/scripts/pm_locked_scan.py --json
python3 .cursor/skills/pm-locked-scan/scripts/pm_locked_scan.py --hours 48 --league ptc --json
python3 .cursor/skills/pm-locked-scan/scripts/pm_locked_scan.py --tradeable --json
python3 .cursor/skills/pm-locked-scan/scripts/pm_locked_scan.py --from-snapshot --json
python3 .cursor/skills/pm-locked-scan/scripts/pm_locked_scan.py --hours 24 --tradeable --require-af --json
```

`--tradeable` keeps asks ≤ **0.995** (quote taker cap). Default `--max-ask 1.0`
keeps any remaining sell, including 0.999 walls.

`--json` still prints per-match progress on stderr unless `-q`.

`--require-af`: settle only from API-Football regulation (`score.fulltime` + HT).
Skip matches that would have used a Dongqiudi league fallback. The hourly
quote sweep always passes this flag.

Writes `data/pm-locked-scan/latest.json`.

Unknown `--league` codes raise (same as `pm_lib.load_matches`), they do not
silently scan zero rows.

## What it does

1. List Gamma soccer events that are **not closed**, not `live`, kickoff in
   the last N hours, and look finished (`ended`, period FT/AET/PEN, or
   kickoff + 100 minutes as a *candidate* when Gamma `ended` is stale).
   Wall clock alone does not confirm a result — a regulation score must
   resolve, or the match is skipped.
2. Pull main + More Markets + Exact Score catalogs.
3. Resolve **regulation FT + HT** (never Gamma’s `score` field — it can stick at 0-0):
   - API-Football `score.fulltime` + `score.halftime` from local
     `data/apifootball/date_fixtures/`. Rank only fixtures where that extract
     succeeds (a same-day NS / youth-team name match cannot hide a finished row).
     On AET, if `fulltime` equals live `goals` and `extratime` is an increment
     (e.g. 2-3 + 0-1), subtract extra time so settlement is 90' (2-2).
   - else Dongqiudi `data/snapshot.json` for **league** games when `period=FT`
     and the clock is not past 90' — **research CLI only**. The hourly quote
     sweep always passes `--require-af` and skips that fallback (error
     `no_af_regulation_score`). Cups / knockout (UCL, domestic cups, …)
     are not taken from DQD: after AET, DQD often stays `period=FT` with the
     120' score. If AF has the same matchup live or in ET/pen without a usable
     90' score, DQD is not used either.
4. Settle tokens with quote helpers (moneyline / spreads / totals / BTTS / exact).
5. `POST /books` on WIN tokens; report leftover asks.

Skip a match when FT or HT is missing. Do not invent a 0-0.

JSON `scored` is matches with a regulation score. Hits go in `results`;
scored matches with no leftover asks go in `scored_no_asks`. One Gamma
series failure is recorded in `list.league_errors` and does not abort the
rest of the scan.

## `--refresh-af`

Default is **read-only**: use whatever is already in
`data/apifootball/date_fixtures/`. That cache is also written by
apifootball-bridge watch.

`--refresh-af` force-refetches those calendar dates via the API-Football
key, **overwrites the shared date files**, and consumes free-plan quota.
Do not pass it on a schedule that overlaps AF watch. Prefer leaving it off
unless the date cache is empty or you know it is stale.

## Reused (read-only)

| Source | Use |
|---|---|
| `polymarket-soccer` `pm_lib` | Gamma soccer catalog + events + proxy |
| `polymarket-quote` `quote_lib` | Market enrich, settlement, CLOB books |
| `match-bridge` `bridge_lib` | Team fuzzy match + side swap |
| `apifootball-bridge` | Date-fixture cache + `score.fulltime` |
| `data/snapshot.json` | Dongqiudi FT / half scores (leagues only) |
| `data/polymarket/snapshot.json` | Optional `--from-snapshot` list |

## Not reused by the scan CLI

Quote watch, pitch-gate, T+10, rest, flatten. The **watch scheduler** (not
this CLI) posts leftover FAK via `postft_sweep` into `trades.jsonl`.

Hand-run the scan only:

```bash
python3 .cursor/skills/pm-locked-scan/scripts/pm_locked_scan.py --hours 24 --tradeable --json
```

## Smokes

```bash
python3 .cursor/skills/pm-locked-scan/scripts/smoke_locked_scan.py
```
