"""Match scenarios: tactical tendencies plus an acceptance test.

A scenario biases how teams play (pressing, directness, finishing, attacking
intent) as a function of minute and score. Because football is random, the
generator then rejection-samples seeds until the finished match actually tells
the intended story (e.g. a real comeback), keeping every event plausible.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

# Modifier keys understood by the engine (all default to 1.0 / False).
DEFAULT_MODS = {"press": 1.0, "directness": 1.0, "finishing": 1.0, "attack": 1.0, "control": 1.0,
                "force_red": False}


@dataclass(frozen=True)
class GameView:
    """What a scenario may look at when deciding modifiers."""

    minute: int
    is_home: bool
    goal_diff: int  # from this team's point of view
    players_on_pitch: int


ModFn = Callable[[GameView], dict]
AcceptFn = Callable[[dict], bool]


def _game_state_mods(v: GameView) -> dict:
    """Generic, scenario-independent behaviour: chase when behind, manage when ahead."""
    m: dict = {}
    if v.minute >= 60 and v.goal_diff < 0:
        m |= {"attack": 1.25, "directness": 1.15, "press": 1.2}
    elif v.minute >= 70 and v.goal_diff > 0:
        m |= {"attack": 0.8, "control": 1.1, "press": 0.85}
    return m


@dataclass(frozen=True)
class Scenario:
    name: str
    description: str
    home_strength: float = 1.0
    away_strength: float = 1.0
    mods: ModFn = field(default=lambda v: {})
    accept: AcceptFn = field(default=lambda s: True)

    def modifiers(self, view: GameView) -> dict:
        return DEFAULT_MODS | _game_state_mods(view) | self.mods(view)


def _goal_minutes(summary: dict, team_key: str) -> list[int]:
    return [g["minute"] for g in summary["goals"] if g["side"] == team_key]


def _comeback_accept(s: dict) -> bool:
    # Home trailed by 2+ at some point after the 50th minute and did not lose.
    return s["max_deficit_after_50"]["home"] >= 2 and s["score"]["home"] >= s["score"]["away"]


def _dominant_accept(s: dict) -> bool:
    return (s["possession"]["home"] >= 58 and s["shots"]["home"] >= 1.5 * max(1, s["shots"]["away"])
            and s["score"]["away"] > s["score"]["home"])


def _late_drama_accept(s: dict) -> bool:
    late = [g for g in s["goals"] if g["minute"] >= 88]
    return any(g["changed_result"] for g in late)


def _red_card_accept(s: dict) -> bool:
    return any(c["side"] == "away" and c["minute"] <= 45 for c in s["red_cards"])


SCENARIOS: dict[str, Scenario] = {
    "balanced": Scenario(
        name="balanced",
        description="Two evenly matched sides; whatever happens, happens.",
    ),
    "dominant_but_losing": Scenario(
        name="dominant_but_losing",
        description="Home side controls the ball and creates more, but a direct away side wins on the counter.",
        home_strength=1.06,
        away_strength=0.98,
        mods=lambda v: ({"control": 1.2, "press": 1.25, "directness": 0.8} if v.is_home
                        else {"directness": 1.35, "finishing": 1.35, "press": 0.8}),
        accept=_dominant_accept,
    ),
    "comeback": Scenario(
        name="comeback",
        description="Away side races into a two-goal lead; home side storms back after the hour.",
        mods=lambda v: (
            {"finishing": 0.8} if v.is_home and v.minute < 55 else
            {"finishing": 1.35, "attack": 1.25, "press": 1.3} if v.is_home else
            {"finishing": 1.3} if v.minute < 55 else {"press": 0.8, "attack": 0.8}
        ),
        accept=_comeback_accept,
    ),
    "late_drama": Scenario(
        name="late_drama",
        description="A tight game decided (or rescued) in the 88th minute or later.",
        mods=lambda v: {"finishing": 1.4, "attack": 1.3} if v.minute >= 85 else {"finishing": 0.85},
        accept=_late_drama_accept,
    ),
    "red_card": Scenario(
        name="red_card",
        description="Away side goes down to ten men in the first half; watch control shift.",
        mods=lambda v: {"force_red": True} if (not v.is_home and 20 <= v.minute <= 40) else {},
        accept=_red_card_accept,
    ),
}
