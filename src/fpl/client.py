"""Async client for the public FPL API."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

BASE = "https://fantasy.premierleague.com/api"
TIMEOUT = 30.0
USER_AGENT = "fpl-league-scraper/0.2"

log = logging.getLogger(__name__)


class FPLClient:
    """Thin wrapper with concurrency cap, exponential backoff, and 404 → empty."""

    def __init__(self, client: httpx.AsyncClient, sem: asyncio.Semaphore):
        self.client = client
        self.sem = sem

    async def _get(self, path: str) -> Any:
        url = f"{BASE}{path}"
        async with self.sem:
            for attempt in range(4):
                try:
                    r = await self.client.get(url)
                    if r.status_code == 429:
                        wait = 2 ** attempt
                        log.warning("429 on %s, sleeping %ss", path, wait)
                        await asyncio.sleep(wait)
                        continue
                    if r.status_code == 404:
                        return {}
                    r.raise_for_status()
                    return r.json()
                except (httpx.HTTPError, httpx.TimeoutException) as e:
                    if attempt == 3:
                        raise
                    log.warning("Error on %s attempt %d: %s", path, attempt + 1, e)
                    await asyncio.sleep(2 ** attempt)
            raise RuntimeError("unreachable")

    async def bootstrap(self) -> dict:
        return await self._get("/bootstrap-static/")

    async def fixtures(self) -> list[dict]:
        return await self._get("/fixtures/")

    async def league_page(self, league_id: int, page: int) -> dict:
        return await self._get(
            f"/leagues-classic/{league_id}/standings/?page_standings={page}"
        )

    async def manager_history(self, entry_id: int) -> dict:
        return await self._get(f"/entry/{entry_id}/history/")

    async def entry(self, entry_id: int) -> dict:
        """Manager profile — works pre-season, before a league renews."""
        return await self._get(f"/entry/{entry_id}/")

    async def manager_picks(self, entry_id: int, event: int) -> dict:
        return await self._get(f"/entry/{entry_id}/event/{event}/picks/")

    async def manager_transfers(self, entry_id: int) -> list[dict]:
        return await self._get(f"/entry/{entry_id}/transfers/")

    async def element_summary(self, element_id: int) -> dict:
        return await self._get(f"/element-summary/{element_id}/")
