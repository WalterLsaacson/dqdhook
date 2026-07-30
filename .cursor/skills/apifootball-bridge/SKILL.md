---
name: apifootball-bridge
description: >-
  Maps match-bridge Dongqiudi↔Polymarket fixtures onto API-Football fixture IDs
  with a local cache, and serves on-demand /fixtures/events by Dongqiudi match
  id for other skills (score referee / latency). Use when the user mentions
  API-Football bridge, AF fixture cache, AF events by dongqiudi id, or confirming
  goals against API-Football after a bridge match.
---

# API-Football Bridge

Listens to **match-bridge** matched fixtures, resolves each Dongqiudi match onto an API-Football `fixture_id`, caches the mapping, and exposes an **events** CLI for cooperating skills.

## Quick start

From repo root:

```bash
# One-shot: cache-first sync from data/bridge/matches.json
python3 .cursor/skills/apifootball-bridge/scripts/af_bridge.py sync --once --json

# Resident: re-sync every 15s when bridge matches change
python3 .cursor/skills/apifootball-bridge/scripts/af_bridge.py watch --foreground

# On-demand events for a Dongqiudi match id (other skills call this)
python3 .cursor/skills/apifootball-bridge/scripts/af_bridge.py events \
  --match-id 54528347 --json

# Cache / status (local only; status does not call AF unless --af-status)
python3 .cursor/skills/apifootball-bridge/scripts/af_bridge.py list --json
python3 .cursor/skills/apifootball-bridge/scripts/af_bridge.py status --json
```

Defaults: Free-plan AF spacing **6.5s** (`--free-plan` on). Mapping cache: `data/apifootball/fixture_cache.json`. Events artifacts share the latency probe tree: `data/dqd-probe/af-latency/bursts/`.

`events` path: **cache lookup → one** `GET /fixtures/events` (fixture id already cached by sync/watch).
## Agent workflow

1. Ensure match-bridge has written `data/bridge/matches.json` (or run bridge `once` / `start` first).
2. Run `sync --once` or keep `watch` running so AF fixture IDs stay warm.
3. When another skill needs timeline confirmation, call `events --match-id <DQD_ID> --json` and read stdout; also check `burst_dir` under `data/dqd-probe/af-latency/bursts/`.
4. Do not invent fixture IDs when cache + AF resolve fail — return `ok: false`.

## Cooperation contract

| Channel | Path / signal | Purpose |
|---|---|---|
| Bridge input | `data/bridge/matches.json` | Source of DQD↔PM pairs to map |
| Fixture cache | `data/apifootball/fixture_cache.json` | DQD id → AF fixture_id |
| Events CLI | `events --match-id … --json` stdout | On-demand AF events for consumers |
| Events disk | `data/dqd-probe/af-latency/bursts/{id}_{ts}/` | Same layout as latency bursts |
| Burst index | `data/dqd-probe/af-latency/burst_index.jsonl` | Append `kind=events_request` |

Consumer skills should:

- Prefer cache hits (`list` / read `fixture_cache.json`) before requesting events.
- Pass **Dongqiudi** `match_id` (not Polymarket event id) to `events`.
- Treat missing AF coverage as inconclusive (Free plan / league without events).
- **polymarket-quote referee**: calls this skill’s **lib** `fetch_events_for_match_id` asynchronously (same path as `events` CLI); burst persisted on confirm; Free-plan spacing bypassed during confirm with 429 backoff.

### Example handoff

```text
Other skill:
  python3 .cursor/skills/apifootball-bridge/scripts/af_bridge.py events \
    --match-id 54528347 --json

Stdout:
{
  "ok": true,
  "dqd_match_id": "54528347",
  "af_fixture_id": 1546417,
  "goals": {"home": 1, "away": 0},
  "events": [ ... ],
  "burst_dir": "data/dqd-probe/af-latency/bursts/54528347_…"
}
```

## Related skills

- [`match-bridge`](../match-bridge/SKILL.md) — upstream DQD↔PM pairs
- [`dongqiudi-match`](../dongqiudi-match/SKILL.md) — DQD scores / latency probe
- [`polymarket-quote`](../polymarket-quote/SKILL.md) — AF referee gate consumer (goal confirm → trade)

## Frontend

Board: `frontend/af-bridge-board` (port **8791**).

```bash
python3 frontend/run_af_bridge.py
# Preferred: System Main boots the board and POSTs /api/af/start so watch runs
python3 frontend/run_main.py
# → http://127.0.0.1:8791/
```

Shows cached DQD→AF mappings (league rail, name score, fixture ids, unresolved TTL list).  
Board starts **read-only** (no AF calls); System Main (or **Sync once** / **Start watch** on the UI) turns watch on. Disable hub auto-start with `MAIN_AF_WATCH=0`.

## Files

- Scripts: [scripts/af_bridge.py](scripts/af_bridge.py), [scripts/af_bridge_lib.py](scripts/af_bridge_lib.py), [scripts/smoke_af_bridge.py](scripts/smoke_af_bridge.py)
- Docs: this file, [reference.md](reference.md)
