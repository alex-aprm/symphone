# Third-party components

Symphone builds include or download the following components. Each keeps its own license; the Symphone license
(LICENSE) does not apply to them.

| Component | Used for | License |
|---|---|---|
| [scrcpy](https://github.com/Genymobile/scrcpy) 5.0.1 (Genymobile) | Phone screen and app windows, audio. The server is built from upstream with `scrcpy/symphone-server.patch` applied. | Apache License 2.0 |
| Android Debug Bridge (`adb`, shipped with scrcpy) | Device connection | Apache License 2.0 |
| [rclone](https://rclone.org) | Phone storage in Explorer / Finder / Files | MIT |
| [WinFsp](https://winfsp.dev) (installed by `setup/admin-setup.ps1`) | Explorer mount | GPLv3 with FLOSS exception |
| [Virtual Display Driver](https://github.com/VirtualDrivers/Virtual-Display-Driver) (`sidecar/driver/VirtualDisplayDriver`) | Side display | MIT |
| [SoundVolumeCommandLine (svcl)](https://www.nirsoft.net/utils/sound_volume_command_line.html), NirSoft | Routing an app's audio to VB-CABLE | Freeware |
| [VB-CABLE](https://vb-audio.com/Cable/) (downloaded by `setup/admin-setup.ps1`) | Virtual microphone | Donationware |
| [ZXing core](https://github.com/zxing/zxing) | QR pairing in the Android app | Apache License 2.0 |
| [pywebview](https://pywebview.flowrl.com), [aiohttp](https://github.com/aio-libs/aiohttp), [pystray](https://github.com/moses-palmer/pystray), [Pillow](https://python-pillow.org), [windows-toasts](https://github.com/DatGuy1/Windows-Toasts), [PyAudioWPatch](https://github.com/s0d3s/PyAudioWPatch), [PyAV](https://github.com/PyAV-Org/PyAV), [segno](https://github.com/heuer/segno), [bleak](https://github.com/hbldh/bleak), [cryptography](https://github.com/pyca/cryptography), [PyWinRT](https://github.com/pywinrt/pywinrt), [PyInstaller](https://pyinstaller.org) | Desktop app | BSD / MIT / Apache / LGPL (see each project) |
| [desktop-notifier](https://github.com/samschott/desktop-notifier), [sounddevice](https://github.com/spatialaudio/python-sounddevice) + PortAudio, [pynput](https://github.com/moses-palmer/pynput), [PyQt6](https://www.riverbankcomputing.com/software/pyqt/) + Qt 6 / Qt WebEngine, [QtPy](https://github.com/spyder-ide/qtpy) | macOS and Linux app | MIT / LGPL / GPLv3 (PyQt6); see each project |
| [Material Symbols](https://github.com/google/material-design-icons) (subset in `pc/phonesync/ui/icons.woff2`) | Icons on macOS and Linux | Apache License 2.0 |
| Phone photos shown as the device picture | Fetched at run time from [Wikimedia Commons](https://commons.wikimedia.org); the author and license of the photo in use are shown in the app | Per photo (e.g. CC BY-SA) |
