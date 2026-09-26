"""Kommandorad:

    python -m shlbot run            Starta Discord-boten
    python -m shlbot run --dry-run  Kör bevakningen men skriv notiser i terminalen
    python -m shlbot games          Lista dagens matcher (med matchens uuid)
    python -m shlbot probe UUID     Spara rå JSON för en match i probe/UUID/
    python -m shlbot replay DIR     Spela upp sparad data och visa alla notiser
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from collections import Counter
from pathlib import Path

from .api import NotAvailable, SHLClient, is_playoff
from .config import Config
from .formatting import render
from .models import GameInfo, GameStatus, extract_event_list, parse_events
from .monitor import ConsoleSink, Monitor
from .stats import parse_team_stats
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
    print(f"Data sparad i {out}/ – kör 'python -m shlbot replay {out}' för att se notiserna.")


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
    elif args.cmd == "replay":
        cmd_replay(args.directory)
    elif args.cmd == "run" and args.dry_run:

        async def dry() -> None:
            async with SHLClient(cfg) as client:
                await Monitor(cfg, client, ConsoleSink()).run_forever()

        asyncio.run(dry())
    else:
        if not cfg.discord_token or not cfg.channel_id:
            sys.exit("DISCORD_TOKEN och DISCORD_CHANNEL_ID måste vara satta (se .env.example)")
        from .bot import SHLBot

        SHLBot(cfg).run(cfg.discord_token, log_handler=None)


if __name__ == "__main__":
    main()
