"""Multi-agent orchestration with Microsoft Agent Framework workflows.

    MomentJob
        │
     intake ──fan-out──► analyst ──┐            (parallel specialists)
        │                          ├─fan-in──► composer
        └──────────────► tactician ┘               │
                                                narrator ◄──────────┐
                                                   │                │ rewrite with feedback
                                               fact_guard ──────────┘ (max 2 attempts)
                                                   │ verified (or deterministic fallback)
                                                persona          (one variant per viewer profile)
                                                   │
                                                producer ──► list[OverlayCue]

Every specialist runs its LLM agent under a timeout and falls back to its
deterministic twin on any failure; the trace records which path was taken.
"""

from __future__ import annotations

import json
import time
from typing import Any, Awaitable, Callable

from agent_framework import Executor, WorkflowBuilder, WorkflowContext, handler
from typing_extensions import Never

from . import fallback
from .agents import AgentTeam, build_tools, moment_prompt
from .models import (Contribution, Explanation, Interpretation, MomentJob, Narrative, OverlayCue, PersonaBatch,
                     PersonaVariant, Recap, TraceStep)
from .overlays import Producer, is_relevant
from .state import MatchState

MAX_NARRATIVE_ATTEMPTS = 2


async def _attempt(job: MomentJob, role: str, team: AgentTeam, llm: Callable[[], Awaitable[Any]],
                   twin: Callable[[], Any], expected: type | None = None) -> Any:
    """Run the LLM path if available, otherwise (or on failure) the deterministic twin."""
    t0 = time.perf_counter()
    if team.enabled:
        try:
            result = await llm()
            if expected is not None and not isinstance(result, expected):
                raise TypeError(f"expected {expected.__name__}, got {type(result).__name__}")
            job.trace.append(TraceStep(role, "llm", int((time.perf_counter() - t0) * 1000)))
            return result
        except Exception as exc:  # noqa: BLE001 - any model failure must not stop the broadcast
            note = f"{type(exc).__name__}: {str(exc)[:120]}"
            result = twin()
            job.trace.append(TraceStep(role, "fallback", int((time.perf_counter() - t0) * 1000), note))
            return result
    result = twin()
    job.trace.append(TraceStep(role, "deterministic", int((time.perf_counter() - t0) * 1000), "offline twin"))
    return result


class Intake(Executor):
    """Entry point: the moment arrives with a frozen snapshot of match state."""

    @handler
    async def accept(self, job: MomentJob, ctx: WorkflowContext[MomentJob]) -> None:
        await ctx.send_message(job)


class Analyst(Executor):
    def __init__(self, team: AgentTeam, state: MatchState):
        super().__init__(id="analyst")
        self.team, self.state = team, state

    @handler
    async def run(self, job: MomentJob, ctx: WorkflowContext[Contribution]) -> None:
        job.interpretation = await _attempt(
            job, "analyst", self.team,
            lambda: self.team.run("analyst", moment_prompt(job), Interpretation, build_tools(self.state, job)),
            lambda: fallback.interpret(job.moment), Interpretation)
        await ctx.send_message(Contribution("analyst", job))


class Tactician(Executor):
    def __init__(self, team: AgentTeam, state: MatchState):
        super().__init__(id="tactician")
        self.team, self.state = team, state

    @handler
    async def run(self, job: MomentJob, ctx: WorkflowContext[Contribution]) -> None:
        job.explanation = await _attempt(
            job, "tactician", self.team,
            lambda: self.team.run("tactician", moment_prompt(job), Explanation, build_tools(self.state, job)),
            lambda: fallback.explain(job.moment), Explanation)
        await ctx.send_message(Contribution("tactician", job))


class Composer(Executor):
    """Fan-in: waits for both specialists, then hands the enriched job to the narrator."""

    @handler
    async def gather(self, parts: list[Contribution], ctx: WorkflowContext[MomentJob]) -> None:
        await ctx.send_message(parts[0].job)


class Narrator(Executor):
    def __init__(self, team: AgentTeam):
        super().__init__(id="narrator")
        self.team = team

    @handler
    async def run(self, job: MomentJob, ctx: WorkflowContext[MomentJob]) -> None:
        job.narrative_attempts += 1
        extra: dict[str, Any] = {
            "analysis": job.interpretation.model_dump() if job.interpretation else None,
            "explanation": job.explanation.model_dump() if job.explanation else None,
        }
        if job.verification_issues:
            extra["fact_check_feedback"] = (f"These numbers are not supported by the facts, remove them: "
                                            f"{', '.join(job.verification_issues)}")
        if job.narrative_attempts > MAX_NARRATIVE_ATTEMPTS:
            job.narrative = fallback.narrate(job.moment, job.explanation)
            job.trace.append(TraceStep("narrator", "fallback", 0, "max rewrites reached"))
        else:
            job.narrative = await _attempt(
                job, "narrator", self.team,
                lambda: self.team.run("narrator", moment_prompt(job, extra), Narrative),
                lambda: fallback.narrate(job.moment, job.explanation), Narrative)
        if job.moment.kind in ("half_time", "full_time") and job.recap is None:
            job.recap = await _attempt(
                job, "recap", self.team,
                lambda: self.team.run("recap", moment_prompt(job, extra), Recap),
                lambda: fallback.recap(job.moment), Recap)
        await ctx.send_message(job)


class FactGuard(Executor):
    """Deterministic guardrail: every number on screen must trace back to facts or tool evidence."""

    def __init__(self):
        super().__init__(id="fact_guard")

    @handler
    async def check(self, job: MomentJob, ctx: WorkflowContext[MomentJob]) -> None:
        t0 = time.perf_counter()
        allowed = fallback.allowed_numbers(job.moment.facts, job.evidence,
                                           job.interpretation.model_dump() if job.interpretation else None)
        text = " ".join([job.narrative.headline, job.narrative.caption, job.narrative.commentary])
        if job.recap:
            text += " " + job.recap.summary + " " + " ".join(job.recap.key_moments)
        issues = fallback.unverified_numbers(text, allowed)
        job.verification_issues = issues
        job.needs_rewrite = bool(issues) and job.narrative_attempts <= MAX_NARRATIVE_ATTEMPTS
        if issues and not job.needs_rewrite:
            # Out of retries: ship the deterministic copy, which is grounded by construction.
            job.narrative = fallback.narrate(job.moment, job.explanation)
            job.recap = fallback.recap(job.moment) if job.recap else None
            job.verification_issues = []
        note = f"unverified: {issues}" if issues else "all numbers grounded"
        job.trace.append(TraceStep("fact_guard", "deterministic", int((time.perf_counter() - t0) * 1000), note))
        await ctx.send_message(job)


class Persona(Executor):
    def __init__(self, team: AgentTeam, state: MatchState):
        super().__init__(id="persona")
        self.team, self.state = team, state

    @handler
    async def run(self, job: MomentJob, ctx: WorkflowContext[MomentJob]) -> None:
        targets = [p for p in job.profiles if is_relevant(job.moment, p)]

        def twin() -> list[PersonaVariant]:
            return [fallback.personalize(job.moment, p, job.interpretation, job.explanation, job.narrative)
                    for p in targets]

        async def llm() -> list[PersonaVariant]:
            extra = {"approved_copy": job.narrative.model_dump(),
                     "stats": [s.model_dump() for s in job.interpretation.key_stats] if job.interpretation else [],
                     "why_it_matters": job.explanation.why_it_matters if job.explanation else None,
                     "profiles": [p.model_dump() | {
                         "favorite_team": self.state.team_name(p.favorite_team_id),
                         "favorite_player": self.state.player_name(p.favorite_player_id, short=False)}
                         for p in targets]}
            batch = await self.team.run("persona", moment_prompt(job, extra), PersonaBatch,
                                        build_tools(self.state, job))
            return batch.variants

        variants = await _attempt(job, "persona", self.team, llm, twin, list) if targets else []
        # Per-variant fact check: a hallucinated number replaces only that viewer's copy.
        allowed = fallback.allowed_numbers(job.moment.facts, job.evidence,
                                           job.interpretation.model_dump() if job.interpretation else None)
        by_id = {p.profile_id: p for p in targets}
        checked = []
        for v in variants:
            text = " ".join([v.headline, v.caption] + [s.value for s in v.stats])
            if v.profile_id in by_id and fallback.unverified_numbers(text, allowed):
                v = fallback.personalize(job.moment, by_id[v.profile_id], job.interpretation, job.explanation,
                                         job.narrative)
                job.trace.append(TraceStep("persona", "fallback", 0, f"variant {v.profile_id} failed fact check"))
            checked.append(v)
        job.variants = checked
        await ctx.send_message(job)


class ProducerExecutor(Executor):
    def __init__(self, producer: Producer, latency_match_s: Callable[[MomentJob], float]):
        super().__init__(id="producer")
        self.producer, self.latency = producer, latency_match_s

    @handler
    async def publish(self, job: MomentJob, ctx: WorkflowContext[Never, list[OverlayCue]]) -> None:
        await ctx.yield_output(self.producer.produce(job, self.latency(job)))


def build_workflow(team: AgentTeam, state: MatchState, producer: Producer,
                   latency_match_s: Callable[[MomentJob], float] = lambda job: 1.5):
    intake = Intake(id="intake")
    analyst, tactician = Analyst(team, state), Tactician(team, state)
    composer = Composer(id="composer")
    narrator, guard = Narrator(team), FactGuard()
    persona = Persona(team, state)
    publish = ProducerExecutor(producer, latency_match_s)
    return (
        WorkflowBuilder(name="moment_to_overlay", start_executor=intake, max_iterations=30)
        .add_fan_out_edges(intake, [analyst, tactician])
        .add_fan_in_edges([analyst, tactician], composer)
        .add_edge(composer, narrator)
        .add_edge(narrator, guard)
        .add_edge(guard, narrator, condition=lambda job: job.needs_rewrite)
        .add_edge(guard, persona, condition=lambda job: not job.needs_rewrite)
        .add_edge(persona, publish)
        .build()
    )


def describe_workflow() -> str:
    return json.dumps({"stages": ["intake", "analyst||tactician", "composer", "narrator", "fact_guard",
                                  "persona", "producer"]})
