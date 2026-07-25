---
name: match-bridge
description: >-
  Bridges dongqiudi-match and polymarket-soccer: starts both at their default
  refresh cadences, fuzzy-matches fixtures, and emits Polymarket market handles
  (event id, slug, URL, condition_ids) for matched games. Use when the user
  wants cross-skill match alignment, Polymarket markets for Dongqiudi fixtures,
  or a joint match list for downstream odds consumers.
---

# Match Bridge (Dongqiudi ↔ Polymarket)

Orchestrates the two data skills, matches fixtures (English names + Beijing kickoff), and outputs **Polymarket handles** so other tools can load the market/odds.

## Quick start

From repo root:

```bash
# One-shot: refresh both skills + match
python3 .cursor/skills/match-bridge/scripts/bridge_match.py once --json

# Rematch from existing snapshots only
python3 .cursor/skills/match-bridge/scripts/bridge_match.py once --offline --json

# Resident loops (DQD 15s/60s, Polymarket 10 min)
python3 .cursor/skills/match-bridge/scripts/bridge_match.py start --foreground

# Last result / status
python3 .cursor/skills/match-bridge/scripts/bridge_match.py list --json
python3 .cursor/skills/match-bridge/scripts/bridge_match.py status --json
```

## Default cadences

| Source | Skill | Default refresh |
|---|---|---|
| Dongqiudi | `dongqiudi-match` watch (`full` tab) | **15s** live / **60s** idle |
| Polymarket | `polymarket-soccer` list | **600s** (10 min), `within_hours=48` |

## Agent workflow

1. Prefer `once` for a single JSON handoff, or `start --foreground` for continuous updates.
2. Read matched rows from stdout or `data/bridge/matches.json`.
3. For each match, give downstream consumers `polymarket.url` / `polymarket.slug` / `polymarket.event_id` / `polymarket.condition_ids` (see [reference.md](reference.md)).
4. **进球 / 终场 / 回撤**：read `score_change` and `match_finished` from that tick’s `events` or `data/bridge/events.jsonl`. Goals rise → `is_goal`; score drop → `is_reversal` (quote skill flattens).
5. Do not invent markets when `count` is 0 — coverage is the intersection of DQD today’s list and PM’s 48h window.

## Cooperation contract

| Channel | Path | Purpose |
|---|---|---|
| CLI JSON | `once` / `list` stdout | Matched pairs (+ `events` that tick) |
| Snapshot | `data/bridge/matches.json` | Last successful match run |
| FT events | `data/bridge/events.jsonl` | `match_finished` when status → played |
| Status baseline | `data/bridge/prev_status.json` | For FT transition detection |
| Upstream DQD | `data/snapshot.json` | Written by embedded DQD watch |
| Upstream PM | `data/polymarket/snapshot.json` | Written by embedded PM list |

## Related frontend

Demo board (scores + 伤停补时 / 墙钟 + Polymarket links):

```bash
python3 frontend/run_bridge.py
# → http://127.0.0.1:8789/
# Board boot autostarts match-bridge (DQD watch + PM poll + rematch).
```

Module: [`frontend/bridge-board/`](../../../frontend/bridge-board/).

## Related skills

- [`dongqiudi-match`](../dongqiudi-match/SKILL.md)
- [`polymarket-soccer`](../polymarket-soccer/SKILL.md)
- [`polymarket-quote`](../polymarket-quote/SKILL.md) — `watch` autostarts this bridge (+ DQD + PM); full UI via `python3 frontend/run_stack.py`

## Files

- Scripts: [scripts/bridge_match.py](scripts/bridge_match.py), [scripts/bridge_lib.py](scripts/bridge_lib.py), [scripts/team_aliases.py](scripts/team_aliases.py) (CN / abbr → EN)
- Details: [reference.md](reference.md)
