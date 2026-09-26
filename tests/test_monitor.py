import asyncio
import json
from datetime import datetime, timedelta
from pathlib import Path

from shlbot.api import NotAvailable
from shlbot.config import Config
from shlbot.models import STOCKHOLM, GameInfo
from shlbot.monitor import Monitor

FIXTURE = Path(__file__).parent / "fixtures" / "sample_game"


class FakeClient:
    def __init__(self):
        raw = json.loads((FIXTURE / "game.json").read_text("utf-8"))
        raw["startDateTime"] = (datetime.now(STOCKHOLM) - timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M:%S")
        raw["state"] = "ongoing"
        self.game = GameInfo.parse(raw)
        self.events = json.loads((FIXTURE / "play_by_play.json").read_text("utf-8"))
        self.visible = 0
        self.state = "ongoing"

    async def schedule(self):
        return [self.game]

    async def play_by_play(self, uuid):
        return self.events[: self.visible]

    async def overview(self, uuid):
        return {"state": self.state}

    async def team_stats(self, uuid):
        raise NotAvailable(uuid)


class Collect:
    def __init__(self):
        self.titles = []

    async def send(self, game, embeds):
        self.titles += [e.title for e in embeds]


def test_monitor_end_to_end(tmp_path):
    cfg = Config()
    cfg.state_file = tmp_path / "state.json"
    cfg.teams = set()
    client, sink = FakeClient(), Collect()

    async def scenario():
        mon = Monitor(cfg, client, sink)
        await mon.tick()  # inga händelser än
        for n in range(1, len(client.events) + 1):
            client.visible = n
            await mon.tick()
        client.state = "post-game"
        await mon.tick()
        await mon.tick()  # matchen är klar och ska inte pollas mer
        return mon

    mon = asyncio.run(scenario())
    assert sink.titles[0].startswith("🏒 Nedsläpp")
    assert sum(t.startswith("🚨 MÅL") for t in sink.titles) == 4
    assert sum(t.startswith("⏸️ Efter") for t in sink.titles) == 3
    assert sink.titles[-1].startswith("🏁 Slutresultat: FBK 3–1 LHF")
    assert mon.active_games() == []
    state = json.loads(cfg.state_file.read_text("utf-8"))
    assert state[client.game.uuid]["final_posted"] is True

    # Omstart: inget postas igen
    sink2 = Collect()
    mon2 = Monitor(cfg, client, sink2)
    asyncio.run(mon2.tick())
    assert sink2.titles == []
