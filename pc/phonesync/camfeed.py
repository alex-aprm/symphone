"""
Feeds the phone camera into the Windows virtual camera ("Symphone Camera").

The camera only runs while some Windows app is actually reading the virtual camera (the DLL publishes
`consumer_active`): then a scrcpy camera session streams H.264, PyAV decodes it, and frames are scaled to the
size the app asked for and written as NV12 into the shared memory.
"""
import asyncio
import logging
import queue
import threading
import time

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

    def status(self):
        return {"installed": self.cam.available, "active": self.session is not None, "facing": self.facing,
                "size": list(self.size), "fps": round(self.fps, 1), "error": self.error}

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
        want = self.cam.consumer_active and bool(self.core.adbm.serial)
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
        if self.session is not None:
            await self._stop()        # the loop restarts it with the new camera

    async def _start(self, size):
        await self._stop()
        from .scrcpy_client import ScrcpySession
        w, h = size
        ar = f"{w}:{h}"
        log.info("starting phone camera (%s) for virtual camera %dx%d", self.facing, w, h)
        self.size = size
        self.error = ""
        s = ScrcpySession(self.core.adbm.serial, video_source="camera", camera_facing=self.facing, camera_ar=ar,
                          max_size=max(w, h), camera_fps=30, max_fps=30, video_bit_rate="16M", control=False,
                          power_on=False)
        s.on_packet = self._on_packet
        await s.start()
        self.session = s
        self._worker = threading.Thread(target=self._decode_loop, args=(w, h), daemon=True, name="camfeed-decode")
        self._worker.start()

    async def _stop(self):
        s, self.session = self.session, None
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
        while True:
            pkt = self._q.get()
            if pkt is None:
                break
            try:
                frames = ctx.decode(av.Packet(pkt.data))
            except av.error.InvalidDataError:
                continue
            for frame in frames:
                f = frame.reformat(width=w, height=h, format="nv12")
                buf = bytearray(y_size * 3 // 2)
                _copy_plane(f.planes[0], buf, 0, w, h)
                _copy_plane(f.planes[1], buf, y_size, w, h // 2)
                self.cam.write_nv12(buf, w, h)
                n += 1
                el = time.monotonic() - t0
                if el >= 2:
                    self.fps, n, t0 = n / el, 0, time.monotonic()


def _copy_plane(plane, dst: bytearray, off: int, row_bytes: int, rows: int):
    src = memoryview(plane)
    stride = plane.line_size
    if stride == row_bytes:
        dst[off:off + row_bytes * rows] = src[:row_bytes * rows]
        return
    for r in range(rows):
        dst[off + r * row_bytes: off + (r + 1) * row_bytes] = src[r * stride: r * stride + row_bytes]
