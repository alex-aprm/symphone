"""Local UI backend on 127.0.0.1: static UI, JSON API, thumbnails/media proxy and a live event stream."""
import asyncio
import json
import logging
import mimetypes
import os
import tempfile
from pathlib import Path
from urllib.parse import quote

import aiohttp
from aiohttp import web

from .config import UI_DIR, UI_PORT, ICON_DIR, settings
from . import desktop

log = logging.getLogger("ui")
mimetypes.add_type("font/woff2", ".woff2")      # the icon font (not in every system's table)


ALLOWED_HOSTS = {f"127.0.0.1:{UI_PORT}", f"localhost:{UI_PORT}"}


@web.middleware
async def local_only(req, handler):
    # The server listens on the LAN only for phones (/peer/*: pairing and "I'm here" hellos). Everything else is the
    # local UI: loopback peers only, plus a Host check against DNS rebinding; mutating calls need a custom header,
    # which a foreign origin can't send without a CORS preflight we never grant.
    if req.path.startswith("/peer/"):
        return await handler(req)
    if req.remote not in ("127.0.0.1", "::1") or req.host not in ALLOWED_HOSTS:
        return web.Response(status=403)
    if req.method not in ("GET", "HEAD") and req.headers.get("X-PhoneSync") != "1":
        return web.Response(status=403)
    resp = await handler(req)
    if req.path.endswith((".js", ".css", ".html")):
        resp.headers["Cache-Control"] = "no-cache"   # always revalidate UI code after updates
    return resp


def build_app(core) -> web.Application:
    app = web.Application(client_max_size=1 << 40, middlewares=[local_only])
    r = app.router

    def j(data, status=200):
        return web.json_response(data, status=status, dumps=lambda o: json.dumps(o, default=str))

    async def guard(handler):
        try:
            return await handler()
        except (ConnectionError, aiohttp.ClientError, asyncio.TimeoutError) as e:
            return j({"ok": False, "error": f"phone unreachable: {e}"}, 503)

    # ------------------------------------------------------------ static
    from . import screen_ws
    screen_ws.attach(app, core)

    async def screen_open(req):
        b = await req.json()
        kind = b.pop("kind", "screen")
        if kind == "screen" and b.get("mode") == "main" and settings["unlock_on_mirror"]:
            await core.unlock()
        await screen_ws.open_window(kind, **b)
        return j({"ok": True})
    r.add_post("/api/screen/open", screen_open)

    async def index(req):
        return web.FileResponse(UI_DIR / "index.html", headers={"Cache-Control": "no-cache"})
    r.add_get("/", index)
    r.add_static("/ui/", UI_DIR, append_version=False)

    # ------------------------------------------------------------ state & events
    async def state(req):
        return j(core.state())
    r.add_get("/api/state", state)

    async def events(req):
        resp = web.StreamResponse(headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache"})
        await resp.prepare(req)
        q: asyncio.Queue = asyncio.Queue(maxsize=2000)
        core.ui_queues.append(q)
        try:
            await resp.write(f"event: state\ndata: {json.dumps(core.state(), default=str)}\n\n".encode())
            while True:
                try:
                    name, data = await asyncio.wait_for(q.get(), 15)
                    await resp.write(f"event: {name}\ndata: {json.dumps(data, default=str)}\n\n".encode())
                except asyncio.TimeoutError:
                    await resp.write(b": ping\n\n")
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        finally:
            core.ui_queues.remove(q)
        return resp
    r.add_get("/api/events", events)

    # ------------------------------------------------------------ pairing / settings
    async def pair(req):
        b = await req.json()
        try:
            err = await core.hub.pair_code(b["ip"], b["code"])
        except Exception as e:
            err = str(e)
        return j({"ok": err is None, "error": err or ""})
    r.add_post("/api/pair", pair)

    async def pair_start(req):
        """QR code for pairing a phone (valid 10 minutes)."""
        import segno
        payload = core.hub.qr_payload()
        qr = segno.make(payload, error="m")
        svg = qr.svg_inline(scale=6, dark="#000", light=None, border=2, omitsize=True)
        core.ui_event("state", core.state())
        return j({"ok": True, "payload": payload, "svg": svg, "ips": payload.split("&a=")[1].split("&")[0]})
    r.add_post("/api/pair/start", pair_start)

    # ------------------------------------------------------------ first-run setup wizard
    async def setup_state(req):
        from . import setup
        return j(await setup.state(core))
    r.add_get("/api/setup", setup_state)

    async def setup_op(req):
        from . import setup
        b = await req.json()
        op = b.get("op")
        if op == "apk-qr":                      # QR code: download the phone app
            import segno
            return j({"ok": True, "url": setup.APK_URL,
                      "svg": segno.make(setup.APK_URL, error="m").svg_inline(scale=6, dark="#000", light=None, border=2, omitsize=True)})
        if op == "adb-pair":                    # QR code for Wireless debugging > Pair device with QR code
            import segno
            text = core.adbm.start_qr_pair()
            return j({"ok": True, "svg": segno.make(text, error="m").svg_inline(scale=6, dark="#000", light=None, border=2, omitsize=True)})
        if op == "adb-pair-stop":
            core.adbm.stop_qr_pair()
            return j({"ok": True})
        if op == "extras":
            return j(setup.install_extras(list(b.get("items") or [])))
        if op == "done":
            settings["setup_done"] = True
            core.hub.pair_secret = None
            core.adbm.stop_qr_pair()
            core.ui_event("state", core.state())
            return j({"ok": True})
        return j({"ok": False, "error": "unknown op"}, 400)
    r.add_post("/api/setup", setup_op)

    async def pair_stop(req):
        core.hub.pair_secret = None
        core.ui_event("state", core.state())
        return j({"ok": True})
    r.add_post("/api/pair/stop", pair_stop)

    async def peer_pair(req):
        try:
            b = await req.json()
        except Exception:
            return web.Response(status=400)
        res = core.hub.peer_pair(b)
        if not res:
            await asyncio.sleep(1)          # slows down guessing
            return j({"ok": False, "error": "pairing not open or wrong code"}, 403)
        ph = b.get("phone") or {}
        core.ui_event("paired", {"name": ph.get("name"), "via": "Wi-Fi"})
        core.notifier.simple("Phone paired", f"{ph.get('name') or 'Phone'} is now connected to this PC")
        return j(res)
    r.add_post("/peer/pair", peer_pair)

    async def peer_hello(req):
        try:
            b = await req.json()
        except Exception:
            return web.Response(status=400)
        ok = core.hub.peer_hello(b, req.remote or "")
        return j({"ok": ok, "pc": settings["pc_id"]}, 200 if ok else 403)
    r.add_post("/peer/hello", peer_hello)

    async def device_op(req):
        b = await req.json()
        op, dev = b.get("op"), b.get("id", "")
        if op == "switch":
            core.switch_device(dev)
        elif op == "forget":
            await core.hub.forget(dev)
            desktop.install_send_to(core.hub)
        elif op == "rename":
            settings.upsert_device(dev, display_name=str(b.get("name", "")).strip()[:60])
            if dev == settings["active_device"]:
                core.drive.stop()
            desktop.install_send_to(core.hub)
        elif op == "master":
            p = core.hub.phones.get(dev)
            if p and p.connected:
                await p.post_json("/api/pc/master", {"id": settings["pc_id"]}, timeout=5)
                await p.refresh_info()
        core.ui_event("state", core.state())
        return j({"ok": True})
    r.add_post("/api/device", device_op)

    async def reconnect(req):
        core.adbm._last_mdns = 0
        for p in core.hub.phones.values():
            p.wake()
        return j({"ok": True})
    r.add_post("/api/reconnect", reconnect)

    async def theme(req):
        return j(await asyncio.to_thread(desktop.theme))
    r.add_get("/api/theme", theme)

    async def phone_picture(req):
        """A picture of the phone with its own wallpaper (?dev=<id>&h=<px>)."""
        from . import phonepic
        dev = req.query.get("dev") or settings["device_id"] or ""
        h = max(32, min(800, int(req.query.get("h", "160"))))
        p = core.hub.phones.get(dev)
        changed = False
        if p is not None:
            dinfo = settings.device(dev) or {}
            try:            # the model's photo (looked up once, then cached), then the phone's wallpaper for its screen
                changed |= bool(await asyncio.wait_for(
                    phonepic.fetch_model_photo(dev, p.name, dinfo.get("model") or ""), 25)) and not \
                    phonepic.model_info(dev).get("icons_done")
            except Exception as e:
                log.debug("model photo: %s", e)
            if p.connected:
                try:
                    changed |= await asyncio.wait_for(phonepic.refresh_wallpaper(p), 6)
                except Exception:
                    pass
        if changed:
            asyncio.ensure_future(refresh_icons())
        accent = (await asyncio.to_thread(desktop.theme))["accent"]
        data = await asyncio.to_thread(phonepic.png, dev, h, accent)
        return web.Response(body=data, content_type="image/png", headers={"Cache-Control": "max-age=60"})

    async def refresh_icons():
        """The file manager entry and Send to shortcuts show the current phone picture."""
        from . import phonepic
        from .drive import MOUNT_DIR
        info_path = phonepic.ICON_DIR / f"model-{settings['device_id']}.json"
        try:
            await asyncio.to_thread(desktop.install_send_to, core.hub)
            if desktop.IS_WIN and core.drive.as_device and core.drive.mounted:
                ico = await asyncio.to_thread(phonepic.ico, settings["device_id"] or "", desktop.theme()["accent"])
                await asyncio.to_thread(desktop.register_phone_folder, settings.phone_name(), MOUNT_DIR, ico)
            if info_path.exists():
                d = json.loads(info_path.read_text("utf-8"))
                d["icons_done"] = True
                info_path.write_text(json.dumps(d), "utf-8")
        except Exception as e:
            log.info("icons: %s", e)

    async def picture_info(req):
        from . import phonepic
        return j(phonepic.source(req.query.get("dev") or settings["device_id"] or ""))

    async def picture_set(req):
        """Body: an image file (the user's own picture for this phone)."""
        from . import phonepic
        dev = req.query.get("dev") or settings["device_id"] or ""
        data = await req.read()
        try:
            await asyncio.to_thread(phonepic.set_custom, dev, data)
        except Exception as e:
            return j({"ok": False, "error": f"not an image: {e}"}, 400)
        asyncio.ensure_future(refresh_icons())
        return j({"ok": True})

    async def picture_reset(req):
        from . import phonepic
        await asyncio.to_thread(phonepic.clear_custom, req.query.get("dev") or settings["device_id"] or "")
        asyncio.ensure_future(refresh_icons())
        return j({"ok": True})
    r.add_get("/api/device/picture", picture_info)
    r.add_post("/api/device/picture", picture_set)
    r.add_delete("/api/device/picture", picture_reset)
    r.add_get("/api/phone-picture", phone_picture)

    async def get_settings(req):
        s = {k: v for k, v in settings.data.items() if k not in ("devices", "pin", "local_secret", "wfd_network")}
        s["display_name"] = settings["display_name"]
        s["pc_name"] = settings.pc_name()
        s["hasPin"] = bool(settings["pin"])
        return j(s)
    r.add_get("/api/settings", get_settings)

    async def set_settings(req):
        b = await req.json()
        allowed = {"drive_letter", "drive_enabled", "auto_import", "import_dir", "downloads_dir", "unlock_on_mirror",
                   "toasts", "toast_muted_apps", "mirror_max_size", "mirror_bitrate", "mirror_fps",
                   "mirror_wifi_bitrate", "mirror_wifi_fps",
                   "turn_screen_off", "autostart", "mirror_audio", "drive_mode", "display_name", "pc_name",
                   "clipboard_sync", "pc_keyboard", "transfer_toasts", "ble", "wifi_direct", "wfd_prefer", "sidecar_orientation", "sidecar_scale", "sidecar_size", "sidecar_size_auto"}
        upd = {k: v for k, v in b.items() if k in allowed}
        if "pin" in b:
            settings.set_pin(str(b["pin"]).strip())
        old_letter = settings["drive_letter"]
        settings.update(**upd)
        if "autostart" in upd:
            desktop.set_autostart(bool(upd["autostart"]))
        if "wfd_prefer" in upd or "wifi_direct" in upd:      # tell connected phones (join / leave the direct link)
            core.nearby._wfd_creds()
            for p in core.hub.phones.values():
                if p.connected:
                    try:
                        await p.post_json("/api/pc/wfd", core.hub.wfd_creds or {"ssid": ""}, timeout=5)
                    except Exception:
                        pass
        if "pc_keyboard" in upd and screen_ws.kbd:
            if upd["pc_keyboard"]:
                screen_ws.kbd.sessions_changed()
            elif screen_ws.kbd._serial:
                await screen_ws.kbd.release()
        if "display_name" in upd:
            desktop.install_send_to(core.hub)
        if "pc_name" in upd:
            for p in core.hub.phones.values():
                if p.connected:
                    asyncio.ensure_future(p.post_json("/api/pc", {"id": settings["pc_id"], "name": settings.pc_name()}))
        if ("drive_letter" in upd and upd["drive_letter"] != old_letter) or upd.get("drive_enabled") is False                 or "drive_mode" in upd or "display_name" in upd:
            core.drive.stop()          # the drive loop remounts with the new settings
            if upd.get("drive_enabled") is False or upd.get("drive_mode") == "letter":
                desktop.unregister_phone_folder()
        return j({"ok": True})
    r.add_post("/api/settings", set_settings)

    # ------------------------------------------------------------ media
    async def media_list(req):
        return j({"rows": core.media.sorted_rows(), "gen": core.media.gen, "synced": core.media.synced})
    r.add_get("/api/media", media_list)

    async def media_resync(req):
        return await guard(lambda: _ok(core.media.full_sync()))
    r.add_post("/api/media/resync", media_resync)

    async def thumb(req):
        mid = int(req.match_info["id"])
        size = int(req.query.get("s", 256))
        try:
            data = await core.media.thumb(mid, size)
        except Exception:
            return web.Response(status=404)
        return web.Response(body=data, content_type="image/jpeg", headers={"Cache-Control": "max-age=604800"})
    r.add_get("/thumb/{id}", thumb)

    async def media_orig(req):
        """Streams an original (supports Range for video seeking)."""
        mid = int(req.match_info["id"])
        headers = {"Range": req.headers["Range"]} if "Range" in req.headers else {}
        try:
            up = await core.phone.request("GET", f"/api/media/{mid}", headers=headers,
                                          timeout=aiohttp.ClientTimeout(total=None, sock_read=60))
        except Exception:
            return web.Response(status=503)
        async with up:
            h = {k: up.headers[k] for k in ("Content-Type", "Content-Range", "Accept-Ranges", "Last-Modified", "ETag")
                 if k in up.headers}
            resp = web.StreamResponse(status=up.status, headers=h)
            if "Content-Length" in up.headers:
                resp.content_length = int(up.headers["Content-Length"])
            await resp.prepare(req)
            try:
                async for chunk in up.content.iter_chunked(1 << 20):
                    await resp.write(chunk)
            except (ConnectionResetError, aiohttp.ClientError):
                pass
        return resp
    r.add_get("/media/{id}", media_orig)

    async def media_download(req):
        b = await req.json()
        async def go():
            ids = [int(i) for i in b["ids"]]
            ph = core.phone
            what = core.media.items[ids[0]]["name"] if len(ids) == 1 and ids[0] in core.media.items else f"{len(ids)} items"
            async with core.transfers.batch(f"Copying {what} from {ph.name}", "down", ph.name) as bt:
                bt.files_total = len(ids)
                paths = await core.media.download(ids, b.get("dest") or None)
            local = Path(paths[0]) if len(paths) == 1 else (Path(paths[0]).parent if paths else None)
            core.notifier.transfer_done(bt, len(ids), [] if len(paths) == len(ids) else ["some items failed"], local=local)
            return j({"ok": True, "paths": paths})
        return await guard(go)
    r.add_post("/api/media/download", media_download)

    async def media_copy(req):
        """Copy (Ctrl+C) in Photos: downloads the originals and puts them on the clipboard, ready to paste."""
        b = await req.json()
        async def go():
            ids = [int(i) for i in b["ids"]][:500]
            if len(ids) > 3:
                ph = core.phone
                async with core.transfers.batch(f"Copying {len(ids)} items from {ph.name}", "down", ph.name) as bt:
                    bt.files_total = len(ids)
                    paths = await core.media.download(ids)
            else:
                paths = await core.media.download(ids)
            order = {core.media.local_name(core.media.items[i]).name: n for n, i in enumerate(ids) if i in core.media.items}
            paths.sort(key=lambda p: order.get(Path(p).name, 0))
            ok = await asyncio.to_thread(desktop.copy_files, paths)
            if not paths:
                return j({"ok": False, "error": "Couldn't get the files from the phone"})
            return j({"ok": ok, "count": len(paths), "failed": len(ids) - len(paths),
                      **({} if ok else {"error": "Couldn't put the files on the clipboard"})})
        return await guard(go)
    r.add_post("/api/media/copy", media_copy)

    async def open_local(req):
        """Downloads (if needed) and opens a media item with the default app."""
        mid = int(req.match_info["id"])
        async def go():
            paths = await core.media.download([mid])
            if paths:
                desktop.open_path(paths[0])
            return j({"ok": bool(paths)})
        return await guard(go)
    r.add_post("/api/media/{id}/open", open_local)

    # ------------------------------------------------------------ files
    async def ls(req):
        return await guard(lambda: _json(core.phone.get_json(f"/api/ls?path={quote(req.query.get('path', '/'))}")))
    r.add_get("/api/ls", ls)

    async def files_download(req):
        b = await req.json()
        dest = b.get("dest") or settings["downloads_dir"]
        async def go():
            res = await core.transfers.fetch_paths(b["paths"], Path(dest))
            return j({"ok": not res["errors"], **res})
        return await guard(go)
    r.add_post("/api/files/download", files_download)

    async def files_upload(req):
        """Multipart upload from the UI (drag & drop). Streams each part to disk, then multipath to the phone."""
        target = req.query.get("dir", "/Download/PC")
        reader = await req.multipart()
        tmpdir = Path(tempfile.gettempdir()) / "phonesync-up"
        tmpdir.mkdir(exist_ok=True)
        files = []
        while (part := await reader.next()) is not None:
            if not part.filename:
                continue
            rel = part.filename.replace("\\", "/").lstrip("/")
            p = tmpdir / os.urandom(4).hex() / Path(rel).name
            p.parent.mkdir(parents=True, exist_ok=True)
            with open(p, "wb") as f:
                while chunk := await part.read_chunk(1 << 20):
                    f.write(chunk)
            files.append((p, f"{target.rstrip('/')}/{rel}"))
        async def go():
            ph = core.phone
            errors = []
            n = len(files)
            async with core.transfers.batch(f"Sending {n} file{'s' if n != 1 else ''} to {ph.name}", "up", ph.name) as bt:
                bt.files_total = n
                for p, remote in files:
                    try:
                        await core.transfers.upload(p, remote)
                    except Exception as e:
                        errors.append(f"{p.name}: {e}")
                    finally:
                        p.unlink(missing_ok=True)
            core.notifier.transfer_done(bt, n, errors, remote_dir=target)
            return j({"ok": not errors, "count": n})
        return await guard(go)
    r.add_post("/api/files/upload", files_upload)

    async def send_paths(req):
        b = await req.json()
        return await guard(lambda: _json(core.send_files(b["paths"], b.get("dir", "/Download/PC"), b.get("dev"))))
    r.add_post("/api/send", send_paths)

    async def pick(req):
        """Native file/folder picker (pywebview) for uploads and destination folders."""
        b = await req.json()
        from . import gui
        res = await asyncio.to_thread(gui.pick, b.get("kind", "files"))
        return j({"paths": res or []})
    r.add_post("/api/pick", pick)

    # ------------------------------------------------------------ notifications
    async def notifs(req):
        return j({"items": sorted(core.notifications.values(), key=lambda n: -n["when"]),
                  "history": core.history_for_active()})
    r.add_get("/api/notifications", notifs)

    async def notif_history_clear(req):
        core.notif_history = []
        core._save_history()
        return j({"ok": True})
    r.add_delete("/api/notifications/history", notif_history_clear)

    async def notif_open(req):
        """From a toast body click via the phonesync: protocol (see __main__)."""
        b = await req.json()
        phone = core.hub.phones.get(b.get("dev") or "") or core.hub.active
        return j(await core.notifier.open(b["key"], phone))
    r.add_post("/api/notif/open", notif_open)

    async def notif_act(req):
        b = await req.json()
        async def go():
            res = await core.notif_action(b["key"], b["op"], b.get("index", -1), b.get("text"))
            return j(res)
        return await guard(go)
    r.add_post("/api/notif/act", notif_act)

    async def notif_icon(req):
        key = req.query["key"]
        return await guard(lambda: _bytes(core.phone.get_bytes(f"/api/notif/icon?key={quote(key)}"), "image/png"))
    r.add_get("/notif-icon", notif_icon)

    # ------------------------------------------------------------ apps
    async def apps(req):
        return await guard(lambda: _json(core.phone.get_json("/api/apps")))
    r.add_get("/api/apps", apps)

    async def app_icon(req):
        pkg = req.match_info["pkg"]
        p = ICON_DIR / f"{pkg}.png"
        if not p.exists():
            try:
                p.write_bytes(await core.phone.get_bytes(f"/api/icon/{pkg}"))
            except Exception:
                return web.Response(status=404)
        return web.FileResponse(p, headers={"Cache-Control": "max-age=86400"})
    r.add_get("/icon/{pkg}", app_icon)

    async def launch(req):
        b = await req.json()
        async def go():
            await core.mirror.app(b["pkg"], b.get("label", ""), own_window=b.get("window", True))
            return j({"ok": True})
        return await guard(go)
    r.add_post("/api/launch", launch)

    # ------------------------------------------------------------ phone control
    async def mirror(req):
        b = await req.json()
        async def go():
            kind = b.get("kind", "screen")
            if kind == "screen":
                await core.mirror.screen(unlock=b.get("unlock", True))
            elif kind == "phone":
                await core.mirror.phone()
            elif kind == "camera":
                await core.mirror.camera(b.get("facing", "back"))
            elif kind == "stop":
                core.mirror.stop(b["key"])
            core.ui_event("state", core.state())
            return j({"ok": True})
        return await guard(go)
    r.add_post("/api/mirror", mirror)

    async def unlock(req):
        return await guard(lambda: _json(core.unlock()))
    r.add_post("/api/unlock", unlock)

    async def lock(req):
        return j(await core.lock())
    r.add_post("/api/lock", lock)

    async def key(req):
        b = await req.json()
        await core.adbm.key(b["code"])
        return j({"ok": True})
    r.add_post("/api/key", key)

    async def capture(req):
        b = await req.json()
        async def go():
            if b.get("native"):
                await core.adbm.take_photo_native()
                return j({"ok": True})
            if settings["unlock_on_mirror"] and b.get("unlock"):
                await core.unlock()
            return j(await core.phone.post_json("/api/capture", {"facing": b.get("facing", "back")}))
        return await guard(go)
    r.add_post("/api/capture", capture)

    async def clipboard(req):
        return await guard(lambda: _json(core.push_clipboard()))
    r.add_post("/api/clipboard", clipboard)

    async def audio(req):
        b = await req.json()
        what, on = b["what"], b.get("on", True)
        # Remember the switch: the route comes back after a restart / reconnect (core._audio_keeper).
        routes = [r for r in (settings["audio_routes"] or []) if r != what]
        if what in ("system", "mic"):
            routes = [r for r in routes if r not in ("system", "mic")]
        if on:
            routes.append(what)
        settings["audio_routes"] = routes
        async def go():
            if what in ("system", "mic"):                       # PC -> phone
                if on:
                    await core.pc_audio.start(what)
                else:
                    await core.pc_audio.stop()
            elif what == "bt_sink":                             # phone -> PC over Bluetooth A2DP
                core.bt.start(settings["display_name"] or settings["device_name"] or "") if on else core.bt.stop()
                if on:
                    await asyncio.sleep(0.5)
                    if not core.bt.active:
                        core.bt.start("")                        # fall back to first A2DP source
            elif what in ("output", "playback", "mic-voice-communication", "voice-call"):   # phone -> PC via adb
                key = f"audio:{what}"
                if on:
                    core.mirror.audio(what, to_virtual_mic=b.get("virtual_mic", False))
                else:
                    core.mirror.stop(key)
            core.ui_event("state", core.state())
            return j({"ok": True})
        return await guard(go)
    r.add_post("/api/audio", audio)

    async def call(req):
        b = await req.json()
        async def go():
            if b["op"] == "answer_pc":
                await core.answer_call_on_pc()
                return j({"ok": True})
            if b["op"] == "end":
                core.end_call_audio()
            return j(await core.phone.post_json("/api/call", {"op": b["op"]}))
        return await guard(go)
    r.add_post("/api/call", call)

    async def controls_get(req):
        from . import controls
        if not core.adbm.serial:                  # debug-free: the phone app answers (special permissions)
            return await guard(lambda: _json(core.phone.get_json("/api/controls", timeout=5)))
        st = await controls.read_all(core.adbm.serial)
        try:
            st["torch"] = (await core.phone.get_json("/api/torch", timeout=3)).get("on", False)
        except Exception:
            st["torch"] = None
        return j(st)
    r.add_get("/api/controls", controls_get)

    async def controls_set(req):
        from . import controls
        b = await req.json()
        s = core.adbm.serial
        if not s:
            return await guard(lambda: _json(core.phone.post_json("/api/controls", b, timeout=8)))
        if "toggle" in b:
            if b["toggle"] == "torch":
                await core.phone.post_json("/api/torch", {"on": bool(b["on"])})
            else:
                await controls.set_toggle(s, b["toggle"], bool(b["on"]))
        if "brightness" in b:
            await controls.set_brightness(s, b["brightness"])
        if "volume" in b:
            await controls.set_volume(s, b["volume"], b["value"])
        return j({"ok": True})
    r.add_post("/api/controls", controls_set)

    async def vcam(req):
        b = await req.json() if req.method == "POST" else {}
        if b.get("facing") in ("back", "front"):
            await core.camfeed.set_facing(b["facing"])
        opts = {}
        if "camera" in b:
            opts["vcam_camera_id"] = str(b["camera"] or "")
            cam = next((c for c in core.camfeed.cameras if c.get("id") == opts["vcam_camera_id"]), None)
            if cam and cam.get("facing") in ("back", "front"):
                core.camfeed.facing = cam["facing"]
                settings["vcam_facing"] = cam["facing"]
        if "mirror" in b:
            opts["vcam_mirror"] = bool(b["mirror"])
        if "rotate" in b:
            opts["vcam_rotate"] = int(b["rotate"]) % 360 // 90 * 90
        if "autorotate" in b:
            opts["vcam_autorotate"] = bool(b["autorotate"])
        if opts:
            await core.camfeed.apply(**opts)
        if req.method == "GET" or not core.camfeed.cameras:
            if core.phone.connected:
                await core.camfeed.list_cameras()
        core.ui_event("state", core.state())
        return j({"ok": True, **core.camfeed.status()})
    r.add_post("/api/vcam", vcam)
    r.add_get("/api/vcam", vcam)

    async def sidecar(req):
        b = await req.json() if req.can_read_body else {}
        op = b.get("op", "status")
        if op == "start":
            orient = b.get("orientation") or settings["sidecar_orientation"]
            scale = b.get("scale") or (None if settings["sidecar_size_auto"] else settings["sidecar_size"])
            settings.update(sidecar_orientation=orient)
            res = await core.sidecar.start(core, orientation=orient, scale=scale)
            core.ui_event("state", core.state())
            return j(res)
        if op == "stop":
            await core.sidecar.stop()
            core.ui_event("state", core.state())
        elif op == "scale":
            size = int(b.get("scale") or 0) or None             # 0: automatic
            res = await core.sidecar.set_scale(size)
            core.ui_event("state", core.state())
            return j(res)
        return j(core.sidecar.status())
    r.add_post("/api/sidecar", sidecar)
    r.add_get("/api/sidecar", sidecar)

    async def drive_open(req):
        core.drive.open()
        return j({"ok": core.drive.mounted})
    r.add_post("/api/drive/open", drive_open)

    async def open_path(req):
        b = await req.json()
        p = b.get("path") or settings[b.get("setting", "downloads_dir")]
        if not str(p).startswith("ms-settings:"):
            Path(p).mkdir(parents=True, exist_ok=True)
        elif not desktop.IS_WIN:
            return j({"ok": False})
        desktop.open_path(p)
        return j({"ok": True})
    r.add_post("/api/open", open_path)

    async def show(req):
        from . import gui
        gui.show_window()
        return j({"ok": True})
    r.add_post("/api/show", show)

    async def transfers(req):
        return j({"items": core.transfers.active()})
    r.add_get("/api/transfers", transfers)

    return app


async def _json(coro):
    return web.json_response(await coro)


async def _ok(coro):
    await coro
    return web.json_response({"ok": True})


async def _bytes(coro, ctype):
    return web.Response(body=await coro, content_type=ctype)


async def start(core):
    app = build_app(core)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    try:
        await web.TCPSite(runner, "0.0.0.0", UI_PORT).start()       # LAN: /peer/* only (see local_only)
    except OSError:
        await web.TCPSite(runner, "127.0.0.1", UI_PORT).start()
    log.info("UI on http://127.0.0.1:%d", UI_PORT)
    return runner
