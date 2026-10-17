"""Symphone desktop app entry point."""
import asyncio
import json
import logging
import logging.handlers
import sys
import threading
import time
import urllib.request

from .config import UI_PORT, LOG_DIR, settings


def _post_running(path, body=None) -> bool:
    """Talks to an already-running instance (single instance + the file manager's 'Send to')."""
    if sys.platform == "win32":
        import ctypes
        ctypes.windll.user32.AllowSetForegroundWindow(-1)     # ASFW_ANY: let it come to the front for us
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{UI_PORT}{path}", data=json.dumps(body or {}).encode(),
                                     headers={"Content-Type": "application/json", "X-PhoneSync": "1"}, method="POST")
        urllib.request.urlopen(req, timeout=2 if path == "/api/show" else 3600).read()
        return True
    except Exception:
        return False


_instance_lock = None


def _single_instance() -> bool:
    """True if this is the only running instance (held until exit). A second instance must never start its own core:
    on Windows its 127.0.0.1 fallback bind succeeds next to the first one's 0.0.0.0 and silently takes the UI traffic."""
    global _instance_lock
    if sys.platform == "win32":
        import ctypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateMutexW.restype = ctypes.c_void_p
        _instance_lock = k32.CreateMutexW(None, False, "Local\\Symphone.instance")
        return ctypes.get_last_error() != 183          # ERROR_ALREADY_EXISTS
    import fcntl
    _instance_lock = open(LOG_DIR / ".instance.lock", "w")
    try:
        fcntl.flock(_instance_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def _send_paths(args):
    out, skip = [], False
    for a in args:
        if skip:
            skip = False
            continue
        if a == "--dev":
            skip = True
        elif not a.startswith("--"):
            out.append(a)
    return out


def main():
    args = sys.argv[1:]
    dev = args[args.index("--dev") + 1] if "--dev" in args and args.index("--dev") + 1 < len(args) else None
    if "--cleanup" in args:                 # uninstaller: remove what the app added to the system
        from . import desktop
        desktop.cleanup()
        return
    url = next((a for a in args if a.lower().startswith("phonesync:")), None)
    if url:
        # Toast body click (also from Action Center): open that notification in the running instance.
        from urllib.parse import urlparse, parse_qs
        q = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
        if q.get("key") and _post_running("/api/notif/open", {"key": q["key"], "dev": q.get("dev")}):
            return
        if _post_running("/api/show"):
            return
    elif "--send" in args:
        paths = _send_paths(args)
        if _post_running("/api/send", {"paths": paths, "dev": dev}):
            return
        # Not running: start normally, then send once connected
    elif _post_running("/api/show"):
        return
    if not _single_instance():
        # Running but busy or still starting: keep asking it to show itself instead of starting a second core
        for _ in range(20):
            if _post_running("/api/show"):
                break
            time.sleep(1)
        return

    handler = logging.handlers.RotatingFileHandler(LOG_DIR / "phonesync.log", maxBytes=5 << 20, backupCount=3, encoding="utf-8")
    logging.basicConfig(level=logging.INFO, handlers=[handler, logging.StreamHandler()],
                        format="%(asctime)s %(levelname).1s %(name)s: %(message)s")

    from .core import Core
    from . import server, gui, desktop

    core = Core()
    loop = asyncio.new_event_loop()
    ready = threading.Event()

    async def boot():
        await server.start(core)
        await core.start()
        ready.set()
        if "--send" in args:
            for _ in range(60):
                p = core.hub.phones.get(dev) if dev else core.hub.active
                if p and p.connected:
                    break
                await asyncio.sleep(0.5)
            await core.send_files(_send_paths(args), dev_id=dev)

    def run_loop():
        asyncio.set_event_loop(loop)
        loop.create_task(boot())
        loop.run_forever()

    threading.Thread(target=run_loop, daemon=True, name="core").start()
    ready.wait(15)

    def call(coro_fn):
        return lambda: asyncio.run_coroutine_threadsafe(coro_fn(), loop)

    async def _noop():
        pass

    actions = {
        "mirror": call(lambda: core.mirror.screen(unlock=True)),
        "phone": call(core.mirror.phone),
        "lock": call(core.lock),
        "unlock": call(core.unlock),
        "camera": call(lambda: core.mirror.camera("back")),
        "drive": lambda: core.drive.open(),
        "clipboard": call(core.push_clipboard),
        "quit": lambda: asyncio.run_coroutine_threadsafe(core.shutdown(), loop).result(10),
    }
    desktop.start_hotkeys(actions)

    def integrate():
        try:
            desktop.install_shell_integration(core.hub)
            desktop.set_autostart(bool(settings["autostart"]))
        except Exception as e:
            logging.warning("shell integration: %s", e)
    threading.Thread(target=integrate, daemon=True).start()

    gui.run(actions, start_hidden="--minimized" in args)


if __name__ == "__main__":
    main()
