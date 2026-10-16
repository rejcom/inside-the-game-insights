"""Match feed ingest gateway (Azure Functions, Python v2 model).

Data providers POST match events (single object or a batch array). The gateway
validates each event against the matchsim schema, stamps ingestion time and
publishes valid events to Event Hubs with partition key = match_id, so every
match stays strictly ordered for the insights worker. Invalid events are
rejected with reasons instead of poisoning the stream.

Auth: function key for callers; managed identity (Azure Event Hubs Data Sender)
towards Event Hubs - no connection strings.
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections import defaultdict

import azure.functions as func
from azure.eventhub import EventData, EventHubProducerClient
from azure.identity import DefaultAzureCredential

app = func.FunctionApp(http_auth_level=func.AuthLevel.FUNCTION)

EVENT_TYPES = {
    "match_metadata", "match_start", "period_start", "kickoff", "pass", "carry", "pressure", "tackle",
    "interception", "ball_recovery", "possession_change", "shot", "goal", "save", "foul", "card", "free_kick",
    "corner", "throw_in", "goal_kick", "substitution", "physical_update", "player_alert", "added_time",
    "period_end", "match_end",
}
MAX_BATCH = 500
_producer: EventHubProducerClient | None = None


def producer() -> EventHubProducerClient:
    global _producer
    if _producer is None:
        _producer = EventHubProducerClient(
            fully_qualified_namespace=os.environ["EVENTHUB_NAMESPACE"],
            eventhub_name=os.environ.get("EVENTHUB_NAME", "match-events"),
            credential=DefaultAzureCredential(),
        )
    return _producer


def validate(e: object) -> str | None:
    """Return a rejection reason, or None when the event is valid."""
    if not isinstance(e, dict):
        return "event must be a JSON object"
    if not isinstance(e.get("match_id"), str) or not e["match_id"]:
        return "missing match_id"
    if e.get("type") not in EVENT_TYPES:
        return f"unknown type {e.get('type')!r}"
    if e["type"] == "match_metadata":
        return None if isinstance(e.get("metadata"), dict) else "match_metadata needs a metadata object"
    for key, typ in (("event_id", str), ("seq", int), ("match_seconds", (int, float)), ("minute", int)):
        if not isinstance(e.get(key), typ):
            return f"missing or invalid {key}"
    for key, hi in (("x", 105.0), ("end_x", 105.0), ("y", 68.0), ("end_y", 68.0)):
        if key in e and not (isinstance(e[key], (int, float)) and 0 <= e[key] <= hi):
            return f"{key} outside the pitch"
    if not 0 <= e["match_seconds"] <= 4 * 3600:
        return "match_seconds out of range"
    return None


@app.route(route="ingest", methods=["POST"])
def ingest(req: func.HttpRequest) -> func.HttpResponse:
    try:
        body = req.get_json()
    except ValueError:
        return func.HttpResponse('{"error": "invalid JSON"}', status_code=400, mimetype="application/json")
    events = body if isinstance(body, list) else [body]
    if len(events) > MAX_BATCH:
        return func.HttpResponse(f'{{"error": "batch larger than {MAX_BATCH}"}}', status_code=413,
                                 mimetype="application/json")
    accepted: dict[str, list[dict]] = defaultdict(list)
    rejected = []
    now = time.time()
    for i, e in enumerate(events):
        reason = validate(e)
        if reason:
            rejected.append({"index": i, "reason": reason})
            continue
        e["ingested_at"] = now
        accepted[e["match_id"]].append(e)

    client = producer()
    for match_id, items in accepted.items():
        batch = client.create_batch(partition_key=match_id)
        for e in items:
            data = EventData(json.dumps(e, ensure_ascii=False))
            try:
                batch.add(data)
            except ValueError:  # batch full: send and start a new one
                client.send_batch(batch)
                batch = client.create_batch(partition_key=match_id)
                batch.add(data)
        client.send_batch(batch)
    count = sum(len(v) for v in accepted.values())
    if rejected:
        logging.warning("rejected %d events: %s", len(rejected), rejected[:5])
    return func.HttpResponse(json.dumps({"accepted": count, "rejected": rejected}), status_code=202,
                             mimetype="application/json")


@app.route(route="health", methods=["GET"], auth_level=func.AuthLevel.ANONYMOUS)
def health(req: func.HttpRequest) -> func.HttpResponse:
    return func.HttpResponse('{"ok": true}', mimetype="application/json")
