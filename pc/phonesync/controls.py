"""Quick settings (the phone's pull-down drawer) over adb: read all states in one shell call, toggle individually."""
import re

from . import adb

# key -> (label, read expression, enable cmd, disable cmd)
TOGGLES = {
    "wifi": ("Wi‑Fi", "settings get global wifi_on", "cmd wifi set-wifi-enabled enabled", "cmd wifi set-wifi-enabled disabled"),
    "bluetooth": ("Bluetooth", "settings get global bluetooth_on", "svc bluetooth enable", "svc bluetooth disable"),
    "data": ("Mobile data", "settings get global mobile_data", "svc data enable", "svc data disable"),
    "airplane": ("Airplane mode", "settings get global airplane_mode_on",
                 "cmd connectivity airplane-mode enable", "cmd connectivity airplane-mode disable"),
    "dnd": ("Do not disturb", "settings get global zen_mode", "cmd notification set_dnd priority", "cmd notification set_dnd off"),
    "location": ("Location", "settings get secure location_mode",
                 "cmd location set-location-enabled true", "cmd location set-location-enabled false"),
    "rotate": ("Auto-rotate", "settings get system accelerometer_rotation",
               "settings put system accelerometer_rotation 1", "settings put system accelerometer_rotation 0"),
    "saver": ("Battery saver", "settings get global low_power", "cmd power set-mode 1", "cmd power set-mode 0"),
    "dark": ("Dark mode", "cmd uimode night | grep -c ': yes'", "cmd uimode night yes", "cmd uimode night no"),
    "nfc": ("NFC", "dumpsys nfc | grep -m1 -c 'mState=on'", "svc nfc enable", "svc nfc disable"),
    "autobright": ("Auto brightness", "settings get system screen_brightness_mode",
                   "settings put system screen_brightness_mode 1", "settings put system screen_brightness_mode 0"),
}
STREAMS = {"media": 3, "ring": 2, "alarm": 4, "call": 0}


async def read_all(serial: str) -> dict:
    parts = [f"echo T_{k}=$({v[1]})" for k, v in TOGGLES.items()]
    parts.append("echo B=$(settings get system screen_brightness)")
    for name, s in STREAMS.items():
        parts.append(f"echo V_{name}=$(cmd media_session volume --stream {s} --get 2>/dev/null | tail -1)")
    out = await adb.shell("; ".join(parts), serial, timeout=10)
    toggles, volumes, brightness = {}, {}, None
    for line in out.splitlines():
        if line.startswith("T_"):
            k, _, v = line[2:].partition("=")
            v = v.strip()
            toggles[k] = {"label": TOGGLES[k][0], "on": v not in ("", "0", "null")}
        elif line.startswith("B="):
            try:
                brightness = int(line[2:])
            except ValueError:
                pass
        elif line.startswith("V_"):
            k, _, v = line[2:].partition("=")
            m = re.search(r"volume is (\d+) in range \[(\d+)\.\.(\d+)\]", v)
            if m:
                volumes[k] = {"value": int(m.group(1)), "min": int(m.group(2)), "max": int(m.group(3))}
    return {"toggles": toggles, "brightness": brightness, "volumes": volumes}


async def set_toggle(serial: str, key: str, on: bool) -> None:
    _, _, en, dis = TOGGLES[key]
    await adb.shell(en if on else dis, serial, timeout=10)


async def set_brightness(serial: str, value: int) -> None:
    value = max(1, min(255, int(value)))
    await adb.shell(f"settings put system screen_brightness_mode 0; settings put system screen_brightness {value}", serial)


async def set_volume(serial: str, stream: str, value: int) -> None:
    await adb.shell(f"cmd media_session volume --stream {STREAMS[stream]} --set {int(value)}", serial)
