"""Run the insights pipeline over a generated match and write overlay cues."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from collections import Counter
from pathlib import Path

from matchsim.stream import load_match

from .agents import AgentSettings
from .pipeline import InsightsPipeline
from .telemetry import setup_telemetry


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="insights", description="Multi-agent match insights pipeline")
    parser.add_argument("match_dir", help="folder produced by `matchsim generate`")
    parser.add_argument("--out", help="output folder (default: <match_dir>/insights)")
    parser.add_argument("--speed", type=float, default=0.0, help="replay speed; 0 = as fast as possible")
    parser.add_argument("--offline", action="store_true", help="force deterministic twins (no Foundry calls)")
    parser.add_argument("--concurrency", type=int, default=4, help="parallel agent workflows")
    parser.add_argument("--show", default="analyst", help="print cues for this profile id to stdout")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")

    setup_telemetry("insights-cli")
    metadata, events = load_match(args.match_dir)
    out = Path(args.out or Path(args.match_dir) / "insights")
    out.mkdir(parents=True, exist_ok=True)
    settings = AgentSettings.from_env()
    if args.offline:
        settings.endpoint = None
    cues_f = (out / "overlays.jsonl").open("w", encoding="utf-8")
    moments_f = (out / "moments.jsonl").open("w", encoding="utf-8")

    def on_cue(cue: dict) -> None:
        cues_f.write(json.dumps(cue, ensure_ascii=False) + "\n")
        if cue["profile_id"] == args.show:
            modes = ",".join(sorted({t["mode"] for t in cue["provenance"]["trace"]}))
            print(f"[{cue['clock']:>9}] {cue['template']:<12} {cue['headline']}  |  {cue['caption']}  ({modes})")

    pipeline = InsightsPipeline(metadata, settings=settings, cue_sink=on_cue,
                                moment_sink=lambda m: moments_f.write(json.dumps(m, ensure_ascii=False, default=str) + "\n"),
                                ai_concurrency=args.concurrency, speed=args.speed)
    print(f"mode: {pipeline.mode}  profiles: {', '.join(p.profile_id for p in pipeline.profiles)}", file=sys.stderr)
    stats = asyncio.run(pipeline.run_events(events))
    cues_f.close()
    moments_f.close()
    (out / "profiles.json").write_text(json.dumps([p.model_dump() for p in pipeline.profiles], indent=2,
                                                  ensure_ascii=False), encoding="utf-8")
    per_profile = Counter(c.profile_id for c in pipeline.cues)
    stats["cues_per_profile"] = dict(per_profile)
    (out / "run_stats.json").write_text(json.dumps(stats, indent=2), encoding="utf-8")
    print(json.dumps(stats), file=sys.stderr)


if __name__ == "__main__":
    main()
