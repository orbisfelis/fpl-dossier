"""Report generator — Markdown and PDF.

Collects data from the SQL views into a plain dict, then renders it as
either Markdown (default) or a styled PDF via Jinja2 + WeasyPrint.
"""

from __future__ import annotations

import logging
import math
import sqlite3
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from textwrap import dedent

log = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).parent / "templates"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def generate_report(db_path: Path, output: Path, league_id: int,
                    event: int | None = None, fmt: str = "md") -> Path:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    # Rebuild views to pick up any schema changes
    views_sql = TEMPLATES_DIR.parent / "views.sql"
    if views_sql.exists():
        conn.executescript(views_sql.read_text())

    if event is None:
        row = conn.execute(
            """SELECT MAX(event) AS e FROM manager_gameweeks
               WHERE entry_id IN (SELECT entry_id FROM managers WHERE league_id = ?)""",
            (league_id,),
        ).fetchone()
        event = row["e"]
        if event is None:
            raise RuntimeError(f"No gameweek data found for league {league_id}")

    log.info("Generating %s report for league %d, GW %d", fmt.upper(), league_id, event)

    data = _collect_data(conn, league_id, event)
    conn.close()

    output.parent.mkdir(parents=True, exist_ok=True)

    if fmt == "html":
        _render_html(data, output)
    else:
        _render_markdown(data, output)

    log.info("Report → %s", output)
    return output


# ---------------------------------------------------------------------------
# Data collection
# ---------------------------------------------------------------------------

def _collect_data(conn: sqlite3.Connection, league_id: int, event: int) -> dict:
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
        "SELECT COUNT(*) AS c FROM managers WHERE league_id = ?", (league_id,)
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
    tpnl_rows = _rows(conn.execute("""
        SELECT tp.entry_id, tp.gross_pnl, mg.event_transfers_cost
        FROM v_transfer_pnl tp
        JOIN manager_gameweeks mg
          ON mg.entry_id = tp.entry_id AND mg.event = tp.event
        WHERE tp.event = ?
          AND tp.entry_id IN (SELECT entry_id FROM managers WHERE league_id = ?)
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

        placeholders = ",".join("?" * len(old_elements))
        window_end = min(wc_gw + 4, event)
        total_old = 0
        total_new = 0
        gw_details = []

        for gw in range(wc_gw, window_end + 1):
            old_pts_row = conn.execute(f"""
                SELECT COALESCE(SUM(pev.event_points), 0) AS pts
                FROM v_player_event_points pev
                WHERE pev.element IN ({placeholders}) AND pev.event = ?
            """, old_elements + [gw]).fetchone()
            old_pts = old_pts_row["pts"] if old_pts_row else 0

            new_pts_row = conn.execute("""
                SELECT points FROM manager_gameweeks
                WHERE entry_id = ? AND event = ?
            """, (eid, gw)).fetchone()
            new_pts = new_pts_row["points"] if new_pts_row else 0

            total_old += old_pts
            total_new += new_pts
            gw_details.append({"gw": gw, "old_pts": old_pts, "new_pts": new_pts,
                               "diff": new_pts - old_pts})

        num_gws = window_end - wc_gw + 1
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
            actual_row = conn.execute("""
                SELECT points FROM manager_gameweeks
                WHERE entry_id = ? AND event = ?
            """, (eid, gw)).fetchone()
            actual_pts = actual_row["points"] if actual_row else 0
            pre_gw = gw - 1
            old_pts = 0
            if pre_gw >= 1:
                old_xi = conn.execute("""
                    SELECT element FROM manager_picks
                    WHERE entry_id = ? AND event = ? AND multiplier > 0
                """, (eid, pre_gw)).fetchall()
                old_elements = [row[0] for row in old_xi]
                if old_elements:
                    ph = ",".join("?" * len(old_elements))
                    old_pts_row = conn.execute(f"""
                        SELECT COALESCE(SUM(pev.event_points), 0) AS pts
                        FROM v_player_event_points pev
                        WHERE pev.element IN ({ph}) AND pev.event = ?
                    """, old_elements + [gw]).fetchone()
                    old_pts = old_pts_row["pts"] if old_pts_row else 0
            value = actual_pts - old_pts

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
               pev.own_goals, pev.yellow_cards, pev.red_cards
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
            row["bonus"] or 0,
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
    xpts_raw = _rows(conn.execute("""
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
               COALESCE(pev.bonus, 0) AS bonus
        FROM manager_picks mp
        JOIN players p ON p.id = mp.element
        LEFT JOIN v_player_event_points pev
            ON pev.element = mp.element AND pev.event = mp.event
        WHERE mp.entry_id IN (SELECT entry_id FROM managers WHERE league_id = ?)
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
            r["bonus"],
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

    return {
        "event": event,
        "league_id": league_id,
        "league_name": league_name,
        "mgr_count": mgr_count,
        "gw_avg": gw_avg,
        "fpl_avg": fpl_avg,
        "generated": datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
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
    }


# ---------------------------------------------------------------------------
# xPts computation
# ---------------------------------------------------------------------------

def _compute_xpts(element_type: int, minutes: int, xg: float, xa: float,
                  xgc: float, saves: int = 0, penalties_saved: int = 0,
                  penalties_missed: int = 0, own_goals: int = 0,
                  yellow_cards: int = 0, red_cards: int = 0,
                  bonus: int = 0) -> float:
    """Hybrid xPts: expected for goals/assists/CS, actual for everything else."""
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
