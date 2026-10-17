"""Wires the subsystems together and routes phone events."""
import asyncio
import json
import logging
import os
import re
import time
from pathlib import Path

from .adb import AdbManager, shell as adb_shell, PKG
from .audio import PcToPhone, BluetoothSink
from .config import settings, CACHE_DIR, IS_WIN
from .davproxy import DavProxy
from .drive import Drive
from .media import MediaIndex
from .mirror import Mirror
from .notify import Notifier
from .phone import PhoneHub
from .transfer import TransferManager
from . import desktop

log = logging.getLogger("core")
ANSWER_WORDS = re.compile(r"answer|accept|ответ|принять|відповісти|annehmen|répondre|contestar|rispondi", re.I)


class NoSidecar:
    """The side display needs the Windows virtual display driver."""
    ERROR = "The side display is available on Windows only"

    def status(self) -> dict:
        return {"state": "unavailable", "error": self.ERROR}

    async def start(self, *a, **kw) -> dict:
        return {"state": "error", "error": self.ERROR}

    async def set_scale(self, *a) -> dict:
        return self.status()

    async def stop(self):
        pass


class Core:
    def __init__(self):
        self.loop: asyncio.AbstractEventLoop | None = None
        self.adbm = AdbManager()
        self.hub = PhoneHub(self.adbm)
        self.phone = self.hub.proxy              # always the active phone
        self.transfers = TransferManager(self.phone)
        self.media = MediaIndex(self)
        self.notifier = Notifier(self)
        self.mirror = Mirror(self)
        self.pc_audio = PcToPhone(self.phone)
        self.bt = BluetoothSink()
        self.drive = Drive(self.phone)
        self.proxy = DavProxy(self.phone)
        from .camfeed import CameraFeed
        from .clipsync import ClipSync
        from .nearby import Nearby
        self.camfeed = CameraFeed(self)
        if IS_WIN:
            from .sidecar import Sidecar
            self.sidecar = Sidecar()
        else:
            self.sidecar = NoSidecar()
        self.clip = ClipSync(self)
        self.nearby = Nearby(self)
        self.notif_history: list[dict] = self._load_history()
        self.ui_queues: list[asyncio.Queue] = []
        self.call_active = False
        self._active = settings["active_device"]

    @property
    def notifications(self) -> dict[str, dict]:
        return self.phone.notifications

    async def start(self):
        self.loop = asyncio.get_running_loop()
        self.notifier.bind(self.loop)
        self.hub.listeners.append(self.on_phone_event)
        self.hub.on_state(self._hub_changed)
        self.adbm.on_change = lambda: self.ui_event("state", self.state())
        self.adbm.on_paired = lambda dev_id: self.loop.call_soon_threadsafe(self.hub.sync_devices)
        self.transfers.on_update = lambda: self.ui_event("transfers", self.transfers.active())
        await self.proxy.start()
        await self.hub.start()
        for coro in (self.adbm.loop(), self.drive.loop(), self._ticker(), self.camfeed.loop(), self.clip.loop(),
                     self.nearby.loop(), self._audio_keeper()):
            asyncio.create_task(coro)

    async def _audio_keeper(self):
        """Sound routes switched on in Settings stay on: started when the phone is (re)connected and restarted if
        a stream drops."""
        await asyncio.sleep(8)                          # let links and adb come up first
        while True:
            try:
                routes = settings["audio_routes"] or []
                if routes and self.phone.connected:
                    running = self.mirror.running()
                    for what in routes:
                        if what in ("system", "mic"):
                            if self.pc_audio.mode != what:
                                await self.pc_audio.start(what)
                        elif what == "bt_sink":
                            if not self.bt.active:
                                self.bt.start(settings["display_name"] or settings["device_name"] or "")
                        elif what in ("output", "playback", "mic-voice-communication", "voice-call"):
                            if not running.get(f"audio:{what}"):
                                self.mirror.audio(what, to_virtual_mic=what == "mic-voice-communication")
            except Exception as e:
                log.info("audio routes: %s", e)
            await asyncio.sleep(10)

    def _hub_changed(self):
        if settings["active_device"] != self._active:
            self._active = settings["active_device"]
            asyncio.ensure_future(self._on_switch())
        self.ui_event("state", self.state())

    async def _on_switch(self):
        """The UI now shows another phone: reload its photos, remount Explorer, close streams of the old one."""
        log.info("active phone -> %s", settings.phone_name())
        self.media.reset()
        self.drive.stop()                     # the drive loop remounts for the new phone
        try:
            from . import screen_ws
            await screen_ws.shutdown()
        except Exception:
            pass
        self.ui_event("media_reset", {})
        self.ui_event("notifs", list(self.notifications.values()))
        if self.phone.connected:
            asyncio.create_task(self.media.full_sync())

    def switch_device(self, dev_id: str) -> bool:
        return self.hub.set_active(dev_id)

    async def _ticker(self):
        """Pushes transfer progress twice a second, and state (battery, lock, ...) whenever it changes."""
        last_state, n = None, 0
        while True:
            await asyncio.sleep(0.5)
            n += 1
            if any(t.state == "running" for t in self.transfers.items):
                self.ui_event("transfers", self.transfers.active())
            if n % 8 == 0:                      # lock state / battery for the sidebar
                for p in list(self.hub.phones.values()):
                    if p.connected and (p is self.hub.active or n % 120 == 0):
                        await p.refresh_info()
            if n % 2 == 0:
                s = self.state()
                if s != last_state:
                    last_state = s
                    self.ui_event("state", s)

    # ---------------------------------------------------------------- UI plumbing
    def ui_event(self, name, data):
        for q in list(self.ui_queues):
            try:
                q.put_nowait((name, data))
            except asyncio.QueueFull:
                pass

    def state(self):
        p = self.hub.active
        dev = p.to_json() if p.dev_id else None
        return {
            "connected": p.connected,
            "paired": bool(self.hub.phones),
            "device": dev,
            "phones": self.hub.to_json(),
            "active": settings["active_device"],
            "pc": {"id": settings["pc_id"], "name": settings.pc_name()},
            "adb": {"usb": self.adbm.usb_serial if self.adbm.usb_dev == p.dev_id else None,
                    "wifi": self.adbm.serial_for(p.dev_id) if self.adbm.serial_for(p.dev_id) != self.adbm.usb_serial else None},
            "candidates": [c for c in self.hub.candidates.values() if time.time() - c.get("seen", 0) < 60],
            "drive": self.drive.status(),
            "mirror": self.mirror.running(),
            "audio": {"pc_to_phone": self.pc_audio.status(), "bt_sink": self.bt.active},
            "hasPin": bool(settings["pin"]),
            "vcam": self.camfeed.status(),
            "sidecar": self._safe_status(self.sidecar),
            "nearby": self.nearby.status(),
            "pairing": bool(self.hub.pair_secret),
        }

    # ---------------------------------------------------------------- phone events
    async def on_phone_event(self, phone, event, data):
        active = phone is self.hub.active
        if event == "hello":
            if active:
                self.ui_event("state", self.state())
                asyncio.create_task(self.media.on_event(event, data))
            asyncio.create_task(self._initial_sync(phone))
        elif event == "notif":
            phone.notifications[data["key"]] = data
            if active:
                self.ui_event("notif", data)
            await self.notifier.show(data, phone)
            if data.get("call") and active:
                self.ui_event("call", data)
        elif event == "notif_removed":
            n = phone.notifications.pop(data["key"], None)
            self.notifier.remove(data["key"])
            if n and not n.get("ongoing"):
                h = {**n, "removed": int(time.time() * 1000), "dev": phone.dev_id}
                self.notif_history = [h] + [x for x in self.notif_history if x["key"] != n["key"]][:499]
                self._save_history()
                data = {**data, "history": h}
            if active:
                self.ui_event("notif_removed", data)
            if n and n.get("call") and self.call_active and not any(x.get("call") for x in phone.notifications.values()):
                self.end_call_audio()
        elif event == "media":
            if active:
                await self.media.on_event(event, data)
        elif event == "share":
            await self.receive_share(phone, data)
        elif event == "share_text":
            await self.receive_text(phone, data)
        elif event == "clip":
            self.clip.from_phone(phone, data.get("text", ""))
        elif event == "pcs":
            self.ui_event("state", self.state())
        elif event == "captured":
            if data.get("error"):
                self.notifier.simple("Camera capture failed", data["error"])

    async def _initial_sync(self, phone):
        try:
            d = await phone.get_json("/api/notifications")
            phone.notifications.clear()
            phone.notifications.update({n["key"]: n for n in d["items"]})
            if phone is self.hub.active:
                self.ui_event("notifs", list(phone.notifications.values()))
            shares = await phone.get_json("/api/shares")
            for s in shares["items"]:
                await self.receive_share(phone, s)
        except Exception as e:
            log.warning("initial sync: %s", e)

    async def receive_share(self, phone, s):
        dest = Path(settings["downloads_dir"]) / s["name"]
        if dest.exists():
            dest = dest.with_name(f"{dest.stem} ({int(time.time())}){dest.suffix}")
        try:
            await self.transfers.download(f"/api/share/{s['id']}", dest, name=s["name"], size=s["size"],
                                          phone=phone, toast=False)
            async with await phone.request("DELETE", f"/api/share/{s['id']}"):
                pass
            self.notifier.received(dest, phone.name)
            self.ui_event("received", {"path": str(dest)})
        except Exception as e:
            log.warning("share %s: %s", s, e)

    async def receive_text(self, phone, d):
        text = d.get("text", "")
        self.clip.set_local(text)
        url = re.match(r"https?://\S+$", text.strip())
        self.notifier.simple(f"Copied from {phone.name}", text[:200],
                             on_click=(lambda: desktop.open_path(text.strip())) if url else None)

    @staticmethod
    def _safe_status(obj):
        try:
            st = obj.status()
            return {k: st.get(k) for k in ("state", "link", "monitor", "fps", "mbps", "latency_ms", "error", "orientation",
                                           "scale")
                    if k in st}
        except Exception as e:
            return {"state": "error", "error": str(e)}

    def _load_history(self):
        try:
            return json.loads((CACHE_DIR / "notif_history.json").read_text("utf-8"))
        except Exception:
            return []

    def _save_history(self):
        try:
            (CACHE_DIR / "notif_history.json").write_text(json.dumps(self.notif_history), "utf-8")
        except OSError:
            pass

    def history_for_active(self):
        dev = settings["active_device"]
        return [h for h in self.notif_history if h.get("dev", dev) == dev]

    # ---------------------------------------------------------------- actions
    async def is_locked(self) -> bool:
        try:
            info = await self.phone.get_json("/api/info", timeout=3)
            self.phone.info = info
            return bool(info.get("locked"))
        except Exception:
            return (await self.adbm.screen_state())[1]

    async def unlock(self) -> dict:
        if not self.adbm.serial:
            ok, msg = await self._unlock_app()
        else:
            ok, msg = await self.adbm.unlock(settings.get_pin(), self.is_locked)
        if not ok:
            log.info("unlock: %s", msg)
        await self.phone.refresh_info()
        self.ui_event("state", self.state())
        return {"ok": ok, "message": msg}

    async def _unlock_app(self) -> tuple[bool, str]:
        """Debug-free unlock: the phone app's accessibility service taps the PIN in (once). The PIN travels
        encrypted with a key only this PC and the phone know (from the pairing token)."""
        pin = settings.get_pin()
        if not pin:
            return False, "no PIN saved (Settings › Phone PIN)"
        import hashlib, json, os
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        key = hashlib.sha256(self.phone.token.encode()).digest()
        nonce = os.urandom(12)
        ct = AESGCM(key).encrypt(nonce, json.dumps({"pin": pin}).encode(), None)
        try:
            r = await self.phone.post_json("/api/unlock", {"nonce": nonce.hex(), "ct": ct.hex()}, timeout=15)
        except Exception as e:
            return False, f"phone not reachable: {e}"
        return bool(r.get("ok")), r.get("error") or r.get("message") or ""

    async def lock(self) -> dict:
        if not self.adbm.serial:
            try:
                await self.phone.post_json("/api/lock", {}, timeout=5)
            except Exception as e:
                return {"ok": False, "error": str(e)}
            await asyncio.sleep(0.4)
            await self.phone.refresh_info()
            self.ui_event("state", self.state())
            return {"ok": True}
        await self.adbm.lock()
        await asyncio.sleep(0.4)
        await self.phone.refresh_info()
        self.ui_event("state", self.state())
        return {"ok": True}

    async def answer_call_on_pc(self, n: dict | None = None):
        n = n or next((x for x in self.notifications.values() if x.get("call")), None)
        done = False
        if n:
            act = next((a for a in n.get("actions", []) if ANSWER_WORDS.search(a["title"])), None)
            if act:
                r = await self.phone.post_json("/api/notif/act", {"key": n["key"], "op": "action", "index": act["i"]})
                done = r.get("ok", False)
        if not done:
            await self.phone.post_json("/api/call", {"op": "answer"})
        await asyncio.sleep(1.0)
        self.start_call_audio()

    def start_call_audio(self):
        """Hear the call on the PC (phone keeps using its own mic)."""
        self.call_active = True
        try:
            self.mirror.audio("voice-call")
        except Exception as e:
            log.warning("call audio: %s", e)

    def end_call_audio(self):
        self.call_active = False
        self.mirror.stop("audio:voice-call")

    async def send_files(self, paths: list[str], remote_dir="/Download/PC", dev_id: str | None = None):
        paths = [p for p in paths if p and str(p).strip() and Path(p).is_absolute() and Path(p).exists()]
        if not paths:
            return {"count": 0, "errors": ["nothing to send"]}
        phone = self.hub.phones.get(dev_id) if dev_id else self.hub.active
        if not phone or not phone.connected:
            self.notifier.simple("Phone not connected", "Couldn't send the files: the phone isn't reachable right now")
            return {"count": 0, "errors": ["not connected"]}
        return await self.transfers.send_paths([Path(p) for p in paths], remote_dir, phone=phone)

    async def push_clipboard(self):
        text = await asyncio.to_thread(desktop.get_clipboard)
        if text:
            await self.phone.post_json("/api/clipboard", {"text": text})
        return {"ok": bool(text)}

    async def open_notification(self, key: str, phone=None, op="open", index=-1, text=None):
        """Opens a phone notification the way a tap on the phone would: its app comes up in a PC window
        (on a virtual display, so it works with the phone locked in a pocket) at the notification's target."""
        phone = phone or self.hub.active
        n = phone.notifications.get(key) or next((h for h in self.notif_history if h["key"] == key), None)
        if not n:
            return {"ok": False, "error": "notification gone"}
        from . import screen_ws
        if phone is not self.hub.active:
            self.switch_device(phone.dev_id)
            await asyncio.sleep(0.3)
        if not self.adbm.serial:
            # Debug-free: no hidden displays; the notification opens on the phone, shown in the phone-screen window
            await screen_ws.open_window("screen", mode="main")
            if key in phone.notifications:
                return await phone.post_json("/api/notif/act", {"key": key, "op": op, "index": index, "text": text})
            return await phone.post_json("/api/launch", {"pkg": n["pkg"]})
        disp = await screen_ws.ensure_app_display(n["pkg"], n.get("app") or n["pkg"])
        body = {"key": key, "op": op, "index": index, "text": text}
        if disp is not None:
            body["display"] = disp
        if key in phone.notifications:
            r = await phone.post_json("/api/notif/act", body)
        else:   # history entry: the notification is gone, open its app
            r = {"ok": True}
        return r

    async def notif_action(self, key, op, index=-1, text=None):
        if op == "open":
            return await self.open_notification(key)
        return await self.phone.post_json("/api/notif/act", {"key": key, "op": op, "index": index, "text": text})

    async def shutdown(self):
        from . import screen_ws
        try:
            await screen_ws.shutdown()
        except Exception:
            pass
        try:
            await self.sidecar.stop()
        except Exception:
            pass
        self.nearby.stop()
        self.mirror.stop_all()
        self.drive.stop()
        self.bt.stop()
        await self.pc_audio.stop()
