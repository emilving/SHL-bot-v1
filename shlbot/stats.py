"""Beräkning av period-, match- och målvaktsstatistik.

Grunden räknas fram ur play-by-play (mål, skott, räddningar, utvisningar,
PP/PK). Om SHL:s lagstatistik-endpoint svarar används dess siffror i första
hand, och den kan också bidra med sådant som inte finns i play-by-play
(t.ex. tekningar och tacklingar).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .models import (
    AWAY,
    FACEOFF,
    GOAL,
    GOALIE_IN,
    GOALIE_OUT,
    HOME,
    PENALTY,
    SHOT,
    Event,
    as_int,
    first,
)

SIDES = (HOME, AWAY)

# Nycklar i TeamLine som kan skrivas över av lagstatistik-endpointen
STAT_ALIASES: dict[str, tuple[str, ...]] = {
    "goals": ("goals", "g", "mål"),
    "shots": ("shotsongoal", "sog", "shots", "skott", "skottpåmål"),
    "saves": ("saves", "sv", "räddningar"),
    "pim": ("pim", "penaltyminutes", "utvisningsminuter", "utvisningsmin"),
    "pp_goals": ("ppg", "powerplaygoals", "ppgoals"),
    "pp_opps": ("ppo", "powerplayopportunities", "ppopportunities", "powerplays", "ppchanser"),
    "sh_goals": ("shg", "shorthandedgoals", "shgoals", "boxplaygoals"),
    "faceoffs_won": ("faceoffswon", "fow", "faceoffs", "tekningarvunna", "wonfaceoffs", "tekningar"),
    "hits": ("hits", "tacklingar"),
    "blocked": ("blockedshots", "blocked", "blocks", "blockeradeskott"),
    "shots_missed": ("shotsmissed", "missedshots", "shotswide", "skottutanför"),
    "giveaways": ("giveaways", "turnovers"),
    "takeaways": ("takeaways",),
}


@dataclass
class TeamLine:
    goals: int = 0
    shots: int | None = 0
    saves: int | None = 0
    pim: int = 0
    pp_goals: int = 0
    pp_opps: int = 0
    sh_goals: int = 0
    en_goals: int = 0
    faceoffs_won: int | None = None
    hits: int | None = None
    blocked: int | None = None
    shots_missed: int | None = None
    giveaways: int | None = None
    takeaways: int | None = None

    def add(self, other: "TeamLine") -> None:
        for name in self.__dataclass_fields__:
            a, b = getattr(self, name), getattr(other, name)
            if a is None and b is None:
                continue
            setattr(self, name, (a or 0) + (b or 0))


@dataclass
class PeriodStats:
    home: TeamLine = field(default_factory=TeamLine)
    away: TeamLine = field(default_factory=TeamLine)

    def side(self, side: str) -> TeamLine:
        return self.home if side == HOME else self.away


@dataclass
class GoalieLine:
    name: str
    side: str
    shots_against: int = 0
    goals_against: int = 0
    seconds: int = 0
    # Tider i kassen (sekunder från matchstart), för att fördela officiella räddningar
    intervals: list[tuple[int, int]] = field(default_factory=list)

    @property
    def saves(self) -> int:
        return self.shots_against - self.goals_against

    @property
    def save_pct(self) -> float | None:
        if not self.shots_against:
            return None
        return self.saves / self.shots_against


@dataclass
class GameStats:
    periods: dict[int, PeriodStats]
    goalies: list[GoalieLine]
    has_faceoffs: bool = False
    # Officiella skott/räddningar för matchen hittills (från målvaktsstatistiken), per lag
    official_shots: dict[str, int] | None = None
    official_saves: dict[str, int] | None = None

    def total(self, upto: int | None = None) -> PeriodStats:
        tot = PeriodStats()
        for p, st in sorted(self.periods.items()):
            if upto is not None and p > upto:
                continue
            tot.home.add(st.home)
            tot.away.add(st.away)
        last = max(self.periods, default=0)
        if self.official_shots and (upto is None or upto >= last):
            for side in SIDES:
                tot.side(side).shots = self.official_shots[side]
                tot.side(side).saves = (self.official_saves or {}).get(side, tot.side(side).saves)
        return tot


def other(side: str) -> str:
    return AWAY if side == HOME else HOME


def _is_scoring_period(p: int, shootout_period: int) -> bool:
    return p < shootout_period


def compute(
    events: list[Event],
    team_stats: dict[int, PeriodStats] | None = None,
    shootout_period: int = 5,
    game_over: bool = False,
    official_goalies: list[GoalieLine] | None = None,
    official_periods: dict[str, dict[str, list[int]]] | None = None,
) -> GameStats:
    """Räknar fram statistik ur händelserna.

    `shootout_period` är den period som räknas som straffläggning (5 i
    grundserien). I slutspel används ett högt värde så att alla
    förlängningar räknas som vanliga perioder.
    """
    periods: dict[int, PeriodStats] = {}

    def ps(p: int) -> PeriodStats:
        return periods.setdefault(p, PeriodStats())

    shot_events_include_goals = any(e.kind == SHOT and e.shot_is_goal for e in events)
    has_faceoffs = False

    # Utvisningar grupperade per tidpunkt för att hitta kvittningar
    penalties_at: dict[tuple[int, str], dict[str, int]] = {}

    for e in events:
        if e.side not in SIDES:
            if e.period < shootout_period:
                ps(e.period)
            continue
        if not _is_scoring_period(e.period, shootout_period):
            continue
        line = ps(e.period).side(e.side)
        if e.kind == GOAL:
            line.goals += 1
            line.shots += 1
            if e.is_pp:
                line.pp_goals += 1
            elif e.is_sh:
                line.sh_goals += 1
            if e.is_empty_net:
                line.en_goals += 1
        elif e.kind == SHOT:
            if e.on_goal and not (shot_events_include_goals and e.shot_is_goal):
                line.shots += 1
        elif e.kind == PENALTY:
            mins = e.penalty_minutes or 0
            line.pim += mins
            if mins in (2, 4, 5):
                penalties_at.setdefault((e.period, e.time), {HOME: 0, AWAY: 0})[e.side] += 1
        elif e.kind == FACEOFF:
            has_faceoffs = True
            line.faceoffs_won = (line.faceoffs_won or 0) + 1

    # PP-chanser: motståndarens mindre/större straff som inte kvittas samtidigt
    for (period, _), counts in penalties_at.items():
        diff = counts[HOME] - counts[AWAY]
        if diff > 0:
            ps(period).away.pp_opps += diff
        elif diff < 0:
            ps(period).home.pp_opps += -diff

    for st in periods.values():
        st.home.saves = st.away.shots - st.away.goals
        st.away.saves = st.home.shots - st.home.goals

    if team_stats:
        for p, override in team_stats.items():
            if p >= shootout_period:
                continue
            target = ps(p)
            for side in SIDES:
                src, dst = override.side(side), target.side(side)
                for name in dst.__dataclass_fields__:
                    v = getattr(src, name)
                    if v is not None:
                        setattr(dst, name, v)
            for side in SIDES:
                if override.side(side).saves is None:
                    opp = target.side(other(side))
                    target.side(side).saves = opp.shots - opp.goals
            if any(override.side(s).faceoffs_won is not None for s in SIDES):
                has_faceoffs = True

    result = GameStats(
        periods=periods,
        goalies=compute_goalies(events, shootout_period, game_over),
        has_faceoffs=has_faceoffs,
    )
    shots_by_period = (official_periods or {}).get("shots") or {}
    if all(shots_by_period.get(side) for side in SIDES):
        # Officiella skott och räddningar per period (swehockeys sidhuvud)
        saves_by_period = (official_periods or {}).get("saves") or {}
        for side in SIDES:
            for i, n in enumerate(shots_by_period[side]):
                if i + 1 >= shootout_period:
                    break
                ps(i + 1).side(side).shots = n
            for i, n in enumerate(saves_by_period.get(side) or []):
                if i + 1 >= shootout_period:
                    break
                ps(i + 1).side(side).saves = n
        result.periods = periods
        result.official_shots = {side: sum(st.side(side).shots or 0 for st in periods.values()) for side in SIDES}
        result.official_saves = {side: sum(st.side(side).saves or 0 for st in periods.values()) for side in SIDES}
        if official_goalies:
            result.goalies = official_goalies
        else:
            last = max((e.game_seconds for e in events if e.period < shootout_period), default=0)
            result.goalies = apply_official_saves(result.goalies, saves_by_period, last)
    elif official_goalies:
        # Officiell målvaktsstatistik: lagets skott på mål = motståndarmålvakternas
        # skott mot + mål i tom kasse
        result.goalies = official_goalies
        en = {side: sum(st.side(side).en_goals for st in periods.values()) for side in SIDES}
        result.official_shots = {
            side: sum(g.shots_against for g in official_goalies if g.side == other(side)) + en[side]
            for side in SIDES
        }
        result.official_saves = {side: sum(g.saves for g in official_goalies if g.side == side) for side in SIDES}
        # Skott per period från SHL:s lista visas bara om de stämmer med de officiella siffrorna
        derived = {side: sum(st.side(side).shots or 0 for st in periods.values()) for side in SIDES}
        if derived != result.official_shots:
            for st in periods.values():
                for side in SIDES:
                    st.side(side).shots = None
                    st.side(side).saves = None
    return result


def compute_goalies(events: list[Event], shootout_period: int = 5, game_over: bool = False) -> list[GoalieLine]:
    """Målvaktsstatistik: skott och mål mot den målvakt som stod i kassen."""
    lines: dict[tuple[str, str], GoalieLine] = {}
    in_net: dict[str, str | None] = {HOME: None, AWAY: None}
    entered_at: dict[str, int] = {}
    shot_events_include_goals = any(e.kind == SHOT and e.shot_is_goal for e in events)
    last_second = 0

    def line(side: str, name: str) -> GoalieLine:
        return lines.setdefault((side, name), GoalieLine(name=name, side=side))

    def leave(side: str, at: int) -> None:
        name = in_net.get(side)
        if name:
            start = entered_at.get(side, at)
            line(side, name).seconds += max(0, at - start)
            if at > start:
                line(side, name).intervals.append((start, at))
        in_net[side] = None

    for e in events:
        if e.period >= shootout_period:
            continue
        last_second = max(last_second, e.game_seconds)
        if e.kind == GOALIE_IN and e.side in SIDES and e.player:
            leave(e.side, e.game_seconds)
            in_net[e.side] = e.player
            entered_at[e.side] = e.game_seconds
            line(e.side, e.player)
        elif e.kind == GOALIE_OUT and e.side in SIDES:
            if e.player is None or in_net.get(e.side) == e.player:
                leave(e.side, e.game_seconds)
        elif e.kind in (GOAL, SHOT) and e.side in SIDES:
            if e.kind == SHOT and (not e.on_goal or (shot_events_include_goals and e.shot_is_goal)):
                continue
            defending = other(e.side)
            name = e.goalie or in_net.get(defending)
            if not name or (e.kind == GOAL and e.is_empty_net and not e.goalie):
                continue
            gl = line(defending, name)
            gl.shots_against += 1
            if e.kind == GOAL:
                gl.goals_against += 1

    end = max(last_second, 3600) if game_over else last_second
    for side in SIDES:
        if in_net.get(side):
            leave(side, end)
    return sorted(lines.values(), key=lambda g: (g.side != HOME, -g.seconds))


def apply_official_saves(
    goalies: list[GoalieLine], saves_by_period: dict[str, list[int]], last_second: int
) -> list[GoalieLine]:
    """Ersätter målvakternas räddningar med de officiella per period.

    SHL:s händelselista räknar alla skottförsök, så skott mot målvakten blir för
    många. Räddningarna per period är officiella; har laget bytt målvakt under
    en period fördelas periodens räddningar efter tid i kassen. Insläppta mål
    kommer från målhändelserna. Skott mot = räddningar + insläppta mål.
    """
    out = []
    for side in SIDES:
        team = [g for g in goalies if g.side == side]
        per_period = saves_by_period.get(side) or []
        if not team or not per_period:
            out.extend(team)
            continue
        saves = {g.name: 0.0 for g in team}
        for i, n in enumerate(per_period):
            start, end = i * 1200, (i + 1) * 1200
            if last_second < end:
                end = max(last_second, start + 1)
            overlap = {
                g.name: sum(max(0, min(b, end) - max(a, start)) for a, b in g.intervals) for g in team
            }
            total = sum(overlap.values())
            if total == 0:
                main = max(team, key=lambda g: g.seconds)
                saves[main.name] += n
                continue
            for name, sec in overlap.items():
                saves[name] += n * sec / total
        for g in team:
            sv = round(saves[g.name])
            out.append(
                GoalieLine(
                    name=g.name,
                    side=side,
                    shots_against=sv + g.goals_against,
                    goals_against=g.goals_against,
                    seconds=g.seconds,
                    intervals=g.intervals,
                )
            )
    return [g for g in out if g.shots_against or g.seconds]


# ---------------------------------------------------------------------------
# Valfri lagstatistik-endpoint
# ---------------------------------------------------------------------------


def empty_line() -> TeamLine:
    """En TeamLine där inget värde är känt (används för inläst lagstatistik)."""
    line = TeamLine()
    for name in line.__dataclass_fields__:
        setattr(line, name, None)
    return line


def _norm_key(k: str) -> str:
    return "".join(ch for ch in k.lower() if ch.isalnum())


_ALIAS_LOOKUP = {alias: name for name, aliases in STAT_ALIASES.items() for alias in aliases}


def _line_from_mapping(d: dict) -> TeamLine:
    line = empty_line()
    for k, v in d.items():
        name = _ALIAS_LOOKUP.get(_norm_key(str(k)))
        n = as_int(v)
        if name and n is not None:
            setattr(line, name, n)
    return line


def parse_team_stats(data: Any) -> dict[int, PeriodStats]:
    """Tolkar lagstatistik i några vanliga format. Period 0 = hela matchen (ignoreras).

    Format som stöds:
      * {"periods": [{"period": 1, "home": {...}, "away": {...}}, ...]}
      * {"home": {...}, "away": {...}}  -> hela matchen (returneras som tomt)
      * [{"name": "shots", "period": 1, "home": 5, "away": 7}, ...]
    Hela-matchen-värden används inte eftersom de räknas fram som summa.
    """
    out: dict[int, PeriodStats] = {}

    def rows(obj: Any) -> list:
        if isinstance(obj, list):
            return obj
        if isinstance(obj, dict):
            for key in ("periods", "periodStats", "stats", "statistics", "data", "items"):
                if isinstance(obj.get(key), list):
                    return obj[key]
        return []

    for row in rows(data):
        if not isinstance(row, dict):
            continue
        period = as_int(first(row, "period", "periodNumber"))
        if not period:
            continue
        home = first(row, "home", "homeTeam", "homeStats")
        away = first(row, "away", "awayTeam", "awayStats")
        if isinstance(home, dict) and isinstance(away, dict):
            st = out.setdefault(period, PeriodStats(empty_line(), empty_line()))
            st.home = _line_from_mapping(home)
            st.away = _line_from_mapping(away)
            continue
        # "rad per statistiktyp"
        name = _ALIAS_LOOKUP.get(_norm_key(str(first(row, "name", "key", "type", "stat", default=""))))
        hv, av = as_int(first(row, "home", "homeValue")), as_int(first(row, "away", "awayValue"))
        if name and hv is not None and av is not None:
            st = out.setdefault(period, PeriodStats(empty_line(), empty_line()))
            setattr(st.home, name, hv)
            setattr(st.away, name, av)
    return out

