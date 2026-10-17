"""Builds phonesync/ui/icons.woff2: the UI's icon glyphs for macOS and Linux, where Segoe Fluent Icons isn't installed.

    python tools/make_icon_font.py <MaterialSymbolsRounded[FILL,GRAD,opsz,wght].ttf> <...codepoints>

Takes the matching Material Symbols Rounded glyphs (Apache License 2.0,
https://github.com/google/material-design-icons/tree/master/variablefont) and maps them to the Segoe Fluent Icons
code points the UI uses, so the same markup renders on every platform (style.css lists this font after Segoe).
"""
import pathlib
import sys

from fontTools import subset
from fontTools.ttLib import TTFont
from fontTools.varLib import instancer

OUT = pathlib.Path(__file__).resolve().parents[1] / "phonesync" / "ui" / "icons.woff2"
MAP = {  # Segoe Fluent Icons codepoint -> Material Symbols name
    0xE73E: "check", 0xE896: "download", 0xEC50: "folder_open", 0xE898: "upload", 0xE76C: "chevron_right",
    0xE91B: "image", 0xE8EA: "smartphone", 0xE8B7: "folder", 0xE8A7: "open_in_new", 0xE7F8: "laptop",
    0xE7F4: "desktop_windows", 0xE7BA: "warning", 0xE7AD: "rotate_right", 0xE767: "volume_up", 0xE765: "keyboard",
    0xE72C: "refresh", 0xE722: "photo_camera", 0xE720: "mic", 0xE714: "videocam", 0xE710: "add",
    0xE70D: "expand_more", 0xE702: "bluetooth", 0xE701: "wifi", 0xEDA2: "hard_drive", 0xEB9F: "add_photo_alternate",
    0xEA8F: "notifications", 0xE9CE: "help", 0xE8B4: "screen_rotation", 0xE8AB: "flip", 0xE88E: "usb",
    0xE839: "wifi_tethering", 0xE80F: "home", 0xE7F5: "speaker", 0xE7E8: "power_settings_new", 0xE7B8: "checklist",
    0xE785: "lock_open", 0xE77F: "content_paste", 0xE76B: "chevron_left", 0xE74A: "arrow_upward",
    0xE740: "fullscreen", 0xE72E: "lock", 0xE721: "search", 0xE71E: "zoom_in", 0xE71D: "apps", 0xE717: "call",
    0xE713: "settings", 0xE711: "close",
}


def main(ttf: str, codepoints: str):
    cps = {}
    for line in open(codepoints, encoding="utf-8"):
        n, c = line.split()
        cps[n] = int(c, 16)
    f = instancer.instantiateVariableFont(TTFont(ttf), {"FILL": 0, "wght": 400, "GRAD": 0, "opsz": 24})
    src = f.getBestCmap()
    glyph = {seg: src[cps[name]] for seg, name in MAP.items()}
    opts = subset.Options()
    opts.layout_features = []
    opts.name_IDs = ["*"]
    s = subset.Subsetter(opts)
    s.populate(glyphs=sorted(set(glyph.values())))
    s.subset(f)
    f["cmap"].tables = [t for t in f["cmap"].tables if t.isUnicode()]
    for t in f["cmap"].tables:
        t.cmap = dict(glyph)
    for rec in f["name"].names:
        if rec.nameID in (1, 4, 16):
            rec.string = "Symphone Icons"
        elif rec.nameID == 6:
            rec.string = "SymphoneIcons"
    f.flavor = "woff2"            # needs brotli
    f.save(OUT)
    print(f"{OUT} ({len(glyph)} glyphs)")


if __name__ == "__main__":
    main(*sys.argv[1:3])
