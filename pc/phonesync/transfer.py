"""
Multipath transfer engine.

Large files are split into slices that are pulled/pushed concurrently over every healthy link
(USB adb-forward and Wi-Fi). Faster links naturally take more slices (work stealing), and a slice
that fails on one link is retried on another, so a cable pull mid-copy doesn't abort the transfer.
"""
import asyncio
import contextvars
import itertools
import logging
import os
import secrets
import threading
import time
from pathlib import Path
from urllib.parse import quote

import aiohttp

log = logging.getLogger("xfer")

SLICE = 6 << 20            # bytes per slice
MULTIPATH_MIN = 12 << 20   # below this a single request is faster
WORKERS_PER_LINK = {"usb": 2, "wifi": 3}

_ids = itertools.count(1)
_phone = contextvars.ContextVar("phone", default=None)      # per-call phone override (else the active phone)
_batch = contextvars.ContextVar("batch", default=None)


class Batch:
    """A user-visible job (one or many files) shown as a single native Windows toast with a progress bar."""

    def __init__(self, mgr, title: str, direction: str, phone_name: str):
        self.mgr = mgr
        self.title = title
        self.direction = direction
        self.phone_name = phone_name
        self.items: list["Transfer"] = []
        self.files_total = 0
        self.errors: list[str] = []
        self.started = time.time()
        self.toast = None
        self._task = None

    @property
    def size(self):
        return sum(t.size for t in self.items)

    @property
    def done(self):
        return sum(t.done for t in self.items)

    def status(self):
        el = max(0.001, time.time() - self.started)
        n_done = sum(1 for t in self.items if t.state != "running")
        speed = self.done / el
        return {"title": self.title, "progress": (self.done / self.size) if self.size else 0.0,
                "status": (f"{n_done} of {max(self.files_total, len(self.items))} · " if max(self.files_total, len(self.items)) > 1 else "") + f"{_fmt(speed)}/s",
                "caption": None}

    async def __aenter__(self):
        self._tok = _batch.set(self)
        notifier = self.mgr.notifier
        if notifier:
            async def ticker():
                await asyncio.sleep(0.8)            # quick single files never show a progress toast
                while True:
                    if self.toast is None:
                        self.toast = notifier.progress(self.status())
                    else:
                        notifier.progress_update(self.toast, self.status())
                    await asyncio.sleep(0.7)
            self._task = asyncio.create_task(ticker())
        return self

    async def __aexit__(self, et, e, tb):
        _batch.reset(self._tok)
        if self._task:
            self._task.cancel()
        if self.mgr.notifier and self.toast is not None:
            self.mgr.notifier.progress_done(self.toast)
        return False


def _fmt(n):
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {u}" if u in ("B", "KB") else f"{n:.1f} {u}"
        n /= 1024
    return f"{n:.1f} TB"


class Transfer:
    def __init__(self, name, direction, size):
        self.id = next(_ids)
        self.name = name
        self.direction = direction      # "down" | "up"
        self.size = size or 0
        self.done = 0
        self.state = "running"          # running | done | error
        self.error = ""
        self.started = time.time()
        self.finished = 0.0
        self.per_link: dict[str, int] = {}

    def add(self, n, link=None):
        self.done += n
        if link:
            self.per_link[link] = self.per_link.get(link, 0) + n

    def to_json(self):
        el = (self.finished or time.time()) - self.started
        return {"id": self.id, "name": self.name, "dir": self.direction, "size": self.size, "done": self.done,
                "state": self.state, "error": self.error, "speed": self.done / el if el > 0.2 else 0,
                "links": self.per_link}


class TransferManager:
    def __init__(self, phone):
        self._default_phone = phone
        self.items: list[Transfer] = []
        self.on_update = None
        self.notifier = None

    @property
    def phone(self):
        return _phone.get() or self._default_phone

    def batch(self, title, direction, phone_name):
        return Batch(self, title, direction, phone_name)

    def _new(self, name, direction, size):
        t = Transfer(name, direction, size)
        b = _batch.get()
        if b:
            b.items.append(t)
        self.items.append(t)
        self.items = self.items[-200:]
        return t

    def _finish(self, t, err=None):
        t.finished = time.time()
        t.state = "error" if err else "done"
        t.error = str(err or "")
        if self.on_update:
            self.on_update()

    def active(self):
        return [t.to_json() for t in self.items if t.state == "running" or time.time() - t.finished < 8]

    # ------------------------------------------------------------------ download
    async def head(self, url_path) -> tuple[int, float]:
        async with await self.phone.request("HEAD", url_path, timeout=aiohttp.ClientTimeout(total=15)) as r:
            r.raise_for_status()
            size = int(r.headers.get("Content-Length", 0))
            lm = r.headers.get("Last-Modified")
        mtime = 0.0
        if lm:
            from email.utils import parsedate_to_datetime
            mtime = parsedate_to_datetime(lm).timestamp()
        return size, mtime

    async def download(self, url_path: str, dest: Path, name=None, size=None, mtime=None, t: Transfer | None = None,
                       phone=None, toast=True):
        if phone is not None:
            tok = _phone.set(phone)
            try:
                return await self.download(url_path, dest, name, size, mtime, t, toast=toast)
            finally:
                _phone.reset(tok)
        if toast and _batch.get() is None:
            async with self.batch(f"Receiving {name or Path(dest).name}", "down", self.phone.name):
                return await self.download(url_path, dest, name, size, mtime, t, toast=False)
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if size is None or mtime is None:
            s, m = await self.head(url_path)
            size = s if size is None else size
            mtime = m if mtime is None else mtime
        t = t or self._new(name or dest.name, "down", size)
        t.size = size
        part = dest.with_name(dest.name + ".phonesync-part")
        try:
            with _Sink(part, size) as sink:
                if size < MULTIPATH_MIN or len(self.phone.links) < 2:
                    await self._get_range(url_path, sink, 0, size, t, self.phone.best(), whole=True)
                else:
                    await self._multipath_get(url_path, sink, size, t)
            os.replace(part, dest)
            if mtime:
                os.utime(dest, (mtime, mtime))
            self._finish(t)
            return dest
        except BaseException as e:
            self._finish(t, e)
            try:
                part.unlink()
            except OSError:
                pass
            raise

    async def _get_range(self, url_path, sink, start, length, t, link, whole=False):
        headers = {} if whole else {"Range": f"bytes={start}-{start + length - 1}"}
        async with await self.phone.request("GET", url_path, link=link, headers=headers,
                                             timeout=aiohttp.ClientTimeout(total=None, sock_read=30)) as r:
            if r.status not in (200, 206):
                raise IOError(f"HTTP {r.status}")
            pos = start
            buf = bytearray()
            async for chunk in r.content.iter_chunked(1 << 20):
                buf += chunk
                t.add(len(chunk), link.name)
                if len(buf) >= 4 << 20:
                    await sink.write(pos, bytes(buf))
                    pos += len(buf)
                    buf.clear()
            if buf:
                await sink.write(pos, bytes(buf))
                pos += len(buf)
            if pos - start != length:
                t.add(-(pos - start), link.name)
                raise IOError(f"short read {pos - start}/{length}")

    async def _multipath_get(self, url_path, sink, size, t):
        queue: asyncio.Queue = asyncio.Queue()
        for off in range(0, size, SLICE):
            queue.put_nowait((off, min(SLICE, size - off)))
        remaining = [queue.qsize()]
        done_evt = asyncio.Event()

        async def worker(link):
            errors = 0
            while not done_evt.is_set():
                try:
                    off, ln = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                try:
                    await self._get_range(url_path, sink, off, ln, t, link)
                    remaining[0] -= 1
                    if remaining[0] == 0:
                        done_evt.set()
                except Exception as e:
                    errors += 1
                    queue.put_nowait((off, ln))
                    log.info("slice %d on %s failed: %s", off, link.name, e)
                    if errors >= 3 or not link.healthy:
                        return
                    await asyncio.sleep(0.3)

        await self._run_workers(worker)
        if remaining[0]:
            raise IOError("transfer failed on all links")

    async def _run_workers(self, worker):
        """Runs workers for every healthy link; re-spawns if links come back while work is pending."""
        tasks = []
        for link in self.phone.links:
            for _ in range(WORKERS_PER_LINK.get(link.kind, 2)):
                tasks.append(asyncio.create_task(worker(link)))
        if not tasks:
            raise ConnectionError("phone not reachable")
        await asyncio.gather(*tasks)

    # ------------------------------------------------------------------ upload
    async def upload(self, src: Path, remote_path: str, t: Transfer | None = None):
        src = Path(src)
        st = src.stat()
        size = st.st_size
        mtime_ms = int(st.st_mtime * 1000)
        t = t or self._new(src.name, "up", size)
        qpath = quote(remote_path)
        try:
            if size < MULTIPATH_MIN:
                data = await asyncio.to_thread(src.read_bytes)
                link = self.phone.best()
                async with await self.phone.request("PUT", f"/api/upload?path={qpath}&mtime={mtime_ms}", link=link,
                                                     data=data, timeout=aiohttp.ClientTimeout(total=300)) as r:
                    if r.status >= 300:
                        raise IOError(f"HTTP {r.status}: {await r.text()}")
                t.add(size, link.name)
            else:
                await self._multipath_put(src, qpath, size, mtime_ms, t)
            self._finish(t)
        except BaseException as e:
            self._finish(t, e)
            raise

    async def _multipath_put(self, src, qpath, size, mtime_ms, t):
        uid = secrets.token_hex(6)
        queue: asyncio.Queue = asyncio.Queue()
        for off in range(0, size, SLICE):
            queue.put_nowait((off, min(SLICE, size - off)))
        remaining = [queue.qsize()]

        async def worker(link):
            errors = 0
            while True:
                try:
                    off, ln = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                try:
                    data = await asyncio.to_thread(_pread, src, off, ln)
                    async with await self.phone.request(
                            "PUT", f"/api/chunk?path={qpath}&id={uid}&offset={off}", link=link, data=data,
                            timeout=aiohttp.ClientTimeout(total=None, sock_read=60)) as r:
                        if r.status >= 300:
                            raise IOError(f"HTTP {r.status}")
                    t.add(ln, link.name)
                    remaining[0] -= 1
                except Exception as e:
                    errors += 1
                    queue.put_nowait((off, ln))
                    log.info("upload slice %d on %s failed: %s", off, link.name, e)
                    if errors >= 3 or not link.healthy:
                        return
                    await asyncio.sleep(0.3)

        await self._run_workers(worker)
        if remaining[0]:
            raise IOError("upload failed on all links")
        async with await self.phone.request("POST", f"/api/commit?path={qpath}&id={uid}&total={size}&mtime={mtime_ms}",
                                             timeout=aiohttp.ClientTimeout(total=60)) as r:
            if r.status >= 300:
                raise IOError(f"commit HTTP {r.status}: {await r.text()}")

    # ------------------------------------------------------------------ bulk helpers
    async def upload_paths(self, paths: list[Path], remote_dir: str, parallel=4, batch=None):
        """Uploads files and folders (recursively) into remote_dir on the phone."""
        jobs = []
        for p in map(Path, paths):
            if p.is_dir():
                for f in p.rglob("*"):
                    if f.is_file():
                        jobs.append((f, f"{remote_dir.rstrip('/')}/{p.name}/{f.relative_to(p).as_posix()}"))
            elif p.is_file():
                jobs.append((p, f"{remote_dir.rstrip('/')}/{p.name}"))
        if batch:
            batch.files_total += len(jobs)
        sem = asyncio.Semaphore(parallel)
        errors = []

        async def one(f, r):
            async with sem:
                try:
                    await self.upload(f, r)
                except Exception as e:
                    errors.append(f"{f.name}: {e}")

        await asyncio.gather(*(one(f, r) for f, r in jobs))
        return len(jobs), errors

    async def send_paths(self, paths: list[Path], remote_dir: str, phone=None) -> dict:
        """User-facing upload (Send to, drag & drop): one native progress toast and a completion toast."""
        tok = _phone.set(phone) if phone is not None else None
        try:
            ph = self.phone
            n_items = len(paths)
            what = Path(paths[0]).name if n_items == 1 else f"{n_items} items"
            async with self.batch(f"Sending {what} to {ph.name}", "up", ph.name) as b:
                n, errors = await self.upload_paths(paths, remote_dir, batch=b)
            if self.notifier:
                self.notifier.transfer_done(b, n, errors, remote_dir=remote_dir)
            return {"count": n, "errors": errors}
        finally:
            if tok is not None:
                _phone.reset(tok)

    async def fetch_paths(self, remote_paths: list[str], dest_dir: Path) -> dict:
        """User-facing download of phone files/folders: one progress toast, no Explorer windows popping up."""
        ph = self.phone
        what = remote_paths[0].rstrip("/").rsplit("/", 1)[-1] if len(remote_paths) == 1 else f"{len(remote_paths)} items"
        total, errors, first = 0, [], None
        async with self.batch(f"Copying {what} from {ph.name}", "down", ph.name) as b:
            for p in remote_paths:
                n, e = await self.download_dav(p, Path(dest_dir), batch=b)
                total += n
                errors += e
                first = first or Path(dest_dir) / p.rstrip("/").rsplit("/", 1)[-1]
        if self.notifier:
            self.notifier.transfer_done(b, total, errors, local=first if len(remote_paths) == 1 else Path(dest_dir))
        return {"count": total, "errors": errors}

    async def download_dav(self, remote_path: str, dest_dir: Path, parallel=4, batch=None):
        """Downloads a phone file or folder (recursively) into dest_dir."""
        dest_dir = Path(dest_dir)
        items = await self._walk(remote_path)
        if batch:
            batch.files_total += len(items)
        base = remote_path.rstrip("/").rsplit("/", 1)[0]
        sem = asyncio.Semaphore(parallel)
        errors = []

        async def one(path, size, mtime):
            async with sem:
                rel = path[len(base):].lstrip("/")
                try:
                    await self.download("/dav" + quote(path), dest_dir / rel, size=size,
                                        mtime=None if mtime is None else mtime / 1000, toast=False)
                except Exception as e:
                    errors.append(f"{rel}: {e}")

        await asyncio.gather(*(one(*i) for i in items))
        return len(items), errors

    async def _walk(self, path):
        try:
            d = await self.phone.get_json(f"/api/ls?path={quote(path)}")
        except aiohttp.ClientResponseError as e:
            if e.status == 404:
                return [(path, None, None)]     # a single file
            raise
        if "items" not in d:
            return []
        out = []
        for it in d["items"]:
            p = f"{path.rstrip('/')}/{it['n']}"
            if it["d"]:
                out += await self._walk(p)
            else:
                out.append((p, it["s"], it["m"]))
        return out


class _Sink:
    """One preallocated file handle shared by all slice writers (reopening per write is slow under AV scanning)."""

    def __init__(self, path, size):
        self.fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_BINARY)
        os.ftruncate(self.fd, size)
        self.lock = threading.Lock()

    def _w(self, pos, data):
        with self.lock:
            os.lseek(self.fd, pos, os.SEEK_SET)
            os.write(self.fd, data)

    async def write(self, pos, data):
        await asyncio.to_thread(self._w, pos, data)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        os.close(self.fd)


def _pread(path, off, ln):
    with open(path, "rb") as f:
        f.seek(off)
        return f.read(ln)
