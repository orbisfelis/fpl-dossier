"""SQLite schema, Store class, and view loading.

All upserts are idempotent so the weekly scrape can be re-run safely.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Iterable

SCHEMA = """
-- ---------------------------------------------------------------------------
-- Reference data (refreshed on every scrape)
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS teams (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    short_name  TEXT NOT NULL,
    strength    INTEGER
);

CREATE TABLE IF NOT EXISTS players (
    id                      INTEGER PRIMARY KEY,
    web_name                TEXT NOT NULL,
    first_name              TEXT,
    second_name             TEXT,
    team_id                 INTEGER REFERENCES teams(id),
    element_type            INTEGER NOT NULL,           -- 1 GK, 2 DEF, 3 MID, 4 FWD
    now_cost                INTEGER,                    -- tenths of a million
    selected_by_percent     REAL,
    total_points            INTEGER,
    form                    REAL,
    status                  TEXT,
    ep_next                 REAL,                       -- FPL's expected points, next GW
    code                    INTEGER                     -- stable across seasons (unlike id)
);

CREATE TABLE IF NOT EXISTS gameweeks (
    id                      INTEGER PRIMARY KEY,
    name                    TEXT,
    deadline_time           TEXT,
    finished                INTEGER,
    is_current              INTEGER,
    is_next                 INTEGER,
    average_entry_score     INTEGER,
    highest_score           INTEGER
);

CREATE TABLE IF NOT EXISTS fixtures (
    id                  INTEGER PRIMARY KEY,
    event               INTEGER,
    kickoff_time        TEXT,
    team_h              INTEGER REFERENCES teams(id),
    team_a              INTEGER REFERENCES teams(id),
    team_h_score        INTEGER,
    team_a_score        INTEGER,
    finished            INTEGER,
    team_h_difficulty   INTEGER,
    team_a_difficulty   INTEGER
);

-- Per-player per-fixture stats (from /element-summary/).
-- PK uses fixture rather than event so DGWs work naturally.
CREATE TABLE IF NOT EXISTS player_gameweeks (
    element                         INTEGER NOT NULL REFERENCES players(id),
    event                           INTEGER NOT NULL,
    fixture                         INTEGER NOT NULL,
    opponent_team                   INTEGER REFERENCES teams(id),
    was_home                        INTEGER,
    kickoff_time                    TEXT,
    total_points                    INTEGER,
    minutes                         INTEGER,
    goals_scored                    INTEGER,
    assists                         INTEGER,
    clean_sheets                    INTEGER,
    goals_conceded                  INTEGER,
    own_goals                       INTEGER,
    penalties_saved                 INTEGER,
    penalties_missed                INTEGER,
    yellow_cards                    INTEGER,
    red_cards                       INTEGER,
    saves                           INTEGER,
    bonus                           INTEGER,
    bps                             INTEGER,
    influence                       REAL,
    creativity                      REAL,
    threat                          REAL,
    ict_index                       REAL,
    expected_goals                  REAL,
    expected_assists                REAL,
    expected_goal_involvements      REAL,
    expected_goals_conceded         REAL,
    clearances_blocks_interceptions INTEGER,
    recoveries                      INTEGER,
    tackles                         INTEGER,
    defensive_contribution          INTEGER,    -- CBIT (DEF) / CBIRT (MID,FWD) count
    value                           INTEGER,
    selected                        INTEGER,
    transfers_in                    INTEGER,
    transfers_out                   INTEGER,
    PRIMARY KEY (element, fixture)
);

CREATE INDEX IF NOT EXISTS idx_pgw_event   ON player_gameweeks(event);
CREATE INDEX IF NOT EXISTS idx_pgw_element ON player_gameweeks(element);

-- ---------------------------------------------------------------------------
-- Manager data
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS leagues (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS managers (
    entry_id        INTEGER PRIMARY KEY,
    entry_name      TEXT,
    player_name     TEXT,
    league_id       INTEGER NOT NULL,
    first_seen      TEXT NOT NULL,
    last_updated    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS manager_gameweeks (
    entry_id                INTEGER NOT NULL REFERENCES managers(entry_id),
    event                   INTEGER NOT NULL,
    points                  INTEGER,
    total_points            INTEGER,
    rank                    INTEGER,
    rank_sort               INTEGER,
    overall_rank            INTEGER,
    bank                    INTEGER,
    value                   INTEGER,
    event_transfers         INTEGER,
    event_transfers_cost    INTEGER,
    points_on_bench         INTEGER,
    PRIMARY KEY (entry_id, event)
);

CREATE TABLE IF NOT EXISTS manager_picks (
    entry_id            INTEGER NOT NULL REFERENCES managers(entry_id),
    event               INTEGER NOT NULL,
    element             INTEGER NOT NULL REFERENCES players(id),
    position            INTEGER NOT NULL,    -- 1..15 (XI = 1..11)
    multiplier          INTEGER NOT NULL,    -- 0 bench, 1 starter, 2 captain, 3 TC
    is_captain          INTEGER NOT NULL,
    is_vice_captain     INTEGER NOT NULL,
    PRIMARY KEY (entry_id, event, element)
);

CREATE INDEX IF NOT EXISTS idx_picks_event   ON manager_picks(event);
CREATE INDEX IF NOT EXISTS idx_picks_element ON manager_picks(element);

CREATE TABLE IF NOT EXISTS manager_chips (
    entry_id    INTEGER NOT NULL REFERENCES managers(entry_id),
    event       INTEGER NOT NULL,
    chip        TEXT NOT NULL,
    PRIMARY KEY (entry_id, event, chip)
);

CREATE TABLE IF NOT EXISTS manager_transfers (
    entry_id            INTEGER NOT NULL REFERENCES managers(entry_id),
    event               INTEGER NOT NULL,
    element_in          INTEGER NOT NULL REFERENCES players(id),
    element_in_cost     INTEGER,
    element_out         INTEGER NOT NULL REFERENCES players(id),
    element_out_cost    INTEGER,
    time                TEXT,
    PRIMARY KEY (entry_id, event, element_in, element_out)
);

CREATE INDEX IF NOT EXISTS idx_tx_event ON manager_transfers(event);

-- Historical season summaries for each manager (from /entry/{id}/history/ "past").
-- Only populated for managers who've played FPL before — newcomers get no rows.
CREATE TABLE IF NOT EXISTS manager_past_seasons (
    entry_id        INTEGER NOT NULL REFERENCES managers(entry_id),
    season_name     TEXT NOT NULL,
    total_points    INTEGER,
    rank            INTEGER,
    PRIMARY KEY (entry_id, season_name)
);
"""

VIEWS_PATH = Path(__file__).parent / "views.sql"


class Store:
    """Thin wrapper over a SQLite connection with table-specific upserts."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.executescript(SCHEMA)
        self._migrate()
        if VIEWS_PATH.exists():
            self.conn.executescript(VIEWS_PATH.read_text())

    def _migrate(self) -> None:
        """Add columns introduced after a DB was first created (SQLite has no
        ALTER ... IF NOT EXISTS, so we diff against PRAGMA table_info)."""
        pgw = {r[1] for r in self.conn.execute("PRAGMA table_info(player_gameweeks)")}
        for col in ("clearances_blocks_interceptions", "recoveries",
                    "tackles", "defensive_contribution"):
            if col not in pgw:
                self.conn.execute(
                    f"ALTER TABLE player_gameweeks ADD COLUMN {col} INTEGER")
        players = {r[1] for r in self.conn.execute("PRAGMA table_info(players)")}
        if "ep_next" not in players:
            self.conn.execute("ALTER TABLE players ADD COLUMN ep_next REAL")
        if "code" not in players:
            self.conn.execute("ALTER TABLE players ADD COLUMN code INTEGER")

    # ----- Reference -----

    def upsert_teams(self, teams: Iterable[dict]) -> None:
        self.conn.executemany(
            "INSERT OR REPLACE INTO teams (id, name, short_name, strength) VALUES (?,?,?,?)",
            [(t["id"], t["name"], t["short_name"], t.get("strength")) for t in teams],
        )

    def upsert_players(self, players: Iterable[dict]) -> None:
        self.conn.executemany(
            """INSERT OR REPLACE INTO players
                (id, web_name, first_name, second_name, team_id, element_type,
                 now_cost, selected_by_percent, total_points, form, status, ep_next,
                 code)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            [(p["id"], p["web_name"], p.get("first_name"), p.get("second_name"),
              p["team"], p["element_type"], p["now_cost"],
              _to_float(p.get("selected_by_percent")),
              p.get("total_points"),
              _to_float(p.get("form")),
              p.get("status"),
              _to_float(p.get("ep_next")),
              p.get("code")) for p in players],
        )

    def upsert_gameweeks(self, events: Iterable[dict]) -> None:
        self.conn.executemany(
            """INSERT OR REPLACE INTO gameweeks
                (id, name, deadline_time, finished, is_current, is_next,
                 average_entry_score, highest_score)
                VALUES (?,?,?,?,?,?,?,?)""",
            [(e["id"], e["name"], e["deadline_time"], int(e["finished"]),
              int(e["is_current"]), int(e["is_next"]),
              e.get("average_entry_score"), e.get("highest_score")) for e in events],
        )

    def upsert_fixtures(self, fixtures: Iterable[dict]) -> None:
        self.conn.executemany(
            """INSERT OR REPLACE INTO fixtures
                (id, event, kickoff_time, team_h, team_a, team_h_score, team_a_score,
                 finished, team_h_difficulty, team_a_difficulty)
                VALUES (?,?,?,?,?,?,?,?,?,?)""",
            [(f["id"], f.get("event"), f.get("kickoff_time"),
              f["team_h"], f["team_a"],
              f.get("team_h_score"), f.get("team_a_score"),
              int(f.get("finished") or 0),
              f.get("team_h_difficulty"), f.get("team_a_difficulty"))
             for f in fixtures],
        )

    def upsert_player_history(self, element_id: int, history: Iterable[dict]) -> None:
        rows = []
        for h in history:
            rows.append((
                element_id,
                h["round"],
                h["fixture"],
                h.get("opponent_team"),
                int(h.get("was_home") or 0),
                h.get("kickoff_time"),
                h.get("total_points"),
                h.get("minutes"),
                h.get("goals_scored"),
                h.get("assists"),
                h.get("clean_sheets"),
                h.get("goals_conceded"),
                h.get("own_goals"),
                h.get("penalties_saved"),
                h.get("penalties_missed"),
                h.get("yellow_cards"),
                h.get("red_cards"),
                h.get("saves"),
                h.get("bonus"),
                h.get("bps"),
                _to_float(h.get("influence")),
                _to_float(h.get("creativity")),
                _to_float(h.get("threat")),
                _to_float(h.get("ict_index")),
                _to_float(h.get("expected_goals")),
                _to_float(h.get("expected_assists")),
                _to_float(h.get("expected_goal_involvements")),
                _to_float(h.get("expected_goals_conceded")),
                h.get("clearances_blocks_interceptions"),
                h.get("recoveries"),
                h.get("tackles"),
                h.get("defensive_contribution"),
                h.get("value"),
                h.get("selected"),
                h.get("transfers_in"),
                h.get("transfers_out"),
            ))
        if not rows:
            return
        self.conn.executemany(
            """INSERT OR REPLACE INTO player_gameweeks
                (element, event, fixture, opponent_team, was_home, kickoff_time,
                 total_points, minutes, goals_scored, assists, clean_sheets,
                 goals_conceded, own_goals, penalties_saved, penalties_missed,
                 yellow_cards, red_cards, saves, bonus, bps,
                 influence, creativity, threat, ict_index,
                 expected_goals, expected_assists, expected_goal_involvements,
                 expected_goals_conceded,
                 clearances_blocks_interceptions, recoveries, tackles,
                 defensive_contribution,
                 value, selected, transfers_in, transfers_out)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            rows,
        )

    # ----- Managers -----

    def upsert_league(self, league_id: int, name: str) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO leagues (id, name) VALUES (?,?)",
            (league_id, name),
        )

    def upsert_manager(self, entry_id: int, entry_name: str, player_name: str,
                       league_id: int, now: str) -> None:
        self.conn.execute(
            """INSERT INTO managers (entry_id, entry_name, player_name, league_id,
                                     first_seen, last_updated)
                 VALUES (?,?,?,?,?,?)
                 ON CONFLICT(entry_id) DO UPDATE SET
                     entry_name   = excluded.entry_name,
                     player_name  = excluded.player_name,
                     last_updated = excluded.last_updated""",
            (entry_id, entry_name, player_name, league_id, now, now),
        )

    def upsert_manager_history(self, entry_id: int, current: Iterable[dict]) -> None:
        self.conn.executemany(
            """INSERT OR REPLACE INTO manager_gameweeks
                (entry_id, event, points, total_points, rank, rank_sort, overall_rank,
                 bank, value, event_transfers, event_transfers_cost, points_on_bench)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            [(entry_id, h["event"], h["points"], h["total_points"], h.get("rank"),
              h.get("rank_sort"), h.get("overall_rank"), h.get("bank"), h.get("value"),
              h.get("event_transfers"), h.get("event_transfers_cost"),
              h.get("points_on_bench")) for h in current],
        )

    def upsert_picks(self, entry_id: int, event: int, picks: Iterable[dict]) -> None:
        rows = [(entry_id, event, p["element"], p["position"], p["multiplier"],
                 int(p["is_captain"]), int(p["is_vice_captain"])) for p in picks]
        if not rows:
            return
        self.conn.executemany(
            """INSERT OR REPLACE INTO manager_picks
                (entry_id, event, element, position, multiplier,
                 is_captain, is_vice_captain)
                VALUES (?,?,?,?,?,?,?)""",
            rows,
        )

    def upsert_chips(self, entry_id: int, chips: Iterable[dict]) -> None:
        rows = [(entry_id, c["event"], c["name"]) for c in chips]
        if not rows:
            return
        self.conn.executemany(
            "INSERT OR REPLACE INTO manager_chips (entry_id, event, chip) VALUES (?,?,?)",
            rows,
        )

    def upsert_transfers(self, transfers: Iterable[dict]) -> None:
        rows = [(t["entry"], t["event"],
                 t["element_in"], t.get("element_in_cost"),
                 t["element_out"], t.get("element_out_cost"),
                 t.get("time")) for t in transfers]
        if not rows:
            return
        self.conn.executemany(
            """INSERT OR REPLACE INTO manager_transfers
                (entry_id, event, element_in, element_in_cost,
                 element_out, element_out_cost, time)
                VALUES (?,?,?,?,?,?,?)""",
            rows,
        )

    def upsert_past_seasons(self, entry_id: int, past: Iterable[dict]) -> None:
        rows = [(entry_id, p["season_name"], p.get("total_points"), p.get("rank"))
                for p in past]
        if not rows:
            return
        self.conn.executemany(
            """INSERT OR REPLACE INTO manager_past_seasons
                (entry_id, season_name, total_points, rank)
                VALUES (?,?,?,?)""",
            rows,
        )

    # ----- Lifecycle -----

    def commit(self) -> None:
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()


def _to_float(value) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
