"""Shared clipboard: the PC clipboard follows the phone's and vice versa (text)."""
import asyncio
import ctypes
import logging

from .adb import shell, PKG
from .config import settings
from . import winutil

log = logging.getLogger("clip")
_seq = ctypes.windll.user32.GetClipboardSequenceNumber


class ClipSync:
    def __init__(self, core):
        self.core = core
        self.last = ""                # last text exchanged in either direction (echo suppression)
        self._seq = 0
        self._helper_checked: dict[str, float] = {}

    def status(self):
        return {"enabled": bool(settings["clipboard_sync"])}

    def set_local(self, text: str):
        if not text:
            return
        self.last = text
        winutil.set_clipboard(text)
        self._seq = _seq()

    def from_phone(self, phone, text: str):
        if settings["clipboard_sync"] and text and text != self.last:
            log.info("clipboard from %s (%d chars)", phone.name, len(text))
            self.set_local(text)

    def _targets(self):
        hub = self.core.hub
        return [p for p in hub.phones.values() if p.connected and (p is hub.active or p.is_master)]

    async def loop(self):
        self._seq = _seq()
        n = 0
        while True:
            await asyncio.sleep(0.4)
            n += 1
            try:
                if settings["clipboard_sync"]:
                    s = _seq()
                    if s != self._seq:
                        self._seq = s
                        await asyncio.sleep(0.05)          # let the source app finish writing all formats
                        text = winutil.get_clipboard()
                        if text and text != self.last and len(text) < 1_000_000:
                            self.last = text
                            for p in self._targets():
                                try:
                                    await p.post_json("/api/clipboard", {"text": text}, timeout=5)
                                except Exception as e:
                                    log.info("clipboard -> %s: %s", p.name, e)
                    if n % 50 == 1:
                        await self._ensure_helpers()
            except Exception:
                log.exception("clip loop")

    async def _ensure_helpers(self):
        """Android only lets the focused app read the clipboard; adb shell may. A tiny helper inside the APK runs as
        shell (app_process) and forwards clipboard changes to the phone app, which sends them to its master PC."""
        for p in list(self.core.hub.phones.values()):
            serial = self.core.adbm.serial_for(p.dev_id)
            if not serial or not p.token or not p.connected:
                continue
            out = await shell('pgrep -f "dev[.]phonesync[.]ShellCli[p]" || echo none', serial, timeout=6)
            if "none" not in out:
                continue
            log.info("starting the clipboard helper on %s", p.name)
            await shell(f"CLASSPATH=$(pm path {PKG} | head -1 | cut -d: -f2) nohup app_process / dev.phonesync.ShellClip "
                        f"{p.token} >/dev/null 2>&1 &", serial, timeout=8)
