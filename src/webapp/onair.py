"""On-air mode: fan the Event Hubs broadcast out to every viewer, and simulate a data provider.

* `OnAir` reads `match-broadcast` (one reader per replica, from @latest) on a background
  thread and keeps a per-match buffer. Each SSE viewer gets the buffered history and then
  live messages, so late joiners see the whole match so far.
* `kickoff()` plays a sample match into the ingest Function, paced like a real feed, the
  way an official data provider would push events to the gateway.

Enabled when EVENTHUB_NAMESPACE (and, for kickoff, FEED_INGEST_URL) are configured.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import queue
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any

from matchsim.stream import load_match

log = logging.getLogger("insights.onair")
KEEP_FINISHED_S = 15 * 60
FEED_TICK_S = 0.5


class OnAir:
    def __init__(self) -> None:
        self.namespace = os.getenv("EVENTHUB_NAMESPACE")
        self.hub = os.getenv("EVENTHUB_OUT", "match-broadcast")
        self.ingest_url = os.getenv("FEED_INGEST_URL")
        self.lock = threading.Lock()
        self.matches: dict[str, dict[str, Any]] = {}
        self.feeding: str | None = None
        self._started = False

    @property
    def enabled(self) -> bool:
        return bool(self.namespace)

    # ------------------------------------------------------------------ broadcast consumer
    def start(self) -> None:
        if self._started or not self.enabled:
            return
        self._started = True
        threading.Thread(target=lambda: asyncio.run(self._consume()), name="onair-consumer", daemon=True).start()

    async def _consume(self) -> None:
        from azure.eventhub.aio import EventHubConsumerClient
        from azure.identity.aio import DefaultAzureCredential

        while True:
            try:
                async with DefaultAzureCredential() as cred:
                    client = EventHubConsumerClient(fully_qualified_namespace=self.namespace, eventhub_name=self.hub,
                                                    consumer_group="$Default", credential=cred)
                    async with client:
                        log.info("on-air consumer reading %s/%s", self.namespace, self.hub)
                        await client.receive(on_event=self._on_event, starting_position="@latest")
            except Exception:  # noqa: BLE001 - reconnect forever
                log.exception("on-air consumer crashed; reconnecting in 5 s")
                await asyncio.sleep(5)

    async def _on_event(self, partition_context: Any, event: Any) -> None:
        if event is None:
            return
        msg = event.body_as_json()
        self.publish(msg["match_id"], msg["kind"], msg["data"])

    def publish(self, match_id: str, kind: str, data: dict) -> None:
        with self.lock:
            if kind == "hello":
                self.matches[match_id] = {"hello": data, "items": [], "done": False, "subs": set(),
                                          "started": time.time(), "finished": None}
            m = self.matches.get(match_id)
            if m is None:
                return  # joined mid-match without hello
            if kind != "hello":
                m["items"].append((kind, data))
            if kind == "done":
                m["done"], m["finished"] = True, time.time()
            subs = list(m["subs"])
            for mid in [k for k, v in self.matches.items() if v["finished"] and time.time() - v["finished"] > KEEP_FINISHED_S]:
                self.matches.pop(mid, None)
        for q in subs:
            q.put((kind, data))

    def list(self) -> list[dict]:
        with self.lock:
            return [{"match_id": k, "done": v["done"], "items": len(v["items"]), "engine": v["hello"].get("engine"),
                     "feeding": self.feeding == k} for k, v in self.matches.items()]

    def subscribe(self, match_id: str) -> tuple[list[tuple[str, dict]], "queue.Queue[tuple[str, dict]]"] | None:
        with self.lock:
            m = self.matches.get(match_id)
            if m is None:
                return None
            q: queue.Queue = queue.Queue()
            m["subs"].add(q)
            return [("hello", m["hello"])] + list(m["items"]), q

    def unsubscribe(self, match_id: str, q: queue.Queue) -> None:
        with self.lock:
            if match_id in self.matches:
                self.matches[match_id]["subs"].discard(q)

    # ------------------------------------------------------------------ simulated data provider
    def kickoff(self, match_dir: Path, speed: float) -> tuple[bool, str]:
        if not self.ingest_url:
            return False, "FEED_INGEST_URL is not configured"
        with self.lock:
            if self.feeding:
                return False, f"{self.feeding} is already on air"
            self.feeding = match_dir.name
        threading.Thread(target=self._feed, args=(match_dir, speed), name="feed", daemon=True).start()
        return True, "kicked off"

    def _post(self, payload: Any) -> None:
        req = urllib.request.Request(self.ingest_url, data=json.dumps(payload).encode("utf-8"),
                                     headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=20) as r:
            r.read()

    def _feed(self, match_dir: Path, speed: float) -> None:
        try:
            metadata, events = load_match(match_dir)
            self._post({"type": "match_metadata", "match_id": metadata["match_id"], "metadata": metadata,
                        "feed_speed": speed})
            # Ship everything that has "happened" on a fixed tick, like a real provider's feed.
            # One HTTPS call per event would cap the feed at ~10 events/s; a tick keeps up at 60x.
            t0, start_ms, i = time.monotonic(), events[0]["match_seconds"], 0
            while i < len(events):
                tick_start = time.monotonic()
                now_ms = start_ms + (tick_start - t0) * speed
                batch = []
                while i < len(events) and events[i]["match_seconds"] <= now_ms:
                    batch.append(events[i])
                    i += 1
                for k in range(0, len(batch), 200):
                    self._post(batch[k:k + 200])
                time.sleep(max(0.0, FEED_TICK_S - (time.monotonic() - tick_start)))
        except Exception:  # noqa: BLE001
            log.exception("feed failed")
        finally:
            with self.lock:
                self.feeding = None


ONAIR = OnAir()
