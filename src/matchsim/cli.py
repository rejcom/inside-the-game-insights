"""Command line interface: generate, inspect and stream synthetic matches."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .engine import simulate_match
from .roster import CLUBS
from .scenarios import SCENARIOS
from .stats import format_table, team_stats
from .stream import http_sink, load_match, replay, stdout_sink


def _write_match(result, out_dir: Path) -> Path:
    match_dir = out_dir / result.metadata["match_id"]
    match_dir.mkdir(parents=True, exist_ok=True)
    (match_dir / "metadata.json").write_text(json.dumps(result.metadata, indent=2, ensure_ascii=False), encoding="utf-8")
    with (match_dir / "events.jsonl").open("w", encoding="utf-8") as f:
        for e in result.events:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
    stats = team_stats(result.events, result.metadata)
    (match_dir / "stats.json").write_text(json.dumps(stats, indent=2), encoding="utf-8")
    return match_dir


def cmd_generate(args: argparse.Namespace) -> None:
    out = Path(args.out)
    for i in range(args.count):
        result = simulate_match(seed=args.seed + i * 1000, scenario=args.scenario, home=args.home, away=args.away)
        match_dir = _write_match(result, out)
        md = result.metadata
        h, a = md["home"], md["away"]
        print(f"{match_dir}  {h['name']} {md['final_score'][h['team_id']]}-{md['final_score'][a['team_id']]} "
              f"{a['name']}  ({len(result.events)} events, scenario={args.scenario}, "
              f"seed={md['generator']['seed']})", file=sys.stderr)


def cmd_stats(args: argparse.Namespace) -> None:
    metadata, events = load_match(args.match_dir)
    print(format_table(team_stats(events, metadata), metadata))


def cmd_stream(args: argparse.Namespace) -> None:
    sinks = [stdout_sink] if not args.quiet else []
    if args.http:
        sinks.append(http_sink(args.http))
    n = replay(args.match_dir, sinks, speed=args.speed, from_minute=args.from_minute)
    print(f"streamed {n} events", file=sys.stderr)


def cmd_list(_: argparse.Namespace) -> None:
    print("Scenarios:")
    for s in SCENARIOS.values():
        print(f"  {s.name:22} {s.description}")
    print("Clubs (fictional):")
    for c in CLUBS:
        print(f"  {c['short']}  {c['name']}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="matchsim", description="Synthetic football match generator")
    sub = parser.add_subparsers(dest="cmd", required=True)

    g = sub.add_parser("generate", help="simulate match(es) and write metadata.json, events.jsonl, stats.json")
    g.add_argument("--seed", type=int, default=7)
    g.add_argument("--scenario", choices=sorted(SCENARIOS), default="balanced")
    g.add_argument("--home", help="club name or short code (default: random)")
    g.add_argument("--away", help="club name or short code (default: random)")
    g.add_argument("--count", type=int, default=1)
    g.add_argument("--out", default="data/generated")
    g.set_defaults(func=cmd_generate)

    s = sub.add_parser("stats", help="print team statistics for a generated match")
    s.add_argument("match_dir")
    s.set_defaults(func=cmd_stats)

    st = sub.add_parser("stream", help="replay a match as a paced live event feed")
    st.add_argument("match_dir")
    st.add_argument("--speed", type=float, default=10.0, help="1 = real time, 0 = no pacing")
    st.add_argument("--from-minute", type=int, default=0)
    st.add_argument("--http", help="also POST each event to this URL")
    st.add_argument("--quiet", action="store_true", help="do not print events to stdout")
    st.set_defaults(func=cmd_stream)

    ls = sub.add_parser("list", help="list scenarios and clubs")
    ls.set_defaults(func=cmd_list)

    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
