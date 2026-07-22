# Dongqiudi Match — Reference

## Upstream API

```
GET https://www.dongqiudi.com/magicball/v1/list/match_list
  ?language=en
  &cmp_type=soccer
  &tab_type=all
  &_t=<epoch_ms>
```

- Scope of this skill: **soccer only**.
- Default window: **today + next 2 Beijing calendar days** (`--days 3`, aligns with Polymarket ~48h).
- Today: `GET /magicball/v1/list/match_list?language=zh-cn&cmp_type=soccer&tab_type=all`
- Future days: `GET /magicball/v1/list/schedule_list?language=zh-CN&tab_type=fixture&cmp_type=soccer&start=<encoded>`
  - `start` is **not** a bare date. Nuxt uses `Date.UTC(y,m-1,d) - 8h` formatted as UTC `YYYY-MM-DD HH:00:00`
    (e.g. Beijing `2026-07-22` → `2026-07-21 16:00:00`).
- English team names: `/magicball/v1/team/detail` → `team_en_name` (cached in `data/dqd_team_en.json`).
- Do **not** join `match_list?language=en` by `match_id` — that feed is often months stale.
- Explicit `--language zh-cn` skips English rename.
- Past days use `schedule_list` with `tab_type=schedule` (not wired in the default watcher).

### Important raw fields

| Raw | Mapped |
|---|---|
| `match_id` | `id` |
| `competition.id` / `.name` | `league_id` / `league` |
| `team_A.name` / `team_B.name` | `home` / `away` |
| `team_A.fs` / `team_B.fs` | `home_score` / `away_score` |
| `team_A.hts` / `team_B.hts` | `home_half` / `away_half` |
| `status` (`Playing`/`Played`/`Fixture`) | `status_raw` + normalized `status` |
| `minute` | `minute` |
| `start_play` (UTC) | `time`, `local_date` (Asia/Shanghai) |
| `business_status` | bit flags for tabs |

## Tab bit flags

Copied from the official Nuxt `/match` page:

- Hot: `(cmp_type == soccer && (business_status & 320) == 320) || league_id in {43, 129}`
- Beidan: `soccer && (business_status & 320) == 320`
- Jingcai: `soccer && (business_status & 2) != 0`

## `list` JSON shape

```json
{
  "fetched_at": "2026-07-19T15:00:00+08:00",
  "language": "en",
  "today": "2026-07-19",
  "tab": "hot",
  "count": 18,
  "has_live": false,
  "leagues": [{"id": "21", "name": "Allsvenskan", "count": 3}],
  "counts": {"full": 300, "hot": 18, "beidan": 18, "jingcai": 6},
  "matches": [
    {
      "id": "54340458",
      "league": "Allsvenskan",
      "league_id": "21",
      "home": "Hammarby",
      "away": "Degerfors",
      "home_score": null,
      "away_score": null,
      "status": "Fixture",
      "tabs": ["full", "hot", "beidan", "jingcai"]
    }
  ]
}
```

`list --tab all` wraps four snapshots under `tabs`.

## `score_change` event

Appended to `data/events.jsonl` and printed as:

```text
DQD_SCORE_CHANGE {"type":"score_change",...}
```

```json
{
  "type": "score_change",
  "ts": "2026-07-19T15:00:00+08:00",
  "match_id": "54340458",
  "league": "Allsvenskan",
  "league_id": "21",
  "home": "Hammarby",
  "away": "Degerfors",
  "prev": {"home": 0, "away": 0},
  "curr": {"home": 1, "away": 0},
  "side": "home",
  "is_goal": true,
  "status": "Playing 50'",
  "tab": "hot"
}
```

- `side`: `home` | `away` | `both` | `other`
- `is_goal`: true when soccer and either side's score increased
- First sighting of a match only seeds `prev_scores`; it does not emit an event

## State files (`data/`)

| File | Role |
|---|---|
| `snapshot.json` | Last `list` / `watch` snapshot |
| `prev_scores.json` | `{match_id: {home, away}}` baseline for diffs |
| `events.jsonl` | Append-only score_change log |

## Loop integration

```bash
# In-session: arm loop skill every 15s when live / 30–60s when idle (sentinels on stdout):
python3 .cursor/skills/dongqiudi-match/scripts/dqd_match.py watch --tab hot --once
```

Polling defaults: `--interval 15` (live, clamped ≥10), `--idle-interval 60` (no live, clamped ≥30).

When stdout contains `DQD_SCORE_CHANGE`, forward the JSON object to the consumer skill.

For pure JSON (no sentinel lines), use `--json` and read the `events` array.
