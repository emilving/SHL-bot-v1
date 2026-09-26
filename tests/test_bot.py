import json
from pathlib import Path

import pytest

from shlbot.bot import split_large
from shlbot.formatting import EmbedSpec, render
from shlbot.models import (
    GOAL,
    GOALIE_IN,
    GOALIE_OUT,
    INJURY,
    PENALTY,
    PERIOD_END,
    SHOT,
    GameInfo,
    GameStatus,
    parse_event,
    parse_events,
    parse_status,
)
from shlbot.stats import compute, parse_team_stats
from shlbot.tracker import MISSING_GOAL_THRESHOLD, GameTracker

FIXTURE = Path(__file__).parent / "fixtures" / "sample_game"


@pytest.fixture
def game() -> GameInfo:
    return GameInfo.parse(json.loads((FIXTURE / "game.json").read_text("utf-8")))


@pytest.fixture
def events(game):
    return parse_events(json.loads((FIXTURE / "play_by_play.json").read_text("utf-8")), game)


def live(events):
    return GameStatus("live", period=events[-1].period if events else 1)


# -- parsning ----------------------------------------------------------------


def test_game_info(game):
    assert game.home.code == "FBK" and game.away.name == "Luleå Hockey"
    assert game.is_finished
    assert game.start.hour == 19 and game.start.tzinfo is not None


def test_parse_goal(game):
    e = parse_event(
        {
            "type": "goal",
            "period": 2,
            "time": "5:03",
            "eventTeam": {"place": "away", "teamCode": "LHF"},
            "player": {"firstName": "A", "familyName": "B", "jerseyToday": 9},
            "assists": {"first": {"firstName": "C", "familyName": "D"}},
            "homeGoals": 0,
            "awayGoals": 1,
            "goalStatus": "PP1",
        },
        game,
    )
    assert e.kind == GOAL and e.side == "away" and e.time == "05:03"
    assert e.player == "#9 A B" and e.assists == ["C D"]
    assert e.is_pp and not e.is_empty_net


def test_side_from_team_code(game):
    e = parse_event({"type": "penalty", "team": {"code": "FBK"}, "penaltyMinutes": "5 min"}, game)
    assert e.kind == PENALTY and e.side == "home" and e.penalty_minutes == 5


@pytest.mark.parametrize(
    "raw,kind",
    [
        ({"type": "goalkeeper", "isEntering": True}, GOALIE_IN),
        ({"type": "goalkeeper", "isEntering": False}, GOALIE_OUT),
        ({"type": "period", "finished": True}, PERIOD_END),
        ({"type": "shot"}, SHOT),
        ({"type": "Injury"}, INJURY),
    ],
)
def test_event_kinds(raw, kind):
    assert parse_event(raw).kind == kind


def test_events_wrapped_in_object(game):
    data = {"events": [{"type": "goal", "id": 1, "period": 1, "time": "01:00"}]}
    assert [e.kind for e in parse_events(data, game)] == [GOAL]


def test_status_from_overview(game):
    st = parse_status({"state": "Intermission", "gameTime": {"period": 2}, "homeTeam": {"score": 2}}, game)
    assert st.phase == "intermission" and st.period == 2 and st.home_score == 2


# -- statistik ---------------------------------------------------------------


def test_stats(events):
    s = compute(events, game_over=True)
    tot = s.total()
    assert (tot.home.goals, tot.away.goals) == (3, 1)
    assert (tot.home.shots, tot.away.shots) == (7, 4)
    assert (tot.home.pim, tot.away.pim) == (2, 4)
    # Kvittade utvisningar i P2 ger inget powerplay
    assert (tot.home.pp_goals, tot.home.pp_opps) == (1, 1)
    assert tot.away.pp_opps == 0
    assert tot.home.en_goals == 1


def test_goalie_stats(events):
    goalies = {g.name: g for g in compute(events, game_over=True).goalies}
    assert goalies["#30 Emil Larsson"].shots_against == 4
    assert goalies["#30 Emil Larsson"].goals_against == 1
    lass = goalies["#35 Joel Lassinantti"]
    assert (lass.shots_against, lass.goals_against) == (5, 2)
    # Målet i tom kasse belastar ingen målvakt
    assert goalies["#1 Filip Gunnarsson"].goals_against == 0


def test_team_stats_override(events):
    extra = parse_team_stats(
        {"periods": [{"period": 1, "home": {"faceoffsWon": 8, "shotsOnGoal": 10}, "away": {"faceoffsWon": 5}}]}
    )
    s = compute(events, extra)
    assert s.has_faceoffs
    assert s.periods[1].home.faceoffs_won == 8
    assert s.periods[1].home.shots == 10
    assert s.periods[1].away.saves == 9  # 10 skott - 1 mål
    assert s.periods[2].home.faceoffs_won is None


# -- tracker -----------------------------------------------------------------


def run_live(tracker, events):
    notes = tracker.update([], GameStatus("pre"))
    for i in range(1, len(events) + 1):
        notes += tracker.update(events[:i], live(events[:i]))
    notes += tracker.update(events, GameStatus("final", period=3))
    return notes


def test_full_game(game, events):
    notes = run_live(GameTracker(game=game), events)
    kinds = [n.kind for n in notes]
    assert kinds.count("start") == 1
    assert [n.period for n in notes if n.kind == "period"] == [1, 2, 3]
    assert kinds[-1] == "final"
    posted = [n.event.kind for n in notes if n.kind == "event"]
    assert posted.count(GOAL) == 4
    assert posted.count(PENALTY) == 3
    assert posted.count(INJURY) == 1
    # Byte = en notis, tom kasse sent i P3 = en notis
    assert posted.count(GOALIE_IN) == 1 and posted.count(GOALIE_OUT) == 1
    assert [n.score for n in notes if n.kind == "event" and n.event.kind == GOAL] == [(1, 0), (1, 1), (2, 1), (3, 1)]
    for n in notes:
        render(n)  # ska inte krascha


def test_no_duplicates_on_repeat(game, events):
    t = GameTracker(game=game)
    run_live(t, events[:10])
    assert t.update(events[:10], live(events[:10])) == []


def test_state_roundtrip(game, events):
    t = GameTracker(game=game)
    t.update([], GameStatus("pre"))
    t.update(events[:12], live(events[:12]))
    restored = GameTracker.from_dict(game, json.loads(json.dumps(t.to_dict())))
    assert restored.update(events[:12], live(events[:12])) == []


def test_join_mid_game(game, events):
    t = GameTracker(game=game)
    notes = t.update(events[:15], live(events[:15]))
    assert [n.kind for n in notes] == ["tracking"]
    later = t.update(events[:17], live(events[:17]))
    assert all(n.kind != "period" or n.period >= 2 for n in later)


def test_finished_game_on_startup_is_silent(game, events):
    t = GameTracker(game=game)
    assert t.update(events, GameStatus("final", period=3)) == []
    assert t.final_posted


def test_goal_correction_and_disallowed(game, events):
    t = GameTracker(game=game)
    upto = [e for e in events if e.period == 1]
    t.update([], GameStatus("pre"))
    t.update(upto, live(upto))
    goal = next(e for e in upto if e.kind == GOAL)

    goal.player = "#10 Någon Annan"
    notes = t.update(upto, live(upto))
    assert [n.kind for n in notes] == ["correction"]

    without = [e for e in upto if e.kind != GOAL]
    notes = []
    for _ in range(MISSING_GOAL_THRESHOLD):
        notes += t.update(without, live(without))
    assert [n.kind for n in notes] == ["disallowed"]
    assert notes[0].score == (0, 0)


def test_intermission_from_overview(game, events):
    t = GameTracker(game=game)
    p1 = [e for e in events if e.period == 1 and e.kind != PERIOD_END]
    t.update([], GameStatus("pre"))
    t.update(p1, live(p1))
    notes = t.update(p1, GameStatus("intermission", period=1))
    assert [n.kind for n in notes] == ["period"]


def test_split_large_embed():
    spec = EmbedSpec("t", fields=[(f"f{i}", "x" * 1000, False) for i in range(10)], footer="ft")
    parts = split_large(spec)
    assert len(parts) == 2
    assert sum(len(p.fields) for p in parts) == 10
    assert parts[-1].footer == "ft"
