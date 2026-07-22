---
name: dongqiudi-match
description: >-
  Fetches Dongqiudi soccer match tabs (full/hot/beidan/jingcai), English team
  and league names, scores, and score-change events via the public match_list
  API. Use when the user asks about 懂球帝, Dongqiudi matches, hot tab, 比分,
  goals, score change notifications, or cooperating skills that need live
  soccer score events.
---

# Dongqiudi Match Data

Project skill for soccer match tabs and score-change events. Scripts live in this skill folder; runtime state is under the repo `data/` directory.

## Quick start

From the repo root (`dongqiudihook`):

```bash
# Hot tab snapshot (JSON)
python3 .cursor/skills/dongqiudi-match/scripts/dqd_match.py list --tab hot --json

# All tabs
python3 .cursor/skills/dongqiudi-match/scripts/dqd_match.py list --tab all --json

# One poll + diff for /loop (sentinels on stdout)
python3 .cursor/skills/dongqiudi-match/scripts/dqd_match.py watch --tab hot --once

# Same poll as pure JSON (events in .events; no sentinel lines)
python3 .cursor/skills/dongqiudi-match/scripts/dqd_match.py watch --tab hot --once --json --quiet

# Resident poll (15s when live, 60s when idle)
python3 .cursor/skills/dongqiudi-match/scripts/dqd_match.py watch --tab hot --interval 15 --idle-interval 60
```

Defaults: soccer only, `language=en`, **`--days 3`**. Today from `match_list`; later days from `schedule_list` (`tab_type=fixture` + Nuxt-encoded `start`). English names via `team_en_name` cache. Polling: **10–15s live** / **30–60s idle**.

## Agent workflow

1. **Tab data** — run `list --tab <full|hot|beidan|jingcai|all> --json`. Return the JSON (or a short league summary). Do not scrape the website DOM.
2. **Watch scores** — prefer `watch --tab hot --once` on a loop (`/loop 15s` when live; 30–60s when idle). Resident `watch` without `--once` is fine for a dedicated terminal.
3. **On score change** — text mode prints `DQD_SCORE_CHANGE {...}` on stdout; every mode appends the same object to `data/events.jsonl`. With `--json`, read the `events` array instead. Forward that JSON to any cooperating skill the user named (notify / announce / log). Do not invent events.

## Cooperation contract (for other skills)

| Channel | Path / signal | Purpose |
|---|---|---|
| CLI JSON | stdout from `list` / `watch --once --json` | Immediate tab snapshot |
| Event log | `data/events.jsonl` (append-only) | Durable score_change history |
| Sentinel | `DQD_SCORE_CHANGE <json>` on stdout | Wake loop / session notify |

Consumer skills should:

- Read the latest `score_change` lines from `data/events.jsonl`, **or** accept the JSON the agent forwards after a sentinel.
- Treat `is_goal: true` as a soccer goal notification.
- Not call Dongqiudi HTML/OCR themselves.

### Example handoff

```text
Other skill input:
{
  "type": "score_change",
  "match_id": "...",
  "home": "Hammarby",
  "away": "Degerfors",
  "prev": {"home": 0, "away": 0},
  "curr": {"home": 1, "away": 0},
  "side": "home",
  "is_goal": true,
  "tab": "hot"
}
```

## Tabs

| Tab | Rule (same as official /match page) |
|---|---|
| `full` | All soccer matches in the date window (default **today + next 2 days** Beijing) |
| `hot` | `business_status & 320 == 320`, or `league_id` in `{43, 129}` |
| `beidan` | soccer and `business_status & 320 == 320` |
| `jingcai` | soccer and `business_status & 2 != 0` |

## Related skill

For Polymarket soccer fixtures (Gamma API), see [`polymarket-soccer`](../polymarket-soccer/SKILL.md).

To align Dongqiudi fixtures with Polymarket markets, see [`match-bridge`](../match-bridge/SKILL.md).

**Kickoff note:** prefer API `match_timestamp` (UTC epoch). The `start_play` string date can be stale; this skill maps Beijing time from the timestamp.

## Official frontend module

Repo module `@dongqiudi/match-board` consumes this skill:

```bash
python3 frontend/run.py
# → http://127.0.0.1:8787/
```

Path: `frontend/match-board/` (`module.json`, `public/`, `src/`, `server/`).

## Files

- Scripts: [scripts/dqd_match.py](scripts/dqd_match.py), [scripts/dqd_lib.py](scripts/dqd_lib.py)
- Field/event schema: [reference.md](reference.md)
- State (gitignored contents): `data/snapshot.json`, `data/prev_scores.json`, `data/events.jsonl`
