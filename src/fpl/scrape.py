"""Scraping pipeline: bootstrap → league → managers → players."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path

import httpx

from .client import FPLClient, TIMEOUT, USER_AGENT
from .db import Store

log = logging.getLogger(__name__)


async def collect_league_entries(fpl: FPLClient, league_id: int) -> tuple[list[dict], str]:
    """Return (entries, league_name)."""
    entries: list[dict] = []
    league_name = ""
    page = 1
    while True:
        data = await fpl.league_page(league_id, page)
        if not data:
            raise RuntimeError(
                f"League {league_id} returned no data — wrong ID, or private?"
            )
        if not league_name:
            league_name = data.get("league", {}).get("name", "")
        standings = data["standings"]
        entries.extend(standings["results"])
        if not standings.get("has_next"):
            break
        page += 1
    log.info("League %d (%s): %d managers", league_id, league_name, len(entries))
    return entries, league_name


async def scrape_manager(fpl: FPLClient, store: Store, entry: dict,
                         league_id: int, current_event: int, now: str) -> None:
    entry_id = entry["entry"]
    store.upsert_manager(entry_id, entry["entry_name"], entry["player_name"], league_id, now)

    history = await fpl.manager_history(entry_id)
    store.upsert_manager_history(entry_id, history.get("current", []))
    store.upsert_chips(entry_id, history.get("chips", []))
    store.upsert_past_seasons(entry_id, history.get("past", []))

    transfers = await fpl.manager_transfers(entry_id)
    if isinstance(transfers, list):
        store.upsert_transfers(transfers)

    pick_tasks = [fpl.manager_picks(entry_id, gw) for gw in range(1, current_event + 1)]
    pick_results = await asyncio.gather(*pick_tasks, return_exceptions=True)
    for gw, result in enumerate(pick_results, start=1):
        if isinstance(result, Exception):
            log.warning("Picks failed entry=%d gw=%d: %s", entry_id, gw, result)
            continue
        if isinstance(result, dict) and result.get("picks"):
            store.upsert_picks(entry_id, gw, result["picks"])

    log.info("Scraped %s (entry %d)", entry["entry_name"], entry_id)


async def scrape_player_history(fpl: FPLClient, store: Store, element_ids: list[int]) -> None:
    """Fetch /element-summary/ for each player. ~700 requests at full season."""
    log.info("Fetching per-GW history for %d players…", len(element_ids))
    sem_lock = asyncio.Lock()
    completed = 0

    async def one(pid: int) -> None:
        nonlocal completed
        data = await fpl.element_summary(pid)
        if isinstance(data, dict) and data.get("history"):
            async with sem_lock:
                store.upsert_player_history(pid, data["history"])
                completed += 1
                if completed % 50 == 0:
                    store.commit()
                    log.info("  player history: %d / %d", completed, len(element_ids))

    await asyncio.gather(*[one(pid) for pid in element_ids], return_exceptions=False)
    store.commit()
    log.info("Player history: %d / %d complete", completed, len(element_ids))


async def run_scrape(
    league_id: int,
    db_path: Path,
    concurrency: int,
    skip_player_history: bool = False,
    owned_only: bool = False,
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    store = Store(db_path)
    sem = asyncio.Semaphore(concurrency)

    async with httpx.AsyncClient(
        timeout=TIMEOUT,
        headers={"User-Agent": USER_AGENT},
    ) as client:
        fpl = FPLClient(client, sem)

        log.info("Fetching bootstrap + fixtures…")
        bootstrap, fixtures = await asyncio.gather(fpl.bootstrap(), fpl.fixtures())

        store.upsert_teams(bootstrap["teams"])
        store.upsert_players(bootstrap["elements"])
        store.upsert_gameweeks(bootstrap["events"])
        store.upsert_fixtures(fixtures)
        store.commit()

        current_event = next(
            (e["id"] for e in bootstrap["events"] if e["is_current"]),
            max((e["id"] for e in bootstrap["events"] if e["finished"]), default=0),
        )
        log.info("Current / last finished GW: %d", current_event)

        entries, league_name = await collect_league_entries(fpl, league_id)
        if league_name:
            store.upsert_league(league_id, league_name)
            store.commit()

        # Managers in batches so we commit incrementally.
        batch_size = 25
        for i in range(0, len(entries), batch_size):
            batch = entries[i:i + batch_size]
            await asyncio.gather(*[
                scrape_manager(fpl, store, e, league_id, current_event, now)
                for e in batch
            ])
            store.commit()
            log.info("Committed %d / %d managers",
                     min(i + batch_size, len(entries)), len(entries))

        # Per-player history (xG, bonus, minutes etc.)
        if not skip_player_history and current_event > 0:
            if owned_only:
                cur = store.conn.execute(
                    "SELECT DISTINCT element FROM manager_picks"
                )
                element_ids = [r[0] for r in cur.fetchall()]
                log.info("--owned-only: limiting to %d players", len(element_ids))
            else:
                element_ids = [p["id"] for p in bootstrap["elements"]]
            await scrape_player_history(fpl, store, element_ids)

    store.close()
    log.info("Done. DB → %s", db_path)
