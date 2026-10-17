# Builds dist\Symphone (Symphone.exe, windowed) with the UI, scrcpy (patched server), rclone, svcl, the side display
# helpers, the virtual camera (when built), the setup scripts and the phone app - the same folder the installer and
# the release zip ship.
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
$py = ".\.venv\Scripts\python.exe"
& $py -m PyInstaller --noconfirm --clean --windowed --name Symphone `
    --icon phonesync\ui\icon.ico `
    --add-data "phonesync\ui;ui" `
    --add-data "phonesync\bt_sink.ps1;." `
    --collect-all windows_toasts --collect-all winrt --collect-all bleak --collect-submodules segno `
    --collect-submodules webview --collect-submodules pyaudiowpatch `
    --distpath dist --workpath build `
    Symphone.pyw
$app = "dist\Symphone"
$tools = "$app\tools"
New-Item -ItemType Directory -Force $tools | Out-Null
$root = Resolve-Path ..\tools
Copy-Item -Recurse -Force (Get-ChildItem $root -Directory -Filter 'scrcpy-win64-*').FullName $tools
Copy-Item -Recurse -Force (Get-ChildItem $root -Directory -Filter 'rclone-*').FullName $tools
Copy-Item -Recurse -Force "$root\svcl" $tools
Copy-Item -Force ..\dist\Symphone.apk $tools     # the PC installs/updates the phone app from this copy
Get-ChildItem "$tools\scrcpy-win64-*" -Filter 'scrcpy-server.orig' | Remove-Item

# side display: native helpers + driver package
New-Item -ItemType Directory -Force "$app\sidecar\bin", "$app\sidecar\driver" | Out-Null
Copy-Item -Force (Join-Path (Resolve-Path ..\sidecar\bin) '*.exe') "$app\sidecar\bin"
Copy-Item -Recurse -Force ..\sidecar\driver\* "$app\sidecar\driver" -Exclude '*.log'

# virtual camera (built by vcam\build.ps1; optional locally)
New-Item -ItemType Directory -Force "$app\vcam" | Out-Null
Copy-Item -Force ..\vcam\install.ps1 "$app\vcam"
if (Test-Path ..\vcam\bin) { Copy-Item -Recurse -Force ..\vcam\bin "$app\vcam" }

# setup scripts (extras.ps1 is started elevated by the app's setup wizard), docs
New-Item -ItemType Directory -Force "$app\setup" | Out-Null
Copy-Item -Force ..\setup\extras.ps1, ..\setup\admin-setup.ps1 "$app\setup"
Copy-Item -Force ..\setup\install.ps1 $app
Copy-Item -Force ..\LICENSE, ..\THIRD_PARTY_NOTICES.md $app
Write-Host "Built dist\Symphone\Symphone.exe"
