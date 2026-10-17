"""Push a moving colour-bar NV12 test pattern into the PhoneSync virtual camera at 30 fps.

Usage:  C:\\Repos\\sync\\pc\\.venv\\Scripts\\python.exe vcam\\test_pattern.py [--seconds N]
Uses numpy when available (fast); falls back to pure-Python row templates otherwise.
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "pc"))
from phonesync.vcam import VirtualCamera  # noqa: E402

# 75% colour bars in BT.601 limited-range YUV: white, yellow, cyan, green, magenta, red, blue, black
BARS = [(180, 128, 128), (162, 44, 142), (131, 156, 44), (112, 72, 58),
        (84, 184, 198), (65, 100, 212), (35, 212, 114), (16, 128, 128)]

try:
    import numpy as np
except ImportError:  # pragma: no cover
    np = None


class Pattern:
    def __init__(self, w: int, h: int):
        self.w, self.h = w, h
        bw = w // len(BARS)
        y_row = bytearray(w)
        uv_row = bytearray(w)
        for x in range(w):
            yy, u, v = BARS[min(x // bw, len(BARS) - 1)]
            y_row[x] = yy
            if x % 2 == 0:
                uv_row[x], uv_row[x + 1] = u, v
        # doubled rows so a horizontal scroll is a simple slice
        self.y2 = bytes(y_row) * 2
        self.uv2 = bytes(uv_row) * 2
        if np is not None:
            self.ny = np.frombuffer(self.y2, np.uint8)
            self.nuv = np.frombuffer(self.uv2, np.uint8)
            self.frame = np.empty(w * h * 3 // 2, np.uint8)

    def render(self, n: int):
        w, h = self.w, self.h
        off = (n * 8) % w & ~1
        if np is not None:
            f = self.frame
            y = f[: w * h].reshape(h, w)
            uv = f[w * h:].reshape(h // 2, w)
            y[:] = self.ny[off: off + w]
            uv[:] = self.nuv[off: off + w]
            # moving white box (frame counter visualisation)
            bx = (n * 12) % (w - 160)
            y[h // 2 - 80: h // 2 + 80, bx: bx + 160] = 235
            uv[h // 4 - 40: h // 4 + 40, bx: bx + 160] = 128
            # bottom gray ramp
            y[h - 60:, :] = (np.arange(w) * 219 // w + 16).astype(np.uint8)
            uv[h // 2 - 30:, :] = 128
            return f
        yrow = self.y2[off: off + w]
        uvrow = self.uv2[off: off + w]
        return yrow * h + uvrow * (h // 2)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=0, help="stop after N seconds (0 = forever)")
    ap.add_argument("--always", action="store_true", help="send even when no app is reading")
    args = ap.parse_args()

    cam = VirtualCamera()
    print("waiting for camera shared memory (Global\\PhoneSyncVCam)...", flush=True)
    while not cam.available:
        time.sleep(1)
    print("connected; requested size", cam.requested_size, "consumer_active", cam.consumer_active, flush=True)

    pattern = None
    n = 0
    t0 = time.monotonic()
    next_t = t0
    last_report = t0
    sent = 0
    while not args.seconds or time.monotonic() - t0 < args.seconds:
        w, h = cam.requested_size
        if pattern is None or (pattern.w, pattern.h) != (w, h):
            pattern = Pattern(w, h)
            print(f"rendering {w}x{h} (numpy={'yes' if np is not None else 'no'})", flush=True)
        if args.always or cam.consumer_active:
            cam.write_nv12(pattern.render(n), w, h)
            sent += 1
        n += 1
        now = time.monotonic()
        if now - last_report >= 5:
            print(f"frames sent: {sent}, consumer_active={cam.consumer_active}, counter={cam.frame_counter}", flush=True)
            last_report = now
        next_t += 1 / 30
        delay = next_t - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        else:
            next_t = time.monotonic()
    cam.close()


if __name__ == "__main__":
    main()
