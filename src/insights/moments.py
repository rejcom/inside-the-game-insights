"""Key-moment detection with an importance score.

Deterministic rules turn the event stream into candidate moments. Each moment
carries `facts`: the only numbers downstream agents are allowed to put on screen.
Importance decides the route: high-importance moments go through the full
multi-agent workflow, low-importance ones become instant template overlays.
"""

from __future__ import annotations

import itertools
from dataclasses import asdict, dataclass, field
from typing import Any

from .state import MatchState

AI_THRESHOLD = 0.5


@dataclass
class Moment:
    moment_id: str
    kind: str
    importance: float
    match_seconds: float
    clock: str
    minute: int
    period: int
    team_id: str | None = None
    player_id: str | None = None
    related_player_id: str | None = None
    event_ids: list[str] = field(default_factory=list)
    facts: dict[str, Any] = field(default_factory=dict)

    @property
    def needs_ai(self) -> bool:
        return self.importance >= AI_THRESHOLD

    def to_dict(self) -> dict[str, Any]:
        return asdict(self) | {"needs_ai": self.needs_ai}


class MomentDetector:
    def __init__(self, state: MatchState):
        self.state = state
        self._ids = itertools.count(1)
        self._last_shot: dict | None = None
        self._next_window_check = 120.0
        self._last_momentum_call: dict[str, float] = {}
        self._last_game_state: str | None = None
        self._game_state_since = 0.0
        self._last_state_call = -1e9
        self._last_press_call: dict[str, float] = {}
        self._last_pass_call = -1e9
        self._distance_alerts: dict[tuple[str, int], int] = {}
        self.focus_players: set[str] = set()
        self._next_focus = 900.0

    def _moment(self, e: dict, kind: str, importance: float, facts: dict, **kw) -> Moment:
        s = self.state
        facts = {"score": s.score_line(), "clock": e.get("clock", s.clock), "minute": e.get("minute", s.minute)} | facts
        return Moment(moment_id=f"{s.match_id}-m{next(self._ids):04d}", kind=kind, importance=round(min(importance, 1.0), 2),
                      match_seconds=e.get("match_seconds", s.match_seconds), clock=e.get("clock", s.clock),
                      minute=e.get("minute", s.minute), period=e.get("period", s.period),
                      event_ids=[e["event_id"]] if "event_id" in e else [], facts=facts, **kw)

    def process(self, e: dict) -> list[Moment]:
        """Call AFTER state.update(e)."""
        s = self.state
        typ, m, tags = e["type"], e.get("metrics", {}), e.get("tags", [])
        out: list[Moment] = []
        team = s.team_name(e.get("team_id"))
        player = s.player_name(e.get("player_id"), short=False)

        if typ == "shot":
            self._last_shot = e
            if e.get("outcome") in ("saved", "blocked", "off_target") and m.get("xg", 0) >= 0.25:
                out.append(self._moment(e, "big_chance", 0.5 + min(m["xg"], 0.5) * 0.4, {
                    "team": team, "player": player, "xg": m["xg"], "outcome": e["outcome"],
                    "distance_m": m["distance_m"], "shot_speed_kmh": m["shot_speed_kmh"],
                    "team_xg_total": round(s.team_xg[e["team_id"]], 2),
                }, team_id=e["team_id"], player_id=e.get("player_id")))
        elif typ == "save" and self._last_shot and self._last_shot["metrics"].get("xg", 0) >= 0.15:
            out.append(self._moment(e, "key_save", 0.45 + self._last_shot["metrics"]["xg"] * 0.5, {
                "team": team, "goalkeeper": player, "xg_faced": m.get("xg_faced"),
                "shot_speed_kmh": m.get("shot_speed_kmh"),
                "shooter": s.player_name(self._last_shot.get("player_id"), short=False),
            }, team_id=e["team_id"], player_id=e.get("player_id")))
        elif typ == "goal":
            shot = self._last_shot or {"metrics": {}}
            importance = 0.9 + 0.05 * ("late_goal" in tags) + 0.05 * bool({"equaliser", "go_ahead"} & set(tags))
            pl = s.players.get(e.get("player_id"))
            out.append(self._moment(e, "goal", importance, {
                "team": team, "scorer": player, "assist": s.player_name(e.get("related_player_id"), short=False) or None,
                "xg": m.get("xg"), "shot_distance_m": shot["metrics"].get("distance_m"),
                "shot_speed_kmh": shot["metrics"].get("shot_speed_kmh"),
                "body_part": shot["metrics"].get("body_part"),
                "build_up_passes": m.get("possession_passes"), "build_up_seconds": m.get("possession_duration_s"),
                "scorer_goals_today": m.get("scorer_goals"), "tags": tags,
                "team_xg_total": round(s.team_xg[e["team_id"]], 2),
                "scorer_season_style": pl.role if pl else None,
            }, team_id=e["team_id"], player_id=e.get("player_id"), related_player_id=e.get("related_player_id")))
        elif typ == "card":
            red = e.get("outcome") != "yellow"
            out.append(self._moment(e, "red_card" if red else "yellow_card", 0.85 if red else 0.3, {
                "team": team, "player": player, "card": e.get("outcome"),
                "players_remaining": m.get("players_remaining"),
                "player_fouls": s.players[e["player_id"]].c["fouls"] if e.get("player_id") in s.players else None,
            }, team_id=e["team_id"], player_id=e.get("player_id")))
        elif typ == "substitution":
            out.append(self._moment(e, "substitution", 0.25, {
                "team": team, "player_on": player, "player_off": s.player_name(e.get("related_player_id"), short=False),
                "reason": m.get("reason"), "outgoing_distance_km": round(m.get("outgoing_distance_m", 0) / 1000, 1),
            }, team_id=e["team_id"], player_id=e.get("player_id"), related_player_id=e.get("related_player_id")))
        elif typ == "player_alert" and not self._skip_distance_alert(e):
            kind = m.get("kind")
            facts = {"team": team, "player": player, "alert": kind}
            if kind == "top_speed":
                facts |= {"speed_kmh": m["speed_kmh"], "threshold_kmh": m["threshold_kmh"]}
            else:
                facts |= {"distance_km": round(m["distance_m"] / 1000, 1)}
            out.append(self._moment(e, "player_alert", 0.3, facts, team_id=e["team_id"], player_id=e.get("player_id")))
        elif typ == "pass" and e.get("outcome") == "complete" and m.get("completion_probability", 1) <= 0.4 \
                and (m.get("into_box") or m.get("pass_type") == "through_ball") \
                and e["match_seconds"] - self._last_pass_call > 240:
            self._last_pass_call = e["match_seconds"]
            out.append(self._moment(e, "pass_quality", 0.42, {
                "team": team, "player": player, "receiver": s.player_name(e.get("related_player_id"), short=False),
                "distance_m": m["distance_m"], "ball_speed_kmh": m["ball_speed_kmh"], "pass_type": m["pass_type"],
                "difficulty_rating": m["difficulty_rating"],
                "completion_probability_pct": round(m["completion_probability"] * 100),
                "under_pressure": e.get("under_pressure", False),
            }, team_id=e["team_id"], player_id=e.get("player_id"), related_player_id=e.get("related_player_id")))
        elif typ == "added_time":
            out.append(self._moment(e, "added_time", 0.2, {"added_minutes": m.get("minutes")}))
        elif typ == "period_end":
            kind = "half_time" if e.get("period") == 1 else "full_time"
            out.append(self._moment(e, kind, 0.95, {
                "home": s.team_totals(s.home_id), "away": s.team_totals(s.away_id),
                "timeline": list(s.timeline), "top_performers": s.top_performers(),
            }))

        if self.focus_players and s.match_seconds >= self._next_focus and typ in ("pass", "carry"):
            self._next_focus += 900
            for pid in sorted(self.focus_players):
                ps = s.player_stats(pid)
                if ps and ps["on_pitch"]:
                    out.append(self._moment(e, "player_focus", 0.35, {"player": ps["name"]} | {
                        k: ps[k] for k in ("passes", "pass_accuracy_pct", "key_passes", "xg", "shots", "goals",
                                           "distance_km", "top_speed_kmh", "take_ons", "tackles_won")},
                        team_id=ps["team_id"], player_id=pid))
        if s.match_seconds >= self._next_window_check and typ in ("pass", "carry", "shot", "possession_change"):
            self._next_window_check = s.match_seconds + 60
            out.extend(self._window_moments(e))
        return out

    def _skip_distance_alert(self, e: dict) -> bool:
        """Only the first player per team to reach each distance milestone is newsworthy."""
        m = e.get("metrics", {})
        if m.get("kind") != "distance_milestone":
            return False
        key = (e.get("team_id"), m.get("distance_m"))
        self._distance_alerts[key] = self._distance_alerts.get(key, 0) + 1
        return self._distance_alerts[key] > 1

    def _window_moments(self, e: dict) -> list[Moment]:
        s = self.state
        w = s.window_metrics()
        out: list[Moment] = []
        now = s.match_seconds
        # Momentum swing: a side clearly on top, not announced for this side in the last 10 minutes.
        leader = w["momentum_leader"]
        if leader and abs(w["momentum"]) >= 45 and now - self._last_momentum_call.get(leader, -1e9) > 600:
            self._last_momentum_call[leader] = now
            tid = w["team_ids"][leader]
            out.append(self._moment(e, "momentum_swing", 0.52 + abs(w["momentum"]) / 400, {
                "team": leader, "momentum": w["momentum"], "window": w,
            }, team_id=tid))
        # Control vs chaos: the game's character changed and held for at least two checks.
        gs = w["game_state"]
        if gs != self._last_game_state:
            self._last_game_state, self._game_state_since = gs, now
        elif gs != "balanced" and now - self._game_state_since >= 120 and now - self._last_state_call > 600:
            self._last_state_call = now
            out.append(self._moment(e, "control_shift", 0.5, {
                "game_state": gs, "chaos_index": w["chaos_index"],
                "possession_changes_per_min": w["possession_changes_per_min"], "window": w,
            }))
        # Pressing wave: sustained high pressure that is actually winning the ball.
        for name, block in w["teams"].items():
            tid = w["team_ids"][name]
            if block["high_pressures"] >= 9 and block["high_ball_wins"] >= 2 \
                    and now - self._last_press_call.get(tid, -1e9) > 600:
                self._last_press_call[tid] = now
                out.append(self._moment(e, "pressing_wave", 0.5, {
                    "team": name, "high_pressures": block["high_pressures"],
                    "high_ball_wins": block["high_ball_wins"], "ppda": block["ppda"],
                    "window_minutes": w["window_minutes"], "window": w,
                }, team_id=tid))
        return out
