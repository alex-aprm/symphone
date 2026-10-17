# One-time elevated setup for Symphone. Run as Administrator.
# Installs: WinFsp (native fast drive), VB-CABLE (virtual mic), Quick Share for Windows,
# tunes WebClient (WebDAV fallback) and adds firewall rules for LAN discovery.
$ErrorActionPreference = 'Continue'
$log = Join-Path $PSScriptRoot 'admin-setup.log'
Start-Transcript -Path $log -Force | Out-Null

function Step($name, [scriptblock]$body) {
    Write-Host "==> $name"
    try { & $body } catch { Write-Host "    FAILED: $_" }
}

Step 'WinFsp' {
    winget install --id WinFsp.WinFsp -e --silent --accept-package-agreements --accept-source-agreements --scope machine
}

Step 'Quick Share from Google' {
    winget install --id Google.QuickShare -e --silent --accept-package-agreements --accept-source-agreements
}

Step 'VB-CABLE virtual audio device' {
    $tmp = Join-Path $env:TEMP 'vbcable'
    New-Item -ItemType Directory -Force $tmp | Out-Null
    $zip = Join-Path $tmp 'vbcable.zip'
    Invoke-WebRequest 'https://download.vb-audio.com/Download_CABLE/VBCABLE_Driver_Pack45.zip' -OutFile $zip -UseBasicParsing
    Expand-Archive $zip -DestinationPath $tmp -Force
    # Trust VB-Audio's driver publisher so the install doesn't stop on a prompt
    $cat = Get-ChildItem $tmp -Filter *.cat -Recurse | Select-Object -First 1
    if ($cat) {
        $cert = (Get-AuthenticodeSignature $cat.FullName).SignerCertificate
        if ($cert) {
            $store = New-Object System.Security.Cryptography.X509Certificates.X509Store('TrustedPublisher', 'LocalMachine')
            $store.Open('ReadWrite'); $store.Add($cert); $store.Close()
        }
    }
    $p = Start-Process (Join-Path $tmp 'VBCABLE_Setup_x64.exe') -ArgumentList '-i', '-h' -Wait -PassThru
    Write-Host "    VB-CABLE setup exit code $($p.ExitCode)"
}

Step 'WebClient (WebDAV fallback) tuning' {
    $k = 'HKLM:\SYSTEM\CurrentControlSet\Services\WebClient\Parameters'
    Set-ItemProperty $k FileSizeLimitInBytes 0xFFFFFFFF -Type DWord
    Set-ItemProperty $k BasicAuthLevel 2 -Type DWord
    Set-ItemProperty $k FileAttributesLimitInBytes 0x00989680 -Type DWord
    Set-Service WebClient -StartupType Automatic
    Restart-Service WebClient -Force
}

Step 'Firewall rules (private network)' {
    Get-NetFirewallRule -DisplayName 'PhoneSync*', 'Symphone*' -ErrorAction SilentlyContinue | Remove-NetFirewallRule
    New-NetFirewallRule -DisplayName 'Symphone discovery (UDP)' -Direction Inbound -Protocol UDP -LocalPort 47800 -Action Allow -Profile Private,Domain | Out-Null
    New-NetFirewallRule -DisplayName 'Symphone agent (TCP)' -Direction Inbound -Protocol TCP -LocalPort 47801 -Action Allow -Profile Private,Domain | Out-Null
    Get-NetConnectionProfile | Where-Object { $_.IPv4Connectivity -ne 'Disconnected' } | Format-Table Name, InterfaceAlias, NetworkCategory
}

Write-Host 'DONE'
Stop-Transcript | Out-Null
