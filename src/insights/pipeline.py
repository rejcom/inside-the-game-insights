"""Live insights pipeline: ingest → interpret → explain → render → personalize.

Events are ingested in order and never wait for the AI. Detected moments are
routed by importance:

* fast lane: low-importance moments (player tags, yellow cards, subs) become
  template overlays immediately via the deterministic twins;
* agent lane: high-importance moments run the multi-agent workflow concurrently
  (bounded by a semaphore) so live ingestion keeps up with play.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, AsyncIterator, Callable, Iterable

from . import fallback
from .agents import AgentSettings, AgentTeam
from .models import MomentJob, OverlayCue, TraceStep, ViewerProfile
from .moments import Moment, MomentDetector
from .overlays import Producer, default_profiles, is_relevant
from .state import MatchState
from .telemetry import span
from .workflow import build_workflow

CueSink = Callable[[dict[str, Any]], None]


class InsightsPipeline:
    def __init__(self, metadata: dict, profiles: list[ViewerProfile] | None = None,
                 settings: AgentSettings | None = None, cue_sink: CueSink | None = None,
                 moment_sink: CueSink | None = None, ai_concurrency: int = 4, speed: float = 0.0,
                 team: AgentTeam | None = None, event_sink: CueSink | None = None):
        self.state = MatchState(metadata)
        self.detector = MomentDetector(self.state)
        self.profiles = profiles or default_profiles(self.state)
        self.detector.focus_players = {p.favorite_player_id for p in self.profiles
                                       if p.mode == "player_focus" and p.favorite_player_id}
        self.team = team or AgentTeam(settings or AgentSettings.from_env())
        self.producer = Producer(self.state, self.profiles)
        self.cue_sink = cue_sink or (lambda c: None)
        self.moment_sink = moment_sink or (lambda m: None)
        self.event_sink = event_sink or (lambda e: None)
        self.speed = speed
        self._sem = asyncio.Semaphore(ai_concurrency)
        self._tasks: set[asyncio.Task] = set()
        self.cues: list[OverlayCue] = []
        self.evidence: dict[str, list] = {}  # moment_id -> tool outputs the agents grounded their copy on
        self.stats = {"events": 0, "moments": 0, "agent_lane": 0, "fast_lane": 0, "cues": 0, "fallbacks": 0}

    @property
    def mode(self) -> str:
        return "foundry" if self.team.enabled else "offline"

    # ------------------------------------------------------------------ ingest
    async def ingest(self, event: dict) -> None:
        self.stats["events"] += 1
        self.state.update(event)
        self.event_sink(event)
        for moment in self.detector.process(event):
            self.stats["moments"] += 1
            self.moment_sink(moment.to_dict())
            if moment.needs_ai:
                self.stats["agent_lane"] += 1
                # Freeze the match picture at the moment itself; agents may run later than the event.
                snapshot = self.state.snapshot()
                task = asyncio.create_task(self._agent_lane(moment, snapshot, time.perf_counter()))
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)
            else:
                self.stats["fast_lane"] += 1
                self._fast_lane(moment)

    def _emit(self, cues: list[OverlayCue], job: MomentJob) -> None:
        self.stats["fallbacks"] += sum(t.mode == "fallback" for t in job.trace)
        for cue in cues:
            self.cues.append(cue)
            self.stats["cues"] += 1
            self.cue_sink(cue.model_dump())

    def _fast_lane(self, moment: Moment) -> None:
        job = MomentJob(moment=moment, profiles=self.profiles, snapshot={})
        t0 = time.perf_counter()
        job.interpretation = fallback.interpret(moment)
        job.explanation = fallback.explain(moment)
        job.narrative = fallback.narrate(moment, job.explanation)
        job.variants = [fallback.personalize(moment, p, job.interpretation, job.explanation, job.narrative)
                        for p in self.profiles if is_relevant(moment, p)]
        job.trace.append(TraceStep("fast_lane", "deterministic", int((time.perf_counter() - t0) * 1000),
                                   "template overlay"))
        self._emit(self.producer.produce(job, latency_match_s=0.5), job)

    async def _agent_lane(self, moment: Moment, snapshot: dict, started: float) -> None:
        async with self._sem:
            job = MomentJob(moment=moment, profiles=self.profiles, snapshot=snapshot)

            def latency(_: MomentJob) -> float:
                # Wall-clock processing time expressed in match seconds at the current replay speed.
                wall = time.perf_counter() - started
                return max(1.5, wall * self.speed) if self.speed > 0 else 1.5

            workflow = build_workflow(self.team, self.state, self.producer, latency)
            with span(f"moment {moment.kind}", **{
                    "insights.match_id": self.state.match_id, "insights.moment_id": moment.moment_id,
                    "insights.moment_kind": moment.kind, "insights.importance": moment.importance,
                    "insights.clock": moment.clock, "insights.engine": self.mode}) as s:
                result = await workflow.run(job)
                outputs = result.get_outputs()
                cues = outputs[0] if outputs else []
                if s is not None:
                    s.set_attribute("insights.cues", len(cues))
                    s.set_attribute("insights.fallbacks", sum(t.mode == "fallback" for t in job.trace))
                    s.set_attribute("insights.verified", not job.verification_issues)
                    s.set_attribute("insights.narrator_attempts", job.narrative_attempts)
            self.evidence[moment.moment_id] = job.evidence
            self._emit(cues, job)

    async def drain(self) -> None:
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=False)

    # ------------------------------------------------------------------ drivers
    async def run_events(self, events: Iterable[dict] | AsyncIterator[dict]) -> dict[str, Any]:
        t0 = time.perf_counter()
        if hasattr(events, "__aiter__"):
            async for e in events:  # type: ignore[union-attr]
                await self.ingest(e)
        else:
            prev_t = None
            for e in events:
                if self.speed > 0 and prev_t is not None:
                    await asyncio.sleep(max(0.0, (e["match_seconds"] - prev_t) / self.speed))
                prev_t = e.get("match_seconds", prev_t)
                await self.ingest(e)
                if self.speed <= 0 and self.stats["events"] % 200 == 0:
                    await asyncio.sleep(0)  # let agent tasks progress during fast replays
        await self.drain()
        self.stats["mode"] = self.mode
        self.stats["seconds"] = round(time.perf_counter() - t0, 2)
        return self.stats
