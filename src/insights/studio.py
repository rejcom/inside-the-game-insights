"""Studio Q&A agent: viewers ask questions, a Foundry agent answers through the MCP server.

The agent's only tools come from the match-intelligence MCP server (remote via
streamable HTTP when MCP_URL is set, otherwise a local stdio subprocess). Two
deterministic guardrails run after the model:

* spoiler guard - every tool call must evaluate at or before the viewer's minute;
  a call without `minute` or with a later one triggers one corrected retry;
* fact guard - every number in the answer must appear in a tool result.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from typing import Any

from pydantic import BaseModel, Field

from . import fallback
from .agents import AgentSettings, GROUNDING_RULE, azure_credential
from .models import StatLine

INSTRUCTIONS = (
    "You are the studio analyst answering a viewer's question during a live football broadcast. "
    "Use the match-intelligence tools to look things up; always pass the match_id and the viewer's current "
    "minute to every tool so you never reveal what happens later. Answer in the viewer's language in 2-4 "
    "short sentences, conversational but precise, and pick up to 3 key numbers for an on-screen card. "
    "If the tools cannot answer, say so briefly. " + GROUNDING_RULE
)


class StudioAnswer(BaseModel):
    answer: str = Field(description="2-4 sentence answer in the viewer's language")
    key_stats: list[StatLine] = Field(default_factory=list, max_length=3)


def _mcp_tool() -> Any:
    from agent_framework import MCPStdioTool, MCPStreamableHTTPTool

    url = os.getenv("MCP_URL")
    if url:
        return MCPStreamableHTTPTool(name="match_intel", url=url, approval_mode="never_require")
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [os.path.abspath("src"), env.get("PYTHONPATH")]))
    return MCPStdioTool(name="match_intel", command=sys.executable, args=["-m", "insights.mcp_server"], env=env,
                        approval_mode="never_require")


def _tool_activity(response: Any) -> tuple[list[dict], list[Any]]:
    calls, results = [], []
    for msg in getattr(response, "messages", []) or []:
        for c in getattr(msg, "contents", []) or []:
            d = c.to_dict() if hasattr(c, "to_dict") else {}
            if d.get("type") == "function_call":
                args = d.get("arguments")
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except ValueError:
                        args = {"raw": args}
                calls.append({"tool": d.get("name"), "arguments": args or {}})
            elif d.get("type") == "function_result":
                results.append(d.get("result"))
    return calls, results


def spoiler_violations(calls: list[dict], minute: int) -> list[str]:
    bad = []
    for c in calls:
        if c["tool"] in ("list_matches", "list_players", "get_broadcast_copy"):
            continue
        m = c["arguments"].get("minute")
        if m is None or int(m) > minute:
            bad.append(f"{c['tool']}(minute={m})")
    return bad


async def ask(match_id: str, minute: int, question: str, language: str = "en") -> dict[str, Any]:
    from agent_framework import Agent
    from agent_framework.foundry import FoundryChatClient

    settings = AgentSettings.from_env()
    if not settings.enabled:
        return {"answer": "The studio analyst needs Microsoft Foundry; it is not configured here.", "key_stats": [],
                "available": False}
    t0 = time.perf_counter()
    client = FoundryChatClient(project_endpoint=settings.endpoint, model=settings.model, credential=azure_credential())
    prompt = json.dumps({"match_id": match_id, "viewer_current_minute": minute, "viewer_language": language,
                         "question": question}, ensure_ascii=False)
    attempts: list[dict] = []
    async with _mcp_tool() as tool:
        agent = Agent(client=client, name="studio_agent", instructions=INSTRUCTIONS, tools=tool)
        for attempt in range(2):
            response = await asyncio.wait_for(agent.run(prompt, options={"response_format": StudioAnswer}),
                                              timeout=settings.timeout_s * 2)
            calls, results = _tool_activity(response)
            answer = response.value if isinstance(response.value, StudioAnswer) else \
                StudioAnswer.model_validate_json(response.text)
            spoilers = spoiler_violations(calls, minute)
            attempts.append({"calls": calls, "spoiler_violations": spoilers})
            if not spoilers:
                break
            prompt = json.dumps({"match_id": match_id, "viewer_current_minute": minute, "viewer_language": language,
                                 "question": question,
                                 "guard_feedback": f"Rejected: these calls looked past minute {minute} or omitted it: "
                                                   f"{spoilers}. Call every tool with minute <= {minute}."},
                                ensure_ascii=False)
    allowed = fallback.allowed_numbers(results, {"minute": minute})
    text = answer.answer + " " + " ".join(s.value for s in answer.key_stats)
    unverified = fallback.unverified_numbers(text, allowed)
    spoiler_safe = not attempts[-1]["spoiler_violations"]
    if not spoiler_safe:
        answer = StudioAnswer(answer="I can't answer that without looking ahead in the match. Ask me again later!")
    return {
        "answer": answer.answer, "key_stats": [s.model_dump() for s in answer.key_stats],
        "tools_used": [c["tool"] for c in attempts[-1]["calls"]], "attempts": len(attempts),
        "spoiler_safe": spoiler_safe, "facts_verified": not unverified, "unverified_numbers": unverified,
        "latency_ms": int((time.perf_counter() - t0) * 1000), "model": settings.model,
        "mcp": "remote" if os.getenv("MCP_URL") else "stdio", "available": True,
    }


def main() -> None:
    import argparse

    p = argparse.ArgumentParser(description="Ask the studio analyst (Foundry agent + MCP tools)")
    p.add_argument("match_id")
    p.add_argument("minute", type=int)
    p.add_argument("question")
    p.add_argument("--language", default="en")
    a = p.parse_args()
    print(json.dumps(asyncio.run(ask(a.match_id, a.minute, a.question, a.language)), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
