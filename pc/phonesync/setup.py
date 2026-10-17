"""First-run setup (the wizard in the UI): what is installed on this PC, the phone's checklist, the optional extras.

Extras (Windows) need administrator rights; they are installed by setup/extras.ps1 (bundled with the app), started
elevated only when the user clicks "Install" in the wizard or in Settings. macOS and Linux have none to install
(Linux: the mount needs fuse3, which the package depends on).
"""
import asyncio
import logging
import os
import subprocess
import time
from pathlib import Path

from .config import APP_DIR, BASE_DIR, IS_WIN, NO_WINDOW, settings

log = logging.getLogger("setup")
VCAM_CLSID = "{4A97DD59-7288-40DC-A638-997758C35BD1}"
APK_URL = "https://github.com/alex-aprm/symphone/releases/latest/download/Symphone.apk"
_cache: dict = {"t": 0.0, "v": {}}


def _key_exists(root, path: str) -> bool:
    import winreg
    try:
        winreg.CloseKey(winreg.OpenKey(root, path))
        return True
    except OSError:
        return False


def _vdd_installed() -> bool:
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command",
                              "(Get-PnpDevice -FriendlyName 'Virtual Display Driver' -ErrorAction SilentlyContinue | "
                              "Measure-Object).Count"], capture_output=True, text=True, timeout=15,
                             creationflags=NO_WINDOW).stdout.strip()
        return out not in ("", "0")
    except Exception:
        return False


def extras() -> dict:
    """Which optional system components are installed (cached for a minute; querying PnP is slow)."""
    if not IS_WIN:
        return {}
    if time.time() - _cache["t"] < 60 and _cache["v"]:
        return _cache["v"]
    import winreg
    from .drive import winfsp_installed
    v = {
        "winfsp": winfsp_installed(),
        "vbcable": _key_exists(winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Services\VBAudioVACMME"),
        "display": _vdd_installed(),
        "webcam": _key_exists(winreg.HKEY_LOCAL_MACHINE, rf"SOFTWARE\Classes\CLSID\{VCAM_CLSID}"),
    }
    _cache.update(t=time.time(), v=v)
    return v


def _script() -> Path | None:
    for p in (APP_DIR / "setup" / "extras.ps1", BASE_DIR.parent / "setup" / "extras.ps1",
              Path(__file__).resolve().parents[2] / "setup" / "extras.ps1"):
        if p.exists():
            return p
    return None


def install_extras(items: list[str]) -> dict:
    """Runs setup/extras.ps1 elevated (Windows asks for permission) for the chosen items."""
    if not IS_WIN:
        return {"ok": False, "error": "nothing to install on this system"}
    script = _script()
    if not script:
        return {"ok": False, "error": "setup/extras.ps1 not found next to the app"}
    flags = {"winfsp": "-Drive", "vbcable": "-Microphone", "display": "-SideDisplay", "webcam": "-Webcam"}
    args = " ".join(flags[i] for i in items if i in flags)
    if not args:
        return {"ok": False, "error": "nothing to install"}
    cmd = (f"Start-Process powershell -Verb RunAs -WindowStyle Normal -ArgumentList "
           f"'-NoProfile -ExecutionPolicy Bypass -File \"{script}\" {args}'")
    try:
        subprocess.Popen(["powershell", "-NoProfile", "-Command", cmd], creationflags=NO_WINDOW)
    except OSError as e:
        return {"ok": False, "error": str(e)}
    _cache["t"] = 0.0
    return {"ok": True}


async def phone_checklist(core) -> list | None:
    """The phone app's own checklist (GET /api/setup, phone app 2.6+)."""
    p = core.hub.active
    if p is None or not p.connected:
        return None
    try:
        return (await p.get_json("/api/setup", timeout=4)).get("items")
    except Exception as e:
        log.debug("phone checklist: %s", e)
        return None


async def state(core) -> dict:
    adbm = core.adbm
    return {
        "done": bool(settings["setup_done"]),
        "apk_url": APK_URL,
        "usb": bool(adbm.usb_serial),
        "wireless": bool(adbm.wifi_serial),
        "adb_pair": {k: v for k, v in adbm.qr_pair.items() if k != "pass"},
        "phones": len(core.hub.phones),
        "connected": bool(core.hub.active and core.hub.active.connected),
        "checklist": await phone_checklist(core),
        "extras": await asyncio.to_thread(extras),
    }
