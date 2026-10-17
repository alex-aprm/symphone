"""Gesture engine tests with synthetic input:  .venv\\Scripts\\python.exe -m pytest sidecar\\tests  (or run directly)."""
import math
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "pc"))
from phonesync.touchpad import GestureEngine, Config, VK_MENU, VK_TAB, VK_LWIN, VK_D, VK_CONTROL, VK_SHIFT, VK_LEFT, VK_RIGHT  # noqa: E402


class Rec:
    def __init__(self):
        self.ev = []
        self.mx = self.my = 0.0
        self.wv = self.wh = 0.0

    def move(self, dx, dy):
        self.mx += dx
        self.my += dy
        self.ev.append(("move",))

    def button(self, b, down):
        self.ev.append(("btn", b, down))

    def click(self, b):
        self.ev.append(("click", b))

    def wheel(self, v, h=0):
        self.wv += v
        self.wh += h
        self.ev.append(("wheel",))

    def reset_wheel(self):
        pass

    def key(self, vk, down):
        self.ev.append(("key", vk, down))

    def combo(self, *vks):
        self.ev.append(("combo",) + vks)

    def haptic(self, k):
        self.ev.append(("haptic", k))

    def kinds(self, k):
        return [e for e in self.ev if e[0] == k]


def eng():
    r = Rec()
    return GestureEngine(r, Config()), r


def stroke(e, t0, fingers, dx, dy, dur, steps=None, lift=True, spread=0.0):
    """Moves `fingers` (list of (id, x, y)) by (dx, dy) mm over dur seconds at 120 Hz; spread grows the distance from the centroid."""
    steps = steps or max(int(dur * 120), 1)
    cx = sum(f[1] for f in fingers) / len(fingers)
    cy = sum(f[2] for f in fingers) / len(fingers)
    t = t0
    e.touch(t, [(i, False, x, y) for i, x, y in fingers])
    for s in range(1, steps + 1):
        t = t0 + dur * s / steps
        k = s / steps
        pts = []
        for i, x, y in fingers:
            ox, oy = x - cx, y - cy
            n = math.hypot(ox, oy) or 1
            pts.append((i, False, x + dx * k + ox / n * spread * k, y + dy * k + oy / n * spread * k))
        e.touch(t, pts)
    if lift:
        e.touch(t + 0.001, [(p[0], True, p[2], p[3]) for p in pts])
    return t + 0.001


def test_tap_left_click():
    e, r = eng()
    e.touch(0.0, [(0, False, 50, 30)])
    e.touch(0.05, [(0, False, 50.2, 30.1)])
    e.touch(0.09, [(0, True, 50.2, 30.1)])
    assert r.kinds("click") == [("click", "left")]
    assert ("haptic", "click") in r.ev
    assert r.mx == 0


def test_two_finger_tap_right_click():
    e, r = eng()
    e.touch(0.0, [(0, False, 50, 30)])
    e.touch(0.02, [(0, False, 50, 30), (1, False, 70, 30)])
    e.touch(0.10, [(0, True, 50, 30), (1, False, 70, 30)])
    e.touch(0.12, [(1, True, 70, 30)])
    assert r.kinds("click") == [("click", "right")]


def test_three_finger_tap_middle_click():
    e, r = eng()
    e.touch(0.0, [(0, False, 40, 30), (1, False, 55, 30), (2, False, 70, 30)])
    e.touch(0.1, [(0, True, 40, 30), (1, True, 55, 30), (2, True, 70, 30)])
    assert r.kinds("click") == [("click", "middle")]


def test_long_touch_is_not_tap():
    e, r = eng()
    e.touch(0.0, [(0, False, 50, 30)])
    e.touch(0.5, [(0, True, 50, 30)])
    assert not r.kinds("click")


def test_pointer_move_and_acceleration():
    e, r = eng()
    stroke(e, 0.0, [(0, 20, 30)], 10, 0, 1.0)          # slow: 10 mm/s
    slow = r.mx
    e2, r2 = eng()
    stroke(e2, 0.0, [(0, 20, 30)], 10, 0, 0.025)       # fast: 400 mm/s
    fast = r2.mx
    assert slow > 30 and fast > slow * 4, (slow, fast)
    assert abs(r.my) < 1e-6
    assert not r.kinds("click")


def test_two_finger_scroll_natural_and_inertia():
    e, r = eng()
    t = stroke(e, 0.0, [(0, 40, 40), (1, 60, 40)], 0, -20, 0.12)   # fingers up, fast
    assert r.wv < -150, r.wv                    # content up -> wheel towards the user (negative)
    assert not r.kinds("click")
    before = r.wv
    assert e.inertia
    tt = t
    while e.tick(tt):
        tt += 1 / 120
    assert r.wv < before - 30                   # momentum continued in the same direction
    assert tt - t < 3


def test_horizontal_scroll():
    e, r = eng()
    stroke(e, 0.0, [(0, 40, 30), (1, 40, 50)], -15, 0, 0.3)       # fingers move left
    assert r.wh > 100                           # -> scroll right


def test_pinch_zoom_ctrl_wheel():
    e, r = eng()
    stroke(e, 0.0, [(0, 50, 35), (1, 70, 35)], 0, 0, 0.3, spread=10)
    assert ("key", VK_CONTROL, True) in r.ev and ("key", VK_CONTROL, False) in r.ev
    assert r.wv > 120                           # spread -> zoom in
    assert r.ev.index(("key", VK_CONTROL, False)) > r.ev.index(("key", VK_CONTROL, True))


def test_three_finger_swipes():
    e, r = eng()
    stroke(e, 0.0, [(0, 40, 40), (1, 55, 40), (2, 70, 40)], 0, -20, 0.2)
    assert ("combo", VK_LWIN, VK_TAB) in r.ev
    e, r = eng()
    stroke(e, 0.0, [(0, 40, 20), (1, 55, 20), (2, 70, 20)], 0, 20, 0.2)
    assert ("combo", VK_LWIN, VK_D) in r.ev


def test_three_finger_alt_tab_holds_alt():
    e, r = eng()
    stroke(e, 0.0, [(0, 20, 40), (1, 35, 40), (2, 50, 40)], 60, 0, 0.6)
    assert ("key", VK_MENU, True) in r.ev
    tabs = [x for x in r.ev if x == ("combo", VK_TAB)]
    assert len(tabs) >= 3                       # initial + every 18 mm
    assert r.ev[-1] == ("key", VK_MENU, False) or ("key", VK_MENU, False) in r.ev
    assert r.ev.index(("key", VK_MENU, False)) > r.ev.index(tabs[-1])


def test_four_finger_desktop_switch():
    e, r = eng()
    stroke(e, 0.0, [(0, 60, 40), (1, 70, 40), (2, 80, 40), (3, 90, 40)], -25, 0, 0.2)
    assert ("combo", VK_CONTROL, VK_LWIN, VK_RIGHT) in r.ev
    e, r = eng()
    stroke(e, 0.0, [(0, 20, 40), (1, 30, 40), (2, 40, 40), (3, 50, 40)], 25, 0, 0.2)
    assert ("combo", VK_CONTROL, VK_LWIN, VK_LEFT) in r.ev


def test_force_press_drag():
    e, r = eng()
    e.touch(0.0, [(0, False, 50, 30)])
    e.press(0.1, True, 1)
    assert ("btn", "left", True) in r.ev
    stroke(e, 0.1, [(0, 50, 30)], 20, 0, 0.3, lift=False)
    assert r.mx > 50
    e.press(0.5, False, 1)
    assert ("btn", "left", False) in r.ev
    e.touch(0.6, [(0, True, 70, 30)])
    assert not r.kinds("click")                 # a press is never also a tap


def test_force_press_two_fingers_right_click():
    e, r = eng()
    e.touch(0.0, [(0, False, 50, 30), (1, False, 70, 30)])
    e.press(0.1, True, 2)
    e.press(0.2, False, 2)
    e.touch(0.3, [(0, True, 50, 30), (1, True, 70, 30)])
    assert r.kinds("click") == [("click", "right")]
    assert not r.kinds("btn")


def test_tap_and_drag():
    e, r = eng()
    e.touch(0.0, [(0, False, 50, 30)])
    e.touch(0.08, [(0, True, 50, 30)])
    assert r.kinds("click") == [("click", "left")]
    stroke(e, 0.2, [(1, 50, 30)], 15, 0, 0.3)
    assert ("btn", "left", True) in r.ev and ("btn", "left", False) in r.ev
    assert r.mx > 20


def test_scroll_inertia_with_staggered_lift():
    e, r = eng()
    t = stroke(e, 0.0, [(0, 40, 40), (1, 60, 40)], 0, -20, 0.12, lift=False)
    e.touch(t + 0.01, [(0, True, 40, 20), (1, False, 60, 20)])
    e.touch(t + 0.05, [(1, True, 60, 20)])
    assert e.inertia and not r.kinds("click")


def test_tiny_scroll_is_not_right_click():
    e, r = eng()
    stroke(e, 0.0, [(0, 40, 40), (1, 60, 40)], 0, -2, 0.1)
    assert not r.kinds("click")


def test_reset_releases_everything():
    e, r = eng()
    stroke(e, 0.0, [(0, 20, 40), (1, 35, 40), (2, 50, 40)], 30, 0, 0.3, lift=False)
    e.reset()
    assert ("key", VK_MENU, False) in r.ev


if __name__ == "__main__":
    n = 0
    for k, f in list(globals().items()):
        if k.startswith("test_"):
            f()
            n += 1
            print("ok", k)
    print(n, "passed")
