#!/bin/sh
# Packages the Linux app (pc/build.sh output) as a .deb for Ubuntu / Debian:
#   /opt/symphone (the app), /usr/bin/symphone, a launcher entry and its icon.
#   build-deb.sh <app dir> <version> <output dir>
set -eu
APP=$1
VERSION=$2
OUT=$3
HERE=$(cd "$(dirname "$0")" && pwd)
PKG=$(mktemp -d)
trap 'rm -rf "$PKG"' EXIT

mkdir -p "$PKG/opt/symphone" "$PKG/usr/bin" "$PKG/usr/share/applications" \
    "$PKG/usr/share/icons/hicolor/256x256/apps" "$PKG/DEBIAN"
cp -a "$APP"/. "$PKG/opt/symphone/"
ln -s /opt/symphone/Symphone "$PKG/usr/bin/symphone"
cp "$HERE/symphone.desktop" "$PKG/usr/share/applications/"
cp "$HERE/../../pc/phonesync/ui/icon.png" "$PKG/usr/share/icons/hicolor/256x256/apps/symphone.png"

# Debian versions can't contain "-dev.N" the way tags do: 0.0.0-dev.12 -> 0.0.0~dev.12
DEBVER=$(echo "$VERSION" | sed 's/-/~/')
SIZE=$(du -sk "$PKG" | cut -f1)
cat > "$PKG/DEBIAN/control" <<EOF
Package: symphone
Version: $DEBVER
Architecture: amd64
Maintainer: Symphone <noreply@github.com>
Installed-Size: $SIZE
Section: utils
Priority: optional
Homepage: https://github.com/alex-aprm/symphone
Depends: libc6 (>= 2.35), libxcb-cursor0, libegl1, libgl1, libxkbcommon-x11-0, libnss3, libasound2t64 | libasound2, fuse3, pulseaudio-utils
Recommends: xclip | wl-clipboard, libnotify-bin
Description: Your Android phone on this computer
 Notifications with replies, phone apps in their own windows, photos and files, shared clipboard, audio and the
 phone camera, over Wi-Fi, USB or Bluetooth. Pairs with the Symphone Android app.
EOF
cat > "$PKG/DEBIAN/postinst" <<'EOF'
#!/bin/sh
set -e
command -v update-desktop-database >/dev/null && update-desktop-database -q /usr/share/applications || true
command -v gtk-update-icon-cache >/dev/null && gtk-update-icon-cache -q /usr/share/icons/hicolor || true
EOF
chmod 755 "$PKG/DEBIAN/postinst"
mkdir -p "$OUT"
dpkg-deb --root-owner-group --build "$PKG" "$OUT/symphone_${VERSION}_amd64.deb"
