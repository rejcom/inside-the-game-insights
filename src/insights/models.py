"""Typed contracts between agents, the workflow and the overlay renderer."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, Field

from .moments import Moment

# --------------------------------------------------------------------------- agent outputs (structured)


class StatLine(BaseModel):
    label: str = Field(description="Short on-screen label, e.g. 'xG' or 'Top speed'")
    value: str = Field(description="Display value copied from tool output or facts, e.g. '0.34' or '33.8 km/h'")


class Interpretation(BaseModel):
    """Analyst agent: which numbers matter for this moment."""
    key_stats: list[StatLine] = Field(description="2-4 most relevant stats for this moment", max_length=4)
    pattern: str = Field(description="One sentence on the statistical pattern behind the moment")


Driver = Literal["control_vs_chaos", "pressing", "game_rhythm", "individual_quality", "set_piece",
                 "fatigue", "numerical_advantage", "finishing", "goalkeeping"]


class Explanation(BaseModel):
    """Tactician agent: why the moment matters."""
    why_it_matters: str = Field(description="1-2 sentences explaining why this moment matters, not what happened")
    driver: Driver = Field(description="Main tactical driver")
    evidence: list[str] = Field(description="1-3 short metric-backed evidence points", max_length=3)


class Narrative(BaseModel):
    """Narrator agent: broadcast-ready text in English."""
    headline: str = Field(description="On-screen headline, max 60 characters")
    caption: str = Field(description="Lower-third caption, max 160 characters")
    commentary: str = Field(description="One spoken commentary line, max 280 characters")


class PersonaVariant(BaseModel):
    profile_id: str
    language: str = Field(description="BCP-47 language code, e.g. 'en', 'es', 'cs'")
    headline: str = Field(description="Max 60 characters, in the profile's language")
    caption: str = Field(description="Max 160 characters, in the profile's language")
    stats: list[StatLine] = Field(default_factory=list, max_length=4,
                                  description="Stats for this viewer; fewer and simpler for casual fans")


class PersonaBatch(BaseModel):
    variants: list[PersonaVariant]


class Recap(BaseModel):
    title: str = Field(description="Max 70 characters")
    summary: str = Field(description="3-5 sentence recap explaining how the match was decided")
    key_moments: list[str] = Field(description="3-5 bullet lines with minute marks", max_length=5)
    player_of_the_match: str


# --------------------------------------------------------------------------- viewer profiles

Mode = Literal["analyst", "casual", "player_focus", "club_fan"]


class ViewerProfile(BaseModel):
    profile_id: str
    label: str
    mode: Mode
    language: str = "en"
    favorite_team_id: str | None = None
    favorite_player_id: str | None = None
    focus_metric: str | None = None


# --------------------------------------------------------------------------- overlays

Template = Literal["goal_splash", "lower_third", "stat_card", "player_tag", "momentum_bar", "recap_card", "alert_chip"]


class OverlayCue(BaseModel):
    """A timed, machine-readable graphic for a renderer to draw over the live feed."""
    cue_id: str
    match_id: str
    moment_id: str
    moment_kind: str
    profile_id: str
    language: str
    template: Template
    slot: Literal["center", "lower", "corner", "fullscreen"]
    priority: float
    trigger_match_seconds: float
    display_at_match_seconds: float
    duration_s: float
    clock: str
    team_id: str | None = None
    player_id: str | None = None
    headline: str
    caption: str = ""
    stats: list[StatLine] = Field(default_factory=list)
    why_it_matters: str | None = None
    data: dict[str, Any] = Field(default_factory=dict, description="Template-specific numbers, e.g. momentum value")
    provenance: dict[str, Any] = Field(default_factory=dict)


# --------------------------------------------------------------------------- workflow messages


@dataclass
class TraceStep:
    agent: str
    mode: Literal["llm", "fallback", "deterministic"]
    latency_ms: int
    note: str = ""


@dataclass
class MomentJob:
    """Shared state for one moment travelling through the workflow (blackboard pattern)."""
    moment: Moment
    profiles: list[ViewerProfile]
    snapshot: dict[str, Any]
    evidence: list[Any] = field(default_factory=list)  # tool outputs: numbers here are allowed on screen
    interpretation: Interpretation | None = None
    explanation: Explanation | None = None
    narrative: Narrative | None = None
    narrative_attempts: int = 0
    verification_issues: list[str] = field(default_factory=list)
    needs_rewrite: bool = False
    variants: list[PersonaVariant] = field(default_factory=list)
    recap: Recap | None = None
    trace: list[TraceStep] = field(default_factory=list)


@dataclass
class Contribution:
    """Output of a parallel specialist, gathered by the fan-in composer."""
    role: Literal["analyst", "tactician"]
    job: MomentJob
