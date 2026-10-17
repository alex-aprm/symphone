"""README screenshots of the running Windows app (http://127.0.0.1:47801), with personal details blurred.

    python tools/screenshots.py <out dir>

Drives headless Microsoft Edge over the DevTools protocol (nothing appears on screen). Before each capture the
phone's name, notification senders/texts, the PC name and similar personal text are blurred with CSS.
"""
import asyncio
import base64
import json
import pathlib
import subprocess
import sys
import tempfile

import aiohttp

EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
PORT = 9339
BLUR = """
#devName, #heroName, #dhTitle, .phone-row .row-text b, .row-menu,
.notif .ti, .notif .tx, .notif .avatar, input[data-set=pc_name], input[data-set=display_name]
{ filter: blur(7px) !important; }
"""
SHOTS = [("status", 1320, 860), ("settings", 1320, 860), ("display", 1320, 860)]


async def main(out: pathlib.Path):
    out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as prof:
        edge = subprocess.Popen([EDGE, "--headless=new", "--disable-gpu", "--hide-scrollbars", "--force-dark-mode",
                                 f"--remote-debugging-port={PORT}", f"--user-data-dir={prof}", "about:blank"])
        try:
            async with aiohttp.ClientSession() as http:
                for _ in range(50):
                    try:
                        async with http.get(f"http://127.0.0.1:{PORT}/json") as r:
                            pages = [p for p in await r.json() if p.get("type") == "page"]
                        if pages:
                            break
                    except aiohttp.ClientError:
                        pass
                    await asyncio.sleep(0.2)
                async with http.ws_connect(pages[0]["webSocketDebuggerUrl"], max_msg_size=1 << 28) as ws:
                    n = 0

                    async def cdp(method, **params):
                        nonlocal n
                        n += 1
                        await ws.send_str(json.dumps({"id": n, "method": method, "params": params}))
                        while True:
                            m = json.loads((await ws.receive()).data)
                            if m.get("id") == n:
                                return m.get("result", {})

                    for tab, w, h in SHOTS:
                        await cdp("Emulation.setDeviceMetricsOverride", width=w, height=h, deviceScaleFactor=1.5,
                                  mobile=False)
                        await cdp("Page.navigate", url=f"http://127.0.0.1:47801/#{tab}")
                        await asyncio.sleep(5)          # state, phone picture, notifications
                        await cdp("Runtime.evaluate", expression=(
                            "(() => { const s = document.createElement('style'); s.textContent = "
                            + json.dumps(BLUR) + "; document.head.append(s); })()"))
                        await asyncio.sleep(0.5)
                        shot = await cdp("Page.captureScreenshot", format="png")
                        (out / f"{tab}.png").write_bytes(base64.b64decode(shot["data"]))
                        print("captured", tab)
        finally:
            edge.kill()
            edge.wait(10)


if __name__ == "__main__":
    asyncio.run(main(pathlib.Path(sys.argv[1])))
