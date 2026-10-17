# Installs the Virtual Display Driver (VirtualDrivers/Virtual-Display-Driver 25.7.23, SignPath-signed IddCx driver)
# used by Symphone Sidecar. Must run elevated. Log: install.log next to this script.
#   powershell -ExecutionPolicy Bypass -File install.ps1 [-Uninstall]
param([switch]$Uninstall)
$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$log = Join-Path $here 'install.log'
Start-Transcript -Path $log -Force | Out-Null
try {
    $vddctl = Join-Path $here '..\bin\vddctl.exe'
    $drv = Join-Path $here 'VirtualDisplayDriver'
    if ($Uninstall) {
        & $vddctl uninstall
        Get-WindowsDriver -Online | Where-Object { $_.OriginalFileName -like '*mttvdd.inf' } | ForEach-Object {
            pnputil /delete-driver $_.Driver /uninstall /force
        }
        return
    }
    $cat = Join-Path $drv 'mttvdd.cat'
    $sig = Get-AuthenticodeSignature $cat
    if ($sig.Status -ne 'Valid') { throw "driver catalog signature is $($sig.Status)" }
    foreach ($f in 'MttVDD.dll') {
        $s = Get-AuthenticodeSignature (Join-Path $drv $f)
        if ($s.Status -ne 'Valid') { throw "$f signature is $($s.Status)" }
    }
    # Trust the publisher so the PnP install does not stop on a "Would you like to install this device software" prompt.
    $store = New-Object System.Security.Cryptography.X509Certificates.X509Store('TrustedPublisher', 'LocalMachine')
    $store.Open('ReadWrite'); $store.Add($sig.SignerCertificate); $store.Close()
    Write-Host "Trusted publisher: $($sig.SignerCertificate.Subject)"

    # Driver settings: phone-native modes only (2688x1216 panel, landscape + portrait + half resolution).
    $cfgDir = 'C:\VirtualDisplayDriver'
    New-Item -ItemType Directory -Force $cfgDir | Out-Null
    Copy-Item (Join-Path $here 'vdd_settings.xml') (Join-Path $cfgDir 'vdd_settings.xml') -Force
    Write-Host "Settings written to $cfgDir"

    & $vddctl install (Join-Path $drv 'MttVDD.inf')
    if ($LASTEXITCODE -ne 0) { throw "vddctl install failed ($LASTEXITCODE)" }
    Start-Sleep 3
    Get-PnpDevice -Class Display | Format-Table -AutoSize Status, FriendlyName, InstanceId | Out-String | Write-Host
    Write-Host 'DONE'
} catch {
    Write-Host "ERROR: $_"
} finally {
    Stop-Transcript | Out-Null
}
