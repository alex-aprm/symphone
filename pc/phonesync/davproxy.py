"""
Local WebDAV endpoint for the Explorer drive (rclone + WinFsp mounts it as a drive letter).

Everything is forwarded to the phone over the best link, except bulk data:
  * large GETs are split into slices fetched in parallel over USB and Wi-Fi and re-assembled in order;
  * large PUTs are cut into slices and uploaded in parallel through the phone's /api/chunk endpoint.
"""
import asyncio
import logging
import re
import secrets
from urllib.parse import unquote, quote

import aiohttp
from aiohttp import web

from .config import DAV_PROXY_PORT, settings
from . import transfer

log = logging.getLogger("dav")

HOP = {"host", "authorization", "connection", "keep-alive", "transfer-encoding", "te", "upgrade",
       "proxy-authorization", "content-length", "accept-encoding"}
WINDOW = 8   # slices in flight per stream


class LinkPool:
    """Hands out link 'seats' (N per link) so slices spread over links in proportion to their speed."""

    def __init__(self, phone):
        self.phone = phone

    def seats(self) -> asyncio.Queue:
        q = asyncio.Queue()
        for link in self.phone.links:
            for _ in range(transfer.WORKERS_PER_LINK.get(link.kind, 2)):
                q.put_nowait(link)
        return q


class DavProxy:
    def __init__(self, phone):
        self.phone = phone
        self.pool = LinkPool(phone)
        self.runner = None

    async def start(self):
        app = web.Application(client_max_size=1 << 40)
        app.router.add_route("*", "/{tail:.*}", self.handle)
        self.runner = web.AppRunner(app, access_log=None)
        await self.runner.setup()
        await web.TCPSite(self.runner, "127.0.0.1", DAV_PROXY_PORT, backlog=256).start()
        log.info("dav proxy on %d", DAV_PROXY_PORT)

    async def handle(self, req: web.Request):
        if req.headers.get("Authorization", "") != f"Bearer {settings['local_secret']}":
            return web.Response(status=401)
        if not self.phone.connected:
            return web.Response(status=503, text="phone offline")
        try:
            size = int(req.headers.get("Content-Length") or 0)
            if req.method == "GET" and len(self.phone.links) > 1:
                return await self.striped_get(req)
            if req.method == "PUT" and size >= transfer.MULTIPATH_MIN and len(self.phone.links) > 1:
                return await self.striped_put(req, size)
            return await self.forward(req)
        except (ConnectionError, aiohttp.ClientConnectionError, asyncio.TimeoutError) as e:
            log.info("%s %s: %s", req.method, req.path, e)
            return web.Response(status=503, text=str(e))

    # ------------------------------------------------------------ pass-through
    async def forward(self, req: web.Request):
        headers = {k: v for k, v in req.headers.items() if k.lower() not in HOP}
        body = req.content if req.body_exists else None
        if body is not None and req.content_length is not None:
            headers["Content-Length"] = str(req.content_length)
        link = self.phone.best()
        async with await self.phone.request(req.method, req.raw_path, link=link, headers=headers, data=body,
                                             allow_redirects=False,
                                             timeout=aiohttp.ClientTimeout(total=None, sock_read=120)) as up:
            out_headers = {k: v for k, v in up.headers.items() if k.lower() not in HOP}
            length = up.headers.get("Content-Length")
            if req.method == "HEAD" or up.status in (204, 304):
                if length:
                    out_headers["Content-Length"] = length
                return web.Response(status=up.status, headers=out_headers)
            resp = web.StreamResponse(status=up.status, headers=out_headers)
            if length is not None:
                resp.content_length = int(length)
            await resp.prepare(req)
            async for chunk in up.content.iter_chunked(1 << 20):
                await resp.write(chunk)
            await resp.write_eof()
            return resp

    # ------------------------------------------------------------ striped GET
    async def striped_get(self, req: web.Request):
        rng = req.headers.get("Range", "")
        m = re.fullmatch(r"bytes=(\d*)-(\d*)", rng.strip()) if rng else None
        if rng and not m:
            return await self.forward(req)
        start = int(m.group(1)) if m and m.group(1) else 0
        if m and not m.group(1):           # suffix range: let the phone handle it
            return await self.forward(req)
        req_end = int(m.group(2)) if m and m.group(2) else None
        slice_ = transfer.SLICE

        first_end = start + slice_ - 1 if req_end is None else min(req_end, start + slice_ - 1)
        link0 = self.phone.best()
        up = await self.phone.request("GET", req.raw_path, link=link0, headers={"Range": f"bytes={start}-{first_end}"},
                                      timeout=aiohttp.ClientTimeout(total=None, sock_read=60))
        if up.status != 206:
            up.release()
            return await self.forward(req)
        total = int(up.headers["Content-Range"].rsplit("/", 1)[1])
        end = total - 1 if req_end is None else min(req_end, total - 1)
        headers = {k: up.headers[k] for k in ("Content-Type", "Last-Modified", "ETag") if k in up.headers}
        headers["Accept-Ranges"] = "bytes"
        status = 200
        if m:
            status = 206
            headers["Content-Range"] = f"bytes {start}-{end}/{total}"
        resp = web.StreamResponse(status=status, headers=headers)
        resp.content_length = end - start + 1
        await resp.prepare(req)

        seats = self.pool.seats()
        pending: list[asyncio.Task] = []
        offsets = list(range(first_end + 1, end + 1, slice_))
        next_i = 0

        async def fetch(off):
            ln = min(slice_, end + 1 - off)
            for attempt in range(4):
                link = await seats.get()
                try:
                    async with await self.phone.request(
                            "GET", req.raw_path, link=link, headers={"Range": f"bytes={off}-{off + ln - 1}"},
                            timeout=aiohttp.ClientTimeout(total=None, sock_read=30)) as r:
                        if r.status != 206:
                            raise IOError(f"HTTP {r.status}")
                        data = await r.read()
                        if len(data) != ln:
                            raise IOError("short slice")
                        return data
                except Exception as e:
                    log.info("slice %d via %s failed: %s", off, link.name, e)
                    if attempt == 3:
                        raise
                finally:
                    if link.healthy:
                        seats.put_nowait(link)
                    elif seats.empty():
                        for l in self.phone.links:
                            seats.put_nowait(l)

        def refill():
            nonlocal next_i
            while next_i < len(offsets) and len(pending) < WINDOW:
                pending.append(asyncio.create_task(fetch(offsets[next_i])))
                next_i += 1

        try:
            refill()
            async with up:
                async for chunk in up.content.iter_chunked(1 << 20):
                    await resp.write(chunk)
            while pending:
                t = pending.pop(0)
                data = await t
                refill()
                await resp.write(data)
            await resp.write_eof()
        finally:
            for t in pending:
                t.cancel()
        return resp

    # ------------------------------------------------------------ striped PUT
    async def striped_put(self, req: web.Request, size: int):
        path = unquote(req.path)
        if not path.startswith("/dav/"):
            return await self.forward(req)
        qpath = quote(path[4:])
        uid = secrets.token_hex(6)
        seats = self.pool.seats()
        slice_ = transfer.SLICE
        sem = asyncio.Semaphore(WINDOW)
        tasks = []

        async def send(off, data):
            try:
                for attempt in range(4):
                    link = await seats.get()
                    try:
                        async with await self.phone.request(
                                "PUT", f"/api/chunk?path={qpath}&id={uid}&offset={off}", link=link, data=data,
                                timeout=aiohttp.ClientTimeout(total=None, sock_read=60)) as r:
                            if r.status >= 300:
                                raise IOError(f"HTTP {r.status}")
                            return
                    except Exception as e:
                        log.info("put slice %d via %s failed: %s", off, link.name, e)
                        if attempt == 3:
                            raise
                    finally:
                        seats.put_nowait(link)
            finally:
                sem.release()

        off = 0
        try:
            while off < size:
                data = await req.content.readexactly(min(slice_, size - off))
                await sem.acquire()
                tasks.append(asyncio.create_task(send(off, data)))
                off += len(data)
            await asyncio.gather(*tasks)
        except Exception:
            for t in tasks:
                t.cancel()
            raise
        mt = req.headers.get("X-OC-Mtime")
        mtime_q = f"&mtime={int(mt) * 1000}" if mt and mt.isdigit() else ""
        async with await self.phone.request("POST", f"/api/commit?path={qpath}&id={uid}&total={size}{mtime_q}",
                                             timeout=aiohttp.ClientTimeout(total=60)) as r:
            if r.status >= 300:
                return web.Response(status=502, text=await r.text())
        return web.Response(status=201, headers={"X-OC-Mtime": "accepted"} if mt else {})
