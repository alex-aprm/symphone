"""Native asyncio client for scrcpy-server 5.0.1 (video + control), used by the in-app screen viewer.

Protocol (tunnel_forward=true):
  * the server listens on localabstract:scrcpy_<scid>; we `adb forward tcp:N` to it and connect video, [audio],
    control in that order. The first socket receives one dummy byte (0) as soon as the server accepted it, which is
    how we detect the server is up (adb accepts and immediately closes the TCP connection before that).
  * first socket: 64 bytes device name (NUL padded)
  * video socket: u32 codec id, then 12-byte headers. MSB set = session header (u32 flags, u32 width, u32 height),
    otherwise media header (u64 pts|flags, u32 size) followed by <size> bytes of Annex B data.
  * control socket: client -> server ControlMessage (see ControlMessageReader.java), server -> client DeviceMessage.
"""
from __future__ import annotations

import asyncio
import hashlib
import math
import os
import logging
import random
import re
import struct
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .config import ADB, SCRCPY_DIR

log = logging.getLogger("scrcpy")
NO_WINDOW = subprocess.CREATE_NO_WINDOW

SERVER_VERSION = "5.0.1"
SERVER_LOCAL = Path(os.environ.get("PHONESYNC_SCRCPY_SERVER") or SCRCPY_DIR / "scrcpy-server")   # env: test builds
# Own path (not scrcpy-server.jar): scrcpy.exe deletes its jar on exit, and we keep ours (cleanup=false) so it is
# only pushed when it changed.
SERVER_REMOTE = "/data/local/tmp/phonesync-scrcpy-server.jar"

CODEC_IDS = {0x68323634: "h264", 0x68323635: "h265", 0x00617631: "av1",
             0x6f707573: "opus", 0x00616163: "aac", 0x666c6163: "flac", 0x00726177: "raw"}

FLAG_SESSION = 1 << 63
FLAG_CONFIG = 1 << 62
FLAG_KEY_FRAME = 1 << 61
PTS_MASK = FLAG_KEY_FRAME - 1

# control message types
MSG_INJECT_KEYCODE = 0
MSG_INJECT_TEXT = 1
MSG_INJECT_TOUCH = 2
MSG_INJECT_SCROLL = 3
MSG_BACK_OR_SCREEN_ON = 4          # never used: it may wake the phone
MSG_EXPAND_NOTIFICATION_PANEL = 5
MSG_EXPAND_SETTINGS_PANEL = 6
MSG_COLLAPSE_PANELS = 7
MSG_GET_CLIPBOARD = 8
MSG_SET_CLIPBOARD = 9
MSG_SET_DISPLAY_POWER = 10
MSG_ROTATE_DEVICE = 11
MSG_OPEN_HARD_KEYBOARD_SETTINGS = 15
MSG_START_APP = 16
MSG_RESET_VIDEO = 17
MSG_CAMERA_SET_TORCH = 18
MSG_CAMERA_ZOOM_IN = 19
MSG_CAMERA_ZOOM_OUT = 20
MSG_RESIZE_DISPLAY = 21

# Android constants
ACTION_DOWN, ACTION_UP, ACTION_MOVE = 0, 1, 2
BUTTON_PRIMARY, BUTTON_SECONDARY, BUTTON_TERTIARY = 1, 2, 4
POINTER_ID_MOUSE = -1
POINTER_ID_GENERIC_FINGER = -2
POINTER_ID_VIRTUAL_FINGER = -3

KEYCODES = {
    "HOME": 3, "BACK": 4, "DPAD_UP": 19, "DPAD_DOWN": 20, "DPAD_LEFT": 21, "DPAD_RIGHT": 22, "VOLUME_UP": 24,
    "VOLUME_DOWN": 25, "TAB": 61, "SPACE": 62, "ENTER": 66, "DEL": 67, "MENU": 82, "PAGE_UP": 92, "PAGE_DOWN": 93,
    "ESCAPE": 111, "FORWARD_DEL": 112, "MOVE_HOME": 122, "MOVE_END": 123, "INSERT": 124, "VOLUME_MUTE": 164,
    "APP_SWITCH": 187, "CUT": 277, "COPY": 278, "PASTE": 279, "MEDIA_PLAY_PAUSE": 85, "CAMERA": 27, "SEARCH": 84,
}

_pushed: dict[str, str] = {}      # serial -> md5 of the jar we know is on the device


@dataclass
class VideoPacket:
    config: bool
    keyframe: bool
    pts: int | None
    data: bytes


@dataclass
class StreamSize:
    width: int
    height: int
    client_resized: bool = False


class ScrcpyError(RuntimeError):
    pass


async def adb(*args, serial=None, timeout=20) -> tuple[int, str]:
    cmd = [str(ADB)] + (["-s", serial] if serial else []) + [str(a) for a in args]
    proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.STDOUT, creationflags=NO_WINDOW)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return -1, "timeout"
    return proc.returncode, out.decode("utf-8", "replace").strip()


async def push_server(serial: str, local: Path = SERVER_LOCAL, remote: str = SERVER_REMOTE) -> bool:
    """Pushes the (patched) server jar unless the device already has an identical copy. Returns True if pushed."""
    md5 = hashlib.md5(local.read_bytes()).hexdigest()
    if _pushed.get(serial) == md5:
        return False
    rc, out = await adb("shell", f"md5sum {remote} 2>/dev/null", serial=serial)
    if out.split()[:1] != [md5]:
        rc, out = await adb("push", str(local), remote, serial=serial, timeout=60)
        if rc != 0:
            raise ScrcpyError(f"push server: {out}")
        log.info("pushed scrcpy server to %s", serial)
        _pushed[serial] = md5
        return True
    _pushed[serial] = md5
    return False


_swept: set[str] = set()


async def sweep_stale_forwards(serial: str, keep: set[int] = frozenset()):
    """Removes `adb forward`s left by sessions of a previous (crashed / killed) process. Once per serial."""
    if serial in _swept:
        return
    _swept.add(serial)
    rc, out = await adb("forward", "--list")
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 3 and parts[0] == serial and parts[2].startswith("localabstract:scrcpy_"):
            try:
                scid = int(parts[2].rsplit("_", 1)[1], 16)
            except ValueError:
                continue
            if scid not in keep:
                await adb("forward", "--remove", parts[1], serial=serial, timeout=5)


async def home_activity(serial: str) -> str | None:
    """Default launcher component, e.g. com.zte.mifavor.launcher/com.android.launcher3...QuickstepLauncher."""
    rc, out = await adb("shell", "cmd package resolve-activity --brief -c android.intent.category.HOME "
                        "-a android.intent.action.MAIN", serial=serial)
    lines = [l.strip() for l in out.splitlines() if "/" in l]
    return lines[-1] if lines else None


async def wakefulness(serial: str) -> str:
    rc, out = await adb("shell", "dumpsys power | grep -m1 mWakefulness=", serial=serial)
    m = re.search(r"mWakefulness=(\w+)", out)
    return m.group(1) if m else "?"


def _b(v) -> str:
    return "true" if v else "false"


class ScrcpySession:
    """One scrcpy-server instance: video stream + control channel.

    Usage:
        s = ScrcpySession(serial, new_display="", vd_system_decorations=True, start_app="com.google.android.apps.photos")
        await s.start()
        async for ev in s:            # VideoPacket | StreamSize
            ...
        await s.stop()
    or set s.on_packet / s.on_size / s.on_clipboard callbacks and iterate nothing (callbacks are called from the
    reader task; when callbacks are set the iterator queue is not used).
    """

    def __init__(self, serial: str, *, video_source: str = "display", display_id: int | None = None,
                 new_display: str | None = None, start_app: str | None = None, max_size: int = 0,
                 video_bit_rate: int | str = "8M", max_fps: int = 60, video_codec: str = "h264",
                 video_encoder: str | None = None, video_codec_options: str | None = None,
                 audio: bool = False, control: bool = True, camera_facing: str | None = None,
                 camera_id: str | None = None, camera_size: str | None = None, camera_ar: str | None = None,
                 camera_fps: int | None = None, camera_high_speed: bool = False, power_on: bool = False,
                 stay_awake: bool = False, vd_system_decorations: bool = True, vd_destroy_content: bool = True,
                 display_ime_policy: str | None = None, flex_display: bool = False, content_gate: bool = False,
                 keep_active: bool = False,
                 clipboard_autosync: bool = True, capture_orientation: str | None = None,
                 show_touches: bool = False, log_level: str = "info", extra: dict | None = None):
        self.serial = serial
        self.opts = dict(video_source=video_source, display_id=display_id, new_display=new_display,
                         max_size=max_size, video_bit_rate=video_bit_rate, max_fps=max_fps, video_codec=video_codec,
                         video_encoder=video_encoder, video_codec_options=video_codec_options, audio=audio,
                         control=control, camera_facing=camera_facing, camera_id=camera_id, camera_size=camera_size,
                         camera_ar=camera_ar, camera_fps=camera_fps, camera_high_speed=camera_high_speed,
                         power_on=power_on, stay_awake=stay_awake, vd_system_decorations=vd_system_decorations,
                         vd_destroy_content=vd_destroy_content, display_ime_policy=display_ime_policy,
                         flex_display=flex_display, content_gate=content_gate, keep_active=keep_active,
                         clipboard_autosync=clipboard_autosync,
                         capture_orientation=capture_orientation, show_touches=show_touches, log_level=log_level)
        self.extra = dict(extra or {})
        self.start_app = start_app
        self.scid = random.randint(0, 0x7FFFFFFF)
        self.port: int | None = None
        self.proc: asyncio.subprocess.Process | None = None
        self.device_name = ""
        self.codec = ""
        self.width = 0
        self.height = 0
        self.closed = False
        self.on_packet = None       # callable(VideoPacket)
        self.on_size = None         # callable(StreamSize)
        self.on_clipboard = None    # callable(str)
        self.on_content = None      # callable(shown: bool, pkg: str), content_gate sessions (patched server)
        self.content_shown: bool | None = None
        self.content_pkg = ""
        self.on_close = None        # callable()
        self.server_log: list[str] = []
        self.virtual_display_id: int | None = None       # set for new_display sessions (parsed from the log)
        self.virtual_display_size: tuple[int, int, int] | None = None
        self._video: tuple[asyncio.StreamReader, asyncio.StreamWriter] | None = None
        self._audio: tuple[asyncio.StreamReader, asyncio.StreamWriter] | None = None
        self._ctrl: tuple[asyncio.StreamReader, asyncio.StreamWriter] | None = None
        self._tasks: list[asyncio.Task] = []
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=240)
        self._ctrl_lock = asyncio.Lock()
        self._clip_seq = 0
        self._gestures: dict[str, dict] = {}

    # ------------------------------------------------------------------ lifecycle
    def server_args(self) -> list[str]:
        o = self.opts
        a = [f"scid={self.scid:08x}", f"log_level={o['log_level']}", "tunnel_forward=true", "cleanup=false",
             f"audio={_b(o['audio'])}", f"control={_b(o['control'])}", f"video_codec={o['video_codec']}",
             f"video_bit_rate={_bitrate(o['video_bit_rate'])}", f"power_on={_b(o['power_on'])}"]
        if o["max_size"]:
            a.append(f"max_size={int(o['max_size'])}")
        if o["max_fps"]:
            a.append(f"max_fps={o['max_fps']}")
        if o["stay_awake"]:
            a.append("stay_awake=true")
        if o["show_touches"]:
            a.append("show_touches=true")
        if not o["clipboard_autosync"]:
            a.append("clipboard_autosync=false")
        for k in ("video_encoder", "video_codec_options", "capture_orientation", "display_ime_policy"):
            if o[k]:
                a.append(f"{k}={o[k]}")
        if o["video_source"] == "camera":
            a.append("video_source=camera")
            for k in ("camera_id", "camera_facing", "camera_size", "camera_ar", "camera_fps"):
                if o[k]:
                    a.append(f"{k}={o[k]}")
            if o["camera_high_speed"]:
                a.append("camera_high_speed=true")
        elif o["new_display"] is not None:
            a.append(f"new_display={o['new_display']}")
            if not o["vd_system_decorations"]:
                a.append("vd_system_decorations=false")
            if not o["vd_destroy_content"]:
                a.append("vd_destroy_content=false")
            if o["flex_display"]:
                a.append("flex_display=true")
            if o["content_gate"]:
                a.append("content_gate=true")     # patched server: no frames while only the launcher is shown
        elif o["display_id"] is not None:
            a.append(f"display_id={o['display_id']}")
        if o["keep_active"]:
            a.append("keep_active=true")
        a += [f"{k}={v}" for k, v in self.extra.items()]   # raw pass-through server options
        return a

    async def start(self, timeout: float = 15.0):
        s = self.serial
        if not s:
            raise ScrcpyError("phone not reachable over adb")
        await sweep_stale_forwards(s)
        await push_server(s)
        socket_name = f"scrcpy_{self.scid:08x}"
        rc, out = await adb("forward", "tcp:0", f"localabstract:{socket_name}", serial=s)
        if rc != 0 or not out.strip().splitlines()[-1].strip().isdigit():
            raise ScrcpyError(f"adb forward failed: {out}")
        self.port = int(out.strip().splitlines()[-1])
        cmd = [str(ADB), "-s", s, "shell", f"CLASSPATH={SERVER_REMOTE}", "app_process", "/",
               "com.genymobile.scrcpy.Server", SERVER_VERSION] + self.server_args()
        log.info("scrcpy-server %s", " ".join(cmd[4:]))
        self.proc = await asyncio.create_subprocess_exec(*cmd, stdin=asyncio.subprocess.DEVNULL,
                                                         stdout=asyncio.subprocess.PIPE,
                                                         stderr=asyncio.subprocess.STDOUT, creationflags=NO_WINDOW)
        self._tasks.append(asyncio.create_task(self._pump_log()))
        try:
            await asyncio.wait_for(self._connect(), timeout)
        except BaseException as e:
            err = "\n".join(self.server_log[-8:])
            await self.stop()
            if isinstance(e, asyncio.TimeoutError):
                raise ScrcpyError(f"scrcpy-server did not start in time\n{err}") from None
            raise
        return self

    async def _connect(self):
        o = self.opts
        # 1st socket: retry until the server accepted it (dummy byte), adb closes it immediately otherwise
        first = None
        for _ in range(200):
            if self.proc.returncode is not None:
                raise ScrcpyError("scrcpy-server exited:\n" + "\n".join(self.server_log[-10:]))
            try:
                r, w = await asyncio.open_connection("127.0.0.1", self.port)
            except OSError:
                await asyncio.sleep(0.1)
                continue
            try:
                b = await asyncio.wait_for(r.readexactly(1), 2)
                first = (r, w)
                break
            except (asyncio.IncompleteReadError, ConnectionError, asyncio.TimeoutError):
                w.close()
                await asyncio.sleep(0.1)
        if not first:
            raise ScrcpyError("could not connect to scrcpy-server")
        self._video = first
        if o["audio"]:
            self._audio = await asyncio.open_connection("127.0.0.1", self.port)
        if o["control"]:
            self._ctrl = await asyncio.open_connection("127.0.0.1", self.port)
        for rw in (self._video, self._audio, self._ctrl):
            if rw:
                _nodelay(rw[1])
        r = self._video[0]
        name = await r.readexactly(64)
        self.device_name = name.split(b"\0", 1)[0].decode("utf-8", "replace")
        codec_id = struct.unpack(">I", await r.readexactly(4))[0]
        if codec_id == 0:
            raise ScrcpyError("video stream disabled by the device")
        if codec_id == 1:
            raise ScrcpyError("video configuration error on the device:\n" + "\n".join(self.server_log[-10:]))
        self.codec = CODEC_IDS.get(codec_id, hex(codec_id))
        hdr = await r.readexactly(12)
        if not hdr[0] & 0x80:
            raise ScrcpyError("unexpected first video packet (no session header)")
        _, self.width, self.height = struct.unpack(">III", hdr)
        log.info("scrcpy connected: %s %s %dx%d", self.device_name, self.codec, self.width, self.height)
        self._tasks.append(asyncio.create_task(self._read_video()))
        if self._ctrl:
            self._tasks.append(asyncio.create_task(self._read_device_msgs()))
            if self.start_app:
                await self.start_app_cmd(self.start_app)

    async def _pump_log(self):
        try:
            while True:
                line = await self.proc.stdout.readline()
                if not line:
                    break
                t = line.decode("utf-8", "replace").rstrip()
                self.server_log.append(t)
                m = re.search(r"New display: (\d+)x(\d+)/(\d+) \(id=(\d+)\)", t)
                if m:
                    self.virtual_display_id = int(m.group(4))
                    self.virtual_display_size = (int(m.group(1)), int(m.group(2)), int(m.group(3)))
                del self.server_log[:-200]
                log.info("[server %08x] %s", self.scid, t)
        except Exception:
            pass
        finally:
            if not self.closed:
                self._emit(None)

    async def _read_video(self):
        r = self._video[0]
        try:
            while True:
                hdr = await r.readexactly(12)
                if hdr[0] & 0x80:
                    _, w, h = struct.unpack(">III", hdr)
                    self.width, self.height = w, h
                    ev = StreamSize(w, h, bool(hdr[3] & 1))
                    if self.on_size:
                        self.on_size(ev)
                    else:
                        self._emit(ev)
                    continue
                pts_flags, size = struct.unpack(">QI", hdr)
                data = await r.readexactly(size)
                config = bool(pts_flags & FLAG_CONFIG)
                pkt = VideoPacket(config, bool(pts_flags & FLAG_KEY_FRAME), None if config else pts_flags & PTS_MASK,
                                  data)
                if self.on_packet:
                    self.on_packet(pkt)
                else:
                    self._emit(pkt)
        except (asyncio.IncompleteReadError, ConnectionError, OSError):
            pass
        except asyncio.CancelledError:
            raise
        finally:
            self._emit(None)

    def _emit(self, ev):
        if ev is None:
            if self.on_close and not getattr(self, "_close_signalled", False):
                self._close_signalled = True
                try:
                    self.on_close()
                except Exception:
                    log.exception("on_close")
        try:
            self._queue.put_nowait(ev)
        except asyncio.QueueFull:
            # consumer is late: drop everything and ask for a fresh keyframe
            while not self._queue.empty():
                self._queue.get_nowait()
            if ev is None:
                self._queue.put_nowait(None)
            else:
                asyncio.create_task(self.reset_video())

    async def _read_device_msgs(self):
        r = self._ctrl[0]
        try:
            while True:
                t = (await r.readexactly(1))[0]
                if t == 0:          # clipboard
                    n = struct.unpack(">I", await r.readexactly(4))[0]
                    text = (await r.readexactly(n)).decode("utf-8", "replace")
                    if self.on_clipboard:
                        self.on_clipboard(text)
                elif t == 1:        # ack clipboard
                    await r.readexactly(8)
                elif t == 2:        # uhid output
                    _id, n = struct.unpack(">HH", await r.readexactly(4))
                    await r.readexactly(n)
                elif t == 0x40:     # content gate state (Symphone patch): u8 shown, u16 len, utf-8 package
                    shown, n = struct.unpack(">BH", await r.readexactly(3))
                    pkg = (await r.readexactly(n)).decode("utf-8", "replace")
                    self.content_shown, self.content_pkg = bool(shown), pkg
                    if self.on_content:
                        self.on_content(bool(shown), pkg)
                else:
                    log.warning("unknown device message %d", t)
                    return
        except (asyncio.IncompleteReadError, ConnectionError, OSError):
            pass

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.closed and self._queue.empty():
            raise StopAsyncIteration
        ev = await self._queue.get()
        if ev is None:
            raise StopAsyncIteration
        return ev

    async def stop(self):
        if self.closed:
            return
        self.closed = True
        for rw in (self._video, self._audio, self._ctrl):
            if rw:
                try:
                    rw[1].close()
                except Exception:
                    pass
        me = asyncio.current_task()
        for t in self._tasks:
            if t is not me:
                t.cancel()
        if self.proc and self.proc.returncode is None:
            try:
                await asyncio.wait_for(self.proc.wait(), 1.5)
            except asyncio.TimeoutError:
                try:
                    self.proc.kill()
                except ProcessLookupError:
                    pass
                # the device-side process may survive the local adb client: kill it by its unique scid
                await adb("shell", f"pkill -f 'scid={self.scid:08x}'", serial=self.serial, timeout=5)
        if self.port:
            await adb("forward", "--remove", f"tcp:{self.port}", serial=self.serial, timeout=5)
            self.port = None
        self._emit(None)
        log.info("scrcpy session %08x stopped", self.scid)

    async def __aenter__(self):
        return await self.start()

    async def __aexit__(self, *exc):
        await self.stop()

    # ------------------------------------------------------------------ control: raw messages
    async def send(self, data: bytes):
        if not self._ctrl or self.closed:
            return
        async with self._ctrl_lock:
            w = self._ctrl[1]
            w.write(data)
            try:
                await w.drain()
            except (ConnectionError, OSError):
                pass

    def _pos(self, x: float, y: float) -> bytes:
        return struct.pack(">iiHH", int(round(x)), int(round(y)), self.width, self.height)

    async def keycode(self, action: int, keycode: int, repeat: int = 0, meta: int = 0):
        await self.send(struct.pack(">BBiii", MSG_INJECT_KEYCODE, action, keycode, repeat, meta))

    async def key(self, keycode: int | str, meta: int = 0):
        """Press + release (keycode int or name from KEYCODES)."""
        kc = KEYCODES[keycode.upper()] if isinstance(keycode, str) else keycode
        await self.keycode(ACTION_DOWN, kc, 0, meta)
        await self.keycode(ACTION_UP, kc, 0, meta)

    async def text(self, text: str):
        raw = text.encode("utf-8")[:300]
        while raw:   # don't cut a UTF-8 sequence
            try:
                raw.decode("utf-8")
                break
            except UnicodeDecodeError:
                raw = raw[:-1]
        await self.send(struct.pack(">BI", MSG_INJECT_TEXT, len(raw)) + raw)

    async def touch(self, action: int, pointer_id: int, x: float, y: float, pressure: float = 1.0,
                    action_button: int = 0, buttons: int = 0):
        """x, y in video-frame pixels (0..width, 0..height)."""
        p = 0xFFFF if pressure >= 1 else max(0, int(pressure * 65536))
        await self.send(struct.pack(">BBq", MSG_INJECT_TOUCH, action, pointer_id) + self._pos(x, y) +
                        struct.pack(">HII", p, action_button, buttons))

    async def scroll(self, x: float, y: float, hscroll: float, vscroll: float, buttons: int = 0):
        """hscroll/vscroll in 'wheel notches' (range [-16, 16]); positive vscroll scrolls up (content moves down)."""
        def fp(v):
            v = max(-1.0, min(1.0, v / 16))
            return 0x7FFF if v >= 1 else max(-0x8000, int(v * 32768))
        await self.send(struct.pack(">B", MSG_INJECT_SCROLL) + self._pos(x, y) +
                        struct.pack(">hhI", fp(hscroll), fp(vscroll), buttons))

    async def set_clipboard(self, text: str, paste: bool = False):
        self._clip_seq += 1
        raw = text.encode("utf-8")[: (1 << 18) - 14]
        await self.send(struct.pack(">BQBI", MSG_SET_CLIPBOARD, self._clip_seq, 1 if paste else 0, len(raw)) + raw)

    async def get_clipboard(self, copy_key: int = 0):
        await self.send(struct.pack(">BB", MSG_GET_CLIPBOARD, copy_key))

    async def start_app_cmd(self, name: str):
        """'pkg', '+pkg' (force-stop first) or '?App name'. Starts on the streamed (virtual) display."""
        raw = name.encode("utf-8")[:255]
        await self.send(struct.pack(">BB", MSG_START_APP, len(raw)) + raw)

    async def simple(self, msg_type: int):
        await self.send(bytes([msg_type]))

    async def rotate(self):
        await self.simple(MSG_ROTATE_DEVICE)

    async def reset_video(self):
        await self.simple(MSG_RESET_VIDEO)

    async def expand_notifications(self):
        await self.simple(MSG_EXPAND_NOTIFICATION_PANEL)

    async def expand_settings(self):
        await self.simple(MSG_EXPAND_SETTINGS_PANEL)

    async def collapse_panels(self):
        await self.simple(MSG_COLLAPSE_PANELS)

    async def resize_display(self, w: int, h: int):
        """Only for new_display + flex_display sessions."""
        await self.send(struct.pack(">BHH", MSG_RESIZE_DISPLAY, w, h))

    async def camera_torch(self, on: bool):
        await self.send(struct.pack(">BB", MSG_CAMERA_SET_TORCH, 1 if on else 0))

    async def camera_zoom(self, zoom_in: bool):
        await self.simple(MSG_CAMERA_ZOOM_IN if zoom_in else MSG_CAMERA_ZOOM_OUT)

    # ------------------------------------------------------------------ gestures
    # Pointer ids used for synthetic gestures (distinct from real touch ids, which the viewer keeps < 64).
    PINCH_IDS = (0x70, 0x71)
    PAN_ID = 0x72

    async def pinch(self, x: float, y: float, scale: float, steps: int = 10, duration: float = 0.18,
                    span: float | None = None, angle_deg: float = 0.0):
        """Complete two-finger pinch centered at (x, y): fingers start `span` px apart and end span*scale apart."""
        span = span or min(self.width, self.height) * 0.25
        await self.pinch_begin(x, y, span, angle_deg)
        for i in range(1, steps + 1):
            await asyncio.sleep(duration / steps)
            await self.pinch_update(scale=1 + (scale - 1) * i / steps)
        await asyncio.sleep(0.05)    # hold still so the release doesn't fling
        await self.pinch_end()

    async def pinch_begin(self, x: float, y: float, span: float | None = None, angle_deg: float = 0.0):
        if "pinch" in self._gestures:
            await self.pinch_end()
        span = span or min(self.width, self.height) * 0.25
        g = {"cx": x, "cy": y, "span": span, "scale": 1.0, "dx": 0.0, "dy": 0.0,
             "ux": math.cos(math.radians(angle_deg)), "uy": math.sin(math.radians(angle_deg))}
        self._gestures["pinch"] = g
        (x0, y0), (x1, y1) = self._pinch_points(g)
        await self.touch(ACTION_DOWN, self.PINCH_IDS[0], x0, y0)
        await self.touch(ACTION_DOWN, self.PINCH_IDS[1], x1, y1)

    async def pinch_update(self, scale: float | None = None, dx: float = 0.0, dy: float = 0.0, rel_scale=None):
        """Absolute `scale` (relative to the start span) or multiplicative `rel_scale`; dx/dy move both fingers."""
        g = self._gestures.get("pinch")
        if not g:
            return
        if rel_scale is not None:
            g["scale"] *= rel_scale
        elif scale is not None:
            g["scale"] = scale
        g["dx"] += dx
        g["dy"] += dy
        # Re-grip like a human when the fingers would leave the screen / collide: lift and start a new pinch
        lim = min(self.width, self.height)
        cur = g["span"] * g["scale"]
        if cur > lim * 0.9 or cur < lim * 0.05:
            cx, cy = g["cx"] + g["dx"], g["cy"] + g["dy"]
            g["scale"] = min(max(g["scale"], 0.05 * lim / g["span"]), 0.9 * lim / g["span"])
            await self.pinch_end()
            await self.pinch_begin(cx, cy, lim * (0.15 if cur > lim * 0.9 else 0.85),
                                   math.degrees(math.atan2(g["uy"], g["ux"])))
            return
        (x0, y0), (x1, y1) = self._pinch_points(g)
        await self.touch(ACTION_MOVE, self.PINCH_IDS[0], x0, y0)
        await self.touch(ACTION_MOVE, self.PINCH_IDS[1], x1, y1)

    async def pinch_end(self):
        g = self._gestures.pop("pinch", None)
        if not g:
            return
        (x0, y0), (x1, y1) = self._pinch_points(g)
        await self.touch(ACTION_UP, self.PINCH_IDS[1], x1, y1, pressure=0)
        await self.touch(ACTION_UP, self.PINCH_IDS[0], x0, y0, pressure=0)

    def _pinch_points(self, g):
        half = g["span"] * g["scale"] / 2
        cx, cy = g["cx"] + g["dx"], g["cy"] + g["dy"]
        pts = []
        for sgn in (-1, 1):
            px = cx + sgn * half * g["ux"]
            py = cy + sgn * half * g["uy"]
            pts.append((min(max(px, 0), self.width - 1), min(max(py, 0), self.height - 1)))
        return pts

    async def pan_begin(self, x: float, y: float):
        """One-finger drag used to forward touchpad two-finger scrolling as a real 'pan'."""
        if "pan" in self._gestures:
            await self.pan_end()
        self._gestures["pan"] = {"x": x, "y": y}
        await self.touch(ACTION_DOWN, self.PAN_ID, x, y)

    async def pan_update(self, dx: float, dy: float):
        g = self._gestures.get("pan")
        if not g:
            return
        g["x"] = min(max(g["x"] + dx, 0), self.width - 1)
        g["y"] = min(max(g["y"] + dy, 0), self.height - 1)
        await self.touch(ACTION_MOVE, self.PAN_ID, g["x"], g["y"])

    async def pan_end(self):
        g = self._gestures.pop("pan", None)
        if g:
            await self.touch(ACTION_UP, self.PAN_ID, g["x"], g["y"], pressure=0)

    async def two_finger_pan(self, x: float, y: float, dx: float, dy: float, steps: int = 10,
                             duration: float = 0.2, gap: float | None = None):
        """Two fingers side by side dragged by (dx, dy) — e.g. two-finger pan/tilt in Maps."""
        gap = gap or min(self.width, self.height) * 0.12
        a, b = self.PINCH_IDS
        await self.touch(ACTION_DOWN, a, x - gap / 2, y)
        await self.touch(ACTION_DOWN, b, x + gap / 2, y)
        for i in range(1, steps + 1):
            await asyncio.sleep(duration / steps)
            ox, oy = dx * i / steps, dy * i / steps
            await self.touch(ACTION_MOVE, a, x - gap / 2 + ox, y + oy)
            await self.touch(ACTION_MOVE, b, x + gap / 2 + ox, y + oy)
        await asyncio.sleep(0.05)
        await self.touch(ACTION_UP, b, x + gap / 2 + dx, y + dy, pressure=0)
        await self.touch(ACTION_UP, a, x - gap / 2 + dx, y + dy, pressure=0)

    async def tap(self, x: float, y: float, pointer_id: int = POINTER_ID_GENERIC_FINGER):
        await self.touch(ACTION_DOWN, pointer_id, x, y)
        await asyncio.sleep(0.04)
        await self.touch(ACTION_UP, pointer_id, x, y, pressure=0)

    async def swipe(self, x0, y0, x1, y1, duration=0.25, steps=12, pointer_id=POINTER_ID_GENERIC_FINGER):
        await self.touch(ACTION_DOWN, pointer_id, x0, y0)
        for i in range(1, steps + 1):
            await asyncio.sleep(duration / steps)
            await self.touch(ACTION_MOVE, pointer_id, x0 + (x1 - x0) * i / steps, y0 + (y1 - y0) * i / steps)
        await self.touch(ACTION_UP, pointer_id, x1, y1, pressure=0)


def _bitrate(v) -> int:
    if isinstance(v, (int, float)):
        return int(v)
    s = str(v).strip().upper()
    mul = {"K": 1_000, "M": 1_000_000, "G": 1_000_000_000}.get(s[-1:], 1)
    return int(float(s.rstrip("KMG")) * mul)


def _nodelay(writer: asyncio.StreamWriter):
    import socket
    sock = writer.get_extra_info("socket")
    if sock is not None:
        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass
