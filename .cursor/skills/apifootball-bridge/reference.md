# API-Football Bridge — Reference

## Inputs

- Bridge pairs: `data/bridge/matches.json` (`matches[].dongqiudi` + `matches[].polymarket`)
- Optional fallback for events resolve: `data/snapshot.json` (DQD list)
- API key: `.env` field `apifootball_key`
- Base URL: `https://v3.football.api-sports.io`

## Fixture matching

For each bridge row with Dongqiudi `id`:

1. **Cache hit** — `fixture_cache.json` `entries[id].af_fixture_id` set → reuse; refresh bridge metadata timestamps
2. **Unresolved TTL** — if `unresolved[id].tried_at` within **6h**, skip AF call
3. **Resolve** — `GET /fixtures?date=` for kickoff Beijing day and ±1 calendar day; score candidates with:
   - team name similarity (via match-bridge `team_similarity`) average ≥ **0.75** (direct or swapped)
   - kickoff skew ≤ **120** minutes (`match_timestamp` / AF `fixture.timestamp`)
4. Best score wins; write `entries[id]` or `unresolved[id]`

## Cache schema

Path: `data/apifootball/fixture_cache.json`

```json
{
  "updated_at": "2026-07-30T21:00:00+08:00",
  "entries": {
    "54528347": {
      "dqd_match_id": "54528347",
      "af_fixture_id": 1546417,
      "dqd_home": "…",
      "dqd_away": "…",
      "af_home": "…",
      "af_away": "…",
      "af_league": "…",
      "kickoff_beijing": "…",
      "name_score": 0.96,
      "skew_min": 0,
      "matched_at": "…",
      "source": "bridge+af",
      "last_bridge_matched_at": "…"
    }
  },
  "unresolved": {
    "5449…": {
      "reason": "no_af_fixture",
      "tried_at": "…",
      "dqd_home": "…",
      "dqd_away": "…"
    }
  }
}
```

Prune: on sync/watch, drop `entries` whose DQD id has been absent from bridge for **>24h** (tracked via `last_bridge_matched_at`).

## Events request

```bash
python3 .cursor/skills/apifootball-bridge/scripts/af_bridge.py events --match-id <DQD_ID> --json
```

1. Resolve `af_fixture_id` from **fixture cache** (warm via `sync`/`watch`). On miss only: one-shot match using bridge row or DQD snapshot (respects unresolved 6h TTL unless `--force-resolve`)
2. **One** AF call: `GET /fixtures/events?fixture=` (no extra `/fixtures?id=`)
3. Derive `goals` from standing Goal events vs cached `af_home`/`af_away`
4. Write under `data/dqd-probe/af-latency/bursts/{dqd_id}_{YYYYMMDDTHHMMSSmmm}/`:
   - `meta.json` — `source: events_request`
   - `af_events.json` — raw events API body
   - `result.json` — summary
5. Append `burst_index.jsonl` with `kind: events_request`
6. Print JSON to stdout (`ok`, ids, `goals`, `events`, `burst_dir`)

## Free plan

API-Football Free ≈ **10 req/min** and a rolling **~3-day** fixture date window.  
CLI defaults `--free-plan` → client min spacing **6.5s**. Disable with `--no-free-plan` only on paid keys.

## Watch cadence

`watch` polls bridge every **15s**. Sync runs when the bridge fingerprint (`matched_at` + match ids) changes, or every `--force-every` ticks (default 40 ≈ 10 min) even if unchanged.

## Status fields

`status --json` reports **local** cache entry counts, last sync time, unresolved count, and bridge size. It does **not** call AF by default. Pass `--af-status` only if you need live quota (`/status`); that burns Free-plan daily requests.