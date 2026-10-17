# Time-boxed sidecar_host test: logs to sidecar\progress.log first, kills the process after -Timeout seconds,
# optionally attaches the virtual monitor for the test and always detaches it afterwards.
#   powershell -File run_host_boxed.ps1 -Label "primary sw" -HostArgs "--primary --out 640x360 --sw --max-seconds 4" [-Virtual "2688 1216 60"] [-Timeout 8] [-Anim anim.py]
param([string]$Label, [string]$HostArgs, [string]$Virtual = "", [int]$Timeout = 8, [string]$OutDir = $env:TEMP, [string]$Anim = "")
$root = 'C:\repos\sync\sidecar'
$bin = "$root\bin"
$py = 'C:\repos\sync\pc\.venv\Scripts\python.exe'
$log = "$root\progress.log"
function Note($m) { Add-Content -Path $log -Value ("{0} [boxed] {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $m) }
$out = Join-Path $OutDir 'host_out.bin'; $err = Join-Path $OutDir 'host_err.txt'
try {
    if ($Virtual) {
        Note "attach virtual monitor $Virtual for '$Label'"
        & "$bin\vddctl.exe" attach $Virtual.Split(' ') | Out-Null
        Start-Sleep -Seconds 3   # let the mode set settle before touching the GPU again
        $info = & "$bin\vddctl.exe" list | ConvertFrom-Json
        if (-not $info.active) { throw "virtual monitor not active" }
        $HostArgs = "--gdi $($info.gdi) $HostArgs"
        if ($Anim) {
            Start-Process $py -ArgumentList $Anim, ($info.x + 100), ($info.y + 100), ($Timeout - 1) | Out-Null
            Start-Sleep 1
        }
    }
    Note "RUN sidecar_host $HostArgs ($Label, timeout ${Timeout}s)"
    $p = Start-Process "$bin\sidecar_host.exe" -ArgumentList "$HostArgs --ignore-stdin" -RedirectStandardOutput $out -RedirectStandardError $err -PassThru -NoNewWindow
    $null = $p.Handle
    if (-not $p.WaitForExit($Timeout * 1000)) { Note "timeout -> kill"; Stop-Process -Id $p.Id -Force; $rc = "killed" } else { $rc = $p.ExitCode }
    Note "done rc=$rc"
    "rc=$rc"
    Get-Content $err
} finally {
    if ($Virtual) { & "$bin\vddctl.exe" detach | Out-Null; Note "detached virtual monitor" }
}
