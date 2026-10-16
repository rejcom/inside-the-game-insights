"""Produce the < 2 minute demo video automatically.

1. Voice-over per segment with Azure AI Speech (neural voice, Entra ID auth via the Foundry resource).
2. Screen segments recorded with Playwright (Microsoft Edge, 1920x1080) from the deployed player,
   using deep links so every shot lands on the exact moment; plus HTML title/diagram cards.
3. ffmpeg trims, concatenates, lays the voice-over under each segment and burns English subtitles.

    python demo/make_video.py            # -> demo/out/inside-the-game-demo.mp4 (+ .srt)
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "out"
sys.path.insert(0, str(ROOT.parent / "src"))

BASE = os.getenv("DEMO_BASE_URL", "https://insidethegame-player.whiteplant-48237189.swedencentral.azurecontainerapps.io")
MATCH = "syn-comeback-kba-hbc-211"
VOICE = os.getenv("DEMO_VOICE", "en-US-AndrewMultilingualNeural")
SPEECH_REGION = "swedencentral"
FOUNDRY_RESOURCE_ID = os.getenv("DEMO_SPEECH_RESOURCE_ID", "")
W, H, FPS = 1920, 1080, 30
VO_LEAD = 0.35  # seconds of picture before the voice starts in each segment


@dataclass
class Segment:
    name: str
    vo: str
    url: str
    act: Callable | None = None     # (page, deadline) -> None
    min_s: float = 0.0
    tail: float = 0.6
    vo_s: float = 0.0
    clip: Path | None = None
    dur: float = 0.0
    extra: dict = field(default_factory=dict)


def deep(t: float, profile: str = "analyst", speed: int = 5, play: bool = False) -> str:
    return f"{BASE}/?match={MATCH}&t={t}&profile={profile}&speed={speed}" + ("&play=1" if play else "")


def card(name: str) -> str:
    return (ROOT / "cards" / f"{name}.html").as_uri()


# --------------------------------------------------------------------------- page actions
def wait_until(page, deadline: float) -> None:
    remaining = deadline - time.monotonic()
    if remaining > 0:
        page.wait_for_timeout(int(remaining * 1000))


def personas(page, deadline: float) -> None:
    # Narrow the broadcast screen so the "Same moment - four viewers" strip fits in the frame.
    page.add_style_tag(content=".stage .screen{max-width:1000px;margin:0 auto}.transport,.timeline,.legend"
                               "{max-width:1000px;margin-left:auto;margin-right:auto}.compare{margin-top:8px}")
    page.evaluate("const c = document.querySelector('.compare').getBoundingClientRect();"
                  "window.scrollTo(0, Math.max(0, c.bottom + window.scrollY - window.innerHeight + 110))")  # leave room for subtitles
    page.wait_for_timeout(2600)
    for key in "234":
        page.keyboard.press(key)
        page.wait_for_timeout(3000)
    page.keyboard.press("1")
    wait_until(page, deadline)


def ask(page, deadline: float, t0: float) -> list[tuple[float, float]]:
    """Type, submit, then jump-cut the wait for the agent: returns the time ranges to keep."""
    page.click("#askInput")
    page.type("#askInput", "Why are Kingsbridge losing when they have more of the game?", delay=22)
    page.wait_for_timeout(300)
    page.keyboard.press("Enter")
    submitted = time.monotonic() - t0
    try:  # wait for the agent's answer (meta line appears when done)
        page.wait_for_selector("#askAnswer .meta", timeout=60000)
    except Exception:  # noqa: BLE001 - keep recording whatever is on screen
        pass
    answered = time.monotonic() - t0
    page.wait_for_timeout(4500)
    page.evaluate("document.getElementById('askAnswer').scrollTo({top: 1e6, behavior: 'smooth'})")
    page.wait_for_timeout(4500)
    end = time.monotonic() - t0
    return [(0.0, submitted + 1.6), (answered - 0.3, end)]


def on_air(page, deadline: float) -> None:
    page.click("#modeOnAir")
    wait_until(page, deadline)


SEGMENTS = [
    Segment("hook", "Every match is a story. Most broadcasts only show you the score.",
            deep(4995, speed=1, play=True)),
    Segment("title", "Inside the Game is an AI insights engine built on Microsoft Foundry. It finds the moments "
                     "that matter, explains why they matter, and tells that story differently to every fan.",
            card("title")),
    Segment("numbers", "Code computes every number: expected goals, pass difficulty, momentum, and a "
                       "control-versus-chaos index.", deep(4381, speed=1, play=True)),
    Segment("agents", "Key moments go to a team of agents built with Microsoft Agent Framework. An analyst and a "
                      "tactician work in parallel, a narrator writes the copy, and a fact guard rejects any number "
                      "that isn't in the data.", card("agents")),
    Segment("personas", "Then a persona agent reshapes the story: dense stats for the studio analyst, plain words "
                        "for a casual fan, a Czech supporter's view, and a Spanish player cam. Same moment, four "
                        "different experiences.", deep(4404), act=personas, min_s=14),
    Segment("ask", "Fans can also ask the studio anything. The agent answers through our MCP server, whose tools "
                   "only see the match up to the viewer's minute, so it never spoils what happens next.",
            deep(4460), act=ask, min_s=16),
    Segment("onair", "In production, everything is event-driven.", deep(4460, speed=60), act=on_air, min_s=5),
    Segment("azure", "An Azure Function validates the feed, Event Hubs keeps each match in order, and a Container "
                     "Apps worker runs the agents once for every viewer, with managed identities and no keys. Every "
                     "run is traced in Foundry, and Foundry evaluations rate the copy fifty-nine out of sixty for "
                     "groundedness.", card("azure")),
    Segment("recap", "Explainable match intelligence, real-time narratives and automated recaps,",
            deep(6089, speed=1, play=True)),
    Segment("end", "personalized for every fan, in any language. Inside the Game.", card("end"), min_s=6, tail=1.4),
]


# --------------------------------------------------------------------------- voice-over
def synthesize(seg: Segment) -> None:
    from insights.agents import _load_dotenv, azure_credential

    _load_dotenv()
    path = OUT / f"vo_{seg.name}.wav"
    if not path.exists():
        token = azure_credential().get_token("https://cognitiveservices.azure.com/.default").token
        ssml = (f'<speak version="1.0" xmlns="http://www.w3.org/2001/10/synthesis" xml:lang="en-US">'
                f'<voice name="{VOICE}"><prosody rate="+4%">{seg.vo}</prosody></voice></speak>')
        req = urllib.request.Request(
            f"https://{SPEECH_REGION}.tts.speech.microsoft.com/cognitiveservices/v1", data=ssml.encode("utf-8"),
            method="POST", headers={"Authorization": f"Bearer aad#{FOUNDRY_RESOURCE_ID}#{token}",
                                    "Content-Type": "application/ssml+xml",
                                    "X-Microsoft-OutputFormat": "riff-48khz-16bit-mono-pcm",
                                    "User-Agent": "insidethegame-demo"})
        path.write_bytes(urllib.request.urlopen(req, timeout=60).read())
    with wave.open(str(path)) as w:
        seg.vo_s = w.getnframes() / w.getframerate()


# --------------------------------------------------------------------------- recording
def record(seg: Segment, browser) -> None:
    target = max(seg.min_s, VO_LEAD + seg.vo_s + seg.tail)
    ctx = browser.new_context(viewport={"width": W, "height": H}, record_video_dir=str(OUT / "raw"),
                              record_video_size={"width": W, "height": H})
    page = ctx.new_page()
    t_page = time.monotonic()
    page.goto(seg.url, wait_until="networkidle", timeout=90000)
    if seg.url.startswith("http"):
        page.wait_for_function("typeof S !== 'undefined' && S.meta && S.cues.length > 0", timeout=60000)
        page.wait_for_timeout(500)
    else:
        page.wait_for_timeout(150)
    start = time.monotonic() - t_page
    deadline = time.monotonic() + target
    t_start = time.monotonic()
    keep = None
    if seg.act is ask:
        keep = ask(page, deadline, t_start)
    elif seg.act:
        seg.act(page, deadline)
    else:
        wait_until(page, deadline)
    total = time.monotonic() - t_page - start
    keep = keep or [(0.0, total)]
    video = page.video
    ctx.close()
    raw = Path(video.path())
    seg.clip = OUT / f"clip_{seg.name}.mp4"
    parts = []
    for i, (a, b) in enumerate(keep):
        part = OUT / f"part_{seg.name}_{i}.mp4"
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{start + a:.3f}", "-i", str(raw),
                        "-t", f"{b - a:.3f}", "-vf", f"fps={FPS},scale={W}:{H},format=yuv420p", "-an",
                        "-c:v", "libx264", "-preset", "medium", "-crf", "18", str(part)], check=True)
        parts.append(part)
    lst = OUT / f"parts_{seg.name}.txt"
    lst.write_text("".join(f"file '{p.as_posix()}'\n" for p in parts), encoding="utf-8")
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy",
                    str(seg.clip)], check=True)
    seg.dur = sum(b - a for a, b in keep)
    print(f"  {seg.name:<9} {seg.dur:5.1f}s (voice {seg.vo_s:4.1f}s)")


# --------------------------------------------------------------------------- assembly
def srt_time(t: float) -> str:
    ms = int(round(t * 1000))
    return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"


def assemble() -> Path:
    concat = OUT / "clips.txt"
    concat.write_text("".join(f"file '{s.clip.as_posix()}'\n" for s in SEGMENTS), encoding="utf-8")
    video = OUT / "video_only.mp4"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(concat),
                    "-c", "copy", str(video)], check=True)

    # Voice track: each segment's VO delayed by VO_LEAD and padded to the segment length.
    parts, srt, t = [], [], 0.0
    for i, s in enumerate(SEGMENTS):
        part = OUT / f"audio_{s.name}.wav"
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(OUT / f"vo_{s.name}.wav"), "-af",
                        f"adelay={int(VO_LEAD * 1000)},apad", "-t", f"{s.dur:.3f}", "-ar", "48000", "-ac", "2",
                        str(part)], check=True)
        parts.append(part)
        srt += _subtitle_lines(len(srt), t + VO_LEAD, s.vo, s.vo_s)
        t += s.dur
    alist = OUT / "audio.txt"
    alist.write_text("".join(f"file '{p.as_posix()}'\n" for p in parts), encoding="utf-8")
    audio = OUT / "voice.wav"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(alist),
                    "-c", "copy", str(audio)], check=True)
    srt_path = OUT / "inside-the-game-demo.srt"
    srt_path.write_text("\n".join(srt), encoding="utf-8")

    final = OUT / "inside-the-game-demo.mp4"
    style = ("FontName=Segoe UI,FontSize=17,PrimaryColour=&H00FFFFFF,OutlineColour=&H80000000,"
             "BackColour=&H99000000,BorderStyle=4,Outline=0,Shadow=0,MarginV=26")
    sub_filter = f"subtitles=inside-the-game-demo.srt:force_style='{style}'"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", video.name, "-i", audio.name, "-vf", sub_filter,
                    "-c:v", "libx264", "-preset", "slow", "-crf", "20", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", final.name],
                   check=True, cwd=OUT)
    return final


def _subtitle_lines(start_index: int, start: float, text: str, dur: float) -> list[str]:
    """Split a segment's voice-over into readable cues of <= 12 words, timed by word share."""
    words = text.split()
    chunks, cur = [], []
    for w in words:
        cur.append(w)
        if len(cur) >= 12 or (w.endswith((".", ",", ":")) and len(cur) >= 6):
            chunks.append(cur)
            cur = []
    if cur:
        chunks.append(cur)
    lines, t = [], start
    n = start_index // 4 + 1
    for c in chunks:
        d = dur * len(c) / len(words)
        lines += [str(n), f"{srt_time(t)} --> {srt_time(t + d)}", " ".join(c), ""]
        t += d
        n += 1
    return lines


def main() -> None:
    from playwright.sync_api import sync_playwright

    global FOUNDRY_RESOURCE_ID
    if not FOUNDRY_RESOURCE_ID:
        sub = subprocess.run(["powershell", "-NoProfile", "-Command",
                              "$env:Path += ';C:\\Program Files\\Microsoft SDKs\\Azure\\CLI2\\wbin'; "
                              "az cognitiveservices account show -g rg-insidethegame -n insidethegame-kr-foundry "
                              "--query id -o tsv"], capture_output=True, text=True)
        FOUNDRY_RESOURCE_ID = sub.stdout.strip()
    OUT.mkdir(exist_ok=True)
    print("voice-over")
    for s in SEGMENTS:
        synthesize(s)
    total_vo = sum(s.vo_s for s in SEGMENTS)
    print(f"  total voice {total_vo:.1f}s")
    print("warming up the player")
    urllib.request.urlopen(BASE + "/api/replay/" + MATCH, timeout=120).read()
    for _ in range(120):  # the On air shot needs the single feed slot to be free
        if json.loads(urllib.request.urlopen(BASE + "/api/onair", timeout=30).read()).get("feeding") is None:
            break
        time.sleep(5)
    print("recording")
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="msedge", headless=True)
        for s in SEGMENTS:
            record(s, browser)
        browser.close()
    final = assemble()
    total = sum(s.dur for s in SEGMENTS)
    (OUT / "timeline.json").write_text(json.dumps([{"segment": s.name, "seconds": round(s.dur, 2),
                                                    "voice": round(s.vo_s, 2)} for s in SEGMENTS], indent=2))
    print(f"done: {final} ({total:.1f}s)")


if __name__ == "__main__":
    main()
