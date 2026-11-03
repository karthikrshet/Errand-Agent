"""Produce the narrated demo film: docs/demo/errand-agent-demo.mp4 (+ GIF preview and poster).

Pipeline (fully local; no narration text or key leaves the machine):
1. Narration: each sentence is synthesised with the Windows speech engine (SAPI) to a WAV.
2. Recording: Playwright drives Microsoft Edge at 1280x720. Deck scenes animate tools/video/
   deck.html; app scenes drive the REAL web app (keyless demo mode, fresh database, forced
   mock-world scenario) with an animated cursor that types, clicks and highlights trace rows.
   Each sentence's caption is shown, and its audio offset recorded, at the moment it starts,
   so captions, voice and on-screen action stay in sync even when the app is slower.
3. Assembly: ffmpeg places every sentence at its offset, adds a soft ambient bed, fades,
   concatenates scenes and encodes H.264 + a GIF preview for the README.

Run (needs Windows, Edge, and a tool env with playwright + imageio-ffmpeg):
    <tool-python> tools/video/make_video.py
"""

from __future__ import annotations

import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
import wave
from pathlib import Path
from typing import Any

import imageio_ffmpeg
from playwright.sync_api import Page, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from script import SCENES, Scene

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
OUT = ROOT / "docs" / "demo"
PROJECT_PY = ROOT / ".venv" / "Scripts" / "python.exe"
FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
VOICE = os.environ.get("VIDEO_VOICE", "Microsoft David Desktop")
W, H, FPS = 1280, 720, 30
GAP_S = 0.45  # silence between sentences
LEAD_TRIM_S = 0.6  # drop the blank first frames of each recording


# ----------------------------------------------------------------------------- narration


def synthesize(text: str, wav: Path) -> float:
    txt = wav.with_suffix(".txt")
    txt.write_text(text, encoding="utf-8")
    ps = (
        "Add-Type -AssemblyName System.Speech;"
        "$s=New-Object System.Speech.Synthesis.SpeechSynthesizer;"
        f"$s.SelectVoice('{VOICE}');$s.Rate=1;$s.Volume=100;"
        f"$s.SetOutputToWaveFile('{wav}');"
        f"$s.Speak([IO.File]::ReadAllText('{txt}'));$s.Dispose()"
    )
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], check=True)
    with wave.open(str(wav)) as w:
        return w.getnframes() / w.getframerate()


# ----------------------------------------------------------------------------- web app


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def start_app(scenario: str, work: Path) -> tuple[subprocess.Popen[bytes], str]:
    port = free_port()
    db = work / f"app-{scenario}-{port}.db"
    env = {
        **os.environ,
        "ERRAND_SCRIPT": "demo",
        "ERRAND_SCENARIO": scenario,
        "ERRAND_DB_PATH": str(db),
        "ERRAND_TRACES_DIR": str(work / "traces"),
        "ERRAND_WATCH_INTERVAL_S": "0",
        "PYTHONIOENCODING": "utf-8",
    }
    code = f"import uvicorn; uvicorn.run('errand.web.app:app', port={port}, log_level='warning')"
    proc = subprocess.Popen([str(PROJECT_PY), "-c", code], cwd=ROOT, env=env)
    url = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            urllib.request.urlopen(url + "/health", timeout=1)
            return proc, url
        except OSError:
            time.sleep(0.2)
    proc.kill()
    raise RuntimeError("web app did not start")


# ----------------------------------------------------------------------------- actions


class Driver:
    def __init__(self, page: Page) -> None:
        self.page = page
        self.baseline = 0

    def agent_messages(self) -> int:
        return int(self.page.locator("#log .msg:not(.me)").count())

    def move(self, selector: str) -> tuple[float, float]:
        box = self.page.locator(selector).first.bounding_box()
        assert box is not None, selector
        x = box["x"] + min(box["width"] / 2, 140)
        y = box["y"] + box["height"] / 2
        self.page.evaluate("([x, y]) => window.__cursor(x, y)", [x, y])
        self.page.wait_for_timeout(820)
        return x, y

    def click(self, selector: str) -> None:
        x, y = self.move(selector)
        self.page.evaluate("([x, y]) => window.__ripple(x, y)", [x, y])
        self.page.locator(selector).first.click()
        self.page.wait_for_timeout(350)

    def type(self, text: str) -> None:
        self.baseline = self.agent_messages()
        self.click("#text")
        self.page.locator("#text").press_sequentially(text, delay=34)
        self.page.wait_for_timeout(250)
        self.page.evaluate("([x, y]) => window.__ripple(x, y)", self._send_point())
        self.page.keyboard.press("Enter")

    def _send_point(self) -> list[float]:
        box = self.page.locator("form button").first.bounding_box()
        assert box is not None
        return [box["x"] + box["width"] / 2, box["y"] + box["height"] / 2]

    def wait_reply(self, n: int) -> None:
        deadline = time.monotonic() + 25
        while self.agent_messages() < self.baseline + n and time.monotonic() < deadline:
            self.page.wait_for_timeout(120)
        self.page.wait_for_timeout(700)

    def run(self, action: tuple[Any, ...]) -> None:
        kind = action[0]
        if kind == "type":
            self.type(action[1])
        elif kind == "wait_reply":
            self.wait_reply(action[1])
        elif kind == "click":
            self.baseline = self.agent_messages()
            self.click(action[1])
        elif kind == "hover":
            self.move(action[1])
        elif kind == "highlight":
            self.page.evaluate("n => window.__highlight(n)", action[1])
            self.page.wait_for_timeout(900)
        elif kind == "unhighlight":
            self.page.evaluate("() => window.__unhighlight()")
        elif kind == "zoom":
            self.page.evaluate("([s, k]) => window.__zoom(s, k)", [action[1], action[2]])
            self.page.wait_for_timeout(1100)
        elif kind == "chip":
            self.page.evaluate("t => window.__chip(t)", action[1])
        elif kind == "pause":
            self.page.wait_for_timeout(int(action[1] * 1000))
        else:
            raise ValueError(kind)


# ----------------------------------------------------------------------------- recording


def eval_lines() -> list[str]:
    text = (ROOT / "evals" / "RESULTS.md").read_text(encoding="utf-8")
    lines = ["$ make eval", "+ python -m evals.report"]
    for row in re.findall(r"^\| ([0-9a-z-]+) \| ([^|]+) \|.*?\| (PASS|FAIL) \|", text, re.M):
        lines.append(f"{row[2]}  {row[0]:>6}  {row[1].strip()}")
    total = re.search(r"\*\*(\d+/\d+) passed\*\*", text)
    lines.append(f"{total.group(1) if total else ''} passed")
    return lines


def record_scene(
    browser: Any, scene: Scene, durs: list[float], work: Path
) -> tuple[Path, list[float], float]:
    vdir = work / f"rec-{scene.sid}"
    shutil.rmtree(vdir, ignore_errors=True)
    proc = None
    if scene.kind == "app":
        proc, url = start_app(scene.app_scenario, work)
    else:
        url = (HERE / "deck.html").as_uri() + f"?s={scene.sid}"
    ctx = browser.new_context(
        viewport={"width": W, "height": H},
        record_video_dir=str(vdir),
        record_video_size={"width": W, "height": H},
    )
    t0 = time.monotonic()
    ctx.add_init_script(path=str(HERE / "overlay.js"))
    page = ctx.new_page()
    page.goto(url)
    page.wait_for_load_state("networkidle")
    if scene.sid == "evals":
        page.evaluate("l => { window.__evalLines = l; }", eval_lines())
    if scene.chip:
        page.evaluate("t => window.__chip(t)", scene.chip)
    driver = Driver(page)
    page.wait_for_timeout(900)
    offsets: list[float] = []
    prev_end = 1.0
    for i, line in enumerate(scene.lines):
        now = time.monotonic() - t0
        start = max(now, prev_end + GAP_S)
        page.wait_for_timeout(int(max(0.0, start - now) * 1000))
        start = time.monotonic() - t0
        page.evaluate("t => window.__caption(t)", line.text)
        if scene.kind == "deck":
            page.evaluate("i => window.__reveal(i)", i)
        offsets.append(start)
        for action in line.actions:
            driver.run(action)
        prev_end = start + durs[i]
    end = max(time.monotonic() - t0, prev_end) + scene.tail_s
    page.wait_for_timeout(int(max(0.0, end - (time.monotonic() - t0)) * 1000))
    page.evaluate("() => window.__caption('')")
    page.wait_for_timeout(400)
    ctx.close()
    if proc is not None:
        proc.terminate()
    webm = next(vdir.glob("*.webm"))
    return webm, offsets, end


# ----------------------------------------------------------------------------- assembly


def ff(*args: str) -> None:
    subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", *args], check=True)


def assemble_scene(
    webm: Path, wavs: list[Path], offsets: list[float], end: float, out: Path
) -> None:
    length = end - LEAD_TRIM_S
    inputs: list[str] = ["-ss", f"{LEAD_TRIM_S}", "-t", f"{length:.3f}", "-i", str(webm)]
    chains = []
    for k, (wav, off) in enumerate(zip(wavs, offsets, strict=True), start=1):
        inputs += ["-i", str(wav)]
        delay = max(0, int((off - LEAD_TRIM_S) * 1000))
        chains.append(f"[{k}:a]adelay={delay}|{delay},aformat=channel_layouts=stereo[a{k}]")
    mix = "".join(f"[a{k}]" for k in range(1, len(wavs) + 1))
    fade_out = max(0.0, length - 0.5)
    graph = ";".join(
        [
            *chains,
            f"{mix}amix=inputs={len(wavs)}:normalize=0,apad,atrim=0:{length:.3f}[aud]",
            f"[0:v]fps={FPS},scale={W}:{H},format=yuv420p,"
            f"fade=t=in:st=0:d=0.45,fade=t=out:st={fade_out:.3f}:d=0.5[vid]",
        ]
    )
    ff(*inputs, "-filter_complex", graph, "-map", "[vid]", "-map", "[aud]",
       "-c:v", "libx264", "-preset", "medium", "-crf", "24", "-c:a", "aac", "-b:a", "160k",
       "-ar", "48000", str(out))  # fmt: skip


def finalize(parts: list[Path], work: Path) -> Path:
    OUT.mkdir(parents=True, exist_ok=True)
    listing = work / "concat.txt"
    listing.write_text("".join(f"file '{p.as_posix()}'\n" for p in parts), encoding="utf-8")
    joined = work / "joined.mp4"
    ff("-f", "concat", "-safe", "0", "-i", str(listing), "-c", "copy", str(joined))
    probe = subprocess.run([FFMPEG, "-i", str(joined)], capture_output=True, text=True, check=False)
    m = re.search(r"Duration: (\d+):(\d+):([\d.]+)", probe.stderr)
    assert m, probe.stderr
    total = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    # A quiet ambient bed (a slow A-minor pad) under the narration.
    pad = (
        "aevalsrc='0.5*sin(2*PI*110*t)*(0.6+0.4*sin(2*PI*0.07*t))"
        "+0.35*sin(2*PI*164.81*t)*(0.6+0.4*sin(2*PI*0.05*t+1))"
        "+0.3*sin(2*PI*220*t)*(0.6+0.4*sin(2*PI*0.09*t+2))"
        f"+0.2*sin(2*PI*261.63*t)*(0.5+0.5*sin(2*PI*0.04*t+3))':s=48000:d={total:.2f}"
    )
    final = OUT / "errand-agent-demo.mp4"
    graph = (
        f"[1:a]lowpass=f=900,volume=0.035,afade=t=in:d=3,afade=t=out:st={total - 3:.2f}:d=3,"
        "aformat=channel_layouts=stereo[bed];[0:a][bed]amix=inputs=2:normalize=0[a]"
    )
    ff("-i", str(joined), "-f", "lavfi", "-i", pad, "-filter_complex", graph,
       "-map", "0:v", "-map", "[a]", "-c:v", "copy", "-c:a", "aac", "-b:a", "160k",
       "-movflags", "+faststart", str(final))  # fmt: skip
    return final


def previews(final: Path, booking_part: Path) -> None:
    ff("-ss", "3", "-i", str(final), "-frames:v", "1", str(OUT / "poster.png"))
    gif = OUT / "errand-agent-demo.gif"
    graph = (
        "fps=9,scale=900:-1:flags=lanczos,split[a][b];[a]palettegen=max_colors=128[p];"
        "[b][p]paletteuse=dither=bayer:bayer_scale=4"
    )
    ff("-ss", "4", "-t", "26", "-i", str(booking_part), "-vf", graph, str(gif))


def main() -> int:
    work = Path(os.environ.get("VIDEO_WORK") or tempfile.mkdtemp(prefix="errand-video-"))
    work.mkdir(parents=True, exist_ok=True)
    print(f"work dir: {work}")
    parts: list[Path] = []
    booking_part: Path | None = None
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="msedge")
        only = set(filter(None, os.environ.get("VIDEO_ONLY", "").split(",")))
        redo = set(filter(None, os.environ.get("VIDEO_REDO", "").split(",")))
        for scene in (s for s in SCENES if not only or s.sid in only):
            part = work / f"part-{len(parts):02d}-{scene.sid}.mp4"
            # Re-use a scene rendered earlier in the same work dir unless asked to redo it.
            if not (redo and scene.sid not in redo and part.exists()):
                wavs = [work / f"{scene.sid}-{i:02d}.wav" for i in range(len(scene.lines))]
                durs = [synthesize(line.text, w) for line, w in zip(scene.lines, wavs, strict=True)]
                webm, offsets, end = record_scene(browser, scene, durs, work)
                assemble_scene(webm, wavs, offsets, end, part)
                print(f"scene {scene.sid}: {end:.1f}s")
            parts.append(part)
            if scene.sid == "booking":
                booking_part = part
        browser.close()
    final = finalize(parts, work)
    if booking_part is not None:
        previews(final, booking_part)
    print(f"wrote {final}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
