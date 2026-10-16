"""Web player server (stdlib only).

Routes
  GET /                         broadcast player UI
  GET /api/matches              available matches (+ whether a saved replay exists)
  GET /api/replay/<match_id>    replay bundle (saved Foundry run, else built offline on the fly)
  GET /api/live/<match_id>      Server-Sent Events: the pipeline runs live and streams
                                `event`, `cue` and `moment` messages (?speed=20&engine=foundry|offline)
  GET /healthz                  liveness probe for Azure Container Apps
"""

from __future__ import annotations

import argparse
import asyncio
import json
import mimetypes
import os
import threading
from functools import lru_cache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from insights.agents import AgentSettings
from insights.pipeline import InsightsPipeline
from insights.telemetry import setup_telemetry
from matchsim.stream import load_match

from .bundle import REPLAY_DIR, build_bundle, slim_event
from .onair import ONAIR

STATIC = Path(__file__).parent / "static"
SAMPLES = Path("data/samples")
# Public deployments must not let anyone fan out unlimited model calls: at most this many live
# sessions run on Foundry at once; further viewers get the deterministic twins.
LIVE_FOUNDRY_SLOTS = threading.BoundedSemaphore(int(os.getenv("LIVE_FOUNDRY_MAX", "2")))
ASK_SLOTS = threading.BoundedSemaphore(int(os.getenv("ASK_MAX", "3")))


def list_matches() -> list[dict]:
    out = []
    for d in sorted(SAMPLES.iterdir()) if SAMPLES.exists() else []:
        meta_path = d / "metadata.json"
        if not meta_path.exists():
            continue
        md = json.loads(meta_path.read_text(encoding="utf-8"))
        h, a = md["home"], md["away"]
        out.append({
            "match_id": md["match_id"], "scenario": md["generator"]["scenario"],
            "title": f"{h['name']} {md['final_score'][h['team_id']]}-{md['final_score'][a['team_id']]} {a['name']}",
            "replay": (REPLAY_DIR / f"{md['match_id']}.json").exists(),
        })
    return out


@lru_cache(maxsize=8)
def offline_bundle(match_id: str) -> bytes:
    return json.dumps(build_bundle(SAMPLES / match_id, "offline"), ensure_ascii=False).encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    server_version = "InsideTheGame/0.1"

    def log_message(self, fmt: str, *args) -> None:  # quieter console
        if "/api/live" in (args[0] if args else ""):
            super().log_message(fmt, *args)

    def _send(self, code: int, body: bytes, ctype: str = "application/json; charset=utf-8") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def do_GET(self) -> None:  # noqa: N802
        url = urlparse(self.path)
        path = url.path
        if path == "/healthz":
            return self._json({"ok": True})
        if path == "/api/matches":
            return self._json(list_matches())
        if path.startswith("/api/replay/"):
            match_id = path.rsplit("/", 1)[-1]
            if not (SAMPLES / match_id).exists():
                return self._json({"error": "unknown match"}, 404)
            saved = REPLAY_DIR / f"{match_id}.json"
            body = saved.read_bytes() if saved.exists() else offline_bundle(match_id)
            return self._send(200, body)
        if path.startswith("/api/live/"):
            return self._live(path.rsplit("/", 1)[-1], parse_qs(url.query))
        if path == "/api/onair":
            return self._json({"enabled": ONAIR.enabled, "can_kickoff": bool(ONAIR.ingest_url),
                               "feeding": ONAIR.feeding, "matches": ONAIR.list()})
        if path.startswith("/api/onair/"):
            return self._onair(path.rsplit("/", 1)[-1])
        file = STATIC / ("index.html" if path in ("/", "") else path.lstrip("/"))
        if file.is_file() and STATIC in file.resolve().parents:
            ctype = mimetypes.guess_type(file.name)[0] or "application/octet-stream"
            return self._send(200, file.read_bytes(), ctype)
        self._json({"error": "not found"}, 404)

    def do_POST(self) -> None:  # noqa: N802
        url = urlparse(self.path)
        if url.path == "/api/ask":
            return self._ask()
        if url.path.startswith("/api/kickoff/"):
            match_id = url.path.rsplit("/", 1)[-1]
            if not (SAMPLES / match_id).exists():
                return self._json({"error": "unknown match"}, 404)
            speed = max(5.0, min(60.0, float(parse_qs(url.query).get("speed", ["30"])[0])))
            ok, msg = ONAIR.kickoff(SAMPLES / match_id, speed)
            return self._json({"ok": ok, "message": msg}, 202 if ok else 409)
        self._json({"error": "not found"}, 404)

    def _ask(self) -> None:
        from insights.studio import ask

        try:
            body = json.loads(self.rfile.read(min(int(self.headers.get("Content-Length", "0")), 8192)) or b"{}")
            match_id, question = str(body["match_id"]), str(body["question"]).strip()[:300]
            minute = max(1, min(130, int(body.get("minute", 90))))
            language = str(body.get("language", "en"))[:8]
        except (ValueError, KeyError, TypeError):
            return self._json({"error": "expected JSON with match_id, minute, question"}, 400)
        if not question or not (SAMPLES / match_id).exists():
            return self._json({"error": "unknown match or empty question"}, 400)
        if not ASK_SLOTS.acquire(blocking=False):
            return self._json({"error": "the studio is busy, try again in a moment"}, 429)
        try:
            result = asyncio.run(ask(match_id, minute, question, language))
            return self._json(result)
        except Exception as exc:  # noqa: BLE001 - surface a friendly error, never a stack trace
            return self._json({"error": f"studio unavailable: {type(exc).__name__}"}, 503)
        finally:
            ASK_SLOTS.release()

    def _sse_headers(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

    def _onair(self, match_id: str) -> None:
        sub = ONAIR.subscribe(match_id)
        if sub is None:
            return self._json({"error": "match is not on air"}, 404)
        history, q = sub
        self._sse_headers()

        def push(kind: str, data: dict) -> None:
            self.wfile.write(f"event: {kind}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode("utf-8"))

        try:
            for kind, data in history:
                push(kind, data)
            self.wfile.flush()
            done = any(k == "done" for k, _ in history)
            while not done:
                try:
                    kind, data = q.get(timeout=15)
                except Exception:  # noqa: BLE001 - keep-alive comment
                    self.wfile.write(b": keep-alive\n\n")
                    self.wfile.flush()
                    continue
                push(kind, data)
                self.wfile.flush()
                done = kind == "done"
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        finally:
            ONAIR.unsubscribe(match_id, q)

    # ------------------------------------------------------------------ live SSE
    def _live(self, match_id: str, q: dict) -> None:
        if not (SAMPLES / match_id).exists():
            return self._json({"error": "unknown match"}, 404)
        speed = max(1.0, min(120.0, float(q.get("speed", ["20"])[0])))
        engine = q.get("engine", ["foundry"])[0]
        metadata, events = load_match(SAMPLES / match_id)
        settings = AgentSettings.from_env()
        holds_slot = engine != "offline" and settings.enabled and LIVE_FOUNDRY_SLOTS.acquire(blocking=False)
        if not holds_slot:
            settings.endpoint = None

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        lock = threading.Lock()

        def push(kind: str, data: dict) -> None:
            payload = f"event: {kind}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode("utf-8")
            with lock:
                self.wfile.write(payload)
                self.wfile.flush()

        pipe = InsightsPipeline(metadata, settings=settings, speed=speed,
                                event_sink=lambda e: push("event", slim_event(e)),
                                cue_sink=lambda c: push("cue", c),
                                moment_sink=lambda m: push("moment", {k: v for k, v in m.items() if k != "facts"}))
        try:
            push("hello", {"metadata": metadata, "profiles": [p.model_dump() for p in pipe.profiles],
                           "engine": pipe.mode, "model": settings.model if pipe.mode == "foundry" else None,
                           "speed": speed})
            stats = asyncio.run(pipe.run_events(events))
            push("done", stats)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass  # viewer closed the tab
        finally:
            if holds_slot:
                LIVE_FOUNDRY_SLOTS.release()


def main() -> None:
    parser = argparse.ArgumentParser(description="Inside the Game web player")
    parser.add_argument("--host", default=os.getenv("HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("PORT", "8000")))
    args = parser.parse_args()
    setup_telemetry("insights-player")
    ONAIR.start()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Inside the Game player on http://{args.host}:{args.port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
