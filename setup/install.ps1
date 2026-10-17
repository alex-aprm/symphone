# Installs Symphone for the current user: copies the app to %LOCALAPPDATA%\Programs\Symphone, installs the
# Android app on a USB-connected phone, and starts Symphone (which creates the Start menu entry, Explorer
# "Send to > <phone>" and autostart on first run).
#
# Works from the repo (after pc\build.ps1) and from an extracted release zip (this script next to Symphone.exe).
param([switch]$SkipPhone)
$ErrorActionPreference = 'Stop'
if (Test-Path (Join-Path $PSScriptRoot 'Symphone.exe')) {
    $src = $PSScriptRoot                                           # release zip
    $apk = Join-Path $src 'tools\Symphone.apk'
} else {
    $root = Split-Path $PSScriptRoot -Parent                       # repo checkout
    $src = Join-Path $root 'pc\dist\Symphone'
    $apk = Join-Path $root 'dist\Symphone.apk'
}
$dst = Join-Path $env:LOCALAPPDATA 'Programs\Symphone'
if (-not (Test-Path "$src\Symphone.exe")) { throw "Build first: pc\build.ps1" }

# PhoneSync is the app's former name: stop and remove that install too (settings move over on first start).
Get-Process Symphone, PhoneSync, rclone, scrcpy -ErrorAction SilentlyContinue | Stop-Process -Force
Start-Sleep 1
$old = Join-Path $env:LOCALAPPDATA 'Programs\PhoneSync'
if (Test-Path $old) { Remove-Item -Recurse -Force $old -ErrorAction SilentlyContinue }
New-Item -ItemType Directory -Force $dst | Out-Null
robocopy $src $dst /MIR /NFL /NDL /NJH /NJS /NP | Out-Null

if (-not $SkipPhone) {
    $adb = Get-ChildItem "$dst\tools\scrcpy-win64-*\adb.exe" | Select-Object -First 1
    if ((Test-Path $apk) -and $adb) {
        & $adb.FullName -d install -r -g $apk
        # Updated apps start "stopped"; nubia's AutoLaunch also blocks background starts. Whitelist and start the
        # foreground service right away (otherwise Android may freeze the cached process until the PC heals it).
        & $adb.FullName -d shell "dumpsys AutoLaunch -wl dev.phonesync; dumpsys AutoLaunch -a dev.phonesync; cmd notification allow_listener dev.phonesync/dev.phonesync.NotifListener; am start-foreground-service -n dev.phonesync/.SyncService; sleep 1; cmd activity unfreeze --sticky dev.phonesync" | Out-Null
    }
}
Start-Process "$dst\Symphone.exe"
Write-Host "Installed to $dst"
