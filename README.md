# FPL League Scraper

Self-contained Docker image that pulls Fantasy Premier League data for a
classic league into SQLite, and generates a weekly markdown report. Designed
to be run on-demand (e.g. weekly via cron) — the SQLite DB and reports persist
on the host via a volume mount.

## Prerequisites

- **Docker** and **Docker Compose v2** installed (`docker compose version` should work).
- Your FPL **classic league ID** — find it in the URL when viewing your league
  on fantasy.premierleague.com:
  `https://fantasy.premierleague.com/leagues/`**`59720`**`/standings/c`

## Setup

```bash
# 1. Extract the project (or git clone) and cd into it
cd fpl-scraper

# 2. Configure your league ID
cp .env.example .env
$EDITOR .env                          # set FPL_LEAGUE_ID=<your_id>

# 3. Build the image (~30s, one-off)
docker compose build

# 4. First scrape. Mid-season this takes 2-3 minutes; end-of-season longer
#    because it backfills every gameweek's picks for every manager.
docker compose run --rm fpl scrape

# 5. Generate the report from the freshly scraped data
docker compose run --rm fpl report
```

The SQLite DB lands at `./data/fpl.db` and the report at
`./data/reports/dossier_GW{N}.md`. Both directories are created automatically
on first run.

## Commands

| Command | What it does |
|---------|--------------|
| `fpl scrape` | Pull league standings, manager histories, picks, transfers, past seasons, and per-player gameweek stats |
| `fpl scrape --skip-player-history` | Faster scrape that skips `/element-summary/` (~1-2 min saved) |
| `fpl scrape --owned-only` | Only fetch player history for players owned by your league |
| `fpl report` | Generate markdown report for the latest scraped GW |
| `fpl report --gw 35` | Generate report for a specific GW |
| `fpl publish` | Render the latest GW as HTML into `docs/` for GitHub Pages |
| `fpl publish --all` | Render **every** scraped GW into `docs/` (backfill an archive) |
| `fpl shell` | Drop into a `sqlite3` shell on the DB |

All commands accept `--db PATH`, `--league ID`, and `-v` (verbose). The league
ID falls back to `FPL_LEAGUE_ID` from `.env` if not passed explicitly.

## Publishing to GitHub Pages

`fpl publish` renders the styled HTML Dossier into `docs/`, laid out so the
page itself carries a **season + gameweek dropdown** in the top nav — readers
can jump to any previously published GW (or, once you start a new season, any
season) without leaving the page.

```bash
# Publish the latest scraped GW (run weekly, alongside scrape)
docker compose run --rm fpl publish

# One-off: backfill the whole season into the archive
docker compose run --rm fpl publish --all
```

This produces:

```
docs/
├── index.html          # redirect → the latest GW
├── manifest.json       # list of seasons + GWs (drives the dropdown)
└── 25-26/
    ├── GW1.html
    ├── …
    └── GW38.html
```

Point GitHub Pages at the `docs/` folder (Settings → Pages → *Deploy from a
branch* → `/docs`), commit, and push. The dropdown is built client-side from
`manifest.json`, so it always reflects whatever files are present. When the
new season starts, `publish` derives the season label from the date (e.g.
`26-27`) and creates `docs/26-27/` automatically — both seasons then appear in
the dropdown. Override the label with `--season 26-27` if needed.

> A standalone report opened on its own (no `manifest.json` next to it) simply
> hides the dropdown, so `fpl report -f html` output still works as before.

## What's in the HTML dossier

Beyond the core leaderboard / captains / transfers / chips / xPts sections, the
report includes a layer of season-long analysis:

- **Hall of Records** — banter awards: Mr Reliable / The Rollercoaster
  (consistency), Magnum Opus / The Stinker (best & worst single GW), Bench
  Disaster, Hit Merchant, Scrooge, The Tinkerman, Mr Template / The Hipster,
  Lone Wolf, Ride or Die, Captain Marvel / Calamity.
- **By the Numbers** — a consistency table (avg / best / worst / std dev /
  weeks above league average) and a rank-trajectory table (green vs red arrows).
- **Transfer Lab** — activity, hits, net transfer P&L and each manager's
  deadline-day habit, plus the biggest bandwagon.
- **Defensive Contributions** — DefCon points (2025/26 scoring) for league-owned
  players and a "Park the Bus" manager ranking. **DefCon also feeds the xPts
  model**, so defenders/midfielders earning defensive points are no longer
  flagged as "lucky".
- **Alternative Tables** — captaincy-only and "no-hits" standings.
- **Head to Head** — every manager vs every other on weekly points, with a
  full W-D-L grid and each manager's Nemesis & Bunny.
- **Around the League** — league darlings, the best players nobody owned, and
  the filthiest (most-carded) owned players.

> **DefCon needs a full scrape.** The defensive-action fields
> (`tackles`, `clearances_blocks_interceptions`, `recoveries`,
> `defensive_contribution`) come from `/element-summary/`, so they only populate
> when player history is scraped (i.e. *not* with `--skip-player-history`).
> Databases created before this feature are migrated automatically (the new
> columns are added on next open) but stay empty until the next full scrape —
> the DefCon section shows a reminder until then.

## End-of-season archive

The FPL API recycles its endpoints for the new season in early August. Once
that happens, `/element-summary/` returns next year's data and historical
per-player GW stats become unreachable. To preserve a full season snapshot:

```bash
# Run a full scrape (Tue/Wed after GW38 for finalised bonus points)
docker compose run --rm fpl scrape

# Snapshot the DB before next season starts
cp data/fpl.db data/fpl-2025-26.db

# Optional: also archive any final reports
cp -r data/reports data/reports-2025-26
```

Next season opens with a fresh `fpl.db` (just delete or rename the current one
before the first scrape of the new season). Historical DBs can be queried side
by side via `ATTACH DATABASE` in sqlite3.

## Architecture

```
src/fpl/
├── cli.py        # argparse dispatcher
├── client.py     # async FPL API client (retries + concurrency cap)
├── scrape.py     # pipeline: bootstrap → league → managers → players
├── db.py         # schema + Store with idempotent upserts
├── views.sql     # derived SQL views (captain points, transfer P&L, …)
└── report.py     # markdown report generator over the views
```

### Schema

**Reference data** (refreshed every scrape):
- `teams`, `players`, `gameweeks`, `fixtures`
- `player_gameweeks` — per-player per-fixture stats incl. xG/xA/xGI/xGC

**Manager data** (per league):
- `managers` — entry IDs, names, league membership
- `manager_gameweeks` — points/rank/value/transfers per GW
- `manager_picks` — 15-man squad per GW with multiplier + captain flags
- `manager_chips` — WC / BB / TC / FH usage
- `manager_transfers` — full transfer log with timestamps
- `manager_past_seasons` — historical season summaries for returning managers

**Views** (recreated on every connection — edit `views.sql` and re-open):
- `v_player_event_points` — sums DGW fixtures per (player, event)
- `v_pick_points` — picks joined to actual points, multiplier applied
- `v_captain_points` — captain return per manager per GW
- `v_bench_points` — bench points per manager per GW
- `v_transfer_pnl` — gross transfer P&L per manager per GW
- `v_ownership_event` — league ownership per player per event
- `v_leaderboard_latest` — most recent GW row per manager

## Data freshness

Each `scrape` run is idempotent — safe to re-run on the same GW. FPL updates
bonus points and finalises stats a few hours after Monday's late game, so the
ideal cadence is Tuesday morning. Add a cron entry on the host:

```cron
# Tuesday 08:00 UK time
0 8 * * 2 cd /path/to/fpl-scraper && docker compose run --rm fpl scrape && docker compose run --rm fpl report
```

## Updating the project

When the schema or code changes (e.g. pulling in updates):

```bash
docker compose build                # rebuild the image
docker compose run --rm fpl scrape  # new tables added via CREATE IF NOT EXISTS
```

SQLite migrations are handled by `CREATE TABLE IF NOT EXISTS` — new tables
appear cleanly on the next scrape. **Modifying an existing table's columns**
would require an explicit `ALTER TABLE` step or a rebuild from scratch
(`rm data/fpl.db && docker compose run --rm fpl scrape`).

## Inspecting the data

```bash
# SQL shell inside the container
docker compose run --rm fpl shell

# Or from the host directly (no Docker needed for read-only inspection)
sqlite3 data/fpl.db

# Common queries
.schema managers
SELECT * FROM v_leaderboard_latest ORDER BY total_points DESC;
SELECT * FROM v_captain_points WHERE event = 37 ORDER BY captain_effective_points DESC;
```

From Python:

```python
import sqlite3, pandas as pd
con = sqlite3.connect("data/fpl.db")
df = pd.read_sql_query("SELECT * FROM v_transfer_pnl", con)
```

## Troubleshooting

**Permission denied writing to `/data`** — the container runs as UID 1000. If
your host user has a different UID, either chown the data dir
(`sudo chown -R $USER data/`) or rebuild with a matching UID in the Dockerfile.

**`403 Forbidden` from FPL API** — the API blocks some cloud/data-center IP
ranges. Run from a residential or office network. Adding a more browser-like
User-Agent in `client.py` can also help.

**Scrape hangs on `element-summary`** — the per-player stage makes ~700
requests. If it stalls, lower concurrency: `--concurrency 5`. To skip it
entirely on a quick weekly refresh, use `--skip-player-history`.

**`No gameweek data found for league X`** — the scrape hit the league
standings but no manager data was written. Most often a typo in
`FPL_LEAGUE_ID`, or the league is private/draft (this scraper only supports
public classic leagues).

## Extending to PDF reports

The report generator is intentionally simple markdown. The data layer
(tables + views) already supports the full Dossier-style PDF — when you want
that:

1. Add `jinja2` and `weasyprint` to `requirements.txt`
2. Create `src/fpl/templates/dossier.html` with the layout
3. Add a `--format pdf` flag to `fpl report`

Each Dossier section maps to one query against the views:

| Dossier section | View / table |
|-----------------|--------------|
| Leaderboard | `v_leaderboard_latest` |
| Hall of Fame / Shame | `manager_gameweeks` ordered |
| Manager of the Week | `manager_gameweeks` where `event = current` |
| Wheeler Dealer / Rogue Trader | `v_transfer_pnl` |
| Captain's Corner | `v_captain_points` |
| Most Captained | `manager_picks` where `is_captain` |
| Template / Differential | `v_ownership_event` + bootstrap `selected_by_percent` |
| Bench Points | `v_bench_points` |
| Scout Report (xG underperformers) | `player_gameweeks` aggregated |
| Who Should Be Top? | `v_pick_points` + projected points model |
| Manager history / longevity | `manager_past_seasons` |

## Local development (no Docker)

```bash
pip install -r requirements.txt
PYTHONPATH=src python -m fpl scrape --league 59720 --db ./data/fpl.db
PYTHONPATH=src python -m fpl report --league 59720 --db ./data/fpl.db
```

## Data source

All data comes from the public FPL API at `fantasy.premierleague.com/api`.
No authentication required. Default concurrency of 10 is comfortably below
what the API tolerates; the client has exponential backoff on 429s if you
push it.
