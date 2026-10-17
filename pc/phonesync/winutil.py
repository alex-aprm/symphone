"""Small Win32 helpers: clipboard, global hotkeys, shortcuts, autostart."""
import ctypes
import logging
import subprocess
import sys
import threading
import winreg
from ctypes import wintypes
from pathlib import Path

log = logging.getLogger("win")
user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
kernel32.GlobalLock.restype = ctypes.c_void_p
kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
user32.GetClipboardData.restype = wintypes.HANDLE
user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
CF_UNICODETEXT = 13


def set_clipboard(text: str):
    data = (text + "\0").encode("utf-16-le")
    for _ in range(10):
        if user32.OpenClipboard(None):
            break
        threading.Event().wait(0.05)
    else:
        return False
    try:
        user32.EmptyClipboard()
        h = kernel32.GlobalAlloc(0x0042, len(data))
        p = kernel32.GlobalLock(h)
        ctypes.memmove(p, data, len(data))
        kernel32.GlobalUnlock(h)
        user32.SetClipboardData(CF_UNICODETEXT, h)
        return True
    finally:
        user32.CloseClipboard()


CF_DIB, CF_HDROP = 8, 15


def _global(data: bytes):
    h = kernel32.GlobalAlloc(0x0042, len(data))
    p = kernel32.GlobalLock(h)
    ctypes.memmove(p, data, len(data))
    kernel32.GlobalUnlock(h)
    return h


def set_clipboard_files(paths: list[str], dib: bytes | None = None) -> bool:
    """Puts files on the clipboard the way Explorer's Copy does (paste in Explorer, chats, mail); `dib` (a BMP without
    its file header) adds the picture itself for apps that paste images (Paint, Word, editors)."""
    # DROPFILES {pFiles=20, pt, fNC, fWide=1} + a double-null-terminated list of wide paths
    names = ("\0".join(str(Path(p).resolve()) for p in paths) + "\0\0").encode("utf-16-le")
    drop = (20).to_bytes(4, "little") + bytes(12) + (1).to_bytes(4, "little") + names
    for _ in range(10):
        if user32.OpenClipboard(None):
            break
        threading.Event().wait(0.05)
    else:
        return False
    try:
        user32.EmptyClipboard()
        user32.SetClipboardData(CF_HDROP, _global(drop))
        effect = user32.RegisterClipboardFormatW("Preferred DropEffect")
        user32.SetClipboardData(effect, _global((1).to_bytes(4, "little")))      # DROPEFFECT_COPY
        if dib:
            user32.SetClipboardData(CF_DIB, _global(dib))
        return True
    finally:
        user32.CloseClipboard()


def bring_to_front(title: str) -> bool:
    """Raises this process's top-level window called `title` (restoring it if minimized)."""
    pid = kernel32.GetCurrentProcessId()
    found = []
    proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def each(hwnd, _):
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid and user32.IsWindowVisible(hwnd):
            buf = ctypes.create_unicode_buffer(256)
            user32.GetWindowTextW(hwnd, buf, 256)
            if buf.value == title:
                found.append(hwnd)
                return False
        return True

    user32.EnumWindows(proc(each), 0)
    if not found:
        return False
    hwnd = found[0]
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 9)                    # SW_RESTORE
    user32.BringWindowToTop(hwnd)
    return bool(user32.SetForegroundWindow(hwnd))


def get_clipboard() -> str:
    if not user32.OpenClipboard(None):
        return ""
    try:
        h = user32.GetClipboardData(CF_UNICODETEXT)
        if not h:
            return ""
        p = kernel32.GlobalLock(h)
        try:
            return ctypes.wstring_at(p)
        finally:
            kernel32.GlobalUnlock(h)
    finally:
        user32.CloseClipboard()


def clipboard_seq() -> int:
    return user32.GetClipboardSequenceNumber()


MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_NOREPEAT = 0x1, 0x2, 0x4, 0x4000
WM_HOTKEY = 0x0312


def start_hotkeys(bindings: dict[tuple[int, int], callable]):
    """bindings: {(modifiers, vk): callback}; callbacks run on the hotkey thread."""
    def run():
        ids = {}
        for i, ((mods, vk), cb) in enumerate(bindings.items(), 1):
            if user32.RegisterHotKey(None, i, mods | MOD_NOREPEAT, vk):
                ids[i] = cb
            else:
                log.warning("hotkey %s/%s unavailable", mods, vk)
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
            if msg.message == WM_HOTKEY and msg.wParam in ids:
                try:
                    ids[msg.wParam]()
                except Exception:
                    log.exception("hotkey")
    threading.Thread(target=run, daemon=True, name="hotkeys").start()


def exe_command() -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable]
    pyw = Path(sys.executable).with_name("pythonw.exe")
    launcher = Path(__file__).resolve().parents[1] / "Symphone.pyw"   # works from any working directory
    return [str(pyw if pyw.exists() else sys.executable), str(launcher)]


def set_autostart(enabled: bool):
    key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run", 0, winreg.KEY_ALL_ACCESS)
    try:
        if enabled:
            cmd = " ".join(f'"{c}"' if " " in c or "\\" in c else c for c in exe_command()) + " --minimized"
            winreg.SetValueEx(key, "Symphone", 0, winreg.REG_SZ, cmd)
            try:
                winreg.DeleteValue(key, "PhoneSync")      # the app's former name
            except FileNotFoundError:
                pass
        else:
            try:
                winreg.DeleteValue(key, "Symphone")
            except FileNotFoundError:
                pass
    finally:
        winreg.CloseKey(key)


def create_shortcut(lnk: Path, args: str, description: str, icon: Path | None = None):
    cmd = exe_command()
    target, extra = cmd[0], " ".join(cmd[1:])
    workdir = str(Path(__file__).resolve().parents[1]) if not getattr(sys, "frozen", False) else str(Path(target).parent)
    ps = (
        "$s=(New-Object -ComObject WScript.Shell).CreateShortcut($env:LNK);"
        "$s.TargetPath=$env:TGT;$s.Arguments=$env:ARGS;$s.Description=$env:DESC;$s.WorkingDirectory=$env:WD;"
        "if($env:ICO){$s.IconLocation=$env:ICO};$s.Save()"
    )
    env = dict(__import__("os").environ, LNK=str(lnk), TGT=target, ARGS=f"{extra} {args}".strip(), DESC=description,
               WD=workdir, ICO=str(icon) if icon else "")
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], env=env, creationflags=subprocess.CREATE_NO_WINDOW, timeout=20)


def install_shell_integration(icon: Path, hub=None):
    """'Send to > <phone>' in Explorer and a Start menu entry."""
    import os
    appdata = Path(os.environ["APPDATA"])
    start = appdata / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Symphone.lnk"
    # Recreated on every start so they always point at the running build (dev vs installed exe).
    create_shortcut(start, "", "Symphone", icon)
    try:
        (start.parent / "PhoneSync.lnk").unlink()       # the app's former name
    except OSError:
        pass
    register_protocol()
    if hub is not None:
        install_send_to(hub)


def cleanup():
    """Undo what the app set up in Windows (uninstaller): autostart, Start menu entry, Send to, Explorer, links."""
    import os
    from .config import settings
    try:
        set_autostart(False)
    except OSError:
        pass
    appdata = Path(os.environ["APPDATA"])
    sendto = appdata / "Microsoft" / "Windows" / "SendTo"
    for n in list(settings["sendto_links"] or []) + ["Phone (PhoneSync).lnk", "Phone (Symphone).lnk"]:
        try:
            (sendto / n).unlink()
        except OSError:
            pass
    for lnk in ("Symphone.lnk", "PhoneSync.lnk"):
        try:
            (appdata / "Microsoft" / "Windows" / "Start Menu" / "Programs" / lnk).unlink()
        except OSError:
            pass
    unregister_phone_folder()
    ns = r"Software\Classes\CLSID" + "\\" + PHONE_NS_CLSID
    for key in (r"Software\Classes\phonesync\shell\open\command", r"Software\Classes\phonesync\shell\open",
                r"Software\Classes\phonesync\shell", r"Software\Classes\phonesync",
                ns + r"\Instance\InitPropertyBag", ns + r"\Instance", ns + r"\ShellFolder",
                ns + r"\InProcServer32", ns + r"\DefaultIcon", ns):
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key)
        except OSError:
            pass


def register_protocol():
    """phonesync: URLs (notification clicks) start/notify this app."""
    cmd = " ".join(f'"{c}"' for c in exe_command()) + ' "%1"'
    _setv(r"Software\Classes\phonesync", "", winreg.REG_SZ, "URL:Symphone")
    _setv(r"Software\Classes\phonesync", "URL Protocol", winreg.REG_SZ, "")
    _setv(r"Software\Classes\phonesync\shell\open\command", "", winreg.REG_SZ, cmd)


# ---------------------------------------------------------------- Explorer "device" (shell namespace folder)
PHONE_NS_CLSID = "{5E7C3A1E-9B0D-4C55-9E6A-50A0C3E7D6A1}"
_FOLDER_SHORTCUT = "{0E5AAE11-A475-4c5b-AB00-C66DE400274E}"
SHDID_MOBILE_DEVICE = 21
_EXPLORER = r"Software\Microsoft\Windows\CurrentVersion\Explorer"


def _setv(path, name, typ, val):
    k = winreg.CreateKey(winreg.HKEY_CURRENT_USER, path)
    winreg.SetValueEx(k, name, 0, typ, val)
    winreg.CloseKey(k)


def register_phone_folder(name: str, target: Path, icon: Path):
    """Shows `target` as its own entry in Explorer's navigation pane, next to This PC (like OneDrive), with a picture of
    the phone as its icon."""
    base = r"Software\Classes\CLSID" + "\\" + PHONE_NS_CLSID
    _setv(base, "", winreg.REG_SZ, name)
    _setv(base, "System.IsPinnedToNameSpaceTree", winreg.REG_DWORD, 1)
    _setv(base, "SortOrderIndex", winreg.REG_DWORD, 0x54)
    _setv(base, "DescriptionID", winreg.REG_DWORD, SHDID_MOBILE_DEVICE)
    _setv(base, "InfoTip", winreg.REG_SZ, "Phone storage (Symphone)")
    _setv(base + r"\DefaultIcon", "", winreg.REG_SZ, f"{icon},0")
    _setv(base + r"\InProcServer32", "", winreg.REG_EXPAND_SZ, r"%SystemRoot%\system32\shell32.dll")
    _setv(base + r"\Instance", "CLSID", winreg.REG_SZ, _FOLDER_SHORTCUT)
    _setv(base + r"\Instance\InitPropertyBag", "Attributes", winreg.REG_DWORD, 0x11)
    _setv(base + r"\Instance\InitPropertyBag", "TargetFolderPath", winreg.REG_EXPAND_SZ, str(target))
    _setv(base + r"\ShellFolder", "FolderValueFlags", winreg.REG_DWORD, 0x28)
    _setv(base + r"\ShellFolder", "Attributes", winreg.REG_DWORD, 0xF080004D)
    _setv(_EXPLORER + r"\Desktop\NameSpace" + "\\" + PHONE_NS_CLSID, "", winreg.REG_SZ, name)
    try:                                       # older builds put it inside This PC
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, _EXPLORER + r"\MyComputer\NameSpace" + "\\" + PHONE_NS_CLSID)
    except OSError:
        pass
    _setv(_EXPLORER + r"\HideDesktopIcons\NewStartPanel", PHONE_NS_CLSID, winreg.REG_DWORD, 1)
    ctypes.windll.shell32.SHChangeNotify(0x08000000, 0, None, None)  # SHCNE_ASSOCCHANGED


def unregister_phone_folder():
    for where in ("MyComputer", "Desktop"):
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, _EXPLORER + "\\" + where + r"\NameSpace" + "\\" + PHONE_NS_CLSID)
        except OSError:
            pass
    ctypes.windll.shell32.SHChangeNotify(0x08000000, 0, None, None)


def open_phone_folder():
    subprocess.Popen(["explorer.exe", "shell:::" + PHONE_NS_CLSID])


# ---------------------------------------------------------------- Windows look: light/dark + accent colors
def theme() -> dict:
    """The user's Windows app mode and accent palette (what Settings itself uses)."""
    dark, transparency = True, True
    try:
        k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize")
        dark = winreg.QueryValueEx(k, "AppsUseLightTheme")[0] == 0
        try:
            transparency = winreg.QueryValueEx(k, "EnableTransparency")[0] != 0     # Settings > Transparency effects
        except OSError:
            pass
        winreg.CloseKey(k)
    except OSError:
        pass
    pal = {}
    try:
        from winrt.windows.ui.viewmanagement import UISettings, UIColorType
        u = UISettings()
        for name, t in (("accent", "ACCENT"), ("light1", "ACCENT_LIGHT1"), ("light2", "ACCENT_LIGHT2"),
                        ("light3", "ACCENT_LIGHT3"), ("dark1", "ACCENT_DARK1"), ("dark2", "ACCENT_DARK2"),
                        ("dark3", "ACCENT_DARK3")):
            c = u.get_color_value(getattr(UIColorType, t))
            pal[name] = f"#{c.r:02x}{c.g:02x}{c.b:02x}"
    except Exception:
        pal = {"accent": "#0078d4", "light1": "#0091f8", "light2": "#4cc2ff", "light3": "#99ebff",
               "dark1": "#0067c0", "dark2": "#003e92", "dark3": "#001a68"}
    from . import gui
    return {"dark": dark, "transparency": transparency, "mica": bool(gui.mica_on), **pal}


class _Margins(ctypes.Structure):
    _fields_ = [("l", ctypes.c_int), ("r", ctypes.c_int), ("t", ctypes.c_int), ("b", ctypes.c_int)]


def backdrop(hwnd: int, dark: bool, mica: bool, solid: str) -> bool:
    """Windows 11 look for a window: Mica material behind the whole window (title bar included, like Settings) when
    `mica`, else a title bar painted the window's own background color `solid`. True if Mica is on."""
    dwm = ctypes.windll.dwmapi
    h = wintypes.HWND(hwnd)

    def attr(a, v):
        val = ctypes.c_int(v)
        return dwm.DwmSetWindowAttribute(h, a, ctypes.byref(val), ctypes.sizeof(val)) == 0
    attr(20, 1 if dark else 0)                                   # DWMWA_USE_IMMERSIVE_DARK_MODE
    if mica:
        m = _Margins(-1, -1, -1, -1)
        dwm.DwmExtendFrameIntoClientArea(h, ctypes.byref(m))   # the material shows through the client area
        attr(35, 0xFFFFFFFF)                                     # DWMWA_CAPTION_COLOR: default (material)
        if attr(38, 2):                                          # DWMWA_SYSTEMBACKDROP_TYPE: Mica (22H2+)
            return True
    m = _Margins(0, 0, 0, 0)
    dwm.DwmExtendFrameIntoClientArea(h, ctypes.byref(m))
    attr(38, 1)                                                  # no backdrop
    c = solid.lstrip("#")
    attr(35, int(c[4:6] + c[2:4] + c[0:2], 16))                   # COLORREF 0x00BBGGRR: title bar = background
    return False


def dark_title_bar(hwnd: int, dark: bool):
    """Matches the window frame to the app theme (DWMWA_USE_IMMERSIVE_DARK_MODE) and uses Mica for the backdrop."""
    try:
        v = ctypes.c_int(1 if dark else 0)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(wintypes.HWND(hwnd), 20, ctypes.byref(v), ctypes.sizeof(v))
    except Exception:
        pass


# ---------------------------------------------------------------- Explorer "Send to" (one entry per phone)
def install_send_to(hub, icon: Path | None = None):
    import os
    from .config import settings, UI_DIR
    sendto = Path(os.environ["APPDATA"]) / "Microsoft" / "Windows" / "SendTo"
    icon = icon or UI_DIR / "phone.ico"
    old = set(settings["sendto_links"] or [])
    legacy = sendto / "Phone (PhoneSync).lnk"
    if legacy.exists():
        old.add(legacy.name)
    want = {}
    for p in hub.phones.values():
        name = "".join(c for c in p.name if c not in r'\/:*?"<>|').strip() or "Phone"
        want[f"{name}.lnk"] = p.dev_id
    if not want:
        want["Phone (Symphone).lnk"] = ""
    for n in old - set(want):
        try:
            (sendto / n).unlink()
        except OSError:
            pass
    accent = theme()["accent"]
    for n, dev in want.items():
        ico = icon
        if dev:
            try:
                from . import phonepic
                ico = phonepic.ico(dev, accent)
            except Exception:
                pass
        create_shortcut(sendto / n, f"--send --dev {dev}" if dev else "--send", "Send to phone (Symphone)", ico)
    settings["sendto_links"] = sorted(want)
