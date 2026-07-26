# dqdhook

Dongqiudi ↔ Polymarket soccer pipeline: live scores from 懂球帝, fixture pairing, CLOB quoting, and optional in-process trading.

## What it does

```text
dongqiudi-match ──┐
                  ├── match-bridge (fuzzy pair + events) ──► polymarket-quote (books + trade)
polymarket-soccer ┘
```

| Skill | Role |
|---|---|
| `dongqiudi-match` | DQD match list / scores / score-change |
| `polymarket-soccer` | Polymarket Gamma soccer fixtures |
| `match-bridge` | Align DQD ↔ PM; emit `score_change` / `match_finished` |
| `polymarket-quote` | Settle markets from score, quote CLOB, dry-run or live FAK |

**System Main** (`frontend/run_main.py`) is the **only** process you start. It boots the hub (:8790), three boards, and `pm_quote watch`, which cascades bridge → DQD + Polymarket. Do **not** start `pm_quote`, boards, or a second `run_main` yourself (a second hub exits immediately if :8790 is taken).

## Quick start

```bash
# Optional trading deps (live / position checks)
pip install -r .cursor/skills/polymarket-quote/requirements-trade.txt

# Secrets in repo-root .env (never commit). Required for live:
#   PRIVATE_KEY, FUNDER, SIGNATURE_TYPE, CHAIN_ID, CLOB_HOST

# Only entrypoint — pulls up boards + quote (+ bridge cascade)
python3 frontend/run_main.py --no-browser

# Hub
open http://127.0.0.1:8790/
```

Stop: `Ctrl-C` / `kill` the `run_main` process, or `POST http://127.0.0.1:8790/api/stop` (exits hub + children; no empty shell on :8790).

Logs / data (gitignored under `data/` except a few keepers):

- Hub: http://127.0.0.1:8790/
- Quote watch log: `data/pm-quote/watch.log`
- Trades: `data/pm-quote/trades.jsonl`
- Bridge events: `data/bridge/events.jsonl`

## Trading modes

Default is **dry-run** for both goal and full-time signals. You can split them:

```bash
# Both channels live
python3 frontend/run_main.py --live --max-usdc 2

# Goals simulated, FT real
python3 frontend/run_main.py --goals-mode dry --ft-mode live --max-usdc 1

# Goals real, FT simulated
python3 frontend/run_main.py --goals-mode live --ft-mode dry --max-usdc 1

# Quote only
python3 frontend/run_main.py --no-trade
```

| Flag | Meaning |
|---|---|
| `--live` | Both `score_change` and `match_finished` post real CLOB orders |
| `--goals-mode dry\|live` | Mode for goal events (`score_change`); overrides `--live` for that channel |
| `--ft-mode dry\|live` | Mode for full-time (`match_finished`); overrides `--live` for that channel |
| `--take-depth top\|walk` | Fill from best level or walk the book |
| `--max-usdc` / `--max-shares` | Size caps (defaults 5 / 25) |

Env equivalents: `QUOTE_LIVE`, `QUOTE_GOALS_MODE`, `QUOTE_FT_MODE`, `QUOTE_TAKE_DEPTH`, `QUOTE_MAX_USDC`, `QUOTE_TRADE=0`, …

Flatten on score reversal follows each lot’s own `live` flag, so a dry goal fill is never live-sold when FT is live.

CLOB auth (repo `.env` or `--trade-env-file`): `PRIVATE_KEY`, `FUNDER`, `SIGNATURE_TYPE`, `CHAIN_ID`, `CLOB_HOST`.

## Skill entrypoints (debug only)

Prefer System Main above. Direct skill CLIs are for one-off debugging — do not run them alongside a live hub.

```bash
# Bridge + quote once from backlog
python3 .cursor/skills/polymarket-quote/scripts/pm_quote.py once --from-bridge --json

# Mode smoke
python3 .cursor/skills/polymarket-quote/scripts/smoke_trade_modes.py
```

Skill docs live under `.cursor/skills/*/SKILL.md`.

## Layout

```text
.cursor/skills/     # Agent skills (scripts + SKILL.md)
frontend/           # System Main hub + boards
data/               # Runtime snapshots / jsonl (mostly gitignored)
```

## Safety

- Default is dry-run; `--live` / `live` modes spend real USDC.
- Do not commit `.env` or private keys.
- Prefer small `--max-usdc` while validating mixed modes.
