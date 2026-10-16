"""Deterministic live match state: the single source of truth for every number.

`MatchState` ingests events one by one (stage 1, Ingest) and maintains running
team/player tallies plus rolling-window indicators (stage 2, Interpret):

* momentum      -100..100, positive = home side on top in the window
* control/chaos 0..100 chaos index from turnover rate, duels and possession length
* pressing      pressures in the opponent half per minute and window PPDA
* rhythm        passing tempo and average possession length

Agents read this state through tools; they never compute statistics themselves,
which keeps on-screen numbers correct and auditable.
"""

from __future__ import annotations

from collections import Counter, defaultdict, deque
from dataclasses import dataclass, field
from typing import Any

IN_PLAY = {"pass", "carry", "pressure", "tackle", "interception", "ball_recovery", "shot", "possession_change", "save"}
WON_BALL = {"tackle", "interception", "ball_recovery"}


def _r(v: float, nd: int = 1) -> float:
    return round(float(v), nd)


@dataclass
class PlayerTally:
    player_id: str
    team_id: str
    name: str
    short_name: str
    number: int
    role: str
    c: Counter = field(default_factory=Counter)
    xg: float = 0.0
    pass_difficulty_completed: float = 0.0
    distance_m: float = 0.0
    top_speed_kmh: float = 0.0
    max_shot_speed_kmh: float = 0.0
    max_ball_speed_kmh: float = 0.0
    longest_pass_m: float = 0.0
    on_pitch: bool = True

    def to_dict(self) -> dict[str, Any]:
        passes = self.c["passes"]
        return {
            "player_id": self.player_id, "name": self.name, "number": self.number, "role": self.role,
            "team_id": self.team_id, "goals": self.c["goals"], "assists": self.c["assists"],
            "shots": self.c["shots"], "xg": _r(self.xg, 2), "key_passes": self.c["key_passes"],
            "passes": passes, "pass_accuracy_pct": _r(100 * self.c["passes_completed"] / passes) if passes else None,
            "progressive_passes": self.c["progressive_passes"],
            "avg_completed_pass_difficulty": _r(self.pass_difficulty_completed / self.c["passes_completed"], 2)
            if self.c["passes_completed"] else None,
            "longest_pass_m": _r(self.longest_pass_m), "max_ball_speed_kmh": _r(self.max_ball_speed_kmh),
            "max_shot_speed_kmh": _r(self.max_shot_speed_kmh), "carries": self.c["carries"],
            "take_ons": self.c["take_ons"], "tackles_won": self.c["tackles_won"],
            "interceptions": self.c["interceptions"], "recoveries": self.c["recoveries"],
            "pressures": self.c["pressures"], "fouls": self.c["fouls"], "cards": self.c["cards"],
            "distance_km": _r(self.distance_m / 1000, 2), "top_speed_kmh": _r(self.top_speed_kmh),
            "on_pitch": self.on_pitch,
        }


class MatchState:
    def __init__(self, metadata: dict, window_s: float = 300.0):
        self.metadata = metadata
        self.match_id = metadata["match_id"]
        self.home_id = metadata["home"]["team_id"]
        self.away_id = metadata["away"]["team_id"]
        self.teams = {t["team_id"]: t for t in (metadata["home"], metadata["away"])}
        self.players: dict[str, PlayerTally] = {}
        for side in ("home", "away"):
            t = metadata[side]
            for p in t["players"]:
                self.players[p["player_id"]] = PlayerTally(
                    p["player_id"], t["team_id"], p["name"], p["short_name"], p["number"], p["role"],
                    on_pitch=p.get("starter", True))
        self.window_s = window_s
        self.team = {tid: Counter() for tid in self.teams}
        self.team_xg = {tid: 0.0 for tid in self.teams}
        self.poss_time = {tid: 0.0 for tid in self.teams}
        self.score = {tid: 0 for tid in self.teams}
        self.period, self.minute, self.clock, self.match_seconds = 1, 0, "00:00", 0.0
        self.window: deque[dict] = deque()
        self.timeline: list[dict] = []  # notable events (goals, cards, subs) for recaps
        self.possessions: dict[int, dict] = {}
        self._prev_in_play: dict | None = None
        self.events_seen = 0
        self.finished = False

    # ------------------------------------------------------------------ naming helpers
    def other(self, tid: str) -> str:
        return self.away_id if tid == self.home_id else self.home_id

    def team_name(self, tid: str | None) -> str:
        return self.teams[tid]["name"] if tid in self.teams else ""

    def player_name(self, pid: str | None, short: bool = True) -> str:
        p = self.players.get(pid or "")
        return (p.short_name if short else p.name) if p else ""

    def attacking_x(self, tid: str, period: int, x: float) -> float:
        """Convert absolute x into metres from `tid`'s own goal line."""
        direction = self.metadata["attacking_direction"][str(period)][tid]
        return x if direction == "right" else 105.0 - x

    def score_line(self) -> str:
        h, a = self.teams[self.home_id], self.teams[self.away_id]
        return f"{h['name']} {self.score[self.home_id]}-{self.score[self.away_id]} {a['name']}"

    # ------------------------------------------------------------------ ingest
    def update(self, e: dict) -> None:
        self.events_seen += 1
        typ = e["type"]
        if typ == "match_metadata":
            return
        self.period = e.get("period", self.period)
        self.minute, self.clock = e.get("minute", self.minute), e.get("clock", self.clock)
        self.match_seconds = e.get("match_seconds", self.match_seconds)
        if e.get("score"):
            self.score = dict(e["score"])

        # possession time, attributed like the offline stats module
        prev = self._prev_in_play
        if typ in IN_PLAY:
            if prev is not None and prev.get("period") == e.get("period"):
                owner = None if prev["type"] == "pressure" else prev.get("team_id")
                if owner in self.poss_time:
                    self.poss_time[owner] += max(0.0, e["match_seconds"] - prev["match_seconds"])
            self._prev_in_play = e
        elif typ in ("period_end", "goal", "foul"):
            self._prev_in_play = None

        self.window.append(e)
        while self.window and self.match_seconds - self.window[0]["match_seconds"] > self.window_s:
            self.window.popleft()

        tid, pid = e.get("team_id"), e.get("player_id")
        tc = self.team.get(tid)
        pl = self.players.get(pid or "")
        m = e.get("metrics", {})
        if e.get("possession_id") is not None and tid:
            pos = self.possessions.setdefault(e["possession_id"], {"team_id": tid, "start_s": e["match_seconds"],
                                                                   "passes": 0, "end_s": e["match_seconds"]})
            pos["end_s"] = e["match_seconds"]

        if typ == "pass":
            ok = e.get("outcome") == "complete"
            tc["passes"] += 1
            tc["passes_completed"] += ok
            if ok and e.get("possession_id") in self.possessions:
                self.possessions[e["possession_id"]]["passes"] += 1
            if ok and m.get("into_final_third"):
                tc["final_third_entries"] += 1
            if ok and m.get("into_box"):
                tc["box_entries"] += 1
            if m.get("pass_type") == "cross":
                tc["crosses"] += 1
            if pl:
                pl.c["passes"] += 1
                pl.c["passes_completed"] += ok
                pl.c["progressive_passes"] += bool(ok and m.get("progressive"))
                if ok:
                    pl.pass_difficulty_completed += m.get("difficulty", 0)
                    pl.longest_pass_m = max(pl.longest_pass_m, m.get("distance_m", 0))
                pl.max_ball_speed_kmh = max(pl.max_ball_speed_kmh, m.get("ball_speed_kmh", 0))
        elif typ == "carry":
            if pl:
                pl.c["carries"] += 1
                pl.c["take_ons"] += "took_on_defender" in e.get("tags", [])
                pl.top_speed_kmh = max(pl.top_speed_kmh, m.get("speed_kmh", 0))
        elif typ == "shot":
            tc["shots"] += 1
            tc["shots_on_target"] += bool(m.get("on_target"))
            self.team_xg[tid] += m.get("xg", 0)
            if pl:
                pl.c["shots"] += 1
                pl.xg += m.get("xg", 0)
                pl.max_shot_speed_kmh = max(pl.max_shot_speed_kmh, m.get("shot_speed_kmh", 0))
            passer = self.players.get(e.get("related_player_id") or "")
            if passer:
                passer.c["key_passes"] += 1
        elif typ == "goal":
            tc["goals"] += 1
            if pl:
                pl.c["goals"] += 1
            assister = self.players.get(e.get("related_player_id") or "")
            if assister:
                assister.c["assists"] += 1
            self.timeline.append(self._timeline_entry(e))
        elif typ in WON_BALL:
            key = {"tackle": "tackles", "interception": "interceptions", "ball_recovery": "recoveries"}[typ]
            if typ == "tackle" and e.get("outcome") != "won":
                tc["tackle_fouls"] += 1
            else:
                tc[key] += 1
                if pl:
                    pl.c["tackles_won" if typ == "tackle" else key] += 1
        elif typ == "pressure":
            tc["pressures"] += 1
            if pl:
                pl.c["pressures"] += 1
                pl.top_speed_kmh = max(pl.top_speed_kmh, m.get("closing_speed_kmh", 0))
        elif typ == "foul":
            tc["fouls"] += 1
            if pl:
                pl.c["fouls"] += 1
        elif typ == "card":
            tc["yellow_cards" if e.get("outcome") == "yellow" else "red_cards"] += 1
            if pl:
                pl.c["cards"] += 1
                if e.get("outcome") != "yellow":
                    pl.on_pitch = False
            self.timeline.append(self._timeline_entry(e))
        elif typ == "corner":
            tc["corners"] += 1
        elif typ == "substitution":
            tc["substitutions"] += 1
            if pl:
                pl.on_pitch = True
            off = self.players.get(e.get("related_player_id") or "")
            if off:
                off.on_pitch = False
            self.timeline.append(self._timeline_entry(e))
        elif typ == "physical_update":
            for row in m.get("players", []):
                p = self.players.get(row["player_id"])
                if p:
                    p.distance_m = row["distance_m"]
                    p.top_speed_kmh = max(p.top_speed_kmh, row["top_speed_kmh"])
            if tid in self.team:
                self.team[tid]["distance_m"] = m.get("team_distance_m", 0)
        elif typ == "match_end":
            self.finished = True

    def _timeline_entry(self, e: dict) -> dict:
        return {"minute": e["minute"], "clock": e["clock"], "type": e["type"], "team": self.team_name(e.get("team_id")),
                "player": self.player_name(e.get("player_id"), short=False),
                "related_player": self.player_name(e.get("related_player_id"), short=False),
                "outcome": e.get("outcome"), "tags": e.get("tags", []), "score": self.score_line()}

    # ------------------------------------------------------------------ interpret
    def possession_pct(self) -> dict[str, float]:
        total = sum(self.poss_time.values()) or 1.0
        return {t: _r(100 * v / total) for t, v in self.poss_time.items()}

    def team_totals(self, tid: str) -> dict[str, Any]:
        c = self.team[tid]
        passes = c["passes"]
        return {
            "team": self.team_name(tid), "goals": self.score.get(tid, 0), "xg": _r(self.team_xg[tid], 2),
            "shots": c["shots"], "shots_on_target": c["shots_on_target"],
            "possession_pct": self.possession_pct()[tid], "passes": passes,
            "pass_accuracy_pct": _r(100 * c["passes_completed"] / passes) if passes else None,
            "final_third_entries": c["final_third_entries"], "box_entries": c["box_entries"],
            "pressures": c["pressures"], "tackles_won": c["tackles"], "interceptions": c["interceptions"],
            "fouls": c["fouls"], "corners": c["corners"], "yellow_cards": c["yellow_cards"],
            "red_cards": c["red_cards"], "distance_km": _r(c["distance_m"] / 1000),
        }

    def window_metrics(self, seconds: float | None = None) -> dict[str, Any]:
        """Rolling indicators over the last `seconds` of match time (default: state window)."""
        seconds = seconds or self.window_s
        start = self.match_seconds - seconds
        evs = [e for e in self.window if e["match_seconds"] >= start]
        per = {t: Counter() for t in self.teams}
        xg = {t: 0.0 for t in self.teams}
        poss = {t: 0.0 for t in self.teams}
        changes, prev = 0, None
        possession_ids: dict[str, set] = defaultdict(set)
        for e in evs:
            t, typ, m = e.get("team_id"), e["type"], e.get("metrics", {})
            if typ in IN_PLAY:
                if prev is not None and prev.get("period") == e.get("period") and prev["type"] != "pressure" \
                        and prev.get("team_id") in poss:
                    poss[prev["team_id"]] += max(0.0, e["match_seconds"] - prev["match_seconds"])
                prev = e
            if t not in per:
                continue
            c = per[t]
            if e.get("possession_id") is not None and typ in ("pass", "carry", "shot"):
                possession_ids[t].add(e["possession_id"])
            if typ == "pass":
                c["passes"] += 1
                ok = e.get("outcome") == "complete"
                c["completed"] += ok
                c["final_third_entries"] += bool(ok and m.get("into_final_third"))
                c["box_entries"] += bool(ok and m.get("into_box"))
            elif typ == "shot":
                c["shots"] += 1
                xg[t] += m.get("xg", 0)
            elif typ == "pressure":
                c["pressures"] += 1
                if e.get("x") is not None and self.attacking_x(t, e["period"], e["x"]) > 52.5:
                    c["high_pressures"] += 1
            elif typ in WON_BALL and e.get("outcome") == "won":
                c["ball_wins"] += 1
                if e.get("x") is not None and self.attacking_x(t, e["period"], e["x"]) > 52.5:
                    c["high_ball_wins"] += 1
            elif typ in ("tackle", "foul"):
                c["duels"] += 1
            elif typ == "possession_change":
                changes += 1
        minutes = max(seconds / 60, 1e-6)
        total_poss = sum(poss.values()) or 1.0

        def team_block(t: str) -> dict[str, Any]:
            c, o = per[t], per[self.other(t)]
            n_poss = len(possession_ids[t]) or 1
            def_actions = c["ball_wins"] + c["duels"]
            return {
                "possession_pct": _r(100 * poss[t] / total_poss), "passes": c["passes"],
                "pass_accuracy_pct": _r(100 * c["completed"] / c["passes"]) if c["passes"] else None,
                "xg": _r(xg[t], 2), "shots": c["shots"], "final_third_entries": c["final_third_entries"],
                "box_entries": c["box_entries"], "pressures_per_min": _r(c["pressures"] / minutes),
                "high_pressures": c["high_pressures"], "high_ball_wins": c["high_ball_wins"],
                "ppda": _r(o["passes"] / def_actions) if def_actions else None,
                "passes_per_possession": _r(c["completed"] / n_poss),
                "tempo_passes_per_min_in_possession": _r(c["passes"] / max(poss[t] / 60, 0.5)),
            }

        teams = {t: team_block(t) for t in self.teams}
        h, a = self.home_id, self.away_id

        def pressure_score(t: str) -> float:
            b = teams[t]
            return (b["xg"] * 12 + b["box_entries"] * 1.2 + b["final_third_entries"] * 0.5 + b["shots"] * 0.8
                    + b["high_ball_wins"] * 0.8 + b["possession_pct"] / 25)

        hs, as_ = pressure_score(h), pressure_score(a)
        momentum = _r(100 * (hs - as_) / (hs + as_ + 4), 0)
        changes_per_min = changes / minutes
        avg_ppp = (teams[h]["passes_per_possession"] + teams[a]["passes_per_possession"]) / 2
        duels_per_min = (per[h]["duels"] + per[a]["duels"]) / minutes
        chaos = max(0.0, min(100.0, 100 * (0.55 * (changes_per_min - 1.2) / 2.6 + 0.30 * (4.5 - avg_ppp) / 4.0
                                           + 0.15 * duels_per_min / 1.2)))
        return {
            "window_minutes": _r(minutes),
            "from_clock": evs[0]["clock"] if evs else self.clock, "to_clock": self.clock,
            "momentum": momentum,
            "momentum_leader": self.team_name(h if momentum > 0 else a) if abs(momentum) >= 15 else None,
            "chaos_index": _r(chaos, 0),
            "game_state": "chaotic" if chaos >= 60 else "controlled" if chaos <= 35 else "balanced",
            "possession_changes_per_min": _r(changes_per_min, 2),
            "teams": {self.team_name(t): v for t, v in teams.items()},
            "team_ids": {self.team_name(t): t for t in self.teams},
        }

    def snapshot(self) -> dict[str, Any]:
        """Compact, LLM-friendly state of the match right now."""
        return {
            "match_id": self.match_id, "clock": self.clock, "minute": self.minute, "period": self.period,
            "score": self.score_line(),
            "home": self.team_totals(self.home_id), "away": self.team_totals(self.away_id),
            "last_window": self.window_metrics(),
            "timeline": self.timeline[-8:],
            "top_performers": self.top_performers(),
        }

    def top_performers(self, n: int = 3) -> list[dict[str, Any]]:
        def rating(p: PlayerTally) -> float:
            c = p.c
            return (c["goals"] * 3 + c["assists"] * 2 + p.xg * 2 + c["key_passes"] * 0.6
                    + c["progressive_passes"] * 0.12 + c["take_ons"] * 0.2 + c["tackles_won"] * 0.3
                    + c["interceptions"] * 0.3 + (c["passes_completed"] / 40))
        ranked = sorted(self.players.values(), key=rating, reverse=True)[:n]
        return [{"name": p.name, "team": self.team_name(p.team_id), "impact": _r(rating(p), 1),
                 "goals": p.c["goals"], "assists": p.c["assists"], "xg": _r(p.xg, 2),
                 "key_passes": p.c["key_passes"]} for p in ranked]

    def player_stats(self, player_id: str) -> dict[str, Any] | None:
        p = self.players.get(player_id)
        return p.to_dict() if p else None

    def find_player(self, query: str) -> dict[str, Any] | None:
        q = query.lower().strip()
        for p in self.players.values():
            if q in (p.player_id.lower(), p.name.lower(), p.short_name.lower()) or q in p.name.lower():
                return p.to_dict()
        return None
