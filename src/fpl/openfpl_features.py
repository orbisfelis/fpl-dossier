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
            # FPL creates a player_gameweeks row for every fixture in the live
            # gameweek, zeroed, before a ball is kicked. Those rows are not
            # blanks — the match has not happened — and letting them into the
            # short windows reads as a player who has just stopped playing.
            # A fixture that has kicked off has a score, provisional or final.
            for r in self.conn.execute(f"""
                    SELECT g.*, f.kickoff_time ko FROM {table} g
                    JOIN {'fixtures' if season == self.cur_season else 'prev.fixtures'} f
                      ON f.id = g.fixture
                    WHERE f.team_h_score IS NOT NULL"""):
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

        # League rank is computed as-of a date, not once at load time.
        #
        # Two problems motivated this. Ranking on current-season points alone
        # meant that in GW3 the model was told Hull were 3rd and Liverpool 13th
        # — two gameweeks of noise feeding twelve columns. Worse, because the
        # table was built once from cur_season, every *training* row (drawn from
        # the previous season) received that same constant, leaking a later
        # season's form backwards into older examples. Ranks now derive only
        # from matches before the fixture being predicted, blended with the
        # prior season's final table until enough of the current one is played.
        self._rank_cache: dict[str | None, dict[str, int]] = {}
        self._def_rank_cache: dict[str | None, dict[str, int]] = {}
        self._season_starts: dict[str, str] = {}
        for club, rows in self.team_hist.items():
            for m in rows:
                s, d = m["season"], m["date"]
                if s not in self._season_starts or d < self._season_starts[s]:
                    self._season_starts[s] = d
        self.rank = self._rank_as_of(None)

    # a newly promoted side has no prior table; start it near the bottom
    PROMOTED_PRIOR = 18
    # matches after which the live table is trusted on its own
    RANK_SETTLES_AFTER = 10

    def _season_for(self, as_of: str | None) -> str:
        """Which season a date falls in — the latest one that had started."""
        if as_of is None:
            return self.cur_season
        best = self.cur_season
        for s, start in sorted(self._season_starts.items(), key=lambda kv: kv[1]):
            if start <= as_of:
                best = s
        return best

    def _points_table(self, season: str, before: str | None):
        pts: dict[str, float] = defaultdict(float)
        played: dict[str, int] = defaultdict(int)
        for club, rows in self.team_hist.items():
            for m in rows:
                if m["season"] != season:
                    continue
                if before and m["date"] >= before:
                    continue
                res = m.get("result")
                pts[club] += 3 if res == "w" else 1 if res == "d" else 0
                played[club] += 1
        return pts, played

    def _rank_as_of(self, as_of: str | None) -> dict[str, int]:
        """Table-to-date blended with last season's finish.

        Early in a season the live table is mostly noise, so it is averaged with
        the previous season's final positions, the live table taking over as
        matches accumulate.
        """
        if as_of in self._rank_cache:
            return self._rank_cache[as_of]
        season = self._season_for(as_of)
        clubs = sorted({c for c, rows in self.team_hist.items()
                        if any(m["season"] == season for m in rows)})
        cur_pts, cur_played = self._points_table(season, as_of)

        prev_rank: dict[str, int] = {}
        if season == self.cur_season:
            prev_pts, _ = self._points_table(self.prev_season, None)
            for i, (c, _) in enumerate(
                    sorted(prev_pts.items(), key=lambda kv: -kv[1]), 1):
                prev_rank[c] = i

        live = {c: i for i, c in enumerate(
            sorted(clubs, key=lambda c: -cur_pts.get(c, 0.0)), 1)}
        n = max(cur_played.values()) if cur_played else 0
        w = min(n / self.RANK_SETTLES_AFTER, 1.0)
        blended = {c: w * live[c] + (1 - w) * prev_rank.get(c, self.PROMOTED_PRIOR)
                   for c in clubs}
        out = {c: i for i, (c, _) in enumerate(
            sorted(blended.items(), key=lambda kv: kv[1]), 1)}
        self._rank_cache[as_of] = out
        return out

    def _xga_table(self, season: str, before: str | None):
        """Mean expected goals conceded per match, per club, up to `before`."""
        tot: dict[str, float] = defaultdict(float)
        played: dict[str, int] = defaultdict(int)
        for club, rows in self.team_hist.items():
            for m in rows:
                if m["season"] != season:
                    continue
                if before and m["date"] >= before:
                    continue
                v = m.get("xga")
                if v is None:
                    continue
                tot[club] += v
                played[club] += 1
        return ({c: tot[c] / played[c] for c in tot if played[c]}, played)

    def _def_rank_as_of(self, as_of: str | None) -> dict[str, int]:
        """Defensive strength as a 1-20 rank, 1 = fewest expected goals conceded.

        The league-position feature already carries overall quality, but a side
        can be mid-table and defend well (or lead the league by outscoring
        everyone). Expected goals conceded is the direct signal for how hard a
        team is to score against, and it was previously only visible to the
        model spread thinly across ten windowed `opponent xga` columns. Blended
        with the prior season on the same schedule as the league table, since
        three matches of xGA is mostly noise.
        """
        if as_of in self._def_rank_cache:
            return self._def_rank_cache[as_of]
        season = self._season_for(as_of)
        clubs = sorted({c for c, rows in self.team_hist.items()
                        if any(m["season"] == season for m in rows)})
        cur_xga, cur_played = self._xga_table(season, as_of)

        prev_rank: dict[str, int] = {}
        if season == self.cur_season:
            prev_xga, _ = self._xga_table(self.prev_season, None)
            for i, (c, _) in enumerate(
                    sorted(prev_xga.items(), key=lambda kv: kv[1]), 1):
                prev_rank[c] = i

        worst = max(cur_xga.values(), default=0.0) + 1.0
        live = {c: i for i, c in enumerate(
            sorted(clubs, key=lambda c: cur_xga.get(c, worst)), 1)}
        n = max(cur_played.values()) if cur_played else 0
        w = min(n / self.RANK_SETTLES_AFTER, 1.0)
        blended = {c: w * live[c] + (1 - w) * prev_rank.get(c, self.PROMOTED_PRIOR)
                   for c in clubs}
        out = {c: i for i, (c, _) in enumerate(
            sorted(blended.items(), key=lambda kv: kv[1]), 1)}
        self._def_rank_cache[as_of] = out
        return out

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
        rank = self._rank_as_of(as_of)
        for w in WINDOWS:
            f[f"team league rank {w}"] = rank.get(club)
            f[f"team opponent league rank {w}"] = rank.get(opponent)
        f["status team league rank"] = rank.get(club)
        f["status opponent league rank"] = rank.get(opponent)
        # How hard each side is to score against, as one ordinal the trees can
        # split on cleanly — see _def_rank_as_of.
        drank = self._def_rank_as_of(as_of)
        f["status team defensive rank"] = drank.get(club)
        f["status opponent defensive rank"] = drank.get(opponent)
        # The paper describes this as a percentage, but the shipped scaler was
        # fit on 0-1 (data_min_=0.0, data_max_=1.0). Passing 0-100 puts every
        # value far outside the trained range, where the trees saturate and the
        # feature stops doing anything at all — a flagged player then scores
        # exactly like a fit one. Accept the percentage callers naturally have
        # and normalise here.
        f["status player availability"] = (availability / 100.0
                                           if availability is not None and availability > 1.0
                                           else availability)
        return f
