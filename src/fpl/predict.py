"""Train and apply the points model that sits on top of the OpenFPL features.

The published OpenFPL weights are not redistributable, so this trains an
equivalent on our own history instead: the same feature vector from
`openfpl_features`, fitted to the FPL points each player actually scored.

Training is walk-forward. Every row is built with `as_of` set to its own kickoff
date, so a match is only ever described by matches that preceded it — without
that the windows would summarise the result being predicted and the model would
look far better than it is.

A projection is built in three steps:

  1. Two stages: the chance of playing 60+ minutes, then the points expected
     with and without that. Minutes are the strongest signal in the data, and
     the two kinds of match score very differently.
  2. Calibration: a simple fit on the most recent matches, held out of
     training, because raw projections above about 4 points ran high.
  3. Availability: FPL's chance of playing, applied as a multiplier. It cannot
     be a feature, because we hold no history of flags: every training row would
     say "fit", and the trees would then ignore the column completely.

Train with `fpl train` after each gameweek, so the current season is included.
The artefacts land under `data/` (git-ignored) rather than in the tree.
"""

from __future__ import annotations

import csv
import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS predictions (
    event            INTEGER NOT NULL,
    element          INTEGER NOT NULL,
    model            TEXT    NOT NULL,
    predicted_points REAL,
    opponent         TEXT,
    was_home         INTEGER,
    created_at       TEXT    NOT NULL,
    PRIMARY KEY (event, element, model)
);
CREATE INDEX IF NOT EXISTS idx_pred_event ON predictions(event);

-- Append-only: `predictions` keeps just the latest run, so this is the record
-- of what was known before each deadline, for scoring the model afterwards.
CREATE TABLE IF NOT EXISTS prediction_log (
    taken_at            TEXT    NOT NULL,
    event               INTEGER NOT NULL,
    element             INTEGER NOT NULL,
    model               TEXT    NOT NULL,
    predicted_points    REAL,
    unflagged_points    REAL,
    start_chance        REAL,
    availability        REAL,
    status              TEXT,
    chance_of_playing   INTEGER,
    news                TEXT,
    ep_next             REAL,
    selected_by_percent REAL,
    now_cost            INTEGER,
    net_transfers       INTEGER,
    PRIMARY KEY (taken_at, event, element, model)
);
CREATE INDEX IF NOT EXISTS idx_pred_log_event ON prediction_log(event);

CREATE TABLE IF NOT EXISTS bookmaker_odds (
    taken_at     TEXT NOT NULL,
    event        INTEGER NOT NULL,
    market       TEXT NOT NULL,      -- e.g. anytime_scorer, clean_sheet
    subject      TEXT NOT NULL,      -- player web_name or team short name
    element      INTEGER,            -- set when the subject is a known player
    decimal_odds REAL NOT NULL,
    source       TEXT,
    PRIMARY KEY (taken_at, event, market, subject)
);
"""

DEFAULT_MODEL = Path("data/models/openfpl.json")

# Not trained on: see the module docstring.
UNTRAINED_COLUMNS = {"status player availability"}
START_MINUTES = 60
HOLDOUT_SHARE = 0.2
STAGE_PARAMS = dict(n_estimators=300, max_depth=5, learning_rate=0.03,
                    subsample=0.8, colsample_bytree=0.6, min_child_weight=8,
                    reg_lambda=2.0, n_jobs=4, random_state=0)
STAGES = ("start", "points_start", "points_other")
CALIBRATION_KNOT = 3.0
MIN_CALIBRATION_SLOPE = 0.2


def _store(cur_db, prev_db, cur_season, prev_season):
    from .openfpl_features import FeatureStore
    return FeatureStore(str(cur_db), str(prev_db), cur_season, prev_season)


def _season_rows(fs, db, current: bool, before_event: int | None = None):
    """Feature rows and outcomes for the completed matches in one season's DB."""
    con = sqlite3.connect(str(db))
    con.row_factory = sqlite3.Row
    short = {r["id"]: r["short_name"] for r in con.execute("SELECT id, short_name FROM teams")}
    rows, info = [], []
    query = """SELECT g.element, g.event, g.total_points, g.minutes, g.was_home,
                      g.opponent_team, f.kickoff_time ko, p.team_id
               FROM player_gameweeks g
               JOIN fixtures f ON f.id = g.fixture
               JOIN players  p ON p.id = g.element
               WHERE (f.finished = 1
                  -- FPL can take days to mark a played gameweek as finished
                  OR julianday(f.kickoff_time) < julianday('now') - 0.5)"""
    params: tuple = ()
    if before_event is not None:
        query += " AND g.event < ?"
        params = (before_event,)
    for r in con.execute(query, params):
        uid = None
        if not current:
            uid = fs.link_prev.get(r["element"])
            if uid is None:
                continue
        date = (r["ko"] or "")[:10]
        club, opp = short.get(r["team_id"]), short.get(r["opponent_team"])
        if not (date and club and opp):
            continue
        f = fs.features(r["element"], club, opp, bool(r["was_home"]),
                        100.0, as_of=date, uid=uid)
        if not f:
            continue
        rows.append(f)
        info.append({"season": "current" if current else "previous",
                     "event": r["event"], "element": r["element"],
                     "points": r["total_points"], "minutes": r["minutes"] or 0,
                     "date": date})
    con.close()
    return rows, info


def build_training_set(cur_db, prev_db, cur_season: str, prev_season: str,
                       before_event: int | None = None):
    """Features and outcomes from last season plus this season's completed matches."""
    import pandas as pd

    fs = _store(cur_db, prev_db, cur_season, prev_season)
    prev_rows, prev_info = _season_rows(fs, prev_db, current=False)
    cur_rows, cur_info = _season_rows(fs, cur_db, current=True, before_event=before_event)
    X = pd.DataFrame(prev_rows + cur_rows).astype(float)
    X = X.drop(columns=[c for c in UNTRAINED_COLUMNS if c in X.columns])
    return X, pd.DataFrame(prev_info + cur_info)


def _fit_stages(X, info) -> dict:
    from xgboost import XGBClassifier, XGBRegressor

    started = (info["minutes"] >= START_MINUTES).to_numpy()
    y = info["points"].to_numpy(dtype=float)
    return {
        "start": XGBClassifier(**STAGE_PARAMS).fit(X, started.astype(int)),
        "points_start": XGBRegressor(**STAGE_PARAMS).fit(X[started], y[started]),
        "points_other": XGBRegressor(**STAGE_PARAMS).fit(X[~started], y[~started]),
    }


def _expected(stages: dict, X):
    """Chance of playing 60+, and the points that implies before calibration."""
    chance = stages["start"].predict_proba(X)[:, 1]
    points = (chance * stages["points_start"].predict(X)
              + (1 - chance) * stages["points_other"].predict(X))
    return chance, points


def _fit_calibrated(X, info):
    """Fit on everything, with calibration learned from the latest matches.

    The calibration has to see predictions for matches its model never trained
    on, so a first fit stops short of the most recent matches and scores them.
    """
    order = info["date"].sort_values(kind="stable")
    cut = order.iloc[int(len(order) * (1 - HOLDOUT_SHARE))]
    older = (info["date"] < cut).to_numpy()
    _, held_out = _expected(_fit_stages(X[older], info[older]), X[~older])
    y = info["points"].to_numpy(dtype=float)[~older]
    calibration = _fit_calibration(held_out, y)
    holdout = {"rows": int((~older).sum()), "from": cut,
               "mae_raw": float(abs(held_out - y).mean()),
               "mae_calibrated": float(abs(_calibrate(held_out, calibration) - y).mean())}
    return _fit_stages(X, info), calibration, holdout


def _fit_calibration(raw, y) -> dict:
    """Two straight lines joined at CALIBRATION_KNOT, as points for `np.interp`.

    Deliberately stiffer than an isotonic fit, which collapsed most good players
    onto a single value and left no gaps to compare transfers with.
    """
    import numpy as np

    design = np.column_stack([np.ones_like(raw), raw,
                              np.maximum(raw - CALIBRATION_KNOT, 0)])
    intercept, slope, change = np.linalg.lstsq(design, y, rcond=None)[0]
    slope = max(slope, MIN_CALIBRATION_SLOPE)
    high_slope = max(slope + change, MIN_CALIBRATION_SLOPE)
    at_knot = intercept + slope * CALIBRATION_KNOT
    return {"x": [0.0, CALIBRATION_KNOT, 15.0],
            "y": [float(max(intercept, 0.0)), float(at_knot),
                  float(at_knot + high_slope * (15.0 - CALIBRATION_KNOT))]}


def _calibrate(raw, calibration: dict):
    import numpy as np
    return np.interp(raw, calibration["x"], calibration["y"])


def _stage_path(model_path: Path, stage: str) -> Path:
    return Path(model_path).with_suffix(f".{stage}.json")


def train(cur_db, prev_db, cur_season: str, prev_season: str,
          model_path: Path = DEFAULT_MODEL) -> dict:
    """Fit the model and write it, alongside the column order it expects."""
    X, info = build_training_set(cur_db, prev_db, cur_season, prev_season)
    log.info("training rows %d, features %d", len(X), X.shape[1])
    stages, calibration, holdout = _fit_calibrated(X, info)

    metrics = {
        "rows": len(X),
        "current_season_rows": int((info["season"] == "current").sum()),
        "features": X.shape[1],
        "holdout": holdout,
        "trained_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    model_path = Path(model_path)
    model_path.parent.mkdir(parents=True, exist_ok=True)
    for name, model in stages.items():
        model.save_model(str(_stage_path(model_path, name)))
    model_path.with_suffix(".meta.json").write_text(json.dumps(
        {"columns": list(X.columns), "calibration": calibration, **metrics}, indent=2))
    return metrics


def backtest(cur_db, prev_db, cur_season: str, prev_season: str,
             first_event: int, last_event: int) -> list[dict]:
    """Score each gameweek using only a model trained on what came before it.

    Availability is left out, because we hold no history of flags to replay.
    """
    X, info = build_training_set(cur_db, prev_db, cur_season, prev_season)
    current = (info["season"] == "current").to_numpy()
    results = []
    for event in range(first_event, last_event + 1):
        test = current & (info["event"] == event).to_numpy()
        if not test.any():
            continue
        train_mask = ~current | (current & (info["event"] < event).to_numpy())
        stages, calibration, _ = _fit_calibrated(X[train_mask], info[train_mask])
        _, raw = _expected(stages, X[test])
        scored = info[test].copy()
        scored["projected"] = _calibrate(raw, calibration)
        # A double gameweek is one decision, so score the player's week.
        week = scored.groupby("element", as_index=False)[["projected", "points"]].sum()
        high = week[week["projected"] >= 4]
        results.append({
            "event": event,
            "players": len(week),
            "mae": float((week["projected"] - week["points"]).abs().mean()),
            "rank_corr": float(week["projected"].rank().corr(week["points"].rank())),
            "top20_points": float(week.nlargest(20, "projected")["points"].mean()),
            "high_players": len(high),
            "high_projected": float(high["projected"].mean()) if len(high) else None,
            "high_points": float(high["points"].mean()) if len(high) else None,
        })
    return results


def availability(status: str | None, chance: int | None, is_next_event: bool) -> float:
    """Share of a player's projection to keep, given his FPL flag.

    Flags describe the coming gameweek, so `i`, `s` and `d` apply only to that.
    Later weeks need a judgement on the recovery date, which FPL does not give.
    """
    if status in ("u", "n"):    # left the club, or not in the squad
        return 0.0
    if status == "a" or status is None or not is_next_event:
        return 1.0
    if chance is not None:
        return chance / 100.0
    return 0.5 if status == "d" else 0.0


def player_state(con, flags: dict | None) -> dict[int, dict]:
    """Each player's flag and market data: live if fetched, else from the last scrape."""
    columns = {r[1] for r in con.execute("PRAGMA table_info(players)")}
    chance = "chance_of_playing_next" if "chance_of_playing_next" in columns else "NULL"
    state = {}
    for p in con.execute(
            f"""SELECT id, status, {chance} AS chance_of_playing, ep_next,
                       selected_by_percent, now_cost FROM players"""):
        state[p["id"]] = dict(p) | {"news": None, "net_transfers": None}
    for element, live in (flags or {}).items():
        state.setdefault(element, {}).update(live)
    return state


def live_state(bootstrap: dict) -> tuple[dict[int, dict], int | None]:
    """Reduce FPL's bootstrap to `player_state` values and the next gameweek."""
    def number(value):
        return float(value) if value not in (None, "") else None

    flags = {e["id"]: {
        "status": e.get("status"),
        "chance_of_playing": e.get("chance_of_playing_next_round"),
        "news": e.get("news") or None,
        "ep_next": number(e.get("ep_next")),
        "selected_by_percent": number(e.get("selected_by_percent")),
        "now_cost": e.get("now_cost"),
        "net_transfers": (e.get("transfers_in_event") or 0) - (e.get("transfers_out_event") or 0),
    } for e in bootstrap["elements"]}
    next_event = next((e["id"] for e in bootstrap["events"] if e.get("is_next")), None)
    return flags, next_event


def predict_event(cur_db, prev_db, cur_season: str, prev_season: str, event: int,
                  model_path: Path = DEFAULT_MODEL, write: bool = True,
                  flags: dict | None = None, next_event: int | None = None) -> list[dict]:
    """Predict every player with a fixture in `event`, optionally persisting."""
    import numpy as np
    import pandas as pd
    from xgboost import XGBClassifier, XGBRegressor

    model_path = Path(model_path)
    meta = json.loads(model_path.with_suffix(".meta.json").read_text())
    if "calibration" not in meta:
        raise ValueError(f"{model_path} predates the two-stage model; run `fpl train`")
    cols = meta["columns"]
    stages = {}
    for name in STAGES:
        stages[name] = XGBClassifier() if name == "start" else XGBRegressor()
        stages[name].load_model(str(_stage_path(model_path, name)))

    fs = _store(cur_db, prev_db, cur_season, prev_season)
    con = sqlite3.connect(str(cur_db))
    con.row_factory = sqlite3.Row
    short = {r["id"]: r["short_name"] for r in con.execute("SELECT id, short_name FROM teams")}
    if next_event is None:
        row = con.execute("SELECT id FROM gameweeks WHERE is_next = 1").fetchone()
        next_event = row[0] if row else None
    state = player_state(con, flags)

    slate = {}
    for r in con.execute("SELECT team_h, team_a, kickoff_time FROM fixtures WHERE event = ?",
                         (event,)):
        date = (r["kickoff_time"] or "")[:10]
        slate[r["team_h"]] = (short[r["team_a"]], True, date)
        slate[r["team_a"]] = (short[r["team_h"]], False, date)
    if not slate:
        return []

    rows, meta_rows = [], []
    for p in con.execute("SELECT id, team_id FROM players"):
        fx = slate.get(p["team_id"])
        if not fx:
            continue
        opp, home, date = fx
        f = fs.features(p["id"], short[p["team_id"]], opp, home, 100.0, as_of=date)
        if not f:
            continue
        rows.append(f)
        meta_rows.append((p["id"], opp, home))
    if not rows:
        return []

    X = pd.DataFrame(rows).reindex(columns=cols).astype(float)
    chance, raw = _expected(stages, X)
    unflagged = _calibrate(raw, meta["calibration"])
    avail = np.array([availability(state.get(e, {}).get("status"),
                                   state.get(e, {}).get("chance_of_playing"),
                                   event == next_event)
                      for e, _, _ in meta_rows])
    preds = unflagged * avail

    out = [{"element": e, "opponent": o, "was_home": int(h),
            "predicted_points": float(v), "unflagged_points": float(u),
            "start_chance": float(c), "availability": float(a)}
           for v, u, c, a, (e, o, h) in zip(preds, unflagged, chance, avail, meta_rows)]
    if write:
        con.executescript(SCHEMA)
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        name = model_path.stem
        con.executemany(
            """INSERT OR REPLACE INTO predictions
               (event, element, model, predicted_points, opponent, was_home, created_at)
               VALUES (?,?,?,?,?,?,?)""",
            [(event, r["element"], name, r["predicted_points"],
              r["opponent"], r["was_home"], now) for r in out])
        con.executemany(
            """INSERT INTO prediction_log
               (taken_at, event, element, model, predicted_points, unflagged_points,
                start_chance, availability, status, chance_of_playing, news, ep_next,
                selected_by_percent, now_cost, net_transfers)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            [(now, event, r["element"], name, r["predicted_points"], r["unflagged_points"],
              r["start_chance"], r["availability"], *(
                  state.get(r["element"], {}).get(k) for k in
                  ("status", "chance_of_playing", "news", "ep_next",
                   "selected_by_percent", "now_cost", "net_transfers")))
             for r in out])
        con.commit()
    return sorted(out, key=lambda r: -r["predicted_points"])


def import_odds(cur_db, event: int, path: Path) -> tuple[int, list[str]]:
    """Log bookmaker prices from a CSV of market,subject,decimal_odds[,source].

    Returns the rows stored, and any subjects that matched no player or team.
    """
    con = sqlite3.connect(str(cur_db))
    con.executescript(SCHEMA)
    players = {}
    for element, name in con.execute("SELECT id, web_name FROM players"):
        players.setdefault(name.casefold(), []).append(element)
    teams = {r[0].casefold() for r in con.execute("SELECT short_name FROM teams")}
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    rows, unmatched = [], []
    with open(path, newline="", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            subject = r["subject"].strip()
            matches = players.get(subject.casefold(), [])
            element = matches[0] if len(matches) == 1 else None
            if element is None and subject.casefold() not in teams:
                unmatched.append(subject)
            rows.append((now, event, r["market"].strip(), subject, element,
                         float(r["decimal_odds"]), (r.get("source") or "").strip() or None))
    con.executemany("INSERT INTO bookmaker_odds VALUES (?,?,?,?,?,?,?)", rows)
    con.commit()
    return len(rows), unmatched


def captain_shortlist(cur_db, event: int, rows: list[dict], state: dict | None = None,
                      limit: int = 8) -> list[dict]:
    """Likely starters, ranked by anytime-scorer odds when logged, else ownership.

    In the backtest the model's top pick scored far less than the most-owned
    player, so its projection is shown here as context and does not set the order.
    """
    con = sqlite3.connect(str(cur_db))
    con.row_factory = sqlite3.Row
    info = player_state(con, state)
    odds = {}
    if con.execute("SELECT 1 FROM sqlite_master WHERE name = 'bookmaker_odds'").fetchone():
        for r in con.execute(
                """SELECT element, decimal_odds FROM bookmaker_odds
                   WHERE event = ? AND market = 'anytime_scorer' AND element IS NOT NULL
                   ORDER BY taken_at""", (event,)):
            odds[r["element"]] = 1 / r["decimal_odds"]     # latest price wins
    goalkeepers = {r[0] for r in con.execute("SELECT id FROM players WHERE element_type = 1")}
    picks = [r | {"own": info.get(r["element"], {}).get("selected_by_percent") or 0.0,
                  "anytime": odds.get(r["element"])}
             for r in rows if r["start_chance"] * r["availability"] >= 0.6
             and r["element"] not in goalkeepers]
    return sorted(picks, key=lambda r: (-(r["anytime"] or 0), -r["own"]))[:limit]
