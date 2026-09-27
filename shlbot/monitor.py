"""Bevakningsloopen: hämtar schema och live-data och skickar notiser till en "sink"."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import Counter
from datetime import datetime, timedelta
from typing import Any, Protocol

from .api import NotAvailable, SHLClient, is_playoff
from .config import Config
from .formatting import EmbedSpec, render
from .models import GOAL, STOCKHOLM, GameInfo, GameStatus, parse_events, parse_status
from .stats import GoalieLine, parse_team_stats
from .swehockey import SweGame, SweHockeyClient, _compact
from .tracker import GameTracker, Notification

log = logging.getLogger(__name__)

# Hur länge före nedsläpp matchen börjar pollas
PRE_GAME_WINDOW = timedelta(minutes=10)
# Hur länge swehockey ska ha visat paus/slut innan sammanfattningen postas (sekunder),
# så att SHL:s händelser hinner ikapp
SWE_PAUSE_DELAY = 180
SWE_FINAL_DELAY = 300
# Laguppställningar kontrolleras från 90 min före till 30 min efter nedsläpp, varannan minut
LINEUP_BEFORE_START = 90 * 60
LINEUP_AFTER_START = 30 * 60
LINEUP_RETRY = 120
MIN_LINEUP = 12  # färre spelare = uppställningen är inte publicerad än
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
    def __init__(self, config: Config, client: SHLClient, sink: Sink, swe: SweHockeyClient | None = None):
        self.config = config
        self.client = client
        self.sink = sink
        self.swe = swe
        # stats.swehockey.se: match-id per SHL-match, senaste data och tidpunkter för paus/slut
        self.swe_ids: dict[str, int | None] = {}
        self.swe_lookup_at: dict[str, float] = {}
        self.swe_last: dict[str, SweGame] = {}
        self.swe_since: dict[tuple[str, str], float] = {}
        self.lineup_at: dict[str, float] = {}
        self._lineup_task: asyncio.Task | None = None
        self.games: dict[str, GameInfo] = {}
        self.trackers: dict[str, GameTracker] = {}
        self.no_team_stats: set[str] = set()
        self.warned: set[str] = set()
        self.last_summary: dict[str, tuple] = {}
        self._schedule_at: datetime | None = None
        self._schedule_task: asyncio.Task | None = None
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
        # Händelser och matchstatus hämtas samtidigt för att spara tid
        pbp_res, ov_res, swe = await asyncio.gather(
            self.client.play_by_play(game.uuid),
            self.client.overview(game.uuid),
            self.swe_game(game),
            return_exceptions=True,
        )
        if isinstance(swe, BaseException):
            swe = None
        if isinstance(pbp_res, NotAvailable):
            if game.uuid not in self.warned:
                log.warning("%s: händelselistan (play-by-play) finns inte hos SHL (404)", game.title)
                self.warned.add(game.uuid)
            pbp = []
        elif isinstance(pbp_res, BaseException):
            raise pbp_res
        else:
            pbp = pbp_res
        if isinstance(ov_res, BaseException):
            log.debug("Ingen overview för %s: %s", game.uuid, ov_res)
            overview = None
        else:
            overview = ov_res

        team_stats = None
        if game.uuid not in self.no_team_stats:
            try:
                team_stats = parse_team_stats(await self.client.team_stats(game.uuid)) or None
            except (NotAvailable, RuntimeError):
                self.no_team_stats.add(game.uuid)

        events = parse_events(pbp, game)
        status = self.merge_status(game, parse_status(overview, game), swe)
        summary = (len(events), status.home_score, status.away_score, status.period, status.phase)
        if self.last_summary.get(game.uuid) != summary:
            self.last_summary[game.uuid] = summary
            kinds = Counter(e.kind for e in events)
            log.info(
                "%s %s–%s (period %s %s, %s): %d händelser %s%s",
                game.title,
                status.home_score,
                status.away_score,
                status.period,
                status.clock or "",
                status.phase,
                len(events),
                dict(kinds),
                f" [swehockey: {swe.state_text or ''} {swe.clock or ''}]" if swe else " [swehockey: ingen data]"
                if self.swe
                else "",
            )
        t = self.tracker(game)
        notes = t.update(
            events,
            status,
            team_stats,
            official_goalies=self.official_goalies(game, t, swe),
            official_periods={"shots": swe.shots, "saves": swe.saves} if swe and swe.shots else None,
        )
        for n in notes:
            # Ett misslyckat meddelande får inte stoppa resten
            try:
                await self._deliver(t, n)
            except Exception:
                log.exception("%s: kunde inte posta notis av typen %s", game.title, n.kind)
        if notes:
            self._save_state()

    # -- stats.swehockey.se ---------------------------------------------------

    async def swe_id(self, game: GameInfo) -> int | None:
        """Matchens id på stats.swehockey.se (söks upp högst varannan minut tills den hittas)."""
        if self.swe is None or not game.start:
            return None
        now = time.monotonic()
        if self.swe_ids.get(game.uuid) is None:
            if now - self.swe_lookup_at.get(game.uuid, -1e9) < 120:
                return None
            self.swe_lookup_at[game.uuid] = now
            try:
                day = game.start.astimezone(STOCKHOLM).date()
                self.swe_ids[game.uuid] = await self.swe.find_game_id(day, game.home.name, game.away.name)
            except Exception as e:
                log.warning("%s: kunde inte nå stats.swehockey.se: %s", game.title, e)
                return None
            if self.swe_ids[game.uuid] is None:
                log.warning("%s: hittar inte matchen på stats.swehockey.se", game.title)
                return None
            log.info("%s: följer även stats.swehockey.se (match %s)", game.title, self.swe_ids[game.uuid])
        return self.swe_ids.get(game.uuid)

    async def swe_game(self, game: GameInfo) -> SweGame | None:
        if await self.swe_id(game) is None:
            return None
        try:
            data = await self.swe.game(self.swe_ids[game.uuid])  # type: ignore[arg-type,union-attr]
        except Exception as e:
            log.debug("%s: swehockey-fel: %s", game.title, e)
            return None
        self.swe_last[game.uuid] = data
        return data

    # -- laguppställningar -----------------------------------------------------

    def previous_game(self, game: GameInfo, code: str) -> GameInfo | None:
        earlier = [
            g
            for g in self.games.values()
            if g.uuid != game.uuid and g.start and game.start and g.start < game.start
            and code in (g.home.code, g.away.code)
        ]
        return max(earlier, key=lambda g: g.start, default=None)  # type: ignore[arg-type,return-value]

    async def _lineup(self, game: GameInfo) -> dict[str, dict[str, str]] | None:
        game_id = await self.swe_id(game)
        if game_id is None or self.swe is None:
            return None
        return await self.swe.lineup(game_id, [game.home.name, game.home.code], [game.away.name, game.away.code])

    async def check_lineups(self, game: GameInfo) -> None:
        """Postar vilka spelare som saknas (och är nya) jämfört med lagets förra match."""
        t = self.tracker(game)
        sides = [
            side
            for side, team in (("home", game.home), ("away", game.away))
            if side not in t.lineup_done and self.config.follows(team.code)
        ]
        if not sides or self.swe is None:
            return
        now = time.monotonic()
        if now - self.lineup_at.get(game.uuid, -1e9) < LINEUP_RETRY:
            return
        self.lineup_at[game.uuid] = now
        current = await self._lineup(game)
        if not current:
            return
        for side in sides:
            team = game.home if side == "home" else game.away
            if len(current[side]) < MIN_LINEUP:
                continue  # inte publicerad än
            prev = self.previous_game(game, team.code)
            prev_lineup = await self._lineup(prev) if prev else None
            prev_side = "home" if prev and prev.home.code == team.code else "away"
            t.lineup_done.append(side)
            if not prev or not prev_lineup or len(prev_lineup[prev_side]) < MIN_LINEUP:
                log.info("%s: ingen tidigare uppställning att jämföra med för %s", game.title, team.code)
                continue
            before, now_players = prev_lineup[prev_side], current[side]
            opponent = prev.away if prev_side == "home" else prev.home
            note = Notification(
                "lineup",
                game,
                extra={
                    "team": team,
                    "missing": [before[k] for k in before if k not in now_players],
                    "new": [now_players[k] for k in now_players if k not in before],
                    "previous": (
                        f"mot {opponent.name} {prev.start.astimezone(STOCKHOLM).day}/{prev.start.astimezone(STOCKHOLM).month}"
                        if prev.start
                        else ""
                    ),
                },
            )
            try:
                await self._deliver(t, note)
            except Exception:
                log.exception("%s: kunde inte posta uppställning", game.title)
        self._save_state()

    def official_goalies(self, game: GameInfo, t: GameTracker, swe: SweGame | None) -> list[GoalieLine] | None:
        """Målvaktsstatistik från swehockey (officiell), med rätt lag kopplat till varje målvakt."""
        if swe is None or not swe.goalies:
            return None
        shl_goalies = t.last_stats.goalies if t.last_stats else []
        order = list(dict.fromkeys(g.team for g in swe.goalies))
        lines = []
        for g in swe.goalies:
            last = g.name.split()[-1].lower()
            side = next((x.side for x in shl_goalies if last in x.name.lower()), None)
            if side is None:
                code = _compact(g.team)[:3]
                if code and code in (game.home.code.lower()[:3], _compact(game.home.name)[:3]):
                    side = "home"
                elif code and code in (game.away.code.lower()[:3], _compact(game.away.name)[:3]):
                    side = "away"
                else:
                    side = "home" if order.index(g.team) == 0 else "away"
            lines.append(GoalieLine(name=g.name, side=side, shots_against=g.shots, goals_against=g.shots - g.saves))
        return lines

    def merge_status(self, game: GameInfo, shl: GameStatus, swe: SweGame | None) -> GameStatus:
        """Kombinerar SHL:s status med swehockeys, som oftast ligger före.

        Ställningen tas från den källa som ligger längst fram. Paus och slut från
        swehockey används först efter en stund, så att SHL:s händelser (som
        statistiken bygger på) hinner ikapp innan sammanfattningen postas.
        """
        if swe is None or swe.home_score is None:
            return shl
        now = time.monotonic()

        def since(key: str, active: bool) -> float:
            k = (game.uuid, key)
            if not active:
                self.swe_since.pop(k, None)
                return 0.0
            return now - self.swe_since.setdefault(k, now)

        final_for = since("final", swe.final)
        pause_for = since(f"pause{swe.period}", swe.intermission)
        phase = shl.phase
        if shl.phase != "final":
            if swe.final and final_for >= SWE_FINAL_DELAY:
                phase = "final"
            elif swe.intermission and pause_for >= SWE_PAUSE_DELAY:
                phase = "intermission"
            elif shl.phase == "pre" and (swe.period or swe.final):
                phase = "live"
        hs = max(x for x in (shl.home_score, swe.home_score) if x is not None)
        as_ = max(x for x in (shl.away_score, swe.away_score) if x is not None)
        return GameStatus(
            phase=phase,
            period=swe.period or shl.period,
            clock=swe.clock or shl.clock,
            home_score=hs,
            away_score=as_,
        )

    async def _deliver(self, t: GameTracker, n: Notification) -> None:
        # Mål från ställningsändring: lägg till målskytt från swehockey om den finns
        if n.kind == "score_goal" and n.score:
            swe = self.swe_last.get(n.game.uuid)
            goal = swe.goal_with_score(*n.score) if swe else None
            if goal:
                n.extra["swe_goal"] = goal
        # Rättelser (t.ex. assist som läggs till i efterhand) redigerar målmeddelandet
        if n.kind == "correction" and n.event and n.event.id in t.messages:
            updated = render(
                Notification("event", n.game, event=n.event, score=n.score, status=n.status), t.shootout_period
            )
            edited = await self.sink.edit(t.messages[n.event.id], updated)
            if edited and not n.extra.get("scorer_changed"):
                return
        spec = render(n, t.shootout_period)
        # Mål som redan postats utifrån ställningen: redigera det meddelandet med detaljerna
        replace = n.extra.get("replace")
        if replace and replace in t.messages and n.event:
            if await self.sink.edit(t.messages[replace], spec):
                log.info("Uppdaterar mål med målskytt: %s", spec.title)
                t.messages[n.event.id] = t.messages.pop(replace)
                return
        where = f" (händelse {n.event.period}:{n.event.time})" if n.event else ""
        log.info("Postar i Discord: %s%s", spec.title, where)
        ids = await self.sink.send(n.game, [spec])
        if n.kind == "event" and n.event and n.event.kind == GOAL and ids and ids[0]:
            t.messages[n.event.id] = ids[0]
        if n.kind == "score_goal" and ids and ids[0]:
            t.messages[n.extra["key"]] = ids[0]

    async def _refresh_schedule_safe(self) -> None:
        try:
            await self.refresh_schedule()
        except Exception as e:  # nätverksfel ska inte stoppa bevakningen
            log.warning("Kunde inte hämta spelschemat: %s", e)

    async def tick(self) -> None:
        # Schemat hämtas i bakgrunden så att live-uppdateringarna inte behöver vänta på det
        if not self.games:
            await self._refresh_schedule_safe()
        elif self._schedule_task is None or self._schedule_task.done():
            self._schedule_task = asyncio.create_task(self._refresh_schedule_safe())
        games = self.active_games()
        results = await asyncio.gather(
            *(asyncio.wait_for(self.poll_game(g), timeout=25) for g in games), return_exceptions=True
        )
        for g, r in zip(games, results):
            if isinstance(r, Exception):
                log.warning("Fel vid uppdatering av %s: %s", g.title, r, exc_info=r)
        # Laguppställningar i bakgrunden så att live-uppdateringarna inte väntar
        if self.swe is not None and (self._lineup_task is None or self._lineup_task.done()):
            self._lineup_task = asyncio.create_task(self.check_upcoming_lineups())

    async def check_upcoming_lineups(self) -> None:
        now = datetime.now(STOCKHOLM)
        upcoming = [
            g
            for g in self.games.values()
            if g.start and -LINEUP_AFTER_START < (g.start - now).total_seconds() < LINEUP_BEFORE_START
        ]
        for g in upcoming:
            try:
                await self.check_lineups(g)
            except Exception as e:
                log.warning("%s: kunde inte läsa laguppställningen: %s", g.title, e)

    async def run_forever(self) -> None:
        log.info("Bevakar SHL var %s:e sekund", self.config.poll_interval)
        while True:
            started = asyncio.get_running_loop().time()
            await self.tick()
            elapsed = asyncio.get_running_loop().time() - started
            if elapsed > self.config.poll_interval:
                log.warning("Uppdateringen tog %.1f sekunder (SHL svarar långsamt)", elapsed)
            await asyncio.sleep(max(1.0, self.config.poll_interval - elapsed))
