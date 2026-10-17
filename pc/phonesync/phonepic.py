"""A picture of the actual phone, used in the app (sidebar, status page) and as the Explorer / Send to icon.

In order of preference: a picture the user chose; a photo of the phone's model from Wikimedia Commons (freely licensed,
auto-cropped to the phone, with the phone's own wallpaper put on its screen); a drawn modern phone around the
wallpaper (or an accent-colored one until the wallpaper is known)."""
from __future__ import annotations

import hashlib
import io
import json
import logging
import re
import time
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from .config import ICON_DIR

log = logging.getLogger("pic")
ASPECT = 0.468                     # body width / height of a current flagship (20:9 screen, slim bezels)
SS = 4                             # supersampling for smooth edges
_cache: dict[tuple, bytes] = {}


UA = "Symphone/2.0 (personal Windows-Android sync app; device picture lookup)"
COMMONS = "https://commons.wikimedia.org/w/api.php"


def custom_path(dev: str) -> Path:
    return ICON_DIR / f"custom-{dev}.png"


def model_path(dev: str) -> Path:
    return ICON_DIR / f"model-{dev}.png"


def model_info(dev: str) -> dict:
    try:
        return json.loads((ICON_DIR / f"model-{dev}.json").read_text("utf-8"))
    except (OSError, ValueError):
        return {}


def set_custom(dev: str, data: bytes):
    img = Image.open(io.BytesIO(data))
    img.load()
    img = img.convert("RGBA")
    img.thumbnail((900, 900), Image.LANCZOS)
    img.save(custom_path(dev), "PNG")
    _cache.clear()


def clear_custom(dev: str):
    try:
        custom_path(dev).unlink()
    except OSError:
        pass
    _cache.clear()


def source(dev: str) -> dict:
    """What the picture is, for the UI (and the photo credit)."""
    if custom_path(dev).exists():
        return {"kind": "custom"}
    info = model_info(dev)
    if model_path(dev).exists() and info.get("title"):
        return {"kind": "model", **{k: info.get(k) for k in ("title", "author", "license", "page")}}
    return {"kind": "drawn"}


def _queries(name: str, model: str, brand: str = "") -> list[str]:
    out = []
    n = re.sub(r"^.*?['\u2019]s\s+", "", name or "").strip()          # "Ann's Pixel 9 Pro" -> "Pixel 9 Pro"
    if n and n.lower() not in ("phone", "android phone"):
        out.append(n)
    if model:
        out.append(f"{brand} {model}".strip())
    return out


def _get(url: str, params: dict | None = None) -> bytes:
    import urllib.parse
    import urllib.request
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read()


async def fetch_model_photo(dev: str, name: str, model: str, brand: str = "", max_age=14 * 86400) -> bool:
    """Looks the phone's model up on Wikimedia Commons (free photos) and keeps a cut-out of it. True if found."""
    import asyncio
    info = model_info(dev)
    if info.get("checked", 0) > time.time() - max_age and info.get("query_for") == [name, model]:
        return model_path(dev).exists()
    return await asyncio.to_thread(_lookup, dev, name, model, brand)


def _lookup(dev: str, name: str, model: str, brand: str) -> bool:
    found = None
    for q in _queries(name, model, brand):
        words = [w.lower() for w in re.findall(r"\w+", q)]
        hits = json.loads(_get(COMMONS, {"action": "query", "list": "search", "srsearch": q, "srnamespace": "6",
                                         "format": "json", "srlimit": "20"})).get("query", {}).get("search", [])
        titles = [h["title"] for h in hits if re.search(r"\.(jpe?g|png|webp)$", h["title"], re.I)
                  and all(w in h["title"].lower() for w in words)]
        # a front view shows a phone best; back/box/detail shots last
        titles.sort(key=lambda t: (0 if "front" in t.lower() else 2 if re.search(r"back|rear|box|camera", t, re.I)
                                   else 1, len(t)))
        for title in titles[:4]:
            pages = json.loads(_get(COMMONS, {"action": "query", "titles": title, "prop": "imageinfo",
                                              "iiprop": "url|extmetadata", "iiurlwidth": "960",
                                              "format": "json"})).get("query", {}).get("pages", {})
            ii = next(iter(pages.values()), {}).get("imageinfo", [{}])[0]
            url = ii.get("thumburl") or ii.get("url")
            if not url:
                continue
            try:
                res = _isolate(Image.open(io.BytesIO(_get(url))))
            except Exception as e:
                log.debug("isolate %s: %s", title, e)
                res = None
            if res is None:
                continue
            meta = ii.get("extmetadata", {})
            artist = re.sub(r"<[^>]+>", "", meta.get("Artist", {}).get("value", "")).strip()
            found = (res, {"title": title[5:].rsplit(".", 1)[0], "author": artist,
                           "license": meta.get("LicenseShortName", {}).get("value", ""),
                           "page": ii.get("descriptionurl", "")})
            break
        if found:
            break
    rec = {"checked": time.time(), "query_for": [name, model]}
    if found:
        (img, screen), meta = found
        img.save(model_path(dev), "PNG")
        rec.update(meta, screen=list(screen))
        log.info("phone picture: %s (%s, %s)", meta["title"], meta["author"], meta["license"])
    (ICON_DIR / f"model-{dev}.json").write_text(json.dumps(rec), "utf-8")
    _cache.clear()
    return bool(found)


def _isolate(img: Image.Image):
    """Crops a photo/render of a phone to the phone itself, with rounded corners on transparency: colors seen along
    the photo's border are background; the phone is the dense block of other colors in the middle. Returns
    (phone RGBA, screen box inside it) or None."""
    img = img.convert("RGB")
    small = img.copy()
    small.thumbnail((400, 400))
    w, h = small.size
    q = small.filter(ImageFilter.MedianFilter(3)).quantize(colors=48, method=Image.Quantize.MEDIANCUT)
    p = q.load()
    border = {}
    for x in range(w):
        for y in (0, 1, 2, h - 3, h - 2, h - 1):
            border[p[x, y]] = border.get(p[x, y], 0) + 1
    for y in range(h):
        for x in (0, 1, 2, w - 3, w - 2, w - 1):
            border[p[x, y]] = border.get(p[x, y], 0) + 1
    total = sum(border.values())
    bgset = {c for c, n in border.items() if n > total * 0.004}
    fg = [[p[x, y] not in bgset for x in range(w)] for y in range(h)]
    rows = [sum(r) for r in fg]
    cols = [sum(fg[y][x] for y in range(h)) for x in range(w)]
    ys = [y for y, n in enumerate(rows) if n > max(rows) * 0.5]
    xs = [x for x, n in enumerate(cols) if n > max(cols) * 0.5]
    if not xs or not ys:
        return None
    x0, x1, y0, y1 = xs[0], xs[-1], ys[0], ys[-1]
    if (x1 - x0) < w * 0.15 or (y1 - y0) < h * 0.25:
        return None
    sx, sy = img.width / w, img.height / h
    box = [round(x0 * sx), round(y0 * sy), round((x1 + 1) * sx), round((y1 + 1) * sy)]
    screen = tuple(box)
    # the block found is often just the lit screen: grow over the dark frame/bezel around it
    hsv = img.convert("HSV")
    gp = hsv.load()
    limit = round((box[2] - box[0]) * 0.08)

    def dark_line(coords):
        # phone body: neutral metal/glass (low saturation, highlights included) or dark, unlike most backgrounds
        vals = [gp[x, y] for x, y in coords]
        return sum(1 for _h, sat, v in vals if sat < 50 or v < 70) > len(vals) * 0.6
    def grow(side):
        """Outer edge of the dark band just outside `side` (0 left, 1 top, 2 right, 3 bottom), or unchanged."""
        seen, last = False, None
        for i in range(1, limit + 1):
            if side == 0:
                x = box[0] - i; line = [(x, y) for y in range(box[1], box[3], 3)] if x >= 0 else None
            elif side == 2:
                x = box[2] - 1 + i; line = [(x, y) for y in range(box[1], box[3], 3)] if x < img.width else None
            elif side == 1:
                y = box[1] - i; line = [(x, y) for x in range(box[0], box[2], 3)] if y >= 0 else None
            else:
                y = box[3] - 1 + i; line = [(x, y) for x in range(box[0], box[2], 3)] if y < img.height else None
            if line is None:
                break
            if dark_line(line):
                seen, last = True, i
            elif seen:
                break
        return last or 0
    g = [grow(k) for k in range(4)]
    box = [box[0] - g[0], box[1] - g[1], box[2] + g[2], box[3] + g[3]]
    box = tuple(box)
    crop = img.crop(box).convert("RGBA")
    cw, ch = crop.size
    r = round(min(cw, ch) * 0.09)
    SS = 4
    mk = Image.new("L", (cw * SS, ch * SS), 0)
    ImageDraw.Draw(mk).rounded_rectangle((0, 0, cw * SS - 1, ch * SS - 1), r * SS, fill=255)
    crop.putalpha(mk.resize((cw, ch), Image.LANCZOS))
    return crop, (screen[0] - box[0], screen[1] - box[1], screen[2] - box[0], screen[3] - box[1])




def _photo(dev: str, height: int) -> Image.Image | None:
    """The user's picture, or the model photo with the phone's wallpaper on its screen, `height` px tall."""
    if custom_path(dev).exists():
        img = Image.open(custom_path(dev)).convert("RGBA")
    elif model_path(dev).exists():
        img = Image.open(model_path(dev)).convert("RGBA")
        scr = model_info(dev).get("screen")
        wp = wallpaper_path(dev)
        if scr and wp.exists():
            x0, y0, x1, y1 = scr
            sw, sh = x1 - x0, y1 - y0
            try:
                wall = _cover(Image.open(wp).convert("RGB"), sw, sh).convert("RGBA")
                img.paste(wall, (x0, y0), _rrect_mask((sw, sh), (0, 0, sw - 1, sh - 1), round(sw * 0.07)))
            except Exception as e:
                log.debug("wallpaper on photo: %s", e)
    else:
        return None
    w = max(1, round(img.width * height / img.height))
    return img.resize((w, height), Image.LANCZOS)


def wallpaper_path(dev: str) -> Path:
    return ICON_DIR / f"wall-{dev}.jpg"


async def refresh_wallpaper(phone, max_age=6 * 3600) -> bool:
    """Fetches the phone's home-screen wallpaper (phone app 2.2+). True if it changed."""
    p = wallpaper_path(phone.dev_id)
    if p.exists() and time.time() - p.stat().st_mtime < max_age:
        return False
    try:
        data = await phone.get_bytes("/api/wallpaper?w=540", timeout=15)
    except Exception as e:
        log.debug("wallpaper %s: %s", phone.name, e)
        if p.exists():
            p.touch()                      # keep the old one, retry later
        return False
    old = p.read_bytes() if p.exists() else b""
    p.write_bytes(data)
    if old != data:
        _cache.clear()
        return True
    return False


def _accent_screen(w: int, h: int, accent: str) -> Image.Image:
    """Fallback 'wallpaper': a soft diagonal gradient in the Windows accent color."""
    a = tuple(int(accent.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4))
    top = tuple(min(255, int(c * 0.55 + 255 * 0.45)) for c in a)
    bot = tuple(int(c * 0.35) for c in a)
    g = Image.new("RGB", (1, 256))
    for y in range(256):
        t = y / 255
        g.putpixel((0, y), tuple(int(top[i] * (1 - t) + bot[i] * t) for i in range(3)))
    img = g.resize((w, h), Image.BILINEAR)
    # a few large blurred blobs read as "wallpaper", not as a flat block
    blob = Image.new("L", (w, h), 0)
    d = ImageDraw.Draw(blob)
    d.ellipse((-w * 0.3, h * 0.05, w * 0.9, h * 0.55), fill=120)
    d.ellipse((w * 0.25, h * 0.55, w * 1.4, h * 1.1), fill=90)
    blob = blob.filter(ImageFilter.GaussianBlur(w * 0.18))
    light = Image.new("RGB", (w, h), tuple(min(255, c + 70) for c in top))
    return Image.composite(light, img, blob)


def _cover(img: Image.Image, w: int, h: int) -> Image.Image:
    s = max(w / img.width, h / img.height)
    img = img.resize((max(w, round(img.width * s)), max(h, round(img.height * s))), Image.LANCZOS)
    x, y = (img.width - w) // 2, (img.height - h) // 2
    return img.crop((x, y, x + w, y + h))


def _rrect_mask(size, box, r):
    m = Image.new("L", size, 0)
    ImageDraw.Draw(m).rounded_rectangle(box, r, fill=255)
    return m


def render(dev: str, height: int, accent: str = "#0078d4", clock: bool = True) -> Image.Image:
    """RGBA picture of the phone, `height` px tall."""
    if dev:
        try:
            photo = _photo(dev, height)
            if photo is not None:
                return photo
        except Exception as e:
            log.debug("photo: %s", e)
    return _drawn(dev, height, accent, clock)


def _drawn(dev: str, height: int, accent: str, clock: bool) -> Image.Image:
    """A drawn modern phone (body plus side buttons) around the wallpaper."""
    H = height * SS
    W = round(H * ASPECT)
    btn = max(2, round(W * 0.014))           # side buttons stick out this much on the right
    img = Image.new("RGBA", (W + btn, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    r = W * 0.135
    # side buttons (volume, power)
    for y0, y1 in ((0.20, 0.28), (0.33, 0.38)):
        d.rounded_rectangle((W - btn * 2, H * y0, W + btn - 1, H * y1), btn, fill=(70, 72, 78, 255))
    # metal rim, then the black glass with the screen inside
    d.rounded_rectangle((0, 0, W - 1, H - 1), r, fill=(92, 95, 104, 255))
    rim = max(1, round(W * 0.012))
    d.rounded_rectangle((rim, rim, W - 1 - rim, H - 1 - rim), r - rim, fill=(12, 13, 15, 255))
    bezel = round(W * 0.038)
    sx0, sy0, sx1, sy1 = bezel, bezel, W - bezel, H - bezel
    sw, sh = sx1 - sx0, sy1 - sy0
    wp = wallpaper_path(dev)
    screen = None
    if dev and wp.exists():
        try:
            screen = _cover(Image.open(wp).convert("RGB"), sw, sh)
        except Exception:
            screen = None
    if screen is None:
        screen = _accent_screen(sw, sh, accent)
    screen = screen.convert("RGBA")
    sd = ImageDraw.Draw(screen)
    if clock and height >= 96:
        # a lock-screen style clock makes it read as a phone at a glance
        try:
            f = ImageFont.truetype("segoeuisl.ttf", round(sw * 0.24))
            t = time.strftime("%H:%M")
            tw = sd.textlength(t, font=f)
            sd.text(((sw - tw) / 2 + 2, sh * 0.13 + 2), t, font=f, fill=(0, 0, 0, 60))
            sd.text(((sw - tw) / 2, sh * 0.13), t, font=f, fill=(255, 255, 255, 235))
        except OSError:
            pass
    # gentle glass reflection
    glare = Image.new("L", (sw, sh), 0)
    ImageDraw.Draw(glare).polygon([(0, 0), (sw * 0.62, 0), (0, sh * 0.42)], fill=28)
    glare = glare.filter(ImageFilter.GaussianBlur(sw * 0.04))
    screen = Image.composite(Image.new("RGBA", (sw, sh), (255, 255, 255, 255)), screen, glare)
    img.paste(screen, (sx0, sy0), _rrect_mask((sw, sh), (0, 0, sw - 1, sh - 1), r - bezel))
    # punch-hole camera
    cam = W * 0.05
    cx, cy = W / 2, sy0 + sw * 0.055
    d.ellipse((cx - cam / 2, cy - cam / 2, cx + cam / 2, cy + cam / 2), fill=(6, 6, 8, 255))
    return img.resize(((W + btn) // SS, height), Image.LANCZOS)


def _sig(dev: str) -> str:
    return "|".join(str(p.stat().st_mtime) if dev and p.exists() else "-" for p in (custom_path(dev), model_path(dev)))


def png(dev: str, height: int, accent: str = "#0078d4") -> bytes:
    wp = wallpaper_path(dev)
    key = (dev, height, accent, wp.stat().st_mtime if dev and wp.exists() else 0, time.strftime("%H:%M"), _sig(dev))
    if key not in _cache:
        buf = io.BytesIO()
        render(dev, height, accent).save(buf, "PNG", optimize=True)
        if len(_cache) > 64:
            _cache.clear()
        _cache[key] = buf.getvalue()
    return _cache[key]


def ico(dev: str, accent: str = "#0078d4") -> Path:
    """Square multi-size .ico of the phone (Explorer, Send to). Rewritten only when the picture changes."""
    wp = wallpaper_path(dev)
    sig = hashlib.sha1(f"{accent}|{wp.stat().st_mtime if dev and wp.exists() else 0}|{_sig(dev)}".encode()).hexdigest()[:10]
    path = ICON_DIR / f"phone-{dev or 'default'}-{sig}.ico"
    if path.exists():
        return path
    for old in ICON_DIR.glob(f"phone-{dev or 'default'}-*.ico"):
        try:
            old.unlink()
        except OSError:
            pass
    frames = []
    for size in (256, 64, 48, 32, 24, 16):
        pic = render(dev, size if size >= 32 else size + 1, accent, clock=False)
        sq = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        if pic.height > size:
            pic = pic.resize((round(pic.width * size / pic.height), size), Image.LANCZOS)
        sq.paste(pic, ((size - pic.width) // 2, (size - pic.height) // 2), pic)
        frames.append(sq)
    frames[0].save(path, format="ICO", sizes=[(f.width, f.height) for f in frames], append_images=frames[1:])
    return path
