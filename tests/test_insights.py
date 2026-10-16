import asyncio
import re

import pytest

from insights import fallback
from insights.agents import AgentSettings
from insights.models import Explanation, Narrative, PersonaBatch, PersonaVariant, StatLine
from insights.overlays import is_relevant
from insights.pipeline import InsightsPipeline
from matchsim import simulate_match


@pytest.fixture(scope="module")
def match():
    r = simulate_match(seed=211, scenario="comeback")
    return r.metadata, r.events


def run(metadata, events, **kw):
    pipe = InsightsPipeline(metadata, settings=AgentSettings(endpoint=None, model=None), **kw)
    asyncio.run(pipe.run_events(events))
    return pipe


def test_offline_pipeline_covers_every_goal_for_every_profile(match):
    metadata, events = match
    pipe = run(metadata, events)
    goals = [e for e in events if e["type"] == "goal"]
    for profile in pipe.profiles:
        splash = [c for c in pipe.cues if c.profile_id == profile.profile_id and c.template == "goal_splash"]
        assert len(splash) == len(goals)
    assert {c.language for c in pipe.cues} == {"en", "cs", "es"}
    assert all(c.provenance["verified"] for c in pipe.cues)


def test_cues_respect_personalization(match):
    metadata, events = match
    pipe = run(metadata, events)
    profiles = {p.profile_id: p for p in pipe.profiles}
    by_profile = {pid: [c for c in pipe.cues if c.profile_id == pid] for pid in profiles}
    assert len(by_profile["analyst"]) > len(by_profile["casual"])
    focus = profiles["player_focus_es"]
    for c in by_profile["player_focus_es"]:
        # Everything else on the player cam must involve the favourite player (as actor or receiver).
        assert c.moment_kind in ("goal", "red_card", "half_time", "full_time", "added_time", "pass_quality",
                                 "substitution") or c.player_id == focus.favorite_player_id
    assert any(c.moment_kind == "player_focus" for c in by_profile["player_focus_es"])
    assert all(len(c.stats) <= 1 for c in by_profile["casual"])


def test_cues_are_timed_after_their_trigger(match):
    metadata, events = match
    pipe = run(metadata, events)
    for c in pipe.cues:
        assert c.display_at_match_seconds >= c.trigger_match_seconds
        assert c.duration_s > 0


def test_fact_guard_flags_invented_numbers():
    allowed = fallback.allowed_numbers({"xg": 0.34, "shot_speed_kmh": 101.3, "distance_m": 10450})
    assert fallback.unverified_numbers("A 0.34 xG strike at 101 km/h, 34% chance", allowed) == []
    assert fallback.unverified_numbers("He covered 10.45 km", allowed) == []
    assert fallback.unverified_numbers("His 27th goal of the season at 120 km/h", allowed) == ["27", "120"]


class FakeTeam:
    """Stands in for Foundry: one agent times out, the narrator hallucinates once, persona hallucinates once."""

    enabled = True

    def __init__(self):
        self.calls = []
        self.narrator_calls = 0

    async def run(self, role, prompt, output, tools=None):
        self.calls.append(role)
        if role == "analyst":
            raise asyncio.TimeoutError("model too slow")
        if role == "tactician":
            return Explanation(why_it_matters="The press forced the error.", driver="pressing", evidence=[])
        if role == "narrator":
            self.narrator_calls += 1
            if "fact_check_feedback" not in prompt:
                return Narrative(headline="Record 99th goal!", caption="Fastest ever at 140 km/h", commentary="x")
            return Narrative(headline="What a goal", caption="Clinical finish.", commentary="Clinical finish.")
        if role == "persona":
            ids = re.findall(r'"profile_id": "([a-z_]+)"', prompt)
            return PersonaBatch(variants=[PersonaVariant(profile_id=pid, language="en", headline="Wow",
                                                         caption="777 passes" if i == 0 else "Nice",
                                                         stats=[StatLine(label="xG", value="0.5")][:0])
                                          for i, pid in enumerate(ids)])
        if role == "recap":
            return fallback.recap  # wrong type on purpose -> must fall back
        raise AssertionError(role)


def test_workflow_recovers_from_failures_and_hallucinations(match):
    metadata, events = match
    team = FakeTeam()
    pipe = InsightsPipeline(metadata, team=team)
    asyncio.run(pipe.run_events(events))
    goal_cues = [c for c in pipe.cues if c.moment_kind == "goal" and c.profile_id == "analyst"]
    assert goal_cues
    for c in goal_cues:
        trace = c.provenance["trace"]
        modes = {(t["agent"], t["mode"]) for t in trace}
        assert ("analyst", "fallback") in modes            # timeout -> deterministic twin
        assert ("tactician", "llm") in modes
        assert sum(t["agent"] == "narrator" for t in trace) == 2   # rewrite loop after fact check
        assert any(t["agent"] == "fact_guard" and "unverified" in t["note"] for t in trace)
        assert "99" not in c.headline and "140" not in c.caption
    assert not any("777" in c.caption for c in pipe.cues)


def test_every_agent_lane_moment_reaches_the_analyst(match):
    # Agent-lane cues finish out of order; scheduling must not drop them as stale.
    metadata, events = match
    pipe = run(metadata, events)
    analyst_moments = {c.moment_id for c in pipe.cues if c.profile_id == "analyst"}
    assert len(analyst_moments) == pipe.stats["moments"]


def test_slots_never_overlap(match):
    metadata, events = match
    pipe = run(metadata, events)
    by_slot = {}
    for c in pipe.cues:
        by_slot.setdefault((c.profile_id, c.slot), []).append((c.display_at_match_seconds, c.duration_s))
    for intervals in by_slot.values():
        intervals.sort()
        for (a, da), (b, _) in zip(intervals, intervals[1:]):
            assert b >= a + da - 0.11
