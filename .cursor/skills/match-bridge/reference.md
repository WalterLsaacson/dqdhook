# Match Bridge — Reference

## Matching

Inputs:

- Dongqiudi: `data/snapshot.json` (`language=en`, default tab `full`)
- Polymarket: `data/polymarket/snapshot.json` (default next 48h)

Algorithm (greedy 1:1):

1. Kickoff within **45 minutes** (Beijing `local_date` + `time`)
2. Fuzzy team similarity on `home`/`away` (and swapped), after stripping FC/IF/… noise and diacritics
3. Keep pairs with `match_score ≥ 0.62` (override `--min-score`)

League IDs are **not** used (DQD numeric vs PM sport codes).

## Output shape

```json
{
  "matched_at": "2026-07-19T16:50:00+08:00",
  "source": "match-bridge",
  "dqd_count": 40,
  "pm_count": 15,
  "count": 3,
  "matches": [
    {
      "match_score": 0.91,
      "kickoff_beijing": "2026-07-19 18:30",
      "dongqiudi": {
        "id": "54407355",
        "league": "K League 1",
        "home": "Bucheon 1995",
        "away": "Seoul",
        "local_date": "2026-07-19",
        "time": "18:30",
        "status": "Fixture"
      },
      "polymarket": {
        "event_id": "674334",
        "slug": "kor-bch-seo-2026-07-19",
        "url": "https://polymarket.com/event/kor-bch-seo-2026-07-19",
        "gamma_event_url": "https://gamma-api.polymarket.com/events/674334",
        "series_id": "10444",
        "title": "Bucheon FC 1995 vs. FC Seoul",
        "league_id": "kor",
        "condition_ids": ["0x…"],
        "market_refs": [
          {
            "market_id": "…",
            "condition_id": "0x…",
            "question": "…",
            "clob_token_ids": ["…", "…"]
          }
        ]
      }
    }
  ]
}
```

## Polymarket handles for external consumers

Prefer these fields (most → least specific):

| Field | Use |
|---|---|
| `polymarket.url` | Open event page / scrape UI |
| `polymarket.slug` | `GET https://gamma-api.polymarket.com/events?slug=` |
| `polymarket.event_id` | `GET https://gamma-api.polymarket.com/events/{id}` |
| `polymarket.gamma_event_url` | Direct Gamma event JSON |
| `polymarket.condition_ids` / `market_refs[].clob_token_ids` | CLOB / price APIs |

## End-of-match (`match_finished`)

Dongqiudi skill only exposes `status` / `status_raw` (`Fixture` / `Playing` / `Played`).  
**Bridge** detects transitions into `played` on matched rows and emits:

```json
{
  "type": "match_finished",
  "ts": "2026-07-19T20:05:00+08:00",
  "match_id": "54363289",
  "prev_status": "playing",
  "status": "played",
  "home": "FC Anyang",
  "away": "Gwangju FC",
  "home_score": 1,
  "away_score": 0,
  "polymarket": {
    "event_id": "…",
    "slug": "…",
    "url": "…",
    "condition_ids": ["0x…"],
    "market_refs": [{ "market_id": "…", "condition_id": "0x…", "question": "…", "clob_token_ids": ["…", "…"] }]
  }
}
```

Downstream `polymarket-quote` joins this event (or `matches.json`) for CLOB quoting.

Rules:

- First sighting of a match only seeds `prev_status.json` (no event)
- Fire when previous status ≠ `played` and current = `played`
- Appended to `data/bridge/events.jsonl`; also in that rematch tick’s `matches.json` → `events`

Matched rows get `finished: true` / `dongqiudi.is_finished: true` while status is played.

## State files

| File | Role |
|---|---|
| `data/bridge/matches.json` | Last match result (+ `events` for that tick) |
| `data/bridge/latest.json` | Same payload (alias) |
| `data/bridge/prev_status.json` | Per-match status baseline for FT detection |
| `data/bridge/events.jsonl` | Append-only `match_finished` log |
| `data/snapshot.json` | DQD upstream |
| `data/polymarket/snapshot.json` | PM upstream |

## Notes

- Coverage is limited to fixtures present in **both** upstream snapshots.
- PM list needs the local proxy (`http://127.0.0.1:1082`) when Gamma is blocked.
- Prop markets (`"A vs. B - Halftime"`) are already filtered by the Polymarket skill.
- Dongqiudi skill uses `zh-cn` schedule + per-team `team_en_name` (cached). Bridge residual mismatches go in **[`scripts/team_aliases.py`](scripts/team_aliases.py)** (CN / abbreviations → canonical EN tokens).
- Prefer `match_timestamp` for kickoff (DQD `start_play` string may be wrong on the EN feed).
