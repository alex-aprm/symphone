"""Desktop shell: WebView2 window, tray icon, dialogs."""
import logging
import threading

import webview
import pystray
from PIL import Image

from .config import UI_PORT, UI_DIR, CACHE_DIR, APP_NAME

log = logging.getLogger("gui")
_window: webview.Window | None = None
_tray: pystray.Icon | None = None
_quitting = False
mica_on = False         # the main window shows the Windows Mica material (the page then draws translucent)


def show_window():
    if _window:
        _window.show()
        _window.restore()
        try:
            _window.on_top = True
            _window.on_top = False
        except Exception:
            pass


def pick(kind="files"):
    if not _window:
        return []
    ft = getattr(webview, "FileDialog", None)
    if kind == "folder":
        mode = ft.FOLDER if ft else webview.FOLDER_DIALOG
        res = _window.create_file_dialog(mode)
    else:
        mode = ft.OPEN if ft else webview.OPEN_DIALOG
        res = _window.create_file_dialog(mode, allow_multiple=True)
    return list(res or [])


def run(core_actions: dict, start_hidden: bool):
    """Blocks on the GUI loop (must be the main thread)."""
    global _window, _tray

    def on_closing():
        if _quitting:
            return True
        _window.hide()
        return False

    from .winutil import theme, backdrop
    dark = theme()["dark"]
    _window = webview.create_window(APP_NAME, f"http://127.0.0.1:{UI_PORT}/", width=1320, height=860,
                                    min_size=(920, 620), background_color="#202020" if dark else "#f3f3f3",
                                    hidden=start_hidden, text_select=True)
    _window.events.closing += on_closing

    def frame_theme():
        """Window frame follows Windows: app mode, and Transparency effects -> Mica behind the whole window (title
        bar and sidebar see-through, like Settings); otherwise a title bar in the window's background color.
        Re-checked whenever the window is shown."""
        global mica_on
        try:
            t = theme()
            form = _window.native
            solid = "#202020" if t["dark"] else "#f3f3f3"
            on = backdrop(int(form.Handle.ToInt64()), t["dark"], t["transparency"], solid)

            def ui():
                from System.Drawing import Color, ColorTranslator
                bg = Color.Black if on else ColorTranslator.FromHtml(solid)   # black = "glass" in the extended frame
                form.BackColor = bg
                wv = getattr(getattr(form, "browser", None), "webview", None)
                if wv is not None:
                    wv.DefaultBackgroundColor = Color.Transparent if on else bg
            from System import Action
            form.Invoke(Action(ui))
            if on != mica_on:
                mica_on = on
                _window.evaluate_js("window.applyTheme && applyTheme()")
        except Exception as e:
            log.info("window backdrop: %s", e)
    _window.events.shown += frame_theme
    _window.events.restored += frame_theme

    def act(name):
        return lambda icon, item: core_actions[name]()

    def quit_app(icon, item):
        global _quitting
        _quitting = True
        core_actions["quit"]()
        icon.stop()
        _window.destroy()

    menu = pystray.Menu(
        pystray.MenuItem("Open Symphone", lambda i, it: show_window(), default=True),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Phone screen\tCtrl+Alt+P", act("phone")),
        pystray.MenuItem("Main display", act("mirror")),
        pystray.MenuItem("Unlock phone\tCtrl+Alt+U", act("unlock")),
        pystray.MenuItem("Lock phone\tCtrl+Alt+L", act("lock")),
        pystray.MenuItem("Camera", act("camera")),
        pystray.MenuItem("Open phone in Explorer", act("drive")),
        pystray.MenuItem("Send clipboard to phone", act("clipboard")),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit", quit_app),
    )
    _tray = pystray.Icon(APP_NAME, Image.open(UI_DIR / "icon.png"), APP_NAME, menu)
    threading.Thread(target=_tray.run, daemon=True, name="tray").start()

    webview.start(gui="edgechromium", private_mode=False, storage_path=str(CACHE_DIR / "webview"))
