import os
import io
import time
import asyncio
import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import aiohttp
import discord
from discord import app_commands


# ============================================================
# KONFIGURATION
# ============================================================

TOKEN = os.getenv("TOKEN")
CHANNEL_ID = int(os.getenv("CHANNEL_ID", "1552338794001469640"))

# Valfritt: server-ID. Om satt syncas slash-kommandona direkt till
# den servern (globala kommandon kan ta upp till en timme att dyka upp).
GUILD_ID = os.getenv("GUILD_ID")

# Hur ofta boten letar efter nya/ändrade matcher.
SCOREBOARD_INTERVAL = 30

# Hur ofta en aktiv match hämtas.
LIVE_INTERVAL = 8

# Lineups före match: börja leta så här många minuter före nedsläpp,
# och hämta som oftast så här ofta (sekunder) per match.
LINEUP_WINDOW_MINUTES = 60
LINEUP_INTERVAL = 300

# Hur länge en misslyckad logga ska vila innan nytt försök (sekunder).
LOGO_RETRY_SECONDS = 3600

# Ett mål måste saknas i så här många hämtningar i rad innan det
# räknas som bortdömt (skyddar mot tillfälliga glapp i API:t).
DISALLOWED_CONFIRMATIONS = 2

# NHL API
API_BASE = "https://api-web.nhle.com/v1"
USER_AGENT = "Discord-NHL-Live-Bot/5.0"

# Svensk tid
TIMEZONE = ZoneInfo("Europe/Stockholm")

# Debug-information i konsolen
DEBUG = os.getenv("DEBUG", "1") != "0"


# ============================================================
# LOGGNING
# ============================================================

logging.basicConfig(
    level=logging.DEBUG if DEBUG else logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

# discord.py och aiohttp pratar väldigt mycket på DEBUG.
logging.getLogger("discord").setLevel(logging.INFO)
logging.getLogger("aiohttp").setLevel(logging.WARNING)

log = logging.getLogger("nhl-bot")


# ============================================================
# NHL-LAGFÄRGER
# ============================================================

TEAM_COLORS = {
    "ANA": 0xF47A38,
    "BOS": 0xFFB81C,
    "BUF": 0x003087,
    "CAR": 0xCC0000,
    "CBJ": 0x002654,
    "CGY": 0xC8102E,
    "CHI": 0xCF0A2C,
    "COL": 0x6F263D,
    "DAL": 0x006847,
    "DET": 0xCE1126,
    "EDM": 0xFF4C00,
    "FLA": 0xC8102E,
    "LAK": 0x111111,
    "MIN": 0x154734,
    "MTL": 0xAF1E2D,
    "NJD": 0xCE1126,
    "NSH": 0xFFB81C,
    "NYI": 0x00529B,
    "NYR": 0x0038A8,
    "OTT": 0xC52032,
    "PHI": 0xF74902,
    "PIT": 0xFCB514,
    "SEA": 0x001628,
    "SJS": 0x006D75,
    "STL": 0x002F87,
    "TBL": 0x002868,
    "TOR": 0x00205B,
    "UTA": 0x69B3E7,
    "VAN": 0x00205B,
    "VGK": 0xB4975A,
    "WPG": 0x041E42,
    "WSH": 0xC8102E,
}


def team_color(abbrev):
    return TEAM_COLORS.get(str(abbrev).upper(), 0x1E90FF)


# ============================================================
# GLOBAL STATE
# ============================================================

# game_id -> GameTracker
ACTIVE_GAMES = {}

# game_id -> senaste lineup (för att upptäcka ändringar före match)
LATEST_LINEUPS = {}

# game_id -> tidpunkt (monotonic) för senaste lineup-kontroll
LAST_LINEUP_CHECK = {}

# game_id -> slutrapport skickad
FINAL_SENT = set()

# Senaste matcherna från scoreboard (används av /lineup)
LAST_SCOREBOARD = []


# ============================================================
# NHL API
# ============================================================

class NhlApiError(Exception):
    pass


class NhlApi:
    """
    Asynkron klient mot NHL:s API.

    Återanvänder en och samma aiohttp-session (en anslutningspool)
    istället för att öppna en ny HTTP-anslutning för varje anrop,
    och backar av vid 429 (rate limit) och 5xx-fel.
    """

    RETRIES = 4

    def __init__(self):
        self.session = None

    async def start(self):
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=15),
                headers={"User-Agent": USER_AGENT},
            )

    async def close(self):
        if self.session is not None and not self.session.closed:
            await self.session.close()

    async def _request(self, url, accept, as_json):
        await self.start()

        delay = 2
        last_error = None

        for attempt in range(1, self.RETRIES + 1):
            try:
                async with self.session.get(url, headers={"Accept": accept}) as resp:

                    if resp.status == 429 or resp.status >= 500:
                        retry_after = resp.headers.get("Retry-After")
                        wait = delay
                        if retry_after and retry_after.isdigit():
                            wait = max(wait, int(retry_after))
                        last_error = NhlApiError(f"HTTP {resp.status} för {url}")
                        log.warning(
                            "⏳ %s – försök %d/%d, väntar %ds",
                            last_error, attempt, self.RETRIES, wait,
                        )
                        await asyncio.sleep(wait)
                        delay *= 2
                        continue

                    if resp.status >= 400:
                        raise NhlApiError(f"HTTP {resp.status} för {url}")

                    if as_json:
                        return await resp.json(content_type=None)
                    return await resp.read()

            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                last_error = NhlApiError(f"Nätverksfel för {url}: {exc!r}")
                log.warning("⏳ %s – försök %d/%d", last_error, attempt, self.RETRIES)
                await asyncio.sleep(delay)
                delay *= 2

        raise last_error or NhlApiError(f"Okänt fel för {url}")

    async def get(self, path):
        return await self._request(f"{API_BASE}{path}", "application/json", True)

    async def get_bytes(self, url):
        return await self._request(url, "image/png,image/*;q=0.9,*/*;q=0.8", False)


api = NhlApi()


async def fetch_scoreboard():
    """
    /score/now istället för /score/{svenskt datum}: NHL:s matchdygn
    och svensk kalenderdag är inte samma sak (en match 01:30 svensk
    tid tillhör fortfarande föregående NHL-dygn).
    """
    return await api.get("/score/now")


async def fetch_landing(game_id):
    return await api.get(f"/gamecenter/{game_id}/landing")


async def fetch_boxscore(game_id):
    return await api.get(f"/gamecenter/{game_id}/boxscore")


async def fetch_play_by_play(game_id):
    return await api.get(f"/gamecenter/{game_id}/play-by-play")


async def fetch_right_rail(game_id):
    """Lagstatistik (hits, blocks, powerplay, PIM, tekningar m.m.)."""
    return await api.get(f"/gamecenter/{game_id}/right-rail")


# ============================================================
# HJÄLPFUNKTIONER
# ============================================================

def today_str():
    return datetime.now(TIMEZONE).strftime("%Y-%m-%d")


def text_value(value, default=""):
    """NHL skickar ofta text som {"default": "..."}."""
    if isinstance(value, dict):
        return value.get("default", default) or default
    if value is None:
        return default
    return str(value)


def player_name(player):
    if not player:
        return "Okänd"

    name = text_value(player.get("name"))
    if name:
        return name

    full = (
        f"{text_value(player.get('firstName'))} "
        f"{text_value(player.get('lastName'))}"
    ).strip()
    if full:
        return full

    return text_value(player.get("playerName"), "Okänd")


def clock_to_seconds(clock):
    try:
        minutes, seconds = str(clock).split(":")
        return int(minutes) * 60 + int(seconds)
    except (ValueError, AttributeError):
        return None


def period_label(number, period_type="REG", playoffs=False):
    """
    1–3 -> "1. perioden" osv, OT -> "Övertid", SO -> "Straffläggning".
    I slutspelet kan det bli flera övertider.
    """
    period_type = str(period_type or "REG").upper()

    if period_type == "SO":
        return "Straffläggning"

    if period_type == "OT":
        try:
            ot_number = int(number) - 3
        except (TypeError, ValueError):
            ot_number = 1
        if playoffs and ot_number > 1:
            return f"Övertid {ot_number}"
        return "Övertid"

    return f"{number}. perioden"


def is_playoffs(landing):
    return landing.get("gameType") == 3


def get_teams(data):
    away = data.get("awayTeam", {}) or {}
    home = data.get("homeTeam", {}) or {}
    return (
        away.get("abbrev", "AWAY"),
        home.get("abbrev", "HOME"),
        away,
        home,
    )


def resolve_team_abbrev(landing, team_id):
    """Vilket lags förkortning hör ett teamId till?"""
    if team_id is None:
        return None

    _, _, away, home = get_teams(landing)

    if away.get("id") == team_id:
        return away.get("abbrev")
    if home.get("id") == team_id:
        return home.get("abbrev")
    return None


def parse_utc(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


# ============================================================
# GAME STATES
# ============================================================

def game_state(game):
    return str(game.get("gameState", "")).upper()


def is_live_state(state):
    return state in {"LIVE", "CRIT"}


def is_final_state(state):
    return state in {"FINAL", "OFF"}


def is_pregame_state(state):
    return state in {"FUT", "PRE"}


# ============================================================
# NHL-LOGGOR
# ============================================================
#
# NHL:s egna loggor är SVG, och Discord kan inte rendera SVG.
# Vi använder därför ESPN:s CDN som har riktiga PNG-filer.
# De flesta lag matchar sin NHL-förkortning (lowercase), men ett
# fåtal har en annan kod hos ESPN.

LOGO_BASE_URL = "https://a.espncdn.com/i/teamlogos/nhl/500"

ESPN_ABBR_OVERRIDES = {
    "NJD": "nj",
    "SJS": "sj",
    "TBL": "tb",
    "LAK": "la",
    "UTA": "utah",
}

# abbrev -> bytes (lyckad hämtning)
LOGO_CACHE = {}

# abbrev -> tidpunkt (monotonic) för senaste misslyckade försök
LOGO_FAILED_AT = {}


def logo_url(abbrev):
    abbrev = str(abbrev).upper()
    return f"{LOGO_BASE_URL}/{ESPN_ABBR_OVERRIDES.get(abbrev, abbrev.lower())}.png"


async def get_logo_file(abbrev):
    if not abbrev:
        return None

    abbrev = str(abbrev).upper()
    data = LOGO_CACHE.get(abbrev)

    if data is None:
        # Misslyckades nyligen – vänta innan vi försöker igen, så att
        # vi inte slösar ett nätverksanrop var 8:e sekund.
        failed_at = LOGO_FAILED_AT.get(abbrev)
        if failed_at is not None and time.monotonic() - failed_at < LOGO_RETRY_SECONDS:
            return None

        try:
            data = await api.get_bytes(logo_url(abbrev))
        except Exception as exc:
            LOGO_FAILED_AT[abbrev] = time.monotonic()
            log.warning("⚠️ Kunde inte hämta lag-logga %s: %s", abbrev, exc)
            return None

        LOGO_CACHE[abbrev] = data
        LOGO_FAILED_AT.pop(abbrev, None)
        log.debug("🖼️ Lag-logga cachad: %s", abbrev)

    return discord.File(io.BytesIO(data), filename=f"{abbrev}.png")


# ============================================================
# SKICKA TILL DISCORD
# ============================================================

async def get_channel():
    """Försöker först cache, annars hämtas kanalen direkt från Discord."""
    channel = client.get_channel(CHANNEL_ID)
    if channel is not None:
        return channel

    try:
        return await client.fetch_channel(CHANNEL_ID)
    except Exception as exc:
        log.error("❌ Kunde inte hämta Discord-kanal: %s", exc)
        return None


async def deliver(embed, logo_abbrev=None, target=None):
    """
    Skickar en embed (med lag-logga som thumbnail om den finns).
    `target` kan vara en kanal eller interaction.followup – båda har
    .send(embed=..., file=...). Utan target används bevakningskanalen.
    """
    if target is None:
        target = await get_channel()
        if target is None:
            return

    logo_file = await get_logo_file(logo_abbrev) if logo_abbrev else None

    if logo_file is not None:
        embed.set_thumbnail(url=f"attachment://{str(logo_abbrev).upper()}.png")
        await target.send(embed=embed, file=logo_file)
    else:
        await target.send(embed=embed)


def new_embed(title, description=None, color=0x1E90FF):
    return discord.Embed(
        title=title,
        description=description,
        color=color,
        timestamp=datetime.now(timezone.utc),
    )


# ============================================================
# LINEUP
# ============================================================
#
# I /boxscore ligger spelarna på toppnivå, uppdelat per lag och
# position:
#   box["playerByGameStats"]["awayTeam"]["forwards" | "defense" | "goalies"]
#
# OBS: listan är inte sorterad efter kedjor, så vi visar inte
# påhittade femmor – bara vilka som är uppställda.

def format_player_short(player):
    number = player.get("sweaterNumber")
    name = player_name(player)
    return f"#{number} {name}" if number is not None else name


def build_lineup_from_boxscore(box):
    result = {}
    by_game = box.get("playerByGameStats", {}) or {}

    for side_key in ("awayTeam", "homeTeam"):
        team = box.get(side_key, {}) or {}
        abbrev = team.get("abbrev", side_key)
        side = by_game.get(side_key, {}) or {}

        scratches = [
            player_name(s) if isinstance(s, dict) else str(s)
            for s in (team.get("scratches") or side.get("scratches") or [])
        ]

        result[abbrev] = {
            "forwards": [format_player_short(p) for p in side.get("forwards", []) or []],
            "defense": [format_player_short(p) for p in side.get("defense", []) or []],
            "goalies": [format_player_short(p) for p in side.get("goalies", []) or []],
            "scratches": scratches,
        }

    return result


def lineup_has_data(lineup):
    return any(
        side.get("forwards") or side.get("defense") or side.get("goalies")
        for side in lineup.values()
    )


def lineup_signature(lineup):
    return {
        team: {key: sorted(values.get(key, [])) for key in values}
        for team, values in lineup.items()
    }


def lineup_to_text(side):
    parts = []

    for key, label in (
        ("forwards", "Forwards"),
        ("defense", "Backar"),
        ("goalies", "Målvakter"),
        ("scratches", "Scratches"),
    ):
        players = side.get(key, [])
        if players:
            parts.append(f"**{label}**\n" + ", ".join(players))

    text = "\n\n".join(parts).strip() or "Ingen lineup-data tillgänglig ännu."

    if len(text) > 1024:
        text = text[:1021] + "..."
    return text


def build_lineup_embed(game_id, box, lineup):
    away_abbr, home_abbr, _, _ = get_teams(box)

    embed = new_embed(
        f"📋 Lineups – {away_abbr} @ {home_abbr}",
        "Senaste tillgängliga lineup från NHL:s boxscore.",
        team_color(home_abbr),
    )

    embed.add_field(
        name=f"{away_abbr} lineup",
        value=lineup_to_text(lineup.get(away_abbr, {})),
        inline=False,
    )
    embed.add_field(
        name=f"{home_abbr} lineup",
        value=lineup_to_text(lineup.get(home_abbr, {})),
        inline=False,
    )
    embed.set_footer(text=f"Game ID: {game_id}")

    return embed, home_abbr


async def check_pregame_lineup(game):
    """
    Före match: leta bara efter lineup inom LINEUP_WINDOW_MINUTES före
    nedsläpp, och högst var LINEUP_INTERVAL:e sekund per match.
    Skickar en notis första gången lineupen dyker upp och när den ändras.
    """
    game_id = game.get("id")

    start = parse_utc(game.get("startTimeUTC"))
    if start is not None:
        minutes_left = (start - datetime.now(timezone.utc)).total_seconds() / 60
        if minutes_left > LINEUP_WINDOW_MINUTES:
            return

    now = time.monotonic()
    last = LAST_LINEUP_CHECK.get(game_id)
    if last is not None and now - last < LINEUP_INTERVAL:
        return
    LAST_LINEUP_CHECK[game_id] = now

    try:
        box = await fetch_boxscore(game_id)
    except Exception as exc:
        log.debug("ℹ️ Ingen lineup ännu för %s: %s", game_id, exc)
        return

    lineup = build_lineup_from_boxscore(box)
    if not lineup_has_data(lineup):
        return

    old = LATEST_LINEUPS.get(game_id)
    LATEST_LINEUPS[game_id] = lineup

    if old is None or lineup_signature(old) != lineup_signature(lineup):
        embed, logo = build_lineup_embed(game_id, box, lineup)
        await deliver(embed, logo)
        log.info("📋 Lineup skickad för %s", game_id)


# ============================================================
# MÅL
# ============================================================

STRENGTH_NAMES = {
    "EV": "Fullt lag",
    "PP": "Powerplay",
    "SH": "Boxplay",
    "EN": "Tom kasse",
    "PS": "Straffslag",
}


def goal_key(goal, period_number):
    return str(
        goal.get("eventId")
        or f"{period_number}:{goal.get('timeInPeriod')}:{goal.get('playerId')}"
    )


def extract_goals(landing):
    """Mål från landing -> summary -> scoring."""
    goals = []

    for period_block in (landing.get("summary", {}) or {}).get("scoring", []) or []:
        descriptor = period_block.get("periodDescriptor", {}) or {}
        period_number = descriptor.get("number", 0)
        period_type = descriptor.get("periodType", "REG")

        for goal in period_block.get("goals", []) or []:

            scorer = player_name(goal)
            goals_to_date = goal.get("goalsToDate")

            assists = []
            for assist in goal.get("assists", []) or []:
                name = player_name(assist)
                if assist.get("assistsToDate") is not None:
                    name += f" ({assist['assistsToDate']})"
                assists.append(name)

            goals.append({
                "id": goal_key(goal, period_number),
                "team": text_value(goal.get("teamAbbrev"), "NHL"),
                "scorer": scorer,
                "goals_to_date": goals_to_date,
                "assists": assists,
                "strength": str(goal.get("strength") or "EV").upper(),
                "time": goal.get("timeInPeriod", "0:00"),
                "period": period_number,
                "period_type": period_type,
                "home_score": goal.get("homeScore"),
                "away_score": goal.get("awayScore"),
                "shot_type": goal.get("shotType"),
                "modifier": goal.get("goalModifier"),
            })

    return goals


def score_line(landing, away_score=None, home_score=None):
    away_abbr, home_abbr, away, home = get_teams(landing)
    if away_score is None:
        away_score = away.get("score", 0)
    if home_score is None:
        home_score = home.get("score", 0)
    return f"**{away_abbr} {away_score} – {home_score} {home_abbr}**"


def build_goal_embed(game_id, landing, goal):
    scorer = goal["scorer"]
    if goal.get("goals_to_date") is not None:
        scorer += f" ({goal['goals_to_date']})"

    assists = ", ".join(goal.get("assists") or []) or "Oassisterat"

    strength = STRENGTH_NAMES.get(goal.get("strength"), goal.get("strength", "EV"))
    modifier = goal.get("modifier")
    if modifier and str(modifier).lower() != "none":
        strength += f" • {str(modifier).replace('-', ' ')}"

    embed = new_embed(
        "🥅 MÅL!",
        f"**{scorer}** ({goal['team']})\nAssist: {assists}",
        team_color(goal["team"]),
    )

    embed.add_field(
        name="Ställning",
        value=score_line(landing, goal.get("away_score"), goal.get("home_score")),
        inline=False,
    )
    embed.add_field(
        name="Tid",
        value=(
            f"{period_label(goal.get('period'), goal.get('period_type'), is_playoffs(landing))}"
            f" • {goal.get('time', '0:00')}"
        ),
        inline=True,
    )
    embed.add_field(name="Måltyp", value=strength, inline=True)

    if goal.get("shot_type"):
        embed.add_field(
            name="Skott",
            value=str(goal["shot_type"]).replace("-", " ").capitalize(),
            inline=True,
        )

    embed.set_footer(text=f"Game ID: {game_id}")

    # Logga för laget som gjorde målet (inte alltid hemmalaget).
    return embed, goal["team"]


def build_disallowed_embed(game_id, landing, goal):
    label = period_label(goal.get("period"), goal.get("period_type"), is_playoffs(landing))

    embed = new_embed(
        "❌ MÅLET BORTDÖMT",
        (
            f"Målet av **{goal['scorer']}** ({goal['team']}) "
            f"i {label.lower()} • {goal.get('time', '0:00')} har tagits bort."
        ),
        0x7F8C8D,
    )
    embed.add_field(name="Ny ställning", value=score_line(landing), inline=False)
    embed.set_footer(text=f"Game ID: {game_id}")

    return embed, goal["team"]


# ============================================================
# UTVISNINGAR (PLAY-BY-PLAY)
# ============================================================
#
# typeDescKey i play-by-play är gemener med bindestreck, t.ex.
# "penalty", "goal", "shot-on-goal", "blocked-shot", "faceoff".
# Vi behöver bara utvisningarna härifrån – mål tas från landing.

PENALTY_TYPE_NAMES = {
    "MIN": "Liten bestraffning",
    "MAJ": "Stor bestraffning",
    "MIS": "Personligt straff",
    "GAM": "Matchstraff",
    "MAT": "Matchstraff",
    "BEN": "Lagstraff",
    "PS": "Straffslag",
}

PENALTY_NAMES = {
    "high-sticking-double-minor": "High-sticking (dubbel)",
    "delaying-game-puck-over-glass": "Delay of game (pucken över sargen)",
    "delaying-game-unsuccessful-challenge": "Delay of game (misslyckad challenge)",
    "too-many-men-on-the-ice": "Too many men on the ice",
    "unsportsmanlike-conduct": "Unsportsmanlike conduct",
    "game-misconduct": "Game misconduct",
}


def penalty_name(desc_key):
    key = str(desc_key or "").lower().strip()
    if not key:
        return "Utvisning"
    return PENALTY_NAMES.get(key, key.replace("-", " ").replace("_", " ").capitalize())


def build_roster_lookup(pbp):
    lookup = {}
    for player in pbp.get("rosterSpots", []) or []:
        player_id = player.get("playerId")
        if player_id is None:
            continue
        lookup[str(player_id)] = {
            "name": player_name(player),
            "number": player.get("sweaterNumber"),
            "team_id": player.get("teamId"),
        }
    return lookup


def lookup_player(roster, player_id):
    if player_id is None:
        return None
    player = roster.get(str(player_id))
    if not player:
        return None
    if player.get("number") is not None:
        return f"#{player['number']} {player['name']}"
    return player["name"]


def extract_penalties(pbp):
    roster = build_roster_lookup(pbp)
    penalties = []

    for play in pbp.get("plays", []) or []:
        if str(play.get("typeDescKey", "")).lower() != "penalty":
            continue

        details = play.get("details", {}) or {}
        descriptor = play.get("periodDescriptor", {}) or {}

        committed_id = details.get("committedByPlayerId")
        served_id = details.get("servedByPlayerId")

        team_id = details.get("eventOwnerTeamId")
        if team_id is None and committed_id is not None:
            team_id = (roster.get(str(committed_id)) or {}).get("team_id")

        try:
            minutes = int(float(details.get("duration") or 2))
        except (TypeError, ValueError):
            minutes = 2

        penalties.append({
            "id": str(play.get("eventId") or f"{descriptor.get('number')}:{play.get('timeInPeriod')}:penalty"),
            "player": lookup_player(roster, committed_id),
            "served_by": lookup_player(roster, served_id) if served_id != committed_id else None,
            "drawn_by": lookup_player(roster, details.get("drawnByPlayerId")),
            "penalty": penalty_name(details.get("descKey")),
            "type_code": str(details.get("typeCode") or "").upper(),
            "minutes": minutes,
            "team_id": team_id,
            "period": descriptor.get("number", "?"),
            "period_type": descriptor.get("periodType", "REG"),
            "time": play.get("timeInPeriod", "0:00"),
        })

    return penalties


def build_penalty_embed(game_id, landing, penalty):
    away_abbr, home_abbr, _, _ = get_teams(landing)

    team_abbr = resolve_team_abbrev(landing, penalty.get("team_id")) or home_abbr

    # Lagstraff (t.ex. too many men) saknar ofta "committedBy".
    who = penalty.get("player") or f"{team_abbr} (lagstraff)"

    description = f"**{who}** ({team_abbr})\n{penalty['minutes']} min"
    if penalty.get("served_by"):
        description += f"\nAvtjänas av: {penalty['served_by']}"
    if penalty.get("drawn_by"):
        description += f"\nDragen av: {penalty['drawn_by']}"

    embed = new_embed("🚨 UTVISNING!", description, team_color(team_abbr))

    embed.add_field(name="Match", value=f"{away_abbr} @ {home_abbr}", inline=False)
    embed.add_field(
        name="Tid",
        value=(
            f"{period_label(penalty['period'], penalty['period_type'], is_playoffs(landing))}"
            f" • {penalty['time']}"
        ),
        inline=True,
    )

    kind = PENALTY_TYPE_NAMES.get(penalty.get("type_code"))
    value = penalty["penalty"]
    if kind:
        value += f"\n{kind}"
    embed.add_field(name="Utvisning", value=value, inline=True)

    embed.set_footer(text=f"Game ID: {game_id}")

    # Logga för laget som fick utvisningen.
    return embed, team_abbr


# ============================================================
# PERIODER
# ============================================================

def build_period_start_embed(game_id, landing, period, period_type):
    away_abbr, home_abbr, _, _ = get_teams(landing)
    label = period_label(period, period_type, is_playoffs(landing))

    if period == 1 and str(period_type).upper() == "REG":
        title = f"🏒 NEDSLÄPP – {away_abbr} @ {home_abbr}"
        description = "Matchen har börjat!"
    else:
        title = f"▶️ {label} startar"
        description = f"{away_abbr} @ {home_abbr}\n{score_line(landing)}"

    embed = new_embed(title, description, team_color(home_abbr))
    embed.set_footer(text=f"Game ID: {game_id}")
    return embed, home_abbr


def build_period_end_embed(game_id, landing, period, period_type):
    away_abbr, home_abbr, away, home = get_teams(landing)
    label = period_label(period, period_type, is_playoffs(landing))

    embed = new_embed(
        f"⏸️ Periodslut – {label}",
        score_line(landing),
        team_color(home_abbr),
    )

    if away.get("sog") is not None or home.get("sog") is not None:
        embed.add_field(
            name="Skott",
            value=f"{away_abbr} {away.get('sog', '?')} – {home.get('sog', '?')} {home_abbr}",
            inline=False,
        )

    embed.set_footer(text=f"Game ID: {game_id}")
    return embed, home_abbr


# ============================================================
# STATISTIK
# ============================================================
#
# Direkt på lagobjektet i /boxscore finns i princip bara "sog".
# Hits, blocks, powerplay, PIM och tekningar ligger i
# /gamecenter/{id}/right-rail -> "teamGameStats":
#   [{"category": "hits", "awayValue": 20, "homeValue": 18}, ...]
# Om right-rail saknas räknar vi ihop det vi kan från spelarna.

STAT_ROWS = (
    ("sog", "Skott"),
    ("powerPlay", "Powerplay"),
    ("pim", "Utvisningsmin"),
    ("hits", "Hits"),
    ("blockedShots", "Blocks"),
    ("faceoffWinningPctg", "Tekningar"),
    ("giveaways", "Pucktapp"),
    ("takeaways", "Puckvinster"),
)


def stats_from_right_rail(rail):
    stats = {}
    for row in (rail or {}).get("teamGameStats", []) or []:
        category = row.get("category")
        if category:
            stats[category] = (row.get("awayValue"), row.get("homeValue"))
    return stats


def stats_from_boxscore(box):
    """Reservväg: summera spelarnas statistik från boxscore."""
    by_game = box.get("playerByGameStats", {}) or {}
    sums = {"sog": [], "pim": [], "hits": [], "blockedShots": [], "powerPlayGoals": []}

    for side_key in ("awayTeam", "homeTeam"):
        side = by_game.get(side_key, {}) or {}
        skaters = (side.get("forwards") or []) + (side.get("defense") or [])

        for key in ("pim", "hits", "blockedShots", "powerPlayGoals"):
            values = [p.get(key) for p in skaters if p.get(key) is not None]
            sums[key].append(sum(values) if values else None)

        sums["sog"].append((box.get(side_key, {}) or {}).get("sog"))

    stats = {}
    for key, (away, home) in sums.items():
        if away is not None or home is not None:
            stats[key] = (away, home)

    if "powerPlayGoals" in stats:
        stats["powerPlay"] = tuple(
            f"{v} mål" if v is not None else None for v in stats.pop("powerPlayGoals")
        )

    return stats


def format_stat(category, value):
    if value is None or value == "":
        return "–"
    if category == "faceoffWinningPctg":
        try:
            value = float(value)
            return f"{value * 100:.1f}%" if value <= 1 else f"{value:.1f}%"
        except (TypeError, ValueError):
            return str(value)
    return str(value)


def format_team_stats(stats, away_abbr, home_abbr):
    lines = [f"`{'':<14}` **{away_abbr}** – **{home_abbr}**"]

    for category, label in STAT_ROWS:
        if category not in stats:
            continue
        away, home = stats[category]
        lines.append(
            f"`{label:<14}` {format_stat(category, away)} – {format_stat(category, home)}"
        )

    if len(lines) == 1:
        return "Ingen lagstatistik rapporterad av NHL."
    return "\n".join(lines)


def goalie_line(goalie):
    name = player_name(goalie)

    saves = goalie.get("saves")
    shots = goalie.get("shotsAgainst")

    # Äldre/andra format: "saveShotsAgainst": "25/27"
    if (saves is None or shots is None) and goalie.get("saveShotsAgainst"):
        try:
            saves, shots = (int(x) for x in str(goalie["saveShotsAgainst"]).split("/"))
        except ValueError:
            pass

    save_pct = goalie.get("savePctg")
    if save_pct is None and saves is not None and shots:
        save_pct = saves / shots

    if saves is None and shots is None:
        return f"**{name}** – ingen räddningsstatistik"

    text = f"**{name}** – {saves}/{shots} räddningar"
    if isinstance(save_pct, (int, float)):
        text += f" ({save_pct:.3f})".replace("(0.", "(.")
    if goalie.get("decision"):
        text += f" • {goalie['decision']}"
    return text


def goalies_who_played(box, side_key):
    goalies = (
        ((box.get("playerByGameStats", {}) or {}).get(side_key, {}) or {})
        .get("goalies", [])
        or []
    )
    played = [
        g for g in goalies
        if g.get("starter") or clock_to_seconds(g.get("toi", "00:00")) not in (None, 0)
    ]
    return played or goalies[:1]


def format_goalies(box, side_key):
    goalies = goalies_who_played(box, side_key)
    if not goalies:
        return "Ingen målvaktsstatistik rapporterad av NHL."
    return "\n".join(goalie_line(g) for g in goalies)


def extract_three_stars(landing):
    stars = []
    for star in (landing.get("summary", {}) or {}).get("threeStars", []) or []:
        name = player_name(star)
        team = text_value(star.get("teamAbbrev"))
        line = f"{name} ({team})" if team else name

        goals, assists = star.get("goals"), star.get("assists")
        if goals is not None and assists is not None:
            line += f" – {goals}+{assists}"
        elif star.get("savePctg") is not None:
            line += f" – {star['savePctg']:.3f}".replace(" 0.", " .")
        stars.append(line)
    return stars[:3]


def extract_top_scorers(box, limit=3):
    by_game = box.get("playerByGameStats", {}) or {}
    players = []

    for side_key in ("awayTeam", "homeTeam"):
        abbrev = (box.get(side_key, {}) or {}).get("abbrev", "")
        side = by_game.get(side_key, {}) or {}

        for p in (side.get("forwards") or []) + (side.get("defense") or []):
            goals = p.get("goals") or 0
            assists = p.get("assists") or 0
            points = p.get("points") or goals + assists
            if points > 0:
                players.append({
                    "name": player_name(p),
                    "team": abbrev,
                    "goals": goals,
                    "assists": assists,
                    "points": points,
                })

    players.sort(key=lambda x: (x["points"], x["goals"]), reverse=True)
    return players[:limit]


# ============================================================
# SLUTRAPPORT
# ============================================================

async def send_final_report(game_id, landing=None):
    if game_id in FINAL_SENT:
        return

    try:
        if landing is None:
            landing = await fetch_landing(game_id)

        box = await fetch_boxscore(game_id)

        try:
            rail = await fetch_right_rail(game_id)
        except Exception as exc:
            log.warning("⚠️ right-rail saknas för %s: %s", game_id, exc)
            rail = None

        away_abbr, home_abbr, away, home = get_teams(landing)
        away_score = away.get("score", 0)
        home_score = home.get("score", 0)

        last_period_type = (
            (landing.get("gameOutcome", {}) or {}).get("lastPeriodType")
            or (landing.get("periodDescriptor", {}) or {}).get("periodType")
            or "REG"
        ).upper()
        suffix = {"OT": " (efter övertid)", "SO": " (efter straffar)"}.get(last_period_type, "")

        winner = away_abbr if away_score > home_score else home_abbr

        embed = new_embed(
            f"🏁 SLUTRESULTAT – {away_abbr} @ {home_abbr}",
            f"{score_line(landing)}{suffix}",
            team_color(winner),
        )

        stats = stats_from_right_rail(rail) or stats_from_boxscore(box)
        embed.add_field(
            name="📊 Statistik",
            value=format_team_stats(stats, away_abbr, home_abbr)[:1024],
            inline=False,
        )

        embed.add_field(
            name=f"🧤 {away_abbr} målvakt",
            value=format_goalies(box, "awayTeam")[:1024],
            inline=False,
        )
        embed.add_field(
            name=f"🧤 {home_abbr} målvakt",
            value=format_goalies(box, "homeTeam")[:1024],
            inline=False,
        )

        stars = extract_three_stars(landing)
        if stars:
            embed.add_field(
                name="⭐ Three Stars",
                value="\n".join(f"{i + 1}. {s}" for i, s in enumerate(stars))[:1024],
                inline=False,
            )
        else:
            top = extract_top_scorers(box)
            if top:
                embed.add_field(
                    name="🏒 Matchens poängplockare",
                    value="\n".join(
                        f"**{p['name']}** ({p['team']}) – "
                        f"{p['goals']}+{p['assists']} ({p['points']} p)"
                        for p in top
                    )[:1024],
                    inline=False,
                )

        embed.set_footer(text=f"Game ID: {game_id}")

        # Vinnarens logga.
        await deliver(embed, winner)
        FINAL_SENT.add(game_id)
        log.info("🏁 Slutrapport skickad för %s", game_id)

    except Exception as exc:
        log.exception("❌ Kunde inte skicka slutrapport %s: %s", game_id, exc)


# ============================================================
# GAME TRACKER
# ============================================================

class GameTracker:

    def __init__(self, game_id):
        self.game_id = game_id
        self.task = None

        self.last_state = None
        self.last_score_text = None
        self.last_period_text = None

        # Mål som redan skickats: id -> goal-dict (behövs för bortdömda mål)
        self.seen_goals = {}

        # Mål-id -> antal hämtningar i rad som målet saknats
        self.missing_goal_counts = {}

        # Utvisningar som redan behandlats
        self.seen_penalty_ids = set()

        # Periodnotiser som redan skickats, t.ex. "start:2", "end:2"
        self.period_events = set()

        # Hindrar gamla händelser från att skickas när boten startar
        # mitt i en match.
        self.initialized = False

    # --------------------------------------------------------
    # MÅL
    # --------------------------------------------------------

    async def handle_goals(self, landing):
        if "summary" not in landing:
            # Ofullständigt svar – dra inga slutsatser om bortdömda mål.
            return

        goals = extract_goals(landing)
        current = {goal["id"]: goal for goal in goals}

        log.debug("🥅 Mål i landing %s: %d", self.game_id, len(goals))

        if not self.initialized:
            self.seen_goals = dict(current)
            return

        for goal_id, goal in current.items():
            self.missing_goal_counts.pop(goal_id, None)

            if goal_id in self.seen_goals:
                # Uppdatera (t.ex. ändrad målskytt/assist) utan ny notis.
                self.seen_goals[goal_id] = goal
                continue

            self.seen_goals[goal_id] = goal
            try:
                embed, logo = build_goal_embed(self.game_id, landing, goal)
                await deliver(embed, logo)
                log.info("🥅 Mål: %s (%s)", goal["scorer"], goal["team"])
            except Exception as exc:
                log.exception("❌ Målnotis-fel %s: %s", self.game_id, exc)

        # Bortdömda mål: fanns förut, saknas nu i flera hämtningar i rad.
        for goal_id in list(self.seen_goals):
            if goal_id in current:
                continue

            count = self.missing_goal_counts.get(goal_id, 0) + 1
            self.missing_goal_counts[goal_id] = count

            if count < DISALLOWED_CONFIRMATIONS:
                continue

            goal = self.seen_goals.pop(goal_id)
            self.missing_goal_counts.pop(goal_id, None)

            try:
                embed, logo = build_disallowed_embed(self.game_id, landing, goal)
                await deliver(embed, logo)
                log.info("❌ Mål bortdömt: %s (%s)", goal["scorer"], goal["team"])
            except Exception as exc:
                log.exception("❌ Bortdömt-notis-fel %s: %s", self.game_id, exc)

    # --------------------------------------------------------
    # PERIODER
    # --------------------------------------------------------

    async def handle_periods(self, landing):
        descriptor = landing.get("periodDescriptor", {}) or {}
        period = descriptor.get("number")
        period_type = descriptor.get("periodType", "REG")

        if not period:
            return

        clock = landing.get("clock", {}) or {}
        in_intermission = bool(clock.get("inIntermission"))

        self.last_period_text = period_label(period, period_type, is_playoffs(landing))
        if in_intermission:
            self.last_period_text += " (paus)"
        elif clock.get("timeRemaining"):
            self.last_period_text += f" • {clock['timeRemaining']} kvar"

        start_key = f"start:{period}"
        end_key = f"end:{period}"

        if not self.initialized:
            # Markera allt som redan har hänt som skickat...
            for p in range(1, int(period)):
                self.period_events.update({f"start:{p}", f"end:{p}"})
            if in_intermission:
                self.period_events.update({start_key, end_key})
                return

            # ...förutom om vi precis har missat nedsläppet i period 1.
            remaining = clock_to_seconds(clock.get("timeRemaining"))
            fresh_start = (
                int(period) == 1
                and str(period_type).upper() == "REG"
                and remaining is not None
                and remaining >= 19 * 60
            )
            if not fresh_start:
                self.period_events.add(start_key)
                return

        if not in_intermission and start_key not in self.period_events:
            self.period_events.add(start_key)
            embed, logo = build_period_start_embed(self.game_id, landing, int(period), period_type)
            await deliver(embed, logo)
            log.info("▶️ Periodstart %s i %s", period, self.game_id)

        if in_intermission and end_key not in self.period_events:
            self.period_events.update({start_key, end_key})
            embed, logo = build_period_end_embed(self.game_id, landing, int(period), period_type)
            await deliver(embed, logo)
            log.info("⏸️ Periodslut %s i %s", period, self.game_id)

    # --------------------------------------------------------
    # UTVISNINGAR
    # --------------------------------------------------------

    async def handle_penalties(self, landing):
        try:
            pbp = await fetch_play_by_play(self.game_id)
        except Exception as exc:
            log.warning("⚠️ PBP API-fel %s: %s", self.game_id, exc)
            return

        penalties = extract_penalties(pbp)
        log.debug("📜 PBP %s: %d utvisningar", self.game_id, len(penalties))

        if not self.initialized:
            self.seen_penalty_ids = {p["id"] for p in penalties}
            return

        for penalty in penalties:
            if penalty["id"] in self.seen_penalty_ids:
                continue
            self.seen_penalty_ids.add(penalty["id"])

            try:
                embed, logo = build_penalty_embed(self.game_id, landing, penalty)
                await deliver(embed, logo)
                log.info(
                    "🚨 Utvisning: %s – %s (%d min)",
                    penalty.get("player"), penalty["penalty"], penalty["minutes"],
                )
            except Exception as exc:
                log.exception("❌ Utvisningsnotis-fel %s: %s", self.game_id, exc)

    # --------------------------------------------------------
    # LOOP
    # --------------------------------------------------------

    async def run(self):
        log.info("🔄 Startar bevakning av match %s", self.game_id)

        try:
            while True:
                try:
                    landing = await fetch_landing(self.game_id)
                    state = game_state(landing)
                    self.last_state = state
                    self.last_score_text = score_line(landing).replace("**", "")

                    log.debug("🏒 %s | %s", self.last_score_text, state)

                    await self.handle_periods(landing)
                    await self.handle_goals(landing)
                    await self.handle_penalties(landing)

                    self.initialized = True

                    if is_final_state(state):
                        await send_final_report(self.game_id, landing=landing)
                        break

                except Exception as exc:
                    log.exception("❌ Live-loop fel %s: %s", self.game_id, exc)

                await asyncio.sleep(LIVE_INTERVAL)

        finally:
            ACTIVE_GAMES.pop(self.game_id, None)
            log.info("⏹️ Avslutar bevakning av %s", self.game_id)


async def start_game(game_id):
    if game_id in ACTIVE_GAMES:
        return

    tracker = GameTracker(game_id)
    ACTIVE_GAMES[game_id] = tracker
    tracker.task = asyncio.create_task(tracker.run(), name=f"nhl-game-{game_id}")


# ============================================================
# MATCH MANAGER
# ============================================================

async def match_manager():
    await client.wait_until_ready()
    log.info("📡 Match Manager kör.")

    while not client.is_closed():
        try:
            scoreboard = await fetch_scoreboard()
            games = scoreboard.get("games", []) or []

            LAST_SCOREBOARD[:] = games
            live_count = 0

            for game in games:
                game_id = game.get("id")
                if not game_id:
                    continue

                state = game_state(game)
                away_abbr, home_abbr, _, _ = get_teams(game)
                log.debug("   GAME %s: %s @ %s | %s", game_id, away_abbr, home_abbr, state)

                if is_live_state(state):
                    live_count += 1
                    await start_game(game_id)

                elif is_pregame_state(state):
                    await check_pregame_lineup(game)

            log.info(
                "📡 Scheduler: %d matcher, %d live, %d bevakas.",
                len(games), live_count, len(ACTIVE_GAMES),
            )

        except Exception as exc:
            log.exception("❌ Scheduler-fel: %s", exc)

        await asyncio.sleep(SCOREBOARD_INTERVAL)


# ============================================================
# DISCORD-KLIENT
# ============================================================

class NhlBot(discord.Client):

    def __init__(self):
        # Inga privilegierade intents behövs – slash-kommandon
        # kräver inte message_content.
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)
        self.manager_task = None

    async def setup_hook(self):
        await api.start()

        if GUILD_ID:
            guild = discord.Object(id=int(GUILD_ID))
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
        else:
            synced = await self.tree.sync()
        log.info("✅ %d slash-kommandon syncade.", len(synced))

        # setup_hook körs bara en gång, till skillnad från on_ready
        # som körs igen vid varje återanslutning.
        self.manager_task = asyncio.create_task(match_manager(), name="nhl-match-manager")

    async def on_ready(self):
        log.info("=" * 60)
        log.info("🏒 NHL Discord Bot online som %s", self.user)
        log.info("📅 Svenskt datum: %s", today_str())
        log.info("📺 Kanal-ID: %s", CHANNEL_ID)
        log.info("=" * 60)

    async def close(self):
        await api.close()
        await super().close()


client = NhlBot()


# ============================================================
# SLASH-KOMMANDON
# ============================================================

@client.tree.command(name="ping", description="Kolla att NHL-boten är online.")
async def ping_command(interaction: discord.Interaction):
    await interaction.response.send_message(
        f"🏒 Pong! NHL-boten är online ({round(client.latency * 1000)} ms)."
    )


@client.tree.command(name="status", description="Visa vilka matcher som bevakas just nu.")
async def status_command(interaction: discord.Interaction):
    if not ACTIVE_GAMES:
        await interaction.response.send_message("📡 Inga matcher bevakas just nu.")
        return

    lines = ["📡 **Aktiva NHL-matcher**"]
    for game_id, tracker in ACTIVE_GAMES.items():
        score = tracker.last_score_text or "–"
        period = tracker.last_period_text or tracker.last_state or "UNKNOWN"
        lines.append(f"• {score} — {period} (`{game_id}`)")

    await interaction.response.send_message("\n".join(lines))


@client.tree.command(name="lineup", description="Visa lineups för dagens pågående/kommande matcher.")
@app_commands.describe(lag="Lagförkortning, t.ex. TOR (valfritt)")
async def lineup_command(interaction: discord.Interaction, lag: str | None = None):
    await interaction.response.defer(thinking=True)

    team_filter = lag.strip().upper() if lag else None

    games = [
        g for g in LAST_SCOREBOARD
        if game_state(g) in {"PRE", "LIVE", "CRIT"}
        and (
            team_filter is None
            or team_filter in {get_teams(g)[0].upper(), get_teams(g)[1].upper()}
        )
    ]

    sent = 0
    for game in games:
        game_id = game.get("id")
        try:
            box = await fetch_boxscore(game_id)
            lineup = build_lineup_from_boxscore(box)
            if not lineup_has_data(lineup):
                continue
            embed, logo = build_lineup_embed(game_id, box, lineup)
            await deliver(embed, logo, target=interaction.followup)
            sent += 1
        except Exception as exc:
            log.warning("❌ /lineup-fel %s: %s", game_id, exc)

    if sent == 0:
        await interaction.followup.send("❌ Ingen lineup-data tillgänglig ännu.")


@client.tree.command(name="testmal", description="Skicka en testnotis för ett mål.")
async def test_goal_command(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True)

    landing = {
        "awayTeam": {"abbrev": "CBJ", "score": 1},
        "homeTeam": {"abbrev": "TOR", "score": 0},
    }
    goal = {
        "team": "CBJ",
        "scorer": "Testspelare",
        "goals_to_date": 1,
        "assists": ["Assistspelare (1)"],
        "strength": "PP",
        "time": "12:34",
        "period": 1,
        "period_type": "REG",
        "away_score": 1,
        "home_score": 0,
        "shot_type": "wrist",
        "modifier": None,
    }

    embed, logo = build_goal_embed("TEST", landing, goal)
    await deliver(embed, logo, target=interaction.followup)


# ============================================================
# START
# ============================================================

if __name__ == "__main__":
    if not TOKEN:
        raise RuntimeError("TOKEN saknas. Lägg Discord-token i miljövariabeln TOKEN.")

    # log_handler=None: vi har redan satt upp loggningen ovan.
    client.run(TOKEN, log_handler=None)
