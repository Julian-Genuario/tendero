"""Builds video/demo.mp4 with no human on camera or microphone.

1. Synthesises the narration (video/narration.json) with edge-tts, keeping sentence timings.
2. Starts Tendero locally (offline demo brain + local PayPal simulator, fresh database).
3. Drives a headless Chromium with Playwright, recording the screen, scene by scene, so
   that each scene lasts as long as its narration.
4. Mixes the voice-over at the recorded scene start times, burns subtitles and encodes an
   H.264/AAC mp4 with ffmpeg.

    python video/make_video.py            # needs: pip install -r requirements-dev.txt,
                                          # python -m playwright install chromium, ffmpeg on PATH
"""
from __future__ import annotations

import asyncio
import json
import re
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import edge_tts
from playwright.async_api import Page, async_playwright

ROOT = Path(__file__).resolve().parent.parent
VID = ROOT / "video"
BUILD = VID / "build"
PORT = int(os.environ.get("VIDEO_PORT", "8020"))
BASE = f"http://127.0.0.1:{PORT}"
SLIDES = (VID / "slides" / "slides.html").as_uri()
FFMPEG = os.environ.get("FFMPEG", shutil.which("ffmpeg") or "ffmpeg")
FFPROBE = os.environ.get("FFPROBE", shutil.which("ffprobe") or "ffprobe")
TAIL = 0.45  # silence after each narration segment
VW, VH, OUT_H = 1280, 656, 720  # browser area + a bottom bar for subtitles


# ---------------------------------------------------------------- narration
async def synth(text: str, voice: str, rate: str, out: Path) -> list[dict]:
    sentences = []
    com = edge_tts.Communicate(text, voice, rate=rate)
    with out.open("wb") as f:
        async for chunk in com.stream():
            if chunk["type"] == "audio":
                f.write(chunk["data"])
            elif chunk["type"] == "SentenceBoundary":
                sentences.append({"start": chunk["offset"] / 1e7, "dur": chunk["duration"] / 1e7,
                                  "text": chunk["text"]})
    return sentences


def duration(path: Path) -> float:
    out = subprocess.run([FFPROBE, "-v", "error", "-show_entries", "format=duration", "-of",
                          "default=nw=1:nk=1", str(path)], capture_output=True, text=True, check=True)
    return float(out.stdout.strip())


async def build_audio(cfg: dict) -> list[dict]:
    adir = BUILD / "audio"
    adir.mkdir(parents=True, exist_ok=True)
    segs = []
    for i, s in enumerate(cfg["segments"]):
        mp3 = adir / f"{i:02d}_{s['id']}.mp3"
        sents = await synth(s["text"], cfg["voice"], cfg.get("rate", "+0%"), mp3)
        segs.append({**s, "mp3": mp3, "dur": duration(mp3), "sentences": sents})
    total = sum(s["dur"] + TAIL for s in segs)
    print(f"narration: {len(segs)} segments, {total:.1f}s of speech+pauses")
    return segs


# ---------------------------------------------------------------- app server
def start_server() -> subprocess.Popen:
    db = BUILD / "demo.sqlite3"
    if db.exists():
        db.unlink()
    env = {**os.environ, "PORT": str(PORT), "HOST": "127.0.0.1", "PUBLIC_BASE_URL": BASE,
           "TENDERO_DB": str(db), "PAYPAL_MODE": "mock", "LLM_PROVIDER": "scripted",
           "MOCK_WEBHOOK_DELAY": "2.2", "LOG_LEVEL": "WARNING", "PYTHONIOENCODING": "utf-8"}
    proc = subprocess.Popen([sys.executable, "-m", "tendero.main"], cwd=ROOT, env=env)
    for _ in range(60):
        try:
            urllib.request.urlopen(BASE + "/api/config", timeout=1)
            return proc
        except Exception:
            time.sleep(0.5)
    proc.kill()
    raise RuntimeError("server did not start")


# ---------------------------------------------------------------- browser helpers
CURSOR_JS = """
(() => {
  if (window.top !== window) return;
  const add = () => {
    if (document.getElementById('__cur')) return;
    const c = document.createElement('div');
    c.id = '__cur';
    c.style.cssText = 'position:fixed;left:640px;top:360px;width:18px;height:18px;margin:-3px 0 0 -3px;z-index:99999;' +
      'pointer-events:none;transition:left .55s cubic-bezier(.3,.7,.3,1),top .55s cubic-bezier(.3,.7,.3,1);';
    c.innerHTML = '<svg width="18" height="22" viewBox="0 0 18 22"><path d="M1 1 L1 17 L5.5 13 L8.5 20 L11 19 L8 12 L14 12 Z" fill="#111" stroke="#fff" stroke-width="1.4"/></svg>';
    document.body.appendChild(c);
  };
  window.__cursor = (x, y) => { add(); const c = document.getElementById('__cur'); c.style.left = x + 'px'; c.style.top = y + 'px'; };
  window.__ripple = (x, y) => {
    const r = document.createElement('div');
    r.style.cssText = `position:fixed;left:${x-14}px;top:${y-14}px;width:28px;height:28px;border-radius:50%;` +
      'border:2px solid #0f766e;z-index:99998;pointer-events:none;transition:all .45s ease-out;opacity:.9';
    document.body.appendChild(r);
    requestAnimationFrame(() => { r.style.transform = 'scale(1.8)'; r.style.opacity = '0'; });
    setTimeout(() => r.remove(), 600);
  };
  document.addEventListener('DOMContentLoaded', add);
})();
"""


class Director:
    def __init__(self, page: Page):
        self.page = page

    async def point(self, locator, settle: float = 0.6):
        await locator.wait_for(state="visible")
        box = await locator.bounding_box()
        x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
        await self.page.evaluate("([x,y]) => window.__cursor && window.__cursor(x,y)", [x, y])
        await asyncio.sleep(settle)
        return x, y

    async def click(self, locator, settle: float = 0.6):
        x, y = await self.point(locator, settle)
        await self.page.evaluate("([x,y]) => window.__ripple && window.__ripple(x,y)", [x, y])
        await locator.click()

    async def type(self, selector: str, text: str, delay: int = 32):
        loc = self.page.locator(selector)
        await self.click(loc, 0.5)
        await loc.press_sequentially(text, delay=delay)
        await asyncio.sleep(0.35)
        await loc.press("Enter")


# ---------------------------------------------------------------- scenes
async def sc_title(d: Director, dur: float):
    await d.page.goto(SLIDES + "#title")


async def sc_problem(d: Director, dur: float):
    await d.page.goto(SLIDES + "#problem")


async def sc_overview(d: Director, dur: float):
    await d.page.goto(BASE + "/")
    await d.page.wait_for_selector("#llmBadge.demo")
    await asyncio.sleep(dur * 0.22)
    await d.point(d.page.locator("#custThread"))
    await asyncio.sleep(dur * 0.18)
    await d.point(d.page.locator("#ordersGrid"))
    await asyncio.sleep(dur * 0.2)
    await d.point(d.page.locator("#llmBadge"))


async def sc_ask(d: Director, dur: float):
    await d.type("#custInput", "Hi! Do you ship to Madrid? I'd like 2 mate gourds and a bombilla", delay=28)
    await d.page.wait_for_selector("text=Shall I send you")
    await d.point(d.page.locator("#custThread .msg.agent .tools").last)


async def sc_confirm(d: Director, dur: float):
    await asyncio.sleep(0.6)
    await d.type("#custInput", "Yes, send me the link")
    await d.page.wait_for_selector(".paybtn")
    await asyncio.sleep(1.0)
    await d.point(d.page.locator(".ag-row").first)


async def sc_checkout(d: Director, dur: float):
    t = time.monotonic()
    await d.click(d.page.locator(".paybtn").last)
    approve = d.page.frame_locator("#checkoutFrame").locator("#approve")
    await approve.wait_for()
    await d.point(d.page.frame_locator("#checkoutFrame").locator("table"))
    await asyncio.sleep(max(0.5, dur - (time.monotonic() - t) - 1.6))
    await d.click(approve, 0.7)


async def sc_webhook(d: Director, dur: float):
    await d.page.wait_for_selector("#checkout", state="hidden", timeout=15000)
    await d.point(d.page.locator("#timeline"))
    await d.page.wait_for_selector(".paidcard", timeout=20000)
    await asyncio.sleep(1.2)
    await d.point(d.page.locator(".paidcard"))
    await asyncio.sleep(1.6)
    await d.point(d.page.locator("#kNet"))


async def sc_forged(d: Director, dur: float):
    await asyncio.sleep(1.2)
    await d.click(d.page.locator("#forgeBtn"))
    await asyncio.sleep(1.6)
    await d.point(d.page.locator("#timeline li.bad").last)
    await asyncio.sleep(1.5)
    await d.point(d.page.locator("#kRejected"))


async def sc_copilot(d: Director, dur: float):
    await asyncio.sleep(0.4)
    await d.type("#mInput", "How did we do today? Any issues?")
    await d.page.wait_for_selector("#mThread .msg.agent")
    await asyncio.sleep(0.6)
    await d.point(d.page.locator("#mThread .msg.agent").last)


async def sc_refund(d: Director, dur: float):
    await asyncio.sleep(0.6)
    await d.type("#mInput", "Refund the last order - the gourd arrived cracked")
    await d.page.wait_for_selector("text=refund_order")
    await asyncio.sleep(0.6)
    await d.page.wait_for_selector("text=refund of 60.00 USD", timeout=20000)
    await d.point(d.page.locator("#custThread .msg.agent").last)
    await asyncio.sleep(1.0)
    await d.point(d.page.locator(".pill.REFUNDED"))


async def sc_arch(d: Director, dur: float):
    await d.page.goto(SLIDES + "#arch")


async def sc_close(d: Director, dur: float):
    await d.page.goto(SLIDES + "#close")


SCENES = {k[3:]: v for k, v in globals().items() if k.startswith("sc_")}


async def record(segs: list[dict]) -> tuple[Path, list[float], float]:
    raw = BUILD / "raw"
    shutil.rmtree(raw, ignore_errors=True)
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        ctx = await browser.new_context(viewport={"width": VW, "height": VH}, device_scale_factor=1,
                                        record_video_dir=str(raw), record_video_size={"width": VW, "height": VH})
        await ctx.add_init_script(CURSOR_JS)
        page = await ctx.new_page()
        t0 = time.monotonic()
        d = Director(page)
        # warm-up: load fonts/AG Grid once so the first scenes do not flash
        await page.goto(SLIDES + "#title")
        await page.wait_for_timeout(1200)
        starts = []
        for s in segs:
            start = time.monotonic() - t0
            starts.append(start)
            target = s["dur"] + TAIL
            try:
                await asyncio.wait_for(SCENES[s["id"]](d, s["dur"]), timeout=max(target, 25))
            except asyncio.TimeoutError:
                print(f"WARNING: scene {s['id']} overran")
            spent = time.monotonic() - t0 - start
            if spent < target:
                await asyncio.sleep(target - spent)
            print(f"  scene {s['id']:<9} start {start:6.2f}s  audio {s['dur']:5.2f}s  took {time.monotonic()-t0-start:5.2f}s")
        end = time.monotonic() - t0 + 0.8
        await page.wait_for_timeout(800)
        video_path = await page.video.path()
        await ctx.close()
        await browser.close()
    return Path(video_path), starts, end


# ---------------------------------------------------------------- muxing
def srt_time(t: float) -> str:
    ms = int(round(t * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def chunks(text: str, limit: int = 92) -> list[str]:
    """Split a sentence at clause boundaries into subtitle-sized pieces."""
    if len(text) <= limit:
        return [text]
    parts = re.split(r"(?<=[,;:])\s+", text)
    out, cur = [], ""
    for part in parts:
        if cur and len(cur) + 1 + len(part) > limit:
            out.append(cur)
            cur = part
        else:
            cur = f"{cur} {part}".strip()
    out.append(cur)
    final = []
    for c in out:  # hard-wrap anything still too long
        while len(c) > limit:
            cut = c.rfind(" ", 0, limit)
            final.append(c[:cut])
            c = c[cut + 1:]
        final.append(c)
    return final


def write_srt(segs: list[dict], starts: list[float], path: Path) -> None:
    cues = []
    for s, st in zip(segs, starts):
        sents = s["sentences"] or [{"start": 0, "dur": s["dur"], "text": s["text"]}]
        for i, sent in enumerate(sents):
            a = st + sent["start"]
            b = st + (sents[i + 1]["start"] if i + 1 < len(sents) else sent["start"] + sent["dur"] + 0.2)
            pieces = chunks(sent["text"])
            total = sum(len(p) for p in pieces)
            t = a
            for p in pieces:
                d = (b - a) * len(p) / total
                cues.append((t, t + d - 0.04, p))
                t += d
    with path.open("w", encoding="utf-8") as f:
        for n, (a, b, t) in enumerate(cues, 1):
            f.write(f"{n}\n{srt_time(a)} --> {srt_time(b)}\n{t}\n\n")


def mux(raw: Path, segs: list[dict], starts: list[float], end: float, out: Path) -> None:
    srt = BUILD / "subs.srt"
    write_srt(segs, starts, srt)
    inputs = ["-i", str(raw)]
    for s in segs:
        inputs += ["-i", str(s["mp3"])]
    delays = ";".join(f"[{i+1}:a]adelay={int(st*1000)}|{int(st*1000)}[a{i}]" for i, st in enumerate(starts))
    mix = "".join(f"[a{i}]" for i in range(len(segs))) + f"amix=inputs={len(segs)}:normalize=0,apad[aout]"
    style = ("FontName=Arial,FontSize=8.6,PrimaryColour=&H00F2F2F2,BorderStyle=1,Outline=0,Shadow=0,"
             "Alignment=2,MarginV=7")
    vf = (f"[0:v]fps=30,pad={VW}:{OUT_H}:0:0:color=0x14181f,"
          f"subtitles=subs.srt:force_style='{style}',format=yuv420p[vout]")
    cmd = [FFMPEG, "-y", "-hide_banner", "-loglevel", "error", *inputs,
           "-filter_complex", f"{delays};{mix};{vf}", "-map", "[vout]", "-map", "[aout]",
           "-t", f"{end:.2f}", "-c:v", "libx264", "-preset", "medium", "-crf", "20",
           "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", str(out)]
    subprocess.run(cmd, cwd=BUILD, check=True)


async def main() -> None:
    BUILD.mkdir(parents=True, exist_ok=True)
    cfg = json.loads((VID / "narration.json").read_text(encoding="utf-8"))
    segs = await build_audio(cfg)
    server = start_server()
    try:
        raw, starts, end = await record(segs)
    finally:
        server.terminate()
    out = VID / "demo.mp4"
    mux(raw, segs, starts, end, out)
    shutil.copy(BUILD / "subs.srt", VID / "demo.en.srt")
    print(f"done: {out}  ({duration(out):.1f}s)")


if __name__ == "__main__":
    asyncio.run(main())
