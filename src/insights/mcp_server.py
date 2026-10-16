"""MCP server exposing live-replayable match intelligence.

Any MCP client can use it: our own Foundry agents (the studio Q&A agent),
GitHub Copilot in VS Code, Claude Desktop, or another broadcaster's tooling.
All tools are read-only and take an optional `minute`: results are computed
as of that minute only, so a viewer watching the 30th minute never gets spoilers.

    insights-mcp                       # stdio (desktop clients)
    insights-mcp --http --port 8001    # streamable HTTP at /mcp (remote clients, Azure)
"""

from __future__ import annotations

import argparse
import os
from typing import Annotated, Any

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

from . import match_store as store

READ_ONLY = ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=False)
Minute = Annotated[int | None, Field(description="Football minute to evaluate at (1-90+; 45 and 90 include "
                                                 "stoppage time). Omit for full time. Never pass a minute later "
                                                 "than the viewer's current minute.", ge=1, le=130)]
MatchId = Annotated[str, Field(description="Match id from list_matches, e.g. 'syn-comeback-kba-hbc-211'")]

server = FastMCP(
    name="inside-the-game-match-intelligence",
    instructions=(
        "Read-only football match intelligence over fully synthetic matches. Start with list_matches. "
        "Pass `minute` to stay spoiler-safe. Numbers come from a deterministic match state; quote them "
        "exactly and do not invent statistics."
    ),
)


def _guard(fn):
    """Turn lookup errors into clean tool errors instead of stack traces."""
    import functools

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except store.MatchNotFound as exc:
            return {"error": f"unknown match {exc}; call list_matches"}
        except KeyError as exc:
            return {"error": str(exc).strip("'\"")}
    return wrapper


@server.tool(annotations=READ_ONLY)
def list_matches() -> list[dict[str, Any]]:
    """List available synthetic matches with scenario, clubs and final score."""
    return store.list_matches()


@server.tool(annotations=READ_ONLY)
@_guard
def get_match_snapshot(match_id: MatchId, minute: Minute = None) -> dict[str, Any]:
    """Score, team totals (xG, shots, possession, passing, pressing, cards), the last 5 minutes'
    momentum / chaos index / pressing, recent timeline and top performers - as of `minute`."""
    return store.state_at(match_id, minute).snapshot()


@server.tool(annotations=READ_ONLY)
@_guard
def get_window_metrics(match_id: MatchId, minute: Minute = None,
                       window_minutes: Annotated[int, Field(ge=2, le=20)] = 5) -> dict[str, Any]:
    """Rolling indicators for the `window_minutes` before `minute`: momentum (-100..100, positive = home),
    chaos index (0 controlled .. 100 chaotic), possession changes per minute, high pressures, PPDA, tempo."""
    return store.state_at(match_id, minute).window_metrics(window_minutes * 60)


@server.tool(annotations=READ_ONLY)
@_guard
def get_team_totals(match_id: MatchId, team: Annotated[str, Field(description="Club name or short code")],
                    minute: Minute = None) -> dict[str, Any]:
    """Cumulative totals for one team as of `minute`."""
    state = store.state_at(match_id, minute)
    return state.team_totals(store.team_id(state, team))


@server.tool(annotations=READ_ONLY)
@_guard
def get_player_stats(match_id: MatchId, player: Annotated[str, Field(description="Player name, short name or id")],
                     minute: Minute = None) -> dict[str, Any]:
    """One player's live stats as of `minute`: goals, assists, xG, key passes, passing accuracy, completed-pass
    difficulty, progressive passes, take-ons, defensive actions, distance covered and top speed."""
    found = store.state_at(match_id, minute).find_player(player)
    if not found:
        raise KeyError(f"player '{player}' not found in {match_id}")
    return found


@server.tool(annotations=READ_ONLY)
@_guard
def list_players(match_id: MatchId, team: Annotated[str | None, Field(description="Optional club filter")] = None
                 ) -> list[dict[str, Any]]:
    """Squads with shirt number, role and whether the player started."""
    metadata, _ = store._load(match_id)
    out = []
    for side in ("home", "away"):
        t = metadata[side]
        if team and team.lower() not in (t["name"].lower(), t["short"].lower(), t["team_id"]):
            continue
        out += [{"player_id": p["player_id"], "name": p["name"], "number": p["number"], "role": p["role"],
                 "team": t["name"], "starter": p["starter"]} for p in t["players"]]
    return out


@server.tool(annotations=READ_ONLY)
@_guard
def list_key_moments(match_id: MatchId, minute: Minute = None,
                     kind: Annotated[str | None, Field(description=(
                         "Optional filter: goal, big_chance, key_save, red_card, yellow_card, momentum_swing, "
                         "pressing_wave, control_shift, pass_quality, player_alert, substitution, half_time, "
                         "full_time"))] = None) -> list[dict[str, Any]]:
    """Key moments detected up to `minute`, each with an importance score and verified facts."""
    return store.moments_until(match_id, minute, kind)


@server.tool(annotations=READ_ONLY)
@_guard
def get_broadcast_copy(match_id: MatchId, moment_id: str,
                       profile_id: Annotated[str, Field(description="analyst | casual | club_fan_cs | "
                                                                    "player_focus_es")] = "analyst") -> dict[str, Any]:
    """The on-screen copy our Foundry agents wrote for a moment and viewer profile (headline, caption,
    stats, why it matters), with the agents involved and the fact-check verdict."""
    copy = store.broadcast_copy(match_id, moment_id, profile_id)
    if copy is None:
        raise KeyError(f"no recorded broadcast copy for {moment_id} / {profile_id}")
    return copy


def main() -> None:
    parser = argparse.ArgumentParser(description="Inside the Game MCP server")
    parser.add_argument("--http", action="store_true", help="serve streamable HTTP instead of stdio")
    parser.add_argument("--host", default=os.getenv("HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("PORT", "8001")))
    args = parser.parse_args()
    if not (args.http or os.getenv("MCP_TRANSPORT", "").lower() == "http"):
        server.run("stdio")
        return
    from mcp.server.transport_security import TransportSecuritySettings

    s = server.settings
    s.host, s.port, s.streamable_http_path = args.host, args.port, "/mcp"
    s.stateless_http, s.json_response = True, True  # scale-to-zero friendly, no sticky sessions
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        # Public, read-only server over synthetic data behind Azure ingress: Host-header pinning
        # (DNS-rebinding protection) is meant for localhost servers, so allow the ingress hostname.
        s.transport_security = TransportSecuritySettings(enable_dns_rebinding_protection=False)
    server.run("streamable-http")


if __name__ == "__main__":
    main()
