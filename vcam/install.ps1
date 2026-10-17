# Installs the Symphone virtual camera (self-elevating).
#   powershell -ExecutionPolicy Bypass -File vcam\install.ps1 [-Name "Symphone Camera"] [-Uninstall] [-Log <file>]
param(
    [string]$Name = 'Symphone Camera',
    [switch]$Uninstall,
    [string]$Log = ''
)
$ErrorActionPreference = 'Stop'
$src  = Join-Path $PSScriptRoot 'bin'
$dest = 'C:\Program Files\PhoneSync\vcam'
if (-not $Log) { $Log = Join-Path $env:TEMP 'phonesync-vcam-install.log' }

$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    $argList = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', "`"$PSCommandPath`"", '-Name', "`"$Name`"", '-Log', "`"$Log`"")
    if ($Uninstall) { $argList += '-Uninstall' }
    Write-Host "Requesting elevation (log: $Log)..."
    $p = Start-Process powershell.exe -Verb RunAs -ArgumentList $argList -Wait -PassThru
    if (Test-Path $Log) { Get-Content $Log }
    exit $p.ExitCode
}

Start-Transcript -Path $Log -Force | Out-Null
$rc = 0
try {
    # log dir writable by Frame Server (LocalService) + apps
    $logDir = 'C:\ProgramData\PhoneSync'
    New-Item -ItemType Directory -Force $logDir | Out-Null
    & icacls $logDir /grant '*S-1-1-0:(OI)(CI)M' /T /C /Q | Out-Null

    if ($Uninstall) {
        if (Test-Path "$dest\PhoneSyncCam.exe") {
            & "$dest\PhoneSyncCam.exe" uninstall --name $Name
            $rc = $LASTEXITCODE
        } elseif (Test-Path "$src\PhoneSyncCam.exe") {
            & "$src\PhoneSyncCam.exe" uninstall --name $Name --keep-com
            $rc = $LASTEXITCODE
        }
        try { Remove-Item -Recurse -Force $dest } catch {
            Write-Host "Files in use, restarting Frame Server services..."
            Stop-Service FrameServerMonitor, FrameServer -Force -ErrorAction SilentlyContinue
            Remove-Item -Recurse -Force $dest -ErrorAction SilentlyContinue
            Start-Service FrameServer -ErrorAction SilentlyContinue
        }
        Write-Host "Uninstalled (rc=$rc)."
    } else {
        if (-not (Test-Path "$src\PhoneSyncVCam.dll")) { throw "Build first: $src\PhoneSyncVCam.dll missing (run vcam\build.ps1)" }
        New-Item -ItemType Directory -Force $dest | Out-Null
        $files = 'PhoneSyncVCam.dll', 'PhoneSyncCam.exe'
        try {
            foreach ($f in $files) { Copy-Item -Force (Join-Path $src $f) $dest }
        } catch {
            Write-Host "DLL in use, stopping Frame Server services to replace it..."
            Stop-Service FrameServerMonitor, FrameServer -Force -ErrorAction SilentlyContinue
            Start-Sleep -Seconds 2
            foreach ($f in $files) { Copy-Item -Force (Join-Path $src $f) $dest }
            Start-Service FrameServer -ErrorAction SilentlyContinue
        }
        # registers the COM source in HKLM and creates the System-lifetime virtual camera
        & "$dest\PhoneSyncCam.exe" install --name $Name
        $rc = $LASTEXITCODE
        & "$dest\PhoneSyncCam.exe" status
        Write-Host "Install finished (rc=$rc)."
    }
} catch {
    Write-Host "ERROR: $_"
    $rc = 1
} finally {
    Stop-Transcript | Out-Null
}
exit $rc

