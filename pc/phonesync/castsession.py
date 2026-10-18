"""Debug-free streams from the phone app itself (no adb): screen casting, the app's own camera, phone audio.

CastSession speaks the phone app's media stream protocol (TCP 47904, docs/protocol-v2.md "Debug-free mode") but
looks like a ScrcpySession to the viewer bridge and the virtual camera: same callbacks (on_packet, on_size,
on_close), same input methods (touch, scroll, keycode, text, gestures). Input is carried out on the phone by the
Symphone accessibility service.
"""
from __future__ import annotations

import asyncio
import json
import logging
import struct

from .scrcpy_client import ScrcpyError, ScrcpySession, StreamSize, VideoPacket

log = logging.getLogger("cast")
CAST_PORT = 47904


class CastSession(ScrcpySession):
    def __init__(self, host: str, token: str, kind: str = "screen", *, port: int = CAST_PORT, max_size: int = 1920,
                 fps: int = 60, bitrate: int = 8_000_000, camera_id: str = "", facing: str = "back",
                 audio: str = "playback", codec: str = "h264"):
        super().__init__("", video_source="camera" if kind == "camera" else "display", max_size=max_size,
                         max_fps=fps, video_bit_rate=bitrate, camera_id=camera_id or None, camera_facing=facing)
        self.host, self.cport, self.token, self.kind = host, port, token, kind
        self.hello = {"token": token, "kind": kind, "max_size": max_size, "fps": fps, "bitrate": bitrate,
                      "camera": camera_id, "facing": facing, "audio": audio,
                      "codec": codec}         # "h265": the phone uses it if it can (the reply says what it sent)
        self.on_audio = None            # callable(pts_us, pcm bytes)  (kind="audio")
        self.cast = True

    async def start(self, timeout: float = 60.0):
        """Connects and waits for the phone to start the stream (screen: until the user allowed casting)."""
        try:
            r, w = await asyncio.wait_for(asyncio.open_connection(self.host, self.cport), 6)
        except (OSError, asyncio.TimeoutError) as e:
            raise ScrcpyError(f"phone not reachable for streaming ({self.host}): {e}") from e
        w.write((json.dumps(self.hello) + "\n").encode())
        await w.drain()
        try:
            line = await asyncio.wait_for(r.readline(), timeout)
        except asyncio.TimeoutError:
            w.close()
            raise ScrcpyError("The phone didn't start the stream (allow screen casting on the phone)")
        try:
            res = json.loads(line or b"{}")
        except ValueError:
            res = {}
        if not res.get("ok"):
            w.close()
            needs = res.get("needs")
            msg = res.get("error") or "the phone refused the stream"
            if needs == "consent":
                msg = "Screen casting wasn't allowed on the phone"
            elif needs == "accessibility":
                msg = "Turn on Symphone in the phone's Accessibility settings to control it from the PC"
            raise ScrcpyError(msg)
        self.width, self.height = int(res.get("width", 0)), int(res.get("height", 0))
        self.codec = res.get("codec", "h264")
        self.device_name = res.get("device", "")
        self._ctrl = (r, w)
        self._tasks.append(asyncio.create_task(self._read_stream()))
        log.info("cast %s from %s: %dx%d %s", self.kind, self.host, self.width, self.height, self.codec)
        return self

    async def _read_stream(self):
        r = self._ctrl[0]
        try:
            while True:
                t = (await r.readexactly(1))[0]
                if t == 0x01:
                    flags, pts, n = struct.unpack(">BQI", await r.readexactly(13))
                    pkt = VideoPacket(config=bool(flags & 1), keyframe=bool(flags & 2), pts=pts,
                                      data=await r.readexactly(n))
                    if self.on_packet:
                        self.on_packet(pkt)
                    else:
                        self._emit(pkt)
                elif t == 0x02:
                    pts, n = struct.unpack(">QI", await r.readexactly(12))
                    data = await r.readexactly(n)
                    if self.on_audio:
                        self.on_audio(pts, data)
                elif t == 0x10:
                    n = struct.unpack(">I", await r.readexactly(4))[0]
                    ev = json.loads((await r.readexactly(n)).decode("utf-8", "replace"))
                    if ev.get("type") == "size":
                        self.width, self.height = int(ev["width"]), int(ev["height"])
                        size = StreamSize(self.width, self.height)
                        if self.on_size:
                            self.on_size(size)
                        else:
                            self._emit(size)
                    elif ev.get("type") == "ended":
                        log.info("cast ended: %s", ev.get("reason"))
                        break
                else:
                    log.warning("cast: unknown packet type %d", t)
                    break
        except (asyncio.IncompleteReadError, ConnectionError, OSError):
            pass
        finally:
            self._emit(None)

    async def stop(self):
        if self.closed:
            return
        self.closed = True
        if self._ctrl:
            try:
                self._ctrl[1].close()
            except Exception:
                pass
        me = asyncio.current_task()
        for t in self._tasks:
            if t is not me:
                t.cancel()
        self._emit(None)
        log.info("cast %s stopped", self.kind)

    # ------------------------------------------------------------------ input (accessibility on the phone)
    async def send_json(self, o: dict):
        await self.send((json.dumps(o, separators=(",", ":")) + "\n").encode())

    async def keycode(self, action: int, keycode: int, repeat: int = 0, meta: int = 0):
        await self.send_json({"t": "key", "code": keycode, "a": action, "repeat": repeat, "meta": meta})

    async def text(self, text: str):
        await self.send_json({"t": "text", "s": text})

    async def touch(self, action: int, pointer_id: int, x: float, y: float, pressure: float = 1.0,
                    action_button: int = 0, buttons: int = 0):
        w, h = max(1, self.width), max(1, self.height)
        await self.send_json({"t": "touch", "a": action, "id": pointer_id & 0xFFFF,
                              "x": round(min(1, max(0, x / w)), 5), "y": round(min(1, max(0, y / h)), 5)})

    async def scroll(self, x: float, y: float, hscroll: float, vscroll: float, buttons: int = 0):
        w, h = max(1, self.width), max(1, self.height)
        await self.send_json({"t": "scroll", "x": round(x / w, 5), "y": round(y / h, 5), "h": hscroll, "v": vscroll})

    async def set_clipboard(self, text: str, paste: bool = False):
        if paste:
            await self.text(text)       # paste into the focused field (the clipboard itself syncs via the app)

    async def get_clipboard(self, copy_key: int = 0):
        pass

    async def start_app_cmd(self, name: str):
        await self.send_json({"t": "launch", "pkg": name.lstrip("+")})

    async def rotate(self):
        await self.send_json({"t": "rotate"})

    async def reset_video(self):
        await self.send_json({"t": "reset"})

    async def expand_notifications(self):
        await self.send_json({"t": "global", "action": "notifications"})

    async def expand_settings(self):
        await self.send_json({"t": "global", "action": "quick_settings"})

    async def collapse_panels(self):
        await self.send_json({"t": "global", "action": "back"})

    async def resize_display(self, w: int, h: int):
        pass

    async def camera_torch(self, on: bool):
        await self.send_json({"t": "torch", "on": bool(on)})

    async def camera_zoom(self, zoom_in: bool):
        await self.send_json({"t": "zoom", "in": bool(zoom_in)})


def phone_host(phone) -> str | None:
    """The phone's address on a network link (casting has no USB path without adb)."""
    for link in phone.links:
        if link.kind in ("wfd", "wifi") and link.base:
            return link.base.split("//", 1)[1].rsplit(":", 1)[0]
    return None
