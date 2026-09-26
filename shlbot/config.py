"""Konfiguration från miljövariabler (och en valfri .env-fil)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # python-dotenv är valfritt
    pass


def _int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    return int(raw) if raw else default


def _optional_int(name: str) -> int | None:
    raw = os.getenv(name, "").strip()
    return int(raw) if raw else None


def _codes(name: str) -> set[str]:
    return {c.strip().upper() for c in os.getenv(name, "").split(",") if c.strip()}


@dataclass
class Config:
    discord_token: str = field(default_factory=lambda: os.getenv("DISCORD_TOKEN", "").strip())
    channel_id: int | None = field(default_factory=lambda: _optional_int("DISCORD_CHANNEL_ID"))
    guild_id: int | None = field(default_factory=lambda: _optional_int("DISCORD_GUILD_ID"))

    # Hur ofta pågående matcher kontrolleras (sekunder)
    poll_interval: int = field(default_factory=lambda: _int("POLL_INTERVAL", 10))
    # Hur ofta spelschemat hämtas om när ingen match är nära (sekunder)
    schedule_interval: int = field(default_factory=lambda: _int("SCHEDULE_INTERVAL", 300))

    # Lagkoder att följa, t.ex. "FBK,LHF". Tomt = alla matcher.
    teams: set[str] = field(default_factory=lambda: _codes("SHL_TEAMS"))
    series_code: str = field(default_factory=lambda: os.getenv("SHL_SERIES_CODE", "SHL").strip().upper())

    base_url: str = field(default_factory=lambda: os.getenv("SHL_BASE_URL", "https://www.shl.se").rstrip("/"))
    overview_path: str = field(
        default_factory=lambda: os.getenv("SHL_OVERVIEW_PATH", "/api/gameday/game-overview/{uuid}")
    )
    pbp_path: str = field(default_factory=lambda: os.getenv("SHL_PBP_PATH", "/api/gameday/play-by-play/{uuid}"))
    # Valfri endpoint med lagstatistik (tekningar m.m.). Tom sträng stänger av.
    team_stats_path: str = field(
        default_factory=lambda: os.getenv("SHL_TEAM_STATS_PATH", "/api/gameday/team-stats/{uuid}")
    )

    # Använd stats.swehockey.se för snabbare ställning, period och paus
    swehockey: bool = field(
        default_factory=lambda: os.getenv("SWEHOCKEY", "1").strip() not in ("0", "false", "no")
    )

    state_file: Path = field(default_factory=lambda: Path(os.getenv("STATE_FILE", "state.json")))
    # Posta startande målvakter när matchen börjar
    post_starting_goalies: bool = field(
        default_factory=lambda: os.getenv("POST_STARTING_GOALIES", "1").strip() not in ("0", "false", "no")
    )

    def follows(self, *codes: str | None) -> bool:
        if not self.teams:
            return True
        return any(c and c.upper() in self.teams for c in codes)
