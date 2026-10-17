- **🎬 Demo video:** https://youtu.be/_QToXIQQ3Nk
- **▶ Live demo:** https://insidethegame-player.whiteplant-48237189.swedencentral.azurecontainerapps.io
- **💻 Code (public):** https://github.com/rejcom/inside-the-game-insights
- **🔌 MCP server:** https://insidethegame-mcp.whiteplant-48237189.swedencentral.azurecontainerapps.io/mcp

### Elevator pitch
**Inside the Game** is an AI match-insights engine that explains football beyond the scoreline,
live, for every fan. It ingests synthetic match events through Azure Functions and Event Hubs,
computes every statistic deterministically (xG, pass difficulty, momentum, a control-versus-chaos
index) and sends only the moments that matter to a team of agents built with **Microsoft Agent
Framework on Microsoft Foundry**. An analyst and a tactician work in parallel, a narrator writes
broadcast copy, a fact guard rejects any number the data does not support, and a persona agent
reshapes the story for a studio analyst, a casual fan, a Czech club supporter or a Spanish
player cam. The result is timed, machine-readable overlays, automated recaps and a spoiler-safe
"Ask the studio" agent served over MCP.

### Project description
#### The problem
Broadcasters already have the data. What they lack is the story behind it, told fast enough for
live TV and personal enough for millions of different fans. A single graphics desk cannot write a
stat card for a data-hungry analyst, a casual viewer and a supporter in another language at the
same moment. Generic AI commentary is risky: one invented statistic on air damages trust.

#### What we built: one pipeline, five stages
1. **Ingest.** `matchsim`, our synthetic data engine, generates football-realistic matches at event
   level. Its realism is benchmarked and enforced by tests (2,600 events per match, 1.5 goals and
   12.9 shots per team, 81 % pass accuracy, 106 km run per team). It also has storytelling
   scenarios such as comebacks, late drama and red cards. A data provider pushes events to an
   **Azure Functions** gateway, which validates them and publishes to **Event Hubs**, partitioned
   by match.
2. **Interpret.** A deterministic `MatchState` computes every number: xG, pass distance, speed and
   difficulty, shot speed, top speed and distance, rolling momentum, PPDA, tempo, and a
   *control-vs-chaos* index. A moment detector scores importance for goals, big chances, saves,
   cards, momentum swings, pressing waves, control shifts, passes of quality and speed or distance
   thresholds.
3. **Explain.** High-importance moments run a **Microsoft Agent Framework workflow** on
   **Microsoft Foundry** (gpt-5.4-mini):
   * an *Analyst* and a *Tactician* work in parallel (fan-out/fan-in) using tools over the live state;
   * a *Narrator* writes headline, caption and commentary;
   * a deterministic *Fact Guard* sends the copy back for a rewrite if any number is ungrounded;
   * a *Persona* agent adapts the copy per viewer;
   * a *Producer* schedules the graphic.

   Every agent has a deterministic twin with the same output contract, so timeouts, errors and
   malformed output never stall the broadcast. Each cue records which path ran.
4. **Render.** The Producer emits timed, machine-readable overlay cues (goal splash, lower third,
   stat card, momentum and chaos meter, player tag, recap card) per screen slot. It avoids
   collisions and drops graphics that would arrive too late. A broadcast-style web player renders
   them over the pitch with ball flight, pass trails and live player name tags.
5. **Personalize.** The same moment becomes four different experiences:
   * a studio analyst (EN, dense stats and *why it matters*);
   * a casual fan (EN, plain language);
   * a home-club supporter (Czech);
   * a player cam (Spanish) that follows one player with 15-minute check-ins.

   The agents can write in any language.

**Ask the studio.** Viewers can ask questions at any minute. A Foundry agent answers through our
**MCP server**, whose tools replay the match only up to the viewer's minute. A spoiler guard and a
fact guard verify every answer. The same MCP server works with GitHub Copilot agent mode, Claude
Desktop or any other MCP client.

#### Microsoft and Azure technology
* **Microsoft Foundry:** project, model deployment (gpt-5.4-mini), **tracing** (Application
  Insights connected) and **evaluations** (groundedness, relevance, coherence, fluency).
* **Microsoft Agent Framework:** agents, structured outputs, function tools, a workflow with
  fan-out/fan-in and a conditional rewrite loop, MCP tools (`MCPStreamableHTTPTool`, `MCPStdioTool`),
  and OpenTelemetry instrumentation.
* **Azure Container Apps** for the player, the insights worker and the MCP server (scale to zero,
  KEDA rule).
* **Azure Functions** (Flex Consumption) as the ingest gateway.
* **Azure Event Hubs** for per-match ordered ingest and a broadcast hub.
* **Azure Storage** for checkpoints, **Azure Container Registry** for cloud builds,
  **Application Insights** and **Log Analytics** for telemetry.
* **Managed identities and RBAC** on every hop, so no keys or connection strings live in code.
* **MCP** (Model Context Protocol) server for match intelligence.
* **GitHub** (public repository, Actions CI on Python 3.12 and 3.13), built with **GitHub Copilot**.

#### Results
* All five recorded matches ran fully on Foundry with **0 fallbacks**; a full match is processed in
  about 55 s at full speed.
* **Foundry evaluations** (60 items, all profiles, EN/CS/ES): groundedness **59/60**, relevance
  **60/60**, coherence **60/60**, fluency **59/60**. The local fact guard passed **60/60**.
* The end-to-end Azure path (Function → Event Hubs → worker → broadcast) streamed a full match of
  2,641 events in 116 s at 60× speed.
* 29 automated tests cover generator realism, the pipeline, failure recovery (simulated model
  timeouts and hallucinated numbers), overlay scheduling, the web server and MCP.

#### Real-world impact
* **Rights holders and studios** get explainable, on-brand graphics in seconds instead of minutes,
  with an audit trail for every number on screen.
* **Streaming platforms** can offer per-viewer modes (language, club, player) without extra
  editorial staff.
* **Developers** get open building blocks: an event schema, a realistic synthetic data engine, an
  MCP server and a reusable agent workflow pattern. The pattern is deterministic numbers, agents
  that explain them, guards that verify the output, and personas that adapt it.

#### Responsible AI
* All data is synthetic; no real clubs or players.
* Agents never compute statistics. Every on-screen number is grounded and verified.
* Answers to viewers are spoiler-safe by construction.
* Prompt and completion content is not recorded in traces unless explicitly enabled.
* Public endpoints have rate and concurrency caps on model use.

### Testing instructions
1. Open the **live demo**. The first load can take a few seconds because the app scales to zero.
2. **Replay** mode plays recorded Foundry runs. Pick *Kingsbridge Athletic 2-2 Harbour City ·
   comeback*.
   * Press **Play**, choose **10×**, or press **n** to jump to the next key moment.
   * Switch viewers with the tabs or keys **1–4**, and watch *Same moment · four viewers* below the pitch.
   * The **Agent activity** sidebar shows which agents ran on the LLM, their latency and the fact
     guard verdict.
3. **Ask the studio** (sidebar): ask a question in any language, for example *"Why are they losing
   despite more chances?"*. The answer uses only data up to the current minute.
4. **Live agents** runs the agent pipeline live on Foundry for the selected match.
5. **On air** kicks off the match through Azure Functions → Event Hubs → worker → broadcast. All
   viewers share one agent run, and the engine badge reads *via Event Hubs*.
6. **MCP**: add the MCP URL to VS Code (`.vscode/mcp.json` in the repo) or any MCP client and call
   `list_matches`, then `get_match_snapshot` with `minute`.
7. **Run locally without Azure**:
   ```bash
   pip install -e .[dev]
   pytest -q
   python -m webapp
   ```
   Without Foundry settings, the agents use their deterministic twins.

Deep links make any moment reproducible:
`/?match=syn-comeback-kba-hbc-211&t=4396&profile=club_fan_cs` (`t` is in match seconds; `play=1`
autoplays; `speed=` sets the speed).
