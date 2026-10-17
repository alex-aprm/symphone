# Builds dist\Symphone\Symphone.exe (windowed) with the UI, scrcpy (patched server), rclone and svcl bundled.
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
$tools = "dist\Symphone\tools"
New-Item -ItemType Directory -Force $tools | Out-Null
$root = Resolve-Path ..\tools
Copy-Item -Recurse -Force (Get-ChildItem $root -Directory -Filter 'scrcpy-win64-*').FullName $tools
Copy-Item -Recurse -Force (Get-ChildItem $root -Directory -Filter 'rclone-*').FullName $tools
Copy-Item -Recurse -Force "$root\svcl" $tools
$sc = "dist\Symphone\sidecar\bin"
New-Item -ItemType Directory -Force $sc | Out-Null
Copy-Item -Force (Join-Path (Resolve-Path ..\sidecar\bin) '*.exe') $sc
Copy-Item -Force ..\dist\Symphone.apk $tools     # the PC installs/updates the phone app from this copy
Get-ChildItem "$tools\scrcpy-win64-*" -Filter 'scrcpy-server.orig' | Remove-Item
Write-Host "Built dist\Symphone\Symphone.exe"
