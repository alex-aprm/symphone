"""Native Windows toasts: phone notifications (actions, inline reply, calls) and file-transfer progress."""
import asyncio
import hashlib
import logging
import os
import subprocess
import time
import winreg
from pathlib import Path
from urllib.parse import urlencode

from windows_toasts import (InteractableWindowsToaster, Toast, ToastButton, ToastDisplayImage, ToastProgressBar,
                            ToastImagePosition, ToastInputTextBox, ToastScenario, ToastDuration)

from .config import AUMID, APP_NAME, ICON_DIR, BASE_DIR, settings

log = logging.getLogger("notify")
IMAGE_EXT = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp")


def register_aumid(icon: Path):
    key = winreg.CreateKey(winreg.HKEY_CURRENT_USER, rf"Software\Classes\AppUserModelId\{AUMID}")
    winreg.SetValueEx(key, "DisplayName", 0, winreg.REG_SZ, APP_NAME)
    winreg.SetValueEx(key, "IconUri", 0, winreg.REG_SZ, str(icon))
    winreg.SetValueEx(key, "IconBackgroundColor", 0, winreg.REG_SZ, "FF1F6FEB")
    winreg.CloseKey(key)


def _tag(key: str) -> str:
    return hashlib.sha1(key.encode()).hexdigest()[:16]


def reveal(path: Path):
    """Selects the file in an Explorer window (or opens the folder)."""
    path = Path(path)
    if path.is_file():
        subprocess.Popen(["explorer.exe", "/select,", str(path)])
    elif path.exists():
        os.startfile(path)


class Notifier:
    def __init__(self, app):
        self.app = app                      # Core
        self.loop: asyncio.AbstractEventLoop | None = None
        icon = BASE_DIR / "ui" / "icon.png"
        try:
            register_aumid(icon)
        except OSError as e:
            log.warning("aumid: %s", e)
        self.toaster = InteractableWindowsToaster(APP_NAME, AUMID)
        self.shown: dict[str, tuple[Toast, tuple]] = {}
        self._opened: dict[str, float] = {}
        self._seq = 0
        app.transfers.notifier = self

    def bind(self, loop):
        self.loop = loop

    def _call(self, fn):
        """Toast callbacks run on a WinRT thread: hop back to the asyncio loop."""
        def cb(*_a):
            if self.loop:
                self.loop.call_soon_threadsafe(fn)
        return cb

    def _run(self, coro_fn):
        def cb(*_a):
            if self.loop:
                asyncio.run_coroutine_threadsafe(coro_fn(), self.loop)
        return cb

    # ------------------------------------------------------------------ phone notifications
    async def _icon_file(self, n, phone) -> Path | None:
        """Sender avatar if the notification has one, else the app icon (both cached on disk)."""
        try:
            if n.get("hasIcon"):
                p = ICON_DIR / f"n_{_tag(n['key'])}.png"
                if not p.exists():
                    from urllib.parse import quote
                    p.write_bytes(await phone.get_bytes(f"/api/notif/icon?key={quote(n['key'])}"))
                return p
            p = ICON_DIR / f"{n['pkg']}.png"
            if not p.exists():
                p.write_bytes(await phone.get_bytes(f"/api/icon/{n['pkg']}"))
            return p
        except Exception:
            return None

    async def show(self, n: dict, phone):
        if not settings["toasts"] or n["pkg"] in settings["toast_muted_apps"]:
            return
        if n.get("ongoing") and not n.get("call"):
            return
        sig = (n.get("title"), n.get("text"), tuple(a["title"] for a in n.get("actions", [])))
        prev = self.shown.get(n["key"])
        if prev and prev[1] == sig:
            return
        body = n.get("text") or ""
        msgs = n.get("messages")
        if msgs:
            body = "\n".join(f"{m['from']}: {m['text']}" if m.get("from") else m["text"] for m in msgs[-3:])
        toast = Toast(text_fields=[n.get("title") or n.get("app"), body[:300]])
        toast.tag = _tag(n["key"])
        toast.group = "phone"
        many = len(self.app.hub.phones) > 1
        toast.attribution_text = f"{n.get('app', '')} · {phone.name}" if many else n.get("app", "")
        icon = await self._icon_file(n, phone)
        if icon:
            toast.AddImage(ToastDisplayImage.fromPath(icon, position=ToastImagePosition.AppLogo, circleCrop=bool(n.get("hasIcon"))))

        if n.get("call"):
            toast.scenario = ToastScenario.IncomingCall
            toast.duration = ToastDuration.Long
            for a in n.get("actions", [])[:3]:
                toast.AddAction(ToastButton(a["title"], f"action:{a['i']}"))
            toast.AddAction(ToastButton("Answer · audio on PC", "answer_pc"))
        else:
            reply = next((a for a in n.get("actions", []) if a.get("reply")), None)
            if reply:
                box = ToastInputTextBox("reply", placeholder="Reply…")
                toast.AddInput(box)
                toast.AddAction(ToastButton("Send", f"reply:{reply['i']}", relatedInput=box))
            for a in n.get("actions", []):
                if a is reply or len(toast.actions) >= 5:
                    continue
                toast.AddAction(ToastButton(a["title"], f"action:{a['i']}"))

        key = n["key"]
        # The body click goes through the phonesync: protocol (registered by winutil), so it also works from Action
        # Center after Symphone restarted; the running instance gets it either way.
        toast.launch_action = "phonesync://notif/?" + urlencode({"dev": phone.dev_id, "key": key})
        toast.on_activated = lambda args, key=key, n=n, phone=phone: self._activated(phone, key, n, args.arguments or "",
                                                                                    args.inputs or {})
        try:
            self.toaster.show_toast(toast)
            self.shown[key] = (toast, sig)
        except Exception as e:
            log.warning("toast: %s", e)

    def remove(self, key: str):
        item = self.shown.pop(key, None)
        if item:
            try:
                self.toaster.remove_toast(item[0])
            except Exception:
                pass

    async def open(self, key: str, phone):
        """Body click = tap on the phone: the app opens at the notification's target, in a PC window. Clicks can
        arrive twice (toast event + protocol launch); the second is ignored."""
        now = time.time()
        if now - self._opened.get(key, 0) < 4:
            return {"ok": True}
        self._opened = {k: t for k, t in self._opened.items() if now - t < 60}
        self._opened[key] = now
        return await self.app.open_notification(key, phone)

    def _activated(self, phone, key, n, arg, inputs):
        async def go():
            core = self.app
            if arg in ("", "open") or arg.startswith("phonesync:"):
                await self.open(key, phone)
            elif arg.startswith("action:"):
                await phone.post_json("/api/notif/act", {"key": key, "op": "action", "index": int(arg[7:])})
            elif arg.startswith("reply:"):
                text = inputs.get("reply", "")
                if text:
                    await phone.post_json("/api/notif/act", {"key": key, "op": "action", "index": int(arg[6:]), "text": text})
            elif arg == "answer_pc":
                await core.answer_call_on_pc(n)
        if self.loop:
            asyncio.run_coroutine_threadsafe(go(), self.loop)

    # ------------------------------------------------------------------ simple toasts
    def simple(self, title: str, text: str = "", on_click=None, image: Path | None = None, buttons=()):
        toast = Toast(text_fields=[title, text])
        if image and Path(image).exists():
            toast.AddImage(ToastDisplayImage.fromPath(image, position=ToastImagePosition.Hero))
        handlers = {}
        for label, fn in buttons:
            arg = f"b{len(handlers)}"
            handlers[arg] = fn
            toast.AddAction(ToastButton(label, arg))

        def activated(args):
            fn = handlers.get(args.arguments or "", on_click)
            if fn and self.loop:
                self.loop.call_soon_threadsafe(fn)
        toast.on_activated = activated
        try:
            self.toaster.show_toast(toast)
        except Exception as e:
            log.warning("toast: %s", e)
        return toast

    def received(self, dest: Path, phone_name: str):
        img = dest if dest.suffix.lower() in IMAGE_EXT else None
        self.simple(f"Received from {phone_name}", dest.name, on_click=lambda: os.startfile(dest), image=img,
                    buttons=[("Open", lambda: os.startfile(dest)), ("Show in folder", lambda: reveal(dest))])

    def imported(self, paths: list[Path], phone_name: str):
        first = paths[0]
        n = len(paths)
        img = first if first.suffix.lower() in IMAGE_EXT else None
        self.simple("New photo" if n == 1 else f"{n} new photos", f"From {phone_name} · saved to {first.parent.name}",
                    on_click=lambda: os.startfile(first if n == 1 else first.parent), image=img,
                    buttons=[("Open", lambda: os.startfile(first)), ("Show in folder", lambda: reveal(first))] if n == 1
                    else [("Open folder", lambda: os.startfile(first.parent))])

    # ------------------------------------------------------------------ transfer progress (one toast per job)
    def progress(self, st: dict) -> Toast | None:
        if not settings["transfer_toasts"]:
            return None
        self._seq += 1
        toast = Toast(text_fields=[st["title"]], progress_bar=ToastProgressBar(st["status"], st["caption"], st["progress"]))
        toast.tag = f"xfer{self._seq}"
        toast.group = "transfers"
        toast.suppress_popup = False
        toast.on_activated = self._call(lambda: __import__("phonesync.gui", fromlist=["x"]).show_window())
        try:
            self.toaster.show_toast(toast)
            return toast
        except Exception as e:
            log.warning("progress toast: %s", e)
            return None

    def progress_update(self, toast: Toast | None, st: dict):
        if toast is None:
            return
        toast.progress_bar = ToastProgressBar(st["status"], st["caption"], st["progress"])
        try:
            self.toaster.update_toast(toast)
        except Exception:
            pass

    def progress_done(self, toast: Toast | None):
        if toast is None:
            return
        try:
            self.toaster.remove_toast(toast)
        except Exception:
            pass

    def transfer_done(self, batch, n: int, errors: list[str], remote_dir: str | None = None, local: Path | None = None):
        """Completion toast; quick single-file jobs get one too, but nothing opens on its own."""
        if not settings["transfer_toasts"] and not errors:
            return
        ok = n - len(errors)
        if batch.direction == "up":
            title = (f"Sent to {batch.phone_name}" if not errors else f"Sent {ok} of {n} to {batch.phone_name}")
            text = f"{n} item{'s' if n != 1 else ''} · {remote_dir}" if not errors else "; ".join(errors)[:200]
            self.simple(title, text)
            return
        title = f"Copied from {batch.phone_name}" if not errors else f"Copied {ok} of {n} from {batch.phone_name}"
        text = (local.name if local else "") if not errors else "; ".join(errors)[:200]
        btns = []
        if local and Path(local).exists():
            if Path(local).is_file():
                btns.append(("Open", lambda: os.startfile(local)))
            btns.append(("Show in folder", lambda: reveal(local)))
        self.simple(title, text, on_click=(lambda: reveal(local)) if local else None, buttons=btns,
                    image=local if local and Path(local).suffix.lower() in IMAGE_EXT else None)
