"""Sidecar: the phone becomes an extra Windows monitor.

Pieces (all under <repo>/sidecar):
  * Virtual Display Driver (IddCx, VirtualDrivers/Virtual-Display-Driver 25.7.23) gives Windows a real monitor
    ("VDD by MTT") that can be arranged/scaled in Settings > Display. It is kept *detached* from the desktop except
    while a sidecar session runs (bin/vddctl.exe attach/detach via SetDisplayConfig; no admin needed).
  * bin/sidecar_host.exe captures that monitor (Windows.Graphics.Capture, cursor included) and encodes H.264 with
    the GPU's hardware MFT (AMD AMF here), low-latency VBR, no B-frames, keyframes on demand.
  * SidecarActivity on the phone decodes with MediaCodec (low-latency) into a SurfaceView and streams touches back,
    which are injected as real multi-touch (InjectTouchInput) on the virtual monitor's desktop rectangle.

usage:
    sc = Sidecar()
    await sc.start(core, orientation="landscape", scale=150)    # or "portrait"
    sc.status()
    await sc.stop()
"""
from __future__ import annotations

import asyncio
import ctypes
import json
import logging
import socket
import struct
import subprocess
import time
from ctypes import wintypes
from pathlib import Path

from .config import APP_DIR, LOG_DIR, settings


_u32 = ctypes.WinDLL("user32")
_u32.SetThreadDpiAwarenessContext.restype = ctypes.c_void_p
_u32.SetThreadDpiAwarenessContext.argtypes = (ctypes.c_void_p,)


class _DpiAware:
    """Temporarily makes the calling thread per-monitor DPI aware so coordinates are physical pixels."""

    def __enter__(self):
        self.old = _u32.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))   # PER_MONITOR_AWARE_V2

    def __exit__(self, *a):
        if self.old:
            _u32.SetThreadDpiAwarenessContext(ctypes.c_void_p(self.old))

log = logging.getLogger("sidecar")

PHONE_PORT = 47903
USB_FWD_PORT = 47913
ACTIVITY = "dev.phonesync/.SidecarActivity"


def _bin_dir() -> Path:
    for c in (APP_DIR / "sidecar" / "bin", Path(__file__).resolve().parents[2] / "sidecar" / "bin"):
        if (c / "sidecar_host.exe").exists():
            return c
    return APP_DIR / "sidecar" / "bin"


BIN = _bin_dir()
VDDCTL = BIN / "vddctl.exe"
HOST = BIN / "sidecar_host.exe"
NO_WINDOW = subprocess.CREATE_NO_WINDOW

# Phone panel (nubia NX741J): 1216x2688. Modes offered by the driver config (sidecar/driver/vdd_settings.xml).
MODES = {
    ("landscape", "full"): (2688, 1216), ("portrait", "full"): (1216, 2688),
    ("landscape", "medium"): (2016, 912), ("portrait", "medium"): (912, 2016),
    ("landscape", "half"): (1344, 608), ("portrait", "half"): (608, 1344),
}
# Text size on the side display, % of the panel's own pixels. Windows scales this monitor only up to 200% (it keeps at
# least ~600 logical px on the short side); larger sizes use a proportionally lower mode at 100% that the phone
# upscales (also in vdd_settings.xml).
SIZES = (100, 125, 150, 175, 200, 250, 300, 350, 400, 450, 500, 550, 600)


def mode_for(orientation: str, quality: str, size: int | None) -> tuple[tuple[int, int], int | None]:
    """((width, height), Windows scale % or None for automatic) for a text size (None: Windows' recommendation)."""
    if size and size > 200:
        size = min(SIZES, key=lambda s: abs(s - size))
        w, h = round(2688 * 100 / size / 8) * 8, round(1216 * 100 / size / 2) * 2
        return ((w, h) if orientation == "landscape" else (h, w)), 100
    return MODES[(orientation, quality)], size or None


async def vddctl(*args, timeout=15) -> dict:
    p = await asyncio.create_subprocess_exec(str(VDDCTL), *map(str, args), stdout=asyncio.subprocess.PIPE,
                                             stderr=asyncio.subprocess.PIPE, creationflags=NO_WINDOW)
    out, err = await asyncio.wait_for(p.communicate(), timeout)
    lines = [l for l in out.decode(errors="replace").splitlines() if l.startswith("{")]
    res = json.loads(lines[-1]) if lines else {}
    if p.returncode:
        res["error"] = err.decode(errors="replace").strip() or f"vddctl exit {p.returncode}"
    return res


# --------------------------------------------------------------------------------------------- touch injection
user32 = ctypes.WinDLL("user32", use_last_error=True)

PT_TOUCH = 2
POINTER_FLAG_INRANGE, POINTER_FLAG_INCONTACT = 0x2, 0x4
POINTER_FLAG_PRIMARY, POINTER_FLAG_CANCELED = 0x2000, 0x8000
POINTER_FLAG_DOWN, POINTER_FLAG_UPDATE, POINTER_FLAG_UP = 0x10000, 0x20000, 0x40000
TOUCH_MASK_CONTACTAREA, TOUCH_MASK_PRESSURE = 0x1, 0x4
TOUCH_FEEDBACK_DEFAULT = 0x1


class POINTER_INFO(ctypes.Structure):
    _fields_ = [("pointerType", ctypes.c_uint32), ("pointerId", ctypes.c_uint32), ("frameId", ctypes.c_uint32),
                ("pointerFlags", ctypes.c_uint32), ("sourceDevice", wintypes.HANDLE), ("hwndTarget", wintypes.HWND),
                ("ptPixelLocation", wintypes.POINT), ("ptHimetricLocation", wintypes.POINT),
                ("ptPixelLocationRaw", wintypes.POINT), ("ptHimetricLocationRaw", wintypes.POINT),
                ("dwTime", wintypes.DWORD), ("historyCount", ctypes.c_uint32), ("InputData", ctypes.c_int32),
                ("dwKeyStates", wintypes.DWORD), ("PerformanceCount", ctypes.c_uint64),
                ("ButtonChangeType", ctypes.c_int)]


class POINTER_TOUCH_INFO(ctypes.Structure):
    _fields_ = [("pointerInfo", POINTER_INFO), ("touchFlags", ctypes.c_uint32), ("touchMask", ctypes.c_uint32),
                ("rcContact", wintypes.RECT), ("rcContactRaw", wintypes.RECT), ("orientation", ctypes.c_uint32),
                ("pressure", ctypes.c_uint32)]


user32.InjectTouchInput.argtypes = (ctypes.c_uint32, ctypes.POINTER(POINTER_TOUCH_INFO))
user32.InitializeTouchInjection.argtypes = (ctypes.c_uint32, ctypes.c_uint32)
_touch_ready = False


class TouchInjector:
    """Multi-touch on a desktop rectangle. Every injection carries all active contacts (Windows requirement);
    stationary contacts are refreshed by `keepalive()` so Windows doesn't time them out."""

    MAX = 10

    def __init__(self):
        global _touch_ready
        if not _touch_ready:
            if not user32.InitializeTouchInjection(self.MAX, TOUCH_FEEDBACK_DEFAULT):
                raise OSError(f"InitializeTouchInjection failed ({ctypes.get_last_error()})")
            _touch_ready = True
        self.rect = (0, 0, 1, 1)                 # x, y, w, h in physical px
        self.contacts: dict[int, list] = {}      # phone id -> [slot, x, y, pressure]
        self.last_inject = 0.0
        self.injected = 0
        self.errors = 0
        self.trace: list = []                    # last injections (debugging)

    def _slot(self):
        used = {c[0] for c in self.contacts.values()}
        return next(i for i in range(self.MAX) if i not in used)

    def frame(self, pointers):
        """pointers: [(phone_id, lifting, cancel, nx, ny, pressure 0..1)] for all pointers on the phone."""
        x0, y0, w, h = self.rect
        infos = []
        seen = set()
        for pid, lifting, cancel, nx, ny, pr in pointers:
            px = x0 + min(max(int(nx * w), 0), w - 1)
            py = y0 + min(max(int(ny * h), 0), h - 1)
            c = self.contacts.get(pid)
            if c is None:
                if lifting or len(self.contacts) >= self.MAX:
                    continue
                c = self.contacts[pid] = [self._slot(), px, py, pr]
                flags = POINTER_FLAG_DOWN | POINTER_FLAG_INRANGE | POINTER_FLAG_INCONTACT
            elif lifting:
                flags = POINTER_FLAG_UP | (POINTER_FLAG_CANCELED if cancel else 0)
                px, py = c[1], c[2]
            else:
                flags = POINTER_FLAG_UPDATE | POINTER_FLAG_INRANGE | POINTER_FLAG_INCONTACT
            c[1], c[2], c[3] = px, py, pr
            seen.add(pid)
            infos.append(self._info(c, flags))
            if lifting:
                self.contacts.pop(pid, None)
        # Contacts the phone no longer reports (missed lift): release them.
        for pid in [p for p in self.contacts if p not in seen]:
            infos.append(self._info(self.contacts.pop(pid), POINTER_FLAG_UP | POINTER_FLAG_CANCELED))
        self._inject(infos)

    def keepalive(self):
        if self.contacts and time.monotonic() - self.last_inject > 0.05:
            self._inject([self._info(c, POINTER_FLAG_UPDATE | POINTER_FLAG_INRANGE | POINTER_FLAG_INCONTACT)
                          for c in self.contacts.values()])

    def release_all(self):
        if self.contacts:
            self._inject([self._info(c, POINTER_FLAG_UP | POINTER_FLAG_CANCELED) for c in self.contacts.values()])
            self.contacts.clear()

    def _info(self, c, flags) -> POINTER_TOUCH_INFO:
        slot, px, py, pr = c
        t = POINTER_TOUCH_INFO()
        t.pointerInfo.pointerType = PT_TOUCH
        t.pointerInfo.pointerId = slot
        t.pointerInfo.pointerFlags = flags       # (InjectTouchInput rejects POINTER_FLAG_PRIMARY; Windows assigns it)
        t.pointerInfo.ptPixelLocation = wintypes.POINT(px, py)
        t.touchMask = TOUCH_MASK_CONTACTAREA | TOUCH_MASK_PRESSURE
        t.rcContact = wintypes.RECT(px - 4, py - 4, px + 4, py + 4)
        t.pressure = max(1, min(1024, int(pr * 1024) if pr > 0 else 512))
        return t

    def _inject(self, infos):
        if not infos:
            return
        self.trace.append([(i.pointerInfo.ptPixelLocation.x, i.pointerInfo.ptPixelLocation.y, hex(i.pointerInfo.pointerFlags)) for i in infos])
        del self.trace[:-20]
        arr = (POINTER_TOUCH_INFO * len(infos))(*infos)
        with _DpiAware():
            ok = user32.InjectTouchInput(len(infos), arr)
        self.last_inject = time.monotonic()
        if ok:
            self.injected += 1
        else:
            self.errors += 1
            if self.errors < 20:
                log.warning("InjectTouchInput failed: %s", ctypes.get_last_error())


# --------------------------------------------------------------------------------------------- session
class Sidecar:
    def __init__(self, fps: int = 60, bitrate: int = 30_000_000, quality: str = "full"):
        self.fps = fps
        self.bitrate = bitrate
        self.quality = quality              # full | medium | half (virtual monitor resolution)
        self.orientation = "landscape"
        self.state = "idle"
        self.error = ""
        self.link = ""
        self.monitor: dict = {}
        self.host_info: dict = {}
        self.phone_info: dict = {}
        self._task: asyncio.Task | None = None
        self._host: asyncio.subprocess.Process | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._serial = None
        self._adbm = None
        self._token = ""
        self._stopping = False
        self._touch: TouchInjector | None = None
        self._lat: list[float] = []
        self._frames = 0
        self._dropped = 0
        self._bytes = 0
        self._t_start = 0.0
        self._skip_to_key = False
        self._last_key_req = 0.0

    # ---------------------------------------------------------------- public API
    async def start(self, core, orientation: str = "landscape", scale: int | None = None,
                    host: str | None = None, port: int | None = None) -> dict:
        if self._task and not self._task.done():
            if orientation != self.orientation:
                await self.stop()
            else:
                return self.status()
        self.orientation = "portrait" if orientation == "portrait" else "landscape"
        self._core = core
        self._adbm = getattr(core, "adbm", None)
        self._serial = self._adbm.serial if self._adbm else None
        self._token = settings["token"]
        self._stopping = False
        self.error = ""
        self.state = "starting"
        self._lat.clear()
        self._frames = self._dropped = self._bytes = 0
        try:
            await self._attach(scale)
            if self._serial:
                from . import adb
                await adb.shell(f"input keyevent KEYCODE_WAKEUP ; am start -n {ACTIVITY} --es orientation {self.orientation}",
                                self._serial)
            self._touch = TouchInjector()
            self._touch_rect()
        except Exception as e:
            self.error = str(e)
            self.state = "error"
            await vddctl("detach")
            return self.status()
        self._task = asyncio.create_task(self._run(host, port))
        for _ in range(100):
            if self.state in ("streaming", "error", "closed") or self._task.done():
                break
            await asyncio.sleep(0.1)
        return self.status()

    async def stop(self):
        self._stopping = True
        try:
            if self._writer:
                self._send_json({"cmd": "close"})
                await asyncio.wait_for(self._writer.drain(), 1)
        except Exception:
            pass
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        await self._stop_host()
        if self._touch:
            self._touch.release_all()
        await vddctl("detach")
        self.state = "idle"

    async def set_scale(self, pct: int | None) -> dict:
        """Text size while running: Windows scaling in place, or a restart when another mode is needed."""
        mode, win = mode_for(self.orientation, self.quality, pct)
        cur = (self.monitor.get("w"), self.monitor.get("h")) if self.monitor else None
        if self.state in ("streaming", "starting") and cur and tuple(cur) != tuple(mode):
            core = getattr(self, "_core", None)
            await self.stop()
            return await self.start(core, orientation=self.orientation, scale=pct)
        self.monitor = await vddctl("scale", win or 0) if win else await vddctl("list")
        self._touch_rect()
        return self.status()

    def status(self) -> dict:
        lat = sorted(self._lat[-300:])
        up = time.monotonic() - self._t_start if self._t_start else 0
        return {
            "state": self.state, "error": self.error, "link": self.link, "orientation": self.orientation,
            "monitor": self.monitor, "encoder": self.host_info, "decoder": self.phone_info.get("decoder"),
            "fps": round(self._frames / up, 1) if up > 1 else 0, "frames": self._frames, "dropped": self._dropped,
            "mbps": round(self._bytes * 8 / up / 1e6, 1) if up > 1 else 0,
            "latency_ms": {"p50": round(lat[len(lat) // 2] / 1000, 1), "p90": round(lat[len(lat) * 9 // 10] / 1000, 1)}
            if lat else None,
            "touches": self._touch.injected if self._touch else 0,
        }

    # ---------------------------------------------------------------- monitor
    async def _attach(self, scale):
        (w, h), scale = mode_for(self.orientation, self.quality, scale)
        info = await vddctl("attach", w, h, 60)
        if "error" in info or not info.get("active"):
            raise RuntimeError(f"virtual monitor: {info.get('error', 'not available (driver installed?)')}")
        await asyncio.sleep(1.5)        # let the mode set settle before the GPU capture starts
        if scale:
            info = await vddctl("scale", scale)
        else:
            info = await vddctl("list")
        self.monitor = info
        self._touch_rect()

    def _touch_rect(self):
        if self._touch and self.monitor.get("active"):
            m = self.monitor
            self._touch.rect = (m["x"], m["y"], m["w"], m["h"])

    # ---------------------------------------------------------------- host process
    async def _start_host(self):
        await self._stop_host()
        m = self.monitor
        args = [str(HOST), "--gdi", m["gdi"], "--fps", str(self.fps), "--bitrate", str(self.bitrate)]
        errlog = open(LOG_DIR / "sidecar_host.log", "a", encoding="utf-8", errors="replace")
        self._host = await asyncio.create_subprocess_exec(*args, stdin=asyncio.subprocess.PIPE,
                                                          stdout=asyncio.subprocess.PIPE, stderr=errlog,
                                                          creationflags=NO_WINDOW, limit=8 << 20)
        hdr = await asyncio.wait_for(self._host.stdout.readexactly(13), 15)   # init watchdog
        n, fl, _ = struct.unpack(">IBQ", hdr)
        body = await self._host.stdout.readexactly(n)
        if not fl & 0x80:
            raise RuntimeError("unexpected host output")
        self.host_info = json.loads(body)
        self._send_json({"video": {"w": self.host_info["w"], "h": self.host_info["h"]}})
        self._skip_to_key = False

    async def _stop_host(self):
        p, self._host = self._host, None
        if not p or p.returncode is not None:
            return
        try:
            p.stdin.write(b"Q\n")
            await p.stdin.drain()
            await asyncio.wait_for(p.wait(), 4)
        except Exception:
            try:
                p.kill()
            except ProcessLookupError:
                pass

    def _host_cmd(self, cmd: str):
        p = self._host
        if p and p.returncode is None:
            try:
                p.stdin.write(cmd.encode() + b"\n")
            except Exception:
                pass

    def _request_key(self):
        now = time.monotonic()
        if now - self._last_key_req > 0.3:
            self._last_key_req = now
            self._host_cmd("K")

    # ---------------------------------------------------------------- phone link
    async def _connect(self, host, port):
        from . import adb
        eps = []
        if host:
            eps.append((host, port or PHONE_PORT, "custom"))
        else:
            usb = self._adbm.usb_serial if self._adbm else None
            if usb:
                rc, _ = await adb.run("forward", f"tcp:{USB_FWD_PORT}", f"tcp:{PHONE_PORT}", serial=usb)
                if rc == 0:
                    eps.append(("127.0.0.1", USB_FWD_PORT, "usb"))
            if settings["last_ip"]:
                eps.append((settings["last_ip"], PHONE_PORT, "wifi"))
        last = None
        for h, p, kind in eps:
            try:
                r, w = await asyncio.wait_for(asyncio.open_connection(h, p, limit=1 << 20), 3)
                s = w.get_extra_info("socket")
                if s is not None:
                    s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                    s.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4 << 20)
                w.write(f"PSAUTH {self._token}\n".encode())
                await w.drain()
                line = await asyncio.wait_for(r.readline(), 3)
                if line.strip() != b"OK":
                    w.close()
                    last = ConnectionError(f"{kind}: auth failed")
                    continue
                self.link = kind
                return r, w
            except (OSError, asyncio.TimeoutError, asyncio.IncompleteReadError) as e:
                last = e
        raise last or ConnectionError("phone not reachable")

    async def _run(self, host, port):
        fails = 0
        try:
            while not self._stopping:
                try:
                    r, w = await self._connect(host, port)
                except Exception as e:
                    fails += 1
                    self.error = str(e) or type(e).__name__
                    if fails > 40:
                        self.state = "error"
                        return
                    await asyncio.sleep(0.3)
                    continue
                fails = 0
                self._writer = w
                self.error = ""
                tasks = []
                try:
                    await self._start_host()
                    self.state = "streaming"
                    self._t_start = time.monotonic()
                    self._restarts = 0
                    tasks = [asyncio.create_task(self._pump_video()), asyncio.create_task(self._read_phone(r)),
                             asyncio.create_task(self._keepalive())]
                    done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                    for t in done:
                        if t.exception() and not isinstance(t.exception(), (ConnectionError, asyncio.IncompleteReadError)):
                            log.warning("sidecar task: %r", t.exception())
                except Exception as e:
                    log.warning("sidecar: %s", e)
                    self.error = str(e)
                finally:
                    for t in tasks:
                        t.cancel()
                    if self._touch:
                        self._touch.release_all()
                    self._writer = None
                    try:
                        w.close()
                    except Exception:
                        pass
                if self._stopping:
                    break
                rc = self._host.returncode if self._host else None
                if rc == 3:          # monitor mode changed in Display Settings: re-read and restart
                    self.monitor = await vddctl("list")
                    self._touch_rect()
                if not await self._phone_alive():
                    self.state = "closed"
                    break
                self.state = "reconnecting"
                # Back off so a persistent failure never respawns the GPU encoder in a tight loop.
                self._restarts = getattr(self, "_restarts", 0) + 1
                if self._restarts > 8:
                    self.state = "error"
                    self.error = self.error or "too many restarts"
                    break
                await asyncio.sleep(min(0.5 * self._restarts, 3))
        finally:
            await self._stop_host()
            if self._touch:
                self._touch.release_all()
            if self.state in ("closed", "error") and not self._stopping:
                await vddctl("detach")       # phone closed the sidecar: give the windows back

    async def _phone_alive(self) -> bool:
        if not self._serial:
            return True
        from . import adb
        out = await adb.shell("dumpsys activity activities | grep -m1 topResumedActivity", self._serial, timeout=5)
        return "SidecarActivity" in out

    async def _pump_video(self):
        rd = self._host.stdout
        w = self._writer
        tr = w.transport
        while True:
            hdr = await rd.readexactly(13)
            n, fl, pts = struct.unpack(">IBQ", hdr)
            data = await rd.readexactly(n)
            if fl & 0x80:
                continue
            key = bool(fl & 1)
            # Congestion control: never queue more than ~4 MB; skip to the next keyframe instead.
            if tr.get_write_buffer_size() > (4 << 20):
                self._skip_to_key = True
            if self._skip_to_key:
                if not key:
                    self._dropped += 1
                    self._request_key()
                    continue
                self._skip_to_key = False
            w.write(b"\x04" + struct.pack(">IBQ", n + 9, fl & 1, pts) + data)
            self._frames += 1
            self._bytes += n
            if tr.get_write_buffer_size() > (1 << 20):
                await w.drain()

    async def _read_phone(self, r: asyncio.StreamReader):
        while True:
            hdr = await r.readexactly(5)
            typ, ln = hdr[0], struct.unpack(">I", hdr[1:])[0]
            data = await r.readexactly(ln) if ln else b""
            if typ == 1 and self._touch:
                n = data[5]
                pts = []
                for i in range(n):
                    pid, flags, x, y, major, pr, _ = struct.unpack_from(">BBHHHHH", data, 6 + i * 12)
                    pts.append((pid, bool(flags & 5), bool(flags & 4), x / 65535, y / 65535, pr / 10000))
                self._touch.frame(pts)
            elif typ == 6:
                pts_us = struct.unpack(">Q", data)[0]
                self._lat.append(time.perf_counter_ns() / 1000 - pts_us)
                if len(self._lat) > 2000:
                    del self._lat[:1000]
            elif typ == 3:
                msg = json.loads(data)
                if msg.get("need_key"):
                    self._request_key()
                if msg.get("decoder"):
                    self.phone_info["decoder"] = msg["decoder"]
                    self.phone_info["low_latency"] = msg.get("low_latency")
                if msg.get("hello"):
                    self.phone_info.update(view=(msg["w"], msg["h"]))

    def _send_json(self, obj):
        w = self._writer
        if w:
            b = json.dumps(obj).encode()
            w.write(bytes([3]) + struct.pack(">I", len(b)) + b)

    async def _keepalive(self):
        while True:
            await asyncio.sleep(0.03)
            if self._touch:
                self._touch.keepalive()
            if self._host and self._host.returncode is not None:
                raise ConnectionError(f"host exited ({self._host.returncode})")
