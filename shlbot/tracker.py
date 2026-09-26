"""Håller reda på en match och avgör vilka notiser som ska skickas.

Klassen är helt fri från Discord och nätverk så att den kan testas och
köras i "replay"-läge mot sparad data.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .models import (
    GOAL,
    GOALIE_IN,
    GOALIE_OUT,
    INJURY,
    PENALTY,
    PENALTY_SHOT,
    PERIOD_END,
    PERIOD_START,
    SHOOTOUT,
    Event,
    GameInfo,
    GameStatus,
    clock_seconds,
)
from .stats import GameStats, PeriodStats, compute

# Händelser som postas direkt när de dyker upp
INSTANT_KINDS = {GOAL, PENALTY, INJURY, PENALTY_SHOT, SHOOTOUT}

# Antal uppdateringar i rad ett postat mål måste saknas innan det räknas som bortdömt
MISSING_GOAL_THRESHOLD = 3


@dataclass
class Notification:
    kind: str  # start, starters, tracking, event, correction, disallowed, period, final
    game: GameInfo
    event: Event | None = None
    period: int | None = None
    stats: GameStats | None = None
    status: GameStatus | None = None
    score: tuple[int, int] | None = None
    events: list[Event] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class GameTracker:
    game: GameInfo
    shootout_period: int = 5
    post_starting_goalies: bool = True
    initialized: bool = False
    announced_start: bool = False
    final_posted: bool = False
    seen: dict[str, str] = field(default_factory=dict)  # event-id -> signatur
    seen_kind: dict[str, str] = field(default_factory=dict)
    missing: dict[str, int] = field(default_factory=dict)
    periods_posted: set[int] = field(default_factory=set)
    # Senast kända ställning, för mål-reserven när händelselistan saknar mål
    known_score: tuple[int, int] | None = None
    last_events: list[Event] = field(default_factory=list, repr=False)
    last_stats: GameStats | None = field(default=None, repr=False)
    last_status: GameStatus | None = field(default=None, repr=False)

    # -- persistens ---------------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "initialized": self.initialized,
            "announced_start": self.announced_start,
            "final_posted": self.final_posted,
            "seen": self.seen,
            "seen_kind": self.seen_kind,
            "periods_posted": sorted(self.periods_posted),
            "known_score": list(self.known_score) if self.known_score else None,
        }

    @classmethod
    def from_dict(cls, game: GameInfo, d: dict, **kwargs: Any) -> "GameTracker":
        t = cls(game=game, **kwargs)
        t.initialized = bool(d.get("initialized"))
        t.announced_start = bool(d.get("announced_start"))
        t.final_posted = bool(d.get("final_posted"))
        t.seen = dict(d.get("seen") or {})
        t.seen_kind = dict(d.get("seen_kind") or {})
        t.periods_posted = set(d.get("periods_posted") or [])
        t.known_score = tuple(d["known_score"]) if d.get("known_score") else None
        return t

    # -- hjälpare -----------------------------------------------------------

    def _score_after(self, events: list[Event], target: Event) -> tuple[int, int]:
        """Ställning efter ett mål. Använder SHL:s siffror om de finns."""
        if target.home_goals is not None and target.away_goals is not None:
            return target.home_goals, target.away_goals
        h = a = 0
        for e in events:
            if e.kind == GOAL and e.period < self.shootout_period:
                if e.side == "home":
                    h += 1
                elif e.side == "away":
                    a += 1
            if e.id == target.id:
                break
        return h, a

    def _score(self, events: list[Event], status: GameStatus | None) -> tuple[int, int]:
        h = sum(1 for e in events if e.kind == GOAL and e.side == "home" and e.period < self.shootout_period)
        a = sum(1 for e in events if e.kind == GOAL and e.side == "away" and e.period < self.shootout_period)
        if status and status.home_score is not None and status.away_score is not None:
            # Officiell ställning vinner (inkluderar t.ex. avgörande straffmål)
            return status.home_score, status.away_score
        return h, a

    def _remember_score(self, status: GameStatus) -> None:
        if status.home_score is not None and status.away_score is not None:
            self.known_score = (status.home_score, status.away_score)

    def _score_fallback(self, events: list[Event], status: GameStatus) -> list[Notification]:
        """Postar mål utifrån ställningen när händelselistan inte innehåller några mål alls.

        Skyddar mot att SHL:s händelselista saknas eller har ett okänt format.
        """
        previous = self.known_score
        self._remember_score(status)
        current = self.known_score
        if previous is None or current is None or any(e.kind == GOAL for e in events):
            return []
        notes = []
        h, a = previous
        while (h, a) != current and (h <= current[0] and a <= current[1]):
            if h < current[0]:
                h += 1
                side = "home"
            else:
                a += 1
                side = "away"
            notes.append(Notification("score_goal", self.game, score=(h, a), status=status, extra={"side": side}))
        return notes

    def _goalie_note(self, events: list[Event], i: int, status: GameStatus) -> Notification | str | None:
        """Avgör om en målvaktshändelse ska postas.

        Returnerar "starter" för startmålvakter, en notis för riktiga byten och
        tom kasse sent i matchen, annars None (t.ex. vid fördröjd utvisning).
        """
        e = events[i]
        previous = next(
            (x.player for x in reversed(events[:i]) if x.kind == GOALIE_IN and x.side == e.side),
            None,
        )
        if e.kind == GOALIE_IN:
            if previous is None:
                return "starter"
            if previous == e.player:
                return None  # tillbaka efter att ha lämnat kassen
            return Notification("event", self.game, event=e, status=status, extra={"replaced": previous})
        # GOALIE_OUT: ett byte (in-händelse samtidigt) postas via in-händelsen
        if any(x.kind == GOALIE_IN and x.side == e.side and x.game_seconds == e.game_seconds for x in events):
            return None
        late = e.period > 3 or (e.period == 3 and clock_seconds(e.time) >= 15 * 60)
        if late and e.period < self.shootout_period:
            return Notification("event", self.game, event=e, status=status)
        return None

    # -- huvudlogik ---------------------------------------------------------

    def update(
        self,
        events: list[Event],
        status: GameStatus,
        team_stats: dict[int, PeriodStats] | None = None,
    ) -> list[Notification]:
        out: list[Notification] = []
        game_over = status.phase == "final"
        stats = compute(events, team_stats, self.shootout_period, game_over=game_over)
        self.last_events, self.last_stats, self.last_status = events, stats, status
        ids = {e.id for e in events}

        # Första gången matchen ses: om den redan pågår, spola förbi gamla händelser
        if not self.initialized:
            self.initialized = True
            # Precis efter nedsläpp räknas matchen som ny, så att inget missas
            already_running = (
                any(e.game_seconds > 60 for e in events)
                or (status.period or 1) > 1
                or status.phase in ("intermission", "final")
            )
            if already_running:
                for e in events:
                    self.seen[e.id] = e.signature
                    self.seen_kind[e.id] = e.kind
                self.announced_start = True
                current = max((e.period for e in events), default=status.period or 1)
                self.periods_posted.update(range(1, current))
                if status.phase in ("intermission", "final"):
                    self.periods_posted.add(current)
                self._remember_score(status)
                if game_over:
                    self.final_posted = True
                    return out
                out.append(
                    Notification("tracking", self.game, stats=stats, status=status, score=self._score(events, status))
                )
                return out

        starters: list[Event] = []
        start_note: Notification | None = None
        if not self.announced_start and (events or status.phase != "pre"):
            self.announced_start = True
            start_note = Notification("start", self.game, status=status)
            out.append(start_note)

        for i, e in enumerate(events):
            if e.id not in self.seen:
                self.seen[e.id] = e.signature
                self.seen_kind[e.id] = e.kind
                if e.kind in (GOALIE_IN, GOALIE_OUT):
                    note = self._goalie_note(events, i, status)
                    if note == "starter":
                        starters.append(e)
                    elif note is not None:
                        out.append(note)
                elif e.kind in INSTANT_KINDS:
                    out.append(
                        Notification(
                            "event",
                            self.game,
                            event=e,
                            score=self._score_after(events, e) if e.kind == GOAL else self._score(events, status),
                            status=status,
                        )
                    )
                elif e.kind == PERIOD_END:
                    self._maybe_period(out, e.period, events, stats, status)
            elif e.kind == GOAL and self.seen[e.id] != e.signature:
                self.seen[e.id] = e.signature
                out.append(Notification("correction", self.game, event=e, score=self._score_after(events, e)))

        if starters and self.post_starting_goalies:
            if start_note is not None:
                start_note.events = starters
            else:
                out.insert(0, Notification("starters", self.game, events=starters))

        out.extend(self._score_fallback(events, status))

        # Mål som försvunnit ur flödet = bortdömda (efter några uppdateringar i rad)
        if events:
            for eid, kind in list(self.seen_kind.items()):
                if kind != GOAL:
                    continue
                if eid in ids:
                    self.missing.pop(eid, None)
                    continue
                self.missing[eid] = self.missing.get(eid, 0) + 1
                if self.missing[eid] >= MISSING_GOAL_THRESHOLD:
                    self.missing.pop(eid)
                    self.seen.pop(eid, None)
                    self.seen_kind.pop(eid, None)
                    out.append(
                        Notification("disallowed", self.game, score=self._score(events, status), status=status)
                    )

        # Periodsammanfattning: via overview-paus, eller när nästa period har startat
        max_period = max((e.period for e in events), default=0)
        started_periods = {e.period for e in events if e.kind == PERIOD_START}
        for p in range(1, max_period):
            if p + 1 in started_periods or any(e.period > p and e.kind != PERIOD_END for e in events):
                self._maybe_period(out, p, events, stats, status)
        if status.phase == "intermission" and status.period:
            self._maybe_period(out, status.period, events, stats, status)

        if game_over and not self.final_posted:
            last = max((p for p in stats.periods if p < self.shootout_period), default=3)
            self._maybe_period(out, last, events, stats, status)
            self.final_posted = True
            out.append(
                Notification(
                    "final",
                    self.game,
                    stats=stats,
                    status=status,
                    score=self._score(events, status),
                    events=events,
                )
            )
        return out

    def _maybe_period(
        self,
        out: list[Notification],
        period: int,
        events: list[Event],
        stats: GameStats,
        status: GameStatus,
    ) -> None:
        if period in self.periods_posted or period < 1 or period >= self.shootout_period:
            return
        self.periods_posted.add(period)
        upto = [e for e in events if e.period <= period]
        out.append(
            Notification(
                "period",
                self.game,
                period=period,
                stats=stats,
                status=status,
                score=self._score(upto, None),
                events=upto,
            )
        )
