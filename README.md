# dqdhook

Dongqiudi ↔ Polymarket soccer pipeline: live scores from 懂球帝, fixture pairing, API-Football goal confirmation, CLOB quoting, and optional in-process trading.

## What it does

```text
dongqiudi-match ──┐
                  ├── match-bridge (in-process · memory queue)
polymarket-soccer ┘         │
                            ▼
              polymarket-quote watch
                    │
                    ├── AF referee → apifootball-bridge (events HTTP)
                    └── CLOB books + dry-run / live FAK
```

| Skill | Role |
|---|---|
| `dongqiudi-match` | DQD match list / scores / score-change |
| `polymarket-soccer` | Polymarket Gamma soccer fixtures |
| `match-bridge` | Align DQD ↔ PM; emit `score_change` / `match_finished` |
| `apifootball-bridge` | DQD→AF fixture map + `/fixtures/events` (goal referee) |
| `polymarket-quote` | AF-confirm goals, settle markets, quote CLOB, dry-run or live FAK |

**System Main** (`frontend/run_main.py`) is the **only** process you start. It boots:

- Hub :8790
- Boards (UI): Dongqiudi :8787 · Polymarket :8788 · Match Bridge :8789 · AF Bridge :8791
- AF bridge **watch** via board API (warm fixture cache)
- `pm_quote watch`, which **owns in-process match-bridge** (memory `event_queue` → AF referee → quote/trade)

Do **not** start `pm_quote`, boards as skill hosts, or a second `run_main` (hub exits if :8790 is taken). Bridge-board Start is blocked while quote owns the bridge (`data/bridge/.inproc_owner`).

## Quick start (dry-run)

```bash
# Optional trading deps (needed for live / position checks; dry-run plans work without posting)
pip install -r .cursor/skills/polymarket-quote/requirements-trade.txt

# Repo-root .env (never commit):
#   apifootball_key / API_FOOTBALL_KEY   — required for goal AF confirm
#   PRIVATE_KEY, FUNDER, …               — only for --live / live modes
#   PM_PROXY (optional)                  — default http://127.0.0.1:1082

# Default: goals + FT both dry-run, AF referee on
python3 frontend/run_main.py --no-browser

# Hub
open http://127.0.0.1:8790/
```

Stop: `Ctrl-C` / `kill` the `run_main` process, or `POST http://127.0.0.1:8790/api/stop`.

### Dry-run checklist

1. Hub pills: Quote up · Trade dry-run · AF watch · Boards 4/4.
2. Bridge board shows paired matches (files written async by in-process bridge).
3. AF board shows mapped fixtures after watch sync.
4. On a DQD goal-up: `data/pm-quote/watch.log` shows `af-referee → CONFIRMED` then quote lines with `books=once` / `latency_ms=…`.
5. Planned fills land in `data/pm-quote/trades.jsonl` with `"live": false` / `status: dry_run` (no CLOB post).
6. Reversals are ignored for new buys when AF referee is on (no flatten from DQD alone).

## Latency path (hot vs cold)

| Hot path | Cold path (async disk) |
|---|---|
| DQD poll → rematch → memory `event_queue` | `data/bridge/events.jsonl`, `matches.json`, `prev_*` |
| AF HTTP confirm (in-process lib) | AF burst dirs, `af_confirmed_scores.json` |
| One CLOB `/books` + FAK / dry plan | `trades.jsonl`, quote snapshots |

Events are appended to jsonl **before** `prev_*` so a crash cannot permanently drop a goal. Quote serializes trade/flatten with a lock.

Env: `MAIN_BRIDGE_INPROC=0` falls back to bridge-board file wake (not recommended for latency). `MAIN_AF_WATCH=0` skips hub auto-start of AF watch. `QUOTE_AF_REFEREE=0` / `--no-af-referee` disables the goal gate.

## Trading modes

Default is **dry-run** for both goal and full-time signals:

```bash
python3 frontend/run_main.py --no-browser
python3 frontend/run_main.py --take-depth walk --max-usdc 5 --no-browser

# Both channels live (real USDC)
python3 frontend/run_main.py --live --max-usdc 2

# Split channels
python3 frontend/run_main.py --goals-mode dry --ft-mode live --max-usdc 1
python3 frontend/run_main.py --goals-mode live --ft-mode dry --max-usdc 1

# Quote only
python3 frontend/run_main.py --no-trade
```

| Flag | Meaning |
|---|---|
| `--live` | Both `score_change` and `match_finished` post real CLOB orders |
| `--goals-mode dry\|live` | Mode for goal events; overrides `--live` for that channel |
| `--ft-mode dry\|live` | Mode for full-time; overrides `--live` for that channel |
| `--take-depth top\|walk` | Fill from best level or walk the book |
| `--max-usdc` / `--max-shares` | Size caps (defaults 5 / 25) |
| `--no-af-referee` | Skip AF goal confirm (not for production goals) |

Env: `QUOTE_LIVE`, `QUOTE_GOALS_MODE`, `QUOTE_FT_MODE`, `QUOTE_TAKE_DEPTH`, `QUOTE_MAX_USDC`, `QUOTE_TRADE=0`, `QUOTE_AF_REFEREE`, …

## Logs & data

| Path | Purpose |
|---|---|
| http://127.0.0.1:8790/ | System Main hub |
| `data/pm-quote/watch.log` | Quote / AF / trade stdout |
| `data/pm-quote/trades.jsonl` | Dry-run plans + live posts |
| `data/pm-quote/af_confirmed_scores.json` | AF-confirmed score baseline |
| `data/bridge/events.jsonl` | Durable bridge events |
| `data/apifootball/fixture_cache.json` | DQD→AF fixture map |
| `data/dqd-probe/af-latency/bursts/` | AF events artifacts (async) |

## Static checks

```bash
# Compile all skill + frontend Python
python3 -c "from pathlib import Path; import py_compile; \
  [py_compile.compile(str(p), doraise=True) for p in Path('.cursor/skills').rglob('*.py')] ; \
  [py_compile.compile(str(p), doraise=True) for p in Path('frontend').rglob('*.py')]"

# Smokes (no live network required for most)
python3 .cursor/skills/apifootball-bridge/scripts/smoke_af_bridge.py
python3 .cursor/skills/match-bridge/scripts/smoke_ft_period.py
python3 .cursor/skills/match-bridge/scripts/smoke_match_hardening.py
python3 .cursor/skills/polymarket-quote/scripts/smoke_af_referee.py
python3 .cursor/skills/polymarket-quote/scripts/smoke_latency_path.py
python3 .cursor/skills/polymarket-quote/scripts/smoke_trade_modes.py
python3 .cursor/skills/polymarket-quote/scripts/smoke_post_goal_sampler.py
```

## Skill entrypoints (debug only)

Prefer System Main. Direct CLIs are for one-off debugging — do not run them alongside a live hub.

```bash
python3 .cursor/skills/polymarket-quote/scripts/pm_quote.py once --from-bridge --json
python3 .cursor/skills/apifootball-bridge/scripts/af_bridge.py status --json
```

Skill docs: `.cursor/skills/*/SKILL.md`.

## Layout

```text
.cursor/skills/     # Agent skills (scripts + SKILL.md)
frontend/           # System Main hub + boards
data/               # Runtime snapshots / jsonl (gitignored)
```

## Safety

- Default is dry-run; `--live` / `live` modes spend real USDC.
- Goal trades wait for API-Football confirmation; DQD reversals do not open new buys.
- Do not commit `.env` or private keys.
- Prefer small `--max-usdc` while validating.
