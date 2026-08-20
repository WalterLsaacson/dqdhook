# dqdhook

Dongqiudi ↔ Polymarket soccer pipeline: live scores from 懂球帝, fixture pairing, pitch-gated CLOB quoting/trading.

## What it does

```text
dongqiudi-match ──┐
                  ├── match-bridge (in-process · memory queue)
polymarket-soccer ┘         │
                            ▼
              polymarket-quote watch
                    │
                    ├── goal → 5 frames @ 5s → first in_play → one buy (keep capturing)
                    ├── FT → immediate quote (default live)
                    └── DQD stream + pitch-state frames
```

| Skill | Role |
|---|---|
| `dongqiudi-match` | DQD match list / scores / score-change |
| `polymarket-soccer` | Polymarket Gamma soccer fixtures |
| `match-bridge` | Align DQD ↔ PM; emit `score_change` / `match_finished` |
| `polymarket-quote` | Pitch-gate goals + FT quotes/trades (default live) |
| `pitch-state` | Screenshot resume-play judge (`in_play` gate) |

**System Main** (`frontend/run_main.py`) is the **only** process you start. It boots:

- Hub :8790
- Boards (UI): Dongqiudi :8787 · Polymarket :8788 · Match Bridge :8789 · Pitch Gate :8791
- `pm_quote watch`, which **owns in-process match-bridge** (memory `event_queue` → quote)

Do **not** start `pm_quote`, boards as skill hosts, or a second `run_main` (hub exits if :8790 is taken). Bridge-board Start is blocked while quote owns the bridge (`data/bridge/.inproc_owner`).

AF referee / Odds-Bet365 third confirmation / AF Bridge board have been **removed**. Buys use the **DQD goal + pitch-state `in_play`** two-confirm gate.

## Quick start

```bash
pip install -r .cursor/skills/polymarket-quote/requirements-trade.txt

# Repo-root .env (never commit):
#   PRIVATE_KEY, FUNDER, …   — required for live goals
#   PM_PROXY (optional)      — default http://127.0.0.1:1082
#   QUOTE_DQD_STREAM_OBSERVE=1 / QUOTE_PITCH_STATE=1  — required for pitch-gate

python3 frontend/run_main.py --no-browser

open http://127.0.0.1:8790/
```

Stop: `Ctrl-C` / `kill` the `run_main` process, or `POST http://127.0.0.1:8790/api/stop`.

### Checklist

1. Hub pills: Quote up · Trade goals:live ft:live · Boards 4/4.
2. Bridge board shows paired matches.
3. Pitch Gate board (`:8791`) shows each goal’s frames + `play_state`.
4. On a DQD goal-up: `watch.log` shows `pitch-gate → START`, then capture/judge, then `IN_PLAY` / trade (or `TIMEOUT` / `CANCEL`).
5. Fills land in `data/pm-quote/trades.jsonl` (`live: true` when goals live and posted).
6. DQD reversal cancels rest + pitch-gate; does **not** auto-flatten.

## Latency path (hot vs cold)

| Hot path | Cold path (async disk) |
|---|---|
| DQD poll → rematch → memory `event_queue` | `data/bridge/events.jsonl`, `matches.json`, `prev_*` |
| Pitch-gate → one CLOB `/books` + buy | `trades.jsonl`, quote snapshots |

Env: `MAIN_BRIDGE_INPROC=0` falls back to bridge-board file wake (not recommended for latency).

## Trading modes

Default: **goals=live**, **ft=live**. Override with flags / `QUOTE_GOALS_MODE` / `QUOTE_FT_MODE` / `QUOTE_LIVE`.

```bash
python3 frontend/run_main.py --no-browser
python3 frontend/run_main.py --take-depth walk --max-usdc 5 --no-browser
python3 frontend/run_main.py --goals-mode dry --ft-mode dry --no-browser
python3 frontend/run_main.py --no-trade
```

| Flag | Meaning |
|---|---|
| `--take-depth top\|walk` | Fill from best level or walk the book |
| `--max-usdc` / `--max-shares` | Size caps |
| `--no-trade` | Quote only (no executor) |
| `--live` / `--goals-mode` / `--ft-mode` | Live CLOB per channel |

## Logs & data

| Path | Purpose |
|---|---|
| http://127.0.0.1:8790/ | System Main hub |
| `data/pm-quote/watch.log` | Quote / trade stdout |
| `data/pm-quote/trades.jsonl` | Dry / live attempts |
| `data/bridge/events.jsonl` | Durable bridge events |
| `data/pm-quote/dqd_stream_observe.jsonl` | Pitch-gate frame metadata |

## Static checks

```bash
python3 -c "from pathlib import Path; import py_compile; \
  [py_compile.compile(str(p), doraise=True) for p in Path('.cursor/skills').rglob('*.py')] ; \
  [py_compile.compile(str(p), doraise=True) for p in Path('frontend').rglob('*.py')]"

python3 .cursor/skills/match-bridge/scripts/smoke_ft_period.py
python3 .cursor/skills/match-bridge/scripts/smoke_match_hardening.py
python3 .cursor/skills/polymarket-quote/scripts/smoke_trade_modes.py
python3 .cursor/skills/polymarket-quote/scripts/smoke_pitch_gate.py
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

- Goals+FT default live (goals still need pitch-gate in_play). Prefer small `--max-usdc`.
- Do not commit `.env` or private keys.
- Pitch-gate needs `QUOTE_DQD_STREAM_OBSERVE=1` and `QUOTE_PITCH_STATE=1`.
