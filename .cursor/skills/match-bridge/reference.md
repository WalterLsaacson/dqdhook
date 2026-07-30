# Match Bridge — Reference

## Matching

Inputs:

- Dongqiudi: `data/snapshot.json` (`language=en`, default tab `full`)
- Polymarket: `data/polymarket/snapshot.json` (default next 48h)

Algorithm (greedy 1:1):

1. **Drop stale PM** — kickoff more than **6h** in the past (override `--pm-stale-hours`)
2. **Kickoff skew** — absolute `|t_dqd − t_pm| ≤ 90` minutes (override `--max-skew-min`). Prefer DQD `match_timestamp` (UTC epoch → Beijing) and PM `kickoff_beijing`; fall back to `local_date` + `time`
3. **Bilateral team floor** — home **and** away similarity each ≥ **0.75** on the chosen orientation (direct or swapped); override `--min-side`. Digit tokens like `2028` / `04` are **kept**; digits inside a token are never stripped
4. **League gate** — when both sides have league fields, alias to canonical codes ([`league_aliases.py`](scripts/league_aliases.py)); known codes must match exactly; otherwise fuzzy ratio must be ≥ **0.40**. Missing league on either side skips this gate
5. Keep pairs with composite `match_score ≥ 0.70` (override `--min-score`)

Smoke: `python3 .cursor/skills/match-bridge/scripts/smoke_match_hardening.py`  
FT period: `python3 .cursor/skills/match-bridge/scripts/smoke_ft_period.py`

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

Dongqiudi exposes `status` (`Fixture` / `Playing` / `Played`) and **`period`** (`1H` / `2H` / `FT`).  
Stoppage time stays `Playing` + `1H`/`2H`. Full time is **`period=FT`** (status may briefly still be Playing).

**Bridge** emits when `period` transitions into `FT` on matched rows:

```json
{
  "type": "match_finished",
  "ts": "2026-07-19T20:05:00+08:00",
  "match_id": "54363289",
  "prev_status": "playing",
  "prev_period": "2H",
  "status": "played",
  "period": "FT",
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

- First sighting only seeds `prev_status.json` + `prev_period.json` (no event)
- Fire when previous `period` ≠ `FT` and current `period` = `FT` (does **not** require `Played`)
- `1H` / `2H` never emit full-time (covers half-time and injury time)
- Appended to `data/bridge/events.jsonl`; also in that rematch tick’s `matches.json` → `events`
- Poll cadence: default live **5s**; if any DQD row is `Played` but `period` ≠ `FT`, next sleep is **5s** until FT

Matched rows get `finished: true` / `dongqiudi.is_finished: true` while `period` is `FT`.

## State files

| File | Role |
|---|---|
| `data/bridge/matches.json` | Last match result (+ `events` for that tick) |
| `data/bridge/latest.json` | Same payload (alias) |
| `data/bridge/prev_status.json` | Per-match status baseline |
| `data/bridge/prev_period.json` | Per-match period baseline for FT detection |
| `data/bridge/events.jsonl` | Append-only `match_finished` log |
| `data/snapshot.json` | DQD upstream |
| `data/polymarket/snapshot.json` | PM upstream |

## Notes

- Coverage is limited to fixtures present in **both** upstream snapshots.
- PM list needs the local proxy (`http://127.0.0.1:1082`) when Gamma is blocked.
- Prop markets (`"A vs. B - Halftime"`) are already filtered by the Polymarket skill.
- Dongqiudi skill uses `zh-cn` schedule + per-team `team_en_name` (cached). Bridge residual mismatches go in **[`scripts/team_aliases.py`](scripts/team_aliases.py)** (CN / abbreviations → canonical EN tokens).
- Prefer `match_timestamp` for kickoff (DQD `start_play` string may be wrong on the EN feed).
