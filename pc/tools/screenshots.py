"""README screenshots of the running Windows app (http://127.0.0.1:47801), with personal details erased.

    python tools/screenshots.py <out dir> [name ...]

Drives headless Microsoft Edge over the DevTools protocol (nothing appears on screen). Before each capture,
personal details are erased: the phone's and PC's names are blurred, photo thumbnails and non-standard file names
are blurred, and the notification list is replaced by neutral sample notifications (real app icons, made-up
senders and texts). App-window shots run real apps in the viewer page (pick apps that show nothing personal).
"""
import asyncio
import base64
import json
import pathlib
import subprocess
import sys
import tempfile
from urllib.parse import urlencode

import aiohttp

EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
PORT = 9339
APP = "http://127.0.0.1:47801"

BLUR = """
#devName, #heroName, #dhTitle, .phone-row .row-text b, .row-menu, #crumbs button:first-child,
.notif .avatar, input[data-set=pc_name], input[data-set=display_name], .fs-row.private span:nth-child(2)
{ filter: blur(7px) !important; }
#grid .tile { filter: blur(30px) saturate(.8) !important; }
"""

SAMPLE_NOTIFICATIONS = """
(() => {
  const now = Date.now();
  notifs.clear(); freshKeys.clear();
  const add = o => notifs.set(o.key, Object.assign({ clearable: true, actions: [], ongoing: false, call: false,
                                                     hasIcon: false }, o));
  add({ key: "s1", pkg: "org.telegram.messenger", app: "Telegram", title: "Anna", when: now - 60e3,
        text: "Are we still on for lunch at 1?",
        actions: [{ i: 0, title: "Reply", reply: true }, { i: 1, title: "Mark as read" }] });
  add({ key: "s2", pkg: "com.google.android.calendar", app: "Calendar", title: "Team stand-up", when: now - 6 * 60e3,
        text: "10:30 – 10:45 · Meeting room 2", actions: [{ i: 0, title: "Snooze" }] });
  add({ key: "s3", pkg: "com.google.android.gm", app: "Gmail", title: "Your order has shipped", when: now - 25 * 60e3,
        text: "Your package is on its way and should arrive on Tuesday.",
        actions: [{ i: 0, title: "Archive" }, { i: 1, title: "Reply" }] });
  history = [
    { key: "h1", pkg: "com.whatsapp", app: "WhatsApp", title: "Book club", text: "See you all on Thursday!", when: now - 3 * 3600e3 },
    { key: "h2", pkg: "com.google.android.keep", app: "Keep", title: "Shopping list", text: "Milk, bread, coffee", when: now - 5 * 3600e3 },
  ];
  renderNotifs();
})()
"""

STANDARD_FOLDERS = ["Alarms", "Android", "Audiobooks", "DCIM", "Documents", "Download", "Movies", "Music",
                    "Notifications", "Pictures", "Podcasts", "Recordings", "Ringtones"]
FILES_PREP = """
(async () => {
  await fsLoad("/");
  const ok = new Set(%s);
  document.querySelectorAll("#fsList .fs-row").forEach(r => {
    const n = r.querySelector("span:nth-child(2)")?.textContent || "";
    if (!ok.has(n)) r.classList.add("private");
  });
})()
""" % json.dumps(STANDARD_FOLDERS)


def viewer(pkg: str, label: str) -> str:
    return f"{APP}/ui/screen.html?" + urlencode({"kind": "app", "pkg": pkg, "label": label, "title": label})


# Areas inside app content (the video, not the page) erased after capture: fractions of the image (x0, y0, x1, y1).
BLUR_RECTS = {
    "app-settings": [(0.66, 0.17, 0.92, 0.24)],          # the Wi-Fi network name
}

# name, url, css width, css height, seconds to wait, JS to run before capture
SHOTS = [
    ("status", f"{APP}/#status", 1320, 860, 5, SAMPLE_NOTIFICATIONS),
    ("photos", f"{APP}/#photos", 1320, 860, 6, ""),
    ("files", f"{APP}/#files", 1320, 860, 4, FILES_PREP),
    ("settings", f"{APP}/#settings", 1320, 860, 4, ""),
    ("display", f"{APP}/#display", 1320, 860, 4, ""),
    ("app-calculator", viewer("com.google.android.calculator", "Calculator"), 420, 900, 9, ""),
    ("app-settings", viewer("com.android.settings", "Settings"), 420, 900, 8, ""),
]


def erase(path: pathlib.Path, rects):
    from PIL import Image, ImageFilter
    img = Image.open(path).convert("RGB")
    for x0, y0, x1, y1 in rects:
        box = tuple(round(v) for v in (x0 * img.width, y0 * img.height, x1 * img.width, y1 * img.height))
        img.paste(img.crop(box).filter(ImageFilter.GaussianBlur(14)), box[:2])
    img.save(path)


async def main(out: pathlib.Path, only: set[str]):
    out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as prof:
        edge = subprocess.Popen([EDGE, "--headless=new", "--hide-scrollbars", "--force-dark-mode",
                                 f"--remote-debugging-port={PORT}", f"--user-data-dir={prof}", "about:blank"])
        try:
            async with aiohttp.ClientSession() as http:
                pages = []
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

                    for name, url, w, h, wait, prep in SHOTS:
                        if only and name not in only:
                            continue
                        await cdp("Emulation.setDeviceMetricsOverride", width=w, height=h,
                                  deviceScaleFactor=1.5 if w > 600 else 2, mobile=False)
                        await cdp("Page.navigate", url=url)
                        await asyncio.sleep(wait)
                        if prep:
                            await cdp("Runtime.evaluate", expression=prep, awaitPromise=True)
                        await cdp("Runtime.evaluate", expression=(
                            "(() => { const s = document.createElement('style'); s.textContent = "
                            + json.dumps(BLUR) + "; document.head.append(s); })()"))
                        await asyncio.sleep(1)
                        shot = await cdp("Page.captureScreenshot", format="png")
                        (out / f"{name}.png").write_bytes(base64.b64decode(shot["data"]))
                        if name in BLUR_RECTS:
                            erase(out / f"{name}.png", BLUR_RECTS[name])
                        print("captured", name)
                    await cdp("Page.navigate", url="about:blank")    # ends the last app session
                    await asyncio.sleep(1)
        finally:
            edge.kill()
            edge.wait(10)


if __name__ == "__main__":
    asyncio.run(main(pathlib.Path(sys.argv[1]), set(sys.argv[2:])))
