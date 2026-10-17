"""scrcpy sessions: screen projection with touch, apps in their own windows, camera, and phone audio capture."""
import asyncio
import ctypes
import logging
import shutil
import subprocess
from ctypes import wintypes

from .adb import env_for_scrcpy
from .config import SCRCPY, SCRCPY_DIR, LOG_DIR, settings

log = logging.getLogger("mirror")
user32 = ctypes.windll.user32


def focus_window(title_prefix: str) -> bool:
    found = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, _):
        n = user32.GetWindowTextLengthW(hwnd)
        if n:
            buf = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(hwnd, buf, n + 1)
            if buf.value.startswith(title_prefix) and user32.IsWindowVisible(hwnd):
                found.append(hwnd)
                return False
        return True

    user32.EnumWindows(cb, 0)
    if found:
        user32.ShowWindow(found[0], 9)  # SW_RESTORE
        user32.SetForegroundWindow(found[0])
        return True
    return False


class Mirror:
    def __init__(self, core):
        self.core = core
        self.procs: dict[str, subprocess.Popen] = {}

    def running(self) -> dict:
        return {k: True for k, p in self.procs.items() if p.poll() is None}

    def _spawn(self, key: str, args: list[str], exe=None) -> bool:
        p = self.procs.get(key)
        if p and p.poll() is None:
            return False
        serial = self.core.adbm.serial
        if not serial:
            raise ConnectionError("phone not reachable over adb (USB or wireless debugging)")
        cmd = [str(exe or SCRCPY), "-s", serial] + args
        log.info("scrcpy %s", " ".join(args))
        logf = open(LOG_DIR / f"scrcpy-{key.replace(':', '_')}.log", "w", encoding="utf-8", errors="replace")
        self.procs[key] = subprocess.Popen(cmd, cwd=str(SCRCPY_DIR), env=env_for_scrcpy(), stdout=logf, stderr=logf,
                                           creationflags=subprocess.CREATE_NO_WINDOW)
        return True

    def stop(self, key: str):
        p = self.procs.pop(key, None)
        if p and p.poll() is None:
            p.terminate()

    def stop_all(self):
        for k in list(self.procs):
            self.stop(k)

    def _video_args(self):
        a = ["--video-bit-rate", settings["mirror_bitrate"], "--max-fps", str(settings["mirror_fps"]),
             "--keyboard=uhid", "--disable-screensaver"]
        if settings["mirror_max_size"]:
            a += ["--max-size", str(settings["mirror_max_size"])]
        return a

    async def screen(self, unlock=True):
        """Main display in the in-app viewer (unlocks first: the lock screen is a secure layer)."""
        if unlock and settings["unlock_on_mirror"]:
            await self.core.unlock()
        from . import screen_ws
        await screen_ws.open_window("screen", mode="main")

    async def screen_sdl(self, unlock=True):
        """Legacy scrcpy SDL window (kept as a fallback)."""
        if focus_window(self._title("Phone")):
            return
        if unlock and settings["unlock_on_mirror"]:
            await self.core.unlock()
        args = self._video_args() + ["--window-title", self._title("Phone"), "--stay-awake"]
        if settings["turn_screen_off"]:
            args.append("--turn-screen-off")
        if not settings["mirror_audio"] or self.running().get("audio:output"):
            args.append("--no-audio")
        self._spawn("screen", args)

    async def app(self, pkg: str, label: str = "", own_window=True):
        if own_window:
            from . import screen_ws
            await screen_ws.open_window("app", pkg=pkg, label=label or pkg)
            return
        if not own_window:
            if settings["unlock_on_mirror"]:
                await self.core.unlock()
            await self.core.phone.post_json("/api/launch", {"pkg": pkg})
            return await self.screen(unlock=False)
        title = self._title(label or pkg)
        if focus_window(title):
            return
        # Virtual display: works while the phone is locked in a pocket, without waking its screen.
        args = self._video_args() + ["--window-title", title, "--new-display", f"--start-app={pkg}",
                                     "--audio-source=playback", "--no-power-on"]
        self._spawn(f"app:{pkg}", args)

    async def phone(self):
        """The whole phone in the in-app viewer: the real screen if the phone is in use, otherwise a virtual
        desktop that works while locked without waking the screen."""
        from . import screen_ws
        await screen_ws.open_window("screen", mode="auto")

    async def phone_sdl(self):
        """The whole phone UI (launcher + apps) on a virtual display: no unlock, no screen wake."""
        title = self._title("Phone screen")
        if focus_window(title):
            return
        # The launcher's secondary-display home appears automatically (system decorations on).
        args = self._video_args() + ["--window-title", title, "--new-display", "--no-power-on",
                                     "--audio-source=playback"]
        self._spawn("phone", args)

    async def camera(self, facing="back"):
        from . import screen_ws
        await screen_ws.open_window("camera", facing=facing)

    async def camera_sdl(self, facing="back"):
        title = self._title(f"Camera ({facing})")
        if focus_window(title):
            return
        args = ["--video-source=camera", f"--camera-facing={facing}", "--camera-ar=4:3", "--max-size=1920",
                "--camera-fps=30", "--no-audio", "--window-title", title, "--video-bit-rate", "16M"]
        for k in [k for k in self.procs if k.startswith("camera:")]:
            self.stop(k)
        self._spawn(f"camera:{facing}", args)

    def audio(self, source: str, to_virtual_mic=False):
        """Phone audio -> PC. source: output | playback | mic-voice-communication | voice-call ..."""
        key = f"audio:{source}"
        if not self.core.adbm.serial:
            # Debug-free: the phone app streams its own capture (playback capture asks for casting consent once)
            if source == "voice-call":
                raise ConnectionError("call audio on the PC needs wireless debugging")
            p = self.procs.get(key)
            if p and p.poll() is None:
                return False
            self.procs[key] = CastAudio(self.core, "mic" if source.startswith("mic") else "playback", to_virtual_mic)
            return True
        exe = None
        if to_virtual_mic:
            exe = SCRCPY_DIR / "scrcpy-mic.exe"      # separate exe so Windows can route it to CABLE Input
            if not exe.exists():
                shutil.copy2(SCRCPY, exe)
            route_app_to_cable(exe.name)
        args = ["--no-video", "--no-control", "--no-window", f"--audio-source={source}", "--audio-buffer=40"]
        if source == "playback":
            args.append("--audio-dup")
        return self._spawn(key, args, exe=exe)

    def _title(self, what):
        name = settings.phone_name()
        return f"{what} · {name}" if what != "Phone" else name


class CastAudio:
    """Phone audio from the phone app's own capture (castsession kind="audio"), played on the PC. Looks like a
    Popen to Mirror (poll / terminate)."""

    def __init__(self, core, source: str, to_virtual_mic: bool):
        import queue
        import threading
        self.core, self.source, self.to_cable = core, source, to_virtual_mic
        self._q: queue.Queue = queue.Queue(maxsize=50)
        self._done = False
        self._sess = None
        self._task = asyncio.ensure_future(self._run())
        threading.Thread(target=self._play, daemon=True, name=f"cast-audio-{source}").start()

    def poll(self):
        return 0 if self._done else None

    def terminate(self):
        self._done = True
        self._task.cancel()
        if self._sess:
            asyncio.ensure_future(self._sess.stop())
        try:
            self._q.put_nowait(None)
        except Exception:
            pass

    async def _run(self):
        from .castsession import CastSession, phone_host
        try:
            host = phone_host(self.core.phone)
            if not host:
                raise ConnectionError("phone not reachable over Wi-Fi")
            s = self._sess = CastSession(host, self.core.phone.token, "audio", audio=self.source)
            s.on_audio = lambda pts, pcm: self._q.full() or self._q.put_nowait(pcm)
            done = asyncio.Event()
            s.on_close = done.set
            await s.start()
            await done.wait()
        except asyncio.CancelledError:
            pass
        except Exception as e:
            log.info("cast audio: %s", e)
        finally:
            self._done = True
            try:
                self._q.put_nowait(None)
            except Exception:
                pass

    def _play(self):
        import pyaudiowpatch as pa
        p = pa.PyAudio()
        try:
            idx = None
            if self.to_cable:
                idx = next((i for i in range(p.get_device_count())
                            if "CABLE Input" in p.get_device_info_by_index(i)["name"]
                            and p.get_device_info_by_index(i)["maxOutputChannels"] > 0), None)
            out = p.open(format=pa.paInt16, channels=2, rate=48000, output=True, output_device_index=idx,
                         frames_per_buffer=960)
            while not self._done:
                pcm = self._q.get()
                if pcm is None:
                    break
                out.write(pcm)
            out.close()
        except Exception as e:
            log.info("cast audio playback: %s", e)
        finally:
            p.terminate()


def route_app_to_cable(exe_name: str) -> bool:
    """Per-app default output -> VB-CABLE, using NirSoft svcl if present (Windows stores it persistently)."""
    from .config import TOOLS_DIR
    svcl = TOOLS_DIR / "svcl" / "svcl.exe"
    if not svcl.exists():
        log.warning("svcl not found; route %s to 'CABLE Input' manually in Settings > Sound > Volume mixer", exe_name)
        return False
    subprocess.run([str(svcl), "/SetAppDefault", "CABLE Input", "all", exe_name],
                   creationflags=subprocess.CREATE_NO_WINDOW, timeout=10)
    return True
