"""Snabbare matchstatus från stats.swehockey.se (Svenska Ishockeyförbundet).

SHL:s egen händelselista kan ligga flera minuter efter. Swehockeys matchsida
visar ställning, period, klocka och paus direkt, och har målskytt för målen.
Sidan är HTML, så den läses av med BeautifulSoup.

Sidstrukturen bygger på beskrivningen i ha-swehockey-api av Tiimber
(https://github.com/Tiimber/ha-swehockey-api, MIT-licens).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date

import aiohttp
from bs4 import BeautifulSoup

from .api import HEADERS

log = logging.getLogger(__name__)

BASE_URL = "https://stats.swehockey.se"

# Rader för andra serier än SHL som kan ha samma lagnamn
_NOT_SHL = re.compile(r"\b(J20|J18|U20|U18|U16|Dam|SDHL|Damallsvenskan|NDHL)\b", re.I)
_GENERIC_TOKENS = {"hockey", "hc", "if", "ik", "bk", "hk", "aik", "sk", "ff", "club"}

_STATE_PERIOD_RE = re.compile(r"(\d)(?:st|nd|rd|th)\s+period(\s+ended)?", re.I)
_PAIRS_RE = re.compile(r"^\(\s*\d+-\d+(?:\s*,\s*\d+-\d+)*\s*\)$")
_SCORE_RE = re.compile(r"^(\d+)\s*[-–]\s*(\d+)$")
_CLOCK_RE = re.compile(r"^\d{1,2}:\d{2}$")
_TRAILING_CLOCK_RE = re.compile(r"\((\d{1,2}:\d{2})\)\s*$")
_GOAL_ROW_RE = re.compile(r"^(\d+)-(\d+)\s*\(([^)]+)\)\s*$")
_GOALIE_RE = re.compile(r"\d+[,.]\d+\s*%\s*\((\d+)\s*/\s*(\d+)\)")
_PENALTY_ROW_RE = re.compile(r"^(\d+)\s*min\b", re.I)


def _text(el) -> str:
    return re.sub(r"\s+", " ", el.get_text(" ", strip=True).replace("\xa0", " ")).strip()


def _name_tokens(name: str) -> list[str]:
    return [t for t in re.findall(r"[\wåäöÅÄÖ]+", name.lower()) if t not in _GENERIC_TOKENS and len(t) >= 3]


def _compact(name: str) -> str:
    words = [t for t in re.findall(r"[\wåäöÅÄÖ]+", name.lower()) if t not in _GENERIC_TOKENS]
    return "".join(words)


def same_team(a: str, b: str) -> bool:
    """Lös jämförelse av lagnamn, t.ex. "Djurgårdens IF" och "Djurgården Hockey" eller "HV71" och "HV 71"."""
    if any(x[:5] == y[:5] for x in _name_tokens(a) for y in _name_tokens(b)):
        return True
    ca, cb = _compact(a), _compact(b)
    return len(ca) >= 4 and ca[:5] == cb[:5]


def _player_name(raw: str) -> str:
    """"34. Brodin, Daniel (1)" -> "#34 Daniel Brodin"."""
    raw = re.sub(r"\(\d+\)", "", raw).strip()
    m = re.match(r"^(\d+)\.\s*(.+)$", raw)
    num, name = (m.group(1), m.group(2)) if m else (None, raw)
    name = name.strip().rstrip(",")
    if "," in name:
        last, first = name.split(",", 1)
        name = f"{first.strip()} {last.strip()}"
    return f"#{num} {name}" if num else name


def _split_players(cell: str) -> list[str]:
    cell = re.sub(r"\(\d+\)", "", cell)
    parts = re.findall(r"\d+\.\s*[^\d]+?(?=\s*\d+\.|$)", cell)
    return [_player_name(p) for p in parts] if parts else ([_player_name(cell)] if cell else [])


@dataclass
class SweGoal:
    time: str  # speltid från matchstart, t.ex. "27:13"
    home: int
    away: int
    strength: str
    team: str
    scorer: str | None
    assists: list[str] = field(default_factory=list)


@dataclass
class SweGoalie:
    team: str
    name: str
    saves: int
    shots: int


@dataclass
class SweGame:
    home_score: int | None = None
    away_score: int | None = None
    period: int | None = None  # 4 = förlängning, 5 = straffar
    clock: str | None = None
    intermission: bool = False
    final: bool = False
    state_text: str | None = None
    goals: list[SweGoal] = field(default_factory=list)
    penalties: int = 0
    goalies: list[SweGoalie] = field(default_factory=list)
    # Officiell statistik per period från sidhuvudet, t.ex. "Shots 34 (15:9:10)"
    shots: dict[str, list[int]] = field(default_factory=dict)
    saves: dict[str, list[int]] = field(default_factory=dict)

    def goal_with_score(self, home: int, away: int) -> SweGoal | None:
        return next((g for g in self.goals if (g.home, g.away) == (home, away)), None)


def parse_games_by_date(html: str) -> list[tuple[int, str, str, bool]]:
    """(match-id, hemmalag, bortalag, är_shl) för alla matcher på sidan."""
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for row in soup.find_all("tr"):
        cells = row.find_all("td")
        if len(cells) < 2:
            continue
        game_id = None
        for a in row.find_all("a"):
            m = re.search(r"Game/Events/(\d+)", (a.get("href") or "") + (a.get("onclick") or ""))
            if m:
                game_id = int(m.group(1))
                break
        if game_id is None:
            continue
        row_text = _text(row)
        teams = next((_text(c) for c in cells if re.search(r"\s[-–]\s", _text(c))), "")
        m = re.match(r"(.+?)\s+[-–]\s+(.+?)(?:\s*\[.*\])?$", teams)
        if not m:
            continue
        is_shl = not _NOT_SHL.search(row_text)
        out.append((game_id, m.group(1).strip(), m.group(2).strip(), is_shl))
    return out


def find_game(rows: list[tuple[int, str, str, bool]], home: str, away: str) -> int | None:
    matches = [r for r in rows if r[3] and same_team(r[1], home) and same_team(r[2], away)]
    return matches[0][0] if matches else None


def _period_number(label: str) -> int | None:
    m = _STATE_PERIOD_RE.search(label)
    if m:
        return int(m.group(1))
    low = label.lower()
    if "overtime" in low or low.startswith("ot") or "förl" in low:
        return 4
    if "shootout" in low or "straff" in low or "penalty shot" in low:
        return 5
    return None


def parse_game_page(html: str) -> SweGame:
    soup = BeautifulSoup(html, "html.parser")
    game = SweGame()

    info = soup.find("td", class_="tdInfoArea")
    if info:
        for div in info.find_all("div"):
            t = _text(div)
            if not t:
                continue
            low = t.lower()
            score = _SCORE_RE.match(t)
            if score and game.home_score is None:
                game.home_score, game.away_score = int(score.group(1)), int(score.group(2))
            elif _STATE_PERIOD_RE.search(t):
                m = _STATE_PERIOD_RE.search(t)
                game.state_text = t
                game.period = int(m.group(1))
                game.intermission = bool(m.group(2))
            elif "final" in low or "finished" in low:
                game.state_text = t
                game.final = True
            elif "overtime" in low or "shootout" in low or low.startswith("straff"):
                game.state_text = t
                game.period = _period_number(t)
                game.intermission = "ended" in low
            elif _PAIRS_RE.match(t):
                continue
            elif _CLOCK_RE.match(t) and game.clock is None:
                game.clock = f"{int(t.split(':')[0]):02d}:{t.split(':')[1]}"
            elif game.clock is None and not low.startswith("spectators"):
                m = _TRAILING_CLOCK_RE.search(t)
                if m:
                    game.clock = f"{int(m.group(1).split(':')[0]):02d}:{m.group(1).split(':')[1]}"

    # "Goalkeeper Summary": t.ex. "VÄX | 70. Åhman, Adam | 80,00% (20/25)"
    for row in soup.find_all("tr"):
        cells = [_text(c) for c in row.find_all("td")]
        joined = " ".join(cells)
        m = _GOALIE_RE.search(joined)
        if not m or re.fullmatch(r"\d{1,3}:\d{2}", cells[0] if cells else ""):
            continue
        player = next((c for c in cells if re.match(r"^\d+\.\s*\S", c) and "%" not in c), None)
        team = next((c for c in cells if c and c != player and "%" not in c), "")
        if player:
            game.goalies.append(
                SweGoalie(team=team, name=_player_name(player), saves=int(m.group(1)), shots=int(m.group(2)))
            )

    # Händelsetabellen: första cellen är speltid "mm:ss" från matchstart, nyast först
    for table in soup.find_all("table"):
        rows = []
        for row in table.find_all("tr"):
            cells = [_text(c) for c in row.find_all("td")]
            if len(cells) >= 2 and re.fullmatch(r"\d{1,3}:\d{2}", cells[0]):
                rows.append(cells)
        if not rows:
            continue
        for cells in reversed(rows):
            kind = cells[1]
            team = cells[2] if len(cells) > 2 else ""
            players = _split_players(cells[3]) if len(cells) > 3 else []
            gm = _GOAL_ROW_RE.match(kind)
            if gm:
                game.goals.append(
                    SweGoal(
                        time=cells[0],
                        home=int(gm.group(1)),
                        away=int(gm.group(2)),
                        strength=gm.group(3).strip(),
                        team=team,
                        scorer=players[0] if players else None,
                        assists=players[1:],
                    )
                )
            elif _PENALTY_ROW_RE.match(kind):
                game.penalties += 1
        break

    _parse_header_stats(soup, game)
    if game.home_score is None and game.goals:
        game.home_score, game.away_score = game.goals[-1].home, game.goals[-1].away
    return game


_PERIODS_RE = re.compile(r"^\((\d+(?:\s*:\s*\d+)*)\)$")


def _parse_header_stats(soup, game: SweGame) -> None:
    """Sidhuvudet: "Shots 34 (15:9:10)" för hemmalaget till vänster och bortalaget till höger."""
    strings = [" ".join(x.split()) for x in soup.stripped_strings]
    found: dict[str, list[list[int]]] = {"shots": [], "saves": []}
    for i, text in enumerate(strings):
        key = text.strip().rstrip(":").lower()
        if key not in found:
            continue
        for nxt in strings[i + 1 : i + 4]:
            m = _PERIODS_RE.match(nxt.strip())
            if m:
                found[key].append([int(x) for x in re.split(r"\s*:\s*", m.group(1))])
                break
    for key in found:
        if len(found[key]) >= 2:
            setattr(game, key, {"home": found[key][0], "away": found[key][1]})


class SweHockeyClient:
    def __init__(self, session: aiohttp.ClientSession | None = None):
        self._session = session
        self._own = session is None
        self._by_date: dict[str, list[tuple[int, str, str, bool]]] = {}

    async def __aenter__(self) -> "SweHockeyClient":
        if self._session is None:
            self._session = aiohttp.ClientSession(headers=HEADERS, timeout=aiohttp.ClientTimeout(total=8))
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    async def close(self) -> None:
        if self._own and self._session is not None:
            await self._session.close()
            self._session = None

    async def _html(self, path: str) -> str:
        assert self._session is not None
        async with self._session.get(BASE_URL + path, headers={"Accept": "text/html"}) as resp:
            resp.raise_for_status()
            return (await resp.read()).decode("utf-8", errors="replace")

    async def games_on(self, day: date, refresh: bool = False) -> list[tuple[int, str, str, bool]]:
        key = day.isoformat()
        if refresh or key not in self._by_date:
            self._by_date[key] = parse_games_by_date(await self._html(f"/GamesByDate/{key}/ByTime/90"))
        return self._by_date[key]

    async def find_game_id(self, day: date, home: str, away: str) -> int | None:
        game_id = find_game(await self.games_on(day), home, away)
        if game_id is None:
            game_id = find_game(await self.games_on(day, refresh=True), home, away)
        return game_id

    async def lineup_html(self, game_id: int) -> str:
        return await self._html(f"/Game/LineUps/{game_id}")

    async def lineup(self, game_id: int, home_names: list[str], away_names: list[str]) -> dict[str, dict[str, str]]:
        return parse_lineup(await self._html(f"/Game/LineUps/{game_id}"), home_names, away_names)

    async def game(self, game_id: int) -> SweGame:
        return parse_game_page(await self._html(f"/Game/Events/{game_id}"))


# ---------------------------------------------------------------------------
# Laguppställningar (Game/LineUps/{id})
# ---------------------------------------------------------------------------

_LINEUP_PLAYER_RE = re.compile(r"^(\d{1,2})\.?\s+([^\d].*)$")
_NUMBER_ONLY_RE = re.compile(r"^(\d{1,2})\.?$")
# Domare står före lagen; ledare står under lagnamnet och ska bara hoppas över
_OFFICIALS_RE = re.compile(r"referee|linesm|domare|linjedomare", re.I)
_STAFF_RE = re.compile(r"coach|tränare|staff|ledare|manager", re.I)


def _lineup_name(raw: str) -> str | None:
    name = re.sub(r"\(.*?\)", "", raw).strip().rstrip(",")
    if not re.search(r"[A-Za-zÅÄÖåäöÉéÜü]{2}", name):
        return None
    if "," in name:
        last, first = name.split(",", 1)
        name = f"{first.strip()} {last.strip()}"
    return " ".join(name.split())


def parse_lineup(html: str, home_names: list[str], away_names: list[str]) -> dict[str, dict[str, str]]:
    """Spelare per lag: {"home": {normaliserat namn: "#22 Linus Johansson"}, "away": {...}}.

    Sidans upplägg är inte dokumenterat. Läsaren går igenom texten i ordning,
    byter lag när ett lagnamn eller en lagkod dyker upp som egen rubrik, och
    tar med rader av typen "22. Johansson, Linus" (eller "22" följt av namnet).
    Domare och ledare hoppas över.
    """
    soup = BeautifulSoup(html, "html.parser")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    m = re.match(r"\s*(\S+)\s*-\s*(\S+)", title)
    if m:
        home_names = [*home_names, m.group(1)]
        away_names = [*away_names, m.group(2)]

    def team_of(text: str) -> str | None:
        if len(text) > 40 or re.search(r"\d+\.", text):
            return None
        for side, names in (("home", home_names), ("away", away_names)):
            for n in names:
                if text.strip().lower() == n.strip().lower() or (len(text) > 4 and same_team(text, n)):
                    return side
        return None

    out: dict[str, dict[str, str]] = {"home": {}, "away": {}}
    current: str | None = None
    strings = [" ".join(s.split()) for s in soup.body.stripped_strings] if soup.body else []
    i = 0
    while i < len(strings):
        text = strings[i]
        side = team_of(text)
        if side:
            current = side
        elif _OFFICIALS_RE.search(text) and not _LINEUP_PLAYER_RE.match(text):
            current = None
        elif _STAFF_RE.search(text) and not _LINEUP_PLAYER_RE.match(text):
            pass
        elif current:
            number = name = None
            pm = _LINEUP_PLAYER_RE.match(text)
            nm = _NUMBER_ONLY_RE.match(text)
            if pm:
                number, name = pm.group(1), _lineup_name(pm.group(2))
            elif nm and i + 1 < len(strings) and not _NUMBER_ONLY_RE.match(strings[i + 1]):
                number, name = nm.group(1), _lineup_name(strings[i + 1])
                if name and team_of(strings[i + 1]) is None:
                    i += 1
                else:
                    name = None
            if name:
                out[current].setdefault(name.lower(), f"#{number} {name}")
        i += 1
    return out


def describe_structure(html: str, limit: int = 60) -> list[str]:
    """Felsökning: var på sidan varje text står (tabell, rad, cell), för att förstå upplägget."""
    soup = BeautifulSoup(html, "html.parser")
    tables = soup.find_all("table")
    index = {id(t): i for i, t in enumerate(tables)}
    lines = []
    for node in soup.body.find_all(string=True) if soup.body else []:
        text = " ".join(node.split())
        if not text:
            continue
        cell = node.find_parent(["td", "th"])
        row = node.find_parent("tr")
        table = node.find_parent("table")
        where = "-"
        if table is not None and row is not None:
            rows = table.find_all("tr", recursive=False) or table.find_all("tr")
            cells = row.find_all(["td", "th"], recursive=False)
            r = next((i for i, x in enumerate(rows) if x is row), "?")
            c = next((i for i, x in enumerate(cells) if x is cell), "?")
            where = f"T{index.get(id(table), '?')} R{r} C{c}"
        lines.append(f"{where:<12} {text[:45]}")
        if len(lines) >= limit:
            break
    return lines
