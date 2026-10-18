"""Finding phones without a shared Wi-Fi: Bluetooth LE presence/pairing and a Wi-Fi Direct fallback network.

BLE: the phone advertises service 5e7c0001-… with service data = SHA-256(phone id)[:6] + its IPv4 address. A known
phone that shows up nearby with a new address is reconnected right away; while the PC shows a pairing QR code an
unknown Symphone phone is paired over GATT (AES-GCM with a key derived from the QR secret).

Wi-Fi Direct: when a paired phone is nearby (BLE) but unreachable over the LAN, or while pairing, the PC hosts a
WPA2 "legacy" Wi-Fi Direct network (DIRECT-PS-xxxx) next to its normal Wi-Fi connection. The phone knows the
credentials (QR code / POST /api/pc/wfd) and joins it when it can't reach the PC otherwise. While the network is up
the PC also advertises over BLE (manufacturer data 0xFFFF: "SY" + SHA-256(PC id)[:6]): a phone off Wi-Fi only asks
Android to join the network (which opens a system dialog) once it sees its PC that way.
"""
import asyncio
import hashlib
import json
import logging
import os
import secrets
import socket
import time

from .config import IS_WIN, settings

log = logging.getLogger("nearby")
SVC = "5e7c0001-9b0d-4c55-9e6a-50a0c3e7d6a1"
CH_INFO = "5e7c0002-9b0d-4c55-9e6a-50a0c3e7d6a1"
CH_PAIR_REQ = "5e7c0003-9b0d-4c55-9e6a-50a0c3e7d6a1"
CH_PAIR_RES = "5e7c0004-9b0d-4c55-9e6a-50a0c3e7d6a1"


BEACON_COMPANY = 0xFFFF
BEACON_MAGIC = b"SY"


def _hash6(dev_id: str) -> bytes:
    return hashlib.sha256(dev_id.encode()).digest()[:6]


def _key(secret: str) -> bytes:
    return hashlib.sha256(secret.encode()).digest()


class Nearby:
    def __init__(self, core):
        self.core = core
        self.ble_state = "off"          # off | scanning | unavailable
        self.ble_error = ""
        self.near: dict[str, float] = {}         # phone id -> last BLE sighting
        self.wfd_state = "off"          # off | on | unavailable
        self.wfd_error = ""
        self._publisher = None
        self._pairing_tried: set[str] = set()
        self._scanner = None
        self._beacon = None             # BLE advertisement: "this PC's direct network is up"

    def status(self):
        now = time.time()
        return {"ble": self.ble_state, "bleError": self.ble_error,
                "wfd": self.wfd_state if IS_WIN else "unavailable", "wfdError": self.wfd_error,
                "near": [k for k, t in self.near.items() if now - t < 30]}

    # ------------------------------------------------------------------ main loop
    async def loop(self):
        await asyncio.sleep(3)
        self._wfd_creds()
        while True:
            try:
                if settings["ble"] and self.ble_state == "off":
                    await self._start_ble()
                elif not settings["ble"] and self._scanner:
                    await self._stop_ble()
                await self._manage_wfd()
            except Exception as e:
                log.warning("nearby: %s", e)
            await asyncio.sleep(5)

    # ------------------------------------------------------------------ BLE
    async def _start_ble(self):
        try:
            from bleak import BleakScanner
        except ImportError as e:
            self.ble_state, self.ble_error = "unavailable", str(e)
            return
        try:
            self._scanner = BleakScanner(self._on_adv, service_uuids=[SVC])
            await self._scanner.start()
            self.ble_state = "scanning"
            log.info("BLE scanning")
        except Exception as e:
            self.ble_state, self.ble_error = "unavailable", str(e)
            self._scanner = None
            log.info("BLE unavailable: %s", e)

    async def _stop_ble(self):
        try:
            await self._scanner.stop()
        except Exception:
            pass
        self._scanner = None
        self.ble_state = "off"

    def _on_adv(self, device, adv):
        data = (adv.service_data or {}).get(SVC)
        if not data or len(data) < 10:
            return
        h, ip = bytes(data[:6]), socket.inet_ntoa(bytes(data[6:10]))
        ip = "" if ip == "0.0.0.0" else ip
        hub = self.core.hub
        for dev_id in hub.phones:
            if _hash6(dev_id) == h:
                self.near[dev_id] = time.time()
                hub.seen(dev_id, ip, "bluetooth")
                return
        if hub.pair_secret and device.address not in self._pairing_tried:
            self._pairing_tried.add(device.address)
            asyncio.ensure_future(self._ble_pair(device))

    async def _ble_pair(self, device):
        """Pairs an unknown phone that scanned our QR code but can't reach us over the LAN."""
        from bleak import BleakClient
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        hub = self.core.hub
        secret = hub.pair_secret[0] if hub.pair_secret else None
        if not secret:
            return
        aes = AESGCM(_key(secret))
        try:
            async with BleakClient(device, timeout=15) as c:
                nonce = os.urandom(12)
                req = json.dumps({"pc": settings["pc_id"], "n": settings.pc_name()}).encode()
                await c.write_gatt_char(CH_PAIR_REQ, nonce + aes.encrypt(nonce, req, None), response=True)
                for _ in range(10):
                    raw = bytes(await c.read_gatt_char(CH_PAIR_RES))
                    if len(raw) > 12:
                        break
                    await asyncio.sleep(0.5)
                ph = json.loads(aes.decrypt(raw[:12], raw[12:], None))
        except Exception as e:
            log.info("BLE pairing with %s failed: %s", device.address, e)
            self._pairing_tried.discard(device.address)
            return
        if hub.peer_pair({"s": secret, "phone": ph}):
            log.info("paired %s over Bluetooth", ph.get("name"))
            self.core.ui_event("paired", {"name": ph.get("name"), "via": "Bluetooth"})

    # ------------------------------------------------------------------ Wi-Fi Direct
    def _wfd_creds(self):
        c = settings["wfd_network"]
        if not c:
            c = {"ssid": f"DIRECT-PS-{settings['pc_id'][:4].upper()}", "pass": secrets.token_urlsafe(12)}
            settings["wfd_network"] = c
        # Hosting the network is a Windows feature (WiFiDirectAdvertisementPublisher): elsewhere phones never get it
        self.core.hub.wfd_creds = dict(c, prefer=bool(settings["wfd_prefer"])) if settings["wifi_direct"] and IS_WIN else None
        return c

    def _want_wfd(self) -> bool:
        if not settings["wifi_direct"] or not IS_WIN:
            return False
        hub = self.core.hub
        if hub.pair_secret:
            return True
        if settings["wfd_prefer"] and any(settings.device(d) and settings.device(d).get("wfd_capable") for d in hub.phones):
            return True                   # the preferred link: always up for phones that can use it
        now = time.time()
        for dev_id, p in hub.phones.items():
            if not p.connected and now - self.near.get(dev_id, 0) < 60:
                return True               # the phone is right here but has no LAN path to us
        return False

    async def _manage_wfd(self):
        self._wfd_creds()
        want = self._want_wfd()
        if want and self._publisher is None and self.wfd_state != "unavailable":
            self._start_wfd()
        elif not want and self._publisher is not None:
            self.stop_wfd()

    def _start_wfd(self):
        try:
            from winrt.windows.devices.wifidirect import (WiFiDirectAdvertisementPublisher,
                                                          WiFiDirectAdvertisementListenStateDiscoverability)
            from winrt.windows.security.credentials import PasswordCredential
            c = self._wfd_creds()
            p = WiFiDirectAdvertisementPublisher()
            adv = p.advertisement
            adv.listen_state_discoverability = WiFiDirectAdvertisementListenStateDiscoverability.NORMAL
            adv.is_autonomous_group_owner_enabled = True
            ls = adv.legacy_settings
            ls.is_enabled = True
            ls.ssid = c["ssid"]
            cred = PasswordCredential()
            cred.password = c["pass"]
            ls.passphrase = cred
            p.start()
            self._publisher = p
            self.wfd_state = "on"
            log.info("Wi-Fi Direct network %s up", c["ssid"])
            self._start_beacon()
        except Exception as e:
            self.wfd_state, self.wfd_error = "unavailable", str(e)
            log.info("Wi-Fi Direct unavailable: %s", e)

    def _start_beacon(self):
        try:
            from winrt.windows.devices.bluetooth.advertisement import (BluetoothLEAdvertisementPublisher,
                                                                       BluetoothLEManufacturerData)
            from winrt.windows.storage.streams import DataWriter
            w = DataWriter()
            w.write_bytes(BEACON_MAGIC + _hash6(settings["pc_id"]))
            md = BluetoothLEManufacturerData()
            md.company_id = BEACON_COMPANY
            md.data = w.detach_buffer()
            b = BluetoothLEAdvertisementPublisher()
            b.advertisement.manufacturer_data.append(md)
            b.start()
            self._beacon = b
            log.info("BLE beacon on")
        except Exception as e:
            log.info("BLE beacon unavailable: %s", e)

    def _stop_beacon(self):
        if self._beacon is not None:
            try:
                self._beacon.stop()
            except Exception:
                pass
            self._beacon = None

    def stop_wfd(self):
        self._stop_beacon()
        if self._publisher is not None:
            try:
                self._publisher.stop()
            except Exception:
                pass
            self._publisher = None
            self.wfd_state = "off"
            log.info("Wi-Fi Direct network down")

    def stop(self):
        self.stop_wfd()
        if self._scanner:
            asyncio.ensure_future(self._stop_ble())
