import statistics

import pytest

from matchsim import simulate_match
from matchsim.engine import _simulate_once
from matchsim.scenarios import SCENARIOS
from matchsim.schema import PITCH_LENGTH, PITCH_WIDTH, EventType
from matchsim.stats import team_stats
from matchsim.stream import paced

EVENT_TYPES = {str(t) for t in EventType}


@pytest.fixture(scope="module")
def match():
    return simulate_match(seed=42)


def test_same_seed_is_deterministic():
    a, b = simulate_match(seed=5), simulate_match(seed=5)
    assert a.events == b.events
    assert a.metadata == b.metadata


def test_events_are_well_formed(match):
    prev_seq, prev_t = 0, -1.0
    for e in match.events:
        assert e["type"] in EVENT_TYPES
        assert e["seq"] == prev_seq + 1
        assert e["match_seconds"] >= prev_t
        assert 1 <= e["minute"] <= 90
        for k in ("x", "end_x"):
            if k in e:
                assert 0 <= e[k] <= PITCH_LENGTH
        for k in ("y", "end_y"):
            if k in e:
                assert 0 <= e[k] <= PITCH_WIDTH
        prev_seq, prev_t = e["seq"], e["match_seconds"]
    assert match.events[0]["type"] == "match_start"
    assert match.events[-1]["type"] == "match_end"


def test_score_matches_goal_events(match):
    goals = [e for e in match.events if e["type"] == "goal"]
    final = match.metadata["final_score"]
    for team_id, n in final.items():
        assert sum(g["team_id"] == team_id for g in goals) == n
    assert match.events[-1]["score"] == final


def test_player_ids_belong_to_match(match):
    ids = {p["player_id"] for side in ("home", "away") for p in match.metadata[side]["players"]}
    for e in match.events:
        for k in ("player_id", "related_player_id"):
            if k in e:
                assert e[k] in ids


def test_statistics_look_like_real_football():
    rows = []
    for seed in range(15):
        r = _simulate_once(seed, SCENARIOS["balanced"], None, None, None)
        rows.extend(team_stats(r.events, r.metadata).values())

    def mean(k):
        return statistics.mean(row.get(k) or 0 for row in rows)

    assert 0.8 <= mean("goals") <= 2.2
    assert 9 <= mean("shots") <= 17
    assert 0.08 <= mean("xg") / mean("shots") <= 0.16
    assert 76 <= mean("pass_accuracy_pct") <= 87
    assert 350 <= mean("passes") <= 700
    assert 95 <= mean("distance_km") <= 118
    assert 3 <= mean("corners") <= 9
    assert 5 <= mean("fouls") <= 15


@pytest.mark.parametrize("name", ["dominant_but_losing", "comeback", "late_drama", "red_card"])
def test_story_scenarios_are_satisfied(name):
    r = simulate_match(seed=100, scenario=name)
    assert SCENARIOS[name].accept(r.summary)


def test_paced_replay_respects_speed(match):
    slept = []
    events = list(paced(match.events[:200], speed=10, sleep=slept.append))
    assert len(events) == 200
    span = match.events[199]["match_seconds"] - match.events[0]["match_seconds"]
    assert sum(slept) == pytest.approx(span / 10, rel=1e-6)
