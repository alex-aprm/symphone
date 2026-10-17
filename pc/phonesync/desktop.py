"""What the app needs from the desktop, on Windows, macOS and Linux: opening files, the clipboard, global shortcuts,
autostart, the app's entries in the file manager and the system look.

Windows goes through winutil (Win32 / registry); macOS and Linux use their command-line tools (open, pbcopy,
xdg-open, wl-copy / xclip, gsettings) and per-user files (LaunchAgents, XDG autostart, Nautilus scripts).
"""
import hashlib
import logging
import os
import shlex
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

from .config import APP_NAME, IS_WIN, IS_MAC, IS_LINUX, PLATFORM, NO_WINDOW, UI_DIR, settings

if IS_WIN:
    from . import winutil

log = logging.getLogger("desktop")


# ---------------------------------------------------------------- open / reveal
def open_path(p) -> None:
    """Opens a file, folder or URL with the default app."""
    p = str(p)
    if IS_WIN:
        os.startfile(p)
    elif p.startswith(("http://", "https://")):
        webbrowser.open(p)
    else:
        subprocess.Popen(["open" if IS_MAC else "xdg-open", p], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def reveal(path) -> None:
    """Selects the file in a file manager window (or opens the folder)."""
    path = Path(path)
    if not path.exists():
        return
    if IS_WIN:
        if path.is_file():
            subprocess.Popen(["explorer.exe", "/select,", str(path)])
        else:
            os.startfile(path)
    elif IS_MAC:
        subprocess.Popen(["open", "-R", str(path)] if path.is_file() else ["open", str(path)])
    else:
        # File managers that implement org.freedesktop.FileManager1 (Nautilus, Dolphin, Nemo...) select the file
        try:
            if path.is_file() and shutil.which("dbus-send") and subprocess.run(
                    ["dbus-send", "--session", "--print-reply", "--dest=org.freedesktop.FileManager1",
                     "/org/freedesktop/FileManager1", "org.freedesktop.FileManager1.ShowItems",
                     f"array:string:{path.as_uri()}", "string:"], capture_output=True, timeout=3).returncode == 0:
                return
        except Exception:
            pass
        open_path(path if path.is_dir() else path.parent)


FILE_MANAGER = "Explorer" if IS_WIN else "Finder" if IS_MAC else "Files"


# ---------------------------------------------------------------- clipboard (text)
def _clip_cmd(write: bool) -> list[str] | None:
    if IS_MAC:
        return ["pbcopy"] if write else ["pbpaste"]
    # X11 tools first: under Xwayland the compositor keeps the X and Wayland clipboards in sync, and wl-paste would
    # flash a focus-stealing surface on GNOME every time the clipboard is polled.
    if os.environ.get("DISPLAY"):
        if shutil.which("xclip"):
            return ["xclip", "-selection", "clipboard"] + ([] if write else ["-o"])
        if shutil.which("xsel"):
            return ["xsel", "--clipboard", "--input" if write else "--output"]
    if os.environ.get("WAYLAND_DISPLAY") and shutil.which("wl-copy"):
        return ["wl-copy"] if write else ["wl-paste", "--no-newline", "--type", "text"]
    return None


def set_clipboard(text: str) -> bool:
    if IS_WIN:
        return winutil.set_clipboard(text)
    cmd = _clip_cmd(True)
    if not cmd:
        log.warning("no clipboard tool (install wl-clipboard or xclip)")
        return False
    try:
        # wl-copy / xclip stay in the background serving the selection: don't wait for them
        p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             env=dict(os.environ, LANG=os.environ.get("LANG") or "en_US.UTF-8"))
        p.stdin.write(text.encode("utf-8"))
        p.stdin.close()
        if IS_MAC:
            p.wait(5)
        return True
    except Exception as e:
        log.info("clipboard write: %s", e)
        return False


def _dib(path: str) -> bytes | None:
    """The picture as a device-independent bitmap (BMP minus its 14-byte file header), or None if it can't be read."""
    try:
        import io
        from PIL import Image
        with Image.open(path) as im:
            buf = io.BytesIO()
            im.convert("RGB").save(buf, "BMP")
        return buf.getvalue()[14:]
    except Exception:
        return None


def copy_files(paths: list[str]) -> bool:
    """Files on the clipboard, ready to paste; a single photo also as the picture itself."""
    if not paths:
        return False
    if IS_WIN:
        one = paths[0] if len(paths) == 1 and Path(paths[0]).suffix.lower() in (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp") else None
        return winutil.set_clipboard_files(paths, _dib(one) if one else None)
    uris = "\n".join(Path(p).resolve().as_uri() for p in paths)
    try:
        if IS_MAC:
            files = ", ".join(f'POSIX file "{Path(p).resolve()}"' for p in paths)
            return subprocess.run(["osascript", "-e", f"set the clipboard to {{{files}}}"], timeout=10).returncode == 0
        if os.environ.get("DISPLAY") and shutil.which("xclip"):
            cmd = ["xclip", "-selection", "clipboard", "-t", "text/uri-list"]
        elif os.environ.get("WAYLAND_DISPLAY") and shutil.which("wl-copy"):
            cmd = ["wl-copy", "--type", "text/uri-list"]
        else:
            return False
        p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        p.stdin.write(uris.encode())
        p.stdin.close()
        return True
    except Exception as e:
        log.info("clipboard files: %s", e)
        return False


def get_clipboard() -> str:
    if IS_WIN:
        return winutil.get_clipboard()
    cmd = _clip_cmd(False)
    if not cmd:
        return ""
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=3,
                           env=dict(os.environ, LANG=os.environ.get("LANG") or "en_US.UTF-8"))
        return r.stdout.decode("utf-8", "replace") if r.returncode == 0 else ""
    except Exception:
        return ""


_mac_pb = None


def clipboard_seq() -> int:
    """Changes whenever the clipboard does (cheap to poll on Windows and macOS; Linux compares the text)."""
    global _mac_pb
    if IS_WIN:
        return winutil.clipboard_seq()
    if IS_MAC:
        try:
            if _mac_pb is None:
                from AppKit import NSPasteboard          # pyobjc, which pywebview uses on macOS
                _mac_pb = NSPasteboard.generalPasteboard()
            return int(_mac_pb.changeCount())
        except Exception:
            pass
    return int.from_bytes(hashlib.blake2b(get_clipboard().encode("utf-8", "replace"), digest_size=8).digest(), "big")


# ---------------------------------------------------------------- global shortcuts
SHORTCUTS = {"unlock": "U", "phone": "P", "lock": "L"}       # Ctrl+Alt+<key>


def start_hotkeys(actions: dict) -> bool:
    """Ctrl+Alt+U / P / L. Windows: RegisterHotKey. Linux (X11): pynput. macOS: none (would need the Input
    Monitoring permission); the menu bar icon has the same commands."""
    if IS_WIN:
        winutil.start_hotkeys({(winutil.MOD_CONTROL | winutil.MOD_ALT, ord(k)): actions[a] for a, k in SHORTCUTS.items()})
        return True
    if IS_MAC or not os.environ.get("DISPLAY"):
        return False
    try:
        from pynput import keyboard
    except Exception as e:
        log.info("global shortcuts unavailable: %s", e)
        return False

    def wrap(fn):
        def cb():
            threading.Thread(target=fn, daemon=True).start()     # never block the listener thread
        return cb
    try:
        hk = keyboard.GlobalHotKeys({f"<ctrl>+<alt>+{k.lower()}": wrap(actions[a]) for a, k in SHORTCUTS.items()})
        hk.daemon = True
        hk.start()
        return True
    except Exception as e:
        log.info("global shortcuts unavailable: %s", e)
        return False


# ---------------------------------------------------------------- launching this app
def exe_command() -> list[str]:
    if IS_WIN:
        return winutil.exe_command()
    if getattr(sys, "frozen", False):
        return [sys.executable]
    return [sys.executable, str(Path(__file__).resolve().parents[1] / "Symphone.pyw")]


_LAUNCH_AGENT = Path.home() / "Library" / "LaunchAgents" / "dev.symphone.app.plist"
_XDG_DATA = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
_XDG_CONFIG = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
_AUTOSTART = _XDG_CONFIG / "autostart" / "symphone.desktop"
_APP_ENTRY = _XDG_DATA / "applications" / "symphone.desktop"
_NAUTILUS = _XDG_DATA / "nautilus" / "scripts"


def _desktop_entry(args: str = "") -> str:
    cmd = " ".join(shlex.quote(c) for c in exe_command()) + (f" {args}" if args else "")
    return ("[Desktop Entry]\nType=Application\nName=Symphone\nComment=Your phone on this computer\n"
            f"Exec={cmd}\nIcon={UI_DIR / 'icon.png'}\nTerminal=false\nCategories=Utility;\n"
            "StartupWMClass=Symphone\n")


def set_autostart(enabled: bool):
    if IS_WIN:
        return winutil.set_autostart(enabled)
    if IS_MAC:
        if enabled:
            args = "".join(f"<string>{a}</string>" for a in exe_command() + ["--minimized"])
            _LAUNCH_AGENT.parent.mkdir(parents=True, exist_ok=True)
            _LAUNCH_AGENT.write_text(
                '<?xml version="1.0" encoding="UTF-8"?>\n'
                '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
                '<plist version="1.0"><dict><key>Label</key><string>dev.symphone.app</string>'
                f'<key>ProgramArguments</key><array>{args}</array>'
                '<key>RunAtLoad</key><true/><key>ProcessType</key><string>Interactive</string></dict></plist>\n', "utf-8")
        else:
            _LAUNCH_AGENT.unlink(missing_ok=True)
        return
    if enabled:
        _AUTOSTART.parent.mkdir(parents=True, exist_ok=True)
        _AUTOSTART.write_text(_desktop_entry("--minimized") + "X-GNOME-Autostart-enabled=true\n", "utf-8")
    else:
        _AUTOSTART.unlink(missing_ok=True)


def install_shell_integration(hub=None):
    """Windows: Start menu entry, phonesync: links, Send to. Linux: the app launcher entry and Nautilus'
    Scripts > Send to <phone>. macOS: nothing (the app lives in /Applications)."""
    if IS_WIN:
        return winutil.install_shell_integration(UI_DIR / "icon.ico", hub)
    if IS_LINUX:
        _APP_ENTRY.parent.mkdir(parents=True, exist_ok=True)
        _APP_ENTRY.write_text(_desktop_entry(), "utf-8")
        if hub is not None:
            install_send_to(hub)


def install_send_to(hub):
    """One 'Send to <phone>' per paired phone: Explorer's Send to (Windows), Nautilus scripts (Linux)."""
    if IS_WIN:
        return winutil.install_send_to(hub)
    if not IS_LINUX:
        return
    old = set(settings["sendto_links"] or [])
    want = {}
    for p in hub.phones.values():
        name = "".join(c for c in p.name if c not in '/\\:*?"<>|').strip() or "Phone"
        want[f"Send to {name}"] = p.dev_id
    _NAUTILUS.mkdir(parents=True, exist_ok=True)
    for n in old - set(want):
        (_NAUTILUS / n).unlink(missing_ok=True)
    cmd = " ".join(shlex.quote(c) for c in exe_command())
    for n, dev in want.items():
        f = _NAUTILUS / n
        f.write_text(f'#!/bin/sh\n# Symphone: send the selected files to the phone\nexec {cmd} --send --dev {shlex.quote(dev)} "$@"\n',
                     "utf-8")
        f.chmod(0o755)
    settings["sendto_links"] = sorted(want)


def cleanup():
    """Undo what the app set up (uninstaller / package removal)."""
    if IS_WIN:
        return winutil.cleanup()
    try:
        set_autostart(False)
    except OSError:
        pass
    if IS_LINUX:
        _APP_ENTRY.unlink(missing_ok=True)
        for n in settings["sendto_links"] or []:
            (_NAUTILUS / n).unlink(missing_ok=True)
        unregister_phone_folder()


# ---------------------------------------------------------------- the phone in the file manager
_BOOKMARKS = _XDG_CONFIG / "gtk-3.0" / "bookmarks"


def register_phone_folder(name: str, target: Path, icon: Path | None = None):
    """Windows: an entry next to This PC. Linux: a sidebar bookmark in Files (GTK). macOS: the mounted volume shows
    up in Finder's sidebar by itself."""
    if IS_WIN:
        return winutil.register_phone_folder(name, target, icon)
    if IS_LINUX:
        _set_bookmark(target, name)


def unregister_phone_folder():
    if IS_WIN:
        return winutil.unregister_phone_folder()
    if IS_LINUX:
        _set_bookmark(None, "")


def _set_bookmark(target: Path | None, name: str):
    from .drive import MOUNT_DIR
    uri = MOUNT_DIR.as_uri()
    try:
        lines = _BOOKMARKS.read_text("utf-8").splitlines() if _BOOKMARKS.exists() else []
        keep = [l for l in lines if l.split(" ")[0] != uri]
        if target is not None:
            keep.append(f"{Path(target).as_uri()} {name}")
        if keep != lines:
            _BOOKMARKS.parent.mkdir(parents=True, exist_ok=True)
            _BOOKMARKS.write_text("\n".join(keep) + ("\n" if keep else ""), "utf-8")
    except OSError as e:
        log.info("bookmark: %s", e)


def open_phone_folder(target: Path):
    if IS_WIN:
        return winutil.open_phone_folder()
    open_path(target)


# ---------------------------------------------------------------- look: light/dark + accent
_DEFAULT_ACCENT = {"accent": "#0078d4", "light1": "#0091f8", "light2": "#4cc2ff", "light3": "#99ebff",
                   "dark1": "#0067c0", "dark2": "#003e92", "dark3": "#001a68"}
# macOS AppleAccentColor / GNOME accent-color -> the system's own accent swatch
_MAC_ACCENTS = {"-1": "#8c8c8c", "0": "#ff5257", "1": "#f7821b", "2": "#ffc600", "3": "#62ba46", "4": "#007aff",
                "5": "#a550a7", "6": "#f74f9e"}
_GNOME_ACCENTS = {"blue": "#3584e4", "teal": "#2190a4", "green": "#3a944a", "yellow": "#c88800", "orange": "#ed5b00",
                  "red": "#e62d42", "pink": "#d56199", "purple": "#9141ac", "slate": "#6f8396"}
_theme_cache = {"t": 0.0, "v": None}


def _run(cmd: list[str]) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=3).stdout.strip()
    except Exception:
        return ""


def _palette(hex_color: str) -> dict:
    """Windows-style light/dark accent steps from one color."""
    c = hex_color.lstrip("#")
    r, g, b = (int(c[i:i + 2], 16) for i in (0, 2, 4))

    def mix(t, f):
        return "#" + "".join(f"{round(x + (t - x) * f):02x}" for x in (r, g, b))
    return {"accent": f"#{c}", "light1": mix(255, .2), "light2": mix(255, .45), "light3": mix(255, .7),
            "dark1": mix(0, .15), "dark2": mix(0, .45), "dark3": mix(0, .7)}


def theme() -> dict:
    """The system's app mode and accent palette."""
    if IS_WIN:
        return {**winutil.theme(), "platform": PLATFORM}
    if _theme_cache["v"] and time.time() - _theme_cache["t"] < 5:
        return _theme_cache["v"]
    dark, pal = False, dict(_DEFAULT_ACCENT)
    if IS_MAC:
        dark = _run(["defaults", "read", "-g", "AppleInterfaceStyle"]) == "Dark"
        a = _MAC_ACCENTS.get(_run(["defaults", "read", "-g", "AppleAccentColor"]))
        if a:
            pal = _palette(a)
    elif shutil.which("gsettings"):
        dark = "dark" in _run(["gsettings", "get", "org.gnome.desktop.interface", "color-scheme"]) or \
            "dark" in _run(["gsettings", "get", "org.gnome.desktop.interface", "gtk-theme"]).lower()
        a = _GNOME_ACCENTS.get(_run(["gsettings", "get", "org.gnome.desktop.interface", "accent-color"]).strip("'"))
        if a:
            pal = _palette(a)
    v = {"dark": dark, "transparency": False, "mica": False, "platform": PLATFORM, **pal}
    _theme_cache.update(t=time.time(), v=v)
    return v
