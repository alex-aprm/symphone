"""Connections to paired phones: links (USB forward / Wi-Fi), LAN discovery, REST client and event streams.

Every paired phone gets its own `Phone` (links, health loop, event stream, notifications). `PhoneHub` owns them,
runs the shared LAN discovery and exposes the *active* phone (the one the UI shows) through `ActivePhone`, a proxy
the other subsystems hold on to, so switching phones needs no rewiring.
"""
import asyncio
import hashlib
import hmac
import json
import logging
import secrets
import socket
import time

import aiohttp

from .config import USB_FWD_PORT, PHONE_PORT, UDP_PORT, UI_PORT, settings

log = logging.getLogger("phone")


class Link:
    def __init__(self, name: str, kind: str):
        self.name = name
        self.kind = kind            # "usb" | "wifi"
        self.base: str | None = None
        self.healthy = False
        self.rtt = 0.0
        self.session: aiohttp.ClientSession | None = None
        self.fails = 0

    def ensure_session(self):
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(limit=32, ttl_dns_cache=None, enable_cleanup_closed=True),
                read_bufsize=1 << 20,
                timeout=aiohttp.ClientTimeout(total=None, sock_connect=3, sock_read=60),
            )
        return self.session

    def to_json(self):
        return {"name": self.name, "kind": self.kind, "base": self.base, "healthy": self.healthy,
                "rtt": round(self.rtt * 1000, 1)}


class Phone:
    def __init__(self, dev_id: str, adbm, hub=None):
        self.dev_id = dev_id
        self.adbm = adbm
        self.hub = hub
        self.usb = Link("USB", "usb")
        self.wifi = Link("Wi-Fi", "wifi")
        self.info: dict = {}
        self.notifications: dict[str, dict] = {}
        self.listeners: list = []               # callables(phone, event, data)
        self._state_listeners: list = []
        self._events_task: asyncio.Task | None = None
        self._events_link: Link | None = None
        self._connected = False
        self._dead_ticks = 0
        self._wake = asyncio.Event()
        self._task: asyncio.Task | None = None
        self.since = 0.0                        # time of the last connect

    # ---------- identity ----------
    @property
    def dev(self) -> dict:
        return settings.device(self.dev_id) or {}

    @property
    def token(self) -> str:
        return self.dev.get("token", "")

    @property
    def name(self) -> str:
        return settings.phone_name(self.dev) if self.dev else "Phone"

    def set(self, **fields):
        if self.dev_id:
            settings.upsert_device(self.dev_id, **fields)

    # ---------- state ----------
    @property
    def links(self) -> list[Link]:
        """Healthy links, fastest-latency first."""
        ls = [l for l in (self.usb, self.wifi) if l.healthy]
        return sorted(ls, key=lambda l: (l.kind != "usb", l.rtt))

    @property
    def connected(self) -> bool:
        return bool(self.links) and bool(self.token)

    @property
    def via(self) -> str:
        ls = self.links
        return ls[0].name if ls else ""

    def best(self) -> Link:
        ls = self.links
        if not ls:
            raise ConnectionError(f"{self.name} not reachable")
        return ls[0]

    def headers(self):
        return {"X-Token": self.token}

    def on_state(self, cb):
        self._state_listeners.append(cb)

    def _state_changed(self):
        for cb in self._state_listeners:
            try:
                cb()
            except Exception:
                log.exception("state listener")

    def wake(self):
        """Something hinted the phone is reachable (beacon, hello, BLE): check right away."""
        self._wake.set()

    # ---------- REST ----------
    async def request(self, method, path, link: Link | None = None, **kw):
        link = link or self.best()
        s = link.ensure_session()
        headers = {**self.headers(), **kw.pop("headers", {})}
        return await s.request(method, link.base + path, headers=headers, **kw)

    async def get_json(self, path, timeout=20, **kw):
        last = None
        for link in self.links or [None]:
            if link is None:
                raise ConnectionError(f"{self.name} not reachable")
            try:
                async with await self.request("GET", path, link=link, timeout=aiohttp.ClientTimeout(total=timeout), **kw) as r:
                    r.raise_for_status()
                    return await r.json(content_type=None)
            except (aiohttp.ClientConnectionError, asyncio.TimeoutError) as e:
                last = e
                continue
        raise last or ConnectionError(f"{self.name} not reachable")

    async def post_json(self, path, body=None, timeout=20, method="POST"):
        async with await self.request(method, path, json=body or {}, timeout=aiohttp.ClientTimeout(total=timeout)) as r:
            data = await r.json(content_type=None) if r.content_type == "application/json" else {"ok": r.status < 300}
            if r.status >= 400 and "error" not in data:
                data["error"] = f"HTTP {r.status}"
            return data

    async def get_bytes(self, path, timeout=20) -> bytes:
        async with await self.request("GET", path, timeout=aiohttp.ClientTimeout(total=timeout)) as r:
            r.raise_for_status()
            return await r.read()

    # ---------- link maintenance ----------
    async def _ping(self, link: Link) -> bool:
        if not link.base:
            return False
        s = link.ensure_session()
        t = time.perf_counter()
        try:
            async with s.get(link.base + "/api/ping", timeout=aiohttp.ClientTimeout(total=2)) as r:
                data = await r.json(content_type=None)
            if data.get("id") != self.dev_id:
                return False
            link.rtt = time.perf_counter() - t
            return True
        except Exception:
            return False

    def start(self):
        if not self._task or self._task.done():
            self._task = asyncio.create_task(self.health_loop())

    async def stop(self):
        if self._task:
            self._task.cancel()
        if self._events_task:
            self._events_task.cancel()
        for l in (self.usb, self.wifi):
            l.healthy = False
            if l.session and not l.session.closed:
                await l.session.close()

    async def health_loop(self):
        while True:
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("health %s", self.dev_id)
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), 2 if self.connected else 1.5)
            except asyncio.TimeoutError:
                pass

    async def _tick(self):
        usb_ok = self.adbm.forward_ok and self.adbm.usb_dev == self.dev_id
        self.usb.base = f"http://127.0.0.1:{USB_FWD_PORT}" if usb_ok else None
        ip = self.dev.get("last_ip")
        self.wifi.base = f"http://{ip}:{PHONE_PORT}" if ip else None
        changed = False
        for link in (self.usb, self.wifi):
            ok = await self._ping(link)
            link.fails = 0 if ok else link.fails + 1
            # one missed ping is tolerated to avoid flapping on busy Wi-Fi
            new = ok or (link.healthy and link.fails < 2 and bool(link.base))
            if new != link.healthy:
                link.healthy = new
                changed = True
                log.info("%s: link %s %s", self.name, link.name, "up" if new else "down")
        serial = self.adbm.serial_for(self.dev_id)
        if self.token and not self.links and serial:
            self._dead_ticks += 1
            if self._dead_ticks % 5 == 3:
                log.info("%s: phone app unreachable; restarting its service over adb", self.name)
                await self.adbm.ensure_app(serial)
        else:
            self._dead_ticks = 0
        if self.connected != self._connected:
            self._connected = self.connected
            changed = True
            if self._connected:
                self.since = time.time()
                await self._on_connect()
        self._ensure_events()
        if changed:
            self._state_changed()

    async def _on_connect(self):
        try:
            self.info = await self.get_json("/api/info")
            upd = {"device_name": self.info.get("name", ""), "model": self.info.get("model", "")}
            if self.info.get("wifiIp"):
                upd["last_ip"] = self.info["wifiIp"]
            self.set(**upd)
        except Exception as e:
            log.warning("info: %s", e)
        try:   # v2 phones: register this PC's id and name (and the Wi-Fi Direct fallback network)
            await self.post_json("/api/pc", {"id": settings["pc_id"], "name": settings.pc_name()}, timeout=5)
            if self.hub and self.hub.wfd_creds:
                await self.post_json("/api/pc/wfd", self.hub.wfd_creds, timeout=5)
        except Exception:
            pass

    async def refresh_info(self):
        try:
            self.info = await self.get_json("/api/info", timeout=5)
        except Exception:
            pass
        return self.info

    @property
    def is_master(self) -> bool:
        m = self.info.get("master")
        return not m or m in (settings["pc_id"], "legacy") or self.info.get("you") == m

    # ---------- events (SSE) ----------
    def _ensure_events(self):
        want = self.links[0] if self.connected else None
        if self._events_task and not self._events_task.done():
            # Stay on the current link unless it died; switching USB<->Wi-Fi would only add churn.
            if self._events_link and self._events_link.healthy:
                return
            self._events_task.cancel()
        if want:
            self._events_link = want
            self._events_task = asyncio.create_task(self._events(want))

    async def _events(self, link: Link):
        try:
            s = link.ensure_session()
            async with s.get(link.base + "/api/events", headers=self.headers(),
                             timeout=aiohttp.ClientTimeout(total=None, sock_read=40)) as r:
                if r.status != 200:
                    log.warning("events HTTP %s", r.status)
                    await asyncio.sleep(2)
                    return
                event, data = "message", []
                async for raw in r.content:
                    line = raw.decode("utf-8", "replace").rstrip("\r\n")
                    if not line:
                        if data:
                            self._dispatch(event, "\n".join(data))
                        event, data = "message", []
                    elif line.startswith("event:"):
                        event = line[6:].strip()
                    elif line.startswith("data:"):
                        data.append(line[5:].lstrip())
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.info("events ended: %s", e)

    def _dispatch(self, event, payload):
        try:
            data = json.loads(payload)
        except ValueError:
            data = payload
        if event == "hello" and isinstance(data, dict):
            self.info = {**self.info, **data}
        elif event == "pcs" and isinstance(data, dict):
            self.info = {**self.info, "master": data.get("master"), "pcs": data.get("pcs", [])}
        for cb in self.listeners:
            try:
                res = cb(self, event, data)
                if asyncio.iscoroutine(res):
                    asyncio.create_task(res)
            except Exception:
                log.exception("listener %s", event)

    def to_json(self):
        info = self.info or {}
        d = self.dev
        return {"id": self.dev_id, "name": self.name, "model": info.get("model") or d.get("model", ""),
                "connected": self.connected, "via": self.via, "android": info.get("android", ""),
                "battery": info.get("battery"), "charging": info.get("charging"),
                "locked": info.get("locked"), "screenOn": info.get("screenOn"),
                "storageFree": info.get("storageFree"), "storageTotal": info.get("storageTotal"),
                "ip": d.get("last_ip", ""), "master": self.is_master if self.connected else None,
                "links": [l.to_json() for l in (self.usb, self.wifi)]}


class ActivePhone:
    """Proxy to the hub's active phone (or a disconnected placeholder when none is paired)."""

    def __init__(self, hub):
        object.__setattr__(self, "_hub", hub)

    def __getattr__(self, k):
        return getattr(self._hub.active, k)

    def __setattr__(self, k, v):
        setattr(self._hub.active, k, v)


class PhoneHub:
    def __init__(self, adbm):
        self.adbm = adbm
        self.phones: dict[str, Phone] = {}
        self._none = Phone("", adbm, self)
        self.candidates: dict[str, dict] = {}   # discovered, not paired, by id
        self.listeners: list = []               # (phone, event, data)
        self._state_listeners: list = []
        self.proxy = ActivePhone(self)
        self.pair_secret: tuple[str, float] | None = None
        self.wfd_creds: dict | None = None
        self._started = False

    # ---------- phones ----------
    @property
    def active(self) -> Phone:
        return self.phones.get(settings["active_device"]) or self._none

    def on_state(self, cb):
        self._state_listeners.append(cb)

    def _state_changed(self):
        for cb in self._state_listeners:
            try:
                cb()
            except Exception:
                log.exception("hub state listener")

    def sync_devices(self):
        """Creates/removes Phone objects to match settings["devices"]."""
        want = {d["device_id"] for d in settings.devices() if d.get("device_id") and d.get("token")}
        for dev_id in want - set(self.phones):
            p = Phone(dev_id, self.adbm, self)
            p.listeners.append(self._relay)
            p.on_state(self._state_changed)
            self.phones[dev_id] = p
            if self._started:
                p.start()
        for dev_id in set(self.phones) - want:
            p = self.phones.pop(dev_id)
            asyncio.create_task(p.stop())
        self._state_changed()

    def _relay(self, phone, event, data):
        for cb in self.listeners:
            res = cb(phone, event, data)
            if asyncio.iscoroutine(res):
                asyncio.create_task(res)

    def set_active(self, dev_id: str):
        if dev_id in self.phones and dev_id != settings["active_device"]:
            settings["active_device"] = dev_id
            self._state_changed()
            return True
        return False

    async def forget(self, dev_id: str):
        p = self.phones.get(dev_id)
        if p and p.connected:
            try:
                await p.post_json("/api/pc", method="DELETE", timeout=4)
            except Exception:
                pass
        settings.remove_device(dev_id)
        self.sync_devices()

    async def start(self):
        self._started = True
        self.sync_devices()
        for p in self.phones.values():
            p.start()
        asyncio.create_task(self.discovery_loop())

    # ---------- discovery ----------
    def seen(self, dev_id: str, ip: str, how: str):
        """A paired phone announced itself at `ip` (UDP beacon, hello, BLE)."""
        p = self.phones.get(dev_id)
        if not p:
            return False
        if ip and p.dev.get("last_ip") != ip:
            log.info("%s is at %s (%s)", p.name, ip, how)
            p.set(last_ip=ip)
        if not p.connected:
            p.wake()
        return True

    async def discovery_loop(self):
        loop = asyncio.get_running_loop()
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        try:
            sock.bind(("0.0.0.0", UDP_PORT))
        except OSError:
            sock.bind(("0.0.0.0", 0))
        sock.setblocking(False)

        async def probe():
            while True:
                if any(not p.wifi.healthy for p in self.phones.values()) or not self.phones or self.pair_secret:
                    try:
                        sock.sendto(b"PHONESYNC?", ("255.255.255.255", UDP_PORT))
                    except OSError:
                        pass
                await asyncio.sleep(5)

        asyncio.create_task(probe())
        while True:
            try:
                data, addr = await loop.sock_recvfrom(sock, 4096)
            except OSError:
                await asyncio.sleep(1)
                continue
            if not data.startswith(b"PHONESYNC!"):
                continue
            try:
                info = json.loads(data[10:])
            except ValueError:
                continue
            ip = info.get("ip") or addr[0]
            if not self.seen(info.get("id", ""), ip, "beacon"):
                info["ip"] = ip
                info["seen"] = time.time()
                new = info.get("id") not in self.candidates
                self.candidates[info["id"]] = info
                if new:
                    self._state_changed()

    # ---------- pairing ----------
    async def pair_code(self, ip: str, code: str) -> str | None:
        """Pairs with the 6-digit code shown in the phone app."""
        async with aiohttp.ClientSession() as s:
            async with s.post(f"http://{ip}:{PHONE_PORT}/api/pair",
                              json={"code": code, "pc": {"id": settings["pc_id"], "name": settings.pc_name()}},
                              timeout=aiohttp.ClientTimeout(total=5)) as r:
                if r.status != 200:
                    return await r.text()
                d = await r.json(content_type=None)
        self.add_device(d["id"], d["token"], ip=ip, name=d.get("name", ""))
        return None

    def add_device(self, dev_id: str, token: str, ip: str = "", name: str = "", model: str = ""):
        fields = {"token": token}
        if ip:
            fields["last_ip"] = ip
        if name:
            fields["device_name"] = name
        if model:
            fields["model"] = model
        settings.upsert_device(dev_id, **fields)
        self.candidates.pop(dev_id, None)
        self.sync_devices()
        if dev_id in self.phones:
            self.phones[dev_id].wake()
        log.info("paired %s (%s)", name or dev_id, ip)

    def new_pair_secret(self) -> str:
        s = secrets.token_hex(16)
        self.pair_secret = (s, time.time() + 600)
        return s

    def check_pair_secret(self, s: str) -> bool:
        if not self.pair_secret or time.time() > self.pair_secret[1]:
            return False
        return hmac.compare_digest(s or "", self.pair_secret[0])

    def peer_pair(self, body: dict) -> dict | None:
        """POST /peer/pair from a phone that scanned our QR code."""
        if not self.check_pair_secret(body.get("s", "")):
            return None
        ph = body.get("phone") or {}
        if not ph.get("id") or not ph.get("token"):
            return None
        self.pair_secret = None
        self.add_device(ph["id"], ph["token"], ip=ph.get("ip", ""), name=ph.get("name", ""), model=ph.get("model", ""))
        self.paired_event = ph
        return {"ok": True, "pc": {"id": settings["pc_id"], "name": settings.pc_name()}}

    def peer_hello(self, body: dict, remote_ip: str) -> bool:
        p = self.phones.get(body.get("id", ""))
        if not p or not p.token:
            return False
        try:
            ts = int(body.get("ts", 0))
        except (TypeError, ValueError):
            return False
        if abs(time.time() * 1000 - ts) > 10 * 60_000 and abs(time.time() - ts) > 600:
            return False
        ip = body.get("ip") or remote_ip
        msg = f"{p.dev_id}|{ip}|{body.get('ts')}".encode()
        want = hmac.new(p.token.encode(), msg, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(want, str(body.get("sig", ""))):
            return False
        self.seen(p.dev_id, ip, "hello")
        return True

    def qr_payload(self) -> str:
        from urllib.parse import quote
        s = self.new_pair_secret()
        ips = ",".join(local_ips())
        q = f"phonesync://pair?v=2&pc={settings['pc_id']}&n={quote(settings.pc_name())}&s={s}&a={ips}&p={UI_PORT}"
        if settings["ble"]:
            q += "&b=1"
        if self.wfd_creds:
            q += f"&w={quote(self.wfd_creds['ssid'])}&k={quote(self.wfd_creds['pass'])}"
        return q

    def to_json(self):
        return [p.to_json() for p in self.phones.values()]


def local_ips() -> list[str]:
    """IPv4 addresses of this PC's LAN interfaces (no loopback/link-local), best guess first."""
    ips = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ips.append(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    try:
        for ip in socket.gethostbyname_ex(socket.gethostname())[2]:
            if ip not in ips and not ip.startswith(("127.", "169.254.")):
                ips.append(ip)
    except OSError:
        pass
    return ips[:4]
