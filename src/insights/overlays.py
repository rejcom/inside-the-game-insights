"""Producer: turns approved, personalized copy into timed overlay cues.

The producer is the broadcast director. It decides per viewer profile whether a
moment is relevant, which graphic template and screen slot to use, when to show
it (respecting slot collisions and staleness), and attaches provenance so every
cue can be audited (which agents ran, which fell back, whether facts verified).
"""

from __future__ import annotations

from typing import Any

from . import fallback
from .models import MomentJob, OverlayCue, PersonaVariant, ViewerProfile
from .moments import Moment
from .state import MatchState

# kind -> (template, slot, duration_s, max_delay_s)
LAYOUT: dict[str, tuple[str, str, float, float]] = {
    "goal": ("goal_splash", "center", 8, 60),
    "red_card": ("lower_third", "lower", 7, 30),
    "yellow_card": ("alert_chip", "corner", 4, 15),
    "big_chance": ("stat_card", "corner", 7, 25),
    "key_save": ("stat_card", "corner", 7, 25),
    "pass_quality": ("stat_card", "corner", 8, 25),
    "player_alert": ("player_tag", "lower", 5, 12),
    "substitution": ("lower_third", "lower", 6, 40),
    "momentum_swing": ("momentum_bar", "corner", 10, 45),
    "pressing_wave": ("momentum_bar", "corner", 10, 45),
    "control_shift": ("momentum_bar", "corner", 10, 45),
    "added_time": ("alert_chip", "corner", 4, 20),
    "player_focus": ("stat_card", "corner", 8, 30),
    "half_time": ("recap_card", "fullscreen", 15, 300),
    "full_time": ("recap_card", "fullscreen", 20, 600),
}

CASUAL_KINDS = {"goal", "red_card", "big_chance", "key_save", "momentum_swing", "half_time", "full_time",
                "added_time", "player_alert"}
ALWAYS = {"goal", "red_card", "half_time", "full_time"}


def default_profiles(state: MatchState) -> list[ViewerProfile]:
    """Four demo viewers watching the same match."""
    home, away = state.home_id, state.away_id
    attackers = [p for p in state.metadata["away"]["players"] if p.get("starter") and p["role"] in ("ST", "AM", "LW", "RW")]
    best = max(attackers, key=lambda p: p["attributes"]["finishing"] + p["attributes"]["pace"], default=None)
    away_star = state.players.get(best["player_id"]) if best else None
    return [
        ViewerProfile(profile_id="analyst", label="Studio analyst", mode="analyst", language="en"),
        ViewerProfile(profile_id="casual", label="Casual fan", mode="casual", language="en"),
        ViewerProfile(profile_id="club_fan_cs", label=f"{state.team_name(home)} fan (Czech)", mode="club_fan",
                      language="cs", favorite_team_id=home),
        ViewerProfile(profile_id="player_focus_es", label=f"Player cam: {away_star.name if away_star else ''} (Spanish)",
                      mode="player_focus", language="es", favorite_team_id=away,
                      favorite_player_id=away_star.player_id if away_star else None, focus_metric="top_speed_kmh"),
    ]


def is_relevant(m: Moment, p: ViewerProfile) -> bool:
    if m.kind in ALWAYS:
        return True
    involved = {m.player_id, m.related_player_id} - {None}
    if p.mode == "analyst":
        return True
    if p.mode == "casual":
        return m.kind in CASUAL_KINDS and not (m.kind == "player_alert" and m.facts.get("alert") != "top_speed")
    if p.mode == "club_fan":
        if m.kind in ("big_chance", "key_save", "momentum_swing", "pressing_wave", "added_time"):
            return True
        return m.team_id == p.favorite_team_id
    if p.mode == "player_focus":
        return p.favorite_player_id in involved or m.kind == "added_time"
    return False


class Producer:
    def __init__(self, state: MatchState, profiles: list[ViewerProfile]):
        self.state = state
        self.profiles = profiles
        # Booked (start, end) intervals per (profile, slot). Agent-lane cues can finish out of order,
        # so scheduling looks for the earliest free gap rather than "after the last booking".
        self._bookings: dict[tuple[str, str], list[tuple[float, float]]] = {}
        self._seq = 0

    def produce(self, job: MomentJob, latency_match_s: float = 1.5) -> list[OverlayCue]:
        m = job.moment
        template, slot, duration, max_delay = LAYOUT.get(m.kind, ("lower_third", "lower", 6, 20))
        variants = {v.profile_id: v for v in job.variants}
        trace = [{"agent": t.agent, "mode": t.mode, "latency_ms": t.latency_ms, "note": t.note} for t in job.trace]
        fallback_used = any(t.mode == "fallback" for t in job.trace)
        cues: list[OverlayCue] = []
        for p in self.profiles:
            if not is_relevant(m, p):
                continue
            v: PersonaVariant = variants.get(p.profile_id) or fallback.personalize(
                m, p, job.interpretation, job.explanation, job.narrative)
            key = (p.profile_id, slot)
            display_at = self._earliest_gap(key, m.match_seconds + latency_match_s, duration)
            if display_at - m.match_seconds > max_delay and m.importance < 0.8:
                continue  # stale: the moment has passed for this viewer
            self._bookings.setdefault(key, []).append((display_at, display_at + duration))
            self._seq += 1
            cues.append(OverlayCue(
                cue_id=f"{m.moment_id}-{p.profile_id}", match_id=self.state.match_id, moment_id=m.moment_id,
                moment_kind=m.kind, profile_id=p.profile_id, language=v.language, template=template, slot=slot,
                priority=m.importance, trigger_match_seconds=m.match_seconds,
                display_at_match_seconds=round(display_at, 1), duration_s=duration, clock=m.clock,
                team_id=m.team_id, player_id=m.player_id, headline=v.headline, caption=v.caption, stats=v.stats,
                why_it_matters=job.explanation.why_it_matters if (job.explanation and p.mode == "analyst") else None,
                data=self._template_data(m, job),
                provenance={"trace": trace, "fallback_used": fallback_used,
                            "verified": not job.verification_issues, "issues": job.verification_issues},
            ))
        return cues

    def _earliest_gap(self, key: tuple[str, str], start: float, duration: float) -> float:
        t = start
        for b_start, b_end in sorted(self._bookings.get(key, [])):
            if b_end <= t:
                continue
            if b_start >= t + duration:
                break
            t = b_end
        return t

    def _template_data(self, m: Moment, job: MomentJob) -> dict[str, Any]:
        f = m.facts
        if m.kind in ("momentum_swing", "pressing_wave", "control_shift"):
            w = f["window"]
            return {"momentum": w["momentum"], "chaos_index": w["chaos_index"], "game_state": w["game_state"],
                    "home": self.state.team_name(self.state.home_id), "away": self.state.team_name(self.state.away_id)}
        if m.kind == "goal":
            return {"score": f["score"], "scorer": f["scorer"], "assist": f.get("assist"), "tags": f.get("tags", [])}
        if m.kind == "player_alert":
            pl = self.state.players.get(m.player_id or "")
            return {"number": pl.number if pl else None, "alert": f.get("alert"),
                    "speed_kmh": f.get("speed_kmh"), "distance_km": f.get("distance_km")}
        if m.kind in ("half_time", "full_time") and job.recap:
            return {"recap": job.recap.model_dump(), "home": f["home"], "away": f["away"]}
        return {}
