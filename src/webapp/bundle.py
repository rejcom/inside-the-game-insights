"""Replay bundles: one JSON with everything the web player needs for a match.

A bundle holds the match metadata, the (slimmed) event stream, every overlay cue
for every viewer profile, detected moments, a momentum/chaos series for the
timeline, and run statistics. Bundles produced with Foundry are saved under
`data/replays/` so the demo plays back real agent output without live calls.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

from insights.agents import AgentSettings
from insights.pipeline import InsightsPipeline
from matchsim.stream import load_match

REPLAY_DIR = Path("data/replays")
SERIES_STEP_S = 30.0


def slim_event(e: dict) -> dict:
    """Drop bulky fields the player does not draw (per-player physical arrays)."""
    if e["type"] == "physical_update":
        e = dict(e)
        e["metrics"] = {"team_distance_m": e.get("metrics", {}).get("team_distance_m")}
    return e


def build_bundle(match_dir: str | Path, engine: str = "offline", concurrency: int = 6) -> dict[str, Any]:
    metadata, events = load_match(match_dir)
    settings = AgentSettings.from_env()
    if engine == "offline":
        settings.endpoint = None
    series: list[dict] = []
    moments: list[dict] = []
    next_t = [SERIES_STEP_S]
    holder: dict[str, InsightsPipeline] = {}

    def on_event(e: dict) -> None:
        if e.get("match_seconds", 0) >= next_t[0] and e["type"] in ("pass", "carry", "shot", "possession_change"):
            next_t[0] = e["match_seconds"] + SERIES_STEP_S
            w = holder["p"].state.window_metrics()
            series.append({"t": e["match_seconds"], "clock": e["clock"], "momentum": w["momentum"],
                           "chaos": w["chaos_index"]})

    pipe = InsightsPipeline(metadata, settings=settings, event_sink=on_event,
                            moment_sink=lambda m: moments.append(m), ai_concurrency=concurrency)
    holder["p"] = pipe
    stats = asyncio.run(pipe.run_events(events))
    cues = sorted((c.model_dump() for c in pipe.cues), key=lambda c: c["display_at_match_seconds"])
    return {
        "match_id": metadata["match_id"],
        "engine": pipe.mode,
        "model": settings.model if pipe.mode == "foundry" else None,
        "metadata": metadata,
        "profiles": [p.model_dump() for p in pipe.profiles],
        "events": [slim_event(e) for e in events],
        "cues": cues,
        "moments": [{k: v for k, v in m.items() if k != "facts"} | {"headline_facts": {
            k: v for k, v in m["facts"].items() if not isinstance(v, (dict, list))}} for m in moments],
        "series": series,
        "run_stats": stats,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Build a replay bundle for the web player")
    parser.add_argument("match_dirs", nargs="+")
    parser.add_argument("--engine", choices=["foundry", "offline"], default="foundry")
    parser.add_argument("--out", default=str(REPLAY_DIR))
    args = parser.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    for d in args.match_dirs:
        bundle = build_bundle(d, args.engine)
        path = out / f"{bundle['match_id']}.json"
        path.write_text(json.dumps(bundle, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        print(f"{path}  engine={bundle['engine']}  cues={len(bundle['cues'])}  "
              f"stats={json.dumps(bundle['run_stats'])}")


if __name__ == "__main__":
    main()
