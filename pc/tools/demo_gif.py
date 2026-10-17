"""Short README demo GIF of the running Windows app, with personal details erased (see screenshots.py).

    python tools/demo_gif.py <out.gif>

A quick tour (Status with sample notifications, Photos, Files) and then a real phone app (Calculator) in a Symphone
window, operated from the PC: the viewer page's test hook sends taps, the phone app reacts.
"""
import asyncio
import base64
import io
import json
import subprocess
import sys
import tempfile
from pathlib import Path

import aiohttp
from PIL import Image, ImageDraw, ImageFilter

sys.path.insert(0, str(Path(__file__).resolve().parent))
import screenshots as S     # noqa: E402  (BLUR, SAMPLE_NOTIFICATIONS, viewer())

W, H = 1320, 860           # main window (CSS px)
OUT_W = 900                 # GIF width
FPS = 8

# Calculator keys (normalized in the app's picture): columns and rows of the Google Calculator keypad
COLS = (0.129, 0.376, 0.624, 0.871)
ROWS = (0.459, 0.572, 0.686, 0.800, 0.915)
KEYS = {"AC": (0, 0), "7": (0, 1), "8": (1, 1), "9": (2, 1), "×": (3, 1), "4": (0, 2), "5": (1, 2), "6": (2, 2),
        "1": (0, 3), "2": (1, 3), "3": (2, 3), "+": (3, 3), "0": (0, 4), "=": (3, 4)}


class Page:
    def __init__(self, ws):
        self.ws, self.n = ws, 0

    async def cdp(self, method, **params):
        self.n += 1
        await self.ws.send_str(json.dumps({"id": self.n, "method": method, "params": params}))
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


async def open_page(http, port):
    async with http.put(f"http://127.0.0.1:{port}/json/new?about:blank") as r:
        return await r.json()


async def main(out: Path):
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as prof:
        edge = subprocess.Popen([S.EDGE, "--headless=new", "--hide-scrollbars", "--force-dark-mode",
                                 f"--remote-debugging-port={S.PORT}", f"--user-data-dir={prof}", "about:blank"])
        frames: list[Image.Image] = []
        try:
            async with aiohttp.ClientSession() as http:
                for _ in range(50):
                    try:
                        async with http.get(f"http://127.0.0.1:{S.PORT}/json/version"):
                            break
                    except aiohttp.ClientError:
                        await asyncio.sleep(0.2)
                main_t, app_t = await open_page(http, S.PORT), await open_page(http, S.PORT)
                async with http.ws_connect(main_t["webSocketDebuggerUrl"], max_msg_size=1 << 28) as w1, \
                        http.ws_connect(app_t["webSocketDebuggerUrl"], max_msg_size=1 << 28) as w2:
                    m, a = Page(w1), Page(w2)
                    await m.cdp("Emulation.setDeviceMetricsOverride", width=W, height=H, deviceScaleFactor=1, mobile=False)
                    await a.cdp("Emulation.setDeviceMetricsOverride", width=420, height=900, deviceScaleFactor=1, mobile=False)

                    async def tour(tab, prep="", seconds=1.6):
                        await m.cdp("Page.navigate", url=f"{S.APP}/#{tab}")
                        await asyncio.sleep(4)
                        if prep:
                            await m.js(prep)
                        await m.blur()
                        await asyncio.sleep(0.6)
                        img = await m.shot()
                        frames.extend([img] * int(seconds * FPS))
                        return img

                    status = await tour("status", S.SAMPLE_NOTIFICATIONS, 2.2)
                    await tour("photos", "", 1.3)
                    await tour("files", S.FILES_PREP, 1.3)

                    # a real phone app in a Symphone window, over the main window
                    await a.cdp("Page.navigate", url=S.viewer("com.google.android.calculator", "Calculator"))
                    await asyncio.sleep(9)
                    bg = status.filter(ImageFilter.GaussianBlur(1.5))

                    async def app_frame(hold=1):
                        img = await a.shot()
                        f = bg.copy()
                        win = img.resize((int(img.width * (H - 80) / img.height), H - 80), Image.LANCZOS)
                        x, y = W - win.width - 70, 40
                        sh = Image.new("RGBA", (win.width + 40, win.height + 40), (0, 0, 0, 0))
                        ImageDraw.Draw(sh).rounded_rectangle((20, 20, win.width + 20, win.height + 20), 12, fill=(0, 0, 0, 150))
                        f.paste(sh.filter(ImageFilter.GaussianBlur(14)), (x - 20, y - 12), sh.filter(ImageFilter.GaussianBlur(14)))
                        f.paste(win, (x, y))
                        frames.extend([f] * hold)

                    await app_frame(FPS)
                    for k in ("AC", "1", "2", "×", "3", "="):
                        cx, cy = COLS[KEYS[k][0]], ROWS[KEYS[k][1]]
                        await a.js(f"__screen.send({{t:'touch',a:0,id:0,x:{cx},y:{cy},p:1}}); "
                                   f"setTimeout(() => __screen.send({{t:'touch',a:1,id:0,x:{cx},y:{cy},p:0}}), 70)")
                        for _ in range(4):
                            await asyncio.sleep(0.12)
                            await app_frame(1)
                    await asyncio.sleep(0.8)
                    await app_frame(int(2.5 * FPS))
                    await a.cdp("Page.navigate", url="about:blank")      # ends the app session
                    await asyncio.sleep(1)
        finally:
            edge.kill()
            edge.wait(10)

    small = [f.resize((OUT_W, int(f.height * OUT_W / f.width)), Image.LANCZOS) for f in frames]
    pal = [f.quantize(colors=200, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE) for f in small]
    pal[0].save(out, save_all=True, append_images=pal[1:], duration=int(1000 / FPS), loop=0, optimize=True)
    print(f"{out}: {len(pal)} frames, {out.stat().st_size // 1024} KB")


if __name__ == "__main__":
    asyncio.run(main(Path(sys.argv[1])))
