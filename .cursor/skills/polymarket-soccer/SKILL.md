---
name: polymarket-soccer
description: >-
  Fetches Polymarket soccer game matchups via the public Gamma API (leagues,
  home/away teams, Beijing kickoff times). Use when the user asks about
  Polymarket, gamma-api, sports/soccer/games, prediction-market soccer lists,
  or cooperating modules that need Polymarket fixture JSON.
---

# Polymarket Soccer Games

Project skill for football matchups listed on [polymarket.com/zh/sports/soccer/games](https://polymarket.com/zh/sports/soccer/games). Data comes from the official **Gamma API** — do not scrape the page DOM.

## Quick start

From the repo root (`dongqiudihook`):

```bash
# All allowlisted soccer leagues
python3 .cursor/skills/polymarket-soccer/scripts/pm_soccer.py list --json

# One or more leagues
python3 .cursor/skills/polymarket-soccer/scripts/pm_soccer.py list --league epl --json
python3 .cursor/skills/polymarket-soccer/scripts/pm_soccer.py list --league epl,ucl,mls --json

# Time window (default: next 48h, local gameStartTime filter; all /sports tag 100350 leagues)
python3 .cursor/skills/polymarket-soccer/scripts/pm_soccer.py list --within-hours 48 --json
python3 .cursor/skills/polymarket-soccer/scripts/pm_soccer.py list --within-hours 0 --json   # no window

# Available league codes
python3 .cursor/skills/polymarket-soccer/scripts/pm_soccer.py leagues --json
```

Outbound requests **default to** `http://127.0.0.1:1082` (Shadowrocket / MacPacket system HTTP proxy; same port as Chrome). SOCKS5 (`socks5h://127.0.0.1:1082`) also works when enabled. Override with `--proxy`, `PM_PROXY` / `ALL_PROXY`, or disable with `--no-proxy`. SOCKS needs `PySocks` (`pip3 install PySocks`).

```bash
# Explicit proxy / direct
python3 .cursor/skills/polymarket-soccer/scripts/pm_soccer.py list --proxy http://127.0.0.1:1082 --json
python3 .cursor/skills/polymarket-soccer/scripts/pm_soccer.py list --proxy socks5h://127.0.0.1:1082 --json
python3 .cursor/skills/polymarket-soccer/scripts/pm_soccer.py list --no-proxy --json
```

## Agent workflow

1. **Match list** — run `list` (optionally `--league`). Return the JSON (league, home, away, `kickoff_beijing`).
2. **League codes** — run `leagues` when the user asks which competitions are available.
3. **Handoff** — give stdout JSON or `data/polymarket/snapshot.json` to cooperating skills/modules. Do not invent fixtures.

## Cooperation contract

| Channel | Path / signal | Purpose |
|---|---|---|
| CLI JSON | stdout from `list` / `leagues` | Immediate structured data |
| Snapshot | `data/polymarket/snapshot.json` | Last successful `list` |

Each match includes at least: `league`, `home`, `away`, `kickoff_beijing` (Asia/Shanghai), plus `time` / `local_date` for alignment with other modules.

The demo board (`frontend/run_polymarket.py`) writes this snapshot on a **3h** Gamma loop. **match-bridge reads the file and does not rescan leagues.**

## Related frontend

Demo board that starts this skill and renders the match list with league colors:

```bash
python3 frontend/run_polymarket.py
# → http://127.0.0.1:8788/
```

Module: [`frontend/polymarket-board/`](../../../frontend/polymarket-board/).

## Related skill

For Dongqiudi live scores / hot tabs, see [`dongqiudi-match`](../dongqiudi-match/SKILL.md).

To align Dongqiudi fixtures with Polymarket markets, see [`match-bridge`](../match-bridge/SKILL.md).

## Files

- Scripts: [scripts/pm_soccer.py](scripts/pm_soccer.py), [scripts/pm_lib.py](scripts/pm_lib.py)
- API details: [reference.md](reference.md)
