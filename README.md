# dqdhook

Dongqiudi ↔ Polymarket soccer pipeline: live scores from 懂球帝, fixture pairing, CLOB quoting, and (next round) screenshot-gated trading.

## What it does

```text
dongqiudi-match ──┐
                  ├── match-bridge (in-process · memory queue)
polymarket-soccer ┘         │
                            ▼
              polymarket-quote watch
                    │
                    ├── dry CLOB quote / trade plans
                    └── optional DQD stream + pitch-state observe
```

| Skill | Role |
|---|---|
| `dongqiudi-match` | DQD match list / scores / score-change |
| `polymarket-soccer` | Polymarket Gamma soccer fixtures |
| `match-bridge` | Align DQD ↔ PM; emit `score_change` / `match_finished` |
| `polymarket-quote` | Settle markets, quote CLOB, dry-run plans (live paused) |
| `pitch-state` | Screenshot resume-play judge (next-round gate) |

**System Main** (`frontend/run_main.py`) is the **only** process you start. It boots:

- Hub :8790
- Boards (UI): Dongqiudi :8787 · Polymarket :8788 · Match Bridge :8789
- `pm_quote watch`, which **owns in-process match-bridge** (memory `event_queue` → dry quote)

Do **not** start `pm_quote`, boards as skill hosts, or a second `run_main` (hub exits if :8790 is taken). Bridge-board Start is blocked while quote owns the bridge (`data/bridge/.inproc_owner`).

AF referee / Odds-Bet365 third confirmation / AF Bridge board have been **removed**. Live CLOB buys are **paused** until the DQD + screenshot two-confirm gate lands.

## Quick start (dry-run)

```bash
pip install -r .cursor/skills/polymarket-quote/requirements-trade.txt

# Repo-root .env (never commit):
#   PRIVATE_KEY, FUNDER, …   — only needed when live is re-enabled later
#   PM_PROXY (optional)      — default http://127.0.0.1:1082
#   QUOTE_DQD_STREAM_OBSERVE=1 / QUOTE_PITCH_STATE=1  — optional observe

python3 frontend/run_main.py --no-browser

open http://127.0.0.1:8790/
```

Stop: `Ctrl-C` / `kill` the `run_main` process, or `POST http://127.0.0.1:8790/api/stop`.

### Dry-run checklist

1. Hub pills: Quote up · Trade dry (live paused) · Boards 3/3.
2. Bridge board shows paired matches.
3. On a DQD goal-up: `data/pm-quote/watch.log` shows quote lines (no AF/Odds waits).
4. Planned fills land in `data/pm-quote/trades.jsonl` with `"live": false` / `status: dry_run`.
5. DQD reversal cancels rest orders but does **not** auto-flatten this round.

## Latency path (hot vs cold)

| Hot path | Cold path (async disk) |
|---|---|
| DQD poll → rematch → memory `event_queue` | `data/bridge/events.jsonl`, `matches.json`, `prev_*` |
| One CLOB `/books` + dry plan | `trades.jsonl`, quote snapshots |

Env: `MAIN_BRIDGE_INPROC=0` falls back to bridge-board file wake (not recommended for latency).

## Trading modes

**This round: live is forced off** (`--live` / `QUOTE_LIVE` / per-channel modes are ignored).

```bash
python3 frontend/run_main.py --no-browser
python3 frontend/run_main.py --take-depth walk --max-usdc 5 --no-browser
python3 frontend/run_main.py --no-trade
```

| Flag | Meaning |
|---|---|
| `--take-depth top\|walk` | Fill from best level or walk the book |
| `--max-usdc` / `--max-shares` | Size caps |
| `--no-trade` | Quote only (no executor) |
| `--live` / `--goals-mode` / `--ft-mode` | Ignored (live paused) |

## Logs & data

| Path | Purpose |
|---|---|
| http://127.0.0.1:8790/ | System Main hub |
| `data/pm-quote/watch.log` | Quote / trade stdout |
| `data/pm-quote/trades.jsonl` | Dry-run plans (+ historical live) |
| `data/bridge/events.jsonl` | Durable bridge events |
| `data/pm-quote/dqd_stream_observe.jsonl` | Optional goal screenshots |

## Static checks

```bash
python3 -c "from pathlib import Path; import py_compile; \
  [py_compile.compile(str(p), doraise=True) for p in Path('.cursor/skills').rglob('*.py')] ; \
  [py_compile.compile(str(p), doraise=True) for p in Path('frontend').rglob('*.py')]"

python3 .cursor/skills/match-bridge/scripts/smoke_ft_period.py
python3 .cursor/skills/match-bridge/scripts/smoke_match_hardening.py
python3 .cursor/skills/polymarket-quote/scripts/smoke_trade_modes.py
python3 .cursor/skills/polymarket-quote/scripts/smoke_post_goal_sampler.py
```

## Skill entrypoints (debug only)

```bash
python3 .cursor/skills/polymarket-quote/scripts/pm_quote.py once --from-bridge --json
```

Skill docs: `.cursor/skills/*/SKILL.md`.

## Layout

```text
.cursor/skills/     # Agent skills (scripts + SKILL.md)
frontend/           # System Main hub + boards
data/               # Runtime snapshots / jsonl (gitignored)
```

## Safety

- Live CLOB is paused this round; dry-run only.
- Do not commit `.env` or private keys.
- Prefer small `--max-usdc` while validating.
