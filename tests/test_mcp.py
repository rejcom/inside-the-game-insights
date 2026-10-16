import asyncio
import json

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from insights.mcp_server import server
from insights.studio import spoiler_violations

MATCH = "syn-comeback-kba-hbc-211"


def call(name, args):
    async def go():
        async with create_connected_server_and_client_session(server._mcp_server) as client:
            r = await client.call_tool(name, args)
            if r.structuredContent is not None:  # FastMCP wraps non-object results as {"result": ...}
                sc = r.structuredContent
                return sc["result"] if set(sc) == {"result"} else sc
            items = [json.loads(c.text) for c in r.content]
            return items[0] if len(items) == 1 else items
    return asyncio.run(go())


def test_tools_are_listed_and_read_only():
    async def go():
        async with create_connected_server_and_client_session(server._mcp_server) as client:
            return (await client.list_tools()).tools
    tools = {t.name: t for t in asyncio.run(go())}
    assert {"list_matches", "get_match_snapshot", "get_window_metrics", "get_player_stats",
            "list_key_moments", "get_broadcast_copy"} <= set(tools)
    assert all(t.annotations and t.annotations.readOnlyHint for t in tools.values())


def test_snapshot_is_spoiler_safe():
    early = call("get_match_snapshot", {"match_id": MATCH, "minute": 30})
    full = call("get_match_snapshot", {"match_id": MATCH})
    assert early["minute"] <= 30 and early["score"] != full["score"]
    goals_early = call("list_key_moments", {"match_id": MATCH, "minute": 30, "kind": "goal"})
    goals_all = call("list_key_moments", {"match_id": MATCH, "kind": "goal"})
    assert len(goals_early) < len(goals_all)
    assert all(m["minute"] <= 30 for m in goals_early)


def test_player_and_team_lookups():
    players = call("list_players", {"match_id": MATCH})
    one = players[0]["name"]
    stats = call("get_player_stats", {"match_id": MATCH, "player": one, "minute": 60})
    assert stats["name"] == one
    team = call("get_team_totals", {"match_id": MATCH, "team": "Harbour City", "minute": 45})
    assert team["team"] == "Harbour City"


def test_errors_are_clean():
    assert "error" in call("get_match_snapshot", {"match_id": "nope"})
    assert "error" in call("get_player_stats", {"match_id": MATCH, "player": "Nobody Atall"})


def test_recorded_agent_copy_is_exposed():
    goal = call("list_key_moments", {"match_id": MATCH, "kind": "goal"})[0]
    copy = call("get_broadcast_copy", {"match_id": MATCH, "moment_id": goal["moment_id"]})
    assert copy["headline"] and copy["facts_verified"]


@pytest.mark.parametrize("calls,expected", [
    ([{"tool": "get_match_snapshot", "arguments": {"match_id": MATCH, "minute": 40}}], []),
    ([{"tool": "get_match_snapshot", "arguments": {"match_id": MATCH}}], ["get_match_snapshot(minute=None)"]),
    ([{"tool": "list_key_moments", "arguments": {"match_id": MATCH, "minute": 80}}], ["list_key_moments(minute=80)"]),
    ([{"tool": "list_players", "arguments": {"match_id": MATCH}}], []),
])
def test_spoiler_guard(calls, expected):
    assert spoiler_violations(calls, 45) == expected
