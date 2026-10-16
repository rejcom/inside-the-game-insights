"""Specialist agents built with Microsoft Agent Framework on Microsoft Foundry.

| Agent       | Pipeline stage | Responsibility                                          |
|-------------|----------------|---------------------------------------------------------|
| Analyst     | Interpret      | pick the stats that explain the moment (via tools)      |
| Tactician   | Explain        | say WHY it matters: control vs chaos, pressing, rhythm  |
| Narrator    | Render         | broadcast headline, caption and commentary line         |
| Persona     | Personalize    | analyst / casual / club fan / player focus, any language|
| Recap       | Render         | half-time and full-time recaps                          |

Agents never compute numbers: tools expose the deterministic `MatchState`, and
every tool result is logged into the job's evidence so the fact guard can check
that each number on screen is grounded.

Configuration (environment or `.env`): FOUNDRY_PROJECT_ENDPOINT, FOUNDRY_MODEL.
Without them the pipeline runs on the deterministic twins in `fallback.py`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from typing import Annotated, Any, TypeVar

from pydantic import BaseModel

from .models import MomentJob
from .state import MatchState

log = logging.getLogger(__name__)
M = TypeVar("M", bound=BaseModel)

GROUNDING_RULE = (
    "Grounding rule: every number you write must be copied from the moment facts, the analysis you were given, "
    "or a tool result. Never estimate, round differently, or invent statistics, records or history. "
    "All clubs and players are fictional; do not mention real clubs, players or competitions."
)

INSTRUCTIONS = {
    "analyst": (
        "You are the Stats Analyst of a live football broadcast insights team. You receive a detected key "
        "moment with verified facts. Use the tools to look at match and player context, then choose the 2-4 "
        "statistics that best explain the moment for an on-screen stat card, plus one sentence describing the "
        "statistical pattern behind it. Prefer stats that add context (trend, comparison, rarity) over restating "
        "the obvious. " + GROUNDING_RULE
    ),
    "tactician": (
        "You are the Tactical Analyst. Explain WHY the moment matters, not what happened. Use the rolling window "
        "metrics: momentum (-100..100, positive = home), chaos index (0 controlled .. 100 chaotic), possession "
        "changes per minute, pressing (high pressures, high ball wins, PPDA) and rhythm (passes per possession, "
        "tempo). Name the single main driver and back it with 1-3 short evidence points. Be concrete and "
        "broadcast-friendly, max 2 sentences. " + GROUNDING_RULE
    ),
    "narrator": (
        "You are the lead broadcast writer. Turn the moment, the analyst's stats and the tactician's explanation "
        "into English on-screen copy: a punchy headline (max 60 characters), a lower-third caption (max 160 "
        "characters) that says why it matters, and one spoken commentary line (max 280 characters). Use player "
        "and club names exactly as given. If you receive fact-check feedback, rewrite without the flagged "
        "numbers. " + GROUNDING_RULE
    ),
    "persona": (
        "You are the Personalization Producer. Adapt the approved copy into one variant per viewer profile. "
        "analyst: dense, technical, up to 4 stats. casual: simple and emotional, explain jargon, at most 1 stat. "
        "club_fan: speak from the supporter's side of their favourite club (partisan but respectful). "
        "player_focus: centre the story on the favourite player; use the player stats tool for context. "
        "Write headline and caption in the profile's language (BCP-47 code) with natural, idiomatic phrasing; "
        "keep numbers identical to the source. Respect the length limits. " + GROUNDING_RULE
    ),
    "recap": (
        "You are the Studio Recap Writer. Write a half-time or full-time recap that explains how the match is "
        "being decided: the key moments with minute marks, the control vs chaos story, and the standout player. "
        + GROUNDING_RULE
    ),
}


@dataclass
class AgentSettings:
    endpoint: str | None
    model: str | None
    timeout_s: float = 20.0

    @property
    def enabled(self) -> bool:
        return bool(self.endpoint and self.model)

    @classmethod
    def from_env(cls) -> "AgentSettings":
        _load_dotenv()
        return cls(endpoint=os.getenv("FOUNDRY_PROJECT_ENDPOINT"), model=os.getenv("FOUNDRY_MODEL"),
                   timeout_s=float(os.getenv("INSIGHTS_AGENT_TIMEOUT_S", "20")))


def _load_dotenv(path: str = ".env") -> None:
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"'))


AUTH_RECORD_PATH = os.path.join(os.path.expanduser("~"), ".insidethegame", "auth_record.json")


def azure_credential() -> Any:
    """Keyless Entra ID auth.

    FOUNDRY_AUTH=default (default): DefaultAzureCredential (az login, managed identity, VS Code ...).
    FOUNDRY_AUTH=browser: interactive browser sign-in once, then silent via a persisted token cache;
    handy on dev machines without the Azure CLI.
    """
    from azure.identity import (AuthenticationRecord, DefaultAzureCredential, InteractiveBrowserCredential,
                                TokenCachePersistenceOptions)

    if os.getenv("FOUNDRY_AUTH", "default").lower() != "browser":
        return DefaultAzureCredential()
    record = None
    if os.path.exists(AUTH_RECORD_PATH):
        with open(AUTH_RECORD_PATH, encoding="utf-8") as f:
            record = AuthenticationRecord.deserialize(f.read())
    credential = InteractiveBrowserCredential(
        tenant_id=os.getenv("AZURE_TENANT_ID"), authentication_record=record,
        cache_persistence_options=TokenCachePersistenceOptions(name="insidethegame"))
    if record is None:
        record = credential.authenticate(scopes=["https://ai.azure.com/.default"])
        os.makedirs(os.path.dirname(AUTH_RECORD_PATH), exist_ok=True)
        with open(AUTH_RECORD_PATH, "w", encoding="utf-8") as f:
            f.write(record.serialize())
    return credential


def build_tools(state: MatchState, job: MomentJob) -> list[Any]:
    """Tools bound to the live match state; every result is recorded as evidence for the fact guard."""
    from agent_framework import tool

    def record(result: Any) -> str:
        job.evidence.append(result)
        return json.dumps(result, ensure_ascii=False)

    @tool(name="get_match_snapshot", approval_mode="never_require",
          description="Current score, team totals, last-5-minute window metrics, timeline and top performers.")
    def get_match_snapshot() -> str:
        return record(state.snapshot())

    @tool(name="get_window_metrics", approval_mode="never_require",
          description="Rolling metrics for the last N minutes: momentum, chaos index, pressing, rhythm per team.")
    def get_window_metrics(minutes: Annotated[int, "Window length in minutes (2-15)"] = 5) -> str:
        return record(state.window_metrics(max(2, min(15, minutes)) * 60))

    @tool(name="get_player_stats", approval_mode="never_require",
          description="Live stats for one player by name or player_id: goals, xG, passes, difficulty, speed, distance.")
    def get_player_stats(player: Annotated[str, "Player name or player_id"]) -> str:
        return record(state.find_player(player) or {"error": f"player '{player}' not found"})

    @tool(name="get_team_totals", approval_mode="never_require",
          description="Full-match totals for a team by name: xG, shots, possession, passes, pressing, cards.")
    def get_team_totals(team: Annotated[str, "Club name"]) -> str:
        for tid, t in state.teams.items():
            if team.lower() in t["name"].lower() or team.lower() == tid:
                return record(state.team_totals(tid))
        return record({"error": f"team '{team}' not found"})

    return [get_match_snapshot, get_window_metrics, get_player_stats, get_team_totals]


class AgentTeam:
    """Lazily creates one Agent Framework `Agent` per role on a shared Foundry chat client."""

    def __init__(self, settings: AgentSettings):
        self.settings = settings
        self._agents: dict[str, Any] = {}
        self._client = None
        if settings.enabled:
            from agent_framework.foundry import FoundryChatClient

            self._client = FoundryChatClient(project_endpoint=settings.endpoint, model=settings.model,
                                             credential=azure_credential())

    @property
    def enabled(self) -> bool:
        return self._client is not None

    def agent(self, role: str) -> Any:
        if role not in self._agents:
            from agent_framework import Agent

            self._agents[role] = Agent(client=self._client, name=f"{role}_agent", instructions=INSTRUCTIONS[role])
        return self._agents[role]

    @staticmethod
    def _options(output: type[BaseModel]) -> dict[str, Any]:
        options: dict[str, Any] = {"response_format": output}
        # Reasoning models (gpt-5 family) reject sampling parameters; only send temperature when configured.
        if os.getenv("INSIGHTS_TEMPERATURE"):
            options["temperature"] = float(os.environ["INSIGHTS_TEMPERATURE"])
        return options

    async def run(self, role: str, prompt: str, output: type[M], tools: list[Any] | None = None) -> M:
        """Run one agent with structured output; raises on timeout, error or invalid output."""
        if not self.enabled:
            raise RuntimeError("Foundry not configured")
        agent = self.agent(role)
        response = await asyncio.wait_for(
            agent.run(prompt, tools=tools or None, options=self._options(output)),
            timeout=self.settings.timeout_s,
        )
        value = getattr(response, "value", None)
        if isinstance(value, output):
            return value
        return output.model_validate_json(response.text)


def moment_prompt(job: MomentJob, extra: dict[str, Any] | None = None) -> str:
    m = job.moment
    payload = {
        "moment": {"kind": m.kind, "clock": m.clock, "importance": m.importance, "facts": m.facts},
        "match": {"score": job.snapshot["score"], "clock": job.snapshot["clock"],
                  "home": job.snapshot["home"]["team"], "away": job.snapshot["away"]["team"]},
    }
    if extra:
        payload |= extra
    return json.dumps(payload, ensure_ascii=False, default=str)
