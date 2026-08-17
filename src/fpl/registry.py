"""Season-spanning identity registry.

FPL recycles nothing: a mini-league is re-created each year under a new
``league_id``, and every manager gets a new ``entry_id``. Team names change
too, and so occasionally does the display name a manager plays under. That
leaves no durable key for "the same league last year" or "the same person last
year" — which is exactly what year-on-year comparisons need.

This module keeps a small SQLite file (default ``data/registry.db``) that
outlives any single season and records, per season:

    lineage   one row per logical league    ("tml")
    league    lineage + season -> league_id
    person    one row per human             (stable person_id)
    identity  person + season -> entry_id

Names are only used to *propose* links when syncing a new season. Once a link
exists it is authoritative, so a manager can rename themselves freely
afterwards. Bad guesses are repaired with ``fpl registry link``.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from pathlib import Path

log = logging.getLogger(__name__)

DEFAULT_REGISTRY = Path("data/registry.db")

SCHEMA = """
PRAGMA foreign_keys = ON;

-- A logical league, persisting across seasons under many different ids.
CREATE TABLE IF NOT EXISTS lineage (
    lineage_id  TEXT PRIMARY KEY,          -- slug, e.g. "tml"
    label       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS league (
    lineage_id  TEXT NOT NULL REFERENCES lineage(lineage_id),
    season      TEXT NOT NULL,             -- "25-26"
    league_id   INTEGER NOT NULL,
    name        TEXT,
    PRIMARY KEY (lineage_id, season),
    UNIQUE (season, league_id)
);

-- A human. person_id is the only identifier that never changes.
CREATE TABLE IF NOT EXISTS person (
    person_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    display_name TEXT NOT NULL,            -- most recent name seen
    match_key    TEXT NOT NULL             -- normalised, for auto-linking
);

CREATE TABLE IF NOT EXISTS identity (
    person_id   INTEGER NOT NULL REFERENCES person(person_id),
    lineage_id  TEXT NOT NULL REFERENCES lineage(lineage_id),
    season      TEXT NOT NULL,
    entry_id    INTEGER NOT NULL,
    entry_name  TEXT,
    player_name TEXT,
    PRIMARY KEY (lineage_id, season, entry_id),
    UNIQUE (lineage_id, season, person_id)
);

CREATE INDEX IF NOT EXISTS idx_identity_person ON identity(person_id);
CREATE INDEX IF NOT EXISTS idx_person_key ON person(match_key);
"""


def match_key(name: str | None) -> str:
    """Normalise a manager name for auto-linking: case, spacing, punctuation."""
    cleaned = re.sub(r"[^\w\s]", "", (name or ""), flags=re.UNICODE)
    return " ".join(cleaned.split()).casefold()


def slugify(label: str) -> str:
    slug = re.sub(r"[^\w]+", "-", (label or "").strip().casefold()).strip("-")
    return slug or "league"


def open_registry(path: Path = DEFAULT_REGISTRY) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


# ---------------------------------------------------------------------------
# lookups — what the report code actually needs
# ---------------------------------------------------------------------------

def lineage_for_league(conn: sqlite3.Connection, league_id: int,
                       season: str | None = None) -> str | None:
    sql = "SELECT lineage_id FROM league WHERE league_id = ?"
    args: list = [league_id]
    if season:
        sql += " AND season = ?"
        args.append(season)
    row = conn.execute(sql + " LIMIT 1", args).fetchone()
    return row["lineage_id"] if row else None


def league_id_for_season(conn: sqlite3.Connection, lineage_id: str,
                         season: str) -> int | None:
    row = conn.execute(
        "SELECT league_id FROM league WHERE lineage_id = ? AND season = ?",
        (lineage_id, season)).fetchone()
    return row["league_id"] if row else None


def previous_league_id(conn: sqlite3.Connection, league_id: int) -> int | None:
    """The id this same league used in the season before the one `league_id`
    belongs to — the whole reason this module exists."""
    row = conn.execute(
        "SELECT lineage_id, season FROM league WHERE league_id = ? LIMIT 1",
        (league_id,)).fetchone()
    if not row:
        return None
    prev = conn.execute(
        """SELECT league_id FROM league
           WHERE lineage_id = ? AND season < ?
           ORDER BY season DESC LIMIT 1""",
        (row["lineage_id"], row["season"])).fetchone()
    return prev["league_id"] if prev else None


def person_by_entry(conn: sqlite3.Connection, season: str,
                    entry_id: int) -> int | None:
    row = conn.execute(
        "SELECT person_id FROM identity WHERE season = ? AND entry_id = ?",
        (season, entry_id)).fetchone()
    return row["person_id"] if row else None


def entry_by_person(conn: sqlite3.Connection, lineage_id: str, season: str,
                    person_id: int) -> int | None:
    row = conn.execute(
        """SELECT entry_id FROM identity
           WHERE lineage_id = ? AND season = ? AND person_id = ?""",
        (lineage_id, season, person_id)).fetchone()
    return row["entry_id"] if row else None


def entry_map(conn: sqlite3.Connection, lineage_id: str,
              from_season: str, to_season: str) -> dict[int, int]:
    """entry_id in `from_season` -> entry_id in `to_season`, same person."""
    rows = conn.execute(
        """SELECT a.entry_id AS src, b.entry_id AS dst
           FROM identity a JOIN identity b
             ON a.person_id = b.person_id AND a.lineage_id = b.lineage_id
           WHERE a.lineage_id = ? AND a.season = ? AND b.season = ?""",
        (lineage_id, from_season, to_season)).fetchall()
    return {r["src"]: r["dst"] for r in rows}


# ---------------------------------------------------------------------------
# sync
# ---------------------------------------------------------------------------

def sync_season(conn: sqlite3.Connection, season_db: Path, season: str,
                league_id: int, lineage_id: str | None = None,
                label: str | None = None) -> dict:
    """Record a season's league id and roster, linking managers to the people
    already known. Returns a summary for the CLI to print.

    Linking is by normalised name and only ever *adds* links; an existing link
    always wins, so renaming yourself after a sync is harmless.
    """
    src = sqlite3.connect(season_db)
    src.row_factory = sqlite3.Row
    try:
        league_row = src.execute("SELECT name FROM leagues WHERE id = ?",
                                 (league_id,)).fetchone()
        managers = src.execute(
            """SELECT entry_id, entry_name, player_name FROM managers
               WHERE league_id = ? ORDER BY entry_name""",
            (league_id,)).fetchall()
    finally:
        src.close()

    league_name = (league_row["name"] if league_row else None) or label or "League"
    if lineage_id is None:
        lineage_id = lineage_for_league(conn, league_id)
    if lineage_id is None:
        # Same label as an existing lineage => same league, new season.
        existing = conn.execute(
            "SELECT lineage_id FROM lineage WHERE label = ? LIMIT 1",
            (league_name,)).fetchone()
        lineage_id = existing["lineage_id"] if existing else slugify(league_name)

    conn.execute(
        "INSERT OR IGNORE INTO lineage (lineage_id, label) VALUES (?,?)",
        (lineage_id, league_name))
    conn.execute(
        """INSERT INTO league (lineage_id, season, league_id, name)
           VALUES (?,?,?,?)
           ON CONFLICT(lineage_id, season)
             DO UPDATE SET league_id = excluded.league_id, name = excluded.name""",
        (lineage_id, season, league_id, league_name))

    linked, created, already = 0, 0, 0
    for m in managers:
        entry_id, player_name = m["entry_id"], m["player_name"]
        existing = conn.execute(
            """SELECT person_id FROM identity
               WHERE lineage_id = ? AND season = ? AND entry_id = ?""",
            (lineage_id, season, entry_id)).fetchone()
        if existing:
            already += 1
            person_id = existing["person_id"]
        else:
            key = match_key(player_name)
            # Only consider people not already claimed for this season.
            cand = conn.execute(
                """SELECT p.person_id FROM person p
                   WHERE p.match_key = ?
                     AND p.person_id NOT IN (
                       SELECT person_id FROM identity
                       WHERE lineage_id = ? AND season = ?)
                   ORDER BY p.person_id LIMIT 1""",
                (key, lineage_id, season)).fetchone()
            if cand:
                person_id = cand["person_id"]
                linked += 1
            else:
                cur = conn.execute(
                    "INSERT INTO person (display_name, match_key) VALUES (?,?)",
                    (player_name, key))
                person_id = cur.lastrowid
                created += 1
            conn.execute(
                """INSERT INTO identity
                     (person_id, lineage_id, season, entry_id, entry_name, player_name)
                   VALUES (?,?,?,?,?,?)""",
                (person_id, lineage_id, season, entry_id,
                 m["entry_name"], player_name))
        conn.execute("UPDATE person SET display_name = ? WHERE person_id = ?",
                     (player_name, person_id))
    conn.commit()

    prev = previous_league_id(conn, league_id)
    return {"lineage_id": lineage_id, "label": league_name, "season": season,
            "league_id": league_id, "managers": len(managers),
            "linked": linked, "created": created, "already": already,
            "previous_league_id": prev}


def link(conn: sqlite3.Connection, lineage_id: str, season: str,
         entry_id: int, person_id: int) -> None:
    """Repair a mis-linked manager (e.g. someone who changed their name)."""
    conn.execute(
        """INSERT INTO identity (person_id, lineage_id, season, entry_id)
           VALUES (?,?,?,?)
           ON CONFLICT(lineage_id, season, entry_id)
             DO UPDATE SET person_id = excluded.person_id""",
        (person_id, lineage_id, season, entry_id))
    conn.commit()


def roster(conn: sqlite3.Connection, lineage_id: str | None = None) -> list[dict]:
    """Every person and the entry_id they used each season."""
    seasons = [r["season"] for r in conn.execute(
        "SELECT DISTINCT season FROM identity"
        + (" WHERE lineage_id = ?" if lineage_id else "")
        + " ORDER BY season", (lineage_id,) if lineage_id else ())]
    rows = conn.execute(
        """SELECT i.person_id, p.display_name, i.season, i.entry_id, i.entry_name
           FROM identity i JOIN person p ON p.person_id = i.person_id"""
        + (" WHERE i.lineage_id = ?" if lineage_id else "")
        + " ORDER BY p.display_name, i.season",
        (lineage_id,) if lineage_id else ()).fetchall()
    people: dict[int, dict] = {}
    for r in rows:
        rec = people.setdefault(r["person_id"], {
            "person_id": r["person_id"], "name": r["display_name"], "seasons": {}})
        rec["seasons"][r["season"]] = {"entry_id": r["entry_id"],
                                       "entry_name": r["entry_name"]}
    return [{"all_seasons": seasons, **p} for p in people.values()]


def prev_entry_translation(conn: sqlite3.Connection,
                           current_league_id: int) -> dict[int, int]:
    """{previous season's entry_id -> this season's entry_id} for one league.

    Lets year-on-year code join on ids again, without caring that FPL issued a
    fresh set, and without depending on anyone's display name.
    """
    row = conn.execute(
        "SELECT lineage_id, season FROM league WHERE league_id = ? LIMIT 1",
        (current_league_id,)).fetchone()
    if not row:
        return {}
    prev = conn.execute(
        """SELECT season FROM league
           WHERE lineage_id = ? AND season < ? ORDER BY season DESC LIMIT 1""",
        (row["lineage_id"], row["season"])).fetchone()
    if not prev:
        return {}
    return entry_map(conn, row["lineage_id"], prev["season"], row["season"])
