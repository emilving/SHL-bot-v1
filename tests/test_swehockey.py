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
