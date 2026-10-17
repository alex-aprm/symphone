"""
Feeds the phone camera into the Windows virtual camera ("Symphone Camera").

The camera only runs while some Windows app is actually reading the virtual camera (the DLL publishes
`consumer_active`): then a scrcpy camera session streams H.264, PyAV decodes it, and frames are rotated / mirrored
(an FFmpeg filter graph), scaled and cropped to fill the size the app asked for, and written as NV12 into the
shared memory.

Rotation = the user's setting + (with auto-rotate) what the phone reports for its current physical orientation
(the Camera2 sensor-orientation formula, GET /api/camera/orientation), so the picture stays upright however the
phone stands.
"""
import asyncio
import logging
import queue
import threading
import time
from fractions import Fraction

from .config import settings
from .vcam import VirtualCamera

log = logging.getLogger("camfeed")


class CameraFeed:
    def __init__(self, core):
        self.core = core
        self.cam = VirtualCamera()
        self.session = None
        self.size = (0, 0)
        self.facing = settings["vcam_facing"] or "back"
        self.fps = 0.0
        self.error = ""
        self._q: queue.Queue = queue.Queue(maxsize=8)
        self._worker: threading.Thread | None = None
        self._idle_since = 0.0
        self.camera_id = ""                 # the camera actually opened
        self.auto_rot = 0                   # from the phone (auto-rotate)
        self._orient_task: asyncio.Task | None = None
        self.cameras: list[dict] = []

    def status(self):
        return {"installed": self.cam.available, "active": self.session is not None, "facing": self.facing,
                "size": list(self.size), "fps": round(self.fps, 1), "error": self.error,
                "camera": settings["vcam_camera_id"] or "", "cameras": self.cameras,
                "mirror": bool(settings["vcam_mirror"]), "rotate": int(settings["vcam_rotate"] or 0),
                "autorotate": bool(settings["vcam_autorotate"]), "rotation": self.rotation}

    @property
    def rotation(self) -> int:
        """Total clockwise rotation applied to the frames."""
        auto = self.auto_rot if settings["vcam_autorotate"] else 0
        return (int(settings["vcam_rotate"] or 0) + auto) % 360

    async def list_cameras(self) -> list[dict]:
        try:
            self.cameras = (await self.core.phone.get_json("/api/cameras", timeout=5)).get("cameras", [])
        except Exception as e:
            log.debug("cameras: %s", e)
        return self.cameras

    async def apply(self, **opts):
        """Settings changed (camera, mirror, rotate, autorotate). Picture options apply live; a new camera restarts."""
        restart = "vcam_camera_id" in opts and opts["vcam_camera_id"] != settings["vcam_camera_id"]
        settings.update(**opts)
        if restart and self.session is not None:
            await self._stop()

    async def loop(self):
        while True:
            try:
                await self._tick()
            except Exception as e:
                self.error = str(e)
                log.warning("camfeed: %s", e)
                await self._stop()
                await asyncio.sleep(3)
            await asyncio.sleep(0.5)

    async def _tick(self):
        if not self.cam.available:
            return
        # adb (scrcpy camera) or, debug-free, the phone app's own camera stream over Wi-Fi
        want = self.cam.consumer_active and (bool(self.core.adbm.serial) or self.core.phone.connected)
        if want:
            self._idle_since = 0
            req = self.cam.requested_size
            if self.session is None or req != self.size:
                await self._start(req)
        elif self.session is not None:
            # small grace period: apps often close and reopen the camera while negotiating formats
            self._idle_since = self._idle_since or time.monotonic()
            if time.monotonic() - self._idle_since > 4:
                await self._stop()

    async def set_facing(self, facing: str):
        self.facing = facing
        settings["vcam_facing"] = facing
        settings["vcam_camera_id"] = ""
        if self.session is not None:
            await self._stop()        # the loop restarts it with the new camera

    async def _start(self, size):
        await self._stop()
        from .scrcpy_client import ScrcpySession
        w, h = size
        if not self.cameras:
            await self.list_cameras()
        cid = settings["vcam_camera_id"] or ""
        if cid and not any(c.get("id") == cid for c in self.cameras or [{"id": cid}]):
            cid = ""
        if not cid:
            cid = next((c["id"] for c in self.cameras if c.get("facing") == self.facing), "")
        self.camera_id = cid
        log.info("starting phone camera %s (%s) for virtual camera %dx%d", cid or "-", self.facing, w, h)
        self.size = size
        self.error = ""
        cam = {"camera_id": cid} if cid else {"camera_facing": self.facing}
        size = max(w, h) * 2 if self.rotation % 180 else max(w, h)
        if self.core.adbm.serial:
            s = ScrcpySession(self.core.adbm.serial, video_source="camera", max_size=size, camera_fps=30, max_fps=30,
                              video_bit_rate="16M", control=False, power_on=False, **cam)
        else:
            from .castsession import CastSession, phone_host
            host = phone_host(self.core.phone)
            if not host:
                raise RuntimeError("phone not reachable over Wi-Fi")
            s = CastSession(host, self.core.phone.token, "camera", max_size=size, fps=30, bitrate=16_000_000,
                            camera_id=cid, facing=self.facing)
        s.on_packet = self._on_packet
        await s.start()
        self.session = s
        self._worker = threading.Thread(target=self._decode_loop, args=(w, h), daemon=True, name="camfeed-decode")
        self._worker.start()
        self._orient_task = asyncio.create_task(self._orientation_loop())

    async def _orientation_loop(self):
        """While the camera runs: the phone's physical orientation -> upright picture (auto-rotate)."""
        while self.session is not None:
            if settings["vcam_autorotate"]:
                try:
                    r = await self.core.phone.get_json(f"/api/camera/orientation?id={self.camera_id}", timeout=3)
                    rot = int(r.get("rotation", 0)) % 360
                    if rot != self.auto_rot:
                        log.info("camera orientation: %d", rot)
                        self.auto_rot = rot
                except Exception:
                    pass
            await asyncio.sleep(0.7)

    async def _stop(self):
        s, self.session = self.session, None
        if self._orient_task:
            self._orient_task.cancel()
            self._orient_task = None
        if s is not None:
            log.info("stopping phone camera")
            try:
                await s.stop()
            except Exception:
                pass
        if self._worker is not None:
            self._q.put(None)
            self._worker.join(2)
            self._worker = None
            while not self._q.empty():
                self._q.get_nowait()

    def _on_packet(self, pkt):
        try:
            self._q.put_nowait(pkt)
        except queue.Full:
            pass   # decoder is behind; dropping a packet only costs a few frames until the next keyframe

    def _decode_loop(self, w: int, h: int):
        import av
        ctx = av.CodecContext.create("h264", "r")
        ctx.thread_type = "AUTO"
        n, t0 = 0, time.monotonic()
        y_size = w * h
        graph, gkey = None, None
        while True:
            pkt = self._q.get()
            if pkt is None:
                break
            try:
                frames = ctx.decode(av.Packet(pkt.data))
            except av.error.InvalidDataError:
                continue
            for frame in frames:
                key = (frame.width, frame.height, frame.format.name, self.rotation, bool(settings["vcam_mirror"]))
                if key != gkey:
                    graph, gkey = _graph(frame, w, h, key[3], key[4]), key
                frame.pts, frame.time_base = n, Fraction(1, 30)
                graph.push(frame)
                f = graph.pull()
                buf = bytearray(y_size * 3 // 2)
                _copy_plane(f.planes[0], buf, 0, w, h)
                _copy_plane(f.planes[1], buf, y_size, w, h // 2)
                self.cam.write_nv12(buf, w, h)
                n += 1
                el = time.monotonic() - t0
                if el >= 2:
                    self.fps, n, t0 = n / el, 0, time.monotonic()


def _graph(frame, w: int, h: int, rotation: int, mirror: bool):
    """rotate (clockwise) -> mirror -> scale to cover w x h -> center crop -> NV12."""
    import av
    g = av.filter.Graph()
    chain = [g.add_buffer(width=frame.width, height=frame.height, format=frame.format.name, time_base=Fraction(1, 30))]
    steps = {90: [("transpose", "clock")], 180: [("hflip", ""), ("vflip", "")], 270: [("transpose", "cclock")]}
    for name, arg in steps.get(rotation, []) + ([("hflip", "")] if mirror else []) + [
            ("scale", f"{w}:{h}:force_original_aspect_ratio=increase"), ("crop", f"{w}:{h}"), ("format", "nv12")]:
        chain.append(g.add(name, arg) if arg else g.add(name))
    chain.append(g.add("buffersink"))
    for a, b in zip(chain, chain[1:]):
        a.link_to(b)
    g.configure()
    return g


def _copy_plane(plane, dst: bytearray, off: int, row_bytes: int, rows: int):
    src = memoryview(plane)
    stride = plane.line_size
    if stride == row_bytes:
        dst[off:off + row_bytes * rows] = src[:row_bytes * rows]
        return
    for r in range(rows):
        dst[off + r * row_bytes: off + (r + 1) * row_bytes] = src[r * stride: r * stride + row_bytes]
