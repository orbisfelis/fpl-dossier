"""Pre-season welcome screen — published as GW0 before a ball is kicked.

Mid-season the dossier runs on gameweek results. Before GW1 there are none, so
this builds the equivalent from the two things that DO exist: the archived
previous season (league history + per-player underlying numbers) and the new
season's bootstrap (fresh prices, fixtures, deadlines).

Everything here is deterministic — no LLM required — so it renders offline.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

POS_LABEL = {1: "GK", 2: "DEF", 3: "MID", 4: "FWD"}
GOAL_PTS = {1: 10, 2: 6, 3: 5, 4: 4}
# 2025/26 DefCon thresholds: DEF need 10+ CBIT, MID/FWD 12+ CBIRT, GK none.
DC_SQL = """(CASE WHEN p.element_type = 2
                   AND COALESCE(g.clearances_blocks_interceptions,0)
                     + COALESCE(g.tackles,0) >= 10 THEN 1
                  WHEN p.element_type IN (3,4)
                   AND COALESCE(g.clearances_blocks_interceptions,0)
                     + COALESCE(g.tackles,0)
                     + COALESCE(g.recoveries,0) >= 12 THEN 1
                  ELSE 0 END)"""


def _rows(cur) -> list[dict]:
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


# ---------------------------------------------------------------------------
# league history (previous season)
# ---------------------------------------------------------------------------

def _league_history(conn: sqlite3.Connection, league_id: int) -> dict:
    """Final table, the champion's margin, and the season's extremes.

    Note: manager_gameweeks.rank is unreliable in archived DBs, so every
    ordering here is by total_points.
    """
    final_ev = conn.execute(
        "SELECT MAX(event) FROM manager_gameweeks").fetchone()[0]
    if not final_ev:
        return {}

    table = _rows(conn.execute(
        """SELECT m.entry_id, m.entry_name, m.player_name, mg.total_points
           FROM manager_gameweeks mg JOIN managers m ON m.entry_id = mg.entry_id
           WHERE mg.event = ? AND m.league_id = ?
           ORDER BY mg.total_points DESC""", (final_ev, league_id)))
    for i, r in enumerate(table, 1):
        r["pos"] = i

    # Best and worst single gameweeks of the whole season.
    best_gw = _rows(conn.execute(
        """SELECT m.entry_name, mg.event, mg.points
           FROM manager_gameweeks mg JOIN managers m ON m.entry_id = mg.entry_id
           WHERE m.league_id = ? ORDER BY mg.points DESC LIMIT 1""", (league_id,)))
    worst_gw = _rows(conn.execute(
        """SELECT m.entry_name, mg.event, mg.points
           FROM manager_gameweeks mg JOIN managers m ON m.entry_id = mg.entry_id
           WHERE m.league_id = ? AND mg.points > 0
           ORDER BY mg.points ASC LIMIT 1""", (league_id,)))

    # Who led for the most weeks, and where they actually finished.
    leaders = _rows(conn.execute(
        """WITH ranked AS (
             SELECT mg.event, m.entry_id, m.entry_name, mg.total_points,
                    ROW_NUMBER() OVER (PARTITION BY mg.event
                                       ORDER BY mg.total_points DESC) rn
             FROM manager_gameweeks mg JOIN managers m ON m.entry_id = mg.entry_id
             WHERE m.league_id = ?)
           SELECT entry_id, entry_name, COUNT(*) weeks_top
           FROM ranked WHERE rn = 1
           GROUP BY entry_id ORDER BY weeks_top DESC""", (league_id,)))
    pos_by_id = {r["entry_id"]: r["pos"] for r in table}
    for lead in leaders:
        lead["final_pos"] = pos_by_id.get(lead["entry_id"])

    # Bench regret and hits taken across the season — pure banter fuel.
    bench = _rows(conn.execute(
        """SELECT m.entry_name, SUM(mg.points_on_bench) benched
           FROM manager_gameweeks mg JOIN managers m ON m.entry_id = mg.entry_id
           WHERE m.league_id = ? GROUP BY m.entry_id
           ORDER BY benched DESC LIMIT 1""", (league_id,)))
    hits = _rows(conn.execute(
        """SELECT m.entry_name, SUM(mg.event_transfers_cost) cost,
                  SUM(mg.event_transfers) moves
           FROM manager_gameweeks mg JOIN managers m ON m.entry_id = mg.entry_id
           WHERE m.league_id = ? GROUP BY m.entry_id
           ORDER BY cost DESC LIMIT 1""", (league_id,)))

    champ = table[0] if table else None
    runner = table[1] if len(table) > 1 else None
    bottler = next((lead for lead in leaders
                    if lead["weeks_top"] >= 5 and (lead["final_pos"] or 99) > 2), None)
    return {
        "final_event": final_ev,
        "table": table,
        "champion": champ,
        "runner_up": runner,
        "margin": (champ["total_points"] - runner["total_points"])
                  if champ and runner else None,
        "best_gw": best_gw[0] if best_gw else None,
        "worst_gw": worst_gw[0] if worst_gw else None,
        "leaders": leaders[:3],
        "bottler": bottler,
        "bench_king": bench[0] if bench else None,
        "hit_merchant": hits[0] if hits else None,
    }


# ---------------------------------------------------------------------------
# player intel (previous season underlying → this season's prices)
# ---------------------------------------------------------------------------

def _player_intel(conn: sqlite3.Connection, prev_db: Path, limit: int = 8) -> dict:
    """Join last season's per-90 numbers onto this season's prices by name."""
    conn.execute("ATTACH DATABASE ? AS prev", (str(prev_db),))
    try:
        base = f"""
          WITH hist AS (
            SELECT p.web_name, p.first_name, p.element_type,
                   SUM(g.minutes) mins, SUM(g.total_points) pts,
                   SUM(g.goals_scored) goals, SUM(g.assists) assists,
                   SUM(g.expected_goals) xg, SUM(g.expected_assists) xa,
                   SUM(g.bonus) bonus, COUNT(*) apps,
                   SUM({DC_SQL}) dc_hits
            FROM prev.player_gameweeks g
            JOIN prev.players p ON p.id = g.element
            WHERE g.minutes > 0
            GROUP BY g.element),
          joined AS (
            SELECT n.web_name, n.element_type, n.now_cost/10.0 price,
                   n.selected_by_percent own, t.short_name club,
                   h.mins, h.pts, h.goals, h.assists, h.xg, h.xa, h.bonus,
                   h.apps, h.dc_hits
            FROM players n
            JOIN teams t ON t.id = n.team_id
            JOIN hist h ON h.web_name = n.web_name AND h.first_name = n.first_name
            WHERE n.status = 'a')
        """

        # DefCon kings — the 25/26 scoring change, still the market's blind spot.
        defcon = _rows(conn.execute(base + f"""
            SELECT web_name, club, price, own, element_type, apps, dc_hits,
                   2 * dc_hits dc_pts,
                   ROUND(100.0 * dc_hits / apps) hit_rate, pts
            FROM joined WHERE apps >= 20 AND element_type IN (2,3,4)
            ORDER BY dc_pts DESC, hit_rate DESC LIMIT {limit}"""))

        # Finishing luck: goals well clear of xG regress. These are the traps.
        traps = _rows(conn.execute(base + f"""
            SELECT web_name, club, price, own, element_type, pts, goals,
                   ROUND(xg, 1) xg, ROUND(goals - xg, 1) over_xg
            FROM joined
            WHERE mins >= 1200 AND xg >= 3 AND goals - xg >= 2.5
            ORDER BY over_xg DESC LIMIT {limit}"""))

        # The mirror image: big underlying numbers, unlucky returns.
        bargains = _rows(conn.execute(base + f"""
            SELECT web_name, club, price, own, element_type, pts, goals, xg,
                   assists, xa, goals + assists ga,
                   ROUND(xg + xa, 1) xga,
                   ROUND((xg + xa) - (goals + assists), 1) under_x
            FROM joined
            WHERE mins >= 1200 AND (xg + xa) - (goals + assists) >= 1.5
            ORDER BY under_x DESC LIMIT {limit}"""))

        # Never injured, never rotated, never dropped.
        ever_present = _rows(conn.execute(base + f"""
            SELECT web_name, club, price, own, element_type, mins, apps, pts
            FROM joined WHERE mins >= 3000
            ORDER BY mins DESC LIMIT {limit}"""))

        # Points per million at THIS season's price.
        value = _rows(conn.execute(base + f"""
            SELECT web_name, club, price, own, element_type, pts, mins,
                   ROUND(pts / price, 1) ppm
            FROM joined WHERE mins >= 1500 AND price <= 7.0
            ORDER BY ppm DESC LIMIT {limit}"""))

        # Priced up hardest after a big season — must repeat to justify it.
        premium = _rows(conn.execute(base + f"""
            SELECT web_name, club, price, own, element_type, pts, goals, assists
            FROM joined WHERE price >= 8.0
            ORDER BY price DESC, pts DESC LIMIT {limit}"""))
    finally:
        conn.execute("DETACH DATABASE prev")

    for group in (defcon, traps, bargains, ever_present, value, premium):
        for r in group:
            r["pos"] = POS_LABEL.get(r.get("element_type"), "")
    return {"defcon": defcon, "traps": traps, "bargains": bargains,
            "ever_present": ever_present, "value": value, "premium": premium}


# ---------------------------------------------------------------------------
# manager form book (multi-season history)
# ---------------------------------------------------------------------------

def _form_book(conn: sqlite3.Connection, league_id: int,
               prev_names: set[str],
               returning_ids: set[int] | None = None) -> list[dict]:
    """Every manager's FPL career, from /entry/<id>/history/ `past`.

    A manager is returning if the identity registry links their entry to a
    previous season (authoritative, survives renames), or failing that if their
    name appears in the archive. Raw entry_id cannot be compared directly: it
    changes at the rollover, so an id diff would flag everyone as new annually.
    """
    from .report import manager_key
    managers = _rows(conn.execute(
        """SELECT entry_id, entry_name, player_name FROM managers
           WHERE league_id = ? ORDER BY entry_name""", (league_id,)))
    if not managers:
        return []

    history = _rows(conn.execute(
        """SELECT entry_id, season_name, total_points, rank, rank_percentage
           FROM manager_past_seasons ORDER BY entry_id, season_name"""))
    by_entry: dict[int, list[dict]] = {}
    for row in history:
        by_entry.setdefault(row["entry_id"], []).append(row)

    out = []
    for m in managers:
        # prev_names empty (no archive) => nobody can be judged new
        if returning_ids:
            is_new = m["entry_id"] not in returning_ids
        else:
            is_new = (bool(prev_names)
                      and manager_key(m["player_name"]) not in prev_names)
        seasons = by_entry.get(m["entry_id"], [])
        scored = [s for s in seasons if s["total_points"]]
        pcts = [s["rank_percentage"] for s in seasons
                if s["rank_percentage"] is not None]
        best = max(scored, key=lambda s: s["total_points"]) if scored else None
        # Best finish = lowest percentile (top N%).
        best_pct = min(pcts) if pcts else None
        recent = sorted(scored, key=lambda s: s["season_name"])[-3:]
        out.append({
            "entry_id": m["entry_id"],
            "entry_name": m["entry_name"],
            "player_name": m["player_name"],
            "is_new": is_new,
            "new_label": "NEW" if is_new else "",
            "seasons": len(seasons),
            "best_points": best["total_points"] if best else None,
            "best_season": best["season_name"] if best else None,
            "best_pct": best_pct,
            "avg_points": round(sum(s["total_points"] for s in scored) / len(scored))
                          if scored else None,
            "last_points": recent[-1]["total_points"] if recent else None,
            "last_season": recent[-1]["season_name"] if recent else None,
            "recent": " · ".join(f"{s['season_name'][-2:]}: {s['total_points']}"
                                 for s in recent),
        })
    # Strongest career first; managers with no history sink to the bottom.
    out.sort(key=lambda r: (r["best_points"] is None, -(r["best_points"] or 0)))
    return out


# ---------------------------------------------------------------------------
# fixtures + market (new season)
# ---------------------------------------------------------------------------

def _fixture_intel(conn: sqlite3.Connection, span: int = 5) -> dict:
    runs = _rows(conn.execute(
        """SELECT t.short_name club,
                  ROUND(AVG(CASE WHEN f.team_h = t.id THEN f.team_h_difficulty
                                 ELSE f.team_a_difficulty END), 2) fdr
           FROM teams t
           JOIN fixtures f ON (f.team_h = t.id OR f.team_a = t.id) AND f.event <= ?
           GROUP BY t.id ORDER BY fdr""", (span,)))
    gw1 = _rows(conn.execute(
        """SELECT th.short_name home, ta.short_name away,
                  f.team_h_difficulty hfdr, f.team_a_difficulty afdr, f.kickoff_time
           FROM fixtures f
           JOIN teams th ON th.id = f.team_h JOIN teams ta ON ta.id = f.team_a
           WHERE f.event = 1 ORDER BY f.kickoff_time"""))
    return {"span": span, "easiest": runs[:5], "hardest": runs[-5:][::-1],
            "gw1": gw1}


def _market(conn: sqlite3.Connection) -> dict:
    priciest = _rows(conn.execute(
        """SELECT p.web_name, t.short_name club, p.now_cost/10.0 price,
                  p.element_type, p.selected_by_percent own, p.total_points pts
           FROM players p JOIN teams t ON t.id = p.team_id
           WHERE p.status = 'a' ORDER BY p.now_cost DESC LIMIT 6"""))
    owned = _rows(conn.execute(
        """SELECT p.web_name, t.short_name club, p.now_cost/10.0 price,
                  p.element_type, p.selected_by_percent own
           FROM players p JOIN teams t ON t.id = p.team_id
           ORDER BY p.selected_by_percent DESC LIMIT 10"""))
    for group in (priciest, owned):
        for r in group:
            r["pos"] = POS_LABEL.get(r["element_type"], "")
    return {"priciest": priciest, "most_owned": owned}


# ---------------------------------------------------------------------------
# predictions — the bit built to be argued about
# ---------------------------------------------------------------------------

def _predictions(history: dict, intel: dict, fixtures: dict,
                 market: dict) -> list[dict]:
    """Falsifiable, data-derived calls. Each one names a number so the league
    can hold it against the dossier come May."""
    out: list[dict] = []

    champ = history.get("champion")
    table = history.get("table") or []
    if champ:
        out.append({
            "tag": "The Title",
            "text": f"{champ['entry_name']} does NOT retain it. Reigning champions "
                    f"have the biggest target and the worst luck — "
                    f"{champ['player_name']} finishes outside the top two.",
            "subject": champ["entry_name"],
            "basis": f"won 25/26 on {champ['total_points']} pts"})
    if len(table) >= 3:
        dark = table[2]
        gap = table[0]["total_points"] - dark["total_points"]
        out.append({
            "tag": "The Dark Horse",
            "text": f"{dark['entry_name']} wins the whole thing. Third last "
                    f"season, {gap} points off the title — one good captaincy "
                    f"run is worth more than that.",
            "subject": dark["entry_name"],
            "basis": f"3rd in 25/26 on {dark['total_points']} pts"})

    bottler = history.get("bottler")
    if bottler:
        out.append({
            "tag": "The Bottle Watch",
            "text": f"{bottler['entry_name']} leads the league again at some point "
                    f"— and blows it again. Led for {bottler['weeks_top']} weeks "
                    f"last season and still finished {bottler['final_pos']}th.",
            "subject": bottler["entry_name"],
            "basis": f"{bottler['weeks_top']} weeks top, finished "
                     f"{bottler['final_pos']}th"})

    if intel["defcon"]:
        d = intel["defcon"][0]
        out.append({
            "tag": "The DefCon King",
            "text": f"{d['web_name']} ({d['club']}, £{d['price']}m) is the most "
                    f"underpriced asset in the game. {int(d['dc_pts'])} DefCon "
                    f"points last season at a {int(d['hit_rate'])}% hit rate, and "
                    f"only {d['own']}% of the game owns him.",
            "basis": f"{d['dc_hits']}/{d['apps']} DefCon games"})

    if intel["traps"]:
        t = intel["traps"][0]
        out.append({
            "tag": "The Trap",
            "text": f"{t['web_name']} ({t['club']}, £{t['price']}m) is this "
                    f"season's great disappointment. Beat his xG by "
                    f"{t['over_xg']} goals last season — finishing that hot does "
                    f"not repeat, and {t['own']}% of you are about to find out.",
            "basis": f"{t['goals']} goals from {round(t['xg'], 1)} xG"})

    if intel["bargains"]:
        b = intel["bargains"][0]
        out.append({
            "tag": "The Bounce-Back",
            "text": f"{b['web_name']} ({b['club']}, £{b['price']}m) outscores his "
                    f"price bracket. Underperformed his expected numbers by "
                    f"{b['under_x']} last season — that is variance, not decline.",
            "basis": f"{b['goals']}G+{b['assists']}A from "
                     f"{round(b['xg'] + b['xa'], 1)} expected"})

    if fixtures["easiest"]:
        e = fixtures["easiest"][0]
        out.append({
            "tag": "The Fast Start",
            "text": f"Whoever loads up on {e['club']} assets wins August. Easiest "
                    f"opening {fixtures['span']} fixtures in the league "
                    f"(avg difficulty {e['fdr']}).",
            "basis": f"{e['club']} FDR {e['fdr']} over GW1-{fixtures['span']}"})

    if market["most_owned"]:
        m = market["most_owned"][0]
        out.append({
            "tag": "The Template",
            "text": f"{m['web_name']} at {m['own']}% ownership is the single "
                    f"biggest rank swing of the season. Own him and you tread "
                    f"water; fade him and you either win the league or lose it.",
            "basis": f"{m['own']}% owned at £{m['price']}m"})

    # Don't pile two predictions onto the same manager — spread the abuse.
    named = {p.get("subject") for p in out}
    bench = history.get("bench_king")
    if bench and bench.get("benched") and bench["entry_name"] not in named:
        out.append({
            "tag": "The Bench Curse",
            "text": f"{bench['entry_name']} leaves another {int(bench['benched'] * 0.9)}+ "
                    f"points on the bench. Last season's {int(bench['benched'])} "
                    f"was not bad luck, it was a personality trait.",
            "subject": bench["entry_name"],
            "basis": f"{int(bench['benched'])} points benched in 25/26"})

    hits = history.get("hit_merchant")
    if hits and hits.get("cost") and hits["entry_name"] not in named:
        out.append({
            "tag": "The Hit Merchant",
            "text": f"{hits['entry_name']} takes at least {int(hits['cost'])} points "
                    f"of hits again and still finishes below the manager who "
                    f"took none. {int(hits['moves'])} transfers last season says "
                    f"the itch is incurable.",
            "subject": hits["entry_name"],
            "basis": f"-{int(hits['cost'])} pts on hits across "
                     f"{int(hits['moves'])} transfers"})

    worst = history.get("worst_gw")
    if worst:
        out.append({
            "tag": "The Floor",
            "text": f"Someone in this league scores under 20 in a gameweek before "
                    f"Christmas. {worst['entry_name']} managed {worst['points']} "
                    f"in GW{worst['event']} last season and the bar has never "
                    f"been lower.",
            "basis": f"{worst['points']} pts in GW{worst['event']}, 25/26"})

    return out


def whatsapp_text(data: dict) -> str:
    """Plain-text digest sized for a WhatsApp paste. *bold* is WhatsApp markup."""
    lines = [f"*THE DOSSIER — {data['season_label']} PRE-SEASON*",
             f"_{data['league_name']}_", ""]
    d = data.get("deadline_human")
    if d:
        lines += [f"⏰ GW1 deadline: {d}", ""]

    h = data.get("history") or {}
    if h.get("champion"):
        c = h["champion"]
        lines += [f"🏆 Defending champion: {c['entry_name']} ({c['total_points']} pts)"]
        if h.get("bottler"):
            b = h["bottler"]
            lines += [f"🪣 Last season's bottle job: {b['entry_name']} — "
                      f"{b['weeks_top']} weeks top, finished {b['final_pos']}th"]
        lines.append("")

    lines.append("*PREDICTIONS — screenshot these and hold me to them*")
    for i, p in enumerate(data["predictions"], 1):
        lines.append(f"{i}. [{p['tag']}] {p['text']}")
    lines.append("")

    if data["intel"]["defcon"]:
        lines.append("*DEFCON KINGS (25/26)*")
        for r in data["intel"]["defcon"][:5]:
            lines.append(f"• {r['web_name']} ({r['club']}, £{r['price']}m) — "
                         f"{int(r['dc_pts'])} pts, {int(r['hit_rate'])}% of games")
        lines.append("")

    if data["fixtures"]["easiest"]:
        best = ", ".join(f"{r['club']} ({r['fdr']})"
                         for r in data["fixtures"]["easiest"][:3])
        lines.append(f"*EASIEST GW1-{data['fixtures']['span']}:* {best}")
    lines.append("")
    lines.append("Full dossier: " + data.get("public_url", ""))
    return "\n".join(lines).strip()


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------

def collect_preseason(db_path: Path, league_id: int, prev_db: Path | None,
                      season: str, public_url: str = "") -> dict:
    conn = sqlite3.connect(db_path)
    try:
        gw1 = conn.execute(
            "SELECT name, deadline_time FROM gameweeks WHERE id = 1").fetchone()
        league = conn.execute(
            "SELECT name FROM leagues WHERE id = ?", (league_id,)).fetchone()
        fixtures = _fixture_intel(conn)
        market = _market(conn)
        intel = {"defcon": [], "traps": [], "bargains": [],
                 "ever_present": [], "value": [], "premium": []}
        if prev_db and Path(prev_db).exists():
            intel = _player_intel(conn, Path(prev_db))
        n_players = conn.execute(
            "SELECT COUNT(*) FROM players WHERE status='a'").fetchone()[0]
        managers_present = conn.execute(
            "SELECT COUNT(*) FROM managers WHERE league_id = ?",
            (league_id,)).fetchone()[0]
    finally:
        conn.close()

    history: dict = {}
    prev_names: set[str] = set()
    league_name = league[0] if league else f"League {league_id}"
    if prev_db and Path(prev_db).exists():
        pconn = sqlite3.connect(prev_db)
        try:
            from .report import manager_key, resolve_prev_league
            # The mini-league is re-created each year under a new id, so the
            # archive is keyed under last season's number, not this one's.
            prev_league = resolve_prev_league(pconn, league_id) or league_id
            history = _league_history(pconn, prev_league)
            prev_names = {manager_key(r[0]) for r in pconn.execute(
                "SELECT player_name FROM managers WHERE league_id = ?",
                (prev_league,))}
            if not league_name or league_name.startswith("League "):
                row = pconn.execute("SELECT name FROM leagues WHERE id = ?",
                                    (league_id,)).fetchone()
                if row:
                    league_name = row[0]
        finally:
            pconn.close()

    deadline = gw1[1] if gw1 else None
    deadline_dt = None
    days_left = None
    if deadline:
        deadline_dt = datetime.fromisoformat(deadline.replace("Z", "+00:00"))
        days_left = (deadline_dt - datetime.now(timezone.utc)).days

    # Needs prev_names, so it runs after the archive has been read.
    form_book: list[dict] = []
    if managers_present:
        returning_ids: set[int] = set()
        try:
            import os
            from .registry import (DEFAULT_REGISTRY, open_registry,
                                   prev_entry_translation)
            reg_path = Path(os.environ.get("FPL_REGISTRY", DEFAULT_REGISTRY))
            if reg_path.exists():
                reg = open_registry(reg_path)
                try:
                    returning_ids = set(
                        prev_entry_translation(reg, league_id).values())
                finally:
                    reg.close()
        except (sqlite3.Error, ImportError, OSError):
            returning_ids = set()

        fconn = sqlite3.connect(db_path)
        try:
            form_book = _form_book(fconn, league_id, prev_names, returning_ids)
        finally:
            fconn.close()

    data = {
        "league_name": league_name,
        "league_id": league_id,
        "season": season,
        "season_label": "20" + season.replace("-", "/20"),
        "deadline": deadline,
        "deadline_human": deadline_dt.strftime("%a %d %b %Y, %H:%M UTC")
                          if deadline_dt else None,
        "days_left": days_left,
        "player_count": n_players,
        "history": history,
        "form_book": form_book,
        "new_joiners": [m for m in form_book if m["is_new"]],
        "intel": intel,
        "fixtures": fixtures,
        "market": market,
        "public_url": public_url,
        "generated": datetime.now(timezone.utc).strftime("%d %b %Y, %H:%M UTC"),
    }
    data["predictions"] = _predictions(history, intel, fixtures, market)
    data["whatsapp"] = whatsapp_text(data)
    return data


def render_preseason(data: dict, output: Path) -> Path:
    from jinja2 import Environment, FileSystemLoader

    templates = Path(__file__).parent / "templates"
    env = Environment(loader=FileSystemLoader(str(templates)), autoescape=True)
    html = env.get_template("preseason.html").render(**data)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(html, encoding="utf-8")
    log.info("Pre-season page → %s", output)
    return output


def generate_preseason(db_path: Path, output: Path, league_id: int,
                       prev_db: Path | None = None, season: str | None = None,
                       public_url: str = "") -> Path:
    from .report import current_season

    season = season or current_season()
    data = collect_preseason(db_path, league_id, prev_db, season, public_url)
    data["prev_season_label"] = _prev_label(season)
    return render_preseason(data, output)


def _prev_label(season: str) -> str:
    """'26-27' -> '2025/26' (the season the intel comes from)."""
    try:
        start = int(season.split("-")[0])
        return f"20{start - 1:02d}/{start:02d}"
    except (ValueError, IndexError):
        return "last season"


def publish_preseason(db_path: Path, league_id: int, docs_dir: Path,
                      prev_db: Path | None = None, season: str | None = None,
                      public_url: str = "") -> Path:
    """Render the pre-season page as ``docs/<season>/GW0.html`` so it slots into
    the existing manifest and season/gameweek picker."""
    from .report import build_manifest, current_season, _write_index

    season = season or current_season()
    season_dir = docs_dir / season
    out = season_dir / "GW0.html"
    generate_preseason(db_path, out, league_id, prev_db, season, public_url)

    manifest = build_manifest(docs_dir)
    (docs_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8")
    _write_index(docs_dir, manifest)
    return out
