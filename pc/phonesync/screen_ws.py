"""WebSocket bridge between scrcpy sessions (scrcpy_client) and the in-app viewer (ui/screen.html).

Route: GET /ws/screen?kind=screen|app|camera
        kind=screen: [&mode=auto|main|virtual]  auto = main display when the phone is unlocked with the panel on,
                     otherwise a virtual "phone desktop" display (works locked / screen off, panel stays off)
        kind=app:    &pkg=<package>  (app started on its own virtual display)
        kind=camera: [&facing=back|front][&ar=4:3][&camera_fps=30]
        common:      [&max_size=1920][&bitrate=8M][&fps=60][&codec_options=..][&wid=<window id for resizes>]
                     [&vd=WxH]  virtual displays: initial size in pixels; the display then follows the window
                     (flex display), so the picture always fills it exactly
  server -> client  text  {"type":"meta", codec, width, height, device, kind, display, flex}
                    text  {"type":"size", width, height} | {"type":"clipboard", text} | {"type":"error", message}
                    text  {"type":"content", shown}   app windows: false while the app isn't on its display (picked
                          up on the phone / closed); no frames arrive then, so the last one stays (blurred)
                    binary  u8 flags (1=config, 2=keyframe) | f64 BE PC receive time (ms since epoch) |
                            u64 BE pts (µs) | Annex B payload
  client -> server  text JSON, coordinates normalized to [0,1] of the video frame:
                    {t:"touch", a:0|1|2, id, x, y, p}           a = Android MotionEvent action (down/up/move)
                    {t:"scroll", x, y, h, v}                    h/v in wheel notches
                    {t:"pinch", ph:"begin"|"update"|"end", x, y, s}   s = multiplicative scale since last update
                    {t:"pan", ph:"begin"|"update"|"end", x, y, dx, dy}
                    {t:"key", k:"BACK"|<int>, a:"press"|"down"|"up", meta, repeat}
                    {t:"text", s} | {t:"clip", s, paste}
                    {t:"cmd", c:"rotate"[, r]|"notifications"|"settings"|"collapse"|"reset"|"torch", on|
                                "zoom_in"|"zoom_out"|"start_app", pkg}
                    {t:"win", w, h, iw, ih}                     resize the hosting window's content to w x h
                    {t:"vd", w, h}                              resize a flex virtual display (CSS px)
                    {t:"pin", on}                               keep the hosting window always on top (remembered)
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import struct
import time
from urllib.parse import urlencode

from aiohttp import web, WSMsgType

from . import scrcpy_client as sc
from .config import IS_WIN, UI_PORT, settings
from .pckeyboard import PcKeyboard

log = logging.getLogger("screen")
ROUTE = "/ws/screen"
MAX_QUEUED = 45            # ~0.75 s of video: beyond that drop and ask for a keyframe
# MediaCodec low-latency hints (scrcpy syntax key[:type]=value); "" disables
DEFAULT_CODEC_OPTIONS = ""

sessions: set[sc.ScrcpySession] = set()
_windows: dict[str, object] = {}
_home_cache: dict[str, str] = {}
kbd: PcKeyboard | None = None
DP_PER_CSS = 0.9        # app windows: ~0.9 dp per CSS px of window, i.e. a phone-sized window shows a phone layout
_density: dict[str, int] = {}


async def phone_density(serial: str) -> int:
    """The phone's own density: virtual displays use it too (some apps, e.g. Tesla, lay out from the phone's screen
    metrics and break on a display with another density)."""
    if serial not in _density:
        rc, out = await sc.adb("shell", "wm density", serial=serial, timeout=6)
        m = re.findall(r"density: (\d+)", out)
        _density[serial] = int(m[-1]) if m else 420        # an override ("Override density") wins: it is last
    return _density[serial]


VD_MAX = 2400           # long side of a flex display (it is also the video size: scrcpy clips flex displays to max_size)


def vd_pixels(css_w: float, css_h: float, density: int) -> tuple[int, int]:
    k = min(DP_PER_CSS * density / 160, VD_MAX / max(css_w, css_h, 1))      # big windows: a little more space instead
    return max(200, int(css_w * k)) // 8 * 8, max(200, int(css_h * k)) // 8 * 8
_GATE = "content_gate" in sc.ScrcpySession.__init__.__code__.co_varnames


def attach(app: web.Application, core) -> None:
    """Adds the viewer websocket. `core.adbm.serial` must give the adb serial to use."""
    global kbd
    kbd = PcKeyboard(core)
    async def handler(req: web.Request):
        return await screen_socket(req, core)
    app.router.add_get(ROUTE, handler)

    async def _cleanup(_app):
        await shutdown()
    app.on_shutdown.append(_cleanup)


async def shutdown():
    await asyncio.gather(*(s.stop() for s in list(sessions)), return_exceptions=True)
    if kbd and kbd._serial:
        try:
            await asyncio.wait_for(kbd.release(), 5)
        except Exception:
            pass


def _vd_param(q, density: int) -> str:
    """'WxH/dpi' for a new flex display from the viewer's ?vd=WxH (CSS px of its picture area), or '' (phone-sized)."""
    m = re.fullmatch(r"(\d{2,4})x(\d{2,4})", q.get("vd", ""))
    if not m:
        return ""
    w, h = vd_pixels(int(m.group(1)), int(m.group(2)), density)
    return f"{w}x{h}/{density}"


async def _phone_state(serial: str) -> tuple[bool, bool]:
    """(physical panel on, locked). Global mWakefulness is not used: an open virtual display keeps it Awake."""
    rc, out = await sc.adb("shell", "dumpsys display | grep -m1 mScreenState= ; dumpsys window | grep -m1 -E "
                           "'mDreamingLockscreen|isKeyguardShowing|mShowingLockscreen'", serial=serial)
    import re
    on = "mScreenState=ON" in out
    locked = bool(re.search(r"(mDreamingLockscreen|isKeyguardShowing|mShowingLockscreen)=true", out))
    return on, locked


def wireless(serial: str | None) -> bool:
    """adb over Wi-Fi: '192.168.1.5:5555' or an mDNS 'adb-XXXX._adb-tls-connect._tcp' transport."""
    return bool(serial) and (":" in serial or "._adb-tls-connect" in serial)


def stream_quality(wifi: bool) -> tuple[str, int]:
    """(bitrate, fps) from the settings for the link the video takes."""
    if wifi:
        return settings["mirror_wifi_bitrate"] or "8M", min(int(settings["mirror_wifi_fps"] or 60), 120)
    return settings["mirror_bitrate"] or "8M", min(int(settings["mirror_fps"] or 60), 120)


def _codec(q) -> str:
    """H.265 when the viewer can decode it (it says so with ?codec=h265): on Wi-Fi the same picture for less data.
    Over USB bandwidth is plentiful and H.264 encoders start faster, so only Wi-Fi streams use it."""
    return "h265" if q.get("codec") == "h265" else "h264"


async def session_options(serial: str, q) -> tuple[dict, str]:
    """Maps viewer query params to ScrcpySession kwargs. Returns (kwargs, display description)."""
    kind = q.get("kind", "screen")
    # 1920 default: full-res (2688p) H.264 decode can't sustain 60 fps in WebView2 on every PC
    max_size = int(q.get("max_size") or settings["mirror_max_size"] or 1920)
    bitrate, fps = stream_quality(wireless(serial))
    kw = dict(video_bit_rate=q.get("bitrate") or bitrate, max_fps=int(q.get("fps") or fps),
              max_size=max_size, power_on=False, stay_awake=False, audio=False, control=True,
              video_codec_options=q.get("codec_options") or DEFAULT_CODEC_OPTIONS or None)
    if wireless(serial) and kind != "camera":
        kw["video_codec"] = _codec(q)
    if kind == "camera":
        kw.update(video_source="camera", camera_facing=q.get("facing", "back"), camera_ar=q.get("ar", "4:3"),
                  camera_fps=int(q.get("camera_fps", 30)), max_size=int(q.get("max_size") or 1920),
                  video_bit_rate=q.get("bitrate") or "16M")
        return kw, f"camera ({kw['camera_facing']})"
    if kind == "app":
        pkg = q.get("pkg")
        if not pkg:
            raise ValueError("pkg is required for kind=app")
        vd = _vd_param(q, await phone_density(serial))
        # "local": the keyboard belongs to this display (so the phone's PC keyboard knows the field is in a PC window)
        kw.update(new_display=vd or q.get("new_display", ""), start_app=pkg, vd_system_decorations=True,
                  flex_display=bool(vd), display_ime_policy="local")
        if vd:
            kw["max_size"] = 0                  # the display already has the window's size (see VD_MAX)
        if _GATE:
            kw["content_gate"] = True
        return kw, f"virtual display: {pkg}"
    # whole phone UI
    mode = q.get("mode", "auto")
    if mode == "auto":
        on, locked = await _phone_state(serial)
        mode = "main" if on and not locked else "virtual"
    if mode == "main":
        return kw, "main display"
    # Virtual "desktop": a new display with system decorations. The launcher provides its secondary-display home
    # there by itself (nubia: launcher3 SecondaryDisplayLauncher). Works with the physical panel off and the
    # keyguard up (the keyguard only covers display 0, which is a black secure layer in screenshot capture).
    vd = _vd_param(q, await phone_density(serial))
    kw.update(new_display=vd or q.get("new_display", ""), vd_system_decorations=True, flex_display=bool(vd),
              display_ime_policy="local")
    if vd:
        kw["max_size"] = 0
    if q.get("home") == "1":     # fallback for launchers without a secondary-display home
        home = _home_cache.get(serial) or await sc.home_activity(serial)
        if home:
            _home_cache[serial] = home
            kw["start_app"] = home.split("/")[0]
    return kw, "virtual display (phone desktop)"


async def cast_session(core, q):
    from .castsession import CastSession, phone_host
    phone = core.hub.active
    host = phone_host(phone) if phone is not None and phone.connected else None
    if not host:
        raise sc.ScrcpyError("The phone isn't connected over Wi-Fi")
    kind = q.get("kind", "screen")
    max_size = int(q.get("max_size") or settings["mirror_max_size"] or 1920)
    if kind == "camera":
        sess = CastSession(host, phone.token, "camera", max_size=max_size, fps=30, bitrate=16_000_000,
                           facing=q.get("facing", "back"))
        return sess, f"camera ({q.get('facing', 'back')})"
    bitrate, fps = stream_quality(True)
    sess = CastSession(host, phone.token, "screen", max_size=max_size, fps=fps, bitrate=sc._bitrate(bitrate),
                       codec=_codec(q))
    if kind == "app" and q.get("pkg"):
        sess.start_app = q.get("pkg")
    return sess, "phone screen (casting)"


async def _cast_unlock(core, sess):
    """Debug-free: only the phone's own screen can be shown, so a locked phone would show black. Wake and unlock it
    with the saved PIN (typed once by the phone app, never retried); remembered, so closing the window locks it again."""
    sess.ps_unlocked = False
    if not settings["unlock_on_mirror"] or not settings.get_pin():
        return
    try:
        if not await core.is_locked():
            return
        r = await core.unlock()
        sess.ps_unlocked = bool(r.get("ok"))
    except Exception as e:
        log.info("unlock for casting: %s", e)


async def _cast_done(core, sess, kind: str):
    """Window closed: leave the phone as it was — an app opened from the PC goes away (home screen), and a phone this
    window unlocked is locked again (unless another window still shows it)."""
    try:
        others = [s for s in sessions if getattr(s, "cast", False) and s.kind == "screen" and not s.closed]
        if kind == "app" and not others:
            await sess.send_json({"t": "global", "action": "home"})
            await asyncio.sleep(0.3)
        if getattr(sess, "ps_unlocked", False) and not others:
            await core.lock()
    except Exception as e:
        log.debug("cast done: %s", e)


async def screen_socket(req: web.Request, core) -> web.WebSocketResponse:
    # Websockets bypass CORS: only accept pages served by this very server.
    origin = req.headers.get("Origin", "")
    if origin and origin != f"http://{req.host}":
        return web.Response(status=403)
    ws = web.WebSocketResponse(compress=False, max_msg_size=1 << 20, heartbeat=None)
    await ws.prepare(req)
    serial = core.adbm.serial
    q = req.query
    sess = None
    sender = None
    watcher = None
    try:
        if serial:
            kw, display = await session_options(serial, q)
            sess = sc.ScrcpySession(serial, **kw)
        else:
            # Debug-free: the phone app streams by itself (screen casting / its own camera); no hidden displays,
            # so an app opens on the phone's own screen and the window shows that screen.
            sess, display = await cast_session(core, q)
            kw = {}
        queue: asyncio.Queue = asyncio.Queue()
        state = {"skip_until_key": False}

        def on_packet(p: sc.VideoPacket):
            if queue.qsize() > MAX_QUEUED:
                # the viewer can't keep up: drop the backlog and resync on a fresh keyframe
                while not queue.empty():
                    queue.get_nowait()
                state["skip_until_key"] = True
                asyncio.ensure_future(sess.reset_video())
            if state["skip_until_key"]:
                if not (p.config or p.keyframe):
                    return
                state["skip_until_key"] = False
            flags = (1 if p.config else 0) | (2 if p.keyframe else 0)
            queue.put_nowait(struct.pack(">BdQ", flags, time.time() * 1000, p.pts or 0) + p.data)

        sess.on_packet = on_packet
        sess.on_size = lambda s: queue.put_nowait(json.dumps({"type": "size", "width": s.width, "height": s.height}))
        sess.on_clipboard = lambda t: queue.put_nowait(json.dumps({"type": "clipboard", "text": t}))
        sess.on_close = lambda: queue.put_nowait(None)
        kind = q.get("kind", "screen")
        sess.ps_kind = "app" if kind == "app" else ("desktop" if kw.get("new_display") is not None else kind)
        sess.ps_wid = q.get("wid")
        sess.last_exit_key = 0.0
        sess.key_routes = {}

        def on_content(shown: bool, top: str = ""):
            prev = getattr(sess, "ps_shown", None)       # (the client updates sess.content_shown before calling)
            sess.ps_shown = shown
            if prev is shown or (prev is None and not shown):    # not started yet: nothing to hand over
                return
            if not shown and sess.ps_kind == "app" and time.time() - sess.last_exit_key < 1.5:
                # Back/Home pressed in this window left the app: the window closes like a desktop app's would.
                asyncio.ensure_future(close_window(sess.ps_wid))
            queue.put_nowait(json.dumps({"type": "content", "shown": shown}))
            if kbd:
                kbd.sessions_changed()
        sess.on_content = on_content
        sessions.add(sess)                  # visible to ensure_app_display() as soon as its display exists
        if getattr(sess, "cast", False) and kind in ("screen", "app"):
            await _cast_unlock(core, sess)
        if getattr(sess, "cast", False) or wireless(serial):
            sess.ps_wifi = True
            wifi_streams(core, +1)
        await sess.start()
        await ws.send_str(json.dumps({"type": "meta", "codec": sess.codec, "width": sess.width,
                                      "height": sess.height, "device": sess.device_name,
                                      "kind": kind, "display": display,
                                      "flex": bool(sess.opts.get("flex_display"))}))
        if getattr(sess, "cast", False) and sess.start_app:
            await sess.start_app_cmd(sess.start_app)
        if sess.ps_kind == "app" and sess.virtual_display_id is not None:
            # Opened from the PC on purpose: if the app is already running on the phone's own screen, its task
            # comes here (otherwise Android just brings it up on the phone and this window shows nothing).
            asyncio.ensure_future(pull_task(sess, q.get("pkg"), delay=1.2))
        if kbd and sess.ps_kind in ("app", "desktop"):
            kbd.sessions_changed()

        async def pump():
            while True:
                m = await queue.get()
                if m is None:
                    await ws.send_str(json.dumps({"type": "error", "message": "stream ended",
                                                  "log": sess.server_log[-6:]}))
                    await ws.close()
                    return
                if isinstance(m, bytes):
                    await ws.send_bytes(m)
                else:
                    await ws.send_str(m)
        sender = asyncio.create_task(pump())
        if q.get("kind") == "app" and sess.virtual_display_id is not None:
            watcher = asyncio.create_task(_watch_app(sess, q.get("pkg"), queue))

        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                try:
                    await handle_input(sess, json.loads(msg.data), q.get("wid"))
                except Exception as e:
                    log.warning("input %s: %s", msg.data[:200], e)
            elif msg.type in (WSMsgType.ERROR, WSMsgType.CLOSE):
                break
    except (sc.ScrcpyError, ValueError) as e:
        log.warning("screen session: %s", e)
        if not ws.closed:
            await ws.send_str(json.dumps({"type": "error", "message": str(e)}))
    except (ConnectionError, asyncio.CancelledError):
        pass
    finally:
        if sender:
            sender.cancel()
        if watcher:
            watcher.cancel()
        if sess:
            sessions.discard(sess)
            if getattr(sess, "ps_wifi", False):
                wifi_streams(core, -1)
            if getattr(sess, "cast", False) and q.get("kind", "screen") in ("screen", "app"):
                await _cast_done(core, sess, q.get("kind", "screen"))
            await sess.stop()
            if kbd:
                kbd.sessions_changed()
        if not ws.closed:
            await ws.close()
    return ws


_wifi_count = 0
_wifi_lease: asyncio.Task | None = None


def wifi_streams(core, delta: int):
    """Counts the streams riding on Wi-Fi; while there are any, the phone keeps its Wi-Fi out of power save (a lease
    the phone drops by itself if the PC goes away)."""
    global _wifi_count, _wifi_lease
    _wifi_count = max(0, _wifi_count + delta)
    if _wifi_count and not _wifi_lease:
        _wifi_lease = asyncio.create_task(_lease_wifi(core))
    elif not _wifi_count and _wifi_lease:
        _wifi_lease.cancel()
        _wifi_lease = None
        asyncio.ensure_future(_post_lease(core, 0))


async def _post_lease(core, ttl: int):
    phone = core.hub.active
    if phone is None or not phone.connected:
        return
    try:
        await phone.post_json("/api/stream", {"ttl": ttl}, timeout=5)     # older phone apps: 404, harmless
    except Exception as e:
        log.debug("wifi lease: %s", e)


async def _lease_wifi(core):
    while True:
        await _post_lease(core, 45)
        await asyncio.sleep(20)


_TASK_RE = re.compile(r"taskId=(\d+): ([\w.]+)/\S* .*?visible=(true|false)")
_ROOT_RE = re.compile(r"^RootTask id=(\d+) .*displayId=(\d+)")


async def _tasks(serial: str, secure: bool = False):
    """([(root task id, display id, task id, package, visible)], physical panel on[, {displays showing a window that
    forbids capture (FLAG_SECURE)}])."""
    cmd = "am stack list | grep -E '^RootTask|taskId=' ; dumpsys display | grep -m1 mScreenState="
    if secure:
        cmd += " ; dumpsys window windows | grep -E '^  Window #|mDisplayId=|fl=.* SECURE'"
    rc, out = await sc.adb("shell", cmd, serial=serial, timeout=8)
    tasks, root, disp = [], None, None
    secure_displays, wdisp = set(), None
    for line in out.splitlines():
        if secure:
            ls = line.strip()
            if ls.startswith("Window #"):
                wdisp = None
                continue
            m = re.match(r"mDisplayId=(\d+)", ls)
            if m:
                wdisp = int(m.group(1))
                continue
            if ls.startswith("fl=") and " SECURE" in f" {ls}" and wdisp is not None:
                secure_displays.add(wdisp)
                continue
        m = _ROOT_RE.match(line.strip())
        if m:
            root, disp = int(m.group(1)), int(m.group(2))
            continue
        m = _TASK_RE.search(line)
        if m and root is not None:
            tasks.append((root, disp, int(m.group(1)), m.group(2), m.group(3) == "true"))
    if secure:
        return tasks, "mScreenState=ON" in out, secure_displays
    return tasks, "mScreenState=ON" in out


async def to_phone(sess):
    """Hands this window's app over to the phone's own screen (wakes it) and closes the window."""
    vd = sess.virtual_display_id
    if vd is None:
        return
    sess.to_phone = True
    tasks, _ = await _tasks(sess.serial)
    here = [t for t in tasks if t[1] == vd and "launcher" not in t[3].lower()]
    for root in dict.fromkeys(t[0] for t in here):
        await sc.adb("shell", f"am display move-stack {root} 0", serial=sess.serial)
    await sc.adb("shell", "input keyevent WAKEUP", serial=sess.serial)
    await close_window(getattr(sess, "ps_wid", None))


async def pull_task(sess, pkg: str, delay: float = 0.0):
    """Moves `pkg`'s task onto this session's virtual display if it lives on another one (the phone's screen)."""
    if delay:
        await asyncio.sleep(delay)
    vd = sess.virtual_display_id
    if vd is None or not pkg:
        return
    try:
        tasks, _ = await _tasks(sess.serial)
        if any(t[3] == pkg and t[1] == vd for t in tasks):
            return
        away = [t for t in tasks if t[3] == pkg and t[1] != vd]
        if away:
            log.info("moving %s here from display %s", pkg, away[0][1])
            await sc.adb("shell", f"am display move-stack {away[0][0]} {vd}", serial=sess.serial)
        else:
            await sess.start_app_cmd(pkg)
    except Exception as e:
        log.debug("pull %s: %s", pkg, e)


async def _watch_app(sess, pkg: str, queue: asyncio.Queue):
    """Graceful hand-over: when the app is picked up on the phone itself (Android moves its task to the phone's
    screen) the PC window blurs its last frame; once the phone is put down (panel off) or the app is left there,
    the task is moved back to this window's virtual display and the view sharpens again."""
    vd = sess.virtual_display_id
    away_since = None
    last_start = 0.0
    await asyncio.sleep(1.5)                        # let the app start on the virtual display
    while True:
        try:
            tasks, panel_on, secure = await _tasks(sess.serial, secure=True)
            # Apps that forbid capture (FLAG_SECURE: banking, government ids…) are blacked out by Android itself for
            # anything but the phone's own screen; say so instead of showing a black window.
            is_secure = vd in secure
            if is_secure != getattr(sess, "secure", False):
                sess.secure = is_secure
                queue.put_nowait(json.dumps({"type": "secure", "on": is_secure}))
            if getattr(sess, "to_phone", False):
                await asyncio.sleep(1.5)
                continue
            mine = [t for t in tasks if t[3] == pkg]
            # Anything real on our display counts (a notification may have opened another app's activity here);
            # the launcher's secondary-display home doesn't.
            others_here = [t for t in tasks if t[1] == vd and t[3] != pkg and t[4] and "launcher" not in t[3].lower()]
            here = any(t[1] == vd for t in mine) or bool(others_here)
            pkgs = {t[3] for t in tasks if t[1] == vd and "launcher" not in t[3].lower()} | {pkg}
            if pkgs != getattr(sess, "vd_pkgs", None):
                sess.vd_pkgs = pkgs
                if kbd:
                    kbd.sessions_changed()
            gated = getattr(sess, "ps_shown", None) is not None     # the server reports it, instantly
            if here:
                if away_since is not None:
                    away_since = None
                    if not gated:
                        queue.put_nowait(json.dumps({"type": "back"}))
                        await sess.reset_video()
            else:
                if away_since is None:
                    away_since = time.time()
                    if not gated:
                        queue.put_nowait(json.dumps({"type": "away"}))
                on_phone = [t for t in mine if t[1] != vd]
                visible_there = any(t[4] for t in on_phone)
                if time.time() - sess.last_exit_key < 3:
                    pass                                # left on purpose from this window (it is closing)
                elif not panel_on or not visible_there:
                    # The phone was put down, or the app went to the background there: bring it back here.
                    if on_phone:
                        await sc.adb("shell", f"am display move-stack {on_phone[0][0]} {vd}", serial=sess.serial)
                    elif time.time() - away_since > 2 and time.time() - last_start > 15:
                        last_start = time.time()
                        await sess.start_app_cmd(pkg)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.debug("watch %s: %s", pkg, e)
        await asyncio.sleep(1.5)


async def ensure_app_display(pkg: str, label: str = "", timeout: float = 10) -> int | None:
    """Opens (or reuses) the PC window running `pkg` on its own virtual display; returns that display's id."""
    def find():
        for s in sessions:
            if s.start_app == pkg and s.virtual_display_id is not None:
                return s.virtual_display_id
        return None
    d = find()
    if d is not None:
        await open_window("app", pkg=pkg, label=label or pkg)     # focus it
        s = next((s for s in sessions if s.start_app == pkg and s.virtual_display_id == d), None)
        if s is not None:
            await pull_task(s, pkg)
        return d
    t0 = time.time()
    await open_window("app", pkg=pkg, label=label or pkg)
    while time.time() - t0 < timeout:
        await asyncio.sleep(0.2)
        d = find()
        if d is not None:
            await asyncio.sleep(0.6)              # the launcher activity starts first; the notification target after
            return d
    log.info("no display for %s after %.0fs", pkg, timeout)
    return None


async def handle_input(s: sc.ScrcpySession, m: dict, wid: str | None = None):
    t = m.get("t")
    W, H = s.width, s.height

    def xy():
        return float(m.get("x", 0.5)) * W, float(m.get("y", 0.5)) * H

    if t == "touch":
        x, y = xy()
        a = int(m["a"])
        await s.touch(a, int(m.get("id", sc.POINTER_ID_GENERIC_FINGER)), x, y,
                      pressure=0.0 if a == sc.ACTION_UP else float(m.get("p", 1.0)))
    elif t == "scroll":
        x, y = xy()
        await s.scroll(x, y, float(m.get("h", 0)), float(m.get("v", 0)))
    elif t == "pinch":
        ph = m.get("ph")
        if ph == "begin":
            x, y = xy()
            await s.pinch_begin(x, y)
            if m.get("s"):
                await s.pinch_update(rel_scale=float(m["s"]))
        elif ph == "update":
            await s.pinch_update(rel_scale=float(m.get("s", 1)), dx=float(m.get("dx", 0)) * W,
                                 dy=float(m.get("dy", 0)) * H)
        else:
            await s.pinch_end()
    elif t == "pan":
        ph = m.get("ph")
        if ph == "begin":
            x, y = xy()
            await s.pan_begin(x, y)
        elif ph == "update":
            await s.pan_update(float(m.get("dx", 0)) * W, float(m.get("dy", 0)) * H)
        else:
            await s.pan_end()
    elif t == "key":
        k = m["k"]
        kc = sc.KEYCODES[k.upper()] if isinstance(k, str) else int(k)
        meta = int(m.get("meta", 0))
        a = m.get("a", "press")
        if kc in (sc.KEYCODES["BACK"], sc.KEYCODES["HOME"]) and a in ("press", "down"):
            s.last_exit_key = time.time()
        if _typing(s) and (not isinstance(k, str) or k.upper() in _IME_KEYS):
            routes = s.key_routes
            if a in ("press", "down"):
                ok = await kbd.input({"key": kc, "meta": meta, "action": "tap" if a == "press" else "down",
                                      "repeat": int(m.get("repeat", 0))})
                routes[kc] = ok
                if ok:
                    return
            elif routes.pop(kc, False):
                await kbd.input({"key": kc, "meta": meta, "action": "up"})
                return
        if a in ("press", "down"):
            await s.keycode(sc.ACTION_DOWN, kc, int(m.get("repeat", 0)), meta)
        if a in ("press", "up"):
            await s.keycode(sc.ACTION_UP, kc, 0, meta)
    elif t == "text":
        text = str(m.get("s", ""))
        if not (_typing(s) and await kbd.input({"text": text})):
            await s.text(text)
    elif t == "clip":
        text = str(m.get("s", ""))
        if not (m.get("paste") and _typing(s) and await kbd.input({"paste": text})):
            await s.set_clipboard(text, paste=bool(m.get("paste")))
    elif t == "focus":
        s.ps_focused = bool(m.get("on"))
        if kbd and _kbd_window(s):
            kbd.sessions_changed()
            if s.ps_focused:
                asyncio.ensure_future(kbd.ensure())
    elif t == "vd":
        if s.opts.get("flex_display"):
            await s.resize_display(*vd_pixels(float(m["w"]), float(m["h"]), await phone_density(s.serial)))
    elif t == "cmd":
        c = m.get("c")
        fn = {"notifications": s.expand_notifications, "settings": s.expand_settings,
              "collapse": s.collapse_panels, "reset": s.reset_video}.get(c)
        if fn:
            await fn()
        elif c == "rotate":
            await rotate(s, m.get("r"))
        elif c == "torch":
            await s.camera_torch(bool(m.get("on")))
        elif c in ("zoom_in", "zoom_out"):
            await s.camera_zoom(c == "zoom_in")
        elif c == "start_app" and m.get("pkg"):
            await s.start_app_cmd(str(m["pkg"]))
        elif c == "to_phone":
            await to_phone(s)
        elif c == "pull" and getattr(s, "ps_kind", "") == "app":
            s.to_phone = False
            await pull_task(s, s.start_app)


    elif t == "win":
        await resize_window(wid, int(m["w"]), int(m["h"]), int(m.get("iw", 0)), int(m.get("ih", 0)))
    elif t == "pin":
        await pin_window(wid, bool(m.get("on")))


_IME_KEYS = {"ENTER", "DEL", "FORWARD_DEL", "TAB", "DPAD_UP", "DPAD_DOWN", "DPAD_LEFT", "DPAD_RIGHT", "MOVE_HOME",
             "MOVE_END", "PAGE_UP", "PAGE_DOWN", "INSERT", "SPACE"}


def _typing(s) -> bool:
    """Keys of this window go through the phone's PC keyboard (app windows, the phone desktop, and the phone's own
    screen over adb: the phone's on-screen keyboard stays down while the PC types)."""
    return kbd is not None and kbd.enabled and _kbd_window(s)


def _kbd_window(s) -> bool:
    kind = getattr(s, "ps_kind", "")
    return kind in ("app", "desktop") or kind == "screen" and not getattr(s, "cast", False)


async def rotate(s: sc.ScrcpySession, r=None):
    """Rotates the streamed content by 90° (or to rotation r in 0..3).

    Virtual displays (app windows, phone-desktop mode): the display itself is rotated with
    `cmd window user-rotation -d <id> lock <r>`, so apps relayout in landscape; all four rotations work and nothing
    persists (the display dies with the session). Main display: scrcpy's ROTATE_DEVICE (portrait <-> landscape,
    keeps the user's auto-rotate setting)."""
    if s.virtual_display_id is None:
        await s.rotate()
        return
    cur = getattr(s, "rotation", 0)
    new = (cur + 1) % 4 if r is None else int(r) % 4
    rc, out = await sc.adb("shell", f"cmd window user-rotation -d {s.virtual_display_id} lock {new}", serial=s.serial)
    if rc == 0:
        s.rotation = new
    else:
        log.warning("rotate: %s", out)


async def pin_window(wid: str | None, on: bool):
    """Always on top for a viewer window; remembered, so the window reopens pinned."""
    pinned = [k for k in (settings["pinned_windows"] or []) if k != wid]
    if on and wid:
        pinned.append(wid)
    settings["pinned_windows"] = pinned
    win = _windows.get(wid or "")
    if win is not None:
        try:
            from . import gui
            await asyncio.to_thread(gui.set_topmost, win, on)
        except Exception as e:
            log.debug("pin: %s", e)


async def resize_window(wid: str | None, w: int, h: int, cur_iw: int = 0, cur_ih: int = 0):
    """Viewer asks for its content area to become w x h CSS px (e.g. after a rotation). pywebview sizes are the
    outer window size in the same logical units, so the current frame size (outer - inner) is added."""
    win = _windows.get(wid or "")
    if win is None or w < 100 or h < 100:
        return
    try:
        fw = max(0, int(win.width) - cur_iw) if cur_iw else 16
        fh = max(0, int(win.height) - cur_ih) if cur_ih else 39
        await asyncio.to_thread(win.resize, w + fw, h + fh)
    except Exception as e:
        log.debug("resize: %s", e)


# ---------------------------------------------------------------------- windows
async def close_window(wid: str | None):
    win = _windows.pop(wid or "", None)
    if win is not None:
        try:
            await asyncio.to_thread(win.destroy)
        except Exception as e:
            log.debug("close: %s", e)


def viewer_url(kind: str = "screen", port: int = UI_PORT, **params) -> str:
    q = {"kind": kind, **{k: v for k, v in params.items() if v is not None and v != ""}}   # may include wid=
    return f"http://127.0.0.1:{port}/ui/screen.html?{urlencode(q)}"


def _fit_work_area(width: int, height: int) -> tuple[int, int]:
    """Scales a default window size (logical px) down to fit the primary monitor's work area."""
    try:
        import ctypes
        from ctypes import wintypes
        r = wintypes.RECT()
        ctypes.windll.user32.SystemParametersInfoW(0x30, 0, ctypes.byref(r), 0)       # SPI_GETWORKAREA
        scale = ctypes.windll.user32.GetDpiForSystem() / 96
        aw, ah = (r.right - r.left) / scale, (r.bottom - r.top) / scale
        f = min(1.0, ah * 0.88 / height, aw * 0.9 / width)
        return round(width * f), round(height * f)
    except Exception:
        return width, height


async def open_window(kind: str = "screen", *, port: int = UI_PORT, **params):
    """Opens (or focuses) a pywebview window with the viewer. kind: screen | app (pkg=, label=) | camera (facing=).

    Must be called after webview.start() (i.e. from the running app); safe from any thread / event loop."""
    import webview
    key = f"{kind}:{params.get('pkg') or params.get('facing') or ''}"
    w = _windows.get(key)
    if w is not None:
        try:
            await asyncio.to_thread(w.restore)
            await asyncio.to_thread(w.show)
            await asyncio.to_thread(_to_front, w.title)
            return w
        except Exception:
            _windows.pop(key, None)
    name = settings.phone_name()
    if kind == "app":
        title = f"{params.get('label') or params.get('pkg')} · {name}"
        width, height = 420, 900
    elif kind == "camera":
        title = f"Camera ({params.get('facing', 'back')}) · {name}"
        width, height = 1000, 750
    else:
        title = name
        width, height = 420, 900          # ~9:20 phone + a little chrome

    width, height = _fit_work_area(width, height)

    pinned = key in (settings["pinned_windows"] or [])

    def create():
        win = webview.create_window(title, viewer_url(kind, port, wid=key, pinned="1" if pinned else "", **params),
                                    on_top=pinned, width=width, height=height,
                                    min_size=(220, 300), background_color="#0b0f14", text_select=False)
        win.events.closed += lambda: _windows.pop(key, None)
        return win
    w = await asyncio.to_thread(create)
    _windows[key] = w
    _start_pin_keeper()
    # opened from a notification click / hotkey: the new window should be the active one, not behind the others
    asyncio.get_running_loop().run_in_executor(None, _to_front, title, 5.0)
    return w


_pin_keeper: asyncio.Task | None = None


def _start_pin_keeper():
    global _pin_keeper
    if IS_WIN and (_pin_keeper is None or _pin_keeper.done()):
        _pin_keeper = asyncio.get_running_loop().create_task(_keep_pinned())


async def _keep_pinned():
    """Pinned windows stay on top: if anything clears always-on-top, it is put back (the user shouldn't have to
    pin a window again)."""
    from . import winutil
    while _windows:
        await asyncio.sleep(2)
        for key in list(settings["pinned_windows"] or []):
            w = _windows.get(key)
            if w is None:
                continue
            try:
                if await asyncio.to_thread(winutil.keep_topmost, w.title):
                    log.info("window %s: always on top restored", key)
            except Exception as e:
                log.debug("keep pinned: %s", e)


def _to_front(title: str, wait: float = 0.0):
    """Activates this app's window `title` (waits up to `wait` s for it to appear). Windows only."""
    if not IS_WIN:
        return
    from . import winutil
    end = time.time() + wait
    while True:
        try:
            if winutil.bring_to_front(title):
                return
        except Exception as e:
            log.debug("to front: %s", e)
            return
        if time.time() >= end:
            return
        time.sleep(0.15)
