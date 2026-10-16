# Inside the Game – Synthetic Match Insights Engine

Entry for the **Inside the Game: Developer Hackathon** (Microsoft + Premier League).

> All data in this repository is **fully synthetic**. Clubs, players, venues and match events are
> procedurally generated. No real Premier League match data is used or redistributed.

This repository is being built in stages:

| Stage | Component | Status |
|---|---|---|
| Data | `matchsim`: synthetic, football-realistic match generator + live event streamer | ✅ ready |
| Ingest → Interpret → Explain → Render → Personalize | `insights`: multi-agent pipeline (Microsoft Agent Framework on Microsoft Foundry) producing timed overlay cues | ✅ ready |
| Experience | `webapp`: broadcast-style web player with analyst / casual / club-fan / player-focus modes | ✅ ready |
| Cloud | Event Hubs ingest, Container Apps, Foundry tracing & evaluations | 🚧 planned |

---

## `insights`: multi-agent match intelligence

```
 events ─► MatchState (deterministic stats, momentum, control-vs-chaos, pressing, rhythm)
             │
           MomentDetector ─► importance score
             ├─ low  ─► fast lane: deterministic templates ─────────────────────────────┐
             └─ high ─► Agent Framework workflow                                         │
                         intake ─┬─► Analyst agent   (tools: snapshot, window, player)  │
                                 └─► Tactician agent (why it matters)                    │
                                       └─fan-in─► Narrator agent ◄─┐                     │
                                                    │              │ rewrite feedback    │
                                                 Fact Guard ───────┘ (max 2 attempts)    │
                                                    │                                    │
                                                 Persona agent (one variant per viewer)  │
                                                    │                                    │
                                                 Producer ─► timed OverlayCue JSON ◄─────┘
```

* **Distinct roles, shared state.** Agents read the live `MatchState` only through tools. Every
  tool result is logged as evidence on the job (blackboard pattern).
* **Grounded numbers.** The deterministic Fact Guard checks that every number in generated copy
  comes from the moment facts or tool evidence. If it does not, the narrator rewrites with feedback.
  When retries run out, the grounded template copy ships instead. Persona variants are checked
  one viewer at a time.
* **Failure recovery.** Each agent runs under a timeout and has a deterministic twin with the same
  output contract. Timeouts, errors and malformed outputs fall back automatically, and the cue's
  `provenance.trace` records which path ran.
* **Real-time first.** Ingestion never waits for the AI. High-importance moments run concurrently
  (bounded), and the Producer schedules cues per screen slot and drops stale low-priority graphics.
* **Personalization.** The default viewers are a studio analyst (EN), a casual fan (EN), a home-club
  fan (Czech) and a player cam on the away star (Spanish, with 15-minute player check-ins).

```bash
pip install -e .[agents,dev]
cp .env.example .env           # set FOUNDRY_PROJECT_ENDPOINT and FOUNDRY_MODEL; az login
insights data/samples/syn-comeback-kba-hbc-211 --show analyst
insights data/samples/syn-comeback-kba-hbc-211 --offline --show club_fan_cs   # no Azure needed
```

Outputs go to `<match>/insights/`: `overlays.jsonl` (cues), `moments.jsonl`, `profiles.json` and
`run_stats.json`.

**Measured on Foundry** (`gpt-5.4-mini`, GlobalStandard; sample `comeback` match, 2,562 events):
70 moments → 154 personalized cues for 4 viewers. All 31 agent workflows ran on the LLM with 0 fallbacks
and 0 unverified numbers. Median agent latency was 2–4 s, and the full match processed in 79 s with
6 parallel workflows.

---

## `matchsim`: synthetic match generator

`matchsim` simulates a full 90-minute match at event level. It is a possession-chain model:
the ball carrier picks an action (pass, carry, cross, shot) whose probabilities depend on
pitch zone, opponent pressure, player attributes, fatigue, team style and game state. Momentum,
pressing waves, late-game chasing and ten-men collapses therefore **emerge** from the simulation
instead of being scripted.

### Quick start

```bash
pip install -e .[dev]              # or: set PYTHONPATH=src
matchsim list                      # scenarios and fictional clubs
matchsim generate --scenario comeback --seed 100
matchsim stats data/generated/<match_id>
matchsim stream data/generated/<match_id> --speed 10          # live feed on stdout
matchsim stream data/generated/<match_id> --http http://localhost:7071/api/ingest
pytest -q
```

Each generated match is a folder with:

* `metadata.json`: clubs, squads (attributes, top speed), formations, venue, referee, weather,
  attacking direction per period, final score, generator seed and scenario.
* `events.jsonl`: the ordered event stream (≈2,500 events per match).
* `stats.json`: deterministic team statistics computed from the events.

Five ready-made matches, one per scenario, live in [`data/samples`](data/samples).

### Storytelling scenarios

| Scenario | Story |
|---|---|
| `balanced` | Evenly matched sides; anything can happen. |
| `dominant_but_losing` | Home side controls the ball and creates more, but a direct away side wins on the counter. |
| `comeback` | Away side goes two up; the home side storms back after the hour. |
| `late_drama` | Decided (or rescued) in the 88th minute or later. |
| `red_card` | Away side goes down to ten men in the first half; watch control shift. |

Scenarios bias tactics (pressing, directness, finishing, intent) and the generator then
**rejection-samples seeds** until the finished match really tells that story. Every event stays
plausible, and the seed is recorded so each match is reproducible.

### Realism benchmarks

Averages per team over 80 simulated `balanced` matches, against typical top-flight values:

| Metric | matchsim | Typical top flight |
|---|---|---|
| Goals | 1.52 | ~1.4–1.5 |
| Expected goals (xG) | 1.57 | ~1.4–1.5 |
| Shots | 12.9 | ~12–13 |
| xG per shot | 0.12 | ~0.10–0.12 |
| Pass accuracy | 81 % | ~80–84 % |
| Corners | 6–7 | ~5 |
| Fouls | 7.6 | ~10 |
| Penalties per match | 0.3 | ~0.3 |
| Team distance covered | 106 km | ~105–115 km |
| Top sprint speed | 33 km/h | ~32–36 km/h |

`tests/test_matchsim.py` enforces these ranges, so tuning cannot silently break realism.

### Event schema (v1.0)

Coordinates are absolute broadcast-camera metres: `x ∈ [0, 105]` left→right, `y ∈ [0, 68]`
top→bottom. `metadata.attacking_direction` tells which goal each team attacks per period.

```json
{
  "event_id": "syn-comeback-kba-ngu-101-00812", "match_id": "syn-comeback-kba-ngu-101",
  "seq": 812, "period": 1, "match_seconds": 1694.2, "clock": "28:14", "minute": 29,
  "type": "pass", "team_id": "kba", "player_id": "kba-07", "related_player_id": "kba-10",
  "x": 61.3, "y": 50.2, "end_x": 84.0, "end_y": 41.7, "outcome": "complete",
  "under_pressure": true, "possession_id": 74, "score": {"kba": 0, "ngu": 1},
  "metrics": {"distance_m": 24.3, "ball_speed_kmh": 55.1, "pass_type": "through_ball",
              "difficulty": 0.54, "difficulty_rating": 5, "completion_probability": 0.61,
              "progressive": true, "into_final_third": true, "into_box": false},
  "tags": ["counter_attack"]
}
```

| `type` | Key metrics / notes |
|---|---|
| `pass` | `distance_m`, `ball_speed_kmh`, `pass_type` (short, medium, long, switch, through_ball, back, cross), `difficulty` 0–1, `difficulty_rating` 1–10, `completion_probability`, `progressive`, `into_final_third`, `into_box` |
| `carry` | `distance_m`, `speed_kmh`, `duration_s`, `progressive`; tag `took_on_defender` |
| `shot` | `xg`, `distance_m`, `angle_deg`, `shot_speed_kmh`, `body_part`, `assist_type`, `on_target`; outcome goal / saved / blocked / off_target |
| `goal` | `xg`, `scorer_goals`, `possession_passes`, `possession_duration_s`; tags `equaliser`, `go_ahead`, `late_goal`, `brace`, `penalty`, `counter_attack` … |
| `pressure` | `distance_to_ball_m`, `closing_speed_kmh` |
| `tackle`, `interception`, `ball_recovery` | defensive actions (outcome `won` / `foul`) |
| `possession_change` | `how` (tackle, interception, loose_ball, out_of_play, goalkeeper_save …), `previous_duration_s`, `previous_passes` |
| `foul`, `card`, `free_kick`, `corner`, `throw_in`, `goal_kick`, `kickoff` | set pieces and discipline |
| `substitution` | `player_id` on, `related_player_id` off, `reason`, `outgoing_fatigue` |
| `player_alert` | threshold triggers for overlays: `top_speed` (≥ 32 km/h) and `distance_milestone` (10 km, 12 km) |
| `physical_update` | every 5 minutes: per-player distance, top speed, sprints, fatigue; team distance |
| `added_time`, `period_start`, `period_end`, `match_start`, `match_end` | match flow |

## `webapp`: broadcast overlay player

```bash
python -m webapp --port 8000          # http://localhost:8000
python -m webapp.bundle data/samples/<match> --engine foundry   # record a Foundry run as a replay
```

* **Replay mode** plays a recorded agent run (`data/replays/*.json`). Without a recording, the
  bundle is built on the fly with the offline twins.
* **Live agents mode** streams the pipeline over Server-Sent Events while the agents run on
  Foundry in real time (1×–60× match speed).
* **Broadcast screen.** Pitch with ball flight and pass trails, player name tagging, a score bug,
  and every overlay template: goal splash, lower third, stat card, momentum and chaos meter,
  player tag and full-screen recap. Each overlay is placed in its slot and timed by the cue.
* **Viewer profiles.** Switch between the four viewers (keys `1`–`4`). The *Same moment · four
  viewers* strip shows how one moment is told to each of them, including viewers the Producer
  filtered out.
* **Agent activity.** For every cue the sidebar shows which agents ran on the LLM, which used a
  deterministic or fallback path, their latencies, and the Fact Guard verdict.
* **Timeline.** Momentum bars per side, the chaos index line, goals and red cards. Click to seek;
  press `n` to jump to the next key moment.

## Repository layout

```
src/matchsim/     generator, stats, streamer, CLI
src/insights/     match state, moment detection, agents, workflow, producer
src/webapp/       web player (stdlib server + vanilla JS), replay bundles
data/replays/     recorded Foundry runs played back by the web player
data/samples/     five reference matches (one per scenario)
tests/            determinism, schema, realism and scenario tests
```
