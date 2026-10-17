"""
Audio bridge.

  PC -> phone : WASAPI loopback (everything the PC plays) or the PC microphone, streamed as PCM to the
                phone app, which plays it on the phone speaker / its connected headset.
  phone -> PC : scrcpy audio capture (see mirror.py) or Bluetooth A2DP sink (AudioPlaybackConnection).
"""
import asyncio
import logging
import queue
import subprocess
import threading

import aiohttp

from .config import BASE_DIR

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
        import pyaudiowpatch as pa
        q: queue.Queue = queue.Queue(maxsize=50)
        p = pa.PyAudio()
        try:
            if mode == "system":
                wasapi = p.get_host_api_info_by_type(pa.paWASAPI)
                spk = p.get_device_info_by_index(wasapi["defaultOutputDevice"])
                dev = next((d for d in p.get_loopback_device_info_generator() if spk["name"] in d["name"]), None)
                if dev is None:
                    raise RuntimeError("no loopback device for " + spk["name"])
            else:
                dev = p.get_default_input_device_info()
            rate = int(dev["defaultSampleRate"])
            ch = min(2, int(dev["maxInputChannels"])) or 1
            frames = rate // 100  # 10 ms

            def cb(data, n, t, status):
                try:
                    q.put_nowait(data)
                except queue.Full:
                    pass  # drop rather than build latency
                return (None, pa.paContinue)

            stream = p.open(format=pa.paInt16, channels=ch, rate=rate, input=True, frames_per_buffer=frames,
                            input_device_index=dev["index"], stream_callback=cb)
            log.info("streaming %s (%s, %d Hz, %d ch) to phone", mode, dev["name"], rate, ch)

            async def body():
                loop = asyncio.get_running_loop()
                while not self._stop.is_set():
                    data = await loop.run_in_executor(None, q.get)
                    # coalesce whatever queued up to keep requests efficient
                    while not q.empty():
                        data += q.get_nowait()
                    yield data

            try:
                async with await self.phone.request("POST", f"/api/audio?rate={rate}&ch={ch}", data=body(),
                                                     timeout=aiohttp.ClientTimeout(total=None)) as r:
                    await r.read()
            finally:
                stream.stop_stream()
                stream.close()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self.error = str(e)
            log.warning("audio: %s", e)
        finally:
            p.terminate()
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
        if self.active:
            return
        script = BASE_DIR / "bt_sink.ps1"
        self.proc = subprocess.Popen(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script), "-Hint", device_hint],
            creationflags=subprocess.CREATE_NO_WINDOW, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

    def stop(self):
        if self.active:
            self.proc.terminate()
        self.proc = None
