import time
from pathlib import Path

from shlbot.config import Config
from shlbot.models import GameInfo, GameStatus
from shlbot.monitor import SWE_PAUSE_DELAY, Monitor
from shlbot.swehockey import find_game, parse_game_page, parse_games_by_date, same_team

FIX = Path(__file__).parent / "fixtures" / "swehockey"


def test_same_team():
    assert same_team("Djurgårdens IF", "Djurgården Hockey")
    assert same_team("Växjö Lakers", "Växjö Lakers HC")
    assert same_team("HV71", "HV 71")
    assert same_team("Malmö Redhawks", "IF Malmö Redhawks")
    assert not same_team("Frölunda HC", "Färjestad BK")


def test_games_by_date_prefers_shl():
    rows = parse_games_by_date((FIX / "games_by_date.html").read_text("utf-8"))
    assert (222, "Växjö Lakers HC", "HV 71", True) in rows
    assert find_game(rows, "Växjö Lakers", "HV 71") == 222
    assert find_game(rows, "Djurgårdens IF", "Timrå IK") == 333
    assert find_game(rows, "Timrå IK", "Djurgårdens IF") is None  # fel hemmalag
    assert find_game(rows, "Frölunda HC", "Växjö Lakers") is None  # bara J20-matchen finns


def test_game_page():
    g = parse_game_page((FIX / "game_events.html").read_text("utf-8"))
    assert (g.home_score, g.away_score) == (3, 1)
    assert g.period == 1 and g.intermission and not g.final
    assert [(x.home, x.away) for x in g.goals] == [(1, 0), (1, 1), (2, 1), (3, 1)]
    last = g.goal_with_score(3, 1)
    assert last.scorer == "#25 Lucas Elvenes"
    assert last.assists == ["#40 Dennis Rasmussen", "#7 Erik Andersson"]
    assert last.strength == "PP1" and last.time == "18:40"
    assert g.penalties == 1


def test_live_clock_from_note():
    html = (
        '<table><tr><td class="tdInfoArea"><div>0 - 0</div><div>2nd period</div>'
        "<div>Powerplay (5 on 4) for HV71 (03:12)</div></td></tr></table>"
    )
    g = parse_game_page(html)
    assert g.period == 2 and g.clock == "03:12" and not g.intermission


def _monitor():
    cfg = Config()
    return Monitor(cfg, client=None, sink=None)


def test_merge_status_score_and_delayed_pause():
    mon = _monitor()
    game = GameInfo.parse({"uuid": "x", "homeTeamInfo": {"code": "VLH"}, "awayTeamInfo": {"code": "HV71"}})
    swe = parse_game_page((FIX / "game_events.html").read_text("utf-8"))
    shl = GameStatus("live", period=1, clock="11:11", home_score=2, away_score=1)

    st = mon.merge_status(game, shl, swe)
    assert (st.home_score, st.away_score) == (3, 1)
    assert st.phase == "live"  # pausen väntar på att SHL:s händelser hinner ikapp

    key = (game.uuid, "pause1")
    mon.swe_since[key] = time.monotonic() - SWE_PAUSE_DELAY - 1
    assert mon.merge_status(game, shl, swe).phase == "intermission"
    assert mon.merge_status(game, shl, None) is shl


def test_goalkeeper_summary_and_official_stats(game=None):
    from shlbot.stats import compute
    from shlbot.tracker import GameTracker

    g = parse_game_page((FIX / "game_events.html").read_text("utf-8"))
    assert [(x.team, x.name, x.saves, x.shots) for x in g.goalies] == [
        ("VÄX", "#70 Adam Åhman", 8, 9),
        ("HV71", "#80 Herman Liv", 7, 10),
    ]
    info = GameInfo.parse(
        {
            "uuid": "x",
            "homeTeamInfo": {"code": "VLH", "names": {"long": "Växjö Lakers"}},
            "awayTeamInfo": {"code": "HV71", "names": {"long": "HV71"}},
        }
    )
    mon = _monitor()
    lines = mon.official_goalies(info, GameTracker(game=info), g)
    assert [(x.side, x.saves, x.save_pct) for x in lines] == [("home", 8, 8 / 9), ("away", 7, 0.7)]

    stats = compute([], official_goalies=lines)
    tot = stats.total()
    assert (tot.home.shots, tot.away.shots) == (10, 9)
    assert (tot.home.saves, tot.away.saves) == (8, 7)


def test_parse_lineup():
    from shlbot.swehockey import parse_lineup

    lu = parse_lineup((FIX / "lineup_prev.html").read_text("utf-8"), ["Färjestad BK", "FBK"], ["Luleå Hockey", "LHF"])
    assert lu["home"]["linus johansson"] == "#21 Linus Johansson"
    assert len(lu["home"]) == 16 and "anton lindholm" in lu["away"]
    assert "dan domare" not in lu["away"]  # domare räknas inte som spelare


def test_lineup_changes_posted(tmp_path):
    import asyncio
    from datetime import datetime, timedelta

    from shlbot.formatting import render
    from shlbot.models import STOCKHOLM
    from shlbot.swehockey import parse_lineup

    now = datetime.now(STOCKHOLM)

    def game(uuid, home, hname, away, aname, start):
        return GameInfo.parse(
            {
                "uuid": uuid,
                "startDateTime": start.strftime("%Y-%m-%d %H:%M:%S"),
                "homeTeamInfo": {"code": home, "names": {"long": hname}},
                "awayTeamInfo": {"code": away, "names": {"long": aname}},
            }
        )

    prev = game("prev", "FBK", "Färjestad BK", "LHF", "Luleå Hockey", now - timedelta(days=3))
    cur = game("cur", "FBK", "Färjestad BK", "RBK", "Rögle BK", now + timedelta(minutes=30))
    pages = {1: "lineup_prev.html", 2: "lineup_current.html"}

    class FakeSwe:
        async def find_game_id(self, day, home, away):
            return 1 if "Luleå" in away else 2

        async def lineup(self, game_id, home_names, away_names):
            return parse_lineup((FIX / pages[game_id]).read_text("utf-8"), home_names, away_names)

    class Sink:
        def __init__(self):
            self.specs = []

        async def send(self, game, embeds):
            self.specs += embeds
            return [1]

        async def edit(self, *a):
            return True

    cfg = Config()
    cfg.state_file = tmp_path / "s.json"
    cfg.teams = {"FBK"}
    sink = Sink()
    mon = Monitor(cfg, client=None, sink=sink, swe=FakeSwe())
    mon.games = {g.uuid: g for g in (prev, cur)}
    asyncio.run(mon.check_lineups(cur))
    assert len(sink.specs) == 1  # bara FBK följs
    spec = sink.specs[0]
    assert spec.title == "📋 Förändringar i FBK:s uppställning"
    fields = dict((f[0], f[1]) for f in spec.fields)
    assert fields["Saknas"] == "#91 Marcus Sörensen\n#22 Anders Andersson"
    assert fields["Nya i laguppställningen"] == "#29 Namn Nytt"
    # Postas bara en gång
    mon.lineup_at.clear()
    asyncio.run(mon.check_lineups(cur))
    assert len(sink.specs) == 1
