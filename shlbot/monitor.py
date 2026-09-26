"""Bevakningsloopen: hämtar schema och live-data och skickar notiser till en "sink"."""

from __future__ import annotations

import asyncio
import json
import logging
from collections import Counter
from datetime import datetime, timedelta
from typing import Any, Protocol

from .api import NotAvailable, SHLClient, is_playoff
from .config import Config
from .formatting import EmbedSpec, render
from .models import GOAL, STOCKHOLM, GameInfo, parse_events, parse_status
from .stats import parse_team_stats
from .tracker import GameTracker, Notification

log = logging.getLogger(__name__)

# Hur länge före nedsläpp matchen börjar pollas
PRE_GAME_WINDOW = timedelta(minutes=10)
# Längsta tid en match kan pågå innan vi slutar polla den
MAX_GAME_LENGTH = timedelta(hours=6)


class Sink(Protocol):
    async def send(self, game: GameInfo, embeds: list[EmbedSpec]) -> list[int | None]:
        """Skickar meddelanden och returnerar deras id:n (ett per embed)."""
        ...

    async def edit(self, message_id: int, spec: EmbedSpec) -> bool:
        """Ersätter innehållet i ett tidigare meddelande. False om det inte gick."""
        ...


class ConsoleSink:
    def __init__(self) -> None:
        self._next_id = 0

    async def send(self, game: GameInfo, embeds: list[EmbedSpec]) -> list[int | None]:
        ids: list[int | None] = []
        for spec in embeds:
            print(spec.as_text(), end="\n\n", flush=True)
            self._next_id += 1
            ids.append(self._next_id)
        return ids

    async def edit(self, message_id: int, spec: EmbedSpec) -> bool:
        print(f"(meddelande {message_id} uppdaterat)\n{spec.as_text()}", end="\n\n", flush=True)
        return True


class Monitor:
    def __init__(self, config: Config, client: SHLClient, sink: Sink):
        self.config = config
        self.client = client
        self.sink = sink
        self.games: dict[str, GameInfo] = {}
        self.trackers: dict[str, GameTracker] = {}
        self.no_team_stats: set[str] = set()
        self.warned: set[str] = set()
        self.last_summary: dict[str, tuple] = {}
        self._schedule_at: datetime | None = None
        self._saved_state: dict[str, Any] = self._load_state()

    # -- state --------------------------------------------------------------

    def _load_state(self) -> dict[str, Any]:
        try:
            return json.loads(self.config.state_file.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as e:
            log.warning("Kunde inte läsa %s: %s", self.config.state_file, e)
            return {}

    def _save_state(self) -> None:
        cutoff = datetime.now(STOCKHOLM) - timedelta(days=2)
        data = {
            uuid: t.to_dict()
            for uuid, t in self.trackers.items()
            if not (t.game.start and t.game.start < cutoff)
        }
        tmp = self.config.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        tmp.replace(self.config.state_file)

    def tracker(self, game: GameInfo) -> GameTracker:
        t = self.trackers.get(game.uuid)
        if t is None:
            kwargs = {
                "shootout_period": 99 if is_playoff(game) else 5,
                "post_starting_goalies": self.config.post_starting_goalies,
            }
            saved = self._saved_state.get(game.uuid)
            t = GameTracker.from_dict(game, saved, **kwargs) if saved else GameTracker(game=game, **kwargs)
            self.trackers[game.uuid] = t
        t.game = game
        return t

    # -- schema -------------------------------------------------------------

    async def refresh_schedule(self, force: bool = False) -> None:
        now = datetime.now(STOCKHOLM)
        interval = timedelta(seconds=self.config.schedule_interval)
        # Tätare uppdatering när en match är nära eller pågår
        if any(self._is_active(g, now) for g in self.games.values()):
            interval = min(interval, timedelta(seconds=60))
        if not force and self._schedule_at and now - self._schedule_at < interval:
            return
        games = await self.client.schedule()
        self.games = {g.uuid: g for g in games if self.config.follows(g.home.code, g.away.code)}
        self._schedule_at = now

    def _is_active(self, g: GameInfo, now: datetime) -> bool:
        if not g.start:
            return False
        if g.start - now > PRE_GAME_WINDOW:
            return False
        if now - g.start > MAX_GAME_LENGTH:
            return False
        t = self.trackers.get(g.uuid)
        if t and t.final_posted:
            return False
        return True

    def active_games(self) -> list[GameInfo]:
        now = datetime.now(STOCKHOLM)
        return [g for g in self.games.values() if self._is_active(g, now)]

    def todays_games(self) -> list[GameInfo]:
        today = datetime.now(STOCKHOLM).date()
        return [g for g in self.games.values() if g.start and g.start.astimezone(STOCKHOLM).date() == today]

    # -- polling ------------------------------------------------------------

    async def poll_game(self, game: GameInfo) -> None:
        try:
            pbp = await self.client.play_by_play(game.uuid)
        except NotAvailable:
            if game.uuid not in self.warned:
                log.warning("%s: händelselistan (play-by-play) finns inte hos SHL (404)", game.title)
                self.warned.add(game.uuid)
            pbp = []
        try:
            overview = await self.client.overview(game.uuid)
        except (NotAvailable, RuntimeError) as e:
            log.debug("Ingen overview för %s: %s", game.uuid, e)
            overview = None

        team_stats = None
        if game.uuid not in self.no_team_stats:
            try:
                team_stats = parse_team_stats(await self.client.team_stats(game.uuid)) or None
            except (NotAvailable, RuntimeError):
                self.no_team_stats.add(game.uuid)

        events = parse_events(pbp, game)
        status = parse_status(overview, game)
        summary = (len(events), status.home_score, status.away_score, status.period)
        if self.last_summary.get(game.uuid) != summary:
            self.last_summary[game.uuid] = summary
            kinds = Counter(e.kind for e in events)
            log.info(
                "%s %s–%s (period %s %s): %d händelser %s",
                game.title,
                status.home_score,
                status.away_score,
                status.period,
                status.clock or "",
                len(events),
                dict(kinds),
            )
        t = self.tracker(game)
        notes = t.update(events, status, team_stats)
        for n in notes:
            await self._deliver(t, n)
        if notes:
            self._save_state()

    async def _deliver(self, t: GameTracker, n: Notification) -> None:
        # Rättelser (t.ex. assist som läggs till i efterhand) redigerar målmeddelandet
        if n.kind == "correction" and n.event and n.event.id in t.messages:
            updated = render(
                Notification("event", n.game, event=n.event, score=n.score, status=n.status), t.shootout_period
            )
            edited = await self.sink.edit(t.messages[n.event.id], updated)
            if edited and not n.extra.get("scorer_changed"):
                return
        ids = await self.sink.send(n.game, [render(n, t.shootout_period)])
        if n.kind == "event" and n.event and n.event.kind == GOAL and ids and ids[0]:
            t.messages[n.event.id] = ids[0]

    async def tick(self) -> None:
        try:
            await self.refresh_schedule()
        except Exception as e:  # nätverksfel ska inte stoppa bevakningen
            log.warning("Kunde inte hämta spelschemat: %s", e)
        games = self.active_games()
        results = await asyncio.gather(*(self.poll_game(g) for g in games), return_exceptions=True)
        for g, r in zip(games, results):
            if isinstance(r, Exception):
                log.warning("Fel vid uppdatering av %s: %s", g.title, r, exc_info=r)

    async def run_forever(self) -> None:
        log.info("Bevakar SHL var %s:e sekund", self.config.poll_interval)
        while True:
            started = asyncio.get_running_loop().time()
            await self.tick()
            elapsed = asyncio.get_running_loop().time() - started
            await asyncio.sleep(max(1.0, self.config.poll_interval - elapsed))
