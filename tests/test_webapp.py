import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from webapp import server as srv


@pytest.fixture(scope="module")
def base_url():
    # Force offline agents so tests never call Foundry (an empty value also beats .env).
    import os
    os.environ["FOUNDRY_PROJECT_ENDPOINT"] = ""
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


def get(url):
    with urllib.request.urlopen(url, timeout=60) as r:
        return r.status, r.read()


def test_health_and_index(base_url):
    assert get(base_url + "/healthz")[0] == 200
    status, body = get(base_url + "/")
    assert status == 200 and b"Inside the Game" in body


def test_matches_and_replay_bundle(base_url):
    matches = json.loads(get(base_url + "/api/matches")[1])
    assert len(matches) >= 5
    bundle = json.loads(get(base_url + f"/api/replay/{matches[0]['match_id']}")[1])
    assert bundle["events"] and bundle["cues"] and bundle["series"]
    assert {p["profile_id"] for p in bundle["profiles"]} == {c["profile_id"] for c in bundle["cues"]}


def test_live_stream_sends_events_and_cues(base_url):
    match_id = json.loads(get(base_url + "/api/matches")[1])[0]["match_id"]
    seen = set()
    with urllib.request.urlopen(base_url + f"/api/live/{match_id}?speed=120&engine=offline", timeout=60) as r:
        for raw in r:
            line = raw.decode("utf-8").strip()
            if line.startswith("event: "):
                seen.add(line[7:])
            if {"hello", "event", "cue"} <= seen:
                break
    assert {"hello", "event", "cue"} <= seen
