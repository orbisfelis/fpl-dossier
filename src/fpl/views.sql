-- Derived views layered on top of the raw tables.
-- All read-only; rebuilt on every Store() init so safe to edit and reload.

DROP VIEW IF EXISTS v_player_event_points;
CREATE VIEW v_player_event_points AS
    -- Player points per event (sums DGW fixtures into a single row).
    SELECT
        element,
        event,
        SUM(total_points)               AS event_points,
        SUM(minutes)                    AS minutes,
        SUM(goals_scored)               AS goals,
        SUM(assists)                    AS assists,
        SUM(clean_sheets)              AS clean_sheets,
        SUM(goals_conceded)            AS goals_conceded,
        SUM(bonus)                      AS bonus,
        SUM(bps)                        AS bps,
        SUM(saves)                      AS saves,
        SUM(penalties_saved)            AS penalties_saved,
        SUM(penalties_missed)           AS penalties_missed,
        SUM(own_goals)                  AS own_goals,
        SUM(yellow_cards)               AS yellow_cards,
        SUM(red_cards)                  AS red_cards,
        SUM(expected_goals)             AS xg,
        SUM(expected_assists)           AS xa,
        SUM(expected_goal_involvements) AS xgi,
        SUM(expected_goals_conceded)    AS xgc,
        SUM(clearances_blocks_interceptions) AS cbi,
        SUM(recoveries)                 AS recoveries,
        SUM(tackles)                    AS tackles,
        SUM(defensive_contribution)     AS def_con,
        COUNT(*)                        AS fixtures_played
    FROM player_gameweeks
    GROUP BY element, event;


DROP VIEW IF EXISTS v_pick_points;
CREATE VIEW v_pick_points AS
    -- Joins picks to actual points scored, applying the multiplier.
    SELECT
        mp.entry_id,
        mp.event,
        mp.element,
        mp.position,
        mp.multiplier,
        mp.is_captain,
        mp.is_vice_captain,
        COALESCE(pev.event_points, 0)            AS raw_points,
        COALESCE(pev.event_points, 0) * mp.multiplier AS effective_points
    FROM manager_picks mp
    LEFT JOIN v_player_event_points pev
      ON pev.element = mp.element AND pev.event = mp.event;


DROP VIEW IF EXISTS v_captain_points;
CREATE VIEW v_captain_points AS
    -- Effective captain: the player who actually carried the captain multiplier.
    -- Normally the captain (multiplier=2).  When the captain didn't play, the
    -- vice-captain inherits the armband (multiplier=2).  When neither played,
    -- no row is produced (no captain bonus earned that GW).
    SELECT
        mp.entry_id,
        mp.event,
        mp.element              AS captain_element,
        p.web_name              AS captain_name,
        mp.multiplier,
        COALESCE(pev.event_points, 0)            AS captain_raw_points,
        COALESCE(pev.event_points, 0) * mp.multiplier AS captain_effective_points,
        mp.is_vice_captain      AS vc_activated
    FROM manager_picks mp
    JOIN players p ON p.id = mp.element
    LEFT JOIN v_player_event_points pev
      ON pev.element = mp.element AND pev.event = mp.event
    WHERE mp.multiplier >= 2;


DROP VIEW IF EXISTS v_bench_points;
CREATE VIEW v_bench_points AS
    -- Points sitting on each manager's bench per event.
    SELECT
        entry_id,
        event,
        SUM(raw_points) AS bench_points
    FROM v_pick_points
    WHERE multiplier = 0
    GROUP BY entry_id, event;


DROP VIEW IF EXISTS v_transfer_pnl;
CREATE VIEW v_transfer_pnl AS
    -- Per-event transfer P&L: points gained by bought players minus points
    -- foregone by sold players, gross of hits.
    SELECT
        mt.entry_id,
        mt.event,
        COUNT(*) AS num_transfers,
        SUM(COALESCE(pin.event_points, 0))  AS points_in,
        SUM(COALESCE(pout.event_points, 0)) AS points_out,
        SUM(COALESCE(pin.event_points, 0) - COALESCE(pout.event_points, 0)) AS gross_pnl
    FROM manager_transfers mt
    LEFT JOIN v_player_event_points pin
        ON pin.element = mt.element_in AND pin.event = mt.event
    LEFT JOIN v_player_event_points pout
        ON pout.element = mt.element_out AND pout.event = mt.event
    GROUP BY mt.entry_id, mt.event;


DROP VIEW IF EXISTS v_ownership_event;
CREATE VIEW v_ownership_event AS
    -- League ownership per player per event (XI + bench, used for template).
    SELECT
        event,
        element,
        COUNT(*) AS owners,
        SUM(CASE WHEN multiplier > 0 THEN 1 ELSE 0 END) AS starters,
        SUM(CASE WHEN is_captain  = 1 THEN 1 ELSE 0 END) AS captains
    FROM manager_picks
    GROUP BY event, element;


DROP VIEW IF EXISTS v_leaderboard_latest;
CREATE VIEW v_leaderboard_latest AS
    -- Convenience: each manager's latest scraped GW row.
    SELECT
        m.entry_id, m.entry_name, m.player_name, m.league_id,
        mg.event AS latest_event,
        mg.points AS gw_points,
        mg.total_points,
        mg.overall_rank,
        mg.value,
        mg.bank,
        mg.event_transfers,
        mg.event_transfers_cost,
        mg.points_on_bench
    FROM managers m
    JOIN manager_gameweeks mg ON mg.entry_id = m.entry_id
    WHERE mg.event = (SELECT MAX(event) FROM manager_gameweeks WHERE entry_id = m.entry_id);
