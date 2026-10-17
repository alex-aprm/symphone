"""Keeps the phone unlocked near this PC: a Bluetooth link the phone sees as "this PC is connected".

Android's Extend Unlock (Smart Lock) keeps the phone unlocked while a *trusted device* is connected over Bluetooth.
Android doesn't let apps add trusted devices, so the user picks this PC once in the phone's settings (the Settings
page opens that screen on the phone); this module makes sure the PC actually stays connected: it holds an RFCOMM
connection to the phone app's Symphone service (BtLink.kt), with keep-alives, and reconnects when it drops.
Windows only (WinRT Bluetooth); the phone must be paired with the PC in Windows' Bluetooth settings.
"""
import asyncio
import logging
import time
import uuid

from .config import IS_WIN, settings

log = logging.getLogger("btlink")
SERVICE = uuid.UUID("5f1c0d2e-8a4b-4c39-9e7a-53796d70686f")
KEEPALIVE = 20          # s between keep-alives (also how soon a dead link is noticed)


class BtLink:
    def __init__(self, core):
        self.core = core
        self.state = "off"          # off | searching | connected | not-paired | unavailable
        self.error = ""
        self.since = 0.0

    def status(self) -> dict:
        return {"state": self.state, "error": self.error, "since": self.since}

    def _names(self) -> list[str]:
        """The phone's Bluetooth name (reported by the phone app), then the names Symphone knows it by."""
        p = self.core.hub.active
        info = getattr(p, "info", None) or {}
        out = [info.get("btName"), settings["device_name"], settings["display_name"]]
        return [n for n in out if n]

    async def loop(self):
        if not IS_WIN:
            self.state = "unavailable"
            return
        delay = 5
        while True:
            if not settings["bt_unlock"] or not self.core.hub.phones:
                self.state = "off"
                await asyncio.sleep(5)
                continue
            try:
                await self._hold()
                delay = 5
            except asyncio.CancelledError:
                raise
            except Exception as e:
                if self.state != "not-paired":
                    self.state, self.error = "searching", str(e)
                log.debug("bt link: %s", e)
                delay = min(delay * 2, 60)
            await asyncio.sleep(delay)

    async def _hold(self):
        from winrt.windows.devices.bluetooth import BluetoothDevice, BluetoothCacheMode
        from winrt.windows.devices.bluetooth.rfcomm import RfcommServiceId
        from winrt.windows.devices.enumeration import DeviceInformation
        from winrt.windows.networking.sockets import StreamSocket, SocketProtectionLevel
        from winrt.windows.storage.streams import DataWriter, DataReader, InputStreamOptions

        names = self._names()
        devs = await DeviceInformation.find_all_async_aqs_filter(BluetoothDevice.get_device_selector_from_pairing_state(True))
        info = next((d for n in names for d in devs if d.name == n), None)
        if info is None:
            self.state = "not-paired"
            self.error = "Pair the phone with this PC in Windows Bluetooth settings"
            return
        dev = await BluetoothDevice.from_id_async(info.id)
        res = await dev.get_rfcomm_services_for_id_with_cache_mode_async(RfcommServiceId.from_uuid(SERVICE),
                                                                          BluetoothCacheMode.UNCACHED)
        if not res.services or res.services.size == 0:
            raise RuntimeError("the phone app isn't listening (Bluetooth off on the phone?)")
        svc = res.services[0]
        sock = StreamSocket()
        await sock.connect_with_protection_level_async(svc.connection_host_name, svc.connection_service_name,
                                                       SocketProtectionLevel.BLUETOOTH_ENCRYPTION_ALLOW_NULL_AUTHENTICATION)
        log.info("Bluetooth link to %s up", info.name)
        self.state, self.error, self.since = "connected", "", time.time()
        self.core.ui_event("state", self.core.state())
        try:
            w = DataWriter(sock.output_stream)
            r = DataReader(sock.input_stream)
            r.input_stream_options = InputStreamOptions.PARTIAL
            while settings["bt_unlock"]:
                w.write_bytes(b"ping")
                await asyncio.wait_for(w.store_async(), 10)
                n = await asyncio.wait_for(r.load_async(64), 15)
                if not n:
                    break                           # the phone closed it
                r.read_buffer(n)
                await asyncio.sleep(KEEPALIVE)
        finally:
            sock.close()
            log.info("Bluetooth link down")
            self.state = "searching"
            self.core.ui_event("state", self.core.state())
