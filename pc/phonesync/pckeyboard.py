"""PC keyboard for phone windows.

While a phone app runs in a PC window, the phone's own keyboard would pop up (on the phone) for every text field and
key injection only knows ASCII. Instead the phone app's invisible input method (dev.phonesync/.PcKeyboard) is made
the current keyboard for as long as PC windows are open: it shows nothing anywhere and commits exactly what is typed
on the PC (any language). The phone side holds a lease (packages shown on the PC, renewed here every few seconds);
a text field of any other app, or an expired lease, makes it hand back to the user's own keyboard by itself, so the
phone is never left without one. Keys go through it only while it is bound to a field; everything else falls back to
scrcpy key injection.
"""
from __future__ import annotations

import asyncio
import logging
import time

from . import scrcpy_client as sc
from .config import settings

log = logging.getLogger("kbd")
OURS = "dev.phonesync/.PcKeyboard"
OURS_FULL = "dev.phonesync/dev.phonesync.PcKeyboard"
LEASE_TTL = 20
RENEW = 7


def _is_ours(ime: str) -> bool:
    return ime.strip() in (OURS, OURS_FULL)


class PcKeyboard:
    def __init__(self, core):
        self.core = core
        self.prev: str | None = None             # the user's keyboard, to hand back to
        self._serial: str | None = None
        self._renewer: asyncio.Task | None = None
        self._last_set = 0.0
        self._lock = asyncio.Lock()
        self._wake = asyncio.Event()
        self.unsupported = False                 # phone app without the input method (older APK)

    @property
    def enabled(self) -> bool:
        return bool(settings["pc_keyboard"]) and not self.unsupported

    # ------------------------------------------------------------------ which windows want it
    def _wanted(self) -> tuple[set[str], bool]:
        from . import screen_ws
        pkgs, any_app = set(), False
        for s in screen_ws.sessions:
            kind = getattr(s, "ps_kind", "")
            if kind == "screen" and not getattr(s, "cast", False):
                if getattr(s, "ps_focused", False):
                    any_app = True               # the phone's own screen, typed into from the PC right now
                continue
            if s.virtual_display_id is None or getattr(s, "ps_shown", True) is False:
                continue
            if kind == "app":
                pkgs |= getattr(s, "vd_pkgs", None) or {s.start_app}
                if getattr(s, "ps_focused", False):
                    any_app = True               # typing in this window right now: whatever field has focus
            elif kind == "desktop":
                any_app = True
        pkgs.discard(None)
        return pkgs, any_app

    def sessions_changed(self):
        pkgs, any_app = self._wanted()
        if not self.enabled:
            return
        if (pkgs or any_app) and (self._renewer is None or self._renewer.done()):
            self._renewer = asyncio.create_task(self._renew_loop())
        elif pkgs or any_app:
            self._wake.set()                     # new package on a PC display: tell the phone now
        elif not (pkgs or any_app) and self._renewer and not self._renewer.done():
            self._renewer.cancel()
            asyncio.create_task(self.release())

    async def _renew_loop(self):
        try:
            await self.ensure()
            while True:
                pkgs, any_app = self._wanted()
                if not (pkgs or any_app):
                    break
                await self._lease(pkgs, any_app)
                self._wake.clear()
                try:
                    await asyncio.wait_for(self._wake.wait(), RENEW)
                except asyncio.TimeoutError:
                    pass
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.info("keyboard lease: %s", e)

    async def refresh_pkgs(self):
        """Packages of every task on the PC windows' displays, right now (a field may belong to another package
        than the app, e.g. Settings search lives in com.android.settings.intelligence)."""
        from . import screen_ws
        app_sessions = [s for s in screen_ws.sessions if getattr(s, "ps_kind", "") == "app" and s.virtual_display_id is not None]
        if not app_sessions:
            return
        try:
            tasks, _ = await screen_ws._tasks(app_sessions[0].serial)
        except Exception:
            return
        for s in app_sessions:
            s.vd_pkgs = {t[3] for t in tasks if t[1] == s.virtual_display_id and "launcher" not in t[3].lower()} | {s.start_app}

    # ------------------------------------------------------------------ phone side
    async def _lease(self, pkgs=None, any_app=None):
        if pkgs is None:
            pkgs, any_app = self._wanted()
        r = await self.core.phone.post_json("/api/ime/lease", {"pkgs": sorted(pkgs), "any": bool(any_app),
                                                               "prev": self.prev, "ttl": LEASE_TTL}, timeout=4)
        if r.get("error") and "404" in str(r.get("error")):
            self.unsupported = True
            log.info("phone app has no PC keyboard yet")
        return r

    async def ensure(self, force=False):
        """Makes the Symphone keyboard the phone's current one (remembering the user's)."""
        serial = self.core.adbm.serial
        if not serial or not self.enabled:
            return False
        async with self._lock:
            if not force and time.time() - self._last_set < 2:
                return True
            self._last_set = time.time()
            rc, cur = await sc.adb("shell", "settings get secure default_input_method", serial=serial, timeout=6)
            cur = cur.strip()
            if rc != 0:
                return False
            if _is_ours(cur):
                self._serial = serial
                if force:
                    try:
                        await self._lease()
                    except Exception as e:
                        log.info("keyboard lease: %s", e)
                return True
            if cur and cur != "null":
                self.prev = cur
            try:
                await self._lease()              # lease first: the keyboard checks it as soon as it is bound
            except Exception as e:               # phone briefly unreachable (reinstall, link switch)
                log.info("keyboard lease: %s", e)
                return False
            rc, out = await sc.adb("shell", f"ime enable {OURS} >/dev/null; ime set {OURS}", serial=serial, timeout=8)
            if rc != 0 or "error" in out.lower() or "unknown" in out.lower():
                log.info("ime set: %s", out.strip())
                if "unknown" in out.lower() or "not found" in out.lower():
                    self.unsupported = True
                return False
            self._serial = serial
            log.info("PC keyboard on (user keyboard: %s)", self.prev)
            return True

    async def release(self):
        serial = self._serial or self.core.adbm.serial
        try:
            await self.core.phone.post_json("/api/ime/release", {}, timeout=4)
        except Exception:
            pass
        if serial and self.prev:
            rc, cur = await sc.adb("shell", "settings get secure default_input_method", serial=serial, timeout=6)
            if rc == 0 and _is_ours(cur):
                await sc.adb("shell", f"ime set {self.prev}", serial=serial, timeout=8)
        self._serial = None
        log.info("PC keyboard off")

    async def input(self, body: dict) -> bool:
        """Types through the phone keyboard; False when it isn't bound to a text field (caller falls back)."""
        if not self.enabled:
            return False
        try:
            r = await self.core.phone.post_json("/api/ime/input", body, timeout=2)
            if r.get("ok"):
                return True
            if r.get("reason") == "not-current":
                # The user's keyboard took over (they typed on the phone, or this field belongs to a package the
                # lease didn't list yet): refresh what is on the PC displays and take it back for the PC window.
                await self.refresh_pkgs()
                if await self.ensure(force=True):
                    await asyncio.sleep(0.35)
                    r = await self.core.phone.post_json("/api/ime/input", body, timeout=2)
                    return bool(r.get("ok"))
            elif "404" in str(r.get("error", "")):
                self.unsupported = True
        except Exception as e:
            log.debug("ime input: %s", e)
        return False
