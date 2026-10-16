"""Event-level football match simulator.

The model is a possession chain: the ball carrier repeatedly picks an action
(pass, carry, cross, shot) whose probabilities depend on pitch zone, pressure,
player attributes, fatigue, team style and the scenario. Outcomes feed back into
possession, score and game state, so momentum, pressing waves and late-game
chasing emerge from the simulation rather than being scripted.

Internally the ball position is held in the attacking frame of the team in
possession (x = 0 own goal line, x = 105 opponent goal line); events are written
in absolute broadcast coordinates (see schema.py).
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass

from .roster import CLUBS, FIRST_NAMES, Player, Team, _surname, make_team, pick_clubs
from .scenarios import SCENARIOS, GameView, Scenario
from .schema import PITCH_LENGTH, PITCH_WIDTH, SCHEMA_VERSION, Event, EventType

GENERATOR_VERSION = "0.1.0"
HALF = 45 * 60
GOAL_Y = PITCH_WIDTH / 2
POST_HALF_WIDTH = 3.66

TOP_SPEED_ALERT_KMH = 32.0
DISTANCE_MILESTONES_M = (10000, 12000)
PHYSICAL_TICK_S = 300  # physical snapshot every 5 match minutes

# Average off-ball running speed (m/s) by role; ~10-11.5 km per 90 minutes.
ROLE_RUN_SPEED = {"GK": 0.75, "CB": 1.72, "RB": 1.95, "LB": 1.95, "RWB": 2.02, "LWB": 2.02, "DM": 2.0,
                  "CM": 2.1, "AM": 2.0, "RW": 1.95, "LW": 1.95, "ST": 1.85}

# Seconds the ball is out of play before each restart.
RESTART_DELAY = {"throw_in": (8, 18), "goal_kick": (16, 28), "corner": (22, 38), "free_kick": (18, 40),
                 "kickoff": (45, 75)}


@dataclass
class MatchResult:
    metadata: dict
    events: list[dict]
    summary: dict


def _clamp(v: float, lo: float, hi: float) -> float:
    return lo if v < lo else hi if v > hi else v


def _xg(x: float, y: float) -> tuple[float, float, float]:
    """Return (xG, distance m, angle rad) for a shot from (x, y) in the attacking frame."""
    dx, dy = PITCH_LENGTH - x, abs(y - GOAL_Y)
    dist = math.hypot(dx, dy)
    a1 = math.atan2(dy + POST_HALF_WIDTH, dx)
    a2 = math.atan2(dy - POST_HALF_WIDTH, dx)
    angle = abs(a1 - a2)
    # Logistic fit to public open-play benchmarks: ~0.45 at 6 m central, ~0.23 from
    # the penalty spot, ~0.07 from 20 m, ~0.02 from 30 m.
    logit = -0.55 - 0.1198 * dist + 1.0 * angle
    return _clamp(1 / (1 + math.exp(-logit)), 0.01, 0.9), dist, angle


class _Sim:
    def __init__(self, rng: random.Random, home: Team, away: Team, scenario: Scenario, match_id: str):
        self.rng = rng
        self.home, self.away = home, away
        self.teams = {home.team_id: home, away.team_id: away}
        self.scenario = scenario
        self.match_id = match_id
        self.on_pitch = {t.team_id: [p for p in t.players if p.starter] for t in (home, away)}
        self.bench = {t.team_id: [p for p in t.players if not p.starter] for t in (home, away)}
        self.players = {p.player_id: p for t in (home, away) for p in t.players}
        self.team_of = {p.player_id: t.team_id for t in (home, away) for p in t.players}
        self.score = {home.team_id: 0, away.team_id: 0}
        self.events: list[Event] = []
        self.seq = 0
        self.period = 1
        self.t = 0.0
        self.p1_length = float(HALF)
        self.period_end: float | None = None
        self.stoppage_load = 0.0
        self.poss_team = home.team_id
        self.carrier: Player = home.players[0]
        self.bx, self.by = 52.5, GOAL_Y
        self.poss_id = 0
        self.poss_start_t = 0.0
        self.poss_start_x = 52.5
        self.poss_max_x = 52.5
        self.poss_passes = 0
        self.last_passer: Player | None = None
        self.last_action_cross = False
        self.restart: dict | None = None
        self.forced: str | None = None
        self.set_piece_until = -1.0
        self.poss_time = {home.team_id: 0.0, away.team_id: 0.0}
        self.phys = {pid: {"distance_m": 0.0, "top_speed_kmh": 0.0, "sprints": 0, "milestone": 0}
                     for pid in self.players}
        self.fatigue = {pid: 1.0 for pid in self.players}
        self.yellows: dict[str, int] = {}
        self.subs_made = {home.team_id: 0, away.team_id: 0}
        self.forced_red_done = {home.team_id: False, away.team_id: False}
        self.next_phys_tick = PHYSICAL_TICK_S
        self.goal_log: list[dict] = []
        self.red_log: list[dict] = []
        self.max_deficit_after_50 = {home.team_id: 0, away.team_id: 0}

    # ------------------------------------------------------------------ helpers
    def other(self, tid: str) -> str:
        return self.away.team_id if tid == self.home.team_id else self.home.team_id

    def attacks_right(self, tid: str) -> bool:
        return (tid == self.home.team_id) == (self.period == 1)

    def to_abs(self, tid: str, x: float, y: float) -> tuple[float, float]:
        return (x, y) if self.attacks_right(tid) else (PITCH_LENGTH - x, PITCH_WIDTH - y)

    @property
    def clock_seconds(self) -> float:
        return self.t + (HALF if self.period == 2 else 0)

    @property
    def match_seconds(self) -> float:
        return self.t + (self.p1_length if self.period == 2 else 0)

    @property
    def minute(self) -> int:
        regulation = HALF * self.period
        c = self.clock_seconds
        if c >= regulation:  # stoppage time is reported as 45' / 90'
            return regulation // 60
        return int(c // 60) + 1

    def clock(self) -> str:
        c = self.clock_seconds
        regulation = HALF * self.period
        if c < regulation:
            return f"{int(c // 60):02d}:{int(c % 60):02d}"
        extra = c - regulation
        return f"{regulation // 60}+{int(extra // 60):02d}:{int(extra % 60):02d}"

    def mods(self, tid: str) -> dict:
        view = GameView(minute=self.minute, is_home=tid == self.home.team_id,
                        goal_diff=self.score[tid] - self.score[self.other(tid)],
                        players_on_pitch=len(self.on_pitch[tid]))
        return self.scenario.modifiers(view)

    def eff(self, p: Player, attr: str) -> float:
        """Attribute adjusted for fatigue and team strength."""
        team = self.teams[self.team_of[p.player_id]]
        return p.attributes.get(attr, 50) * (0.82 + 0.18 * self.fatigue[p.player_id]) * team.strength

    def numbers_factor(self, tid: str) -> float:
        return (len(self.on_pitch[tid]) / 11) ** 1.5

    def positions(self, tid: str) -> dict[str, tuple[float, float]]:
        """Approximate player positions in `tid`'s attacking frame for the current ball position."""
        in_poss = tid == self.poss_team
        bx, by = (self.bx, self.by) if in_poss else (PITCH_LENGTH - self.bx, PITCH_WIDTH - self.by)
        out = {}
        for p in self.on_pitch[tid]:
            if p.role == "GK":
                x = _clamp(p.base_x + (bx - 52.5) * 0.15, 2, 22 if in_poss else 12)
                y = _clamp(GOAL_Y + (by - GOAL_Y) * 0.2, 28, 40)
            elif in_poss:
                x = _clamp(p.base_x + (bx - 52.5) * 0.5 + 6, 3, 94 if p.role == "ST" else 89)
                y = _clamp(p.base_y * 0.78 + by * 0.22, 2, 66)
            else:
                x = _clamp(p.base_x * 0.85 + (bx - 52.5) * 0.45 - 4, 3, 90)
                y = _clamp(p.base_y * 0.72 + by * 0.28, 2, 66)
            out[p.player_id] = (x, y)
        return out

    def nearest_opponent(self, x: float, y: float, include_gk: bool = False) -> tuple[Player, float]:
        """Nearest defender to point (x, y) given in the possessing team's frame."""
        opp = self.other(self.poss_team)
        ox, oy = PITCH_LENGTH - x, PITCH_WIDTH - y
        pos = self.positions(opp)
        best, best_d = None, 1e9
        for p in self.on_pitch[opp]:
            if p.role == "GK" and not include_gk:
                continue
            px, py = pos[p.player_id]
            d = math.hypot(px - ox, py - oy) * self.rng.uniform(0.8, 1.2)
            if d < best_d:
                best, best_d = p, d
        return best, best_d

    def goalkeeper(self, tid: str) -> Player:
        return next((p for p in self.on_pitch[tid] if p.role == "GK"), self.on_pitch[tid][0])

    # ------------------------------------------------------------------ output
    def emit(self, etype: EventType, team: str | None = None, player: Player | None = None,
             x: float | None = None, y: float | None = None, end: tuple[float, float] | None = None,
             outcome: str | None = None, related: Player | None = None, under_pressure: bool = False,
             metrics: dict | None = None, tags: list[str] | None = None, frame: str | None = None) -> Event:
        frame = frame or team
        ax = ay = ex = ey = None
        if x is not None and frame is not None:
            ax, ay = self.to_abs(frame, x, y)
            ax, ay = round(ax, 1), round(ay, 1)
        if end is not None and frame is not None:
            ex, ey = self.to_abs(frame, *end)
            ex, ey = round(ex, 1), round(ey, 1)
        tags = list(tags or [])
        if self.match_seconds <= self.set_piece_until and etype in (EventType.PASS, EventType.SHOT, EventType.GOAL):
            tags.append("set_piece")
        self.seq += 1
        ev = Event(
            event_id=f"{self.match_id}-{self.seq:05d}", match_id=self.match_id, seq=self.seq,
            period=self.period, match_seconds=round(self.match_seconds, 1), clock=self.clock(),
            minute=self.minute, type=etype, team_id=team, player_id=player.player_id if player else None,
            x=ax, y=ay, end_x=ex, end_y=ey, outcome=outcome,
            related_player_id=related.player_id if related else None, under_pressure=under_pressure,
            possession_id=self.poss_id if team else None, score=dict(self.score),
            metrics={k: (round(v, 3) if isinstance(v, float) else v) for k, v in (metrics or {}).items()},
            tags=tags,
        )
        self.events.append(ev)
        return ev

    # ------------------------------------------------------------------ time & physical
    def advance(self, dt: float, in_play: bool = True) -> None:
        self.t += dt
        if in_play:
            self.poss_time[self.poss_team] += dt
        intensity = 1.0 if in_play else 0.45
        for tid, players in self.on_pitch.items():
            for p in players:
                base = ROLE_RUN_SPEED.get(p.role, 1.9) * intensity * (0.85 + 0.15 * self.fatigue[p.player_id])
                self.add_distance(p, base * dt * self.rng.uniform(0.9, 1.1))
                decay = (1.25 - p.attributes["stamina"] / 100) * 0.000075 * dt * intensity
                self.fatigue[p.player_id] = max(0.45, self.fatigue[p.player_id] - decay)
        if self.clock_seconds >= self.next_phys_tick:
            self.physical_tick()

    def add_distance(self, p: Player, metres: float) -> None:
        ph = self.phys[p.player_id]
        ph["distance_m"] += metres
        for i, milestone in enumerate(DISTANCE_MILESTONES_M):
            if ph["milestone"] <= i and ph["distance_m"] >= milestone:
                ph["milestone"] = i + 1
                tid = self.team_of[p.player_id]
                self.emit(EventType.PLAYER_ALERT, team=tid, player=p, outcome="distance_milestone",
                          metrics={"kind": "distance_milestone", "distance_m": milestone,
                                   "threshold_m": milestone})

    def record_speed(self, p: Player, speed_kmh: float, x: float, y: float, frame: str) -> None:
        ph = self.phys[p.player_id]
        if speed_kmh >= 25.0:
            ph["sprints"] += 1
        if speed_kmh > ph["top_speed_kmh"]:
            previous = ph["top_speed_kmh"]
            ph["top_speed_kmh"] = speed_kmh
            if speed_kmh >= TOP_SPEED_ALERT_KMH and (previous < TOP_SPEED_ALERT_KMH or speed_kmh >= previous + 1.0):
                self.emit(EventType.PLAYER_ALERT, team=self.team_of[p.player_id], player=p, x=x, y=y,
                          frame=frame, outcome="top_speed",
                          metrics={"kind": "top_speed", "speed_kmh": speed_kmh,
                                   "threshold_kmh": TOP_SPEED_ALERT_KMH, "match_best": True})

    def physical_tick(self) -> None:
        self.next_phys_tick += PHYSICAL_TICK_S
        for tid, players in self.on_pitch.items():
            team_total = sum(self.phys[q.player_id]["distance_m"] for q in self.teams[tid].players)
            self.emit(EventType.PHYSICAL_UPDATE, team=tid, metrics={"team_distance_m": round(team_total), "players": [
                {"player_id": p.player_id, "distance_m": round(self.phys[p.player_id]["distance_m"]),
                 "top_speed_kmh": round(self.phys[p.player_id]["top_speed_kmh"], 1),
                 "sprints": self.phys[p.player_id]["sprints"],
                 "fatigue": round(self.fatigue[p.player_id], 2)}
                for p in players]})

    # ------------------------------------------------------------------ possession
    def change_possession(self, new_team: str, carrier: Player, x: float, y: float, how: str) -> None:
        """Hand the ball to `new_team`; (x, y) is given in the NEW team's frame."""
        old = self.poss_team
        prev = {"previous_team_id": old, "how": how,
                "previous_duration_s": round(self.match_seconds - self.poss_start_t, 1),
                "previous_passes": self.poss_passes,
                "previous_max_progress_m": round(self.poss_max_x, 1)}
        self.poss_team, self.carrier = new_team, carrier
        self.bx, self.by = _clamp(x, 1, 104), _clamp(y, 1, 67)
        self.poss_id += 1
        self.poss_start_t = self.match_seconds
        self.poss_start_x = self.poss_max_x = self.bx
        self.poss_passes = 0
        self.last_passer = None
        self.last_action_cross = False
        self.emit(EventType.POSSESSION_CHANGE, team=new_team, player=carrier, x=self.bx, y=self.by,
                  outcome=how, metrics=prev)

    def is_counter(self) -> bool:
        return self.poss_start_x < 50 and self.match_seconds - self.poss_start_t <= 14 and self.bx > 70

    # ------------------------------------------------------------------ main loop
    def run(self) -> None:
        self.emit(EventType.MATCH_START, metrics={"home_team_id": self.home.team_id,
                                                  "away_team_id": self.away.team_id})
        for period in (1, 2):
            self.period, self.t = period, 0.0
            self.period_end = None
            self.stoppage_load = 0.0
            if period == 2:
                self.next_phys_tick = HALF + PHYSICAL_TICK_S
            kicker = self.home.team_id if period == 1 else self.away.team_id
            self.emit(EventType.PERIOD_START, metrics={"period": period})
            self.restart = {"kind": "kickoff", "team": kicker, "x": 52.5, "y": GOAL_Y, "free": True}
            self.poss_team = kicker
            while True:
                if self.period_end is None and self.t >= HALF:
                    added = int(_clamp(round(1 + self.stoppage_load + self.rng.uniform(0, 1.2)),
                                       1 if period == 1 else 3, 9))
                    self.period_end = HALF + added * 60 + self.rng.uniform(0, 40)
                    self.emit(EventType.ADDED_TIME, metrics={"minutes": added})
                if self.period_end is not None and self.t >= self.period_end and (
                        self.restart is not None or self.bx < 70 or self.t >= self.period_end + 45):
                    break
                self.step()
            if period == 2:
                self.physical_tick()
            self.emit(EventType.PERIOD_END, metrics={"period": period, "length_s": round(self.t, 1)})
            if period == 1:
                self.p1_length = self.t
        self.emit(EventType.MATCH_END, outcome="full_time", metrics={"final_score": dict(self.score)})

    def step(self) -> None:
        if self.minute > 50:
            for tid in self.score:
                deficit = self.score[self.other(tid)] - self.score[tid]
                self.max_deficit_after_50[tid] = max(self.max_deficit_after_50[tid], deficit)
        if self.restart:
            self.do_restart()
            return
        if self.forced == "cross":
            self.forced = None
            self.cross(self.carrier, under_pressure=False)
            return
        if self.forced == "fk_shot":
            self.forced = None
            self.shot(self.carrier, under_pressure=False, assist="free_kick", xg_mult=0.55, tags=["direct_free_kick"])
            return

        team, p = self.poss_team, self.carrier
        opp = self.other(team)
        m, om = self.mods(team), self.mods(opp)
        opp_team = self.teams[opp]

        # Pressure from the defending side: high pressers squeeze higher up the pitch.
        press = opp_team.style["press"] * om["press"] * self.numbers_factor(opp)
        p_press = 0.10 + 0.32 * press * (1.15 - self.bx / PITCH_LENGTH) + (0.12 if self.bx > 82 else 0)
        under_pressure = p.role != "GK" and self.rng.random() < p_press
        presser = None
        if under_pressure:
            presser, gap = self.nearest_opponent(self.bx, self.by)
            closing = _clamp(14 + presser.attributes["pace"] / 100 * 16 * (0.8 + 0.2 * self.fatigue[presser.player_id])
                             + self.rng.gauss(0, 2.5), 10, presser.top_speed_kmh)
            ox, oy = PITCH_LENGTH - self.bx, PITCH_WIDTH - self.by
            self.emit(EventType.PRESSURE, team=opp, player=presser, x=ox, y=oy, related=p,
                      metrics={"distance_to_ball_m": round(_clamp(gap * 0.25, 0.5, 4.5), 1),
                               "closing_speed_kmh": round(closing, 1)})
            self.record_speed(presser, round(closing, 1), ox, oy, opp)
            self.add_distance(presser, self.rng.uniform(5, 15))
            if self.rng.random() < 0.035 * self.foul_risk_factor():
                self.foul(presser, p)
                return

        xg, dist, _ = _xg(self.bx, self.by)
        counter = self.is_counter()
        p_shot = 0.0
        if self.bx > 72 and p.role != "GK":
            p_shot = (0.27 * xg + 0.017) * m["attack"] * (1.3 if counter else 1.0) * (0.7 if under_pressure else 1.0)
            if 75 < self.bx < 90 and abs(self.by - GOAL_Y) < 16:
                p_shot += 0.02 * m["attack"] * p.attributes["shot_power"] / 70
        wide = self.by < 14 or self.by > 54
        p_cross = 0.30 if wide and self.bx > 80 else 0.0
        p_carry = (0.16 + 0.22 * p.attributes["dribbling"] / 100 - (0.07 if under_pressure else 0)
                   + (0.08 if counter else 0)) if p.role != "GK" else 0.03

        r = self.rng.random()
        if r < p_shot:
            self.shot(p, under_pressure, assist="cross" if self.last_action_cross else "pass",
                      tags=["counter_attack"] if counter else [])
        elif r < p_shot + p_cross:
            self.cross(p, under_pressure)
        elif r < p_shot + p_cross + p_carry:
            self.carry(p, under_pressure, presser)
        else:
            self.pass_ball(p, under_pressure, m)

    # ------------------------------------------------------------------ actions
    def pass_ball(self, p: Player, under_pressure: bool, m: dict, forced_short: bool = False) -> None:
        team = self.poss_team
        opp = self.other(team)
        style = self.teams[team].style
        directness = _clamp(style["directness"] * m["directness"] / m["control"], 0.05, 1.0)
        if forced_short:
            directness *= 0.3
        pos = self.positions(team)
        cands, weights = [], []
        for q in self.on_pitch[team]:
            if q is p:
                continue
            qx, qy = pos[q.player_id]
            d = math.hypot(qx - self.bx, qy - self.by)
            fwd = qx - self.bx
            w = math.exp(-d / (7 + 14 * directness)) * math.exp(fwd * (0.01 + 0.025 * directness))
            if d < 5:
                w *= 0.2
            if q.role == "GK":
                w *= 0.25 if (under_pressure and self.bx < 40) else 0.03
            cands.append(q)
            weights.append(w)
        q = self.rng.choices(cands, weights)[0]
        tx, ty = pos[q.player_id]
        tx = _clamp(tx + self.rng.gauss(0, 2.5), 1, 104)
        ty = _clamp(ty + self.rng.gauss(0, 2.5), 1, 67)
        d = math.hypot(tx - self.bx, ty - self.by)
        fwd = tx - self.bx
        if d < 18:
            ptype = "short"
        elif d < 32:
            ptype = "medium"
        else:
            ptype = "long"
        if abs(ty - self.by) > 35:
            ptype = "switch"
        elif fwd > 18 and tx > 75:
            ptype = "through_ball"
        elif fwd < -5:
            ptype = "back"
        def in_box(px: float, py: float) -> bool:
            return px > 88.5 and 13.8 < py < 54.2
        into_box = in_box(tx, ty) and not in_box(self.bx, self.by)
        difficulty = (0.02 + (d / 80) ** 1.4 + (0.12 if under_pressure else 0) + (0.07 if fwd > 12 else 0)
                      + (0.08 if tx > 80 else 0) + (0.14 if into_box else 0)
                      + (0.12 if in_box(tx, ty) and not into_box else 0)
                      + (0.06 if ptype in ("long", "switch") else 0)
                      + (0.08 if ptype == "through_ball" else 0) - (0.05 if fwd < -5 else 0))
        difficulty = _clamp(difficulty, 0.02, 0.97)
        skill = self.eff(p, "passing") * (0.7 + 0.3 * self.eff(p, "vision") / 75)
        opp_quality = self.teams[opp].strength * self.numbers_factor(opp) ** 0.5
        p_success = _clamp(1 - difficulty * (1.08 - skill / 100 * 0.65) * opp_quality ** 1.5, 0.04, 0.99)
        speed = _clamp(26 + d * 1.15 + self.rng.gauss(0, 5), 18, 112)
        flight = d / (speed / 3.6) * 1.3
        progressive = fwd >= 10 and tx > 40
        metrics = {"distance_m": round(d, 1), "ball_speed_kmh": round(speed, 1), "pass_type": ptype,
                   "difficulty": round(difficulty, 2), "difficulty_rating": int(_clamp(round(difficulty * 10), 1, 10)),
                   "completion_probability": round(p_success, 2), "progressive": progressive,
                   "into_final_third": self.bx < 70 <= tx, "into_box": into_box}
        success = self.rng.random() < p_success
        tags = ["counter_attack"] if self.is_counter() else []
        self.emit(EventType.PASS, team=team, player=p, x=self.bx, y=self.by, end=(tx, ty),
                  outcome="complete" if success else "incomplete", related=q, under_pressure=under_pressure,
                  metrics=metrics, tags=tags)
        self.last_action_cross = False
        if success:
            self.advance(flight + self.rng.uniform(1.0, 2.6))
            self.poss_passes += 1
            self.last_passer = p
            self.carrier, self.bx, self.by = q, tx, ty
            self.poss_max_x = max(self.poss_max_x, tx)
            return
        self.advance(flight + 0.6)
        self.turnover_after_failed_ball(tx + self.rng.gauss(0, 4), ty + self.rng.gauss(0, 4))

    def turnover_after_failed_ball(self, tx: float, ty: float) -> None:
        team = self.poss_team
        opp = self.other(team)
        if ty < 0.5 or ty > 67.5 or (self.rng.random() < 0.22 and (ty < 8 or ty > 60)):
            y_line = 0.0 if ty < GOAL_Y else PITCH_WIDTH
            self.restart = {"kind": "throw_in", "team": opp, "x": PITCH_LENGTH - _clamp(tx, 1, 104),
                            "y": PITCH_WIDTH - y_line, "lost_by": "out_of_play"}
            return
        if tx > 104:
            self.restart = {"kind": "goal_kick", "team": opp, "x": 5.5, "y": GOAL_Y + self.rng.choice((-9, 9)),
                            "lost_by": "out_of_play"}
            return
        tx, ty = _clamp(tx, 1, 104), _clamp(ty, 1, 67)
        winner, _ = self.nearest_opponent(tx, ty, include_gk=tx > 96)
        ox, oy = PITCH_LENGTH - tx, PITCH_WIDTH - ty
        read_it = self.rng.random() < 0.14 * self.eff(winner, "tackling") / 65
        etype = EventType.INTERCEPTION if read_it else EventType.BALL_RECOVERY
        self.emit(etype, team=opp, player=winner, x=ox, y=oy, outcome="won")
        self.change_possession(opp, winner, ox, oy, "interception" if read_it else "loose_ball")

    def carry(self, p: Player, under_pressure: bool, presser: Player | None) -> None:
        team = self.poss_team
        opp = self.other(team)
        space = 1.4 if self.is_counter() else 1.0
        dist = _clamp(self.rng.uniform(3, 12) * space * (0.8 + p.attributes["dribbling"] / 250), 2, 40)
        angle = self.rng.gauss(0, 0.6)
        if self.by < 12:
            angle += 0.3
        elif self.by > 56:
            angle -= 0.3
        nx = _clamp(self.bx + dist * math.cos(angle), 1, 97)
        ny = _clamp(self.by + dist * math.sin(angle), 2, 66)
        burst = self.rng.uniform(0.62, 0.99) if space > 1 else self.rng.uniform(0.45, 0.85)
        speed = _clamp(p.top_speed_kmh * burst * (0.85 + 0.15 * self.fatigue[p.player_id]), 8, p.top_speed_kmh)
        duration = dist / (speed / 3.6)

        tackled = False
        if under_pressure and presser is not None:
            p_tackle = _clamp(0.5 * self.eff(presser, "tackling") / 70 - 0.12 * (self.eff(p, "dribbling") - 60) / 40,
                              0.08, 0.6)
            tackled = self.rng.random() < p_tackle
        metrics = {"distance_m": round(math.hypot(nx - self.bx, ny - self.by), 1), "speed_kmh": round(speed, 1),
                   "duration_s": round(duration, 1), "progressive": nx - self.bx >= 8}
        if not tackled:
            tags = ["took_on_defender"] if under_pressure else []
            if self.is_counter():
                tags.append("counter_attack")
            self.emit(EventType.CARRY, team=team, player=p, x=self.bx, y=self.by, end=(nx, ny), outcome="complete",
                      under_pressure=under_pressure, metrics=metrics, tags=tags)
            self.add_distance(p, metrics["distance_m"])
            self.record_speed(p, round(speed, 1), nx, ny, team)
            self.advance(duration)
            self.bx, self.by = nx, ny
            self.poss_max_x = max(self.poss_max_x, nx)
            return

        # Tackle attempt by the presser: clean win, foul, or (rarely) card.
        ox, oy = PITCH_LENGTH - self.bx, PITCH_WIDTH - self.by
        foul = self.rng.random() < (0.16 if self.bx > 60 else 0.11) * self.foul_risk_factor()
        if foul:
            self.emit(EventType.TACKLE, team=opp, player=presser, x=ox, y=oy, outcome="foul", related=p)
            self.foul(presser, p)
            return
        self.emit(EventType.TACKLE, team=opp, player=presser, x=ox, y=oy, outcome="won", related=p)
        self.advance(1.0)
        self.change_possession(opp, presser, ox, oy, "tackle")

    def foul_risk_factor(self) -> float:
        """Defenders are far more careful inside their own box (~0.3 penalties per match)."""
        return 0.12 if (self.bx > 88.5 and 13.8 < self.by < 54.2) else 1.0

    def foul(self, offender: Player, victim: Player) -> None:
        team = self.poss_team  # fouled team keeps the ball
        opp = self.team_of[offender.player_id]
        ox, oy = PITCH_LENGTH - self.bx, PITCH_WIDTH - self.by
        self.emit(EventType.FOUL, team=opp, player=offender, x=ox, y=oy, related=victim)
        om = self.mods(opp)
        card = None
        if om["force_red"] and not self.forced_red_done[opp] and offender.role != "GK":
            card = "red"
            self.forced_red_done[opp] = True
        elif self.rng.random() < (0.05 if self.yellows.get(offender.player_id) else 0.17):
            card = "second_yellow" if self.yellows.get(offender.player_id) else "yellow"
        elif self.rng.random() < 0.006:
            card = "red"
        if card:
            self.yellows[offender.player_id] = self.yellows.get(offender.player_id, 0) + 1
            self.emit(EventType.CARD, team=opp, player=offender, outcome=card,
                      metrics={"card": card, "players_remaining": len(self.on_pitch[opp]) - (card != "yellow")})
            self.stoppage_load += 0.25
            if card != "yellow":
                self.on_pitch[opp] = [q for q in self.on_pitch[opp] if q is not offender]
                self.red_log.append({"side": "home" if opp == self.home.team_id else "away", "minute": self.minute,
                                     "player_id": offender.player_id})
        in_box = self.bx > 88.5 and 13.8 < self.by < 54.2
        if in_box:
            self.penalty(victim)
            return
        self.restart = {"kind": "free_kick", "team": team, "x": self.bx, "y": self.by, "taker": None}

    def penalty(self, fouled: Player) -> None:
        team = self.poss_team
        taker = max(self.on_pitch[team], key=lambda q: q.attributes["finishing"] + q.attributes["composure"])
        self.advance(self.rng.uniform(50, 80), in_play=False)
        self.carrier, self.bx, self.by = taker, 94.0, GOAL_Y
        self.set_piece_until = self.match_seconds + 3
        self.shot(taker, under_pressure=False, assist="penalty", fixed_xg=0.78, tags=["penalty"])

    def cross(self, p: Player, under_pressure: bool) -> None:
        team = self.poss_team
        pos = self.positions(team)
        targets = [q for q in self.on_pitch[team] if q is not p and q.role in ("ST", "AM", "RW", "LW", "CM", "CB")]
        q = self.rng.choice(targets) if targets else self.rng.choice([x for x in self.on_pitch[team] if x is not p])
        tx = _clamp(self.rng.uniform(88, 99), 1, 104)
        ty = _clamp(GOAL_Y + self.rng.gauss(0, 6), 20, 48)
        d = math.hypot(tx - self.bx, ty - self.by)
        skill = self.eff(p, "passing")
        p_success = _clamp(0.20 + (skill - 60) / 200 - (0.06 if under_pressure else 0), 0.08, 0.45)
        speed = _clamp(55 + self.rng.gauss(0, 8), 35, 95)
        success = self.rng.random() < p_success
        self.emit(EventType.PASS, team=team, player=p, x=self.bx, y=self.by, end=(tx, ty),
                  outcome="complete" if success else "incomplete", related=q, under_pressure=under_pressure,
                  metrics={"distance_m": round(d, 1), "ball_speed_kmh": round(speed, 1), "pass_type": "cross",
                           "difficulty": round(1 - p_success, 2),
                           "difficulty_rating": int(_clamp(round((1 - p_success) * 10), 1, 10)),
                           "completion_probability": round(p_success, 2), "progressive": False,
                           "into_final_third": False, "into_box": True})
        self.advance(d / (speed / 3.6) * 1.2)
        if success:
            self.poss_passes += 1
            self.last_passer = p
            self.last_action_cross = True
            self.carrier, self.bx, self.by = q, tx, ty
            if self.rng.random() < 0.55:
                self.shot(q, under_pressure=True, assist="cross", body_part="head" if self.rng.random() < 0.6 else None,
                          xg_mult=0.6)
            return
        if self.rng.random() < 0.38:
            self.restart = {"kind": "corner", "team": team, "x": 104.5, "y": 0.5 if self.by < GOAL_Y else 67.5,
                            "lost_by": "cleared"}
            return
        self.turnover_after_failed_ball(tx - self.rng.uniform(5, 20), ty)

    def shot(self, p: Player, under_pressure: bool, assist: str = "pass", xg_mult: float = 1.0,
             fixed_xg: float | None = None, body_part: str | None = None, tags: list[str] | None = None) -> None:
        team = self.poss_team
        opp = self.other(team)
        m = self.mods(team)
        xg, dist, angle = _xg(self.bx, self.by)
        if fixed_xg is not None:
            xg = fixed_xg
        else:
            xg = _clamp(xg * xg_mult * (0.78 if under_pressure else 1.0) * (1.15 if self.is_counter() else 1.0),
                        0.01, 0.9)
        body_part = body_part or ("right_foot" if self.rng.random() < 0.68 else "left_foot")
        power = p.attributes["shot_power"]
        shot_speed = _clamp(52 + power * 0.48 + (8 if dist > 18 else 0) - (25 if body_part == "head" else 0)
                            + self.rng.gauss(0, 7), 35, 128)
        gk = self.goalkeeper(opp)
        finishing = self.eff(p, "finishing")
        p_goal = _clamp(xg * (0.72 + finishing / 100 * 0.45) * m["finishing"]
                        * (1.18 - self.eff(gk, "goalkeeping") / 100 * 0.3), 0.005, 0.92)
        r = self.rng.random()
        if r < p_goal:
            outcome = "goal"
        else:
            rest = self.rng.random()
            outcome = "saved" if rest < 0.38 else "blocked" if rest < 0.62 else "off_target"
        tags = list(tags or [])
        if self.is_counter() and "counter_attack" not in tags:
            tags.append("counter_attack")
        self.emit(EventType.SHOT, team=team, player=p, x=self.bx, y=self.by,
                  end=(PITCH_LENGTH, _clamp(GOAL_Y + self.rng.gauss(0, 2.2 if outcome in ("goal", "saved") else 5), 25, 43)),
                  outcome=outcome, related=self.last_passer if assist != "penalty" else None,
                  under_pressure=under_pressure,
                  metrics={"xg": round(xg, 3), "distance_m": round(dist, 1), "angle_deg": round(math.degrees(angle), 1),
                           "shot_speed_kmh": round(shot_speed, 1), "body_part": body_part, "assist_type": assist,
                           "on_target": outcome in ("goal", "saved")},
                  tags=tags)
        self.advance(self.rng.uniform(0.8, 1.6))

        if outcome == "goal":
            self.goal(p, xg, tags)
        elif outcome == "saved":
            self.emit(EventType.SAVE, team=opp, player=gk, x=PITCH_LENGTH - 104, y=GOAL_Y, related=p,
                      metrics={"shot_speed_kmh": round(shot_speed, 1), "xg_faced": round(xg, 3)})
            if self.rng.random() < 0.33:
                self.restart = {"kind": "corner", "team": team, "x": 104.5,
                                "y": 0.5 if self.rng.random() < 0.5 else 67.5, "lost_by": "save"}
            else:
                self.advance(self.rng.uniform(3, 7))
                self.change_possession(opp, gk, 6.0, GOAL_Y, "goalkeeper_save")
        elif outcome == "blocked":
            if self.rng.random() < 0.42:
                self.restart = {"kind": "corner", "team": team, "x": 104.5,
                                "y": 0.5 if self.by < GOAL_Y else 67.5, "lost_by": "blocked"}
            elif self.rng.random() < 0.5:
                self.bx = _clamp(self.bx - self.rng.uniform(5, 15), 60, 100)  # rebound stays with attackers
            else:
                self.turnover_after_failed_ball(self.bx - 2, self.by)
        else:
            self.restart = {"kind": "goal_kick", "team": opp, "x": 5.5, "y": GOAL_Y + self.rng.choice((-9, 9)),
                            "lost_by": "off_target"}

    def goal(self, scorer: Player, xg: float, shot_tags: list[str]) -> None:
        team = self.poss_team
        opp = self.other(team)
        before_diff = self.score[team] - self.score[opp]
        self.score[team] += 1
        tags = [t for t in shot_tags if t in ("counter_attack", "penalty", "direct_free_kick")]
        if before_diff == -1:
            tags.append("equaliser")
        elif before_diff == 0:
            tags.append("go_ahead")
        elif before_diff >= 1:
            tags.append("extends_lead")
        else:
            tags.append("pulls_one_back")
        if self.minute >= 85:
            tags.append("late_goal")
        scorer_goals = sum(1 for g in self.goal_log if g["player_id"] == scorer.player_id) + 1
        if scorer_goals == 2:
            tags.append("brace")
        elif scorer_goals == 3:
            tags.append("hat_trick")
        assist = self.last_passer if self.last_passer is not scorer and "penalty" not in tags else None
        self.emit(EventType.GOAL, team=team, player=scorer, x=self.bx, y=self.by, related=assist,
                  metrics={"xg": round(xg, 3), "scorer_goals": scorer_goals,
                           "possession_passes": self.poss_passes,
                           "possession_duration_s": round(self.match_seconds - self.poss_start_t, 1)},
                  tags=tags)
        side = "home" if team == self.home.team_id else "away"
        self.goal_log.append({"side": side, "team_id": team, "player_id": scorer.player_id, "minute": self.minute,
                              "match_seconds": round(self.match_seconds, 1), "xg": round(xg, 3), "tags": tags,
                              "changed_result": before_diff in (-1, 0)})
        self.stoppage_load += 0.6
        self.restart = {"kind": "kickoff", "team": opp, "x": 52.5, "y": GOAL_Y, "lost_by": "goal_conceded"}

    # ------------------------------------------------------------------ restarts & substitutions
    def maybe_substitute(self, half_time: bool = False) -> None:
        if self.period != 2:
            return
        for tid in (self.home.team_id, self.away.team_id):
            if self.subs_made[tid] >= 5 or not self.bench[tid]:
                continue
            behind = self.score[tid] < self.score[self.other(tid)]
            if half_time:
                rate = 0.35 if behind else 0.15
            else:
                rate = 0.0 if self.minute < 56 else (0.2 if behind else 0.14) + (0.1 if self.minute > 72 else 0)
            if self.rng.random() >= rate:
                continue
            outfield = [p for p in self.on_pitch[tid] if p.role != "GK"]
            off = min(outfield, key=lambda q: self.fatigue[q.player_id] + self.rng.uniform(0, 0.08))
            same = [b for b in self.bench[tid] if b.role == off.role] or \
                   [b for b in self.bench[tid] if b.role != "GK"]
            if not same:
                continue
            on = self.rng.choice(same)
            on.base_x, on.base_y = off.base_x, off.base_y
            if on.role != off.role:
                on.role = off.role
            self.bench[tid].remove(on)
            self.on_pitch[tid] = [on if q is off else q for q in self.on_pitch[tid]]
            self.subs_made[tid] += 1
            self.stoppage_load += 0.35
            reason = "tactical" if behind else ("fatigue" if self.fatigue[off.player_id] < 0.8 else "rotation")
            self.emit(EventType.SUBSTITUTION, team=tid, player=on, related=off,
                      metrics={"reason": reason, "outgoing_distance_m": round(self.phys[off.player_id]["distance_m"]),
                               "outgoing_fatigue": round(self.fatigue[off.player_id], 2),
                               "subs_used": self.subs_made[tid]})

    def do_restart(self) -> None:
        r, self.restart = self.restart, None
        kind, team = r["kind"], r["team"]
        lo, hi = RESTART_DELAY[kind]
        if not r.get("free"):
            self.advance(self.rng.uniform(lo, hi), in_play=False)
        self.maybe_substitute(half_time=kind == "kickoff" and self.period == 2 and self.t < 1)
        x, y = r["x"], r["y"]
        if team != self.poss_team:
            # Possession switches at the restart; record why it was lost.
            self.poss_team = team
            self.poss_id += 1
            self.poss_start_t, self.poss_start_x, self.poss_max_x, self.poss_passes = self.match_seconds, x, x, 0
            self.last_passer, self.last_action_cross = None, False
            self.emit(EventType.POSSESSION_CHANGE, team=team, x=x, y=y, outcome=r.get("lost_by", kind),
                      metrics={"previous_team_id": self.other(team), "how": r.get("lost_by", kind)})
        on = self.on_pitch[team]
        if kind == "goal_kick":
            taker = self.goalkeeper(team)
        elif kind == "kickoff":
            taker = next((q for q in on if q.role == "ST"), on[-1])
        elif kind == "corner":
            taker = max((q for q in on if q.role != "GK"), key=lambda q: q.attributes["passing"])
        elif kind == "throw_in":
            fullbacks = [q for q in on if q.role in ("RB", "LB", "RWB", "LWB")]
            taker = self.rng.choice(fullbacks or [q for q in on if q.role != "GK"])
        else:
            taker = max((q for q in on if q.role != "GK"), key=lambda q: q.attributes["passing"] + self.rng.uniform(0, 15))
        self.carrier, self.bx, self.by = taker, x, y
        etype = {"throw_in": EventType.THROW_IN, "goal_kick": EventType.GOAL_KICK, "corner": EventType.CORNER,
                 "free_kick": EventType.FREE_KICK, "kickoff": EventType.KICKOFF}[kind]
        self.emit(etype, team=team, player=taker, x=x, y=y)
        if kind in ("corner", "free_kick"):
            self.set_piece_until = self.match_seconds + 8
        if kind == "corner":
            self.forced = "cross"
        elif kind == "free_kick" and x > 76 and abs(y - GOAL_Y) < 18 and self.rng.random() < 0.4:
            self.forced = "fk_shot"
        elif kind == "free_kick" and x > 70 and self.rng.random() < 0.5:
            self.forced = "cross"
        elif kind in ("kickoff", "throw_in"):
            self.pass_ball(taker, under_pressure=False, m=self.mods(team), forced_short=True)

    # ------------------------------------------------------------------ summary
    def summary(self) -> dict:
        h, a = self.home.team_id, self.away.team_id
        side = {h: "home", a: "away"}
        total = sum(self.poss_time.values()) or 1
        shots = {"home": 0, "away": 0}
        for e in self.events:
            if e.type == EventType.SHOT:
                shots[side[e.team_id]] += 1
        return {
            "score": {"home": self.score[h], "away": self.score[a]},
            "possession": {"home": round(100 * self.poss_time[h] / total), "away": round(100 * self.poss_time[a] / total)},
            "shots": shots,
            "goals": self.goal_log,
            "red_cards": self.red_log,
            "max_deficit_after_50": {"home": self.max_deficit_after_50[h], "away": self.max_deficit_after_50[a]},
            "event_count": len(self.events),
        }


def _referee_name(rng: random.Random) -> str:
    return f"{rng.choice(FIRST_NAMES)} {_surname(rng)}"


def _simulate_once(seed: int, scenario: Scenario, home_club: str | None, away_club: str | None,
                   match_id: str | None) -> MatchResult:
    rng = random.Random(seed)
    hc, ac = pick_clubs(rng, home_club, away_club)
    home = make_team(rng, hc, hc["short"].lower(), scenario.home_strength * rng.uniform(0.98, 1.02))
    away = make_team(rng, ac, ac["short"].lower(), scenario.away_strength * rng.uniform(0.98, 1.02))
    match_id = match_id or f"syn-{scenario.name.replace('_', '-')}-{home.short.lower()}-{away.short.lower()}-{seed}"
    sim = _Sim(rng, home, away, scenario, match_id)
    sim.run()
    summary = sim.summary()
    metadata = {
        "match_id": match_id,
        "schema_version": SCHEMA_VERSION,
        "synthetic": True,
        "disclaimer": "Fully synthetic data. Clubs, players, venues and events are procedurally generated.",
        "generator": {"name": "matchsim", "version": GENERATOR_VERSION, "seed": seed, "scenario": scenario.name,
                      "scenario_description": scenario.description},
        "competition": "Inside the Game Synthetic League",
        "venue": hc["venue"],
        "referee": _referee_name(rng),
        "weather": rng.choice(["clear", "overcast", "light_rain", "windy", "cold_and_dry"]),
        "pitch": {"length_m": PITCH_LENGTH, "width_m": PITCH_WIDTH},
        "attacking_direction": {
            "1": {home.team_id: "right", away.team_id: "left"},
            "2": {home.team_id: "left", away.team_id: "right"},
        },
        "home": home.to_dict(),
        "away": away.to_dict(),
        "final_score": {home.team_id: sim.score[home.team_id], away.team_id: sim.score[away.team_id]},
        "period_lengths_s": [round(sim.p1_length, 1), round(sim.t, 1)],
    }
    return MatchResult(metadata=metadata, events=[e.to_dict() for e in sim.events], summary=summary)


def simulate_match(seed: int = 7, scenario: str = "balanced", home: str | None = None, away: str | None = None,
                   match_id: str | None = None, max_attempts: int = 400) -> MatchResult:
    """Simulate one match. For storytelling scenarios, seeds are tried from `seed`
    upwards until the match satisfies the scenario; the used seed is in the metadata."""
    sc = SCENARIOS[scenario]
    for attempt in range(max_attempts):
        result = _simulate_once(seed + attempt, sc, home, away, match_id)
        if sc.accept(result.summary):
            result.metadata["generator"]["attempts"] = attempt + 1
            return result
    raise RuntimeError(f"scenario '{scenario}' not satisfied within {max_attempts} seeds starting at {seed}")


__all__ = ["simulate_match", "MatchResult", "CLUBS"]
