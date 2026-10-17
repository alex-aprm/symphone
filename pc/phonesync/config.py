"""Paths, persistent settings and DPAPI-protected secrets."""
import base64
import ctypes
import ctypes.wintypes as wt
import json
import os
import secrets
import sys
import threading
from pathlib import Path

APP_NAME = "Symphone"
AUMID = "Symphone.Desktop"

UI_PORT = 47801          # local UI (127.0.0.1 only)
DAV_PROXY_PORT = 47811   # local multipath WebDAV proxy for the Explorer drive
USB_FWD_PORT = 47910     # adb forward -> phone:47900
PHONE_PORT = 47900
UDP_PORT = 47800

if getattr(sys, "frozen", False):
    BASE_DIR = Path(sys._MEIPASS)          # bundled resources
    APP_DIR = Path(sys.executable).parent
else:
    BASE_DIR = Path(__file__).resolve().parent
    APP_DIR = BASE_DIR.parent.parent       # repo root

UI_DIR = BASE_DIR / "ui"


def _find_tool(*candidates: Path) -> Path:
    for c in candidates:
        if c.exists():
            return c
    return candidates[0]


TOOLS_DIR = _find_tool(APP_DIR / "tools", BASE_DIR / "tools", Path(__file__).resolve().parents[2] / "tools")
SCRCPY_DIR = next(TOOLS_DIR.glob("scrcpy-win64-*"), TOOLS_DIR / "scrcpy")
ADB = SCRCPY_DIR / "adb.exe"
SCRCPY = SCRCPY_DIR / "scrcpy.exe"
RCLONE = next(TOOLS_DIR.glob("rclone-*/rclone.exe"), TOOLS_DIR / "rclone.exe")

DATA_DIR = Path(os.environ.get("APPDATA", Path.home())) / APP_NAME
CACHE_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home())) / APP_NAME
# The app used to be called PhoneSync: its settings (pairings) and caches move over once.
for _new in (DATA_DIR, CACHE_DIR):
    _old = _new.with_name("PhoneSync")
    if _old.is_dir() and not _new.exists():
        try:
            _old.rename(_new)
        except OSError:                    # something still holds a file in there: keep at least the pairings
            if (_old / "settings.json").exists():
                _new.mkdir(parents=True, exist_ok=True)
                (_new / "settings.json").write_bytes((_old / "settings.json").read_bytes())
THUMB_DIR = CACHE_DIR / "thumbs"
ICON_DIR = CACHE_DIR / "icons"
LOG_DIR = CACHE_DIR / "logs"
for d in (DATA_DIR, CACHE_DIR, THUMB_DIR, ICON_DIR, LOG_DIR):
    d.mkdir(parents=True, exist_ok=True)

SETTINGS_FILE = DATA_DIR / "settings.json"

# Per-phone keys: reading/writing them goes to the *active* phone's record in "devices".
DEVICE_KEYS = ("token", "device_id", "device_name", "display_name", "last_ip", "model")

DEFAULTS = {
    "devices": [],                  # [{device_id, token, device_name, display_name, last_ip, model}]
    "active_device": "",
    "pc_id": "",
    "pc_name": "",
    "clipboard_sync": True,
    "setup_done": False,                # the first-run wizard was finished (or skipped)
    "pinned_windows": [],               # viewer windows kept always on top ("app:<pkg>", "screen:", "camera:back")
    "vcam_camera_id": "",               # phone camera for the Windows webcam ("" = first one facing vcam_facing)
    "vcam_mirror": False,               # flip left-right
    "vcam_rotate": 0,                   # extra clockwise rotation, degrees
    "vcam_autorotate": True,            # keep the picture upright as the phone is turned
    "wfd_prefer": True,                 # Wi-Fi Direct as the preferred link for phones that can keep their Wi-Fi
    "pc_keyboard": True,
    "transfer_toasts": True,
    "ble": True,
    "wifi_direct": True,
    "sidecar_orientation": "landscape",
    "sidecar_scale": 0,                 # (old: Windows scale %, 0 = automatic)
    "sidecar_size": 300,                # side display text size, % of the panel's pixels (100-600)
    "sidecar_size_auto": False,         # Windows' recommended size instead
    "sendto_links": [],
    "wfd_network": None,
    "pin": "",                      # DPAPI-encrypted, base64
    "drive_letter": "P",
    "drive_mode": "device",          # "device" (This PC > Devices, no letter) | "letter"
    "drive_enabled": True,
    "auto_import": True,
    "import_dir": str(Path.home() / "Pictures" / "Phone"),
    "downloads_dir": str(Path.home() / "Downloads" / "Symphone"),
    "unlock_on_mirror": True,
    "toasts": True,
    "toast_muted_apps": [],
    "mirror_max_size": 0,
    "mirror_bitrate": "16M",
    "mirror_fps": 120,
    "turn_screen_off": False,
    "autostart": True,
    "local_secret": "",
    "import_gen": None,
    "vcam_facing": "back",
    "mirror_audio": True,
    "audio_routes": [],                 # sound routes switched on in Settings (kept on across restarts/reconnects)
}


class Settings:
    def __init__(self):
        self._lock = threading.Lock()
        self.data = dict(DEFAULTS)
        if SETTINGS_FILE.exists():
            try:
                self.data.update(json.loads(SETTINGS_FILE.read_text("utf-8")))
            except Exception:
                pass
        dirty = False
        if not self.data["local_secret"]:
            self.data["local_secret"] = secrets.token_hex(16)
            dirty = True
        if not self.data["pc_id"]:
            self.data["pc_id"] = secrets.token_hex(6)
            dirty = True
        # v1 -> v2: the single paired phone becomes the first entry of "devices"
        if self.data.get("token") and not self.data["devices"]:
            self.data["devices"] = [{k: self.data.get(k, "") for k in DEVICE_KEYS}]
            self.data["active_device"] = self.data.get("device_id", "")
            dirty = True
        for k in DEVICE_KEYS:
            self.data.pop(k, None)
        if dirty:
            self.save()

    # ---- devices ----
    def devices(self) -> list[dict]:
        return list(self.data["devices"])

    def device(self, dev_id: str | None = None) -> dict | None:
        dev_id = self.data["active_device"] if dev_id is None else dev_id
        return next((d for d in self.data["devices"] if d.get("device_id") == dev_id), None)

    def upsert_device(self, dev_id: str, activate_if_none=True, **fields) -> dict:
        with self._lock:
            d = next((d for d in self.data["devices"] if d.get("device_id") == dev_id), None)
            if d is None:
                d = {k: "" for k in DEVICE_KEYS}
                d["device_id"] = dev_id
                self.data["devices"].append(d)
            d.update(fields)
            if activate_if_none and not self.device():
                self.data["active_device"] = dev_id
        self.save()
        return d

    def remove_device(self, dev_id: str):
        with self._lock:
            self.data["devices"] = [d for d in self.data["devices"] if d.get("device_id") != dev_id]
            if self.data["active_device"] == dev_id:
                self.data["active_device"] = self.data["devices"][0]["device_id"] if self.data["devices"] else ""
        self.save()

    def __getitem__(self, k):
        if k in DEVICE_KEYS:
            d = self.device()
            return (d or {}).get(k, "") if k != "wfd" else (d or {}).get(k)
        return self.data.get(k, DEFAULTS.get(k))

    def __setitem__(self, k, v):
        self.update(**{k: v})

    def update(self, **kw):
        dev = {k: kw.pop(k) for k in list(kw) if k in DEVICE_KEYS}
        with self._lock:
            self.data.update(kw)
            if dev:
                d = self.device()
                if d is None and dev.get("device_id"):
                    d = {k: "" for k in DEVICE_KEYS}
                    self.data["devices"].append(d)
                    self.data["active_device"] = dev["device_id"]
                if d is not None:
                    d.update(dev)
        self.save()

    def save(self):
        with self._lock:
            tmp = SETTINGS_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, indent=2, default=str), "utf-8")
            os.replace(tmp, SETTINGS_FILE)

    def phone_name(self, dev: dict | None = None) -> str:
        d = dev if dev is not None else (self.device() or {})
        return d.get("display_name") or d.get("device_name") or "Phone"

    def pc_name(self) -> str:
        return self["pc_name"] or os.environ.get("COMPUTERNAME", "PC")

    # ---- PIN (DPAPI, current user) ----
    def set_pin(self, pin: str):
        self["pin"] = base64.b64encode(dpapi_protect(pin.encode())).decode() if pin else ""

    def get_pin(self) -> str:
        v = self["pin"]
        if not v:
            return ""
        try:
            return dpapi_unprotect(base64.b64decode(v)).decode()
        except Exception:
            return ""


class _Blob(ctypes.Structure):
    _fields_ = [("cbData", wt.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _blob(data: bytes) -> _Blob:
    buf = ctypes.create_string_buffer(data, len(data))
    return _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))


def _crypt(fn, data: bytes) -> bytes:
    out = _Blob()
    if not fn(ctypes.byref(_blob(data)), None, None, None, None, 0x1, ctypes.byref(out)):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(out.pbData)


def dpapi_protect(data: bytes) -> bytes:
    return _crypt(ctypes.windll.crypt32.CryptProtectData, data)


def dpapi_unprotect(data: bytes) -> bytes:
    return _crypt(ctypes.windll.crypt32.CryptUnprotectData, data)


settings = Settings()
