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
    """Return (entries, league_name).

    Before a ball is kicked the standings list is empty and every member sits
    under ``new_entries`` instead, with the manager's name split across two
    fields. Both shapes are normalised to ``entry`` / ``entry_name`` /
    ``player_name`` so the rest of the pipeline doesn't care which it came from.
    """
    entries: list[dict] = []
    league_name = ""
    seen: set[int] = set()
    page = 1
    while True:
        data = await fpl.league_page(league_id, page)
        if not data or "standings" not in data:
            # League may not exist yet — mini-leagues renew close to GW1.
            log.warning("League %d has no standings yet (pre-season?) — "
                        "scraping bootstrap/fixtures only", league_id)
            return entries, league_name
        if not league_name:
            league_name = data.get("league", {}).get("name", "")

        standings = data["standings"]
        for row in standings.get("results", []):
            if row.get("entry") and row["entry"] not in seen:
                seen.add(row["entry"])
                entries.append(row)

        # Pre-season members, only present on the first page.
        for row in data.get("new_entries", {}).get("results", []):
            entry_id = row.get("entry")
            if not entry_id or entry_id in seen:
                continue
            seen.add(entry_id)
            name = " ".join(filter(None, [row.get("player_first_name"),
                                          row.get("player_last_name")])).strip()
            entries.append({
                "entry": entry_id,
                "entry_name": row.get("entry_name") or f"Entry {entry_id}",
                "player_name": name or row.get("player_name") or "Unknown",
            })

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


async def resolve_entries(fpl: FPLClient, entry_ids: list[int]) -> list[dict]:
    """Look up managers by entry ID via /entry/<id>/.

    Pre-season the league endpoint 404s until the mini-league renews, so this is
    the only way to pick up new joiners. Returns league-shaped dicts so they can
    flow through scrape_manager unchanged.
    """
    results = await asyncio.gather(*[fpl.entry(e) for e in entry_ids],
                                   return_exceptions=True)
    entries = []
    for entry_id, data in zip(entry_ids, results):
        if isinstance(data, Exception) or not isinstance(data, dict) or not data:
            log.warning("Entry %d not found or failed: %s", entry_id,
                        data if isinstance(data, Exception) else "empty response")
            continue
        name = " ".join(filter(None, [data.get("player_first_name"),
                                      data.get("player_last_name")])).strip()
        entries.append({
            "entry": data.get("id", entry_id),
            "entry_name": data.get("name") or f"Entry {entry_id}",
            "player_name": name or "Unknown",
        })
        log.info("Resolved entry %d: %s (%s)", entry_id,
                 entries[-1]["entry_name"], entries[-1]["player_name"])
    return entries


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
    extra_entries: list[int] | None = None,
    force_unsealed: bool = False,
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    store = Store(db_path, force_unsealed=force_unsealed)
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

        # Managers named explicitly by ID — new joiners before the league
        # renews, or anyone the standings page misses.
        if extra_entries:
            known = {e["entry"] for e in entries}
            wanted = [e for e in extra_entries if e not in known]
            if wanted:
                entries.extend(await resolve_entries(fpl, wanted))
            log.info("Added %d manager(s) by entry ID", len(wanted))

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

        # Anyone still carrying this league_id who wasn't in the standings has
        # left. Guarded on a non-empty enumeration so a 404 or a pre-season
        # empty page can't wipe the roster.
        if entries:
            departed = store.mark_departed(
                league_id, [e["entry"] for e in entries], now)
            store.commit()
            for who in departed:
                log.info("Left the league: %s", who)

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
