"""Train and apply the points model that sits on top of the OpenFPL features.

The published OpenFPL weights are not redistributable, so this trains an
equivalent on our own history instead: the same 228-column feature vector from
`openfpl_features`, fitted to the FPL points each player actually scored.

Training is walk-forward. Every row is built with `as_of` set to its own kickoff
date, so a match is only ever described by matches that preceded it — without
that the windows would summarise the result being predicted and the model would
look far better than it is.

The artefact lands under `data/` (git-ignored) rather than in the tree: it is a
couple of megabytes of JSON, and `fpl train` reproduces it in about a minute.
"""

from __future__ import annotations

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
"""

DEFAULT_MODEL = Path("data/models/openfpl.json")


def _store(cur_db, prev_db, cur_season, prev_season):
    from .openfpl_features import FeatureStore
    return FeatureStore(str(cur_db), str(prev_db), cur_season, prev_season)


def build_training_set(cur_db, prev_db, cur_season: str, prev_season: str):
    """Feature matrix and target from the previous season's completed matches."""
    import pandas as pd

    fs = _store(cur_db, prev_db, cur_season, prev_season)
    prev = sqlite3.connect(str(prev_db))
    prev.row_factory = sqlite3.Row
    short = {r["id"]: r["short_name"] for r in prev.execute("SELECT id, short_name FROM teams")}

    rows, target = [], []
    for r in prev.execute(
            """SELECT g.element, g.total_points, g.was_home, g.opponent_team,
                      f.kickoff_time ko, p.team_id
               FROM player_gameweeks g
               JOIN fixtures f ON f.id = g.fixture
               JOIN players  p ON p.id = g.element"""):
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
        target.append(r["total_points"])
    X = pd.DataFrame(rows).astype(float)
    return X, target


def train(cur_db, prev_db, cur_season: str, prev_season: str,
          model_path: Path = DEFAULT_MODEL, seed: int = 0) -> dict:
    """Fit the model and write it, alongside the column order it expects."""
    import numpy as np
    from sklearn.metrics import mean_absolute_error
    from xgboost import XGBRegressor

    X, y = build_training_set(cur_db, prev_db, cur_season, prev_season)
    y = np.asarray(y, dtype=float)
    log.info("training rows %d, features %d", len(X), X.shape[1])

    rng = np.random.default_rng(seed)
    idx = rng.permutation(len(y))
    cut = int(len(y) * 0.8)
    tr, va = idx[:cut], idx[cut:]

    model = XGBRegressor(
        n_estimators=600, max_depth=5, learning_rate=0.03,
        subsample=0.8, colsample_bytree=0.6, min_child_weight=8,
        reg_lambda=2.0, n_jobs=4, early_stopping_rounds=40)
    model.fit(X.iloc[tr], y[tr], eval_set=[(X.iloc[va], y[va])], verbose=False)

    pred = model.predict(X.iloc[va])
    metrics = {
        "rows": len(X),
        "features": X.shape[1],
        "mae": float(mean_absolute_error(y[va], pred)),
        "baseline_mae": float(mean_absolute_error(
            y[va], np.full_like(pred, y[tr].mean()))),
        "corr": float(np.corrcoef(pred, y[va])[0, 1]),
        "best_iteration": int(model.best_iteration),
        "trained_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    model_path = Path(model_path)
    model_path.parent.mkdir(parents=True, exist_ok=True)
    model.save_model(str(model_path))
    model_path.with_suffix(".meta.json").write_text(
        json.dumps({"columns": list(X.columns), **metrics}, indent=2))
    return metrics


def predict_event(cur_db, prev_db, cur_season: str, prev_season: str, event: int,
                  model_path: Path = DEFAULT_MODEL, write: bool = True) -> list[dict]:
    """Predict every player with a fixture in `event`, optionally persisting."""
    import pandas as pd
    from xgboost import XGBRegressor

    model_path = Path(model_path)
    meta = json.loads(model_path.with_suffix(".meta.json").read_text())
    cols = meta["columns"]
    model = XGBRegressor()
    model.load_model(str(model_path))

    fs = _store(cur_db, prev_db, cur_season, prev_season)
    con = sqlite3.connect(str(cur_db))
    con.row_factory = sqlite3.Row
    short = {r["id"]: r["short_name"] for r in con.execute("SELECT id, short_name FROM teams")}

    slate = {}
    for r in con.execute("SELECT team_h, team_a, kickoff_time FROM fixtures WHERE event = ?",
                         (event,)):
        date = (r["kickoff_time"] or "")[:10]
        slate[r["team_h"]] = (short[r["team_a"]], True, date)
        slate[r["team_a"]] = (short[r["team_h"]], False, date)
    if not slate:
        return []

    rows, meta_rows = [], []
    for p in con.execute("SELECT id, team_id, status, ep_next FROM players"):
        fx = slate.get(p["team_id"])
        if not fx:
            continue
        opp, home, date = fx
        # `status` is the only availability signal we hold per player; a flagged
        # player keeps his history but should not be projected as if fit.
        avail = 100.0 if p["status"] == "a" else 0.0
        f = fs.features(p["id"], short[p["team_id"]], opp, home, avail, as_of=date)
        if not f:
            continue
        rows.append(f)
        meta_rows.append((p["id"], opp, home))
    if not rows:
        return []

    X = pd.DataFrame(rows).reindex(columns=cols).astype(float)
    preds = model.predict(X)

    out = [{"element": e, "opponent": o, "was_home": int(h),
            "predicted_points": float(v)}
           for v, (e, o, h) in zip(preds, meta_rows)]
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
        con.commit()
    return sorted(out, key=lambda r: -r["predicted_points"])
