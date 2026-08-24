"""Report generator — Markdown and PDF.

Collects data from the SQL views into a plain dict, then renders it as
either Markdown (default) or a styled PDF via Jinja2 + WeasyPrint.
"""

from __future__ import annotations

import json
import logging
import math
import os
import random
import re
import shutil
import sqlite3
import statistics
import subprocess
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from itertools import groupby
from pathlib import Path
from textwrap import dedent

from .db import active_clause

POS_LABEL = {1: "GK", 2: "DEF", 3: "MID", 4: "FWD"}
GOAL_PTS = {1: 6, 2: 6, 3: 5, 4: 4}  # FPL points per goal by position

# Legal FPL formations as (DEF, MID, FWD); GK is always exactly 1.
FORMATIONS = [(3, 4, 3), (3, 5, 2), (4, 3, 3), (4, 4, 2), (4, 5, 1),
              (5, 2, 3), (5, 3, 2), (5, 4, 1)]

# "Rage transfer" window: within ~2h of the final whistle of the previous GW's
# last kickoff, plus 24h of stewing time.
RAGE_WINDOW = timedelta(hours=26)


def _best_xi_points(squad: list[tuple[int, int]]) -> int | None:
    """Best hindsight XI total from a squad of (element_type, points) tuples,
    respecting formation rules. None if no legal XI exists."""
    by_pos: dict[int, list[int]] = {1: [], 2: [], 3: [], 4: []}
    for t, p in squad:
        by_pos.setdefault(t if t in by_pos else 4, []).append(p)
    for v in by_pos.values():
        v.sort(reverse=True)
    best = None
    for d, m, f in FORMATIONS:
        if not by_pos[1] or len(by_pos[2]) < d or len(by_pos[3]) < m or len(by_pos[4]) < f:
            continue
        s = by_pos[1][0] + sum(by_pos[2][:d]) + sum(by_pos[3][:m]) + sum(by_pos[4][:f])
        if best is None or s > best:
            best = s
    return best

log = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).parent / "templates"

SEASON_RE = re.compile(r"\d{2}-\d{2}")
GW_FILE_RE = re.compile(r"GW(\d+)\.html")


def _ensure_columns(conn: sqlite3.Connection) -> None:
    """Add columns introduced after a DB was first created, so views built on
    them don't fail on older databases (mirrors Store._migrate)."""
    existing = {r[1] for r in conn.execute("PRAGMA table_info(player_gameweeks)")}
    for col in ("clearances_blocks_interceptions", "recoveries",
                "tackles", "defensive_contribution"):
        if col not in existing:
            conn.execute(f"ALTER TABLE player_gameweeks ADD COLUMN {col} INTEGER")
    if "ep_next" not in {r[1] for r in conn.execute("PRAGMA table_info(players)")}:
        conn.execute("ALTER TABLE players ADD COLUMN ep_next REAL")


def current_season(today: date | None = None) -> str:
    """Current FPL season as a 'YY-YY' string (the season runs Aug–May)."""
    today = today or date.today()
    start = today.year if today.month >= 7 else today.year - 1
    return f"{start % 100:02d}-{(start + 1) % 100:02d}"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def generate_report(db_path: Path, output: Path, league_id: int,
                    event: int | None = None, fmt: str = "md",
                    season: str | None = None, narrative: str = "auto",
                    refresh_narrative: bool = False,
                    prev_db: Path | None = None) -> Path:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    # Ensure post-release columns exist before views reference them (older DBs
    # created before DefCon was added won't have them until the next scrape).
    _ensure_columns(conn)

    # Rebuild views to pick up any schema changes
    views_sql = TEMPLATES_DIR.parent / "views.sql"
    if views_sql.exists():
        conn.executescript(views_sql.read_text())

    if event is None:
        row = conn.execute(
            f"""SELECT MAX(event) AS e FROM manager_gameweeks
               WHERE entry_id IN (SELECT entry_id FROM managers
                                  WHERE league_id = ?{active_clause(conn)})""",
            (league_id,),
        ).fetchone()
        event = row["e"]
        if event is None:
            raise RuntimeError(f"No gameweek data found for league {league_id}")

    log.info("Generating %s report for league %d, GW %d", fmt.upper(), league_id, event)

    season = season or current_season()
    data = _collect_data(conn, league_id, event, season=season, prev_db=prev_db)
    data["season"] = season
    conn.close()

    # Optionally let Claude write the top narrative (HTML only). "auto" prefers
    # the API when ANTHROPIC_API_KEY is set, then the local `claude` CLI login,
    # else the deterministic phrase banks. Called in every mode, because a
    # column already in the cache should be served whatever this machine can
    # generate today.
    if fmt == "html":
        mode = narrative
        if mode == "auto":
            if os.environ.get("ANTHROPIC_API_KEY"):
                mode = "llm"
            elif shutil.which("claude"):
                mode = "cli"
            else:
                mode = "phrases"
        written = write_narrative(data.get("narrative_facts") or {}, league_id,
                                  event, db_path, mode, refresh=refresh_narrative)
        if written is not None:
            data["narrative"] = written

    output.parent.mkdir(parents=True, exist_ok=True)

    if fmt == "html":
        _render_html(data, output)
    else:
        _render_markdown(data, output)

    log.info("Report → %s", output)
    return output


# ---------------------------------------------------------------------------
# GitHub Pages publishing
# ---------------------------------------------------------------------------

def publish_reports(db_path: Path, league_id: int, docs_dir: Path,
                    season: str | None = None, event: int | None = None,
                    all_gws: bool = False, narrative: str = "auto",
                    refresh_narrative: bool = False,
                    prev_db: Path | None = None,
                    force_unsealed: bool = False) -> Path:
    """Render HTML report(s) into ``docs/<season>/GW<N>.html``, then rebuild the
    manifest and the root ``index.html`` redirect that drive the GW/season
    dropdown on GitHub Pages."""
    season = season or current_season()
    season_dir = docs_dir / season
    from .seal import check_docs_writable
    check_docs_writable(season_dir, force=force_unsealed)
    season_dir.mkdir(parents=True, exist_ok=True)

    events = _available_events(db_path, league_id)
    if not events:
        raise RuntimeError(f"No gameweek data found for league {league_id}")

    if all_gws:
        targets = events
    elif event is not None:
        targets = [event]
    else:
        targets = [max(events)]

    for ev in targets:
        generate_report(db_path, season_dir / f"GW{ev}.html", league_id,
                        ev, fmt="html", season=season, narrative=narrative,
                        refresh_narrative=refresh_narrative, prev_db=prev_db)

    manifest = build_manifest(docs_dir)
    (docs_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8")
    _write_index(docs_dir, manifest)

    log.info("Published %d report(s) to %s", len(targets), season_dir)
    return season_dir


def manager_key(player_name: str | None) -> str:
    """Identity that survives a season rollover.

    entry_id is NOT stable year to year, and team names get renamed constantly,
    so the human's own name is the only durable join key between an archived
    season and the current one.
    """
    return " ".join((player_name or "").split()).casefold()


def resolve_prev_league(conn: sqlite3.Connection, league_id: int) -> int | None:
    """Map the current league_id onto the archived season's own league_id.

    Mini-leagues are re-created each year under a new id, so last season's
    archive is keyed under a different number. Order of preference:

      1. the identity registry, if it knows this lineage (authoritative)
      2. the id itself, if the archive happens to contain it
      3. the archive's largest league — these files hold one league, but this
         is a guess, so it is the last resort
    """
    try:
        from .registry import DEFAULT_REGISTRY, open_registry, previous_league_id
        if Path(os.environ.get("FPL_REGISTRY", DEFAULT_REGISTRY)).exists():
            reg = open_registry(Path(os.environ.get("FPL_REGISTRY",
                                                    DEFAULT_REGISTRY)))
            try:
                prev = previous_league_id(reg, league_id)
            finally:
                reg.close()
            if prev:
                row = conn.execute(
                    "SELECT COUNT(*) FROM managers WHERE league_id = ?",
                    (prev,)).fetchone()
                if row and row[0]:
                    return prev
    except (sqlite3.Error, ImportError, OSError):
        pass

    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM managers WHERE league_id = ?",
            (league_id,)).fetchone()
        if row and row[0]:
            return league_id
        row = conn.execute(
            """SELECT league_id FROM managers
               GROUP BY league_id ORDER BY COUNT(*) DESC LIMIT 1""").fetchone()
        return row[0] if row else None
    except sqlite3.Error:
        return None


def _prev_season_stats(prev_db_path: Path, league_id: int, event: int) -> dict | None:
    """Read the same-gameweek snapshot from an archived previous-season DB, for
    year-on-year comparisons. Returns None if the file/data isn't there, so the
    feature stays dormant until a prior season exists to point at.

    Returning managers are matched on manager name (see manager_key), so the
    per-manager deltas only cover managers present in both seasons.
    """
    try:
        if not prev_db_path or not Path(prev_db_path).exists():
            return None
        conn = sqlite3.connect(prev_db_path)
        conn.row_factory = sqlite3.Row
        prev_league = resolve_prev_league(conn, league_id)
        if prev_league is None:
            conn.close()
            return None
        rows = conn.execute(
            """SELECT m.entry_id, m.entry_name, m.player_name,
                      mg.points, mg.total_points
               FROM manager_gameweeks mg
               JOIN managers m ON m.entry_id = mg.entry_id
               WHERE m.league_id = ? AND mg.event = ?
               ORDER BY mg.total_points DESC""",
            (prev_league, event)).fetchall()
        conn.close()
    except sqlite3.Error:
        return None
    if not rows:
        return None
    # Prefer the registry: it translates last season's entry ids into this
    # season's, which survives both id churn and managers renaming themselves.
    # Name matching stays as the fallback when no registry exists.
    translate: dict[int, int] = {}
    try:
        from .registry import (DEFAULT_REGISTRY, open_registry,
                               prev_entry_translation)
        reg_path = Path(os.environ.get("FPL_REGISTRY", DEFAULT_REGISTRY))
        if reg_path.exists():
            reg = open_registry(reg_path)
            try:
                translate = prev_entry_translation(reg, league_id)
            finally:
                reg.close()
    except (sqlite3.Error, ImportError, OSError):
        translate = {}

    totals = {}
    for i, r in enumerate(rows, 1):
        rec = {"team": r["entry_name"], "total": r["total_points"], "rank": i}
        totals[manager_key(r["player_name"])] = rec
        mapped = translate.get(r["entry_id"])
        if mapped:
            totals[f"id:{mapped}"] = rec
    pts = [r["points"] or 0 for r in rows]
    return {
        "league_avg": round(sum(pts) / len(pts), 1) if pts else None,
        "leader": {"team": rows[0]["entry_name"], "total": rows[0]["total_points"]},
        "totals": totals,
    }


# A running joke, scoped to exactly one league and one season on purpose.
# Keyed by (league_id, season); delete the entry and the section disappears.
_BENCHMARK_SECTIONS = {
    (126735, "26-27"): {
        "title": "Am I doing better than Jay's girlfriend?",
        "subtitle": "Measured against her pace from last season, gameweek by gameweek",
        "benchmark_manager": "Jay Curtis",
        "benchmark_label": "Jay's girlfriend",
    },
}


def _benchmark_block(prev_db_path: Path | None, league_id: int, season: str,
                     event: int, leaderboard: list[dict]) -> dict | None:
    """Every manager measured against one nominated manager's pace from the
    archived season, at the same gameweek. Returns None unless this league and
    season have a joke configured and the archive can answer it."""
    cfg = _BENCHMARK_SECTIONS.get((league_id, season))
    if not cfg or not prev_db_path or not Path(prev_db_path).exists():
        return None
    try:
        conn = sqlite3.connect(prev_db_path)
        conn.row_factory = sqlite3.Row
        prev_league = resolve_prev_league(conn, league_id)
        row = conn.execute(
            """SELECT mg.total_points, m.entry_name
               FROM manager_gameweeks mg JOIN managers m ON m.entry_id = mg.entry_id
               WHERE m.league_id = ? AND m.player_name = ? AND mg.event = ?""",
            (prev_league, cfg["benchmark_manager"], event)).fetchone()
        final = conn.execute(
            """SELECT MAX(mg.total_points)
               FROM manager_gameweeks mg JOIN managers m ON m.entry_id = mg.entry_id
               WHERE m.league_id = ? AND m.player_name = ?""",
            (prev_league, cfg["benchmark_manager"])).fetchone()
        conn.close()
    except sqlite3.Error:
        return None
    if not row or row["total_points"] is None:
        return None

    target = row["total_points"]
    managers = []
    for r in leaderboard:
        total = r["total_points"] or 0
        managers.append({
            "entry_id": r["entry_id"], "team": r["entry_name"],
            "manager": r["player_name"], "total": total,
            "beats": total > target, "diff": total - target,
        })
    beating = sum(1 for m in managers if m["beats"])
    return {
        "title": cfg["title"], "subtitle": cfg["subtitle"],
        "label": cfg["benchmark_label"], "event": event, "target": target,
        "season_total": final[0] if final else None,
        "managers": managers, "beating": beating, "total_managers": len(managers),
    }


def _available_events(db_path: Path, league_id: int) -> list[int]:
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            """SELECT DISTINCT mg.event
               FROM manager_gameweeks mg
               JOIN managers m ON m.entry_id = mg.entry_id
               WHERE m.league_id = ?
               ORDER BY mg.event""",
            (league_id,),
        ).fetchall()
    finally:
        conn.close()
    return [r[0] for r in rows]


def build_manifest(docs_dir: Path) -> dict:
    """Scan ``docs/`` for ``<season>/GW<N>.html`` files and build the manifest
    the front-end fetches to populate the dropdowns."""
    seasons: dict[str, list[int]] = {}
    for child in sorted(docs_dir.iterdir()):
        if not child.is_dir() or not SEASON_RE.fullmatch(child.name):
            continue
        gws = sorted(
            int(m.group(1))
            for f in child.glob("GW*.html")
            if (m := GW_FILE_RE.fullmatch(f.name))
        )
        if gws:
            seasons[child.name] = gws

    latest = None
    if seasons:
        latest_season = max(seasons)
        latest = {"season": latest_season, "gw": max(seasons[latest_season])}
    return {"latest": latest, "seasons": seasons}


def _write_index(docs_dir: Path, manifest: dict) -> None:
    latest = manifest.get("latest")
    if not latest:
        return
    url = f"./{latest['season']}/GW{latest['gw']}.html"
    (docs_dir / "index.html").write_text(dedent(f"""\
        <!doctype html>
        <html lang="en">
        <head>
          <meta charset="utf-8">
          <meta http-equiv="refresh" content="0; url={url}">
          <link rel="canonical" href="{url}">
          <title>The Dossier</title>
        </head>
        <body>Redirecting to <a href="{url}">the latest Dossier</a>&hellip;</body>
        </html>
    """), encoding="utf-8")


# ---------------------------------------------------------------------------
# Optional LLM-written narrative (Claude API)
# ---------------------------------------------------------------------------

_NARRATIVE_MODEL = "claude-opus-4-8"

_NARRATIVE_SYSTEM = (
    "You are the resident columnist for a Fantasy Premier League mini-league, "
    "writing the weekly editorial in an ongoing season-long column — sharp, "
    "witty, a little merciless, but always affectionate. You are not summarising "
    "a spreadsheet; you are telling the continuing story of a title race.\n\n"
    "Write exactly four flowing paragraphs, in this order:\n"
    "1. disgraces — ruthlessly but playfully mock the week's worst decisions: the "
    "wooden spoon, captaincy howlers, bench disasters, hits that backfired, "
    "players who never got off the bench. Name names.\n"
    "2. shockers — the week's standouts: the top score, a new league leader, the "
    "transfer of the week, chips that paid off, brave differentials.\n"
    "3. title_race — the state of the championship: who leads and by how much, who "
    "is charging or fading, the gaps that matter, and — especially in the run-in "
    "— what this week did to the race with N gameweeks left. This is where the "
    "season-long arc lives; build momentum, tension and stakes.\n"
    "4. nerd — dry, analytical observations: the league vs the wider FPL world, "
    "the template-XI benchmark, defensive-contribution points, and the xPts luck "
    "story.\n\n"
    "CONTINUITY IS THE WHOLE POINT. The JSON includes a `season` block (standings, "
    "the title-race margin, gameweeks remaining, form, and `running_storylines`) "
    "and, when available, `story_so_far` — last week's column. Use them:\n"
    "- Keep the thread going: refer back to what you said last week, pick up "
    "running jokes (e.g. a manager's serial captaincy howlers, a perennially "
    "lucky side, someone bottling a lead), and let them evolve rather than "
    "resetting each week.\n"
    "- Frame the week against the bigger picture: who leads and by how much, who's "
    "closing in or fading, and — especially in the run-in — what the result means "
    "for the title with N gameweeks left. Build momentum and stakes.\n"
    "- Vary your openings and don't simply relist the facts; weave them into a "
    "story with a point of view.\n\n"
    "Hard rules: use ONLY the facts in the JSON — never invent players, managers, "
    "clubs, numbers or events, and never imply a fact that isn't there; if "
    "something is absent, leave it out. Each paragraph should run 5-8 sentences — "
    "give it room to breathe, don't pad. Wrap key team/manager/player names and "
    "standout numbers in **double asterisks**. Plain prose only — no HTML, no "
    "markdown headings, no bullet lists. British spelling. Do not address the "
    "reader as 'you'."
)

_NARRATIVE_SCHEMA = {
    "type": "object",
    "properties": {
        "disgraces": {"type": "string"},
        "shockers": {"type": "string"},
        "title_race": {"type": "string"},
        "nerd": {"type": "string"},
    },
    "required": ["disgraces", "shockers", "title_race", "nerd"],
    "additionalProperties": False,
}


def _narrative_html(text: str) -> str:
    """Escape model output, then promote **bold** markers to <strong> — so the
    rendered HTML can never contain arbitrary tags from the model or the data."""
    esc = str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", esc)


def _parse_narrative_json(text: str) -> dict | None:
    """Pull the first JSON object out of model output (tolerates ``` fences or
    stray preamble) and return it, or None if there isn't a usable one."""
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return None
    try:
        d = json.loads(m.group(0))
    except (TypeError, ValueError):
        return None
    return d if isinstance(d, dict) else None


def _shape_narrative(parsed: dict) -> list[dict] | None:
    out = [{"label": lbl, "color": col, "html": _narrative_html(parsed.get(key, ""))}
           for lbl, col, key in (
               ("The Disgraces", "var(--red)", "disgraces"),
               ("The Shockers", "var(--strong)", "shockers"),
               ("The Title Race", "#b8860b", "title_race"),
               ("The Nerd Corner", "var(--green)", "nerd"))
           if (parsed.get(key) or "").strip()]
    return out or None


def _narrative_plain_text(shaped: list[dict]) -> str:
    """Flatten a cached (shaped) narrative back to plain prose, for feeding last
    week's column to the model as continuity context."""
    parts = []
    for sec in shaped:
        body = re.sub(r"<[^>]+>", "", sec.get("html", "")).strip()
        if body:
            parts.append(f"{sec.get('label', '')}: {body}")
    return "\n".join(parts)


def _provider_api(facts: dict) -> dict | None:
    """Generate the narrative JSON via the Anthropic API (needs ANTHROPIC_API_KEY)."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    try:
        import anthropic
    except ImportError:
        log.warning("anthropic package not installed — using phrase-bank narrative")
        return None
    try:
        client = anthropic.Anthropic(timeout=150)
        resp = client.messages.create(
            model=_NARRATIVE_MODEL,
            max_tokens=4500,
            thinking={"type": "adaptive"},
            system=_NARRATIVE_SYSTEM,
            output_config={"effort": "medium",
                           "format": {"type": "json_schema", "schema": _NARRATIVE_SCHEMA}},
            messages=[{"role": "user", "content":
                       "Gameweek facts (JSON):\n```json\n"
                       + json.dumps(facts, indent=2) + "\n```\n\nWrite the three paragraphs."}],
        )
    except Exception as e:   # auth, rate limit, network, bad request, …
        log.warning("API narrative failed (%s) — using phrase-bank narrative", e)
        return None
    return _parse_narrative_json(next((b.text for b in resp.content if b.type == "text"), ""))


def _provider_cli(facts: dict) -> dict | None:
    """Generate the narrative JSON via the local ``claude`` CLI, using whatever
    Claude Code login is configured — no API key required. Needs the binary on
    PATH and an active `claude` session."""
    claude = shutil.which("claude")
    if not claude:
        log.warning("claude CLI not found on PATH — using phrase-bank narrative")
        return None
    prompt = (
        _NARRATIVE_SYSTEM
        + "\n\nGameweek facts (JSON):\n```json\n" + json.dumps(facts, indent=2)
        + "\n```\n\nReturn ONLY a JSON object with string keys "
          '"disgraces", "shockers", "title_race" and "nerd" — no other text.')
    try:
        proc = subprocess.run(
            [claude, "-p", prompt, "--model", _NARRATIVE_MODEL,
             "--output-format", "json"],
            capture_output=True, text=True, timeout=180)
    except Exception as e:   # binary missing mid-run, timeout, etc.
        log.warning("claude CLI failed (%s) — using phrase-bank narrative", e)
        return None
    if proc.returncode != 0:
        log.warning("claude CLI exited %d — using phrase-bank narrative: %s",
                    proc.returncode, (proc.stderr or "").strip()[:200])
        return None
    # `--output-format json` wraps the reply: {"result": "<text>", ...}
    text = proc.stdout
    try:
        env = json.loads(proc.stdout)
        if isinstance(env, dict) and "result" in env:
            text = env["result"]
    except (TypeError, ValueError):
        pass
    return _parse_narrative_json(text)


_NARRATIVE_PROVIDERS = {"llm": _provider_api, "cli": _provider_cli}


def read_cached_narrative(db_path: Path, league_id: int,
                          event: int) -> list[dict] | None:
    """Previously written column for this gameweek, if any."""
    try:
        cache = sqlite3.connect(db_path)
    except sqlite3.Error:
        return None
    try:
        row = cache.execute(
            "SELECT content FROM narrative_cache WHERE league_id = ? AND event = ?",
            (league_id, event)).fetchone()
    except sqlite3.Error:
        return None
    finally:
        cache.close()
    if not row:
        return None
    try:
        return json.loads(row[0])
    except (TypeError, ValueError):
        return None


def write_narrative(facts: dict, league_id: int, event: int, db_path: Path,
                    mode: str, refresh: bool = False) -> list[dict] | None:
    """Narrative paragraphs written by Claude (via the ``mode`` provider) from the
    gameweek facts, or None to fall back to the deterministic phrase-bank version.
    Returns None when there's nothing worth writing about or the provider is
    unavailable/fails — so publishing never breaks. Cached per (league, GW) in
    the DB; pass refresh=True to regenerate."""
    # A cached column is worth serving even when this machine has no provider
    # configured. It was written by one; losing it on republish just because
    # ANTHROPIC_API_KEY is unset would silently downgrade the page to phrase
    # banks and nobody would notice until they read it.
    if not refresh:
        cached = read_cached_narrative(db_path, league_id, event)
        if cached:
            return cached

    provider = _NARRATIVE_PROVIDERS.get(mode)
    if provider is None or not facts or len(facts) <= 4:  # 4 = bare header fields
        return None

    cache = sqlite3.connect(db_path)
    try:
        cache.execute(
            "CREATE TABLE IF NOT EXISTS narrative_cache ("
            "league_id INTEGER NOT NULL, event INTEGER NOT NULL, model TEXT, "
            "content TEXT, created_at TEXT, PRIMARY KEY (league_id, event))")
        # Thread in last week's column for continuity (callbacks, running jokes,
        # momentum). Sequential generation means GW N-1 is already cached.
        ctx = dict(facts)
        prev = cache.execute(
            "SELECT content FROM narrative_cache WHERE league_id = ? AND event = ?",
            (league_id, event - 1)).fetchone()
        if prev:
            try:
                ctx["story_so_far"] = {"last_week": _narrative_plain_text(json.loads(prev[0]))}
            except (TypeError, ValueError):
                pass

        parsed = provider(ctx)
        if not parsed:
            return None
        out = _shape_narrative(parsed)
        if not out:
            return None

        cache.execute(
            "INSERT OR REPLACE INTO narrative_cache "
            "(league_id, event, model, content, created_at) VALUES (?,?,?,?,?)",
            (league_id, event, _NARRATIVE_MODEL, json.dumps(out),
             datetime.now(timezone.utc).isoformat()))
        cache.commit()
        log.info("%s narrative written for GW%d", mode.upper(), event)
        return out
    finally:
        cache.close()


# ---------------------------------------------------------------------------
# Data collection
# ---------------------------------------------------------------------------

def _collect_data(conn: sqlite3.Connection, league_id: int, event: int,
                  season: str | None = None,
                  prev_db: Path | None = None) -> dict:
    """Gather every piece of data the report needs into a plain dict."""

    # --- Header ---
    # Ensure leagues table exists (may be missing in older DBs)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS leagues (id INTEGER PRIMARY KEY, name TEXT NOT NULL)"
    )
    league_row = conn.execute(
        "SELECT name FROM leagues WHERE id = ?", (league_id,)
    ).fetchone()
    league_name = league_row["name"] if league_row else f"League {league_id}"

    mgr_count = conn.execute(
        f"SELECT COUNT(*) AS c FROM managers "
        f"WHERE league_id = ?{active_clause(conn)}", (league_id,)
    ).fetchone()["c"]
    gw_avg = conn.execute(
        "SELECT AVG(points) AS a FROM manager_gameweeks mg "
        "JOIN managers m ON m.entry_id = mg.entry_id "
        "WHERE m.league_id = ? AND mg.event = ?",
        (league_id, event),
    ).fetchone()["a"]
    fpl_avg_row = conn.execute(
        "SELECT average_entry_score FROM gameweeks WHERE id = ?", (event,)
    ).fetchone()
    fpl_avg = fpl_avg_row["average_entry_score"] if fpl_avg_row else None

    # --- Hall of Fame ---
    top_overall = _rows(conn.execute("""
        SELECT m.entry_name, m.player_name, mg.total_points
        FROM manager_gameweeks mg
        JOIN managers m ON m.entry_id = mg.entry_id
        WHERE m.league_id = ? AND mg.event = ?
        ORDER BY mg.total_points DESC LIMIT 3
    """, (league_id, event)))

    motw = _rows(conn.execute("""
        SELECT m.entry_name, m.player_name, mg.points
        FROM manager_gameweeks mg
        JOIN managers m ON m.entry_id = mg.entry_id
        WHERE m.league_id = ? AND mg.event = ?
        ORDER BY mg.points DESC LIMIT 3
    """, (league_id, event)))

    wheeler = _rows(conn.execute("""
        SELECT m.entry_name, m.player_name, tp.gross_pnl, mg.event_transfers_cost
        FROM v_transfer_pnl tp
        JOIN managers m ON m.entry_id = tp.entry_id
        JOIN manager_gameweeks mg
          ON mg.entry_id = tp.entry_id AND mg.event = tp.event
        WHERE m.league_id = ? AND tp.event = ?
        ORDER BY (tp.gross_pnl - COALESCE(mg.event_transfers_cost, 0)) DESC
        LIMIT 3
    """, (league_id, event)))

    most_valuable = _rows(conn.execute("""
        SELECT m.entry_name, m.player_name, mg.value
        FROM manager_gameweeks mg
        JOIN managers m ON m.entry_id = mg.entry_id
        WHERE m.league_id = ? AND mg.event = ?
        ORDER BY mg.value DESC LIMIT 3
    """, (league_id, event)))

    # --- Hall of Shame ---
    bottom_overall = _rows(conn.execute("""
        SELECT m.entry_name, m.player_name, mg.total_points
        FROM manager_gameweeks mg
        JOIN managers m ON m.entry_id = mg.entry_id
        WHERE m.league_id = ? AND mg.event = ?
        ORDER BY mg.total_points ASC LIMIT 3
    """, (league_id, event)))

    rogue = _rows(conn.execute("""
        SELECT m.entry_name, m.player_name, tp.gross_pnl, mg.event_transfers_cost
        FROM v_transfer_pnl tp
        JOIN managers m ON m.entry_id = tp.entry_id
        JOIN manager_gameweeks mg
          ON mg.entry_id = tp.entry_id AND mg.event = tp.event
        WHERE m.league_id = ? AND tp.event = ?
        ORDER BY (tp.gross_pnl - COALESCE(mg.event_transfers_cost, 0)) ASC
        LIMIT 3
    """, (league_id, event)))

    least_valuable = _rows(conn.execute("""
        SELECT m.entry_name, m.player_name, mg.value
        FROM manager_gameweeks mg
        JOIN managers m ON m.entry_id = mg.entry_id
        WHERE m.league_id = ? AND mg.event = ?
        ORDER BY mg.value ASC LIMIT 3
    """, (league_id, event)))

    # --- Form: last 5 GWs (On a Hot Streak / Must be on Holiday) ---
    form_start = max(1, event - 4)
    num_form_gws = event - form_start + 1

    hot_streak = _rows(conn.execute("""
        SELECT m.entry_id, m.entry_name, m.player_name,
               SUM(mg.points) AS form_points
        FROM manager_gameweeks mg
        JOIN managers m ON m.entry_id = mg.entry_id
        WHERE m.league_id = ? AND mg.event BETWEEN ? AND ?
        GROUP BY m.entry_id
        ORDER BY form_points DESC LIMIT 3
    """, (league_id, form_start, event)))

    cold_streak = _rows(conn.execute("""
        SELECT m.entry_id, m.entry_name, m.player_name,
               SUM(mg.points) AS form_points
        FROM manager_gameweeks mg
        JOIN managers m ON m.entry_id = mg.entry_id
        WHERE m.league_id = ? AND mg.event BETWEEN ? AND ?
        GROUP BY m.entry_id
        ORDER BY form_points ASC LIMIT 3
    """, (league_id, form_start, event)))

    # --- Season captain points (Teacher's Pet / Lost the Dressing Room) ---
    teachers_pet = _rows(conn.execute("""
        SELECT m.entry_id, m.entry_name, m.player_name,
               SUM(cp.captain_effective_points) AS total_captain_pts
        FROM v_captain_points cp
        JOIN managers m ON m.entry_id = cp.entry_id
        WHERE m.league_id = ? AND cp.event <= ?
        GROUP BY cp.entry_id
        ORDER BY total_captain_pts DESC LIMIT 3
    """, (league_id, event)))

    lost_dressing_room = _rows(conn.execute("""
        SELECT m.entry_id, m.entry_name, m.player_name,
               SUM(cp.captain_effective_points) AS total_captain_pts
        FROM v_captain_points cp
        JOIN managers m ON m.entry_id = cp.entry_id
        WHERE m.league_id = ? AND cp.event <= ?
        GROUP BY cp.entry_id
        ORDER BY total_captain_pts ASC LIMIT 3
    """, (league_id, event)))

    # --- Balls of Steel (most differential captain picks) ---
    balls_of_steel = _rows(conn.execute("""
        SELECT m.entry_id, m.entry_name, m.player_name,
               AVG(COALESCE(own.captains * 100.0 / NULLIF(own.owners, 0), 0)) AS avg_captain_popularity
        FROM v_captain_points cp
        JOIN managers m ON m.entry_id = cp.entry_id
        LEFT JOIN v_ownership_event own ON own.event = cp.event AND own.element = cp.captain_element
        WHERE m.league_id = ? AND cp.event <= ?
        GROUP BY m.entry_id
        ORDER BY avg_captain_popularity ASC
        LIMIT 3
    """, (league_id, event)))

    # --- Balls of Cotton Wool (most template captain picks) ---
    balls_of_cotton = _rows(conn.execute("""
        SELECT m.entry_id, m.entry_name, m.player_name,
               AVG(COALESCE(own.captains * 100.0 / NULLIF(own.owners, 0), 0)) AS avg_captain_popularity
        FROM v_captain_points cp
        JOIN managers m ON m.entry_id = cp.entry_id
        LEFT JOIN v_ownership_event own ON own.event = cp.event AND own.element = cp.captain_element
        WHERE m.league_id = ? AND cp.event <= ?
        GROUP BY m.entry_id
        ORDER BY avg_captain_popularity DESC
        LIMIT 3
    """, (league_id, event)))

    # --- Enriched leaderboard ---
    leaderboard_raw = _rows(conn.execute("""
        SELECT m.entry_id, m.entry_name, m.player_name,
               mg.points, mg.total_points, mg.overall_rank,
               mg.event_transfers, mg.event_transfers_cost,
               mg.points_on_bench, mg.value
        FROM manager_gameweeks mg
        JOIN managers m ON m.entry_id = mg.entry_id
        WHERE m.league_id = ? AND mg.event = ?
        ORDER BY mg.total_points DESC
    """, (league_id, event)))

    # Previous GW standings for rank movement
    prev_league_pos = {}
    prev_overall_rank = {}
    if event > 1:
        prev_rows = _rows(conn.execute("""
            SELECT m.entry_id, mg.total_points, mg.overall_rank
            FROM manager_gameweeks mg
            JOIN managers m ON m.entry_id = mg.entry_id
            WHERE m.league_id = ? AND mg.event = ?
            ORDER BY mg.total_points DESC
        """, (league_id, event - 1)))
        for i, r in enumerate(prev_rows, 1):
            prev_league_pos[r["entry_id"]] = i
            prev_overall_rank[r["entry_id"]] = r["overall_rank"]

    # Form per manager (last 5 GWs)
    form_map = {}
    form_rows = _rows(conn.execute("""
        SELECT m.entry_id, SUM(mg.points) AS form_points
        FROM manager_gameweeks mg
        JOIN managers m ON m.entry_id = mg.entry_id
        WHERE m.league_id = ? AND mg.event BETWEEN ? AND ?
        GROUP BY m.entry_id
    """, (league_id, form_start, event)))
    for r in form_rows:
        form_map[r["entry_id"]] = r["form_points"]

    # Captain name per manager this GW (accounts for VC activation)
    captain_map = {}
    captain_rows = _rows(conn.execute("""
        SELECT cp.entry_id, cp.captain_name,
               cp.captain_raw_points, cp.captain_effective_points,
               cp.vc_activated
        FROM v_captain_points cp
        JOIN managers m ON m.entry_id = cp.entry_id
        WHERE m.league_id = ? AND cp.event = ?
    """, (league_id, event)))

    for r in captain_rows:
        vc = bool(r["vc_activated"])
        name = r["captain_name"]
        if vc:
            name = f"{name} (VC)"
        captain_map[r["entry_id"]] = {
            "name": name,
            "points": r["captain_effective_points"],
            "raw": r["captain_raw_points"],
        }

    # Transfer P&L per manager this GW
    transfer_pnl_map = {}
    tpnl_rows = _rows(conn.execute(f"""
        SELECT tp.entry_id, tp.gross_pnl, mg.event_transfers_cost
        FROM v_transfer_pnl tp
        JOIN manager_gameweeks mg
          ON mg.entry_id = tp.entry_id AND mg.event = tp.event
        WHERE tp.event = ?
          AND tp.entry_id IN (SELECT entry_id FROM managers
                              WHERE league_id = ?{active_clause(conn)})
    """, (event, league_id)))
    for r in tpnl_rows:
        gross = r["gross_pnl"] or 0
        hits = r["event_transfers_cost"] or 0
        transfer_pnl_map[r["entry_id"]] = gross - hits

    # Season total hits per manager
    season_hits_map = {}
    season_hits_rows = _rows(conn.execute("""
        SELECT mg.entry_id, SUM(mg.event_transfers_cost) AS season_hits
        FROM manager_gameweeks mg
        JOIN managers m ON m.entry_id = mg.entry_id
        WHERE m.league_id = ? AND mg.event <= ?
        GROUP BY mg.entry_id
    """, (league_id, event)))
    for r in season_hits_rows:
        season_hits_map[r["entry_id"]] = r["season_hits"] or 0

    # Season captain total per manager
    season_captain_map = {}
    season_cap_rows = _rows(conn.execute("""
        SELECT cp.entry_id, SUM(cp.captain_effective_points) AS season_captain_pts
        FROM v_captain_points cp
        JOIN managers m ON m.entry_id = cp.entry_id
        WHERE m.league_id = ? AND cp.event <= ?
        GROUP BY cp.entry_id
    """, (league_id, event)))
    for r in season_cap_rows:
        season_captain_map[r["entry_id"]] = r["season_captain_pts"] or 0

    # GW points ranked within league
    gw_points_sorted = sorted(leaderboard_raw, key=lambda r: r["points"], reverse=True)
    gw_rank_map = {}
    for i, r in enumerate(gw_points_sorted, 1):
        gw_rank_map[r["entry_id"]] = i

    # Build enriched leaderboard
    leaderboard = []
    for i, r in enumerate(leaderboard_raw, 1):
        eid = r["entry_id"]
        prev_pos = prev_league_pos.get(eid)
        movement = (prev_pos - i) if prev_pos is not None else None
        cur_or = r["overall_rank"]
        prev_or = prev_overall_rank.get(eid)
        or_change = (prev_or - cur_or) if (prev_or and cur_or) else None
        cap = captain_map.get(eid, {"name": "-", "points": 0})
        leaderboard.append({
            **r,
            "pos": i,
            "prev_pos": prev_pos,
            "movement": movement,
            "or_change": or_change,
            "form": form_map.get(eid, 0),
            "gw_rank": gw_rank_map.get(eid),
            "captain_name": cap["name"],
            "captain_points": cap["points"],
            "transfer_net": transfer_pnl_map.get(eid, 0),
            "season_hits": season_hits_map.get(eid, 0),
            "season_captain_pts": season_captain_map.get(eid, 0),
        })

    # --- GW Review ---
    top_scorers = _rows(conn.execute("""
        SELECT p.web_name, t.short_name AS team, pep.event_points
        FROM v_player_event_points pep
        JOIN players p ON p.id = pep.element
        JOIN teams t ON t.id = p.team_id
        WHERE pep.event = ?
        ORDER BY pep.event_points DESC LIMIT 6
    """, (event,)))

    most_captained = _rows(conn.execute("""
        SELECT p.web_name, COUNT(*) AS picks
        FROM manager_picks mp
        JOIN managers m ON m.entry_id = mp.entry_id
        JOIN players p ON p.id = mp.element
        WHERE m.league_id = ? AND mp.event = ? AND mp.is_captain = 1
        GROUP BY p.web_name
        ORDER BY picks DESC LIMIT 5
    """, (league_id, event)))

    # GW by numbers
    biggest_riser = max(leaderboard, key=lambda r: r["movement"] or 0)
    biggest_faller = min(leaderboard, key=lambda r: r["movement"] or 0)

    most_bought_row = conn.execute("""
        SELECT p.web_name, COUNT(*) AS cnt
        FROM manager_transfers mt
        JOIN managers m ON m.entry_id = mt.entry_id
        JOIN players p ON p.id = mt.element_in
        WHERE m.league_id = ? AND mt.event = ?
        GROUP BY mt.element_in ORDER BY cnt DESC LIMIT 1
    """, (league_id, event)).fetchone()

    most_sold_row = conn.execute("""
        SELECT p.web_name, COUNT(*) AS cnt
        FROM manager_transfers mt
        JOIN managers m ON m.entry_id = mt.entry_id
        JOIN players p ON p.id = mt.element_out
        WHERE m.league_id = ? AND mt.event = ?
        GROUP BY mt.element_out ORDER BY cnt DESC LIMIT 1
    """, (league_id, event)).fetchone()

    # Best/worst individual transfer this GW
    best_transfer = None
    worst_transfer = None
    gw_transfers = _rows(conn.execute("""
        SELECT m.entry_name, pin.web_name AS bought, pout.web_name AS sold,
               COALESCE(pin_pts.event_points, 0) - COALESCE(pout_pts.event_points, 0) AS net
        FROM manager_transfers mt
        JOIN managers m ON m.entry_id = mt.entry_id
        JOIN players pin ON pin.id = mt.element_in
        JOIN players pout ON pout.id = mt.element_out
        LEFT JOIN v_player_event_points pin_pts
            ON pin_pts.element = mt.element_in AND pin_pts.event = mt.event
        LEFT JOIN v_player_event_points pout_pts
            ON pout_pts.element = mt.element_out AND pout_pts.event = mt.event
        WHERE m.league_id = ? AND mt.event = ?
        ORDER BY net DESC
    """, (league_id, event)))
    if gw_transfers:
        best_transfer = gw_transfers[0]
        worst_transfer = gw_transfers[-1]

    # Chips used this GW
    chips_this_gw = _rows(conn.execute("""
        SELECT m.entry_name, mc.chip
        FROM manager_chips mc
        JOIN managers m ON m.entry_id = mc.entry_id
        WHERE m.league_id = ? AND mc.event = ?
    """, (league_id, event)))

    # New league leader check
    new_leader = None
    if len(leaderboard) >= 2:
        leader = leaderboard[0]
        if leader.get("prev_pos") and leader["prev_pos"] > 1:
            new_leader = {
                "entry_name": leader["entry_name"],
                "from_pos": leader["prev_pos"],
            }

    gw_stats = {
        "motw_team": motw[0]["entry_name"] if motw else "-",
        "motw_name": motw[0]["player_name"] if motw else "-",
        "motw_points": motw[0]["points"] if motw else 0,
        "biggest_riser": biggest_riser if (biggest_riser["movement"] or 0) > 0 else None,
        "biggest_faller": biggest_faller if (biggest_faller["movement"] or 0) < 0 else None,
        "most_bought": dict(most_bought_row) if most_bought_row else None,
        "most_sold": dict(most_sold_row) if most_sold_row else None,
        "best_transfer": best_transfer,
        "worst_transfer": worst_transfer,
        "chips_this_gw": chips_this_gw,
        "new_leader": new_leader,
    }

    # --- Transfer Roundup ---
    transfers = _rows(conn.execute("""
        SELECT m.entry_name, m.player_name,
               GROUP_CONCAT(pin.web_name || ' (' || COALESCE(pin_pts.event_points, 0) || ')', ', ') AS bought,
               GROUP_CONCAT(pout.web_name || ' (' || COALESCE(pout_pts.event_points, 0) || ')', ', ') AS sold,
               SUM(COALESCE(pin_pts.event_points, 0) - COALESCE(pout_pts.event_points, 0)) AS gross,
               mg.event_transfers_cost AS hits
        FROM manager_transfers mt
        JOIN managers m ON m.entry_id = mt.entry_id
        JOIN players pin ON pin.id = mt.element_in
        JOIN players pout ON pout.id = mt.element_out
        LEFT JOIN v_player_event_points pin_pts
          ON pin_pts.element = mt.element_in AND pin_pts.event = mt.event
        LEFT JOIN v_player_event_points pout_pts
          ON pout_pts.element = mt.element_out AND pout_pts.event = mt.event
        LEFT JOIN manager_gameweeks mg
          ON mg.entry_id = mt.entry_id AND mg.event = mt.event
        WHERE m.league_id = ? AND mt.event = ?
        GROUP BY m.entry_id
        ORDER BY (SUM(COALESCE(pin_pts.event_points, 0) - COALESCE(pout_pts.event_points, 0))
                  - COALESCE(mg.event_transfers_cost, 0)) DESC
    """, (league_id, event)))

    # --- Captain's Corner ---
    captains = _rows(conn.execute("""
        SELECT m.entry_name, m.player_name, cp.captain_name,
               cp.captain_raw_points, cp.captain_effective_points,
               cp.vc_activated
        FROM v_captain_points cp
        JOIN managers m ON m.entry_id = cp.entry_id
        WHERE m.league_id = ? AND cp.event = ?
        ORDER BY cp.captain_effective_points DESC
    """, (league_id, event)))

    captain_avg = (
        sum(r["captain_effective_points"] for r in captains) / len(captains)
        if captains else 0
    )

    # Season-long most captained players
    total_captain_picks = conn.execute("""
        SELECT COUNT(*) AS c FROM v_captain_points cp
        JOIN managers m ON m.entry_id = cp.entry_id
        WHERE m.league_id = ? AND cp.event <= ?
    """, (league_id, event)).fetchone()["c"] or 1

    season_captains = _rows(conn.execute("""
        SELECT p.web_name, COUNT(*) AS times_captained,
               ROUND(100.0 * COUNT(*) / ?, 1) AS pct,
               ROUND(AVG(cp.captain_raw_points), 1) AS avg_pts
        FROM v_captain_points cp
        JOIN managers m ON m.entry_id = cp.entry_id
        JOIN players p ON p.id = cp.captain_element
        WHERE m.league_id = ? AND cp.event <= ?
        GROUP BY cp.captain_element
        ORDER BY times_captained DESC LIMIT 10
    """, (total_captain_picks, league_id, event)))

    # All managers' season captain totals (for bar chart)
    all_captain_totals = _rows(conn.execute("""
        SELECT m.entry_id, m.entry_name, m.player_name,
               SUM(cp.captain_effective_points) AS total_captain_pts
        FROM v_captain_points cp
        JOIN managers m ON m.entry_id = cp.entry_id
        WHERE m.league_id = ? AND cp.event <= ?
        GROUP BY cp.entry_id
        ORDER BY total_captain_pts DESC
    """, (league_id, event)))

    # --- GW Score Breakdown ---
    # Which chips were used this GW
    chip_gw_map: dict[int, str] = {}
    chip_gw_rows = _rows(conn.execute("""
        SELECT mc.entry_id, mc.chip
        FROM manager_chips mc
        JOIN managers m ON m.entry_id = mc.entry_id
        WHERE m.league_id = ? AND mc.event = ?
    """, (league_id, event)))
    for r in chip_gw_rows:
        chip_gw_map[r["entry_id"]] = r["chip"]

    gw_breakdown = []
    for r in leaderboard:
        eid = r["entry_id"]
        hits = r["event_transfers_cost"] or 0
        gross = r["points"] + hits
        cap = captain_map.get(eid, {"name": "-", "points": 0, "raw": 0})
        cap_bonus = (cap["points"] or 0) - (cap["raw"] or 0)
        tpnl = transfer_pnl_map.get(eid, 0)
        chip = chip_gw_map.get(eid, "")
        base = gross - cap_bonus
        gw_breakdown.append({
            "entry_name": r["entry_name"],
            "player_name": r["player_name"],
            "gross": gross,
            "base": base,
            "captain_bonus": cap_bonus,
            "transfer_net": tpnl,
            "hits": hits,
            "chip": chip,
            "net": r["points"],
        })
    gw_breakdown.sort(key=lambda r: r["net"], reverse=True)

    # --- Chip Usage (Phase 6) ---
    chip_rows = _rows(conn.execute("""
        SELECT m.entry_id, m.entry_name, m.player_name, mc.chip, mc.event
        FROM manager_chips mc
        JOIN managers m ON m.entry_id = mc.entry_id
        WHERE m.league_id = ? AND mc.event <= ?
        ORDER BY m.entry_name, mc.event
    """, (league_id, event)))

    # Enhanced chip table: include points scored that GW
    chip_types = ["wildcard", "freehit", "bboost", "3xc"]
    chip_labels = {"wildcard": "Wildcard", "freehit": "Free Hit",
                   "bboost": "Bench Boost", "3xc": "Triple Captain"}

    # --- Wildcard Maestro (computed first — needed for WC chip values) ---
    # Build per-manager set of Free Hit GWs so we can skip them
    fh_gws: dict[int, set[int]] = defaultdict(set)
    for r in chip_rows:
        if r["chip"] == "freehit":
            fh_gws[r["entry_id"]].add(r["event"])

    # Helpers for chip valuation. The "value" of a reshape chip (Wildcard, Free
    # Hit) is the points gained by the new team vs. the counterfactual of keeping
    # the old team — both scored captain-aware so the comparison is symmetric:
    #   • old (frozen) team carries its pre-chip captain/VC for every week,
    #   • new team's captain is counted as a double only (so a Triple Captain or
    #     Bench Boost played in the window doesn't get credited to the reshape).
    def _team_cap_vc(entry_id: int, ev: int):
        rows = conn.execute(
            "SELECT element, is_captain, is_vice_captain FROM manager_picks "
            "WHERE entry_id = ? AND event = ? AND (is_captain = 1 OR is_vice_captain = 1)",
            (entry_id, ev)).fetchall()
        cap = next((r[0] for r in rows if r[1]), None)
        vc = next((r[0] for r in rows if r[2]), None)
        return cap, vc

    def _player_week(element, ev: int):
        if element is None:
            return None
        return conn.execute(
            "SELECT event_points, minutes FROM v_player_event_points "
            "WHERE element = ? AND event = ?", (element, ev)).fetchone()

    def _frozen_team_week(elements: list[int], cap_el, vc_el, ev: int) -> int:
        """Points a fixed XI scores in event `ev`, doubling the held captain
        (or the vice-captain if the captain didn't play that week)."""
        if not elements:
            return 0
        ph = ",".join("?" * len(elements))
        xi = conn.execute(
            f"SELECT COALESCE(SUM(event_points), 0) AS p FROM v_player_event_points "
            f"WHERE element IN ({ph}) AND event = ?", elements + [ev]).fetchone()["p"]
        cr = _player_week(cap_el, ev)
        if cr and (cr["minutes"] or 0) > 0:
            return xi + (cr["event_points"] or 0)
        vr = _player_week(vc_el, ev)
        if vr and (vr["minutes"] or 0) > 0:
            return xi + (vr["event_points"] or 0)
        return xi

    def _actual_team_week(entry_id: int, ev: int) -> int:
        """Points the manager's actual XI scored in event `ev`, captain doubled
        (TC counted as a double, bench excluded — isolates the squad)."""
        xi = conn.execute(
            "SELECT COALESCE(SUM(pev.event_points), 0) AS p FROM manager_picks mp "
            "JOIN v_player_event_points pev "
            "  ON pev.element = mp.element AND pev.event = mp.event "
            "WHERE mp.entry_id = ? AND mp.event = ? AND mp.multiplier > 0",
            (entry_id, ev)).fetchone()["p"]
        cr = conn.execute(
            "SELECT captain_raw_points FROM v_captain_points "
            "WHERE entry_id = ? AND event = ?", (entry_id, ev)).fetchone()
        return xi + ((cr["captain_raw_points"] or 0) if cr else 0)

    wc_maestro = []
    wc_activations = [r for r in chip_rows if r["chip"] == "wildcard"]
    for wc in wc_activations:
        eid = wc["entry_id"]
        wc_gw = wc["event"]
        # Find pre-WC GW, skipping any Free Hit weeks (FH uses a temporary team)
        pre_gw = wc_gw - 1
        while pre_gw >= 1 and pre_gw in fh_gws.get(eid, set()):
            pre_gw -= 1
        if pre_gw < 1:
            continue

        old_xi = conn.execute("""
            SELECT element FROM manager_picks
            WHERE entry_id = ? AND event = ? AND multiplier > 0
        """, (eid, pre_gw)).fetchall()
        old_elements = [r[0] for r in old_xi]
        if not old_elements:
            continue

        old_cap, old_vc = _team_cap_vc(eid, pre_gw)
        window_end = min(wc_gw + 4, event)
        total_old = 0
        total_new = 0
        counted = 0
        gw_details = []

        for gw in range(wc_gw, window_end + 1):
            if gw in fh_gws.get(eid, set()):
                continue  # Free Hit week fields a one-off team, not the WC squad
            old_pts = _frozen_team_week(old_elements, old_cap, old_vc, gw)
            new_pts = _actual_team_week(eid, gw)
            total_old += old_pts
            total_new += new_pts
            counted += 1
            gw_details.append({"gw": gw, "old_pts": old_pts, "new_pts": new_pts,
                               "diff": new_pts - old_pts})

        if counted == 0:
            continue
        num_gws = counted
        wc_maestro.append({
            "entry_id": eid,
            "entry_name": wc["entry_name"],
            "player_name": wc["player_name"],
            "wc_gw": wc_gw,
            "num_gws": num_gws,
            "old_total": total_old,
            "new_total": total_new,
            "gain": total_new - total_old,
            "avg_gain_per_gw": round((total_new - total_old) / num_gws, 1) if num_gws else 0,
            "details": gw_details,
        })
    wc_maestro.sort(key=lambda r: r["gain"], reverse=True)

    # WC gain lookup for chip value computation
    wc_gain_lookup: dict[tuple[int, int], int] = {}
    for wm in wc_maestro:
        wc_gain_lookup[(wm["entry_id"], wm["wc_gw"])] = wm["gain"]

    # --- Compute actual chip values ---
    # WC: 5-GW gain over old team (from wc_maestro)
    # BB: bench player points that GW
    # FH: GW points minus what previous week's team would have scored
    # TC: extra captain multiplier (1× raw captain pts, i.e. 3× − 2× = 1×)
    chip_usage_count: dict[tuple[int, str], int] = {}
    chip_detail = []
    for r in chip_rows:
        key = (r["entry_id"], r["chip"])
        chip_usage_count[key] = chip_usage_count.get(key, 0) + 1
        usage_num = chip_usage_count[key]

        eid = r["entry_id"]
        gw = r["event"]
        chip_type = r["chip"]

        if chip_type == "wildcard":
            value = wc_gain_lookup.get((eid, gw), 0)

        elif chip_type == "bboost":
            bb_row = conn.execute("""
                SELECT COALESCE(SUM(pev.event_points), 0) AS bench_pts
                FROM manager_picks mp
                LEFT JOIN v_player_event_points pev
                    ON pev.element = mp.element AND pev.event = mp.event
                WHERE mp.entry_id = ? AND mp.event = ? AND mp.position > 11
            """, (eid, gw)).fetchone()
            value = bb_row["bench_pts"] if bb_row else 0

        elif chip_type == "freehit":
            # Same reshape comparison as the wildcard, over the single FH week:
            # the one-off FH team vs. holding the previous team (captain/VC kept).
            pre_gw = gw - 1
            while pre_gw >= 1 and pre_gw in fh_gws.get(eid, set()):
                pre_gw -= 1
            old_pts = 0
            if pre_gw >= 1:
                fh_cap, fh_vc = _team_cap_vc(eid, pre_gw)
                old_elements = [row[0] for row in conn.execute("""
                    SELECT element FROM manager_picks
                    WHERE entry_id = ? AND event = ? AND multiplier > 0
                """, (eid, pre_gw)).fetchall()]
                old_pts = _frozen_team_week(old_elements, fh_cap, fh_vc, gw)
            value = _actual_team_week(eid, gw) - old_pts

        elif chip_type == "3xc":
            cap_row = conn.execute("""
                SELECT COALESCE(pev.event_points, 0) AS raw_pts
                FROM manager_picks mp
                LEFT JOIN v_player_event_points pev
                    ON pev.element = mp.element AND pev.event = mp.event
                WHERE mp.entry_id = ? AND mp.event = ? AND mp.is_captain = 1
            """, (eid, gw)).fetchone()
            value = cap_row["raw_pts"] if cap_row else 0

        else:
            value = 0

        chip_detail.append({
            "entry_id": eid,
            "entry_name": r["entry_name"],
            "player_name": r["player_name"],
            "chip": chip_type,
            "chip_label": chip_labels.get(chip_type, chip_type),
            "event": gw,
            "points": value,
            "usage_num": usage_num,
        })

    # Single unified table: one row per manager, columns = chip GW + value
    chip_summary = []
    chips_by_manager: dict[int, dict] = {}
    for r in chip_detail:
        eid = r["entry_id"]
        if eid not in chips_by_manager:
            chips_by_manager[eid] = {
                "entry_name": r["entry_name"], "player_name": r["player_name"],
                "chips": {},
            }
        label = r["chip"]
        if r["usage_num"] > 1:
            label = f"{r['chip']}_{r['usage_num']}"
        chips_by_manager[eid]["chips"][label] = {"event": r["event"], "points": r["points"]}

    for eid, info in chips_by_manager.items():
        row = {"entry_name": info["entry_name"], "player_name": info["player_name"]}
        total = 0
        for ct in chip_types:
            c = info["chips"].get(ct, {})
            row[f"{ct}_gw"] = f"GW{c['event']}" if c else ""
            row[f"{ct}_pts"] = c.get("points", "")
            if c:
                total += c["points"]
            c2 = info["chips"].get(f"{ct}_2", {})
            row[f"{ct}_2_gw"] = f"GW{c2['event']}" if c2 else ""
            row[f"{ct}_2_pts"] = c2.get("points", "")
            if c2:
                total += c2["points"]
        row["total"] = total
        chip_summary.append(row)
    chip_summary.sort(key=lambda r: r["total"], reverse=True)

    # Sort chip_detail by points desc for the pre-rendered ranking table
    chip_detail.sort(key=lambda r: r["points"], reverse=True)

    # Top 5 highest-scoring chip activations up to current GW
    chips_up_to_gw = [r for r in chip_detail if r["event"] <= event]
    chip_top5 = sorted(chips_up_to_gw, key=lambda r: r["points"], reverse=True)[:5]

    # --- Season Progression per Manager (for filterable table) ---
    season_table = _rows(conn.execute("""
        SELECT m.entry_id, m.entry_name, m.player_name,
               mg.event, mg.points, mg.total_points, mg.overall_rank
        FROM manager_gameweeks mg
        JOIN managers m ON m.entry_id = mg.entry_id
        WHERE m.league_id = ? AND mg.event <= ?
        ORDER BY mg.event, mg.total_points DESC
    """, (league_id, event)))

    # Compute TML rank per GW
    gw_groups: dict[int, list] = defaultdict(list)
    for r in season_table:
        gw_groups[r["event"]].append(r)
    for gw_num, rows in gw_groups.items():
        rows.sort(key=lambda r: r["total_points"], reverse=True)
        for i, r in enumerate(rows, 1):
            r["tml_rank"] = i

    # Pre-compute first manager's progression for no-JS rendering
    first_entry_id = leaderboard[0]["entry_id"] if leaderboard else None
    season_table_first = [
        r for r in season_table if r["entry_id"] == first_entry_id
    ] if first_entry_id else []
    season_table_first.sort(key=lambda r: r["event"])

    # --- Transfer Ticker (Phase 7) ---
    transfer_ticker = _rows(conn.execute("""
        SELECT m.entry_name, m.player_name,
               pin.web_name AS bought, pout.web_name AS sold,
               COALESCE(pin_pts.event_points, 0) AS bought_pts,
               COALESCE(pout_pts.event_points, 0) AS sold_pts,
               COALESCE(pin_pts.event_points, 0) - COALESCE(pout_pts.event_points, 0) AS net
        FROM manager_transfers mt
        JOIN managers m ON m.entry_id = mt.entry_id
        JOIN players pin ON pin.id = mt.element_in
        JOIN players pout ON pout.id = mt.element_out
        LEFT JOIN v_player_event_points pin_pts
          ON pin_pts.element = mt.element_in AND pin_pts.event = mt.event
        LEFT JOIN v_player_event_points pout_pts
          ON pout_pts.element = mt.element_out AND pout_pts.event = mt.event
        WHERE m.league_id = ? AND mt.event = ?
        ORDER BY net DESC
    """, (league_id, event)))

    # --- Manager of the Month (Phase 8) ---
    motm_raw = _rows(conn.execute("""
        SELECT m.entry_id, m.entry_name, m.player_name,
               strftime('%Y-%m', g.deadline_time) AS month,
               SUM(mg.points - mg.event_transfers_cost) AS month_points,
               COUNT(*) AS month_gws
        FROM manager_gameweeks mg
        JOIN managers m ON m.entry_id = mg.entry_id
        JOIN gameweeks g ON g.id = mg.event
        WHERE m.league_id = ? AND mg.event <= ?
        GROUP BY m.entry_id, strftime('%Y-%m', g.deadline_time)
    """, (league_id, event)))

    # Pivot to find monthly winners
    month_data: dict[str, list] = defaultdict(list)
    for r in motm_raw:
        month_data[r["month"]].append(r)

    month_labels = {
        "01": "January", "02": "February", "03": "March", "04": "April",
        "05": "May", "06": "June", "07": "July", "08": "August",
        "09": "September", "10": "October", "11": "November", "12": "December",
    }
    monthly_winners = []
    for month_key in sorted(month_data.keys()):
        entries = sorted(month_data[month_key], key=lambda r: r["month_points"], reverse=True)
        winner = entries[0]
        mm = month_key.split("-")[1]
        runners_up = []
        for r in entries[1:3]:
            runners_up.append({
                "entry_name": r["entry_name"],
                "points": r["month_points"],
            })
        gws = winner["month_gws"] or 1
        monthly_winners.append({
            "month": month_key,
            "month_label": month_labels.get(mm, mm),
            "entry_name": winner["entry_name"],
            "player_name": winner["player_name"],
            "points": winner["month_points"],
            "pts_per_gw": round(winner["month_points"] / gws, 1),
            "runners_up": runners_up,
        })

    # --- Scout Report (Phase 9) ---
    scout_gw = _rows(conn.execute("""
        SELECT p.web_name, t.short_name AS team, p.element_type,
               pev.event_points, pev.xg, pev.xa, pev.xgi, pev.xgc,
               pev.minutes, pev.goals, pev.assists, pev.bonus,
               pev.saves, pev.penalties_saved, pev.penalties_missed,
               pev.own_goals, pev.yellow_cards, pev.red_cards, pev.def_con
        FROM v_player_event_points pev
        JOIN players p ON p.id = pev.element
        JOIN teams t ON t.id = p.team_id
        WHERE pev.event = ? AND pev.minutes > 0
        ORDER BY pev.event_points DESC
    """, (event,)))

    # Compute xPts and find over/under-performers
    for row in scout_gw:
        row["xpts"] = round(_compute_xpts(
            row["element_type"], row["minutes"],
            row["xg"] or 0, row["xa"] or 0, row["xgc"] or 0,
            row["saves"] or 0, row["penalties_saved"] or 0,
            row["penalties_missed"] or 0, row["own_goals"] or 0,
            row["yellow_cards"] or 0, row["red_cards"] or 0,
            row["bonus"] or 0, row["def_con"] or 0,
        ), 1)
        row["diff"] = round(row["event_points"] - row["xpts"], 1)

    scout_over = sorted(scout_gw, key=lambda r: r["diff"], reverse=True)[:5]
    scout_under = sorted(scout_gw, key=lambda r: r["diff"])[:5]

    # Season xGI leaders
    scout_season = _rows(conn.execute("""
        SELECT p.web_name, t.short_name AS team, p.element_type,
               SUM(pev.xg) AS season_xg, SUM(pev.xa) AS season_xa,
               SUM(pev.xgi) AS season_xgi, SUM(pev.event_points) AS season_pts,
               COALESCE(own.owners, 0) AS league_owners
        FROM v_player_event_points pev
        JOIN players p ON p.id = pev.element
        JOIN teams t ON t.id = p.team_id
        LEFT JOIN v_ownership_event own ON own.element = p.id AND own.event = ?
        WHERE pev.minutes > 0 AND pev.event <= ?
        GROUP BY pev.element
        ORDER BY season_xgi DESC LIMIT 20
    """, (event, event)))

    # --- Who Should Be Top (Phase 10) ---
    xpts_raw = _rows(conn.execute(f"""
        SELECT mp.entry_id, mp.event, mp.element, mp.multiplier,
               p.element_type,
               COALESCE(pev.event_points, 0) AS actual_pts,
               COALESCE(pev.xg, 0) AS xg,
               COALESCE(pev.xa, 0) AS xa,
               COALESCE(pev.xgc, 0) AS xgc,
               COALESCE(pev.minutes, 0) AS minutes,
               COALESCE(pev.saves, 0) AS saves,
               COALESCE(pev.penalties_saved, 0) AS penalties_saved,
               COALESCE(pev.penalties_missed, 0) AS penalties_missed,
               COALESCE(pev.own_goals, 0) AS own_goals,
               COALESCE(pev.yellow_cards, 0) AS yellow_cards,
               COALESCE(pev.red_cards, 0) AS red_cards,
               COALESCE(pev.bonus, 0) AS bonus,
               COALESCE(pev.def_con, 0) AS def_con
        FROM manager_picks mp
        JOIN players p ON p.id = mp.element
        LEFT JOIN v_player_event_points pev
            ON pev.element = mp.element AND pev.event = mp.event
        WHERE mp.entry_id IN (SELECT entry_id FROM managers
                              WHERE league_id = ?{active_clause(conn)})
          AND mp.multiplier > 0
          AND mp.event <= ?
    """, (league_id, event)))

    # Aggregate xPts per manager
    xpts_by_manager: dict[int, dict] = {}
    manager_names = {r["entry_id"]: (r["entry_name"], r["player_name"])
                     for r in leaderboard_raw}
    for r in xpts_raw:
        eid = r["entry_id"]
        if eid not in xpts_by_manager:
            names = manager_names.get(eid, ("", ""))
            xpts_by_manager[eid] = {
                "entry_id": eid, "entry_name": names[0],
                "player_name": names[1],
                "total_xpts": 0.0, "total_actual": 0.0,
            }
        player_xpts = _compute_xpts(
            r["element_type"], r["minutes"],
            r["xg"], r["xa"], r["xgc"],
            r["saves"], r["penalties_saved"], r["penalties_missed"],
            r["own_goals"], r["yellow_cards"], r["red_cards"],
            r["bonus"], r["def_con"],
        )
        # Apply captain/TC multiplier
        xpts_by_manager[eid]["total_xpts"] += player_xpts * r["multiplier"]
        xpts_by_manager[eid]["total_actual"] += r["actual_pts"] * r["multiplier"]

    xpts_leaderboard = sorted(xpts_by_manager.values(),
                               key=lambda r: r["total_xpts"], reverse=True)
    for i, r in enumerate(xpts_leaderboard, 1):
        r["xpts_rank"] = i
        r["total_xpts"] = round(r["total_xpts"], 1)
        r["total_actual"] = round(r["total_actual"])
        r["diff"] = round(r["total_actual"] - r["total_xpts"], 1)

    # Luckiest and unluckiest
    xpts_by_luck = sorted(xpts_leaderboard, key=lambda r: r["diff"], reverse=True)
    luckiest = xpts_by_luck[:3] if xpts_by_luck else []
    unluckiest = xpts_by_luck[-3:] if len(xpts_by_luck) >= 3 else xpts_by_luck

    # Last 5 GW points per manager (for sparklines)
    sparkline_rows = _rows(conn.execute("""
        SELECT mg.entry_id, mg.event, mg.points
        FROM manager_gameweeks mg
        JOIN managers m ON m.entry_id = mg.entry_id
        WHERE m.league_id = ? AND mg.event BETWEEN ? AND ?
        ORDER BY mg.event
    """, (league_id, form_start, event)))
    sparkline_map: dict[int, list] = defaultdict(list)
    for r in sparkline_rows:
        sparkline_map[r["entry_id"]].append(r["points"])

    # Add sparklines to leaderboard
    for r in leaderboard:
        r["sparkline"] = sparkline_map.get(r["entry_id"], [])

    # =====================================================================
    # EXTENDED STATS — Hall of Records, analytical tables, DefCon, H2H
    # =====================================================================
    manager_names = {r["entry_id"]: (r["entry_name"], r["player_name"])
                     for r in leaderboard_raw}

    def _name(eid):
        return manager_names.get(eid, ("", ""))

    # --- One pass over the full GW series drives several sections ---
    mg_rows = _rows(conn.execute("""
        SELECT mg.entry_id, mg.event, mg.points, mg.total_points,
               mg.overall_rank, mg.points_on_bench, mg.bank,
               mg.event_transfers, mg.event_transfers_cost
        FROM manager_gameweeks mg
        JOIN managers m ON m.entry_id = mg.entry_id
        WHERE m.league_id = ? AND mg.event <= ?
        ORDER BY mg.entry_id, mg.event
    """, (league_id, event)))
    series: dict[int, list[dict]] = defaultdict(list)
    for r in mg_rows:
        series[r["entry_id"]].append(r)

    gw_points_by_event: dict[int, list[int]] = defaultdict(list)
    for r in mg_rows:
        gw_points_by_event[r["event"]].append(r["points"] or 0)
    league_gw_avg = {ev: sum(v) / len(v) for ev, v in gw_points_by_event.items() if v}

    # Season gross transfer P&L per manager (for "did the hits pay?")
    season_pnl: dict[int, int] = {}
    for r in _rows(conn.execute("""
        SELECT tp.entry_id, SUM(tp.gross_pnl) AS gross
        FROM v_transfer_pnl tp
        JOIN managers m ON m.entry_id = tp.entry_id
        WHERE m.league_id = ? AND tp.event <= ?
        GROUP BY tp.entry_id
    """, (league_id, event))):
        season_pnl[r["entry_id"]] = r["gross"] or 0

    # --- Consistency table + reliability/volatility/best/worst awards ---
    consistency = []
    for eid, rows in series.items():
        pts = [r["points"] or 0 for r in rows]
        if not pts:
            continue
        n = _name(eid)
        above = sum(1 for r in rows
                    if (r["points"] or 0) > league_gw_avg.get(r["event"], 0))
        consistency.append({
            "entry_id": eid, "entry_name": n[0], "player_name": n[1],
            "gws": len(pts), "avg": round(sum(pts) / len(pts), 1),
            "best": max(pts), "worst": min(pts),
            "std": round(statistics.pstdev(pts), 1) if len(pts) > 1 else 0.0,
            "above_avg": above, "total_points": rows[-1]["total_points"],
        })
    consistency.sort(key=lambda r: r["total_points"] or 0, reverse=True)

    qualified = [c for c in consistency if c["gws"] >= 5] or consistency

    def _top(rows, key, reverse):
        out = []
        for c in sorted(rows, key=lambda r: r[key], reverse=reverse)[:3]:
            out.append({"entry_name": c["entry_name"],
                        "player_name": c["player_name"], "value": c[key]})
        return out

    mr_reliable = _top(qualified, "std", False)
    rollercoaster = _top(qualified, "std", True)
    magnum_opus = _top(consistency, "best", True)
    the_stinker = _top(consistency, "worst", False)

    # --- Bench (season) ---
    bench_season = []
    for eid, rows in series.items():
        n = _name(eid)
        worst = max(rows, key=lambda r: r["points_on_bench"] or 0)
        bench_season.append({
            "entry_name": n[0], "player_name": n[1],
            "value": sum(r["points_on_bench"] or 0 for r in rows),
            "worst_gw": worst["event"], "worst_pts": worst["points_on_bench"] or 0,
        })
    bench_season.sort(key=lambda r: r["value"], reverse=True)
    bench_disaster = bench_season[:3]

    # --- Hits (season) ---
    hits_season = []
    for eid, rows in series.items():
        n = _name(eid)
        cost = sum(r["event_transfers_cost"] or 0 for r in rows)
        hits_season.append({
            "entry_name": n[0], "player_name": n[1], "value": cost,
            "hits": cost // 4, "net": season_pnl.get(eid, 0) - cost,
        })
    hits_season.sort(key=lambda r: r["value"], reverse=True)
    hit_merchant = hits_season[:3]

    # --- Scrooge (cash left in the bank, latest GW) ---
    scrooge = sorted(
        ({"entry_name": _name(eid)[0], "player_name": _name(eid)[1],
          "value": rows[-1]["bank"] or 0} for eid, rows in series.items()),
        key=lambda r: r["value"], reverse=True)[:3]

    # --- Transfer counts (Tinkerman / Set & Forget) ---
    tx_count = {r["entry_id"]: r["cnt"] for r in _rows(conn.execute("""
        SELECT mt.entry_id, COUNT(*) AS cnt
        FROM manager_transfers mt
        JOIN managers m ON m.entry_id = mt.entry_id
        WHERE m.league_id = ? AND mt.event <= ?
        GROUP BY mt.entry_id
    """, (league_id, event)))}
    tinker = [{"entry_name": _name(eid)[0], "player_name": _name(eid)[1],
               "value": tx_count.get(eid, 0)} for eid in series]
    tinkerman = sorted(tinker, key=lambda r: r["value"], reverse=True)[:3]
    set_and_forget = sorted(tinker, key=lambda r: r["value"])[:3]

    # --- Rank trajectory table (green vs red arrows) ---
    rank_traj = []
    for eid, rows in series.items():
        ors = [(r["event"], r["overall_rank"]) for r in rows if r["overall_rank"]]
        if not ors:
            continue
        n = _name(eid)
        greens = sum(1 for (_, a), (_, b) in zip(ors, ors[1:]) if b < a)
        reds = sum(1 for (_, a), (_, b) in zip(ors, ors[1:]) if b > a)
        rank_traj.append({
            "entry_name": n[0], "player_name": n[1],
            "start_or": ors[0][1], "cur_or": ors[-1][1],
            "net": ors[0][1] - ors[-1][1], "greens": greens, "reds": reds,
            "best_or": min(o for _, o in ors), "worst_or": max(o for _, o in ors),
        })
    rank_traj.sort(key=lambda r: r["cur_or"])

    # --- Head-to-Head (every manager vs every other on weekly points) ---
    pts_by_eid_ev: dict[int, dict[int, int]] = defaultdict(dict)
    for r in mg_rows:
        pts_by_eid_ev[r["entry_id"]][r["event"]] = r["points"] or 0
    eids = list(series.keys())
    h2h_records: dict[int, dict[int, tuple]] = {}
    for a in eids:
        rec = {}
        for b in eids:
            if a == b:
                continue
            w = d = lo = 0
            for ev, pa in pts_by_eid_ev[a].items():
                pb = pts_by_eid_ev[b].get(ev)
                if pb is None:
                    continue
                if pa > pb:
                    w += 1
                elif pa == pb:
                    d += 1
                else:
                    lo += 1
            rec[b] = (w, d, lo)
        h2h_records[a] = rec

    h2h_summary = []
    for a in eids:
        n = _name(a)
        tw = sum(v[0] for v in h2h_records[a].values())
        td = sum(v[1] for v in h2h_records[a].values())
        tl = sum(v[2] for v in h2h_records[a].values())
        nem = max(h2h_records[a].items(), key=lambda kv: kv[1][2], default=(None, (0, 0, 0)))
        bun = max(h2h_records[a].items(), key=lambda kv: kv[1][0], default=(None, (0, 0, 0)))
        h2h_summary.append({
            "entry_id": a, "entry_name": n[0], "player_name": n[1],
            "w": tw, "d": td, "l": tl,
            "win_pct": round(100 * tw / (tw + td + tl), 1) if (tw + td + tl) else 0,
            "nemesis": _name(nem[0])[0] if nem[0] else "-", "nemesis_l": nem[1][2],
            "bunny": _name(bun[0])[0] if bun[0] else "-", "bunny_w": bun[1][0],
        })
    h2h_summary.sort(key=lambda r: (r["w"], r["win_pct"]), reverse=True)

    h2h_order = [r["entry_id"] for r in h2h_summary]
    h2h_labels = [_name(a)[0] for a in h2h_order]
    h2h_matrix = []
    for a in h2h_order:
        cells = []
        for b in h2h_order:
            if a == b:
                cells.append(None)
            else:
                w, d, lo = h2h_records[a][b]
                cells.append({"w": w, "d": d, "l": lo,
                              "cls": "pos" if w > lo else ("neg" if lo > w else "muted")})
        h2h_matrix.append({"entry_name": _name(a)[0], "cells": cells})

    # --- Alternative table: standings if hits were refunded ---
    real_pos = {r["entry_id"]: r["pos"] for r in leaderboard}
    nohits = []
    for eid, rows in series.items():
        n = _name(eid)
        cost = sum(r["event_transfers_cost"] or 0 for r in rows)
        nohits.append({
            "entry_name": n[0], "player_name": n[1],
            "real_total": rows[-1]["total_points"], "hits_cost": cost,
            "nohits_total": (rows[-1]["total_points"] or 0) + cost,
            "real_pos": real_pos.get(eid),
        })
    nohits.sort(key=lambda r: r["nohits_total"], reverse=True)
    for i, r in enumerate(nohits, 1):
        r["nohits_pos"] = i
        r["pos_change"] = (r["real_pos"] - i) if r["real_pos"] else None

    # --- Alternative table: captaincy-only standings ---
    captaincy_standings = []
    for r in _rows(conn.execute("""
        SELECT cp.entry_id,
               SUM(cp.captain_effective_points) AS total,
               AVG(cp.captain_effective_points) AS avg,
               SUM(CASE WHEN cp.captain_raw_points >= 10 THEN 1 ELSE 0 END) AS hauls,
               SUM(CASE WHEN cp.captain_raw_points <= 2 THEN 1 ELSE 0 END) AS blanks,
               COUNT(*) AS picks
        FROM v_captain_points cp
        JOIN managers m ON m.entry_id = cp.entry_id
        WHERE m.league_id = ? AND cp.event <= ?
        GROUP BY cp.entry_id
    """, (league_id, event))):
        n = _name(r["entry_id"])
        captaincy_standings.append({
            "entry_name": n[0], "player_name": n[1],
            "total": r["total"] or 0, "avg": round(r["avg"] or 0, 1),
            "hauls": r["hauls"] or 0, "blanks": r["blanks"] or 0,
            "picks": r["picks"] or 0,
        })
    captaincy_standings.sort(key=lambda r: r["total"], reverse=True)

    # --- Best & worst single captain pick of the season ---
    cap_extremes = _rows(conn.execute("""
        SELECT m.entry_name, m.player_name, cp.captain_name, cp.event,
               cp.captain_effective_points AS pts
        FROM v_captain_points cp
        JOIN managers m ON m.entry_id = cp.entry_id
        WHERE m.league_id = ? AND cp.event <= ?
        ORDER BY cp.captain_effective_points DESC
    """, (league_id, event)))
    captain_marvel = cap_extremes[:3]
    captain_calamity = list(reversed(cap_extremes[-3:])) if cap_extremes else []

    # --- Squad DNA: template-ness of each manager's current XI ---
    own_pct = {r["element"]: (r["owners"] / (mgr_count or 1)) for r in _rows(conn.execute(
        "SELECT element, owners FROM v_ownership_event WHERE event = ?", (event,)))}
    xi_by_mgr: dict[int, list[int]] = defaultdict(list)
    for r in _rows(conn.execute("""
        SELECT mp.entry_id, mp.element
        FROM manager_picks mp
        JOIN managers m ON m.entry_id = mp.entry_id
        WHERE m.league_id = ? AND mp.event = ? AND mp.multiplier > 0
    """, (league_id, event))):
        xi_by_mgr[r["entry_id"]].append(r["element"])
    squad_style = []
    for eid, els in xi_by_mgr.items():
        if not els:
            continue
        n = _name(eid)
        squad_style.append({
            "entry_name": n[0], "player_name": n[1],
            "template": round(sum(own_pct.get(e, 0) for e in els) / len(els) * 100, 1),
            "diffs": sum(1 for e in els if own_pct.get(e, 0) <= 0.10),
            "uniques": sum(1 for e in els if round(own_pct.get(e, 0) * (mgr_count or 1)) <= 1),
        })
    squad_style.sort(key=lambda r: r["template"], reverse=True)
    mr_template = [{"entry_name": r["entry_name"], "player_name": r["player_name"],
                    "value": r["template"]} for r in squad_style[:3]]
    the_hipster = [{"entry_name": r["entry_name"], "player_name": r["player_name"],
                    "value": r["template"]}
                   for r in sorted(squad_style, key=lambda r: r["template"])[:3]]

    # --- Lone Wolf: best haul by a player only one manager started ---
    lone_wolf = _rows(conn.execute("""
        SELECT m.entry_name, m.player_name, p.web_name AS pick, mp.event,
               pev.event_points AS pts
        FROM manager_picks mp
        JOIN managers m ON m.entry_id = mp.entry_id
        JOIN players p ON p.id = mp.element
        JOIN v_player_event_points pev
          ON pev.element = mp.element AND pev.event = mp.event
        JOIN v_ownership_event own
          ON own.event = mp.event AND own.element = mp.element
        WHERE m.league_id = ? AND mp.event <= ? AND mp.multiplier > 0
          AND own.starters = 1
        ORDER BY pev.event_points DESC LIMIT 3
    """, (league_id, event)))

    # --- Ride or Die: longest unbroken run holding a single player ---
    elt_names = {r["id"]: r["web_name"]
                 for r in _rows(conn.execute("SELECT id, web_name FROM players"))}
    hold_rows = _rows(conn.execute("""
        SELECT mp.entry_id, mp.element, mp.event
        FROM manager_picks mp
        JOIN managers m ON m.entry_id = mp.entry_id
        WHERE m.league_id = ? AND mp.event <= ?
        ORDER BY mp.entry_id, mp.element, mp.event
    """, (league_id, event)))
    ride: dict[int, tuple] = {}
    for (eid, element), grp in groupby(hold_rows, key=lambda r: (r["entry_id"], r["element"])):
        evs = [g["event"] for g in grp]
        longest = cur = 1
        for a, b in zip(evs, evs[1:]):
            cur = cur + 1 if b == a + 1 else 1
            longest = max(longest, cur)
        if longest > ride.get(eid, (0, None))[0]:
            ride[eid] = (longest, element)
    ride_or_die = sorted(
        ({"entry_name": _name(eid)[0], "player_name": _name(eid)[1],
          "pick": elt_names.get(el, "?"), "value": run}
         for eid, (run, el) in ride.items()),
        key=lambda r: r["value"], reverse=True)[:3]

    # --- Defensive Contributions: player leaders (league-owned) ---
    league_owned_sql = """
        SELECT DISTINCT element FROM manager_picks mp
        JOIN managers m ON m.entry_id = mp.entry_id WHERE m.league_id = ?"""
    defcon_player: dict[int, dict] = {}
    for r in _rows(conn.execute(f"""
        SELECT pev.element, p.web_name, t.short_name AS team, p.element_type,
               pev.def_con
        FROM v_player_event_points pev
        JOIN players p ON p.id = pev.element
        JOIN teams t ON t.id = p.team_id
        WHERE pev.event <= ? AND pev.def_con IS NOT NULL
          AND pev.element IN ({league_owned_sql})
    """, (event, league_id))):
        dp = defcon_points(r["element_type"], r["def_con"])
        e = r["element"]
        d = defcon_player.setdefault(e, {
            "web_name": r["web_name"], "team": r["team"],
            "pos": POS_LABEL.get(r["element_type"], "?"),
            "def_con_pts": 0, "weeks": 0, "actions": 0})
        d["def_con_pts"] += dp
        d["actions"] += r["def_con"] or 0
        if dp:
            d["weeks"] += 1
    defcon_leaders = sorted(defcon_player.values(),
                            key=lambda r: r["def_con_pts"], reverse=True)[:10]

    # --- Park the Bus: DefCon points accrued by each manager's starting XI ---
    defcon_mgr: dict[int, int] = defaultdict(int)
    for r in _rows(conn.execute("""
        SELECT mp.entry_id, p.element_type, pev.def_con
        FROM manager_picks mp
        JOIN managers m ON m.entry_id = mp.entry_id
        JOIN players p ON p.id = mp.element
        JOIN v_player_event_points pev
          ON pev.element = mp.element AND pev.event = mp.event
        WHERE m.league_id = ? AND mp.event <= ? AND mp.multiplier > 0
    """, (league_id, event))):
        defcon_mgr[r["entry_id"]] += defcon_points(r["element_type"], r["def_con"])
    park_bus_table = sorted(
        ({"entry_name": _name(eid)[0], "player_name": _name(eid)[1], "value": v}
         for eid, v in defcon_mgr.items()),
        key=lambda r: r["value"], reverse=True)
    park_the_bus = park_bus_table[:3]
    has_defcon = any(r["value"] for r in park_bus_table) or bool(defcon_leaders)

    # --- Around the league: darling, the one that got away, filthiest ---
    league_darling = _rows(conn.execute("""
        SELECT p.web_name, t.short_name AS team,
               ROUND(AVG(own.owners) * 100.0 / ?, 0) AS avg_pct
        FROM v_ownership_event own
        JOIN players p ON p.id = own.element
        JOIN teams t ON t.id = p.team_id
        WHERE own.event <= ?
        GROUP BY own.element
        ORDER BY AVG(own.owners) DESC LIMIT 5
    """, (mgr_count or 1, event)))

    got_away = _rows(conn.execute(f"""
        SELECT p.web_name, t.short_name AS team, p.element_type,
               SUM(pev.event_points) AS pts
        FROM v_player_event_points pev
        JOIN players p ON p.id = pev.element
        JOIN teams t ON t.id = p.team_id
        WHERE pev.event <= ? AND pev.element NOT IN ({league_owned_sql})
        GROUP BY pev.element
        ORDER BY pts DESC LIMIT 5
    """, (event, league_id)))
    for r in got_away:
        r["pos"] = POS_LABEL.get(r["element_type"], "?")

    filthiest = _rows(conn.execute(f"""
        SELECT p.web_name, t.short_name AS team,
               SUM(pev.yellow_cards) AS yc, SUM(pev.red_cards) AS rc
        FROM v_player_event_points pev
        JOIN players p ON p.id = pev.element
        JOIN teams t ON t.id = p.team_id
        WHERE pev.event <= ? AND pev.element IN ({league_owned_sql})
        GROUP BY pev.element
        HAVING (SUM(pev.yellow_cards) + SUM(pev.red_cards)) > 0
        ORDER BY (SUM(pev.yellow_cards) + SUM(pev.red_cards) * 2) DESC LIMIT 5
    """, (event, league_id)))

    bandwagon_row = conn.execute("""
        SELECT p.web_name, t.short_name AS team, mt.event, COUNT(*) AS cnt
        FROM manager_transfers mt
        JOIN managers m ON m.entry_id = mt.entry_id
        JOIN players p ON p.id = mt.element_in
        JOIN teams t ON t.id = p.team_id
        WHERE m.league_id = ? AND mt.event <= ?
        GROUP BY mt.event, mt.element_in
        ORDER BY cnt DESC LIMIT 1
    """, (league_id, event)).fetchone()
    bandwagon = dict(bandwagon_row) if bandwagon_row else None

    # --- Transfer Lab table (activity, hits, net, deadline-day habit) ---
    tx_behav = {r["entry_id"]: r for r in _rows(conn.execute("""
        SELECT mt.entry_id, COUNT(*) AS transfers,
               SUM(CASE WHEN mt.time IS NOT NULL AND g.deadline_time IS NOT NULL
                        AND (julianday(g.deadline_time) - julianday(mt.time)) * 24 <= 3
                        THEN 1 ELSE 0 END) AS last_minute
        FROM manager_transfers mt
        JOIN managers m ON m.entry_id = mt.entry_id
        JOIN gameweeks g ON g.id = mt.event
        WHERE m.league_id = ? AND mt.event <= ?
        GROUP BY mt.entry_id
    """, (league_id, event)))}
    transfer_lab = []
    for eid, rows in series.items():
        n = _name(eid)
        tb = tx_behav.get(eid, {})
        t = (tb.get("transfers") if tb else 0) or 0
        lm = (tb.get("last_minute") if tb else 0) or 0
        cost = sum(r["event_transfers_cost"] or 0 for r in rows)
        transfer_lab.append({
            "entry_id": eid,
            "entry_name": n[0], "player_name": n[1], "transfers": t,
            "hits": cost // 4, "hit_pts": cost, "net": season_pnl.get(eid, 0) - cost,
            "last_minute_pct": round(100 * lm / t) if t else 0,
        })
    transfer_lab.sort(key=lambda r: r["transfers"], reverse=True)

    # =====================================================================
    # FORWARD-LOOKING + COUNTERFACTUAL + RIVALRY STATS
    # =====================================================================

    # --- Crystal Ball: projected points for the upcoming GW (FPL ep_next) ---
    max_event = conn.execute("""
        SELECT MAX(mg.event) AS e FROM manager_gameweeks mg
        JOIN managers m ON m.entry_id = mg.entry_id WHERE m.league_id = ?
    """, (league_id,)).fetchone()["e"]
    next_gw = conn.execute(
        "SELECT id FROM gameweeks WHERE id = ? AND finished = 0", (event + 1,)).fetchone()
    has_ep = conn.execute(
        "SELECT 1 FROM players WHERE ep_next IS NOT NULL AND ep_next > 0 LIMIT 1").fetchone()
    show_crystal = bool(next_gw) and event == max_event and bool(has_ep)
    crystal_ball = []
    if show_crystal:
        for r in _rows(conn.execute("""
            SELECT mp.entry_id, SUM(COALESCE(p.ep_next, 0) * mp.multiplier) AS proj
            FROM manager_picks mp
            JOIN managers m ON m.entry_id = mp.entry_id
            JOIN players p ON p.id = mp.element
            WHERE m.league_id = ? AND mp.event = ? AND mp.multiplier > 0
            GROUP BY mp.entry_id
        """, (league_id, event))):
            n = _name(r["entry_id"])
            crystal_ball.append({
                "entry_name": n[0], "player_name": n[1],
                "proj": round(r["proj"] or 0, 1),
                "captain_name": captain_map.get(r["entry_id"], {"name": "-"})["name"],
            })
        crystal_ball.sort(key=lambda r: r["proj"], reverse=True)

    # --- Attacking Returns: player leaders (league-owned) ---
    attack_player: dict[int, dict] = {}
    for r in _rows(conn.execute(f"""
        SELECT pev.element, p.web_name, t.short_name AS team, p.element_type,
               SUM(pev.goals) AS goals, SUM(pev.assists) AS assists
        FROM v_player_event_points pev
        JOIN players p ON p.id = pev.element
        JOIN teams t ON t.id = p.team_id
        WHERE pev.event <= ? AND pev.element IN ({league_owned_sql})
        GROUP BY pev.element
    """, (event, league_id))):
        g, a = r["goals"] or 0, r["assists"] or 0
        ap = g * GOAL_PTS.get(r["element_type"], 4) + a * 3
        if ap > 0:
            attack_player[r["element"]] = {
                "web_name": r["web_name"], "team": r["team"],
                "pos": POS_LABEL.get(r["element_type"], "?"),
                "goals": g, "assists": a, "attack_pts": ap}
    attack_leaders = sorted(attack_player.values(),
                            key=lambda r: r["attack_pts"], reverse=True)[:10]

    # --- The Entertainers: attacking points across each manager's XI ---
    attack_mgr: dict[int, int] = defaultdict(int)
    for r in _rows(conn.execute("""
        SELECT mp.entry_id, p.element_type, pev.goals, pev.assists
        FROM manager_picks mp
        JOIN managers m ON m.entry_id = mp.entry_id
        JOIN players p ON p.id = mp.element
        JOIN v_player_event_points pev
          ON pev.element = mp.element AND pev.event = mp.event
        WHERE m.league_id = ? AND mp.event <= ? AND mp.multiplier > 0
    """, (league_id, event))):
        attack_mgr[r["entry_id"]] += ((r["goals"] or 0) * GOAL_PTS.get(r["element_type"], 4)
                                      + (r["assists"] or 0) * 3)
    entertainers = sorted(
        ({"entry_name": _name(e)[0], "player_name": _name(e)[1], "value": v}
         for e, v in attack_mgr.items()), key=lambda r: r["value"], reverse=True)

    # --- Emperor's New Clothes: popular players with the worst returns ---
    half_season = max(1, event // 2)
    emperors = _rows(conn.execute("""
        SELECT p.web_name, t.short_name AS team, p.element_type,
               ROUND(AVG(own.owners) * 100.0 / ?, 0) AS avg_pct,
               COALESCE(SUM(pev.event_points), 0) AS pts,
               COALESCE(SUM(pev.minutes), 0) AS minutes
        FROM v_ownership_event own
        JOIN players p ON p.id = own.element
        JOIN teams t ON t.id = p.team_id
        LEFT JOIN v_player_event_points pev
          ON pev.element = own.element AND pev.event = own.event
        WHERE own.event <= ?
        GROUP BY own.element
        HAVING AVG(own.owners) >= ? AND COUNT(*) >= ?
        ORDER BY pts ASC LIMIT 5
    """, (mgr_count or 1, event, max(2, 0.2 * (mgr_count or 1)), half_season)))
    for r in emperors:
        r["pos"] = POS_LABEL.get(r["element_type"], "?")

    # --- Sliding Doors: last GW's XI scored on this GW vs what they did ---
    sliding_doors = []
    if event > 1:
        actual_this = {r["entry_id"]: (r["points"] or 0) for r in leaderboard_raw}
        for r in _rows(conn.execute("""
            SELECT mp.entry_id,
                   SUM(COALESCE(pev.event_points, 0) * mp.multiplier) AS standpat
            FROM manager_picks mp
            JOIN managers m ON m.entry_id = mp.entry_id
            LEFT JOIN v_player_event_points pev
              ON pev.element = mp.element AND pev.event = ?
            WHERE m.league_id = ? AND mp.event = ? AND mp.multiplier > 0
            GROUP BY mp.entry_id
        """, (event, league_id, event - 1))):
            eid = r["entry_id"]
            n = _name(eid)
            sp = r["standpat"] or 0
            actual = actual_this.get(eid, 0)
            sliding_doors.append({"entry_name": n[0], "player_name": n[1],
                                  "standpat": sp, "actual": actual,
                                  "diff": actual - sp})
        sliding_doors.sort(key=lambda r: r["diff"], reverse=True)

    # --- What If You'd Done Nothing: GW1 XI held all season ---
    actual_total = {eid: (rows[-1]["total_points"] or 0) for eid, rows in series.items()}
    donothing = []
    for r in _rows(conn.execute("""
        SELECT mp.entry_id,
               SUM(COALESCE(pev.event_points, 0) * mp.multiplier) AS pts
        FROM manager_picks mp
        JOIN managers m ON m.entry_id = mp.entry_id
        LEFT JOIN v_player_event_points pev
          ON pev.element = mp.element AND pev.event <= ?
        WHERE m.league_id = ? AND mp.event = 1 AND mp.multiplier > 0
        GROUP BY mp.entry_id
    """, (event, league_id))):
        eid = r["entry_id"]
        n = _name(eid)
        dn = r["pts"] or 0
        donothing.append({"entry_name": n[0], "player_name": n[1], "donothing": dn,
                          "actual": actual_total.get(eid, 0),
                          "diff": actual_total.get(eid, 0) - dn})
    donothing.sort(key=lambda r: r["donothing"], reverse=True)
    for i, r in enumerate(donothing, 1):
        r["dn_pos"] = i

    # --- Should've Kept Him: the let-go player who scored most afterwards ---
    # Driven off end-of-GW squads (manager_picks), not the transfer log, so
    # wildcard/free-hit churn doesn't double-count provisional moves. For each
    # player a manager owned, take the last GW they held him; if that's before
    # the current GW, tally the points he scored in the spell after.
    kept_best: dict[int, dict] = {}
    for r in _rows(conn.execute("""
        SELECT lo.entry_id, lo.element, lo.last_owned,
               SUM(COALESCE(pev.event_points, 0)) AS pts_after
        FROM (
            SELECT mp.entry_id, mp.element, MAX(mp.event) AS last_owned
            FROM manager_picks mp
            JOIN managers m ON m.entry_id = mp.entry_id
            WHERE m.league_id = ? AND mp.event <= ?
            GROUP BY mp.entry_id, mp.element
        ) lo
        JOIN v_player_event_points pev
          ON pev.element = lo.element AND pev.event > lo.last_owned AND pev.event <= ?
        WHERE lo.last_owned < ?
        GROUP BY lo.entry_id, lo.element
    """, (league_id, event, event, event))):
        eid = r["entry_id"]
        pa = r["pts_after"] or 0
        if pa > kept_best.get(eid, {"value": -1})["value"]:
            kept_best[eid] = {"element": r["element"], "value": pa,
                              "sold_gw": r["last_owned"]}
    should_kept = sorted(
        ({"entry_name": _name(eid)[0], "player_name": _name(eid)[1],
          "pick": elt_names.get(info["element"], "?"), "sold_gw": info["sold_gw"],
          "value": info["value"]} for eid, info in kept_best.items()),
        key=lambda r: r["value"], reverse=True)

    # --- Captain Regret: points missed vs always captaining your best player ---
    maxraw = {(r["entry_id"], r["event"]): (r["max_raw"] or 0) for r in _rows(conn.execute("""
        SELECT mp.entry_id, mp.event, MAX(COALESCE(pev.event_points, 0)) AS max_raw
        FROM manager_picks mp
        JOIN managers m ON m.entry_id = mp.entry_id
        LEFT JOIN v_player_event_points pev
          ON pev.element = mp.element AND pev.event = mp.event
        WHERE m.league_id = ? AND mp.event <= ? AND mp.multiplier > 0
        GROUP BY mp.entry_id, mp.event
    """, (league_id, event)))}
    regret_acc: dict[int, dict] = defaultdict(lambda: {"regret": 0, "perfect": 0, "actual": 0})
    for r in _rows(conn.execute("""
        SELECT cp.entry_id, cp.event, cp.captain_raw_points AS craw
        FROM v_captain_points cp
        JOIN managers m ON m.entry_id = cp.entry_id
        WHERE m.league_id = ? AND cp.event <= ?
    """, (league_id, event))):
        mr = maxraw.get((r["entry_id"], r["event"]), 0)
        cr = r["craw"] or 0
        d = regret_acc[r["entry_id"]]
        d["regret"] += max(0, mr - cr)
        d["perfect"] += mr
        d["actual"] += cr
    captain_regret = sorted(
        ({"entry_name": _name(eid)[0], "player_name": _name(eid)[1],
          "regret": d["regret"], "perfect": d["perfect"], "actual": d["actual"]}
         for eid, d in regret_acc.items()), key=lambda r: r["regret"], reverse=True)
    captain_hindsight = [{"entry_name": r["entry_name"], "player_name": r["player_name"],
                          "value": r["regret"]} for r in captain_regret[:3]]

    # --- Doppelgangers: most similar squads (avg XI overlap over the season) ---
    xi_sets: dict[int, dict[int, set]] = defaultdict(dict)
    for r in _rows(conn.execute("""
        SELECT mp.entry_id, mp.event, mp.element
        FROM manager_picks mp
        JOIN managers m ON m.entry_id = mp.entry_id
        WHERE m.league_id = ? AND mp.event <= ? AND mp.multiplier > 0
    """, (league_id, event))):
        xi_sets[r["entry_id"]].setdefault(r["event"], set()).add(r["element"])
    doppel = []
    dop_ids = list(xi_sets.keys())
    for i in range(len(dop_ids)):
        for j in range(i + 1, len(dop_ids)):
            a, b = dop_ids[i], dop_ids[j]
            common = set(xi_sets[a]) & set(xi_sets[b])
            if not common:
                continue
            overlap = sum(len(xi_sets[a][ev] & xi_sets[b][ev]) for ev in common)
            doppel.append({"a": _name(a)[0], "b": _name(b)[0],
                           "overlap": round(overlap / (len(common) * 11) * 100, 1)})
    doppelgangers = sorted(doppel, key=lambda r: r["overlap"], reverse=True)[:5]

    # --- Head-to-Head Bullies: most lopsided rivalries ---
    bullies = []
    seen_pairs = set()
    for a in eids:
        for b, (w, _d, lo) in h2h_records[a].items():
            if (b, a) in seen_pairs:
                continue
            seen_pairs.add((a, b))
            if w > lo:
                bullies.append({"bully": _name(a)[0], "victim": _name(b)[0],
                                "w": w, "l": lo, "margin": w - lo})
            elif lo > w:
                bullies.append({"bully": _name(b)[0], "victim": _name(a)[0],
                                "w": lo, "l": w, "margin": lo - w})
    h2h_bullies = sorted(bullies, key=lambda r: r["margin"], reverse=True)[:5]

    # =====================================================================
    # DISRESPECT PACK 2 + ANALYTICS — Perfect You, Jinx, FOMO, Ghost XI,
    # Title Race, Rage Transfers, Decision Attribution, PAR, Squad Churn
    # =====================================================================

    ptypes = {r["id"]: r["element_type"]
              for r in _rows(conn.execute("SELECT id, element_type FROM players"))}
    pstat: dict[tuple[int, int], tuple[int, int]] = {}
    for r in _rows(conn.execute(
            "SELECT element, event, event_points, minutes "
            "FROM v_player_event_points WHERE event <= ?", (event,))):
        pstat[(r["element"], r["event"])] = (r["event_points"] or 0, r["minutes"] or 0)

    all_picks = _rows(conn.execute("""
        SELECT mp.entry_id, mp.event, mp.element, mp.multiplier, mp.position
        FROM manager_picks mp
        JOIN managers m ON m.entry_id = mp.entry_id
        WHERE m.league_id = ? AND mp.event <= ?
    """, (league_id, event)))
    squad15: dict[int, dict[int, list]] = defaultdict(dict)
    for r in all_picks:
        squad15[r["entry_id"]].setdefault(r["event"], []).append(
            (r["element"], r["multiplier"], r["position"]))

    bb_weeks = {(r["entry_id"], r["event"]) for r in chip_rows if r["chip"] == "bboost"}

    # --- The Perfect You + Ghost XI (one pass over every squad-week) ---
    sel_err_total: dict[int, int] = defaultdict(int)
    ghost_counts: dict[int, int] = defaultdict(int)
    squad_raw: dict[int, int] = defaultdict(int)       # season XI raw pts (incl BB bench)
    weekly_score: dict[int, dict[int, int]] = defaultdict(dict)  # XI-only raw, for PAR
    for eid, by_ev in squad15.items():
        for ev, picks in by_ev.items():
            xi_raw = 0
            bench_bb_pts = 0
            squad_tp = []
            for el, mult, pos in picks:
                pts, minutes = pstat.get((el, ev), (0, 0))
                squad_tp.append((ptypes.get(el, 4), pts))
                if mult > 0:
                    xi_raw += pts
                    if minutes == 0:
                        ghost_counts[eid] += 1
                    if pos > 11:
                        bench_bb_pts += pts   # only possible on a BB week
            squad_raw[eid] += xi_raw
            weekly_score[eid][ev] = xi_raw - bench_bb_pts
            if (eid, ev) in bb_weeks:
                continue   # all 15 counted: no selection error possible
            best = _best_xi_points(squad_tp)
            if best is not None:
                sel_err_total[eid] += max(0, best - xi_raw)

    perfect_you = []
    for eid, rows in series.items():
        n = _name(eid)
        actual = rows[-1]["total_points"] or 0
        hits = sum(r["event_transfers_cost"] or 0 for r in rows)
        cap = regret_acc[eid]["regret"] if eid in regret_acc else 0
        sel = sel_err_total.get(eid, 0)
        waste = sel + cap + hits
        perfect_you.append({
            "entry_name": n[0], "player_name": n[1],
            "actual": actual, "sel": sel, "cap": cap, "hits": hits,
            "perfect": actual + waste, "waste": waste,
            "real_pos": real_pos.get(eid),
        })
    perfect_you.sort(key=lambda r: r["perfect"], reverse=True)
    for i, r in enumerate(perfect_you, 1):
        r["pos_change"] = (r["real_pos"] - i) if r["real_pos"] else None

    ghost_xi_top = sorted(
        ({"entry_name": _name(e)[0], "player_name": _name(e)[1], "value": v}
         for e, v in ghost_counts.items()),
        key=lambda r: r["value"], reverse=True)[:3]

    # --- The Jinx + Buy High Sell Low (squad-delta based, FH weeks skipped) ---
    owned = {eid: {ev: {el for el, _m, _p in picks} for ev, picks in by_ev.items()}
             for eid, by_ev in squad15.items()}

    jinx_rows = []
    fomo_rows = []
    for eid, by_ev in owned.items():
        fh = fh_gws.get(eid, set())
        n = _name(eid)

        jinx_total = 0
        worst = None
        buys = 0
        bought_form = 0.0
        delivered = 0.0
        for ev in sorted(by_ev):
            nxt = ev + 1
            if nxt in by_ev and ev not in fh and nxt not in fh:
                for el in by_ev[ev] - by_ev[nxt]:
                    pts = pstat.get((el, nxt), (0, 0))[0]
                    jinx_total += pts
                    if worst is None or pts > worst[2]:
                        worst = (el, nxt, pts)
            prev = ev - 1
            if prev in by_ev and ev not in fh and prev not in fh:
                for el in by_ev[ev] - by_ev[prev]:
                    pre_evs = range(max(1, ev - 3), ev)
                    post_evs = range(ev, min(event, ev + 2) + 1)
                    pre_avg = sum(pstat.get((el, g), (0, 0))[0] for g in pre_evs) / len(pre_evs)
                    post_avg = sum(pstat.get((el, g), (0, 0))[0] for g in post_evs) / len(post_evs)
                    buys += 1
                    bought_form += pre_avg
                    delivered += post_avg

        if worst:
            jinx_rows.append({
                "entry_name": n[0], "player_name": n[1], "value": jinx_total,
                "worst_pick": elt_names.get(worst[0], "?"),
                "worst_gw": worst[1], "worst_pts": worst[2],
            })
        if buys:
            fomo_rows.append({
                "entry_name": n[0], "player_name": n[1], "buys": buys,
                "bought_form": round(bought_form / buys, 1),
                "delivered": round(delivered / buys, 1),
                "value": round(bought_form - delivered, 1),
            })
    jinx_rows.sort(key=lambda r: r["value"], reverse=True)
    jinx_top = jinx_rows[:3]
    fomo_rows.sort(key=lambda r: r["value"], reverse=True)
    fomo_top = fomo_rows[:3]

    # --- Title Race / The Bottle Job (league-rank history) ---
    rank_hist: dict[int, list] = defaultdict(list)
    for r in season_table:
        rank_hist[r["entry_id"]].append((r["event"], r["tml_rank"]))
    title_race = []
    for eid, hist in rank_hist.items():
        hist.sort()
        ranks = [t for _ev, t in hist]
        n = _name(eid)
        final = real_pos.get(eid) or ranks[-1]
        title_race.append({
            "entry_name": n[0], "player_name": n[1],
            "weeks_first": sum(1 for t in ranks if t == 1),
            "weeks_top3": sum(1 for t in ranks if t <= 3),
            "peak": min(ranks), "final": final, "fall": final - min(ranks),
        })
    title_race.sort(key=lambda r: (-r["weeks_first"], -r["weeks_top3"], r["final"]))
    # A real bottle job led the league and didn't win; failing that, the
    # biggest fall from a top-3 peak.
    bottlers = [r for r in title_race if r["peak"] <= 3 and r["fall"] > 0]
    bottle_job = max(bottlers, key=lambda r: (r["weeks_first"], r["fall"])) if bottlers else None

    # --- Rage Transfers + Squad Churn (added to the Transfer Lab table) ---
    last_kick: dict[int, datetime] = {}
    for r in _rows(conn.execute(
            "SELECT event, MAX(kickoff_time) AS k FROM fixtures "
            "WHERE event IS NOT NULL AND kickoff_time IS NOT NULL GROUP BY event")):
        try:
            last_kick[r["event"]] = datetime.fromisoformat(r["k"].replace("Z", "+00:00"))
        except (TypeError, ValueError):
            pass

    rage_counts: dict[int, list[int]] = defaultdict(lambda: [0, 0])   # [rage, total]
    for r in _rows(conn.execute("""
        SELECT mt.entry_id, mt.event, mt.time
        FROM manager_transfers mt
        JOIN managers m ON m.entry_id = mt.entry_id
        WHERE m.league_id = ? AND mt.event <= ?
    """, (league_id, event))):
        rc = rage_counts[r["entry_id"]]
        rc[1] += 1
        prev_end = last_kick.get(r["event"] - 1)
        if prev_end is None or not r["time"]:
            continue
        try:
            t = datetime.fromisoformat(r["time"].replace("Z", "+00:00"))
        except ValueError:
            continue
        if t <= prev_end + RAGE_WINDOW:
            rc[0] += 1

    players_used = {eid: len(set().union(*by_ev.values()))
                    for eid, by_ev in owned.items() if by_ev}
    for r in transfer_lab:
        rage, tot = rage_counts.get(r["entry_id"], [0, 0])
        r["rage_pct"] = round(100 * rage / tot) if tot else 0
        r["players_used"] = players_used.get(r["entry_id"], 0)

    churn_corr = None
    pairs = [(players_used[eid], real_pos[eid]) for eid in players_used if eid in real_pos]
    if len(pairs) >= 3:
        try:
            churn_corr = round(statistics.correlation(
                [float(p[0]) for p in pairs], [float(p[1]) for p in pairs]), 2)
        except statistics.StatisticsError:
            churn_corr = None

    # --- Decision Attribution: total = XI raw + captaincy extra − hits ---
    cap_extra = {r["entry_id"]: r["extra"] or 0 for r in _rows(conn.execute("""
        SELECT cp.entry_id,
               SUM(cp.captain_effective_points - cp.captain_raw_points) AS extra
        FROM v_captain_points cp
        JOIN managers m ON m.entry_id = cp.entry_id
        WHERE m.league_id = ? AND cp.event <= ?
        GROUP BY cp.entry_id
    """, (league_id, event)))}

    attribution = []
    for eid, rows in series.items():
        n = _name(eid)
        hits = sum(r["event_transfers_cost"] or 0 for r in rows)
        attribution.append({
            "entry_name": n[0], "player_name": n[1],
            "squad": squad_raw.get(eid, 0), "captaincy": cap_extra.get(eid, 0),
            "hits": hits, "total": rows[-1]["total_points"] or 0,
        })
    nmgr = len(attribution) or 1
    base_squad = sum(r["squad"] for r in attribution) / nmgr
    base_cap = sum(r["captaincy"] for r in attribution) / nmgr
    base_hits = sum(r["hits"] for r in attribution) / nmgr
    attribution_base = {"squad": round(base_squad), "cap": round(base_cap),
                        "hits": round(base_hits),
                        "total": round(base_squad + base_cap - base_hits)}
    for r in attribution:
        r["squad_d"] = round(r["squad"] - base_squad)
        r["cap_d"] = round(r["captaincy"] - base_cap)
        r["hits_d"] = round(base_hits - r["hits"])   # fewer hits than average = positive
        r["recon"] = r["squad"] + r["captaincy"] - r["hits"]
    attribution.sort(key=lambda r: r["total"], reverse=True)

    # --- PAR: Points Above Replacement vs the league's template XI ---
    own_by_ev: dict[int, list] = defaultdict(list)
    for r in _rows(conn.execute(
            "SELECT event, element, starters, captains "
            "FROM v_ownership_event WHERE event <= ?", (event,))):
        own_by_ev[r["event"]].append(r)

    cap_raw_ev = {(r["entry_id"], r["event"]): r["captain_raw_points"] or 0
                  for r in _rows(conn.execute("""
        SELECT cp.entry_id, cp.event, cp.captain_raw_points
        FROM v_captain_points cp
        JOIN managers m ON m.entry_id = cp.entry_id
        WHERE m.league_id = ? AND cp.event <= ?
    """, (league_id, event)))}

    template_total = 0
    template_by_ev: dict[int, int] = {}
    for ev, own_rows in own_by_ev.items():
        cands: dict[int, list] = {1: [], 2: [], 3: [], 4: []}
        for r in own_rows:
            t = ptypes.get(r["element"])
            if t in cands:
                cands[t].append((r["starters"] or 0, r["element"]))
        for v in cands.values():
            v.sort(reverse=True)
        best_own = None
        best_sel = None
        for d, m_, f in FORMATIONS:
            if not cands[1] or len(cands[2]) < d or len(cands[3]) < m_ or len(cands[4]) < f:
                continue
            own_sum = (cands[1][0][0] + sum(s for s, _e in cands[2][:d])
                       + sum(s for s, _e in cands[3][:m_])
                       + sum(s for s, _e in cands[4][:f]))
            if best_own is None or own_sum > best_own:
                best_own = own_sum
                best_sel = ([cands[1][0][1]] + [e for _s, e in cands[2][:d]]
                            + [e for _s, e in cands[3][:m_]]
                            + [e for _s, e in cands[4][:f]])
        if not best_sel:
            continue
        tpl = sum(pstat.get((el, ev), (0, 0))[0] for el in best_sel)
        most_cap = max(own_rows, key=lambda r: (r["captains"] or 0, r["starters"] or 0))
        tpl += pstat.get((most_cap["element"], ev), (0, 0))[0]
        template_by_ev[ev] = tpl
        template_total += tpl

    par_table = []
    for eid in series:
        n = _name(eid)
        mine = sum(weekly_score.get(eid, {}).get(ev, 0) + cap_raw_ev.get((eid, ev), 0)
                   for ev in template_by_ev)
        par_table.append({"entry_name": n[0], "player_name": n[1],
                          "score": mine, "par": mine - template_total})
    par_table.sort(key=lambda r: r["par"], reverse=True)

    # =====================================================================
    # THE WEEK IN WORDS — deterministic editorial narrative.
    # Seeded per (league, GW) so the same report always renders the same
    # prose, but each gameweek draws different phrasings from the banks.
    # =====================================================================
    rng = random.Random(league_id * 100 + event)

    def _esc(s) -> str:
        return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))

    def _b(s) -> str:
        return f"<strong>{_esc(s)}</strong>"

    disgraces: list[str] = []
    shockers: list[str] = []
    nerd: list[str] = []

    # --- Disgraces ---
    if len(gw_points_sorted) >= 2:
        worst_w = gw_points_sorted[-1]
        gap = round((gw_avg or 0) - (worst_w["points"] or 0))
        pts_s = f"{worst_w['points']} points"
        disgraces.append(rng.choice([
            f"The wooden spoon goes to {_b(worst_w['entry_name'])} ({_esc(worst_w['player_name'])}), whose {_b(pts_s)} landed {gap} below the league average. Frame it.",
            f"Somebody had to finish last, and {_b(worst_w['entry_name'])} volunteered enthusiastically: {_b(pts_s)}, a full {gap} under par.",
            f"{_b(worst_w['entry_name'])} propped up the entire gameweek with {_b(pts_s)} — {gap} short of average and somehow legal.",
        ]))

    if len(captains) >= 2:
        wc_w, bc_w = captains[-1], captains[0]
        if (wc_w["captain_effective_points"] or 0) <= 6:
            disgraces.append(rng.choice([
                f"{_b(wc_w['entry_name'])} handed the armband to {_esc(wc_w['captain_name'])} for a princely {_b(wc_w['captain_effective_points'])} points, while {_esc(bc_w['entry_name'])} was banking {bc_w['captain_effective_points']} from {_esc(bc_w['captain_name'])}.",
                f"Captaincy masterclass from {_b(wc_w['entry_name'])}: {_esc(wc_w['captain_name'])} returned {_b(wc_w['captain_effective_points'])}. For reference, {_esc(bc_w['entry_name'])}'s {_esc(bc_w['captain_name'])} managed {bc_w['captain_effective_points']}.",
            ]))

    bench_w = max(leaderboard, key=lambda r: r["points_on_bench"] or 0, default=None)
    if bench_w and (bench_w["points_on_bench"] or 0) >= 10:
        disgraces.append(rng.choice([
            f"{_b(bench_w['entry_name'])} left {_b(bench_w['points_on_bench'])} points warming the bench — selection by coin toss, presumably.",
            f"Meanwhile {_b(bench_w['entry_name'])}'s bench outdid itself: {_b(bench_w['points_on_bench'])} points watching from the sidelines.",
        ]))

    burned = [r for r in leaderboard
              if (r["event_transfers_cost"] or 0) > 0 and (r["transfer_net"] or 0) < 0]
    if burned:
        hit_w = min(burned, key=lambda r: r["transfer_net"])
        disgraces.append(rng.choice([
            f"{_b(hit_w['entry_name'])} paid {hit_w['event_transfers_cost']} points in hits for the privilege of losing {_b(abs(hit_w['transfer_net']))} more — premium-rate self-sabotage.",
            f"Special mention to {_b(hit_w['entry_name'])}, who took a -{hit_w['event_transfers_cost']} hit and still came out {_b(abs(hit_w['transfer_net']))} points worse off on transfers.",
        ]))

    if sliding_doors:
        sd_w = sliding_doors[-1]
        if sd_w["diff"] <= -5:
            disgraces.append(
                f"And the Sliding Doors award: had {_b(sd_w['entry_name'])} simply gone on holiday this week, last week's team would have scored {sd_w['standpat']} — instead the meddling produced {sd_w['actual']}. That's {_b(abs(sd_w['diff']))} points of pure activity tax.")

    ghosts_week: dict[int, int] = {}
    for g_eid, g_by_ev in squad15.items():
        g = sum(1 for el, m_, _p in (g_by_ev.get(event) or [])
                if m_ > 0 and pstat.get((el, event), (0, 0))[1] == 0)
        if g:
            ghosts_week[g_eid] = g
    if ghosts_week:
        g_eid, g_n = max(ghosts_week.items(), key=lambda kv: kv[1])
        if g_n >= 2:
            disgraces.append(
                f"{_b(_name(g_eid)[0])} fielded {_b(g_n)} players who recorded zero minutes. Bold strategy: you can't blank if you never play.")

    # --- Shockers ---
    if gw_points_sorted:
        top_w = gw_points_sorted[0]
        margin = (top_w["points"] or 0) - ((gw_points_sorted[1]["points"] or 0)
                                           if len(gw_points_sorted) > 1 else 0)
        if margin >= 5:
            shockers.append(rng.choice([
                f"{_b(top_w['entry_name'])} ({_esc(top_w['player_name'])}) ran away with the week: {_b(top_w['points'])} points, {margin} clear of the field.",
                f"No contest at the top — {_b(top_w['entry_name'])} posted {_b(top_w['points'])}, daylight second ({margin} back).",
            ]))
        else:
            shockers.append(
                f"{_b(top_w['entry_name'])} took Manager of the Week with {_b(top_w['points'])} points.")

    if gw_stats.get("new_leader"):
        nl = gw_stats["new_leader"]
        shockers.append(
            f"There's a new name on the perch: {_b(nl['entry_name'])} seizes top spot, up from {_ordinal(nl['from_pos'])}.")

    riser = gw_stats.get("biggest_riser")
    if riser and (riser.get("movement") or 0) >= 3:
        shockers.append(
            f"{_b(riser['entry_name'])} rocketed {_b(riser['movement'])} places up the table.")

    bt = gw_stats.get("best_transfer")
    if bt and (bt.get("net") or 0) >= 8:
        shockers.append(rng.choice([
            f"Transfer of the week: {_b(bt['entry_name'])} swapped {_esc(bt['sold'])} for {_esc(bt['bought'])} and pocketed {_b('+' + str(bt['net']))}.",
            f"{_b(bt['entry_name'])} saw the future, buying {_esc(bt['bought'])} for {_esc(bt['sold'])} — a {_b('+' + str(bt['net']))} stroke of genius.",
        ]))

    week_chips = [c for c in chip_detail if c["event"] == event]
    for c in sorted(week_chips, key=lambda r: r["points"], reverse=True)[:2]:
        if c["points"] >= 8:
            shockers.append(
                f"{_b(c['entry_name'])} cashed the {c['chip_label']} for {_b(c['points'])} points.")
        elif c["points"] <= 2:
            shockers.append(
                f"{_b(c['entry_name'])} burned the {c['chip_label']} for a grand total of {_b(c['points'])}. Worth the wait.")

    week_starters = {r["element"]: (r["starters"] or 0) for r in own_by_ev.get(event, [])}
    solo_best = None
    for s_eid, s_by_ev in squad15.items():
        for el, m_, _p in (s_by_ev.get(event) or []):
            if m_ > 0 and week_starters.get(el) == 1:
                p = pstat.get((el, event), (0, 0))[0]
                if solo_best is None or p > solo_best[2]:
                    solo_best = (s_eid, el, p)
    if solo_best and solo_best[2] >= 10:
        shockers.append(
            f"Differential of the week: {_b(_name(solo_best[0])[0])} was the only manager starting {_esc(elt_names.get(solo_best[1], '?'))}, who duly hauled {_b(solo_best[2])}.")

    # --- Nerd corner ---
    if fpl_avg is not None and gw_avg is not None:
        diff = gw_avg - fpl_avg
        if diff >= 2:
            nerd.append(
                f"Collectively respectable: the league averaged {_b('%.1f' % gw_avg)}, {('%+.0f' % diff)} against the global FPL average of {fpl_avg}.")
        elif diff <= -2:
            nerd.append(
                f"The league averaged {_b('%.1f' % gw_avg)} this week — {('%.0f' % abs(diff))} points {_b('below')} the global FPL average. Eleven million casuals would like a word.")
        else:
            nerd.append(
                f"The league averaged {_b('%.1f' % gw_avg)}, dead level with the rest of the world ({fpl_avg}).")

    tpl_week = template_by_ev.get(event)
    if tpl_week:
        beat = sum(1 for t_eid in series
                   if weekly_score.get(t_eid, {}).get(event, 0)
                   + cap_raw_ev.get((t_eid, event), 0) > tpl_week)
        if beat == 0:
            nerd.append(
                f"The league's template XI would have scored {_b(tpl_week)} this week. Number of managers who beat it: {_b('zero')}. Original thinking remains overrated.")
        else:
            nerd.append(
                f"The template XI benchmark scored {_b(tpl_week)} this week; only {_b(beat)} of {mgr_count} managers beat the autopilot.")

    dc_week_el = {r["element"]: r["def_con"] or 0 for r in _rows(conn.execute(
        "SELECT element, def_con FROM v_player_event_points WHERE event = ?", (event,)))}
    dc_week_mgr: dict[int, int] = defaultdict(int)
    for d_eid, d_by_ev in squad15.items():
        for el, m_, _p in (d_by_ev.get(event) or []):
            if m_ > 0:
                dc_week_mgr[d_eid] += defcon_points(ptypes.get(el, 4), dc_week_el.get(el, 0))
    if dc_week_mgr:
        d_eid, d_v = max(dc_week_mgr.items(), key=lambda kv: kv[1])
        if d_v >= 6:
            nerd.append(
                f"Park-the-bus report: {_b(_name(d_eid)[0])}'s XI ground out {_b(d_v)} DefCon points, the week's high. Beautiful? No. Points? Yes.")

    if event >= 5 and luckiest:
        lk = luckiest[0]
        if (lk.get("diff") or 0) > 0:
            nerd.append(
                f"The xPts model maintains that {_b(lk['entry_name'])} is the league's luckiest manager, running {_b('+%.0f' % lk['diff'])} above expected. Regression is patient.")

    narrative = [p for p in (
        {"label": "The Disgraces", "color": "var(--red)", "html": " ".join(disgraces)},
        {"label": "The Shockers", "color": "var(--strong)", "html": " ".join(shockers)},
        {"label": "The Nerd Corner", "color": "var(--green)", "html": " ".join(nerd)},
    ) if p["html"]]

    # --- Structured facts for the optional LLM-written narrative ---
    # Same signals as the deterministic prose above, as machine-readable JSON.
    facts: dict = {
        "gameweek": event, "managers": mgr_count,
        "league_average": round(gw_avg, 1) if gw_avg is not None else None,
        "fpl_average": fpl_avg,
    }
    if gw_points_sorted:
        worst = gw_points_sorted[-1]
        facts["wooden_spoon"] = {
            "team": worst["entry_name"], "manager": worst["player_name"],
            "points": worst["points"],
            "below_average": round((gw_avg or 0) - (worst["points"] or 0))}
        top = gw_points_sorted[0]
        facts["manager_of_the_week"] = {
            "team": top["entry_name"], "manager": top["player_name"],
            "points": top["points"],
            "margin_over_second": (top["points"] or 0)
            - ((gw_points_sorted[1]["points"] or 0) if len(gw_points_sorted) > 1 else 0)}
    if captains:
        bc, wcp = captains[0], captains[-1]
        facts["best_captain"] = {
            "team": bc["entry_name"], "manager": bc["player_name"],
            "captain": bc["captain_name"], "points": bc["captain_effective_points"]}
        if (wcp["captain_effective_points"] or 0) <= 6:
            facts["worst_captain"] = {
                "team": wcp["entry_name"], "manager": wcp["player_name"],
                "captain": wcp["captain_name"], "points": wcp["captain_effective_points"]}
    if bench_w and (bench_w["points_on_bench"] or 0) >= 8:
        facts["bench_disaster"] = {
            "team": bench_w["entry_name"], "manager": bench_w["player_name"],
            "bench_points": bench_w["points_on_bench"]}
    if burned:
        hw = min(burned, key=lambda r: r["transfer_net"])
        facts["worst_hit"] = {
            "team": hw["entry_name"], "manager": hw["player_name"],
            "hit_cost": hw["event_transfers_cost"], "net_transfer_points": hw["transfer_net"]}
    if sliding_doors and sliding_doors[-1]["diff"] <= -5:
        sd = sliding_doors[-1]
        facts["sliding_doors"] = {
            "team": sd["entry_name"], "manager": sd["player_name"],
            "stand_pat_points": sd["standpat"], "actual_points": sd["actual"],
            "points_lost": abs(sd["diff"])}
    if ghosts_week:
        ge, gn = max(ghosts_week.items(), key=lambda kv: kv[1])
        if gn >= 2:
            facts["ghost_xi"] = {"team": _name(ge)[0], "manager": _name(ge)[1],
                                 "zero_minute_starters": gn}
    if gw_stats.get("new_leader"):
        nl = gw_stats["new_leader"]
        facts["new_leader"] = {"team": nl["entry_name"], "from_position": nl["from_pos"]}
    rs = gw_stats.get("biggest_riser")
    if rs and (rs.get("movement") or 0) >= 3:
        facts["biggest_riser"] = {"team": rs["entry_name"], "places_climbed": rs["movement"]}
    bt2 = gw_stats.get("best_transfer")
    if bt2 and (bt2.get("net") or 0) >= 8:
        facts["transfer_of_the_week"] = {
            "team": bt2["entry_name"], "bought": bt2["bought"],
            "sold": bt2["sold"], "net_points": bt2["net"]}
    if week_chips:
        facts["chips_played"] = [
            {"team": c["entry_name"], "manager": c["player_name"],
             "chip": c["chip_label"], "points": c["points"]} for c in week_chips]
    if solo_best and solo_best[2] >= 10:
        facts["differential_of_the_week"] = {
            "team": _name(solo_best[0])[0],
            "player": elt_names.get(solo_best[1], "?"), "points": solo_best[2]}
    if tpl_week:
        beat_n = sum(1 for te in series
                     if weekly_score.get(te, {}).get(event, 0)
                     + cap_raw_ev.get((te, event), 0) > tpl_week)
        facts["template_benchmark"] = {
            "points": tpl_week, "managers_who_beat_it": beat_n, "total_managers": mgr_count}
    if dc_week_mgr:
        de, dv = max(dc_week_mgr.items(), key=lambda kv: kv[1])
        if dv >= 6:
            facts["defcon_high"] = {"team": _name(de)[0], "manager": _name(de)[1],
                                    "defcon_points": dv}
    if event >= 5 and luckiest and (luckiest[0].get("diff") or 0) > 0:
        facts["luckiest_manager"] = {
            "team": luckiest[0]["entry_name"], "xpts_overperformance": luckiest[0]["diff"]}

    # --- Season state: standings, title race, momentum, running storylines ---
    # The context that lets a narrator keep a thread going week to week.
    total_gws = (conn.execute("SELECT MAX(id) AS m FROM gameweeks").fetchone()["m"]
                 or event)
    tr_weeks = {r["entry_name"]: r["weeks_first"] for r in title_race}
    ghost_tally: dict[int, int] = defaultdict(int)
    for g2_eid, g2_by_ev in squad15.items():
        for ev2, picks in g2_by_ev.items():
            for el, m_, _p in picks:
                if m_ > 0 and pstat.get((el, ev2), (0, 0))[1] == 0:
                    ghost_tally[g2_eid] += 1
    if leaderboard:
        leader = leaderboard[0]
        lead_total = leader["total_points"] or 0
        second_total = (leaderboard[1]["total_points"] or 0) if len(leaderboard) > 1 else lead_total
        form_sorted = sorted(leaderboard, key=lambda r: r["form"] or 0, reverse=True)
        howlers = sorted(captaincy_standings, key=lambda r: r["blanks"], reverse=True)
        ghosts_ranked = sorted(ghost_tally.items(), key=lambda kv: kv[1], reverse=True)
        most_top = max(title_race, key=lambda r: r["weeks_first"]) if title_race else None
        facts["season"] = {
            "gameweeks_played": event,
            "gameweeks_remaining": max(0, total_gws - event),
            "standings_top": [
                {"pos": r["pos"], "team": r["entry_name"], "manager": r["player_name"],
                 "total": r["total_points"], "gap_to_first": lead_total - (r["total_points"] or 0)}
                for r in leaderboard[:6]],
            "bottom": [
                {"pos": r["pos"], "team": r["entry_name"], "total": r["total_points"]}
                for r in leaderboard[-2:]],
            "leader": {"team": leader["entry_name"], "manager": leader["player_name"],
                       "lead_over_second": lead_total - second_total,
                       "weeks_on_top": tr_weeks.get(leader["entry_name"], 0)},
            "title_race_chasers": [
                {"team": r["entry_name"], "gap": lead_total - (r["total_points"] or 0)}
                for r in leaderboard[1:6] if (lead_total - (r["total_points"] or 0)) <= 25],
            "form_hottest": [{"team": r["entry_name"], "last5": r["form"]}
                             for r in form_sorted[:2]],
            "form_coldest": [{"team": r["entry_name"], "last5": r["form"]}
                             for r in form_sorted[-2:]],
            "running_storylines": {
                "luckiest_all_season": ({"team": luckiest[0]["entry_name"],
                                         "xpts_over": round(luckiest[0]["diff"])}
                                        if luckiest else None),
                "captaincy_howlers": [{"team": r["entry_name"], "blank_weeks": r["blanks"]}
                                      for r in howlers[:2] if r["blanks"] >= 2],
                "ghost_offenders": [{"team": _name(eid)[0], "ghost_starts": n}
                                    for eid, n in ghosts_ranked[:2] if n >= 3],
                "most_weeks_on_top": ({"team": most_top["entry_name"],
                                       "weeks": most_top["weeks_first"]}
                                      if most_top and most_top["weeks_first"] else None),
            },
        }

    # --- Same-week-last-season comparison (dormant until a prev DB is given) ---
    last_season = None
    prev = _prev_season_stats(prev_db, league_id, event) if prev_db else None
    if prev:
        rows = []
        for r in leaderboard:
            pe = (prev["totals"].get(f"id:{r['entry_id']}")
                  or prev["totals"].get(manager_key(r["player_name"])))
            if pe:
                rows.append({
                    "team": r["entry_name"], "manager": r["player_name"],
                    "this_total": r["total_points"], "last_total": pe["total"],
                    "delta": (r["total_points"] or 0) - (pe["total"] or 0),
                    "this_rank": r["pos"], "last_rank": pe["rank"]})
        last_season = {
            "league_avg_now": round(gw_avg, 1) if gw_avg is not None else None,
            "league_avg_last": prev["league_avg"],
            "leader_last": prev["leader"],
            "managers": rows,
        }
        if "season" in facts:
            facts["season"]["last_year"] = {
                "league_avg_last": prev["league_avg"],
                "leader_last": prev["leader"],
                "biggest_improvers": sorted(rows, key=lambda r: r["delta"], reverse=True)[:3],
                "biggest_decliners": sorted(rows, key=lambda r: r["delta"])[:3],
            }

    return {
        "last_season": last_season,
        "benchmark": _benchmark_block(prev_db, league_id, season or "",
                                      event, leaderboard),
        "event": event,
        "league_id": league_id,
        "league_name": league_name,
        "mgr_count": mgr_count,
        "gw_avg": gw_avg,
        "fpl_avg": fpl_avg,
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "num_form_gws": num_form_gws,
        # Hall of Fame
        "top_overall": top_overall,
        "motw": motw,
        "wheeler": wheeler,
        "most_valuable": most_valuable,
        "hot_streak": hot_streak,
        "teachers_pet": teachers_pet,
        "balls_of_steel": balls_of_steel,
        # Hall of Shame
        "bottom_overall": bottom_overall,
        "rogue": rogue,
        "least_valuable": least_valuable,
        "cold_streak": cold_streak,
        "lost_dressing_room": lost_dressing_room,
        "balls_of_cotton": balls_of_cotton,
        # Leaderboard
        "leaderboard": leaderboard,
        # GW Review
        "top_scorers": top_scorers,
        "most_captained": most_captained,
        "total_mgrs": mgr_count or 1,
        "gw_stats": gw_stats,
        # Transfers
        "transfers": transfers,
        "transfer_ticker": transfer_ticker,
        # Captains
        "captains": captains,
        "captain_avg": captain_avg,
        "season_captains": season_captains,
        "all_captain_totals": all_captain_totals,
        # GW Breakdown
        "gw_breakdown": gw_breakdown,
        # Chip Usage
        "chip_summary": chip_summary,
        "chip_types": chip_types,
        "chip_labels": chip_labels,
        "chip_detail": chip_detail,
        "chip_top5": chip_top5,
        # Wildcard Maestro
        "wc_maestro": wc_maestro,
        # Manager of the Month
        "monthly_winners": monthly_winners,
        # Scout Report
        "scout_over": scout_over,
        "scout_under": scout_under,
        "scout_season": scout_season,
        # xPts
        "xpts_leaderboard": xpts_leaderboard,
        "luckiest": luckiest,
        "unluckiest": unluckiest,
        # Season table (per-manager per-GW with TML rank)
        "season_table": season_table,
        "season_table_first": season_table_first,
        # --- Extended stats ---
        # Hall of Records (awards)
        "mr_reliable": mr_reliable,
        "rollercoaster": rollercoaster,
        "magnum_opus": magnum_opus,
        "the_stinker": the_stinker,
        "bench_disaster": bench_disaster,
        "hit_merchant": hit_merchant,
        "scrooge": scrooge,
        "tinkerman": tinkerman,
        "set_and_forget": set_and_forget,
        "mr_template": mr_template,
        "the_hipster": the_hipster,
        "lone_wolf": lone_wolf,
        "ride_or_die": ride_or_die,
        "captain_marvel": captain_marvel,
        "captain_calamity": captain_calamity,
        "park_the_bus": park_the_bus,
        # By the Numbers (tables)
        "consistency": consistency,
        "rank_traj": rank_traj,
        "transfer_lab": transfer_lab,
        "squad_style": squad_style,
        # Defensive Contributions
        "defcon_leaders": defcon_leaders,
        "park_bus_table": park_bus_table,
        "has_defcon": has_defcon,
        # Alternative tables
        "captaincy_standings": captaincy_standings,
        "nohits": nohits,
        # Head-to-Head
        "h2h_summary": h2h_summary,
        "h2h_matrix": h2h_matrix,
        "h2h_labels": h2h_labels,
        # Around the league
        "league_darling": league_darling,
        "got_away": got_away,
        "filthiest": filthiest,
        "bandwagon": bandwagon,
        "emperors": emperors,
        # Forward-looking / counterfactual / rivalry
        "show_crystal": show_crystal,
        "crystal_ball": crystal_ball,
        "next_event": event + 1,
        "attack_leaders": attack_leaders,
        "entertainers": entertainers,
        "sliding_doors": sliding_doors,
        "donothing": donothing,
        "should_kept": should_kept,
        "captain_regret": captain_regret,
        "captain_hindsight": captain_hindsight,
        "doppelgangers": doppelgangers,
        "h2h_bullies": h2h_bullies,
        # Disrespect pack 2 + analytics
        "perfect_you": perfect_you,
        "ghost_xi_top": ghost_xi_top,
        "jinx_rows": jinx_rows,
        "jinx_top": jinx_top,
        "fomo_rows": fomo_rows,
        "fomo_top": fomo_top,
        "title_race": title_race,
        "bottle_job": bottle_job,
        "attribution": attribution,
        "attribution_base": attribution_base,
        "par_table": par_table,
        "template_total": template_total,
        "churn_corr": churn_corr,
        # Narrative
        "narrative": narrative,
        "narrative_facts": facts,
    }


# ---------------------------------------------------------------------------
# xPts computation
# ---------------------------------------------------------------------------

def defcon_points(element_type: int, def_con: int | None) -> int:
    """Defensive-contribution points (2025/26 rules).

    ``def_con`` is the API's count of qualifying actions — CBIT for defenders,
    CBIRT for midfielders/forwards. Defenders earn 2 pts at 10+, mid/fwd at 12+.
    Goalkeepers are not eligible. Capped at 2 per match.
    """
    if not def_con:
        return 0
    if element_type == 2:
        return 2 if def_con >= 10 else 0
    if element_type in (3, 4):
        return 2 if def_con >= 12 else 0
    return 0


def _compute_xpts(element_type: int, minutes: int, xg: float, xa: float,
                  xgc: float, saves: int = 0, penalties_saved: int = 0,
                  penalties_missed: int = 0, own_goals: int = 0,
                  yellow_cards: int = 0, red_cards: int = 0,
                  bonus: int = 0, def_con: int = 0) -> float:
    """Hybrid xPts: expected for goals/assists/CS, actual for everything else
    (including defensive-contribution points, which are volume-driven and
    largely repeatable, so we count what actually happened)."""
    if not minutes:
        return 0.0
    pts = 2.0 if minutes >= 60 else 1.0
    goal_pts = {1: 10, 2: 6, 3: 5, 4: 4}.get(element_type, 4)
    pts += xg * goal_pts
    pts += xa * 3
    cs_prob = math.exp(-xgc) if xgc and xgc > 0 else 1.0
    cs_pts = {1: 4, 2: 4, 3: 1, 4: 0}.get(element_type, 0)
    if minutes >= 60:
        pts += cs_prob * cs_pts
    if element_type <= 2 and minutes >= 60:
        pts -= xgc / 2
    pts += (saves // 3)
    pts += penalties_saved * 5
    pts -= penalties_missed * 2
    pts -= own_goals * 2
    pts -= yellow_cards
    pts -= red_cards * 3
    pts += bonus
    pts += defcon_points(element_type, def_con)
    return pts


# ---------------------------------------------------------------------------
# Markdown renderer (original behaviour)
# ---------------------------------------------------------------------------

def _render_markdown(data: dict, output: Path) -> None:
    sections = [
        _md_header(data),
        _md_hall_of_fame(data),
        _md_hall_of_shame(data),
        _md_leaderboard(data),
        _md_gameweek_review(data),
        _md_transfer_roundup(data),
        _md_captains_corner(data),
        "---\n*Generated by fpl-scraper. Data: fantasy.premierleague.com*",
    ]
    output.write_text("\n\n".join(s for s in sections if s))


def _md_header(d: dict) -> str:
    diff_str = ""
    if d["fpl_avg"] is not None and d["gw_avg"] is not None:
        diff = d["gw_avg"] - d["fpl_avg"]
        diff_str = f" ({diff:+.0f} vs FPL average {d['fpl_avg']})"
    return dedent(f"""
        # The Dossier — Gameweek {d['event']}

        **Managers:** {d['mgr_count']}
        **League average this week:** {d['gw_avg']:.1f}{diff_str}
        **Generated:** {d['generated']}
    """).strip()


def _md_hall_of_fame(d: dict) -> str:
    parts = ["## 🏆 Hall of Fame"]
    if d["top_overall"]:
        parts.append("**Top of the Pile** — highest overall points")
        parts.append(_md_podium(d["top_overall"], "total_points"))
    if d["motw"]:
        parts.append("**Manager of the Week**")
        parts.append(_md_podium(d["motw"], "points"))
    if d["wheeler"]:
        parts.append("**Wheeler Dealer** — best transfer P&L this GW (net of hits)")
        parts.append(_md_podium_net(d["wheeler"]))
    if d["most_valuable"]:
        parts.append("**Sheikh Mansour** — most valuable squad")
        parts.append(_md_podium(d["most_valuable"], "value",
                                value_format="£{:.1f}m", scale=0.1))
    return "\n\n".join(parts)


def _md_hall_of_shame(d: dict) -> str:
    parts = ["## 📉 Hall of Shame"]
    if d["bottom_overall"]:
        parts.append("**Bottom Feeders** — fewest overall points")
        parts.append(_md_podium(d["bottom_overall"], "total_points"))
    if d["rogue"]:
        parts.append("**Rogue Trader** — worst transfer P&L this GW")
        parts.append(_md_podium_net(d["rogue"]))
    return "\n\n".join(parts)


def _md_leaderboard(d: dict) -> str:
    lines = ["## 📊 Leaderboard", "",
             "| # | Team | Manager | GW | Total | OR | TM | Hits | Bench | Value |",
             "|---|------|---------|----|----:|----:|---:|----:|------:|------:|"]
    for r in d["leaderboard"]:
        lines.append(
            f"| {r['pos']} | {r['entry_name']} | {r['player_name']} | "
            f"{r['points']} | {r['total_points']} | "
            f"{_fmt_rank(r['overall_rank'])} | "
            f"{r['event_transfers']} | {r['event_transfers_cost']} | "
            f"{r['points_on_bench']} | £{(r['value'] or 0) / 10:.1f}m |"
        )
    return "\n".join(lines)


def _md_gameweek_review(d: dict) -> str:
    parts = [f"## 🎯 GW{d['event']} Review"]
    if d["top_scorers"]:
        parts.append("**Top FPL scorers**")
        parts.append("\n".join(
            f"- **{r['web_name']}** ({r['team']}) — {r['event_points']} pts"
            for r in d["top_scorers"]
        ))
    if d["most_captained"]:
        parts.append("**Most captained in league**")
        total = d["total_mgrs"]
        parts.append("\n".join(
            f"- {r['web_name']} — {r['picks']} ({100 * r['picks'] / total:.0f}%)"
            for r in d["most_captained"]
        ))
    return "\n\n".join(parts)


def _md_transfer_roundup(d: dict) -> str:
    if not d["transfers"]:
        return "## 🔄 Transfer Roundup\n\nNo transfers this gameweek."
    lines = ["## 🔄 Transfer Roundup", "",
             "| Manager | In | Out | Gross | Hits | Net |",
             "|---------|----|----|------:|-----:|----:|"]
    for r in d["transfers"]:
        gross = r["gross"] or 0
        hits = r["hits"] or 0
        net = gross - hits
        lines.append(
            f"| {r['player_name']} | {r['bought']} | {r['sold']} | "
            f"{gross:+d} | {-hits} | {net:+d} |"
        )
    return "\n".join(lines)


def _md_captains_corner(d: dict) -> str:
    if not d["captains"]:
        return ""
    lines = ["## ©️ Captain's Corner", "",
             f"League average captain return: **{d['captain_avg']:.1f}** pts", "",
             "| Manager | Captain | Raw | Effective |",
             "|---------|---------|----:|----------:|"]
    for r in d["captains"]:
        lines.append(
            f"| {r['player_name']} | {r['captain_name']} | "
            f"{r['captain_raw_points']} | {r['captain_effective_points']} |"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# HTML renderer (Jinja2)
# ---------------------------------------------------------------------------

def _render_html(data: dict, output: Path) -> None:
    from jinja2 import Environment, FileSystemLoader

    env = Environment(loader=FileSystemLoader(str(TEMPLATES_DIR)), autoescape=True)
    env.filters["fmt_rank"] = _fmt_rank
    env.filters["fmt_value"] = lambda v: f"£{(v or 0) / 10:.1f}m"
    env.filters["sign"] = lambda v: f"{v:+d}" if v is not None else "-"
    env.filters["signf"] = lambda v: f"{v:+.0f}" if v is not None else "-"
    env.filters["commas"] = lambda v: f"{v:,}" if v is not None else "-"
    env.filters["net_pnl"] = lambda r: (r["gross_pnl"] or 0) - (r["event_transfers_cost"] or 0)
    env.filters["ordinal"] = _ordinal
    template = env.get_template("dossier.html")

    html_str = template.render(**data)
    output.write_text(html_str, encoding="utf-8")


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _ordinal(n: int) -> str:
    """1 → '1st', 2 → '2nd', etc."""
    if not isinstance(n, int):
        return str(n)
    s = str(n)
    if 11 <= (n % 100) <= 13:
        return s + "th"
    return s + {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")


def _rows(cursor) -> list[dict]:
    """Convert sqlite3.Row results to plain dicts."""
    return [dict(r) for r in cursor.fetchall()]


def _md_podium(rows, value_field, value_format="{}", scale=1.0) -> str:
    medals = ["🥇", "🥈", "🥉"]
    lines = []
    for i, r in enumerate(rows[:3]):
        val = r[value_field]
        if scale != 1.0 and val is not None:
            val = val * scale
        display = value_format.format(val) if val is not None else "-"
        lines.append(
            f"{medals[i]} **{r['entry_name']}** ({r['player_name']}) — {display}"
        )
    return "\n".join(lines)


def _md_podium_net(rows) -> str:
    medals = ["🥇", "🥈", "🥉"]
    lines = []
    for i, r in enumerate(rows[:3]):
        gross = r["gross_pnl"] or 0
        hits = r["event_transfers_cost"] or 0
        net = gross - hits
        lines.append(
            f"{medals[i]} **{r['entry_name']}** ({r['player_name']}) — "
            f"{net:+d} (gross {gross:+d}, hits {-hits})"
        )
    return "\n".join(lines)


def _fmt_rank(rank) -> str:
    if rank is None:
        return "-"
    if rank >= 1_000_000:
        return f"{rank / 1_000_000:.1f}m"
    if rank >= 1_000:
        return f"{rank / 1_000:.0f}k"
    return str(rank)
