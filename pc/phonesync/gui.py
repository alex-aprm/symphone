"""Desktop shell: the app window (WebView2 on Windows, WebKit on macOS, Qt WebEngine on Linux), tray / menu bar icon,
dialogs."""
import logging
import threading

import webview
import pystray
from PIL import Image

from .config import UI_PORT, UI_DIR, CACHE_DIR, APP_NAME, IS_WIN, IS_MAC, IS_LINUX

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

    from .desktop import theme, FILE_MANAGER
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
        if not IS_WIN:
            return
        from .winutil import backdrop
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

    def quit_app():
        global _quitting
        _quitting = True

        def go():
            core_actions["quit"]()
            if _tray is not None:
                _tray.stop()
            _window.destroy()
        # macOS / Linux (Qt): menu callbacks run on the GUI thread, and shutting down waits on window calls to it
        threading.Thread(target=go, daemon=True).start() if not IS_WIN else go()

    keys = (lambda k: f"\tCtrl+Alt+{k}") if not IS_MAC else (lambda k: "")     # no global shortcuts on macOS
    items = [
        ("Open Symphone", show_window), None,
        ("Phone screen" + keys("P"), core_actions["phone"]),
        ("Main display", core_actions["mirror"]),
        ("Unlock phone" + keys("U"), core_actions["unlock"]),
        ("Lock phone" + keys("L"), core_actions["lock"]),
        ("Camera", core_actions["camera"]),
        (f"Open phone in {FILE_MANAGER}", core_actions["drive"]),
        ("Send clipboard to phone", core_actions["clipboard"]), None,
        ("Quit", quit_app),
    ]
    if IS_LINUX and _qt_tray(items):
        _tray = None                    # Qt's own tray icon (StatusNotifierItem), on the GUI thread
    else:
        menu = pystray.Menu(*[pystray.Menu.SEPARATOR if it is None else
                              pystray.MenuItem(it[0], (lambda fn: lambda i, item: fn())(it[1]), default=n == 0)
                              for n, it in enumerate(items)])
        _tray = pystray.Icon(APP_NAME, Image.open(UI_DIR / "icon.png"), APP_NAME, menu)
        if IS_MAC:
            # AppKit: the menu bar item lives on the main thread, in the run loop pywebview runs
            _tray.run_detached()
        else:
            threading.Thread(target=_tray.run, daemon=True, name="tray").start()

    # Linux: Qt WebEngine (bundled with the app)
    backend = "edgechromium" if IS_WIN else "cocoa" if IS_MAC else "qt"
    webview.start(gui=backend, private_mode=False, storage_path=str(CACHE_DIR / "webview"))


_qt_refs: list = []


def _qt_tray(items) -> bool:
    """Linux: a tray icon through Qt (the app's GUI toolkit there), which desktops show via StatusNotifierItem.
    pywebview reuses the QApplication made here."""
    try:
        import sys
        from qtpy.QtGui import QIcon
        from qtpy.QtWidgets import QApplication, QMenu, QSystemTrayIcon
        app = QApplication.instance() or QApplication(sys.argv)
        app.setApplicationName(APP_NAME)
        app.setDesktopFileName("symphone")              # matches symphone.desktop: the window gets the app's icon
        app.setWindowIcon(QIcon(str(UI_DIR / "icon.png")))
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return False
        tray = QSystemTrayIcon(QIcon(str(UI_DIR / "icon.png")), app)
        menu = QMenu()
        for it in items:
            if it is None:
                menu.addSeparator()
            else:
                menu.addAction(it[0].replace("\t", "    ")).triggered.connect(lambda _=False, fn=it[1]: fn())
        tray.setContextMenu(menu)
        tray.setToolTip(APP_NAME)
        tray.activated.connect(lambda reason: reason == QSystemTrayIcon.ActivationReason.Trigger and show_window())
        tray.show()
        _qt_refs.extend([tray, menu])
        return True
    except Exception as e:
        log.info("Qt tray icon: %s", e)
        return False
