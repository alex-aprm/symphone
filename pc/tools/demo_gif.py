"""Short README demo GIF of the running Windows app, with personal details erased (see screenshots.py).

    python tools/demo_gif.py <out.gif>

A tour of the main window (Status with sample notifications, Photos, Files, Settings, Side display), then
several real phone apps in their own Symphone windows at once, one of them operated from the PC: the viewer page's
test hook sends taps, the phone app reacts.
"""
import asyncio
import base64
import io
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import aiohttp
from PIL import Image, ImageChops, ImageDraw, ImageFilter

sys.path.insert(0, str(Path(__file__).resolve().parent))
import screenshots as S     # noqa: E402  (BLUR, SAMPLE_NOTIFICATIONS, FILES_PREP, viewer())

W, H = 960, 620             # main window (CSS px): small enough that the GIF shows it at its real size
DSF = 2                     # captured (and kept) at 2x
FPS = 8
APPS = [("zte.com.cn.alarmclock", "Clock"), ("com.vincentlee.compass", "Compass"),
        ("com.google.android.calculator", "Calculator")]            # the last one is operated
# personal details inside app pictures (normalized rectangles of the viewer page): the compass's street address
APP_BLUR = {"com.vincentlee.compass": [(0.08, 0.16, 0.92, 0.215)]}
STOPWATCH_TAB, STOPWATCH_START = (0.65, 0.93), (0.30, 0.82)          # Clock: show a running stopwatch, not alarms
STOPWATCH_RESET = (0.69, 0.82)
STOPWATCH_DIGITS = (0.15, 0.22, 0.85, 0.33)           # where the time is, in the viewer page

# Calculator keys (normalized in the app's picture): columns and rows of the Google Calculator keypad
COLS = (0.129, 0.376, 0.624, 0.871)
ROWS = (0.459, 0.572, 0.686, 0.800, 0.915)
KEYS = {"AC": (0, 0), "7": (0, 1), "8": (1, 1), "9": (2, 1), "×": (3, 1), "4": (0, 2), "5": (1, 2), "6": (2, 2),
        "1": (0, 3), "2": (1, 3), "3": (2, 3), "+": (3, 3), "0": (0, 4), "=": (3, 4)}
T0 = time.time()


def say(*a):
    print(f"[{time.time() - T0:5.1f}s]", *a, flush=True)


class Page:
    def __init__(self, ws):
        self.ws, self.n = ws, 0

    async def cdp(self, method, timeout=30, **params):
        self.n += 1
        await self.ws.send_str(json.dumps({"id": self.n, "method": method, "params": params}))
        async with asyncio.timeout(timeout):
            while True:
                m = json.loads((await self.ws.receive()).data)
                if m.get("id") == self.n:
                    return m.get("result", {})

    async def js(self, expr):
        return await self.cdp("Runtime.evaluate", expression=expr, awaitPromise=True)

    async def shot(self) -> Image.Image:
        r = await self.cdp("Page.captureScreenshot", format="png")
        return Image.open(io.BytesIO(base64.b64decode(r["data"]))).convert("RGB")

    async def blur(self):
        await self.js("(() => { const s = document.createElement('style'); s.textContent = "
                      + json.dumps(S.BLUR) + "; document.head.append(s); })()")


async def new_page(http, size):
    async with http.put(f"http://127.0.0.1:{S.PORT}/json/new?about:blank") as r:
        t = await r.json()
    ws = await http.ws_connect(t["webSocketDebuggerUrl"], max_msg_size=1 << 28)
    p = Page(ws)
    await p.cdp("Emulation.setDeviceMetricsOverride", width=size[0], height=size[1], deviceScaleFactor=DSF, mobile=False)
    return p


async def tap(page: Page, x: float, y: float):
    """A tap through the viewer's own input path (normalized video coordinates)."""
    await page.js(f"__screen.send({{t:'touch',a:0,id:0,x:{x},y:{y},p:1}}); "
                  f"setTimeout(() => __screen.send({{t:'touch',a:1,id:0,x:{x},y:{y},p:0}}), 70)")


async def stopwatch(page: Page, run: bool):
    """From whatever state the phone's stopwatch is in: stop and reset it, then start it if `run`. Running is told by
    the digits changing (its start/pause button and its reset/lap button swap meaning)."""
    async def digits():
        img = await page.shot()
        r = STOPWATCH_DIGITS
        return img.crop((int(r[0] * img.width), int(r[1] * img.height), int(r[2] * img.width), int(r[3] * img.height)))
    a = await digits()
    await asyncio.sleep(0.5)
    if ImageChops.difference(a, await digits()).getbbox():
        await tap(page, *STOPWATCH_START)                       # pause
        await asyncio.sleep(0.6)
    await tap(page, *STOPWATCH_RESET)
    await asyncio.sleep(0.6)
    if run:
        await tap(page, *STOPWATCH_START)
        await asyncio.sleep(0.8)


def shadowed(bg: Image.Image, win: Image.Image, x: int, y: int):
    pad = 24 * DSF
    sh = Image.new("RGBA", (win.width + 2 * pad, win.height + 2 * pad), (0, 0, 0, 0))
    ImageDraw.Draw(sh).rounded_rectangle((pad, pad, win.width + pad, win.height + pad), 8 * DSF, fill=(0, 0, 0, 160))
    sh = sh.filter(ImageFilter.GaussianBlur(10 * DSF))
    bg.paste(sh, (x - pad, y - pad + 6 * DSF), sh)
    bg.paste(win, (x, y))


async def main(out: Path):
    frames: list[Image.Image] = []
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as prof:
        edge = subprocess.Popen([S.EDGE, "--headless=new", "--hide-scrollbars", "--force-dark-mode",
                                 f"--remote-debugging-port={S.PORT}", f"--user-data-dir={prof}", "about:blank"])
        try:
            async with aiohttp.ClientSession() as http:
                for _ in range(50):
                    try:
                        async with http.get(f"http://127.0.0.1:{S.PORT}/json/version"):
                            break
                    except aiohttp.ClientError:
                        await asyncio.sleep(0.2)
                m = await new_page(http, (W, H))

                async def tour(tab, prep="", seconds=1.6):
                    await m.cdp("Page.navigate", url=f"{S.APP}/#{tab}")
                    await asyncio.sleep(3.5)
                    if prep:
                        await m.js(prep)
                    await m.blur()
                    await asyncio.sleep(0.6)
                    img = await m.shot()
                    frames.extend([img] * int(seconds * FPS))
                    say("captured", tab)
                    return img

                status = await tour("status", S.SAMPLE_NOTIFICATIONS, 2.4)
                await tour("photos", "", 1.4)
                await tour("files", S.FILES_PREP, 1.4)
                await tour("settings", "", 1.8)
                await tour("display", "", 1.8)

                # several phone apps, each in its own Symphone window, over the main window
                apps = []
                for pkg, label in APPS:
                    p = await new_page(http, (420, 900))
                    await p.cdp("Page.navigate", url=S.viewer(pkg, label))
                    apps.append(p)
                    say("opening", label)
                    await asyncio.sleep(2)
                await asyncio.sleep(7)
                await tap(apps[0], *STOPWATCH_TAB)
                await asyncio.sleep(1.2)
                await stopwatch(apps[0], run=True)
                bg = status.filter(ImageFilter.GaussianBlur(2 * DSF))
                wh = (H - 50) * DSF
                calc = apps[-1]
                still = {}

                async def app_frame(hold=1, live=(len(APPS) - 1,)):
                    f = bg.copy()
                    for i, p in enumerate(apps):
                        if i in live or i not in still:
                            img = await p.shot()
                            for r in APP_BLUR.get(APPS[i][0], ()):
                                box = (int(r[0] * img.width), int(r[1] * img.height), int(r[2] * img.width), int(r[3] * img.height))
                                img.paste(img.crop(box).filter(ImageFilter.GaussianBlur(14 * DSF)), box[:2])
                            still[i] = img.resize((int(img.width * wh / img.height), wh), Image.LANCZOS)
                        win = still[i]
                        # cascade from the right edge, the operated app in front
                        x = f.width - win.width - (40 + (len(apps) - 1 - i) * 282) * DSF
                        shadowed(f, win, x, 25 * DSF)
                    frames.extend([f] * hold)

                for _ in range(2 * FPS // 3):                              # all live: the stopwatch runs
                    await app_frame(3, live=range(len(apps)))
                say("apps shown")
                for k in ("AC", "1", "2", "×", "3", "="):
                    cx, cy = COLS[KEYS[k][0]], ROWS[KEYS[k][1]]
                    await tap(calc, cx, cy)
                    for _ in range(3):
                        await asyncio.sleep(0.12)
                        await app_frame(1, live=(0, len(apps) - 1))
                say("typed 12 × 3 =")
                await asyncio.sleep(0.8)
                for _ in range(int(2.5 * FPS) // 3):
                    await app_frame(3, live=range(len(apps)))
                await tap(apps[0], *STOPWATCH_START)                    # it runs (started above): pause, reset
                await asyncio.sleep(0.8)
                await tap(apps[0], *STOPWATCH_RESET)
                await asyncio.sleep(0.8)
                for p in apps:
                    await p.cdp("Page.navigate", url="about:blank")      # ends the app sessions
                await asyncio.sleep(1)
        finally:
            edge.kill()
            edge.wait(10)

    say(len(frames), "frames; encoding")
    # kept at the 2x capture size: the GIF shows the app twice its normal size, readable on any screen
    pal = [f.quantize(colors=200, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE) for f in frames]
    pal[0].save(out, save_all=True, append_images=pal[1:], duration=int(1000 / FPS), loop=0, optimize=True)
    say(f"{out}: {len(pal)} frames, {out.stat().st_size // 1024} KB")


if __name__ == "__main__":
    asyncio.run(main(Path(sys.argv[1])))
