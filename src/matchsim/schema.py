"""Event schema shared by the generator, the streamer and downstream agents.

Coordinates are absolute pitch metres: x in [0, 105] (left to right as seen on
the broadcast camera), y in [0, 68] (top to bottom). Which goal a team attacks
in each period is given in the match metadata (`attacking_direction`).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any

SCHEMA_VERSION = "1.0"

PITCH_LENGTH = 105.0
PITCH_WIDTH = 68.0


class EventType(StrEnum):
    MATCH_START = "match_start"
    PERIOD_START = "period_start"
    KICKOFF = "kickoff"
    PASS = "pass"
    CARRY = "carry"
    PRESSURE = "pressure"
    TACKLE = "tackle"
    INTERCEPTION = "interception"
    BALL_RECOVERY = "ball_recovery"
    POSSESSION_CHANGE = "possession_change"
    SHOT = "shot"
    GOAL = "goal"
    SAVE = "save"
    FOUL = "foul"
    CARD = "card"
    FREE_KICK = "free_kick"
    CORNER = "corner"
    THROW_IN = "throw_in"
    GOAL_KICK = "goal_kick"
    SUBSTITUTION = "substitution"
    PHYSICAL_UPDATE = "physical_update"
    PLAYER_ALERT = "player_alert"
    ADDED_TIME = "added_time"
    PERIOD_END = "period_end"
    MATCH_END = "match_end"


@dataclass(slots=True)
class Event:
    """One match event. `metrics` holds type-specific numbers (see README)."""

    event_id: str
    match_id: str
    seq: int
    period: int
    match_seconds: float  # seconds since kickoff of period 1, stoppage time included
    clock: str  # broadcast clock, e.g. "45+02:13"
    minute: int  # football minute as shown in commentary (1-based, 46 starts period 2)
    type: EventType
    team_id: str | None = None
    player_id: str | None = None
    x: float | None = None
    y: float | None = None
    end_x: float | None = None
    end_y: float | None = None
    outcome: str | None = None
    related_player_id: str | None = None
    under_pressure: bool = False
    possession_id: int | None = None
    score: dict[str, int] = field(default_factory=dict)
    metrics: dict[str, Any] = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["type"] = str(self.type)
        # Drop empty optionals so the stream stays compact.
        return {k: v for k, v in d.items() if v is not None and v != [] and v != {}}
