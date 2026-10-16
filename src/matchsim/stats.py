"""Aggregate team statistics from an event stream.

Used to validate realism of generated matches and as a deterministic baseline
that downstream agents can quote (numbers come from here, never from an LLM).
"""

from __future__ import annotations

from collections import defaultdict
from typing import Iterable

IN_PLAY_TYPES = {"pass", "carry", "pressure", "tackle", "interception", "ball_recovery", "shot", "possession_change", "save"}


def team_stats(events: Iterable[dict], metadata: dict) -> dict[str, dict]:
    events = list(events)
    team_ids = [metadata["home"]["team_id"], metadata["away"]["team_id"]]
    s: dict[str, dict] = {t: defaultdict(float) for t in team_ids}
    poss_time: dict[str, float] = defaultdict(float)
    last_phys: dict[str, list] = {}

    prev = None
    for e in events:
        t, typ = e.get("team_id"), e["type"]
        if prev is not None and typ in IN_PLAY_TYPES and prev["type"] in IN_PLAY_TYPES \
                and prev.get("period") == e.get("period"):
            owner = prev["team_id"] if prev["type"] != "pressure" else None
            if prev["type"] in ("interception", "ball_recovery", "tackle") and prev.get("outcome") == "won":
                owner = prev["team_id"]
            if owner:
                poss_time[owner] += max(0.0, e["match_seconds"] - prev["match_seconds"])
        if typ in IN_PLAY_TYPES:
            prev = e
        if t not in s:
            continue
        st = s[t]
        m = e.get("metrics", {})
        if typ == "pass":
            st["passes"] += 1
            st["passes_completed"] += e.get("outcome") == "complete"
            if m.get("pass_type") == "cross":
                st["crosses"] += 1
            if m.get("progressive") and e.get("outcome") == "complete":
                st["progressive_passes"] += 1
            st["pass_distance_total"] += m.get("distance_m", 0)
        elif typ == "shot":
            st["shots"] += 1
            st["xg"] += m.get("xg", 0)
            st["shots_on_target"] += bool(m.get("on_target"))
        elif typ == "goal":
            st["goals"] += 1
        elif typ == "tackle":
            st["tackles"] += 1
            st["tackles_won"] += e.get("outcome") == "won"
        elif typ == "interception":
            st["interceptions"] += 1
        elif typ == "ball_recovery":
            st["ball_recoveries"] += 1
        elif typ == "pressure":
            st["pressures"] += 1
        elif typ == "foul":
            st["fouls"] += 1
        elif typ == "corner":
            st["corners"] += 1
        elif typ == "card":
            st["yellow_cards" if e.get("outcome") == "yellow" else "red_cards"] += 1
        elif typ == "carry":
            st["carries"] += 1
        elif typ == "substitution":
            st["substitutions"] += 1
        elif typ == "physical_update":
            last_phys[t] = m

    total_poss = sum(poss_time.values()) or 1
    out = {}
    for t in team_ids:
        st = dict(s[t])
        passes = st.get("passes", 0)
        st["pass_accuracy_pct"] = round(100 * st.get("passes_completed", 0) / passes, 1) if passes else 0.0
        st["avg_pass_distance_m"] = round(st.pop("pass_distance_total", 0) / passes, 1) if passes else 0.0
        st["possession_pct"] = round(100 * poss_time[t] / total_poss, 1)
        st["xg"] = round(st.get("xg", 0), 2)
        phys = last_phys.get(t, {})
        players = phys.get("players", [])
        st["distance_km"] = round(phys.get("team_distance_m", 0) / 1000, 1)
        st["top_speed_kmh"] = max((p["top_speed_kmh"] for p in players), default=0.0)
        out[t] = {k: (int(v) if isinstance(v, float) and v.is_integer() else v) for k, v in st.items()}
    # PPDA: opponent passes allowed per defensive action (lower = more intense press).
    for t in team_ids:
        o = team_ids[1] if t == team_ids[0] else team_ids[0]
        actions = out[t].get("tackles", 0) + out[t].get("interceptions", 0) + out[t].get("fouls", 0)
        out[t]["ppda"] = round(out[o].get("passes", 0) / actions, 1) if actions else None
    return out


def format_table(stats: dict[str, dict], metadata: dict) -> str:
    h, a = metadata["home"], metadata["away"]
    keys = ["goals", "xg", "shots", "shots_on_target", "possession_pct", "passes", "pass_accuracy_pct",
            "avg_pass_distance_m", "progressive_passes", "crosses", "carries", "pressures", "ppda", "tackles",
            "interceptions", "ball_recoveries", "fouls", "corners", "yellow_cards", "red_cards", "substitutions", "distance_km",
            "top_speed_kmh"]
    lines = [f"{'':24}{h['short']:>10}{a['short']:>10}"]
    for k in keys:
        lines.append(f"{k:24}{str(stats[h['team_id']].get(k, 0)):>10}{str(stats[a['team_id']].get(k, 0)):>10}")
    return "\n".join(lines)
