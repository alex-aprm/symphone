#!/bin/sh
# dev helper: restart the PC app from source
powershell -NoProfile -Command "Get-CimInstance Win32_Process | ? { \$_.CommandLine -match 'phonesync' -and \$_.Name -match 'python' } | % { Stop-Process -Id \$_.ProcessId -Force }; Get-Process rclone,scrcpy -ErrorAction SilentlyContinue | Stop-Process -Force"
sleep 1
(PHONESYNC_EMULATOR=${PHONESYNC_EMULATOR:-} .venv/Scripts/python -m phonesync "$@" > "$LOCALAPPDATA/Symphone/logs/run.out" 2>&1 &)
