"""System notifications: phone notifications (actions, inline reply, calls) and file transfers.

Windows: native toasts (windows-toasts), with transfer progress bars and the phonesync: protocol for clicks from Action
Center. macOS / Linux: desktop-notifier (Notification Center / freedesktop notifications), same actions and reply;
transfers only report when they finish.
"""
import asyncio
import hashlib
import logging
import re
import time
from pathlib import Path
from urllib.parse import urlencode

from .config import AUMID, APP_NAME, ICON_DIR, BASE_DIR, IS_WIN, IS_MAC, settings
from .desktop import open_path, reveal

log = logging.getLogger("notify")
IMAGE_EXT = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp")


def register_aumid(icon: Path, aumid: str = AUMID, name: str = APP_NAME):
    import winreg
    key = winreg.CreateKey(winreg.HKEY_CURRENT_USER, rf"Software\Classes\AppUserModelId\{aumid}")
    winreg.SetValueEx(key, "DisplayName", 0, winreg.REG_SZ, name)
    winreg.SetValueEx(key, "IconUri", 0, winreg.REG_SZ, str(icon))
    winreg.SetValueEx(key, "IconBackgroundColor", 0, winreg.REG_SZ, "FF1F6FEB")
    winreg.CloseKey(key)


def app_aumid(pkg: str) -> str:
    """Toast sender for one phone app: Windows heads a toast with its sender's name and icon (and lists each sender
    in its notification settings), so a WhatsApp message says WhatsApp instead of Symphone."""
    return f"{AUMID}.App.{re.sub(r'[^A-Za-z0-9.]', '_', pkg)[:96]}"


def unregister_app_aumids():
    """Uninstaller: remove the per-app toast senders."""
    import winreg
    base = r"Software\Classes\AppUserModelId"
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, base) as k:
            names = [winreg.EnumKey(k, i) for i in range(winreg.QueryInfoKey(k)[0])]
    except OSError:
        return
    for n in names:
        if n.startswith(f"{AUMID}.App."):
            try:
                winreg.DeleteKey(winreg.HKEY_CURRENT_USER, rf"{base}\{n}")
            except OSError:
                pass


def _tag(key: str) -> str:
    return hashlib.sha1(key.encode()).hexdigest()[:16]


class _Base:
    """What phone notifications and transfers do; the subclasses show them."""

    def __init__(self, app):
        self.app = app                      # Core
        self.loop: asyncio.AbstractEventLoop | None = None
        self.shown: dict[str, tuple[object, tuple]] = {}
        self._opened: dict[str, float] = {}
        app.transfers.notifier = self

    def bind(self, loop):
        self.loop = loop

    def _call(self, fn):
        """Notification callbacks run on a system thread: hop back to the asyncio loop."""
        def cb(*_a):
            if self.loop:
                self.loop.call_soon_threadsafe(fn)
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

    def _wanted(self, n: dict) -> tuple | None:
        """The notification's signature if it should be shown (None: muted, ongoing or unchanged)."""
        if not settings["toasts"] or n["pkg"] in settings["toast_muted_apps"]:
            return None
        if n.get("ongoing") and not n.get("call"):
            return None
        sig = (n.get("title"), n.get("text"), tuple(a["title"] for a in n.get("actions", [])))
        prev = self.shown.get(n["key"])
        if prev and prev[1] == sig:
            return None
        return sig

    @staticmethod
    def _body(n: dict) -> str:
        body = n.get("text") or ""
        msgs = n.get("messages")
        if msgs:
            body = "\n".join(f"{m['from']}: {m['text']}" if m.get("from") else m["text"] for m in msgs[-3:])
        return body[:300]

    def _attribution(self, n: dict, phone) -> str:
        many = len(self.app.hub.phones) > 1
        return f"{n.get('app', '')} · {phone.name}" if many else n.get("app", "")

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
        self._acted(key)

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

    def _acted(self, key: str):
        """A toast that was clicked leaves Action Center by itself: that isn't a dismissal."""

    def dismissed(self, phone, key: str, n: dict):
        """The user dismissed the toast on the PC: dismiss the notification on the phone too (calls and notifications
        the phone doesn't let you clear stay)."""
        self.shown.pop(key, None)
        if n.get("call") or not n.get("clearable", True) or not self.loop:
            return

        async def go():
            try:
                await phone.post_json("/api/notif/act", {"key": key, "op": "dismiss"}, timeout=5)
            except Exception as e:
                log.debug("dismiss on phone: %s", e)
        asyncio.run_coroutine_threadsafe(go(), self.loop)

    # ------------------------------------------------------------------ file notifications
    def received(self, dest: Path, phone_name: str):
        img = dest if dest.suffix.lower() in IMAGE_EXT else None
        self.simple(f"Received from {phone_name}", dest.name, on_click=lambda: open_path(dest), image=img,
                    buttons=[("Open", lambda: open_path(dest)), ("Show in folder", lambda: reveal(dest))])

    def imported(self, paths: list[Path], phone_name: str):
        first = paths[0]
        n = len(paths)
        img = first if first.suffix.lower() in IMAGE_EXT else None
        self.simple("New photo" if n == 1 else f"{n} new photos", f"From {phone_name} · saved to {first.parent.name}",
                    on_click=lambda: open_path(first if n == 1 else first.parent), image=img,
                    buttons=[("Open", lambda: open_path(first)), ("Show in folder", lambda: reveal(first))] if n == 1
                    else [("Open folder", lambda: open_path(first.parent))])

    def transfer_done(self, batch, n: int, errors: list[str], remote_dir: str | None = None, local: Path | None = None):
        """Completion notification; quick single-file jobs get one too, but nothing opens on its own."""
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
                btns.append(("Open", lambda: open_path(local)))
            btns.append(("Show in folder", lambda: reveal(local)))
        self.simple(title, text, on_click=(lambda: reveal(local)) if local else None, buttons=btns,
                    image=local if local and Path(local).suffix.lower() in IMAGE_EXT else None)

    # ------------------------------------------------------------------ transfer progress (Windows only)
    def progress(self, st: dict):
        return None

    def progress_update(self, toast, st: dict):
        pass

    def progress_done(self, toast):
        pass


class WindowsNotifier(_Base):
    def __init__(self, app):
        super().__init__(app)
        from windows_toasts import InteractableWindowsToaster
        icon = BASE_DIR / "ui" / "icon.png"
        try:
            register_aumid(icon)
        except OSError as e:
            log.warning("aumid: %s", e)
        self.toaster = InteractableWindowsToaster(APP_NAME, AUMID)
        self._seq = 0
        self._app_toasters: dict[str, object] = {}       # pkg -> toaster under that app's own sender
        self._in_center: set[str] = set()                # keys whose toast was seen in Action Center

    def bind(self, loop):
        super().bind(loop)
        loop.create_task(self._watch_center())

    async def _app_toaster(self, n: dict, phone):
        """The phone app's own toast sender (name + icon), registered the first time it notifies."""
        from windows_toasts import InteractableWindowsToaster
        pkg, name = n.get("pkg") or "", n.get("app") or ""
        if not pkg or not name:
            return self.toaster
        t = self._app_toasters.get(pkg)
        if t is not None and t.applicationText == name:
            return t
        icon = ICON_DIR / f"{pkg}.png"
        try:
            if not icon.exists():
                icon.write_bytes(await phone.get_bytes(f"/api/icon/{pkg}"))
        except Exception:
            icon = BASE_DIR / "ui" / "icon.png"
        try:
            register_aumid(icon, app_aumid(pkg), name)
            t = InteractableWindowsToaster(name, app_aumid(pkg))
        except Exception as e:
            log.warning("toast sender for %s: %s", pkg, e)
            return self.toaster
        self._app_toasters[pkg] = t
        return t

    def _acted(self, key: str):
        self._in_center.discard(key)

    def _on_dismissed(self, phone, key, n, args):
        from windows_toasts import ToastDismissalReason
        try:
            if args.reason != ToastDismissalReason.USER_CANCELED:      # timed out (into Action Center) / replaced
                return
        except Exception:
            return
        if self.loop:
            self.loop.call_soon_threadsafe(self.dismissed, phone, key, n)

    async def _watch_center(self):
        """Clearing a toast in Action Center raises no event: a toast that was there and is gone was cleared."""
        from winrt.windows.ui.notifications import ToastNotificationManager
        while True:
            await asyncio.sleep(4)
            if not self.shown:
                continue
            try:
                there = set()
                for aumid in {item[2].notifierAUMID for item in self.shown.values()}:
                    there |= {(aumid, t.tag) for t in ToastNotificationManager.history.get_history_with_id(aumid)}
            except Exception as e:
                log.debug("action center: %s", e)
                continue
            for key, (toast, _sig, toaster, phone, n) in list(self.shown.items()):
                if (toaster.notifierAUMID, toast.tag) in there:
                    self._in_center.add(key)
                elif key in self._in_center:
                    self._in_center.discard(key)
                    self.dismissed(phone, key, n)

    async def show(self, n: dict, phone):
        from windows_toasts import (Toast, ToastButton, ToastDisplayImage, ToastImagePosition, ToastInputTextBox,
                                    ToastScenario, ToastDuration)
        sig = self._wanted(n)
        if sig is None:
            return
        toast = Toast(text_fields=[n.get("title") or n.get("app"), self._body(n)])
        toast.tag = _tag(n["key"])
        toast.group = "phone"
        # the app's name heads the toast (its own sender); with several phones, which one it came from
        toaster = await self._app_toaster(n, phone)
        if toaster is self.toaster:
            toast.attribution_text = self._attribution(n, phone)
        elif len(self.app.hub.phones) > 1:
            toast.attribution_text = phone.name
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
        toast.on_dismissed = lambda args, key=key, n=n, phone=phone: self._on_dismissed(phone, key, n, args)
        prev = self.shown.get(key)
        if prev and prev[2] is not toaster:               # the app's sender changed (renamed app): move it
            self.remove(key)
        try:
            toaster.show_toast(toast)
            self.shown[key] = (toast, sig, toaster, phone, n)
        except Exception as e:
            log.warning("toast: %s", e)

    def remove(self, key: str):
        """The notification went away on the phone."""
        item = self.shown.pop(key, None)
        self._in_center.discard(key)
        if item:
            try:
                item[2].remove_toast(item[0])
            except Exception:
                pass

    def simple(self, title: str, text: str = "", on_click=None, image: Path | None = None, buttons=()):
        from windows_toasts import Toast, ToastButton, ToastDisplayImage, ToastImagePosition
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

    def progress(self, st: dict):
        from windows_toasts import Toast, ToastProgressBar
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

    def progress_update(self, toast, st: dict):
        from windows_toasts import ToastProgressBar
        if toast is None:
            return
        toast.progress_bar = ToastProgressBar(st["status"], st["caption"], st["progress"])
        try:
            self.toaster.update_toast(toast)
        except Exception:
            pass

    def progress_done(self, toast):
        if toast is None:
            return
        try:
            self.toaster.remove_toast(toast)
        except Exception:
            pass


class DesktopNotifier(_Base):
    """macOS Notification Center / Linux freedesktop notifications (desktop-notifier). Falls back to plain
    osascript / notify-send messages (no buttons) when the notification service refuses, e.g. an unsigned macOS app."""

    def __init__(self, app):
        super().__init__(app)
        self._dn = None
        self._fallback = False
        try:
            from desktop_notifier import DesktopNotifier as _DN, Icon
            self._dn = _DN(app_name=APP_NAME, app_icon=Icon(path=BASE_DIR / "ui" / "icon.png"))
        except Exception as e:
            log.info("notifications: %s", e)
            self._fallback = True

    def _submit(self, coro):
        if self.loop:
            asyncio.run_coroutine_threadsafe(coro, self.loop)
        else:
            coro.close()

    async def _send(self, title: str, text: str, *, buttons=(), reply=None, on_click=None, icon: Path | None = None,
                    image: Path | None = None, urgent=False):
        if self._dn is not None and not self._fallback:
            from desktop_notifier import Button, ReplyField, Urgency, Icon, Attachment
            try:
                return await self._dn.send(
                    title=title or APP_NAME, message=text or "",
                    buttons=[Button(title=label, on_pressed=self._call(fn)) for label, fn in buttons],
                    reply_field=ReplyField(title="Reply", button_title="Send", on_replied=reply) if reply else None,
                    on_clicked=self._call(on_click) if on_click else None,
                    icon=Icon(path=Path(icon)) if icon else None,
                    attachment=Attachment(path=Path(image)) if image and Path(image).exists() else None,
                    urgency=Urgency.Critical if urgent else Urgency.Normal)
            except Exception as e:
                log.info("notifications unavailable (%s); using plain messages", e)
                self._fallback = True
        await asyncio.to_thread(_plain, title, text)
        return None

    async def show(self, n: dict, phone):
        sig = self._wanted(n)
        if sig is None:
            return
        key = n["key"]
        self.remove(key)                                  # an update replaces the previous one
        icon = await self._icon_file(n, phone)
        act = lambda arg, inputs=None: self._activated(phone, key, n, arg, inputs or {})
        buttons, reply_cb = [], None
        if n.get("call"):
            buttons = [(a["title"], (lambda a=a: act(f"action:{a['i']}"))) for a in n.get("actions", [])[:2]]
            buttons.append(("Answer · audio on PC", lambda: act("answer_pc")))
        else:
            reply = next((a for a in n.get("actions", []) if a.get("reply")), None)
            if reply:
                reply_cb = lambda text, i=reply["i"]: act(f"reply:{i}", {"reply": text})
            buttons = [(a["title"], (lambda a=a: act(f"action:{a['i']}"))) for a in n.get("actions", [])
                       if a is not reply][:3]
        title = n.get("title") or n.get("app") or ""
        attribution = self._attribution(n, phone)
        body = self._body(n)
        if attribution and attribution != title:          # no attribution line here: the app name goes first
            body = f"{attribution}\n{body}"
        self.shown[key] = (None, sig)
        res = await self._send(title, body, buttons=buttons, reply=reply_cb, on_click=lambda: act("open"), icon=icon,
                               urgent=bool(n.get("call")))
        if key in self.shown:
            self.shown[key] = (res, sig)

    def remove(self, key: str):
        item = self.shown.pop(key, None)
        if item and item[0] is not None and self._dn is not None:
            self._submit(self._clear(item[0]))

    async def _clear(self, identifier: str):
        try:
            await self._dn.clear(identifier)
        except Exception:
            pass

    def simple(self, title: str, text: str = "", on_click=None, image: Path | None = None, buttons=()):
        self._submit(self._send(title, text, buttons=list(buttons), on_click=on_click, image=image))


def _plain(title: str, text: str):
    import subprocess
    try:
        if IS_MAC:
            q = lambda s: '"' + (s or "").replace("\\", "\\\\").replace('"', '\\"') + '"'
            subprocess.run(["osascript", "-e", f"display notification {q(text)} with title {q(title)}"],
                           capture_output=True, timeout=5)
        else:
            subprocess.run(["notify-send", "-a", APP_NAME, "-i", str(BASE_DIR / "ui" / "icon.png"), title, text],
                           capture_output=True, timeout=5)
    except Exception as e:
        log.debug("plain notification: %s", e)


Notifier = WindowsNotifier if IS_WIN else DesktopNotifier
