"""HTTP-klient mot SHL:s (inofficiella) webb-API på shl.se."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import aiohttp

from .config import Config
from .models import GameInfo

log = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; SHL-Discord-bot/1.0)",
    "Accept": "application/json",
}


class NotAvailable(Exception):
    """Endpointen finns inte (404) – används för valfri data som lagstatistik."""


class SHLClient:
    def __init__(self, config: Config, session: aiohttp.ClientSession | None = None):
        self.config = config
        self._session = session
        self._own_session = session is None
        self._filters: dict | None = None
        self._filters_at = 0.0

    async def __aenter__(self) -> "SHLClient":
        if self._session is None:
            self._session = aiohttp.ClientSession(headers=HEADERS, timeout=aiohttp.ClientTimeout(total=15))
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    async def close(self) -> None:
        if self._own_session and self._session is not None:
            await self._session.close()
            self._session = None

    def url(self, path: str, uuid: str | None = None) -> str:
        return self.config.base_url + (path.format(uuid=uuid) if uuid else path)

    async def get_json(self, url: str, params: dict | None = None, retries: int = 2) -> Any:
        assert self._session is not None, "använd 'async with SHLClient(...)'"
        last: Exception | None = None
        for attempt in range(retries + 1):
            try:
                async with self._session.get(url, params=params) as resp:
                    if resp.status == 404:
                        raise NotAvailable(url)
                    resp.raise_for_status()
                    return await resp.json(content_type=None)
            except NotAvailable:
                raise
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as e:
                last = e
                if attempt < retries:
                    await asyncio.sleep(1.5 * (attempt + 1))
        raise RuntimeError(f"Kunde inte hämta {url}: {last}")

    # -- spelschema ---------------------------------------------------------

    async def filters(self) -> dict:
        if self._filters is None or time.monotonic() - self._filters_at > 6 * 3600:
            self._filters = await self.get_json(self.url("/api/sports-v2/season-series-game-types-filter"))
            self._filters_at = time.monotonic()
        return self._filters or {}

    async def schedule(self) -> list[GameInfo]:
        """Alla matcher i aktuell säsong för vald serie (grundserie + slutspel m.m.)."""
        f = await self.filters()
        default = f.get("defaultSsgtFilter") or {}
        season = default.get("season")
        series = next(
            (s.get("uuid") for s in f.get("series") or [] if str(s.get("code", "")).upper() == self.config.series_code),
            default.get("series"),
        )
        game_types = [g for g in f.get("gameType") or [] if g.get("uuid")] or [{"uuid": default.get("gameType")}]
        if not (season and series and all(g.get("uuid") for g in game_types)):
            raise RuntimeError("SHL:s API gav ingen aktuell säsong/serie")

        games: dict[str, GameInfo] = {}
        for gt in game_types:
            data = await self.get_json(
                self.url("/api/sports-v2/game-schedule"),
                params={
                    "seasonUuid": season,
                    "seriesUuid": series,
                    "gameTypeUuid": gt["uuid"],
                    "gamePlace": "all",
                    "played": "all",
                },
            )
            gt_name = " ".join(str(gt.get(k, "")) for k in ("code", "name", "names", "displayName")).lower()
            for raw in (data or {}).get("gameInfo") or []:
                g = GameInfo.parse(raw)
                g.raw.setdefault("_gameTypeName", gt_name)
                games[g.uuid] = g
        return sorted(games.values(), key=lambda g: (g.start is None, g.start))

    # -- live-data ----------------------------------------------------------

    async def overview(self, uuid: str) -> Any:
        return await self.get_json(self.url(self.config.overview_path, uuid))

    async def play_by_play(self, uuid: str) -> Any:
        return await self.get_json(self.url(self.config.pbp_path, uuid))

    async def team_stats(self, uuid: str) -> Any:
        if not self.config.team_stats_path:
            raise NotAvailable("avstängd")
        return await self.get_json(self.url(self.config.team_stats_path, uuid), retries=0)


def is_playoff(game: GameInfo) -> bool:
    text = " ".join(
        str(game.raw.get(k, "")) for k in ("_gameTypeName", "gameType", "gameTypeInfo", "seriesInfo")
    ).lower()
    return any(k in text for k in ("playoff", "slutspel", "kvartsfinal", "semifinal", "final", "kval"))
