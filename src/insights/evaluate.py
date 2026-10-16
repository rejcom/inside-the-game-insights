"""Evaluate agent-written overlay copy with Microsoft Foundry evaluators.

Takes recorded replay bundles, turns every LLM-written cue (all viewer profiles
and languages) into an evaluation item whose grounding context is the verified
moment facts, and runs Foundry's built-in LLM-judge evaluators. The run, with
per-item scores, appears in the Foundry portal under Evaluations. The local
deterministic fact guard is reported next to it for comparison.

    python -m insights.evaluate data/replays/*.json --max-items 60
"""

from __future__ import annotations

import argparse
import asyncio
import glob
import json
from pathlib import Path
from typing import Any

from . import fallback
from .agents import AgentSettings, azure_credential

EVALUATORS = ["groundedness", "relevance", "coherence", "fluency"]


def _llm_cue(cue: dict) -> bool:
    return any(t["mode"] == "llm" for t in cue.get("provenance", {}).get("trace", []))


def collect(bundle_paths: list[str], max_items: int) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in bundle_paths:
        b = json.loads(Path(path).read_text(encoding="utf-8"))
        facts = {m["moment_id"]: m.get("facts", m.get("headline_facts", {})) for m in b["moments"]}
        evidence = b.get("evidence", {})
        profiles = {p["profile_id"]: p for p in b["profiles"]}
        for c in b["cues"]:
            if not _llm_cue(c):
                continue
            p = profiles[c["profile_id"]]
            ctx = {"moment": c["moment_kind"], "clock": c["clock"], "facts": facts.get(c["moment_id"], {}),
                   "stats_on_screen": {s["label"]: s["value"] for s in c.get("stats", [])},
                   "why_it_matters": c.get("why_it_matters"),
                   "tool_evidence": evidence.get(c["moment_id"], [])}
            rows.append({
                "match_id": b["match_id"], "moment_id": c["moment_id"], "profile_id": c["profile_id"],
                "language": c["language"],
                "query": (f"Write {p['mode'].replace('_', ' ')} broadcast overlay copy in language "
                          f"'{c['language']}' for this football moment ({c['moment_kind']} at {c['clock']})."),
                "response": f"{c['headline']}. {c['caption']}",
                "context": json.dumps(ctx, ensure_ascii=False),
            })
    # Spread the sample across matches, moments and profiles.
    rows.sort(key=lambda r: (hash(r["moment_id"]) % 97, r["profile_id"]))
    return rows[:max_items]


def local_fact_guard(rows: list[dict[str, Any]]) -> dict[str, Any]:
    failures = []
    for r in rows:
        allowed = fallback.allowed_numbers(json.loads(r["context"]))
        bad = fallback.unverified_numbers(r["response"], allowed)
        if bad:
            failures.append({"moment_id": r["moment_id"], "profile_id": r["profile_id"], "numbers": bad})
    return {"items": len(rows), "passed": len(rows) - len(failures), "failures": failures[:10]}


async def run_foundry(rows: list[dict[str, Any]], name: str) -> dict[str, Any]:
    from agent_framework import EvalItem, Message
    from agent_framework.foundry import FoundryChatClient, FoundryEvals

    settings = AgentSettings.from_env()
    client = FoundryChatClient(project_endpoint=settings.endpoint, model=settings.model, credential=azure_credential())
    items = [EvalItem(conversation=[Message(role="user", contents=[r["query"]]),
                                    Message(role="assistant", contents=[r["response"]])],
                      context=r["context"]) for r in rows]
    evals = FoundryEvals(client=client, evaluators=EVALUATORS, timeout=900, poll_interval=10)
    res = await evals.evaluate(items, eval_name=name)
    return {"status": res.status, "result_counts": _plain(res.result_counts),
            "per_evaluator": _plain(res.per_evaluator), "report_url": res.report_url,
            "error": _plain(res.error)}


def _plain(obj: Any) -> Any:
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, dict):
        return {k: _plain(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_plain(v) for v in obj]
    for attr in ("model_dump", "to_dict", "as_dict"):
        if hasattr(obj, attr):
            return _plain(getattr(obj, attr)())
    return {k: _plain(v) for k, v in vars(obj).items() if not k.startswith("_")} if hasattr(obj, "__dict__") else str(obj)


def main() -> None:
    parser = argparse.ArgumentParser(description="Foundry evaluation of agent overlay copy")
    parser.add_argument("bundles", nargs="+", help="replay bundle JSON files (globs allowed)")
    parser.add_argument("--max-items", type=int, default=60)
    parser.add_argument("--name", default="inside-the-game overlay copy")
    parser.add_argument("--out", default="data/evals/latest.json")
    parser.add_argument("--local-only", action="store_true")
    args = parser.parse_args()
    paths = [p for pattern in args.bundles for p in glob.glob(pattern)]
    rows = collect(paths, args.max_items)
    report: dict[str, Any] = {"bundles": [Path(p).name for p in paths], "items": len(rows),
                              "evaluators": EVALUATORS, "local_fact_guard": local_fact_guard(rows)}
    if not args.local_only:
        report["foundry"] = asyncio.run(run_foundry(rows, args.name))
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k != "bundles"}, indent=2, ensure_ascii=False)[:3000])


if __name__ == "__main__":
    main()
