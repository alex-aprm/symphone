"""adb plumbing: device tracking (USB + wireless TLS), port forward, token bootstrap, unlock and input."""
import asyncio
import logging
import os
import re
import subprocess

from .config import ADB, USB_FWD_PORT, PHONE_PORT, TOOLS_DIR, APP_DIR, NO_WINDOW, settings

log = logging.getLogger("adb")
PKG = "dev.phonesync"
APK_VERSION = 15                  # versionCode of the phone app bundled with this PC app
APK = next((p for p in (TOOLS_DIR / "Symphone.apk", APP_DIR / "dist" / "Symphone.apk") if p.exists()), None)
ALLOW_EMULATOR = os.environ.get("PHONESYNC_EMULATOR") == "1"     # treat emulators like USB phones (testing)
# testing: only use these adb serials (comma separated), e.g. an emulator while a real phone is plugged in
ADB_ONLY = {x for x in os.environ.get("SYMPHONE_ADB_ONLY", "").split(",") if x}


async def run(*args, serial=None, timeout=15, check=False) -> tuple[int, str]:
    cmd = [str(ADB)] + (["-s", serial] if serial else []) + [str(a) for a in args]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, creationflags=NO_WINDOW)
    except FileNotFoundError:
        return -1, f"adb not found ({ADB})"
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return -1, "timeout"
    text = out.decode("utf-8", "replace").strip()
    if check and proc.returncode != 0:
        raise RuntimeError(f"adb {' '.join(map(str, args))}: {text}")
    return proc.returncode, text


async def shell(cmd: str, serial: str, timeout=15) -> str:
    return (await run("shell", cmd, serial=serial, timeout=timeout))[1]


class AdbManager:
    """Keeps track of the phone's USB and Wi-Fi adb transports and prepares the USB forward."""

    def __init__(self):
        self.usb_serial: str | None = None
        self.wifi_serial: str | None = None
        self.hw_serial: str | None = None     # ro.serialno, used to match the mDNS name
        self.forward_ok = False
        self.serial_dev: dict[str, str] = {}   # adb serial -> phone id (learned from GET_TOKEN)
        self.on_paired = None                  # callback(dev_id) after a phone handed over its token
        self._prepared: set[str] = set()
        self.on_change = None
        self._last_mdns = 0.0
        # Wireless debugging pairing by QR code (setup wizard): {"name", "pass", "state", "error", "serial"}
        self.qr_pair: dict = {}
        self._qr_task: asyncio.Task | None = None
        self._last_unfreeze = 0.0

    @property
    def usb_dev(self) -> str | None:
        return self.serial_dev.get(self.usb_serial or "")

    def serial_for(self, dev_id: str) -> str | None:
        """adb serial of a given phone: USB if that phone is plugged in, else its wireless-debugging transport."""
        if self.usb_serial and self.serial_dev.get(self.usb_serial) == dev_id:
            return self.usb_serial
        dev = settings.device(dev_id) or {}
        if self.wifi_serial and dev.get("last_ip") and self.wifi_serial.startswith(dev["last_ip"] + ":"):
            return self.wifi_serial
        return None

    @property
    def serial(self) -> str | None:
        """Best transport to the active phone for interactive work (USB has lower latency)."""
        return self.serial_for(settings["active_device"])

    async def devices(self) -> list[tuple[str, str]]:
        rc, out = await run("devices")
        res = []
        for line in out.splitlines()[1:]:
            parts = line.split()
            if len(parts) >= 2 and (not ADB_ONLY or parts[0] in ADB_ONLY):
                res.append((parts[0], parts[1]))
        return res

    async def loop(self):
        await run("start-server", timeout=30)
        while True:
            try:
                await self.tick()
            except Exception as e:
                log.warning("adb tick: %s", e)
            await asyncio.sleep(2)

    async def tick(self):
        devs = await self.devices()
        usb = [s for s, st in devs if st == "device" and ":" not in s and (ALLOW_EMULATOR or not s.startswith("emulator"))]
        wifi = [s for s, st in devs if st == "device" and ":" in s]
        old = (self.usb_serial, self.wifi_serial)
        if usb:
            # Prefer the phone we're paired with if several are attached
            self.usb_serial = next((s for s in usb if s == self.hw_serial), usb[0])
            self.hw_serial = self.usb_serial
        else:
            self.usb_serial = None
            self.forward_ok = False
        if self.usb_serial != old[0]:
            self.forward_ok = False
        ip = settings["last_ip"]
        self.wifi_serial = next((s for s in wifi if not ip or s.startswith(ip + ":")), None)

        if self.usb_serial and self.usb_serial not in self._prepared:
            await self.prepare(self.usb_serial)
        if self.wifi_serial and self.wifi_serial not in self._prepared:
            # Phones set up over wireless debugging alone get the same first-contact setup as over USB
            # (install/update the app, permissions, pairing token).
            await self.prepare(self.wifi_serial)
        if self.usb_serial and not self.forward_ok:
            rc, _ = await run("forward", f"tcp:{USB_FWD_PORT}", f"tcp:{PHONE_PORT}", serial=self.usb_serial)
            self.forward_ok = rc == 0
        if not self.wifi_serial:
            await self.connect_wifi()
        # nubia's "Scene-Network" policy freezes even foreground-service apps when their network counter idles;
        # a sticky unfreeze keeps the phone app responsive for its process lifetime (re-applied after restarts).
        now = asyncio.get_running_loop().time()
        if self.serial and now - self._last_unfreeze > 30:
            self._last_unfreeze = now
            await shell(f"cmd activity unfreeze --sticky {PKG}", self.serial)
        if (self.usb_serial, self.wifi_serial) != old and self.on_change:
            self.on_change()

    async def prepare(self, serial: str):
        """First contact over USB: make sure the app runs, has every permission, and hand over the token."""
        log.info("preparing %s", serial)
        await self.update_app(serial)
        cmds = [
            f"appops set {PKG} MANAGE_EXTERNAL_STORAGE allow",
            f"appops set {PKG} SYSTEM_ALERT_WINDOW allow",
            f"appops set {PKG} RUN_ANY_IN_BACKGROUND allow",
            # Screen casting without the "start recording?" dialog in debugging-free mode (survives reboots and
            # turning debugging off; Android checks this before asking, as for the system's own Cast)
            f"appops set {PKG} PROJECT_MEDIA allow",
            # ...and touch/keys from the PC in that mode: Symphone's accessibility service, added to (never replacing)
            # the services already on. Android 13+ blocks turning it on by hand for sideloaded apps ("restricted").
            f"cur=$(settings get secure enabled_accessibility_services); case \"$cur\" in *{PKG}/*) ;; "
            f"null|'') settings put secure enabled_accessibility_services {PKG}/.ControlService ;; "
            f"*) settings put secure enabled_accessibility_services \"$cur:{PKG}/.ControlService\" ;; esac; "
            f"settings put secure accessibility_enabled 1",
            f"cmd notification allow_listener {PKG}/{PKG}.NotifListener",
            f"dumpsys deviceidle whitelist +{PKG}",
            f"pm grant {PKG} android.permission.POST_NOTIFICATIONS",
            f"pm grant {PKG} android.permission.READ_PHONE_STATE",
            f"pm grant {PKG} android.permission.ANSWER_PHONE_CALLS",
            f"pm grant {PKG} android.permission.CAMERA",
            # nubia/ZTE: allow background starts (otherwise "Blocked by AutoLaunch" after updates/reboots)
            f"dumpsys AutoLaunch -wl {PKG}",
            f"dumpsys AutoLaunch -a {PKG}",
            f"am start-foreground-service -n {PKG}/.SyncService",
        ]
        await shell(" ; ".join(cmds), serial)
        if await self.fetch_token(serial):
            dev = settings.device(self.serial_dev[serial]) or {}
            name = (await shell("settings get secure bluetooth_name", serial)).strip()
            if name and name != "null" and not dev.get("display_name"):
                settings.upsert_device(self.serial_dev[serial], display_name=name)
        for perm in ("BLUETOOTH_ADVERTISE", "BLUETOOTH_CONNECT", "BLUETOOTH_SCAN", "NEARBY_WIFI_DEVICES",
                     "ACCESS_FINE_LOCATION"):
            await shell(f"pm grant {PKG} android.permission.{perm} 2>/dev/null", serial)
        self._prepared.add(serial)

    async def update_app(self, serial: str):
        """Installs/updates the phone app from the copy bundled with this PC app (keeps its data and pairings)."""
        if not APK:
            return
        out = await shell(f"dumpsys package {PKG} | grep -m1 versionCode=", serial)
        m = re.search(r"versionCode=(\d+)", out)
        have = int(m.group(1)) if m else 0
        if have >= APK_VERSION:
            return
        log.info("installing phone app v%d on %s (has v%d)", APK_VERSION, serial, have)
        rc, res = await run("install", "-r", "-g", str(APK), serial=serial, timeout=180)
        log.info("install: %s", res.splitlines()[-1] if res else rc)
        await shell(f"dumpsys AutoLaunch -wl {PKG} ; dumpsys AutoLaunch -a {PKG} ; "
                    f"cmd notification allow_listener {PKG}/{PKG}.NotifListener ; "
                    f"am start-foreground-service -n {PKG}/.SyncService ; sleep 1 ; cmd activity unfreeze --sticky {PKG}", serial)

    async def fetch_token(self, serial: str) -> bool:
        for attempt in range(2):
            pc_name = settings.pc_name().replace("'", "")
            out = await shell(f"am broadcast -f 0x20 -a {PKG}.GET_TOKEN -n {PKG}/.PairReceiver "
                              f"--es pc {settings['pc_id']} --es pcname '{pc_name}'", serial)
            m = re.search(r'data="([0-9a-f]{32})\|([0-9a-f]+)\|([\d.]*)"', out)
            if m:
                token, dev_id, ip = m.groups()
                settings.upsert_device(dev_id, token=token, **({"last_ip": ip} if ip else {}))
                self.serial_dev[serial] = dev_id
                if self.on_paired:
                    self.on_paired(dev_id)
                return True
            # Vendor auto-start blockers ignore broadcasts until the app has been launched once.
            await shell(f"am start -n {PKG}/.MainActivity", serial)
            await asyncio.sleep(2)
        log.warning("could not fetch token: %s", out)
        return False

    async def ensure_app(self, serial: str | None = None):
        """Phone app unreachable but adb works: (re)start its service (after updates, reboots or a kill)."""
        s = serial or self.serial
        if s:
            await shell(f"am start-foreground-service -n {PKG}/.SyncService; sleep 1; "
                        f"cmd activity unfreeze --sticky {PKG}", s)

    async def connect_wifi(self):
        """Connect wireless debugging (TLS) found via mDNS; the PC's key is already trusted from USB."""
        loop = asyncio.get_running_loop()
        if loop.time() - self._last_mdns < 8:
            return
        self._last_mdns = loop.time()
        ip = settings["last_ip"]
        if not ip:
            return
        rc, out = await run("mdns", "services", timeout=5)
        for line in out.splitlines():
            if "_adb-tls-connect" in line and ip + ":" in line:
                addr = line.split()[-1]
                rc, res = await run("connect", addr, timeout=6)
                if "connected" in res:
                    log.info("wifi adb connected %s", addr)
                    self.wifi_serial = addr
                    return
        # Fallback: legacy tcpip port (only if it was enabled earlier)
        rc, res = await run("connect", f"{ip}:5555", timeout=4)
        if "connected to" in res:
            self.wifi_serial = f"{ip}:5555"
        else:
            await run("disconnect", f"{ip}:5555", timeout=3)

    # ---------- wireless debugging pairing by QR code ----------
    def start_qr_pair(self) -> str:
        """Starts pairing a phone's wireless debugging: returns the QR text for Android's
        Settings > Developer options > Wireless debugging > Pair device with QR code (like Android Studio)."""
        import secrets
        name = f"symphone-{secrets.token_hex(3)}"
        pw = secrets.token_urlsafe(8)
        self.qr_pair = {"name": name, "pass": pw, "state": "waiting", "error": "", "serial": ""}
        if self._qr_task and not self._qr_task.done():
            self._qr_task.cancel()
        self._qr_task = asyncio.create_task(self._qr_pair_loop(name, pw))
        return f"WIFI:T:ADB;S:{name};P:{pw};;"

    def stop_qr_pair(self):
        if self._qr_task and not self._qr_task.done():
            self._qr_task.cancel()
        if self.qr_pair.get("state") not in ("done",):
            self.qr_pair = {}

    async def _qr_pair_loop(self, name: str, pw: str):
        """The phone advertises `name` over mDNS once it scanned the QR: pair with it, then connect and set it up."""
        st = self.qr_pair
        try:
            deadline = asyncio.get_running_loop().time() + 600
            addr = None
            while asyncio.get_running_loop().time() < deadline and not addr:
                rc, out = await run("mdns", "services", timeout=5)
                for line in out.splitlines():
                    if line.startswith(name) and "_adb-tls-pairing" in line:
                        addr = line.split()[-1]
                if not addr:
                    await asyncio.sleep(1)
            if not addr:
                st.update(state="error", error="The phone didn't show up. Scan the code again from Wireless debugging.")
                return
            st["state"] = "pairing"
            rc, res = await run("pair", addr, pw, timeout=20)
            if "Successfully paired" not in res:
                st.update(state="error", error=res.strip() or "Pairing failed")
                return
            log.info("wireless debugging paired: %s", addr)
            st["state"] = "connecting"
            ip = addr.split(":")[0]
            for _ in range(30):                   # the connect service follows the pairing within seconds
                rc, out = await run("mdns", "services", timeout=5)
                conn = next((l.split()[-1] for l in out.splitlines()
                             if "_adb-tls-connect" in l and l.split()[-1].startswith(ip + ":")), None)
                if conn:
                    rc, res = await run("connect", conn, timeout=8)
                    if "connected" in res:
                        st.update(state="setting-up", serial=conn)
                        await self.prepare(conn)
                        st["state"] = "done"
                        if self.on_change:
                            self.on_change()
                        return
                await asyncio.sleep(1)
            st.update(state="error", error="Paired, but couldn't connect. Keep Wireless debugging on and try again.")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            st.update(state="error", error=str(e))

    # ---------- interactive helpers ----------

    async def screen_state(self) -> tuple[bool, bool]:
        """(screen_on, locked) via dumpsys; used when the app API isn't reachable."""
        s = self.serial
        out = await shell("dumpsys power | grep -m1 mWakefulness= ; dumpsys window | grep -m1 -E 'mDreamingLockscreen|isKeyguardShowing|mShowingLockscreen'", s)
        on = "Awake" in out
        locked = bool(re.search(r"(mDreamingLockscreen|isKeyguardShowing|mShowingLockscreen)=true", out))
        return on, locked

    async def unlock(self, pin: str, is_locked) -> tuple[bool, str]:
        """Wake, raise the bouncer, type the PIN once (never retries, to avoid lockouts).

        Every key goes to display 0 (the phone's own screen): with app windows open, the focused display is one of
        their virtual displays, and an untargeted `input text` would type the PIN into that app instead."""
        s = self.serial
        if not s:
            return False, "phone not connected over adb (USB or Wi-Fi debugging)"
        await shell("input -d 0 keyevent KEYCODE_WAKEUP", s)
        await asyncio.sleep(0.25)
        if not await is_locked():
            return True, "already unlocked"
        await shell("wm dismiss-keyguard", s)
        await asyncio.sleep(0.45)
        if not await is_locked():
            return True, "unlocked (no PIN needed)"
        if not pin:
            return False, "set your PIN in Settings to unlock from the PC"
        await shell(f"input -d 0 text {pin} && input -d 0 keyevent KEYCODE_ENTER", s)
        for _ in range(10):
            await asyncio.sleep(0.25)
            if not await is_locked():
                return True, "unlocked"
        return False, "still locked (wrong PIN or biometric-only lock?)"

    async def lock(self):
        if self.serial:
            await shell("input -d 0 keyevent KEYCODE_SLEEP", self.serial)

    async def key(self, code: str):
        if self.serial:
            await shell(f"input -d 0 keyevent {code}", self.serial)

    async def take_photo_native(self):
        """Opens the stock camera and fires the shutter via KEYCODE_CAMERA."""
        s = self.serial
        await shell("am start -a android.media.action.STILL_IMAGE_CAMERA", s)
        await asyncio.sleep(1.4)
        await shell("input -d 0 keyevent KEYCODE_CAMERA", s)


def env_for_scrcpy() -> dict:
    env = dict(os.environ)
    env["ADB"] = str(ADB)
    return env
