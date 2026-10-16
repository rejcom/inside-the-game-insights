"""Event-driven insights worker: Event Hubs in, Event Hubs out.

    match-events (partition key = match_id)
        └─► this worker: one InsightsPipeline per match (agents on Foundry)
              └─► match-broadcast: hello | event | moment | cue | done

One agent run per match serves every viewer; the web player only fans the
broadcast out over SSE. Checkpoints live in Blob Storage, which also lets KEDA
scale the worker from zero when unprocessed events appear.

Environment: EVENTHUB_NAMESPACE (fully qualified), EVENTHUB_IN (match-events),
EVENTHUB_OUT (match-broadcast), CHECKPOINT_ACCOUNT_URL, CHECKPOINT_CONTAINER.
Auth: DefaultAzureCredential (managed identity in Azure).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Any

from .agents import AgentSettings, _load_dotenv
from .pipeline import InsightsPipeline
from .telemetry import setup_telemetry

log = logging.getLogger("insights.worker")
STALE_AFTER_S = float(os.getenv("STALE_AFTER_S", "600"))


def slim_event(e: dict) -> dict:
    if e.get("type") == "physical_update":
        return {**e, "metrics": {"team_distance_m": e.get("metrics", {}).get("team_distance_m")}}
    return e


class BroadcastWorker:
    def __init__(self, producer: Any, live_max: int = 2):
        self.producer = producer
        self.matches: dict[str, InsightsPipeline] = {}
        self.last_seen: dict[str, float] = {}
        self.outbox: list[tuple[str, dict]] = []
        self.live_max = live_max

    def _push(self, match_id: str, kind: str, data: dict) -> None:
        self.outbox.append((match_id, {"kind": kind, "match_id": match_id, "data": data}))

    def start_match(self, msg: dict) -> None:
        match_id, metadata = msg["match_id"], msg["metadata"]
        settings = AgentSettings.from_env()
        if len(self.matches) >= self.live_max:
            settings.endpoint = None  # protect the model quota: extra matches use deterministic twins
        pipe = InsightsPipeline(
            metadata, settings=settings, speed=float(msg.get("feed_speed", 1.0)),
            event_sink=lambda e: self._push(match_id, "event", slim_event(e)),
            cue_sink=lambda c: self._push(match_id, "cue", c),
            moment_sink=lambda m: self._push(match_id, "moment", {k: v for k, v in m.items() if k != "facts"}),
        )
        self.matches[match_id] = pipe
        self._push(match_id, "hello", {"metadata": metadata, "profiles": [p.model_dump() for p in pipe.profiles],
                                       "engine": pipe.mode, "model": settings.model if pipe.mode == "foundry" else None,
                                       "speed": msg.get("feed_speed", 1.0), "source": "event_hubs"})
        log.info("match %s on air (engine=%s)", match_id, pipe.mode)

    async def handle(self, body: dict) -> None:
        match_id = body.get("match_id")
        if time.time() - float(body.get("ingested_at", time.time())) > STALE_AFTER_S:
            return  # backlog from an old match (e.g. after a restart): do not spend model calls on it
        if body.get("type") == "match_metadata":
            self.start_match(body)
            return
        pipe = self.matches.get(match_id)
        self.last_seen[match_id] = time.time()
        if pipe is None:
            return  # joined mid-match without metadata: wait for the next kickoff
        await pipe.ingest(body)
        if body.get("type") == "match_end":
            asyncio.create_task(self._finish(match_id, pipe))

    async def _finish(self, match_id: str, pipe: InsightsPipeline) -> None:
        await pipe.drain()
        self._push(match_id, "done", pipe.stats | {"mode": pipe.mode})
        self.matches.pop(match_id, None)

    async def flush(self) -> None:
        if not self.outbox:
            return
        pending, self.outbox = self.outbox, []
        from azure.eventhub import EventData

        by_match: dict[str, list[dict]] = {}
        for match_id, msg in pending:
            by_match.setdefault(match_id, []).append(msg)
        for match_id, msgs in by_match.items():
            batch = await self.producer.create_batch(partition_key=match_id)
            for msg in msgs:
                data = EventData(json.dumps(msg, ensure_ascii=False, default=str))
                try:
                    batch.add(data)
                except ValueError:
                    await self.producer.send_batch(batch)
                    batch = await self.producer.create_batch(partition_key=match_id)
                    batch.add(data)
            await self.producer.send_batch(batch)

    def evict_idle(self) -> None:
        """Forget matches whose feed went silent (e.g. the provider crashed before match_end)."""
        for match_id in [m for m in self.matches if time.time() - self.last_seen.get(m, time.time()) > STALE_AFTER_S]:
            self.matches.pop(match_id, None)
            self._push(match_id, "done", {"mode": "abandoned", "reason": "feed went silent"})
            log.warning("match %s abandoned: no events for %ss", match_id, STALE_AFTER_S)

    async def flusher(self) -> None:
        while True:
            self.evict_idle()
            try:
                await self.flush()
            except Exception:  # noqa: BLE001
                log.exception("broadcast flush failed")
            await asyncio.sleep(0.3)


async def run() -> None:
    from azure.eventhub.aio import EventHubConsumerClient, EventHubProducerClient
    from azure.eventhub.extensions.checkpointstoreblobaio import BlobCheckpointStore
    from azure.identity.aio import DefaultAzureCredential

    _load_dotenv()
    setup_telemetry("insights-worker")
    ns = os.environ["EVENTHUB_NAMESPACE"]
    credential = DefaultAzureCredential()
    store = BlobCheckpointStore(blob_account_url=os.environ["CHECKPOINT_ACCOUNT_URL"],
                                container_name=os.getenv("CHECKPOINT_CONTAINER", "checkpoints"), credential=credential)
    consumer = EventHubConsumerClient(fully_qualified_namespace=ns, eventhub_name=os.getenv("EVENTHUB_IN", "match-events"),
                                      consumer_group="$Default", checkpoint_store=store, credential=credential)
    producer = EventHubProducerClient(fully_qualified_namespace=ns,
                                      eventhub_name=os.getenv("EVENTHUB_OUT", "match-broadcast"), credential=credential)
    worker = BroadcastWorker(producer, live_max=int(os.getenv("LIVE_FOUNDRY_MAX", "2")))

    async def on_batch(partition_context: Any, events: list) -> None:
        for ev in events:
            try:
                await worker.handle(ev.body_as_json())
            except Exception:  # noqa: BLE001 - one bad event must not stop the match
                log.exception("failed to process event")
        if events:
            await partition_context.update_checkpoint(events[-1])

    async with consumer, producer:
        flusher = asyncio.create_task(worker.flusher())
        log.info("worker listening on %s", ns)
        try:
            await consumer.receive_batch(on_event_batch=on_batch, max_batch_size=100, max_wait_time=1,
                                         starting_position="-1")  # no checkpoint yet: read the backlog that woke us up
        finally:
            flusher.cancel()
    await credential.close()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("azure").setLevel(logging.WARNING)
    asyncio.run(run())


if __name__ == "__main__":
    main()
