"""Read-only access to stored matches, replayed to any point in time.

Backs the MCP server and the studio Q&A agent: every answer is computed by
replaying the deterministic `MatchState` up to the requested minute, so tools
never leak what happens later (spoiler-safe by construction).
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from matchsim.stream import load_match

from .moments import MomentDetector
from .state import MatchState

DATA_DIR = Path("data")
SAMPLES = DATA_DIR / "samples"
REPLAYS = DATA_DIR / "replays"


class MatchNotFound(KeyError):
    pass


@lru_cache(maxsize=16)
def _load(match_id: str) -> tuple[dict, list[dict]]:
    d = SAMPLES / match_id
    if not (d / "metadata.json").exists():
        raise MatchNotFound(match_id)
    return load_match(d)


@lru_cache(maxsize=16)
def _bundle(match_id: str) -> dict | None:
    p = REPLAYS / f"{match_id}.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def list_matches() -> list[dict[str, Any]]:
    out = []
    for d in sorted(SAMPLES.iterdir()) if SAMPLES.exists() else []:
        if not (d / "metadata.json").exists():
            continue
        md = json.loads((d / "metadata.json").read_text(encoding="utf-8"))
        h, a = md["home"], md["away"]
        out.append({"match_id": md["match_id"], "scenario": md["generator"]["scenario"],
                    "home": h["name"], "away": a["name"],
                    "final_score": f"{md['final_score'][h['team_id']]}-{md['final_score'][a['team_id']]}",
                    "has_agent_replay": (REPLAYS / f"{md['match_id']}.json").exists()})
    return out


def state_at(match_id: str, minute: int | None = None) -> MatchState:
    """MatchState after every event up to and including `minute` (None = full time)."""
    metadata, events = _load(match_id)
    state = MatchState(metadata)
    for e in events:
        if minute is not None and e.get("minute", 0) > minute:
            break
        state.update(e)
    return state


def moments_until(match_id: str, minute: int | None = None, kind: str | None = None) -> list[dict[str, Any]]:
    """Key moments detected up to `minute`, with on-screen-safe scalar facts."""
    metadata, events = _load(match_id)
    state = MatchState(metadata)
    detector = MomentDetector(state)
    out: list[dict[str, Any]] = []
    for e in events:
        if minute is not None and e.get("minute", 0) > minute:
            break
        state.update(e)
        for m in detector.process(e):
            if kind and m.kind != kind:
                continue
            out.append({"moment_id": m.moment_id, "kind": m.kind, "clock": m.clock, "minute": m.minute,
                        "importance": m.importance, "team": state.team_name(m.team_id) or None,
                        "facts": {k: v for k, v in m.facts.items() if not isinstance(v, (dict, list))}})
    return out


def broadcast_copy(match_id: str, moment_id: str, profile_id: str = "analyst") -> dict[str, Any] | None:
    """The agent-written overlay for a moment, from the recorded Foundry run."""
    b = _bundle(match_id)
    if not b:
        return None
    for c in b["cues"]:
        if c["moment_id"] == moment_id and c["profile_id"] == profile_id:
            trace = c.get("provenance", {}).get("trace", [])
            return {"moment_id": moment_id, "profile_id": profile_id, "language": c["language"],
                    "clock": c["clock"], "template": c["template"], "headline": c["headline"],
                    "caption": c["caption"], "stats": c.get("stats", []), "why_it_matters": c.get("why_it_matters"),
                    "agents": sorted({t["agent"] for t in trace if t["mode"] == "llm"}),
                    "facts_verified": c.get("provenance", {}).get("verified", True)}
    return None


def team_id(state: MatchState, team: str) -> str:
    t = team.lower().strip()
    for tid, info in state.teams.items():
        if t in (tid, info["name"].lower(), info["short"].lower()) or t in info["name"].lower():
            return tid
    raise KeyError(f"team '{team}' not found; try one of {[i['name'] for i in state.teams.values()]}")
