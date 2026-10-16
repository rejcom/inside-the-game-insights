"""Replay a generated match as a live event feed.

Events are emitted with the same spacing they had in the match (scaled by
`speed`), so downstream consumers experience a realistic live stream. Sinks are
pluggable: stdout (JSON lines) and HTTP POST ship today; an Azure Event Hubs
sink plugs in through the same interface.
"""

from __future__ import annotations

import json
import sys
import time
import urllib.request
from pathlib import Path
from typing import Callable, Iterable, Iterator

Sink = Callable[[dict], None]


def load_match(match_dir: str | Path) -> tuple[dict, list[dict]]:
    match_dir = Path(match_dir)
    metadata = json.loads((match_dir / "metadata.json").read_text(encoding="utf-8"))
    with (match_dir / "events.jsonl").open(encoding="utf-8") as f:
        events = [json.loads(line) for line in f if line.strip()]
    return metadata, events


def paced(events: Iterable[dict], speed: float = 1.0, from_minute: int = 0,
          sleep: Callable[[float], None] = time.sleep) -> Iterator[dict]:
    """Yield events in match order, sleeping between them to simulate live play.

    speed=1 is real time, speed=10 plays a half in ~5 minutes, speed<=0 disables pacing.
    """
    prev_t = None
    for e in events:
        if e.get("minute", 0) < from_minute and e["type"] not in ("match_start",):
            continue
        t = e["match_seconds"]
        if prev_t is not None and speed > 0:
            gap = (t - prev_t) / speed
            if gap > 0:
                sleep(gap)
        prev_t = t
        yield e


def stdout_sink(event: dict) -> None:
    sys.stdout.write(json.dumps(event, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def http_sink(url: str, timeout: float = 5.0) -> Sink:
    def send(event: dict) -> None:
        req = urllib.request.Request(url, data=json.dumps(event).encode("utf-8"),
                                     headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            resp.read()
    return send


def replay(match_dir: str | Path, sinks: list[Sink], speed: float = 1.0, from_minute: int = 0,
           include_metadata: bool = True) -> int:
    metadata, events = load_match(match_dir)
    count = 0
    if include_metadata:
        for sink in sinks:
            sink({"type": "match_metadata", "match_id": metadata["match_id"], "metadata": metadata})
    for e in paced(events, speed=speed, from_minute=from_minute):
        for sink in sinks:
            sink(e)
        count += 1
    return count
