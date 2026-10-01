"""Discord-delen: postar notiser i en kanal och erbjuder slash-kommandon."""

from __future__ import annotations

import asyncio
import logging

import discord
from discord import app_commands

from .api import SHLClient
from .config import Config
from .formatting import EmbedSpec, goalie_table, period_name, scoreline, stat_table
from .models import STOCKHOLM, GameInfo
from .monitor import Monitor
from .swehockey import SweHockeyClient

log = logging.getLogger(__name__)

EMBED_TOTAL_LIMIT = 5800  # Discord tillåter 6000 tecken per meddelande


def to_discord(spec: EmbedSpec) -> discord.Embed:
    embed = discord.Embed(title=spec.title[:256], description=spec.description[:4096] or None, color=spec.color)
    for name, value, inline in spec.fields[:25]:
        embed.add_field(name=name[:256], value=value[:1024], inline=inline)
    if spec.footer:
        embed.set_footer(text=spec.footer[:2048])
    return embed


def split_large(spec: EmbedSpec) -> list[EmbedSpec]:
    """Delar en embed i flera om den blir för stor för ett Discord-meddelande."""
    size = len(spec.title) + len(spec.description) + len(spec.footer or "")
    parts = [EmbedSpec(spec.title, spec.description, spec.color)]
    for f in spec.fields:
        extra = len(f[0]) + len(f[1])
        if size + extra > EMBED_TOTAL_LIMIT:
            parts.append(EmbedSpec(f"{spec.title} (forts.)", color=spec.color))
            size = len(parts[-1].title)
        parts[-1].fields.append(f)
        size += extra
    parts[-1].footer = spec.footer
    return parts


class DiscordSink:
    def __init__(self, client: discord.Client, channel_id: int):
        self.client = client
        self.channel_id = channel_id

    async def _channel(self):
        return self.client.get_channel(self.channel_id) or await self.client.fetch_channel(self.channel_id)

    async def send(self, game: GameInfo, embeds: list[EmbedSpec]) -> list[int | None]:
        channel = await self._channel()
        ids: list[int | None] = []
        for spec in embeds:
            first_id = None
            for part in split_large(spec):
                msg = await channel.send(embed=to_discord(part))  # type: ignore[union-attr]
                first_id = first_id or msg.id
            ids.append(first_id)
        return ids

    async def edit(self, message_id: int, spec: EmbedSpec) -> bool:
        try:
            channel = await self._channel()
            await channel.get_partial_message(message_id).edit(embed=to_discord(spec))  # type: ignore[union-attr]
            return True
        except (discord.HTTPException, AttributeError) as e:
            log.warning("Kunde inte redigera meddelande %s: %s", message_id, e)
            return False


class SHLBot(discord.Client):
    def __init__(self, config: Config):
        super().__init__(intents=discord.Intents.default())
        self.config = config
        self.tree = app_commands.CommandTree(self)
        self.shl: SHLClient | None = None
        self.swe: SweHockeyClient | None = None
        self.monitor: Monitor | None = None
        self._task: asyncio.Task | None = None
        self._register_commands()

    async def setup_hook(self) -> None:
        assert self.config.channel_id, "DISCORD_CHANNEL_ID saknas"
        self.shl = await SHLClient(self.config).__aenter__()
        self.swe = await SweHockeyClient().__aenter__() if self.config.swehockey else None
        self.monitor = Monitor(self.config, self.shl, DiscordSink(self, self.config.channel_id), self.swe)
        if self.config.guild_id:
            guild = discord.Object(id=self.config.guild_id)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()

    async def on_ready(self) -> None:
        log.info("Inloggad som %s", self.user)
        if self._task is None:
            await self.wait_until_ready()
            self._task = asyncio.create_task(self.monitor.run_forever())  # type: ignore[union-attr]

    async def close(self) -> None:
        if self._task:
            self._task.cancel()
        if self.shl:
            await self.shl.close()
        if self.swe:
            await self.swe.close()
        await super().close()

    # -- slash-kommandon ----------------------------------------------------

    def _register_commands(self) -> None:
        @self.tree.command(name="matcher", description="Dagens SHL-matcher")
        async def matcher(interaction: discord.Interaction) -> None:
            await interaction.response.defer()
            mon = self.monitor
            assert mon is not None
            try:
                await mon.refresh_schedule(force=not mon.games)
            except Exception as e:
                await interaction.followup.send(f"Kunde inte hämta schemat: {e}")
                return
            games = mon.todays_games()
            if not games:
                await interaction.followup.send("Inga SHL-matcher idag.")
                return
            lines = []
            for g in games:
                t = mon.trackers.get(g.uuid)
                st = t.last_status if t else None
                when = g.start.astimezone(STOCKHOLM).strftime("%H:%M") if g.start else "?"
                if st and st.home_score is not None:
                    score = scoreline(g, (st.home_score, st.away_score or 0))
                    extra = " (slut)" if st.phase == "final" else (
                        f" ({period_name(st.period)} {st.clock or ''})".rstrip() if st.period else ""
                    )
                    lines.append(f"`{when}` **{score}**{extra}")
                elif g.home_score is not None and not g.is_pre_game:
                    lines.append(f"`{when}` **{scoreline(g, (g.home_score, g.away_score or 0))}**")
                else:
                    lines.append(f"`{when}` {g.home.name} – {g.away.name}")
            embed = discord.Embed(title="🏒 Dagens SHL-matcher", description="\n".join(lines), color=0x1ABC9C)
            await interaction.followup.send(embed=embed)

        @self.tree.command(name="stats", description="Aktuell statistik för en pågående match")
        @app_commands.describe(lag="Lagkod, t.ex. FBK eller LHF")
        async def stats(interaction: discord.Interaction, lag: str) -> None:
            mon = self.monitor
            assert mon is not None
            code = lag.strip().upper()
            match = next(
                (
                    t
                    for t in mon.trackers.values()
                    if t.last_stats and code in (t.game.home.code, t.game.away.code)
                ),
                None,
            )
            if not match or not match.last_stats:
                await interaction.response.send_message(f"Hittar ingen bevakad match för {code} just nu.")
                return
            g, s = match.game, match.last_stats
            tot = s.total()
            st = match.last_status
            score = (st.home_score, st.away_score) if st and st.home_score is not None else None
            embed = discord.Embed(
                title=f"📊 {scoreline(g, score or (tot.home.goals, tot.away.goals))}",
                color=0x7289DA,
            )
            embed.add_field(name="Statistik", value=stat_table(g, tot.home, tot.away, s.has_faceoffs, s.official_shots is not None), inline=False)
            gt = goalie_table(g, s)
            if gt:
                embed.add_field(name="Målvakter", value=gt[:1024], inline=False)
            await interaction.response.send_message(embed=embed)
