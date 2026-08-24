"""Build the OpenFPL feature vector from our FPL + Understat tables.

The published models want 228 columns: 45 stat families each averaged over the
player's (or team's) last 1, 3, 5, 10 and 38 matches, plus three status scalars.
Definitions that are not obvious from the column names, taken from the paper:

  * "relevant fpl points" is points scored *at the venue of the upcoming match*
    — a home fixture looks back only at previous home games.
  * windows are means over the last N matches, not last N gameweeks, so blanks
    and non-selections do not dilute them.
  * "status player availability" is FPL's chance-of-playing as 0/25/50/75/100.

Match history has to span the season rollover, because in GW2 the 10- and
38-match windows are almost entirely last season. FPL element ids are recycled
each summer, so the join key across seasons is the Understat player id, which
is stable.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict

WINDOWS = (1, 3, 5, 10, 38)

# family name -> key in the merged per-match record
PLAYER_FPL = {
    "player fpl points": "total_points",
    "player minutes played": "minutes",
    "player influence": "influence",
    "player creativity": "creativity",
    "player threat": "threat",
    "player goals scored": "goals_scored",
    "player assists": "assists",
    "player goals conceded": "goals_conceded",
    "player own goals": "own_goals",
    "player penalties saved": "penalties_saved",
    "player penalties missed": "penalties_missed",
    "player yellow cards": "yellow_cards",
    "player red cards": "red_cards",
    "player saves": "saves",
    "player bps": "bps",
    "player fpl bonus points": "bonus",
}
PLAYER_US = {
    "player xg": "us_xg",
    "player xa": "us_xa",
    "player shots": "us_shots",
    "player key passes": "us_key_passes",
    "player xgchain": "us_xgchain",
    "player xgbuildup": "us_xgbuildup",
}
TEAM_FAMS = {
    "xg": "xg", "xga": "xga", "deep": "deep", "deep allowed": "deep_allowed",
    "ppda att": "ppda_att", "ppda def": "ppda_def",
    "ppda allowed att": "ppda_allowed_att", "ppda allowed def": "ppda_allowed_def",
    "goals scored": "scored", "goals conceded": "missed",
}


def _mean(vals):
    vals = [v for v in vals if v is not None]
    return sum(vals) / len(vals) if vals else None


def _windows(history: list[dict], key: str) -> dict[int, float | None]:
    """Mean of `key` over the last N matches, most recent first."""
    seq = [h.get(key) for h in history]
    return {w: _mean(seq[:w]) for w in WINDOWS}


class FeatureStore:
    """Per-match history for every player and team, across both seasons."""

    def __init__(self, cur_db: str, prev_db: str, cur_season: str, prev_season: str):
        self.cur_season, self.prev_season = cur_season, prev_season
        self.conn = sqlite3.connect(cur_db)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("ATTACH DATABASE ? AS prev", (prev_db,))
        self._load_players()
        self._load_teams()

    # ----- identity ------------------------------------------------------
    def _link(self, season: str, prefix: str) -> dict[int, int]:
        """FPL element id -> Understat player id, for one season.

        `prefix` is "" for the current-season schema or "prev." for the attached
        archive. Best-scoring pairs are assigned first so a squad player who
        never appears cannot claim the record belonging to a regular.
        """
        from .namematch import CLUBS, tokens
        us = [dict(r) for r in self.conn.execute(
            "SELECT player_id, player_name, team_title FROM understat_players WHERE season=?",
            (season,))]
        for u in us:
            u["club"] = CLUBS.get(u["team_title"], u["team_title"])
            u["tok"] = tokens(u["player_name"])
        fpl = [dict(r) for r in self.conn.execute(
            f"""SELECT p.id, p.web_name, p.first_name, p.second_name, t.short_name club
                FROM {prefix}players p JOIN {prefix}teams t ON t.id = p.team_id""")]
        pairs = []
        for f in fpl:
            ftok = tokens(f"{f['first_name']} {f['second_name']}") | tokens(f["web_name"])
            for u in us:
                inter = ftok & u["tok"]
                if not inter:
                    continue
                if ftok == u["tok"]:
                    s = 4.0
                elif u["tok"] <= ftok or ftok <= u["tok"]:
                    s = 3.0
                else:
                    s = 1.0 + len(inter) / max(len(ftok | u["tok"]), 1)
                pairs.append((s + (2.0 if u["club"] == f["club"] else 0.0),
                              f["id"], u["player_id"]))
        pairs.sort(key=lambda x: -x[0])
        out, used = {}, set()
        for s, fid, uid in pairs:
            if fid in out or uid in used:
                continue
            out[fid] = uid
            used.add(uid)
        return out

    # ----- history -------------------------------------------------------
    def _load_players(self):
        self.link_cur = self._link(self.cur_season, "")
        self.link_prev = self._link(self.prev_season, "prev.")

        # Understat per-match, keyed by (understat id, date)
        us = defaultdict(dict)
        for r in self.conn.execute("""SELECT player_id, date, minutes, goals, assists,
                                             shots, key_passes, xg, xa
                                      FROM understat_player_matches"""):
            us[r["player_id"]][r["date"]] = {
                "us_xg": r["xg"], "us_xa": r["xa"], "us_shots": r["shots"],
                "us_key_passes": r["key_passes"],
            }
        # xGChain/xGBuildup only exist as season aggregates; spread them per match
        agg = {}
        for r in self.conn.execute("""SELECT season, player_id, games, xgchain, xgbuildup
                                      FROM understat_players"""):
            g = r["games"] or 0
            agg[(r["season"], r["player_id"])] = (
                (r["xgchain"] or 0) / g if g else None,
                (r["xgbuildup"] or 0) / g if g else None)

        self.hist: dict[int, list[dict]] = defaultdict(list)
        for season, table, link in ((self.cur_season, "player_gameweeks", self.link_cur),
                                    (self.prev_season, "prev.player_gameweeks", self.link_prev)):
            rev = {v: k for k, v in link.items()}
            for r in self.conn.execute(f"""
                    SELECT g.*, f.kickoff_time ko FROM {table} g
                    LEFT JOIN {'fixtures' if season == self.cur_season else 'prev.fixtures'} f
                      ON f.id = g.fixture"""):
                uid = link.get(r["element"])
                if uid is None:
                    continue
                d = dict(r)
                rec = {fam: d.get(col) for fam, col in PLAYER_FPL.items()}
                rec["date"] = (d.get("kickoff_time") or d.get("ko") or "")[:10]
                rec["home"] = d.get("was_home")
                rec["season"] = season
                m = us.get(uid, {}).get(rec["date"], {})
                rec.update({k: m.get(k) for k in
                            ("us_xg", "us_xa", "us_shots", "us_key_passes")})
                ch, bu = agg.get((season, uid), (None, None))
                rec["us_xgchain"], rec["us_xgbuildup"] = ch, bu
                self.hist[uid].append(rec)
        for uid in self.hist:
            self.hist[uid].sort(key=lambda x: x["date"], reverse=True)

    def _load_teams(self):
        titles = {}
        for r in self.conn.execute("SELECT season, team_id, title FROM understat_teams"):
            titles[(r["season"], r["team_id"])] = r["title"]
        from .namematch import CLUBS
        self.team_hist: dict[str, list[dict]] = defaultdict(list)
        for r in self.conn.execute("SELECT * FROM understat_team_matches"):
            club = CLUBS.get(titles.get((r["season"], r["team_id"]), ""), None)
            if not club:
                continue
            self.team_hist[club].append({**{k: r[k] for k in r.keys()},
                                         "date": r["date"][:10]})
        for c in self.team_hist:
            self.team_hist[c].sort(key=lambda x: x["date"], reverse=True)

        # league rank by points-to-date, per season
        self.rank: dict[str, int] = {}
        pts = defaultdict(float)
        for club, rows in self.team_hist.items():
            for m in rows:
                if m["season"] != self.cur_season:
                    continue
                res = m.get("result")
                pts[club] += 3 if res == "w" else 1 if res == "d" else 0
        for i, (club, _) in enumerate(sorted(pts.items(), key=lambda kv: -kv[1]), 1):
            self.rank[club] = i

    # ----- assembly ------------------------------------------------------
    def features(self, element: int, club: str, opponent: str, home: bool,
                 availability: float, as_of: str | None = None,
                 uid: int | None = None) -> dict[str, float | None] | None:
        """Feature vector for one player-fixture.

        `as_of` is the kickoff date of the fixture being predicted. Every window
        is then built only from matches strictly before it, which is what makes
        a walk-forward backtest honest — without it the model would be shown the
        result it is being asked to forecast.
        """
        if uid is None:
            uid = self.link_cur.get(element)
        if uid is None:
            return None
        h = [x for x in self.hist.get(uid, []) if x["date"]]
        if as_of:
            h = [x for x in h if x["date"] < as_of]
        if not h:
            return None
        f: dict[str, float | None] = {}
        for fam in PLAYER_FPL:
            for w, v in _windows(h, fam).items():
                f[f"{fam} {w}"] = v
        for fam, col in PLAYER_US.items():
            # the record stores Understat values under us_* keys, not the
            # display family name the model expects
            for w, v in _windows(h, col).items():
                f[f"{fam} {w}"] = v
        # venue-specific points
        same_venue = [x for x in h if bool(x.get("home")) == bool(home)]
        for w, v in _windows(same_venue, "player fpl points").items():
            f[f"player relevant fpl points {w}"] = v

        for side, cl in (("team", club), ("opponent", opponent)):
            rows = self.team_hist.get(cl, [])
            if as_of:
                rows = [x for x in rows if x["date"] < as_of]
            for fam, col in TEAM_FAMS.items():
                for w, v in _windows(rows, col).items():
                    f[f"{side} {fam} {w}"] = v
        for w in WINDOWS:
            f[f"team league rank {w}"] = self.rank.get(club)
            f[f"team opponent league rank {w}"] = self.rank.get(opponent)
        f["status team league rank"] = self.rank.get(club)
        f["status opponent league rank"] = self.rank.get(opponent)
        f["status player availability"] = availability
        return f
