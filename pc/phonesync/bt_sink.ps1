# Opens a Bluetooth A2DP sink connection (PC acts as a Bluetooth speaker for a paired phone).
# Keeps running while the connection is wanted; the parent process terminates it to disconnect.
param([string]$Hint = "", [switch]$List)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Runtime.WindowsRuntime

$asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
$asTaskAction = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncAction' })[0]

function Await($op, [Type]$type) {
    $task = $asTaskGeneric.MakeGenericMethod($type).Invoke($null, @($op))
    $task.Wait(-1) | Out-Null
    $task.Result
}
function AwaitAction($op) {
    $task = $asTaskAction.Invoke($null, @($op))
    $task.Wait(-1) | Out-Null
}

$null = [Windows.Media.Audio.AudioPlaybackConnection, Windows.Media.Audio, ContentType = WindowsRuntime]
$null = [Windows.Devices.Enumeration.DeviceInformation, Windows.Devices.Enumeration, ContentType = WindowsRuntime]

$selector = [Windows.Media.Audio.AudioPlaybackConnection]::GetDeviceSelector()
$devices = Await ([Windows.Devices.Enumeration.DeviceInformation]::FindAllAsync($selector)) ([Windows.Devices.Enumeration.DeviceInformationCollection])

if ($List) {
    foreach ($d in $devices) { Write-Output "$($d.Name)`t$($d.Id)" }
    exit 0
}

$dev = $devices | Where-Object { $_.Name -like "*$Hint*" } | Select-Object -First 1
if (-not $dev) { Write-Output "NO_DEVICE"; exit 2 }

$conn = [Windows.Media.Audio.AudioPlaybackConnection]::TryCreateFromId($dev.Id)
AwaitAction ($conn.StartAsync())
$result = Await ($conn.OpenAsync()) ([Windows.Media.Audio.AudioPlaybackConnectionOpenResult])
Write-Output "OPEN $($result.Status) $($dev.Name)"
if ($result.Status -ne 'Success') { exit 3 }

while ($conn.State -eq 'Opened') { Start-Sleep -Seconds 2 }
Write-Output "CLOSED"
