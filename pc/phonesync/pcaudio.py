"""PC sound in and out, per platform: 16-bit PCM capture (the PC's own output or its microphone) and playback (to the
speakers or to the virtual microphone other apps record from).

  Windows  PyAudioWPatch (WASAPI loopback); the virtual microphone is VB-CABLE ("CABLE Input").
  Linux    PulseAudio / PipeWire tools (parec / pacat): the default sink's monitor is the PC's own sound; the virtual
           microphone is a null sink "symphone_mic" whose monitor is offered as the source "Symphone microphone".
  macOS    sounddevice (Core Audio). The Mac's own sound and the virtual microphone need a loopback device such as
           BlackHole (found by name).
"""
import logging
import queue
import shutil
import subprocess

from .config import IS_WIN, IS_MAC

log = logging.getLogger("pcaudio")
VMIC_SINK = "symphone_mic"


class Capture:
    """Blocking reader: read() returns the next chunk (~10 ms) or b"" when the stream ended."""
    rate = 48000
    ch = 2
    name = ""

    def read(self) -> bytes:
        raise NotImplementedError

    def close(self):
        pass


class Playback:
    def write(self, pcm: bytes):
        raise NotImplementedError

    def close(self):
        pass


def open_capture(mode: str) -> Capture:
    """mode: "system" (everything the PC plays) | "mic" (the default microphone)."""
    if IS_WIN:
        return _WinCapture(mode)
    if IS_MAC:
        return _MacCapture(mode)
    return _PulseCapture(mode)


def open_playback(rate: int = 48000, ch: int = 2, virtual_mic: bool = False) -> Playback:
    if IS_WIN:
        return _WinPlayback(rate, ch, virtual_mic)
    if IS_MAC:
        return _MacPlayback(rate, ch, virtual_mic)
    return _PulsePlayback(rate, ch, virtual_mic)


# ---------------------------------------------------------------------------------------------------- Windows
class _WinCapture(Capture):
    def __init__(self, mode):
        import pyaudiowpatch as pa
        self._q: queue.Queue = queue.Queue(maxsize=50)
        self._p = p = pa.PyAudio()
        try:
            if mode == "system":
                wasapi = p.get_host_api_info_by_type(pa.paWASAPI)
                spk = p.get_device_info_by_index(wasapi["defaultOutputDevice"])
                dev = next((d for d in p.get_loopback_device_info_generator() if spk["name"] in d["name"]), None)
                if dev is None:
                    raise RuntimeError("no loopback device for " + spk["name"])
            else:
                dev = p.get_default_input_device_info()
            self.rate = int(dev["defaultSampleRate"])
            self.ch = min(2, int(dev["maxInputChannels"])) or 1
            self.name = dev["name"]

            def cb(data, n, t, status):
                try:
                    self._q.put_nowait(data)
                except queue.Full:
                    pass  # drop rather than build latency
                return (None, pa.paContinue)

            self._stream = p.open(format=pa.paInt16, channels=self.ch, rate=self.rate, input=True,
                                  frames_per_buffer=self.rate // 100, input_device_index=dev["index"],
                                  stream_callback=cb)
        except Exception:
            p.terminate()
            raise

    def read(self) -> bytes:
        return self._q.get()

    def close(self):
        try:
            self._stream.stop_stream()
            self._stream.close()
        finally:
            self._p.terminate()
            self._q.put_nowait(b"")


class _WinPlayback(Playback):
    def __init__(self, rate, ch, virtual_mic):
        import pyaudiowpatch as pa
        self._p = p = pa.PyAudio()
        idx = None
        if virtual_mic:
            idx = next((i for i in range(p.get_device_count())
                        if "CABLE Input" in p.get_device_info_by_index(i)["name"]
                        and p.get_device_info_by_index(i)["maxOutputChannels"] > 0), None)
        self._out = p.open(format=pa.paInt16, channels=ch, rate=rate, output=True, output_device_index=idx,
                           frames_per_buffer=rate // 50)

    def write(self, pcm: bytes):
        self._out.write(pcm)

    def close(self):
        try:
            self._out.close()
        finally:
            self._p.terminate()


# ---------------------------------------------------------------------------------------------------- Linux
def _need(tool: str):
    if not shutil.which(tool):
        raise RuntimeError(f"{tool} not found (sudo apt install pulseaudio-utils)")


def ensure_virtual_mic() -> bool:
    """Linux: the null sink apps can't hear, and its monitor as a microphone ("Symphone microphone")."""
    try:
        _need("pactl")
        sinks = subprocess.run(["pactl", "list", "short", "sinks"], capture_output=True, text=True, timeout=5).stdout
        if VMIC_SINK not in sinks:
            subprocess.run(["pactl", "load-module", "module-null-sink", f"sink_name={VMIC_SINK}",
                            "sink_properties=device.description=Symphone\\ (phone\\ microphone)"],
                           capture_output=True, timeout=5)
            subprocess.run(["pactl", "load-module", "module-remap-source", f"master={VMIC_SINK}.monitor",
                            "source_name=symphone_mic_src",
                            "source_properties=device.description=Symphone\\ microphone"],
                           capture_output=True, timeout=5)
        return True
    except Exception as e:
        log.warning("virtual microphone: %s", e)
        return False


class _PulseCapture(Capture):
    def __init__(self, mode):
        _need("parec")
        self.rate, self.ch = 48000, 2
        dev = "@DEFAULT_MONITOR@" if mode == "system" else "@DEFAULT_SOURCE@"
        self.name = "PC sound" if mode == "system" else "microphone"
        self._p = subprocess.Popen(["parec", f"--device={dev}", "--format=s16le", f"--rate={self.rate}",
                                    f"--channels={self.ch}", "--raw", "--latency-msec=20",
                                    "--client-name=Symphone"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)

    def read(self) -> bytes:
        return self._p.stdout.read1(self.rate // 100 * self.ch * 2 * 2) if self._p.poll() is None else b""

    def close(self):
        self._p.terminate()


class _PulsePlayback(Playback):
    def __init__(self, rate, ch, virtual_mic):
        _need("pacat")
        args = ["pacat", "--playback", "--format=s16le", f"--rate={rate}", f"--channels={ch}", "--raw",
                "--latency-msec=40", "--client-name=Symphone"]
        if virtual_mic and ensure_virtual_mic():
            args.append(f"--device={VMIC_SINK}")
        self._p = subprocess.Popen(args, stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)

    def write(self, pcm: bytes):
        self._p.stdin.write(pcm)
        self._p.stdin.flush()

    def close(self):
        try:
            self._p.stdin.close()
        except Exception:
            pass
        self._p.terminate()


# ---------------------------------------------------------------------------------------------------- macOS
def _mac_loopback(kind: str) -> int:
    import sounddevice as sd
    for i, d in enumerate(sd.query_devices()):
        if "blackhole" in d["name"].lower() and d[f"max_{kind}_channels"] > 0:
            return i
    raise RuntimeError("needs a loopback audio device: install BlackHole (https://existential.audio/blackhole/)")


class _MacCapture(Capture):
    def __init__(self, mode):
        import sounddevice as sd
        dev = _mac_loopback("input") if mode == "system" else None
        info = sd.query_devices(dev, "input")
        self.rate = int(info["default_samplerate"])
        self.ch = min(2, int(info["max_input_channels"])) or 1
        self.name = info["name"]
        self._q: queue.Queue = queue.Queue(maxsize=50)

        def cb(data, frames, t, status):
            try:
                self._q.put_nowait(bytes(data))
            except queue.Full:
                pass
        self._s = sd.RawInputStream(samplerate=self.rate, channels=self.ch, dtype="int16", device=dev,
                                    blocksize=self.rate // 100, callback=cb)
        self._s.start()

    def read(self) -> bytes:
        return self._q.get()

    def close(self):
        try:
            self._s.stop()
            self._s.close()
        finally:
            self._q.put_nowait(b"")


class _MacPlayback(Playback):
    def __init__(self, rate, ch, virtual_mic):
        import sounddevice as sd
        dev = _mac_loopback("output") if virtual_mic else None
        self._s = sd.RawOutputStream(samplerate=rate, channels=ch, dtype="int16", device=dev, blocksize=rate // 50)
        self._s.start()

    def write(self, pcm: bytes):
        self._s.write(pcm)

    def close(self):
        self._s.stop()
        self._s.close()
