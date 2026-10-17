# Optional Symphone extras that need administrator rights. Started elevated by the app's setup wizard
# (or run it yourself as administrator). Pick any of:
#   -Drive        WinFsp: the phone's storage as a native drive in Explorer (plus Explorer/WebDAV tuning)
#   -Microphone   VB-CABLE: the phone's microphone as a Windows microphone
#   -SideDisplay  Virtual Display Driver: the phone as a second monitor
#   -Webcam       the phone's camera as a Windows webcam
# Always: firewall rules so phones on your private network can find this PC.
param([switch]$Drive, [switch]$Microphone, [switch]$SideDisplay, [switch]$Webcam)
$ErrorActionPreference = 'Continue'
$here = $PSScriptRoot
$app = Split-Path $here -Parent
$log = Join-Path $env:TEMP 'symphone-extras.log'
Start-Transcript -Path $log -Force | Out-Null

function Step($name, [scriptblock]$body) {
    Write-Host "==> $name"
    try { & $body } catch { Write-Host "    FAILED: $_" }
}

Step 'Firewall rules (private networks)' {
    Get-NetFirewallRule -DisplayName 'PhoneSync*', 'Symphone*' -ErrorAction SilentlyContinue | Remove-NetFirewallRule
    New-NetFirewallRule -DisplayName 'Symphone discovery (UDP)' -Direction Inbound -Protocol UDP -LocalPort 47800 -Action Allow -Profile Private,Domain | Out-Null
    New-NetFirewallRule -DisplayName 'Symphone agent (TCP)' -Direction Inbound -Protocol TCP -LocalPort 47801 -Action Allow -Profile Private,Domain | Out-Null
}

if ($Drive) {
    Step 'WinFsp (phone storage in Explorer)' {
        winget install --id WinFsp.WinFsp -e --silent --accept-package-agreements --accept-source-agreements --scope machine
    }
}

if ($Microphone) {
    Step 'VB-CABLE (virtual microphone)' {
        $tmp = Join-Path $env:TEMP 'vbcable'
        New-Item -ItemType Directory -Force $tmp | Out-Null
        $zip = Join-Path $tmp 'vbcable.zip'
        Invoke-WebRequest 'https://download.vb-audio.com/Download_CABLE/VBCABLE_Driver_Pack45.zip' -OutFile $zip -UseBasicParsing
        Expand-Archive $zip -DestinationPath $tmp -Force
        $cat = Get-ChildItem $tmp -Filter *.cat -Recurse | Select-Object -First 1
        if ($cat) {
            $cert = (Get-AuthenticodeSignature $cat.FullName).SignerCertificate
            if ($cert) {
                $store = New-Object System.Security.Cryptography.X509Certificates.X509Store('TrustedPublisher', 'LocalMachine')
                $store.Open('ReadWrite'); $store.Add($cert); $store.Close()
            }
        }
        $p = Start-Process (Join-Path $tmp 'VBCABLE_Setup_x64.exe') -ArgumentList '-i', '-h' -Wait -PassThru
        Write-Host "    exit code $($p.ExitCode)"
    }
}

if ($SideDisplay) {
    Step 'Virtual Display Driver (side display)' {
        & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $app 'sidecar\driver\install.ps1')
    }
}

if ($Webcam) {
    Step 'Windows webcam' {
        & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $app 'vcam\install.ps1')
    }
}

Write-Host 'DONE'
Stop-Transcript | Out-Null
Start-Sleep 2
