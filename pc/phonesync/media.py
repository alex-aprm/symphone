"""Phone photo/video index with live delta updates, a disk thumbnail cache, and auto-import of new shots."""
import asyncio
import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path

from .config import CACHE_DIR, THUMB_DIR, settings

log = logging.getLogger("media")


def _index_file():
    return CACHE_DIR / f"media-{settings['device_id'] or 'none'}.json"


def _thumb_dir():
    d = THUMB_DIR / (settings["device_id"] or "none")
    d.mkdir(parents=True, exist_ok=True)
    return d
COLS = ["id", "name", "mime", "taken", "modified", "size", "w", "h", "bucket", "path", "duration", "orientation", "gen"]


class MediaIndex:
    def __init__(self, core):
        self.core = core
        self.items: dict[int, dict] = {}
        self.gen = 0
        self.device_id = ""
        self.synced = False
        self._lock = asyncio.Lock()
        self._pending = False
        self._thumb_sem = asyncio.Semaphore(12)
        self._inflight: dict[str, asyncio.Future] = {}
        self._load()

    # ---------- persistence ----------
    def reset(self):
        """Active phone changed."""
        self.items, self.gen, self.synced = {}, 0, False
        self._load()

    def _load(self):
        old = CACHE_DIR / "media.json"           # v1 single-phone index
        if old.exists() and not _index_file().exists():
            try:
                os.replace(old, _index_file())
            except OSError:
                pass
        try:
            d = json.loads(_index_file().read_text("utf-8"))
            if d.get("device_id") == settings["device_id"]:
                self.items = {r[0]: dict(zip(COLS, r)) for r in d["rows"]}
                self.gen = d["gen"]
        except Exception:
            pass

    def _save(self):
        rows = [[it[c] for c in COLS] for it in self.items.values()]
        f = _index_file()
        tmp = f.with_suffix(".tmp")
        tmp.write_text(json.dumps({"device_id": settings["device_id"], "gen": self.gen, "rows": rows}), "utf-8")
        os.replace(tmp, f)

    def sorted_rows(self):
        its = sorted(self.items.values(), key=lambda i: (i["taken"], i["id"]), reverse=True)
        return [[it[c] for c in COLS] for it in its]

    # ---------- sync ----------
    async def full_sync(self):
        async with self._lock:
            phone = self.core.phone
            t0 = time.time()
            items, off = {}, 0
            gen = 0
            while True:
                d = await phone.get_json(f"/api/media?offset={off}&limit=5000", timeout=60)
                gen = max(gen, d["gen"])
                for r in d["rows"]:
                    items[r[0]] = dict(zip(COLS, r))
                if len(d["rows"]) < 5000:
                    break
                off += 5000
            added = [i for i in items if i not in self.items]
            removed = [i for i in self.items if i not in items]
            self.items = items
            self.gen = gen
            self.synced = True
            await asyncio.to_thread(self._save)
            log.info("media full sync: %d items in %.1fs", len(items), time.time() - t0)
            if settings["import_gen"] is None:
                settings["import_gen"] = gen   # auto-import starts from "now", not the whole library
            if added or removed:
                self.core.ui_event("media_reset", {})

    async def delta_sync(self):
        if self._lock.locked():
            self._pending = True
            return
        async with self._lock:
            phone = self.core.phone
            d = await phone.get_json(f"/api/media?gen={self.gen}&limit=5000")
            changed, added = [], []
            for r in d["rows"]:
                it = dict(zip(COLS, r))
                if it["id"] not in self.items:
                    added.append(it)
                self.items[it["id"]] = it
                changed.append(it)
            ids = await phone.get_json("/api/media/ids")
            alive = set(ids["ids"])
            removed = [i for i in list(self.items) if i not in alive]
            for i in removed:
                self.items.pop(i, None)
            self.gen = max(self.gen, d["gen"])
            if changed or removed:
                await asyncio.to_thread(self._save)
                self.core.ui_event("media_delta", {"rows": [[it[c] for c in COLS] for it in changed], "removed": removed})
                log.info("media delta: +%d -%d", len(changed), len(removed))
            # Only genuinely new shots: edits/rescans of old items also bump their generation.
            recent = time.time() * 1000 - 2 * 86400_000
            new = [it for it in added if it["gen"] > (settings["import_gen"] or 0) and it["taken"] >= recent]
        if self._pending:
            self._pending = False
            asyncio.create_task(self.delta_sync())
        if new and settings["auto_import"]:
            asyncio.create_task(self.auto_import(new))

    async def on_event(self, event, data):
        if event == "media":
            await asyncio.sleep(0.2)
            await self.delta_sync()
        elif event == "hello":
            if not self.synced:
                await self.full_sync()
            elif data.get("mediaGen", 0) != self.gen:
                await self.delta_sync()

    # ---------- thumbnails ----------
    async def thumb(self, mid: int, size: int) -> bytes:
        it = self.items.get(mid)
        ver = it["modified"] if it else 0
        size = 256 if size <= 256 else 512 if size <= 512 else 1280
        path = _thumb_dir() / f"{mid}_{size}_{ver}.jpg"
        if path.exists():
            return await asyncio.to_thread(path.read_bytes)
        key = path.name
        if key in self._inflight:
            return await asyncio.shield(self._inflight[key])
        fut = asyncio.get_running_loop().create_future()
        self._inflight[key] = fut
        try:
            async with self._thumb_sem:
                data = await self.core.phone.get_bytes(f"/api/thumb/{mid}?s={size}")
            await asyncio.to_thread(path.write_bytes, data)
            fut.set_result(data)
            return data
        except Exception as e:
            fut.set_exception(e)
            fut.exception()
            raise
        finally:
            self._inflight.pop(key, None)

    # ---------- downloads / import ----------
    def local_name(self, it) -> Path:
        dt = datetime.fromtimestamp(it["taken"] / 1000)
        return Path(settings["import_dir"]) / dt.strftime("%Y-%m") / it["name"]

    async def download(self, ids: list[int], dest_dir: str | None = None) -> list[str]:
        out = []
        sem = asyncio.Semaphore(4)

        async def one(mid):
            it = self.items.get(mid)
            if not it:
                return
            dest = Path(dest_dir) / it["name"] if dest_dir else self.local_name(it)
            if dest.exists() and dest.stat().st_size == it["size"]:
                out.append(str(dest))
                return
            async with sem:
                await self.core.transfers.download(f"/api/media/{mid}", dest, name=it["name"], size=it["size"],
                                                   mtime=it["taken"] / 1000, toast=False)
            out.append(str(dest))

        await asyncio.gather(*(one(i) for i in ids), return_exceptions=True)
        return out

    async def auto_import(self, items):
        items = [it for it in items if it["path"].startswith(("DCIM/", "Pictures/"))]
        if not items:
            return
        paths = await self.download([it["id"] for it in items])
        settings["import_gen"] = max(settings["import_gen"] or 0, max(it["gen"] for it in items))
        if paths:
            self.core.notifier.imported([Path(p) for p in paths], settings.phone_name())
