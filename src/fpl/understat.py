"""Understat scraper — xG, xA, xGChain, PPDA and deep completions.

The FPL API gives points, minutes and its own xG/xA. It does not give shot
volume, key passes, xGChain/xGBuildup, or any team pressing metric, and those
are roughly half the feature set of the published forecasting models.

Understat used to embed its data in the page as `JSON.parse('...')`. It no
longer does — the tables are filled by AJAX after load, so fetching the HTML
returns an empty shell and looks exactly like a block. The data is still public;
it just moved to the endpoints the site's own JavaScript calls:

    GET  /getLeagueData/{league}/{season}   teams (per-match history), players, dates
    GET  /getPlayerData/{id}                one player's match-by-match history
    POST /main/getPlayersStats/             season aggregates incl. xGChain/xGBuildup

There is no official API and no documented rate limit, so this pings politely
and caches into SQLite rather than re-fetching.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
import urllib.parse
import urllib.request
from pathlib import Path

log = logging.getLogger(__name__)

BASE = "https://understat.com"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
DELAY = 1.0          # seconds between requests; the site is small, be gentle

SCHEMA = """
CREATE TABLE IF NOT EXISTS understat_teams (
    season      TEXT NOT NULL,
    team_id     INTEGER NOT NULL,
    title       TEXT NOT NULL,
    PRIMARY KEY (season, team_id)
);

-- One row per team per match: the pressing and territory numbers that have no
-- equivalent anywhere in the FPL API.
CREATE TABLE IF NOT EXISTS understat_team_matches (
    season           TEXT NOT NULL,
    team_id          INTEGER NOT NULL,
    date             TEXT NOT NULL,
    home             INTEGER,
    xg               REAL,
    xga              REAL,
    npxg             REAL,
    npxga            REAL,
    ppda_att         REAL,
    ppda_def         REAL,
    ppda_allowed_att REAL,
    ppda_allowed_def REAL,
    deep             INTEGER,
    deep_allowed     INTEGER,
    scored           INTEGER,
    missed           INTEGER,
    xpts             REAL,
    result           TEXT,
    PRIMARY KEY (season, team_id, date)
);

CREATE TABLE IF NOT EXISTS understat_players (
    season      TEXT NOT NULL,
    player_id   INTEGER NOT NULL,
    player_name TEXT,
    team_title  TEXT,
    position    TEXT,
    games       INTEGER,
    time        INTEGER,
    goals       INTEGER,
    assists     INTEGER,
    xg          REAL,
    xa          REAL,
    npg         INTEGER,
    npxg        REAL,
    shots       INTEGER,
    key_passes  INTEGER,
    xgchain     REAL,
    xgbuildup   REAL,
    PRIMARY KEY (season, player_id)
);

CREATE TABLE IF NOT EXISTS understat_player_matches (
    season      TEXT NOT NULL,
    player_id   INTEGER NOT NULL,
    match_id    INTEGER NOT NULL,
    date        TEXT,
    h_team      TEXT,
    a_team      TEXT,
    position    TEXT,
    minutes     INTEGER,
    goals       INTEGER,
    assists     INTEGER,
    shots       INTEGER,
    key_passes  INTEGER,
    xg          REAL,
    xa          REAL,
    npg         INTEGER,
    npxg        REAL,
    PRIMARY KEY (season, player_id, match_id)
);
"""


def _get(path: str, referer: str | None = None) -> dict:
    req = urllib.request.Request(
        f"{BASE}/{path.lstrip('/')}",
        headers={"User-Agent": UA,
                 "X-Requested-With": "XMLHttpRequest",
                 "Accept-Encoding": "gzip, deflate",
                 "Referer": referer or BASE},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        raw = r.read()
        if r.headers.get("Content-Encoding") == "gzip":
            import gzip
            raw = gzip.decompress(raw)
    return json.loads(raw.decode("utf-8"))


def _post(path: str, data: dict, referer: str | None = None) -> dict:
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(
        f"{BASE}/{path.lstrip('/')}", data=body,
        headers={"User-Agent": UA,
                 "X-Requested-With": "XMLHttpRequest",
                 "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                 "Accept-Encoding": "gzip, deflate",
                 "Referer": referer or BASE},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        raw = r.read()
        if r.headers.get("Content-Encoding") == "gzip":
            import gzip
            raw = gzip.decompress(raw)
    return json.loads(raw.decode("utf-8"))


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _i(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def scrape_season(conn: sqlite3.Connection, season: str, league: str = "EPL",
                  with_player_matches: bool = True,
                  player_limit: int | None = None) -> dict:
    """Pull one Understat season into the DB. `season` is the start year, e.g. '2026'."""
    conn.executescript(SCHEMA)

    league_data = _get(f"getLeagueData/{league}/{season}",
                       referer=f"{BASE}/league/{league}/{season}")
    time.sleep(DELAY)

    teams = league_data.get("teams") or {}
    n_tm = 0
    for tid, t in teams.items():
        conn.execute("INSERT OR REPLACE INTO understat_teams VALUES (?,?,?)",
                     (season, int(t["id"]), t["title"]))
        for h in t.get("history", []):
            ppda = h.get("ppda") or {}
            ppda_a = h.get("ppda_allowed") or {}
            conn.execute(
                """INSERT OR REPLACE INTO understat_team_matches
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (season, int(t["id"]), h["date"], 1 if h.get("h_a") == "h" else 0,
                 _f(h.get("xG")), _f(h.get("xGA")), _f(h.get("npxG")), _f(h.get("npxGA")),
                 _f(ppda.get("att")), _f(ppda.get("def")),
                 _f(ppda_a.get("att")), _f(ppda_a.get("def")),
                 _i(h.get("deep")), _i(h.get("deep_allowed")),
                 _i(h.get("scored")), _i(h.get("missed")),
                 _f(h.get("xpts")), h.get("result")))
            n_tm += 1

    # Season aggregates carry xGChain/xGBuildup, which the per-match feed omits.
    stats = _post("main/getPlayersStats/", {"league": league, "season": season},
                  referer=f"{BASE}/league/{league}/{season}")
    time.sleep(DELAY)
    players = stats.get("players") or []
    for p in players:
        conn.execute(
            """INSERT OR REPLACE INTO understat_players
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (season, int(p["id"]), p.get("player_name"), p.get("team_title"),
             p.get("position"), _i(p.get("games")), _i(p.get("time")),
             _i(p.get("goals")), _i(p.get("assists")), _f(p.get("xG")), _f(p.get("xA")),
             _i(p.get("npg")), _f(p.get("npxG")), _i(p.get("shots")),
             _i(p.get("key_passes")), _f(p.get("xGChain")), _f(p.get("xGBuildup"))))
    conn.commit()

    n_pm = 0
    if with_player_matches:
        ids = [int(p["id"]) for p in players][:player_limit]
        for n, pid in enumerate(ids, 1):
            try:
                d = _get(f"getPlayerData/{pid}", referer=f"{BASE}/player/{pid}")
            except Exception as exc:                      # one bad player must not stop the run
                log.warning("understat player %s failed: %s", pid, exc)
                time.sleep(DELAY)
                continue
            for m in d.get("matches", []):
                if str(m.get("season")) != str(season):
                    continue                              # the feed carries a whole career
                conn.execute(
                    """INSERT OR REPLACE INTO understat_player_matches
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (season, pid, _i(m.get("id")), m.get("date"),
                     m.get("h_team"), m.get("a_team"), m.get("position"),
                     _i(m.get("time")), _i(m.get("goals")), _i(m.get("assists")),
                     _i(m.get("shots")), _i(m.get("key_passes")),
                     _f(m.get("xG")), _f(m.get("xA")),
                     _i(m.get("npg")), _f(m.get("npxG"))))
                n_pm += 1
            if n % 25 == 0:
                conn.commit()
                log.info("understat players %d/%d", n, len(ids))
            time.sleep(DELAY)
    conn.commit()
    return {"teams": len(teams), "team_matches": n_tm,
            "players": len(players), "player_matches": n_pm}
