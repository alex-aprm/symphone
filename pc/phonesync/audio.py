"""
Audio bridge.

  PC -> phone : loopback (everything the PC plays) or the PC microphone (pcaudio.py), streamed as PCM to the
                phone app, which plays it on the phone speaker / its connected headset.
  phone -> PC : scrcpy audio capture (see mirror.py) or Bluetooth A2DP sink (AudioPlaybackConnection, Windows).
"""
import asyncio
import logging
import queue
import subprocess
import threading

import aiohttp

from .config import BASE_DIR, IS_WIN, NO_WINDOW
from . import pcaudio

log = logging.getLogger("audio")


class PcToPhone:
    def __init__(self, phone):
        self.phone = phone
        self.mode: str | None = None       # "system" | "mic"
        self._stop = threading.Event()
        self._task: asyncio.Task | None = None
        self.error = ""

    def status(self):
        return {"mode": self.mode, "error": self.error}

    async def start(self, mode: str):
        await self.stop()
        self.mode = mode
        self.error = ""
        self._stop.clear()
        self._task = asyncio.create_task(self._run(mode))

    async def stop(self):
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        self._task = None
        self.mode = None

    async def _run(self, mode):
        q: queue.Queue = queue.Queue(maxsize=50)
        ended = threading.Event()
        cap = None
        try:
            cap = await asyncio.to_thread(pcaudio.open_capture, mode)
            rate, ch = cap.rate, cap.ch
            log.info("streaming %s (%s, %d Hz, %d ch) to phone", mode, cap.name, rate, ch)

            def pump():
                while not self._stop.is_set() and not ended.is_set():
                    data = cap.read()
                    try:
                        q.put_nowait(data)
                    except queue.Full:
                        pass  # drop rather than build latency
                    if not data:
                        break
            threading.Thread(target=pump, daemon=True, name=f"pc-audio-{mode}").start()

            async def body():
                loop = asyncio.get_running_loop()
                while not self._stop.is_set():
                    data = await loop.run_in_executor(None, q.get)
                    if not data:
                        break
                    # coalesce whatever queued up to keep requests efficient
                    while not q.empty():
                        data += q.get_nowait()
                    yield data

            async with await self.phone.request("POST", f"/api/audio?rate={rate}&ch={ch}", data=body(),
                                                 timeout=aiohttp.ClientTimeout(total=None)) as r:
                await r.read()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self.error = str(e)
            log.warning("audio: %s", e)
        finally:
            ended.set()
            if cap is not None:
                cap.close()
            if not self._stop.is_set():
                self.mode = None


class BluetoothSink:
    """Makes the PC a Bluetooth speaker for the (already paired) phone via Windows AudioPlaybackConnection."""

    def __init__(self):
        self.proc: subprocess.Popen | None = None

    @property
    def active(self):
        return self.proc is not None and self.proc.poll() is None

    def start(self, device_hint: str = ""):
        if self.active or not IS_WIN:           # macOS / Linux: pair the phone as a Bluetooth audio source instead
            return
        script = BASE_DIR / "bt_sink.ps1"
        self.proc = subprocess.Popen(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script), "-Hint", device_hint],
            creationflags=NO_WINDOW, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

    def stop(self):
        if self.active:
            self.proc.terminate()
        self.proc = None
