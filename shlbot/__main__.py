"""Kommandorad:

    python -m shlbot run            Starta Discord-boten
    python -m shlbot run --dry-run  Kör bevakningen men skriv notiser i terminalen
    python -m shlbot games          Lista dagens matcher (med matchens uuid)
    python -m shlbot lineups [DATUM] Testa laguppställningar från stats.swehockey.se
    python -m shlbot compare        Jämför SHL och stats.swehockey.se live
    python -m shlbot probe UUID     Spara rå JSON för en match i probe/UUID/
    python -m shlbot replay DIR     Spela upp sparad data och visa alla notiser
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from datetime import datetime
import sys
from collections import Counter
from pathlib import Path

from .api import NotAvailable, SHLClient, is_playoff
from .config import Config
from .formatting import render
from .models import STOCKHOLM, GameInfo, GameStatus, extract_event_list, parse_events, parse_status
from .monitor import ConsoleSink, Monitor
from .stats import parse_team_stats
from .swehockey import SweHockeyClient
from .tracker import GameTracker


async def cmd_games(cfg: Config) -> None:
    async with SHLClient(cfg) as client:
        mon = Monitor(cfg, client, ConsoleSink())
        await mon.refresh_schedule(force=True)
        games = mon.todays_games()
        if not games:
            print("Inga matcher idag.")
        for g in games:
            when = g.start.strftime("%H:%M") if g.start else "?"
            print(f"{when}  {g.home.code:>5} – {g.away.code:<5} {g.state:<10} {g.uuid}")


async def cmd_probe(cfg: Config, uuid: str) -> None:
    out = Path("probe") / uuid
    out.mkdir(parents=True, exist_ok=True)
    async with SHLClient(cfg) as client:
        games = {g.uuid: g for g in await client.schedule()}
        if uuid in games:
            (out / "game.json").write_text(json.dumps(games[uuid].raw, ensure_ascii=False, indent=2), "utf-8")
        for name, fetch in (
            ("overview", client.overview),
            ("play_by_play", client.play_by_play),
            ("team_stats", client.team_stats),
        ):
            try:
                data = await fetch(uuid)
            except (NotAvailable, RuntimeError) as e:
                print(f"{name}: ej tillgänglig ({e})")
                continue
            (out / f"{name}.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")
            print(f"{name}: sparad")
            if name == "play_by_play":
                raw = extract_event_list(data)
                types = Counter(str(e.get("type") or e.get("eventType")) for e in raw)
                print(f"  {len(raw)} händelser, typer: {dict(types)}")
                keys = Counter(k for e in raw for k in e)
                print(f"  fält: {sorted(keys)}")
                # Visa vilka värden skotten har, för att skilja skott på mål från missar/blockeringar
                shots = [e for e in raw if str(e.get("type", "")).lower().startswith("shot")]
                if shots:
                    print(f"  skott ({len(shots)} st), fält med få olika värden:")
                    for k in sorted({k for e in shots for k in e}):
                        vals = Counter(json.dumps(e.get(k), ensure_ascii=False)[:40] for e in shots)
                        if 1 < len(vals) <= 8:
                            print(f"    {k}: {dict(vals)}")
                    print(f"  exempel på skott: {json.dumps(shots[-1], ensure_ascii=False)[:600]}")
        # Jämför färskhet med och utan cache-parameter
        print("\nJämförelse av hur färsk datan är:")
        for label, bust in (("med tidsstämpel", True), ("utan tidsstämpel", False)):
            client.cache_bust = bust
            try:
                evs = parse_events(await client.play_by_play(uuid))
                last = evs[-1] if evs else None
                ov = await client.overview(uuid)
                st = parse_status(ov, GameInfo.parse({"uuid": uuid}))
                print(
                    f"  {label}: {len(evs)} händelser, senaste "
                    f"{f'P{last.period} {last.time} ({last.kind})' if last else '-'}, "
                    f"klocka P{st.period} {st.clock}, ställning {st.home_score}-{st.away_score}"
                )
            except Exception as e:
                print(f"  {label}: fel ({e})")
    print(f"\nData sparad i {out}/ – kör 'python -m shlbot replay {out}' för att se notiserna.")


async def cmd_compare(cfg: Config, rounds: int) -> None:
    """Visar SHL och stats.swehockey.se sida vid sida för pågående matcher."""
    async with SHLClient(cfg) as client, SweHockeyClient() as swe:
        mon = Monitor(cfg, client, ConsoleSink(), swe)
        await mon.refresh_schedule(force=True)
        now = datetime.now(STOCKHOLM)
        games = [g for g in mon.todays_games() if g.start and g.start <= now and not g.is_finished]
        if not games:
            print("Inga pågående matcher just nu. Kör igen under en matchkväll.")
            return
        print("Jämför SHL och stats.swehockey.se var 10:e sekund (Ctrl+C avbryter).\n")
        for _ in range(rounds):
            print(datetime.now(STOCKHOLM).strftime("%H:%M:%S"))
            for g in games:
                pbp, ov, sw = await asyncio.gather(
                    client.play_by_play(g.uuid), client.overview(g.uuid), mon.swe_game(g), return_exceptions=True
                )
                evs = parse_events(pbp, g) if not isinstance(pbp, BaseException) else []
                st = parse_status(ov if not isinstance(ov, BaseException) else None, g)
                last = next((e for e in reversed(evs) if e.kind not in ("shot",)), None)
                shl = (
                    f"{st.home_score}-{st.away_score} P{st.period} {st.clock or ''}, "
                    f"senaste {f'P{last.period} {last.time} {last.kind}' if last else '-'}"
                )
                if isinstance(sw, BaseException) or sw is None:
                    swe_txt = "ingen data"
                else:
                    goal = sw.goals[-1] if sw.goals else None
                    swe_txt = (
                        f"{sw.home_score}-{sw.away_score} {sw.state_text or ''} {sw.clock or ''}, "
                        f"senaste mål {f'{goal.time} ({goal.home}-{goal.away})' if goal else '-'}"
                    )
                print(f"  {g.home.code}-{g.away.code:<5} SHL: {shl}")
                print(f"  {'':<{len(g.home.code) + len(g.away.code) + 5}} SWE: {swe_txt}")
            print()
            await asyncio.sleep(10)


async def cmd_lineups(cfg: Config, day: str | None) -> None:
    """Visar hur laguppställningarna tolkas och vad boten skulle posta."""
    import tempfile
    from datetime import date as date_cls

    cfg.state_file = Path(tempfile.mkdtemp()) / "state.json"  # rör inte botens riktiga state
    target = date_cls.fromisoformat(day) if day else datetime.now(STOCKHOLM).date()
    async with SHLClient(cfg) as client, SweHockeyClient() as swe:
        mon = Monitor(cfg, client, ConsoleSink(), swe)
        await mon.refresh_schedule(force=True)
        games = [g for g in mon.games.values() if g.start and g.start.astimezone(STOCKHOLM).date() == target]
        if not games:
            print(f"Inga matcher {target}.")
        for g in games:
            print(f"== {g.title}")
            lineup = await mon._lineup(g)
            if not lineup:
                print("  Hittar inte matchen eller laguppställningen på stats.swehockey.se\n")
                continue
            for side, team in (("home", g.home), ("away", g.away)):
                players = list(lineup[side].values())
                print(f"  {team.code}: {len(players)} spelare: {', '.join(players[:6])}{' …' if len(players) > 6 else ''}")
            await mon.check_lineups(g)
            print()


def cmd_replay(directory: Path) -> None:
    """Spelar upp en sparad match händelse för händelse, som om den pågick live."""

    def load(name: str):
        p = directory / f"{name}.json"
        return json.loads(p.read_text("utf-8")) if p.exists() else None

    game_raw = load("game") or {"uuid": directory.name}
    game = GameInfo.parse(game_raw)
    pbp = load("play_by_play") or []
    team_stats = parse_team_stats(load("team_stats")) or None
    events = parse_events(pbp, game)
    tracker = GameTracker(game=game, shootout_period=99 if is_playoff(game) else 5)

    kinds = Counter(e.kind for e in events)
    print(f"# {game.title}: {len(events)} händelser {dict(kinds)}\n")
    tracker.update([], GameStatus("pre"))
    for i in range(1, len(events) + 1):
        visible = events[:i]
        status = GameStatus("live", period=visible[-1].period, clock=visible[-1].time)
        for n in tracker.update(visible, status):
            print(render(n, tracker.shootout_period).as_text(), end="\n\n")
    for n in tracker.update(events, GameStatus("final", period=events[-1].period if events else 3), team_stats):
        print(render(n, tracker.shootout_period).as_text(), end="\n\n")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="shlbot", description="SHL live-bot för Discord")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run", help="starta boten")
    run.add_argument("--dry-run", action="store_true", help="skriv notiser i terminalen i stället för Discord")
    sub.add_parser("games", help="lista dagens matcher")
    lineups = sub.add_parser("lineups", help="testa laguppställningar från stats.swehockey.se")
    lineups.add_argument("date", nargs="?", help="datum, t.ex. 2026-09-26 (standard: idag)")
    compare = sub.add_parser("compare", help="jämför SHL och stats.swehockey.se live")
    compare.add_argument("--rounds", type=int, default=30)
    probe = sub.add_parser("probe", help="spara rå JSON för en match")
    probe.add_argument("uuid")
    replay = sub.add_parser("replay", help="spela upp sparad matchdata")
    replay.add_argument("directory", type=Path)
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    cfg = Config()

    if args.cmd == "games":
        asyncio.run(cmd_games(cfg))
    elif args.cmd == "probe":
        asyncio.run(cmd_probe(cfg, args.uuid))
    elif args.cmd == "lineups":
        asyncio.run(cmd_lineups(cfg, args.date))
    elif args.cmd == "compare":
        asyncio.run(cmd_compare(cfg, args.rounds))
    elif args.cmd == "replay":
        cmd_replay(args.directory)
    elif args.cmd == "run" and args.dry_run:

        async def dry() -> None:
            async with SHLClient(cfg) as client, SweHockeyClient() as swe:
                await Monitor(cfg, client, ConsoleSink(), swe if cfg.swehockey else None).run_forever()

        asyncio.run(dry())
    else:
        if not cfg.discord_token or not cfg.channel_id:
            sys.exit("DISCORD_TOKEN och DISCORD_CHANNEL_ID måste vara satta (se .env.example)")
        from .bot import SHLBot

        SHLBot(cfg).run(cfg.discord_token, log_handler=None)


if __name__ == "__main__":
    main()
