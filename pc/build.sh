#!/bin/sh
# Builds the macOS / Linux app with the UI, scrcpy (patched server), rclone and the phone app, from the same pieces
# build.ps1 uses on Windows: ../tools/scrcpy-<platform>-v*, ../tools/rclone-*/rclone and ../dist/Symphone.apk.
# PyAV stays out: it only feeds the Windows virtual camera.
#   Linux  dist/Symphone (Symphone executable + tools/)
#   macOS  dist/Symphone.app (tools inside the bundle, ad-hoc signed)
# With a version argument it also packages into ../dist:
#   Linux  Symphone-<ver>-linux-x64.tar.gz and symphone_<ver>_amd64.deb
#   macOS  Symphone-<ver>-macos-arm64.dmg
set -eu
cd "$(dirname "$0")"
VERSION=${1:-}
PY=${PY:-.venv/bin/python}
ROOT=$(cd .. && pwd)
SCRCPY=$(ls -d "$ROOT"/tools/scrcpy-linux-* "$ROOT"/tools/scrcpy-macos-* 2>/dev/null | head -1)
RCLONE=$(ls "$ROOT"/tools/rclone-*/rclone 2>/dev/null | head -1)
APK="$ROOT/dist/Symphone.apk"
[ -n "$SCRCPY" ] && [ -n "$RCLONE" ] || { echo "scrcpy / rclone not found under $ROOT/tools" >&2; exit 1; }

COMMON="--noconfirm --clean --windowed --name Symphone --icon phonesync/ui/icon.png --add-data phonesync/ui:ui
  --collect-all bleak --collect-all desktop_notifier --collect-submodules segno --collect-submodules webview
  --collect-submodules pystray --distpath dist --workpath build"

case "$(uname -s)" in
Darwin)
    # Tools go in through PyInstaller so they are placed and signed like the app's own binaries
    EXTRA="--add-binary $SCRCPY/adb:tools/scrcpy --add-binary $SCRCPY/scrcpy:tools/scrcpy
      --add-data $SCRCPY/scrcpy-server:tools/scrcpy --add-binary $RCLONE:tools/rclone"
    for f in "$SCRCPY"/*.png; do EXTRA="$EXTRA --add-data $f:tools/scrcpy"; done
    [ -f "$APK" ] && EXTRA="$EXTRA --add-data $APK:tools"
    # shellcheck disable=SC2086
    $PY -m PyInstaller $COMMON $EXTRA --osx-bundle-identifier dev.symphone.app \
        --collect-all sounddevice --collect-all _sounddevice_data --exclude-module av Symphone.pyw
    APP=dist/Symphone.app
    PL="$APP/Contents/Info.plist"
    plutil -replace NSBluetoothAlwaysUsageDescription -string "Symphone finds your phone nearby over Bluetooth." "$PL"
    plutil -replace NSMicrophoneUsageDescription -string "Symphone can play your Mac's microphone on the phone." "$PL"
    plutil -replace NSLocalNetworkUsageDescription -string "Symphone connects to your phone over the local network." "$PL"
    plutil -replace NSHighResolutionCapable -bool YES "$PL"
    [ -n "$VERSION" ] && plutil -replace CFBundleShortVersionString -string "$VERSION" "$PL"
    mkdir -p "$APP/Contents/Resources/legal"
    cp "$ROOT/LICENSE" "$ROOT/THIRD_PARTY_NOTICES.md" "$APP/Contents/Resources/legal/"
    codesign --force --deep --sign - "$APP"          # ad-hoc: Info.plist changed after PyInstaller signed it
    echo "Built $APP"
    if [ -n "$VERSION" ]; then
        STAGE=$(mktemp -d)
        cp -R "$APP" "$STAGE/"
        ln -s /Applications "$STAGE/Applications"
        hdiutil create -volname Symphone -srcfolder "$STAGE" -ov -format UDZO "$ROOT/dist/Symphone-$VERSION-macos-arm64.dmg"
        rm -rf "$STAGE"
    fi
    ;;
Linux)
    # shellcheck disable=SC2086
    $PY -m PyInstaller $COMMON --collect-submodules pynput --collect-submodules Xlib --collect-submodules dbus_fast \
        --hidden-import qtpy.QtWebEngineWidgets --hidden-import qtpy.QtWebEngineCore --hidden-import qtpy.QtWebChannel \
        --hidden-import qtpy.QtNetwork --exclude-module PyQt5 --exclude-module PySide2 --exclude-module PySide6 \
        --exclude-module gi --exclude-module av Symphone.pyw
    APP=dist/Symphone
    # Qt's own UI translations (the app is in English; WebEngine keeps its locales), QML, and the Qt libraries the
    # hooks collected that nothing loads: only the ldd closure of the bindings, plugins and WebEngine helper stays
    QT="$APP/_internal/PyQt6/Qt6"
    find "$QT/translations" -maxdepth 1 -name "*.qm" -delete
    rm -rf "$QT/qml"
    NEEDED=$(find "$APP/_internal/PyQt6" "$QT/plugins" "$QT/libexec" -maxdepth 3 -type f \( -name "*.so" -o -perm -u+x \) \
        ! -path "$QT/lib/*" -exec ldd {} \; 2>/dev/null | awk '/=>/ {print $1}' | sort -u)
    for lib in "$QT"/lib/*.so*; do
        echo "$NEEDED" | grep -qx "$(basename "$lib")" || rm -f "$lib"
    done
    mkdir -p "$APP/tools/scrcpy" "$APP/tools/rclone"
    cp -a "$SCRCPY"/. "$APP/tools/scrcpy/"
    cp -a "$RCLONE" "$APP/tools/rclone/"
    rm -f "$APP/tools/scrcpy/scrcpy-server.orig"
    [ -f "$APK" ] && cp "$APK" "$APP/tools/"       # the PC installs/updates the phone app from this copy
    cp "$ROOT/LICENSE" "$ROOT/THIRD_PARTY_NOTICES.md" "$APP/"
    echo "Built $APP/Symphone"
    if [ -n "$VERSION" ]; then
        mkdir -p "$ROOT/dist"
        tar -C dist -czf "$ROOT/dist/Symphone-$VERSION-linux-x64.tar.gz" Symphone
        sh "$ROOT/setup/linux/build-deb.sh" "$APP" "$VERSION" "$ROOT/dist"
    fi
    ;;
*)
    echo "build.sh is for macOS and Linux; use build.ps1 on Windows" >&2
    exit 1
    ;;
esac
