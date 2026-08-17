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

- **The Week in Words** — a four-part editorial at the top of every report:
  *The Disgraces* (wooden spoon, captaincy disasters, bench crimes, hits that
  backfired, ghost starters), *The Shockers* (MOTW, new leaders, transfer of
  the week, chips cashed, differential hauls), *The Title Race* (standings,
  the gap, momentum and run-in stakes) and *The Nerd Corner* (league vs
  world, template-XI benchmark, DefCon, luck). Written by **Claude**
  (`claude-opus-4-8`) — via an API key or your local `claude` CLI login —
  when one is available (see [AI narrative](#ai-narrative)); otherwise generated
  from offline phrase banks seeded per league + gameweek (deterministic, so
  republishing a GW reproduces the same prose). Either way it draws only on the
  gameweek's real facts.
- **Crystal Ball** — each manager's current squad projected onto the *upcoming*
  gameweek using FPL's expected points (captain doubled). Only shown live
  mid-season, when there's an unfinished next GW (hidden on a finished-season
  archive).
- **Hall of Records** — banter awards: Mr Reliable / The Rollercoaster
  (consistency), Magnum Opus / The Stinker (best & worst single GW), Bench
  Disaster, Hit Merchant, Scrooge, The Tinkerman, Mr Template / The Hipster,
  Lone Wolf, Ride or Die, Captain Marvel / Calamity, Captain Hindsight, plus
  **The Ghost XI** (starts handed to 0-minute players), **The Bottle Job**
  (led the league, didn't win), **The Jinx** and **The FOMO Tax**.
- **By the Numbers** — a consistency table (avg / best / worst / std dev /
  weeks above league average), a rank-trajectory table (green vs red arrows),
  **The Title Race** (weeks on top, peak, fall) and **Where Your Season Came
  From** — an exact decomposition of every total into starting-XI points +
  captaincy bonus − hits, shown as deltas vs the league average.
- **Transfer Lab** — activity, hits, net transfer P&L and each manager's
  deadline-day habit, **Rage%** (transfers within a day of the previous GW's
  final whistle), **players used** (squad churn, with its correlation to final
  position), the biggest bandwagon, **Should've Kept Him** (the let-go player
  who scored most afterwards), **Sliding Doors** (last GW's XI scored on this
  GW vs what they actually did), **The Jinx** (next-week points by dropped
  players) and **Buy High, Sell Low** (form bought vs form received).
- **Defensive Contributions** — DefCon points (2025/26 scoring) for league-owned
  players and a "Park the Bus" manager ranking. **DefCon also feeds the xPts
  model**, so defenders/midfielders earning defensive points are no longer
  flagged as "lucky".
- **Attacking Returns** — the flip side: points from goals & assists, with
  league attacking kings and a "The Entertainers" manager ranking.
- **Captain's Corner** also carries a season **Captain Regret** table (actual vs
  perfect captaincy).
- **Who Should Be Top** adds **Points Above Replacement** — every manager
  scored against the league's "template manager" (most-started XI + most-
  captained player, re-picked weekly).
- **Alternative Tables** — captaincy-only and "no-hits" standings,
  **What If You'd Done Nothing** (your GW1 XI held all season) and
  **The Perfect You** (your ceiling season: perfect hindsight XI, perfect
  captain, no hits — and exactly how many points you wasted).
- **Head to Head** — every manager vs every other on weekly points, with a
  full W-D-L grid, each manager's Nemesis & Bunny, the most lopsided **Bullies**,
  and **Doppelgangers** (who's been copying who).
- **Around the League** — league darlings, the best players nobody owned, the
  filthiest (most-carded) owned players, and **The Emperor's New Clothes**
  (consistently owned but never returned).

> **DefCon needs a full scrape.** The defensive-action fields
> (`tackles`, `clearances_blocks_interceptions`, `recoveries`,
> `defensive_contribution`) come from `/element-summary/`, so they only populate
> when player history is scraped (i.e. *not* with `--skip-player-history`).
> Databases created before this feature are migrated automatically (the new
> columns are added on next open) but stay empty until the next full scrape —
> the DefCon section shows a reminder until then.

> **Crystal Ball needs a live scrape.** Projections use FPL's `ep_next`, a
> snapshot only valid for the upcoming gameweek, so it's captured at scrape time
> and the section appears only when generating the latest GW mid-season.

## AI narrative

"The Week in Words" can be written by Claude (`claude-opus-4-8`) for genuinely
bespoke, varied prose instead of the offline phrase banks. Either way it's fed
*only* the structured facts the report already computed (it can't invent players
or numbers), and the result is cached in the DB so republishing the same GW
costs nothing. There are two ways to wire it up:

**Option A — API key.** A single small API request per gameweek.

```bash
# Put your key in .env (git-ignored — never commit it). docker compose passes
# .env into the container for you.
echo 'ANTHROPIC_API_KEY=sk-ant-...' >> .env
docker compose run --rm fpl publish
```

**Option B — your `claude` CLI login (no key).** Runs the narrative through the
[Claude Code](https://claude.com/claude-code) CLI using your existing
`claude` login (your Claude subscription), so there's no API key anywhere. The
`claude` binary must be on `PATH` and logged in, which means running `fpl`
**directly** (not via the stock Docker image, which doesn't ship the CLI):

```bash
pip install -r requirements.txt
claude login                                   # one-off, if not already
PYTHONPATH=src python -m fpl publish --db data/25_26_fpl.db --narrative cli
```

The `--narrative` (`auto` | `llm` | `cli` | `phrases`) and `--refresh-narrative`
flags work on both `fpl report` and `fpl publish`:

| Value | Source |
|-------|--------|
| `llm` | Claude **API** (needs `ANTHROPIC_API_KEY`) |
| `cli` | local logged-in **`claude` CLI** (no key) |
| `phrases` | offline phrase banks (deterministic) |
| `auto` *(default)* | API key if set, else the `claude` CLI, else phrases |

Any hiccup (no key, CLI not logged in, rate limit, network, bad output) falls
back to the phrase banks, so publishing never breaks. A full `--all` backfill
makes one request per gameweek (≈£1 for a 38-GW season on the API; on the CLI it
draws from your subscription); the weekly run is a single request.

> **Never commit your key.** With Option A keep it in `.env` (already in
> `.gitignore`) or the host environment; Option B needs no key at all. The key
> is never written into `docs/`, the cache, or any tracked file.

### How the narrative stays coherent

Each week's column is fed more than the current scoreline. The model also gets a
**season-state** block (standings, the title-race margin, gameweeks remaining,
form, and running storylines such as the perennially lucky side or a serial
captaincy howler) and **last week's column** (threaded in from the cache), so it
can carry jokes forward, reference what it said before, and build momentum into
the run-in rather than treating every gameweek as a blank slate.

### Comparing to last season

Once you have an archived previous-season DB, pass it with `--prev-db` (or set
`FPL_PREV_DB`) for same-gameweek, year-on-year comparisons — a "vs Last Season"
table plus narrative hooks ("this time last year…"):

```bash
docker compose run --rm fpl publish --prev-db data/25_26_fpl.db
```

Returning managers are matched on their stable `entry_id`, so the per-manager
deltas only cover managers present in both seasons. The feature is **dormant
until a prior season exists** — with no `--prev-db`, the section simply doesn't
render. (So it activates from 26/27 onward, comparing against 25/26.)

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

## Pre-season page

Before GW1 there are no gameweek results, so the normal dossier can't render.
`fpl preseason` builds the equivalent from what does exist — the archived
previous season plus the new season's prices, fixtures and deadlines — and
publishes it as **`GW0.html`**, so it slots into the existing season/gameweek
picker (labelled "Pre-Season") and the root redirect points at it until GW1
lands.

```bash
# Publish into docs/<season>/GW0.html and refresh the manifest + index
fpl preseason --prev-db data/25_26_fpl.db --url https://you.github.io/fpl-dossier/

# Or write a standalone file without touching docs/
fpl preseason --prev-db data/25_26_fpl.db -o /tmp/preseason.html
```

What's on it:

* **Predictions** — falsifiable, data-derived calls (title, dark horse, bottle
  watch, DefCon king, the trap, the bounce-back, fast start, template, hit
  merchant, the floor). Each one cites the number it came from.
* **Copy for WhatsApp** — one button copies a plain-text version, with
  WhatsApp's own `*bold*`/`_italic_` markup, straight to the clipboard.
  "Preview text" shows it first.
* **Where We Left Off** — final table, champion's margin, best/worst single
  gameweek, points left on the bench, longest spell top.
* **Player Intel** — DefCon kings, regression watch (biggest xG
  overperformers), bounce-back candidates (biggest underperformers), best
  value under £7.0m, and the ever-presents — all last season's underlying
  numbers priced at this season's cost.
* **Fixture Intel** — kindest and toughest opening runs, plus every GW1 tie.
* **The Market** — most expensive, and the current template by ownership.

`--prev-db` is optional: without it the league-history section and player
intel are omitted and the page falls back to fixtures and market only, so a
brand-new league still gets a usable page. Everything is deterministic — no
API key or LLM needed.

## Reddit grounding input

r/FantasyPL is a useful sanity check on the model (template picks, injury
chatter, who the community rates). **No setup required** — it works out of the
box:

```bash
# Top posts of the past week, with the 8 best comments on each
fpl reddit --limit 50 --time week

# What's hot right now, posts only (much faster — no per-post requests)
fpl reddit --sort hot --comments 0

# Any subreddit / window
fpl reddit --sub FPL --sort top --time month --out data/reddit_month.json
```

Reddit's plain `.json` endpoints now return a 403 HTML block page to every
unauthenticated client regardless of User-Agent, but the **public Atom feeds
still work**, so that is the default backend. Two consequences:

* RSS exposes no scores or comment counts — posts come back in Reddit's own
  ranking order instead (`rank` in the JSON).
* Rate limits are strict. Requests are paced ~3s apart with exponential
  backoff, so `--comments 8` over 50 posts takes a few minutes. Fetching
  posts only (`--comments 0`) is one request. If you get rate-limited, wait a
  few minutes — failures degrade to a warning and the run still writes output.

**Optional OAuth upgrade** — higher limits, plus scores and comment counts.
Create a free "script" app at <https://www.reddit.com/prefs/apps> (redirect URI
`http://localhost:8080`) and add to `.env`, which is git-ignored:

```bash
REDDIT_CLIENT_ID=your_id
REDDIT_CLIENT_SECRET=your_secret
```

It is then used automatically; force either backend with `--backend rss|oauth`.
Read-only app-only auth — no Reddit password. If your app insists on the
password grant, also set `REDDIT_USERNAME`/`REDDIT_PASSWORD`.

Two files are written: the raw `.json` (everything) and a `.md` digest
(most-mentioned players, then posts with top comments). The digest is the one
to feed an LLM — point Claude at it and it grounds team advice in what the
community is actually saying. Player mentions are matched against `web_name`
in the current-season DB, so the counts surface real players rather than
random capitalised words.

Read-only, app-only OAuth: no Reddit account password is involved. Set
`REDDIT_USERNAME`/`REDDIT_PASSWORD` too only if your app requires the password
grant.

## New season checklist

Pre-season (any time after the FPL API flips to the new season, usually July):

```bash
# 1. Scrape the new season into a fresh DB. Pre-season this pulls players
#    (new prices), teams, gameweeks and fixtures; the league 404s until it
#    renews near GW1, which the scraper tolerates (bootstrap-only warning).
docker compose run --rm fpl scrape --db data/26_27_fpl.db

# 2. Point the env at the new DB and keep last season's archive for
#    same-week comparisons (activates the "vs Last Season" report section
#    and the narrative's cross-season context from GW1).
#    In .env:  FPL_DB=data/26_27_fpl.db
#              FPL_PREV_DB=data/25_26_fpl.db
```

Prices drift over the summer as the deadline approaches — re-run the scrape
the day before the GW1 deadline for final prices. After GW1 finishes, the
league standings exist again and the normal weekly scrape + publish loop
resumes (`--prev-db` / `FPL_PREV_DB` makes the prev-season sections light up).

Note: the `players` table stores FPL's `code` — the ID that is stable across
seasons (`id` is not) — so future cross-season player joins don't need name
matching.

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
