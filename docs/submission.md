# Inside the Game – Submission kit

Everything needed for the Innovation Studio project page: the elevator pitch, the long
description, testing instructions for judges, and a shot-by-shot script for the demo video
(under 2 minutes).

**Live demo:** https://insidethegame-player.whiteplant-48237189.swedencentral.azurecontainerapps.io
**Repository:** https://github.com/rejcom/inside-the-game-insights
**MCP server:** https://insidethegame-mcp.whiteplant-48237189.swedencentral.azurecontainerapps.io/mcp

---

## 1. Elevator pitch (≈125 words)

> **Inside the Game** is an AI match-insights engine that explains football beyond the scoreline,
> live, for every fan. It ingests synthetic match events through Azure Functions and Event Hubs,
> computes every statistic deterministically (xG, pass difficulty, momentum, a control-versus-chaos
> index) and sends only the moments that matter to a team of agents built with **Microsoft Agent
> Framework on Microsoft Foundry**. An analyst and a tactician work in parallel, a narrator writes
> broadcast copy, a fact guard rejects any number the data does not support, and a persona agent
> reshapes the story for a studio analyst, a casual fan, a Czech club supporter or a Spanish
> player cam. The result is timed, machine-readable overlays, automated recaps and a spoiler-safe
> "Ask the studio" agent served over MCP.

## 2. Project description

### The problem
Broadcasters already have the data. What they lack is the story behind it, told fast enough for
live TV and personal enough for millions of different fans. A single graphics desk cannot write a
stat card for a data-hungry analyst, a casual viewer and a supporter in another language at the
same moment. Generic AI commentary is risky: one invented statistic on air damages trust.

### What we built: one pipeline, five stages
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

### Microsoft and Azure technology
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

### Results
* All five recorded matches ran fully on Foundry with **0 fallbacks**; a full match is processed in
  about 55 s at full speed.
* **Foundry evaluations** (60 items, all profiles, EN/CS/ES): groundedness **59/60**, relevance
  **60/60**, coherence **60/60**, fluency **59/60**. The local fact guard passed **60/60**.
* The end-to-end Azure path (Function → Event Hubs → worker → broadcast) streamed a full match of
  2,641 events in 116 s at 60× speed.
* 29 automated tests cover generator realism, the pipeline, failure recovery (simulated model
  timeouts and hallucinated numbers), overlay scheduling, the web server and MCP.

### Real-world impact
* **Rights holders and studios** get explainable, on-brand graphics in seconds instead of minutes,
  with an audit trail for every number on screen.
* **Streaming platforms** can offer per-viewer modes (language, club, player) without extra
  editorial staff.
* **Developers** get open building blocks: an event schema, a realistic synthetic data engine, an
  MCP server and a reusable agent workflow pattern. The pattern is deterministic numbers, agents
  that explain them, guards that verify the output, and personas that adapt it.

### Responsible AI
* All data is synthetic; no real clubs or players.
* Agents never compute statistics. Every on-screen number is grounded and verified.
* Answers to viewers are spoiler-safe by construction.
* Prompt and completion content is not recorded in traces unless explicitly enabled.
* Public endpoints have rate and concurrency caps on model use.

## 3. Testing instructions for judges

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

## 4. Demo video script (target 1:52)

Voice-over (VO) is about 250 words, roughly 135 words per minute. Record the screen at 1920×1080
with the browser at 100 % zoom and the Live demo URL. All links below use the live demo host; the
`t` values are match seconds.

| Time | Screen (what to show) | Action | Voice-over |
|---|---|---|---|
| 0:00–0:07 | `/?match=syn-comeback-kba-hbc-211&t=4990&speed=5&play=1` with the analyst profile | The goal splash "Rosvik levels it" pops in at 79:54 | *"Every match is a story. Most broadcasts only show you the score."* |
| 0:07–0:18 | Same, then the camera pans to the **Agent activity** sidebar | Hover the agent chips: analyst, tactician, narrator and persona (LLM, with latency), fact guard ✓ | *"Inside the Game is an AI insights engine on Microsoft Foundry. It finds the moments that matter, explains why, and tells that story differently to every fan."* |
| 0:18–0:38 | `/?match=syn-comeback-kba-hbc-211&t=4375&speed=5&play=1` | The momentum and chaos card at 69:40, then the goal splash at 69:57. Show the ball trail and player name tag | *"Code computes every number: xG, pass difficulty, momentum, a control-versus-chaos index. Key moments go to a team of agents built with Microsoft Agent Framework. An analyst and a tactician run in parallel, a narrator writes the copy, and a fact guard rejects any number that isn't in the data."* |
| 0:38–0:58 | Pause on the goal; scroll so the **Same moment · four viewers** strip is visible | Press 1 → 2 → 3 → 4 | *"Then a persona agent reshapes it: dense stats for the studio analyst, plain words for a casual fan, a Czech supporter's view, and a Spanish player cam that follows one player all match. Same moment, four genuinely different experiences."* |
| 0:58–1:16 | `/?match=syn-comeback-kba-hbc-211&t=4460` (70:58, score 1-2) | Type in **Ask the studio**: *"Why are Kingsbridge losing when they have more of the game?"* and show the answer plus the meta line (MCP tools · no spoilers · facts verified) | *"Fans can ask the studio anything. The agent answers through our MCP server, whose tools replay the match only up to the viewer's minute, so it never spoils what happens next."* |
| 1:16–1:36 | Click **On air** (the banner reads *ON AIR · Foundry agents · via Event Hubs*). Quick cuts: Azure portal resource group (Function, Event Hubs, Container Apps), Foundry **Tracing**, Foundry **Evaluations** | Kick off, then 2–3 s per cut | *"In production it's event-driven. An Azure Function validates the feed, Event Hubs keeps every match in order, and a Container Apps worker runs the agents once for every viewer, all with managed identities, no keys. Every run is traced in Foundry, and Foundry evaluations score the copy: 59 of 60 grounded."* |
| 1:36–1:52 | `/?match=syn-comeback-kba-hbc-211&t=6088` (full-time recap card), then an end card | End card: logo, *Inside the Game*, the live demo URL, the GitHub URL and the tech line *Microsoft Foundry · Agent Framework · Azure Functions · Event Hubs · Container Apps · MCP · GitHub Copilot* | *"Explainable match intelligence, real-time narratives and automated recaps, personalized for every fan in any language. Inside the Game."* |

### Recording checklist
- [ ] Warm up the apps first (open the player and the MCP URL once) so nothing cold-starts on camera.
- [ ] Before recording, run **On air** once so the worker is warm. Don't record while
      another match is on air; the kickoff button is single-match.
- [ ] Hide bookmarks and extensions, use a clean browser profile, and set system notifications to off.
- [ ] VO: record separately (Clipchamp, Audacity or OBS) and keep it under 1:55 total.
- [ ] Use no copyrighted music or third-party logos. Use silence or royalty-free audio you have
      rights to, and do not show any real league or club branding.
- [ ] Captions: add English subtitles (Clipchamp auto-captions) for accessibility.
- [ ] Upload to YouTube as **Public** (or Vimeo), then paste the URL on the project page.
- [ ] Project page fields: the pitch (section 1), the description (section 2), the repo URL, the video
      URL, and the testing instructions (section 3, including the live demo link).
