# Polymarket Soccer — Reference

## Upstream (Gamma API)

Base: `https://gamma-api.polymarket.com`  
Docs: [Fetching Markets](https://docs.polymarket.com/market-data/fetching-markets), [Sports metadata](https://docs.polymarket.com/api-reference/sports/get-sports-metadata-information)

| Step | Request | Role |
|---|---|---|
| 1 | `GET /sports` | Sport code → `series` id |
| 2 | `GET /events?series_id=&…&start_date_min=&start_date_max=` | Events for a league (optional time window) |
| 3 | `markets[].gameStartTime` | Kickoff (UTC) → Beijing |

### Upcoming 48h window (default)

`list` defaults to **next 48 hours** (plus games that kicked off up to 3h ago, for live coverage).

Filtering is **local only** on `markets[].gameStartTime`. Do **not** send Gamma `order=start_date` or `start_date_*` / `start_time_*` for soccer — those either 400 or return empty / wrong rows.

Soccer leagues come from `GET /sports` entries tagged **`100350`** (same family as the soccer/games page), not a tiny hard-coded list.

CLI: `--within-hours 48` (default), `--within-hours 0` disables the window. Prop markets (`"Team A vs. Team B - Halftime …"`) are dropped; main matchups only.

## Proxy (default)

Outbound HTTPS goes through the local Shadowrocket proxy by default (matches system HTTP/HTTPS proxy used by Chrome):

| Setting | Value |
|---|---|
| Default | `http://127.0.0.1:1082` |
| Env | `PM_PROXY` or `ALL_PROXY` |
| CLI | `--proxy URL` / `--no-proxy` |
| Disable | `--no-proxy` or `PM_PROXY=none` |

- Shadowrocket on this machine exposes **HTTP** on `1082` (`HTTPEnable`/`HTTPSEnable`); SOCKS may be off in system settings even if the port also accepts SOCKS.
- Bare `host:port` is treated as `socks5h://` (remote DNS). Prefer an explicit `http://` URL for Shadowrocket.
- SOCKS needs [PySocks](https://pypi.org/project/PySocks/): `pip3 install PySocks`.
- Requests go through `curl -x` first. If Shadowrocket returns **CONNECT 503** for the hostname `gamma-api.polymarket.com` (while `polymarket.com` still works), the client retries with Cloudflare IP pinning (`curl --connect-to`).
- Optional: in Shadowrocket, add `DOMAIN-SUFFIX,gamma-api.polymarket.com,PROXY` (or GLOBAL) so the hostname itself is allowed.

When the season has no open fixtures, `list` may return `count: 0` with default filters. Use `--include-closed` to include settled games.

**Do not use `tag_id=1059` for soccer** — that id is not a reliable soccer filter.

## Soccer allowlist

| Code | series (typical) | Display name |
|---|---|---|
| epl | 10188 | EPL |
| ucl | 10204 | UCL |
| uel | 10209 | UEL |
| mls | 10189 | MLS |
| afc | 10241 | AFC |
| caf | 10240 | CAF |
| efa | 10307 | EFA |
| fifa | 10428 | FIFA |
| fifaw | 11448 | FIFA Women |
| bkseriea | 10877 | Serie A |
| bkligarg | 11894 | Liga Profesional |
| wwcquefa | 11846 | WWC Qualifiers (UEFA) |

Only events whose `title` matches `A vs[. ] B` (or `A v B`) are kept as matchups.

## `list` JSON shape

```json
{
  "fetched_at": "2026-07-19T15:40:00+08:00",
  "source": "polymarket-gamma",
  "proxy": "http://127.0.0.1:1082",
  "window": {
    "within_hours": 48,
    "start_utc": "2026-07-19T08:00:00Z",
    "end_utc": "2026-07-21T08:00:00Z"
  },
  "count": 12,
  "leagues": [{"id": "epl", "series_id": "10188", "name": "EPL", "count": 5}],
  "matches": [
    {
      "id": "…",
      "slug": "liverpool-vs-bournemouth",
      "league": "EPL",
      "league_id": "epl",
      "series_id": "10188",
      "home": "Liverpool",
      "away": "Bournemouth",
      "start_play": "2025-08-15 19:00:00+00",
      "time": "03:00",
      "local_date": "2025-08-16",
      "kickoff_beijing": "2025-08-16 03:00",
      "url": "https://polymarket.com/event/liverpool-vs-bournemouth"
    }
  ]
}
```

Timezone: `Asia/Shanghai` (UTC+8). Primary display field: `kickoff_beijing`.

## State files

| File | Role |
|---|---|
| `data/polymarket/snapshot.json` | Last `list` result |
| `data/polymarket/leagues.json` | Last `leagues` result |

## Cooperation

Other skills/modules should consume CLI stdout JSON or the snapshot file. Do not scrape `polymarket.com` HTML for this data.
