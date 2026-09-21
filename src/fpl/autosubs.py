"""Detect substitutions FPL still owes a manager.

Scores keep moving after the last final whistle. FPL applies automatic
substitutions in its own time, and until it has, a manager who started someone
who did not play is short their bench replacement's points — the API just
reports an empty ``automatic_subs`` and a total that is quietly too low.

Publishing in that window ships a table that is wrong with nothing on the page
to say so, which is how it gets missed. So rather than trust a flag, check the
squads: if a starter has no minutes and a bench player who did play could
legally replace them, the week is not finished settling.

The rules are FPL's own:

  * only a starter with zero minutes is replaced, and only by a bench player
    who actually played;
  * the bench is tried in order, and a swap is taken only if the resulting
    formation is still legal (1 GK, 3-5 DEF, 2-5 MID, 1-3 FWD);
  * the reserve keeper substitutes for the keeper and for nobody else;
  * if the captain did not play, the armband passes to the vice-captain;
  * a Bench Boost means all fifteen already count, so nothing is substituted.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

MIN_BY_POS = {1: 1, 2: 3, 3: 2, 4: 1}
MAX_BY_POS = {1: 1, 2: 5, 3: 5, 4: 3}


def squad_for(conn: sqlite3.Connection, entry_id: int, event: int) -> list[dict]:
    return [dict(r) for r in conn.execute(
        """SELECT mp.element, mp.position, mp.multiplier, mp.is_captain,
                  mp.is_vice_captain, p.element_type AS pos,
                  COALESCE(g.minutes, 0) AS minutes,
                  COALESCE(g.total_points, 0) AS points
           FROM manager_picks mp
           JOIN players p ON p.id = mp.element
           LEFT JOIN player_gameweeks g ON g.element = mp.element AND g.event = mp.event
           WHERE mp.entry_id = ? AND mp.event = ?
           ORDER BY mp.position""", (entry_id, event))]


def pending_for_squad(squad: list[dict], chip: str | None) -> tuple[list[tuple], int]:
    """Substitutions still owed to one manager, and the points they would add."""
    if chip == "bboost" or len(squad) != 15:
        return [], 0

    xi = [p for p in squad if p["multiplier"] > 0]
    bench = [p for p in squad if p["multiplier"] == 0]
    counts: dict[int, int] = {}
    for p in xi:
        counts[p["pos"]] = counts.get(p["pos"], 0) + 1

    moves, gained, used = [], 0, set()
    for out in xi:
        if out["minutes"] > 0:
            continue
        for sub in bench:
            if sub["element"] in used or sub["minutes"] <= 0:
                continue
            # The reserve keeper is only ever a keeper's replacement.
            if (out["pos"] == 1) != (sub["pos"] == 1):
                continue
            if out["pos"] != 1:
                trial = dict(counts)
                trial[out["pos"]] = trial.get(out["pos"], 0) - 1
                trial[sub["pos"]] = trial.get(sub["pos"], 0) + 1
                if any(trial.get(k, 0) < v for k, v in MIN_BY_POS.items()):
                    continue
                if any(trial.get(k, 0) > v for k, v in MAX_BY_POS.items()):
                    continue
                counts = trial
            used.add(sub["element"])
            moves.append((out, sub))
            gained += sub["points"] * out["multiplier"]
            break

    # A captain who did not play hands the armband to the vice — but only if he
    # is still holding it. Once FPL has moved it, or he was benched to begin
    # with, his multiplier is no longer above 1 and there is nothing pending;
    # reading that as an outstanding handover invents a negative adjustment and
    # blocks a gameweek that has in fact long since settled.
    cap = next((p for p in squad if p["is_captain"]), None)
    vice = next((p for p in squad if p["is_vice_captain"]), None)
    if (cap and vice and cap["multiplier"] > 1
            and cap["minutes"] <= 0 and vice["minutes"] > 0):
        gained += vice["points"] * (cap["multiplier"] - 1)
        moves.append(("captain", cap, vice))
    return moves, gained


def pending(db_path: Path, event: int, league_id: int | None = None) -> dict[int, int]:
    """Entry id -> points still owed, for everyone with a substitution outstanding."""
    try:
        conn = sqlite3.connect(db_path)
    except sqlite3.Error:
        return {}
    conn.row_factory = sqlite3.Row
    try:
        sql, args = "SELECT entry_id FROM managers", ()
        if league_id is not None:
            sql, args = sql + " WHERE league_id = ?", (league_id,)
        out = {}
        for r in conn.execute(sql, args):
            squad = squad_for(conn, r["entry_id"], event)
            if not squad:
                continue
            chip = conn.execute(
                "SELECT chip FROM manager_chips WHERE entry_id = ? AND event = ?",
                (r["entry_id"], event)).fetchone()
            moves, gained = pending_for_squad(squad, chip["chip"] if chip else None)
            if moves:
                out[r["entry_id"]] = gained
        return out
    except sqlite3.Error:
        return {}
    finally:
        conn.close()
