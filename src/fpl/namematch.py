"""Match FPL players to Understat players.

Two sources, no shared key, and names that disagree in every way names can:
accents (Buendía / Buendia), letters that do not decompose (Ødegaard), and FPL
carrying full legal names where Understat carries the common one (FPL's
"Raya Martin" against Understat's "David Raya").

The join is therefore: normalise hard, compare token *sets* rather than
strings, and use the club to break ties. Club is what makes it safe — the two
Whites and the two Wilsons are only ambiguous until you notice they play for
different teams.
"""

from __future__ import annotations

import re
import sqlite3
import unicodedata
from collections import defaultdict

# Letters NFKD will not take apart.
_TRANSLIT = str.maketrans({
    "ø": "o", "Ø": "o", "æ": "ae", "Æ": "ae", "ß": "ss",
    "đ": "d", "Đ": "d", "ð": "d", "Ð": "d", "ł": "l", "Ł": "l",
    "þ": "th", "Þ": "th", "ı": "i", "ʼ": "", "'": "", "'": "",
})

# Understat's club names against FPL's short codes.
CLUBS = {
    "Arsenal": "ARS", "Aston Villa": "AVL", "Bournemouth": "BOU",
    "Brentford": "BRE", "Brighton": "BHA", "Chelsea": "CHE",
    "Coventry": "COV", "Crystal Palace": "CRY", "Everton": "EVE",
    "Fulham": "FUL", "Hull": "HUL", "Ipswich": "IPS", "Leeds": "LEE",
    "Liverpool": "LIV", "Manchester City": "MCI", "Manchester United": "MUN",
    "Newcastle United": "NEW", "Nottingham Forest": "NFO",
    "Sunderland": "SUN", "Tottenham": "TOT", "West Ham": "WHU",
    "Wolverhampton Wanderers": "WOL", "Burnley": "BUR", "Leicester": "LEI",
    "Southampton": "SOU",
}


def normalise(name: str | None) -> str:
    s = (name or "").translate(_TRANSLIT)
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower().replace("-", " ")
    return re.sub(r"[^a-z ]", " ", s).strip()


def tokens(name: str | None) -> set[str]:
    return {t for t in normalise(name).split() if len(t) > 1}


def build(conn: sqlite3.Connection, season: str) -> dict[int, int]:
    """Return {fpl_element_id: understat_player_id}."""
    fpl = [dict(r) for r in conn.execute("""
        SELECT p.id, p.web_name, p.first_name, p.second_name, t.short_name club
        FROM players p JOIN teams t ON t.id = p.team_id""")]
    us = [dict(r) for r in conn.execute(
        "SELECT player_id, player_name, team_title FROM understat_players WHERE season = ?",
        (season,))]
    for u in us:
        u["club"] = CLUBS.get(u["team_title"], u["team_title"])
        u["tok"] = tokens(u["player_name"])

    by_club = defaultdict(list)
    for u in us:
        by_club[u["club"]].append(u)

    def score(f, u) -> float:
        """Higher is a better match. Club agreement is worth more than any name
        similarity, because names collide within a league and clubs do not."""
        ftok = tokens(f"{f['first_name']} {f['second_name']}") | tokens(f["web_name"])
        if not (ftok & u["tok"]):
            return 0.0
        if ftok == u["tok"]:
            s = 4.0
        elif u["tok"] <= ftok or ftok <= u["tok"]:
            s = 3.0
        else:
            s = 1.0 + len(ftok & u["tok"]) / max(len(ftok | u["tok"]), 1)
        return s + (2.0 if u["club"] == f["club"] else 0.0)

    # Score every plausible pair, then assign best-first. Ordering by quality is
    # what stops a squad player who never plays from claiming the record that
    # belongs to a regular with a similar name.
    pairs = []
    for f in fpl:
        pool = by_club.get(f["club"], []) or us
        seen = {id(u) for u in pool}
        for u in list(pool) + [x for x in us if id(x) not in seen]:
            s = score(f, u)
            if s > 0:
                pairs.append((s, f["id"], u["player_id"]))
    pairs.sort(key=lambda x: -x[0])

    out: dict[int, int] = {}
    used: set[int] = set()
    for s, fid, uid in pairs:
        if fid in out or uid in used:
            continue
        out[fid] = uid
        used.add(uid)
    return out
