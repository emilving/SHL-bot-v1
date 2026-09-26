"""Normalisering av SHL:s JSON till enkla, stabila datatyper.

SHL:s API är inte officiellt dokumenterat. All parsning här är därför
defensiv: flera möjliga nyckelnamn prövas och okända fält ignoreras, så att
en ändring hos SHL i värsta fall ger saknad information i stället för krasch.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable
from zoneinfo import ZoneInfo

STOCKHOLM = ZoneInfo("Europe/Stockholm")

# Normaliserade händelsetyper
GOAL = "goal"
PENALTY = "penalty"
SHOT = "shot"
GOALIE_IN = "goalie_in"
GOALIE_OUT = "goalie_out"
PERIOD_START = "period_start"
PERIOD_END = "period_end"
TIMEOUT = "timeout"
INJURY = "injury"
PENALTY_SHOT = "penalty_shot"
SHOOTOUT = "shootout"
FACEOFF = "faceoff"
OTHER = "other"

HOME = "home"
AWAY = "away"


def first(d: Any, *keys: str, default: Any = None) -> Any:
    """Första icke-tomma värdet bland `keys` i dict:en `d`."""
    if not isinstance(d, dict):
        return default
    for k in keys:
        v = d.get(k)
        if v not in (None, "", [], {}):
            return v
    return default


def as_int(v: Any, default: int | None = None) -> int | None:
    if isinstance(v, bool):
        return default
    if isinstance(v, (int, float)):
        return int(v)
    if isinstance(v, str):
        m = re.search(r"-?\d+", v)
        if m:
            return int(m.group())
    return default


def truthy(v: Any) -> bool:
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "ja")
    return bool(v)


def person_name(p: Any) -> str | None:
    if isinstance(p, str):
        return p or None
    if not isinstance(p, dict):
        return None
    fn = first(p, "firstName", "firstname", "givenName")
    ln = first(p, "familyName", "lastName", "lastname", "surname")
    name = " ".join(str(x) for x in (fn, ln) if x)
    return name or first(p, "name", "fullName", "displayName")


def jersey(p: Any) -> str | None:
    v = first(p, "jerseyToday", "jersey", "jerseyNumber", "number", "shirtNumber")
    return str(v) if v is not None else None


def player_label(p: Any) -> str | None:
    name = person_name(p)
    if not name:
        return None
    num = jersey(p)
    return f"#{num} {name}" if num else name


def clock(v: Any) -> str:
    """Normaliserar speltid till "MM:SS"."""
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        s = int(v)
        return f"{s // 60:02d}:{s % 60:02d}"
    if isinstance(v, str):
        m = re.match(r"^\s*(\d{1,2}):(\d{2})", v)
        if m:
            return f"{int(m.group(1)):02d}:{m.group(2)}"
    return "00:00"


def clock_seconds(t: str) -> int:
    m, s = t.split(":")
    return int(m) * 60 + int(s)


# ---------------------------------------------------------------------------
# Matchinformation från spelschemat
# ---------------------------------------------------------------------------


@dataclass
class TeamInfo:
    code: str
    name: str
    short: str

    @classmethod
    def parse(cls, info: Any) -> "TeamInfo":
        info = info if isinstance(info, dict) else {}
        names = info.get("names") if isinstance(info.get("names"), dict) else {}
        code = str(first(info, "code", "teamCode", default="") or first(names, "code", default="?")).upper()
        name = first(names, "long", "full", "short") or first(info, "name", "teamName") or code
        short = first(names, "short", "code") or code
        return cls(code=code, name=str(name), short=str(short))


@dataclass
class GameInfo:
    uuid: str
    home: TeamInfo
    away: TeamInfo
    start: datetime | None
    state: str
    home_score: int | None = None
    away_score: int | None = None
    venue: str | None = None
    raw: dict = field(default_factory=dict, repr=False)

    @classmethod
    def parse(cls, g: dict) -> "GameInfo":
        home_raw = first(g, "homeTeamInfo", "homeTeam", default={})
        away_raw = first(g, "awayTeamInfo", "awayTeam", default={})
        start = None
        raw_start = first(g, "startDateTime", "startDate", "start")
        if isinstance(raw_start, str):
            try:
                start = datetime.fromisoformat(raw_start.replace("Z", "+00:00"))
            except ValueError:
                start = None
            if start and start.tzinfo is None:
                start = start.replace(tzinfo=STOCKHOLM)
        return cls(
            uuid=str(first(g, "uuid", "gameUuid", "id")),
            home=TeamInfo.parse(home_raw),
            away=TeamInfo.parse(away_raw),
            start=start,
            state=str(first(g, "state", "gameState", "status", default="")).lower(),
            home_score=as_int(first(home_raw, "score", "goals")),
            away_score=as_int(first(away_raw, "score", "goals")),
            venue=first(first(g, "venueInfo", default={}), "name"),
            raw=g,
        )

    @property
    def title(self) -> str:
        return f"{self.home.name} – {self.away.name}"

    @property
    def is_finished(self) -> bool:
        return is_final_state(self.state)

    @property
    def is_pre_game(self) -> bool:
        return self.state in ("pre-game", "pregame", "not-started", "notstarted", "scheduled", "")


def is_final_state(state: str) -> bool:
    s = state.lower().replace("_", "-")
    return any(k in s for k in ("post-game", "postgame", "finished", "ended", "final", "game-ended", "gameended"))


# ---------------------------------------------------------------------------
# Matchstatus (från game-overview, med spelschemat som reserv)
# ---------------------------------------------------------------------------


@dataclass
class GameStatus:
    phase: str  # "pre", "live", "intermission", "final"
    period: int | None = None
    clock: str | None = None
    home_score: int | None = None
    away_score: int | None = None


def parse_status(overview: Any, game: GameInfo) -> GameStatus:
    ov = overview if isinstance(overview, dict) else {}
    state = str(first(ov, "state", "gameState", "status", default=game.state)).lower()
    time_info = first(ov, "gameTime", "time", "clock", default={})
    period = as_int(first(time_info, "period", "periodNumber")) or as_int(first(ov, "period", "currentPeriod"))
    clk = first(time_info, "periodTime", "time", "gameTime") if isinstance(time_info, dict) else None

    home = first(ov, "homeTeam", "homeTeamInfo", "home", default={})
    away = first(ov, "awayTeam", "awayTeamInfo", "away", default={})
    hs = as_int(first(home, "score", "goals")) if isinstance(home, dict) else None
    as_ = as_int(first(away, "score", "goals")) if isinstance(away, dict) else None
    if hs is None:
        hs = as_int(first(ov, "homeScore", "homeGoals"), game.home_score)
    if as_ is None:
        as_ = as_int(first(ov, "awayScore", "awayGoals"), game.away_score)

    intermission = any(k in state for k in ("intermission", "paus", "break", "period-end", "periodend")) or truthy(
        first(time_info, "intermission", "isIntermission", "periodEnded", default=False)
    )

    has_own_state = first(ov, "state", "gameState", "status") is not None
    if is_final_state(state) or (not has_own_state and is_final_state(game.state)):
        phase = "final"
    elif intermission:
        phase = "intermission"
    elif state in ("pre-game", "pregame", "not-started", "notstarted", "scheduled", ""):
        phase = "pre" if game.is_pre_game else "live"
    else:
        phase = "live"
    return GameStatus(
        phase=phase,
        period=period,
        clock=clock(clk) if clk is not None else None,
        home_score=hs,
        away_score=as_,
    )


# ---------------------------------------------------------------------------
# Händelser (play-by-play)
# ---------------------------------------------------------------------------


@dataclass
class Event:
    id: str
    kind: str
    raw_type: str
    period: int
    time: str
    side: str | None = None
    team_code: str | None = None
    player: str | None = None
    assists: list[str] = field(default_factory=list)
    home_goals: int | None = None
    away_goals: int | None = None
    strength: str | None = None
    penalty_minutes: int | None = None
    offence: str | None = None
    goalie: str | None = None
    shot_is_goal: bool = False
    description: str | None = None
    raw: dict = field(default_factory=dict, repr=False)

    @property
    def game_seconds(self) -> int:
        return (max(self.period, 1) - 1) * 1200 + clock_seconds(self.time)

    @property
    def signature(self) -> str:
        """Fingeravtryck av det som syns i en notis – används för att upptäcka korrigeringar."""
        parts = [
            self.kind,
            str(self.period),
            self.time,
            self.side or "",
            self.player or "",
            "|".join(self.assists),
            self.strength or "",
            str(self.penalty_minutes or ""),
            self.offence or "",
        ]
        return "~".join(parts)

    @property
    def is_pp(self) -> bool:
        return strength_kind(self.strength) == "pp"

    @property
    def is_sh(self) -> bool:
        return strength_kind(self.strength) == "sh"

    @property
    def is_empty_net(self) -> bool:
        s = (self.strength or "").upper()
        return "EN" in s.replace("PEN", "") or "TM" in s or truthy(first(self.raw, "emptyNet", "isEmptyNet"))


def strength_kind(strength: str | None) -> str | None:
    s = (strength or "").upper()
    if s.startswith("PP") or "POWER" in s:
        return "pp"
    if s.startswith("SH") or s.startswith("BP") or "SHORT" in s:
        return "sh"
    if s.startswith("PS") or "PENALTY SHOT" in s or s == "STRAFF":
        return "ps"
    if s.startswith("EQ") or s.startswith("ES") or s.startswith("EV") or s.startswith("JS"):
        return "eq"
    return None


def _event_kind(raw_type: str, e: dict) -> str:
    t = raw_type.lower().replace("_", "").replace("-", "").replace(" ", "")
    if "shootout" in t or t in ("gws", "so"):
        return SHOOTOUT
    if "penaltyshot" in t or t == "straff":
        return PENALTY_SHOT
    if t.startswith("goal") and "keeper" not in t and "goalie" not in t:
        return GOAL
    if "goalkeeper" in t or "goalie" in t or "malvakt" in t:
        entering = first(e, "isEntering", "entering", "in", "isIn")
        if entering is None:
            action = str(first(e, "action", "direction", "subType", default="")).lower()
            return GOALIE_OUT if any(k in action for k in ("out", "exit", "ut")) else GOALIE_IN
        return GOALIE_IN if truthy(entering) else GOALIE_OUT
    if "penalty" in t or "utvisning" in t:
        return PENALTY
    if t.startswith("shot") or t == "skott":
        return SHOT
    if "faceoff" in t or "tekning" in t:
        return FACEOFF
    if "injur" in t or "skad" in t:
        return INJURY
    if "timeout" in t:
        return TIMEOUT
    if t.startswith("period"):
        ended = first(e, "finished", "ended", "isEnd", "periodEnd", "hasEnded", "isFinished")
        if ended is None:
            sub = str(first(e, "action", "subType", "status", default="")).lower()
            return PERIOD_END if any(k in sub for k in ("end", "finish", "slut")) else PERIOD_START
        return PERIOD_END if truthy(ended) else PERIOD_START
    return OTHER


def _stable_id(raw_type: str, period: int, time: str, e: dict) -> str:
    explicit = first(e, "id", "eventId", "uuid", "eventUuid", "gameSourceId")
    if explicit is not None:
        return f"{raw_type}:{explicit}"
    team = first(first(e, "eventTeam", "team", default={}), "place", "teamCode", "code", default="")
    who = person_name(first(e, "player", "skater", "goalScorer", default=None)) or ""
    blob = json.dumps([raw_type, period, time, team, who, first(e, "isEntering")], sort_keys=True, default=str)
    return f"{raw_type}:h{hashlib.sha1(blob.encode()).hexdigest()[:12]}"


def _side(e: dict, game: GameInfo | None) -> tuple[str | None, str | None]:
    team = first(e, "eventTeam", "team", "teamInfo", default={})
    code = first(team, "teamCode", "code") if isinstance(team, dict) else None
    code = str(code).upper() if code else None
    place = str(first(team, "place", "side", default="") or first(e, "place", default="")).lower()
    if place in (HOME, AWAY):
        side = place
    elif game and code and code == game.home.code:
        side = HOME
    elif game and code and code == game.away.code:
        side = AWAY
    else:
        side = None
    if not code and game and side:
        code = game.home.code if side == HOME else game.away.code
    return side, code


def _penalty_minutes(e: dict) -> int | None:
    variant = first(e, "variant", "penaltyVariant", default={})
    for v in (
        first(variant, "minorTime", "duration", "minutes"),
        first(e, "penaltyMinutes", "minutes", "duration", "pim", "penaltyLength"),
        first(variant, "description", "shortName"),
        first(e, "penaltyType"),
    ):
        n = as_int(v)
        if n:
            return n
    return None


def _offence(e: dict) -> str | None:
    v = first(e, "offence", "offense", "reason", "penaltyReason", "penaltyCode", "infraction")
    if isinstance(v, dict):
        v = first(v, "description", "name", "text")
    return str(v) if v else None


def parse_event(e: dict, game: GameInfo | None = None) -> Event:
    raw_type = str(first(e, "type", "eventType", "action", default="unknown"))
    period = as_int(first(e, "period", "periodNumber"), 1) or 1
    time = clock(first(e, "time", "periodTime", "gameTime", "clock", default="00:00"))
    side, code = _side(e, game)
    kind = _event_kind(raw_type, e)

    player_raw = first(e, "player", "goalScorer", "scorer", "skater", "penalizedPlayer", "goalkeeper", "goalie")
    assists_raw = first(e, "assists", "assist", default=[])
    if isinstance(assists_raw, dict):
        assists_raw = [assists_raw.get(k) for k in ("first", "second", "third") if assists_raw.get(k)]
    assists = [a for a in (player_label(x) for x in assists_raw if x) if a] if isinstance(assists_raw, list) else []

    goalie_raw = first(e, "goalieInNet", "goalkeeperInNet", "goalie", "goalkeeper") if kind in (GOAL, SHOT) else None

    shot_goal = kind == SHOT and (
        truthy(first(e, "isGoal", "goal", "resultedInGoal", default=False))
        or str(first(e, "result", "outcome", default="")).lower() == "goal"
    )

    return Event(
        id=_stable_id(raw_type, period, time, e),
        kind=kind,
        raw_type=raw_type,
        period=period,
        time=time,
        side=side,
        team_code=code,
        player=player_label(player_raw),
        assists=assists,
        home_goals=as_int(first(e, "homeGoals", "homeScore")),
        away_goals=as_int(first(e, "awayGoals", "awayScore")),
        strength=(str(first(e, "goalStatus", "goalType", "strength", "situation", default="")) or None),
        penalty_minutes=_penalty_minutes(e) if kind == PENALTY else None,
        offence=_offence(e) if kind == PENALTY else None,
        goalie=player_label(goalie_raw) if goalie_raw else None,
        shot_is_goal=shot_goal,
        description=first(e, "description", "text", "comment"),
        raw=e,
    )


def extract_event_list(data: Any) -> list[dict]:
    """Hittar listan med händelser oavsett om svaret är en lista eller inbäddad i ett objekt."""
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict):
        for key in ("events", "playByPlay", "actions", "items", "data", "gameEvents"):
            if key in data:
                found = extract_event_list(data[key])
                if found:
                    return found
    return []


def parse_events(data: Any, game: GameInfo | None = None) -> list[Event]:
    events = [parse_event(e, game) for e in extract_event_list(data)]
    # Stabil sortering i speltid; ursprunglig ordning avgör vid lika tid
    return sorted(events, key=lambda ev: ev.game_seconds)


def dedupe(events: Iterable[Event]) -> list[Event]:
    seen: set[str] = set()
    out = []
    for ev in events:
        if ev.id not in seen:
            seen.add(ev.id)
            out.append(ev)
    return out
