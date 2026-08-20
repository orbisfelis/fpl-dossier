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
import math
import sqlite3
import statistics
from datetime import datetime, timezone
from pathlib import Path

from .db import active_clause

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


def _pct_label(pct: float | None) -> str | None:
    """Format a percentile, keeping resolution on the very small ones.

    Above 1% whole numbers are plenty. Below that the interesting finishes all
    live in the same two decimal places — a 704th and a 5,677th are wildly
    different seasons that both round to "0.1%" — so small values get two
    significant figures instead of a flat '<0.1%'.
    """
    if pct is None:
        return None
    if pct >= 1:
        return f"{pct:.0f}%"
    if pct >= 0.1:
        return f"{pct:.1f}%"
    if pct > 0:
        # Two significant figures: 0.00857 -> 0.0086, 0.062 -> 0.062.
        decimals = 1 - math.floor(math.log10(pct))
        return f"{pct:.{min(decimals, 6)}f}%"
    return "<0.1%"


def _season_sizes(conn: sqlite3.Connection) -> dict[str, float]:
    """Estimate how many people played each past season.

    FPL reports `rank_percentage` already rounded — one decimal below 1%, whole
    numbers above it — so every elite finish flattens to "0.1%" or "0.0" and the
    archive can no longer say how good it was. The rank itself is exact, so the
    only missing piece is the size of the field.

    Each (rank, reported pct) pair brackets that size: the true percentage sits
    within half a printed step of what was printed, so the field must lie in
    [rank*100/(p+half), rank*100/(p-half)]. Intersecting the brackets for a
    season pins it to about 1%, which supports the two significant figures
    `_pct_label` prints and no more.

    Only mid-table finishes are used. A rounded 2% carries 25% relative error
    while a rounded 40% carries barely 1%, and one stray low-percentage entry
    is enough to drag the intersection empty — which is exactly what a single
    manager did to 2025/26. Prefer the reliable tail, widen the net only if a
    season is too small to fill it, and fall back to the median where the
    brackets still refuse to agree.
    """
    rows = conn.execute(
        """SELECT season_name, rank, rank_percentage FROM manager_past_seasons
           WHERE rank > 0 AND rank_percentage IS NOT NULL""").fetchall()
    by: dict[str, list[tuple[int, float]]] = {}
    for season, rank, pct in rows:
        by.setdefault(season, []).append((rank, pct))

    sizes: dict[str, float] = {}
    for season, obs in by.items():
        for threshold in (10, 3, 1):
            sample = [(r, p) for r, p in obs if p >= threshold]
            if len(sample) >= 3:
                break
        else:
            sample = [(r, p) for r, p in obs if p > 0]
        if not sample:
            continue

        lo, hi = 0.0, float("inf")
        for rank, pct in sample:
            half = (0.1 if pct < 1 else 1.0) / 2
            lo = max(lo, rank * 100.0 / (pct + half))
            hi = min(hi, rank * 100.0 / (pct - half))
        sizes[season] = ((lo + hi) / 2 if lo <= hi else
                         statistics.median(r * 100.0 / p for r, p in sample))
    return sizes


def _precise_pct(rank: int | None, season: str | None,
                 sizes: dict[str, float], fallback: float | None) -> float | None:
    """Percentile from the exact rank, but only where rounding lost something.

    At 1% and above FPL's own figure is fine — it is computed from their real
    denominator, and substituting an estimate there just picks fights at the
    boundaries (a reported 2% re-deriving to 1.499% and printing as "1%").
    Below 1% the rounding is coarse enough to erase the whole story, so an
    estimated field size beats a number that has already been flattened.
    """
    if fallback is not None and fallback >= 1:
        return fallback
    if rank and season and sizes.get(season):
        return 100.0 * rank / sizes[season]
    return fallback


def _form_pct(recent: list[dict], sizes: dict[str, float]) -> float | None:
    """Recent form as a single percentile: the last three seasons, weighted
    towards the most recent.

    Percentile rather than points, because scoring inflation makes a 2500 from
    a decade ago and a 2500 from last year incomparable. Weighted 3-2-1 rather
    than a flat mean, because one catastrophic season otherwise outweighs two
    good ones and the number stops describing form — a manager who went 90th
    percentile, then 10th, then 3rd is clearly on the way up, and a mean would
    still rank them near the bottom.

    Managers with one or two seasons are scored on what they have; the weights
    slide so their most recent season still counts heaviest.

    Unlike the displayed percentiles this always derives from the exact rank,
    including above 1%. `_precise_pct` defers to FPL's own figure up there to
    avoid contradicting a number they publish, but averaging their integers
    would quantise the whole column — a 1.16% season and a 1.47% season both
    arriving as "1" is fine to print and useless to sort on.
    """
    pcts = []
    for s in recent:
        rank, season = s.get("rank"), s.get("season_name")
        if rank and sizes.get(season):
            pcts.append(100.0 * rank / sizes[season])
        elif s.get("rank_percentage") is not None:
            pcts.append(s["rank_percentage"])
    if not pcts:
        return None
    weights = [1, 2, 3][-len(pcts):]
    return sum(p * w for p, w in zip(pcts, weights)) / sum(weights)


def _form_label(pct: float | None) -> str | None:
    """Form needs a decimal where `_pct_label` would not bother: it is the
    column the table sorts on, so neighbouring rows have to be tellable apart."""
    if pct is None:
        return None
    return f"{pct:.1f}%" if pct < 10 else f"{pct:.0f}%"


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
        """SELECT m.entry_id, m.entry_name, mg.event, mg.points
           FROM manager_gameweeks mg JOIN managers m ON m.entry_id = mg.entry_id
           WHERE m.league_id = ? ORDER BY mg.points DESC LIMIT 1""", (league_id,)))
    worst_gw = _rows(conn.execute(
        """SELECT m.entry_id, m.entry_name, mg.event, mg.points
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
        """SELECT m.entry_id, m.entry_name, SUM(mg.points_on_bench) benched
           FROM manager_gameweeks mg JOIN managers m ON m.entry_id = mg.entry_id
           WHERE m.league_id = ? GROUP BY m.entry_id
           ORDER BY benched DESC LIMIT 1""", (league_id,)))
    hits = _rows(conn.execute(
        """SELECT m.entry_id, m.entry_name, SUM(mg.event_transfers_cost) cost,
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
# identity — last season's records, this season's names
# ---------------------------------------------------------------------------

def _identity_map(db_path: Path, league_id: int) -> dict[int, dict]:
    """{previous season entry_id -> this season's team/manager}.

    Predictions are built from last season's records but read by people looking
    at this season's league, where 11 of 17 returning managers have renamed
    their team. Anyone absent from this map has left and should not be
    referenced at all.
    """
    import os
    try:
        from .registry import (DEFAULT_REGISTRY, open_registry,
                               prev_entry_translation)
        reg_path = Path(os.environ.get("FPL_REGISTRY", DEFAULT_REGISTRY))
        if not reg_path.exists():
            return {}
        reg = open_registry(reg_path)
        try:
            translation = prev_entry_translation(reg, league_id)
        finally:
            reg.close()
    except (sqlite3.Error, ImportError, OSError):
        return {}
    if not translation:
        return {}

    conn = sqlite3.connect(db_path)
    try:
        current = {r[0]: {"team": r[1], "manager": r[2]} for r in conn.execute(
            "SELECT entry_id, entry_name, player_name FROM managers "
            f"WHERE league_id = ?{active_clause(conn)}", (league_id,))}
    finally:
        conn.close()
    return {prev: current[cur] for prev, cur in translation.items()
            if cur in current}


def _apply_identity(history: dict, id_map: dict[int, dict]) -> None:
    """Rewrite last season's records to this season's names, and mark anyone
    who is no longer in the league so predictions can skip them."""
    if not id_map:
        return

    def tag(rec: dict | None) -> None:
        if not rec or "entry_id" not in rec:
            return
        now = id_map.get(rec["entry_id"])
        rec["playing"] = now is not None
        if now:
            rec["then_name"] = rec.get("entry_name")
            rec["entry_name"] = now["team"]
            rec["player_name"] = now["manager"]

    for rec in history.get("table") or []:
        tag(rec)
    for key in ("champion", "runner_up", "best_gw", "worst_gw",
                "bench_king", "hit_merchant", "bottler"):
        tag(history.get(key))
    for lead in history.get("leaders") or []:
        tag(lead)


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
        f"""SELECT entry_id, entry_name, player_name FROM managers
           WHERE league_id = ?{active_clause(conn)}
           ORDER BY entry_name""", (league_id,)))
    if not managers:
        return []

    history = _rows(conn.execute(
        """SELECT entry_id, season_name, total_points, rank, rank_percentage
           FROM manager_past_seasons ORDER BY entry_id, season_name"""))
    by_entry: dict[int, list[dict]] = {}
    for row in history:
        by_entry.setdefault(row["entry_id"], []).append(row)
    sizes = _season_sizes(conn)

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
        # Recover the resolution FPL rounded away before taking any minimum:
        # picking the smallest of the printed values would tie every elite
        # season at "0.0" and then choose between them arbitrarily.
        pcts = [p for p in (_precise_pct(s.get("rank"), s.get("season_name"),
                                         sizes, s.get("rank_percentage"))
                            for s in seasons) if p is not None]
        best = max(scored, key=lambda s: s["total_points"]) if scored else None
        # Best finish is the lowest overall rank, which is not necessarily the
        # season with the most points — scoring rules move between years.
        ranked = [s for s in seasons if s.get("rank")]
        best_rank = min(ranked, key=lambda s: s["rank"]) if ranked else None
        best_pct = min(pcts) if pcts else None
        best_rank_pct = _precise_pct(
            best_rank["rank"], best_rank["season_name"], sizes,
            best_rank["rank_percentage"]) if best_rank else None
        recent = sorted(scored, key=lambda s: s["season_name"])[-3:]
        form_pct = _form_pct(recent, sizes)
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
            "pct_label": _pct_label(best_pct),
            "best_rank": best_rank["rank"] if best_rank else None,
            "best_rank_label": f"{best_rank['rank']:,}" if best_rank else None,
            "best_rank_season": best_rank["season_name"] if best_rank else None,
            "best_rank_points": best_rank["total_points"] if best_rank else None,
            "best_rank_pct": _pct_label(best_rank_pct) if best_rank else None,
            "avg_points": round(sum(s["total_points"] for s in scored) / len(scored))
                          if scored else None,
            "last_points": recent[-1]["total_points"] if recent else None,
            "last_season": recent[-1]["season_name"] if recent else None,
            "recent": " · ".join(f"{s['season_name'][-2:]}: {s['total_points']}"
                                 for s in recent),
            "form_pct": form_pct,
            "form_label": _form_label(form_pct),
        })
    # In form order — this is the Form Book, so the most recent three seasons
    # decide it, not a career peak that might be a decade old. Career best
    # breaks ties (two managers share a 2647), and anyone with no ranked
    # season to their name sinks to the bottom.
    out.sort(key=lambda r: (r["form_pct"] is None, r["form_pct"] or 0,
                            -(r["best_points"] or 0)))
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
# ones to watch — players, and the rivals across the table
# ---------------------------------------------------------------------------

def _watchlist(intel: dict, market: dict, fixtures: dict,
               form_book: list[dict]) -> dict:
    """Named individuals with a one-line reason, drawn from the same data as
    everything else. Players first, then the managers to actually fear."""
    best_fdr = {r["club"]: r["fdr"] for r in fixtures.get("easiest", [])}
    players: list[dict] = []

    def add(rows, tag, reason, limit=2, **kw):
        for r in rows[:limit]:
            players.append({"name": r["web_name"], "club": r["club"],
                            "price": r["price"], "own": r["own"],
                            "pos": r.get("pos", ""), "tag": tag,
                            "reason": reason(r), **kw})

    add(intel.get("defcon") or [], "DefCon engine",
        lambda r: (f"{int(r['dc_pts'])} defensive-contribution points last season "
                   f"at a {int(r['hit_rate'])}% hit rate. The floor nobody prices in."))
    add(intel.get("bargains") or [], "Due a correction",
        lambda r: (f"Underperformed his expected numbers by {r['under_x']} — "
                   f"{r['ga']} returns from {r['xga']} expected. The chances are "
                   f"already being created."))
    add(intel.get("value") or [], "Budget enabler",
        lambda r: (f"{r['ppm']} points per £m at this price. The kind of pick "
                   f"that funds a premium elsewhere."))
    # Cheap starters at a club with a kind opening run.
    fast = [r for r in (intel.get("value") or [])
            if r["club"] in best_fdr and r["price"] <= 6.0]
    add(fast, "Fast starter",
        lambda r: (f"{r['club']} have one of the kindest opening runs "
                   f"(difficulty {best_fdr[r['club']]} over the first "
                   f"{fixtures['span']}), and he is only £{r['price']}m."), limit=2)
    add(intel.get("traps") or [], "Handle with care",
        lambda r: (f"Beat his xG by {r['over_xg']} last season — "
                   f"{r['goals']} goals from {r['xg']} expected. Owned by "
                   f"{r['own']}% who are counting on it happening twice."),
        limit=2, caution=True)

    # De-duplicate: a player earns one billing, the first one he qualified for.
    seen: set[str] = set()
    unique = []
    for p in players:
        key = f"{p['name']}|{p['club']}"
        if key not in seen:
            seen.add(key)
            unique.append(p)

    # Rivals: best career percentile among managers actually in the league.
    rivals = []
    ranked = [x for x in form_book if x.get("best_rank")]
    for m in sorted(ranked, key=lambda x: x["best_rank"])[:4]:
        rivals.append({
            "team": m["entry_name"], "manager": m["player_name"],
            "seasons": m["seasons"], "best_points": m["best_points"],
            "best_season": m["best_season"], "best_pct": m["best_pct"],
            "best_rank": m["best_rank"], "best_rank_label": m["best_rank_label"],
            "best_rank_season": m["best_rank_season"],
            "best_rank_points": m["best_rank_points"],
            "best_rank_pct": m["best_rank_pct"],
            "pct_label": m["best_rank_pct"],
            "is_new": m["is_new"],
            "reason": (f"Finished {m['best_rank_label']} in the world in "
                       f"{m['best_rank_season']} — the top {m['best_rank_pct']} "
                       f"of everyone playing — on {m['best_rank_points']} points"
                       + (f", across {m['seasons']} seasons" if m["seasons"] > 4 else "")
                       + (". And nobody here has seen him play." if m["is_new"] else ".")),
        })
    # Cautions are the point of a watchlist, so never let them fall off.
    keep = [p for p in unique if not p.get("caution")][:6]
    keep += [p for p in unique if p.get("caution")][:2]
    return {"players": keep, "rivals": rivals}


# ---------------------------------------------------------------------------
# predictions — the bit built to be argued about
# ---------------------------------------------------------------------------

def _predictions(history: dict, intel: dict, fixtures: dict,
                 market: dict, form_book: list[dict] | None = None) -> list[dict]:
    """Falsifiable, data-derived calls. Each one names a number so the league
    can hold it against the dossier come May."""
    out: list[dict] = []
    form_book = form_book or []

    # Only reference managers who are actually in the league this season.
    def here(rec: dict | None) -> bool:
        return bool(rec) and rec.get("playing", True)

    table = [r for r in (history.get("table") or []) if here(r)]
    champ = history.get("champion") if here(history.get("champion")) else None
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

    # The title race tightens: a 64-point cushion is a lot of nothing going
    # wrong, and it rarely goes that smoothly twice.
    if history.get("margin"):
        margin = history["margin"]
        out.append({
            "tag": "The Margin",
            "text": f"The title is decided by fewer than {margin} points. Last "
                    f"season's cushion flattered a race that was live into "
                    f"April — nobody gets a clear run like that two years "
                    f"running.",
            "basis": f"{margin}-pt winning margin in 25/26"})

    # A newcomer with real pedigree is worth naming before they beat you.
    newcomers = [m for m in form_book
                 if m.get("is_new") and m.get("best_points")]
    if newcomers:
        n = max(newcomers, key=lambda m: m["best_points"])
        rank_bit = (f", and a best finish of {n['best_rank_label']} overall"
                    if n.get("best_rank_label") else "")
        out.append({
            "tag": "The New Boy",
            "text": f"{n['entry_name']} finishes in the top half at the first "
                    f"attempt. {n['seasons']} seasons on record, a career best "
                    f"of {n['best_points']}{rank_bit} — this is not a beginner, "
                    f"whatever the group chat assumes about new arrivals.",
            "subject": n["entry_name"],
            "basis": f"new to the league; {n['seasons']} seasons, "
                     f"best {n['best_points']} pts"})

    bottler = history.get("bottler") if here(history.get("bottler")) else None
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

    # A premium price the previous season did nothing to earn: the market is
    # quoting a recovery, which is a forecast, not a fact.
    priced_on_hope = [p for p in intel["premium"] if p.get("pts") is not None]
    if priced_on_hope:
        h = min(priced_on_hope, key=lambda p: p["pts"])
        out.append({
            "tag": "The Rehab",
            "text": f"{h['web_name']} ({h['club']}, £{h['price']}m) is the "
                    f"biggest gamble in the game. He scored {h['pts']} points "
                    f"all last season and is still priced like a premium — "
                    f"{h['own']}% of you are paying for a season that has not "
                    f"happened yet. He beats that total by GW20 or he wrecks "
                    f"somebody's year.",
            "basis": f"{h['pts']} pts in 25/26, priced £{h['price']}m"})

    # The bargain nobody has to think about: every minute of last season, at
    # a price that frees up money everywhere else.
    # Don't recommend a club the fixtures section is telling people to avoid —
    # the two calls would contradict each other on the same page.
    hard_clubs = {r["club"] for r in fixtures.get("hardest") or []}
    ep = [p for p in intel["ever_present"] if p["club"] not in hard_clubs] \
         or intel["ever_present"]
    if ep:
        c = min(ep, key=lambda p: (p["price"], p["own"]))
        role = {"GK": "keeper", "DEF": "defender",
                "MID": "midfielder", "FWD": "forward"}.get(c["pos"], "player")
        # Below ~10% he is a differential; above that the angle is durability.
        hook = (f"and only {c['own']}% of the game owns him — the cheapest way "
                f"to stop thinking about that slot until May"
                if c["own"] < 10 else
                f"at an ownership ({c['own']}%) that says the game already "
                f"knows, and still nobody wants to spend here")
        out.append({
            "tag": "The Cheapest Certainty",
            "text": f"{c['web_name']} ({c['club']}, £{c['price']}m) finishes as "
                    f"a top-five {role} for points per million. He played all "
                    f"{c['mins']:,} minutes last season for {c['pts']} points, "
                    f"{hook}.",
            "basis": f"{c['apps']}/38 starts, {c['pts']} pts at £{c['price']}m"})

    if fixtures["easiest"]:
        e = fixtures["easiest"][0]
        out.append({
            "tag": "The Fast Start",
            "text": f"Whoever loads up on {e['club']} assets wins August. Easiest "
                    f"opening {fixtures['span']} fixtures in the league "
                    f"(avg difficulty {e['fdr']}).",
            "basis": f"{e['club']} FDR {e['fdr']} over GW1-{fixtures['span']}"})

    # The mirror of the fast start: a brutal opening depresses prices on
    # players who were fine all along.
    if fixtures["hardest"]:
        hd = fixtures["hardest"][0]
        out.append({
            "tag": "The Slow Start",
            "text": f"{hd['club']} assets are August's worst buy and October's "
                    f"best. Hardest opening {fixtures['span']} in the league "
                    f"(avg difficulty {hd['fdr']}) — whoever holds through the "
                    f"bad run gets them cheaper than anyone who waits.",
            "basis": f"{hd['club']} FDR {hd['fdr']} over GW1-{fixtures['span']}"})

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
    bench = history.get("bench_king") if here(history.get("bench_king")) else None
    if bench and bench.get("benched") and bench["entry_name"] not in named:
        out.append({
            "tag": "The Bench Curse",
            "text": f"{bench['entry_name']} leaves another {int(bench['benched'] * 0.9)}+ "
                    f"points on the bench. Last season's {int(bench['benched'])} "
                    f"was not bad luck, it was a personality trait.",
            "subject": bench["entry_name"],
            "basis": f"{int(bench['benched'])} points benched in 25/26"})

    hits = history.get("hit_merchant") if here(history.get("hit_merchant")) else None
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

    best = history.get("best_gw")
    if best and best.get("points"):
        who = (f"{best['entry_name']}'s" if here(best) else "Last season's best")
        out.append({
            "tag": "The Ceiling",
            "text": f"Somebody beats {best['points']} in a single gameweek. "
                    f"{who} {best['points']} in GW{best['event']} is the bar, "
                    f"and a triple captain on the right double gets there "
                    f"without needing anything clever.",
            "basis": f"{best['points']} pts in GW{best['event']}, 25/26"})

    worst = history.get("worst_gw") if here(history.get("worst_gw")) else None
    if worst:
        out.append({
            "tag": "The Floor",
            "text": f"Someone in this league scores under 20 in a gameweek before "
                    f"Christmas. {worst['entry_name']} managed {worst['points']} "
                    f"in GW{worst['event']} last season and the bar has never "
                    f"been lower.",
            "basis": f"{worst['points']} pts in GW{worst['event']}, 25/26"})

    return out


# ---------------------------------------------------------------------------
# the benchmark, pre-season: no scores yet, so show the target pace
# ---------------------------------------------------------------------------

def _benchmark_pace(prev_db: Path | None, league_id: int, season: str,
                    checkpoints: tuple[int, ...] = (1, 5, 10, 15, 20, 25, 30, 38)
                    ) -> dict | None:
    """The gauntlet a nominated manager set last season, as checkpoints.

    Shares _BENCHMARK_SECTIONS with the weekly dossier, so the joke is
    configured in exactly one place. Returns None unless this league/season has
    one and the archive can answer it.
    """
    from .report import _BENCHMARK_SECTIONS, resolve_prev_league

    cfg = _BENCHMARK_SECTIONS.get((league_id, season))
    if not cfg or not prev_db or not Path(prev_db).exists():
        return None
    try:
        conn = sqlite3.connect(prev_db)
        prev_league = resolve_prev_league(conn, league_id)
        rows = _rows(conn.execute(
            """SELECT mg.event, mg.points, mg.total_points
               FROM manager_gameweeks mg JOIN managers m ON m.entry_id = mg.entry_id
               WHERE m.league_id = ? AND m.player_name = ?
               ORDER BY mg.event""", (prev_league, cfg["benchmark_manager"])))
        conn.close()
    except sqlite3.Error:
        return None
    if not rows:
        return None

    by_event = {r["event"]: r["total_points"] for r in rows}
    final = max((r["total_points"] or 0) for r in rows)
    best = max(rows, key=lambda r: r["points"] or 0)
    worst = min((r for r in rows if (r["points"] or 0) > 0),
                key=lambda r: r["points"], default=None)
    marks = [{"event": ev, "target": by_event[ev]}
             for ev in checkpoints if ev in by_event]
    return {
        "title": cfg["title"],
        "label": cfg["benchmark_label"],
        "final": final,
        "per_gw": round(final / len(rows), 1) if rows else None,
        "marks": marks,
        "best_gw": {"event": best["event"], "points": best["points"]},
        "worst_gw": ({"event": worst["event"], "points": worst["points"]}
                     if worst else None),
    }


# ---------------------------------------------------------------------------
# pre-season narrative
# ---------------------------------------------------------------------------

_PRESEASON_SECTIONS = [
    ("Unfinished Business", "var(--strong)", "state"),
    ("The Market", "var(--gold)", "market"),
    ("Ones to Watch", "var(--green)", "watch"),
    ("The Warnings", "var(--red)", "warnings"),
]

_PRESEASON_SYSTEM = (
    "You are the columnist for a private Fantasy Premier League mini-league's "
    "pre-season dossier. Write exactly four flowing paragraphs, 5-8 sentences "
    "each, about the season that is ABOUT to start — nothing has been played "
    "yet, so never invent results, scorelines or gameweek events.\n\n"
    "  state    — what last season left unresolved and how the league stands "
    "now: the champion and margin, the arc everyone remembers, who has left, "
    "who has joined, and what that sets up for the season about to start. "
    "Open in the past, but finish in the present — the reader is four days "
    "from a deadline, not reminiscing.\n"
    "  market   — this season's prices and the shape of the game: what is "
    "expensive, what the crowd has piled into, where the value is.\n"
    "  watch    — specific players worth owning, with the numbers behind them "
    "(defensive contribution, expected goals, points per million).\n"
    "  warnings — the traps: finishing that will regress, template picks "
    "carrying risk, and the managers in this league to actually fear.\n\n"
    "Use the real names and numbers you are given and nothing else. Be "
    "confident, dry and a little disrespectful about the managers — they all "
    "know each other. Use **bold** sparingly for names and numbers. Return "
    "JSON with keys: state, market, watch, warnings."
)

_PRESEASON_SCHEMA = {
    "type": "object",
    "properties": {k: {"type": "string"} for _, _, k in _PRESEASON_SECTIONS},
    "required": [k for _, _, k in _PRESEASON_SECTIONS],
    "additionalProperties": False,
}


def narrative_facts(data: dict) -> dict:
    """The subset of the page a columnist actually needs."""
    h = data.get("history") or {}
    intel = data.get("intel") or {}
    watch = data.get("watch") or {}

    def slim(rows, keys, n=5):
        return [{k: r.get(k) for k in keys} for r in (rows or [])[:n]]

    return {
        "league": data.get("league_name"),
        "season": data.get("season_label"),
        "deadline": data.get("deadline_human"),
        "days_left": data.get("days_left"),
        "managers": len(data.get("form_book") or []),
        "last_season": {
            "champion": (h.get("champion") or {}).get("entry_name"),
            "champion_points": (h.get("champion") or {}).get("total_points"),
            "margin": h.get("margin"),
            "runner_up": (h.get("runner_up") or {}).get("entry_name"),
            "bottler": h.get("bottler"),
            "best_gw": h.get("best_gw"),
            "worst_gw": h.get("worst_gw"),
            "bench_king": h.get("bench_king"),
            "hit_merchant": h.get("hit_merchant"),
        },
        "new_joiners": [{"team": m["entry_name"], "manager": m["player_name"],
                         "seasons": m["seasons"], "best": m["best_points"]}
                        for m in (data.get("new_joiners") or [])],
        "rivals": watch.get("rivals"),
        "players_to_watch": watch.get("players"),
        "defcon": slim(intel.get("defcon"),
                       ["web_name", "club", "price", "dc_pts", "hit_rate", "own"]),
        "traps": slim(intel.get("traps"),
                      ["web_name", "club", "price", "goals", "xg", "over_xg", "own"]),
        "value": slim(intel.get("value"), ["web_name", "club", "price", "ppm"]),
        "most_owned": slim((data.get("market") or {}).get("most_owned"),
                           ["web_name", "club", "price", "own"], 6),
        "priciest": slim((data.get("market") or {}).get("priciest"),
                         ["web_name", "club", "price", "own"], 4),
        "easiest_fixtures": (data.get("fixtures") or {}).get("easiest"),
        "predictions": [{"tag": p["tag"], "text": p["text"]}
                        for p in (data.get("predictions") or [])],
    }


def write_preseason_narrative(data: dict, db_path: Path, league_id: int,
                              mode: str = "auto",
                              refresh: bool = False) -> list[dict] | None:
    """Four-part pre-season column, cached alongside the weekly ones under
    event 0. Returns None when no provider is available, so the page simply
    renders without it."""
    import os
    import shutil
    from .report import (_NARRATIVE_MODEL, _NARRATIVE_PROVIDERS, _narrative_html,
                         _parse_narrative_json)

    if mode == "auto":
        if os.environ.get("ANTHROPIC_API_KEY"):
            mode = "llm"
        elif shutil.which("claude"):
            mode = "cli"
        else:
            mode = "none"

    cache = sqlite3.connect(db_path)
    try:
        cache.execute(
            "CREATE TABLE IF NOT EXISTS narrative_cache ("
            "league_id INTEGER NOT NULL, event INTEGER NOT NULL, model TEXT, "
            "content TEXT, created_at TEXT, PRIMARY KEY (league_id, event))")
        if not refresh:
            row = cache.execute(
                "SELECT content FROM narrative_cache "
                "WHERE league_id = ? AND event = 0", (league_id,)).fetchone()
            if row:
                try:
                    return json.loads(row[0])
                except (TypeError, ValueError):
                    pass

        provider = _NARRATIVE_PROVIDERS.get(mode)
        if provider is None:
            return None

        facts = dict(narrative_facts(data))
        facts["_system"] = _PRESEASON_SYSTEM
        facts["_schema"] = _PRESEASON_SCHEMA
        parsed = provider(facts)
        if not parsed:
            return None
        if isinstance(parsed, str):
            parsed = _parse_narrative_json(parsed) or {}

        shaped = [{"label": label, "color": color,
                   "html": _narrative_html(parsed[key])}
                  for label, color, key in _PRESEASON_SECTIONS if parsed.get(key)]
        if not shaped:
            return None

        cache.execute(
            "INSERT OR REPLACE INTO narrative_cache "
            "(league_id, event, model, content, created_at) VALUES (?,?,?,?,?)",
            (league_id, 0, _NARRATIVE_MODEL, json.dumps(shaped),
             datetime.now(timezone.utc).isoformat()))
        cache.commit()
        return shaped
    except sqlite3.Error:
        return None
    finally:
        cache.close()


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

    b = data.get("benchmark")
    if b:
        lines.append(f"*{b['title'].upper()}*")
        lines.append(f"Not yet — nobody has kicked a ball. The target is "
                     f"*{b['final']}* ({b['per_gw']}/gw).")
        marks = " · ".join(f"GW{m['event']}: {m['target']}"
                           for m in b["marks"] if m["event"] in (5, 10, 20, 30))
        lines.append(f"Checkpoints — {marks}")
        lines.append("")

    watch = data.get("watch") or {}
    picks = [p for p in watch.get("players", []) if not p.get("caution")][:4]
    if picks:
        lines.append("*ONES TO WATCH*")
        for p in picks:
            lines.append(f"• {p['name']} ({p['club']}, £{p['price']}m) — {p['tag']}")
        lines.append("")
    cautions = [p for p in watch.get("players", []) if p.get("caution")][:2]
    if cautions:
        names = ", ".join(f"{p['name']} (£{p['price']}m)" for p in cautions)
        lines.append(f"⚠️ *HANDLE WITH CARE:* {names}")
        lines.append("")
    if watch.get("rivals"):
        lines.append("*MANAGERS TO FEAR*")
        for r in watch["rivals"][:3]:
            lines.append(f"• {r['team']} ({r['manager']}) — best rank "
                         f"{r['best_rank_label']} ({r['best_rank_season']}), "
                         f"top {r['best_rank_pct']}")
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
                      season: str, public_url: str = "",
                      narrative: str = "auto",
                      refresh_narrative: bool = False) -> dict:
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
            f"SELECT COUNT(*) FROM managers "
            f"WHERE league_id = ?{active_clause(conn)}",
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
    _apply_identity(history, _identity_map(db_path, league_id))
    data["predictions"] = _predictions(history, intel, fixtures, market,
                                       form_book)
    data["watch"] = _watchlist(intel, market, fixtures, form_book)
    data["benchmark"] = _benchmark_pace(prev_db, league_id, season)
    data["narrative"] = write_preseason_narrative(
        data, db_path, league_id, mode=narrative, refresh=refresh_narrative)
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
                       public_url: str = "", narrative: str = "auto",
                       refresh_narrative: bool = False) -> Path:
    from .report import current_season

    season = season or current_season()
    data = collect_preseason(db_path, league_id, prev_db, season, public_url,
                             narrative=narrative,
                             refresh_narrative=refresh_narrative)
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
                      public_url: str = "", narrative: str = "auto",
                      refresh_narrative: bool = False) -> Path:
    """Render the pre-season page as ``docs/<season>/GW0.html`` so it slots into
    the existing manifest and season/gameweek picker."""
    from .report import build_manifest, current_season, _write_index

    season = season or current_season()
    season_dir = docs_dir / season
    out = season_dir / "GW0.html"
    generate_preseason(db_path, out, league_id, prev_db, season, public_url,
                       narrative=narrative, refresh_narrative=refresh_narrative)

    manifest = build_manifest(docs_dir)
    (docs_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8")
    _write_index(docs_dir, manifest)
    return out
