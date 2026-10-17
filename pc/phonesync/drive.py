"""Mounts the phone as a drive letter in Explorer: rclone (WinFsp) -> local DavProxy -> phone."""
import asyncio
import logging
import os
import subprocess
from pathlib import Path

from .config import RCLONE, CACHE_DIR, LOG_DIR, DAV_PROXY_PORT, UI_DIR, settings
from . import winutil

MOUNT_DIR = CACHE_DIR / "mnt" / "Phone"

log = logging.getLogger("drive")


def winfsp_installed() -> bool:
    pf86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    return (Path(pf86) / "WinFsp" / "bin" / "winfsp-x64.dll").exists()


class Drive:
    def __init__(self, phone):
        self.phone = phone
        self.proc: subprocess.Popen | None = None
        self.error = ""

    @property
    def letter(self) -> str:
        return settings["drive_letter"].rstrip(":").upper() + ":"

    @property
    def as_device(self) -> bool:
        return settings["drive_mode"] != "letter"

    @property
    def target(self) -> str:
        return str(MOUNT_DIR) if self.as_device else self.letter

    @property
    def mounted(self) -> bool:
        return self.proc is not None and self.proc.poll() is None and os.path.isdir(self.target + "\\")

    def status(self):
        return {"enabled": settings["drive_enabled"], "letter": self.letter, "mode": settings["drive_mode"], "mounted": self.mounted,
                "winfsp": winfsp_installed(), "error": self.error}

    def start(self):
        if self.proc and self.proc.poll() is None:
            return
        if not winfsp_installed():
            self.error = "WinFsp is not installed (run setup\\admin-setup.ps1)"
            return
        if not RCLONE.exists():
            self.error = f"rclone not found at {RCLONE}"
            return
        name = settings.phone_name()
        cache = CACHE_DIR / "rclone"
        if self.as_device:
            # WinFsp mounts onto a directory that must not exist yet
            MOUNT_DIR.parent.mkdir(parents=True, exist_ok=True)
            try:
                MOUNT_DIR.rmdir()
            except OSError:
                pass
            try:
                from . import phonepic
                icon = phonepic.ico(settings["device_id"] or "", winutil.theme()["accent"])
            except Exception:
                icon = UI_DIR / "phone.ico"
            winutil.register_phone_folder(name, MOUNT_DIR, icon)
        else:
            winutil.unregister_phone_folder()
        args = [
            str(RCLONE), "mount", ":webdav:/", self.target,
            "--webdav-url", f"http://127.0.0.1:{DAV_PROXY_PORT}/dav",
            "--webdav-vendor", "owncloud",
            "--webdav-bearer-token", settings["local_secret"],
            "--volname", name,
            "--cache-dir", str(cache),
            "--vfs-cache-mode", "full",          # Explorer gets local-disk semantics; uploads happen behind it
            "--vfs-cache-max-size", "20G",
            "--vfs-cache-max-age", "72h",
            "--vfs-write-back", "1s",
            "--vfs-read-chunk-size", "64M",
            "--vfs-read-chunk-size-limit", "1G",
            "--vfs-read-ahead", "256M",
            "--buffer-size", "64M",
            "--dir-cache-time", "5s",             # phone-side changes show up quickly
            "--attr-timeout", "2s",
            "--transfers", "6",
            "--checkers", "16",
            "--timeout", "60s",
            "--contimeout", "5s",
            "--low-level-retries", "20",
            "--retries", "5",
            "--no-checksum",
            "--use-server-modtime",
            "--file-perms", "0777",
            "--dir-perms", "0777",
            "--log-file", str(LOG_DIR / "rclone.log"),
            "--log-level", "NOTICE",
            "-o", "FileInfoTimeout=2000",
            "-o", "DirInfoTimeout=2000",
        ]
        log.info("mounting %s", self.target)
        self.proc = subprocess.Popen(args, creationflags=subprocess.CREATE_NO_WINDOW,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.error = ""

    def stop(self):
        if self.proc and self.proc.poll() is None:
            # rclone unmounts cleanly on CTRL_BREAK, but a terminate is fine with WinFsp too
            self.proc.terminate()
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None

    async def loop(self):
        """Keeps the drive mounted while enabled; it stays mounted (showing errors) if the phone goes away briefly."""
        while True:
            try:
                if settings["drive_enabled"] and settings["token"]:
                    if not self.proc or self.proc.poll() is not None:
                        if self.proc and self.proc.poll() is not None:
                            self.error = f"rclone exited ({self.proc.returncode}); see {LOG_DIR / 'rclone.log'}"
                            log.warning(self.error)
                        if self.phone.connected:
                            self.start()
                elif self.proc:
                    self.stop()
            except Exception as e:
                self.error = str(e)
                log.exception("drive loop")
            await asyncio.sleep(3)

    def open(self):
        if self.as_device:
            winutil.open_phone_folder()
        elif self.mounted:
            os.startfile(self.letter + "\\")
