"use strict";

const $ = (id) => document.getElementById(id);
const SPEEDS = [1, 5, 10, 30, 60];
const BALL_TYPES = new Set(["pass", "carry", "shot", "tackle", "interception", "ball_recovery", "possession_change",
  "throw_in", "corner", "goal_kick", "free_kick", "kickoff", "save", "foul"]);
const ACTOR_TYPES = new Set(["pass", "carry", "shot", "tackle", "interception", "ball_recovery", "save", "throw_in",
  "corner", "goal_kick", "free_kick", "kickoff"]);
const SLOT_LIMIT = { corner: 2, lower: 2, center: 1, fullscreen: 1 };
const GOAL_WORD = { en: "GOAL", es: "¡GOL!", cs: "GÓL!" };
const MODE_NAME = { analyst: "Analyst mode", casual: "Casual mode", club_fan: "Club fan mode", player_focus: "Player cam" };

const S = {
  bundle: null, meta: null, events: [], evTimes: [], cues: [], series: [], profiles: [], players: {},
  profileId: "analyst", t: 0, duration: 5400, playing: false, speed: 10, mode: "replay",
  path: [], pathT: [], rendered: new Map(), feedKey: "", compareKey: "", es: null, liveT: 0, liveWall: 0, lastTs: 0,
};

// ------------------------------------------------------------------ helpers
function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined && text !== null) e.textContent = text;
  return e;
}
function svg(tag, attrs) {
  const e = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [k, v] of Object.entries(attrs || {})) e.setAttribute(k, v);
  return e;
}
function lastIndexAtOrBefore(arr, t) { // arr sorted ascending
  let lo = 0, hi = arr.length - 1, ans = -1;
  while (lo <= hi) { const mid = (lo + hi) >> 1; if (arr[mid] <= t) { ans = mid; lo = mid + 1; } else hi = mid - 1; }
  return ans;
}
const pad = (n) => String(Math.floor(n)).padStart(2, "0");
function clockAt(t) {
  const p1 = (S.meta && S.meta.period_lengths_s && S.meta.period_lengths_s[0]) || S.p1Live || 2760;
  const [c, reg] = t < p1 ? [t, 2700] : [2700 + (t - p1), 5400];
  if (c < reg) return `${pad(c / 60)}:${pad(c % 60)}`;
  const x = c - reg;
  return `${reg / 60}+${pad(x / 60)}:${pad(x % 60)}`;
}
function teamColor(teamId) {
  if (!S.meta || !teamId) return "var(--accent)";
  const t = S.meta.home.team_id === teamId ? S.meta.home : S.meta.away;
  return t.colors[0];
}
const profile = () => S.profiles.find((p) => p.profile_id === S.profileId) || S.profiles[0];

// ------------------------------------------------------------------ data loading
async function loadMatches() {
  const matches = await (await fetch("/api/matches")).json();
  const sel = $("matchSelect");
  sel.innerHTML = "";
  for (const m of matches) {
    const o = el("option", null, `${m.title}  ·  ${m.scenario.replaceAll("_", " ")}${m.replay ? "  ·  Foundry replay" : ""}`);
    o.value = m.match_id;
    sel.appendChild(o);
  }
  const preferred = matches.find((m) => m.replay && m.scenario === "comeback") || matches.find((m) => m.replay) || matches[0];
  if (preferred) { sel.value = preferred.match_id; await loadReplay(preferred.match_id); }
}

async function loadReplay(matchId) {
  stopLive();
  const b = await (await fetch(`/api/replay/${matchId}`)).json();
  setBundle(b);
}

function setBundle(b) {
  S.bundle = b; S.meta = b.metadata; S.profiles = b.profiles; S.series = b.series || [];
  S.events = []; S.evTimes = []; S.path = []; S.pathT = []; S.cues = [];
  S.players = {};
  for (const side of ["home", "away"]) for (const p of S.meta[side].players) S.players[p.player_id] = { ...p, team_id: S.meta[side].team_id };
  for (const e of b.events || []) addEvent(e);
  for (const c of b.cues || []) S.cues.push(c);
  S.duration = S.events.length ? S.events[S.events.length - 1].match_seconds : 5700;
  if (!S.profiles.find((p) => p.profile_id === S.profileId)) S.profileId = S.profiles[0].profile_id;
  S.t = 0; S.feedKey = ""; S.compareKey = "";
  clearOverlays();
  document.documentElement.style.setProperty("--home", S.meta.home.colors[0]);
  document.documentElement.style.setProperty("--away", S.meta.away.colors[0]);
  $("lgHome").textContent = S.meta.home.name; $("lgAway").textContent = S.meta.away.name;
  renderTeams(); renderProfileTabs(); renderEngine(); drawTimeline(); render();
}

function addEvent(e) {
  S.events.push(e); S.evTimes.push(e.match_seconds);
  if (e.x === undefined || !BALL_TYPES.has(e.type)) return;
  const last = S.path[S.path.length - 1];
  if (last && last.t > e.match_seconds) { last.t = e.match_seconds - 0.05; S.pathT[S.pathT.length - 1] = last.t; }
  S.path.push({ t: e.match_seconds, x: e.x, y: e.y }); S.pathT.push(e.match_seconds);
  if (e.end_x !== undefined) {
    const m = e.metrics || {};
    let flight = 1.0;
    if (e.type === "pass") flight = (m.distance_m || 10) / Math.max(5, (m.ball_speed_kmh || 50) / 3.6) * 1.25;
    else if (e.type === "carry") flight = m.duration_s || 1.5;
    else if (e.type === "shot") flight = 0.6;
    S.path.push({ t: e.match_seconds + Math.min(flight, 6), x: e.end_x, y: e.end_y });
    S.pathT.push(e.match_seconds + Math.min(flight, 6));
  }
}

// ------------------------------------------------------------------ pitch rendering
function ballAt(t) {
  const p = S.path;
  if (!p.length) return { x: 52.5, y: 34 };
  const i = lastIndexAtOrBefore(S.pathT, t);
  if (i < 0) return p[0];
  if (i >= p.length - 1) return p[p.length - 1];
  const a = p[i], b = p[i + 1];
  const f = Math.max(0, Math.min(1, (t - a.t) / Math.max(0.05, b.t - a.t)));
  return { x: a.x + (b.x - a.x) * f, y: a.y + (b.y - a.y) * f };
}

function renderPitch() {
  const t = S.t;
  const b = ballAt(t);
  $("ball").setAttribute("cx", b.x); $("ball").setAttribute("cy", b.y);
  $("ballGlowEl").setAttribute("cx", b.x); $("ballGlowEl").setAttribute("cy", b.y);

  // trail: recent passes and carries
  const idx = lastIndexAtOrBefore(S.evTimes, t);
  const trail = $("trail");
  trail.innerHTML = "";
  let actor = null;
  for (let i = idx, n = 0; i >= 0 && n < 7; i--) {
    const e = S.events[i];
    if (t - e.match_seconds > 25) break;
    if (!actor && ACTOR_TYPES.has(e.type) && e.player_id && t - e.match_seconds < 8) actor = e;
    if ((e.type === "pass" || e.type === "carry") && e.end_x !== undefined) {
      const age = (t - e.match_seconds) / 25;
      const color = teamColor(e.team_id);
      trail.appendChild(svg("line", { x1: e.x, y1: e.y, x2: e.end_x, y2: e.end_y, stroke: color,
        class: "trail-line", "stroke-dasharray": e.type === "carry" ? "1 .8" : "", opacity: (1 - age) * 0.9 }));
      trail.appendChild(svg("circle", { cx: e.x, cy: e.y, r: 0.55, fill: color, class: "trail-dot", opacity: 1 - age }));
      n++;
    }
  }
  const tag = $("actorTag");
  if (actor && S.players[actor.player_id]) {
    const pl = S.players[actor.player_id];
    const text = `${pl.number}  ${pl.short_name}`;
    $("actorText").textContent = text;
    const w = text.length * 1.12 + 2;
    $("actorBg").setAttribute("width", w);
    $("actorBg").setAttribute("stroke", teamColor(pl.team_id));
    $("actorText").setAttribute("x", 1);
    const x = Math.max(-3, Math.min(108 - w, b.x - w / 2));
    const y = b.y < 8 ? b.y + 2 : b.y - 5.4;
    tag.setAttribute("transform", `translate(${x},${y})`);
    tag.style.display = "";
  } else tag.style.display = "none";

  // score bug
  const e = idx >= 0 ? S.events[idx] : null;
  const sc = (e && e.score) || {};
  $("sbScore").textContent = `${sc[S.meta.home.team_id] || 0}–${sc[S.meta.away.team_id] || 0}`;
  $("sbClock").textContent = clockAt(t);
  $("timeLabel").textContent = clockAt(t);
}

function renderTeams() {
  for (const [id, team] of [["sbHome", S.meta.home], ["sbAway", S.meta.away]]) {
    const box = $(id);
    box.querySelector(".sb-name").textContent = team.short;
    box.querySelector(".sb-color").style.background = team.colors[0];
  }
}

// ------------------------------------------------------------------ overlays
function activeCues() {
  const t = S.t;
  return S.cues.filter((c) => c.profile_id === S.profileId && c._start <= t && t < c._start + c.duration_s);
}

function clearOverlays() {
  for (const slot of ["corner", "lower", "center", "fullscreen"]) $(`slot-${slot}`).innerHTML = "";
  S.rendered.clear();
}

function renderOverlays() {
  const bySlot = { corner: [], lower: [], center: [], fullscreen: [] };
  for (const c of activeCues()) bySlot[c.slot].push(c);
  const wanted = new Set();
  for (const [slot, list] of Object.entries(bySlot)) {
    list.sort((a, b) => b.priority - a.priority);
    for (const c of list.slice(0, SLOT_LIMIT[slot])) wanted.add(c.cue_id);
  }
  for (const [id, node] of S.rendered) {
    if (!wanted.has(id)) {
      S.rendered.delete(id);
      node.classList.add("leaving");
      setTimeout(() => node.remove(), 300);
    }
  }
  for (const [slot, list] of Object.entries(bySlot)) {
    for (const c of list) {
      if (!wanted.has(c.cue_id) || S.rendered.has(c.cue_id)) continue;
      const node = buildOverlay(c);
      node.style.setProperty("--team", teamColor(c.team_id));
      $(`slot-${slot}`).appendChild(node);
      S.rendered.set(c.cue_id, node);
    }
  }
}

function statsBlock(stats) {
  const box = el("div", "ov-stats");
  for (const s of stats || []) {
    const st = el("div", "ov-stat");
    st.appendChild(el("b", null, s.value));
    st.appendChild(el("span", null, s.label));
    box.appendChild(st);
  }
  return box;
}

function sourceLine(c) {
  const trace = (c.provenance && c.provenance.trace) || [];
  const llm = trace.filter((s) => s.mode === "llm").map((s) => s.agent);
  const text = llm.length ? `AI: ${[...new Set(llm)].join(" · ")} · facts verified` : "template · facts verified";
  return el("div", "ov-source", text);
}

function buildOverlay(c) {
  const d = c.data || {};
  let node;
  switch (c.template) {
    case "goal_splash": {
      node = el("div", "ov goal-splash");
      node.appendChild(el("div", "goal-word", GOAL_WORD[c.language] || "GOAL"));
      node.appendChild(el("div", "ov-headline", c.headline));
      if (d.score) node.appendChild(el("div", "score", d.score));
      node.appendChild(el("div", "ov-caption", c.caption));
      if (c.stats && c.stats.length) node.appendChild(statsBlock(c.stats));
      break;
    }
    case "lower_third": {
      node = el("div", "ov lower-third");
      node.appendChild(el("div", "ov-headline", c.headline));
      node.appendChild(el("div", "ov-caption", c.caption));
      if (c.why_it_matters && !c.caption.includes(c.why_it_matters)) node.appendChild(el("div", "ov-why", c.why_it_matters));
      break;
    }
    case "player_tag": {
      node = el("div", "ov player-tag");
      node.appendChild(el("div", "num", d.number ?? "•"));
      const txt = el("div");
      txt.appendChild(el("div", "ov-headline", c.headline));
      txt.appendChild(el("div", "ov-caption", c.caption));
      node.appendChild(txt);
      break;
    }
    case "momentum_bar": {
      node = el("div", "ov momentum-card");
      node.appendChild(el("div", "ov-headline", c.headline));
      node.appendChild(el("div", "ov-caption", c.caption));
      if (d.momentum !== undefined) {
        const wrap = el("div", "momentum-wrap");
        const meter = el("div", "momentum-meter");
        meter.style.background = `linear-gradient(90deg, ${S.meta.home.colors[0]} 0 50%, ${S.meta.away.colors[0]} 50% 100%)`;
        wrap.appendChild(meter);
        const needle = el("div", "momentum-needle");
        needle.style.left = `${50 - d.momentum / 2}%`;
        wrap.appendChild(needle);
        node.appendChild(wrap);
        const labels = el("div", "momentum-labels");
        labels.appendChild(el("span", null, d.home)); labels.appendChild(el("span", null, d.away));
        node.appendChild(labels);
        const chaos = el("div", "chaos");
        chaos.appendChild(el("span", null, d.game_state === "chaotic" ? "Chaos" : d.game_state === "controlled" ? "Control" : "Balanced"));
        const bar = el("div", "chaos-bar"); const fill = el("i"); fill.style.width = `${d.chaos_index}%`;
        bar.appendChild(fill); chaos.appendChild(bar);
        chaos.appendChild(el("span", null, `${Math.round(d.chaos_index)}`));
        node.appendChild(chaos);
      }
      if (c.stats && c.stats.length) node.appendChild(statsBlock(c.stats));
      break;
    }
    case "recap_card": {
      node = el("div", "ov recap");
      node.appendChild(el("div", "ov-headline", c.headline));
      const left = el("div");
      left.appendChild(el("div", "ov-caption", c.caption));
      const r = d.recap;
      if (r) {
        if (c.language === "en" && r.summary && r.summary !== c.caption) left.appendChild(el("div", "ov-why", r.summary));
        const ul = el("ul");
        for (const k of r.key_moments || []) ul.appendChild(el("li", null, k));
        left.appendChild(ul);
        if (r.player_of_the_match) left.appendChild(el("div", "potm", `★ ${r.player_of_the_match}`));
      }
      node.appendChild(left);
      const right = el("div");
      if (d.home && d.away) {
        const rows = [["Goals", "goals"], ["xG", "xg"], ["Shots", "shots"], ["On target", "shots_on_target"],
          ["Possession %", "possession_pct"], ["Pass accuracy %", "pass_accuracy_pct"], ["Corners", "corners"]];
        const head = el("div", "cmp-row");
        head.appendChild(el("span", "v", S.meta.home.short)); head.appendChild(el("span", "lbl", ""));
        head.appendChild(el("span", "v r", S.meta.away.short)); right.appendChild(head);
        for (const [label, key] of rows) {
          const row = el("div", "cmp-row");
          row.appendChild(el("span", "v", d.home[key] ?? "–"));
          row.appendChild(el("span", "lbl", label));
          row.appendChild(el("span", "v r", d.away[key] ?? "–"));
          right.appendChild(row);
        }
      } else if (c.stats) right.appendChild(statsBlock(c.stats));
      node.appendChild(right);
      break;
    }
    case "alert_chip": {
      node = el("div", "ov alert-chip");
      node.appendChild(el("div", "ov-headline", c.headline));
      if (c.caption) node.appendChild(el("div", "ov-caption", c.caption));
      break;
    }
    default: { // stat_card
      node = el("div", "ov stat-card");
      node.appendChild(el("div", "ov-headline", c.headline));
      node.appendChild(el("div", "ov-caption", c.caption));
      if (c.why_it_matters && !c.caption.includes(c.why_it_matters)) node.appendChild(el("div", "ov-why", c.why_it_matters));
      if (c.stats && c.stats.length) node.appendChild(statsBlock(c.stats));
    }
  }
  if (["goal_splash", "lower_third", "stat_card", "momentum_bar", "recap_card"].includes(c.template)) node.appendChild(sourceLine(c));
  return node;
}

// ------------------------------------------------------------------ feed & compare
function isAgentCue(c) { return (c.provenance?.trace || []).some((s) => s.agent === "analyst"); }

function renderFeed() {
  const shown = S.cues.filter((c) => c.profile_id === S.profileId && c._start <= S.t);
  const key = `${S.profileId}:${shown.length}`;
  if (key === S.feedKey) return;
  S.feedKey = key;
  const feed = $("feed");
  feed.innerHTML = "";
  for (const c of shown.slice(-40).reverse()) {
    const li = el("li");
    const meta = el("div", "meta");
    meta.appendChild(el("span", "kind", c.moment_kind.replaceAll("_", " ")));
    meta.appendChild(el("span", null, c.clock));
    li.appendChild(meta);
    li.appendChild(el("div", "h", c.headline));
    const chips = el("div", "chips");
    for (const s of c.provenance?.trace || []) {
      const label = s.agent.replace("_", " ") + (s.mode === "llm" ? ` ${(s.latency_ms / 1000).toFixed(1)}s` : "");
      const chip = el("span", `mode ${s.mode}`, label);
      if (s.note) chip.title = s.note;
      chips.appendChild(chip);
    }
    li.appendChild(chips);
    const ok = c.provenance?.verified !== false;
    const guard = (c.provenance?.trace || []).find((s) => s.agent === "fact_guard");
    li.appendChild(el("div", `verified${ok ? "" : " bad"}`, guard ? `✓ ${guard.note}` : "✓ template numbers from match state"));
    feed.appendChild(li);
  }
}

function renderCompare() {
  const t = S.t;
  const agentCues = S.cues.filter((c) => c._start <= t && isAgentCue(c));
  let momentId = null;
  for (let i = agentCues.length - 1; i >= 0; i--) {
    const c = agentCues[i];
    if (c.profile_id === S.profileId || !momentId) { momentId = c.moment_id; if (c.profile_id === S.profileId) break; }
  }
  const key = `${momentId}:${S.profileId}`;
  if (key === S.compareKey) return;
  S.compareKey = key;
  const grid = $("compareGrid");
  grid.innerHTML = "";
  for (const p of S.profiles) {
    const card = el("div", `cmp-card${p.profile_id === S.profileId ? " active" : ""}`);
    const who = el("div", "who");
    who.appendChild(el("span", null, p.label));
    who.appendChild(el("span", null, p.language.toUpperCase()));
    card.appendChild(who);
    const c = momentId && S.cues.find((x) => x.moment_id === momentId && x.profile_id === p.profile_id);
    if (c) {
      card.appendChild(el("div", "h", c.headline));
      card.appendChild(el("div", "c", c.caption));
    } else card.appendChild(el("div", "empty", momentId ? "Not shown to this viewer: the Producer filtered it as irrelevant." : "Waiting for the first key moment…"));
    card.onclick = () => selectProfile(p.profile_id);
    grid.appendChild(card);
  }
}

// ------------------------------------------------------------------ chrome
function renderProfileTabs() {
  const nav = $("profileTabs");
  nav.innerHTML = "";
  for (const p of S.profiles) {
    const b = el("button", `profile-tab${p.profile_id === S.profileId ? " active" : ""}`);
    b.setAttribute("role", "tab");
    b.appendChild(el("span", "pt-title", p.label));
    const sub = el("span", "pt-sub");
    sub.appendChild(el("span", "lang", p.language.toUpperCase()));
    sub.appendChild(document.createTextNode(`  ${MODE_NAME[p.mode] || p.mode}`));
    b.appendChild(sub);
    b.onclick = () => selectProfile(p.profile_id);
    nav.appendChild(b);
  }
  const p = profile();
  $("viewerChip").textContent = p ? `Viewing as · ${p.label}` : "";
}

function selectProfile(id) {
  S.profileId = id; S.feedKey = ""; S.compareKey = "";
  clearOverlays(); renderProfileTabs(); render();
}

function renderEngine() {
  const b = $("engineBadge");
  const foundry = S.bundle.engine === "foundry";
  b.className = `engine-badge${foundry ? " foundry" : ""}`;
  b.textContent = foundry ? `Microsoft Foundry · ${S.bundle.model || "model"}` : "Offline deterministic twins";
}

function drawTimeline() {
  if (!S.meta) return;
  const box = $("timeline"), g = $("timelineSvg");
  const W = 1000, H = 64, mid = 30;
  g.setAttribute("viewBox", `0 0 ${W} ${H}`);
  g.innerHTML = "";
  const X = (t) => (t / Math.max(1, S.duration)) * W;
  g.appendChild(svg("line", { x1: 0, y1: mid, x2: W, y2: mid, stroke: "#2b3752", "stroke-width": 1 }));
  for (const s of S.series) {
    const h = (s.momentum / 100) * 24;
    g.appendChild(svg("rect", { x: X(s.t) - 2.5, width: 5, y: h > 0 ? mid - h : mid, height: Math.abs(h),
      fill: h > 0 ? S.meta.home.colors[0] : S.meta.away.colors[0], opacity: 0.75 }));
  }
  if (S.series.length > 1) {
    const pts = S.series.map((s) => `${X(s.t)},${H - 4 - (s.chaos / 100) * 14}`).join(" ");
    g.appendChild(svg("polyline", { points: pts, fill: "none", stroke: "#ffb547", "stroke-width": 1.4, opacity: 0.8 }));
  }
  const p1 = S.meta.period_lengths_s ? S.meta.period_lengths_s[0] : null;
  if (p1) g.appendChild(svg("line", { x1: X(p1), y1: 0, x2: X(p1), y2: H, stroke: "#56627d", "stroke-dasharray": "3 3" }));
  for (const e of S.events) {
    if (e.type === "goal") {
      g.appendChild(svg("circle", { cx: X(e.match_seconds), cy: 7, r: 5, fill: teamColor(e.team_id), stroke: "#fff", "stroke-width": 1.2 }));
    } else if (e.type === "card" && e.outcome !== "yellow") {
      g.appendChild(svg("rect", { x: X(e.match_seconds) - 3, y: 2, width: 6, height: 9, fill: "#e5243b", rx: 1 }));
    }
  }
  box.onpointerdown = (ev) => {
    const seek = (e2) => {
      const r = box.getBoundingClientRect();
      S.t = Math.max(0, Math.min(S.duration, ((e2.clientX - r.left) / r.width) * S.duration));
      S.feedKey = ""; render();
    };
    seek(ev);
    box.setPointerCapture(ev.pointerId);
    box.onpointermove = (e2) => seek(e2);
    box.onpointerup = () => { box.onpointermove = null; };
  };
}

function renderPlayhead() {
  $("playhead").style.left = `${(S.t / Math.max(1, S.duration)) * 100}%`;
}

function render() {
  if (!S.meta) return;
  for (const c of S.cues) if (c._start === undefined) c._start = c.display_at_match_seconds;
  renderPitch(); renderOverlays(); renderFeed(); renderCompare(); renderPlayhead();
  $("playBtn").textContent = S.playing ? "❚❚" : "▶";
}

// ------------------------------------------------------------------ playback loop
function tick(ts) {
  const dt = S.lastTs ? Math.min(0.25, (ts - S.lastTs) / 1000) : 0;
  S.lastTs = ts;
  if (S.mode === "live" && S.events.length) {
    const projected = S.liveT + ((performance.now() - S.liveWall) / 1000) * S.speed;
    S.t = Math.min(projected, S.liveT + 4);
    S.duration = Math.max(S.duration, S.t + 60);
    render();
  } else if (S.playing) {
    S.t = Math.min(S.duration, S.t + dt * S.speed);
    if (S.t >= S.duration) S.playing = false;
    render();
  }
  requestAnimationFrame(tick);
}

function setupTransport() {
  const sp = $("speeds");
  for (const v of SPEEDS) {
    const b = el("button", v === S.speed ? "active" : "", `${v}×`);
    b.onclick = () => {
      S.speed = v;
      [...sp.children].forEach((c) => c.classList.toggle("active", c === b));
      if (S.mode === "live") startLive();
    };
    sp.appendChild(b);
  }
  $("playBtn").onclick = () => { if (S.mode === "replay") { S.playing = !S.playing; render(); } };
  $("nextMomentBtn").onclick = () => {
    const next = S.cues.find((c) => c.profile_id === S.profileId && isAgentCue(c) && c._start > S.t + 1);
    if (next) { S.t = Math.max(0, next._start - 4); S.feedKey = ""; S.playing = true; clearOverlays(); render(); }
  };
  document.addEventListener("keydown", (e) => {
    if (e.code === "Space" && e.target === document.body) { e.preventDefault(); $("playBtn").click(); }
    if (e.key >= "1" && e.key <= "4") { const p = S.profiles[Number(e.key) - 1]; if (p) selectProfile(p.profile_id); }
    if (e.key === "n") $("nextMomentBtn").click();
  });
  $("matchSelect").onchange = (e) => (S.mode !== "live" ? loadReplay(e.target.value)
    : $("modeOnAir").classList.contains("active") ? startOnAir() : startLive());
  $("modeReplay").onclick = () => { setMode("replay"); loadReplay($("matchSelect").value); };
  $("modeLive").onclick = () => { setMode("live"); startLive(); };
  $("modeOnAir").onclick = () => { setMode("live"); $("modeLive").classList.remove("active");
    $("modeOnAir").classList.add("active"); startOnAir(); };
  window.addEventListener("resize", drawTimeline);
}

function setMode(mode) {
  S.mode = mode;
  $("modeReplay").classList.toggle("active", mode === "replay");
  $("modeLive").classList.toggle("active", mode === "live");
  $("modeOnAir").classList.remove("active");
  $("liveBanner").hidden = mode !== "live";
}

// ------------------------------------------------------------------ live mode (SSE)
function stopLive() { if (S.es) { S.es.close(); S.es = null; } }

function startLive() {
  connectStream(`/api/live/${$("matchSelect").value}?speed=${S.speed}&engine=foundry`, "LIVE");
}

async function startOnAir() {
  stopLive();
  const id = $("matchSelect").value;
  const banner = $("liveBanner");
  banner.hidden = false;
  const status = await (await fetch("/api/onair")).json();
  if (!status.enabled) { banner.textContent = "ON AIR unavailable: Event Hubs not configured"; return; }
  let entry = status.matches.find((m) => m.match_id === id && !m.done);
  if (!entry) {
    if (!status.can_kickoff) { banner.textContent = "ON AIR: nothing on air for this match"; return; }
    const r = await fetch(`/api/kickoff/${id}?speed=${Math.max(5, Math.min(60, S.speed))}`, { method: "POST" });
    const k = await r.json();
    if (!r.ok) { banner.textContent = `ON AIR: ${k.message}`; return; }
    banner.textContent = "KICK-OFF · Function → Event Hubs → worker…";
    for (let i = 0; i < 40 && !entry; i++) {
      await new Promise((res) => setTimeout(res, 1000));
      entry = (await (await fetch("/api/onair")).json()).matches.find((m) => m.match_id === id && !m.done);
    }
    if (!entry) { banner.textContent = "ON AIR: worker did not start (is it scaled up?)"; return; }
  }
  connectStream(`/api/onair/${id}`, "ON AIR");
}

function connectStream(url, label) {
  stopLive();
  S.playing = false;
  const id = $("matchSelect").value;
  const es = new EventSource(url);
  S.es = es;
  es.addEventListener("hello", (m) => {
    const h = JSON.parse(m.data);
    setBundle({ match_id: id, engine: h.engine, model: h.model, metadata: { ...h.metadata, period_lengths_s: null },
      profiles: h.profiles, events: [], cues: [], series: [] });
    S.p1Live = 2760;
    S.liveT = 0; S.liveWall = performance.now();
    const via = h.source === "event_hubs" ? " · via Event Hubs" : "";
    $("liveBanner").textContent = `${label} · ${h.engine === "foundry" ? "Foundry agents" : "offline twins"}${via}`;
    $("liveBanner").hidden = false;
  });
  es.addEventListener("event", (m) => {
    const e = JSON.parse(m.data);
    if (e.type === "period_end" && e.period === 1) S.p1Live = e.match_seconds;
    addEvent(e);
    S.liveT = e.match_seconds; S.liveWall = performance.now();
    if (e.type === "goal" || (e.type === "card" && e.outcome !== "yellow")) drawTimeline();
  });
  es.addEventListener("cue", (m) => {
    const c = JSON.parse(m.data);
    c._start = Math.max(c.display_at_match_seconds, S.t); // arrived late: show from now
    S.cues.push(c);
  });
  es.addEventListener("done", () => { $("liveBanner").textContent = "FULL TIME · agents finished"; stopLive(); });
  es.onerror = () => { if (S.es === es && es.readyState === EventSource.CLOSED) stopLive(); };
}

// ------------------------------------------------------------------ boot
setupTransport();
loadMatches().catch((err) => { $("engineBadge").textContent = `Failed to load: ${err}`; });
requestAnimationFrame(tick);
